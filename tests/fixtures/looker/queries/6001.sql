SELECT
    (TO_CHAR(DATE_TRUNC('week', orders.created_at ), 'YYYY-MM-DD')) AS "orders.created_week",
    orders.status  AS "orders.status",
    COUNT(DISTINCT orders.order_id ) AS "orders.order_count"
FROM analytics.fct_orders  AS orders
WHERE ((( orders.created_at  ) >= ((DATEADD('week', -7, DATE_TRUNC('week', DATE_TRUNC('day', CURRENT_TIMESTAMP())))))))
GROUP BY
    1,
    2
ORDER BY
    1 DESC
FETCH NEXT 500 ROWS ONLY
