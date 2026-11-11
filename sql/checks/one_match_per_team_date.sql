-- No club plays twice on one date. A violation points to a wrong date, which
-- matters because dates decide which results count as earlier form.
-- Postponed and canceled fixtures are left out: they hold no real date.
-- A table rule cannot enforce this: a club can sit in either team column.
SELECT team_id, match_date, count(*) AS matches_found
FROM (SELECT home_team_id AS team_id, match_date, status FROM matches
      UNION ALL SELECT away_team_id, match_date, status FROM matches) AS sides
WHERE status NOT IN ('postponed', 'canceled')
GROUP BY team_id, match_date
HAVING count(*) > 1
