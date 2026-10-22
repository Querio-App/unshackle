SELECT SUM("order_items"."SALE_PRICE") AS "order_items.total_revenue"
FROM "ecomm"."order_items" AS "order_items"
LIMIT 1
