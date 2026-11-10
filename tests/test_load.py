"""The loader against the real pinned snapshot.

Failures here report only season and row position, and never score values or
totals, so a failing test cannot display 2025/26 outcomes.
"""
import hashlib
import json

import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.checks import run_checks
from soccer_forecasting.loading import load_history, read_json
from soccer_forecasting.parsing import SourceDataError

CONFIG = read_json(PROJECT_ROOT / "config" / "milestone0.json")
SEASONS = CONFIG["initial_modeling_seasons"]
PROVIDER = "openfootball/football.json"


def raw_path(root, season):
    manifest = read_json(root / "data" / "manifest.json")
    entry = next(f for f in manifest["files"] if f["source_path"] == f"{season}/en.1.json")
    return root / entry["local_path"]


def edit_season(root, season, change):
    """Change a season's raw file and update its manifest hash to match."""
    path = raw_path(root, season)
    payload = json.loads(path.read_text(encoding="utf-8"))
    change(payload["matches"])
    data = json.dumps(payload).encode("utf-8")
    path.write_bytes(data)
    manifest_path = root / "data" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["source_path"] == f"{season}/en.1.json":
            entry["sha256"], entry["bytes"] = hashlib.sha256(data).hexdigest(), len(data)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def match_count(conn):
    return conn.execute("SELECT count(*) FROM matches").fetchone()[0]


def test_first_load_inserts_the_selected_seasons(db):
    summary = load_history(db, PROJECT_ROOT)
    assert (summary.files, summary.staged, summary.inserted, summary.already_present) == (6, 2280, 2280, 0)
    counts = dict(db.execute("SELECT season, count(*) FROM matches GROUP BY season").fetchall())
    assert counts == {season: 380 for season in SEASONS}


def test_excluded_seasons_are_not_loaded(db):
    load_history(db, PROJECT_ROOT)
    loaded = {row[0] for row in db.execute("SELECT DISTINCT season FROM matches").fetchall()}
    assert "2019-20" not in loaded and "2026-27" not in loaded


def test_reloading_changes_nothing(db):
    load_history(db, PROJECT_ROOT)
    before = db.execute("SELECT match_id, ingested_at FROM matches ORDER BY match_id").fetchall()
    files_before = db.execute("SELECT source_file_id, loaded_at FROM source_files ORDER BY 1").fetchall()
    summary = load_history(db, PROJECT_ROOT)
    assert (summary.inserted, summary.already_present) == (0, 2280)
    assert db.execute("SELECT match_id, ingested_at FROM matches ORDER BY match_id").fetchall() == before
    assert db.execute("SELECT source_file_id, loaded_at FROM source_files ORDER BY 1").fetchall() == files_before


def test_every_database_row_matches_its_raw_file(db):
    """Reconciles against the raw JSON with separate code from the parser."""
    load_history(db, PROJECT_ROOT)
    expected = {}
    for season in SEASONS:
        rows = json.loads(raw_path(PROJECT_ROOT, season).read_text(encoding="utf-8"))["matches"]
        for index, row in enumerate(rows):
            score = row["score"]
            full_time = score["ft"] if isinstance(score, dict) else score
            expected[(season, index)] = (row["team1"], row["team2"], row["date"], row["time"],
                                         row["round"], full_time[0], full_time[1])
    actual = {
        (season, index): (home, away, str(day), time, rnd, hg, ag)
        for season, index, home, away, day, time, rnd, hg, ag in db.execute(
            """SELECT m.season, m.source_row_index, h.alias, a.alias, m.match_date, m.source_time,
                      m.source_round, m.home_goals, m.away_goals
               FROM matches m
               JOIN team_aliases h ON h.team_id = m.home_team_id AND h.provider = %(p)s
               JOIN team_aliases a ON a.team_id = m.away_team_id AND a.provider = %(p)s""",
            {"p": PROVIDER}).fetchall()
    }
    assert set(actual) == set(expected)
    assert [key for key in expected if actual[key] != expected[key]] == []


