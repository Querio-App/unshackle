select Q1."STATUS" "Status", sum(Q1."AMOUNT_USD") "Revenue"
from ANALYTICS.PUBLIC.FCT_ORDERS Q1
group by 1
limit 25000
