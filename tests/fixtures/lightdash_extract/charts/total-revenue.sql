SELECT
  SUM("orders".post_discount_revenue) AS "orders_total_revenue"
FROM "dunder_mifflin"."analytics"."orders" AS "orders"

LIMIT 1
