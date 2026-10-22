---
name: source-omni
description: Use when migrating Omni (omni.co) shared models (views, topics, relationships, access filters and grants, AI context), workbooks and dashboards with unshackle - Org API key and rate limit, model YAML and dashboard export, what `unshackle build omni` translates, how to finish Omni measure and Excel-style table-calculation TODOs, and parity with Omni's planOnly SQL.
---

# Omni source

## Access

| Variable | Value |
| --- | --- |
| `OMNI_URL` | `https://<org>.omniapp.co` (or your custom domain; no `/api`) |
| `OMNI_API_KEY` | **Organization API key** (Admin > API keys), sent as `Authorization: Bearer <key>` |
| `OMNI_MODEL_ID` | optional: the model to export; default = every `SHARED` model |

- Use an **Org API key**, not a personal access token: dashboard export
  (`GET /api/unstable/documents/{id}/export`, `POST /api/v1/content/export-dashboard`)
  requires it, and a PAT only sees its owner's documents.
- Omni's API limit is **60 requests/minute**; unshackle paces itself at exactly that. A
  large org (hundreds of documents x queries) takes a while: use `--only` for a first
  pass. 429s are retried with `Retry-After`.
- `--only <modelId>` limits models; `--only <documentId|name>` limits documents.

## Commands

```
unshackle inventory omni             # models (kind), dashboards and workbooks, views / last viewed
unshackle extract omni [--only <documentId> --only <modelId>]
unshackle build omni
unshackle todo
```

Raw files under `migration/raw/omni/`:

