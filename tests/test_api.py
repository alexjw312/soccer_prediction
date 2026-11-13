"""The read-only API and dashboard, on the generated season from live_helpers.

Round 11 (10 matches) kicks off at 14:00 UTC on Saturday 31 October 2026.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from live_helpers import add_snapshot, round_kickoff, season, snapshot_name, write_snapshot

from soccer_forecasting.api import ATTRIBUTION, create_app, read_only_connection
from soccer_forecasting.forecasting import forecast_day
from soccer_forecasting.live_loading import load_live

UTC = timezone.utc
MORNING_11 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
DAY_11 = MORNING_11.date()


def read_only_factory(db):
    """The app's own connection code, pointed at the db fixture's test schema."""
    schema = db.execute("SHOW search_path").fetchone()[0]
    separator = "&" if "?" in os.environ["TEST_DATABASE_URL"] else "?"
    return read_only_connection(f"{os.environ['TEST_DATABASE_URL']}{separator}options=-csearch_path%3D{schema}")


def client(db, root, now):
    return TestClient(create_app(root, connect=read_only_factory(db), now=lambda: now))


@pytest.fixture
def forecasted(trained):
    db, root = trained
    forecast_day(db, root, DAY_11, MORNING_11, save=True)
    return db, root


def test_health_reports_fresh_and_stale_data(forecasted):
    db, root = forecasted
    fresh = client(db, root, MORNING_11).get("/health").json()
    assert (fresh["status"], fresh["database"], fresh["latest_snapshot"]) == ("ok", "ok", "20261030T050000Z")
    assert (fresh["saved_forecasts"], fresh["deployment_model_run"]) == (10, 1)
    later = client(db, root, MORNING_11 + timedelta(days=3)).get("/health").json()
    assert later["status"] == "stale"


def test_forecasts_for_a_day(forecasted):
    db, root = forecasted
    body = client(db, root, MORNING_11).get("/forecasts", params={"date": "2026-10-31"}).json()
    assert len(body["forecasts"]) == 10 and body["attribution"] == ATTRIBUTION
    for f in body["forecasts"]:
        assert sum(f["probabilities"].values()) == pytest.approx(1)
        assert f["generated_at_utc"] < f["kickoff_utc"]
        assert f["feature_cutoff_date"] == "2026-10-29" and f["model_run_id"] == 1 and f["result"] is None


def test_today_is_the_default_day(forecasted):
    db, root = forecasted
    assert len(client(db, root, MORNING_11).get("/forecasts").json()["forecasts"]) == 10


def test_a_day_without_forecasts_says_so(forecasted):
    db, root = forecasted
    body = client(db, root, MORNING_11).get("/forecasts", params={"date": "2026-11-07"}).json()
    assert body["forecasts"] == [] and "match day" in body["message"]


def test_a_malformed_date_is_rejected(forecasted):
    db, root = forecasted
    assert client(db, root, MORNING_11).get("/forecasts", params={"date": "31/10/2026"}).status_code == 422


def test_unknown_match_is_404(forecasted):
    db, root = forecasted
    assert client(db, root, MORNING_11).get("/matches/999999/forecasts").status_code == 404


def test_a_match_lists_its_forecasts(forecasted):
    db, root = forecasted
    match_id = db.execute("SELECT match_id FROM predictions LIMIT 1").fetchone()[0]
    body = client(db, root, MORNING_11).get(f"/matches/{match_id}/forecasts").json()
    assert len(body["forecasts"]) == 1


def test_upcoming_lists_only_matches_not_yet_started(forecasted):
    db, root = forecasted
    api = client(db, root, MORNING_11)
    assert len(api.get("/forecasts/upcoming").json()["forecasts"]) == 10
    after = client(db, root, round_kickoff(10) + timedelta(minutes=1))
    assert after.get("/forecasts/upcoming").json()["forecasts"] == []


def test_upcoming_fixtures_show_a_forecast_only_once_published(forecasted):
    db, root = forecasted
    fixtures = client(db, root, MORNING_11).get("/fixtures/upcoming", params={"limit": 15}).json()["fixtures"]
    assert [f["forecast"] is not None for f in fixtures] == [True] * 10 + [False] * 5


def test_saved_forecasts_never_change_when_later_data_arrives(forecasted):
    db, root = forecasted
    before = client(db, root, MORNING_11).get("/forecasts", params={"date": "2026-10-31"}).json()["forecasts"]
    add_snapshot(root, datetime(2026, 11, 1, 5, 0, tzinfo=UTC), finished_rounds=11)
    load_live(db, root)
    after = client(db, root, MORNING_11).get("/forecasts", params={"date": "2026-10-31"}).json()["forecasts"]
    assert [f["probabilities"] for f in after] == [f["probabilities"] for f in before]
    assert all(f["result"] is not None for f in after)


def test_performance_waits_for_results_then_scores_them(forecasted):
    db, root = forecasted
    assert client(db, root, MORNING_11).get("/performance").json()["matches"] == 0
    add_snapshot(root, datetime(2026, 11, 1, 5, 0, tzinfo=UTC), finished_rounds=11)
    load_live(db, root)
    perf = client(db, root, MORNING_11).get("/performance").json()
    expected = db.execute("SELECT avg(log_loss) FROM prediction_results").fetchone()[0]
    assert perf["matches"] == 10 and perf["log_loss"] == pytest.approx(expected)
    assert 0 < perf["baseline_log_loss"] and 0 <= perf["accuracy"] <= 1


def test_the_api_cannot_write(forecasted):
    db, _ = forecasted
    with read_only_factory(db)() as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM predictions")
    assert db.execute("SELECT count(*) FROM predictions").fetchone()[0] == 10


def test_dashboard_shows_forecasts_attribution_and_escapes_names(forecasted):
    db, root = forecasted
    db.execute("UPDATE teams SET team_name = 'Arsenal <b>FC</b>' WHERE team_name = 'Arsenal'")
    page = client(db, root, MORNING_11).get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    html = page.text
    assert ATTRIBUTION in html and "Upcoming fixtures" in html and "published on match day" in html
    assert "Arsenal &lt;b&gt;FC&lt;/b&gt;" in html and "<b>FC</b>" not in html


def test_database_down_gives_503(forecasted):
    db, root = forecasted

    @contextmanager
    def broken():
        raise psycopg.OperationalError("connection refused")
        yield

    api = TestClient(create_app(root, connect=broken, now=lambda: MORNING_11))
    assert api.get("/health").status_code == 503


def test_a_match_moved_after_its_forecast_is_not_scored(forecasted):
    db, root = forecasted
    moved_on = round_kickoff(10) + timedelta(days=3)
    payload = season(finished_rounds=11)
    moved = next(m for m in payload["matches"] if m["id"] == 600100)
    # Moved to three days later, and played then: its forecast was for a different day.
    moved.update(status="FINISHED", utcDate=moved_on.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 score={"duration": "REGULAR", "fullTime": {"home": 1, "away": 1}})
    later = moved_on + timedelta(hours=15)
    write_snapshot(root, snapshot_name(later), payload, later.isoformat())
    load_live(db, root)
    assert client(db, root, later).get("/performance").json()["matches"] == 9
