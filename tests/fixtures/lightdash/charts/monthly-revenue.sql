SELECT
  DATE_TRUNC('MONTH', "orders".created_at) AS "orders_created_at_month",
  SUM("orders".amount) AS "orders_total_revenue"
FROM "jaffle"."analytics"."orders" AS "orders"
GROUP BY 1
ORDER BY "orders_created_at_month"
LIMIT 500
