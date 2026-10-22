SELECT
    customers.full_name  AS "customers.name",
    COUNT(DISTINCT orders.order_id ) AS "orders.order_count",
    COALESCE(SUM(orders.amount_usd ), 0) AS "orders.total_revenue"
FROM analytics.fct_orders  AS orders
LEFT JOIN analytics.dim_customers  AS customers ON orders.customer_id = customers.customer_id
WHERE (orders.status <> 'returned' OR orders.status IS NULL)
GROUP BY
    1
ORDER BY
    3 DESC
FETCH NEXT 25 ROWS ONLY
