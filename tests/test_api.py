"""The read-only API and dashboard, on the generated season from live_helpers.

Round 11 (10 matches) kicks off at 14:00 UTC on Saturday 31 October 2026.
"""
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from live_helpers import add_snapshot, round_kickoff, season, snapshot_name, write_snapshot
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from soccer_forecasting.api import ATTRIBUTION, DASHBOARD_READS, create_app, read_only_connection
from soccer_forecasting.dashboard_login import LoginError, create_dashboard_login
from soccer_forecasting.live_loading import load_live

UTC = timezone.utc
MORNING_11 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
DAY_11 = MORNING_11.date()


def schema_url(db):
    """TEST_DATABASE_URL, pointed at the db fixture's test schema."""
    schema = db.execute("SHOW search_path").fetchone()[0]
    separator = "&" if "?" in os.environ["TEST_DATABASE_URL"] else "?"
    return f"{os.environ['TEST_DATABASE_URL']}{separator}options=-csearch_path%3D{schema}"


def read_only_factory(db):
    """The app's own connection code, pointed at the db fixture's test schema."""
    return read_only_connection(schema_url(db))


def client(db, root, now):
    return TestClient(create_app(root, connect=read_only_factory(db), now=lambda: now))


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


@contextmanager
def broken():
    raise psycopg.OperationalError('connection to server at "ep-secret-host.example" failed: '
                                   'password authentication failed for user "neondb_owner"')
    yield


def test_database_down_gives_503_without_database_details(forecasted):
    _, root = forecasted
    response = TestClient(create_app(root, connect=broken, now=lambda: MORNING_11)).get("/health")
    assert response.status_code == 503
    assert "ep-secret-host" not in response.text and "neondb_owner" not in response.text


def test_healthz_answers_without_the_database(project_copy):
    response = TestClient(create_app(project_copy, connect=broken)).get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_a_malformed_address_is_refused_without_being_shown(project_copy, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql//neondb_owner:pw-hidden-91c3@ep-x.neon.tech/neondb")
    with pytest.raises(RuntimeError, match="not a valid database address") as caught:
        create_app(project_copy)
    assert "pw-hidden-91c3" not in str(caught.value)


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


# --- the public dashboard's read-only login ----------------------------------------

@pytest.fixture
def login_names(db):
    """Unique login names for a test. Logins belong to the whole server, so they are removed afterwards."""
    if not db.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
        pytest.skip("the test database user cannot create logins")
    names = []
    yield names
    for name in reversed(list(dict.fromkeys(names))):
        if db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            role = sql.Identifier(name)
            # The creator may take on the login's privileges, then remove everything it holds or owns.
            db.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(role))
            db.execute(sql.SQL("DROP OWNED BY {}").format(role))
            db.execute(sql.SQL("DROP ROLE {}").format(role))


def new_name(names, prefix="dashboard_reader"):
    names.append(f"{prefix}_{uuid4().hex[:8]}")
    return names[-1]


def test_every_page_works_under_the_read_only_login(forecasted, login_names):
    db, root = forecasted
    login = create_dashboard_login(db, schema_url(db), new_name(login_names))
    assert login["created"] and login["write_refused"] and login["reads"] == list(DASHBOARD_READS)

    as_reader = TestClient(create_app(root, connect=read_only_connection(login["address"]), now=lambda: MORNING_11))
    as_owner = client(db, root, MORNING_11)
    match_id = db.execute("SELECT min(match_id) FROM predictions").fetchone()[0]
    for path in ("/health", "/forecasts?date=2026-10-31", "/forecasts/upcoming", "/fixtures/upcoming",
                 f"/matches/{match_id}/forecasts", "/performance"):
        response = as_reader.get(path)
        assert response.status_code == 200, path
        assert response.json() == as_owner.get(path).json(), path
    team_id = db.execute("SELECT home_team_id FROM matches WHERE match_id = %s", (match_id,)).fetchone()[0]
    for path in ("/", "/?date=2026-11-07", f"/match/{match_id}", f"/team/{team_id}", "/table", "/model-performance",
                 "/how-it-works"):
        page = as_reader.get(path)
        assert page.status_code == 200 and ATTRIBUTION in page.text, path
        assert page.text == as_owner.get(path).text, path


def test_the_read_only_login_cannot_write_or_read_anything_else(forecasted, login_names):
    db, _ = forecasted
    login = create_dashboard_login(db, schema_url(db), new_name(login_names))
    with psycopg.connect(login["address"], autocommit=True) as reader:
        reader.execute("SET default_transaction_read_only = off")  # what an attacker would try first
        for statement in ("DELETE FROM predictions", "UPDATE teams SET team_name = 'x'",
                          "CREATE TABLE stolen (x int)", "SELECT * FROM live_download_files",
                          "SELECT * FROM model_files", "SELECT * FROM match_observations"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(statement)
    assert db.execute("SELECT count(*) FROM predictions").fetchone()[0] == 10


def test_running_again_gives_a_new_password_and_the_old_one_stops_working(forecasted, login_names):
    db, _ = forecasted
    name = new_name(login_names)
    first = create_dashboard_login(db, schema_url(db), name)
    second = create_dashboard_login(db, schema_url(db), name)
    assert first["created"] and not second["created"] and first["address"] != second["address"]
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(first["address"]).close()
    with psycopg.connect(second["address"]) as reader:
        assert reader.execute("SELECT count(*) FROM predictions").fetchone()[0] == 10


def test_running_again_removes_anything_granted_by_hand(forecasted, login_names):
    db, _ = forecasted
    name = new_name(login_names)
    create_dashboard_login(db, schema_url(db), name)
    db.execute(sql.SQL("GRANT SELECT ON live_download_files TO {}").format(sql.Identifier(name)))
    create_dashboard_login(db, schema_url(db), name)
    assert not db.execute("SELECT has_table_privilege(%s, 'live_download_files', 'SELECT')", (name,)).fetchone()[0]


def test_a_login_with_more_powers_is_left_alone(forecasted, login_names):
    db, _ = forecasted
    group, name = new_name(login_names, "admins"), new_name(login_names)
    db.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(group)))
    db.execute(sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(sql.Identifier(name), sql.Identifier(group)))
    with pytest.raises(LoginError, match="more powers"):
        create_dashboard_login(db, schema_url(db), name)
    assert not db.execute("SELECT has_table_privilege(%s, 'predictions', 'SELECT')", (name,)).fetchone()[0]


def test_the_command_puts_the_address_on_the_clipboard_never_on_screen(forecasted, login_names, tmp_path):
    db, _ = forecasted
    name = new_name(login_names)
    tools, clipboard = tmp_path / "bin", tmp_path / "clipboard"
    tools.mkdir()
    (tools / "pbcopy").write_text(f'#!/bin/sh\ncat > "{clipboard}"\n')
    (tools / "pbcopy").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "FOOTBALL_DATA_API_KEY"}
    env.update(DATABASE_URL=schema_url(db), PATH=f"{tools}{os.pathsep}{env.get('PATH', '')}")
    result = subprocess.run([sys.executable, "-m", "soccer_forecasting", "create-dashboard-login", "--role", name],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    address = clipboard.read_text()
    password = conninfo_to_dict(address)["password"]
    assert len(password) >= 40 and password not in result.stdout + result.stderr
    assert "on your clipboard" in result.stdout
    with psycopg.connect(address) as reader:
        assert reader.execute("SELECT count(*) FROM predictions").fetchone()[0] == 10
