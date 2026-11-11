"""Form features: hand-worked examples first, then the real six seasons.

Failures on real data report match ids only, never values, so a failing test
cannot display outcomes from the final test season.
"""
from collections import defaultdict
from datetime import date, timedelta

import psycopg
import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.features import FEATURE_VERSION, build_features, compute_form
from soccer_forecasting.loading import load_history
from soccer_forecasting.parsing import SourceDataError

# ---------------------------------------------------------------------------
# A tiny league worked out by hand.
#
# Team A's matches before the target fixture (Arsenal-style "A v B" on 20 Sep 2021):
#
#   date        match     A's goals  A's points
#   2020-09-01  A 2-0 B   2 for 0    3
#   2020-09-10  C 0-1 A   1 for 0    3
#   2020-09-20  A 0-2 C   0 for 2    0
#   2021-08-20  B 1-1 A   1 for 1    1
#   2021-08-30  A 3-1 D   3 for 1    3
#   2021-09-18  D 2-2 A   2 for 2    1   <- two days before: allowed
#   2021-09-19  C 5-0 A   0 for 5    0   <- one day before: excluded
#
# Last 5 allowed: points 3,0,1,3,1 -> 8/5 = 1.6 (the Milestone 0 report's example)
#                 goals for 1,0,1,3,2 -> 1.4; against 0,2,1,1,2 -> 1.2
# Last 10 allowed (only 6 exist): points 11/6, goals for 9/6 = 1.5, against 6/6 = 1.0
# Team B before the target: lost 0-2, drew 1-1 -> 2 games, 0.5 points, 0.5 for, 1.5 against.
# ---------------------------------------------------------------------------
TINY_LEAGUE = [
    ("2020-21", "2020-09-01", "A", "B", 2, 0),
    ("2020-21", "2020-09-10", "C", "A", 0, 1),
    ("2020-21", "2020-09-20", "A", "C", 0, 2),
    ("2021-22", "2021-08-20", "B", "A", 1, 1),
    ("2021-22", "2021-08-30", "A", "D", 3, 1),
    ("2021-22", "2021-09-18", "D", "A", 2, 2),
    ("2021-22", "2021-09-19", "C", "A", 5, 0),
    ("2021-22", "2021-09-20", "A", "B", 1, 0),
]
TARGET = ("2021-22", "A", "B")


def add_match(conn, season, day, home, away, home_goals, away_goals, row_index):
    return conn.execute(
        """INSERT INTO matches (competition, season, home_team_id, away_team_id, match_date, status,
                                home_goals, away_goals, source_file_id, source_row_index)
           SELECT 'EPL', %s, h.team_id, a.team_id, %s, 'completed', %s, %s, 1, %s
           FROM teams h, teams a WHERE h.team_name = %s AND a.team_name = %s
           RETURNING match_id""",
        (season, day, home_goals, away_goals, row_index, home, away)).fetchone()[0]


@pytest.fixture
def tiny(db):
    db.execute("INSERT INTO teams (team_name) VALUES ('A'), ('B'), ('C'), ('D')")
    db.execute("""INSERT INTO source_files (provider, source_commit, source_path, url, sha256, byte_count, retrieved_at_utc)
                  VALUES ('p', 'c', 'f', 'u', %s, 1, now())""", ("a" * 64,))
    ids = {}
    for index, (season, day, home, away, hg, ag) in enumerate(TINY_LEAGUE):
        ids[(season, home, away)] = add_match(db, season, day, home, away, hg, ag, index)
    return db, ids


def form_by_side(rows):
    return {(row["match_id"], row["venue"]): row for row in rows}


def test_hand_worked_home_side(tiny):
    db, ids = tiny
    home = form_by_side(compute_form(db, PROJECT_ROOT))[(ids[TARGET], "H")]
    assert home["feature_cutoff_date"] == date(2021, 9, 18)
    assert home["latest_history_date"] == date(2021, 9, 18)
    assert (home["games_last5"], home["games_last10"]) == (5, 6)
    assert home["ppg_last5"] == pytest.approx(1.6)
    assert home["goals_for_last5"] == pytest.approx(1.4)
    assert home["goals_against_last5"] == pytest.approx(1.2)
    assert home["ppg_last10"] == pytest.approx(11 / 6)
    assert home["goals_for_last10"] == pytest.approx(1.5)
    assert home["goals_against_last10"] == pytest.approx(1.0)


