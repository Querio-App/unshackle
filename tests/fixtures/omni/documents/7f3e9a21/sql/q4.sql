SELECT "users"."EMAIL" AS "users.email",
  SUM("order_items"."SALE_PRICE") AS "order_items.total_revenue",
  COUNT(DISTINCT "order_items"."ID") AS "order_items.count"
FROM "ecomm"."order_items" AS "order_items"
LEFT JOIN "ecomm"."users" AS "users" ON "order_items"."USER_ID" = "users"."ID"
GROUP BY 1
ORDER BY 2 DESC NULLS LAST
LIMIT 20
