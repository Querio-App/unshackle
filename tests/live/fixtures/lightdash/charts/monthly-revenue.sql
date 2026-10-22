SELECT
  CAST(DATE_TRUNC('MONTH', "orders".purchased_at) AS DATE) AS "orders_purchased_at_month",
  SUM("orders".post_discount_revenue) AS "orders_total_revenue",
  COUNT(DISTINCT "orders".order_id) AS "orders_order_count"
FROM "dunder_mifflin"."analytics"."orders" AS "orders"

WHERE ((
  (("orders".purchased_at) >= ('2025-01-01 00:00:00+00:00') AND ("orders".purchased_at) <= ('2025-06-30 00:00:00+00:00'))
))
GROUP BY 1
ORDER BY "orders_purchased_at_month"
LIMIT 500
