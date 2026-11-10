-- The database holds exactly the selected seasons: none missing, none extra.
SELECT expected.season, 'missing' AS problem
FROM unnest(%(seasons)s::text[]) AS expected(season)
WHERE NOT EXISTS (SELECT 1 FROM matches m WHERE m.season = expected.season)
UNION ALL
SELECT DISTINCT m.season, 'not in cohort' AS problem
FROM matches m
WHERE m.season <> ALL (%(seasons)s::text[])
