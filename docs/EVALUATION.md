Evaluation
==========

How the model was chosen and tested, and what the results do and do not show. Every number here comes from the reports in [reports/](../reports/), produced by the commands in [docs/OPERATIONS.md](OPERATIONS.md).

## The question and the score

Each forecast gives three probabilities (home win, draw, away win) for a Premier League match, from the home team's point of view, for the result after normal time.

The main score is **multiclass log loss**: the average of −log(probability given to what actually happened). It rewards honest probabilities and punishes confident mistakes; lower is better. Giving one third to each outcome scores ln 3 = 1.0986, which is the bar any forecast has to clear. Accuracy (how often the most likely outcome happened) is reported too, but it says little about draws, which are rarely the most likely outcome even though about a quarter of matches end level.

Uncertainty is shown as 95% bootstrap intervals: 2,000 resamples of the season's matches, with the same resampled matches used for every model, so that differences between models are measured match by match ("paired").

## Data and split

2,280 Premier League matches from OpenFootball, six seasons, split by time so that every evaluation season comes after the seasons used to fit it:

| Role | Seasons | Matches |
| --- | --- | ---: |
| Training | 2020/21 – 2023/24 | 1,520 |
| Validation (choosing the model) | 2024/25 | 380 |
| Final test (used once) | 2025/26 | 380 |

The 2019/20 season was left out because its dates have 66 out-of-window errors (see [MILESTONE_0_REPORT.md](MILESTONE_0_REPORT.md)).

## Features

For each side of a fixture: games played, points per game, goals for and goals against over its last 5 and last 10 matches, 16 numbers in all (feature version form_v1, [sql/features/form_v1.sql](../sql/features/form_v1.sql)). Only matches dated at least two days before match day count, a buffer chosen because the historical data has no record of when each result was published. History carries across seasons; a team with no earlier match gets missing values, which logistic regression replaces with training averages and XGBoost handles directly.

## How the model was chosen

The protocol ([config/milestone3.json](../config/milestone3.json)) was written on 10 November 2026, before any model was scored.

1. **Tuning, inside the training seasons only.** Each candidate's settings were chosen on three expanding folds: train on 2020/21 and score 2021/22; train on 2020/21–2021/22 and score 2022/23; train on 2020/21–2022/23 and score 2023/24. Logistic regression tried C from 0.001 to 3; XGBoost tried depths 1–3 with 100, 300 or 600 trees at a learning rate of 0.03.
2. **Validation.** Each tuned candidate was refitted on all four training seasons and scored on 2024/25.
3. **The selection rule.** Start with the class-frequency baseline. Take each more complex candidate in turn; it replaces the current choice only if the 95% paired interval for its log-loss difference lies entirely below zero. Otherwise the simpler model stays.

| Model | Tuned settings | Tuning folds | Validation log loss | Versus baseline (paired) | Accuracy |
| --- | --- | ---: | ---: | ---: | ---: |
| Class-frequency baseline | none | 1.0657 | 1.0797 (1.0529–1.1056) | — | 40.8% |
| Logistic regression | C = 0.03 | 1.0020 | 1.0294 (0.9848–1.0709) | −0.0503 (−0.0815 to −0.0195) | 50.8% |
| XGBoost | depth 2, 100 trees | 1.0173 | 1.0392 (0.9971–1.0782) | −0.0405 (−0.0674 to −0.0143) | 49.5% |

- Logistic regression versus the baseline: −0.0503 (−0.0815 to −0.0195), entirely below zero, so it replaced the baseline.
- XGBoost versus logistic regression: +0.0098 (−0.0047 to +0.0243), not below zero, so logistic regression stayed.

**Selected: logistic regression**, frozen in [reports/frozen_selection.json](../reports/frozen_selection.json) with hashes of the protocol and training data, which the final test checks before running.

Two checks that were reported but not used for choosing:

