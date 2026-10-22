SELECT
  "shippings".region AS "shippings_region",
  SUM(CASE WHEN (("orders".status) IN ('COMPLETED')) THEN ("orders".post_discount_revenue) ELSE NULL END) AS "orders_completed_revenue",
  (SUM("orders".post_discount_revenue)) / NULLIF((COUNT(DISTINCT "orders".order_id)), 0) AS "orders_average_order_value",
  COUNT(DISTINCT "orders".order_id) AS "orders_order_count",
  SUM("orders".post_discount_revenue) AS "orders_total_revenue"
FROM "dunder_mifflin"."analytics"."orders" AS "orders"
LEFT OUTER JOIN "dunder_mifflin"."analytics"."shippings" AS "shippings"
  ON ("orders".order_id) = ("shippings".order_id)
GROUP BY 1
ORDER BY "orders_completed_revenue" DESC
LIMIT 500
