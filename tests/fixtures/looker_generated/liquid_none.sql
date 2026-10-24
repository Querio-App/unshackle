WITH ev_l AS (SELECT 1 AS id, 'apple' AS s, 1 AS n, TIMESTAMP '2026-12-31 23:30:45' AS ts UNION ALL
      SELECT 2, "Levi's", 5, TIMESTAMP '2026-12-01 08:05:00' UNION ALL
      SELECT 3, "O'Brien", 7, TIMESTAMP '2027-01-03 10:00:00' UNION ALL
      SELECT 4, CAST(NULL AS STRING), CAST(NULL AS INT64), CAST(NULL AS TIMESTAMP) )
SELECT
    ev_l.id  AS ev_l_id,
    CAST(NULL AS STRING)  AS ev_l_range_start,
    CAST(NULL AS STRING)  AS ev_l_range_end,
     'no'   AS ev_l_flag_filtered,
     'no'   AS ev_l_flag_in_query,
     'no'   AS ev_l_flag_selected,
    'plain'  AS ev_l_p_str_param,
     'quoted'   AS ev_l_p_str_branch,
    3  AS ev_l_p_num_param
FROM ev_l
ORDER BY
    1
LIMIT 500
