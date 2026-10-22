SELECT
  COUNT(*) AS "count"
FROM
  "public"."orders"
WHERE
  "public"."orders"."status" = 'COMPLETED'
