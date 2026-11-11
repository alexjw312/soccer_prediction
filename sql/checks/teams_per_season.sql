-- A Premier League season has 20 clubs. Counted from all fixtures, played or
-- not, so the live season passes from its first day.
SELECT season, count(DISTINCT team_id) AS teams_found
FROM (SELECT season, home_team_id AS team_id FROM matches
      UNION ALL SELECT season, away_team_id FROM matches) AS sides
WHERE season = ANY(%(seasons)s::text[]) OR season = ANY(%(live_seasons)s::text[])
GROUP BY season
HAVING count(DISTINCT team_id) <> 20
