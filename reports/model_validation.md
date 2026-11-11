Model selection — validation report
===================================

Generated 2026-11-10T19:30:48+00:00 from OpenFootball commit e6744429ee395bc86f247348c6184bb08d4eb361.
Protocol: config/milestone3.json (sha256 80e861f5f16c…), written before any model was scored.
The final test season (2025-26) was not used.

Training: 2020-21, 2021-22, 2022-23, 2023-24 (1520 matches). Validation: 2024-25 (380 matches: 155 home wins, 93 draws, 132 away wins).

Log loss: lower is better; one third each scores 1.0986. Ranges are 95% bootstrap intervals. "Versus baseline" is the paired difference per match; negative means better than the baseline.

| Model | Settings (tuned on training folds) | Tuning folds log loss | Validation log loss | Versus baseline | Accuracy |
| --- | --- | ---: | ---: | ---: | ---: |
| Class-frequency baseline | none | 1.0657 | 1.0797 (1.0529 to 1.1056) | — | 40.8% |
| Logistic regression | C=0.03 | 1.0020 | 1.0294 (0.9848 to 1.0709) | -0.0503 (-0.0815 to -0.0195) | 50.8% |
| XGBoost | max_depth=2, n_estimators=100 | 1.0178 | 1.0398 (0.9981 to 1.0778) | -0.0399 (-0.0671 to -0.0142) | 49.5% |

**Selection, applying the declared rule**

Rule: Start with the baseline as the choice. Take each more complex candidate in order. It replaces the current choice only if the 95% paired bootstrap interval for (candidate minus current choice) validation log loss lies entirely below zero. Otherwise the simpler model stays.

- Logistic regression versus Class-frequency baseline: -0.0503 (-0.0815 to -0.0195), so it replaces it.
- XGBoost versus Logistic regression: +0.0104 (-0.0049 to +0.0247), so it does not replace it.

**Selected: Logistic regression**, frozen in reports/frozen_selection.json.

**Draws**

| Model | Mean draw forecast | Actual draw share | Times a draw was most likely | Log loss on draws | Log loss on other results |
| --- | ---: | ---: | ---: | ---: | ---: |
| Class-frequency baseline | 22.4% | 24.5% | 0 | 1.4975 | 0.9443 |
| Logistic regression | 22.9% | 24.5% | 0 | 1.4906 | 0.8800 |
| XGBoost | 22.5% | 24.5% | 0 | 1.5041 | 0.8894 |

Draws are rarely the single most likely outcome, so accuracy hides how a model handles them;
log loss still rewards giving draws a sensible probability.

**Short history**

| Model | Either side under 5 earlier games | Both sides 5 or more |
| --- | ---: | ---: |
| Class-frequency baseline | 1.2804 (5 matches) | 1.0770 (375 matches) |
| Logistic regression | 1.0509 (5 matches) | 1.0291 (375 matches) |
| XGBoost | 1.0615 (5 matches) | 1.0395 (375 matches) |

The short-history group is small, so differences there are weak evidence.

**Calibration of the selected model (Logistic regression)**

Matches grouped by the probability given to each outcome. Observed share comes with a 95% interval;
bins with fewer than 20 matches are marked sparse.

Home win:

| Forecast bin | Matches | Mean forecast | Observed | 95% interval |
| --- | ---: | ---: | ---: | --- |
| 10%–20% | 19 (sparse) | 15.6% | 10.5% | 3%–31% |
| 20%–30% | 47 | 25.7% | 23.4% | 14%–37% |
| 30%–40% | 90 | 35.4% | 27.8% | 20%–38% |
| 40%–50% | 78 | 44.8% | 47.4% | 37%–58% |
| 50%–60% | 79 | 54.6% | 54.4% | 43%–65% |
| 60%–70% | 43 | 64.4% | 46.5% | 33%–61% |
| 70%–80% | 19 (sparse) | 75.4% | 63.2% | 41%–81% |
| 80%–90% | 4 (sparse) | 82.1% | 100.0% | 51%–100% |
| 90%–100% | 1 (sparse) | 92.8% | 100.0% | 21%–100% |

