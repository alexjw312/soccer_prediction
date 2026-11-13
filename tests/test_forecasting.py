"""Deployment training and live forecasts, on a generated season.

Timeline used below (all from live_helpers): rounds are a week apart, kickoff
14:00 UTC. Rounds 1-10 are finished in the first download. Round 11 is on
Saturday 31 October 2026, after the clocks change, so London time equals UTC.
"""
import json
from datetime import date, datetime, timedelta, timezone

import joblib
import psycopg
import pytest
from live_helpers import add_snapshot, round_kickoff, snapshot_name

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.deployment import train_deployment
from soccer_forecasting.features import compute_form
from soccer_forecasting.forecasting import ForecastError, forecast_day, run_daily
from soccer_forecasting.live_loading import load_live
from soccer_forecasting.live_source import LiveSourceError
from soccer_forecasting.model_selection import ProtocolError

UTC = timezone.utc
ROUND_11 = round_kickoff(10)                      # 2026-10-31 14:00 UTC
ROUND_12 = round_kickoff(11)                      # 2026-11-07 14:00 UTC
DAY_11, DAY_12 = ROUND_11.date(), ROUND_12.date()
MORNING_11 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
MORNING_12 = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


def count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


# --- deployment model ---------------------------------------------------------------

def test_deployment_needs_the_final_test_first(ready):
    db, root = ready
    (root / "reports" / "final_test.json").unlink()
    with pytest.raises(ProtocolError, match="final test first"):
        train_deployment(db, root)


def test_deployment_uses_every_historical_season_and_is_recorded_once(ready):
    db, root = ready
    first = train_deployment(db, root)
    second = train_deployment(db, root)
    assert first["created"] and not second["created"]
    assert first["model_run_id"] == second["model_run_id"]
    assert first["training_matches"] == 2280
    path, seasons = db.execute("SELECT artifact_path, training_seasons FROM model_runs").fetchone()
    assert seasons == ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
    assert list(joblib.load(root / path).classes_) == [0, 1, 2]


def test_deployment_refuses_a_changed_protocol(ready):
    db, root = ready
    path = root / "config" / "milestone3.json"
    path.write_text(path.read_text().replace('"random_seed": 20261110', '"random_seed": 1'))
    with pytest.raises(ProtocolError, match="protocol changed"):
        train_deployment(db, root)


# --- saved forecasts ---------------------------------------------------------------

def test_forecasts_are_saved_once_for_match_day(trained):
    db, root = trained
    result = forecast_day(db, root, DAY_11, MORNING_11, save=True)
    assert (len(result.forecasts), result.inserted, result.already_saved) == (10, 10, 0)
    rows = db.execute("""SELECT p_home + p_draw + p_away, generated_at_utc < kickoff_utc, feature_cutoff_date,
                                data_snapshot, features FROM predictions""").fetchall()
    assert len(rows) == 10
    for total, before_kickoff, cutoff, snapshot, features in rows:
        assert total == pytest.approx(1) and before_kickoff
        assert cutoff == DAY_11 - timedelta(days=2) and snapshot == "20261030T050000Z"
        assert len(features) == 17 and features["home_games_last5"] == 5
    again = forecast_day(db, root, DAY_11, MORNING_11 + timedelta(hours=2), save=True)
    assert (again.inserted, again.already_saved) == (0, 10)
    assert count(db, "SELECT count(*) FROM predictions") == 10


def test_saved_forecasts_are_made_on_match_day_only(trained):
    db, root = trained
    with pytest.raises(ForecastError, match="made on that day"):
        forecast_day(db, root, DAY_11, MORNING_11 - timedelta(days=1), save=True)


def test_matches_already_kicked_off_are_skipped(trained):
    db, root = trained
    result = forecast_day(db, root, DAY_11, ROUND_11 + timedelta(minutes=1), save=True)
    assert result.forecasts == [] and count(db, "SELECT count(*) FROM predictions") == 0


def test_stale_data_is_refused(trained):
    db, root = trained
    with pytest.raises(ForecastError, match="hours old"):
        forecast_day(db, root, DAY_12, MORNING_12, save=True)
    assert count(db, "SELECT count(*) FROM predictions") == 0


