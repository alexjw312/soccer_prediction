-- Milestone 5 step 1: keep copies of files inside the database.
--
-- The daily job can now run on a computer that starts empty every time
-- (GitHub Actions). The database is then the only thing that lasts between
-- runs, so it keeps the two kinds of file the job used to find on disk.

-- Grain: one deployment model file, byte for byte the model.joblib that
-- train-deployment wrote. The database computes the file's hash itself, and
-- the foreign key requires that hash to equal the one recorded in model_runs,
-- so a stored copy can never differ from the recorded model.
ALTER TABLE model_runs ADD CONSTRAINT model_runs_run_and_file_key UNIQUE (model_run_id, artifact_sha256);

CREATE TABLE model_files (
    model_run_id   integer PRIMARY KEY,
    content        bytea   NOT NULL,
    content_sha256 text    GENERATED ALWAYS AS (encode(sha256(content), 'hex')) STORED,
    FOREIGN KEY (model_run_id, content_sha256) REFERENCES model_runs (model_run_id, artifact_sha256)
);

-- Grain: one file of one football-data.org download (matches.json, teams.json
-- or manifest.json). On a Mac the files also stay in data/live; on GitHub's
-- computers they exist only during a run, so this is the lasting raw copy.
-- It stays private in the database, because football-data.org's terms govern
-- the data. PostgreSQL compresses large values like these automatically.
CREATE TABLE live_download_files (
    provider       text        NOT NULL,
    snapshot       text        NOT NULL CHECK (snapshot ~ '^[0-9]{8}T[0-9]{6}Z$'),
    file_name      text        NOT NULL CHECK (file_name ~ '^[a-z_]+\.json$'),
    content        bytea       NOT NULL,
    content_sha256 text        GENERATED ALWAYS AS (encode(sha256(content), 'hex')) STORED,
    stored_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, snapshot, file_name)
);
