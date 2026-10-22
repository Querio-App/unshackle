---
name: source-thoughtspot
description: Use when migrating ThoughtSpot (models/worksheets, tables, liveboards, saved answers, formulas, RLS, column security, sharing, Spotter feedback) with unshackle - REST API v2 auth, TML export, what `unshackle build thoughtspot` translates, how to finish ThoughtSpot formula TODOs (group_aggregate, cumulative_*, ts_groups), the licensing rule against ThoughtSpot's own libraries, and parity with ThoughtSpot's generated SQL.
---

# ThoughtSpot source

## Licensing first (read before changing anything)

ThoughtSpot's own libraries - `thoughtspot_tml`, `cs_tools`,
`thoughtspot_rest_api_python`, `@thoughtspot/rest-api-sdk` / `thoughtspot-rest-api-sdk`
- are licensed under terms that forbid using them to build converters to
non-ThoughtSpot tools. unshackle's ThoughtSpot source is **clean-room**: it calls the
documented REST API v2 with `httpx` and parses TML as plain YAML with `pyyaml`.

- Never `pip install`, import, vendor, or copy code, schemas or tests from those
  packages into this repo or into a customer's repo, even to "just parse one file".
- Work from the public API reference and TML docs (developers.thoughtspot.com,
  docs.thoughtspot.com) and from real exports only.
- `tests/test_thoughtspot.py::test_module_is_clean_room` fails if one is imported or
  added as a dependency.
- **Quick legal check** before a customer migration: the customer's ThoughtSpot
  contract may restrict exporting or automated access; confirm with them that
  exporting their own TML/SQL through the REST API is allowed, and that the service
  account is theirs. When unsure, stop and ask a human - don't work around it.

## Access

| Variable | Value |
| --- | --- |
| `THOUGHTSPOT_URL` | `https://<cluster>.thoughtspot.cloud` (no path) |
| `THOUGHTSPOT_USERNAME` | service user (admin, or can-manage-data + access to all content) |
| `THOUGHTSPOT_PASSWORD` or `THOUGHTSPOT_SECRET_KEY` | password, or the trusted-authentication secret key (Develop > Security settings); secret key wins if both are set |
| `THOUGHTSPOT_ORG_ID` | optional: org id on multi-org clusters (numeric). Run once per org |

- Auth: `POST /api/rest/2.0/auth/token/full` `{username, password | secret_key,
  validity_time_in_sec, org_id}` -> bearer `token`; unshackle re-logs in before
  `expiration_time_in_millis` and once after a `401`.
- The user only exports what it can see: TML export returns per-object errors for the
  rest (`_errors.json`, then the report). Use an admin, and `THOUGHTSPOT_ORG_ID` per org.
- Rate limits are unpublished; unshackle stays at 300 requests/minute. TML export is the
  heavy call.

## Commands

```
unshackle inventory thoughtspot      # models/worksheets/tables, liveboards, answers with views
unshackle extract thoughtspot [--only <guid>]
unshackle build thoughtspot
unshackle todo
```

Raw files under `migration/raw/thoughtspot/`:

| File | From |
| --- | --- |
| `metadata/LOGICAL_TABLE.json`, `LIVEBOARD.json`, `ANSWER.json` | `POST /metadata/search` `{metadata: [{type}], include_stats: true, record_offset, record_size}` (paged) - views, last accessed |
| `tml/<kind>/<guid>.tml` | `POST /metadata/tml/export` `{metadata: [{identifier}], export_associated: true, export_fqn: true, edoc_format: YAML, export_options: {export_with_associated_feedbacks, export_column_security_rules}}`; kinds `table`, `model`, `worksheet`, `sql_view`, `view`, `liveboard`, `answer`, `nls_feedback` |
| `sql/liveboard/<guid>.json`, `sql/answer/<guid>.json` | `POST /metadata/liveboard/sql` / `/metadata/answer/sql` `{metadata_identifier}` -> `{metadata_id, metadata_name, metadata_type, sql_queries: [{metadata_id (viz), metadata_name, sql_query}]}` |
| `permissions/<TYPE>.json` | `POST /security/metadata/fetch-permissions` (sharing per object) |
| `_errors.json` | failures (older clusters without `export_options` are retried without it) |

