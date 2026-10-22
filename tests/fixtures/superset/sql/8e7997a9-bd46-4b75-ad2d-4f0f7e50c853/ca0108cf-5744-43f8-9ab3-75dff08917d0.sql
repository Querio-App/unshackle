SELECT DATE_TRUNC('month', purchased_at) AS purchased_at, SUM(post_discount_revenue) AS revenue 
FROM public.orders 
WHERE status IN ('unshackle__status') GROUP BY DATE_TRUNC('month', purchased_at) ORDER BY revenue DESC 
 LIMIT 10000;
