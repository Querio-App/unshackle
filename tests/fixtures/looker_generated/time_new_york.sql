WITH ev_time AS (SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
      SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
      SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
      SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
      SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
      SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE) )
SELECT
    ev_time.id  AS ev_time_id,
        (FORMAT_TIMESTAMP('%F %T', ev_time.ts )) AS ev_time_created_time,
        (DATE(ev_time.ts )) AS ev_time_created_date,
        (FORMAT_TIMESTAMP('%F %T', ev_time.ts )) AS ev_time_created_tz_time,
        (DATE(ev_time.ts )) AS ev_time_created_tz_date,
        (FORMAT_TIMESTAMP('%F', TIMESTAMP_TRUNC(ev_time.ts , WEEK(MONDAY)))) AS ev_time_created_tz_week,
        (FORMAT_TIMESTAMP('%Y-%m', ev_time.ts )) AS ev_time_created_tz_month
FROM ev_time
ORDER BY
    1
LIMIT 500
