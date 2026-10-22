SELECT
  SUM("orders".amount) AS "orders_total_revenue"
FROM "jaffle"."analytics"."orders" AS "orders"
LIMIT 1
