-- A Premier League season has 20 clubs.
SELECT season, count(DISTINCT team_id) AS teams_found
FROM team_matches
GROUP BY season
HAVING count(DISTINCT team_id) <> 20
