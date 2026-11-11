MILESTONE 0 — SCOPE AND DATA AUDIT
=================================

Soccer Match Forecasting Service • November 9, 2026

**Outcome**

The historical-data feasibility audit is complete. The project can proceed to Milestone 1 using six Premier League seasons, 2020/21–2025/26, containing 2,280 matches and 28 distinct source team names. No model has been trained and no database or deployed service exists yet.

The live-fixture integration remains a later gate. Its documented fields and coverage have been reviewed, but an authenticated football-data.org response and the cross-provider identity mapping have not been tested.

This report records practical defaults selected during the audit. Alex can revise them without changing the project's career objective.

**1. What we are building**

Predict three probabilities for an upcoming Premier League match: home win, draw, and away win. The target is the final league-match result, including stoppage time, from the home team's perspective.

The project will demonstrate SQL feature engineering, reproducible data pipelines, supervised ML, chronological evaluation, and deployment. First models remain a historical class-frequency baseline, multinomial logistic regression, and an XGBoost classifier. A/B testing, xG, and player valuation remain optional extensions.

Milestone 0 answers four questions before training starts: What is one row? What are we predicting? What information is available when we predict? Can the source support those decisions?

**2. Decisions after inspecting the data**

| Decision | Initial implementation |
| --- | --- |
| Competition | English Premier League |
| Historical source | OpenFootball football.json, pinned to a specific repository commit |
| Modeling period | 2020/21–2025/26 |
| Training | 2020/21–2023/24 — 1,520 matches before any feature-history handling |
| Validation | 2024/25 — 380 matches |
| Final test | 2025/26 — 380 matches |
| Target | H / D / A, derived from full-time home and away goals |
| First feature families | Earlier results, goals scored/conceded, rolling form, and history counts |
| Forecast policy | Daily at 06:00 Europe/London on the fixture's local match date |
| Historical feature cutoff | Use matches dated at least two calendar days before the target match date |
| Live feed candidate | football-data.org v4, subject to authenticated field and identity verification |
| Next implementation | Relational schema and historical ingestion in PostgreSQL |

These are match counts, not model sample counts or performance results. A feature pipeline may identify rows needing a short-history policy.

The final test season is reserved for final model evaluation. We inspected its structure and score encoding for data quality; we have not used it to select models or features based on predictive performance.

**3. Why the historical source changed**

The original candidate, Football-Data.co.uk, was accessible. Its current data page states that use is intended for private individuals and includes restrictions around commercial or data-training products using automated bots, scrapers, or AI. We therefore did not select it as the project's reusable training source.

OpenFootball provides its data under CC0 and supplies the results needed for the first feature set. This choice also avoids requiring an API key for the historical learning phase. Preserve attribution and the upstream license in the project.

This is a source-selection decision based on the published statements, not a legal opinion about every possible use of the original site.

