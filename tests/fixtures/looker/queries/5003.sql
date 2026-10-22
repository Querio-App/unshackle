SELECT
    COALESCE(SUM(orders.amount_usd ), 0) AS "orders.total_revenue"
FROM analytics.fct_orders  AS orders
WHERE (( orders.created_at  ) >= ((DATEADD('month', -11, DATE_TRUNC('month', DATE_TRUNC('day', CURRENT_TIMESTAMP()))))))
FETCH NEXT 1 ROWS ONLY
