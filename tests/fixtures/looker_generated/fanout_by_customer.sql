WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01' )
  ,  ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
      SELECT 12, 1, 6.0, 10.0 UNION ALL
      SELECT 13, 1, 1.0, 10.0 UNION ALL
      SELECT 21, 2, 9.0, 20.0 UNION ALL
      SELECT 31, 3, 2.5, 5.0 UNION ALL
      SELECT 32, 3, 2.5, 5.0 UNION ALL
      SELECT 51, 5, 8.0, 3.0 )
SELECT
    ev_orders.customer  AS ev_orders_customer,
    COUNT(DISTINCT ev_orders.id ) AS ev_orders_count,
    ROUND(COALESCE(CAST( ( SUM(DISTINCT (CAST(ROUND(COALESCE( ev_orders.shipping  ,0)*(1/1000*1.0), 9) AS NUMERIC) + (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001 )) - SUM(DISTINCT (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001) )  / (1/1000*1.0) AS FLOAT64), 0), 6) AS ev_orders_total_shipping,
    (ROUND(COALESCE(CAST( ( SUM(DISTINCT (CAST(ROUND(COALESCE( ev_orders.shipping  ,0)*(1/1000*1.0), 9) AS NUMERIC) + (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001 )) - SUM(DISTINCT (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_orders.id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001) )  / (1/1000*1.0) AS FLOAT64), 0), 6) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN   ev_orders.shipping   IS NOT NULL THEN  ev_orders.id   ELSE NULL END) AS FLOAT64), 0.0)) AS ev_orders_avg_shipping,
    COUNT(ev_items.item_id ) AS ev_items_count,
    COALESCE(SUM(ev_items.price ), 0) AS ev_items_total_price,
    ROUND(COALESCE(CAST( ( SUM(DISTINCT (CAST(ROUND(COALESCE( ev_items.order_shipping  ,0)*(1/1000*1.0), 9) AS NUMERIC) + (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001 )) - SUM(DISTINCT (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001) )  / (1/1000*1.0) AS FLOAT64), 0), 6) AS ev_items_shipping_sum_distinct,
    (ROUND(COALESCE(CAST( ( SUM(DISTINCT (CAST(ROUND(COALESCE( ev_items.order_shipping  ,0)*(1/1000*1.0), 9) AS NUMERIC) + (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001 )) - SUM(DISTINCT (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001) )  / (1/1000*1.0) AS FLOAT64), 0), 6) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN   ev_items.order_shipping   IS NOT NULL THEN  ev_items.order_id   ELSE NULL END) AS FLOAT64), 0.0)) AS ev_items_shipping_avg_distinct
FROM ev_orders
LEFT JOIN ev_items ON ev_orders.id = ev_items.order_id
GROUP BY
    1
ORDER BY
    1
LIMIT 500