"""Running the daily job on a computer that starts empty (GitHub Actions with a Neon database).

Such a computer has no data/live folder and no artifacts folder, so the database
keeps a copy of the model file and of every download. These tests remove those
folders to act like a fresh computer. The last group checks the workflow file.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest
import yaml
from live_helpers import round_kickoff, season, write_snapshot
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.deployment import train_deployment
from soccer_forecasting.forecasting import ForecastError, forecast_day
from soccer_forecasting.live_loading import SNAPSHOTS, export_live, load_live
from soccer_forecasting.loading import load_history
from soccer_forecasting.parsing import SourceDataError

UTC = timezone.utc
DAY_11 = round_kickoff(10).date()                 # Saturday 31 October 2026
MORNING_11 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
FIRST, SECOND = "20261111T042633Z", "20261118T050000Z"
FIRST_SEEN, SECOND_SEEN = "2026-11-11T04:26:34+00:00", "2026-11-18T05:00:00+00:00"


def count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


def recorded_model(db, root):
    run_id, path, sha = db.execute("SELECT model_run_id, artifact_path, artifact_sha256 FROM model_runs").fetchone()
    return run_id, root / path, sha


def probabilities(day):
    return [(f.match_id, f.p_home, f.p_draw, f.p_away) for f in day.forecasts]


# --- the model file -----------------------------------------------------------------

def test_training_keeps_a_copy_of_the_model_file_in_the_database(trained):
    db, root = trained
    run_id, path, sha = recorded_model(db, root)
    content, content_sha = db.execute("SELECT content, content_sha256 FROM model_files WHERE model_run_id = %s",
                                      (run_id,)).fetchone()
    assert bytes(content) == path.read_bytes() and content_sha == sha


def test_forecasts_without_the_model_file_use_the_stored_copy(trained):
    db, root = trained
    _, path, _ = recorded_model(db, root)
    with_file = forecast_day(db, root, DAY_11, MORNING_11, save=False)
    path.unlink()  # a fresh computer has no artifacts folder

    without_file = forecast_day(db, root, DAY_11, MORNING_11, save=True)
    assert without_file.inserted == 10
    assert probabilities(without_file) == probabilities(with_file)
    assert not path.exists()


def test_training_on_a_fresh_computer_restores_the_recorded_file(trained, monkeypatch):
    db, root = trained
    run_id, path, sha = recorded_model(db, root)
    path.unlink()

    def no_refitting(*args, **kwargs):
        raise AssertionError("another computer may not reproduce the file, so it must come from the database")
    monkeypatch.setattr("soccer_forecasting.deployment.fit", no_refitting)
    result = train_deployment(db, root)
    assert (result["model_run_id"], result["created"], result.get("restored")) == (run_id, False, True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    assert count(db, "SELECT count(*) FROM model_runs") == 1


def test_a_model_trained_before_stored_files_gets_its_copy_on_the_next_training(trained):
    db, root = trained
    run_id, _, _ = recorded_model(db, root)
    db.execute("DELETE FROM model_files")  # as in a database from before 005_stored_files
    assert not train_deployment(db, root)["created"]
    assert count(db, "SELECT count(*) FROM model_files WHERE model_run_id = %s", run_id) == 1


def test_a_refit_that_reproduces_the_recorded_file_restores_it_and_its_copy(trained):
    db, root = trained
    run_id, path, sha = recorded_model(db, root)
    db.execute("DELETE FROM model_files")
    path.unlink()
    assert train_deployment(db, root).get("restored")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    assert count(db, "SELECT count(*) FROM model_files WHERE model_run_id = %s", run_id) == 1


def test_a_refit_that_differs_from_the_recorded_file_is_refused_and_changes_nothing(trained):
    db, root = trained
    _, path, _ = recorded_model(db, root)
    db.execute("DELETE FROM model_files")
    path.unlink()
    # As if another computer had trained the recorded model, so refitting here cannot reproduce it.
    db.execute("UPDATE model_runs SET artifact_sha256 = %s", ("0" * 64,))
    with pytest.raises(RuntimeError, match="slightly different"):
        train_deployment(db, root)
    assert not path.exists()
    assert count(db, "SELECT count(*) FROM model_runs") == 1
    assert count(db, "SELECT count(*) FROM model_files") == 0


def test_without_the_file_or_a_stored_copy_nothing_is_forecast(trained):
    db, root = trained
    _, path, _ = recorded_model(db, root)
    db.execute("DELETE FROM model_files")
    path.unlink()
    with pytest.raises(ForecastError, match="holds no copy"):
        forecast_day(db, root, DAY_11, MORNING_11, save=True)


def test_the_database_refuses_a_stored_copy_that_differs_from_the_record(trained):
    db, _ = trained
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("UPDATE model_files SET content = content || %s", (b"x",))


# --- downloaded files ---------------------------------------------------------------

@pytest.fixture
def live(db, project_copy):
    load_history(db, project_copy)
    return db, project_copy


def on_disk(root, snapshot):
    return {p.name: p.read_bytes() for p in (root / SNAPSHOTS / snapshot).iterdir()}


def in_database(db, snapshot):
    return {name: bytes(content) for name, content in db.execute(
        "SELECT file_name, content FROM live_download_files WHERE snapshot = %s", (snapshot,))}


def test_loading_keeps_a_copy_of_every_downloaded_file(live):
    db, root = live
    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    [summary] = load_live(db, root)
    assert summary.files_stored == 2
    assert in_database(db, FIRST) == on_disk(root, FIRST)


def test_snapshots_loaded_earlier_get_their_copy_on_the_next_load(live):
    db, root = live
    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    load_live(db, root)
    db.execute("DELETE FROM live_download_files")  # as in a database from before 005_stored_files

    [summary] = load_live(db, root)
    assert (summary.loaded, summary.files_stored) == (False, 2)
    assert in_database(db, FIRST) == on_disk(root, FIRST)
    [again] = load_live(db, root)
    assert again.files_stored == 0


def test_a_listed_file_that_is_missing_saves_nothing(live):
    db, root = live
    folder = write_snapshot(root, FIRST, season(), FIRST_SEEN)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"].append({"file": "teams.json", "sha256": "0" * 64})
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SourceDataError, match="listed in the manifest but missing"):
        load_live(db, root)
    assert count(db, "SELECT count(*) FROM matches WHERE season = '2026-27'") == 0
    assert count(db, "SELECT count(*) FROM live_download_files") == 0


def test_a_listed_file_that_differs_from_the_manifest_saves_nothing(live):
    db, root = live
    folder = write_snapshot(root, FIRST, season(), FIRST_SEEN)
    (folder / "teams.json").write_text('{"teams": []}')
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"].append({"file": "teams.json", "sha256": hashlib.sha256(b"something else").hexdigest()})
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SourceDataError, match="teams.json does not match its manifest hash"):
        load_live(db, root)
    assert count(db, "SELECT count(*) FROM live_download_files") == 0


def test_a_download_changed_after_loading_is_refused(live):
    db, root = live
    folder = write_snapshot(root, FIRST, season(), FIRST_SEEN)
    load_live(db, root)
    (folder / "manifest.json").write_text((folder / "manifest.json").read_text() + "\n")
    with pytest.raises(SourceDataError, match="differs from the copy already in the database"):
        load_live(db, root)


def test_a_computer_that_starts_empty_loads_new_downloads_and_can_export_old_ones(live):
    db, root = live
    write_snapshot(root, FIRST, season(finished_rounds=10), FIRST_SEEN)
    load_live(db, root)
    first_files = on_disk(root, FIRST)
    shutil.rmtree(root / SNAPSHOTS)  # a fresh computer has no data/live

    write_snapshot(root, SECOND, season(finished_rounds=11), SECOND_SEEN)
    [summary] = load_live(db, root)
    assert summary.loaded and summary.newly_completed == 10

    assert export_live(db, root) == [FIRST]
    assert on_disk(root, FIRST) == first_files
    assert export_live(db, root) == []


def test_export_refuses_a_folder_that_differs_from_the_database(live):
    db, root = live
    folder = write_snapshot(root, FIRST, season(), FIRST_SEEN)
    load_live(db, root)
    (folder / "matches.json").write_bytes(b"{}")
    with pytest.raises(SourceDataError, match="differs from the copy in the database"):
        export_live(db, root)


# --- what reaches a public log ------------------------------------------------------

def run_command(database_url, *command):
    env = {k: v for k, v in os.environ.items() if k != "FOOTBALL_DATA_API_KEY"}
    return subprocess.run([sys.executable, "-m", "soccer_forecasting", *command],
                          env={**env, "DATABASE_URL": database_url}, capture_output=True, text=True, timeout=60)


def test_a_failed_connection_never_prints_the_password(test_database_url):
    # A database that does not exist, so this fails even where passwords are not checked.
    params = {**conninfo_to_dict(test_database_url), "password": "pw-that-must-not-appear-4c1e",
              "dbname": "no_such_db_4c1e"}
    result = run_command(make_conninfo(**params), "check")
    assert result.returncode == 1
    assert "Could not connect to the database" in result.stderr
    assert "pw-that-must-not-appear-4c1e" not in result.stdout + result.stderr


@pytest.mark.parametrize("address", [
    "postgresql//neondb_owner:pw-that-must-not-appear-77aa@ep-example.us-east-2.aws.neon.tech/neondb",  # no colon
    "neondb_owner:pw-that-must-not-appear-77aa@ep-example.us-east-2.aws.neon.tech/neondb",             # no scheme
])
def test_a_malformed_address_is_refused_without_being_printed(address):
    result = run_command(address, "check")
    assert result.returncode == 1
    assert "not a valid database address" in result.stderr
    assert "Traceback" not in result.stderr
    assert "pw-that-must-not-appear-77aa" not in result.stdout + result.stderr


# --- the workflow files --------------------------------------------------------------

def read_workflow(name):
    data = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / name).read_text())
    data["on"] = data.pop(True)  # YAML 1.1 reads the key "on" as true
    return data


@pytest.fixture(scope="module")
def workflow():
    return read_workflow("daily.yml")


@pytest.fixture(scope="module")
def tests_workflow():
    return read_workflow("tests.yml")


def steps(workflow):
    return workflow["jobs"]["daily"]["steps"]


def test_runs_on_a_schedule_and_by_hand_never_two_at_once(workflow):
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["concurrency"]["cancel-in-progress"] is False


# Winter and summer days, including the Sunday the clocks go forward.
@pytest.mark.parametrize("day", [date(2026, 8, 22), date(2026, 11, 21), date(2027, 3, 28), date(2027, 5, 23)])
def test_scheduled_runs_fall_on_match_day_before_the_first_kickoff(workflow, day):
    schedule = workflow["on"]["schedule"]
    assert len(schedule) == 2
    for entry in schedule:
        minute, hour, *rest = entry["cron"].split()
        assert rest == ["*", "*", "*"]
        assert int(minute) not in (0, 30)  # GitHub's busiest minutes, when scheduled runs start late
        london = datetime.combine(day, time(int(hour), int(minute)), tzinfo=UTC).astimezone(ZoneInfo("Europe/London"))
        assert london.date() == day and time(5) <= london.time() < time(12)


def test_secrets_reach_only_the_two_steps_that_need_them(workflow):
    assert "env" not in workflow and "env" not in workflow["jobs"]["daily"]
    with_secrets = {s["name"]: sorted(s["env"]) for s in steps(workflow) if "secrets." in json.dumps(s)}
    assert with_secrets == {"Apply any new schema changes": ["DATABASE_URL"],
                            "Download, load and forecast": ["DATABASE_URL", "FOOTBALL_DATA_API_KEY"]}
    assert not any("secrets." in s.get("run", "") for s in steps(workflow))  # never on a command line


def test_read_only_permissions_and_pinned_actions(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    actions = [s for s in steps(workflow) if "uses" in s]
    assert actions and all(re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", s["uses"]) for s in actions)
    checkout = next(s for s in actions if s["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_runs_the_same_commands_as_on_a_mac(workflow):
    # An editable install, because the project finds config/ and sql/ next to its source code.
    assert [s["run"] for s in steps(workflow) if "run" in s] == [
        "python -m pip install -e .", "python -m soccer_forecasting init-db", "python -m soccer_forecasting daily"]


def test_the_test_workflow_runs_the_whole_suite_against_postgres_16(tests_workflow):
    assert set(tests_workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    job = tests_workflow["jobs"]["pytest"]
    service = job["services"]["postgres"]
    assert service["image"] == "postgres:16"
    assert service["image"] in (PROJECT_ROOT / "README.md").read_text()  # the same version as on the Mac
    url = conninfo_to_dict(job["env"]["TEST_DATABASE_URL"])
    assert (url["user"], url["password"], url["dbname"]) == (
        service["env"]["POSTGRES_USER"], service["env"]["POSTGRES_PASSWORD"], service["env"]["POSTGRES_DB"])
    assert [s["run"] for s in job["steps"] if "run" in s] == ['python -m pip install -e ".[dev]"',
                                                              "python -m pytest -ra"]


def test_the_test_workflow_uses_no_secrets_and_cannot_write(tests_workflow):
    assert "secrets." not in (PROJECT_ROOT / ".github" / "workflows" / "tests.yml").read_text()
    assert tests_workflow["permissions"] == {"contents": "read"}


def test_both_workflows_use_the_same_pinned_actions_and_python(workflow, tests_workflow):
    daily_steps, test_steps = steps(workflow), tests_workflow["jobs"]["pytest"]["steps"]

    def actions(job_steps):
        return sorted(s["uses"] for s in job_steps if "uses" in s)

    def python(job_steps):
        return next(s for s in job_steps if s.get("uses", "").startswith("actions/setup-python@"))["with"]["python-version"]

    assert actions(daily_steps) == actions(test_steps)
    assert python(daily_steps) == python(test_steps)
    checkout = next(s for s in test_steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
