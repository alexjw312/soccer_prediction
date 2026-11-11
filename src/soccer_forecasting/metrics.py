"""Scoring tools for probability forecasts.

Every forecast is an array P with one row per match and three columns in the
fixed order H, D, A. Labels are integers in the same order: 0 = H, 1 = D, 2 = A.
"""
import math

import numpy as np

CLASSES = ("H", "D", "A")


def check_probabilities(P: np.ndarray) -> None:
    if P.ndim != 2 or P.shape[1] != 3:
        raise ValueError(f"expected one row of 3 probabilities per match, got shape {P.shape}")
    if np.isnan(P).any() or (P < 0).any():
        raise ValueError("probabilities must be non-negative numbers")
    if not np.allclose(P.sum(axis=1), 1, atol=1e-6):
        raise ValueError("each match's probabilities must sum to 1")


def match_log_loss(P: np.ndarray, y: np.ndarray) -> np.ndarray:
    """-ln(probability given to what happened), one value per match.

    The clip only stops a probability of exactly 0 from becoming infinity.
    """
    check_probabilities(P)
    return -np.log(np.clip(P[np.arange(len(y)), y], 1e-15, 1.0))


def bootstrap_interval(values: np.ndarray, reps: int, seed: int, level: float = 0.95) -> tuple[float, float]:
    """Percentile interval for the mean, resampling matches with replacement."""
    rng = np.random.default_rng(seed)
    values = np.asarray(values, dtype=float)
    means = values[rng.integers(0, len(values), size=(reps, len(values)))].mean(axis=1)
    tail = (1 - level) / 2
    low, high = np.quantile(means, [tail, 1 - tail])
    return float(low), float(high)


def paired_difference(losses_a: np.ndarray, losses_b: np.ndarray, reps: int, seed: int,
                      level: float = 0.95) -> dict:
    """Mean of (a - b) per match, with a bootstrap interval.

    Pairing matters: both models are scored on the same matches, so the
    interval reflects how consistently one beats the other, not how hard the
    matches happened to be. Negative means a has the lower (better) log loss.
    """
    diff = np.asarray(losses_a) - np.asarray(losses_b)
    low, high = bootstrap_interval(diff, reps, seed, level)
    return {"mean": float(diff.mean()), "interval": [low, high]}


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion that stays sensible for small counts."""
    if n == 0:
        return (math.nan, math.nan)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


def calibration_table(P: np.ndarray, y: np.ndarray, class_index: int,
                      edges=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)) -> list[dict]:
    """Group matches by the probability given to one outcome, then compare with how often it happened.

    A well-calibrated forecast's 30-40% bin sees the outcome about 35% of the time.
    """
    p = P[:, class_index]
    rows = []
    for low, high in zip(edges[:-1], edges[1:]):
        inside = (p >= low) & ((p < high) if high < 1 else (p <= high))
        n = int(inside.sum())
        if n == 0:
            continue
        hits = int((y[inside] == class_index).sum())
        ci_low, ci_high = wilson_interval(hits, n)
        rows.append({"bin": [low, high], "matches": n, "mean_forecast": float(p[inside].mean()),
                     "observed": hits / n, "observed_95ci": [ci_low, ci_high], "sparse": n < 20})
    return rows


def confusion_matrix(P: np.ndarray, y: np.ndarray) -> list[list[int]]:
    """Rows: what happened (H, D, A). Columns: the most likely outcome in the forecast."""
    predicted = P.argmax(axis=1)
    return [[int(((y == actual) & (predicted == guess)).sum()) for guess in range(3)] for actual in range(3)]


def summarize(P: np.ndarray, y: np.ndarray, reps: int, seed: int) -> dict:
    losses = match_log_loss(P, y)
    low, high = bootstrap_interval(losses, reps, seed)
    draws = y == 1
    return {
        "matches": int(len(y)),
        "log_loss": float(losses.mean()),
        "log_loss_95ci": [low, high],
        "accuracy": float((P.argmax(axis=1) == y).mean()),
        "draws": {"mean_forecast": float(P[:, 1].mean()), "observed": float(draws.mean()),
                  "times_most_likely": int((P.argmax(axis=1) == 1).sum()),
                  "log_loss_on_draws": float(losses[draws].mean()) if draws.any() else None,
                  "log_loss_on_others": float(losses[~draws].mean()) if (~draws).any() else None},
    }
