select sum(Q1."AMOUNT_USD") / nullif(count(distinct Q1."ORDER_ID"), 0) "AOV"
from ANALYTICS.PUBLIC.FCT_ORDERS Q1
