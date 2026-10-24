WITH chk_time_sunday AS (WITH ev_time AS (SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
          SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
          SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
          SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
          SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
          SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE))
    SELECT id, CAST(ts AS DATE) AS created_date, FORMAT_DATE('%F', DATE_TRUNC(CAST(ts AS DATE), WEEK(SUNDAY))) AS created_week, FORMAT_DATE('%Y-%m', CAST(ts AS DATE)) AS created_month, FORMAT_DATE('%Y-%m', DATE_TRUNC(CAST(ts AS DATE), QUARTER)) AS created_quarter, EXTRACT(YEAR FROM ts) AS created_year, CASE MOD(EXTRACT(DAYOFWEEK FROM CAST(ts AS DATE)) + 5, 7) WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday' WHEN 3 THEN 'Thursday' WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday' WHEN 6 THEN 'Sunday' END AS created_day_of_week, MOD(MOD(EXTRACT(DAYOFWEEK FROM CAST(ts AS DATE)) + 5, 7) + 1, 7) AS created_day_of_week_index, EXTRACT(MONTH FROM ts) AS created_month_num, CASE EXTRACT(MONTH FROM ts) WHEN 1 THEN 'January' WHEN 2 THEN 'February' WHEN 3 THEN 'March' WHEN 4 THEN 'April' WHEN 5 THEN 'May' WHEN 6 THEN 'June' WHEN 7 THEN 'July' WHEN 8 THEN 'August' WHEN 9 THEN 'September' WHEN 10 THEN 'October' WHEN 11 THEN 'November' WHEN 12 THEN 'December' END AS created_month_name, 'Q' || CAST(EXTRACT(QUARTER FROM CAST(ts AS DATE)) AS STRING) AS created_quarter_of_year, EXTRACT(ISOWEEK FROM CAST(ts AS DATE)) AS created_week_of_year, EXTRACT(DAY FROM ts) AS created_day_of_month, EXTRACT(DAYOFYEAR FROM CAST(ts AS DATE)) AS created_day_of_year, EXTRACT(HOUR FROM ts) AS created_hour_of_day FROM ev_time )
SELECT
    chk_time_sunday.id  AS chk_time_sunday_id,
    chk_time_sunday.created_date  AS chk_time_sunday_created_date,
    chk_time_sunday.created_week  AS chk_time_sunday_created_week,
    chk_time_sunday.created_month  AS chk_time_sunday_created_month,
    chk_time_sunday.created_quarter  AS chk_time_sunday_created_quarter,
    chk_time_sunday.created_year  AS chk_time_sunday_created_year,
    chk_time_sunday.created_day_of_week  AS chk_time_sunday_created_day_of_week,
    chk_time_sunday.created_day_of_week_index  AS chk_time_sunday_created_day_of_week_index,
    chk_time_sunday.created_month_num  AS chk_time_sunday_created_month_num,
    chk_time_sunday.created_month_name  AS chk_time_sunday_created_month_name,
    chk_time_sunday.created_quarter_of_year  AS chk_time_sunday_created_quarter_of_year,
    chk_time_sunday.created_week_of_year  AS chk_time_sunday_created_week_of_year,
    chk_time_sunday.created_day_of_month  AS chk_time_sunday_created_day_of_month,
    chk_time_sunday.created_day_of_year  AS chk_time_sunday_created_day_of_year,
    chk_time_sunday.created_hour_of_day  AS chk_time_sunday_created_hour_of_day
FROM chk_time_sunday
GROUP BY
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15
ORDER BY
    1
LIMIT 500
