-- No club plays twice on one date. A violation points to a wrong date, which
-- matters because dates decide which results count as earlier form.
-- A table rule cannot enforce this: a club can sit in either team column.
SELECT team_id, match_date, count(*) AS matches_found
FROM team_matches
GROUP BY team_id, match_date
HAVING count(*) > 1
