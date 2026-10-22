with Q1 as (
  select date_trunc('day', "CREATED_AT") "Day of Created At", sum("AMOUNT_USD") "S"
  from ANALYTICS.PUBLIC.FCT_ORDERS
  group by 1
)
select "Day of Created At",
  avg("S") over (order by "Day of Created At" rows between 6 preceding and current row) "Moving Revenue"
from Q1
order by 1
