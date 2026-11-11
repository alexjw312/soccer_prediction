"""Command line: python -m soccer_forecasting <command>

  init-db            create the schema, or apply any new migrations
  load-history       load the selected historical seasons
  check              run the post-load data checks
  build-features     compute and save team-form features (form_v1)
  evaluate-baseline  score the class-frequency baseline on the validation season
"""
import argparse
import os
import sys

import psycopg

from . import PROJECT_ROOT
from .baseline import evaluate_baseline, write_report
from .checks import run_checks
from .features import FEATURE_VERSION, build_features
from .loading import load_history, read_json
from .migrations import MigrationError, apply_migrations
from .parsing import SourceDataError

COMMANDS = ["init-db", "load-history", "check", "build-features", "evaluate-baseline"]


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m soccer_forecasting", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL first, for example "
                     "postgresql://soccer:dev@localhost:5432/soccer")

    with psycopg.connect(url, autocommit=True) as conn:
        if args.command == "init-db":
            try:
                result = apply_migrations(conn, PROJECT_ROOT)
            except (MigrationError, psycopg.Error) as error:
                print(f"Schema update failed, nothing was changed by the failing step: {error}", file=sys.stderr)
                return 1
            for version in result["adopted"]:
                print(f"Recorded the existing {version} schema as already applied.")
            for version in result["applied"]:
                print(f"Applied {version}.")
            if not result["applied"]:
                print("Schema is up to date.")
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

        if args.command == "build-features":
            try:
                summary = build_features(conn, PROJECT_ROOT)
            except SourceDataError as error:
                print(f"Feature build refused, nothing was saved: {error}", file=sys.stderr)
                return 1
            print(f"Feature version {FEATURE_VERSION}: {summary.sides} team sides "
                  f"({summary.sides // 2} fixtures), {summary.inserted} inserted, "
                  f"{summary.already_present} already present.")
            print(f"Sides with no earlier match: {summary.sides_without_history}. "
                  f"Sides with fewer than 5: {summary.sides_with_fewer_than_5}. "
                  f"These keep missing values; Milestone 3 decides how to fill them.")
            return 0

        if args.command == "evaluate-baseline":
            result = evaluate_baseline(conn, PROJECT_ROOT)
            _, md_path = write_report(result, PROJECT_ROOT)
            v, u = result["validation"], result["reference_uniform"]
            p = result["training"]["probabilities"]
            print(f"Baseline fitted on {result['training']['matches']} training matches: "
                  f"H {p['H']:.1%}, D {p['D']:.1%}, A {p['A']:.1%}")
            print(f"Validation {', '.join(v['seasons'])} ({v['matches']} matches) log loss: "
                  f"{v['log_loss']:.4f} (95% interval {v['log_loss_95ci'][0]:.4f}–{v['log_loss_95ci'][1]:.4f})")
            print(f"Reference, one third each: {u['log_loss']:.4f}")
            print(f"Report written to {md_path.relative_to(PROJECT_ROOT)}")
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
