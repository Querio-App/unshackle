WITH ev_f AS (SELECT 1 AS id, 'apple' AS s, 1.0 AS n, DATE '2026-12-31' AS d, TIMESTAMP '2026-12-31 23:30:00' AS ts, TRUE AS y UNION ALL
      SELECT 2, 'Apple', 5.0, DATE '2027-01-03', TIMESTAMP '2027-01-03 10:00:00', FALSE UNION ALL
      SELECT 3, 'banana', 7.0, DATE '2026-03-01', TIMESTAMP '2026-03-01 12:00:00', CAST(NULL AS BOOL) UNION ALL
      SELECT 4, '', -5.0, DATE '2026-04-01', TIMESTAMP '2026-04-01 00:00:00', TRUE UNION ALL
      SELECT 5, 'a,b', 10.0, DATE '2026-12-01', TIMESTAMP '2026-12-01 08:00:00', FALSE UNION ALL
      SELECT 6, 'pineapple', 2.5, DATE '2027-01-04', TIMESTAMP '2027-01-04 00:15:00', FALSE UNION ALL
      SELECT 7, CAST(NULL AS STRING), CAST(NULL AS FLOAT64), CAST(NULL AS DATE), CAST(NULL AS TIMESTAMP), CAST(NULL AS BOOL) )
