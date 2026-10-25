---
name: source-redash
description: Use when migrating Redash (saved SQL queries with {{ parameters }}, visualizations, dashboards with widget parameter mappings, groups and data source access, query snippets) with unshackle - API key access, what `unshackle build redash` translates, finishing parameter, chart and permission TODOs, and parity through Redash's own query results.
---

# Redash source

Redash is SQL-first: every query is saved SQL against one data source, with
`{{ mustache }}` parameters, and every chart or table is a visualization of one
query. There is no semantic layer and no row-level security, so a Redash migration
is queries, notebooks and Cedar. Tested against Redash 26.9.0 (community maintained,
BSD-2).

## Access

| Variable | Value |
| --- | --- |
| `REDASH_URL` | `https://redash.example.com` (no `/api`) |
| `REDASH_API_KEY` | a **user** API key (Edit Profile -> API Key), sent as `Authorization: Key <key>` |

- Use an **admin**'s key. Other users only see queries on their groups' data sources,
  and groups, ACLs and events (usage) are admin-only. Not the per-query API keys:
  those only read one query's results.
- Extract only reads. It never executes a query. unshackle paces itself at 300
  requests/minute.
- Reading a query or dashboard through the API records a `view` event, so extract
  reads events first and its own reads don't count as usage.
- Redash is BSD-2; unshackle only calls it over HTTP.

## Commands

```
unshackle inventory redash          # dashboards and queries (with views), data sources
unshackle extract redash [--only dashboard:<id> | --only query:<id>]
unshackle build redash
unshackle todo
```

`--only dashboard:<id>` brings that dashboard's queries. Lists skip archived and
draft queries; `--only query:<id>` fetches one anyway.

Raw files under `migration/raw/redash/`:

