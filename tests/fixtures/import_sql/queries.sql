-- Synthetic warehouse query
SELECT customer_id, SUM(amount) AS revenue FROM orders GROUP BY customer_id;
