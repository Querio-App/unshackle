---
name: source-qlik
description: Use when migrating Qlik Sense (Qlik Cloud apps - load script, master dimensions and measures, set analysis, variables, sheets, bookmarks, Section Access) with unshackle - OAuth client or API key access, the engine (QIX) and REST calls extract makes, what `unshackle build qlik` translates, rebuilding the load script in the warehouse, and parity through Qlik's own hypercubes.
---

# Qlik Sense source

A Qlik app is a load script (an ETL language that builds an in-memory associative
model) plus master items, sheets and Section Access. unshackle reads the app through
the engine API (QIX, JSON-RPC over a WebSocket, opened without data) and a few REST
calls, saves it in the layout `qlik app unbuild` writes, and translates what has one
meaning in SQL. Tested against Qlik Cloud (engine 12.2969).

**The load script is the migration's real work.** Qlik apps usually transform data
in the script (resident loads, joins, `GROUP BY`, mapping, QVD layers written by other
apps). unshackle only maps plain loads to warehouse tables; everything else keeps the
statement and a TODO, and the whole script is copied to `knowledge/` so you can
rebuild it in the warehouse (dbt or SQL models) first.

## Access

| Variable | Value |
| --- | --- |
| `QLIK_TENANT_URL` | `https://<tenant>.<region>.qlikcloud.com` |
| `QLIK_CLIENT_ID` / `QLIK_CLIENT_SECRET` | an OAuth **machine-to-machine** client; exchanged at `POST /oauth/token` (`grant_type: client_credentials`, scope `user_default`) |
| `QLIK_API_KEY` | instead of an OAuth client: sent as `Authorization: Bearer <key>` |

- A tenant admin creates the OAuth client (Administration > OAuth) as a
  machine-to-machine client with the **Trusted** consent method. Without Trusted the
  token request fails with `OAUTH-5`. The client acts as its own bot user
  (`qlikbot\<id>`); give that user the roles and space memberships it needs.
- API keys need a role with the **Manage API keys** permission (Qlik retired the
  Developer role; create a custom role) and API keys enabled for the tenant.
- The extracting user must be able to **open** each app: space membership with at
  least view rights (a TenantAdmin sees every shared space), and, for apps with
  Section Access, a row in the access table. Reading the script needs `ACCESS = ADMIN`
  there.
- Extract only reads. It opens each app twice: once without data for the definitions,
  once with data for `GetTablesAndKeys` (per-table key statistics). If the second open
  is refused, it saves the REST data metadata instead, which recognises fewer keys.
- Each engine session uses its own `/identity/<id>` so it never collides with a
  session the same user has open in another mode (`App already open in different
  mode`, code 1009).

## Commands

The engine API needs the `qlik` extra (websockets): `uv tool install "unshackle[run,qlik]"`.

```
unshackle inventory qlik                 # apps, with space, item views, last reload, Section Access flag
unshackle extract qlik [--only app:<id>]
unshackle build qlik
unshackle import qlik <folder>           # a `qlik app unbuild` folder, or a raw extract folder
```

Raw files under `migration/raw/qlik/apps/<app id>/`: `script.qvs`,
`dimensions.json`, `measures.json`, `variables.json`, `objects/<type>-<title>-<id>.json`
(full property trees of sheets and master visualizations), `app-properties.json`,
`connections.yml` (no passwords), and three extras unbuild doesn't write:
`bookmarks.json`, `tables-and-keys.json` and, as its fallback, `data-metadata.json`.
Plus `apps.json`, `spaces.json`, `tenant.json` and `_errors.json` at the top.

`qlik app unbuild --app <id> --dir <folder>` output imports as is. It has no data
model, so `LOAD *` columns, field types and joins are unknown: add the app's
`GET /api/v1/apps/{id}/data/metadata` response as `data-metadata.json` next to
`script.qvs` before importing.

## What is deterministic

