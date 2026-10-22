---
name: source-metabase
description: Use when migrating Metabase (questions, models, metrics, dashboards, collections, data and collection permissions) with unshackle - API key access, what `unshackle build metabase` translates from MBQL and native SQL, template tags and dashboard filters, finishing Metabase metric and chart TODOs, paid-only gaps (row and column security), and parity through Metabase's own CSV export.
---

# Metabase source

Metabase keeps everything in its application database and serves it over the HTTP
API. unshackle reads cards (questions, models, metrics), dashboards, the database
metadata, collections and the permission graphs. Query-builder cards are compiled
to SQL **by Metabase** at extract time (`POST /api/dataset/native`), so tile SQL is
exactly what Metabase runs. Tested against Metabase v0.64.1 (open source); MBQL 4
(before v0.57) and MBQL 5 are both read.

## Access

| Variable | Value |
| --- | --- |
| `METABASE_URL` | `https://metabase.example.com` (no `/api`) |
| `METABASE_API_KEY` | an API key (Admin > Settings > Authentication > API keys), sent as `X-API-Key` |

- Put the key in the **Administrators** group. A key in another group only sees the
  collections and databases that group can see; the permission graphs need admin.
- API keys exist since v0.49. On older versions, create a session
  (`POST /api/session`) and use a key-less proxy, or upgrade first.
- unshackle paces itself at 300 requests/minute. Compiling is one
  `POST /api/dataset/native` per query-builder card (plus one per dashboard tile with
  text filters); it doesn't run queries on the warehouse.
- The API is AGPL server code; unshackle only calls it over HTTP.

## Commands

```
unshackle inventory metabase          # questions, models, metrics, dashboards, collections
unshackle extract metabase [--only dashboard:<id> | --only card:<id> | --only collection:<id>]
unshackle build metabase
unshackle todo
```

`--only dashboard:<id>` pulls every card on it (and cards those reference with
`{{#id}}`); `--only collection:<id>` takes the collection and its sub-collections.

Raw files under `migration/raw/metabase/`:

