# Redash

unshackle reads your Redash queries, visualizations, dashboards and groups and writes them as SQL, notebooks and policies in your repo. Redash has no semantic layer or row-level security, so there are no metrics or row filters to carry.

## Install

```bash
pip install unshackle      # or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `REDASH_URL` | The address you open Redash at, no path: `https://redash.example.com` |
| `REDASH_API_KEY` | A **user** API key: Edit Profile > API Key |

- Use an **admin**'s key. Other users only see queries on their groups' data sources, and groups, sharing and usage are admin-only.
- Not a per-query API key: those only read one query's results.
- unshackle only reads. It never runs a query.

`unshackle check redash --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check redash` tests the connection.

## Run

```bash
unshackle migrate redash
```

Reads everything through the API and writes the repo. Nothing changes in Redash. Use `--only dashboard:<id>` (brings its queries) or `--only query:<id>` to narrow it.

## What you get

| In Redash | In your repo |
| --- | --- |
| Saved queries | `queries/`, with `{{ parameters }}` as SQL binds |
| Dashboards | Notebooks, widgets in grid order (`notebooks/`) |
| Visualizations (line, column, area, scatter, pie, counter, table) | Chart, KPI and table tiles |
| Widget parameters | Notebook parameters |
| Groups, data source access, dashboard sharing | Cedar policies (`policies/`) |
| Query snippets | Markdown (`knowledge/`) |
| View events (last 90 days) | Usage per dashboard |

## What needs finishing

- **Parameters outside a quoted value**: column names, `ORDER BY {{ sort }}`, `'%{{ term }}%'`. Rewrite them over the bind.
- **Multi-value parameters**: bind the list in the notebook tile.
- **Relative dates** (`d_last_7_days`, `d_this_month`): set the default in the notebook.
- **Non-SQL data sources and Query Results**: rewrite them over the warehouse.
- **Filters on result columns** (`region::filter`): filter the dataframe or make them parameters.
- **Charts**: pivot, funnel, maps, cohort, Sankey and others; formatting isn't carried over.

`unshackle todo` lists them; the guide explains how to finish each.

## Without API access

Redash is API only: there is no file import.

## Check the numbers

Redash's own results are the source side: run the saved query through the API with the parameter values you want, write the rows to `migration/parity/data/` as CSV and write a case in `migration/parity/`. Column names are the SQL aliases, so no mapping is needed. Then run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-redash guide](../../skills/source-redash/SKILL.md) has the full detail, written for coding agents and anyone finishing the migration.