Draw:

| Forecast bin | Matches | Mean forecast | Observed | 95% interval |
| --- | ---: | ---: | ---: | --- |
| 0%–10% | 2 (sparse) | 5.9% | 0.0% | 0%–66% |
| 10%–20% | 74 | 17.0% | 27.0% | 18%–38% |
| 20%–30% | 296 | 24.2% | 24.7% | 20%–30% |
| 30%–40% | 8 (sparse) | 30.4% | 0.0% | 0%–32% |

Away win:

| Forecast bin | Matches | Mean forecast | Observed | 95% interval |
| --- | ---: | ---: | ---: | --- |
| 0%–10% | 14 (sparse) | 7.7% | 7.1% | 1%–31% |
| 10%–20% | 58 | 16.1% | 20.7% | 12%–33% |
| 20%–30% | 101 | 25.3% | 24.8% | 17%–34% |
| 30%–40% | 109 | 35.3% | 40.4% | 32%–50% |
| 40%–50% | 63 | 44.2% | 47.6% | 36%–60% |
| 50%–60% | 30 | 54.3% | 53.3% | 36%–70% |
| 60%–70% | 3 (sparse) | 62.2% | 100.0% | 44%–100% |
| 70%–80% | 2 (sparse) | 71.6% | 50.0% | 9%–91% |

Most likely outcome versus what happened (Logistic regression):

| Happened \ Most likely | H | D | A |
| --- | ---: | ---: | ---: |
| H | 124 | 0 | 31 |
| D | 63 | 0 | 30 |
| A | 63 | 0 | 69 |

**What logistic regression learned**

Coefficient for home win minus coefficient for away win, on standardized features.
Positive pushes towards a home win. Features overlap heavily (last 5 sits inside last 10),
so read the signs and rough sizes, not individual values.

| Feature | Home win minus away win |
| --- | ---: |
| home_goals_for_last10 | +0.391 |
| away_goals_for_last10 | -0.327 |
| home_games_last10 | +0.245 |
| away_goals_against_last5 | +0.231 |
| away_ppg_last5 | +0.230 |
| away_ppg_last10 | -0.225 |
| home_ppg_last5 | +0.188 |
| home_goals_against_last5 | -0.161 |

**Sensitivity: training without 2020-21 (not used for selection)**

| Model | Validation log loss, all four seasons | Without 2020-21 |
| --- | ---: | ---: |
| Logistic regression | 1.0294 | 1.0237 |
| XGBoost | 1.0398 | 1.0438 |

**Tuning detail**

Logistic regression: mean log loss over the three training folds for each setting.

| Settings | Mean fold log loss |
| --- | ---: |
| C=0.001 | 1.0223 |
| C=0.003 | 1.0089 |
| C=0.01 | 1.0027 |
| C=0.03 | 1.0020 ← chosen |
| C=0.1 | 1.0041 |
| C=0.3 | 1.0074 |
| C=1.0 | 1.0122 |
| C=3.0 | 1.0165 |

XGBoost: mean log loss over the three training folds for each setting.

| Settings | Mean fold log loss |
| --- | ---: |
| max_depth=1, n_estimators=100 | 1.0248 |
| max_depth=1, n_estimators=300 | 1.0181 |
| max_depth=1, n_estimators=600 | 1.0245 |
| max_depth=2, n_estimators=100 | 1.0178 ← chosen |
| max_depth=2, n_estimators=300 | 1.0335 |
| max_depth=2, n_estimators=600 | 1.0644 |
| max_depth=3, n_estimators=100 | 1.0205 |
| max_depth=3, n_estimators=300 | 1.0529 |
| max_depth=3, n_estimators=600 | 1.1048 |
