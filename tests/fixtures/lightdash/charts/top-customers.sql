SELECT
  "orders".customer_id AS "orders_customer_id",
  SUM("orders".amount) AS "orders_total_revenue"
FROM "jaffle"."analytics"."orders" AS "orders"
GROUP BY 1
ORDER BY "orders_total_revenue" DESC
LIMIT 10
