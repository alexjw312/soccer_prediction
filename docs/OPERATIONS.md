Running and operating the project
=================================

Every command, in the order the project was built. The README gives the overview; docs/ARCHITECTURE.md explains how the parts fit together, and docs/EVALUATION.md the model results.

## Set up

You need Python 3.12 or newer and a PostgreSQL 16 server. From the project folder:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[dev]"

The install must be editable (`-e`): the project finds config/ and sql/ next to its source code. On macOS, XGBoost also needs OpenMP from Homebrew (https://brew.sh): run `brew install libomp` once.

With Docker, this starts a local PostgreSQL server:

    docker run --name soccer-pg -e POSTGRES_USER=soccer -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=soccer -p 5432:5432 -d postgres:16

Every command reads DATABASE_URL from the terminal or from a line in the project's .env file (ignored by Git); a value set in the terminal wins.

    export DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer

## Build the historical database

    python -m soccer_forecasting init-db
    python -m soccer_forecasting load-history
    python -m soccer_forecasting check

init-db applies each numbered file in sql/ once and records it in the schema_migrations table, so running it again applies only new files and an older database is upgraded in place without reloading. load-history loads the six selected seasons (2,280 matches) from the pinned OpenFootball snapshot after checking each file's hash; run it again and it reports 0 inserted and 2,280 already present. check runs the post-load checks in sql/checks/, each a query that returns the rows breaking a rule.

## Build features and score the baseline

    python -m soccer_forecasting build-features
    python -m soccer_forecasting evaluate-baseline

build-features saves two rows per fixture in team_form_snapshots, one per side: games, points per game, goals for and goals against over each team's last 5 and last 10 matches dated at least two days before the fixture. Teams with no earlier match keep missing values. Running it again inserts nothing, and it refuses to overwrite a saved feature with a different value.

evaluate-baseline fits home/draw/away shares on the training seasons, scores them on 2024/25 with multiclass log loss and a bootstrap interval, and writes reports/baseline_validation.md. It refuses to read 2025/26 results, which are reserved for the final test.

## Choose a model, then test it once

    python -m soccer_forecasting select-model

select-model follows config/milestone3.json, written before any model was scored. It tunes logistic regression and XGBoost on expanding season folds inside the training seasons, refits each on all four training seasons and scores them on 2024/25. A more complex model replaces a simpler one only if its paired 95% interval for the log-loss difference lies entirely below zero. The choice is frozen in reports/frozen_selection.json and the comparison is in reports/model_validation.md.

    python -m soccer_forecasting evaluate-final --confirm-final-test

evaluate-final refits the frozen choice and the baseline on 2020/21–2024/25 and scores them once on 2025/26, writing reports/final_test.md. It refuses to run without the flag, without a frozen selection, if the protocol or the training data changed after selection, or a second time. It has run (11 November 2026), so select-model is closed: changing the model now would mean choosing it with the test season.

## Run the tests

The database tests need a separate database. Each test creates its own temporary PostgreSQL schema and drops it afterwards.

    docker exec soccer-pg createdb -U soccer soccer_test
    export TEST_DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer_test
    python -m pytest

On your own computer the database tests are skipped without TEST_DATABASE_URL. On GitHub, .github/workflows/tests.yml runs the same suite on every push and pull request against a temporary PostgreSQL 16 service, using no secrets; there a missing TEST_DATABASE_URL makes the database tests fail instead of skip, so a green check means the whole suite ran. Output from the loader, the checks, the feature build and the tests reports counts and row positions only, so running them never displays 2025/26 outcomes.

## Re-run the source audit

    python3 scripts/audit_data.py

This uses only the standard library, verifies the saved file hashes and regenerates audit/data_audit.json and audit/coverage.md without downloading anything. `--download` fetches the pinned snapshot when starting from the script and configuration alone; it reuses verified files and is not a live-update command. A successful exit means the selected seasons pass the audit. The report keeps the problems found in excluded seasons: 2019/20 has 66 out-of-window dates, and the 2026/27 file is an incomplete, stale snapshot. docs/MILESTONE_0_REPORT.md records how the source was chosen.

## Download and load live data

Register for a free key at football-data.org and put it in .env as one line: `FOOTBALL_DATA_API_KEY=your-key`. Never commit .env.

    python -m soccer_forecasting fetch-live
    python -m soccer_forecasting load-live

fetch-live makes two requests (the free plan allows 10 a minute) and saves a snapshot of this season's matches and teams under data/live/football-data-org/<UTC time>/ with a manifest of URLs, times and hashes. Snapshots are never overwritten and stay out of Git because football-data.org's terms govern their data.

load-live loads every snapshot not yet loaded, oldest first, each in its own transaction, and keeps a copy of every downloaded file in live_download_files. New fixtures are inserted and tied to their football-data.org id; unplayed fixtures take the newest date, kickoff and status; a completed result never changes afterwards, and a different score stops the load for a person to check. Every fixture's state in every snapshot is kept in match_observations, and the result_first_seen view gives the time each result was first downloaded. Unknown teams, unknown statuses, changed files and out-of-order snapshots stop the load. Match dates are London dates of the UTC kickoff. export-live writes the stored downloads back out under data/live.

## Forecast real matches

    python -m soccer_forecasting train-deployment
    python -m soccer_forecasting predict --preview --date 2026-11-21
    python -m soccer_forecasting daily

train-deployment refits the frozen model with its frozen settings on every historical season, after the final test only, and records it in model_runs as a separate training run from the evaluated one. It also stores the model file in model_files, so a computer without the file uses the recorded one. predict --preview shows forecasts for any day without saving them. daily downloads, loads and saves forecasts for the remaining matches of today's London date; `--if-needed` makes it stop at once if today's daily run already succeeded. A saved forecast:

- is made on match day, before kickoff (the database refuses one made at or after kickoff);
- uses form from matches dated at least two days earlier, and only results downloaded before the forecast was made, through the same feature query as the historical features;
- needs a download from the last 30 hours, and no earlier fixture still missing its result;
- is never changed: running again the same day adds nothing, and a match moved to another day gets a new forecast for the new day.

Every daily or predict run is logged in job_runs, including failures. prediction_results shows each forecast next to the result and its log loss once the match is played.

## Run the dashboard locally

    python -m soccer_forecasting serve

Then open http://127.0.0.1:8000 for the dashboard or http://127.0.0.1:8000/docs for the API documentation; it only listens on your own computer.

| Address | What it returns |
| --- | --- |
| / | Dashboard: upcoming fixtures with their forecasts once published, recent forecasts next to results, performance so far, data freshness |
| /health | Database status, newest download and its age, deployment model run, last job run |
| /healthz | Always {"status": "ok"}, without touching the database: for the host's health checks |
| /forecasts?date=YYYY-MM-DD | Saved forecasts for one match day (default: today in London) |
| /forecasts/upcoming | Saved forecasts for matches not yet started |
| /fixtures/upcoming | The next fixtures, each with its forecast or null until match day |
| /matches/{id}/forecasts | Every forecast saved for one match |
| /performance | Log loss and accuracy of forecasts for matches played on their forecast day, against historical shares and one third each |

The service only reads saved forecasts and never recomputes one. Its database session is read-only, and every response carries the attribution "Football data provided by the Football-Data.org API".

## The daily job on GitHub Actions with Neon

.github/workflows/daily.yml runs on GitHub's computers against a PostgreSQL database hosted by Neon (free plan, PostgreSQL 16). GitHub starts scheduled runs late when it is busy, sometimes by hours, and sometimes skips them: in this repository's first days, runs started 2 to 8 hours late. So a run is scheduled every hour from 00:17 to 11:17 UTC (the same in London in winter, an hour later in summer), all before the usual first kickoff. The first to start does the work; with `daily --if-needed` the others stop at once. Each of those checks wakes the Neon database for a few minutes, under a tenth of the free plan's monthly compute. "Run workflow" on the Actions tab starts a run by hand; "Disable workflow" stops the schedule.

Each run starts on an empty computer, so the database keeps what used to live only on disk: the model file in model_files and every download in live_download_files.

One-time move of a local database to Neon (Docker running, with the current code):

    python -m soccer_forecasting init-db
    python -m soccer_forecasting train-deployment
    python -m soccer_forecasting load-live

In Neon, click Connect, turn Connection pooling off and copy the direct connection string. Then, with the line below pasted first and Enter pressed only after copying, copy the database and check it:

    NEON_URL="$(pbpaste | grep -o 'postgresql://[^"]*' | grep -v -- -pooler | head -n 1)"
    docker exec soccer-pg pg_dump -U soccer -d soccer --format=custom --no-owner --no-privileges > ~/soccer.dump
    docker exec -i soccer-pg pg_restore --no-owner --no-privileges --exit-on-error --single-transaction -d "$NEON_URL" < ~/soccer.dump
    DATABASE_URL="$NEON_URL" python -m soccer_forecasting check

