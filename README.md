SOCCER FORECASTING
==================

Start with docs/MILESTONE_0_REPORT.md. The full career/project context is in docs/SOCCER_PROJECT_HANDOFF.md.

This package contains a real source audit and a historical PostgreSQL database, not a trained predictor. The selected initial cohort is 2020/21–2025/26, with 2,280 Premier League matches.

**Build the historical database**

You need Python 3.10 or newer and a PostgreSQL 16 server. From the soccer-forecasting folder:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[dev]"

If you have Docker, this starts a local PostgreSQL server:

    docker run --name soccer-pg -e POSTGRES_USER=soccer -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=soccer -p 5432:5432 -d postgres:16

Then create the tables, load the six seasons, and run the data checks:

    export DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer
    python -m soccer_forecasting init-db
    python -m soccer_forecasting load-history
    python -m soccer_forecasting check

Run load-history a second time to see idempotency: it reports 0 inserted and 2,280 already present. On Windows PowerShell, set the variable with $env:DATABASE_URL="postgresql://..." instead of export.

**Run the tests**

The database tests need a separate database. Each test creates its own temporary PostgreSQL schema and drops it afterwards.

    docker exec soccer-pg createdb -U soccer soccer_test
    export TEST_DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer_test
    pytest

Output from the loader, the checks and the tests reports counts and row positions only, so running them does not display 2025/26 outcomes.

**Run the saved audit**

Use Python 3.10 or newer. The audit uses only the standard library.

Open a terminal in the extracted soccer-forecasting folder and run:

    python3 scripts/audit_data.py

This verifies the saved file hashes and regenerates audit/data_audit.json and audit/coverage.md without downloading anything.

To fetch the pinned snapshot when starting with only the script and configuration, run:

    python3 scripts/audit_data.py --download

The downloader uses the pinned commit in config/milestone0.json. It reuses verified existing files. It is not a live-update command. If a manifest already exists, all files it records must still be present and match their hashes; investigate missing or altered files rather than bypassing verification.

**Files**

| Path | Purpose |
| --- | --- |
| config/milestone0.json | Source commit, selected seasons, split, forecast policy |
| config/team_aliases.csv | Maps each provider's team name to one canonical team |
| sql/001_schema.sql | Tables, constraints and the team_matches view, with the reasoning in comments |
| sql/checks/ | Post-load checks; each query returns the rows that break a rule |
| src/soccer_forecasting/ | Parser, loader, check runner and command line |
| tests/ | Parser, constraint, loader and reconciliation tests |
| pyproject.toml | Pinned dependencies and package definition |
| scripts/audit_data.py | Reproducible downloader and structural audit |
| data/manifest.json | Source paths, URLs, hashes, and retrieval metadata |
| data/raw/openfootball/ | Original JSON snapshots and upstream license/README |
| audit/data_audit.json | Detailed findings, field presence, score formats, and errors |
| audit/coverage.md | Compact season table |
| docs/MILESTONE_0_REPORT.md | Decisions, data dictionary, limitations, next step |
| docs/SOCCER_PROJECT_HANDOFF.md | Updated context for another LLM |

A successful exit means the configured initial modeling seasons pass the audit. The report still retains problems in excluded seasons. In particular, 2019/20 has 66 out-of-window dates; 2026/27 is an incomplete, stale snapshot.

Both nested full-time scores and valid bare two-integer score arrays are recognized. Missing scores never become 0–0 draws. Dates are checked against the expected August-to-July season window for this audited league/cohort. The saved audit withholds outcome totals for the 2025/26 final test season.

The 28 names in the selected cohort are source team names. External provider IDs and aliases have not yet been reconciled. Source data contains no explicit timezone offsets or historical publication timestamps.

**Data attribution**

Historical data: OpenFootball football.json, CC0 1.0, commit e6744429ee395bc86f247348c6184bb08d4eb361.

Upstream: https://github.com/openfootball/football.json

The upstream license is preserved under data/raw/openfootball/<commit>/LICENSE.md. Other candidate providers have their own terms. No raw Football-Data.co.uk data is included in this package.

**Next**

Milestone 2: build SQL form features from the team_matches view under the forecast cutoff rule, then the class-frequency baseline. Model training comes after that.

