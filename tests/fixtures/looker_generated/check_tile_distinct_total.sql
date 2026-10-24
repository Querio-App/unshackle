WITH chk_tile_distinct_total AS (WITH ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
          SELECT 12, 1, 6.0, 10.0 UNION ALL
          SELECT 13, 1, 1.0, 10.0 UNION ALL
          SELECT 21, 2, 9.0, 20.0 UNION ALL
          SELECT 31, 3, 2.5, 5.0 UNION ALL
          SELECT 32, 3, 2.5, 5.0 UNION ALL
          SELECT 51, 5, 8.0, 3.0)
    SELECT
        COUNT(DISTINCT ev_items.item_id) AS ev_items_count,
        COALESCE(SUM(ev_items.price), 0) AS ev_items_total_price,
        CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_items.order_shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) AS ev_items_shipping_sum_distinct,
        CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_items.order_shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN ev_items.order_shipping IS NOT NULL THEN ev_items.order_id END) AS FLOAT64), 0) AS ev_items_shipping_avg_distinct,
        COALESCE(SUM(ev_items.order_shipping), 0) AS ev_items_shipping_naive_sum
    FROM ev_items AS ev_items
    ORDER BY ev_items_count DESC )
SELECT
    chk_tile_distinct_total.ev_items_count  AS chk_tile_distinct_total_ev_items_count,
    chk_tile_distinct_total.ev_items_total_price  AS chk_tile_distinct_total_ev_items_total_price,
    chk_tile_distinct_total.ev_items_shipping_sum_distinct  AS chk_tile_distinct_total_ev_items_shipping_sum_distinct,
    chk_tile_distinct_total.ev_items_shipping_avg_distinct  AS chk_tile_distinct_total_ev_items_shipping_avg_distinct,
    chk_tile_distinct_total.ev_items_shipping_naive_sum  AS chk_tile_distinct_total_ev_items_shipping_naive_sum
FROM chk_tile_distinct_total
GROUP BY
    1,
    2,
    3,
    4,
    5
ORDER BY
    1
LIMIT 500
