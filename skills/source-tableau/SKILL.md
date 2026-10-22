---
name: source-tableau
description: Use when migrating Tableau (Server or Cloud workbooks, published datasources, calculated fields, LODs, table calcs, parameters, user filters, dashboards) with unshackle - PAT auth, what `unshackle build tableau` translates, how to finish Tableau TODOs, and how to prove parity with view CSVs.
---

# Tableau source

## Access

| Variable | Value |
| --- | --- |
| `TABLEAU_SERVER_URL` | `https://tableau.acme.com` or `https://prod-useast-a.online.tableau.com` (no `/api`) |
| `TABLEAU_SITE` | site **content URL** (the `acme` in `/#/site/acme/...`); empty for the Server default site |
| `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` | Personal Access Token: My Account Settings > Personal Access Tokens |
| `TABLEAU_API_VERSION` | optional, default `3.24` (Server 2024.2 / Cloud); older Server: `3.19` (2023.1), `3.15` (2022.1) |

- Use a **Site Administrator Creator** (or Server Admin). Other roles only see
  content they have View on, and need **Download Workbook/Save a Copy** on every
  workbook; anything that fails lands in `migration/raw/tableau/_errors.json` and
  the report's warnings.
- **Metadata API** (calculated fields, lineage, custom SQL): on by default on
  Tableau Cloud; on Server run `tsm maintenance metadata-services enable` (then
  wait for the backfill). If it is off, extract still works; lineage is missing.
- Auth: `POST /api/{ver}/auth/signin` with `personalAccessTokenName/Secret` and
  `site.contentUrl` -> `X-Tableau-Auth: <token>`; paths use the returned site **id**.
  unshackle signs out at the end. PATs expire after 15 days unused (Cloud admins can
  set shorter), and one PAT can hold only one session: don't run two extracts with
  the same token at once.

## Commands

```
unshackle inventory tableau        # workbooks, views (totalViewCount), published datasources
unshackle extract tableau [--only workbook:<luid> --only view:<luid> --only datasource:<luid>]
unshackle build tableau
unshackle todo
```

Raw files under `migration/raw/tableau/`:

| File | From |
| --- | --- |
| `workbooks.json`, `views.json`, `datasources.json`, `groups.json` | REST lists (paginated; views with `includeUsageStatistics=true`) |
| `workbooks/<luid>.twb` | `GET .../workbooks/{id}/content?includeExtract=false` (`.twbx` unzipped; extracts dropped) |
| `datasources/<luid>.tds` | `GET .../datasources/{id}/content?includeExtract=false` |
| `metadata/<luid>.json` | Metadata API: embedded datasources, fields + formulas, upstream tables, custom SQL |
| `views/<luid>.csv` | `GET .../views/{id}/data` for the 20 most viewed views (parity) |
| `permissions/<luid>.json` | `GET .../workbooks/{id}/permissions` |

The GraphQL query asks for 10 workbooks at a time and halves the batch on
`NODE_LIMIT_EXCEEDED`. With `--only`, published datasources used by the chosen
workbooks (`class='sqlproxy'`) are extracted too.

## Manual import (no API access)

Without a PAT, import the workbook and data source files:

```
unshackle import tableau "Superstore Sales.twbx" "Sales Targets.tdsx" more-workbooks/
unshackle build tableau
```

- **Where to get them:** in Tableau Cloud / Server, the workbook's or data source's
  actions menu (`...`) > Download (needs the Download Workbook/Save a Copy
  capability). In Tableau Desktop, File > Export Packaged Workbook writes a `.twbx`.
- Accepts `.twb`, `.twbx`, `.tds`, `.tdsx` and folders of them. Only the XML inside a
  packaged file is kept; extracts (`.hyper`) and images are dropped.
- Name each file after the workbook: the file name becomes the workbook name (and
  notebook slug), since project, owner and description only come from the REST API.
