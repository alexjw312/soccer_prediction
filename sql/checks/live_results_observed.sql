-- A live match marked completed must have a download that showed that exact
-- result, so we always know when the result was first seen.
SELECT m.season, m.match_id
FROM matches m
WHERE m.season = ANY(%(live_seasons)s::text[])
  AND m.status = 'completed'
  AND NOT EXISTS (SELECT 1 FROM match_observations o
                  WHERE o.match_id = m.match_id AND o.status = 'completed'
                    AND o.home_goals = m.home_goals AND o.away_goals = m.away_goals)
