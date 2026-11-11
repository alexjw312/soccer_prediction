-- Every live-season match is tied to exactly one football-data.org match id,
-- so later downloads update it instead of creating a duplicate.
SELECT m.season, m.match_id, count(p.provider_match_id) AS provider_ids
FROM matches m
LEFT JOIN provider_matches p ON p.match_id = m.match_id
WHERE m.season = ANY(%(live_seasons)s::text[])
GROUP BY m.season, m.match_id
HAVING count(p.provider_match_id) <> 1
