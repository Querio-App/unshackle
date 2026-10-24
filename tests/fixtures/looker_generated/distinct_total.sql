WITH ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
      SELECT 12, 1, 6.0, 10.0 UNION ALL
      SELECT 13, 1, 1.0, 10.0 UNION ALL
      SELECT 21, 2, 9.0, 20.0 UNION ALL
      SELECT 31, 3, 2.5, 5.0 UNION ALL
      SELECT 32, 3, 2.5, 5.0 UNION ALL
      SELECT 51, 5, 8.0, 3.0 )
SELECT
    COUNT(*) AS ev_items_count,
    COALESCE(SUM(ev_items.price ), 0) AS ev_items_total_price,
    ROUND(COALESCE(CAST( ( SUM(DISTINCT (CAST(ROUND(COALESCE( ev_items.order_shipping  ,0)*(1/1000*1.0), 9) AS NUMERIC) + (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001 )) - SUM(DISTINCT (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001) )  / (1/1000*1.0) AS FLOAT64), 0), 6) AS ev_items_shipping_sum_distinct,
    (ROUND(COALESCE(CAST( ( SUM(DISTINCT (CAST(ROUND(COALESCE( ev_items.order_shipping  ,0)*(1/1000*1.0), 9) AS NUMERIC) + (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001 )) - SUM(DISTINCT (cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 1, 15)) as int64) as numeric) * 4294967296 + cast(cast(concat('0x', substr(to_hex(md5(CAST( ev_items.order_id   AS STRING))), 16, 8)) as int64) as numeric)) * 0.000000001) )  / (1/1000*1.0) AS FLOAT64), 0), 6) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN   ev_items.order_shipping   IS NOT NULL THEN  ev_items.order_id   ELSE NULL END) AS FLOAT64), 0.0)) AS ev_items_shipping_avg_distinct,
    COALESCE(SUM(ev_items.order_shipping ), 0) AS ev_items_shipping_naive_sum
FROM ev_items
LIMIT 500