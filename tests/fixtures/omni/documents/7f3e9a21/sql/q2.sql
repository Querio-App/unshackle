SELECT "users"."COUNTRY" AS "users.country",
  "order_items"."STATUS" AS "order_items.status",
  SUM("order_items"."SALE_PRICE") AS "order_items.total_revenue"
FROM "ecomm"."order_items" AS "order_items"
LEFT JOIN "ecomm"."users" AS "users" ON "order_items"."USER_ID" = "users"."ID"
WHERE ("order_items"."STATUS" NOT IN ('Cancelled') OR "order_items"."STATUS" IS NULL)
GROUP BY 1, 2
LIMIT 500
