---
name: source-lightdash
description: Use when migrating Lightdash (dbt-based explores, metrics, charts, dashboards, sql_filter / required_attributes access rules, AI hints) with unshackle - API token and project, the charts-as-code export, dbt-YAML-only mode, what `unshackle build lightdash` translates, finishing Lightdash metric and table-calculation TODOs, and parity through compileQuery.
---

# Lightdash source

Lightdash's semantic layer is dbt YAML (`meta:` on models/columns); charts and
dashboards live in Lightdash. unshackle reads the **compiled explores** from the API (what
Lightdash actually runs), and falls back to the dbt YAML when there is no API access.

## Access

| Variable | Value |
| --- | --- |
| `LIGHTDASH_URL` | `https://app.lightdash.cloud` or your self-hosted URL (no `/api/v1`) |
| `LIGHTDASH_API_KEY` | personal access token (Settings > Personal access tokens); sent as `Authorization: ApiKey <token>` |
| `LIGHTDASH_PROJECT` | project UUID (from the URL `/projects/<uuid>/...`) |
| `LIGHTDASH_DBT_PROJECT_DIR` | optional: local dbt project; `models/**/*.yml` are copied to `raw/dbt/` (and are the only input if the three above are unset) |

- The token acts as its user: use an **admin** (or a project admin who can see every
  space). Private spaces the user can't open are silently missing.
- unshackle paces itself at 120 requests/minute; `/code/charts` and `/code/dashboards`
  page with `offset` until `total`.
- Explores with compile errors (`errors` in `GET /explores`) are skipped and reported.

## Commands

```
unshackle inventory lightdash        # explores, dashboards and charts with lifetime views
unshackle extract lightdash [--only <dashboard slug|uuid|name> | --only <chart slug|uuid> | --only <explore>]
unshackle build lightdash
unshackle todo
```

`--only <dashboard>` also pulls every chart on it and the explores those charts use.

Raw files under `migration/raw/lightdash/`:

| File | From |
| --- | --- |
| `project.json` | `GET /api/v1/projects/{uuid}` (+ url, uuid for links) |
| `explores/_index.json`, `explores/<explore>.json` | `GET .../explores`, `GET .../explores/{exploreId}` (compiled: tables -> dimensions/metrics with SQL, joins, `sqlWhere`, `requiredAttributes`, `aiHint`) |
| `charts/<slug>.yaml` | `GET .../code/charts` - charts as code, the same YAML `lightdash download` writes |
| `charts/<slug>.sql` | `POST .../explores/{explore}/compileQuery` with the chart's `metricQuery` (SQL, not run) |
| `dashboards/<slug>.yaml`, `dashboards/_usage.json` | `GET .../code/dashboards`; `GET .../dashboards` (views, first viewed) |
| `dbt/**/*.yml` | copy of `LIGHTDASH_DBT_PROJECT_DIR/models` |
| `_errors.json` | failures, turned into warnings |

## Manual import (no API access)

Without an API key, import the dbt project Lightdash reads its metrics from:

```
unshackle import lightdash path/to/dbt-project
unshackle build lightdash
```

- The folder with `dbt_project.yml`: `models/**/*.yml` are copied to `raw/dbt/`, and
  `parse()` reads the Lightdash `meta` blocks from them (the same path as
  `LIGHTDASH_DBT_PROJECT_DIR` with no API settings).
- Charts and dashboards: if you can run the Lightdash CLI (`lightdash login`, then
  `lightdash download`), import its `lightdash/` folder too (or the whole dbt project
  if it sits inside). Chart YAML (has `metricQuery`) goes to `raw/charts/`, dashboard
  YAML (has `tiles`) to `raw/dashboards/`.
- Missing: compiled explores (fields come from the dbt YAML instead), compiled chart
  SQL (every chart tile carries a TODO to write its SQL from the semantic layer),
  view counts and links, and space access.

## What is deterministic

