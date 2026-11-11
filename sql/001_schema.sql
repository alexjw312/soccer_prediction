-- Milestone 1 schema: the historical database.
--
-- Applied once by python -m soccer_forecasting init-db, which records it in
-- schema_migrations. There is no IF NOT EXISTS on purpose: if this file ever
-- runs against tables that already exist, it should fail loudly rather than
-- leave an out-of-date table in place.

-- One canonical club. team_id is the stable internal identifier that every
-- other table uses, so a provider renaming a club never changes our history.
CREATE TABLE teams (
    team_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_name text NOT NULL UNIQUE
);

-- One name a provider uses for one club. New providers add rows here.
-- A name with no row here stops the load, so a renamed club can never
-- quietly become a brand-new team.
CREATE TABLE team_aliases (
    provider text    NOT NULL,
    alias    text    NOT NULL,
    team_id  integer NOT NULL REFERENCES teams (team_id),
    PRIMARY KEY (provider, alias)
);

-- One raw file from one provider at one commit. This is what lets any match
-- be traced back to the exact bytes it came from. retrieved_at_utc is when
-- this project downloaded the file, which says nothing about when an old
-- result first became known.
CREATE TABLE source_files (
    source_file_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider         text        NOT NULL,
    source_commit    text        NOT NULL,
    source_path      text        NOT NULL,
    url              text        NOT NULL,
    sha256           text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_count       bigint      NOT NULL CHECK (byte_count > 0),
    retrieved_at_utc timestamptz NOT NULL,
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, source_commit, source_path)
);

-- Grain: one league fixture in one season. A rescheduled match stays one row,
-- which is why the date is not part of the key.
--
-- Deliberately absent:
--   half-time goals and the raw score format: a missing half-time score
--     identifies a 0-0 game, so these stay in the raw files;
--   train/validation/test labels: a modeling choice, kept in config;
--   UTC kickoff and "result first seen" times: these cannot be filled
--     honestly for old matches, so they arrive with the live feed.
CREATE TABLE matches (
    match_id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    competition      text        NOT NULL,
    season           text        NOT NULL,
    home_team_id     integer     NOT NULL REFERENCES teams (team_id),
    away_team_id     integer     NOT NULL REFERENCES teams (team_id),
    match_date       date        NOT NULL,
    source_time      text,
    source_round     text,
    status           text        NOT NULL,
    home_goals       smallint,
    away_goals       smallint,
    -- Generated, so the label can never disagree with the goals.
    result           text GENERATED ALWAYS AS (
                         CASE WHEN home_goals > away_goals THEN 'H'
                              WHEN home_goals < away_goals THEN 'A'
                              WHEN home_goals = away_goals THEN 'D' END
                     ) STORED,
    source_file_id   integer     NOT NULL REFERENCES source_files (source_file_id),
    source_row_index integer     NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),

    -- Stops duplicates, and is the target that makes reloading safe.
    CONSTRAINT matches_fixture_unique    UNIQUE (competition, season, home_team_id, away_team_id),
    CONSTRAINT matches_source_row_unique UNIQUE (source_file_id, source_row_index),
    CONSTRAINT matches_different_teams   CHECK (home_team_id <> away_team_id),
    CONSTRAINT matches_season_format     CHECK (season ~ '^[0-9]{4}-[0-9]{2}$'),
    -- Refuses the error found in 2019/20, where July 2020 was stored as July 2019.
    CONSTRAINT matches_date_in_season    CHECK (match_date BETWEEN make_date(left(season, 4)::int, 8, 1)
                                                               AND make_date(left(season, 4)::int + 1, 7, 31)),
    CONSTRAINT matches_status_valid      CHECK (status IN ('scheduled','completed','postponed','abandoned','canceled')),
    CONSTRAINT matches_goals_nonnegative CHECK (home_goals >= 0 AND away_goals >= 0),
    -- Only completed matches have scores, and every completed match has one.
    CONSTRAINT matches_goals_fit_status  CHECK (
        (status =  'completed' AND home_goals IS NOT NULL AND away_goals IS NOT NULL)
     OR (status <> 'completed' AND home_goals IS NULL     AND away_goals IS NULL))
);

-- Grain: one team in one completed match, so each match appears twice.
-- matches stores a team in either of two columns, which makes "a team's last
-- five games" awkward. Here each team has one column to partition by, which
-- is the shape window functions need in Milestone 2. It is a view so it can
-- never drift from matches.
CREATE VIEW team_matches AS
SELECT sides.*,
       CASE WHEN goals_for > goals_against THEN 3
            WHEN goals_for = goals_against THEN 1
            ELSE 0 END AS points
FROM (
    SELECT match_id, competition, season, match_date,
           home_team_id AS team_id, away_team_id AS opponent_team_id,
           'H' AS venue, home_goals AS goals_for, away_goals AS goals_against
    FROM matches
    WHERE status = 'completed'
    UNION ALL
    SELECT match_id, competition, season, match_date,
           away_team_id AS team_id, home_team_id AS opponent_team_id,
           'A' AS venue, away_goals AS goals_for, home_goals AS goals_against
    FROM matches
    WHERE status = 'completed'
) AS sides;
