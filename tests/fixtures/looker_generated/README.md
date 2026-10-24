# Looker's own generated SQL

SQL and result rows that Looker generated for `evidence.view.lkml` / `evidence.explores.lkml`
on a BigQuery connection (Looker in a Cloud Skills Boost lab, Development Mode, connection in
UTC). Every view reads literal rows, so `tests/test_looker_generated.py` loads the same rows into
DuckDB and checks unshackle's translation against these numbers.

| Case | Explore | What it settles |
| --- | --- | --- |
| `time` | `ev_time` | timeframe formats: `week`, `month`, `quarter`, `day_of_week`, `week_of_year`... (#18) |
| `measure_types` | `ev_orders` | `percent_of_total`, `running_total` (computed by Looker after the SQL), `list`, `date`/`string` measures (#31) |
| `fanout_by_customer`, `fanout_total` | `ev_orders` + `ev_items` | symmetric aggregates on a one-to-many join; `sum_distinct` / `average_distinct` (#31) |
| `distinct_total` | `ev_items` | `sum_distinct` vs a plain sum of a repeated value (#31) |
| `liquid_default`, `liquid_set` | `ev_liquid` | `{% condition %}`, `{% parameter %}`, `_parameter_value`, `_user_attributes` (#29) |
| `nested_by_id`, `nested_by_sku`, `nested_total` | `ev_nested` | `LEFT JOIN UNNEST` of a nested record (#30) |

To capture again (another dialect, a new case): see `tests/live/looker/evidence_links.py`.
Only BigQuery has been captured.
