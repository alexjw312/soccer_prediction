"""Scoring tools, datasets and model definitions."""
import math

import numpy as np
import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.baseline import ProtectedSeasonError
from soccer_forecasting.features import FEATURE_VERSION, build_features
from soccer_forecasting.loading import load_history, read_json
from soccer_forecasting.metrics import (bootstrap_interval, calibration_table, check_probabilities,
                                        confusion_matrix, match_log_loss, paired_difference, wilson_interval)
from soccer_forecasting.models import FEATURES, ClassFrequencyModel, fit, load_dataset, predict

SPLIT = read_json(PROJECT_ROOT / "config" / "milestone0.json")
TRAIN = SPLIT["split"]["train"]


# --- scoring -----------------------------------------------------------------

def test_log_loss_per_match_by_hand():
    P = np.array([[0.5, 0.25, 0.25], [0.2, 0.3, 0.5]])
    assert match_log_loss(P, np.array([0, 2])) == pytest.approx([math.log(2), math.log(2)])
    assert match_log_loss(P, np.array([1, 1])) == pytest.approx([math.log(4), -math.log(0.3)])


@pytest.mark.parametrize("bad", [
    np.array([[0.5, 0.5]]), np.array([[0.6, 0.6, -0.2]]), np.array([[0.5, 0.2, 0.2]]),
    np.array([[np.nan, 0.5, 0.5]]),
])
def test_invalid_probabilities_are_refused(bad):
    with pytest.raises(ValueError):
        check_probabilities(bad)


def test_bootstrap_is_reproducible_and_brackets_the_mean():
    values = np.array([0.2, 1.4, 0.9, 1.1, 0.5, 2.0, 0.7, 1.3])
    first = bootstrap_interval(values, 2000, 7)
    assert first == bootstrap_interval(values, 2000, 7)
    assert first[0] < values.mean() < first[1]


def test_paired_difference_is_negative_when_the_first_model_is_better():
    better, worse = np.full(50, 0.9), np.full(50, 1.0)
    result = paired_difference(better, worse, 500, 1)
    assert result["mean"] == pytest.approx(-0.1)
    assert result["interval"][1] < 0


def test_wilson_interval_known_value():
    low, high = wilson_interval(5, 10)
    assert (round(low, 4), round(high, 4)) == (0.2366, 0.7634)


def test_calibration_bins_count_every_match_once():
    P = np.array([[0.05, 0.5, 0.45], [0.15, 0.5, 0.35], [0.15, 0.5, 0.35], [1.0, 0.0, 0.0]])
    y = np.array([0, 0, 2, 0])
    rows = calibration_table(P, y, 0)
    assert [r["matches"] for r in rows] == [1, 2, 1]
    assert rows[1]["observed"] == 0.5 and rows[-1]["bin"] == [0.9, 1.0]


def test_confusion_matrix_rows_are_what_happened():
    P = np.array([[0.6, 0.2, 0.2], [0.1, 0.2, 0.7], [0.5, 0.3, 0.2]])
    assert confusion_matrix(P, np.array([0, 0, 1])) == [[1, 0, 1], [1, 0, 0], [0, 0, 0]]


def test_class_frequency_model():
    model = ClassFrequencyModel().fit(None, np.array([0, 0, 1, 2]))
    assert model.predict_proba(np.zeros((2, 1))).tolist() == [[0.5, 0.25, 0.25]] * 2
    with pytest.raises(ValueError):
        ClassFrequencyModel().fit(None, np.array([0, 2]))


# --- datasets and models on real data -----------------------------------------

@pytest.fixture
def featured(db):
    load_history(db, PROJECT_ROOT)
    build_features(db, PROJECT_ROOT)
    return db


def test_feature_list_matches_the_view(db):
    columns = [row[0] for row in db.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = 'fixture_features' ORDER BY ordinal_position""")]
    assert [c for c in columns if c.startswith(("home_", "away_"))] == FEATURES


def test_dataset_refuses_the_test_season(featured):
    with pytest.raises(ProtectedSeasonError):
        load_dataset(featured, SPLIT, FEATURE_VERSION, ["2024-25", *SPLIT["split"]["test"]])


def test_dataset_needs_features_built(db):
    load_history(db, PROJECT_ROOT)
    with pytest.raises(RuntimeError, match="build-features"):
        load_dataset(db, SPLIT, FEATURE_VERSION, TRAIN)


def test_dataset_shape_and_missing_history(featured):
    data = load_dataset(featured, SPLIT, FEATURE_VERSION, TRAIN)
    assert data.X.shape == (1520, len(FEATURES))
    no_history = data.column("home_games_last10") == 0
    assert no_history.any() and np.isnan(data.column("home_ppg_last5")[no_history]).all()
    assert not np.isnan(data.column("home_ppg_last5")[~no_history]).any()


def test_imputation_is_learned_from_training_rows_only(featured):
    data = load_dataset(featured, SPLIT, FEATURE_VERSION, TRAIN)
    train = data.where(TRAIN[:2])
    model = fit("logistic_regression", {"C": 0.1, "max_iter": 5000}, 0, train)
    assert np.allclose(model.named_steps["impute"].statistics_, np.nanmean(train.X, axis=0))
    assert not np.allclose(model.named_steps["impute"].statistics_, np.nanmean(data.X, axis=0))


@pytest.mark.parametrize("name, settings", [
    ("class_frequency_baseline", {}),
    ("logistic_regression", {"C": 0.1, "max_iter": 5000}),
    ("xgboost", {"max_depth": 1, "n_estimators": 20, "n_jobs": 1}),
])
def test_every_model_uses_the_h_d_a_column_order(featured, name, settings):
    data = load_dataset(featured, SPLIT, FEATURE_VERSION, TRAIN)
    model = fit(name, settings, 0, data)
    assert list(model.classes_) == [0, 1, 2]
    P = predict(model, data)
    # Home wins are the most common training outcome, so column 0 must carry the most probability.
    assert P.mean(axis=0).argmax() == 0


def test_dataset_fingerprint_changes_with_a_result(featured):
    # Swapping a decisive score flips its H/A label, which the model would see.
    before = load_dataset(featured, SPLIT, FEATURE_VERSION, TRAIN).fingerprint()
    featured.execute("""UPDATE matches SET home_goals = away_goals, away_goals = home_goals
                        WHERE match_id = (SELECT min(match_id) FROM matches
                                          WHERE season = '2021-22' AND home_goals <> away_goals)""")
    assert load_dataset(featured, SPLIT, FEATURE_VERSION, TRAIN).fingerprint() != before
