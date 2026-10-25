WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01' )
SELECT
    ev_orders.customer  AS ev_orders_customer,
    COUNT(*) AS ev_orders_count,
    COALESCE(SUM(ev_orders.shipping ), 0) AS ev_orders_total_shipping,
    AVG(ev_orders.shipping ) AS ev_orders_avg_shipping,
    ( COALESCE(SUM(ev_orders.shipping ), 0) )  AS ev_orders_shipping_pct,
    ( COALESCE(SUM(ev_orders.shipping ), 0) )  AS ev_orders_shipping_running,
    STRING_AGG(DISTINCT CAST(ev_orders.id  AS STRING), '|RECORD|') AS ev_orders_order_ids,
        (DATE(MIN(ev_orders.ordered) )) AS ev_orders_first_order,
    STRING_AGG(DISTINCT  ev_orders.customer  , ',' ORDER BY  ev_orders.customer  )  AS ev_orders_customers_string
FROM ev_orders
GROUP BY
    1
ORDER BY
    1
LIMIT 500

-- sql for creating the pivot row totals
WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01' )
SELECT
    ev_orders.customer  AS ev_orders_customer,
    COUNT(*) AS ev_orders_count,
    COALESCE(SUM(ev_orders.shipping ), 0) AS ev_orders_total_shipping,
    AVG(ev_orders.shipping ) AS ev_orders_avg_shipping,
    ( COALESCE(SUM(ev_orders.shipping ), 0) )  AS ev_orders_shipping_pct,
    ( COALESCE(SUM(ev_orders.shipping ), 0) )  AS ev_orders_shipping_running,
    STRING_AGG(DISTINCT CAST(ev_orders.id  AS STRING), '|RECORD|') AS ev_orders_order_ids,
        (DATE(MIN(ev_orders.ordered) )) AS ev_orders_first_order,
    STRING_AGG(DISTINCT  ev_orders.customer  , ',' ORDER BY  ev_orders.customer  )  AS ev_orders_customers_string
FROM ev_orders
GROUP BY
    1
ORDER BY
    1
LIMIT 30000