---
name: source-superset
description: Use when migrating Apache Superset (datasets and their metrics, charts, dashboards with native filters, roles, row-level security) with unshackle - login or API key access, what `unshackle build superset` translates from the export bundle and the chart data API, finishing metric, chart and filter TODOs, and parity through Superset's own chart data results.
---

# Apache Superset source

Superset serves everything over its REST API, and exports dashboards, charts and
datasets as a ZIP of YAML (the same bundle `superset export-dashboards` writes).
unshackle saves that bundle untouched, plus the SQL **Superset itself** compiles for
every chart (`POST /api/v1/chart/data` with `result_type: query`), so tile SQL is
exactly what Superset runs. Tested against Superset 6.1.0.

## Access

| Variable | Value |
| --- | --- |
| `SUPERSET_URL` | the address you open Superset at, no path: `https://superset.example.com` |
| `SUPERSET_USERNAME` / `SUPERSET_PASSWORD` | a database or LDAP user; exchanged at `POST /api/v1/security/login` |
| `SUPERSET_AUTH_PROVIDER` | `db` (default) or `ldap` |
| `SUPERSET_API_KEY` | instead of a password: sent as `Authorization: Bearer <key>` |

- Use an **Admin**. Other roles only export what they can see, the action log
  (usage) is admin-only, and **row-level security applies to the extracting user**:
  a user under an RLS rule gets that rule's `WHERE` compiled into every tile's SQL.
- Roles need `FAB_ADD_SECURITY_API = True` in `superset_config.py`
  (`/api/v1/security/roles/search/`). Without it, `security/roles.json` is an extract
  error and no Cedar policies are written.
- OAuth/SSO-only instances have no password login: create a database user for the
  migration, or pass a bearer token in `SUPERSET_API_KEY`. Superset 6.1.0 (Flask-AppBuilder
  5.0.2) has no API keys of its own; login tokens expire after 15 minutes, which is
  enough for most extracts.
- Extract only reads, plus `POST /api/v1/chart/data`, which Superset exempts from CSRF.
  It compiles SQL but doesn't run queries on the warehouse. unshackle paces itself at
  300 requests/minute.
- Superset is Apache 2.0; unshackle only calls it over HTTP.

## Commands

```
unshackle inventory superset          # dashboards, charts (with log-based views), datasets
unshackle extract superset [--only dashboard:<id> | --only chart:<id> | --only dataset:<id>]
unshackle build superset
unshackle todo
```

Without `--only`, extract uses the full asset export (`GET /api/v1/assets/export/`,
which includes SQL Lab saved queries). `--only dashboard:<id>` exports that dashboard
with its charts, datasets and databases.

Raw files under `migration/raw/superset/`:

