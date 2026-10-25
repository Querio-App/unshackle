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
  New or republished workbooks show up there after a delay (seconds to minutes);
  extract lists any it could not find in `_errors.json`: re-run it later.
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
| numeric column (`role='measure'`) | measure with the column's default aggregation (`Sum`, `Avg`, `CountD`...; `<column aggregation='Avg'>` set in Desktop wins over the metadata record) + simple metric |
| date / datetime column | time dimension (`day`); the only one is `agg_time_dimension`; with several, a TODO lists them |
| `SUM([Sales])`, `AVG`, `COUNT`, `COUNTD`, `MIN`, `MAX`, `MEDIAN` | measure + simple metric (`COUNTD` -> `count_distinct`) |
| `SUM(IF [Returned] = 'Yes' THEN 1 ELSE 0 END)`, `SUM(ZN([x]))` | measure whose `expr` is the row expression |
| `SUM([Profit]) / SUM([Sales])`, `[Calc A] / [Calc B]` | ratio metric (implied measures/metrics added when needed) |
| `[Profit Ratio] * 100` | derived metric |
| row calc: `IF/ELSEIF/ELSE/END`, `CASE`, `IIF`, arithmetic, string `+`, `NULL`, `ZN`, `IFNULL`, `ISNULL`, `DATETRUNC('month', [d])`, `UPPER/LOWER/TRIM/ABS/ROUND`, `#2024-01-01#`, `DATEPART`/`DATENAME` of week, weekday, quarter, year | dimension `expr` (time dimension with that grain for `DATETRUNC`; weeks, quarters and years follow the Date Properties, see Dates below), or measure `expr` when the calc's role is measure (Tableau's NULL, division and ROUND rules: Calculation semantics below) |
| custom SQL `<relation type='text'>` | `queries/<ds>-custom-sql.sql` (TODO: materialize) + semantic model named after the datasource |
| joins (`<relation type='join'>`) on a table's key | foreign entity on the other table's model (Ossie relationship with `join_type` in `custom_extensions`); the join's semantics go in both models' descriptions |
| `{FIXED [a] : AGG([x])}` (one FIXED dimension) | semantic model `<a>_lod` defined by its query (`select a, agg(x) ... group by 1` over the datasource's rows, joins included): Ossie's dataset `source`, and in dbt projects `models/unshackle/<a>_lod.sql`. Primary entity `a`, foreign entity `a` on the table holding it. The value is a dimension (or a measure + metric when the field is a measure); all FIXED fields at the same grain share one model |
| `AVG({FIXED [a] : SUM([x])})`, `AVG([a FIXED field])` | measure + metric on the `<a>_lod` model |
| row calc over FIXED values of one grain (`IF [Account Revenue] >= 1000000 THEN "Large" ... END`) | dimension of the `<a>_lod` model |
| calc with `USERNAME()` / `FULLNAME()` / `USERDOMAIN()` / `ISMEMBEROF('g')` | row filter (`[Owner] = USERNAME()` -> `owner = {{ user.email }}`, `ISMEMBEROF('g')` -> `'g' in ({{ user.groups }})`), always with a TODO; a datasource filter also gets a rule per inner-joined table |
| worksheet | notebook tile with generated SQL (below) |
| dashboard | notebook; zones grouped into layout rows by `y` (cell order, `unshackle.layout` metadata); text zones -> markdown cells; quick filter / parameter zones -> parameters cell |
| worksheets on no dashboard | one extra notebook `<workbook>-sheets` |
| group Read / Write on a workbook | Cedar `view` / `edit` on each of its notebooks (`Deny` -> `forbid`) |
| calc formulas, `<desc>`, parameters, hierarchies (`<drill-path>`), sheet captions, lineage | `knowledge/<workbook>-glossary.md` |

Names: snake_case of the caption (`Profit Ratio` -> `profit_ratio`, `Margin %` ->
`margin_pct`). A caption used in two tables becomes `<model>_<name>`
(`orders_order_count`). Formulas in TODOs show captions instead of `Calculation_123`.

### Worksheets -> tiles

Shelf refs look like `[federated.x].[sum:Sales:qk]` = derivation : field : type
(`qk` quantitative, `nk` nominal, `ok` ordinal). Derivations: `none` (dimension),
`sum/avg/cnt/ctd/min/max/med` (aggregate), `usr` (aggregate calc), `tyr/tqr/tmn/twk/tdy`
(date truncation -> `date_trunc`, temporal), `yr/qr/mn/wk/dy/wd` (date part ->
`extract`, ordinal). Weeks, quarters and years follow the datasource's Date Properties (below).

### Dates: week start and fiscal year

Tableau keeps a datasource's Date Properties as `<date-options start-of-week='monday'
fiscal-year-start='july' />`. A workbook's copy of a published datasource repeats them under
`<overridable-settings>` (with `<default-calendar-type>`), but the published datasource's own
setting is the one that counts; the copy is used only when the `.tds` wasn't extracted.

- `DATETRUNC('week')`, `DATEPART`/`DATENAME('week')` and the shelf dates `twk`/`wk` follow the
  Week start (or the function's `start_of_week`): `date_trunc('week', d + interval '1 day') -
  interval '1 day'` for Sunday; week 1 is the week holding January 1.
- `DATEPART('weekday')` and `wd` are 1 on Sunday to 7 on Saturday, whatever the Week start and
  `start_of_week`. `DATENAME('weekday')` gives the English day name when the locale is English;
  with another or no locale it stays TODO (the names are in the locale's language).
- With no Week start, Tableau uses the locale. unshackle reads the workbook's `locale` or the
  datasource's `author-locale` / `locale` (Sunday for `en_US`, `en_CA`, `ja_JP`...; Monday for
  `en_GB`, `de_DE`, `fr_FR`...). With neither, week results stay TODO (never Monday by default):
  set the start the customer's Tableau uses and write the SQL from the TODO.
- A datasource's Fiscal year start changes nothing for date fields on the standard calendar;
  Tableau applies it only to fields set to the fiscal calendar (Default Properties > Fiscal Year
  Start), which the files don't show. So with a Fiscal year start other than January, quarter,
  year and week-number results (`DATETRUNC`/`DATEPART`/`DATENAME`, `tqr/qr/tyr/yr/wk`) stay TODO
  with the SQL for both cases; for a fiscal field, quarters start in that month, week 1 holds the
  fiscal year's first day, and the fiscal year is named after the calendar year it ends in (July
  start: 2024-07-01 is in 2025). Quarter truncation stays translated when fiscal quarters begin
  with the calendar ones (January, April, July or October).
- The ISO-8601 week-based calendar stays TODO for week and weekday results.

The week SQL shifts Monday weeks (`date_trunc('week')` in Postgres, DuckDB, Redshift, Databricks,
BigQuery's `week(monday)`) with `interval` arithmetic; for BigQuery and Databricks the dialect
pass writes `interval 1 day`. Snowflake's `date_trunc('week')` follows the session's `WEEK_START`:
the SQL assumes the default (Monday); check it if the account sets another. Weekday numbers use
`extract(dow) + 1` (mapped per dialect).

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
`with custom_sql as (...)` CTE). A sheet on a joined datasource gets the datasource's
joins (`from public.orders as orders inner join public.products as products on ...`,
physical columns qualified), even when it uses one table: Tableau computes it over the
joined rows, where inner joins drop unmatched rows and joins can repeat rows. The join
is left out only when it can't change the numbers: the sheet uses the first table and
every join is a left join on the other table's key. Categorical filters
(include / exclude / null) -> `where` (or `having` on aggregates), range filters ->
`>=`/`<=`, Top N by an aggregate -> `order by ... desc limit N`. A filter on a calc
that compares with a parameter (`[Revenue] >= [Parameters].[Min Order Value]`)
becomes `where (post_discount_revenue >= :min_order_value) = true`, bound from the
parameters cell (see **Parameters** below). Aliases follow Tableau's display names
(`Month of Order Date` -> `month_of_order_date`); `source_config.columns` maps each
alias back to its Tableau name.

**LODs in tiles** follow Tableau's order of operations. A FIXED grain is a CTE computed
over all of the datasource's rows (before the sheet's dimension filters):

| Sheet shows | Tile SQL |
| --- | --- |
| a FIXED value as a dimension (`Account Tier`, `Month of First Order`) | `left join account_id_lod on orders.account_id is not distinct from account_id_lod.account_id`, one value per row |
| a FIXED value aggregated (`AVG([Account Revenue])`, `AVG({FIXED ...})`, `MAX` of a two-dimension FIXED) | one value per distinct (sheet dimensions, FIXED dimensions) from the filtered rows, joined to the grain, then aggregated per mark |
| `AVG({INCLUDE [order_id] : SUM([x])})` | aggregate at the sheet's dimensions plus `order_id`, then `avg` per mark |
| `SUM({EXCLUDE [paper_type] : SUM([x])})` | aggregate at the sheet's dimensions minus `paper_type`, joined back to each mark |
| `SUM([x] / [Account Revenue])` (row fields and FIXED values) | an ordinary aggregate over the rows joined to the grain |
| an aggregate of a calc over FIXED values (`SUM(IF [Account Revenue] >= 1000000 THEN 1 ELSE 0 END)`) | as a FIXED value aggregated: the calc once per distinct (sheet dimensions, FIXED dimensions), not per row |
| a FIXED aggregate inside an expression (`SUM([x]) / SUM([Account Revenue])`) | each aggregate at its own level (`SUM([x])` over the rows, `SUM([Account Revenue])` as above), then the expression per mark |
| a FIXED nested in a FIXED (`{FIXED [cycle] : AVG({FIXED [account] : SUM([x])})}`) | a grain of its own: the inner value once per distinct (cycle, account) of the datasource's rows, aggregated per cycle |

Aggregated LODs get their own CTEs joined to a `base` CTE on the sheet's dimensions.
Counting rows instead of these distinct combinations can be off by an order of magnitude.

In the semantic layer, a calc over one grain's FIXED values is a metric on that grain's
model (`large_account_orders` on `account_id_lod`), and a nested FIXED is a `_lod` model
of its own.

### Calculation semantics

Tableau formulas render with Tableau's rules (`sources/_tableau_calc.py`, its function table
`FUNCS`; one case per function in `tests/fixtures/tableau_calc/cases.yaml`):

| Tableau | SQL |
| --- | --- |
| `[a] + [b]` on strings | `a \|\| b` (NULL if either is NULL) |
| `IIF(test, a, b[, unknown])` | `case when test then a when not (test) then b [else unknown] end`: NULL when `test` is NULL, unlike `IF`/`CASE`, which take ELSE |
| `[flag] = TRUE` | `flag = true` (NULL when `flag` is NULL) |
| `NULL` | `null` |
| `[a] / [b]` | `1.0 * a / nullif(b, 0)`: a real, NULL for a zero divisor; double precision on Postgres and Redshift, as Tableau sends it |
| `ROUND(x[, n])`, live connection | the warehouse's `round()`, as Tableau sends it (on Postgres `round(cast(x as numeric), n)` with digits; without, a double's halves round to even) |
| `ROUND(x)`, extract | `round(x, 0)`: halves away from zero |
| `ROUND(x, n)` of a real, extract | TODO: Tableau rounds the stored double, so a decimal tie (0.25 to 1 digit) can round down |
| `MEDIAN` | `median()` (Postgres: `percentile_cont(0.5)`): the mean of the two middle values of an even count |
| `COUNT`, `COUNTD`, `AVG` | skip NULLs; `COUNTD` counts `''`; `AVG` of integers is a real |

Strings compare with the connection's collation. On a live connection that is the
warehouse's, so the SQL does what Tableau did. An extract (`<extract enabled='true'>`)
compares by code point (case- and accent-sensitive, `'B' < 'a'`), maps `ß` to `SS` and a
final `Σ` to `ς` in UPPER/LOWER, and also trims tabs: when a calc relies on these, the
semantic model's description says so. Check string calcs on an extract against a
warehouse whose collation is not binary (`en_US` Postgres, SQL Server `_CI_` collations).

## Finishing TODOs

**FIXED LODs that stay TODO**: several FIXED dimensions in the semantic layer (tiles
work; the TODO holds the grain's SQL: save it as a SQL model with a composite key and
relate it by hand); a FIXED aggregate inside an expression as a metric (it combines two
models: tiles compute it, above); other nestings than FIXED in FIXED (INCLUDE or EXCLUDE
inside, or a nested LOD inside a larger expression: build the inner one first). Tableau
takes each FIXED value once per distinct combination of the view's dimensions and the
FIXED dimensions, not once per row; keep that when writing them by hand. In dbt projects
MetricFlow also needs a time dimension on the `_lod` model before its measures load.

**INCLUDE / EXCLUDE LOD** depend on the view's dimensions, so they are never metrics;
tiles that show them compute them (above). Elsewhere: `{INCLUDE [Order ID] : SUM([Sales])}`
averaged per region -> aggregate a subquery by `region, order_id`, then `avg()` per region.
`{EXCLUDE [Month] : SUM([Sales])}` -> `sum(sum(sales)) over (partition by <other dims>)`.
`{ SUM([Sales]) }` (no keyword) -> `sum(sum(sales)) over ()`.

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

**Parameters**: every workbook parameter a tile reads is a notebook parameter:
`growth_rate = 0.1  # from 0.0 to 1.0, step 0.01` in the parameters cell (default = its
current value, comment = its list, with aliases, or its range), `:growth_rate` in the tile
SQL. Row-level and aggregate calcs that read parameters, directly or through other calcs,
are translated in tile SQL with the binds: what-if calcs
(`[Sales] * (1 + [Parameters].[Growth])` -> `sales * (1 + :growth)`), thresholds
(`IF SUM([Sales]) >= [Parameters].[Target] THEN ...`), and **measure / dimension
switches** (`CASE [Parameters].[Metric] WHEN 'Sales' THEN SUM([Sales]) ... END` ->
`case when :metric = 'Sales' then sum(sales) ... end`). Date parameters are cast
(`cast(:start_date as date)`; datetime -> `timestamp`), so they work in date functions
and comparisons, and `DATETRUNC('week', [Parameters].[Start])` follows the Week start.
The semantic layer has no parameters, so each such calc is a TODO metric or dimension: pin
the current value, or for a switch make one metric (dimension) per value; the TODO lists
what each value picks (`'Sales' -> metric sales; 'Profit' -> ...`). A parameter calc with an
LOD, a table calculation or ATTR() stays TODO. Tableau snaps a range parameter to its step
(a date range with a 1-month step from 2024-01-01 turns 2024-12-31 into 2024-12-01); the
notebook takes any value, so keep to the comment's step for the same numbers. Parameters
inside custom SQL (`<[Parameters].[Year]>`) -> `:year` binds (write them by hand for now).

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