| Lightdash | Becomes |
| --- | --- |
| explore table (`sqlTable`, `schema`, `database`) / dbt model (`config.alias/schema/database`) | `catalog/` table + semantic model (`node_relation`) |
| dimension (`type: string/number/boolean`) | categorical dimension; `${TABLE}.col` -> `col`, `${other_dim}` inlined |
| `type: timestamp/date` | time dimension (granularity day); generated interval dims (`created_at_month`) are dropped - MetricFlow grains replace them |
| `meta.primary_key` (or guessed `id` / `<table>_id`, info warning) | primary entity named after the table |
| `joins: [{join, sql_on: ${a.x} = ${b.y}, relationship}]` | foreign entity on the many side (`many_to_one`, `one_to_many`, `one_to_one`); aliased joins warned |
| metric `type: sum/count/count_distinct/average/min/max/median/percentile/sum_boolean` | measure + simple metric (`percentile: 90` -> `agg_params`) |
| metric `filters:` (`status: completed`, `'!x'`, `'> 5'`, `'%x%'`, `null`, lists; compiled `operator` form) | metric `filter` with `{{ Dimension('orders__status') }}` |
| `type: number` with `${a} / ${b}` or `${a} / NULLIF(${b}, 0)` | ratio metric |
| `type: number/string/date/boolean` over other metrics | derived metric |
| `sql_filter` / `sql_where` with `${lightdash.attributes.region}` | row filter `region = {{ user.region }}` (static filters get a TODO: a view may be better) |
| `required_attributes: {is_admin: 'true'}` on model / dimension | Cedar `forbid query` unless `Group::"is_admin=true"` (TODO: map to real groups) |
| `ai_hint` on model/field (dbt meta or compiled `aiHint`) | `knowledge/<table>-ai-hints.md` |
| dashboard (`tiles`, `x/y`, `filters.dimensions/metrics`) | `notebooks/<slug>.ipynb`, cells in grid-row order, dashboard filters in the parameters cell (`inThePast 12 months` kept as the default text) |
| chart `cartesian` (series `bar/line/area/scatter`, `flipAxes`, pivot) | bar/line/area/point; x = `layout.xField`, y = first `yRef`, pivot -> `color` (+ `xOffset` for grouped bars) |
| `big_number`, `table`, `pie` | KPI, table, arc |
| `markdown`, `loom` tiles | text tiles |
| charts on no dashboard | `queries/<chart>.sql` verified queries (compiled SQL) |

Field names in tile SQL and encodings are Lightdash field ids (`orders_created_at_month`,
`orders_total_revenue`), which is what compileQuery aliases them as.

## Finishing TODOs

- **Custom SQL metrics** (`type: number` with raw aggregates, `sql: SUM(${TABLE}.a) /
  COUNT(DISTINCT ${TABLE}.b)`): split into simple measures and a ratio
  (`a_sum / b_count_distinct`).
- **`type: max/min` on dates, `type: date`, `string` metrics**: `max` measure on the
  date column; string metrics (`LISTAGG`) have no MetricFlow agg - verified query.
- **`average_distinct`, `sum_distinct`** (symmetric aggregates keyed by `distinct_keys`):
  pre-aggregate in a SQL model at the key grain, then `average`/`sum`.
- **Metric filters on joined tables or relative dates** (`inThePast 30 days`):
  `{{ Dimension('customers__region') }}` works through entities; relative dates ->
  `{{ TimeDimension('orders__created_at', 'day') }} >= current_date - 30` or drop and
  constrain at query time.
- **`additional_dimensions`**, **`groups`**, **`urls`**: additional dimensions become
  dimensions; groups and urls are UI only (a note in the knowledge file if useful).
- **Table calculations** (chart `metricQuery.tableCalculations`): SQL ones are already
  in the compiled SQL as a wrapping CTE; formula/template ones (`percent_of_column_total`,
  `running_total`, `percent_change_from_previous`, `rank_in_column`) need windows in
  the tile SQL:
  `revenue / sum(revenue) over ()`, `sum(revenue) over (order by month)`,
  `revenue / lag(revenue) over (order by month) - 1`, `rank() over (order by revenue desc)`.
- **Custom metrics / custom dimensions** in a chart (`additionalMetrics`,
  `customDimensions`, bins): compiled into the SQL; promote to the semantic layer if
  several charts use them.
- **Mixed series / multiple y**: one tile per series or fold in SQL (`UNION ALL` +
  color by series). **Funnel, gauge, treemap, map, custom (Vega-Lite)**: custom charts
  port with `alt.Chart.from_dict(spec)` (single view only); the rest pick a mark.
