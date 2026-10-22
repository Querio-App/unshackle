with Q1 as (
  select * from analytics.public.fct_orders
)
select Q1."REGION" "Region", sum(Q1."AMOUNT_USD") "Revenue (USD)"
from Q1
where Q1."REGION" in ('EMEA')
group by 1
order by 2 desc
limit 25000