- The same parser runs, so models, calculations, worksheets and dashboards come out
  as they do from `extract`. Missing: view counts, workbook permissions (no Cedar
  permits; user filters in the workbook are kept), Metadata API lineage and the view
  CSVs used for parity. For parity, download a view's data as CSV and point a
  parity case at it.
- Download published data sources that workbooks use (`sqlproxy` connections) too;
  without them those workbooks' fields have no tables.

## What is deterministic

| Tableau | Becomes |
| --- | --- |
| `<relation type='table' table='[schema].[table]'>` | `catalog/` table + semantic model (one per table; the same table in many workbooks merges into one) |
| `<metadata-record class='column'>` | catalog column (`remote-name`, `local-type`); primary entity guessed from `row_id` / `id` / `<table>_id` / the only `*_id` column |
| numeric column (`role='measure'`) | measure with the column's default aggregation (`Sum`, `Avg`, `CountD`...) + simple metric |
| date / datetime column | time dimension (`day`); the only one is `agg_time_dimension`; with several, a TODO lists them |
| `SUM([Sales])`, `AVG`, `COUNT`, `COUNTD`, `MIN`, `MAX`, `MEDIAN` | measure + simple metric (`COUNTD` -> `count_distinct`) |
| `SUM(IF [Returned] = 'Yes' THEN 1 ELSE 0 END)`, `SUM(ZN([x]))` | measure whose `expr` is the row expression |
| `SUM([Profit]) / SUM([Sales])`, `[Calc A] / [Calc B]` | ratio metric (implied measures/metrics added when needed) |
| `[Profit Ratio] * 100` | derived metric |
| row calc: `IF/ELSEIF/ELSE/END`, `CASE`, `IIF`, arithmetic, `ZN`, `IFNULL`, `ISNULL`, `DATETRUNC('month', [d])`, `UPPER/LOWER/TRIM/ABS/ROUND`, `#2024-01-01#` | dimension `expr` (time dimension with that grain for `DATETRUNC`), or measure `expr` when the calc's role is measure |
| custom SQL `<relation type='text'>` | `queries/<ds>-custom-sql.sql` (TODO: materialize) + semantic model named after the datasource |
| `{FIXED [a] : AGG([x])}` | TODO on the field + `queries/<field>-fixed-lod.sql` with the pre-aggregation SQL |
| calc with `USERNAME()` / `ISMEMBEROF()` / `USERDOMAIN()` / `FULLNAME()` | row filter (`[Owner] = USERNAME()` -> `owner = {{ user.email }}`), always with a TODO |
| worksheet | notebook tile with generated SQL (below) |
| dashboard | notebook; zones grouped into layout rows by `y` (cell order, `unshackle.layout` metadata); text zones -> markdown cells; quick filter / parameter zones -> parameters cell |
| worksheets on no dashboard | one extra notebook `<workbook>-sheets` |
| group Read / Write on a workbook | Cedar `view` / `edit` on each of its notebooks (`Deny` -> `forbid`) |
| calc formulas, `<desc>`, parameters, sheet captions, lineage | `knowledge/<workbook>-glossary.md` |

Names: snake_case of the caption (`Profit Ratio` -> `profit_ratio`, `Margin %` ->
`margin_pct`). A caption used in two tables becomes `<model>_<name>`
(`orders_order_count`). Formulas in TODOs show captions instead of `Calculation_123`.

### Worksheets -> tiles

Shelf refs look like `[federated.x].[sum:Sales:qk]` = derivation : field : type
(`qk` quantitative, `nk` nominal, `ok` ordinal). Derivations: `none` (dimension),
`sum/avg/cnt/ctd/min/max/med` (aggregate), `usr` (aggregate calc), `tyr/tqr/tmn/twk/tdy`
(date truncation -> `date_trunc`, temporal), `yr/qr/mn/wk/dy/wd` (date part ->
`extract`, ordinal).

