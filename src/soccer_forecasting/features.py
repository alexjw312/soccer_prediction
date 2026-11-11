"""Compute and save team-form features (feature version form_v1).

The calculation lives in sql/features/form_v1.sql. This module runs it,
saves the result in team_form_snapshots, and refuses to overwrite a saved
feature with a different value, the same rule the loader applies to matches.
"""
from dataclasses import dataclass
from pathlib import Path

from psycopg.rows import dict_row

from .loading import read_json
from .parsing import SourceDataError

FEATURE_VERSION = "form_v1"
FEATURE_COLUMNS = ("team_id", "feature_cutoff_date", "latest_history_date",
                   "games_last5", "ppg_last5", "goals_for_last5", "goals_against_last5",
                   "games_last10", "ppg_last10", "goals_for_last10", "goals_against_last10")


@dataclass(frozen=True)
class FeatureSummary:
    sides: int
    inserted: int
    already_present: int
    sides_without_history: int
    sides_with_fewer_than_5: int


def cutoff_offset_days(root: Path) -> int:
    offset = read_json(root / "config" / "milestone0.json")["forecast_policy"][
        "historical_eligible_latest_date_offset_days"]
    if offset >= 0:
        raise ValueError("the cutoff must be before match day, or a match could see its own result")
    return offset


def feature_sql(root: Path) -> str:
    return (root / "sql" / "features" / f"{FEATURE_VERSION}.sql").read_text(encoding="utf-8")


def cohort_seasons(root: Path) -> list[str]:
    return read_json(root / "config" / "milestone0.json")["initial_modeling_seasons"]


def compute_form(conn, root: Path, offset_days: int | None = None, seasons: list[str] | None = None,
                 match_ids: list[int] | None = None, as_of="infinity") -> list[dict]:
    """Run the feature query and return its rows without saving anything.

    With neither seasons nor match_ids, it covers the historical cohort.
    as_of limits live results to those downloaded by then (see form_v1.sql).
    """
    if offset_days is None:
        offset_days = cutoff_offset_days(root)
    if seasons is None and match_ids is None:
        seasons = cohort_seasons(root)
    params = {"offset_days": offset_days, "seasons": seasons or [], "match_ids": match_ids or [], "as_of": as_of}
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(feature_sql(root), params).fetchall()


def build_features(conn, root: Path) -> FeatureSummary:
    """Save features for the historical seasons only.

    Live fixtures get their features at forecast time instead, from results
    already downloaded by then, so they are never built here.
    """
    offset = cutoff_offset_days(root)
    columns = ", ".join(FEATURE_COLUMNS)
    params = {"version": FEATURE_VERSION, "offset_days": offset, "seasons": cohort_seasons(root),
              "match_ids": [], "as_of": "infinity"}

    with conn.transaction():
        conn.execute(f"""CREATE TEMP TABLE computed_form ON COMMIT DROP AS
                         SELECT match_id, venue, {columns}
                         FROM team_form_snapshots WITH NO DATA""")
        conn.execute(f"INSERT INTO computed_form (match_id, venue, {columns}) {feature_sql(root)}", params)

        changed = conn.execute(
            f"""SELECT c.match_id, c.venue
                FROM computed_form c
                JOIN team_form_snapshots s
                  ON s.match_id = c.match_id AND s.venue = c.venue AND s.feature_version = %(version)s
                WHERE ({', '.join('s.' + c for c in FEATURE_COLUMNS)})
                      IS DISTINCT FROM ({', '.join('c.' + c for c in FEATURE_COLUMNS)})
                ORDER BY c.match_id, c.venue""", params).fetchall()
        if changed:
            raise SourceDataError(f"{len(changed)} saved features would change, first at match_id "
                                  f"{changed[0][0]} ({changed[0][1]}); use a new feature version "
                                  f"rather than overwriting saved features")

        inserted = conn.execute(
            f"""INSERT INTO team_form_snapshots (match_id, feature_version, venue, {columns})
                SELECT match_id, %(version)s, venue, {columns} FROM computed_form
                ON CONFLICT (match_id, feature_version, venue) DO NOTHING""", params).rowcount

        problems = conn.execute(
            """SELECT count(*) FROM matches m
               WHERE m.season = ANY(%(seasons)s::text[])
                 AND ((SELECT count(*) FROM team_form_snapshots s
                      WHERE s.match_id = m.match_id AND s.feature_version = %(version)s) <> 2
                  OR EXISTS (SELECT 1 FROM team_form_snapshots s
                             WHERE s.match_id = m.match_id AND s.feature_version = %(version)s
                               AND (s.feature_cutoff_date <> m.match_date + %(offset_days)s::integer
                                    OR s.team_id <> CASE s.venue WHEN 'H' THEN m.home_team_id
                                                                 ELSE m.away_team_id END)))""",
            params).fetchone()[0]
        if problems:
            raise SourceDataError(f"{problems} fixtures have missing or inconsistent features, nothing was saved")

        sides, empty, short = conn.execute(
            """SELECT count(*), count(*) FILTER (WHERE games_last10 = 0),
                      count(*) FILTER (WHERE games_last5 < 5)
               FROM computed_form""").fetchone()

    return FeatureSummary(sides=sides, inserted=inserted, already_present=sides - inserted,
                          sides_without_history=empty, sides_with_fewer_than_5=short)
