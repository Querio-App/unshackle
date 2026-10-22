---
name: source-powerbi
description: Use when migrating Power BI / Microsoft Fabric (semantic models in TMDL, DAX measures, Power Query M, RLS/OLS roles, PBIR or legacy reports, Copilot instructions) with unshackle - service principal setup, what `unshackle build powerbi` translates, how to finish DAX/M/RLS TODOs, and how to prove parity with executeQueries.
---

# Power BI / Fabric source

## Access

| Variable | Value |
| --- | --- |
| `POWERBI_TENANT_ID` | Entra ID tenant (directory) id |
| `POWERBI_CLIENT_ID`, `POWERBI_CLIENT_SECRET` | app registration (service principal) with a client secret |
| `POWERBI_ACCESS_TOKEN` | alternative to the three above: a pre-obtained bearer token (`az account get-access-token --resource https://analysis.windows.net/powerbi/api`). Used for both APIs unless `POWERBI_FABRIC_ACCESS_TOKEN` is set |
| `POWERBI_WORKSPACES` | optional comma list of workspace ids (or names); default every workspace the principal can see |
| `POWERBI_ADMIN` | `1` to use admin APIs: scanner fallback + usage (ViewReport activity events) |
| `POWERBI_USAGE_DAYS` | days of activity events to read (default and max 30) |

Auth is client credentials: `POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`
with `scope=https://analysis.windows.net/powerbi/api/.default` (Power BI REST) and
`scope=https://api.fabric.microsoft.com/.default` (Fabric REST). No delegated login.

Tenant and workspace setup (a Fabric admin does this once):

- Admin portal > Tenant settings > Developer settings: **Service principals can use
  Fabric APIs** (older tenants: "Allow service principals to use Power BI APIs"),
  scoped to a security group that contains the app.
- Add the service principal to each workspace as **Member** or **Contributor**
  (Viewer is not enough). `getDefinition` needs **write** permission on the item.
- For `POWERBI_ADMIN=1`: Admin API settings > **Service principals can access
  read-only admin APIs**, plus **Enhance admin APIs responses with detailed metadata**
  and **...with DAX and mashup expressions** (enhanced metadata scanning). Without the
  last two the scanner returns no tables or DAX.
- `executeQueries` for parity: **Dataset Execute Queries REST API** tenant setting,
  and Build permission on the dataset.

## Commands

```
unshackle inventory powerbi          # workspaces, semantic models, reports, dashboards (+ views with POWERBI_ADMIN=1)
unshackle extract powerbi [--only report:<id> --only model:<id> --only workspace:<id>]
unshackle build powerbi
unshackle todo
```

`--only report:<id>` also extracts that report's semantic model.

## Manual import (no API access)

Without a service principal, import a **Power BI project** (`.pbip`):

```
unshackle import powerbi "Sales Overview.pbip"     # or the project folder
unshackle build powerbi
```

- In Power BI Desktop, File > Save as > "Power BI project files (*.pbip)". This
  writes `<name>.SemanticModel/` and `<name>.Report/` folders next to the `.pbip`.
  A `.pbix` downloaded from the service can be opened in Desktop and saved this way.
- The semantic model must be **TMDL** (`definition/*.tmdl`). Older projects save
  `model.bim` (TMSL), which unshackle doesn't read: turn on the preview feature
  "Store semantic model using TMDL format" (File > Options and settings > Options >
  Preview features), save again, then import. `import` refuses a model.bim-only model.
- Reports in either format work: PBIR (`definition/pages/...`) or legacy `report.json`.
  A project's report points at its model by path (`byPath` in `definition.pbir`), and
  `build` follows that link.