Models/worksheets are exported with their tables (`export_associated`); tables alone
aren't exported separately.

## Manual import (no API access)

Without a service user, import TML exported from the UI:

```
unshackle import thoughtspot "Sales Overview.zip" more-tml/
unshackle build thoughtspot
```

- On a liveboard, answer or model, use Export TML from its more (`...`) menu. Export
  with associated objects to get the models and tables too (a `.zip`). Import the
  `.zip`, `.tml` files or a folder of them.
- Each TML is filed by its top-level key and `guid`, as `extract` does.
- Models, worksheets, tables, RLS rules and Spotter feedback come out the same.
  Missing: ThoughtSpot's generated SQL (liveboard tiles carry a TODO for their SQL;
  saved answers aren't turned into verified queries), view counts, sharing
  (Cedar permits) and links. Column security only appears if the TML includes it.
- unshackle reads TML as plain YAML; the licensing rules above apply unchanged.

## What is deterministic

| ThoughtSpot | Becomes |
| --- | --- |
| table TML (`db`, `schema`, `db_table`, `columns[].db_column_name`, `data_type`) | `catalog/` table with types and descriptions |
| model `model_tables` (or worksheet `tables` + `table_paths`) | one semantic model per physical table (`FCT_ORDERS` -> `orders`) |
| `joins` (`with`, `on: [A::x] = [B::y]`, `cardinality`, or `referencing_join` -> table `joins_with`) | foreign entity on the many side, primary on the one side |
| column `column_type: ATTRIBUTE` | dimension; `DATE`/`DATE_TIME` -> time (day) |
| column `column_type: MEASURE`, `aggregation: SUM/COUNT/COUNT_DISTINCT/AVERAGE/MIN/MAX/MEDIAN` | measure + simple metric (other aggregations -> TODO) |
| formula `sum/count/unique_count/average/min/max/median ( x )` | measure + simple metric |
| `sum_if / count_if / unique_count_if / average_if ( cond , x )` | simple metric with `filter` |
| `safe_divide ( a , b )`, `a / b` over aggregates or measure columns | ratio metric (helper metrics added) |
| arithmetic over measure columns (`[Revenue] - [Refunds]`) | derived metric |
| MEASURE formula over a row expression (`[A] - [B]`, `aggregation: SUM`) | measure with that `expr` |
| `if ( c ) then a else b`, `start_of_month ( d )`, `concat`, `lower`, `round`, ... | dimension `expr` (`CASE`, `date_trunc('month', d)`, ...) |
| `description`, `properties.synonyms`, `properties.ai_context` | `knowledge/thoughtspot-model-<name>.md` ("Also called: ...", "AI context: ...") |
| `nls_feedback` (Spotter coaching): `BUSINESS_TERM`, `REFERENCE_QUESTION` | `knowledge/thoughtspot-spotter-feedback.md` (`context: always` when it has business terms) |
| liveboard `visualizations[].answer` | notebook tile; `chart.type` COLUMN/STACKED_COLUMN -> bar, BAR/STACKED_BAR -> horizontal bar, LINE, AREA, PIE/DONUT -> arc, SCATTER/BUBBLE -> point, KPI -> KPI, `display_mode: TABLE_MODE`/PIVOT_TABLE -> table; `axis_configs` x/y/color -> encodings |
| `note_tile.html_parsed_string` | text tile (HTML -> Markdown) |
| `layout.tiles` (x, y) / `layout.tabs` | cell order (`unshackle.layout` metadata); one `## Tab` markdown cell per tab |
| liveboard `filters` (`oper: in`, `date_filter`) | dashboard filters (`LAST_N_PERIOD 12 MONTH` -> `'last 12 month'`) + TODO on each tile |
| saved answer | `queries/<name>.sql` verified query (ThoughtSpot SQL, description has the search query) |
| table `rls_rules` `[T::REGION] = ts_groups` / `ts_username` | row filter `region in ({{ user.groups }})` (TODO) / `= {{ user.email }}` |
| `column_security_rules` (`column_name`, `group_access`) | Cedar `forbid query` on the Column unless in those groups |
| sharing `READ_ONLY` / `MODIFY` on a liveboard | `permit view` (+ `edit`) on the notebook; on a model: `permit query` on its tables. Group `All` -> everyone |

