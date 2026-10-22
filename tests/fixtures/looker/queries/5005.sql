SELECT
    customers.region  AS "customers.region",
    COUNT(DISTINCT customers.customer_id ) AS "customers.count"
FROM analytics.dim_customers  AS customers
WHERE (customers.is_test_account = false)
GROUP BY
    1
ORDER BY
    2 DESC
FETCH NEXT 500 ROWS ONLY
