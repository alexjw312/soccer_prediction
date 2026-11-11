Class-frequency baseline — validation report
============================================

Generated 2026-11-10T07:50:32+00:00 from OpenFootball commit e6744429ee395bc86f247348c6184bb08d4eb361.
The final test season (2025-26) was not used.

Probabilities are listed as home win / draw / away win. Log loss: lower is better;
the range is a 95% bootstrap interval (2000 resamples of matches).

| Forecast | Fitted on | Probabilities | Validation log loss |
| --- | --- | --- | ---: |
| Class-frequency baseline | 2020-21, 2021-22, 2022-23, 2023-24 (1520 matches) | 43.8% / 22.4% / 33.8% | 1.0797 (1.0539–1.1073) |
| Sensitivity: without 2020-21 | 2021-22, 2022-23, 2023-24 | 45.8% / 22.5% / 31.7% | 1.0826 (1.0544–1.1115) |
| Reference: one third each | nothing | 33.3% / 33.3% / 33.3% | 1.0986 (1.0986–1.0986) |

Validation: 2024-25, 380 matches. Outcomes: 155 home wins, 93 draws, 132 away wins.

As a hard prediction the baseline always picks H, so its accuracy (40.8%) is just the share of that outcome. Accuracy is shown for context only;
log loss is the selection metric.

The sensitivity row is information for Milestone 3, not a decision: both training
windows should be compared on validation for the real models too, never on the test season.
