---
name: source-looker
description: Use when migrating Looker (LookML models, explores, dashboards, Looks, access filters and grants) with unshackle - auth, what `unshackle build looker` translates, how to finish LookML/Liquid/table-calculation TODOs, and how to prove parity with Looker's generated SQL.
---

# Looker source

## Access

| Variable | Value |
| --- | --- |
| `LOOKER_BASE_URL` | `https://<instance>.cloud.looker.com` (no `/api/4.0`; self-hosted: include the port, often `:19999`) |
| `LOOKER_CLIENT_ID`, `LOOKER_CLIENT_SECRET` | API3 key: Admin > Users > edit user > Edit Keys |
| `LOOKER_LOOKML_DIR` | optional but recommended: local clone of the LookML project's Git repo (production branch) |

- Use an **Admin** user's key, or one with `see_lookml`, `see_user_dashboards`,
  `explore`, `see_system_activity` and access to every folder. Looker returns 404 for
  content the user can't see, so a weak key gives a silently partial export; the
  unreadable paths land in `migration/raw/looker/_skipped.json` and the report.
- Auth: `POST /api/4.0/login` (form `client_id`, `client_secret`) -> header
  `Authorization: token <access_token>` (1 hour). Rate limits are unpublished;
  unshackle stays at 120 req/min.
- Without `LOOKER_LOOKML_DIR` unshackle rebuilds views from
  `GET /lookml_models/{m}/explores/{e}`; field `sql` only appears with `see_lookml`,
  and access grants, derived table SQL and `case:` blocks are lost. Clone the repo.

## Commands

```
unshackle inventory looker          # explores, dashboards, Looks, view counts
unshackle extract looker [--only dashboard:12 --only look:7]
unshackle build looker
unshackle todo
```

Raw files: `lookml/**` (copy of the repo), `dashboards/<id>.json`, `looks/<id>.json`,
`queries/<query_id>.sql` (from `GET /queries/{id}/run/sql`, which compiles without
running), `content_usage.json` (`GET /content_view/search`), `api/lookml_models.json`.

## Manual import (no API access)

Without an API key, import the LookML project:

```
unshackle import looker path/to/lookml-checkout     # or the repo's .zip download
unshackle build looker
```

- Clone the LookML project's Git repository (production branch). If you can't use
  Git, your Git host's "download ZIP" works; a single top-level folder in the zip is
  unwrapped.
- Only `.lkml` and `.lookml` files are copied; `.git` and anything else is skipped.
- This is the same LookML `LOOKER_LOOKML_DIR` gives `extract`, so views, explores,
  measures and access grants come out the same. Missing: user-defined dashboards and
  Looks (API only; LookML dashboards are not parsed), Looker's generated SQL, view
  counts and links, and who holds each user attribute.

## What is deterministic

| LookML | Becomes |
| --- | --- |
| view `sql_table_name` | `catalog/` table + semantic model `node_relation` |
| `constant:` in `manifest.lkml` | `@{NAME}` replaced everywhere before parsing (constants that hold Liquid make the field a TODO) |
| `include:` in a model | explores in included files (`explores/*.explore.lkml`) belong to that model; `*`, `**`, relative paths and a missing `.lkml` work |
| `dimension` | categorical dimension; `${TABLE}.col` -> `col`, `${other_dim}` inlined, `case:` -> `CASE WHEN` |
| `primary_key: yes` | primary entity named after the view |
| `dimension_group: type: time` | time dimension named after the group; granularity = smallest timeframe (`date` -> day, `time` -> second, `datatype: date` caps at day) |
| measure `sum/average/count_distinct/min/max/median/percentile` | measure + simple metric |
| `type: count` | `count` of the primary key (`sum` of `1` without one) |
| `filters: [status: "complete"]` | simple metric with `filter: "{{ Dimension('orders__status') }} = 'complete'"` (strings, `-x`, `a,b`, `%x%`, `NULL`, `NOT NULL`, yes/no, numeric comparisons; relative dates stay TODO) |
| `type: number` `${a} / NULLIF(${b}, 0)` | ratio metric |
| other `type: number` over measures | derived metric (`expr` with metric names) |
| explore `join` with `sql_on: ${a.fk} = ${b.pk}` | foreign (or `unique` for one_to_one) entity on `a` named `b` |
| `access_filter {field, user_attribute}` | row filter `<col> = {{ user.<attr> }}` on the field's table, plus `<fk> IN (SELECT ...)` on the explore's base table |
| `sql_always_where` | row filter for `*`; `{{ _user_attributes['x'] }}` -> `{{ user.x }}` |
| `required_access_grants` / `access_grant` | Cedar `permit` per allowed value, TODO to map groups |
| `derived_table` | `queries/<view>-derived-table.sql` + semantic model on a table of the same name (TODO: materialize) |
| `sql_table_name` that is SQL (a subquery or a Liquid switch) | treated like a derived table |
| dashboard elements | notebook tiles (see below); Looks -> `queries/*.sql` |