def test_every_match_traces_to_its_source_file(db):
    load_history(db, PROJECT_ROOT)
    unmatched = db.execute(
        """SELECT count(*) FROM matches m JOIN source_files f USING (source_file_id)
           WHERE f.source_path <> m.season || '/en.1.json'""").fetchone()[0]
    assert unmatched == 0


def test_all_checks_pass_after_loading(db):
    load_history(db, PROJECT_ROOT)
    assert [c.name for c in run_checks(db, PROJECT_ROOT, SEASONS) if not c.passed] == []


def test_changed_file_is_refused(db, project_copy):
    path = raw_path(project_copy, "2020-21")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(SourceDataError, match="manifest hash"):
        load_history(db, project_copy)
    assert match_count(db) == 0


@pytest.mark.parametrize("break_row, message", [
    (lambda rows: rows[0].update(score=[1]), "row 0"),
    (lambda rows: rows[0].pop("score"), "row 0"),
    (lambda rows: rows[0].update(team1="Unheard Of FC"), "Unheard Of FC"),
])
def test_bad_source_rows_stop_the_whole_load(db, project_copy, break_row, message):
    edit_season(project_copy, "2021-22", break_row)
    with pytest.raises(SourceDataError, match=message):
        load_history(db, project_copy)
    assert match_count(db) == 0


def test_a_lost_row_fails_the_checks_and_saves_nothing(db, project_copy):
    edit_season(project_copy, "2020-21", lambda rows: rows.pop())
    with pytest.raises(SourceDataError, match="rows_per_season"):
        load_history(db, project_copy)
    assert match_count(db) == 0
    assert db.execute("SELECT count(*) FROM source_files").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM teams").fetchone()[0] == 0


def test_wrong_year_date_is_refused_and_saves_nothing(db, project_copy):
    # The 2019/20 error, reproduced inside a selected season.
    edit_season(project_copy, "2020-21", lambda rows: rows[0].update(date="2019-07-26"))
    with pytest.raises(Exception, match="matches_date_in_season"):
        load_history(db, project_copy)
    assert match_count(db) == 0


def test_changed_values_from_a_new_snapshot_are_refused(db, project_copy):
    load_history(db, project_copy)
    before = db.execute("SELECT match_id, home_goals, away_goals FROM matches ORDER BY 1").fetchall()

    def add_a_goal(rows):
        score = rows[0]["score"]
        score["ft"][0] += 1

    edit_season(project_copy, "2020-21", add_a_goal)
    for name in ("config/milestone0.json", "data/manifest.json"):
        path = project_copy / name
        document = json.loads(path.read_text(encoding="utf-8"))
        document["source_commit"] = "new-commit"
        path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SourceDataError, match="1 fixtures differ"):
        load_history(db, project_copy)
    assert db.execute("SELECT match_id, home_goals, away_goals FROM matches ORDER BY 1").fetchall() == before


def test_check_reports_a_club_with_two_matches_on_one_date(db):
    load_history(db, PROJECT_ROOT)
    first, second = db.execute(
        """SELECT match_id, match_date FROM team_matches
           WHERE season = '2021-22'
             AND team_id = (SELECT min(team_id) FROM team_matches WHERE season = '2021-22')
           ORDER BY match_date LIMIT 2""").fetchall()
    db.execute("UPDATE matches SET match_date = %s WHERE match_id = %s", (first[1], second[0]))
    failed = [c.name for c in run_checks(db, PROJECT_ROOT, SEASONS) if not c.passed]
    assert failed == ["one_match_per_team_date"]


def test_check_reports_a_season_outside_the_cohort(db):
    load_history(db, PROJECT_ROOT)
    results = {c.name: c for c in run_checks(db, PROJECT_ROOT, SEASONS[:-1])}
    assert results["cohort_seasons"].violations == [(SEASONS[-1], "not in cohort")]
