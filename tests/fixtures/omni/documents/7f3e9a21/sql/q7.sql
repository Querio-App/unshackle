SELECT "order_items"."STATUS" AS "order_items.status",
  COUNT(DISTINCT "order_items"."ID") AS "order_items.count"
FROM "ecomm"."order_items" AS "order_items"
WHERE "order_items"."STATUS" = 'Returned'
GROUP BY 1
