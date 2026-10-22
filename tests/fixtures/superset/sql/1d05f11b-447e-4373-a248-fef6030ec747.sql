SELECT region AS region, mode AS mode, SUM(post_discount_revenue) AS shipped_revenue 
FROM (SELECT o.id, o.status, o.post_discount_revenue, o.purchased_at, s.region, s.mode
FROM orders o
JOIN shippings s ON s.order_id = o.id
) AS virtual_table GROUP BY region, mode ORDER BY shipped_revenue DESC 
 LIMIT 10000;
