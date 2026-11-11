"""Model selection and the one-time final test.

End-to-end tests shift every season one year earlier, so 2023-24 plays the
part of the test season. The real final test season, 2025-26, is never scored.
"""
import json

import numpy as np
import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.features import build_features
from soccer_forecasting.final_evaluation import evaluate_final
from soccer_forecasting.loading import load_history
from soccer_forecasting.model_selection import (ProtocolError, apply_selection_rule, grid, load_protocol,
                                                run_selection)

STAND_IN_TEST = "2023-24"


def test_real_protocol_keeps_tuning_inside_training():
    split, protocol, _ = load_protocol(PROJECT_ROOT)
    held_out = set(split["split"]["validation"]) | set(split["split"]["test"])
    for fold in protocol["tuning"]["folds"]:
        assert not (set(fold["train"]) | {fold["evaluate"]}) & held_out


def test_grid_expands_every_combination():
    assert grid({"a": [1, 2], "b": ["x"]}) == [{"a": 1, "b": "x"}, {"a": 2, "b": "x"}]


def test_selection_rule():
    rng = np.random.default_rng(0)
    base = rng.uniform(0.6, 1.6, 400)
    losses = {
        "baseline": base,
        "clearly_better": base - 0.05 + rng.normal(0, 0.01, 400),
        "about_the_same": base - 0.05 + rng.normal(0, 0.5, 400),
    }
    choice, trace = apply_selection_rule(["baseline", "clearly_better", "about_the_same"], losses, 1000, 1, 0.95)
    assert choice == "clearly_better"
    assert [step["compared_with"] for step in trace] == ["baseline", "clearly_better"]
    assert [step["replaces"] for step in trace] == [True, False]


# --- end to end with shifted seasons ---------------------------------------------

def write(path, document):
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


@pytest.fixture
def shifted(db, project_copy):
    split_path = project_copy / "config" / "milestone0.json"
    split = json.loads(split_path.read_text())
    split["split"] = {"train": ["2020-21", "2021-22"], "validation": ["2022-23"], "test": [STAND_IN_TEST]}
    write(split_path, split)
    protocol_path = project_copy / "config" / "milestone3.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["tuning"]["folds"] = [{"train": ["2020-21"], "evaluate": "2021-22"}]
    protocol["candidates"]["logistic_regression"]["grid"] = {"C": [0.03, 1.0]}
    protocol["candidates"]["xgboost"]["grid"] = {"max_depth": [1], "n_estimators": [50]}
    protocol["bootstrap"]["reps"] = 300
    write(protocol_path, protocol)
    load_history(db, project_copy)
    build_features(db, project_copy)
    return db, project_copy


def comparable(result):
    return {key: result[key] for key in ("tuning", "settings", "scores", "selection", "segments")}


def test_selection_freezes_a_choice_and_saves_artifacts(shifted):
    db, root = shifted
    result = run_selection(db, root)
    frozen = json.loads((root / "reports" / "frozen_selection.json").read_text())
    assert frozen["selected_model"] == result["selection"]["selected"]
    assert frozen["test_seasons"] == [STAND_IN_TEST]
    assert result["final_test_season"]["used"] is False
    for name in ("class_frequency_baseline", "logistic_regression", "xgboost"):
        meta = json.loads((root / "artifacts" / "validation" / name / "metadata.json").read_text())
        assert meta["class_order"] == ["H", "D", "A"] and len(meta["model_sha256"]) == 64
    assert (root / "reports" / "model_validation.md").read_text().count("Selected:") == 1


def test_selection_does_not_depend_on_test_season_results(shifted):
    db, root = shifted
    before = comparable(run_selection(db, root))
    db.execute("UPDATE matches SET home_goals = away_goals, away_goals = home_goals WHERE season = %s",
               (STAND_IN_TEST,))
    assert comparable(run_selection(db, root)) == before


def test_final_test_needs_confirmation_and_a_frozen_choice(shifted):
    db, root = shifted
    with pytest.raises(ProtocolError, match="confirm"):
        evaluate_final(db, root, confirm=False)
    with pytest.raises(ProtocolError, match="frozen selection"):
        evaluate_final(db, root, confirm=True)
    assert not (root / "reports" / "final_test.json").exists()


def test_final_test_runs_once_and_closes_selection(shifted):
    db, root = shifted
    run_selection(db, root)
    result = evaluate_final(db, root, confirm=True)
    assert result["test"]["seasons"] == [STAND_IN_TEST] and result["test"]["matches"] == 380
    assert set(result["scores"]) == {result["selected_model"], "class_frequency_baseline"} or \
        result["selected_model"] == "class_frequency_baseline"
    assert (root / "reports" / "final_test.md").exists()
    with pytest.raises(ProtocolError, match="already been run"):
        evaluate_final(db, root, confirm=True)
    with pytest.raises(ProtocolError, match="selection is closed"):
        run_selection(db, root)


def test_final_test_refuses_a_protocol_changed_after_selection(shifted):
    db, root = shifted
    run_selection(db, root)
    path = root / "config" / "milestone3.json"
    protocol = json.loads(path.read_text())
    protocol["candidates"]["logistic_regression"]["grid"]["C"].append(10.0)
    write(path, protocol)
    with pytest.raises(ProtocolError, match="changed after"):
        evaluate_final(db, root, confirm=True)
    assert not (root / "reports" / "final_test.json").exists()


def test_final_test_refuses_training_data_changed_after_selection(shifted):
    db, root = shifted
    run_selection(db, root)
    db.execute("""UPDATE matches SET home_goals = away_goals, away_goals = home_goals
                        WHERE match_id = (SELECT min(match_id) FROM matches
                                          WHERE season = '2021-22' AND home_goals <> away_goals)""")
    with pytest.raises(ProtocolError, match="data changed"):
        evaluate_final(db, root, confirm=True)


def test_protocol_with_a_fold_on_the_validation_season_is_refused(shifted):
    db, root = shifted
    path = root / "config" / "milestone3.json"
    protocol = json.loads(path.read_text())
    protocol["tuning"]["folds"] = [{"train": ["2020-21", "2021-22"], "evaluate": "2022-23"}]
    write(path, protocol)
    with pytest.raises(ProtocolError, match="outside training"):
        run_selection(db, root)


def test_validation_models_are_fitted_on_training_seasons_only(shifted):
    import joblib
    db, root = shifted
    run_selection(db, root)
    counts = dict(db.execute("""SELECT result, count(*) FROM matches
                                WHERE season = ANY(%s) GROUP BY result""", (["2020-21", "2021-22"],)).fetchall())
    expected = [counts[c] / sum(counts.values()) for c in ("H", "D", "A")]
    baseline = joblib.load(root / "artifacts" / "validation" / "class_frequency_baseline" / "model.joblib")
    assert baseline.probabilities_ == pytest.approx(expected)
