SELECT SUM(post_discount_revenue) / NULLIF(COUNT(*), 0) AS average_order_value 
FROM public.orders 
 LIMIT 10000;