- **Dashboard filters** are not in the chart's compiled SQL: add `:param` predicates
  and a concrete default (`inThePast 12 months` -> `<name>_from` / `<name>_to`).
- **SQL chart / semantic viewer tiles and other tile types**: unshackle adds an `unknown`
  tile with a TODO; copy their SQL from Lightdash into the tile.

## Permissions

| Lightdash | unshackle |
| --- | --- |
| `sql_filter` with user attributes | row filter (done); `${lightdash.user.email}` -> `{{ user.email }}` by hand |
| `required_attributes` (model/dimension/metric) | `forbid query` on Table/Column/Metric unless group; replace `attr=value` groups with IdP groups |
| space access (viewer / editor / admin) | `view` / `edit` on the notebooks from that space (`GET /api/v1/projects/{uuid}/spaces/{spaceUuid}` lists access) |
| project roles (viewer, interactive viewer, editor, developer, admin) | viewer -> `view`; interactive viewer+ -> `query` on the Datasource; editor+ -> `edit` |
| user attribute values | IdP attributes (`{{ user.<attr> }}`) |

## Parity

compileQuery is the oracle: it returns exactly what Lightdash would run for a
`metricQuery` (dimensions, metrics, filters, sorts, limit, table calculations).

```
curl -s -X POST "$LIGHTDASH_URL/api/v1/projects/$LIGHTDASH_PROJECT/explores/orders/compileQuery" \
  -H "Authorization: ApiKey $LIGHTDASH_API_KEY" -H 'Content-Type: application/json' \
  -d @metric_query.json          # the chart's metricQuery with the date filter pinned
```

```yaml
name: Sales overview - Monthly revenue
source:
  sql: "<compiled SQL from migration/raw/lightdash/charts/monthly-revenue.sql, dates pinned>"
target:
  notebook: notebooks/sales-overview.ipynb
  tile: monthly_revenue
keys: [orders_created_at_month]
```

For metrics, compile a query of one metric by month and compare with
`mf query --metrics total_revenue --group-by metric_time__month`. Numbers from Lightdash
itself: run the chart and download CSV, or the query API (`runQuery` / v2 async query).

## Alternatives

- **`lightdash download`** (Lightdash CLI, `lightdash login <url> --token <pat>`,
  `lightdash config set-project`): writes charts and dashboards as code to
  `lightdash/charts/*.yml` and `lightdash/dashboards/*.yml`. Copy them into
  `migration/raw/lightdash/charts/<slug>.yaml` / `dashboards/<slug>.yaml` when the API
  export isn't possible (tiles then lack SQL until you compile).
- **dbt-only**: set only `LIGHTDASH_DBT_PROJECT_DIR`; unshackle reads `meta.dimension`,
  `meta.metrics`, `joins`, `primary_key`, `sql_filter`, `required_attributes`,
  `ai_hint`, `additional_dimensions`, `default_time_dimension` (dbt 1.10 `config.meta`
  too). No charts or dashboards in this mode.
- **Lightdash MCP server** (hosted, per instance): `find_explores`, `find_fields`,
  `read_content` (a chart/dashboard's definition) and `run_metric_query` (numbers for a
  metric query) - use `read_content` to check a chart the token couldn't export and
  `run_metric_query` for quick parity numbers.
- If the repo is the customer's dbt project, keep the MetricFlow YAML next to it
  (`models/semantic/`); unshackle does that automatically when `dbt_project.yml` exists.

## Gotchas

- Explore table names are aliases (`orders`, `customers`); the physical table comes
  from `sqlTable`. Two explores joining the same table under different aliases produce
  one semantic model (the first); role-playing joins need a second entity by hand.
- Lightdash counts with `count` on the primary key; a model without a primary key
  counts rows (`sum` of 1) - check.
- Tiles that reference a chart missing from the export (private space, deleted) become
  `unknown` tiles with a TODO naming the chart slug.
- Compiled SQL uses Lightdash's warehouse dialect and quoted aliases; Snowflake upper-
  cases unquoted names, pandas lower-cases them, so parity keys still match.
