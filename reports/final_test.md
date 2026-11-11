Final test — one-time evaluation
================================

Run 2026-11-11T03:54:23+00:00. This result is reported as it came out and was not used to change anything.

Selected model, frozen before this run: Logistic regression. Refitted on 2020-21, 2021-22, 2022-23, 2023-24, 2024-25 (1900 matches) with the frozen settings.
Test: 2025-26, 380 matches (162 home wins, 104 draws, 114 away wins).

| Forecast | Test log loss (95% interval) | Accuracy |
| --- | ---: | ---: |
| Logistic regression | 1.0541 (1.0163 to 1.0918) | 46.3% |
| Class-frequency baseline | 1.0861 (1.0594 to 1.1112) | 42.6% |
| One third each | 1.0986 | — |

Selected minus baseline, paired per match: -0.0320 (-0.0594 to -0.0046). Negative means the selected model was better.

Calibration of Logistic regression (bins under 20 matches are sparse):

Home win:

| Forecast bin | Matches | Mean forecast | Observed | 95% interval |
| --- | ---: | ---: | ---: | --- |
| 0%–10% | 1 (sparse) | 8.2% | 100.0% | 21%–100% |
| 10%–20% | 11 (sparse) | 17.7% | 0.0% | 0%–26% |
| 20%–30% | 63 | 26.6% | 31.7% | 22%–44% |
| 30%–40% | 86 | 35.2% | 37.2% | 28%–48% |
| 40%–50% | 103 | 45.1% | 41.7% | 33%–51% |
| 50%–60% | 78 | 54.1% | 55.1% | 44%–66% |
| 60%–70% | 31 | 64.0% | 58.1% | 41%–74% |
| 70%–80% | 7 (sparse) | 73.2% | 71.4% | 36%–92% |

Draw:

| Forecast bin | Matches | Mean forecast | Observed | 95% interval |
| --- | ---: | ---: | ---: | --- |
| 0%–10% | 1 (sparse) | 6.8% | 0.0% | 0%–79% |
| 10%–20% | 43 | 18.1% | 27.9% | 17%–43% |
| 20%–30% | 336 | 23.9% | 27.4% | 23%–32% |

Away win:

| Forecast bin | Matches | Mean forecast | Observed | 95% interval |
| --- | ---: | ---: | ---: | --- |
| 0%–10% | 1 (sparse) | 9.6% | 100.0% | 21%–100% |
| 10%–20% | 39 | 16.3% | 7.7% | 3%–20% |
| 20%–30% | 114 | 25.6% | 21.1% | 15%–29% |
| 30%–40% | 109 | 34.8% | 33.0% | 25%–42% |
| 40%–50% | 86 | 44.5% | 43.0% | 33%–54% |
| 50%–60% | 26 | 53.7% | 42.3% | 26%–61% |
| 60%–70% | 4 (sparse) | 62.5% | 50.0% | 15%–85% |
| 70%–80% | 1 (sparse) | 73.5% | 0.0% | 0%–79% |
