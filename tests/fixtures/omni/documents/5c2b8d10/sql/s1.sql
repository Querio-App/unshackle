SELECT order_id, SUM(sale_price) AS refunded
FROM ecomm.order_items
WHERE status = 'Returned'
GROUP BY 1
ORDER BY 2 DESC
