"""Download Premier League fixtures and results from football-data.org (API v4).

Each run saves one snapshot folder under data/live/football-data-org/, named
by the UTC time it was fetched, with the raw responses and a manifest. Old
snapshots are never overwritten, so later we can tell when a result was first
seen, which the forecast cutoff rules depend on.

The API key is read from the FOOTBALL_DATA_API_KEY environment variable or
from the project's .env file. It is only ever sent in the request header and
is never written to disk or printed.

Saved responses stay out of Git (see .gitignore): football-data.org's terms
govern their data, and its CC0 licence covers only OpenFootball.
"""
import hashlib
import json
import os
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "PL"
KEY_NAME = "FOOTBALL_DATA_API_KEY"
# Response headers worth keeping. Anything identifying the account is left out.
KEPT_HEADERS = ("Date", "Content-Type", "X-API-Version", "X-Requests-Available-Minute", "X-RequestCounter-Reset")


class LiveSourceError(RuntimeError):
    """The download failed or looked wrong; nothing was saved."""


def read_api_key(root: Path) -> str:
    key = os.environ.get(KEY_NAME, "").strip()
    env_file = root / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == KEY_NAME:
                key = value.strip().strip('"').strip("'")
    if not key:
        raise LiveSourceError(f"no API key found: put a line {KEY_NAME}=... in the project's .env file")
    return key


def season_start_year(today: date) -> int:
    """Seasons run August to May, so July onwards belongs to the next season."""
    return today.year if today.month >= 7 else today.year - 1


def _get(url: str, key: str) -> tuple[bytes, dict]:
    request = Request(url, headers={"X-Auth-Token": key, "User-Agent": "soccer-prediction personal project"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(), {h: response.headers.get(h) for h in KEPT_HEADERS if response.headers.get(h)}
    except HTTPError as error:
        detail = error.read(300).decode("utf-8", errors="replace").replace(key, "[key]")
        hints = {400: "check the season", 403: "check the key, or this competition/season is not in your plan",
                 429: "rate limit reached; wait a minute and try again"}
        raise LiveSourceError(f"HTTP {error.code} from {url} ({hints.get(error.code, 'unexpected response')}): "
                              f"{detail}") from None
    except URLError as error:
        raise LiveSourceError(f"could not reach {url}: {error.reason}") from None


def fetch_snapshot(root: Path, season: int, *, base_url: str = API_BASE,
                   now: datetime | None = None) -> dict:
    key = read_api_key(root)
    now = now or datetime.now(timezone.utc)
    endpoints = {"matches": f"/competitions/{COMPETITION}/matches", "teams": f"/competitions/{COMPETITION}/teams"}

    responses = {}
    for name, path in endpoints.items():
        url = f"{base_url}{path}?{urlencode({'season': season})}"
        body, headers = _get(url, key)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise LiveSourceError(f"{name}: response was not JSON") from None
        if not isinstance(payload.get(name), list):
            raise LiveSourceError(f"{name}: response has no '{name}' list")
        responses[name] = {"url": url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "body": body, "payload": payload, "headers": headers}

    folder = root / "data" / "live" / "football-data-org" / now.strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True, exist_ok=False)  # a snapshot is never overwritten
    files = []
    for name, r in responses.items():
        (folder / f"{name}.json").write_bytes(r["body"])
        files.append({"file": f"{name}.json", "endpoint": r["url"].removeprefix(base_url),
                      "retrieved_at_utc": r["retrieved_at_utc"], "bytes": len(r["body"]),
                      "sha256": hashlib.sha256(r["body"]).hexdigest(), "response_headers": r["headers"]})

    matches = responses["matches"]["payload"]["matches"]
    summary = {
        "provider": "football-data.org", "api_version": "v4", "competition": COMPETITION, "season": season,
        "snapshot": folder.name, "files": files,
        "matches": len(matches), "teams": len(responses["teams"]["payload"]["teams"]),
        "statuses": dict(Counter(m.get("status") for m in matches)),
        "attribution": "Football data provided by the Football-Data.org API",
    }
    (folder / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return {**summary, "folder": folder}