Then give the workflow its two secrets, and forget the address:

    printf '%s' "$NEON_URL" | gh secret set DATABASE_URL
    gh secret set FOOTBALL_DATA_API_KEY
    unset NEON_URL

~/soccer.dump stays outside the repository and must never be committed: it holds football-data.org data. The logs of a public repository are public: only the two steps that need the secrets receive them, the project never prints them, and GitHub hides their values in the logs. A wrongly formatted address is refused without being shown.

- GitHub switches off schedules in a public repository after 60 days without a commit; "Enable workflow" on the Actions tab turns it back on.
- With Actions notifications on in your GitHub settings, GitHub emails you when a scheduled run fails. Each run is also recorded in job_runs.
- To read the cloud database from your computer, for example with serve, put the Neon address in .env as DATABASE_URL. Tests keep using TEST_DATABASE_URL.

## The public dashboard on Render

render.yaml describes a free Render web service in Ohio, next to the Neon database. Render installs the project and starts only the API module with uvicorn (about 60 MB of memory). Its health checks use /healthz, so they never wake the database, and deploys wait until the Tests workflow has passed. The free service sleeps after 15 minutes without visitors; the next visit takes about a minute.

The dashboard reads Neon through its own login, dashboard_reader, which may only read the seven tables and views the pages use (DASHBOARD_READS in api.py). It cannot write anything or read anything else, even if it switches its session's read-only setting off. Create it, or give it a new password, from your computer with the Neon owner's address (paste the first line, copy the address in Neon, then press Enter):

    NEON_URL="$(pbpaste | grep -o 'postgresql://[^"]*' | grep -v -- -pooler | head -n 1)"
    DATABASE_URL="$NEON_URL" python -m soccer_forecasting create-dashboard-login
    unset NEON_URL

The command creates the login with a long random password, checks that a delete is refused, and puts the login's address on the clipboard rather than the screen. In the Render Dashboard choose New > Blueprint, pick this repository, paste the clipboard into DATABASE_URL and apply, then clear the clipboard with `pbcopy < /dev/null`. After a password change, update DATABASE_URL in Render. Error pages never show database details to visitors; Render's logs keep them.

## Each summer: the new season

config/live.json names the live season (2026-27). Before the next season starts, it needs the new season and start year, and any newly promoted clubs need their football-data.org ids in config/team_aliases.csv. Until then, the daily job stops with an error rather than load another season's data.
