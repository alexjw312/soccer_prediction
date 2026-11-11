-- A Premier League season has 380 fixtures. In a historical season they are
-- all completed; in the live season they include matches not yet played.
-- A short count means rows were lost on the way in.
SELECT season, count(*) AS rows_found
FROM matches
WHERE season = ANY(%(seasons)s::text[]) OR season = ANY(%(live_seasons)s::text[])
GROUP BY season
HAVING count(*) <> 380
