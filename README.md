SOCCER PREDICTION
=================

Start with docs/MILESTONE_0_REPORT.md, which records how the data source was chosen and audited.

This package contains a source audit, a historical PostgreSQL database, saved team-form features, a class-frequency baseline, and model selection between logistic regression and XGBoost on the validation season. The one-time final test on 2025/26 is built but has not been run. The selected cohort is 2020/21–2025/26, with 2,280 Premier League matches.

**Build the historical database**

You need Python 3.10 or newer and a PostgreSQL 16 server. From the soccer-forecasting folder:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[dev]"

On macOS, XGBoost also needs OpenMP from Homebrew (https://brew.sh): run brew install libomp once. Without it, import xgboost fails.

If you have Docker, this starts a local PostgreSQL server:

    docker run --name soccer-pg -e POSTGRES_USER=soccer -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=soccer -p 5432:5432 -d postgres:16

Then create the tables, load the six seasons, and run the data checks. Instead of the export line, you can put DATABASE_URL=... (and TEST_DATABASE_URL=...) in the project's .env file once; a value set in the terminal wins.

    export DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer
    python -m soccer_forecasting init-db
    python -m soccer_forecasting load-history
    python -m soccer_forecasting check

Run load-history a second time to see idempotency: it reports 0 inserted and 2,280 already present. On Windows PowerShell, set the variable with $env:DATABASE_URL="postgresql://..." instead of export.

init-db applies each numbered file in sql/ once and records it in the schema_migrations table. Running it again applies only new files, so a database built for an earlier milestone is upgraded in place without reloading. A Milestone 1 database, which predates the migrations table, is recognized and recorded as having 001 applied.

**Build features and score the baseline**

    python -m soccer_forecasting build-features
    python -m soccer_forecasting evaluate-baseline

build-features saves two rows per fixture in team_form_snapshots, one per side: games, points per game, goals for and goals against over each team's last 5 and last 10 matches dated at least two days before the fixture. Teams with no earlier match keep missing values. Running it again inserts nothing, and it refuses to overwrite a saved feature with a different value.

evaluate-baseline fits home/draw/away shares on the training seasons, scores them on 2024/25 with multiclass log loss and a bootstrap interval, and writes reports/baseline_validation.md. It refuses to read 2025/26 results, which are reserved for the final evaluation.

**Choose a model, then test it once**

    python -m soccer_forecasting select-model

select-model follows config/milestone3.json, which was written before any model was scored. It tunes logistic regression and XGBoost on expanding season folds inside the training seasons, refits each on all four training seasons, and scores them on 2024/25. A more complex model replaces a simpler one only if its paired 95% interval for the log-loss difference is entirely below zero. The choice is frozen in reports/frozen_selection.json, the full comparison is in reports/model_validation.md, and fitted models go to artifacts/ with metadata.

    python -m soccer_forecasting evaluate-final --confirm-final-test

evaluate-final refits the frozen choice and the baseline on 2020/21–2024/25 and scores them once on 2025/26. It refuses to run without the flag, without a frozen selection, if the protocol or the training data changed after selection, or a second time. Once it has run, select-model is closed, because changing the model after seeing the test season would be choosing it with the test season.

**Run the tests**

The database tests need a separate database. Each test creates its own temporary PostgreSQL schema and drops it afterwards.

    docker exec soccer-pg createdb -U soccer soccer_test
    export TEST_DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer_test
    pytest

Output from the loader, the checks, the feature build and the tests reports counts and row positions only, so running them does not display 2025/26 outcomes.

**Run the saved audit**

Use Python 3.10 or newer. The audit uses only the standard library.

Open a terminal in the extracted soccer-forecasting folder and run:

    python3 scripts/audit_data.py

This verifies the saved file hashes and regenerates audit/data_audit.json and audit/coverage.md without downloading anything.

To fetch the pinned snapshot when starting with only the script and configuration, run:

    python3 scripts/audit_data.py --download

The downloader uses the pinned commit in config/milestone0.json. It reuses verified existing files. It is not a live-update command. If a manifest already exists, all files it records must still be present and match their hashes; investigate missing or altered files rather than bypassing verification.

**Download live fixtures and results (Milestone 4, step 1)**

Register for a free key at football-data.org and put it in a file called .env in the project folder, as one line: FOOTBALL_DATA_API_KEY=your-key. The .env file is ignored by Git and must never be committed. Then:

    python -m soccer_forecasting fetch-live

This makes two requests (the free plan allows 10 a minute) and saves a snapshot of this season's Premier League matches and teams in data/live/football-data-org/<UTC time>/, with a manifest of URLs, times and hashes. Snapshots are never overwritten. They are kept out of Git because football-data.org's terms govern their data. Football data provided by the Football-Data.org API.

**Load live snapshots into the database (Milestone 4, step 2)**

    python -m soccer_forecasting init-db
    python -m soccer_forecasting load-live

init-db applies 003_live.sql. load-live loads every snapshot not yet loaded, oldest first, each in its own transaction. New fixtures are inserted and tied to their football-data.org id. Fixtures not yet played take the newest date, kickoff and status. A completed result never changes afterwards; a different score stops the load for a person to check. Every fixture's state in every snapshot is kept in match_observations, and the result_first_seen view gives the time each result was first downloaded, which live forecasts will use as their cutoff. Unknown teams, unknown statuses, changed files and out-of-order snapshots stop the load. Match dates are London dates of the UTC kickoff.

Live fixtures never get features from build-features, which covers only the historical seasons. They get their features at forecast time, from results already downloaded by then.

**Forecast real matches (Milestone 4, step 3)**

    python -m soccer_forecasting init-db
    python -m soccer_forecasting train-deployment
    python -m soccer_forecasting predict --preview --date 2026-11-21

init-db applies 004_predictions.sql. train-deployment refits the frozen model with its frozen settings on every historical season. It is refused until the one-time final test has run, because it trains on the test season, and it is recorded in model_runs as a separate training run from the evaluated one. Running it again reuses the recorded model.

predict --preview shows forecasts for any day without saving them. On match day, run:

    python -m soccer_forecasting daily

daily downloads fresh data, loads it, and saves forecasts for that day's remaining matches (London date). A saved forecast:

- is made on match day, before kickoff (the database refuses one made at or after kickoff);
- uses form from matches dated at least two days earlier, and only results that had been downloaded before the forecast was made, through the same form_v1 query as the historical features;
- needs a download from the last 30 hours, and no earlier fixture still missing its result;
- is never changed: running again the same day adds nothing, and if a match moves to another day a new forecast is added for the new day.

Every daily or predict run is logged in job_runs, including failures. prediction_results shows each forecast next to the result and its log loss once the match is played.

**API and dashboard (Milestone 4, step 4)**

    python -m soccer_forecasting serve

Then open http://127.0.0.1:8000 for the dashboard, or http://127.0.0.1:8000/docs for the API's interactive documentation. Press Ctrl+C to stop. The service only listens on your own computer.

| Address | What it returns |
| --- | --- |
| / | Dashboard: upcoming fixtures with their forecasts once published, recent forecasts next to results, performance so far, data freshness |
| /health | Database status, newest download and its age, deployment model run, last job run |
| /forecasts?date=YYYY-MM-DD | Saved forecasts for one match day (default: today in London) |
| /forecasts/upcoming | Saved forecasts for matches not yet started |
| /fixtures/upcoming | The next fixtures, each with its forecast or null until match day |
| /matches/{id}/forecasts | Every forecast saved for one match |
| /performance | Log loss and accuracy of forecasts for matches played on their forecast day, against historical shares and one third each |

The service only reads saved forecasts and never recomputes one, so it always shows exactly what was recorded before kickoff. Its database session is read-only. Every response carries the attribution Football data provided by the Football-Data.org API.

**Files**

| Path | Purpose |
| --- | --- |
| config/milestone0.json | Source commit, selected seasons, split, forecast policy |
| config/team_aliases.csv | Maps each provider's team name to one canonical team |
| sql/001_schema.sql | Milestone 1 tables, constraints and the team_matches view, with the reasoning in comments |
| sql/002_features.sql | team_form_snapshots table and the fixture_features view |
| sql/003_live.sql | kickoff_utc, provider_matches, match_observations and the result_first_seen view |
| sql/004_predictions.sql | model_runs, predictions, the prediction_results view and job_runs |
| config/live.json | Live season, provider and how provider statuses map to ours |
| sql/features/form_v1.sql | The feature query: window functions plus an as-of join at the cutoff |
| sql/checks/ | Post-load checks; each query returns the rows that break a rule |
| src/soccer_forecasting/ | Parser, loader, checks, migrations, features, baseline and command line |
| tests/ | Parser, schema, loader, migration, feature and baseline tests |
| reports/baseline_validation.md | Baseline validation results, regenerated by evaluate-baseline |
| config/milestone3.json | Milestone 3 protocol: tuning folds, grids, selection rule, bootstrap, final-test rules |
| reports/model_validation.md | Tuning, validation comparison, selection, draws, short history, calibration |
| reports/frozen_selection.json | The chosen model and settings, with hashes that evaluate-final checks |
| artifacts/ | Fitted models with metadata (not in Git; rebuilt by the commands) |
| pyproject.toml | Pinned dependencies and package definition |
| scripts/audit_data.py | Reproducible downloader and structural audit |
| data/manifest.json | Source paths, URLs, hashes, and retrieval metadata |
| data/raw/openfootball/ | Original JSON snapshots and upstream license/README |
| audit/data_audit.json | Detailed findings, field presence, score formats, and errors |
| audit/coverage.md | Compact season table |
| docs/MILESTONE_0_REPORT.md | Decisions, data dictionary, limitations, next step |

A successful exit means the configured initial modeling seasons pass the audit. The report still retains problems in excluded seasons. In particular, 2019/20 has 66 out-of-window dates; 2026/27 is an incomplete, stale snapshot.

Both nested full-time scores and valid bare two-integer score arrays are recognized. Missing scores never become 0–0 draws. Dates are checked against the expected August-to-July season window for this audited league/cohort. The saved audit withholds outcome totals for the 2025/26 final test season.

The 28 names in the selected cohort are source team names. External provider IDs and aliases have not yet been reconciled. Source data contains no explicit timezone offsets or historical publication timestamps.

**Data attribution**

Historical data: OpenFootball football.json, CC0 1.0, commit e6744429ee395bc86f247348c6184bb08d4eb361.

Upstream: https://github.com/openfootball/football.json

The upstream license is preserved under data/raw/openfootball/<commit>/LICENSE.md. Other candidate providers have their own terms. No raw Football-Data.co.uk data is included in this package.

**Next**

Milestone 4 is under way: live fixtures, saved predictions, an API and a dashboard.

