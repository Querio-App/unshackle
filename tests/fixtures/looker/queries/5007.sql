SELECT
    orders.status  AS "orders.status",
    COUNT(DISTINCT orders.order_id ) AS "orders.order_count"
FROM analytics.fct_orders  AS orders
GROUP BY
    1
ORDER BY
    2 DESC
FETCH NEXT 10 ROWS ONLY