def test_hand_worked_away_side(tiny):
    db, ids = tiny
    away = form_by_side(compute_form(db, PROJECT_ROOT))[(ids[TARGET], "A")]
    assert (away["games_last5"], away["games_last10"]) == (2, 2)
    assert away["latest_history_date"] == date(2021, 8, 20)
    assert away["ppg_last5"] == pytest.approx(0.5)
    assert away["goals_for_last5"] == pytest.approx(0.5)
    assert away["goals_against_last5"] == pytest.approx(1.5)


def test_first_ever_match_has_no_history(tiny):
    db, ids = tiny
    form = form_by_side(compute_form(db, PROJECT_ROOT))
    for venue in ("H", "A"):
        side = form[(ids[("2020-21", "A", "B")], venue)]
        assert side["games_last5"] == side["games_last10"] == 0
        assert side["latest_history_date"] is None
        assert side["ppg_last5"] is None and side["goals_for_last10"] is None


def test_later_and_one_day_earlier_results_cannot_change_a_fixture(tiny):
    db, ids = tiny
    before = form_by_side(compute_form(db, PROJECT_ROOT))[(ids[TARGET], "H")]
    # Change the target's own score, the match one day earlier, and add a later match.
    db.execute("UPDATE matches SET home_goals = 9, away_goals = 9 WHERE match_id = ANY(%s)",
               ([ids[TARGET], ids[("2021-22", "C", "A")]],))
    add_match(db, "2021-22", "2021-09-25", "B", "C", 4, 4, 99)
    after = form_by_side(compute_form(db, PROJECT_ROOT))[(ids[TARGET], "H")]
    assert after == before


def test_a_match_two_days_earlier_does_count(tiny):
    db, ids = tiny
    before = form_by_side(compute_form(db, PROJECT_ROOT))[(ids[TARGET], "H")]
    db.execute("UPDATE matches SET home_goals = 0, away_goals = 4 WHERE match_id = %s",
               (ids[("2021-22", "D", "A")],))
    after = form_by_side(compute_form(db, PROJECT_ROOT))[(ids[TARGET], "H")]
    # The 2-2 draw became a 4-0 away win for A: points 3,0,1,3,3 -> 10/5 = 2.0
    assert after["ppg_last5"] == pytest.approx(2.0)
    assert after != before


def test_cutoff_on_match_day_would_leak_the_result(tiny):
    # Why the rule matters: with no gap, a fixture's own result enters its features.
    db, ids = tiny
    leaky = form_by_side(compute_form(db, PROJECT_ROOT, offset_days=0))[(ids[TARGET], "H")]
    assert leaky["latest_history_date"] == date(2021, 9, 20)


def test_build_refuses_a_cutoff_on_or_after_match_day(tiny, project_copy):
    import json
    path = project_copy / "config" / "milestone0.json"
    config = json.loads(path.read_text())
    config["forecast_policy"]["historical_eligible_latest_date_offset_days"] = 0
    path.write_text(json.dumps(config))
    db, _ = tiny
    with pytest.raises(ValueError, match="before match day"):
        build_features(db, project_copy)


# ---------------------------------------------------------------------------
# Saved snapshots and their constraints
# ---------------------------------------------------------------------------

def snapshot_insert(conn, **overrides):
    values = {"match_id": 1, "feature_version": "t", "venue": "H", "team_id": 1,
              "feature_cutoff_date": "2020-08-30", "latest_history_date": "2020-08-20",
              "games_last5": 1, "ppg_last5": 3, "goals_for_last5": 2, "goals_against_last5": 0,
              "games_last10": 1, "ppg_last10": 3, "goals_for_last10": 2, "goals_against_last10": 0}
    values.update(overrides)
    columns = ", ".join(values)
    conn.execute(f"INSERT INTO team_form_snapshots ({columns}) VALUES ({', '.join(['%s'] * len(values))})",
                 list(values.values()))


@pytest.mark.parametrize("overrides, constraint", [
    ({"latest_history_date": "2020-08-31"}, "form_history_before_cutoff"),
    ({"games_last5": 2}, "form_windows_nest"),
    ({"ppg_last5": None}, "form_history_fits_values"),
    ({"games_last5": 0, "games_last10": 0}, "form_history_fits_values"),
    ({"ppg_last10": 3.5}, "team_form_snapshots_ppg_last10_check"),
    ({"venue": "N"}, "team_form_snapshots_venue_check"),
])
def test_bad_snapshots_are_refused(tiny, overrides, constraint):
    db, _ = tiny
    with pytest.raises(psycopg.errors.IntegrityError) as caught:
        snapshot_insert(db, **overrides)
    assert caught.value.diag.constraint_name == constraint


