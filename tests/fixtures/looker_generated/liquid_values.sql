WITH ev_l AS (SELECT 1 AS id, 'apple' AS s, 1 AS n, TIMESTAMP '2026-12-31 23:30:45' AS ts UNION ALL
      SELECT 2, "Levi's", 5, TIMESTAMP '2026-12-01 08:05:00' UNION ALL
      SELECT 3, "O'Brien", 7, TIMESTAMP '2027-01-03 10:00:00' UNION ALL
      SELECT 4, CAST(NULL AS STRING), CAST(NULL AS INT64), CAST(NULL AS TIMESTAMP) )
SELECT
    ev_l.id  AS ev_l_id,
    ev_l.s  AS ev_l_s,
    CAST(TIMESTAMP('2026-12-01 00:00:00') AS STRING)  AS ev_l_range_start,
    CAST(TIMESTAMP('2027-01-01 00:00:00') AS STRING)  AS ev_l_range_end,
     'yes'   AS ev_l_flag_filtered,
     'yes'   AS ev_l_flag_in_query,
     'yes'   AS ev_l_flag_selected,
    'O\'Brien'  AS ev_l_p_str_param,
     'neither'   AS ev_l_p_str_branch,
    5  AS ev_l_p_num_param
FROM ev_l
WHERE (ev_l.s ) <> 'zzz' OR (ev_l.s ) IS NULL
ORDER BY
    1
LIMIT 500
