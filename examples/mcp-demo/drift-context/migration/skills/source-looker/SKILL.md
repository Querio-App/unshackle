---
name: source-looker
description: Use when migrating Looker (LookML models, explores, dashboards, Looks, access filters and grants) with unshackle - auth, what `unshackle build looker` translates, how to finish LookML/Liquid/table-calculation TODOs, and how to prove parity with Looker's generated SQL.
---

# Looker source

## Access

| Variable | Value |
| --- | --- |
| `LOOKER_BASE_URL` | the address you open Looker at, no path: `https://<instance>.cloud.looker.com` (self-hosted: the API host and port, often `:19999`; Admin > API shows the API Host URL) |
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
  measures and access grants come out the same. LookML dashboards
  (`*.dashboard.lookml`) become notebooks (see Dashboards). Missing: user-defined
  dashboards and Looks (API only), Looker's generated SQL, view counts and links,
  and who holds each user attribute.

## What is deterministic

| LookML | Becomes |
| --- | --- |
| view `sql_table_name` | `catalog/` table + semantic model `node_relation` |
| `constant:` in `manifest.lkml` | `@{NAME}` replaced everywhere before parsing (constants that hold Liquid make the field a TODO) |
| `include:` in a model | explores in included files (`explores/*.explore.lkml`) belong to that model; `*`, `**`, relative paths and a missing `.lkml` work |
| `dimension` | categorical dimension; `${TABLE}.col` -> `col`, `${other_dim}` inlined, `case:` -> `CASE WHEN` |
| `${created_date}` and other timeframe refs | the group's SQL with Looker's value for the timeframe (table below) |
| measure over fields of a joined view (`sum: ${sale_price} - ${inventory_items.cost}`) | measure over `{{ Dimension('view__field') }}` refs -> Ossie model-scoped metric (`SUM(order_items.sale_price - inventory_items.cost)`) |
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
| dashboard elements (API) | notebook tiles with Looker's generated SQL (see below); Looks -> `queries/*.sql` |
| LookML dashboards (`*.dashboard.lookml`) | notebooks: tile SQL written from the explore, filters -> parameters (see below) |

Timeframe references (`${created_month}`, `${orders.created_date}`) use the
`dimension_group`'s SQL:

| Timeframe | SQL |
| --- | --- |
| `raw`, `time` | the group's SQL |
| `date` | `CAST(x AS DATE)` (the SQL itself with `datatype: date`) |
| `year`, `month_num`, `day_of_month`, `hour_of_day` | `EXTRACT(YEAR/MONTH/DAY/HOUR FROM x)` (no `hour_of_day` on a date) |
| `month`, `week` | Looker's string (`'2024-01'`, the Monday starting the week as `'2024-01-01'`) in the warehouse's dialect: `FORMAT_DATE` (BigQuery), `TO_CHAR` (Snowflake, Postgres, Redshift), `DATE_FORMAT` (Databricks), `STRFTIME` (DuckDB). Needs `[datasources.warehouse] type` in `unshackle.toml`; a `week_start_day` other than Monday stays TODO |
| others (`quarter`, `day_of_week`, `month_name`, `hour`, fiscal, ...), `datatype: epoch / yyyymmdd` | TODO with the original |

Looker converts timestamps to the query time zone when `convert_tz` is on and the
connection sets one; the translations, like the time dimensions themselves, use the
stored time zone.

A measure that reads another view's fields is translated when every explore with
both views joins them the same way, many_to_one or one_to_one on the other view's
primary key (so the semantic models relate through an entity and no row of the
measure's view repeats). Otherwise it stays TODO and says which: no explore joins
them, explores join them differently, the join repeats the measure's rows (Looker
dedupes with symmetric aggregates), or an inner/full join with an expression that
doesn't drop out on NULL. A *dimension* over another view stays TODO: Ossie fields
read one dataset; the TODO names the join.

Measure/metric names are the LookML names; a bare `count` or a name used in two views
becomes `<view>_<name>`. Labels keep Looker's labels.

## Finishing TODOs

- **Liquid** (`{% if %}`, `{{ _filters['x'] }}`, `{% parameter %}`, `{% condition %}`):
  Ossie expressions are plain SQL. A `parameter` switch becomes one metric per branch
  (`revenue_usd`, `revenue_eur`); `{% condition f %} col {% endcondition %}` becomes
  a metric `filter` or a `:param` in the notebook SQL; `_user_attributes` becomes
  a row filter.
- **Cross-view refs in a dimension** (`${customers.region}` in `orders`): move the
  dimension to the view that owns the column; Ossie relationships join it in.
  Measures built on it are translated when the join allows (see above).
- **Cross-view measures that stay TODO**: for a one_to_many join, pre-aggregate the
  other view per row of the measure's view in a SQL model (what Looker's symmetric
  aggregates compute), or define the measure on the other view.
- **Timeframe refs left TODO** (`${created_quarter}`, `${created_day_of_week}`):
  write Looker's value in your dialect (quarter is `'2024-01'` style for the first
  month, `day_of_week` the day's name), or reference `${created_raw}` and truncate.
