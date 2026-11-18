SELECT c.region, SUM(o.amount) AS revenue
FROM orders AS o JOIN customers AS c USING (customer_id)
WHERE o.status = 'paid'
GROUP BY c.region ORDER BY c.region;
