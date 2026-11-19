# Dunder Mifflin team exploration

Everything here is fictional. Two datasets coexist: never union their revenue.

## Sales scenario (2025)

`orders` has one row per single-product order, 216 completed orders and 24 excluded
orders. Recognized revenue is SUM(net_usd) for status='completed' only. Returned
and cancelled orders contribute zero. All money is USD; net_usd is after discount.
Cost is a synthetic 60% of net; recognized profit is net_usd-cost_usd on completed
orders. Gross margin is profit/revenue, with a zero denominator guarded by NULLIF.
There are no taxes, shipping charges or FX in scenario revenue.

Relationships (many-to-one): orders.customer_id -> customers.customer_id;
customers.employee_id -> employees.employee_id; employees.branch_id -> branches.branch_id;
orders.product_id -> products.product_id. Each customer has one assigned rep, so
joining those dimensions preserves the order grain. Group on ordered_at for calendar
months. All six employees are sales reps; branches are Scranton, Stamford, Nashua.

Each rep r=1..6 places an order for each of three products every month m=1..12,
quantity r*m. Scenario product prices are $10/$20/$30; even-numbered reps receive
10% discount. Odd months use the rep's first customer; even months the second.
Two extra orders per month are excluded: returned $1,000 and cancelled $2,000.
Products reuse three sorted IDs/names from the historical product asset, but scenario
prices intentionally differ. cost_usd is scenario allocated cost, not quantity times
product unit_cost after discount. Product unit_cost is the pre-discount reference cost.

## Historical dataset (2024-01 through 2026-10)

All original rows from tests/data/dunder_mifflin are retained under history_*:
181 accounts, 15 products, 3,503 orders, 2,812 shippings, 1,655 reviews. Contact emails
are replaced with example.com. The historic accounts' account_manager is a fictional
label, not a foreign key to scenario employees. Scenario and history are separate.

Relationships: history_orders.account_id -> history_accounts.account_id;
history_orders.product_id -> history_products.product_id;
history_orders.shipping_id -> history_shippings.shipping_id (nullable; LEFT JOIN);
history_reviews.order_id -> history_orders.order_id (one-to-many; aggregate reviews
before joining revenue). Use purchased_at, post_discount_revenue, and status='COMPLETED'
for completed historical sales. Do not infer history prices from current product price:
the saved order revenue is authoritative. Missing discount or shipping cost is unknown,
not automatically zero. Shipping geography describes destination, not scenario branch.

Explore seasonality, rep and branch performance, discount impact, customer concentration,
product mix, shipping delays and review patterns. The eight acceptance questions and
independent arithmetic are in the source example; expected answers are not runtime context.
