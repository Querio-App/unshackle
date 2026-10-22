SELECT "order_items"."STATUS" AS "order_items.status",
  SUM("order_items"."SALE_PRICE" - "order_items"."COST") AS "order_items.total_margin"
FROM "ecomm"."order_items" AS "order_items"
GROUP BY 1
LIMIT 100
