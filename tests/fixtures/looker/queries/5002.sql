SELECT
    COALESCE(orders.channel, 'web')  AS "orders.channel",
    COALESCE(SUM(orders.amount_usd ), 0) AS "orders.total_revenue",
    COALESCE(SUM(CASE WHEN (orders.status = 'complete') THEN orders.amount_usd  ELSE NULL END), 0) AS "orders.completed_revenue"
FROM analytics.fct_orders  AS orders
GROUP BY
    1
ORDER BY
    2 DESC
FETCH NEXT 50 ROWS ONLY
