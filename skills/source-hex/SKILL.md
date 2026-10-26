---
name: source-hex
description: Use when migrating Hex (hex.tech) projects, apps and semantic models with unshackle - API token and plan requirements, Git export and semantic-project pull, how SQL/Python/chart cells and Jinja parameters are translated, and how to prove parity by re-running Hex SQL cells.
---

# Hex source

## Access

| Variable | Value |
| --- | --- |
| `HEX_API_TOKEN` | Workspace token (Settings > API keys) or personal token (User settings > API keys) |
| `HEX_BASE_URL` | optional, default `https://app.hex.tech/api/v1`; single-tenant/EU: the address you open Hex at plus `/api/v1` |
| `HEX_EXPORT_DIR` | optional: folder of Git-exported project YAML (charts, inputs, app layout) |
| `HEX_SEMANTIC_DIR` | optional: output of `hex context semantic-project pull` (semantic models) |

- The API needs a **Team or Enterprise** plan. A workspace token with read scope sees
  every project; a personal token only sees what that user can see.
- Rate limit is about 30 requests/minute; unshackle paces itself. `GET /projects`
  paginates with `pagination.after`; `GET /cells?projectId=` returns the **draft**
  version of cells (not necessarily what the published app shows).
- The API has no chart configuration or app layout. For charts, export each project
  to Git (Project > Git sync, or `hex` CLI export) and point `HEX_EXPORT_DIR` at the
  YAML files. Without it every SQL cell becomes a table tile.

## Commands

```
unshackle inventory hex               # projects with app views (all time) and last viewed
unshackle extract hex [--only <projectId>]
unshackle build hex
unshackle todo
```

Raw files: `projects.json`, `cells/<projectId>.json`, `export/*.yaml`, `semantic/**/*.yaml`.
When a project has both, the export wins (it has `resultVariable` names, charts and layout).

## Manual import (no API access)

Without a Hex API token (it needs a Team or Enterprise plan), import YAML:

```
unshackle import hex project-exports/ semantic/
unshackle build hex
```

- Project YAML from Hex's Git export (has `meta` and `cells`) goes to `raw/export/`;
  `hex context semantic-project pull` output (has `models`) goes to `raw/semantic/`.
  Other YAML is skipped.
- Exported projects come out as they do from `extract` with `HEX_EXPORT_DIR`: charts,
  inputs and app layout. Missing: projects you didn't export, app view counts,
  categories, links and sharing.

## What is deterministic