| Mark | Tile |
| --- | --- |
| Bar / Line / Area / Circle / Shape / Pie | bar / line / area / point / point / arc (`wedge-size` -> theta, `color` -> color) |
| Text with no dimensions / with dimensions | KPI (`mark_text`) / table (`return df`) |
| Automatic | continuous date + measure -> line; dimension + measure -> bar; measure only -> KPI; measure on both shelves -> point; dimensions only -> table |
| Map, Multipolygon, generated Latitude/Longitude | mark `unknown` + TODO (SQL still generated) |
| Square | TODO: `mark_rect()` heatmap |

`cols` -> x, `rows` -> y; a dimension on rows with the measure on cols is a
horizontal bar (x = measure, y = dimension). Extra dimension -> color; `lod` ->
detail; first tooltip field -> tooltip. SQL is `select <dims>, <aggs> from
<schema.table> [where] group by ... order by ...` (custom SQL datasources become a
`with custom_sql as (...)` CTE). Categorical filters (include / exclude / null) ->
`where` (or `having` on aggregates), range filters -> `>=`/`<=`, Top N by an
aggregate -> `order by ... desc limit N`. Aliases follow Tableau's display names
(`Month of Order Date` -> `month_of_order_date`); `source_config.columns` maps each
alias back to its Tableau name.

## Finishing TODOs

**FIXED LOD** (`{FIXED [Customer Name] : MIN([Order Date])}`): pre-aggregated at a
fixed grain -> SQL model. unshackle already wrote the SQL; save it as
`queries/models/customer_first_order.sql` (dbt: `models/unshackle/`), add a semantic model
with a primary entity on the FIXED columns, and a foreign entity on the base model:

```yaml
semantic_models:
- name: customer_first_order
  node_relation: {alias: customer_first_order, schema_name: analytics}
  defaults: {agg_time_dimension: customer_first_order}
  entities: [{name: customer, type: primary, expr: customer_name}]
  dimensions:
  - {name: customer_first_order, type: time, type_params: {time_granularity: day}}
# and in orders.yml entities: - {name: customer, type: foreign, expr: customer_name}
```

`{FIXED [Region] : SUM([Sales])}` used as a measure (share of region):
`sum(sales) / sum(sum(sales)) over (partition by region)` in the tile SQL, or a SQL
model joined back. Nested LODs: build the inner one first.

