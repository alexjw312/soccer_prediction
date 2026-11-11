-- Every club plays each of the other 19 clubs once at home and once away.
-- Counted from all fixtures, so it also holds for the live season.
SELECT season, team_id,
       count(*) FILTER (WHERE venue = 'H') AS home_games,
       count(*) FILTER (WHERE venue = 'A') AS away_games
FROM (SELECT season, home_team_id AS team_id, 'H' AS venue FROM matches
      UNION ALL SELECT season, away_team_id, 'A' FROM matches) AS sides
WHERE season = ANY(%(seasons)s::text[]) OR season = ANY(%(live_seasons)s::text[])
GROUP BY season, team_id
HAVING count(*) FILTER (WHERE venue = 'H') <> 19
    OR count(*) FILTER (WHERE venue = 'A') <> 19
