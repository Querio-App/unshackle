WITH totals AS (SELECT customer_id, SUM(amount) AS revenue FROM orders GROUP BY customer_id)
SELECT * FROM totals;