**INCLUDE / EXCLUDE LOD** depend on the view's dimensions: tile SQL. `{INCLUDE
[Order ID] : SUM([Sales])}` averaged per region -> aggregate a subquery by
`region, order_id`, then `avg()` per region. `{EXCLUDE [Month] : SUM([Sales])}` ->
`sum(sum(sales)) over (partition by <other dims>)`. `{ SUM([Sales]) }` (no keyword) ->
`sum(sum(sales)) over ()`.

**Table calcs** (`WINDOW_SUM/AVG`, `RUNNING_SUM`, `RANK*`, `LOOKUP`, `INDEX`, `TOTAL`,
`SIZE`, `FIRST/LAST`, `PREVIOUS_VALUE`, quick table calcs like `[pcto:sum:Sales:qk]`)
are computed over the view: window SQL in the tile, never a metric. Check the
"Compute using" (addressing/partitioning) in Tableau: it becomes `partition by` /
`order by`.

| Tableau | SQL |
| --- | --- |
| `RUNNING_SUM(SUM([Sales]))` along month | `sum(sum(sales)) over (order by month)` |
| `WINDOW_SUM(SUM([Sales]), FIRST(), 0)` | same as running sum |
| `WINDOW_AVG(SUM([Sales]), -2, 0)` | `avg(sum(sales)) over (order by month rows between 2 preceding and current row)` |
| `SUM([Sales]) / TOTAL(SUM([Sales]))`, percent of total | `sum(sales) / sum(sum(sales)) over ()` |
| `LOOKUP(SUM([Sales]), -1)` | `lag(sum(sales)) over (order by month)` |
| `RANK(SUM([Sales]))` | `rank() over (order by sum(sales) desc)` |
| `ZN(SUM([Sales])) - LOOKUP(ZN(SUM([Sales])), -12)` (YoY) | a `derived` metric with `offset_window: 1 year`, or `lag(..., 12)` |

A running total over time with no other partition can also be a `cumulative`
metric (see translate-metrics).

**Parameters**: a parameter that feeds a filter or a threshold becomes a notebook
parameter (`target_margin = ...` in the parameters cell, `:target_margin` in SQL), default = the parameter's current value (unshackle
adds these to dashboards that use them). A parameter that swaps measures (`CASE
[Parameters].[Metric] WHEN 'Sales' THEN SUM([Sales]) ...`) becomes one metric per
branch, and a tile per branch or a `:metric` switch in SQL. Parameters inside custom
SQL (`<[Parameters].[Year]>`) -> `:year` binds.

**Sets, groups, bins**: groups -> `case when region in ('East','West') then 'Coasts'
else region end` (unshackle writes the CASE in the TODO); bins -> `floor(sales / 100) *
100`; a fixed set -> `customer_name in (...)`; a Top N set -> `customer_name in
(select customer_name from ... group by 1 order by sum(sales) desc limit 20)`, as a
boolean dimension or in the tile SQL.

**ATTR([x])**: returns x when it has one value in the mark, else `*`. Group by x (it is
usually constant), or `case when min(x) = max(x) then min(x) end`.

**Blending** (a worksheet with two datasources, or `[other_ds].[field]` in a calc):
Tableau aggregates each source and left-joins on the linking fields. Write that in
SQL: aggregate each side by the linking dimensions, then left join; or model both
tables with entities and let MetricFlow join.

**Dual axis / measure names**: several measures on rows become stacked panels
(`alt.vconcat`), on columns side-by-side panels (`alt.hconcat`); a dual axis (panes with
their own marks) becomes `alt.layer(...).resolve_scale(y='independent')`. Check each
layer's mark against the view. `Measure Names`/`Measure Values`: unpivot in SQL
(`union all` of one select per measure with a `measure_name` column) and colour by it.
Dimensions nested on the shelves become `row`/`column` facets (the innermost one stays
on the axis).

**Maps**: no geo marks in unshackle notebooks. Keep the generated SQL (state/country + measure),
render a sorted bar or table, and put the Tableau view URL in the tile title or a text
tile. Don't try to reproduce polygons.

**Custom SQL datasources**: materialize the query as a SQL model/dbt model named
after the semantic model (`returns_custom_sql`); the tile SQL already inlines it as a
CTE so the notebook runs meanwhile.

**Extracts vs live**: unshackle reads the connection behind the extract; the `.hyper` is
never downloaded. Extract filters, aggregation ("aggregate data for visible
dimensions") and incremental refresh are not in the migrated SQL: check the
datasource's Extract settings and add those filters to the semantic model or SQL.
Extracts from Excel/CSV/Google Sheets have no warehouse table: unshackle warns; point the
model at the warehouse copy of that data.

## Permissions and user filters

- `permissions/<wb>.json` grants -> Cedar per notebook of the workbook. Read ->
  `view`, Write -> `edit`, Deny -> `forbid`; `All Users` -> everyone. Individual user
  grants are emitted as `user:<luid>` with a TODO: look up the email
  (`GET /sites/{site}/users/{id}`) or move to a group. Project default permissions,
  locked projects, view-level overrides and datasource Connect permissions are not
  exported: review them in Tableau and add `query` permits on the Datasource/Tables
  for groups with Connect (see translate-permissions).
- User filters: `[Regional Manager] = USERNAME()` -> row filter
  `regional_manager = {{ user.email }}` on the model's table. `USERNAME()` is the
  Tableau user name: the email on Cloud, often a domain login (`ACME\jdoe`) on
  Server; pick the matching IdP attribute. `ISMEMBEROF('Executives') OR ...` means
  members are unfiltered: write the predicate for everyone else and give the
  unfiltered group its own rule, or use an entitlement table. Calcs used as
  datasource filters are marked `(datasource filter)`; others only applied where a
  worksheet filtered on them.
- Row-level security via an entitlement table joined in the datasource (`[Email] =
  USERNAME()` on the joined table) -> subquery predicate
  `region in (select region from security.entitlements where email = {{ user.email }})`.
- Virtual connections / data policies (Cloud Data Management) are not in the
  workbook XML: read them in the Tableau UI and reproduce as row filters.

## Parity

For each `views/<luid>.csv`, the tile's `source_config` has `parity_csv`, `view_id`,
`csv_columns` (the CSV header) and `columns` (tile alias -> Tableau display name).
Write a case per headline tile:

```yaml
name: Superstore Sales - Sales Trend
source:
  csv: migration/raw/tableau/views/a1000000-0000-4000-8000-000000000002.csv
