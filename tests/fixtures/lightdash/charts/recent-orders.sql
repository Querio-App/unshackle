WITH metrics AS (
  SELECT
    "orders".status AS "orders_status",
    COUNT("orders".order_id) AS "orders_order_count",
    SUM("orders".amount) AS "orders_total_revenue"
  FROM "jaffle"."analytics"."orders" AS "orders"
  GROUP BY 1
)
SELECT
  *,
  "orders_total_revenue" / SUM("orders_total_revenue") OVER () AS "revenue_share"
FROM metrics
ORDER BY "orders_order_count" DESC
LIMIT 100
