"""Command line: python -m soccer_forecasting <command>

  init-db            create the schema, or apply any new migrations
  load-history       load the selected historical seasons
  check              run the post-load data checks
  build-features     compute and save team-form features (form_v1)
  evaluate-baseline  score the class-frequency baseline on the validation season
  select-model       tune, compare and choose a model on validation (config/milestone3.json)
  evaluate-final     score the frozen choice once on the final test season;
                     needs --confirm-final-test and refuses a second run
  fetch-live         download this season's fixtures and results from
                     football-data.org (needs FOOTBALL_DATA_API_KEY in .env)
"""
import argparse
import os
import sys

import psycopg

from . import PROJECT_ROOT
from .baseline import evaluate_baseline, write_report
from .checks import run_checks
from .features import FEATURE_VERSION, build_features
from .final_evaluation import evaluate_final
from .live_source import LiveSourceError, fetch_snapshot, season_start_year
from .model_selection import NAMES, ProtocolError, run_selection
from .loading import load_history, read_json
from .migrations import MigrationError, apply_migrations
from .parsing import SourceDataError

COMMANDS = ["init-db", "load-history", "check", "build-features", "evaluate-baseline",
            "select-model", "evaluate-final", "fetch-live"]


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m soccer_forecasting", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--confirm-final-test", action="store_true",
                        help="required by evaluate-final, which can only run once")
    parser.add_argument("--season", type=int,
                        help="fetch-live: season start year, e.g. 2026 for 2026/27 (default: current)")
    args = parser.parse_args()

    if args.command == "fetch-live":  # needs no database
        from datetime import date
        season = args.season or season_start_year(date.today())
        try:
            result = fetch_snapshot(PROJECT_ROOT, season)
        except LiveSourceError as error:
            print(f"Download failed, nothing was saved: {error}", file=sys.stderr)
            return 1
        statuses = ", ".join(f"{n} {s}" for s, n in sorted(result["statuses"].items()))
        print(f"Season {season}/{(season + 1) % 100:02d}: {result['matches']} matches ({statuses}), "
              f"{result['teams']} teams.")
        print(f"Saved to {result['folder'].relative_to(PROJECT_ROOT)}")
        remaining = result["files"][-1]["response_headers"].get("X-Requests-Available-Minute")
        if remaining:
            print(f"Requests left this minute: {remaining}")
        return 0

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
                  f"These stay missing in the database; logistic regression fills them with training "
                  f"means and XGBoost handles them directly.")
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

        if args.command == "select-model":
            try:
                result = run_selection(conn, PROJECT_ROOT)
            except (ProtocolError, RuntimeError) as error:
                print(f"Model selection refused: {error}", file=sys.stderr)
                return 1
            scores, v = result["scores"], result["validation"]
            print(f"Validation {', '.join(v['seasons'])} ({v['matches']} matches), log loss, lower is better:")
            for name, score in scores.items():
                vs = score.get("vs_baseline")
                extra = (f"  versus baseline {vs['mean']:+.4f} "
                         f"({vs['interval'][0]:+.4f} to {vs['interval'][1]:+.4f})") if vs else ""
                print(f"  {NAMES[name]:<25} {score['log_loss']:.4f}{extra}")
            print(f"Selected: {NAMES[result['selection']['selected']]}. "
                  f"Frozen in reports/frozen_selection.json; details in reports/model_validation.md")
            return 0

        if args.command == "evaluate-final":
            try:
                result = evaluate_final(conn, PROJECT_ROOT, confirm=args.confirm_final_test)
            except (ProtocolError, RuntimeError) as error:
                print(f"Final test refused: {error}", file=sys.stderr)
                return 1
            for name, score in result["scores"].items():
                print(f"  {NAMES[name]:<25} test log loss {score['log_loss']:.4f}")
            print("Report written to reports/final_test.md. Selection is now closed.")
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
