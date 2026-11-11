"""The class-frequency baseline and log loss, on hand-checked numbers."""
import math

import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.baseline import (CLASSES, ProtectedSeasonError, bootstrap_interval,
                                         class_frequencies, evaluate_baseline, match_losses,
                                         mean, outcomes_for)
from soccer_forecasting.loading import load_history, read_json

CONFIG = read_json(PROJECT_ROOT / "config" / "milestone0.json")


def test_frequencies_use_a_fixed_class_order():
    probabilities = class_frequencies(["H", "H", "D", "A"])
    assert tuple(probabilities) == CLASSES == ("H", "D", "A")
    assert probabilities == {"H": 0.5, "D": 0.25, "A": 0.25}


def test_log_loss_by_hand():
    # Forecast 50/25/25. A home win costs -ln 0.5 = 0.693; a draw costs -ln 0.25 = 1.386.
    losses = match_losses({"H": 0.5, "D": 0.25, "A": 0.25}, ["H", "D"])
    assert losses == pytest.approx([math.log(2), math.log(4)])
    assert mean(losses) == pytest.approx(1.0397, abs=1e-4)


def test_uniform_forecast_scores_ln_3():
    assert mean(match_losses({c: 1 / 3 for c in CLASSES}, ["H", "D", "A", "A"])) == pytest.approx(math.log(3))


def test_missing_class_is_refused_rather_than_given_zero_probability():
    with pytest.raises(ValueError, match="no training examples"):
        class_frequencies(["H", "A"])


def test_probabilities_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        match_losses({"H": 0.5, "D": 0.5, "A": 0.5}, ["H"])


def test_bootstrap_is_reproducible_and_brackets_the_mean():
    values = [0.2, 1.4, 0.9, 1.1, 0.5, 2.0, 0.7]
    low, high = bootstrap_interval(values)
    assert (low, high) == bootstrap_interval(values)
    assert low < mean(values) < high


def test_final_test_season_is_protected(db):
    with pytest.raises(ProtectedSeasonError):
        outcomes_for(db, CONFIG, CONFIG["split"]["test"])
    with pytest.raises(ProtectedSeasonError):
        outcomes_for(db, CONFIG, ["2024-25", *CONFIG["split"]["test"]])


def test_baseline_on_real_data(db):
    load_history(db, PROJECT_ROOT)
    result = evaluate_baseline(db, PROJECT_ROOT)
    training = result["training"]
    # Training counts from the Milestone 0 review: 666 home wins, 340 draws, 514 away wins.
    assert training["matches"] == 1520
    assert training["counts"] == {"H": 666, "D": 340, "A": 514}
    assert training["probabilities"]["H"] == pytest.approx(666 / 1520)
    assert result["validation"]["matches"] == 380
    assert result["validation"]["seasons"] == ["2024-25"]
    assert result["final_test_season"]["used"] is False
    low, high = result["validation"]["log_loss_95ci"]
    assert low < result["validation"]["log_loss"] < high
    assert result["reference_uniform"]["log_loss"] == pytest.approx(math.log(3))
