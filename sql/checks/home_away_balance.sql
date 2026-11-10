-- Every club plays each of the other 19 clubs once at home and once away.
SELECT season, team_id,
       count(*) FILTER (WHERE venue = 'H') AS home_games,
       count(*) FILTER (WHERE venue = 'A') AS away_games
FROM team_matches
GROUP BY season, team_id
HAVING count(*) FILTER (WHERE venue = 'H') <> 19
    OR count(*) FILTER (WHERE venue = 'A') <> 19