- `.pbix` / `.pbit` are rejected: the model inside is a binary format.
- `.pbi/` folders (Desktop's local cache, which holds data, and local settings) are
  never imported.
- Missing compared with `extract`: usage, workspace and dataset names and ids (models
  and reports are named after their folders), service links, dashboards (pinned
  tiles), and role membership. RLS role filters are in the TMDL and are kept.

## What is extracted

| Call | Raw file |
| --- | --- |
| `GET /v1.0/myorg/groups` | `workspaces.json` |
| `GET /groups/{ws}/datasets`, `/reports`, `/dashboards` | `workspaces/<ws>/*.json` |
| `POST api.fabric.microsoft.com/v1/workspaces/{ws}/semanticModels/{id}/getDefinition?format=TMDL` | `models/<id>/definition/**/*.tmdl`, `definition.pbism`, `Copilot/**` |
| `POST .../reports/{id}/getDefinition` | `reports/<id>/definition/pages/<page>/page.json`, `.../visuals/<v>/visual.json` (PBIR) or `reports/<id>/report.json` (legacy) |
| `POST /admin/workspaces/getInfo?datasetSchema=True&datasetExpressions=True` -> `scanStatus` -> `scanResult` | `scanner/<scan id>.json` (only for models whose getDefinition failed, with `POWERBI_ADMIN=1`) |
| `GET /admin/activityevents?startDateTime='..'&endDateTime='..'&$filter=Activity eq 'viewreport'` (one UTC day per call) | `usage.json` |
| anything that failed | `_errors.json` (becomes report warnings) |

`getDefinition` is a long-running operation: `202` with `Location` / `x-ms-operation-id`
and `Retry-After`; unshackle polls `GET /v1/operations/{id}` until `Succeeded`, then reads
`GET /v1/operations/{id}/result` and base64-decodes `definition.parts[].payload` to
`<prefix>/<id>/<part path>`.

## What is deterministic

| Power BI | Becomes |
| --- | --- |
| table with an M partition `Sql.Database(...)` + `{[Schema="dbo",Item="X"]}` (also Snowflake/Databricks/BigQuery `{[Name=..,Kind=..]}` navigation, M parameters from `expressions.tmdl`) | `catalog/` table `dbo.X` + semantic model on it |
| `Table.RenameColumns` | reversed, so dimensions point at the physical column |
| `Value.NativeQuery(src, "sql")` / `[Query="sql"]` | `queries/<table>-source.sql` (TODO: materialize as SQL model) + semantic model on a table named after it |
| column | dimension (`dateTime` -> time, day grain); the only date column is `agg_time_dimension`; with several, a TODO lists them |
| calculated column `column X = <DAX>` with same-table refs, `IF`, `SWITCH`, `&`, arithmetic, `YEAR`... | dimension `expr` in SQL; `RELATED` etc. -> TODO |
| relationship `fromColumn: Sales.CustomerKey` -> `toColumn: Customer.CustomerKey` (active) | `customer` primary entity on Customer, foreign entity on Sales |
| `isKey` column | primary entity |
| `SUM/AVERAGE/MIN/MAX/COUNT/COUNTA/DISTINCTCOUNT/MEDIAN(T[c])`, `COUNTROWS(T)` | measure + simple metric (on the model of `T`, whatever the measure's home table) |
| `SUMX/AVERAGEX/MINX/MAXX(T, <row expr on T>)` | measure with `expr` (other tables or `RELATED` -> TODO) |
| `DIVIDE([A], [B])`, `[A] / [B]` | ratio |
| arithmetic over measures (`DIVIDE([A] - [B], [A])`) | derived (`(a - b) / nullif(a, 0)`) |
| `CALCULATE([M], T[c] = "x")`, `IN {..}`, `<>`, `&&`, `\|\|`, `KEEPFILTERS` | simple metric with `filter: "{{ Dimension('t__c') }} = 'x'"` |
| `TOTALYTD/QTD/MTD([M], 'Date'[Date])`, `CALCULATE([M], DATESYTD(..))` | cumulative `grain_to_date` |
| `CALCULATE([M], SAMEPERIODLASTYEAR(..))`, `DATEADD(.., -n, MONTH)` | derived with `offset_window: 1 year` / `n months` |
| role `tablePermission T = [Col] = "x"` | row filter `Col = 'x'` on `T`'s table; `USERPRINCIPALNAME()`/`USERNAME()` -> `{{ user.email }}` |
| role members | `groups` of the row filter + Cedar `permit query` on the datasource |
| OLS `columnPermission X` / table `metadataPermission: none` | Cedar `forbid query` on the Column/Table for the role |
| report page | one notebook `<report>-<page>.py` (pages are separate canvases with their own slicers) |
| visual | tile (see Reports) |
| `Copilot/Instructions/instructions.md` | knowledge (`context: always`) |
| `Copilot/examplePrompts.json` | knowledge (reference) |
| `Copilot/VerifiedAnswers/**` | verified query (SQL built from the answer's visual when it has one; the shape is undocumented, so check it) |

Metric and measure names are snake_case of the DAX names (`Margin %` -> `margin_pct`;
CamelCase column names split: `OrderDate` -> `order_date`). Labels keep the originals.
Measure-only tables (`_Measures`, `Key Measures`) are not models.

## DAX -> MetricFlow / SQL

MetricFlow has no filter context, so first ask what the DAX *returns in the visual*.

- **CALCULATE filter context.** Column predicates become metric `filter:`. A filter on
  a column of the fact table, or of a dimension joined through an entity, is the same
  thing. `FILTER(Sales, Sales[Amount] > 100)` is a row condition: measure
  `expr: case when amount > 100 then amount end`, or a metric filter on a dimension.
  `FILTER(ALL(..), ..)` changes context: see below.
- **ALL / REMOVEFILTERS / ALLEXCEPT / ALLSELECTED** (share of total, % of parent):
  compute in the tile SQL with a window,
  `sum(amount) / sum(sum(amount)) over ()` (ALL),
  `... over (partition by region)` (ALLEXCEPT region). If the number is reused
  everywhere, a SQL model at the fixed grain (`queries/models/`) plus a measure on it.
- **Iterators over related tables** (`SUMX(Sales, Sales[Qty] * RELATED(Product[Price]))`):
  SQL model joining the tables (`select s.*, p.price from sales s join product p ...`),
  then `sum(qty * price)` on it. Same-table iterators are already translated.
- **Time intelligence.** YTD/QTD/MTD -> cumulative `grain_to_date`; rolling
  `DATESINPERIOD(d, MAX(d), -3, MONTH)` -> cumulative `window: 3 months`;
  `SAMEPERIODLASTYEAR`/`DATEADD` -> derived `offset_window`; `PARALLELPERIOD`/
  `PREVIOUSYEAR` -> derived over a metric queried at that period grain. Fiscal
  years (`TOTALYTD(.., "6/30")`) need a fiscal time spine / dimension; otherwise a
  verified query.
- **USERELATIONSHIP** (inactive relationship, often ShipDate): add a second foreign
  entity or time dimension (`ship_date`) and a metric that uses it
  (`agg_time_dimension` on the measure, or query `--group-by metric_time` with
  `ship_date`).
- **VAR / RETURN**: inline the variables, then classify the result as above.
- **SELECTEDVALUE / HASONEVALUE / ISINSCOPE**: these read the visual. Turn the slicer
  into a notebook `:param`, or split tiles by hierarchy level.
- **RANKX / TOPN**: `rank() over (order by ..)` / `order by .. limit n` in tile SQL.
- **LASTNONBLANK / CLOSINGBALANCE* / LASTDATE** (balances): measure with
  `non_additive_dimension: {name: <date>, window_choice: max}`.
- **Calculation groups**: each item applied to each targeted measure is its own
  metric (`sales_ytd`, `sales_py`...); the knowledge file lists the items' DAX.
- **Field parameters** (tables of `NAMEOF(...)`): a notebook parameter that picks the
  column/metric; not a model.
- **Calculated tables** (`partition = calculated`, `CALENDARAUTO()`, `SUMMARIZE`): a SQL
  model; date tables -> the dbt/MetricFlow time spine.

Example:

```yaml
# DAX: Share of Region = DIVIDE([Total Sales], CALCULATE([Total Sales], ALL(Customer[Region])))
# -> not a metric; tile SQL:
select region, sum(amount) as total_sales,
       sum(amount) / sum(sum(amount)) over () as share_of_region
from dbo.FactSales s left join dbo.DimCustomer c on s.CustomerKey = c.CustomerKey
group by 1
```

## Power Query M -> SQL

Tables whose M only navigates (`Schema/Item`, `Name/Kind`) and changes types, renames
or removes columns map straight to the warehouse. Anything else is a warning or a
SQL-model TODO:

| M step | SQL |
| --- | --- |
| `Table.SelectRows(t, each [Status] = "paid")` | `where status = 'paid'` |
| `Table.AddColumn(t, "Margin", each [Amount] - [Cost])` | `amount - cost as margin` |
| `Table.Group(t, {"Customer"}, {{"Total", each List.Sum([Amount])}})` | `group by customer` |
| `Table.NestedJoin` + `Table.ExpandTableColumn` | `left join` |
| `Table.Distinct` | `select distinct` |
| `Table.UnpivotOtherColumns` | `unpivot` / `union all` |
| `Table.ReplaceValue` | `case when` / `coalesce` |

Write the SQL model under `queries/models/<name>.sql` (dbt: `models/unshackle/`) with the
name the TODO gives; the semantic model already points at it. Excel/SharePoint/Web
sources must be loaded into the warehouse first. Entered data (`Table.FromRows`) ->
a seed or a small table.

## RLS and OLS

- Power BI ORs roles and hides *all* rows from a viewer in no role; unshackle row filters
  apply to their `groups` and AND together. When several roles filter one table, write
  one predicate with `or` per group combination, and keep a deny-by-default path for
  users in none of them (translate-permissions skill).
- Role **members are usually assigned in the service** (dataset > Security), not in the
  model. unshackle then uses the role name as the group; replace it with the IdP groups
  assigned there (`GET /groups/{ws}/datasets/{id}/users` lists dataset permissions, not
  role membership; use the Security page, XMLA `$SYSTEM.TMSCHEMA_ROLE_MEMBERSHIPS`, or
  the Power BI Modeling MCP).
- Dynamic RLS through a security table
  (`Customer[Region] IN CALCULATETABLE(VALUES(Sec[Region]), Sec[Email] = USERPRINCIPALNAME())`)
  becomes `region in (select region from security.user_regions where email = {{ user.email }})`.
- OLS -> `forbid query` on the Column/Table for the role's groups; unshackle writes those.
  Check that the column is not also reachable through a calculated column or measure.

## Reports

One notebook per page. Visuals: `clusteredColumnChart`/`columnChart`/stacked -> bar
(category on `x`), `clusteredBarChart`/`barChart`/`funnel` -> horizontal bar (category on
`y`), `lineChart` -> line, `areaChart` -> area, `pieChart`/`donutChart` -> arc,
`scatterChart` -> point, `card`/`cardVisual`/`kpi`/`gauge` -> KPI,
`tableEx`/`multiRowCard`/`pivotTable` -> table (matrix columns -> TODO), `textbox` ->
text tile, `slicer` -> notebook filter. Maps, treemap, waterfall, ribbon and custom
visuals keep their SQL and get a TODO for the chart. Images, shapes and buttons are
dropped. Layout rows come from `position` (visuals whose top is within half a row
height share a row).

Fields: `Column` / `Measure` / `Aggregation` / `HierarchyLevel` (auto date hierarchy
`Month` -> `DATE_TRUNC('month', col)`). Aggregation `Function` is the semantic-query
enum: `0` Sum, `1` Avg, `2` Count (Distinct), `3` Min, `4` Max, `5` Count (non-null),
`6` Median, `7` StdDev, `8` Variance. Legacy `report.json` visuals are read from the
`config` string (`singleVisual.projections` + `prototypeQuery`).

Tile SQL is generated from the measure's DAX (same rules as the metrics, pushed into
`CASE WHEN` for CALCULATE filters) when every field is on the measure's table or on a
table it reaches through one active many-to-one relationship. Visual/page/report
`Categorical` filters become `WHERE` clauses; slicers become `(:p IS NULL OR col = :p)`
(dates: `:p_from` / `:p_to`). TopN, relative-date and measure (Values) filters are TODOs:
`order by .. limit n`, a pinned date window, `having`. Time-intelligence measures have no
single-query SQL: query the metric (`mf query`) or write the window by hand.
Report-level measures (`reportExtensions.json`) are not read; add them to the model.

## Parity

Numbers straight from the model with DAX:

```
POST https://api.powerbi.com/v1.0/myorg/datasets/{datasetId}/executeQueries
{"queries": [{"query": "EVALUATE SUMMARIZECOLUMNS(Customer[Region], FILTER(ALL('Date'[Year]), 'Date'[Year] = 2025), \"total_sales\", [Total Sales])"}],
 "serializerSettings": {"includeNulls": true}}
```

Needs the Execute Queries tenant setting and Build permission; the principal's RLS
applies (`impersonatedUserName` to test a role). Save results as CSV under
`migration/parity/data/` and compare with the tile (`keys: [region]`). Column names
come back as `Customer[Region]` / `[total_sales]`; rename before saving. Other ways:
DAX over the XMLA endpoint (Premium/Fabric capacity), or "Export data" from the
visual. Pin the date window on both sides; import-mode models are only as fresh as
their last refresh (`GET /datasets/{id}/refreshes`).

## Alternatives

- **Power BI Modeling MCP** (`microsoft/powerbi-modeling-mcp`, local): connects to
  Power BI Desktop or a Fabric workspace and exports/edits TMDL, runs DAX; use it when
  the principal can't call getDefinition, then copy the TMDL folder into
  `migration/raw/powerbi/models/<id>/definition/`.
- **Fabric / Power BI remote MCP**: `Get Report Metadata` (pages, visuals, fields)
  and `Execute Query` (DAX) for ad-hoc parity checks from an agent.
- **PBIP from Desktop**: File > Save as > Power BI Project gives the same TMDL + PBIR
  folders; drop them into the raw layout above.

## Gotchas

- **Thin reports**: most reports point at a shared model in another workspace
  (`datasetWorkspaceId`); extract that workspace too or the tiles say "semantic model
  not extracted".
- **DirectQuery / composite / Direct Lake**: DirectQuery M is often native SQL (a
  SQL model TODO). Direct Lake tables read Delta tables in OneLake; point the semantic
  model at the lakehouse SQL endpoint table of the same name.
- **Sensitivity labels with protection** make getDefinition fail
  (`ItemHasProtectedLabel`); the scanner still returns schema and DAX, but without
  `sourceColumn`, relationships or OLS.
- **Pro vs Premium/Fabric**: Fabric item APIs and XMLA need the workspace on a
  capacity (F/P SKU or PPU); in Pro workspaces use the scanner or export PBIP from
  Desktop.
- **PBIR vs legacy**: new reports are PBIR (`definition/pages/..`); old ones return a
  single `report.json` with JSON-in-string `config`. Both parse; PBIR is richer
  (titles, filters). Editing a legacy report in the service converts it.
- **Auto date/time** hierarchies (`LocalDateTable_*`) are Power BI internals: unshackle
  maps them to `DATE_TRUNC` on the source column; turn the option off in the model.
- **Bi-directional relationships** and many-to-many: MetricFlow joins many-to-one;
  check distinct counts that relied on bidirectional filtering.
- **Power BI dashboards** (pinned tiles) are not migrated; their tiles come from
  reports that are.
- **Paginated reports** (`.rdl`) are skipped and listed in `_errors.json`.