| Qlik | Becomes |
| --- | --- |
| `LOAD f1, f2 AS name, Upper(f3) AS F3 FROM [lib://...file.parquet]` (or CSV, Excel) | semantic model on a table named after the file (warning: land the file there first); renamed fields and row expressions unshackle translates become dimensions with SQL |
| `LOAD ...; SQL SELECT <columns> FROM schema.table;` (preceding LOAD over a plain select) | semantic model on `schema.table` |
| `LOAD *` | the columns from the app's data model |
| shared field names (associations) | a field with a distinct value on every row (`GetTablesAndKeys`) is its table's primary key; tables holding it join many-to-one, written as foreign entities |
| master measure `Sum/Count/Avg/Min/Max([DISTINCT] <row expression>)` on one table | measure + simple metric |
| set modifiers listing values: `{<status={'COMPLETED'}>}`, `{<Year={2025}>}`, `{<Region-={'West'}>}` | metric filter, so the Ossie metric is `SUM(CASE WHEN status = 'COMPLETED' THEN ... END)`; a field in a table the measure's table joins to many-to-one works too |
| unquoted or double-quoted values without wildcards (`{<status={COMPLETED}>}`, `{"done"}`) | `UPPER(col) = 'COMPLETED'`: Qlik matches the whole value ignoring case |
| `{1}`, `{$}`, `F=` (clear) | no filter (they change how selections apply; see filter panes below) |
| `A / B`, arithmetic with numbers (`Sum(x) / $(vTarget)`) | ratio or derived metric; sides that match a master measure reuse it; every division is `1.0 * a / nullif(b, 0)` |
| `$(vName)` with a static definition, script `SET`/`LET` values after the last reload | expanded before translating |
| row functions `If`, `Upper`, `Lower`, `Trim`, `Len`, `Abs`, `Floor`, `Ceil`, `Coalesce`, `Year`, `MonthStart` / `QuarterStart` / `YearStart` / `DayStart` (`DATE_TRUNC`), `IsNull`, `Num`/`Date` (formatting only) | SQL |
| master dimension on one field | label on that dimension; a calculated one (`=MonthStart(purchased_at)`) becomes a dimension (time, month grain) |
| sheet | `notebooks/<sheet>.ipynb`, cells ordered by the sheet grid |
| `barchart` / `linechart` / `piechart` / `scatterplot` / `combochart` / `kpi` / `table` / `pivot-table` | bar (horizontal swaps axes, a second dimension is color + xOffset unless stacked) / line (several measures are layers) / arc / point / layered bar+line / KPI / table tiles |
| chart hypercube | SQL from the measure's table with `LEFT JOIN`s to the dimension tables, `GROUP BY` the dimensions; null dimension values stay a row, as Qlik's `-` |
| filter pane / listbox fields | notebook parameters, bound into the sheet's tiles as `(col = :name OR :name IS NULL)` |
| layout containers | their charts, placed in the container's cell |
| text & image | markdown cell |
| linked master visualizations (`qExtendsId`) | the master's definition |
| Section Access, `LOAD * INLINE` | see below |
| bookmarks, the load script | `knowledge/` (the script without its Section Access rows) |
| connections with `driver=snowflake/postgres/databricks/...` | the warehouse type |

## Section Access

Qlik reduces the **whole associated model**: a user allowed `PAPER_TYPE = COPY` sees
only the products of that type, the orders of those products, the accounts with those
orders, and so on. **ADMIN rows are reduced too** (`ACCESS` only grants script access),
and `*` means every value listed in that column of the access table, not every value
in the data. Users who aren't listed can't open the app (strict exclusion). unshackle
writes that, for inline access tables:

- one row filter per `USERID` / `USER.EMAIL` / `GROUP` and table: the reduction
  field's predicate on its own table (`UPPER(paper_type) IN ('COPY', 'PRINTER')`), and
  on every associated table a key subquery that walks to it
  (`account_id IN (SELECT account_id FROM orders WHERE product_id IN (SELECT ...))`);
  a user's rows are ORed; tables not associated with the field aren't filtered;
- `OMIT` -> Cedar `forbid query` on that column for the user;
- users not listed -> Cedar `forbid view` on the app's notebooks and `forbid query` on
  its tables unless the principal is listed (`*` in USERID/GROUP lets everyone in).

`USERID` values are Qlik user subjects, not emails or IdP groups: every rule carries a
TODO to map them. Access tables loaded from files or databases, and reduction fields
that are keys in several tables, are TODOs. Section Access field names and values are
upper case: the reduction field must exist upper case in the data model
(`Upper(paper_type) AS PAPER_TYPE`).

