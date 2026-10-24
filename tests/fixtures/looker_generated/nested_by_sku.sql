WITH ev_nested AS (SELECT 1 AS id, [STRUCT('x' AS sku, 2 AS qty), STRUCT('y' AS sku, 3 AS qty)] AS lines UNION ALL
      SELECT 2, ARRAY<STRUCT<sku STRING, qty INT64>>[] UNION ALL
      SELECT 3, [STRUCT('x' AS sku, 1 AS qty)] )
SELECT
    ev_nested_lines.sku  AS ev_nested_lines_sku,
    COUNT(DISTINCT ev_nested.id ) AS ev_nested_count,
    COALESCE(SUM(ev_nested_lines.qty ), 0) AS ev_nested_lines_total_qty
FROM ev_nested
LEFT JOIN UNNEST(ev_nested.lines) AS ev_nested_lines
GROUP BY
    1
ORDER BY
    1
LIMIT 500