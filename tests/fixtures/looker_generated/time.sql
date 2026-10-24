WITH ev_time AS (SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
      SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
      SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
      SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
      SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
      SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE) )
SELECT
    ev_time.id  AS ev_time_id,
    ev_time.ts  AS ev_time_created_raw,
        (FORMAT_TIMESTAMP('%F %T', ev_time.ts )) AS ev_time_created_time,
        (DATE(ev_time.ts )) AS ev_time_created_date,
        (FORMAT_TIMESTAMP('%F', TIMESTAMP_TRUNC(ev_time.ts , WEEK(MONDAY)))) AS ev_time_created_week,
        (FORMAT_TIMESTAMP('%Y-%m', ev_time.ts )) AS ev_time_created_month,
        (FORMAT_TIMESTAMP('%Y-%m', TIMESTAMP_TRUNC(ev_time.ts , QUARTER))) AS ev_time_created_quarter,
        (EXTRACT(YEAR FROM ev_time.ts )) AS ev_time_created_year,
        (FORMAT_TIMESTAMP('%A', ev_time.ts )) AS ev_time_created_day_of_week,
        (MOD((EXTRACT(DAYOFWEEK FROM ev_time.ts ) - 1) - 1 + 7, 7)) AS ev_time_created_day_of_week_index,
        (EXTRACT(MONTH FROM ev_time.ts )) AS ev_time_created_month_num,
        (CASE
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 1 THEN 'January'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 2 THEN 'February'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 3 THEN 'March'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 4 THEN 'April'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 5 THEN 'May'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 6 THEN 'June'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 7 THEN 'July'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 8 THEN 'August'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 9 THEN 'September'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 10 THEN 'October'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 11 THEN 'November'
  WHEN EXTRACT(MONTH FROM ev_time.ts ) = 12 THEN 'December'
END
) AS ev_time_created_month_name,
        (CONCAT('Q', EXTRACT(QUARTER FROM ev_time.ts ))) AS ev_time_created_quarter_of_year,
        (CAST(FORMAT_TIMESTAMP('%V', ev_time.ts ) AS INT64)) AS ev_time_created_week_of_year,
        (EXTRACT(DAY FROM ev_time.ts )) AS ev_time_created_day_of_month,
        (EXTRACT(DAYOFYEAR FROM ev_time.ts )) AS ev_time_created_day_of_year,
        (EXTRACT(HOUR FROM ev_time.ts )) AS ev_time_created_hour_of_day,
        (FORMAT_TIMESTAMP('%F %T', ev_time.ts )) AS ev_time_created_tz_time,
        (DATE(ev_time.ts )) AS ev_time_created_tz_date,
        (FORMAT_TIMESTAMP('%F', TIMESTAMP_TRUNC(ev_time.ts , WEEK(MONDAY)))) AS ev_time_created_tz_week,
        (FORMAT_TIMESTAMP('%Y-%m', ev_time.ts )) AS ev_time_created_tz_month,
        (ev_time.d ) AS ev_time_day_date,
        (FORMAT_DATE('%F', DATE_TRUNC(ev_time.d , WEEK(MONDAY)))) AS ev_time_day_week,
        (FORMAT_DATE('%Y-%m', ev_time.d )) AS ev_time_day_month,
        (FORMAT_DATE('%Y-%m', DATE_TRUNC(ev_time.d , QUARTER))) AS ev_time_day_quarter,
        (EXTRACT(YEAR FROM ev_time.d )) AS ev_time_day_year,
        (MOD((EXTRACT(DAYOFWEEK FROM ev_time.d ) - 1) - 1 + 7, 7)) AS ev_time_day_day_of_week_index,
        (FORMAT_DATE('%A', ev_time.d )) AS ev_time_day_day_of_week
FROM ev_time
ORDER BY
    1
LIMIT 500