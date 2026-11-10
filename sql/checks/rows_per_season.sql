-- A Premier League season has 380 fixtures. A short count means rows were
-- lost on the way in, for example a score format the parser did not handle.
SELECT season, count(*) AS rows_found
FROM matches
GROUP BY season
HAVING count(*) <> 380
