"""Schema migrations: apply once, upgrade an old database, refuse a broken one."""
import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.loading import load_history
from soccer_forecasting.migrations import MigrationError, apply_migrations, migration_files


def versions(conn):
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def test_fixture_database_has_every_migration(db):
    assert versions(db) == [path.stem for path in migration_files(PROJECT_ROOT)]


def test_running_again_applies_nothing(db):
    assert apply_migrations(db, PROJECT_ROOT) == {"adopted": [], "applied": []}


def test_milestone_1_database_is_upgraded_without_reloading(db):
    # Load the history, then strip the schema back to Milestone 1: 001 only, no migrations table.
    load_history(db, PROJECT_ROOT)
    for statement in ("DROP TABLE job_runs", "DROP VIEW prediction_results", "DROP TABLE predictions",
                      "DROP TABLE model_runs",
                      "DROP VIEW result_first_seen", "DROP TABLE match_observations", "DROP TABLE provider_matches",
                      "ALTER TABLE matches DROP COLUMN kickoff_utc",
                      "DROP VIEW fixture_features", "DROP TABLE team_form_snapshots", "DROP TABLE schema_migrations"):
        db.execute(statement)

    result = apply_migrations(db, PROJECT_ROOT)

    later = [path.stem for path in migration_files(PROJECT_ROOT)][1:]
    assert result == {"adopted": ["001_schema"], "applied": later}
    assert later[:3] == ["002_features", "003_live", "004_predictions"]
    assert db.execute("SELECT count(*) FROM matches").fetchone()[0] == 2280
    assert db.execute("SELECT to_regclass('team_form_snapshots')").fetchone()[0] is not None


def test_partial_first_schema_is_refused(db):
    for statement in ("DROP VIEW fixture_features", "DROP TABLE team_form_snapshots",
                      "DROP TABLE schema_migrations", "DROP VIEW team_matches"):
        db.execute(statement)
    with pytest.raises(MigrationError, match="only part"):
        apply_migrations(db, PROJECT_ROOT)
