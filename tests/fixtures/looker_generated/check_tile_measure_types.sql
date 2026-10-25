WITH chk_tile_measure_types AS (WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
          SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
          SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
          SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
          SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01')
    SELECT
        ev_orders.customer AS ev_orders_customer,
        COALESCE(SUM(ev_orders.shipping), 0) AS ev_orders_total_shipping,
        100.0 * COALESCE(SUM(ev_orders.shipping), 0) / NULLIF(SUM(COALESCE(SUM(ev_orders.shipping), 0)) OVER (), 0) AS ev_orders_shipping_pct,
        SUM(COALESCE(SUM(ev_orders.shipping), 0)) OVER (ORDER BY ev_orders.customer ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ev_orders_shipping_running
    FROM ev_orders AS ev_orders
    GROUP BY 1
    ORDER BY ev_orders_customer )
SELECT
    chk_tile_measure_types.ev_orders_customer  AS chk_tile_measure_types_ev_orders_customer,
    chk_tile_measure_types.ev_orders_total_shipping  AS chk_tile_measure_types_ev_orders_total_shipping,
    chk_tile_measure_types.ev_orders_shipping_pct  AS chk_tile_measure_types_ev_orders_shipping_pct,
    chk_tile_measure_types.ev_orders_shipping_running  AS chk_tile_measure_types_ev_orders_shipping_running
FROM chk_tile_measure_types
GROUP BY
    1,
    2,
    3,
    4
ORDER BY
    1
LIMIT 500
