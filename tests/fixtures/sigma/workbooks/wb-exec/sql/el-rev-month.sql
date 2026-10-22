with Q1 as (
  select date_trunc('month', "CREATED_AT") "Month of Created At", "REGION" "Region", "AMOUNT_USD"
  from ANALYTICS.PUBLIC.FCT_ORDERS
  where "CREATED_AT" >= '2025-10-01'
)
select Q1."Month of Created At", Q1."Region", sum(Q1."AMOUNT_USD") "Revenue"
from Q1
group by 1, 2
order by 1 asc
limit 25000
