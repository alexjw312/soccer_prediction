-- Milestone 4: live fixtures and results from football-data.org.

-- Kickoff time in UTC. Historical OpenFootball rows only have a UK clock time,
-- so they keep NULL here; live rows always have it.
ALTER TABLE matches ADD COLUMN kickoff_utc timestamptz;

-- One provider's identifier for one of our matches. Provider ids live here,
-- not in matches, so a second provider could be added without changing it.
CREATE TABLE provider_matches (
    provider          text    NOT NULL,
    provider_match_id text    NOT NULL,
    match_id          integer NOT NULL REFERENCES matches (match_id),
    PRIMARY KEY (provider, provider_match_id),
    UNIQUE (provider, match_id)
);

-- Grain: one provider match as seen in one downloaded snapshot.
-- matches holds each fixture's current state; this table keeps every state
-- ever downloaded, so we can tell when a result was first known. A live
-- forecast may only use results that were downloaded before it was made.
CREATE TABLE match_observations (
    source_file_id    integer     NOT NULL REFERENCES source_files (source_file_id),
    provider          text        NOT NULL,
    provider_match_id text        NOT NULL,
    match_id          integer     NOT NULL REFERENCES matches (match_id),
    observed_at_utc   timestamptz NOT NULL,
    provider_status   text        NOT NULL,
    status            text        NOT NULL
                      CHECK (status IN ('scheduled', 'completed', 'postponed', 'abandoned', 'canceled')),
    kickoff_utc       timestamptz NOT NULL,
    home_goals        smallint    CHECK (home_goals >= 0),
    away_goals        smallint    CHECK (away_goals >= 0),
    PRIMARY KEY (source_file_id, provider, provider_match_id),
    FOREIGN KEY (provider, provider_match_id) REFERENCES provider_matches (provider, provider_match_id),
    CONSTRAINT observation_goals_fit_status CHECK (
        (status =  'completed' AND home_goals IS NOT NULL AND away_goals IS NOT NULL)
     OR (status <> 'completed' AND home_goals IS NULL     AND away_goals IS NULL))
);

-- When each live result was first seen in a download. Historical matches
-- have no row here: for them the two-day rule is a stated assumption instead.
CREATE VIEW result_first_seen AS
SELECT match_id, min(observed_at_utc) AS first_seen_utc
FROM match_observations
WHERE status = 'completed'
GROUP BY match_id;