Measure/metric names are the LookML names; a bare `count` or a name used in two views
becomes `<view>_<name>`. Labels keep Looker's labels.

## Finishing TODOs

- **Liquid** (`{% if %}`, `{{ _filters['x'] }}`, `{% parameter %}`, `{% condition %}`):
  MetricFlow has no templating. A `parameter` switch becomes one metric per branch
  (`revenue_usd`, `revenue_eur`); `{% condition f %} col {% endcondition %}` becomes
  a metric `filter` or a `:param` in the notebook SQL; `_user_attributes` becomes
  a row filter.
- **Cross-view refs in a dimension** (`${customers.region}` in `orders`): move the
  dimension to the view that owns the column; MetricFlow joins through entities.
- **Timeframe refs** (`${created_date}`): `CAST(created_at AS DATE)` or
  `DATE_TRUNC('<tf>', created_at)` in your dialect.
- **Relative date filters** (`"30 days"`, `"last month"`): no MetricFlow equivalent
  in a metric filter that stays correct over time; either drop the filter and use a
  time constraint at query time, or write
  `{{ TimeDimension('orders__created', 'day') }} >= CURRENT_DATE - 30`.
- **sum_distinct / average_distinct** (symmetric aggregates): pre-aggregate in a SQL
  model keyed by `sql_distinct_key`, then a plain `sum`/`average`.
- **running_total / percent_of_total**: a `cumulative` metric, or a window in tile SQL.
- **tier / duration dimension_group**: `CASE` buckets / `DATEDIFF` expression.
- **Timeframe refs in a primary key** (`CONCAT(${TABLE}.id, ${created_date})`): no
  primary entity is written (the warning names the view); write the key with
  `CAST(... AS DATE)` and add the entity, or joins to that view stay missing.
- **`sql_table_name` with Liquid** (often `{% if _dialect._name == ... %}`): pick the
  branch for your warehouse and use it as the table (or materialize it); the query
  file has the original.
- **Joins with `sql:`** (`LEFT JOIN UNNEST(${orders.lines}) AS orders__lines`): nested
  BigQuery records. The child views have no table and are skipped; flatten them in a
  SQL model keyed by the parent's key, then add it as a semantic model.
- **Liquid labels** (`label: "{% if _user_attributes['locale'] == 'ja' %}..."`, or a
  constant like `@{label_sap_field_name}`) fall back to the field name. Pick a label.
- **PDTs**: create a dbt model (or scheduled table) from the derived-table query,
  resolve `${view.SQL_TABLE_NAME}` (unshackle already inlines known tables), and keep the
  datagroup cadence as the model's schedule. Native derived tables (`explore_source`)
  need the SQL from Looker's SQL Runner or `run/sql` of an equivalent query.
  `sql_create` / `create_process` run custom DDL (often BigQuery ML `CREATE MODEL`);
  port them to your scheduler.
- **Access grants**: Looker checks a user attribute; the Cedar policies check groups. Create
  one group per allowed value in the IdP (or rename to the real group), delete the rest.
  Several grants on one object mean AND in Looker: use one group that encodes both.
- **Access filters used in several explores**: unshackle row filters apply to every query
  on the table. The TODO lists the explores that exposed the table without the filter.

