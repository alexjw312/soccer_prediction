"""Load downloaded football-data.org snapshots into the database.

Snapshots are loaded oldest first, each in its own transaction. For every
fixture in a snapshot:

- a fixture seen for the first time is inserted and tied to its provider id;
- a fixture not yet completed takes the newest date, kickoff and status;
- a completed result never changes afterwards; a different score stops the load;
- every fixture's state in every snapshot is recorded in match_observations.

Anything unexpected, such as an unknown team, an unknown status, a changed
file or an out-of-order snapshot, stops the load and saves nothing from that
snapshot.

Every downloaded file is also copied into the database (live_download_files),
including snapshots loaded before that table existed, so the raw data
survives on computers that start empty. export_live writes the copies back
out as snapshot folders.

After loading, each club's manager and squad are copied from the newest
stored teams file onto teams (update_club_people), for the dashboard.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .checks import run_checks
from .loading import _seed_teams, read_json
from .parsing import SourceDataError

SNAPSHOTS = Path("data") / "live" / "football-data-org"


@dataclass(frozen=True)
class LiveMatch:
    row_index: int
    provider_match_id: str
    home_provider_id: str
    away_provider_id: str
    home_name: str
    away_name: str
    kickoff_utc: datetime
    match_date: date
    local_time: str
    matchday: int
    provider_status: str
    status: str
    home_goals: int | None
    away_goals: int | None


@dataclass
class SnapshotSummary:
    snapshot: str
    loaded: bool = False
    fixtures: int = 0
    inserted: int = 0
    newly_completed: int = 0
    rescheduled: int = 0
    other_updates: int = 0
    files_stored: int = 0


def live_config(root: Path) -> dict:
    return read_json(root / "config" / "live.json")


def local_date_and_time(kickoff_utc: datetime, zone: str) -> tuple[date, str]:
    """A match's date is its date in the competition's own time zone."""
    local = kickoff_utc.astimezone(ZoneInfo(zone))
    return local.date(), local.strftime("%H:%M")


def parse_matches(payload: dict, config: dict) -> list[LiveMatch]:
    parsed = []
    for index, m in enumerate(payload.get("matches", [])):
        where = f"match {m.get('id', '?')}"
        try:
            if m["competition"]["code"] != config["api_competition"]:
                raise SourceDataError(f"{where}: competition {m['competition']['code']}")
            if not m["season"]["startDate"].startswith(str(config["season_start_year"])):
                raise SourceDataError(f"{where}: season starting {m['season']['startDate']}")
            if m["stage"] != "REGULAR_SEASON":
                raise SourceDataError(f"{where}: stage {m['stage']}")
            provider_status = m["status"]
            if provider_status not in config["status_map"]:
                raise SourceDataError(f"{where}: status {provider_status} needs a decision before it can be loaded")
            status = config["status_map"][provider_status]
            kickoff = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                raise SourceDataError(f"{where}: kickoff has no time zone")
            home_goals = away_goals = None
            if status == "completed":
                if m["score"]["duration"] != "REGULAR":
                    raise SourceDataError(f"{where}: duration {m['score']['duration']}")
                full_time = m["score"]["fullTime"]
                home_goals, away_goals = full_time["home"], full_time["away"]
                if not all(type(g) is int and g >= 0 for g in (home_goals, away_goals)):
                    raise SourceDataError(f"{where}: unusable full-time score {full_time}")
            match_date, local_time = local_date_and_time(kickoff, config["local_timezone"])
            parsed.append(LiveMatch(
                row_index=index, provider_match_id=str(m["id"]),
                home_provider_id=str(m["homeTeam"]["id"]), away_provider_id=str(m["awayTeam"]["id"]),
                home_name=m["homeTeam"]["name"], away_name=m["awayTeam"]["name"],
                kickoff_utc=kickoff, match_date=match_date, local_time=local_time, matchday=int(m["matchday"]),
                provider_status=provider_status, status=status, home_goals=home_goals, away_goals=away_goals))
        except (KeyError, TypeError, ValueError) as error:
            raise SourceDataError(f"{where}: {error!r}") from error
    if not parsed:
        raise SourceDataError("snapshot has no matches")
    return parsed


def snapshot_folders(root: Path) -> list[Path]:
    base = root / SNAPSHOTS
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / "manifest.json").exists())


def _verified_matches(folder: Path, config: dict) -> tuple[dict, dict, bytes]:
    manifest = read_json(folder / "manifest.json")
    if manifest.get("provider") != config["provider"] or manifest.get("season") != config["season_start_year"]:
        raise SourceDataError(f"{folder.name}: snapshot is for {manifest.get('provider')} season "
                              f"{manifest.get('season')}, not the configured live season")
    entry = next((f for f in manifest["files"] if f["file"] == "matches.json"), None)
    if entry is None:
        raise SourceDataError(f"{folder.name}: manifest lists no matches.json")
    data = (folder / "matches.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SourceDataError(f"{folder.name}: matches.json does not match its manifest hash")
    return manifest, entry, data


def _store_files(conn, provider: str, folder: Path, manifest: dict) -> int:
    """Copy each file of a snapshot into the database, checking it against the manifest first."""
    hashes = {f["file"]: f["sha256"] for f in manifest["files"]}
    stored = 0
    for name in sorted(hashes) + ["manifest.json"]:
        path = folder / name
        if not path.exists():
            raise SourceDataError(f"{folder.name}: {name} is listed in the manifest but missing")
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if name in hashes and sha != hashes[name]:
            raise SourceDataError(f"{folder.name}: {name} does not match its manifest hash")
        inserted = conn.execute(
            """INSERT INTO live_download_files (provider, snapshot, file_name, content) VALUES (%s, %s, %s, %s)
               ON CONFLICT (provider, snapshot, file_name) DO NOTHING""", (provider, folder.name, name, data)).rowcount
        if not inserted:
            kept = conn.execute(
                """SELECT content_sha256 FROM live_download_files
                   WHERE provider = %s AND snapshot = %s AND file_name = %s""", (provider, folder.name, name)).fetchone()[0]
            if kept != sha:
                raise SourceDataError(f"{folder.name}: {name} differs from the copy already in the database")
        stored += inserted
    return stored


def export_live(conn, root: Path) -> list[str]:
    """Write every snapshot kept in the database back out under data/live, checking each file's hash.

    Folders that already exist are left alone if their files match, and refused if they differ.
    Returns the names of the folders written.
    """
    provider = live_config(root)["provider"]
    written = []
    snapshots = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot FROM live_download_files WHERE provider = %s ORDER BY snapshot", (provider,))]
    for snapshot in snapshots:
        files = conn.execute(
            """SELECT file_name, content, content_sha256 FROM live_download_files
               WHERE provider = %s AND snapshot = %s ORDER BY file_name""", (provider, snapshot)).fetchall()
        folder = root / SNAPSHOTS / snapshot
        if folder.exists():
            for name, _, sha in files:
                path = folder / name
                if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise SourceDataError(f"{snapshot}: {name} on disk differs from the copy in the database")
            continue
        folder.mkdir(parents=True)
        for name, content, sha in files:
            data = bytes(content)
            if hashlib.sha256(data).hexdigest() != sha:
                raise SourceDataError(f"{snapshot}: the stored {name} does not match its hash")
            (folder / name).write_bytes(data)
        written.append(snapshot)
    return written


def _team_ids(conn, provider: str, rows: list[LiveMatch]) -> dict[str, int]:
    wanted = {(r.home_provider_id, r.home_name) for r in rows} | {(r.away_provider_id, r.away_name) for r in rows}
    found = dict(conn.execute(
        "SELECT alias, team_id FROM team_aliases WHERE provider = %s AND alias = ANY(%s)",
        (provider, [pid for pid, _ in wanted])).fetchall())
    missing = sorted(f"{name} (id {pid})" for pid, name in wanted if pid not in found)
    if missing:
        raise SourceDataError(f"teams with no row in config/team_aliases.csv: {', '.join(missing)}")
    return found


def _load_one(conn, root: Path, folder: Path, config: dict, competition: str) -> SnapshotSummary:
    summary = SnapshotSummary(snapshot=folder.name)
    provider = config["provider"]
    manifest, entry, data = _verified_matches(folder, config)

    seen = conn.execute(
        "SELECT sha256 FROM source_files WHERE provider = %s AND source_commit = %s AND source_path = 'matches.json'",
        (provider, folder.name)).fetchone()
    if seen:
        if seen[0] != entry["sha256"]:
            raise SourceDataError(f"{folder.name}: differs from the copy already loaded")
        with conn.transaction():  # snapshots loaded before 005_stored_files get their copy now
            summary.files_stored = _store_files(conn, provider, folder, manifest)
        return summary
    newest = conn.execute("SELECT max(source_commit) FROM source_files WHERE provider = %s", (provider,)).fetchone()[0]
    if newest and folder.name < newest:
        raise SourceDataError(f"{folder.name} is older than {newest}, which is already loaded; snapshots "
                              f"must be loaded in time order so the history stays true")

    rows = parse_matches(json.loads(data), config)
    season = config["season"]
    with conn.transaction():
        _seed_teams(conn, root)
        team_id = _team_ids(conn, provider, rows)
        summary.files_stored = _store_files(conn, provider, folder, manifest)
        file_id = conn.execute(
            """INSERT INTO source_files (provider, source_commit, source_path, url, sha256, byte_count, retrieved_at_utc)
               VALUES (%s, %s, 'matches.json', %s, %s, %s, %s) RETURNING source_file_id""",
            (provider, folder.name, entry["endpoint"], entry["sha256"], entry["bytes"],
             entry["retrieved_at_utc"])).fetchone()[0]

        for r in rows:
            home, away = team_id[r.home_provider_id], team_id[r.away_provider_id]
            new = (r.match_date, r.local_time, f"Matchday {r.matchday}", r.kickoff_utc, r.status, r.home_goals, r.away_goals)
            current = conn.execute(
                """SELECT m.match_id, m.home_team_id, m.away_team_id, m.match_date, m.source_time, m.source_round,
                          m.kickoff_utc, m.status, m.home_goals, m.away_goals
                   FROM provider_matches p JOIN matches m USING (match_id)
                   WHERE p.provider = %s AND p.provider_match_id = %s""",
                (provider, r.provider_match_id)).fetchone()

            if current is None:
                clash = conn.execute(
                    """SELECT match_id FROM matches WHERE competition = %s AND season = %s
                       AND home_team_id = %s AND away_team_id = %s""", (competition, season, home, away)).fetchone()
                if clash:
                    raise SourceDataError(f"match {r.provider_match_id}: {r.home_name} v {r.away_name} already "
                                          f"exists under another provider id")
                match_id = conn.execute(
                    """INSERT INTO matches (competition, season, home_team_id, away_team_id, match_date, source_time,
                                            source_round, kickoff_utc, status, home_goals, away_goals,
                                            source_file_id, source_row_index)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING match_id""",
                    (competition, season, home, away, *new, file_id, r.row_index)).fetchone()[0]
                conn.execute("INSERT INTO provider_matches (provider, provider_match_id, match_id) VALUES (%s, %s, %s)",
                             (provider, r.provider_match_id, match_id))
                summary.inserted += 1
            else:
                match_id, old_home, old_away, *old = current
                if (old_home, old_away) != (home, away):
                    raise SourceDataError(f"match {r.provider_match_id}: the teams changed")
                old_status, old_goals = old[4], (old[5], old[6])
                if old_status == "completed":
                    if r.status != "completed" or (r.home_goals, r.away_goals) != old_goals:
                        raise SourceDataError(f"match {r.provider_match_id}: {r.home_name} v {r.away_name} was "
                                              f"recorded as completed, but this snapshot shows a different state "
                                              f"({r.provider_status}); a person needs to check it")
                elif tuple(old) != new:
                    conn.execute(
                        """UPDATE matches SET match_date = %s, source_time = %s, source_round = %s, kickoff_utc = %s,
                                              status = %s, home_goals = %s, away_goals = %s,
                                              source_file_id = %s, source_row_index = %s
                           WHERE match_id = %s""", (*new, file_id, r.row_index, match_id))
                    if r.status == "completed":
                        summary.newly_completed += 1
                    elif old[3] != r.kickoff_utc:
                        summary.rescheduled += 1
                    else:
                        summary.other_updates += 1

            conn.execute(
                """INSERT INTO match_observations (source_file_id, provider, provider_match_id, match_id, observed_at_utc,
                                                   provider_status, status, kickoff_utc, home_goals, away_goals)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (file_id, provider, r.provider_match_id, match_id, entry["retrieved_at_utc"],
                 r.provider_status, r.status, r.kickoff_utc, r.home_goals, r.away_goals))

        cohort = read_json(root / "config" / "milestone0.json")["initial_modeling_seasons"]
        failed = [c.name for c in run_checks(conn, root, cohort) if not c.passed]
        if failed:
            raise SourceDataError(f"{folder.name}: checks failed after loading, nothing was saved: {', '.join(failed)}")

    summary.loaded, summary.fixtures = True, len(rows)
    return summary


