WITH chk_tile_nested_total AS (WITH ev_nested AS (SELECT 1 AS id, [STRUCT('x' AS sku, 2 AS qty), STRUCT('y' AS sku, 3 AS qty)] AS lines UNION ALL
          SELECT 2, ARRAY<STRUCT<sku STRING, qty INT64>>[] UNION ALL
          SELECT 3, [STRUCT('x' AS sku, 1 AS qty)]), ev_nested_lines AS (SELECT ev_nested.id AS ev_nested_key, ev_nested_lines.* FROM ev_nested AS ev_nested LEFT JOIN UNNEST(ev_nested.lines) AS ev_nested_lines)
    SELECT
        COUNT(DISTINCT ev_nested.id) AS ev_nested_count,
        COUNT(*) AS ev_nested_lines_count,
        COALESCE(SUM(ev_nested_lines.qty), 0) AS ev_nested_lines_total_qty
    FROM ev_nested AS ev_nested
    LEFT JOIN ev_nested_lines AS ev_nested_lines ON ev_nested.id = ev_nested_lines.ev_nested_key
    ORDER BY ev_nested_count DESC )
SELECT
    chk_tile_nested_total.ev_nested_count  AS chk_tile_nested_total_ev_nested_count,
    chk_tile_nested_total.ev_nested_lines_count  AS chk_tile_nested_total_ev_nested_lines_count,
    chk_tile_nested_total.ev_nested_lines_total_qty  AS chk_tile_nested_total_ev_nested_lines_total_qty
FROM chk_tile_nested_total
GROUP BY
    1,
    2,
    3
ORDER BY
    1
LIMIT 500