| Hex | Becomes |
| --- | --- |
| SQL cell | `queries/<project>-<cell>.sql` verified query |
| `{{ param }}`, `'{{ param }}'` | `:param` bind (the notebook's parameters cell supplies it) |
| dataframe SQL (`from orders_in_scope`) | upstream SQL cells inlined as CTEs, dependencies first |
| Input cell (`DROPDOWN`, `DATE`, `NUMBER`, ...) | dashboard filter with its default |
| Chart cell | tile: `bar/column/histogram` -> bar, `line`, `area`, `scatter` -> point, `pie/donut` -> arc; x, first y (with its aggregate), series -> color |
| Single value / table display cell | KPI / table tile |
| Markdown / text cell | text tile + part of `knowledge/hex-<project>.md` (with the project description) |
| Project | `notebooks/<project>.ipynb`, tiles ordered by `appLayout` |
| Python cell | `queries/*.py` with a TODO |
| semantic `base_sql_table` model | catalog table + semantic model |
| `dimensions: [{id, type, expr_sql, unique}]` | dimensions; `unique: true` -> primary entity; date/timestamp types -> time (day) |
| `measures: func: sum/count/count_distinct/avg/min/max/median, of:` | measure + simple metric (`count` without `of` counts the key) |
| measure `filters: [{dimension, operator, value}]` | metric `filter` with `{{ Dimension('model__dim') }}` |
| measure `filters: [{expr_sql}]` | `CASE WHEN <pred> THEN <expr> END` inside the measure |
| `func_sql: SUM(<expr>)` (single aggregate) | measure with that agg and expr |
| `func_calc: a / b` or `a / NULLIF(b, 0)` | ratio metric; other arithmetic -> derived metric |
| `relations: many_to_one, join_sql: a.fk = b.pk` | foreign entity on `a` named `b` |
| `base_sql_query` model | `queries/<model>-base-query.sql` + TODO to materialize |

## Finishing TODOs

- **Jinja beyond a bare variable**: `{% if x %}...{% endif %}` -> an always-on
  predicate with a neutral default (`AND (col = :x OR :x IS NULL)`);
  `{{ list | array }}` / `inclause` -> `col IN :x` with an expanding bind, or one
  tile per value; `{{ x | sqlsafe }}` injects raw SQL - inline the chosen value.
  Hex inputs are typed: a date input renders as a quoted date, so `:start_date`
  binds the same way.
- **Inputs missing from the export** (API-only projects): unshackle adds a filter with
  default `None`; set a real default in the parameters cell.
- **Dataframe SQL dialect**: Hex runs dataframe SQL in DuckDB. After inlining, it runs
  on your warehouse: check `date_trunc`, `strftime`, `list_*`, `QUALIFY`, `::` casts.
  The warning lists each inlined cell.
- **Python cells**: if the cell only reshapes a dataframe (`groupby`, `pct_change`,
  `merge`, `pivot`), rewrite it as SQL (window functions) and point the chart at it;
  otherwise keep it in the tile's chart cell (the DataFrame is `<tile>`) or as a
  `queries/*.py` analysis. Charts on a Python dataframe have no SQL until then.
- **Multiple y series / combo charts**: line, area and scatter series become one layer
  each (`alt.layer`); several bar series need a fold in SQL (one row per series) with
  `color` and `xOffset`.
- **Semantic `func_sql` with several aggregates**: split into simple measures and a
  derived metric (`SUM(a) / COUNT(DISTINCT c)` -> `a_sum`, `c_count` + `a_sum / c_count`).
- **stddev/variance**: left TODO (no such aggregate is written); derived metric from `sum`, `sum of squares`
  and `count`, or a SQL query.
- **Relations that are not key equalities** (ranges, composite keys): add a SQL model
  with the join, or composite entity `expr`.

## Parity

Hex SQL cells are the source of truth: re-run the original SQL on the warehouse and
compare with the migrated tile. For chained cells use the inlined SQL from
`queries/` on the source side only after checking it against the Hex result once
(download the cell result as CSV from Hex).

```yaml
name: Revenue Overview - Monthly revenue EU
source:
  csv: migration/parity/data/revenue_overview_monthly_revenue_eu.csv   # Hex cell result download
target:
  notebook: notebooks/revenue-overview.ipynb
  tile: monthly_revenue
  params: {region: EU, start_date: '2026-01-01'}
keys: [month, region]
```

For semantic models, compare a Hex Explore on the semantic model (same measures,
group-by, filters) with the metric's SQL run on the warehouse, grouped by month.
Pin the date window on both sides.

## Alternatives

- **Hex MCP server** (hosted, OAuth): list/search projects, read cells, run projects;
  good for spot checks and for reading a project the token can't see.
- **Hex CLI** (`hex`): `hex context semantic-project pull` for semantic YAML; project
  export for the Git YAML when Git sync isn't set up.
- `POST /project/{id}/run` (API) re-runs a published project with input overrides;
  useful to regenerate numbers for parity with pinned inputs.

## Gotchas

- The Git export schema is undocumented; unshackle reads `cellType`, `config.source`,
  `config.resultVariable`, chart `x/y/series/chartType` and `appLayout.rows` and
  warns on anything else. Check a real export against `export/*.yaml` once.
- In API-only mode the dataframe name is guessed from the cell label; chained SQL is
  only inlined when the label matches the name the downstream cell uses.
- Cells in the notebook but not in the app layout are kept as queries, not tiles.
- Components/imported cells from other projects show up as unknown cell types; migrate
  the source project too.
