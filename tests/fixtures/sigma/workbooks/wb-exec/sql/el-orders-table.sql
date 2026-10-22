select Q1."ORDER_ID" "Order Id", Q1."CREATED_AT" "Created At", Q1."AMOUNT_USD" "Amount", Q1."STATUS" "Status"
from ANALYTICS.PUBLIC.FCT_ORDERS Q1
order by 2 desc
limit 25000