| File | From |
| --- | --- |
| `instance.json` | URL and version (`GET /api/session`, `client_config.version`) |
| `data_sources.json` | `GET /api/data_sources`: id, name, type, syntax (connection options aren't read) |
| `queries/<id>.json` | `GET /api/queries/{id}`: SQL, `options.parameters`, `visualizations`, tags |
| `results/<id>.json` | column names and types of the latest cached result; rows are not kept |
| `dashboards/<id>.json` | `GET /api/dashboards/{id}`: widgets, `options.position`, `options.parameterMappings` |
| `snippets.json` | `GET /api/query_snippets` |
| `security/groups.json` | `GET /api/groups`, plus each group's members (id, name, email) and data sources (`view_only`) |
| `security/acl.json` | `GET /api/<queries/dashboards>/{id}/acl`, non-empty ones |
| `usage.json` | `GET /api/events`, last 90 days: `view` events per dashboard and query |
| `_errors.json` | failures (a query you can't read, events without admin) |

Query API keys, owners and editors are dropped at extract.

## What is deterministic

| Redash | Becomes |
| --- | --- |
| query | `queries/<name>.sql`; description, then `Parameters: :name (type, default ...)`, then the query's visualizations |
| `'{{ p }}'` (text, enum, query dropdown, date, datetime) | `:p`; Redash splices the value as text, so the quotes in the SQL go with it |
| `{{ p }}` (number) | `:p` |
| `'{{ p.start }}'` / `'{{ p.end }}'` (date and datetime ranges) | `:p_start` / `:p_end` |
| `IN ({{ p }})`, multi-value enum or query dropdown quoted with single quotes | `IN :p`, a list; the dashboard filter's default is the list. Tiles get a TODO to bind it with `bindparam('p', expanding=True)`, which writes the parentheses |
| dashboard | `notebooks/<slug>.ipynb`; widgets in grid order (`row`, then `col` on the 6-column grid); text widgets -> markdown cells |
| widget parameter, `dashboard-level` | dashboard filter named after `mapTo`, shared by every widget mapped to it; default from the dashboard's saved value, else the first widget query's value; ordered by `globalParamOrder` |
| widget parameter, `widget-level` (or no mapping) | dashboard filter `<tile>_<param>` |
| widget parameter, `static-value` | the value inlined as raw text, as Redash does (several values joined with `multiValuesOptions`) |
| date-range parameter | two filters, `<name>_start` and `<name>_end` |
| CHART `line` / `column` / `area` / `scatter` / `bubble` | line / bar / area / point: `columnMapping` x -> x, y -> y, `series` -> color (+ `xOffset` for unstacked columns), `size` -> size; `swappedAxes` swaps the axes; `xAxis.type` (`datetime` / `category` / `linear`; `-` = from the cached result's column type) |
| CHART with several y columns | layers with each `seriesOptions[y].type`; a series on `yAxis: 1` -> dual axis; all-column series are a TODO |
| CHART `pie` | arc: y -> theta, x -> color |
| COUNTER | KPI (text) on `counterColName`, `counterLabel` as title |
| TABLE | table tile |
| groups | Cedar: `default` -> any principal, `admin` skipped, others `Group::"<name>"`; data source access -> `permit query` on the datasource, `view_only` -> `permit view` |
| dashboard visibility | `permit view` on the notebook for every group with access to a data source its widgets use (Redash lists a dashboard to those groups) |
| dashboard ACL (`modify`) | `permit edit` on the notebook for `User::"<email>"` |
| query snippets | `knowledge/redash-query-snippets.md` |
| events | `Dashboard.views` in `migration/ir/redash.json`; inventory views |

Every query becomes a verified query, including those on dashboards: Redash queries
are the saved SQL people reuse.

## What needs a human

- **Parameters outside a quoted value**: Redash pastes the value into the SQL as text,
  so each of these keeps its mustache with a TODO:
  - `{{ p }}` for a text or enum value without quotes (a column name,
    `ORDER BY {{ sort }}`): the value is SQL, which a bind can't carry. Inline the value
    the dashboard uses, or write the choices as SQL over `:p` (a `CASE`).
  - `'%{{ term }}%'`, a parameter inside a longer string: build the string around the
    bind, `'%' || :term || '%'`.
  - Multi-value parameters anywhere but `IN ({{ p }})`, or with no quotes or double
    quotes in `multiValuesOptions` (the values are then numbers, columns or identifiers).
  - Mustache sections (`{{# p }}...{{/ p }}`): rewrite as SQL over the binds.
  - Names that aren't declared parameters.
- **Multi-value binds**: `:p` is a list. In the notebook, change the tile's
  `pd.read_sql(text(sql), ...)` to
  `text(sql).bindparams(bindparam("p", expanding=True))` (`from sqlalchemy import bindparam`).
- **Relative dates** (`d_last_7_days`, `d_this_month`, `d_now`): Redash computes them
  when the query runs; the default is left empty with a TODO. Set it in the notebook's
  parameters cell.
- **Non-SQL data sources** (Mongo, JSON, Python, Google Sheets...) and **Query
  Results** (SQLite over other queries' cached results as `query_<id>` tables): the
  query and its tiles get a TODO to rewrite them over the warehouse.
- **Several data sources**: all are written to one datasource, with a warning.
- **Dashboard filters from result columns** (`region::filter`, `region::multi-filter`):
  Redash filters those in the browser; filter the dataframe or make them parameters.
- **Charts**: `heatmap`, `box`, and the other visualizations (PIVOT is a table plus a
  TODO; FUNNEL, MAP, CHOROPLETH, COHORT, SANKEY, SUNBURST, WORD_CLOUD, BOXPLOT,
  DETAILS) are TODOs on the tile. So are error bars (`yError`), 100% stacking
  (`percentValues`), unstacked areas (Altair stacks them), several y columns with a
  series column, several pies, counters on another row, with a target or counting
  rows, and hidden table columns.
- **Query ACLs**: Cedar has no query resource; they're info warnings.
- **Formatting** (number formats, prefixes, colors, labels) isn't carried over.

## Parity

Redash's own results are the source side. Run the saved query with the parameter
values you want, then poll the job:

```python
r = requests.post(f"{url}/api/queries/2/results", headers=auth,
                  json={"parameters": {"status": "CANCELLED"}, "max_age": 0}).json()
while "job" in r:
    job = r["job"]
    if job["status"] == 3:
        r = requests.get(f"{url}/api/query_results/{job['query_result_id']}", headers=auth).json()
    else:
        time.sleep(0.5)
        r = requests.get(f"{url}/api/jobs/{job['id']}", headers=auth).json()
data = r["query_result"]["data"]   # columns [{name, type}], rows [{name: value}]
```

A date range is `{"purchased": {"start": "2025-03-01", "end": "2025-09-01"}}`. Write
`data.rows` to `migration/parity/data/<name>.csv` (columns in `data.columns` order);
names are the SQL aliases, so no `columns:` map is needed:

```yaml
name: Revenue by region, status = CANCELLED
source:
  csv: migration/parity/data/region-cancelled.csv
target:
  notebook: notebooks/sales-overview.ipynb
  tile: revenue_by_region
  params: {status: CANCELLED}
keys: [region, mode]
```

## Gotchas

- Postgres text columns come back with `"type": null` in query results; only numbers,
  dates and booleans are typed. Auto x axes on text columns are nominal.
- `create_root --org <name>` makes the org slug the name, and a single-org Redash then
  answers every API call with "Couldn't find resource". Leave `--org` out (slug
  `default`).
- `GET /api/users/me` returns 500 on 26.9.0; use `GET /api/session`.
- `page_size` above 250 is a 400.
- The editor's "Limit 1000" checkbox (`options.apply_auto_limit`) makes Redash add
  `LIMIT 1000` when it runs the query; the migrated SQL has no limit, so a tile over
  1,000 rows shows more than Redash did.
