"""Milestone 3 model selection, following config/milestone3.json.

1. Tune each candidate on expanding season folds inside the training seasons.
2. Refit each candidate on all training seasons and score it on validation.
3. Apply the declared rule: a more complex model replaces a simpler one only
   if it is better with a 95% paired interval entirely below zero.
4. Freeze the choice in reports/frozen_selection.json for the final test.

The final test season is never read here.
"""
import hashlib
import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .loading import read_json
from .metrics import CLASSES, calibration_table, confusion_matrix, match_log_loss, paired_difference, summarize
from .models import FEATURES, Dataset, fit, load_dataset, predict, save_artifact

BASELINE = "class_frequency_baseline"
FINAL_RESULT = Path("reports") / "final_test.json"
FROZEN_SELECTION = Path("reports") / "frozen_selection.json"


class ProtocolError(RuntimeError):
    """The evaluation rules were broken or changed; nothing was run."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def split_sha256(split_config: dict) -> str:
    return hashlib.sha256(json.dumps(split_config["split"], sort_keys=True).encode()).hexdigest()


def load_protocol(root: Path) -> tuple[dict, dict, str]:
    split = read_json(root / "config" / "milestone0.json")
    path = root / "config" / "milestone3.json"
    protocol = read_json(path)

    train = set(split["split"]["train"])
    for fold in protocol["tuning"]["folds"]:
        seasons = set(fold["train"]) | {fold["evaluate"]}
        if not seasons <= train:
            raise ProtocolError(f"tuning fold {fold} uses seasons outside training: {sorted(seasons - train)}")
        if fold["evaluate"] in fold["train"] or max(fold["train"]) >= fold["evaluate"]:
            raise ProtocolError(f"tuning fold {fold} must evaluate on a season after its training seasons")
    order = protocol["selection"]["order"]
    if order[0] != BASELINE or set(order[1:]) != set(protocol["candidates"]):
        raise ProtocolError("selection order must start with the baseline and list every candidate once")
    return split, protocol, file_sha256(path)


def grid(spec: dict) -> list[dict]:
    keys = list(spec)
    return [dict(zip(keys, values)) for values in itertools.product(*(spec[k] for k in keys))]


def fold_log_loss(name: str, settings: dict, seed: int, data: Dataset, fold: dict) -> float:
    evaluate = data.where([fold["evaluate"]])
    P = predict(fit(name, settings, seed, data.where(fold["train"])), evaluate)
    return float(match_log_loss(P, evaluate.y).mean())


def tune(name: str, spec: dict, folds: list[dict], data: Dataset, seed: int) -> dict:
    results = []
    for params in grid(spec["grid"]):
        scores = [fold_log_loss(name, {**spec["fixed"], **params}, seed, data, fold) for fold in folds]
        results.append({"settings": params, "fold_log_loss": scores, "mean_log_loss": float(np.mean(scores))})
    best = min(results, key=lambda r: r["mean_log_loss"])
    return {"results": results, "chosen": best["settings"], "chosen_mean_log_loss": best["mean_log_loss"]}


def apply_selection_rule(order: list[str], losses: dict, reps: int, seed: int, level: float) -> tuple[str, list]:
    choice, trace = order[0], []
    for name in order[1:]:
        difference = paired_difference(losses[name], losses[choice], reps, seed, level)
        replaces = difference["interval"][1] < 0
        trace.append({"candidate": name, "compared_with": choice, "difference": difference, "replaces": replaces})
        if replaces:
            choice = name
    return choice, trace


def _segment(losses: np.ndarray, mask: np.ndarray) -> dict:
    return {"matches": int(mask.sum()), "log_loss": float(losses[mask].mean()) if mask.any() else None}


def run_selection(conn, root: Path) -> dict:
    if (root / FINAL_RESULT).exists():
        raise ProtocolError("the final test has already been run, so selection is closed; changing the "
                            "model now would be choosing it with the test season")
    split, protocol, protocol_sha = load_protocol(root)
    seed, boot = protocol["random_seed"], protocol["bootstrap"]
    reps, level = boot["reps"], boot["interval"]
    train_seasons, validation_seasons = split["split"]["train"], split["split"]["validation"]

    data = load_dataset(conn, split, protocol["feature_version"], train_seasons + validation_seasons)
    train, validation = data.where(train_seasons), data.where(validation_seasons)

    tuning = {name: tune(name, spec, protocol["tuning"]["folds"], data, seed)
              for name, spec in protocol["candidates"].items()}
    tuning[BASELINE] = {"results": [], "chosen": {}, "chosen_mean_log_loss": float(np.mean(
        [fold_log_loss(BASELINE, {}, seed, data, fold) for fold in protocol["tuning"]["folds"]]))}
    settings = {BASELINE: {}, **{name: {**spec["fixed"], **tuning[name]["chosen"]}
                                 for name, spec in protocol["candidates"].items()}}

    order = protocol["selection"]["order"]
    models, forecasts, losses, scores = {}, {}, {}, {}
    for name in order:
        models[name] = fit(name, settings[name], seed, train)
        forecasts[name] = predict(models[name], validation)
        losses[name] = match_log_loss(forecasts[name], validation.y)
        scores[name] = summarize(forecasts[name], validation.y, reps, seed)
        if name != BASELINE:
            scores[name]["vs_baseline"] = paired_difference(losses[name], losses[BASELINE], reps, seed, level)

    selected, trace = apply_selection_rule(order, losses, reps, seed, level)

    short = np.minimum(validation.column("home_games_last5"), validation.column("away_games_last5")) < 5
    segments = {name: {"fewer_than_5_games_either_side": _segment(losses[name], short),
                       "5_or_more_games_both_sides": _segment(losses[name], ~short)} for name in order}

    coefficients = None
    if "logistic_regression" in models:
        coef = models["logistic_regression"].named_steps["model"].coef_
        home_minus_away = coef[CLASSES.index("H")] - coef[CLASSES.index("A")]
        coefficients = sorted(({"feature": f, "home_win_minus_away_win": float(c)}
                               for f, c in zip(FEATURES, home_minus_away)),
                              key=lambda r: -abs(r["home_win_minus_away_win"]))

    later = [s for s in train_seasons if s != "2020-21"]
    sensitivity = {}
    for name in protocol["candidates"]:
        P = predict(fit(name, settings[name], seed, data.where(later)), validation)
        sensitivity[name] = {"training_seasons": later,
                             "log_loss": float(match_log_loss(P, validation.y).mean())}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    artifacts = {}
    for name in order:
        artifacts[name] = save_artifact(models[name], root / "artifacts" / "validation" / name, {
            "model": name, "settings": settings[name], "purpose": "validation scoring",
            "feature_version": protocol["feature_version"], "training_seasons": train_seasons,
            "training_matches": int(len(train.y)), "source_commit": split["source_commit"]})

    fit_data = data  # training plus validation: what the final model will be refitted on
    frozen = {
        "selected_model": selected, "settings": settings[selected], "selected_at_utc": now,
        "feature_version": protocol["feature_version"], "protocol_sha256": protocol_sha,
        "split_sha256": split_sha256(split),
        "final_fit_seasons": train_seasons + validation_seasons,
        "final_fit_data_fingerprint": fit_data.fingerprint(),
        "test_seasons": split["split"]["test"], "random_seed": seed,
        "baseline_settings": settings[BASELINE],
    }
    result = {
        "generated_at_utc": now, "source_commit": split["source_commit"],
        "protocol_sha256": protocol_sha, "class_order": list(CLASSES), "features": FEATURES,
        "training": {"seasons": train_seasons, "matches": int(len(train.y))},
        "validation": {"seasons": validation_seasons, "matches": int(len(validation.y)),
                       "counts": {c: int((validation.y == i).sum()) for i, c in enumerate(CLASSES)}},
        "tuning": tuning, "settings": settings, "scores": scores,
        "reference_uniform_log_loss": math.log(3),
        "selection": {"rule": protocol["selection"]["rule"], "trace": trace, "selected": selected},
        "selected_analysis": {
            "calibration": {c: calibration_table(forecasts[selected], validation.y, i) for i, c in enumerate(CLASSES)},
            "confusion_matrix": confusion_matrix(forecasts[selected], validation.y)},
        "segments": segments, "logistic_coefficients": coefficients,
        "sensitivity_without_2020_21": sensitivity,
        "artifacts": {name: f"artifacts/validation/{name}" for name in artifacts},
        "final_test_season": {"seasons": split["split"]["test"], "used": False},
    }

    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    (root / FROZEN_SELECTION).write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    (reports / "model_validation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (reports / "model_validation.md").write_text(validation_markdown(result), encoding="utf-8")
    return result


NAMES = {BASELINE: "Class-frequency baseline", "logistic_regression": "Logistic regression", "xgboost": "XGBoost"}


def _settings_text(settings: dict, name: str, protocol_fixed: tuple = ("max_iter", "tree_method", "n_jobs")) -> str:
    shown = {k: v for k, v in settings.items() if k not in protocol_fixed}
    if name == "xgboost":
        shown = {k: settings[k] for k in ("max_depth", "n_estimators") if k in settings}
    return ", ".join(f"{k}={v}" for k, v in shown.items()) or "none"


def _interval(r: dict, key: str = "log_loss") -> str:
    low, high = r[f"{key}_95ci"]
    return f"{r[key]:.4f} ({low:.4f} to {high:.4f})"


def _difference(d: dict) -> str:
    return f"{d['mean']:+.4f} ({d['interval'][0]:+.4f} to {d['interval'][1]:+.4f})"


def validation_markdown(r: dict) -> str:
    s, order = r["scores"], [step["compared_with"] for step in r["selection"]["trace"][:1]] + \
        [step["candidate"] for step in r["selection"]["trace"]]
    v = r["validation"]
    lines = [
        "Model selection — validation report",
        "===================================",
        "",
        f"Generated {r['generated_at_utc']} from OpenFootball commit {r['source_commit']}.",
        f"Protocol: config/milestone3.json (sha256 {r['protocol_sha256'][:12]}…), written before any model was scored.",
        f"The final test season ({', '.join(r['final_test_season']['seasons'])}) was not used.",
        "",
        f"Training: {', '.join(r['training']['seasons'])} ({r['training']['matches']} matches). "
        f"Validation: {', '.join(v['seasons'])} ({v['matches']} matches: {v['counts']['H']} home wins, "
        f"{v['counts']['D']} draws, {v['counts']['A']} away wins).",
        "",
        "Log loss: lower is better; one third each scores "
        f"{r['reference_uniform_log_loss']:.4f}. Ranges are 95% bootstrap intervals. "
        "\"Versus baseline\" is the paired difference per match; negative means better than the baseline.",
        "",
        "| Model | Settings (tuned on training folds) | Tuning folds log loss | Validation log loss | Versus baseline | Accuracy |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name in order:
        vs = _difference(s[name]["vs_baseline"]) if "vs_baseline" in s[name] else "—"
        lines.append(f"| {NAMES[name]} | {_settings_text(r['settings'][name], name)} | "
                     f"{r['tuning'][name]['chosen_mean_log_loss']:.4f} | {_interval(s[name])} | {vs} | "
                     f"{s[name]['accuracy']:.1%} |")

    lines += ["", "**Selection, applying the declared rule**", "", f"Rule: {r['selection']['rule']}", ""]
    for step in r["selection"]["trace"]:
        verdict = "replaces it" if step["replaces"] else "does not replace it"
        lines.append(f"- {NAMES[step['candidate']]} versus {NAMES[step['compared_with']]}: "
                     f"{_difference(step['difference'])}, so it {verdict}.")
    selected = r["selection"]["selected"]
    lines += ["", f"**Selected: {NAMES[selected]}**, frozen in reports/frozen_selection.json.", ""]

    lines += ["**Draws**", "",
              "| Model | Mean draw forecast | Actual draw share | Times a draw was most likely | Log loss on draws | Log loss on other results |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name in order:
        d = s[name]["draws"]
        lines.append(f"| {NAMES[name]} | {d['mean_forecast']:.1%} | {d['observed']:.1%} | {d['times_most_likely']} | "
                     f"{d['log_loss_on_draws']:.4f} | {d['log_loss_on_others']:.4f} |")
    lines += ["", "Draws are rarely the single most likely outcome, so accuracy hides how a model handles them;",
              "log loss still rewards giving draws a sensible probability.", ""]

    lines += ["**Short history**", "",
              "| Model | Either side under 5 earlier games | Both sides 5 or more |",
              "| --- | ---: | ---: |"]
    for name in order:
        a, b = r["segments"][name].values()
        lines.append(f"| {NAMES[name]} | {a['log_loss']:.4f} ({a['matches']} matches) | {b['log_loss']:.4f} ({b['matches']} matches) |")
    lines += ["", "The short-history group is small, so differences there are weak evidence.", ""]

    lines += [f"**Calibration of the selected model ({NAMES[selected]})**", "",
              "Matches grouped by the probability given to each outcome. Observed share comes with a 95% interval;",
              "bins with fewer than 20 matches are marked sparse.", ""]
    for c, label in zip(CLASSES, ("Home win", "Draw", "Away win")):
        lines += [f"{label}:", "", "| Forecast bin | Matches | Mean forecast | Observed | 95% interval |",
                  "| --- | ---: | ---: | ---: | --- |"]
        for row in r["selected_analysis"]["calibration"][c]:
            low, high = row["observed_95ci"]
            flag = " (sparse)" if row["sparse"] else ""
            lines.append(f"| {row['bin'][0]:.0%}–{row['bin'][1]:.0%} | {row['matches']}{flag} | "
                         f"{row['mean_forecast']:.1%} | {row['observed']:.1%} | {low:.0%}–{high:.0%} |")
        lines.append("")

    cm = r["selected_analysis"]["confusion_matrix"]
    lines += [f"Most likely outcome versus what happened ({NAMES[selected]}):", "",
              "| Happened \\ Most likely | H | D | A |", "| --- | ---: | ---: | ---: |"]
    for c, row in zip(CLASSES, cm):
        lines.append(f"| {c} | {row[0]} | {row[1]} | {row[2]} |")
    lines.append("")

    if r["logistic_coefficients"]:
        lines += ["**What logistic regression learned**", "",
                  "Coefficient for home win minus coefficient for away win, on standardized features.",
                  "Positive pushes towards a home win. Features overlap heavily (last 5 sits inside last 10),",
                  "so read the signs and rough sizes, not individual values.", "",
                  "| Feature | Home win minus away win |", "| --- | ---: |"]
        for row in r["logistic_coefficients"][:8]:
            lines.append(f"| {row['feature']} | {row['home_win_minus_away_win']:+.3f} |")
        lines.append("")

    lines += ["**Sensitivity: training without 2020-21 (not used for selection)**", "",
              "| Model | Validation log loss, all four seasons | Without 2020-21 |", "| --- | ---: | ---: |"]
    for name, row in r["sensitivity_without_2020_21"].items():
        lines.append(f"| {NAMES[name]} | {s[name]['log_loss']:.4f} | {row['log_loss']:.4f} |")
    lines += ["", "**Tuning detail**", ""]
    for name in order[1:]:
        lines += [f"{NAMES[name]}: mean log loss over the three training folds for each setting.", "",
                  "| Settings | Mean fold log loss |", "| --- | ---: |"]
        for row in r["tuning"][name]["results"]:
            mark = " ← chosen" if row["settings"] == r["tuning"][name]["chosen"] else ""
            lines.append(f"| {', '.join(f'{k}={v}' for k, v in row['settings'].items())} | {row['mean_log_loss']:.4f}{mark} |")
        lines.append("")
    return "\n".join(lines)
