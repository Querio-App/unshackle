SELECT SUM(post_discount_revenue) / NULLIF(COUNT(*), 0) AS average_order_value 
FROM public.orders 
WHERE status IN ('unshackle__status') 
 LIMIT 10000;
