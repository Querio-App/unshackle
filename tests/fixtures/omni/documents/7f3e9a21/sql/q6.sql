SELECT DATE_TRUNC('WEEK', "order_items"."CREATED_AT") AS "order_items.created_at[week]",
  SUM("order_items"."SALE_PRICE") AS "order_items.total_revenue"
FROM "ecomm"."order_items" AS "order_items"
GROUP BY 1
ORDER BY 1 ASC NULLS LAST
LIMIT 500