| File | From |
| --- | --- |
| `models/_index.json` | `GET /api/v1/models` (cursor paging: `pageInfo.nextCursor`) |
| `models/<modelId>/<file>.yaml` | `GET /api/v1/models/{id}/yaml?fullyResolved=true`: `model`, `relationships`, `<schema>/<view>.view`, `<topic>.topic` (folders kept) |
| `models/<modelId>/sample_queries/<topic>__<slug>.sql` | topic `sample_queries` compiled with `POST /api/v1/query/run` `{query, planOnly: true}` |
| `documents/_index.json` | `GET /api/v1/documents` |
| `documents/<id>/queries.json` | `GET /api/v1/documents/{id}/queries` (every tab's query JSON) |
| `documents/<id>/sql/<queryId>.sql` | `userEditedSQL` for SQL tabs, else planOnly SQL of the query |
| `documents/<id>/export.json` | dashboard export (layout, tiles/`visConfig`, filters, markdown) |
| `_errors.json` | failures, turned into warnings |

## Manual import (no API access)

Without an Org API key, import the model's YAML:

```
unshackle import omni path/to/model-folder     # one folder (or .zip) per model
unshackle build omni
```

- If the model uses Omni's Git sync, clone that repository and import the folder that
  holds `model.yaml`, `relationships.yaml`, `<schema>/<view>.view.yaml` and
  `<topic>.topic.yaml`. Without Git sync, copy those files out of the model IDE into
  a folder with the same names and layout.
- The folder name becomes the model id. Views, topics, relationships and access
  grants come out as they do from `extract`.
- Missing: dashboards and workbooks (Omni documents only come from the API), compiled
  SQL for topic sample queries (left as TODO), view counts and links.

## What is deterministic

| Omni | Becomes |
| --- | --- |
| view `table_name`, `schema`, `catalog` | `catalog/` table + semantic model (`node_relation`) |
| dimension `sql: ${TABLE}."COL"` / `${other}` | dimension `expr` (refs inlined); timestamps/`timeframes`/`convert_tz` -> time (day) |
| `primary_key: true` | primary entity named after the view |
| `relationships.yaml` (`join_from_view`, `join_to_view`, `on_sql: ${a.x} = ${b.y}`, `relationship_type`) | foreign entity on the many side; `join_to_view_as` aliases warned |
| measure `aggregate_type: sum/count/count_distinct/average/min/max/median/percentile` | measure + simple metric |
| measure `filters: {status: {is: Complete}}`, `not`, `greater_than`, `contains`, `starts_with`, `between`, `is: null` | metric `filter` with `{{ Dimension('order_items__status') }}` |
| measure with no `aggregate_type`: `${total_margin} / ${total_revenue}` | ratio metric; other arithmetic over measures -> derived |
| model `ai_context` | `knowledge/<model>-model-context.md` (`context: always`) |
| topic (`base_view`, `joins`, `description`, `ai_context`) | `knowledge/<topic>-topic.md` |
| field `synonyms`, `ai_context` | `knowledge/<view>-field-notes.md` |
| topic `access_filters` `{field, user_attribute, values_for_unfiltered}` | row filter `<col> = {{ user.<attr> }}` (TODO for the unfiltered values) |
| `access_grants` + `required_access_grants` on topic / field | Cedar `forbid query` on Table / Column unless `Group::"<attr>=<value>"` (TODO: map to IdP groups) |
| topic `sample_queries` | verified queries (prompt as name) with their planOnly SQL |
| dashboard tile `visConfig.chartType` bar/column/line/area/scatter/pie/donut, KPI/summary value, table/pivot, markdown | bar/line/area/point/arc, KPI, table, text tiles; first dimension -> x, first measure -> y, pivot (or 2nd dimension) -> color |
| dashboard `layouts.lg` grid, `filterConfig` | cell order (`unshackle.layout` metadata), parameters cell |
| workbook tabs not on the dashboard, documents without a dashboard | verified queries (their SQL) |

Tile SQL is Omni's **planOnly SQL** (what Omni would run). Omni names result columns by
field ref (`"order_items.created_at[month]"`); unshackle wraps it as
`SELECT "order_items.created_at[month]" AS order_items_created_at_month ... FROM (...) AS
omni_query` so Altair fields are plain.

## Finishing TODOs

- **Measures with `aggregate_type: list`, `sum_distinct_on`, `average_distinct_on`**:
  `list` has no MetricFlow agg (verified query); distinct-on -> SQL model at the key
  grain, then `sum` / `average`.
- **Measures whose `sql` aggregates raw columns** (`SUM(${sale_price}) / COUNT(...)`):
  split into simple measures and a ratio.
- **Cross-view refs in a dimension** (`${users.country}` in `order_items`): move it to
  the owning view; MetricFlow joins through entities.
- **Filters on relative dates** ("last 30 days" style measure filters): no stable
  MetricFlow equivalent; `{{ TimeDimension(...) }} >= current_date - 30`, or constrain at
  query time.
- **Views defined by `sql:`** (derived views): materialize as a warehouse view / dbt model
  and point `node_relation` at it.
- **Custom visuals (Vega-Lite `spec`)**: `alt.Chart.from_dict(spec)` with the tile's
  DataFrame as data (single view only; drop layers/facets).
- **Multiple measures on one chart / combo**: fold in SQL or one tile per measure.

### Excel-style table calculations

Omni workbook calculations are spreadsheet formulas over the **result grid**: columns
are letters in field order (A = first field), rows are result rows (1 = first row), and
references are relative like Excel. unshackle flags every `calculations` entry; rewrite each
as a column in the tile SQL (window functions over the query result, ordered the way the
grid was sorted):

| Omni formula (in row 2) | Means | SQL |
| --- | --- | --- |
| `=B2/C2` | row-level ratio | `b / nullif(c, 0)` |
| `=B2-B1` | change vs previous row | `b - lag(b) over (order by a)` |
| `=B2/B1-1` | % change vs previous row | `b / nullif(lag(b) over (order by a), 0) - 1` |
| `=SUM(B$1:B2)` | running total | `sum(b) over (order by a rows unbounded preceding)` |
| `=B2/SUM(B:B)` | percent of total | `b / sum(b) over ()` |
| `=AVERAGE(B1:B7)` (sliding) | 7-row moving average | `avg(b) over (order by a rows between 6 preceding and current row)` |
| `=RANK(B2, B:B)` | rank | `rank() over (order by b desc)` |
| `=OFFSET(B2, -12, 0)` | same period last year (monthly rows) | `lag(b, 12) over (order by a)` - or a derived metric with `offset_window: 1 year` |
| `=IF(B2>1000, "High", "Low")` | bucket | `case when b > 1000 then 'High' else 'Low' end` |
| `=IFERROR(B2/C2, 0)` | safe divide | `coalesce(b / nullif(c, 0), 0)` |
| `=ROUND(B2, 2)`, `=TEXT(A2, "yyyy-mm")` | formatting | `round(b, 2)`; formatting belongs in Altair `format=` |
| `=MATCH`/`VLOOKUP` across tabs | lookup | a join in SQL (usually a dimension through an entity) |

With pivots, a formula over pivoted columns repeats per pivot value: partition the
window by the pivot column (`over (partition by status order by month)`). If a
calculation is used on several dashboards, promote it to a metric (cumulative / derived
with `offset_window`) instead of repeating the window.

## Permissions

| Omni | unshackle |
| --- | --- |
| topic `access_filters` + user attributes | row filter (done); `values_for_unfiltered` users -> leave them out of `groups` |
| `access_grants` / `required_access_grants` | `forbid query` unless group (done, with attr=value placeholder groups) |
| `always_where_sql` / `default_filters` on topics (not translated automatically) | by hand: row filter for `*` if it's security; otherwise a metric/tile filter |
| document / folder roles (viewer, editor, manager) | `view` / `edit` on the notebook (read them in Omni's UI or the documents API) |
| connection roles (querier, restricted querier) | `query` on the Datasource / Tables |

## Parity

planOnly SQL is the oracle: `POST /api/v1/query/run` with `{"query": <queryJson>,
"planOnly": true}` returns the SQL without running it. The query JSON of every tab is in
`migration/raw/omni/documents/<id>/queries.json`.

```yaml
name: Sales Overview - Revenue by month
source:
  sql: "<documents/<id>/sql/<queryId>.sql wrapped with the same AS aliases, dates pinned>"
target:
  notebook: notebooks/sales-overview.ipynb
  tile: revenue_by_month
keys: [order_items_created_at_month]
```

Pin the query's relative date filters in the query JSON before compiling, or edit the
SQL literals on both sides. Numbers from Omni itself: `POST /api/v1/query/run` without
`planOnly` (returns Arrow/JSON; counts against the 60/min limit) or download the tile.

**Omni MCP server** (Omni's hosted MCP, `runQuery` / pick model / pick topic tools): ask
the same question against the topic and compare with `mf query` - good for spot checks
of metrics by an agent; it runs real queries, so pin dates.

## Alternatives

- **Omni Git integration**: if the shared model is Git-synced, the repo already has the
  `.view` / `.topic` / `relationships` YAML; copy it into
  `migration/raw/omni/models/<modelId>/` and run `unshackle build omni` (no API needed for
  the semantic layer; dashboards still need export).
- **Model YAML API directly**: `GET /api/v1/models/{id}/yaml` without `fullyResolved`
  returns the files as authored (extends unresolved); unshackle always asks for
  `fullyResolved=true`, so compare the two if a field looks wrong.
- **Omni MCP server**: for reading topics/fields and running queries during
  verification (see Parity); it does not export dashboards.

## Gotchas

- Omni views often point at Snowflake-quoted columns (`${TABLE}."SALE_PRICE"`); unshackle
  keeps the quotes in `expr` (case-sensitive). Keep them unless the warehouse column is
  lower-case.
- Branch and workbook models (`modelKind: BRANCH`, `WORKBOOK`) are skipped by default:
  workbook-level fields (created in a workbook, not promoted) live in those models; set
  `OMNI_MODEL_ID` or `--only <modelId>` to pull one, or promote the fields in Omni first.
- `extends` / `hidden` / `ignored` fields are resolved by `fullyResolved=true`;
  hidden fields become hidden catalog columns.
- `GET /api/unstable/...` export is an unstable API; unshackle falls back to
  `POST /api/v1/content/export-dashboard`. If both fail, tiles come from the query list
  as verified queries only.
