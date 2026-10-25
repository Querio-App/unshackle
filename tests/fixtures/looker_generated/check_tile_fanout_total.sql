WITH chk_tile_fanout_total AS (WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
          SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
          SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
          SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
          SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01'), ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
          SELECT 12, 1, 6.0, 10.0 UNION ALL
          SELECT 13, 1, 1.0, 10.0 UNION ALL
          SELECT 21, 2, 9.0, 20.0 UNION ALL
          SELECT 31, 3, 2.5, 5.0 UNION ALL
          SELECT 32, 3, 2.5, 5.0 UNION ALL
          SELECT 51, 5, 8.0, 3.0)
    SELECT
        COUNT(DISTINCT ev_orders.id) AS ev_orders_count,
        CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_orders.shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) AS ev_orders_total_shipping,
        CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_orders.shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN ev_orders.shipping IS NOT NULL THEN ev_orders.id END) AS FLOAT64), 0) AS ev_orders_avg_shipping,
        COUNT(DISTINCT ev_items.item_id) AS ev_items_count,
        COALESCE(SUM(ev_items.price), 0) AS ev_items_total_price
    FROM ev_orders AS ev_orders
    LEFT JOIN ev_items AS ev_items ON ev_orders.id = ev_items.order_id
    ORDER BY ev_orders_count DESC )
SELECT
    chk_tile_fanout_total.ev_orders_count  AS chk_tile_fanout_total_ev_orders_count,
    chk_tile_fanout_total.ev_orders_total_shipping  AS chk_tile_fanout_total_ev_orders_total_shipping,
    chk_tile_fanout_total.ev_orders_avg_shipping  AS chk_tile_fanout_total_ev_orders_avg_shipping,
    chk_tile_fanout_total.ev_items_count  AS chk_tile_fanout_total_ev_items_count,
    chk_tile_fanout_total.ev_items_total_price  AS chk_tile_fanout_total_ev_items_total_price
FROM chk_tile_fanout_total
GROUP BY
    1,
    2,
    3,
    4,
    5
ORDER BY
    1
LIMIT 500
