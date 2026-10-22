SELECT
  DATE_TRUNC('month', "public"."orders"."purchased_at") AS "purchased_at",
  SUM("public"."orders"."post_discount_revenue") AS "sum",
  COUNT(*) AS "count"
FROM
  "public"."orders"
GROUP BY
  DATE_TRUNC('month', "public"."orders"."purchased_at")
ORDER BY
  DATE_TRUNC('month', "public"."orders"."purchased_at") ASC