SELECT
    COUNT(*) AS ev_f_count,
    COUNT(CASE WHEN (ev_f.s LIKE 'app%') THEN 1 ELSE NULL END) AS ev_f_s_starts,
    COUNT(CASE WHEN (ev_f.s LIKE '%ple') THEN 1 ELSE NULL END) AS ev_f_s_ends,
    COUNT(CASE WHEN (ev_f.s LIKE '%an%') THEN 1 ELSE NULL END) AS ev_f_s_contains,
    COUNT(CASE WHEN (ev_f.s NOT LIKE 'app%' OR ev_f.s IS NULL) THEN 1 ELSE NULL END) AS ev_f_s_not_starts,
    COUNT(CASE WHEN (ev_f.s = 'apple') THEN 1 ELSE NULL END) AS ev_f_s_eq_lower,
    COUNT(CASE WHEN (ev_f.s = 'APPLE') THEN 1 ELSE NULL END) AS ev_f_s_eq_upper,
    COUNT(CASE WHEN (ev_f.s  IN ('apple', 'banana')) THEN 1 ELSE NULL END) AS ev_f_s_list,
    COUNT(CASE WHEN (ev_f.s = 'a,b') THEN 1 ELSE NULL END) AS ev_f_s_escaped_comma,
    COUNT(CASE WHEN ((ev_f.s IS NULL OR LENGTH(ev_f.s ) = 0 )) THEN 1 ELSE NULL END) AS ev_f_s_empty,
    COUNT(CASE WHEN ((ev_f.s IS NULL)) THEN 1 ELSE NULL END) AS ev_f_s_null,
    COUNT(CASE WHEN ((ev_f.n  >= 1 AND ev_f.n  <= 5)) THEN 1 ELSE NULL END) AS ev_f_n_closed,
    COUNT(CASE WHEN ((ev_f.n  > 1 AND ev_f.n  < 5)) THEN 1 ELSE NULL END) AS ev_f_n_open,
    COUNT(CASE WHEN ((ev_f.n  >= 1 AND ev_f.n  < 5)) THEN 1 ELSE NULL END) AS ev_f_n_half_open,
    COUNT(CASE WHEN (ev_f.n  BETWEEN 1 AND 5) THEN 1 ELSE NULL END) AS ev_f_n_to,
    COUNT(CASE WHEN ((ev_f.n  >= 1 AND ev_f.n  <= 5)) THEN 1 ELSE NULL END) AS ev_f_n_and,
    COUNT(CASE WHEN (ev_f.n  > 5 OR ev_f.n  < 2) THEN 1 ELSE NULL END) AS ev_f_n_or,
    COUNT(CASE WHEN NOT (ev_f.n  = 5) THEN 1 ELSE NULL END) AS ev_f_n_not,
    COUNT(CASE WHEN (ev_f.n  = -5) THEN 1 ELSE NULL END) AS ev_f_n_minus,
    COUNT(CASE WHEN (ev_f.n  IN (5,7)) THEN 1 ELSE NULL END) AS ev_f_n_list,
    COUNT(CASE WHEN NOT (ev_f.n  IN (5,7)) THEN 1 ELSE NULL END) AS ev_f_n_not_list,
    COUNT(CASE WHEN (ev_f.n  IS NULL) THEN 1 ELSE NULL END) AS ev_f_n_null,
    COUNT(CASE WHEN NOT (ev_f.n  IS NULL) THEN 1 ELSE NULL END) AS ev_f_n_not_null,
    COUNT(CASE WHEN (ev_f.n  > 5) THEN 1 ELSE NULL END) AS ev_f_n_gt,
    COUNT(CASE WHEN (ev_f.d  = DATE('2026-12-31')) THEN 1 ELSE NULL END) AS ev_f_d_day,
    COUNT(CASE WHEN (((ev_f.d ) >= ((DATE('2026-12-01'))) AND (ev_f.d ) < ((DATE_ADD(DATE('2026-12-01'), INTERVAL 1 MONTH))))) THEN 1 ELSE NULL END) AS ev_f_d_month,
    COUNT(CASE WHEN (((ev_f.d ) >= ((DATE('2026-01-01'))) AND (ev_f.d ) < ((DATE_ADD(DATE('2026-01-01'), INTERVAL 1 YEAR))))) THEN 1 ELSE NULL END) AS ev_f_d_year,
    COUNT(CASE WHEN (ev_f.d  < (DATE('2027-01-01'))) THEN 1 ELSE NULL END) AS ev_f_d_before,
    COUNT(CASE WHEN (ev_f.d  >= DATE('2027-01-01')) THEN 1 ELSE NULL END) AS ev_f_d_after,
    COUNT(CASE WHEN (((ev_f.d ) >= (DATE('2026-12-01')) AND (ev_f.d ) < (DATE('2027-01-01')))) THEN 1 ELSE NULL END) AS ev_f_d_range,
    COUNT(CASE WHEN (ev_f.d  IS NULL) THEN 1 ELSE NULL END) AS ev_f_d_null,
    COUNT(CASE WHEN (ev_f.d  IS NOT NULL) THEN 1 ELSE NULL END) AS ev_f_d_not_null,
    COUNT(CASE WHEN (((ev_f.d ) >= ((DATE_ADD(CURRENT_DATE(), INTERVAL -6 DAY))) AND (ev_f.d ) < ((DATE_ADD(DATE_ADD(CURRENT_DATE(), INTERVAL -6 DAY), INTERVAL 7 DAY))))) THEN 1 ELSE NULL END) AS ev_f_d_last_7,
    COUNT(CASE WHEN (((ev_f.d ) >= ((DATE_TRUNC(CURRENT_DATE(), MONTH))) AND (ev_f.d ) < ((DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH))))) THEN 1 ELSE NULL END) AS ev_f_d_this_month,
    COUNT(CASE WHEN (((ev_f.d ) >= ((DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL -1 MONTH))) AND (ev_f.d ) < ((DATE_ADD(DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL -1 MONTH), INTERVAL 1 MONTH))))) THEN 1 ELSE NULL END) AS ev_f_d_last_month,
    COUNT(CASE WHEN (ev_f.d  < (DATE_ADD(CURRENT_DATE(), INTERVAL -30 DAY))) THEN 1 ELSE NULL END) AS ev_f_d_ago,
    COUNT(CASE WHEN (((ev_f.ts ) >= ((TIMESTAMP('2026-12-31 00:00:00'))) AND (ev_f.ts ) < ((TIMESTAMP_ADD(TIMESTAMP('2026-12-31 00:00:00'), INTERVAL 1 DAY))))) THEN 1 ELSE NULL END) AS ev_f_t_day,
    COUNT(CASE WHEN (ev_f.ts  < (TIMESTAMP('2027-01-03 10:00:00'))) THEN 1 ELSE NULL END) AS ev_f_t_before,
    COUNT(CASE WHEN (((ev_f.ts ) >= ((TIMESTAMP('2026-12-01 00:00:00'))) AND (ev_f.ts ) < ((TIMESTAMP(DATETIME_ADD(DATETIME(TIMESTAMP('2026-12-01 00:00:00')), INTERVAL 1 MONTH)))))) THEN 1 ELSE NULL END) AS ev_f_t_month,
    COUNT(CASE WHEN ((CASE
WHEN ev_f.n  < 0 THEN 'Below 0'
WHEN ev_f.n  >= 0 AND ev_f.n  < 5 THEN '0 to 4'
WHEN ev_f.n  >= 5 AND ev_f.n  < 10 THEN '5 to 9'
WHEN ev_f.n  >= 10 THEN '10 or Above'
ELSE 'Undefined'
END) = '5 to 9') THEN 1 ELSE NULL END) AS ev_f_tier_filtered
FROM ev_f
LIMIT 500