- **Short history.** Only 5 validation matches had a side with fewer than 5 earlier games, too few to conclude anything (logistic regression 1.0509 on those, 1.0291 on the other 375).
- **Without 2020/21** (a season played largely without crowds): logistic regression scored 1.0237 and XGBoost 1.0432 on validation. Dropping it would not have changed the choice.

## The final test

Run once, on 11 November 2026, with the model refitted on 2020/21–2024/25 (1,900 matches) using the frozen settings. 2025/26: 380 matches, 162 home wins, 104 draws, 114 away wins.

| Forecast | Test log loss | Accuracy |
| --- | ---: | ---: |
| Logistic regression | 1.0541 (1.0163–1.0918) | 46.3% |
| Class-frequency baseline | 1.0861 (1.0594–1.1112) | 42.6% |
| One third each | 1.0986 | — |

Logistic regression minus baseline, paired per match: **−0.0320 (−0.0594 to −0.0046)**. The improvement held on a season no decision had seen, at about two thirds of its validation size (−0.0503). Some shrinkage is expected: the validation season helped choose the model, so its score flatters it slightly.

### Calibration on the test season

Matches grouped by the probability the model gave an outcome, against how often that outcome happened (bins with at least 20 matches; the full tables are in [reports/final_test.md](../reports/final_test.md)):

| Outcome | Forecast bin | Matches | Mean forecast | Observed (95% interval) |
| --- | --- | ---: | ---: | --- |
| Home win | 20–30% | 63 | 26.6% | 31.7% (22–44%) |
| Home win | 30–40% | 86 | 35.2% | 37.2% (28–48%) |
| Home win | 40–50% | 103 | 45.1% | 41.7% (33–51%) |
| Home win | 50–60% | 78 | 54.1% | 55.1% (44–66%) |
| Home win | 60–70% | 31 | 64.0% | 58.1% (41–74%) |
| Draw | 10–20% | 43 | 18.1% | 27.9% (17–43%) |
| Draw | 20–30% | 336 | 23.9% | 27.4% (23–32%) |
| Away win | 10–20% | 39 | 16.3% | 7.7% (3–20%) |
| Away win | 20–30% | 114 | 25.6% | 21.1% (15–29%) |
| Away win | 30–40% | 109 | 34.8% | 33.0% (25–42%) |
| Away win | 40–50% | 86 | 44.5% | 43.0% (33–54%) |
| Away win | 50–60% | 26 | 53.7% | 42.3% (26–61%) |

- **Home and away wins** are well calibrated where there are enough matches: every mean forecast lies inside the observed interval.
- **Draws are under-forecast.** Draws were 27.4% of the test season against about 22% in training, and the model's draw probabilities barely move (336 of 380 between 20% and 30%). It is the clearest weakness. Recalibrating the probabilities on the validation season is the natural next step; it was not done, because its effect could only be judged honestly on a season that has not been used yet, such as the live 2026/27 record.

## What the model learned

From [reports/model_validation.md](../reports/model_validation.md): the largest coefficients (home win minus away win, on scaled features) are the home side's goals scored over its last 10 matches (+0.39) and the away side's (−0.33), then the home side's games available (+0.25). The features overlap heavily, since the last 5 sit inside the last 10, so the signs and rough sizes mean more than individual values.

## The live record

For live forecasts the frozen model is refitted on all six seasons (2,280 matches) and recorded as a separate run from the one tested ([OPERATIONS.md](OPERATIONS.md)). From 21 November 2026, each saved forecast is scored when its match is played: the dashboard's performance section and /performance compare its log loss and accuracy with the historical shares and with one third each, counting only matches played on the day they were forecast for. That record is genuinely prospective; the numbers above are a careful backtest.

## What these results do not show

- **One test season.** 380 matches leave wide intervals, and the model's edge over the baseline is modest; differences between the two models were within noise.
- **Results-only information.** Line-ups, injuries, betting odds and expected goals are not used, so this is a baseline for richer features rather than a strong forecaster. A richer source would have to be evaluated the same way, without touching 2025/26 again.
- **Availability is assumed for history.** The two-day buffer stands in for publication times that the historical data does not have. Live forecasts use actual download times.
