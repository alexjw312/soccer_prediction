"""Run the post-load data checks in sql/checks.

Each check is a query that returns the rows breaking a rule, so no rows means
it passed. These cover rules that span many rows, which a table constraint
cannot express because a constraint only looks at one row at a time.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckResult:
    name: str
    violations: list[tuple]

    @property
    def passed(self) -> bool:
        return not self.violations


def run_checks(conn, root: Path, seasons: list[str]) -> list[CheckResult]:
    results = []
    for path in sorted((root / "sql" / "checks").glob("*.sql")):
        rows = conn.execute(path.read_text(encoding="utf-8"), {"seasons": seasons}).fetchall()
        results.append(CheckResult(path.stem, rows))
    return results