Tile SQL is **ThoughtSpot's generated SQL** (`source_config.source_sql: true`). Its
outputs are `ca_1..ca_n`; unshackle wraps it as `select "ca_1" as month_order_date, ...
from (...) ts`, in the order of `answer_columns`, and confirms the order by finding the
physical column in each select item (TODO when it can't). Encodings use the snake_case
alias; the axis `title` keeps "Month(Order Date)".

## Finishing TODOs

ThoughtSpot formula functions and what to write instead:

| ThoughtSpot | SQL / MetricFlow |
| --- | --- |
| `group_aggregate ( sum ( m ) , { [dim] } , query_filters ( ) )` (LOD) | SQL model at that grain (`select dim, sum(m) ... group by dim`) + semantic model on it; for share-of-total in a chart, tile SQL `m / sum(m) over (partition by ...)` |
| `group_sum ( m , dim )`, `group_count`, `group_average`, `group_unique_count` | same as `group_aggregate` with that aggregate |
| `cumulative_sum ( m , date )`, `cumulative_average` | `cumulative` metric (`window` / `grain_to_date`) or `sum(m) over (order by date)` in the tile |
| `moving_average ( m , 2 , 0 , date )`, `moving_sum` | tile SQL `avg(m) over (order by date rows between 2 preceding and current row)` |
| `rank ( m , 'desc' )`, `rank_percentile` | `rank() over (order by m desc)` / `percent_rank()` in tile SQL |
| `last_value ( m , query_groups ( ) , { date } )` (semi-additive) | measure with `non_additive_dimension: {name: date, window_choice: max}` |
| `diff_days ( a , b )`, `add_days ( d , n )`, `month_number ( d )`, `day_of_week ( d )` | `datediff('day', b, a)`, `dateadd(day, n, d)`, `extract(month from d)`, `dayname(d)` (dialect) |
| `contains ( s , 'x' )`, `strpos`, `substr`, `to_string`, `to_integer` | `s like '%x%'`, `position`, `substr`, `cast(... as varchar)`, `cast(... as integer)` |
| `sql_string_op ( 'md5({0})' , x )`, `sql_int_op`, `sql_date_op`, `sql_number_op` (passthrough) | substitute `{0}`, `{1}` with the args: `md5(x)` |
| `unique_count_if`, `average_if` with complex conditions | simple metric with the condition as `filter` (dimensions via entity names) |
| formula spanning two tables (`sum ( [A::x] * [B::y] )`) | SQL model joining them, or a measure on the fact table with the dimension moved there |

Dashboard TODOs:

- **Liveboard filters** are baked into each viz's SQL as literals (`WHERE "REGION" IN
  ('EMEA', 'AMER')`). Replace them with the named bind and a real default in the parameters cell;
  relative dates (`'last 12 month'`) become `<name>_from` / `<name>_to`.
- **ca_N order not confirmed**: open the SQL and check each `ca_N` against the answer
  column (formulas like `Region Share` have no physical column to find).
- **Answer formulas** (`formulas:` in the answer) are computed in ThoughtSpot's SQL;
  keep them in the tile SQL, or promote to a metric if reused.
- **Search-only features** (`top 10`, `growth of ... by ...`, `vs`): already in the SQL
  (`ORDER BY ... LIMIT 10`, self-joins); keep or rewrite with windows.
- **Chart types with no Altair mark** (GEO_*, TREEMAP, SANKEY, WATERFALL, FUNNEL,
  PIVOT_TABLE): pick the closest chart. LINE_COLUMN combos: `alt.layer` of a bar and a
  line, with `.resolve_scale(y='independent')` for a second axis.
- **sql_view / view TML**: recreate as a warehouse view (SQL views keep their SQL in
  `queries/`), then add a semantic model.

## Permissions

- `ts_groups` in an RLS rule is "any group the user belongs to"; row filters need the IdP
  groups as `{{ user.groups }}` (confirm the platform expands it to a SQL list) or one
  row filter per group with `groups: [<group>]` and a literal predicate.
- `ts_username` is the ThoughtSpot username; the row-filter `{{ user.email }}` only matches if
  usernames are emails - otherwise use a mapping table in a subquery predicate.
- RLS applies to the **table** in ThoughtSpot (every model on it); unshackle row filters
  do the same. Rules in `rls_rules.rules[]` are ORed in ThoughtSpot: if a table has
  several, merge them into one predicate with `or`.
- Admins and users with "can administer" bypass RLS in ThoughtSpot: don't list them in
  `groups`.
- Sharing to users with non-email usernames gets a TODO; sharing on saved answers is
  not migrated (verified queries have no ACL).
- Column security needs 10.12+ and `export_column_security_rules`; on older clusters,
  check column permissions in the UI (Data > model > columns > Security) by hand.

## Parity

The liveboard/answer SQL endpoints are the oracle (they compile, not run):

```yaml
name: Sales Overview - Revenue by Month
source:
  sql: |   # sql_query of that viz from migration/raw/thoughtspot/sql/liveboard/<guid>.json
    select "ca_1" as month_order_date, "ca_2" as region, "ca_3" as total_revenue
    from (<sql_query with the date window pinned>) ts
target:
  notebook: notebooks/sales-overview.ipynb
  tile: revenue_by_month
keys: [month_order_date, region]
```

Numbers straight from ThoughtSpot: `POST /api/rest/2.0/searchdata` with the answer's
`search_query` and `logical_table_identifier` (the model guid), or
`POST /report/liveboard` (CSV/XLSX) for a whole liveboard. Pin relative filters
(`[Order Date] = 'last 12 months'` -> fixed dates) on both sides.

Metrics: compare `mf query --metrics total_revenue --group-by metric_time__month` with a
`searchdata` call `[Revenue] [Order Date].monthly`.

## Alternatives

- **ThoughtSpot MCP server** (ThoughtSpot's hosted Spotter MCP, OAuth): `ping`,
  `getRelevantQuestions`, `getAnswer`, `createLiveboard` - useful to ask the same
  question on both sides during verification. It returns data, not definitions; it
  does not replace TML export. Using it for verification is fine; don't use it to
  build the converter.
- **UI TML export**: Liveboard/model menu > Export TML (zip with associated objects).
  Unzip into `migration/raw/thoughtspot/tml/<kind>/<guid>.tml` if API access isn't
  possible; parse works the same.
- **Git integration** (Develop > Version control): if the customer already commits TML
  to Git, copy those files into `tml/` instead of exporting.
- Do **not** use `cs_tools` or `thoughtspot_tml` for any of this (licensing, above).

## Gotchas

- Worksheets (V1) refer to columns through `table_paths` ids (`FCT_ORDERS_1::AMOUNT`);
  models use table names (`FCT_ORDERS::AMOUNT`). Both are handled; aliased paths to the
  same table (role-playing) collapse into one semantic model - add the second entity by
  hand.
- `BAR` in ThoughtSpot is horizontal; `COLUMN` is vertical.
- ThoughtSpot wraps sums as `CASE WHEN sum(x) IS NOT NULL THEN sum(x) ELSE 0 END`: empty
  groups show 0, not NULL. MetricFlow returns NULL; use `coalesce` in the tile if the
  chart depended on it, or `fill_nulls_with: 0` on the metric.
- Answer column names ("Total Revenue", "Unique Number of Order Id") are generated from
  the aggregation; unshackle strips those prefixes when matching columns.
- Chasm traps: ThoughtSpot avoids double counting across fan-out joins automatically;
  MetricFlow does too through entities, but check distinct counts after joins.
- Multi-org clusters: every guid is per org; run extract once per `THOUGHTSPOT_ORG_ID`
  into separate repos or prefix the notebooks.
