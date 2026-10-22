SELECT status AS status, COUNT(*) AS order_count, SUM(post_discount_revenue) AS revenue, sum(quantity) AS "Units" 
FROM public.orders 
WHERE status IN ('unshackle__status') AND ((quantity > 0)) GROUP BY status ORDER BY order_count DESC 
 LIMIT 1000;
