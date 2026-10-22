SELECT
  CAST(DATE_TRUNC('MONTH', "reviews".reviewed_at) AS DATE) AS "reviews_reviewed_at_month",
  AVG("reviews".rating::DOUBLE PRECISION) AS "reviews_average_rating",
  COUNT(DISTINCT "reviews".review_id) AS "reviews_review_count"
FROM "dunder_mifflin"."analytics"."reviews" AS "reviews"

GROUP BY 1
ORDER BY "reviews_reviewed_at_month"
LIMIT 500
