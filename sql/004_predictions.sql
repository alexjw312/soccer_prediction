-- Milestone 4 step 3: deployment models, saved forecasts and job runs.

-- Grain: one trained model file. The deployment model is refitted on every
-- historical season with the frozen settings, so it is a separate training
-- run from the one the final test evaluated.
CREATE TABLE model_runs (
    model_run_id              integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_name                text        NOT NULL,
    purpose                   text        NOT NULL CHECK (purpose IN ('deployment')),
    feature_version           text        NOT NULL,
    settings                  jsonb       NOT NULL,
    training_seasons          text[]      NOT NULL,
    training_matches          integer     NOT NULL CHECK (training_matches > 0),
    training_data_fingerprint text        NOT NULL,
    protocol_sha256           text        NOT NULL,
    class_order               text[]      NOT NULL CHECK (class_order = ARRAY['H', 'D', 'A']),
    features                  text[]      NOT NULL,
    artifact_path             text        NOT NULL,
    artifact_sha256           text        NOT NULL UNIQUE CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    packages                  jsonb       NOT NULL,
    source_commit             text        NOT NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_name, settings, training_data_fingerprint, protocol_sha256)
);

-- Grain: one forecast for one match, by one model, made for one match day.
-- A forecast is never updated. If a match moves to another day, a new
-- forecast for the new day is added and the old one stays as it was.
CREATE TABLE predictions (
    prediction_id          integer          GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_id               integer          NOT NULL REFERENCES matches (match_id),
    model_run_id           integer          NOT NULL REFERENCES model_runs (model_run_id),
    feature_version        text             NOT NULL,
    forecast_for_date      date             NOT NULL,
    feature_cutoff_date    date             NOT NULL,
    information_cutoff_utc timestamptz      NOT NULL,
    generated_at_utc       timestamptz      NOT NULL,
    kickoff_utc            timestamptz      NOT NULL,
    data_snapshot          text             NOT NULL,
    p_home                 double precision NOT NULL CHECK (p_home BETWEEN 0 AND 1),
    p_draw                 double precision NOT NULL CHECK (p_draw BETWEEN 0 AND 1),
    p_away                 double precision NOT NULL CHECK (p_away BETWEEN 0 AND 1),
    features               jsonb            NOT NULL,
    UNIQUE (match_id, model_run_id, forecast_for_date),
    CONSTRAINT prediction_sums_to_one   CHECK (abs(p_home + p_draw + p_away - 1) < 1e-6),
    -- The point of the whole service: a forecast is made before kickoff.
    CONSTRAINT prediction_before_kickoff CHECK (generated_at_utc < kickoff_utc),
    CONSTRAINT prediction_cutoff_before_match_day CHECK (feature_cutoff_date < forecast_for_date),
    CONSTRAINT prediction_uses_only_known_results CHECK (information_cutoff_utc <= generated_at_utc)
);

-- Each forecast next to what happened. log_loss is filled once the match is
-- completed. Only forecasts still on their match's date count as on time:
-- one made for a day the match was later moved away from is kept for the
-- record but is not a forecast of the match as played.
CREATE VIEW prediction_results AS
SELECT p.*, m.match_date, m.status AS match_status, m.result,
       (m.match_date = p.forecast_for_date) AS on_forecast_date,
       CASE m.result WHEN 'H' THEN -ln(p.p_home)
                     WHEN 'D' THEN -ln(p.p_draw)
                     WHEN 'A' THEN -ln(p.p_away) END AS log_loss
FROM predictions p
JOIN matches m USING (match_id);

-- Grain: one run of a scheduled job, so a failure is visible instead of
-- quietly leaving yesterday's forecasts looking fresh.
CREATE TABLE job_runs (
    job_run_id   integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job          text        NOT NULL,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL,
    status       text        NOT NULL CHECK (status IN ('succeeded', 'failed')),
    details      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CHECK (finished_at >= started_at)
);