**Warehouse dialect**: tile, LOD model and semantic SQL is written for the warehouse type
in `unshackle.toml` (`[datasources.warehouse] type`), else the one the connection classes
report (none for extract-only or mixed connections: set the type and re-run `build`).
Postgres, Redshift, Snowflake, Databricks, BigQuery and DuckDB read it as written except:
BigQuery and Databricks quote names with backticks and escape strings with `\`; BigQuery
writes `date_trunc(x, month)` (`week(monday)` for weeks) and maps `extract` parts to the
Postgres numbering (`dow` 0 = Sunday, ISO weeks); Databricks `dow` is shifted the same way;
Postgres `median(x)` is `percentile_cont(0.5) within group (order by x)`; Redshift averages
and divides in double precision (`avg(cast(x as double precision))`,
`cast(1.0 as double precision) * a / nullif(b, 0)`), because its `avg()` of integers and
decimals and its decimal division truncate to the input's scale, while Tableau computes
them as floats. `is not distinct from` (NULL keys of FIXED/EXCLUDE joins) runs on all of
them. A tile BigQuery has no form for (`MEDIAN`) keeps its SQL with a TODO. Custom SQL is
left as written. Other warehouse types (SQL Server, Oracle, MySQL) get a warning: translate
the SQL by hand.

**Initial SQL and connection options**: a connection's `one-time-sql` (Initial SQL) and
`odbc-connect-string-extras` are not part of the migrated SQL. unshackle warns per data
source and copies them into the knowledge file. Run the Initial SQL in the warehouse session
before the migrated queries (a connect hook on the notebooks' engine, dbt `on-run-start`) or
fold its effect into the models; `[TableauServerUser]`, `[WorkbookName]` and parameters in it
are filled in by Tableau per viewer, so replace them (per-user filtering:
`skills/translate-permissions`).

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
  Server; pick the matching IdP attribute. `ISMEMBEROF('Executives') OR ...` ->
  `'Executives' in ({{ user.groups }}) or ...`, a rule for everyone (`groups: ['*']`):
  confirm your platform expands `{{ user.groups }}` to a SQL list of the user's group
  names and that they match the Tableau group names (or split the rule by group).
  Calcs used as datasource filters are marked `(datasource filter)`; others only
  applied where a worksheet filtered on them.
- A datasource filter applies to the joined rows, so each inner-joined table gets its
  own rule through the join keys: `account_id in (select account_id from
  public.accounts where <predicate>)` on orders, and on from there. Left joins and joins
  not on a key are listed in the TODO: add those rules by hand.
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
  (filter by field caption; `vf_<parameter caption>=1000` sets a parameter);
  `maxAge=1` bypasses the cache. Pin dates on both sides. Send `Accept: */*`: Tableau
  Cloud answers `406` to `Accept: text/csv`.
- Dates and numbers come back formatted (`January 2025`, `"3,794,927.81"`); `unshackle
  verify` reads both, so alias the target columns to the CSV header and compare.
- Headers are display names: an uncaptioned field keeps its column name
  (`paper_type`), a captioned one its caption (`Revenue`), a date part reads `Month of
  purchased_at`.
- For calculated fields no sheet shows, query the published datasource with VizQL Data
  Service (`POST /api/v1/vizql-data-service/query-datasource`, fields by caption,
  `"function": "SUM"`): it returns JSON rows to save as a CSV.
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
- Joined datasources (`<relation type='join'>`) give one model per table, related by
  foreign entities when the join is on the other table's key. Ossie and MetricFlow
  consumers usually read a relationship as a left join, while a Tableau inner join drops
  unmatched rows and repeats one-side rows per match: the models' descriptions say so,
  and tiles keep the join. Joins on several columns or on non-key columns get no
  relationship (unshackle warns); new object-model relationships are not read yet. Join
  columns with the same name get a table suffix in Tableau (`[account_id (accounts)]`).
- Local names vs captions: formulas reference local names (`[Calculation_123]`,
  sometimes `[sales]` with caption `Sales`); unshackle resolves both.
- Hidden fields are still migrated (they may feed calcs); `hidden: true` columns get
  `visible: false` in the catalog.
- Stories and web/image zones are not migrated (warning in the report).
- REST rate limits are not published; unshackle stays at 120 requests/min. Large sites:
  run `extract --only workbook:<luid>` for the inventory's top workbooks first.
