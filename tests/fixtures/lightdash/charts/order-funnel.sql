SELECT
  "orders".status AS "orders_status",
  COUNT("orders".order_id) AS "orders_order_count"
FROM "jaffle"."analytics"."orders" AS "orders"
GROUP BY 1
LIMIT 10