Sources: [Football-Data.co.uk data page](https://www.football-data.co.uk/data.php), [OpenFootball license](https://github.com/openfootball/football.json/blob/master/LICENSE.md).

**4. What was actually downloaded and checked**

The audit fetched 12 Premier League season files, 2015/16–2026/27, plus the upstream README and license. All JSON files were fetched from commit e6744429ee395bc86f247348c6184bb08d4eb361, whose reported commit timestamp is September 22, 2026.

Each download has a URL, retrieval timestamp, byte count, and SHA-256 hash in data/manifest.json. The audit verifies those hashes before reading the saved snapshot. It can run offline.

| Season | Fixture rows | Recognized full-time results | Initial use |
| --- | ---: | ---: | --- |
| 2015/16–2018/19 | 1,520 total | 1,520 | Older reserve data; outside the first cohort |
| 2019/20 | 380 | 380 | Quarantined because of date errors |
| 2020/21 | 380 | 380 | Training |
| 2021/22 | 380 | 380 | Training |
| 2022/23 | 380 | 380 | Training |
| 2023/24 | 380 | 380 | Training |
| 2024/25 | 380 | 380 | Validation |
| 2025/26 | 380 | 380 | Final test |
| 2026/27 | 380 | 50 | Freshness audit only; not the live feed |

For each selected season, the audit found 20 teams, all 380 ordered home/away team pairings, no duplicate pairings, and no missing match dates, team names, time strings, or full-time results. Scores are nonnegative integer pairs. Supplied half-time scores do not exceed full-time scores.

These checks establish structural consistency. They do not independently verify every result against an official record.

**5. Problems the audit caught**

| Finding | Evidence | Decision |
| --- | --- | --- |
| Score encoding varies | In the selected cohort, 2,253 records use a score object and 27 use a bare full-time pair | Recognize both formats explicitly; retain the original JSON |
| A historical season has incorrect date years | 66 rows in 2019/20 fall before the expected season window | Quarantine the whole season; do not silently change years |
| Current snapshot is stale | Latest recognized 2026/27 result is September 20, 2026; 50 dated fixtures on or before November 9 lack results | Do not use this snapshot as a current-results service |
| Timezone metadata is absent | All 2,280 selected time values are clock strings without an offset | Use a documented calendar-date backtest policy |
| Half-time scores are incomplete | 129 selected records lack a half-time score | Exclude half-time features from version 1 |
| Team names can change between seasons | Older records use names such as Manchester United; later records use Manchester United FC | Use explicit aliases and stable internal team IDs when extending coverage |
| Match IDs and status fields are absent | The sampled JSON contains names, dates, rounds, and scores | Create internal fixture identities; do not infer operational status from an empty score alone |

A concrete date-error check found Newcastle United–Liverpool recorded as July 26, 2019 in the 2019/20 JSON. The Premier League's official report dates the 1–3 match to July 26, 2020. A wrong year would let later results enter earlier form calculations, so this is relevant to model validity.

A bare-pair example is Aston Villa–Newcastle on August 16, 2025, recorded as score [0, 0]. The official report confirms the goalless result. The adapter treats a valid two-integer score array as a full-time result; unsupported shapes remain errors. Missing scores remain unknown and are never converted into draws.

Verification sources: [2019/20 match report](https://www.premierleague.com/en/news/1733122/match-summary-newcastle-united-v-liverpool), [2025/26 match report](https://www.premierleague.com/en/news/4374217/aston-villa-v-newcastle-report).

**6. Data dictionary for the first implementation**

| Raw field or metadata | Meaning | Curated representation | Use |
| --- | --- | --- | --- |
| Season directory | Competition season | season, text | Partitioning and split assignment |
| name | Dataset/competition description | source_competition_name, text | Audit metadata |
| matches[] | One fixture per object | One matches row | Main grain |
| round | Source matchday label | source_round, text | Retain for reference; not chronological ordering |
| date | Match calendar date | match_date, date | Historical ordering and cutoff |
| time | Clock time without timezone offset | source_time, nullable text | Preserve; do not silently label UTC |
| team1 | Home team name | home_team_id via alias mapping | Fixture identity and feature joins |
| team2 | Away team name | away_team_id via alias mapping | Fixture identity and feature joins |
| score.ft[0] or score[0] | Full-time home goals | home_goals, integer | Label; features for later matches only |
| score.ft[1] or score[1] | Full-time away goals | away_goals, integer | Label; features for later matches only |
| score.ht | Optional half-time goals | Retained in raw data | Excluded from the first feature set |
| Derived H/D/A | Compare full-time goals | result, constrained text | Prediction target |
| Source commit and file hash | Exact input snapshot | Source-ingestion metadata | Reproducibility |
| Retrieval timestamp | When this project fetched the file | retrieved_at_utc | Provenance, not original publication time |

A home win means home_goals > away_goals. A draw means equality. An away win means home_goals < away_goals.

For this league, a candidate natural fixture key is competition + season + canonical home team + canonical away team. Each ordered pairing occurs once in a season. A kickoff change should not create a different logical fixture. Use a surrogate internal ID and keep provider-specific IDs separately.

The live API's documented id, utcDate, status, homeTeam.id, awayTeam.id, and full-time score fields map to those same concepts. That is a documentation-based field mapping, not a verified authenticated integration. Exact API team IDs must be obtained from a real response, not guessed.

**7. Forecast timing and availability assumptions**

The earlier 24-hours-before-kickoff suggestion is replaced for the initial version.

For a match dated D in England, the intended forecast run is 06:00 Europe/London on D. Historical form uses eligible matches dated D minus two days or earlier. For example, a Saturday forecast can use Thursday and earlier results. Friday results are excluded.

This conservative buffer makes historical ordering independent of uncertain kickoff-time offsets and excludes same-day results. Treat source match dates as the competition's local dates for this backtest; the dataset has no explicit timezone metadata. Verify that convention against additional official records before widening timing-sensitive features.

The buffer does not prove when an old record was originally published. The snapshot contains final historical results and corrected data as currently stored, without a full publication history. Describe the evaluation as a historical backtest under a stated availability assumption.

For future operation, a result must also have actually been observed before the forecast cutoff. Store retrieved_at, feature_cutoff, generated_at, and model_version separately. If the source is stale, fail the freshness check or label the forecast accordingly. Do not backdate a late run.

This is a match-day forecast policy, not a fixed 24-hour prediction horizon. A later version can adopt precise UTC horizons after the source and timestamp requirements are verified.

**8. Initial features and deliberate exclusions**

From eligible earlier matches, calculate each team's points per game, goals scored per game, goals conceded per game, and match count over the previous five and ten games. Build separate home-team and away-team feature snapshots, then join them to the fixture.

For example, earlier points [3, 0, 1, 3, 1] give 8/5 = 1.6 points per game. That is an illustrative calculation, not a feature already produced by this package.

Later in the same modeling milestone, consider venue-specific history and the difference between the teams' form. Elo can be an additional comparison once the simple baseline works.

Preserve short-history counts for promoted teams and early-season games. Keep unavailable values explicitly missing; choose and fit any imputation inside the chronological training procedure.

Do not use the target fixture's goals, later standings, same-match statistics, or season averages calculated using future games. Order by eligible played dates rather than round number.

Shots, possession, xG, injuries, ratings, and lineups are not in this selected historical source and are outside the first feature set. League-only history also cannot establish complete rest time across cup and international fixtures.

**9. Live-data gate**

football-data.org documents Premier League coverage on its free tier, fixtures, delayed results/schedules, and a rate limit of 10 calls per minute. Its policies limit unauthenticated access to area and competition lists. No API key was supplied or account created during this milestone.

Before Milestone 4 can be called operational, verify an authenticated sample, normalize UTC kickoff times, reconcile team and fixture identities, measure update freshness, and implement rescheduling/status handling. The historical database can be built before this work.

The API requires visible attribution. Keep API responses and assets governed by the provider's own terms; do not imply that the OpenFootball CC0 license covers another provider.

Sources: [Coverage](https://www.football-data.org/coverage), [Pricing](https://www.football-data.org/pricing), [Policies](https://docs.football-data.org/general/v4/policies.html), [Match fields](https://docs.football-data.org/general/v4/match.html), [Attribution](https://www.football-data.org/documentation/faq).

**10. Delivered work and verification**

The starter package contains:

- The pinned raw JSON files, upstream README, and license.
- A download manifest with hashes and retrieval metadata.
- A Python audit script requiring only the standard library.
- Machine-readable audit results and a season coverage table.
- Configuration for the cohort, chronological split, and forecast policy.
- This report and the updated project handoff.

The offline audit was run successfully with Python 3.12.14. A second offline run produced an identical audit file. The selected six seasons passed the configured checks, and the 66 out-of-window 2019/20 dates remain recorded as excluded-source findings.

No ML metrics, trained models, database tables, feature matrices, live predictions, or deployments have been created.

**11. What Alex should understand before Milestone 1**

One raw row describes a match and, for completed games, its result. A modeling row will additionally contain information from earlier eligible matches. That transformation is where SQL becomes useful.

The label is H/D/A. The inputs are historical form and goals available under the cutoff rule. The raw score for the target match supplies its label, so it cannot also be an input to its own prediction.

Inspect one raw JSON match and locate its date, home team, away team, and score. Then read the season coverage table and identify why a file with 380 rows can still be unsuitable for modeling. The 2019/20 example is the key case.

**12. Next milestone**

Milestone 1 should define PostgreSQL tables for teams, source aliases, matches, source-ingestion metadata, and a team-match view or table. Ingest the selected six seasons while preserving raw provenance and checking uniqueness and reconciliation counts.

Do not load quarantined seasons into the modeling cohort. Do not train a model until the historical database and cutoff rules are implemented and verified.