- **Relative date filters** (`"30 days"`, `"last month"`): left TODO in a metric
  filter (only absolute dates are translated there); either drop the filter and use a
  time constraint at query time, or write
  `{{ TimeDimension('orders__created', 'day') }} >= CURRENT_DATE - 30`.
- **sum_distinct / average_distinct** (symmetric aggregates): pre-aggregate in a SQL
  model keyed by `sql_distinct_key`, then a plain `sum`/`average`.
- **running_total / percent_of_total**: a `cumulative` metric, or a window in tile SQL.
- **tier / duration dimension_group**: `CASE` buckets / `DATEDIFF` expression.
- **A primary key that doesn't translate** (Liquid, or a timeframe left TODO such as
  `CONCAT(${TABLE}.id, ${created_quarter})`): no primary entity is written (the
  warning names the view); write the key and add the entity, or joins to that view
  stay missing.
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

**From the API** (user-defined dashboards): tile SQL is Looker's generated SQL with
`AS "view.field"` rewritten to `view_field`.

**LookML dashboards** (`*.dashboard.lookml`, YAML): the only dashboards a project
without API access has. Each element's query becomes SQL over its explore, the way
Looker writes it:

- `FROM <base table> AS <explore>`, and only the joins its fields, filters and
  `sql_on`s need (`required_joins` too), in dependency order. `foreign_key` joins use
  the joined view's primary key. Derived tables are read under the name they
  materialize as (TODO until they exist).
- Dimensions are grouped (`GROUP BY 1, 2, ...`, also without measures), yesno fields
  select `'Yes'`/`'No'`. `count` is `COUNT(DISTINCT <primary key>)` (`COUNT(*)`
  without one), `sum/average/min/max/count_distinct` with measure `filters` as
  `CASE WHEN`, `type: number` over other measures inlined.
- Element `filters` -> `WHERE` (dimensions) / `HAVING` (measures); explore
  `sql_always_where`, `sql_always_having`, `always_filter` and `conditionally_filter`
  defaults apply as in Looker. `sorts` -> `ORDER BY` (Looker's default sort without
  them), `limit` -> `LIMIT`.
- Dashboard `filters` -> notebook parameters. A tile that `listen`s gets
  `(:param IS NULL OR <field> = :param)` when the default is one plain value (or
  none); the parameter takes one value, not a Looker filter expression. Date filters,
  defaults like `"7 days"`, `"[50,100]"` or `"a,b"`, and filters whose join would
  change the rows when empty (inner, full or one_to_many) are TODO on the tile.
- `extends` merges elements and filters by name; `extension: required` dashboards
  are not written. A LookML dashboard also exported through the API
  (`model::name`) is written once, from the API.
- Layout follows `row`/`col` (newspaper) or `rows` (grid).

No SQL is written (the tile says why) when a field or filter can't be translated
(Liquid, a Looker filter expression, `tier`/`location` dimensions, `median`,
`percentile`, `list` and other measure types, joins written as `sql:`), for merged
results, and when a measure's rows repeat across a join in the query: Looker uses
symmetric aggregates there (a sum or average on the many side of a one_to_many join,
or on a view joined many_to_one). `count` (distinct primary key), `count_distinct`,
`min` and `max` are safe. Notes that leave the SQL right but incomplete: a dashboard
filter not bound, a pivot with a `limit` (Looker limits pivoted rows; the long-format
SQL has none), a sort on one pivot column, `query_timezone` with converted
timestamps, access filters (enforced by the policies, not the SQL).

- **Table calculations / custom fields** (`dynamic_fields`): Looker expressions over
  the result. `${a} / ${b}` -> column expression; `offset(${x}, -1)` -> `LAG`;
  `running_total(${x})` -> `SUM(...) OVER (ORDER BY ...)`; `pivot_index`/`pivot_row`
  -> pivot in SQL or pandas in the tile's chart cell.
- **Dashboard filters** from the API are not in `run/sql`; the TODO names the param
  to add (`AND customers.region = :region`). Relative date defaults (`"12 months"`)
  need a real default in the parameters cell.
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

Replace relative filters in both sides with fixed dates. For metrics, run the
metric's SQL on the warehouse, grouped by month, against a Looker query of the same fields. Numbers straight from Looker:
`GET /queries/{id}/run/csv` (or `run_inline_query` with `result_format: csv`), saved
under `migration/parity/data/`.

## Alternatives

- **Looker MCP Toolbox** (`genai-toolbox`, `looker` prebuilt tools): `get_models`,
  `get_explores`, `get_dimensions`, `get_measures`, `query`, `query_sql` (returns the
  generated SQL) - handy for ad-hoc parity checks from an agent.
- **gzr** (Gazer CLI): `gzr dashboard cat <id> --dir .` / `gzr look cat` export full
  dashboard JSON; useful when the API key can't list content but a user can.
- LookML dashboards are written from the LookML (see Dashboards). To compare a tile
  with Looker's own SQL, import a user-defined copy (`POST /dashboards/{lookml_id}/import`)
  and extract it, or run the element's query in Looker and diff the SQL.

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
- Looker counts with symmetric aggregates across fan-out joins; unshackle translates
  cross-view measures only over x_to_one joins, but compare distinct counts after joins.
- Snowflake uppercases unquoted aliases; pandas lower-cases them, so `keys` match.
