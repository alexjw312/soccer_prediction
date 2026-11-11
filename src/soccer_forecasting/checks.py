"""Run the post-load data checks in sql/checks.

Each check is a query that returns the rows breaking a rule, so no rows means
it passed. These cover rules that span many rows, which a table constraint
cannot express because a constraint only looks at one row at a time.
"""
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckResult:
    name: str
    violations: list[tuple]

    @property
    def passed(self) -> bool:
        return not self.violations


def configured_live_seasons(root: Path) -> list[str]:
    path = root / "config" / "live.json"
    return [json.loads(path.read_text(encoding="utf-8"))["season"]] if path.exists() else []


def run_checks(conn, root: Path, seasons: list[str], live_seasons: list[str] | None = None) -> list[CheckResult]:
    """seasons: the historical cohort. live_seasons: defaults to config/live.json."""
    if live_seasons is None:
        live_seasons = configured_live_seasons(root)
    params = {"seasons": seasons, "live_seasons": live_seasons}
    results = []
    for path in sorted((root / "sql" / "checks").glob("*.sql")):
        rows = conn.execute(path.read_text(encoding="utf-8"), params).fetchall()
        results.append(CheckResult(path.stem, rows))
    return results
