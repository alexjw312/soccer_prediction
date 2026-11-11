-- Milestone 2: saved team-form features.

-- Grain: one team's form entering one fixture, for one feature version.
-- Each fixture has two rows: venue 'H' for the home side, 'A' for the away side.
--
-- Features are saved rather than recomputed on demand so that a forecast can
-- always be traced to the exact inputs it used, even if later data changes.
CREATE TABLE team_form_snapshots (
    match_id            integer          NOT NULL REFERENCES matches (match_id),
    feature_version     text             NOT NULL,
    venue               text             NOT NULL CHECK (venue IN ('H', 'A')),
    team_id             integer          NOT NULL REFERENCES teams (team_id),
    -- The newest match date that was allowed to count (match date minus 2 days).
    feature_cutoff_date date             NOT NULL,
    -- The newest match date that actually counted. NULL when there was no history.
    latest_history_date date,
    games_last5         smallint         NOT NULL CHECK (games_last5 BETWEEN 0 AND 5),
    ppg_last5           double precision CHECK (ppg_last5 BETWEEN 0 AND 3),
    goals_for_last5     double precision CHECK (goals_for_last5 >= 0),
    goals_against_last5 double precision CHECK (goals_against_last5 >= 0),
    games_last10        smallint         NOT NULL CHECK (games_last10 BETWEEN 0 AND 10),
    ppg_last10          double precision CHECK (ppg_last10 BETWEEN 0 AND 3),
    goals_for_last10    double precision CHECK (goals_for_last10 >= 0),
    goals_against_last10 double precision CHECK (goals_against_last10 >= 0),
    built_at            timestamptz      NOT NULL DEFAULT now(),

    PRIMARY KEY (match_id, feature_version, venue),
    -- The leakage rule, enforced by the database: nothing newer than the
    -- cutoff may be part of a saved feature.
    CONSTRAINT form_history_before_cutoff CHECK (latest_history_date <= feature_cutoff_date),
    -- The last-5 window is the newest part of the last-10 window.
    CONSTRAINT form_windows_nest CHECK (games_last5 = least(games_last10, 5)),
    -- No history means no averages. Any history means every average is present.
    -- Missing values stay missing here; filling them is a modeling decision.
    CONSTRAINT form_history_fits_values CHECK (
        (games_last10 = 0 AND latest_history_date IS NULL
         AND ppg_last5 IS NULL AND goals_for_last5 IS NULL AND goals_against_last5 IS NULL
         AND ppg_last10 IS NULL AND goals_for_last10 IS NULL AND goals_against_last10 IS NULL)
     OR (games_last10 > 0 AND latest_history_date IS NOT NULL
         AND ppg_last5 IS NOT NULL AND goals_for_last5 IS NOT NULL AND goals_against_last5 IS NOT NULL
         AND ppg_last10 IS NOT NULL AND goals_for_last10 IS NOT NULL AND goals_against_last10 IS NOT NULL))
);

-- Grain: one fixture for one feature version, with the home side's form next
-- to the away side's. This is the shape a model needs: one row per prediction.
--
-- It deliberately has no result or goals columns. Features and the label only
-- meet in the training code, which makes it hard to feed a model its own answer.
CREATE VIEW fixture_features AS
SELECT m.match_id, m.competition, m.season, m.match_date,
       h.feature_version, h.feature_cutoff_date,
       h.games_last5          AS home_games_last5,
       h.ppg_last5            AS home_ppg_last5,
       h.goals_for_last5      AS home_goals_for_last5,
       h.goals_against_last5  AS home_goals_against_last5,
       h.games_last10         AS home_games_last10,
       h.ppg_last10           AS home_ppg_last10,
       h.goals_for_last10     AS home_goals_for_last10,
       h.goals_against_last10 AS home_goals_against_last10,
       a.games_last5          AS away_games_last5,
       a.ppg_last5            AS away_ppg_last5,
       a.goals_for_last5      AS away_goals_for_last5,
       a.goals_against_last5  AS away_goals_against_last5,
       a.games_last10         AS away_games_last10,
       a.ppg_last10           AS away_ppg_last10,
       a.goals_for_last10     AS away_goals_for_last10,
       a.goals_against_last10 AS away_goals_against_last10
FROM matches m
JOIN team_form_snapshots h ON h.match_id = m.match_id AND h.venue = 'H'
JOIN team_form_snapshots a ON a.match_id = m.match_id AND a.venue = 'A'
                          AND a.feature_version = h.feature_version;
