SELECT
  "accounts__via__account_id"."account_manager" AS "accounts__via__account_id__account_manager",
  SUM("public"."orders"."post_discount_revenue") AS "sum",
  COUNT(*) AS "count"
FROM
  "public"."orders"
  LEFT JOIN (
    SELECT
      "public"."accounts"."id" AS "id",
      "public"."accounts"."name" AS "name",
      "public"."accounts"."industry" AS "industry",
      "public"."accounts"."business_size" AS "business_size",
      "public"."accounts"."payment_cycle" AS "payment_cycle",
      "public"."accounts"."account_manager" AS "account_manager",
      "public"."accounts"."created_at" AS "created_at"
    FROM
      "public"."accounts"
  ) AS "accounts__via__account_id" ON "public"."orders"."account_id" = "accounts__via__account_id"."id"
WHERE
  (
    ("public"."orders"."status" <> 'CANCELLED')
    OR ("public"."orders"."status" IS NULL)
  )
  AND ("public"."orders"."status" = 'unshackle__status')
GROUP BY
  "accounts__via__account_id"."account_manager"
ORDER BY
  "sum" DESC,
  "accounts__via__account_id"."account_manager" ASC