def test_feature_view_never_contains_the_result(db):
    columns = {row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'fixture_features'")}
    assert columns and not columns & {"result", "home_goals", "away_goals", "status"}


# ---------------------------------------------------------------------------
# The real six seasons
# ---------------------------------------------------------------------------

@pytest.fixture
def loaded(db):
    load_history(db, PROJECT_ROOT)
    return db


def test_build_covers_every_fixture_and_rebuild_inserts_nothing(loaded):
    first = build_features(loaded, PROJECT_ROOT)
    assert (first.sides, first.inserted, first.already_present) == (4560, 4560, 0)
    # Every club's first cohort match: 20 clubs in 2020-21 plus 8 newly promoted later.
    assert first.sides_without_history == 28
    stamps = loaded.execute("SELECT built_at FROM team_form_snapshots ORDER BY 1").fetchall()
    second = build_features(loaded, PROJECT_ROOT)
    assert (second.inserted, second.already_present) == (0, 4560)
    assert loaded.execute("SELECT built_at FROM team_form_snapshots ORDER BY 1").fetchall() == stamps
    assert loaded.execute("SELECT count(*) FROM fixture_features WHERE feature_version = %s",
                          (FEATURE_VERSION,)).fetchone()[0] == 2280


def test_sql_matches_an_independent_python_calculation(loaded):
    """Recomputes every feature with plain Python, sharing no code with the SQL."""
    history = defaultdict(list)
    for team, day, gf, ga in loaded.execute(
            """SELECT home_team_id, match_date, home_goals, away_goals FROM matches
               UNION ALL SELECT away_team_id, match_date, away_goals, home_goals FROM matches"""):
        history[team].append((day, gf, ga, 3 if gf > ga else 1 if gf == ga else 0))
    for games in history.values():
        games.sort()

    def expected(team, day):
        eligible = [g for g in history[team] if g[0] <= day - timedelta(days=2)]
        out = {}
        for n in (5, 10):
            window = eligible[-n:]
            out[f"games_last{n}"] = len(window)
            for name, i in (("goals_for", 1), ("goals_against", 2), ("ppg", 3)):
                out[f"{name}_last{n}"] = sum(g[i] for g in window) / len(window) if window else None
        out["latest_history_date"] = eligible[-1][0] if eligible else None
        return out

    fixtures = {mid: (h, a, d) for mid, h, a, d in loaded.execute(
        "SELECT match_id, home_team_id, away_team_id, match_date FROM matches")}
    mismatched = []
    for row in compute_form(loaded, PROJECT_ROOT):
        home, away, day = fixtures[row["match_id"]]
        want = expected(home if row["venue"] == "H" else away, day)
        for key, value in want.items():
            got = row[key]
            same = got == value if value is None or got is None or isinstance(value, (int, date)) \
                else abs(got - value) < 1e-12
            if not same:
                mismatched.append(row["match_id"])
                break
    assert len(fixtures) == 2280
    assert mismatched == []


def test_later_results_cannot_change_earlier_features(loaded):
    before = form_by_side(compute_form(loaded, PROJECT_ROOT))
    match_id, day, home, away = loaded.execute(
        """SELECT match_id, match_date, home_team_id, away_team_id FROM matches
           WHERE season = '2022-23' ORDER BY match_date, match_id OFFSET 190 LIMIT 1""").fetchone()
    loaded.execute("UPDATE matches SET home_goals = home_goals + 3 WHERE match_id = %s", (match_id,))
    after = form_by_side(compute_form(loaded, PROJECT_ROOT))

    dates = dict(loaded.execute("SELECT match_id, match_date FROM matches").fetchall())
    changed = {key for key in before if before[key] != after[key]}
    earlier_changed = [key[0] for key in changed if dates[key[0]] <= day + timedelta(days=1)]
    assert earlier_changed == []
    # The check has teeth: later fixtures of the two teams involved did change.
    assert changed and {after[key]["team_id"] for key in changed} <= {home, away}


def test_rebuilding_after_a_data_change_is_refused(loaded):
    build_features(loaded, PROJECT_ROOT)
    loaded.execute("""UPDATE matches SET home_goals = home_goals + 1
                      WHERE match_id = (SELECT min(match_id) FROM matches WHERE season = '2021-22')""")
    with pytest.raises(SourceDataError, match="saved features would change"):
        build_features(loaded, PROJECT_ROOT)


def test_one_day_gap_gives_the_same_team_form_on_this_data(loaded):
    # No club played on consecutive days in these seasons (Milestone 0 review),
    # so moving the cutoff from D-2 to D-1 changes nothing for team form.
    assert compute_form(loaded, PROJECT_ROOT, offset_days=-1) == [
        {**row, "feature_cutoff_date": row["feature_cutoff_date"] + timedelta(days=1)}
        for row in compute_form(loaded, PROJECT_ROOT, offset_days=-2)]
