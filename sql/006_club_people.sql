-- Milestone 5: each club's manager and current squad, for the dashboard's team
-- and match pages.
--
-- Copied from the newest football-data.org teams file kept in
-- live_download_files (update_club_people in live_loading.py), by load-live
-- and by the update-clubs command. Display data only: no feature or forecast
-- uses it, and it is replaced whole each time, so it is the squad as listed
-- now, not a history.
--
-- These are columns on teams rather than new tables because the dashboard's
-- read-only login can already read teams, and a table's SELECT grant covers
-- columns added later. So no new grant (and no new password) is needed.
ALTER TABLE teams ADD COLUMN coach_name        text;
ALTER TABLE teams ADD COLUMN coach_nationality text;
-- One object per player: name, position, date_of_birth, nationality.
ALTER TABLE teams ADD COLUMN squad             jsonb
    CONSTRAINT teams_squad_is_a_list CHECK (squad IS NULL OR jsonb_typeof(squad) = 'array');
-- The download (snapshot folder name) the three values above were last read from.
ALTER TABLE teams ADD COLUMN squad_snapshot    text;
