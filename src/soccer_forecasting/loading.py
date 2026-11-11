"""Load the selected historical seasons into PostgreSQL.

The whole load is one transaction: files are verified, rows are staged and
compared, new rows are inserted, and the checks run before anything commits.
If any step fails, the database is left exactly as it was.
"""
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .checks import run_checks
from .parsing import ParsedMatch, SourceDataError, parse_season


@dataclass(frozen=True)
class LoadSummary:
    files: int
    staged: int
    inserted: int
    already_present: int


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _verified_entry(root: Path, manifest: dict, season: str) -> tuple[dict, bytes]:
    """Return a season's manifest entry and bytes, refusing a changed file."""
    source_path = f"{season}/en.1.json"
    entry = next((f for f in manifest["files"] if f["source_path"] == source_path), None)
    if entry is None or entry.get("status") != 200:
        raise SourceDataError(f"{source_path}: no successful download in the manifest")
    data = (root / entry["local_path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SourceDataError(f"{source_path}: file does not match its manifest hash")
    return entry, data


def _seed_teams(conn, root: Path) -> None:
    with (root / "config" / "team_aliases.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        conn.execute("INSERT INTO teams (team_name) VALUES (%s) ON CONFLICT (team_name) DO NOTHING",
                     (row["team_name"],))
        existing = conn.execute(
            """SELECT t.team_name FROM team_aliases a JOIN teams t USING (team_id)
               WHERE a.provider = %s AND a.alias = %s""",
            (row["provider"], row["alias"])).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO team_aliases (provider, alias, team_id)
                   SELECT %s, %s, team_id FROM teams WHERE team_name = %s""",
                (row["provider"], row["alias"], row["team_name"]))
        elif existing[0] != row["team_name"]:
            raise SourceDataError(f"alias {row['alias']!r} is already mapped to {existing[0]!r}")


def _register_source_file(conn, manifest: dict, entry: dict) -> int:
    conn.execute(
        """INSERT INTO source_files
               (provider, source_commit, source_path, url, sha256, byte_count, retrieved_at_utc)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (provider, source_commit, source_path) DO NOTHING""",
        (manifest["provider"], manifest["source_commit"], entry["source_path"], entry["url"],
         entry["sha256"], entry["bytes"], entry["retrieved_at_utc"]))
    file_id, stored_hash = conn.execute(
        """SELECT source_file_id, sha256 FROM source_files
           WHERE provider = %s AND source_commit = %s AND source_path = %s""",
        (manifest["provider"], manifest["source_commit"], entry["source_path"])).fetchone()
    if stored_hash != entry["sha256"]:
        raise SourceDataError(f"{entry['source_path']}: differs from the copy already loaded at this commit")
    return file_id


def _stage_rows(conn, provider: str, parsed: list[ParsedMatch], file_ids: dict[str, int]) -> None:
    conn.execute(
        """CREATE TEMP TABLE staged_matches (
               season text, source_row_index integer, match_date date, source_time text,
               source_round text, home_name text, away_name text,
               home_goals smallint, away_goals smallint, source_file_id integer
           ) ON COMMIT DROP""")
    with conn.cursor() as cur:
        with cur.copy(
            """COPY staged_matches (season, source_row_index, match_date, source_time, source_round,
                                    home_name, away_name, home_goals, away_goals, source_file_id)
               FROM STDIN""") as copy:
            for m in parsed:
                copy.write_row((m.season, m.source_row_index, m.match_date, m.source_time, m.source_round,
                                m.home_name, m.away_name, m.home_goals, m.away_goals, file_ids[m.season]))

    unknown = conn.execute(
        """SELECT DISTINCT names.name
           FROM (SELECT home_name AS name FROM staged_matches
                 UNION SELECT away_name FROM staged_matches) AS names
           LEFT JOIN team_aliases a ON a.provider = %s AND a.alias = names.name
           WHERE a.alias IS NULL
           ORDER BY names.name""", (provider,)).fetchall()
    if unknown:
        names = ", ".join(row[0] for row in unknown)
        raise SourceDataError(f"team names with no alias in config/team_aliases.csv: {names}")

    conn.execute(
        """CREATE TEMP TABLE resolved_matches ON COMMIT DROP AS
           SELECT s.*, h.team_id AS home_team_id, a.team_id AS away_team_id
           FROM staged_matches s
           JOIN team_aliases h ON h.provider = %(provider)s AND h.alias = s.home_name
           JOIN team_aliases a ON a.provider = %(provider)s AND a.alias = s.away_name""",
        {"provider": provider})


def _refuse_changed_rows(conn, competition: str) -> None:
    """Stop if a fixture already in the database arrives with different values.

    ON CONFLICT DO NOTHING would otherwise keep the old row and hide the
    disagreement. Only season and row position are reported, so an error can
    never display outcomes from the final test season.
    """
    changed = conn.execute(
        """SELECT r.season, r.source_row_index
           FROM resolved_matches r
           JOIN matches m ON m.competition = %s AND m.season = r.season
                         AND m.home_team_id = r.home_team_id AND m.away_team_id = r.away_team_id
           WHERE (m.match_date, m.source_time, m.source_round, m.home_goals, m.away_goals)
                 IS DISTINCT FROM
                 (r.match_date, r.source_time, r.source_round, r.home_goals, r.away_goals)
           ORDER BY r.season, r.source_row_index""", (competition,)).fetchall()
    if changed:
        raise SourceDataError(f"{len(changed)} fixtures differ from what is already loaded, "
                              f"first at {changed[0][0]} row {changed[0][1]}")


def _insert_new_rows(conn, competition: str) -> int:
    # Every row in a historical file has a validated final score, so it is
    # completed. A row without one has already stopped the load in parsing.
    return conn.execute(
        """INSERT INTO matches (competition, season, home_team_id, away_team_id, match_date, source_time,
                                source_round, status, home_goals, away_goals, source_file_id, source_row_index)
           SELECT %s, season, home_team_id, away_team_id, match_date, source_time,
                  source_round, 'completed', home_goals, away_goals, source_file_id, source_row_index
           FROM resolved_matches
           ON CONFLICT (competition, season, home_team_id, away_team_id) DO NOTHING""",
        (competition,)).rowcount


def load_history(conn, root: Path) -> LoadSummary:
    config = read_json(root / "config" / "milestone0.json")
    manifest = read_json(root / "data" / "manifest.json")
    if config["source_commit"] != manifest["source_commit"]:
        raise SourceDataError("config and manifest name different source commits")

    seasons = config["initial_modeling_seasons"]
    entries, parsed = {}, []
    for season in seasons:
        entry, data = _verified_entry(root, manifest, season)
        entries[season] = entry
        parsed.extend(parse_season(json.loads(data), season))

    with conn.transaction():
        _seed_teams(conn, root)
        file_ids = {season: _register_source_file(conn, manifest, entry) for season, entry in entries.items()}
        _stage_rows(conn, manifest["provider"], parsed, file_ids)
        _refuse_changed_rows(conn, config["competition"])
        inserted = _insert_new_rows(conn, config["competition"])

        failed = [check.name for check in run_checks(conn, root, seasons) if not check.passed]
        if failed:
            raise SourceDataError(f"post-load checks failed, nothing was saved: {', '.join(failed)}")

    return LoadSummary(files=len(entries), staged=len(parsed), inserted=inserted,
                       already_present=len(parsed) - inserted)