| File | From |
| --- | --- |
| `instance.json` | URL and version (`GET /version`) |
| `export/` | the export ZIP, unzipped without its top folder: `databases/`, `datasets/<db>/`, `charts/`, `dashboards/`, `queries/` (YAML keyed by uuid) |
| `dashboards.json`, `charts.json`, `datasets.json` | `GET /api/v1/<kind>/` - ids, uuids, names, dashboard roles |
| `sql/<chart uuid>.sql` | `POST /api/v1/chart/data` with the chart's `query_context`, `result_type: query` (a mixed chart's second query is `<uuid>-1.sql`) |
| `sql/<dashboard uuid>/<chart uuid>.sql` | the same with the dashboard's select filters set to `'unshackle__<name>'` |
| `security/roles.json` | `GET /api/v1/security/roles/search/` |
| `security/permissions.json` | `GET /api/v1/security/permissions-resources/`, data access permissions only |
| `security/rls.json` | `GET /api/v1/rowlevelsecurity/` |
| `usage.json` | `GET /api/v1/log/`, last 90 days: dashboard page loads and chart data requests per id |
| `_errors.json` | failures (a chart Superset can't compile, a missing security API) |

Superset masks database passwords in exports; unshackle also drops `encrypted_extra`
(service-account JSON and the like).

### Without API access: the export ZIP

```
unshackle import superset dashboard_export_<timestamp>.zip   # or the unzipped folder
unshackle build superset
```

Download it from Superset's UI: select dashboards (or charts, or datasets) on their list
page and choose **Export**; the CLI's `superset export-dashboards` writes the same bundle.
Pass several zips or folders at once. unshackle finds the folder holding `metadata.yaml`,
copies `databases/`, `datasets/`, `charts/`, `dashboards/` and `queries/` to `export/`
(dropping `encrypted_extra` and SSH tunnel settings) and writes `_import.json`.

The export carries datasets, metrics, calculated columns, chart form data, dashboard
layout and native filters, so the semantic layer and notebook structure match an extract.
It does not carry:

- **compiled chart SQL**: every chart tile gets a TODO to write its SQL from
  `source_config` (the form data); charts that are on no dashboard are reported and
  skipped. Select filters are listed per tile instead of bound as `:params`.
- **roles and row-level security**: no Cedar policies or row filters; recreate them by
  hand or run `unshackle extract superset`.
- **usage, ids and the instance URL**: no view counts, and nothing links back to Superset.

`migration/report.md` lists these under "imported manually". With API access later,
`unshackle extract superset` replaces the imported files.

## What is deterministic

| Superset | Becomes |
| --- | --- |
| physical dataset | `catalog/` table + semantic model on `schema.table`; columns -> dimensions (`is_dttm` / timestamp types -> time), `verbose_name` -> label; `main_dttm_col` -> default time dimension |
| calculated column (`expression`) | dimension with that SQL as `expr` (not a catalog column) |
| metric `SUM / COUNT / AVG / MIN / MAX (expr)`, `COUNT(*)`, `COUNT(DISTINCT x)` | measure + simple metric; the argument may be any row expression, so `SUM(CASE WHEN status = 'COMPLETED' THEN revenue ELSE 0 END)` stays a sum over that CASE |
| metric `A / B`, `A / NULLIF(B, 0)`, `1.0 * A / ...`, `CAST(A AS ...) / B` with A and B simple aggregates | ratio metric; sides that match another metric reuse it, others become `<metric>_numerator` / `_denominator` metrics |
| virtual dataset (SQL) | `queries/<name>.sql` (TODO: materialize as a SQL model) + a semantic model named after it |
| dashboard | `notebooks/<slug>.ipynb`; the `position` tree in order: each ROW (with its COLUMNs) is a row of cells, TABs and HEADERs become `## ` headings, MARKDOWN becomes markdown cells |
| native filter `filter_select` | `:name` bind, written `(col = :name OR :name IS NULL)` into Superset's compiled SQL for the charts in its scope (`chartsInScope`, else `scope.rootPath` minus `excluded`) whose dataset has the column; empty filter = no filter, as in Superset; default from `defaultDataMask.filterState.value` |
| `echarts_timeseries_bar` / `_line` / `_smooth` / `_step` / `_scatter`, `echarts_area` | bar / line / point / area: `x_axis` -> x (temporal with `time_grain_sqla`, which the SQL already truncates), first metric -> y, first `groupby` -> color (+ `xOffset` when bars aren't stacked), `orientation: horizontal` swaps the axes; several line metrics -> layers |
| `mixed_timeseries` | layers from `metrics` (`seriesType`) and `metrics_b` (`seriesTypeB`), dual axis with `yAxisIndexB: 1` |
| `pie` | arc: `metric` -> theta, `groupby` -> color |
| `big_number_total`, `big_number` | KPI (text) on `metric` |
| `table`, `pivot_table_v2` | table tiles |
| SQL Lab saved queries (full export) | `queries/<label>.sql` |
| charts not on a dashboard | `queries/<name>.sql` with Superset's compiled SQL |
| roles | Cedar, `Group::"<role>"`; `Admin` skipped, `Public` -> any principal (with a TODO). `all_datasource_access` / `all_database_access` / `database_access` -> `permit query` on the datasource; `schema_access` -> each physical dataset in the schema; `datasource_access` -> the dataset's table (virtual: the model) |
| dashboard roles (`DASHBOARD_RBAC`) | `permit view` on the notebook |
| row-level security, `Regular` | row filter: `clause` as the predicate for the rule's roles, one per table |
| action log | `Dashboard.views` (dashboard page loads) in `migration/ir/superset.json`; inventory views |

Adhoc metric and column labels like `SUM(quantity)` aren't valid Altair field names:
unshackle wraps the tile SQL in `SELECT "SUM(quantity)" AS sum_quantity ... FROM (...) AS q`
and keeps the label as the axis title.

## What needs a human

- **Other metrics** (`1 - SUM(a) / SUM(b)`, window functions, several aggregates):
  TODO metric with the SQL in `source_expression`; split it into measures and a ratio
  or derived metric (skills/translate-metrics).
- **Integer ratios**: `SUM(int_col) / COUNT(*)` divides as integers on Postgres, SQL
  Server and others; unshackle's ratio would not, so it's a TODO. Decide which one the
  business meant.
- **Jinja** (`{{ current_username() }}`, `{{ filter_values('x') }}`, `{% if %}`) in
  virtual datasets, metrics, calculated columns, RLS clauses and saved queries: a TODO
  each. The compiled tile SQL has it rendered **for the extracting user**.
- **Charts saved without a `query_context`** (by Superset versions before it stored one, and
  never re-saved): no SQL. Open and save the chart in Superset, then re-run extract;
  or write the SQL from the form data kept in the tile's `source_config`.
- **Post-processing** that changes numbers (`rolling`, `cum`, `contribution`,
  `compare`/time comparison, `resample`, `prophet`, `boxplot`, `histogram`): Superset
  computes it in pandas after the SQL, so the tile SQL returns the rows before it. Add
  it in SQL (window functions) or pandas.
- **Time, range, time-grain and time-column filters**: listed as dashboard filters, but
  not bound into SQL; add `:name` predicates to the tiles named in their TODOs.
- **Mixed charts**: the second query's SQL is in the raw `-1.sql` file; join both on the
  x axis.
- **Base RLS filters** (everyone except the listed roles), **filters sharing a
  `group_key`** (Superset ORs them per user; unshackle ANDs row filters), and Jinja
  clauses: row filters with a TODO and no predicate.
- **Charts**: everything else (`sankey_v2`, `heatmap_v2`, maps, `funnel`, `gauge`,
  `treemap_v2`, `word_cloud`, deck.gl, handlebars...), `big_number` trendlines, 100%
  stacked bars (`stack: Expand`), several `groupby` columns, and pivot tables are TODOs
  on the tile. Pivot with `pandas.pivot_table`.
- **No primary keys**: Superset datasets don't record keys, so every model warns; add
  the primary entity before joining models.

## Parity

Superset's own results are the source side. Post the chart's saved `query_context`
with `result_type: full` and `result_format: csv`; drop the `pivot` / `flatten`
post-processing so rows stay long like the SQL, and add the dashboard filter value as
a query filter:

```python
qc = json.loads(requests.get(f"{url}/api/v1/chart/2", headers=auth).json()["result"]["query_context"])
for q in qc["queries"]:
    q["post_processing"] = [p for p in q["post_processing"] if p["operation"] not in ("pivot", "flatten")]
    q["filters"].append({"col": "status", "op": "IN", "val": ["COMPLETED"]})  # the dashboard's Status filter
csv = requests.post(f"{url}/api/v1/chart/data", headers=auth,
                    json={**qc, "result_type": "full", "result_format": "csv"}).text
```

A metric on its own: `{"datasource": {"id": <dataset id>, "type": "table"}, "queries":
[{"columns": [], "metrics": ["order_count"]}]}`.

```yaml
name: Sales overview - Orders by status, Status = COMPLETED
source:
  csv: migration/parity/data/orders_by_status_completed.csv
target:
  notebook: notebooks/sales-overview.ipynb
  tile: orders_by_status
  params: {status: COMPLETED}
keys: [status]
columns: {Orders: order_count}
```

CSV headers use the metric's **verbose name** (`Orders`, not `order_count`), so map
them with `columns:`.

## Gotchas

- Chart data via the API only reaches the action log with a `slice_id` (and
  `dashboard_id`) when the caller sends them, as the Superset frontend does; extract's
  own compile calls don't count as views. Dashboard views are page loads
  (`/superset/dashboard/<id>/`, action `dashboard`).
- Dashboards whose `position_json` lacks chart `uuid`s (created through the API) get
  every chart appended again as a new row in the export; the notebook then has
  duplicate tiles (`<name>_2`). Re-save the dashboard in Superset first.
- Writes through the API (not needed for extract) need a CSRF token from
  `GET /api/v1/security/csrf_token/`, sent as `X-CSRFToken` with the session cookie and
  a `Referer`.
- The 6.x Docker images ship without database drivers: add `psycopg2-binary` (or your
  warehouse's driver) to the image.
