---
name: source-sigma
description: Use when migrating Sigma Computing (data models, workbooks, metrics, controls, column-level security, team grants) with unshackle - OAuth client credentials per Sigma cloud, what `unshackle build sigma` translates from the data model and workbook code representations, how to finish Sigma-formula TODOs (Lookup, Rollup, window, passthrough), and how to prove parity with Sigma's generated SQL.
---

# Sigma source

## Access

| Variable | Value |
| --- | --- |
| `SIGMA_API_URL` | API host of your Sigma cloud, no path: `https://api.sigmacomputing.com` (GCP), `https://aws-api.sigmacomputing.com` (AWS US), `https://api.us.azure.sigmacomputing.com`, `https://api.eu.aws.sigmacomputing.com`, `https://api.ca.aws.sigmacomputing.com`, ... (Administration > Developer access shows it) |
| `SIGMA_CLIENT_ID`, `SIGMA_CLIENT_SECRET` | Administration > Developer access > Create new: **REST API** client credentials |

- Create the credentials as (or for) an **Admin** with access to every folder and
  workspace. Credentials act as their owner: anything the owner can't open is missing,
  and lands in `migration/raw/sigma/_errors.json` and the report.
- Auth: `POST /v2/auth/token`, form-encoded `grant_type=client_credentials`,
  `client_id`, `client_secret` -> `access_token` (1 hour). unshackle refreshes before
  expiry and once after a `401`. Don't hammer the token endpoint (about 1/second).
- Rate: Sigma documents ~400 requests/minute for exports; unshackle stays at 300/min.
- `GET /v2/workbooks/{id}?includeContents=true` (the workbook code representation) is
  a public beta since 2026-09-16. If it fails or returns no `contents`, unshackle rebuilds
  a thinner document from `/pages`, `/pages/{pageId}/elements` and
  `/elements/{elementId}/columns` (no axes, no layout, no controls; encodings guessed).

## Commands

```
unshackle inventory sigma            # data models and workbooks (no view counts: see Usage)
unshackle extract sigma [--only <workbookId|dataModelId|name>]
unshackle build sigma
unshackle todo
```

Raw files under `migration/raw/sigma/`:

| File | From |
| --- | --- |
| `data_models/<id>/spec.json` | `GET /v2/dataModels/{id}/spec?format=json` (pages -> elements -> columns, metrics, relationships) |
| `workbooks/<id>/workbook.json` | `GET /v2/workbooks/{id}?includeContents=true` minus `contents` |
| `workbooks/<id>/contents.json` | the `contents` block: flat `elements`, `pages`, XML `layout` (or the legacy rebuild, `"_unshackle_source": "legacy-endpoints"`) |
| `workbooks/<id>/queries.json`, `sql/<elementId>.sql` | `GET /v2/workbooks/{id}/queries` (all elements, `sql` or `error`), else `GET /v2/workbooks/{id}/elements/{elementId}/query` |
| `workbooks/<id>/grants.json`, `teams.json`, `members.json` | `GET /v2/workbooks/{id}/grants`, `GET /v2/teams`, `GET /v2/members` (only id -> name/email kept) |
| `_errors.json` | everything that failed; each becomes a warning |

## Manual import

Not supported. Sigma's downloads are data (CSV, Excel, PDF), not workbook or data
model definitions, so `unshackle import sigma` stops with a message pointing at
`extract`. Ask an admin to create REST API client credentials (see Access).

## What is deterministic

