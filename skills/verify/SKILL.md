---
name: verify
description: Use when proving a migration's numbers match the source BI tool - writing migration/parity/*.yaml cases, getting source numbers from each tool, running `unshackle verify`, and debugging differences. Passing cases become evals/.
---

# Verify parity with the source tool

A parity case runs the same question on both sides and compares the results.
`unshackle verify` runs every `migration/parity/*.yaml`, writes
`migration/parity/results.json`, updates `migration/report.md`, and copies each
passing case to `evals/<slug>.yaml`.

```
UNSHACKLE_WAREHOUSE_URL=snowflake://user@account/db/schema?warehouse=wh unshackle verify
```

(`unshackle verify --url ...` also works; needs the `run` extra plus the warehouse's
SQLAlchemy driver. Never put the URL with a password in a tracked file.)

## Case format

```yaml
name: Monthly revenue by region
question: What was revenue by region per month in H1 2025?   # optional; used in the eval
source:
  csv: migration/raw/tableau/views/abc123.csv    # or  sql: "select ..."
target:
  notebook: notebooks/revenue-overview.ipynb     # or  sql: "select ..."
  tile: monthly_revenue                            # runs monthly_revenue_sql
  params: {date_from: '2025-01-01', date_to: '2025-07-01'}   # override the parameters cell
keys: [month, region]
tolerance: 0.0001
```

- `source` / `target`: exactly one of `csv` (path relative to the repo), `sql`
  (run on the warehouse; optional `params`), or `notebook` + `tile` (runs that tile's
  `<tile>_sql` with the notebook's parameters cell merged with `params`).
- `keys`: columns that identify a row; both sides are sorted by them. Omit only for
  single-row results.
- Column names are compared lower-cased and trimmed. Only columns present on both
  sides are compared; extra source columns are noted. Alias target columns to the
  source's names (`as "month of order date"`) when comparing with a CSV.
- Statuses: `pass`, `differs` (with the first bad column and values), `not checked`
  (an error loading a side; the note has it).

## Where the source numbers come from

Prefer SQL the tool itself generated, run on the same warehouse: it removes
connection and caching differences. Save the SQL in the case's `source.sql`.

| Tool | Source side |
| --- | --- |
| Looker | `GET /api/4.0/queries/{query_id}/run/sql` for a tile's query (or `run_inline_query` with `result_format: sql`); `result_format: csv` for numbers |
| Lightdash | `POST /api/v1/projects/{projectUuid}/explores/{exploreId}/compileQuery` with the chart's metricQuery -> SQL |
| Omni | query API with `planOnly: true` -> generated SQL for a tile's query |
| Sigma | `GET /v2/workbooks/{workbookId}/elements/{elementId}/query` -> element SQL |
| ThoughtSpot | `POST /api/rest/2.0/metadata/liveboard/sql` (or `/metadata/answer/sql`) -> SQL per visualization |
| Tableau | `GET /api/<ver>/sites/{site}/views/{viewId}/data` -> CSV (filter with `vf_<field>=<value>`); save under `migration/raw/tableau/views/` |
| Power BI | `POST /v1.0/myorg/datasets/{datasetId}/executeQueries` with a DAX `EVALUATE SUMMARIZECOLUMNS(...)` (tenant setting must allow it), or DAX over the XMLA endpoint; else a CSV exported from the visual |
| Hex | re-run the project's SQL cells on the warehouse (they are the source of truth) |
| Metabase | `POST /api/card/{id}/query/csv` with `format_rows=false` (a dashboard tile with filters: `POST /api/dashboard/{d}/dashcard/{dc}/card/{c}/query/csv` with `parameters`); the SQL is `POST /api/dataset/native` |
| Superset | `POST /api/v1/chart/data` with the chart's `query_context`, `result_type: full`, `result_format: csv`, `pivot`/`flatten` post-processing dropped (a dashboard filter: add `{"col", "op": "IN", "val"}` to each query's `filters`); headers are metric verbose names; the SQL is the same call with `result_type: query` |
| Redash | `POST /api/queries/{id}/results` with `{"parameters": {...}, "max_age": 0}`, poll `GET /api/jobs/{job}` until `status` 3, then `GET /api/query_results/{query_result_id}` (`data.columns` / `data.rows`; its `query` is the SQL Redash ran); a date range is `{"start", "end"}` |

The source skill (`skills/source-<tool>/SKILL.md`) has the auth details. Save CSVs
you export into `migration/parity/data/` if they aren't already under `migration/raw/`.

## Fix the window

Live data moves. Pin every case to a closed period (last full month or quarter,
e.g. `2025-01-01` to `2025-07-01`, half-open) on both sides, and pull both sides
close together in time. Avoid "last 30 days", "today" and relative filters. For
snapshot tables, pin the snapshot date too. Use the same timezone on both sides.

## Tolerance

- Counts and distinct counts: exact, `tolerance: 0`.
- Sums, averages, ratios: `tolerance: 0.0001` (0.01%, the default).
- Don't loosen tolerance to make a case pass. If a real difference is expected
  (e.g. the source rounded before summing), say so in the case `name`/note and in
  the report.

The comparison is relative: `|a - b| / max(|a|, |b|)`. Numbers formatted as text in
a CSV (`"1,234.50"`, `"12%"`) compare as strings; clean them or use the SQL source.

## What to cover

1. The top dashboards by views: one case per headline tile.
2. Every metric used on them: grouped by its main time dimension (month) for a
   closed period.
3. One case per row filter: run the target SQL with the predicate substituted for a
   known user and compare with the source viewed as that user (if the tool can
   impersonate) or with a hand-checked number.

## Debugging a diff

| Symptom | Look at |
| --- | --- |
| Row count differs | filters (missing dashboard default, date boundary inclusive vs exclusive), join type (inner vs left), nulls grouped differently (source "Null" bucket), top-N / row limits in the source |
| Row count equal, values higher | join fan-out (sum over a one-to-many join; count distinct or aggregate before joining), duplicated rows in a derived table |
| Values lower | inner join dropping rows, filter applied twice, row filter active for the verify user |
| Small % gap | average of averages vs weighted average, integer division, rounding before aggregating, `count` vs `count distinct` |
| Off at month edges | timezone (UTC vs local), timestamp vs date truncation, week start day, fiscal calendar |
| Ratio wrong, parts right | ratio of sums vs sum of ratios; division by zero handling (`nullif`) |
| Nulls | `sum` of all nulls is null vs 0; `avg` ignores nulls; `coalesce` in the source |
| Strings differ | case, trailing spaces, date formatting (`2025-01-01` vs `2025-01-01 00:00:00`: cast to date in the target SQL) |

Fix the translation (metric, tile SQL, row filter), not the case. Re-run until it
passes, or write down why it can't.

## After it passes

Passing cases are written to `evals/<slug>.yaml` with the expected row count and a
20-row sample, so they keep running after the migration. `unshackle verify` overwrites
these on every run; put hand-written evals under a different file name. Report the
pass rate (`## Parity (n/m pass)` in `migration/report.md`) in the PR.
