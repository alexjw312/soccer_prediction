SOCCER PREDICTION
=================

[![Tests](https://github.com/alexjw312/soccer_prediction/actions/workflows/tests.yml/badge.svg)](https://github.com/alexjw312/soccer_prediction/actions/workflows/tests.yml)
[![Daily forecasts](https://github.com/alexjw312/soccer_prediction/actions/workflows/daily.yml/badge.svg)](https://github.com/alexjw312/soccer_prediction/actions/workflows/daily.yml)

Home win, draw and away win probabilities for Premier League matches. Each forecast is made on match day before kickoff, saved, never changed, and scored once the result is in.

**Live dashboard: https://soccer-prediction-1tcb.onrender.com** (API documentation at /docs). It runs on free hosting, so the first visit after a quiet spell takes about a minute.

## What it does

- Every morning it downloads this season's fixtures and results from football-data.org and keeps every download.
- On match day, before kickoff, it computes each team's recent form from results that had actually been downloaded by then, and saves a forecast that is never changed afterwards.
- A public dashboard has four tabs (fixtures, table, performance, how it works), a page per match with its forecast, each team's last five and the last five meetings, and a page per club with its fixtures, place in the table, manager and squad. A JSON API serves the forecasts.

## How it works

```mermaid
flowchart LR
    OF["OpenFootball<br/>6 seasons, pinned by hash"] -->|loaded once| DB
    FD["football-data.org API<br/>fixtures and results"] -->|downloaded daily| GH
    GH["GitHub Actions<br/>daily job, hourly each morning"] -->|load, compute form, save forecasts| DB[("Neon PostgreSQL<br/>matches, features, forecasts,<br/>downloads, model file")]
    DB -->|read-only login| RD["Render<br/>FastAPI dashboard and API"]
    RD --> V(["Visitors"])
    CI["GitHub Actions<br/>tests on every push"] -.->|deploys only after the tests pass| RD
```

- **Data:** 2,280 matches from six seasons (2020/21 to 2025/26) of OpenFootball history, pinned to one commit and checked by hash, plus the current season from football-data.org.
- **Features:** each side's games played, points per game, goals for and goals against over its last 5 and last 10 matches: 16 numbers per match. They are computed in SQL with window functions and an as-of join, by one query that serves both training and live forecasts.
- **Model:** multinomial logistic regression (C = 0.03) with mean imputation and scaling, refitted on all six seasons for live use.
- **Running it:** a GitHub Actions job runs the daily pipeline against a Neon PostgreSQL database. A FastAPI app on Render serves the dashboard and API. All three run on free plans.

More: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (components, data model, timing rules, security) and [docs/OPERATIONS.md](docs/OPERATIONS.md) (every command).

## Results

Multiclass log loss on whole seasons (lower is better; 95% bootstrap intervals in brackets). The model was chosen on 2024/25 by a rule written before any model was scored, then tested once on 2025/26, which no step had used before.

| Forecast | Validation 2024/25 (380 matches) | Final test 2025/26 (380 matches) |
| --- | --- | --- |
| Logistic regression (selected) | 1.0294 (0.9848–1.0709), accuracy 50.8% | 1.0541 (1.0163–1.0918), accuracy 46.3% |
| XGBoost | 1.0392 (0.9971–1.0782), accuracy 49.5% | not tested: not selected |
| Class-frequency baseline | 1.0797 (1.0529–1.1056), accuracy 40.8% | 1.0861 (1.0594–1.1112), accuracy 42.6% |
| One third each | 1.0986 | 1.0986 |

- **The improvement held on the unseen season.** Paired per match, the selected model beat the baseline by 0.0320 (95% interval 0.0046 to 0.0594). That is smaller than on validation (0.0503), as expected once the season used for choosing is no longer the one being scored.
- **XGBoost was not better than logistic regression.** Its difference on validation was +0.0098 (−0.0047 to +0.0243), so under the declared rule the simpler model was kept.
- **Draws are under-forecast.** They were 27.4% of test results, while 336 of the 380 forecasts gave them 20–30% (mean 23.9%). On validation, no model ever made a draw the single most likely result.

The full analysis, with calibration tables, is in [docs/EVALUATION.md](docs/EVALUATION.md); the original reports are in [reports/](reports/). Live forecasts start on 21 November 2026, the first match day after deployment, and the dashboard scores them as results arrive.

## Design decisions

- **No information from the future.** Features count only matches dated at least two days before match day. Live, a result counts only if it had been downloaded before the forecast was made (`result_first_seen`). The same SQL ([sql/features/form_v1.sql](sql/features/form_v1.sql)) builds the training features and the live ones, and a test checks they agree.
- **An honest evaluation.** The split is chronological. Settings were tuned on expanding season folds inside the training seasons. The selection rule ([config/milestone3.json](config/milestone3.json)) was fixed before scoring. The final test ran once, and the code refuses a second run.
- **Forecasts are records.** The database refuses a forecast made at or after kickoff, and forecasts are never updated: a moved match gets a new forecast. Stale data or a missing earlier result blocks saving instead of producing a quietly wrong forecast.
- **Raw data is kept.** Every download is stored with its hash. A completed result can never silently change; a different score stops the load for a person to check.
- **Least privilege.** The daily job's secrets reach only the two steps that need them. The public dashboard uses a login that can read 7 tables and nothing else. Error pages show no database details. GitHub Actions are pinned to commit hashes.
- **Runs for $0.** It uses GitHub Actions, Neon's free plan and Render's free plan. The health check never touches the database, so it doesn't use up the database's free compute.

## Run it yourself

Requirements: Python 3.12 or newer, and Docker for a local PostgreSQL 16.

    git clone https://github.com/alexjw312/soccer_prediction && cd soccer_prediction
    python3 -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"            # on macOS, XGBoost also needs: brew install libomp
    docker run --name soccer-pg -e POSTGRES_USER=soccer -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=soccer -p 5432:5432 -d postgres:16
    export DATABASE_URL=postgresql://soccer:dev@localhost:5432/soccer
    python -m soccer_forecasting init-db
    python -m soccer_forecasting load-history
    python -m soccer_forecasting build-features
    python -m soccer_forecasting select-model

Live forecasts need a free football-data.org key. [docs/OPERATIONS.md](docs/OPERATIONS.md) covers the tests, live data, forecasts, the dashboard and the cloud setup.

## Repository layout

| Path | Contents |
| --- | --- |
| src/soccer_forecasting/ | Loading, checks, features, models, evaluation, live data, forecasting, API and dashboard, command line |
| sql/ | Schema migrations (001–006), the feature query, and post-load data checks |
| config/ | Data source and split, the model-selection protocol, live-data settings, team name mapping, stadiums, club colours |
| reports/ | Baseline, model selection and final test reports, and the frozen model choice |
| tests/ | Automated tests: parsing, loading, features, models, evaluation rules, live data, forecasting, API, cloud setup |
| .github/workflows/ | The daily job and the test run |
| render.yaml | The public dashboard on Render |
| data/, audit/, scripts/ | The pinned OpenFootball snapshot and its audit |
| docs/ | Architecture, evaluation, operations, and the data-source report ([docs/MILESTONE_0_REPORT.md](docs/MILESTONE_0_REPORT.md)) |

## Limitations

- **Results-only features.** There are no line-ups, injuries, odds or expected goals. Clubs returning after promotion carry their old form, and teams with short histories get average values.
- **Draws are under-forecast,** and calibration is uncertain wherever a probability range has few matches.
- **One test season.** Small differences between models are within the noise of 380 matches.
- **No publication times in the historical data.** The two-day buffer is an assumption, not proof of what was known before each match.
- **The live record starts in November 2026,** so there is no settled live performance yet.
- **Free hosting.** The dashboard sleeps after 15 quiet minutes, GitHub can start scheduled runs hours late (hence the hourly morning runs), and the season setting needs updating each summer.

## Data and attribution

Historical data: OpenFootball football.json, CC0 1.0, commit e6744429ee395bc86f247348c6184bb08d4eb361 (https://github.com/openfootball/football.json); the license is kept under data/raw/openfootball/. Live data: Football data provided by the Football-Data.org API. Its downloads are kept privately in the database and never committed, under football-data.org's terms.
