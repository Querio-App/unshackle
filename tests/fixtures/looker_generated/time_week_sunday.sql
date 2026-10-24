WITH ev_time AS (SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
      SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
      SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
      SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
      SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
      SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE) )
SELECT
    ev_time.id  AS ev_time_id,
        (FORMAT_TIMESTAMP('%F', TIMESTAMP_TRUNC(ev_time.ts , WEEK(SUNDAY)))) AS ev_time_created_week,
        ((EXTRACT(DAYOFWEEK FROM ev_time.ts ) - 1)) AS ev_time_created_day_of_week_index,
        (CAST(FORMAT_TIMESTAMP('%V', ev_time.ts ) AS INT64)) AS ev_time_created_week_of_year,
        (FORMAT_DATE('%F', DATE_TRUNC(ev_time.d , WEEK(SUNDAY)))) AS ev_time_day_week
FROM ev_time
ORDER BY
    1
LIMIT 500
