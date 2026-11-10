"""Database rules: each bad row must be refused by the intended constraint."""
import psycopg
import pytest

COLUMNS = ("competition", "season", "home_team_id", "away_team_id", "match_date", "status",
           "home_goals", "away_goals", "source_file_id", "source_row_index")
HASH = "a" * 64


@pytest.fixture
def seeded(db):
    db.execute("INSERT INTO teams (team_name) VALUES ('Alpha'), ('Beta')")
    db.execute(
        """INSERT INTO source_files (provider, source_commit, source_path, url, sha256, byte_count, retrieved_at_utc)
           VALUES ('p', 'c', 'f', 'u', %s, 1, now())""", (HASH,))
    return db


def insert(conn, row_index=1, **overrides):
    values = {"competition": "EPL", "season": "2021-22", "home_team_id": 1, "away_team_id": 2,
              "match_date": "2021-10-01", "status": "completed", "home_goals": 1, "away_goals": 0,
              "source_file_id": 1, "source_row_index": row_index}
    values.update(overrides)
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    conn.execute(f"INSERT INTO matches ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                 [values[c] for c in COLUMNS])


@pytest.mark.parametrize("overrides, constraint", [
    ({"away_team_id": 1}, "matches_different_teams"),
    ({"season": "2019-20", "match_date": "2019-07-26"}, "matches_date_in_season"),
    ({"season": "2021/22"}, "matches_season_format"),
    ({"status": "finished", "home_goals": None, "away_goals": None}, "matches_status_valid"),
    ({"home_goals": -1}, "matches_goals_nonnegative"),
    ({"home_goals": None, "away_goals": None}, "matches_goals_fit_status"),
    ({"away_goals": None}, "matches_goals_fit_status"),
    ({"status": "scheduled", "home_goals": 0, "away_goals": 0}, "matches_goals_fit_status"),
])
def test_bad_rows_are_refused_by_the_intended_constraint(seeded, overrides, constraint):
    with pytest.raises(psycopg.errors.IntegrityError) as caught:
        insert(seeded, **overrides)
    assert caught.value.diag.constraint_name == constraint


def test_same_fixture_cannot_be_inserted_twice_even_on_another_date(seeded):
    insert(seeded)
    with pytest.raises(psycopg.errors.UniqueViolation) as caught:
        insert(seeded, row_index=2, match_date="2022-02-01")
    assert caught.value.diag.constraint_name == "matches_fixture_unique"


def test_reverse_fixture_is_a_different_match(seeded):
    insert(seeded)
    insert(seeded, row_index=2, home_team_id=2, away_team_id=1)
    assert seeded.execute("SELECT count(*) FROM matches").fetchone()[0] == 2


def test_result_cannot_be_written_by_hand(seeded):
    insert(seeded)
    with pytest.raises(psycopg.errors.GeneratedAlways):
        seeded.execute("UPDATE matches SET result = 'A'")


@pytest.mark.parametrize("home, away, expected", [(2, 1, "H"), (0, 3, "A"), (1, 1, "D"), (0, 0, "D")])
def test_result_follows_the_goals(seeded, home, away, expected):
    insert(seeded, home_goals=home, away_goals=away)
    assert seeded.execute("SELECT result FROM matches").fetchone()[0] == expected


def test_unplayed_fixture_is_accepted_with_no_result(seeded):
    insert(seeded, status="scheduled", home_goals=None, away_goals=None)
    assert seeded.execute("SELECT result FROM matches").fetchone()[0] is None


def test_team_matches_shows_each_match_from_both_sides(seeded):
    insert(seeded, home_goals=2, away_goals=1)
    rows = seeded.execute(
        """SELECT team_id, opponent_team_id, venue, goals_for, goals_against, points
           FROM team_matches ORDER BY team_id""").fetchall()
    assert rows == [(1, 2, "H", 2, 1, 3), (2, 1, "A", 1, 2, 0)]


def test_team_matches_gives_one_point_each_for_a_draw(seeded):
    insert(seeded, home_goals=0, away_goals=0)
    assert [r[0] for r in seeded.execute("SELECT points FROM team_matches").fetchall()] == [1, 1]


def test_team_matches_leaves_out_unplayed_fixtures(seeded):
    insert(seeded, status="postponed", home_goals=None, away_goals=None)
    assert seeded.execute("SELECT count(*) FROM team_matches").fetchone()[0] == 0
