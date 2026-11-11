"""The class-frequency baseline: the simplest forecast worth beating.

It ignores both teams and predicts every match with the same probabilities:
the share of home wins, draws and away wins in the training seasons. Any
model that uses form has to beat this on held-out data, or the form adds nothing.

Evaluation uses multiclass log loss: the average of -ln(probability given to
what actually happened). Lower is better. Guessing 1/3 for everything scores
ln 3, about 1.0986. A confident wrong forecast is punished heavily, which is
why it suits probabilities better than accuracy does.

The final test season is protected: outcomes_for() refuses to read it, and
nothing in this module evaluates on it.
"""
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from .loading import read_json

CLASSES = ("H", "D", "A")
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 20261110


class ProtectedSeasonError(PermissionError):
    """Raised when code tries to read outcomes from the final test season."""


def outcomes_for(conn, config: dict, seasons: list[str]) -> list[str]:
    """Results for the given seasons in date order, refusing the test season."""
    protected = set(seasons) & set(config["split"]["test"])
    if protected:
        raise ProtectedSeasonError(f"refusing to read outcomes from the final test season: {sorted(protected)}")
    rows = conn.execute(
        """SELECT result FROM matches
           WHERE season = ANY(%s) AND status = 'completed'
           ORDER BY match_date, match_id""", (seasons,)).fetchall()
    return [row[0] for row in rows]


def class_frequencies(labels: list[str]) -> dict[str, float]:
    if not labels:
        raise ValueError("no training labels")
    unknown = set(labels) - set(CLASSES)
    if unknown:
        raise ValueError(f"unexpected labels: {sorted(unknown)}")
    missing = [c for c in CLASSES if c not in labels]
    if missing:
        # A zero probability makes log loss infinite on the first such outcome.
        raise ValueError(f"no training examples of {missing}")
    return {c: labels.count(c) / len(labels) for c in CLASSES}


def match_losses(probabilities: dict[str, float], labels: list[str]) -> list[float]:
    """-ln(probability of the actual outcome), one value per match."""
    if abs(sum(probabilities.values()) - 1) > 1e-9:
        raise ValueError("probabilities must sum to 1")
    return [-math.log(probabilities[label]) for label in labels]


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def bootstrap_interval(values: list[float], reps: int = BOOTSTRAP_REPS,
                       seed: int = BOOTSTRAP_SEED, level: float = 0.95) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean: resample matches with replacement.

    It shows how much the score could move with a different draw of 380 matches.
    """
    rng = random.Random(seed)
    n = len(values)
    means = sorted(mean([values[rng.randrange(n)] for _ in range(n)]) for _ in range(reps))
    tail = (1 - level) / 2
    return means[int(tail * reps)], means[int((1 - tail) * reps) - 1]


def _scored(probabilities: dict[str, float], labels: list[str]) -> dict:
    losses = match_losses(probabilities, labels)
    low, high = bootstrap_interval(losses)
    return {"log_loss": mean(losses), "log_loss_95ci": [low, high]}


def evaluate_baseline(conn, root: Path) -> dict:
    config = read_json(root / "config" / "milestone0.json")
    train_seasons, validation_seasons = config["split"]["train"], config["split"]["validation"]

    train = outcomes_for(conn, config, train_seasons)
    validation = outcomes_for(conn, config, validation_seasons)
    probabilities = class_frequencies(train)
    predicted = max(CLASSES, key=probabilities.get)

    # Sensitivity check from the Milestone 0 review: 2020/21 was played almost
    # entirely without crowds and has fewer home wins. Reported, not chosen.
    later_seasons = [s for s in train_seasons if s != "2020-21"]
    later_probabilities = class_frequencies(outcomes_for(conn, config, later_seasons))

    return {
        "model": "class_frequency_baseline",
        "class_order": list(CLASSES),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_commit": config["source_commit"],
        "training": {"seasons": train_seasons, "matches": len(train),
                     "counts": {c: train.count(c) for c in CLASSES},
                     "probabilities": probabilities},
        "validation": {"seasons": validation_seasons, "matches": len(validation),
                       "counts": {c: validation.count(c) for c in CLASSES},
                       **_scored(probabilities, validation),
                       "hard_label": predicted,
                       "accuracy": validation.count(predicted) / len(validation)},
        "reference_uniform": {"probabilities": {c: 1 / 3 for c in CLASSES},
                              **_scored({c: 1 / 3 for c in CLASSES}, validation)},
        "sensitivity_without_2020_21": {"seasons": later_seasons, "probabilities": later_probabilities,
                                        **_scored(later_probabilities, validation)},
        "bootstrap": {"reps": BOOTSTRAP_REPS, "seed": BOOTSTRAP_SEED, "method": "percentile, resampling matches"},
        "final_test_season": {"seasons": config["split"]["test"], "used": False},
    }


def write_report(result: dict, root: Path) -> tuple[Path, Path]:
    out = root / "reports"
    out.mkdir(exist_ok=True)
    json_path, md_path = out / "baseline_validation.json", out / "baseline_validation.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    t, v, u, s = result["training"], result["validation"], result["reference_uniform"], result["sensitivity_without_2020_21"]
    pct = lambda p: f"{p:.1%}"
    probs = lambda d: " / ".join(pct(d[c]) for c in CLASSES)
    ci = lambda r: f"{r['log_loss']:.4f} ({r['log_loss_95ci'][0]:.4f}–{r['log_loss_95ci'][1]:.4f})"
    lines = [
        "Class-frequency baseline — validation report",
        "============================================",
        "",
        f"Generated {result['generated_at_utc']} from OpenFootball commit {result['source_commit']}.",
        f"The final test season ({', '.join(result['final_test_season']['seasons'])}) was not used.",
        "",
        "Probabilities are listed as home win / draw / away win. Log loss: lower is better;",
        f"the range is a 95% bootstrap interval ({result['bootstrap']['reps']} resamples of matches).",
        "",
        "| Forecast | Fitted on | Probabilities | Validation log loss |",
        "| --- | --- | --- | ---: |",
        f"| Class-frequency baseline | {', '.join(t['seasons'])} ({t['matches']} matches) | {probs(t['probabilities'])} | {ci(v)} |",
        f"| Sensitivity: without 2020-21 | {', '.join(s['seasons'])} | {probs(s['probabilities'])} | {ci(s)} |",
        f"| Reference: one third each | nothing | {probs(u['probabilities'])} | {ci(u)} |",
        "",
        f"Validation: {', '.join(v['seasons'])}, {v['matches']} matches. Outcomes: "
        f"{v['counts']['H']} home wins, {v['counts']['D']} draws, {v['counts']['A']} away wins.",
        "",
        f"As a hard prediction the baseline always picks {v['hard_label']}, so its accuracy "
        f"({pct(v['accuracy'])}) is just the share of that outcome. Accuracy is shown for context only;",
        "log loss is the selection metric.",
        "",
        "The sensitivity row is information for Milestone 3, not a decision: both training",
        "windows should be compared on validation for the real models too, never on the test season.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
