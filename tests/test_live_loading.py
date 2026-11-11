"""Loading live snapshots, using a generated season.

The fixture list here is generated (a double round robin of the real 20 club
ids), and its scores are invented, so no football-data.org data is stored in
the repository.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from live_helpers import season, write_snapshot

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.checks import run_checks
from soccer_forecasting.features import FEATURE_VERSION, build_features
from soccer_forecasting.live_loading import load_live, local_date_and_time
from soccer_forecasting.loading import load_history, read_json
from soccer_forecasting.models import load_dataset
from soccer_forecasting.parsing import SourceDataError

FIRST, SECOND = "20261111T042633Z", "20261118T050000Z"
FIRST_SEEN, SECOND_SEEN = "2026-11-11T04:26:34+00:00", "2026-11-18T05:00:00+00:00"
COHORT = read_json(PROJECT_ROOT / "config" / "milestone0.json")["initial_modeling_seasons"]


def by_id(payload, match_id):
    return next(m for m in payload["matches"] if m["id"] == match_id)


def state(db, provider_id):
    return db.execute("""SELECT m.match_date, m.status, m.home_goals, m.away_goals, m.kickoff_utc
                         FROM provider_matches p JOIN matches m USING (match_id)
                         WHERE p.provider_match_id = %s""", (str(provider_id),)).fetchone()


def count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


@pytest.fixture
def live(db, project_copy):
    load_history(db, project_copy)
    return db, project_copy


def test_local_dates_follow_london_time():
    utc = timezone.utc
    assert local_date_and_time(datetime(2026, 8, 21, 19, 0, tzinfo=utc), "Europe/London") == (date(2026, 8, 21), "20:00")
    assert local_date_and_time(datetime(2026, 11, 21, 12, 30, tzinfo=utc), "Europe/London") == (date(2026, 11, 21), "12:30")
    # 23:30 UTC on 24 October is already 25 October in London (summer time).
    assert local_date_and_time(datetime(2026, 10, 24, 23, 30, tzinfo=utc), "Europe/London") == (date(2026, 10, 25), "00:30")


def test_first_snapshot_loads_the_whole_season(live):
    db, root = live
    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    [summary] = load_live(db, root)
    assert (summary.loaded, summary.inserted, summary.fixtures) == (True, 380, 380)
    assert count(db, "SELECT count(*) FROM matches WHERE season = '2026-27'") == 380
    assert count(db, "SELECT count(*) FROM matches WHERE season = '2026-27' AND status = 'completed'") == 100
    assert count(db, "SELECT count(*) FROM matches WHERE season = '2026-27' AND kickoff_utc IS NULL") == 0
    assert count(db, "SELECT count(*) FROM teams WHERE team_name IN ('Coventry City', 'Hull City')") == 2
    assert count(db, "SELECT count(*) FROM match_observations") == 380
    assert count(db, "SELECT count(*) FROM result_first_seen WHERE first_seen_utc = %s", FIRST_SEEN) == 100
    assert [c.name for c in run_checks(db, root, COHORT) if not c.passed] == []


def test_reloading_changes_nothing(live):
    db, root = live
    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    load_live(db, root)
    before = db.execute("SELECT match_id, ingested_at FROM matches ORDER BY 1").fetchall()
    [summary] = load_live(db, root)
    assert summary.loaded is False
    assert db.execute("SELECT match_id, ingested_at FROM matches ORDER BY 1").fetchall() == before
    assert count(db, "SELECT count(*) FROM match_observations") == 380


def test_later_snapshot_updates_fixtures_and_keeps_history(live):
    db, root = live
    first = season()
    write_snapshot(root, FIRST, first, FIRST_SEEN)
    load_live(db, root)

    later = season()
    finished, moved, postponed, early = 600100, 600110, 600120, 600000
    by_id(later, finished).update(status="FINISHED", score={"duration": "REGULAR", "fullTime": {"home": 2, "away": 2}})
    moved_kickoff = datetime.fromisoformat(by_id(later, moved)["utcDate"].replace("Z", "+00:00")) + timedelta(days=3)
    by_id(later, moved)["utcDate"] = moved_kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    by_id(later, postponed)["status"] = "POSTPONED"
    write_snapshot(root, SECOND, later, SECOND_SEEN)

    old, new = load_live(db, root)
    assert old.loaded is False
    assert (new.newly_completed, new.rescheduled, new.other_updates) == (1, 1, 1)
    assert state(db, finished)[1:4] == ("completed", 2, 2)
    assert state(db, moved)[0] == moved_kickoff.date()
    assert state(db, postponed)[1] == "postponed"
    first_seen = dict(db.execute("SELECT p.provider_match_id, f.first_seen_utc FROM result_first_seen f "
                                 "JOIN provider_matches p USING (match_id)").fetchall())
    assert first_seen[str(finished)].isoformat() == SECOND_SEEN
    assert first_seen[str(early)].isoformat() == FIRST_SEEN
    assert count(db, "SELECT count(*) FROM match_observations") == 760


def test_in_play_scores_are_never_stored(live):
    db, root = live
    payload = season()
    by_id(payload, 600100).update(status="IN_PLAY", score={"duration": "REGULAR", "fullTime": {"home": 1, "away": 0}})
    write_snapshot(root, FIRST, payload, FIRST_SEEN)
    load_live(db, root)
    assert state(db, 600100)[1:4] == ("scheduled", None, None)


def nothing_live_saved(db):
    return (count(db, "SELECT count(*) FROM matches WHERE season = '2026-27'") == 0
            and count(db, "SELECT count(*) FROM source_files WHERE provider = 'football-data.org'") == 0)


def test_unknown_team_stops_the_load(live):
    db, root = live
    payload = season()
    payload["matches"][0]["homeTeam"] = {"id": 999999, "name": "Mystery Town FC"}
    write_snapshot(root, FIRST, payload, FIRST_SEEN)
    with pytest.raises(SourceDataError, match="Mystery Town FC"):
        load_live(db, root)
    assert nothing_live_saved(db)


def test_unknown_status_stops_the_load(live):
    db, root = live
    payload = season()
    payload["matches"][5]["status"] = "AWARDED"
    write_snapshot(root, FIRST, payload, FIRST_SEEN)
    with pytest.raises(SourceDataError, match="AWARDED needs a decision"):
        load_live(db, root)
    assert nothing_live_saved(db)


def test_a_recorded_result_cannot_change(live):
    db, root = live
    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    load_live(db, root)
    later = season()
    by_id(later, 600000)["score"]["fullTime"]["home"] += 1
    write_snapshot(root, SECOND, later, SECOND_SEEN)
    with pytest.raises(SourceDataError, match="a person needs to check it"):
        load_live(db, root)
    assert state(db, 600000)[2] == season()["matches"][0]["score"]["fullTime"]["home"]
    assert count(db, "SELECT count(*) FROM match_observations") == 380


def test_snapshots_must_load_in_time_order(live):
    db, root = live
    write_snapshot(root, SECOND, season(), SECOND_SEEN)
    load_live(db, root)
    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    with pytest.raises(SourceDataError, match="older than"):
        load_live(db, root)


def test_a_changed_file_is_refused(live):
    db, root = live
    folder = write_snapshot(root, FIRST, season(), FIRST_SEEN)
    (folder / "matches.json").write_bytes((folder / "matches.json").read_bytes() + b" ")
    with pytest.raises(SourceDataError, match="manifest hash"):
        load_live(db, root)
    assert nothing_live_saved(db)


def test_a_snapshot_for_another_season_is_refused(live):
    db, root = live
    write_snapshot(root, FIRST, season(), FIRST_SEEN, season_year=2025)
    with pytest.raises(SourceDataError, match="not the configured live season"):
        load_live(db, root)


def test_live_data_leaves_historical_features_and_training_data_alone(live):
    db, root = live
    build_features(db, root)
    split = read_json(root / "config" / "milestone0.json")
    seasons = split["split"]["train"] + split["split"]["validation"]
    fingerprint = load_dataset(db, split, FEATURE_VERSION, seasons).fingerprint()

    write_snapshot(root, FIRST, season(), FIRST_SEEN)
    load_live(db, root)

    again = build_features(db, root)
    assert (again.inserted, again.already_present) == (0, 4560)
    assert count(db, """SELECT count(*) FROM team_form_snapshots s JOIN matches m USING (match_id)
                        WHERE m.season = '2026-27'""") == 0
    assert load_dataset(db, split, FEATURE_VERSION, seasons).fingerprint() == fingerprint
