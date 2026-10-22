SELECT
  CAST(
    SUM("public"."orders"."pre_discount_revenue") - SUM("public"."orders"."post_discount_revenue") AS DOUBLE PRECISION
  ) / NULLIF(
    CAST(
      SUM("public"."orders"."pre_discount_revenue") AS DOUBLE PRECISION
    ),
    0.0
  ) AS "discount_rate"
FROM
  "public"."orders"