## Dashboards

Vis types: `looker_column/bar` -> bar, `looker_line` -> line, `looker_area` -> area,
`looker_scatter` -> point, `looker_pie/donut_multiples` -> arc, `single_value` -> KPI,
`looker_grid/table` -> table, maps and custom viz -> TODO. First dimension -> x
(temporal for dimension-group fields), first measure -> y, first pivot -> color.
Tile SQL is Looker's generated SQL with `AS "view.field"` rewritten to `view_field`.

- **Table calculations / custom fields** (`dynamic_fields`): Looker expressions over
  the result. `${a} / ${b}` -> column expression; `offset(${x}, -1)` -> `LAG`;
  `running_total(${x})` -> `SUM(...) OVER (ORDER BY ...)`; `pivot_index`/`pivot_row`
  -> pivot in SQL or pandas in the tile's chart cell.
- **Dashboard filters** are not in `run/sql`; the TODO names the param to add
  (`AND customers.region = :region`). Relative date defaults (`"12 months"`) need a
  real default in the parameters cell.
- **Pivoted queries** return SQL with `z__pivot_col_rank` CTEs; long format (one row
  per x and pivot value) is what Altair wants, so drop the ranking wrapper.
- Extra measures on one chart: fold them in SQL (`UNION ALL` or unpivot) and color by
  measure name, or split tiles.

## Parity

`run/sql` compiles with the **API user's** attributes and access filters (the fixture
shows `customers.region = 'EU'` baked in). Use a user without access filters, or
`sudo` as the user whose numbers you compare. Case per tile:

```yaml
name: Sales Overview - Monthly revenue by region
source:
  sql: "<contents of migration/raw/looker/queries/5001.sql, date window pinned>"
target:
  notebook: notebooks/sales-overview.ipynb
  tile: monthly_revenue_by_region
keys: [orders_created_month, customers_region]
```

Replace relative filters in both sides with fixed dates. For metrics, compare
`mf query --metrics total_revenue --group-by metric_time__month` output (or the
compiled SQL) against a Looker query of the same fields. Numbers straight from Looker:
`GET /queries/{id}/run/csv` (or `run_inline_query` with `result_format: csv`), saved
under `migration/parity/data/`.

## Alternatives

- **Looker MCP Toolbox** (`genai-toolbox`, `looker` prebuilt tools): `get_models`,
  `get_explores`, `get_dimensions`, `get_measures`, `query`, `query_sql` (returns the
  generated SQL) - handy for ad-hoc parity checks from an agent.
- **gzr** (Gazer CLI): `gzr dashboard cat <id> --dir .` / `gzr look cat` export full
  dashboard JSON; useful when the API key can't list content but a user can.
- LookML dashboards (`*.dashboard.lookml`) are not parsed. Without API access they are
  the only dashboards you have: rebuild each tile from its element (`model`, `explore`,
  `fields`, `pivots`, `filters`, `listen`). With API access, import a user-defined copy
  (`POST /dashboards/{lookml_id}/import`) or extract the UDD version.

## Gotchas

- Refinements (`view: +orders`, `explore: +orders`) are applied first, then `extends`,
  as Looker does: a view that extends a refined view gets the refinement, and
  `extends` added by a refinement counts. `extension: required` views and explores
  are not written themselves.
- Views are global here, while Looker scopes refinements to the models that include
  them. If two models refine the same view differently, the result is both merged;
  check the view against the model you migrate first. When two files define the same
  view, the one a model includes wins (warning).
- Ignored without a TODO because they don't change numbers: `aggregate_table`
  (aggregate awareness), explore `query:` (Quick Start), `fields:` / `sets`,
  `always_join`, `test:` blocks, `.strings.json` localization (labels that are
  localization keys come through as the key).
- Join aliases (`join: buyer { from: customers }`) need a second entity by hand
  (role-playing dimensions).
- Looker counts with symmetric aggregates across fan-out joins; a MetricFlow `count`
  on a many_to_one join stays correct, but compare distinct counts after joins.
- Snowflake uppercases unquoted aliases; pandas lower-cases them, so `keys` match.
