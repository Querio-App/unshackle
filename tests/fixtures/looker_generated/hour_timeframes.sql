WITH ev_l AS (SELECT 1 AS id, 'apple' AS s, 1 AS n, TIMESTAMP '2026-12-31 23:30:45' AS ts UNION ALL
      SELECT 2, "Levi's", 5, TIMESTAMP '2026-12-01 08:05:00' UNION ALL
      SELECT 3, "O'Brien", 7, TIMESTAMP '2027-01-03 10:00:00' UNION ALL
      SELECT 4, CAST(NULL AS STRING), CAST(NULL AS INT64), CAST(NULL AS TIMESTAMP) )
SELECT
    ev_l.id  AS ev_l_id,
        (FORMAT_TIMESTAMP('%F %T', ev_l.ts )) AS ev_l_at_time,
        (FORMAT_TIMESTAMP('%F %H', ev_l.ts )) AS ev_l_at_hour,
        (FORMAT_TIMESTAMP('%F %H:%M', ev_l.ts )) AS ev_l_at_minute,
        (FORMAT_TIMESTAMP('%H:%M', ev_l.ts )) AS ev_l_at_time_of_day,
        (EXTRACT(HOUR FROM ev_l.ts )) AS ev_l_at_hour_of_day,
    COUNT(CASE WHEN (((ev_l.ts ) >= (TIMESTAMP('2026-12-01 00:00:00')) AND (ev_l.ts ) < (TIMESTAMP('2027-01-01 00:00:00')))) THEN 1 ELSE NULL END) AS ev_l_in_range
FROM ev_l
GROUP BY
    1,
    2,
    3,
    4,
    5,
    6
ORDER BY
    1
LIMIT 500
