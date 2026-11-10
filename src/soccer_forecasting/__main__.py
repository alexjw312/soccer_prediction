"""Command line: python -m soccer_forecasting init-db | load-history | check"""
import argparse
import os
import sys

import psycopg

from . import PROJECT_ROOT
from .checks import run_checks
from .loading import apply_schema, load_history, read_json
from .parsing import SourceDataError


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m soccer_forecasting", description=__doc__)
    parser.add_argument("command", choices=["init-db", "load-history", "check"])
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL first, for example "
                     "postgresql://soccer:dev@localhost:5432/soccer")

    with psycopg.connect(url, autocommit=True) as conn:
        if args.command == "init-db":
            try:
                apply_schema(conn, PROJECT_ROOT)
            except psycopg.errors.DuplicateTable as error:
                print(f"This database already has the schema: {error}", file=sys.stderr)
                return 1
            print("Schema created.")
            return 0

        if args.command == "load-history":
            try:
                summary = load_history(conn, PROJECT_ROOT)
            except SourceDataError as error:
                print(f"Load refused, nothing was saved: {error}", file=sys.stderr)
                return 1
            print(f"Read {summary.staged} matches from {summary.files} files: "
                  f"{summary.inserted} inserted, {summary.already_present} already present.")
            return 0

        seasons = read_json(PROJECT_ROOT / "config" / "milestone0.json")["initial_modeling_seasons"]
        results = run_checks(conn, PROJECT_ROOT, seasons)
        for result in results:
            print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}  ({len(result.violations)} violations)")
            for row in result.violations[:5]:
                print(f"      {row}")
        return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
