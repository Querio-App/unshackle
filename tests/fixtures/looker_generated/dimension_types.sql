WITH ev_f AS (SELECT 1 AS id, 'apple' AS s, 1.0 AS n, DATE '2026-12-31' AS d, TIMESTAMP '2026-12-31 23:30:00' AS ts, TRUE AS y UNION ALL
      SELECT 2, 'Apple', 5.0, DATE '2027-01-03', TIMESTAMP '2027-01-03 10:00:00', FALSE UNION ALL
      SELECT 3, 'banana', 7.0, DATE '2026-03-01', TIMESTAMP '2026-03-01 12:00:00', CAST(NULL AS BOOL) UNION ALL
      SELECT 4, '', -5.0, DATE '2026-04-01', TIMESTAMP '2026-04-01 00:00:00', TRUE UNION ALL
      SELECT 5, 'a,b', 10.0, DATE '2026-12-01', TIMESTAMP '2026-12-01 08:00:00', FALSE UNION ALL
      SELECT 6, 'pineapple', 2.5, DATE '2027-01-04', TIMESTAMP '2027-01-04 00:15:00', FALSE UNION ALL
      SELECT 7, CAST(NULL AS STRING), CAST(NULL AS FLOAT64), CAST(NULL AS DATE), CAST(NULL AS TIMESTAMP), CAST(NULL AS BOOL) )
SELECT
    ev_f.id  AS ev_f_id,
    ev_f.s  AS ev_f_s,
    ev_f.n  AS ev_f_n,
        (CASE WHEN ev_f.y  THEN 'Yes' ELSE 'No' END) AS ev_f_y,
        (CASE WHEN ev_f.n > 3  THEN 'Yes' ELSE 'No' END) AS ev_f_n_big,
    CASE
WHEN ev_f.n  < 0 THEN '0'
WHEN ev_f.n  >= 0 AND ev_f.n  < 5 THEN '1'
WHEN ev_f.n  >= 5 AND ev_f.n  < 10 THEN '2'
WHEN ev_f.n  >= 10 THEN '3'
ELSE '4'
END AS ev_f_n_tier__sort_,
    CASE
WHEN ev_f.n  < 0 THEN 'Below 0'
WHEN ev_f.n  >= 0 AND ev_f.n  < 5 THEN '0 to 4'
WHEN ev_f.n  >= 5 AND ev_f.n  < 10 THEN '5 to 9'
WHEN ev_f.n  >= 10 THEN '10 or Above'
ELSE 'Undefined'
END AS ev_f_n_tier,
    CASE
WHEN ev_f.n  < 0.0 THEN 'T00 (-inf,0.0)'
WHEN ev_f.n  >= 0.0 AND ev_f.n  < 5.0 THEN 'T01 [0.0,5.0)'
WHEN ev_f.n  >= 5.0 AND ev_f.n  < 10.0 THEN 'T02 [5.0,10.0)'
WHEN ev_f.n  >= 10.0 THEN 'T03 [10.0,inf)'
ELSE 'TXX Undefined'
END AS ev_f_n_tier_classic,
    CASE
WHEN ev_f.s = 'apple'  THEN '0'
WHEN ev_f.n > 5  THEN '1'
ELSE '2'
END AS ev_f_s_case__sort_,
    CASE
WHEN ev_f.s = 'apple'  THEN 'Fruit A'
WHEN ev_f.n > 5  THEN 'Big'
ELSE 'Other'
END AS ev_f_s_case
FROM ev_f
ORDER BY
    1
LIMIT 500
