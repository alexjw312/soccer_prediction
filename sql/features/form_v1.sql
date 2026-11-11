-- Feature version form_v1: each team's recent form entering a fixture.
-- Returns two rows per fixture, one for each side.
-- Parameters: offset_days, the cutoff relative to match day (-2 in config);
-- seasons, the fixtures to compute features for. History from any season
-- can count, but only if it is dated on or before the cutoff.
--
-- Step 1, team_form: a window function. For every completed match a team
-- played, it averages that match together with the ones just before it.
-- "ROWS BETWEEN 4 PRECEDING AND CURRENT ROW" means this match and the 4
-- before it: the last 5 by date. PARTITION BY team_id restarts the count for
-- each team, which is why team_matches puts each team in a single column.
--
-- Step 2, the LATERAL join: for each side of each fixture, find that team's
-- newest match dated on or before the cutoff, and take the form computed
-- after it. Every row inside that window is older still, so nothing after the
-- cutoff can enter, including the fixture itself.
--
-- Why not use "ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING" on the fixture's own
-- row? That skips the fixture but would still count a match played the day
-- before, which the two-day rule excludes. It also has no row to start from
-- for a fixture that has not been played yet, and live forecasts need that.
WITH team_form AS MATERIALIZED (
    -- MATERIALIZED computes this once; otherwise PostgreSQL may redo the
    -- window calculation for every fixture in the LATERAL join below.
    SELECT team_id, match_id, match_date,
           count(*)                                OVER last5  AS games_last5,
           (avg(points)        OVER last5)::double precision  AS ppg_last5,
           (avg(goals_for)     OVER last5)::double precision  AS goals_for_last5,
           (avg(goals_against) OVER last5)::double precision  AS goals_against_last5,
           count(*)                                OVER last10 AS games_last10,
           (avg(points)        OVER last10)::double precision AS ppg_last10,
           (avg(goals_for)     OVER last10)::double precision AS goals_for_last10,
           (avg(goals_against) OVER last10)::double precision AS goals_against_last10
    FROM team_matches
    WINDOW last5  AS (PARTITION BY team_id ORDER BY match_date, match_id
                      ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
           last10 AS (PARTITION BY team_id ORDER BY match_date, match_id
                      ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
),
fixture_sides AS (
    SELECT match_id, 'H' AS venue, home_team_id AS team_id,
           match_date + %(offset_days)s::integer AS feature_cutoff_date
    FROM matches WHERE season = ANY(%(seasons)s::text[])
    UNION ALL
    SELECT match_id, 'A', away_team_id,
           match_date + %(offset_days)s::integer
    FROM matches WHERE season = ANY(%(seasons)s::text[])
)
SELECT s.match_id, s.venue, s.team_id, s.feature_cutoff_date,
       f.match_date             AS latest_history_date,
       coalesce(f.games_last5, 0)  AS games_last5,
       f.ppg_last5, f.goals_for_last5, f.goals_against_last5,
       coalesce(f.games_last10, 0) AS games_last10,
       f.ppg_last10, f.goals_for_last10, f.goals_against_last10
FROM fixture_sides s
LEFT JOIN LATERAL (
    SELECT tf.*
    FROM team_form tf
    WHERE tf.team_id = s.team_id
      AND tf.match_date <= s.feature_cutoff_date
    ORDER BY tf.match_date DESC, tf.match_id DESC
    LIMIT 1
) f ON true
ORDER BY s.match_id, s.venue
