"""Shared helpers for live-data tests: a generated season and snapshot folders.

The fixture list is a double round robin of the real 20 club ids, with
invented scores, so no football-data.org data is stored in the repository.
"""
import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone

from soccer_forecasting import PROJECT_ROOT

SEASON_START = datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)


def round_kickoff(round_index: int) -> datetime:
    """Round 1 is index 0. One round a week, every kickoff at 14:00 UTC."""
    return SEASON_START + timedelta(days=7 * round_index)


def club_ids():
    with (PROJECT_ROOT / "config" / "team_aliases.csv").open(newline="") as handle:
        return [(int(r["alias"]), r["team_name"]) for r in csv.DictReader(handle) if r["provider"] == "football-data.org"]


def season(finished_rounds=10):
    """380 fixtures, one round a week, each club once per round."""
    clubs = club_ids()
    ids, names = [c for c, _ in clubs], dict(clubs)
    rounds, rotating = [], ids[1:]
    for _ in range(19):
        order = [ids[0]] + rotating
        rounds.append([(order[i], order[19 - i]) for i in range(10)])
        rotating = rotating[-1:] + rotating[:-1]
    rounds += [[(away, home) for home, away in pairs] for pairs in rounds]
    matches = []
    for k, pairs in enumerate(rounds):
        kickoff = round_kickoff(k)
        for i, (home, away) in enumerate(pairs):
            done = k < finished_rounds
            matches.append({
                "id": 600000 + 10 * k + i, "competition": {"code": "PL"}, "season": {"startDate": "2026-08-21"},
                "stage": "REGULAR_SEASON", "matchday": k + 1, "utcDate": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "FINISHED" if done else "TIMED",
                "homeTeam": {"id": home, "name": names[home]}, "awayTeam": {"id": away, "name": names[away]},
                "score": {"duration": "REGULAR",
                          "fullTime": {"home": (k + i) % 4 if done else None, "away": (k * i) % 3 if done else None}},
            })
    return {"matches": matches}


def write_snapshot(root, name, payload, retrieved, season_year=2026):
    folder = root / "data" / "live" / "football-data-org" / name
    folder.mkdir(parents=True)
    data = json.dumps(payload).encode()
    (folder / "matches.json").write_bytes(data)
    (folder / "manifest.json").write_text(json.dumps({
        "provider": "football-data.org", "season": season_year,
        "files": [{"file": "matches.json", "endpoint": "/competitions/PL/matches?season=2026",
                   "retrieved_at_utc": retrieved, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}))
    return folder