| Sigma | Becomes |
| --- | --- |
| data model element with `source.kind: warehouse-table` (`path: [DB, SCHEMA, TABLE]`) | `catalog/` table + semantic model on `db.schema.table`. Column physical names come from the `inode-.../COLUMN` id; the Sigma display name ("Amount") is the label |
| unnamed element | model named after the table, `fct_`/`dim_` dropped (`DIM_CUSTOMERS` -> `customers`) |
| `source.kind: sql` (custom SQL) | `queries/<name>-sigma-custom-sql.sql` with TODO to materialize, semantic model on `sigma_<name>` |
| `source.kind: table` (child element) | semantic model on the parent's table; its filters/groupings are **not** applied (warning) |
| `source.kind: data-model` (`dataModelId`, `elementId`) | semantic model on the referenced element's table; its columns resolve through that element (calculated columns included). Data models are parsed in dependency order. If the referenced data model was not extracted: warning, element skipped (`extract --only <dataModelId>`) |
| `source.kind: union` (`sources`, `matches`) | `queries/<name>-sigma-union.sql`: `UNION ALL` (Sigma keeps duplicates) of each source's matched columns, aliased to the output names, with a TODO to materialize it; semantic model on `sigma_<name>`. Sources can be warehouse tables, elements of this data model or of another extracted one |
| `source.kind: join` | the join's key pair becomes a foreign entity between the joined elements; the join element's own calcs are warned |
| column `[TABLE/Col]` | dimension; `DateTrunc("month", [x])` -> time dimension at that grain; datetime format/type -> time (day) |
| calculated column (`[Amount] - [Discount]`, `If(...)`, `Upper`, `Coalesce`, `Round`, `DateTrunc`, `&`) | dimension `expr` (`CASE WHEN ... END`, `coalesce`, `date_trunc`, `||`) |
| element `relationships` (`targetElementId`, one key pair) | foreign entity on the source, primary entity on the target (target key is unique) |
| metric `Sum/Count/CountDistinct/Avg/Min/Max/Median([col])` | measure + simple metric (Sigma metrics can't use window or join functions, so most map) |
| `SumIf/CountIf/AvgIf/MinIf/MaxIf/CountDistinctIf(x, cond)` | simple metric with `filter: "{{ Dimension('order__status') }} = 'paid'"` |
| `[Revenue] / [Orders]`, `Sum([a]) / Sum([b])` | ratio metric (helper simple metrics are added for bare aggregates) |
| `[Revenue] - Sum([Discount])` | derived metric over metrics (helper metric for the aggregate) |
| `CumulativeSum(Sum([x]))` | cumulative metric + TODO to confirm window / grain |
| column `[Region] = CurrentUserAttributeText("region")` + element filter on it | row filter `region = {{ user.region }}` (TODO if no element filter enforces it) |
| `columnSecurity` / column `security` with `allowedTeams`/`allowedUsers` | Cedar `forbid query` on the Column unless in those groups/users |
| workbook grant `view` / `explore` / `edit` for a team or member | Cedar `permit view` (+ `edit`) on the notebook; team -> `group:<team name>`, member -> `user:<email>` |
| workbook element `kpi-chart` / `bar-chart` / `line-chart` / `area-chart` / `scatter-chart` / `pie-chart` / `donut-chart` / `table` / `pivot-table` / `text` | KPI / bar / line / area / point / arc / arc / table / table (TODO pivot) / text tile |
| `xAxis.columnId`, `yAxis.columnIds`, `color.columnId`, `value.columnId`, `orientation: horizontal` | encodings (first y only; horizontal bars swap x/y; `color: {by: single}` ignored) |
| `control` (`date-range`, `list`, `slider`, `text`, ...) | dashboard filter with its default (`mode: last, value: 12, unit: month` -> `'last 12 month'`); every element it targets gets a TODO |
| `layout` XML (`<Page>`, `<Container>`, `<Element gridColumn gridRow>`) | cell order (containers resolved, `unshackle.layout` metadata); each visible page gets a `## Page` markdown cell; `visibility: hidden` pages are skipped |

Tile SQL is **Sigma's own generated SQL** (`source_config.source_sql: true`), wrapped in
`select "Display Name" as display_name ... from (...) sigma_q` so Altair fields are
snake_case; the axis keeps the Sigma name as `title`. Metric names are snake_case of
the Sigma metric name; labels keep the original.

## Finishing TODOs

Sigma formulas are spreadsheet-style with `[Element/Column]` references. Map them:

| Sigma | SQL / semantic model |
| --- | --- |
| `Lookup([Customers/Name], [Customer Id], [Customers/Customer Id])` | drop the column; use `customer__name` through the `customer` entity (relationship already migrated). If there is no relationship, add a foreign entity for the key |
| `Rollup(Sum([Items/Amount]), [Order Id], [Items/Order Id])` | a measure on the child model (`items.amount`, sum) queried by `order`; or a SQL model that pre-aggregates items per order |
| `CumulativeSum(x)`, `MovingAvg(x, 6, 0)`, `Rank(x)`, `Lag(x)`, `PercentOfTotal(x)` | tile SQL window: `sum(x) over (order by d)`, `avg(x) over (order by d rows between 6 preceding and current row)`, `rank() over (order by x desc)`, `lag(x) over (order by d)`, `x / sum(x) over ()`; a `cumulative` metric only when it runs over time with no partition |
| `DateDiff("day", [a], [b])`, `DateAdd("month", 1, [d])`, `DatePart("dow", [d])` | `datediff('day', a, b)` / `dateadd(month, 1, d)` / `extract(dow from d)` in the warehouse dialect |
| `Contains([s], "x")`, `StartsWith`, `Left/Right/Mid`, `SplitPart`, `RegexpMatch` | `s like '%x%'`, `left(s, n)`, `substr(s, i, n)`, `split_part(s, '-', 1)`, `regexp_like(s, 'x')` |
| `CallText("MD5", [x])`, `CallNumber(...)`, `AggNumber(...)` (passthrough) | write the warehouse function directly (`md5(x)`); `Agg*` -> a measure with that aggregate `expr` |
| `Zn([x])`, `IfNull([x], 0)`, `Coalesce(...)` | `coalesce(x, 0)` (already automatic) |
| `CountIf([x] > 0)` with no column | `count` of the primary key with a metric filter |
| `CurrentUserInTeam("Admins")` in an RLS column | `groups:` on the row filter (Admins unfiltered = leave them out of `groups`, list the others) |
| `CurrentUserEmail()` | `{{ user.email }}` in the row filter predicate |
| user attributes on column security | map attribute values to IdP groups (translate-permissions) |

Union TODOs: the union is left as a commented copy of its Sigma source when a matched column is
missing in a source, a source element has its own filters/groupings (its rows differ from the
table), a source is custom SQL or CSV, or the union carries options unshackle doesn't know. Write the
`UNION ALL` by hand (check the element's rows in Sigma) and materialize it under the name the TODO gives.

Dashboard TODOs:

- **Controls**: Sigma bakes the control's value at extract time into the SQL (e.g.
  `where "REGION" in ('EMEA')`). Replace the literal with the bind the TODO names
  (`and (:region is null or "REGION" = :region)`) and set a real default in the parameters cell
  (`'last 12 month'` is not bindable: use two keys `order_date_from`/`_to`).
- **Pivot tables**: SQL is long format; `df.pivot_table(...)` in the tile's chart cell.
- **Maps, funnels, sankey, waterfall, combo, gauge**: no Altair mark; pick a bar/line or
  leave `return None` with a link. `/queries` returns an `error` for some of these;
  write the SQL from the semantic layer.
- **Guessed encodings** (legacy rebuild): check against the Sigma screenshot.
- Prefer replacing Sigma SQL with semantic-layer SQL (same measure `expr`/`agg`) once
  parity passes: Sigma SQL has `Q1` CTEs and `limit 25000` you don't want long term.

## Permissions

- **Column-level security** is in the data model (allowlist of users/teams, or a user
  attribute). The spec shape for it is not documented; unshackle reads `columnSecurity`
  on the element and `security` on a column. **Check the Sigma UI** (element >
  column > Column-level security) for every sensitive column and add the `forbid`
  by hand if unshackle emitted none.
- **Row-level security** is a calculated column using `CurrentUserAttributeText()`,
  `CurrentUserEmail()` or `CurrentUserInTeam()` plus an element filter on it. User
  attributes must exist as IdP attributes in your platform (`{{ user.region }}`).
- **Grants**: workbook `view`/`explore` -> view; `edit` -> view + edit. Folder and
  workspace grants are inherited in Sigma; unshackle only exports the workbook's direct
  grants (`GET /v2/workspaces/{id}/grants` covers workspaces if you need them).
  Connection access (`/v2/connections/{id}/grants`) -> `query` on the Datasource.
- Teams map to IdP groups by name; confirm them (TODO on each permit).

## Parity

Sigma's generated SQL is the oracle. It is already the tile SQL, and the raw copy is
`migration/raw/sigma/workbooks/<id>/sql/<elementId>.sql`. Re-fetch with
`GET /v2/workbooks/{workbookId}/elements/{elementId}/query` after changing a control.

```yaml
name: Executive Sales - Revenue by Month
source:
  sql: |   # raw Sigma SQL, window pinned, aliased like the tile
    select "Month of Created At" as month_of_created_at, "Region" as region, "Revenue" as revenue
    from (<contents of sql/el-rev-month.sql with the date literal pinned>) s
target:
  notebook: notebooks/executive-sales.ipynb
  tile: revenue_by_month
keys: [month_of_created_at, region]
```

`unshackle verify` lower-cases but does not snake_case column names, so alias the raw SQL
as above (or compare against a CSV from Sigma: element menu > Download > CSV). Pin
relative controls on both sides. Numbers straight from Sigma: `POST
/v2/workbooks/{id}/export` (async; poll `/v2/query/{queryId}/download`), which counts
against the ~400/min export limit.

## Usage

The REST API has no view counts. Rank workbooks by usage from the **audit logs**
(Administration > Audit logs, or the Sigma audit log connection written to your
warehouse: count workbook view events per workbook id over the last 90 days), then
`unshackle extract sigma --only <id> --only <id>`.

## Alternatives

- **Workbooks/data models as code UI**: File > "Download code representation" gives the
  same YAML as `includeContents` / `spec?format=yaml`; drop it into
  `migration/raw/sigma/workbooks/<id>/contents.yaml` (or `data_models/<id>/spec.yaml`)
  when an API key isn't available.
- **An MCP server or CLI wrapping the Sigma REST API** (community or in-house): handy for
  an agent to list workbooks, read element SQL and run spot checks; it sees exactly what
  its API credentials see, so the same admin-credentials rule applies.
- `GET /v2/workbooks/{id}/elements/{elementId}/columns` gives column formulas for one
  element when `contents` is unavailable.

## Gotchas

- Formulas refer to elements by **display name** (`[Orders/Amount]`); renamed elements
  in workbooks break the match with the data model. Check TODOs that say "references
  another element".
- Snowflake identifiers: Sigma quotes upper-case names (`"AMOUNT_USD"`); unshackle lower-
  cases them in semantic model expressions (unquoted = case-insensitive). Mixed-case columns
  need quotes by hand.
- Custom SQL elements and CSV uploads have no table: materialize them first.
- `limit 25000` in Sigma SQL is Sigma's row cap, not business logic: drop it for
  aggregates, keep a limit for detail tables.
- Hidden pages usually hold source elements; their SQL is extracted but no tiles are
  built.
