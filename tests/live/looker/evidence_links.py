"""Print the links that capture Looker's generated SQL and results for the evidence views (#41).

Paste tests/fixtures/looker_generated/evidence.view.lkml into a view file of a dev-mode LookML
project on BigQuery (a Cloud Skills Boost Looker lab is enough), add evidence.explores.lkml to the
model, then:

    python tests/live/looker/evidence_links.py https://<looker host> [case ...]   # one link per file
    python tests/live/looker/evidence_links.py --console [case ...] | pbcopy      # or all at once

Open each link while logged in (Development Mode on) and save the page as the name shown into
tests/fixtures/looker_generated/, or paste the --console snippet into the browser console on the
Looker tab: it downloads every case as one looker-evidence.json. Explore URLs ending in .json return the result rows and .sql
the SQL Looker generates. Never commit the host.
"""

import json
import sys
from urllib.parse import urlencode

TIME = ["raw", "time", "date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "month_num",
        "month_name", "quarter_of_year", "week_of_year", "day_of_month", "day_of_year", "hour_of_day"]
ORDERS = ["count", "total_shipping", "avg_shipping"]
F_FILTERS = ["s_starts", "s_ends", "s_contains", "s_not_starts", "s_eq_lower", "s_eq_upper", "s_list", "s_escaped_comma",
             "s_empty", "s_null", "n_closed", "n_open", "n_half_open", "n_to", "n_and", "n_or", "n_not", "n_minus", "n_list",
             "n_not_list", "n_null", "n_not_null", "n_gt", "d_day", "d_month", "d_year", "d_before", "d_after", "d_range",
             "d_null", "d_not_null", "d_last_7", "d_this_month", "d_last_month", "d_ago", "t_day", "t_before", "t_month",
             "tier_filtered"]
F_STATS = ["count", "n_median", "n_p25", "n_p90", "s_distinct", "n_sum", "n_avg", "n_min", "n_max_date", "n_per_row", "n_sum_big"]

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
        "params": {"f[ev_liquid.customer_filter]": "-acme", "f[ev_liquid.metric_choice]": "shipping"},
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
    "filters": {"view": "ev_f", "fields": ["ev_f.count", *(f"ev_f.{m}" for m in F_FILTERS)]},
    "dimension_types": {
        "view": "ev_f",
        "fields": ["ev_f.id", "ev_f.s", "ev_f.n", "ev_f.y", "ev_f.n_big", "ev_f.n_tier", "ev_f.n_tier_classic", "ev_f.s_case"],
        "sorts": ["ev_f.id"],
    },
    "stats_total": {"view": "ev_f", "fields": [f"ev_f.{m}" for m in F_STATS]},
    "stats_by_big": {"view": "ev_f", "fields": ["ev_f.n_big", *(f"ev_f.{m}" for m in F_STATS)], "sorts": ["ev_f.n_big"]},
    "time_new_york": {
        "view": "ev_time",
        "fields": ["ev_time.id", "ev_time.created_time", "ev_time.created_date", "ev_time.created_tz_time",
                   "ev_time.created_tz_date", "ev_time.created_tz_week", "ev_time.created_tz_month"],
        "sorts": ["ev_time.id"],
        "params": {"query_timezone": "America/New_York"},
    },
    # captured with week_start_day: sunday in the model
    "time_week_sunday": {
        "view": "ev_time",
        "fields": ["ev_time.id", "ev_time.created_week", "ev_time.created_day_of_week_index", "ev_time.created_week_of_year",
                   "ev_time.day_week"],
        "sorts": ["ev_time.id"],
    },
    "explore_filters": {"view": "ev_af", "fields": ["ev_af.id", "ev_af.s", "ev_af.count"], "sorts": ["ev_af.id"]},
}


# round 3: evidence3.*.lkml. Each case on its own, so one field Looker rejects doesn't hide the others.
L_FLAGS = ["ev_l.range_start", "ev_l.range_end", "ev_l.flag_filtered", "ev_l.flag_in_query", "ev_l.flag_selected",
           "ev_l.p_str_param", "ev_l.p_str_branch", "ev_l.p_num_param"]
CASES.update({
    "dialect": {"view": "ev_l", "fields": ["ev_l.id", "ev_l.dialect_name", "ev_l.dialect_branch"], "sorts": ["ev_l.id"]},
    "liquid_none": {"view": "ev_l", "fields": ["ev_l.id", *L_FLAGS], "sorts": ["ev_l.id"]},
    "liquid_values": {
        "view": "ev_l", "fields": ["ev_l.id", "ev_l.s", *L_FLAGS], "sorts": ["ev_l.id"],
        "filters": {"ev_l.s": "-zzz", "ev_l.date_f": "2026-12-01 to 2027-01-01", "ev_l.p_str": "O'Brien", "ev_l.p_num": "5"},
    },
    "date_start_month": {"view": "ev_l", "fields": ["ev_l.id", "ev_l.range_start", "ev_l.range_end"], "sorts": ["ev_l.id"],
                         "filters": {"ev_l.date_f": "2026-12"}},
    "date_start_relative": {"view": "ev_l", "fields": ["ev_l.id", "ev_l.range_start", "ev_l.range_end"], "sorts": ["ev_l.id"],
                            "filters": {"ev_l.date_f": "7 days"}},
    "parameter_value_string": {"view": "ev_l", "fields": ["ev_l.id", "ev_l.p_str_value"], "sorts": ["ev_l.id"]},
    "filter_field_sql": {"view": "ev_l", "fields": ["ev_l.count"], "filters": {"ev_l.f_sql": "-apple"}},
    "filter_quoted": {"view": "ev_l", "fields": ["ev_l.count"], "filters": {"ev_l.s": '"Levi\'s"'}},
    "filter_apostrophe": {"view": "ev_l", "fields": ["ev_l.count"], "filters": {"ev_l.s": "Levi's,O'Brien"}},
    "hour_timeframes": {"view": "ev_l", "fields": ["ev_l.id", "ev_l.at_time", "ev_l.at_hour", "ev_l.at_minute", "ev_l.at_time_of_day",
                                                   "ev_l.at_hour_of_day", "ev_l.at_minute_of_hour", "ev_l.in_range"], "sorts": ["ev_l.id"]},
})


def links(model: str, only: list[str]) -> dict[str, str]:
    out = {}
    for name, case in CASES.items():
        if only and name not in only:
            continue
        q = {"fields": ",".join(case["fields"]), "limit": "500"}
        if case.get("sorts"):
            q["sorts"] = ",".join(case["sorts"])
        q.update(case.get("params", {}))
        for ext in (".json", ".sql"):
            out[name + ext] = f"/explore/{model}/{case['view']}{ext}?{urlencode(q)}&apply_formatting=false"
    return out


def console(model: str, only: list[str]) -> str:
    """One snippet for the browser console on the Looker tab: fetches every case with the logged-in
    session and downloads looker-evidence.json ({"<case>.json": "...", "<case>.sql": "..."})."""
    return (
        f"(async () => {{ const urls = {json.dumps(links(model, only))}; const out = {{}};\n"
        "for (const [name, url] of Object.entries(urls)) { const r = await fetch(url); out[name] = await r.text(); }\n"
        "const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(out)]));\n"
        "a.download = 'looker-evidence.json'; a.click(); })();"
    )


if __name__ == "__main__":
    if sys.argv[1] == "--console":
        print(console("training_ecommerce", sys.argv[2:]))
    else:
        for name, url in links("training_ecommerce", sys.argv[2:]).items():
            print(f"{name}  {sys.argv[1].rstrip('/')}{url}")
