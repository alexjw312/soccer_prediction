"""The one-time final test on the reserved season.

This is the only code that reads the final test season's results. It runs
once: it needs an explicit confirmation, the frozen selection, an unchanged
protocol and unchanged training data, and it refuses to run a second time.
Whatever the result, it is reported. Changing the model afterwards would mean
choosing it with the test season, so selection is closed once this has run.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .metrics import CLASSES, calibration_table, confusion_matrix, match_log_loss, paired_difference, summarize
from .model_selection import (BASELINE, FINAL_RESULT, FROZEN_SELECTION, NAMES, ProtocolError, _difference,
                              _interval, load_protocol, split_sha256)
from .models import fit, load_dataset, predict, save_artifact


def evaluate_final(conn, root: Path, *, confirm: bool) -> dict:
    if not confirm:
        raise ProtocolError("the final test runs only once; rerun with --confirm-final-test to run it")
    if (root / FINAL_RESULT).exists():
        raise ProtocolError("the final test has already been run; its result is in reports/final_test.md")
    frozen_path = root / FROZEN_SELECTION
    if not frozen_path.exists():
        raise ProtocolError("no frozen selection; run select-model first")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))

    split, protocol, protocol_sha = load_protocol(root)
    if protocol_sha != frozen["protocol_sha256"] or split_sha256(split) != frozen["split_sha256"]:
        raise ProtocolError("the protocol or the season split changed after the model was selected")

    fit_data = load_dataset(conn, split, frozen["feature_version"], frozen["final_fit_seasons"])
    if fit_data.fingerprint() != frozen["final_fit_data_fingerprint"]:
        raise ProtocolError("training or validation data changed after the model was selected")
    test = load_dataset(conn, split, frozen["feature_version"], frozen["test_seasons"], allow_test=True)

    seed, boot = frozen["random_seed"], protocol["bootstrap"]
    selected = frozen["selected_model"]
    entries = [(selected, frozen["settings"])] + ([(BASELINE, frozen["baseline_settings"])] if selected != BASELINE else [])
    forecasts, losses, scores = {}, {}, {}
    for name, settings in entries:
        model = fit(name, settings, seed, fit_data)
        forecasts[name] = predict(model, test)
        losses[name] = match_log_loss(forecasts[name], test.y)
        scores[name] = summarize(forecasts[name], test.y, boot["reps"], seed)
        save_artifact(model, root / "artifacts" / "final" / name, {
            "model": name, "settings": settings, "purpose": "final test scoring",
            "feature_version": frozen["feature_version"], "training_seasons": frozen["final_fit_seasons"],
            "training_matches": int(len(fit_data.y)), "source_commit": split["source_commit"]})

    result = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selected_model": selected, "settings": frozen["settings"],
        "fit_seasons": frozen["final_fit_seasons"], "fit_matches": int(len(fit_data.y)),
        "test": {"seasons": frozen["test_seasons"], "matches": int(len(test.y)),
                 "counts": {c: int((test.y == i).sum()) for i, c in enumerate(CLASSES)}},
        "scores": scores, "reference_uniform_log_loss": math.log(3),
        "selected_vs_baseline": (paired_difference(losses[selected], losses[BASELINE], boot["reps"], seed,
                                                   boot["interval"]) if selected != BASELINE else None),
        "selected_analysis": {
            "calibration": {c: calibration_table(forecasts[selected], test.y, i) for i, c in enumerate(CLASSES)},
            "confusion_matrix": confusion_matrix(forecasts[selected], test.y)},
        "frozen_selection": frozen,
    }
    # "x" creates the file and fails if it already exists: the one-run lock.
    with (root / FINAL_RESULT).open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    (root / "reports" / "final_test.md").write_text(final_markdown(result), encoding="utf-8")
    return result


def final_markdown(r: dict) -> str:
    t, selected = r["test"], r["selected_model"]
    lines = [
        "Final test — one-time evaluation",
        "================================",
        "",
        f"Run {r['run_at_utc']}. This result is reported as it came out and was not used to change anything.",
        "",
        f"Selected model, frozen before this run: {NAMES[selected]}. Refitted on {', '.join(r['fit_seasons'])} "
        f"({r['fit_matches']} matches) with the frozen settings.",
        f"Test: {', '.join(t['seasons'])}, {t['matches']} matches ({t['counts']['H']} home wins, "
        f"{t['counts']['D']} draws, {t['counts']['A']} away wins).",
        "",
        "| Forecast | Test log loss (95% interval) | Accuracy |", "| --- | ---: | ---: |",
    ]
    for name, score in r["scores"].items():
        lines.append(f"| {NAMES[name]} | {_interval(score)} | {score['accuracy']:.1%} |")
    lines.append(f"| One third each | {r['reference_uniform_log_loss']:.4f} | — |")
    if r["selected_vs_baseline"]:
        lines += ["", f"Selected minus baseline, paired per match: {_difference(r['selected_vs_baseline'])}. "
                      "Negative means the selected model was better."]
    lines += ["", f"Calibration of {NAMES[selected]} (bins under 20 matches are sparse):", ""]
    for c, label in zip(CLASSES, ("Home win", "Draw", "Away win")):
        lines += [f"{label}:", "", "| Forecast bin | Matches | Mean forecast | Observed | 95% interval |",
                  "| --- | ---: | ---: | ---: | --- |"]
        for row in r["selected_analysis"]["calibration"][c]:
            low, high = row["observed_95ci"]
            lines.append(f"| {row['bin'][0]:.0%}–{row['bin'][1]:.0%} | {row['matches']}{' (sparse)' if row['sparse'] else ''} | "
                         f"{row['mean_forecast']:.1%} | {row['observed']:.1%} | {low:.0%}–{high:.0%} |")
        lines.append("")
    return "\n".join(lines)