| File | From |
| --- | --- |
| `instance.json` | URL and version (`GET /api/session/properties`) |
| `collections.json` | `GET /api/collection/tree` |
| `cards/<id>.json` | `GET /api/card/{id}` - `type` (question / model / metric), `display`, `dataset_query`, `visualization_settings`, `result_metadata`, `view_count` |
| `cards/<id>.sql` | `POST /api/dataset/native` (pretty) for query-builder cards |
| `dashboards/<id>.json` | `GET /api/dashboard/{id}` - dashcards (24-column grid), `parameters`, `tabs`, `view_count` |
| `dashboards/<id>/<dashcard>.sql` | the card compiled with the dashboard's text filters set to `'unshackle__<slug>'` |
| `databases/<id>.json` | `GET /api/database/{id}/metadata` (connection details removed) |
| `snippets.json` | `GET /api/native-query-snippet` |
| `permissions/groups.json`, `graph.json`, `collections.json` | `GET /api/permissions/group`, `/api/permissions/graph`, `/api/collection/graph` |
| `permissions/gtap.json` | `GET /api/mt/gtap` (paid plans only) |
| `paid_features.json` | paid endpoints that weren't available, with the HTTP error |
| `_errors.json` | failures (a card Metabase can't compile, for example) |

Fingerprints, creator/editor details and database connection details are dropped.

## What is deterministic

| Metabase | Becomes |
| --- | --- |
| database tables and fields (display names, descriptions, `type/PK`, `type/FK` with `fk_target_field_id`) | `catalog/` tables; semantic models (with primary and foreign entities) for the tables metrics read and their FK targets |
| metric card: one `count / sum / avg / distinct / min / max / median` over a column of its `source-table`, optional filters `= != < > <= >= between is-null not-null and or` on the same table | measure + simple metric with a `filter` (`{{ Dimension('orders__status') }} = 'COMPLETED'`); `!=` keeps NULLs like Metabase (`x <> 'a' OR x IS NULL`) |
| any other metric (custom expression, joined column, segment, multi-stage, built on a model) | TODO metric in `semantic/<table>.ossie.yaml` (`custom_extensions`) with the compiled SQL as `source_expression`, plus `queries/<metric>.sql` (`metric: true`) |
| native question | `queries/<name>.sql`: text/number/date tags -> `:tag` binds (listed in the description with defaults); `[[ ... ]]` blocks kept when their tags have a default, else dropped (noted); field filters `{{x}}` -> `("schema"."table"."col" = :x OR :x IS NULL)` for `=`-type widgets; `{{snippet: x}}` inlined; `{{#id-name}}` -> the card's SQL as a subquery |
| query-builder question not on a dashboard | `queries/<name>.sql` with Metabase's compiled SQL |
| model (native or query builder) | `queries/<name>.sql` (TODO: materialize as a SQL model) + a semantic model named after it, dimensions from `result_metadata` |
| dashboard | `notebooks/<slug>.ipynb`; cells in grid order (row, then col); tabs become `## Tab` headings |
| dashboard filter `string/=`, `category`, `id` on a query-builder card field | `:slug` bind, written `(<column> = :slug OR :slug IS NULL)` into Metabase's compiled SQL (empty filter = no filter, as in Metabase); default from the parameter |
| dashboard filter on a native template tag | the tile's `{{tag}}` becomes `:slug`; unmapped tags use the card's default |
| display `bar`, `line`, `area`, `scatter`, `row` (bar, axes swapped), `pie`, `combo` | mark + encodings from `graph.dimensions` / `graph.metrics` (or breakout/aggregation columns when unset); second dimension -> `color` (+ `xOffset` unless stacked); several metrics on line/area/combo -> layers, `axis: right` -> dual axis |
| `scalar`, `smartscalar`, `progress`, `gauge` | KPI (text) on `scalar.field` or the first metric |
| `table`, `object`, `pivot` | table tiles |
| text / heading / link cards | markdown cells |
| collections | `knowledge/metabase-collections.md`: where each item lived |
| groups and data permissions | Cedar: `All Users` -> any principal, other groups -> `Group::"<name>"`; `view-data: unrestricted` + `create-queries: query-builder(-and-native)` -> `permit query` on the datasource; table-level grants -> `permit query` on tables; `blocked` -> `forbid query` |
| collection permissions | `view` (read) / `edit` (write) on the notebooks of dashboards in that collection |
| `view_count` on dashboards | `Dashboard.views` in `migration/ir/metabase.json` (search has no view counts, so the inventory doesn't either) |

Encodings use the column names the SQL returns: Metabase aliases implicit-join
columns as `accounts__via__account_id__account_manager`, and unshackle reads the alias
from `result_metadata` (`lib/desired-column-alias`).

## What needs a human

- **Custom-expression metrics** (`sum(a) / sum(b)`, `(sum(a) - sum(b)) / sum(a)`,
  `count-where`, `share`, cumulative): split into simple measures and a ratio or
  derived metric (skills/translate-metrics). The compiled SQL in `queries/` is the
  reference: Metabase divides as `CAST(a AS DOUBLE PRECISION) / NULLIF(b, 0.0)`, which is
  the same as unshackle's `1.0 * a / nullif(b, 0)`.
- **Models**: materialize the SQL in `queries/models/<name>.sql` (dbt:
  `models/unshackle/<name>.sql`), then mark the key column as the primary entity.
- **Date filters** (`date/range`, `date/relative`, `date/all-options`, `date/single`)
  and `number/*` filters on query-builder cards, and the `temporal-unit` ("Time grouping")
  filter: Metabase applies them in MBQL; add `:from` / `:to` predicates to the tile SQL.
- **String filters other than `=`** (`contains`, `starts-with`) and field filters with
  those widgets: write the predicate (`col ILIKE '%' || :x || '%'`).
- **Optional `[[ ... ]]` clauses without a default**: dropped, which is what Metabase
  runs when the filter is empty. Add `(... = :x OR :x IS NULL)` if the notebook needs the filter.
- **Charts**: `funnel`, `waterfall`, `map`, `sankey`, `boxplot`,
  `progress`/`gauge` goals, `smartscalar` comparisons, 100% stacked bars, pivot tables
  and dashcard `series` (other cards overlaid) are TODOs on the tile. Pivot with
  `pandas.pivot_table`; funnels as sorted bars.
- **Query builder only**: Metabase can limit a group to the query builder (no native
  SQL). Cedar has no such distinction; the permission keeps a TODO.
- **Sample database**: cards on Metabase's H2 sample database are extracted too and
  warned about (several databases in one datasource); skip them with `--only`.
- **Iframe, action and placeholder cards**: unknown tiles with a TODO.

## Paid-only features

| Feature | What unshackle does |
| --- | --- |
| Row and column security (formerly sandboxing, `GET /api/mt/gtap`) | Open source returns 404: an info warning in the report. On paid plans each sandbox becomes a row filter with a TODO: write the predicate from `attribute_remappings` (user attribute -> column) or the sandboxing question's filter |
| Impersonation, `view-data` other than unrestricted/blocked | `forbid query` with a TODO to decide the group's access |
| Official collections, verified content, audit/usage analytics | not read; the collections knowledge file lists items |
| Serialization (`metabase export`) | not used; the API covers the same content |

## Parity

Metabase's own results are the source side:

```
curl -s -X POST "$METABASE_URL/api/card/42/query/csv" -H "X-API-Key: $METABASE_API_KEY" \
  -d format_rows=false > migration/parity/data/42.csv
# a dashboard tile with a filter value:
curl -s -X POST "$METABASE_URL/api/dashboard/2/dashcard/39/card/43/query/csv" -H "X-API-Key: $METABASE_API_KEY" \
  --data-urlencode 'parameters=[{"id":"<parameter id>","type":"string/=","value":["COMPLETED"],"target":<mapping target>}]' \
  -d format_rows=false > migration/parity/data/managers.csv
```

```yaml
name: Sales overview - Monthly revenue
source:
  csv: migration/parity/data/42.csv
target:
  notebook: notebooks/sales-overview.ipynb
  tile: monthly_revenue
keys: [purchased_at]
columns: {"Purchased At: Month": purchased_at, "Sum of Post Discount Revenue ($)": sum}
```

CSV headers are display names (with the currency in brackets), so map them with
`columns:`. The dashcard CSV starts with a byte-order mark; strip it.

## Gotchas

- `view_count` didn't move when we read cards and dashboards through the API (v0.64.1);
  a freshly created instance shows 0. Usage comes from people opening them in Metabase.
- A query-builder card whose breakout bins a field Metabase hasn't fingerprinted yet
  fails to compile ("Unable to bin Field without a min/max value"); re-run extract after
  the next sync.
- Field filters compile to fully qualified columns (`"public"."orders"."status"`), so
  the native SQL must not alias that table. unshackle writes the same reference.
- Snippet tags need `snippet-id` for Metabase to run them; unshackle resolves snippets
  by name.
