# Lightdash

unshackle reads Lightdash's explores, metrics, charts and dashboards and writes them as semantic-layer files, notebooks and policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `LIGHTDASH_URL` | The address you open Lightdash at, no path: `https://app.lightdash.cloud` or your self-hosted URL |
| `LIGHTDASH_API_KEY` | A personal access token: Settings > Personal access tokens |
| `LIGHTDASH_PROJECT` | The project UUID, from the URL: `/projects/<uuid>/...` |
| `LIGHTDASH_DBT_PROJECT_DIR` | Optional: a local copy of your dbt project, to also copy its schema YAML |

- The token acts as its user. Use an admin, or a project admin who can see every space. Private spaces the user can't open are silently missing.
- Explores Lightdash can't compile are skipped and reported.

`unshackle check lightdash --prompt` asks for the non-optional ones and saves them to `.env` (git-ignored). `unshackle check lightdash` tests the connection.

## Run

```bash
unshackle migrate lightdash
```

Inventories, extracts and builds in one go. `--only <dashboard>` limits it to one dashboard, its charts and the explores they use. `--only` also takes a chart slug or an explore.

## What you get

| In Lightdash | In your repo |
| --- | --- |
| Explores and dbt models | Tables in `catalog/`, semantic models in `semantic/` |
| Dimensions, metrics, joins | Ossie dimensions, measures, metrics and entities in `semantic/` |
| Dashboards | Notebooks in `notebooks/`, with dashboard filters as parameters |
| Charts on no dashboard | Verified queries in `queries/` |
| `sql_filter` with user attributes | Row filters in `policies/` |
| `required_attributes` | Cedar policies in `policies/` |
| AI hints | Markdown in `knowledge/` |

Tile SQL is what Lightdash itself compiles for each chart.

## What needs finishing

- Custom SQL metrics with raw aggregates: split into simple measures and a ratio.
- `average_distinct` and `sum_distinct` metrics: pre-aggregate in a SQL model first.
- Metric filters on relative dates: rewrite as a time filter, or apply at query time.
- Table calculations like running totals or percent of total: add window functions to the tile SQL.
- Dashboard filters: add them to each tile's SQL as parameters with a default.
- `required_attributes` groups: map them to your real identity-provider groups.
- SQL chart tiles and unsupported chart types: copy the SQL or pick a chart type.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Import the dbt project Lightdash reads its metrics from. If you can run the Lightdash CLI, add the `lightdash download` output for charts and dashboards.

```bash
unshackle import lightdash path/to/dbt-project
unshackle build lightdash
```

The files can't carry:

- Compiled explores: fields come from the dbt YAML instead.
- Chart SQL: every chart tile gets a TODO to write its SQL from the semantic layer.
- Usage: no view counts and no links back to Lightdash.
- Space and project access: only `required_attributes` and `sql_filter` from the dbt YAML.

## Check the numbers

Lightdash's compileQuery API returns exactly the SQL Lightdash runs for a chart. Pin the dates, use that SQL as the source side, and compare it with the notebook tile. For numbers from Lightdash itself, run the chart and download a CSV. Write each comparison as a case in `migration/parity/` and run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

[source-lightdash guide](../../skills/source-lightdash/SKILL.md): written for coding agents and anyone finishing the migration.
