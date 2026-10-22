-- raw sql results do not include filled-in values for 'orders.created_month'
SELECT
    customers.region  AS "customers.region",
    (TO_CHAR(DATE_TRUNC('month', orders.created_at ), 'YYYY-MM')) AS "orders.created_month",
    COALESCE(SUM(orders.amount_usd ), 0) AS "orders.total_revenue"
FROM analytics.fct_orders  AS orders
LEFT JOIN analytics.dim_customers  AS customers ON orders.customer_id = customers.customer_id
WHERE ((( orders.created_at  ) >= ((DATEADD('month', -11, DATE_TRUNC('month', DATE_TRUNC('day', CURRENT_TIMESTAMP()))))) AND ( orders.created_at  ) < ((DATEADD('month', 12, DATEADD('month', -11, DATE_TRUNC('month', DATE_TRUNC('day', CURRENT_TIMESTAMP())))))))) AND (customers.region = 'EU')
GROUP BY
    1,
    (TO_CHAR(DATE_TRUNC('month', orders.created_at ), 'YYYY-MM'))
ORDER BY
    2 DESC,
    1
FETCH NEXT 500 ROWS ONLY