def _player(entry: dict) -> dict:
    return {"name": str(entry.get("name") or "").strip(), "position": entry.get("position"),
            "date_of_birth": entry.get("dateOfBirth"), "nationality": entry.get("nationality")}


def update_club_people(conn, provider: str) -> int:
    """Copy each club's manager and squad from the newest stored teams file onto teams.

    Returns how many clubs were updated. The values are display data for the dashboard
    and no forecast uses them, so a teams file that cannot be read changes nothing
    rather than stopping the daily job. Clubs without a row in team_aliases are skipped.
    """
    newest = conn.execute(
        """SELECT snapshot, content FROM live_download_files WHERE provider = %s AND file_name = 'teams.json'
           ORDER BY snapshot DESC LIMIT 1""", (provider,)).fetchone()
    if newest is None:
        return 0
    snapshot, content = newest
    try:
        clubs = json.loads(bytes(content))["teams"]
    except (ValueError, KeyError, TypeError):
        return 0
    updated = 0
    for club in clubs if isinstance(clubs, list) else []:
        if not isinstance(club, dict) or club.get("id") is None:
            continue
        coach = club.get("coach") if isinstance(club.get("coach"), dict) else {}
        squad = [_player(p) for p in club.get("squad") or [] if isinstance(p, dict) and p.get("name")]
        updated += conn.execute(
            """UPDATE teams t SET coach_name = %s, coach_nationality = %s, squad = %s::jsonb, squad_snapshot = %s
               FROM team_aliases a WHERE a.provider = %s AND a.alias = %s AND a.team_id = t.team_id""",
            (str(coach.get("name") or "").strip() or None, coach.get("nationality") or None, json.dumps(squad),
             snapshot, provider, str(club["id"]))).rowcount
    return updated


def load_live(conn, root: Path) -> list[SnapshotSummary]:
    config = live_config(root)
    competition = read_json(root / "config" / "milestone0.json")["competition"]
    summaries = [_load_one(conn, root, folder, config, competition) for folder in snapshot_folders(root)]
    with conn.transaction():
        update_club_people(conn, config["provider"])
    return summaries
