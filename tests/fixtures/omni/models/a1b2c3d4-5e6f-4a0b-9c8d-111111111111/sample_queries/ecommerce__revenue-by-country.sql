SELECT "users"."COUNTRY" AS "users.country",
  SUM("order_items"."SALE_PRICE") AS "order_items.total_revenue"
FROM "ecomm"."order_items" AS "order_items"
LEFT JOIN "ecomm"."users" AS "users" ON "order_items"."USER_ID" = "users"."ID"
WHERE "order_items"."CREATED_AT" >= DATEADD(MONTH, -1, DATE_TRUNC('MONTH', CURRENT_DATE()))
  AND "order_items"."CREATED_AT" < DATE_TRUNC('MONTH', CURRENT_DATE())
GROUP BY 1
ORDER BY 2 DESC NULLS LAST
LIMIT 50