target:
  sql: |
    select category as "category", strftime(date_trunc('month', order_date), '%B %Y') as "month of order date",
           sum(sales) as "sales"
    from analytics.orders where region in ('East', 'West') group by 1, 2
keys: [category, month of order date]
```

- The CSV reflects the view's **saved** filters and the API user's user filters: use
  a PAT of a user who sees all rows, or compare row-filtered tiles as that user.
- Re-export with filters: `GET .../views/{id}/data?vf_Region=West&vf_Order%20Date=2025`
  (filter by field caption); `maxAge=1` bypasses the cache. Pin dates on both sides.
- Dates come back formatted (`January 2025`), numbers unformatted; format the target
  column the same way or cast both.
- Measure Names/Values views export long format (`Measure Names`, `Measure Values`
  columns).
- Extract-based views show the data as of the last refresh; compare after a refresh.

## Alternatives

- **Tableau MCP server** (GitHub `tableau/tableau-mcp`, PAT auth): tools to list
  workbooks, views and datasources, fetch a workbook, get a view's data as CSV, and
  query a published datasource (tool names vary by version; list them first). Handy
  for an agent to spot-check numbers or pull a fresh view CSV during parity, or to
  build an inventory when REST scripting is blocked; unshackle's extract is still what
  `build` reads.
- **VizQL Data Service** (`POST /api/v1/vizql-data-service/query-datasource`, Tableau
  Cloud and recent Server): query a published datasource with fields and filters, a
  clean source side for metric parity.
- `tabcmd get "/views/<wb>/<sheet>.csv"` or the Document API
  (`tableaudocumentapi`) when REST access is limited.

## Gotchas

- **Embedded vs published datasources**: embedded ones live in the `.twb`; published
  ones are `class='sqlproxy'` stubs in the workbook and the real definition is
  `datasources/<luid>.tds`. unshackle links them by content URL; if the `.tds` wasn't
  extracted (no permission) the fields have no table and tiles get a TODO.
- The same table used by several workbooks becomes one semantic model; the first
  definition of a calc name wins; a later, different formula under the same caption is
  skipped with a warning in the report: reconcile them (rename one) before migrating.
- Joined datasources (`<relation type='join'>`, new object-model relationships) give
  one model per table; add foreign entities by hand (unshackle warns).
- Local names vs captions: formulas reference local names (`[Calculation_123]`,
  sometimes `[sales]` with caption `Sales`); unshackle resolves both.
- Hidden fields are still migrated (they may feed calcs); `hidden: true` columns get
  `visible: false` in the catalog.
- Stories and web/image zones are not migrated (warning in the report).
- REST rate limits are not published; unshackle stays at 120 requests/min. Large sites:
  run `extract --only workbook:<luid>` for the inventory's top workbooks first.
