"""Print the links that capture Looker's generated SQL and results for the evidence views (#41).

Paste tests/fixtures/looker_generated/evidence.view.lkml into a view file of a dev-mode LookML
project on BigQuery (a Cloud Skills Boost Looker lab is enough), add evidence.explores.lkml to the
model, then:

    python tests/live/looker/evidence_links.py https://<looker host> [model]

Open each link while logged in (Development Mode on) and save the page as the name shown into
tests/fixtures/looker_generated/. Explore URLs ending in .json return the result rows and .sql
the SQL Looker generates. Never commit the host.
"""

import sys
from urllib.parse import urlencode

TIME = ["raw", "time", "date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "month_num",
        "month_name", "quarter_of_year", "week_of_year", "day_of_month", "day_of_year", "hour_of_day"]
ORDERS = ["count", "total_shipping", "avg_shipping"]

CASES: dict[str, dict] = {
    "time": {
        "view": "ev_time",
        "fields": ["ev_time.id", *(f"ev_time.created_{t}" for t in TIME),
                   *(f"ev_time.created_tz_{t}" for t in ("time", "date", "week", "month")),
                   *(f"ev_time.day_{t}" for t in ("date", "week", "month", "quarter", "year", "day_of_week"))],
        "sorts": ["ev_time.id"],
    },
    "measure_types": {
        "view": "ev_orders",
        "fields": ["ev_orders.customer", *(f"ev_orders.{m}" for m in ORDERS), "ev_orders.shipping_pct",
                   "ev_orders.shipping_running", "ev_orders.order_ids", "ev_orders.first_order", "ev_orders.customers_string"],
        "sorts": ["ev_orders.customer"],
    },
    "fanout_by_customer": {
        "view": "ev_orders",
        "fields": ["ev_orders.customer", *(f"ev_orders.{m}" for m in ORDERS), "ev_items.count", "ev_items.total_price",
                   "ev_items.shipping_sum_distinct", "ev_items.shipping_avg_distinct"],
        "sorts": ["ev_orders.customer"],
    },
    "fanout_total": {
        "view": "ev_orders",
        "fields": [*(f"ev_orders.{m}" for m in ORDERS), "ev_items.count", "ev_items.total_price"],
    },
    "distinct_total": {
        "view": "ev_items",
        "fields": ["ev_items.count", "ev_items.total_price", "ev_items.shipping_sum_distinct",
                   "ev_items.shipping_avg_distinct", "ev_items.shipping_naive_sum"],
    },
    "liquid_default": {
        "view": "ev_liquid",
        "fields": ["ev_liquid.customer", "ev_liquid.count", "ev_liquid.chosen_total", "ev_liquid.chosen_label", "ev_liquid.user_locale"],
        "sorts": ["ev_liquid.customer"],
    },
    "liquid_set": {
        "view": "ev_liquid",
        "fields": ["ev_liquid.customer", "ev_liquid.count", "ev_liquid.chosen_total", "ev_liquid.chosen_label"],
        "filters": {"ev_liquid.customer_filter": "-acme", "ev_liquid.metric_choice": "shipping"},
        "sorts": ["ev_liquid.customer"],
    },
    "nested_by_id": {
        "view": "ev_nested",
        "fields": ["ev_nested.id", "ev_nested.count", "ev_nested_lines.count", "ev_nested_lines.total_qty"],
        "sorts": ["ev_nested.id"],
    },
    "nested_by_sku": {
        "view": "ev_nested",
        "fields": ["ev_nested_lines.sku", "ev_nested.count", "ev_nested_lines.total_qty"],
        "sorts": ["ev_nested_lines.sku"],
    },
    "nested_total": {
        "view": "ev_nested",
        "fields": ["ev_nested.count", "ev_nested_lines.count", "ev_nested_lines.total_qty"],
    },
}


def main(host: str, model: str) -> None:
    for name, case in CASES.items():
        q = {"fields": ",".join(case["fields"]), "limit": "500"}
        if case.get("sorts"):
            q["sorts"] = ",".join(case["sorts"])
        q.update({f"f[{k}]": v for k, v in case.get("filters", {}).items()})
        for ext in (".json", ".sql"):
            print(f"{name}{ext}  {host.rstrip('/')}/explore/{model}/{case['view']}{ext}?{urlencode(q)}&apply_formatting=false")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "training_ecommerce")
