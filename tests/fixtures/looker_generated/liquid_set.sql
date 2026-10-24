WITH ev_liquid AS (SELECT customer, shipping, amount FROM (
        SELECT 'acme' AS customer, 10.0 AS shipping, 100.0 AS amount UNION ALL
        SELECT 'acme', 20.0, 50.0 UNION ALL
        SELECT 'bolt', 5.0, 70.0 UNION ALL
        SELECT CAST(NULL AS STRING), 3.0, 30.0
      )
      WHERE (customer <> 'acme' OR customer IS NULL) )
SELECT
    ev_liquid.customer  AS ev_liquid_customer,
    COUNT(*) AS ev_liquid_count,
    COALESCE(SUM(ev_liquid.shipping ), 0) AS ev_liquid_chosen_total,
     'shipping'   AS ev_liquid_chosen_label
FROM ev_liquid
GROUP BY
    1
ORDER BY
    1
LIMIT 500