def test_a_missing_earlier_result_is_refused(trained):
    db, root = trained
    add_snapshot(root, MORNING_12 - timedelta(hours=1), finished_rounds=10)  # round 11 still unplayed
    load_live(db, root)
    with pytest.raises(ForecastError, match="still have no result"):
        forecast_day(db, root, DAY_12, MORNING_12, save=True)


def test_preview_shows_forecasts_and_problems_but_saves_nothing(trained):
    db, root = trained
    result = forecast_day(db, root, DAY_12, MORNING_11, save=False)
    assert len(result.forecasts) == 10 and result.warnings
    assert count(db, "SELECT count(*) FROM predictions") == 0


def test_no_deployment_model_means_no_forecasts(ready):
    db, root = ready
    with pytest.raises(ForecastError, match="train-deployment"):
        forecast_day(db, root, DAY_11, MORNING_11, save=True)


def test_database_refuses_a_forecast_made_after_kickoff(trained):
    db, root = trained
    forecast_day(db, root, DAY_11, MORNING_11, save=True)
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        db.execute("UPDATE predictions SET generated_at_utc = kickoff_utc, information_cutoff_utc = kickoff_utc")
    assert caught.value.diag.constraint_name == "prediction_before_kickoff"


# --- the same features offline and live ----------------------------------------------

def test_live_feature_path_matches_saved_historical_features(trained):
    db, root = trained
    ids = [r[0] for r in db.execute("SELECT match_id FROM matches WHERE season = '2024-25' ORDER BY match_id LIMIT 40")]
    offline = [r for r in compute_form(db, root) if r["match_id"] in ids]
    live = compute_form(db, root, seasons=[], match_ids=ids, as_of=datetime.now(UTC))
    assert live == offline


def test_results_downloaded_after_the_forecast_are_not_used(trained):
    db, root = trained
    later = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    add_snapshot(root, later, finished_rounds=11)          # round 11 results first seen here
    load_live(db, root)
    ids = [r[0] for r in db.execute("SELECT match_id FROM matches WHERE season = '2026-27' AND match_date = %s",
                                    (DAY_12,))]
    before = compute_form(db, root, seasons=[], match_ids=ids, as_of=later - timedelta(minutes=1))
    after = compute_form(db, root, seasons=[], match_ids=ids, as_of=later)
    assert {r["latest_history_date"] for r in before} == {round_kickoff(9).date()}
    assert {r["latest_history_date"] for r in after} == {DAY_11}


def test_forecasts_are_scored_once_results_arrive(trained):
    db, root = trained
    forecast_day(db, root, DAY_11, MORNING_11, save=True)
    add_snapshot(root, datetime(2026, 11, 1, 5, 0, tzinfo=UTC), finished_rounds=11)
    load_live(db, root)
    rows = db.execute("SELECT log_loss, on_forecast_date FROM prediction_results").fetchall()
    assert len(rows) == 10 and all(loss > 0 and on_date for loss, on_date in rows)


# --- the daily job --------------------------------------------------------------------

def test_daily_downloads_loads_forecasts_and_logs(trained):
    db, root = trained

    def fake_fetch(project_root, season_year):
        assert season_year == 2026
        add_snapshot(project_root, MORNING_11 - timedelta(minutes=5), finished_rounds=10)
        return {"snapshot": snapshot_name(MORNING_11 - timedelta(minutes=5))}

    result = run_daily(db, root, now=MORNING_11, fetch=fake_fetch)
    assert result.inserted == 10
    status, details = db.execute("SELECT status, details FROM job_runs").fetchone()
    assert status == "succeeded" and details["inserted"] == 10 and details["snapshots_loaded"] == 1


def test_daily_failure_is_logged(trained):
    db, root = trained

    def failing_fetch(project_root, season_year):
        raise LiveSourceError("HTTP 429 (rate limit reached)")

    with pytest.raises(LiveSourceError):
        run_daily(db, root, now=MORNING_11, fetch=failing_fetch)
    status, details = db.execute("SELECT status, details FROM job_runs").fetchone()
    assert status == "failed" and "429" in details["error"]
    assert count(db, "SELECT count(*) FROM predictions") == 0


def test_an_altered_model_file_is_refused(trained):
    db, root = trained
    path = root / db.execute("SELECT artifact_path FROM model_runs").fetchone()[0]
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ForecastError, match="does not match its recorded hash"):
        forecast_day(db, root, DAY_11, MORNING_11, save=True)