## What needs a human

- **The load script.** Resident loads, `JOIN` / `KEEP` / `CONCATENATE` (also automatic
  concatenation of loads with the same fields), `WHERE`, `GROUP BY`, `DISTINCT`,
  mapping and `ApplyMap`, `INLINE`, loops and `IF` blocks, `CROSSTABLE`,
  `INTERVALMATCH`, `QUALIFY`: the model keeps the statement and a TODO. Rebuild the
  table as a SQL or dbt model with the same name and fields, then remove the TODO.
  Loads from `SQL SELECT` with a `WHERE`/`JOIN` are also written to `queries/` to
  materialize.
- **QVDs.** QVD files are Qlik's own format, usually written by another Qlik app (an
  extract or transform layer). There's no warehouse table behind them until you land
  one: find the app that writes the QVD (`STORE ... INTO`) and migrate its script
  first. Excel, CSV and Parquet files likewise need a landing step (a warning, not a
  TODO, because the data is the file).
- **Expressions**: `Aggr()`, `TOTAL`, inter-record functions (`Above`, `Below`,
  `RangeSum`, `RowNo`), `P()` / `E()`, search strings (`{"*x*"}`, `{">2020"}`),
  `+=` / `*=` modifiers, set operators, alternate states, `$(=...)` and calculated
  variables, `&` (Qlik treats null as empty), `Month()` (a dual of name and number):
  TODO with the expression kept. Rebuild with window functions or a SQL model
  (skills/translate-metrics).
- **Measures over two tables** (`Sum(quantity * price)` across tables) and
  aggregations of a key field no single table holds completely: Qlik evaluates them
  over the association. TODO.
- **Selections vs set expressions.** A measure with `{<status=...>}`, `{1}` or
  `status=` ignores selections on `status`; a filter pane on `status` filters the
  whole tile SQL. Such tiles get a TODO: move the parameter into the other measures.
- Drill-down and cyclic dimensions (first field only), dimension limits ("top N +
  Others"), KPI comparison measures, maps, extensions, tabbed containers (every tab is
  migrated), dynamic text in text objects.
- Bookmarks are listed in `knowledge/` (the engine layout doesn't carry the selected
  values); rebuild useful ones as parameter presets.
- Usage is Qlik's item views of the app; Qlik has no per-sheet counts.

## Parity

Qlik's engine is the source side. Open the app and read each chart's own hypercube,
selecting values first for a case with a filter:

```python
e = Engine(engine_url(tenant, app), token)            # unshackle.sources.qlik
doc = e.handle(e.call(-1, "OpenDoc", app))
chart = e.handle(e.call(doc, "GetObject", "<chart id>"))
page = e.call(chart, "GetHyperCubeData", "/qHyperCubeDef", [{"qTop": 0, "qLeft": 0, "qWidth": 2, "qHeight": 1000}])
status = e.handle(e.call(doc, "GetField", "status"))
e.call(status, "SelectValues", [{"qText": "COMPLETED"}], False, False)   # then read the chart again
```

Use each cell's `qNum` (not the formatted `qText`); dates are day numbers since
1899-12-30; `qIsNull` cells are the `-` row and compare with SQL `NULL`. For a metric,
create a session object with a one-measure `qHyperCubeDef`. A row filter can be
checked without impersonating anyone: a set expression on the reduction field reduces
the same associated data, so `Sum({<PAPER_TYPE={'COPY','PRINTER'}>} revenue)` must
equal the SQL with that user's predicate. `tests/live/test_qlik_live.py` does all of
this.

## Gotchas

- `SET v = 'COMPLETED';` stores `COMPLETED` without the quotes, so `{$(v)}` is an
  unquoted value and matches ignoring case.
- Qlik's `Sum` over no rows is 0; SQL's is `NULL`.
- The REST data metadata lists the section access tables as `$$SysTable n`, and has
  one distinct count per field, not per table.
- `lib://<space>:DataFiles/<file>` paths name the space; the file name (without
  extension) becomes the table unshackle expects.
- Unlabelled loads take the file's base name (or the SQL table name) as the table name.
