"""The football-data.org download, against a fake local server.

No test talks to the real API, so the suite runs offline and never uses a real key.
"""
import json
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from soccer_forecasting.live_source import LiveSourceError, fetch_snapshot, read_api_key, season_start_year

KEY = "testkey123"
MATCHES = {"matches": [{"id": 1, "status": "FINISHED"}, {"id": 2, "status": "TIMED"}, {"id": 3, "status": "TIMED"}]}
TEAMS = {"teams": [{"id": 57, "name": "Arsenal FC"}, {"id": 61, "name": "Chelsea FC"}]}


class FakeApi(BaseHTTPRequestHandler):
    status, bodies, seen = 200, {}, []

    def do_GET(self):
        FakeApi.seen.append((self.path, self.headers.get("X-Auth-Token")))
        name = "teams" if "/teams" in self.path else "matches"
        body = FakeApi.bodies.get(name, b"{}")
        self.send_response(FakeApi.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Requests-Available-Minute", "8")
        self.send_header("X-Authenticated-Client", "someone@example.com")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    FakeApi.status = 200
    FakeApi.bodies = {"matches": json.dumps(MATCHES).encode(), "teams": json.dumps(TEAMS).encode()}
    FakeApi.seen = []
    server = HTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v4"
    server.shutdown()


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".env").write_text(f"# local secrets\nFOOTBALL_DATA_API_KEY={KEY}\n")
    return tmp_path


NOW = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)


def test_key_from_env_file(root):
    assert read_api_key(root) == KEY


def test_environment_variable_wins(root, monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fromenv")
    assert read_api_key(root) == "fromenv"


def test_missing_key_explains_what_to_do(tmp_path, monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    with pytest.raises(LiveSourceError, match=".env"):
        read_api_key(tmp_path)


@pytest.mark.parametrize("day, season", [(date(2026, 11, 10), 2026), (date(2027, 3, 1), 2026), (date(2027, 7, 1), 2027)])
def test_season_start_year(day, season):
    assert season_start_year(day) == season


def test_snapshot_saves_raw_responses_and_manifest(api, root):
    result = fetch_snapshot(root, 2026, base_url=api, now=NOW)
    folder = root / "data" / "live" / "football-data-org" / "20261110T220000Z"
    assert result["folder"] == folder
    assert json.loads((folder / "matches.json").read_bytes()) == MATCHES
    assert json.loads((folder / "teams.json").read_bytes()) == TEAMS
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["matches"] == 3 and manifest["teams"] == 2
    assert manifest["statuses"] == {"FINISHED": 1, "TIMED": 2}
    assert [f["endpoint"] for f in manifest["files"]] == ["/competitions/PL/matches?season=2026",
                                                         "/competitions/PL/teams?season=2026"]
    assert len(manifest["files"][0]["sha256"]) == 64


def test_key_is_sent_in_the_header_and_never_saved(api, root):
    fetch_snapshot(root, 2026, base_url=api, now=NOW)
    assert [token for _, token in FakeApi.seen] == [KEY, KEY]
    for path in (root / "data").rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert KEY not in text and "someone@example.com" not in text


@pytest.mark.parametrize("status, hint", [(403, "check the key"), (429, "rate limit"), (500, "unexpected")])
def test_http_errors_save_nothing(api, root, status, hint):
    FakeApi.status = status
    FakeApi.bodies["matches"] = f'{{"message": "bad token {KEY}"}}'.encode()
    with pytest.raises(LiveSourceError, match=hint) as caught:
        fetch_snapshot(root, 2026, base_url=api, now=NOW)
    assert KEY not in str(caught.value)
    assert not (root / "data").exists()


def test_unexpected_body_saves_nothing(api, root):
    FakeApi.bodies["teams"] = b'{"error": "nope"}'
    with pytest.raises(LiveSourceError, match="no 'teams' list"):
        fetch_snapshot(root, 2026, base_url=api, now=NOW)
    assert not (root / "data").exists()


def test_existing_snapshot_is_never_overwritten(api, root):
    fetch_snapshot(root, 2026, base_url=api, now=NOW)
    with pytest.raises(FileExistsError):
        fetch_snapshot(root, 2026, base_url=api, now=NOW)
