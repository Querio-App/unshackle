SELECT
  "customers".region AS "customers_region",
  "orders".status AS "orders_status",
  SUM("orders".amount) AS "orders_total_revenue"
FROM "jaffle"."analytics"."orders" AS "orders"
LEFT OUTER JOIN "jaffle"."analytics"."customers" AS "customers"
  ON ("orders".customer_id) = ("customers".customer_id)
WHERE (("orders".status) NOT IN ('returned') OR ("orders".status) IS NULL)
GROUP BY 1, 2
ORDER BY "orders_total_revenue" DESC
LIMIT 500
