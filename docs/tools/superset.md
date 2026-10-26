# Apache Superset

unshackle reads Superset's datasets, metrics, charts, dashboards, roles and row-level security and writes them as semantic-layer files, notebooks and policies in your repo.

## Install

```bash
uv tool install "unshackle[run]"
```

Superset needs no extra.

## Connect

| Variable | Where to find it |
| --- | --- |
| `SUPERSET_URL` | The address you open Superset at, no path: `https://superset.example.com` |
| `SUPERSET_USERNAME` / `SUPERSET_PASSWORD` | A database or LDAP user that can log in with a password |
| `SUPERSET_AUTH_PROVIDER` | Optional: `db` (default) or `ldap` |
| `SUPERSET_API_KEY` | Instead of a password: a bearer token |

- Use an Admin. Other roles only export what they can see, and usage is admin-only.
- Row-level security applies to the user you connect as. A user under an RLS rule gets that rule compiled into every tile's SQL.
- Roles need `FAB_ADD_SECURITY_API = True` in `superset_config.py`. Without it, no Cedar policies are written.
- SSO-only instances have no password login: create a database user for the migration, or pass a bearer token in `SUPERSET_API_KEY`.

`unshackle check superset --prompt` asks for the non-optional ones and saves them to `.env` (git-ignored). `unshackle check superset` tests the connection.

## Run

```bash
unshackle migrate superset
```

Inventories, extracts and builds in one go. It only reads: Superset compiles each chart's SQL but runs nothing on the warehouse. `--only dashboard:<id>` (or `chart:<id>`, `dataset:<id>`) limits it to one item.

## What you get

| In Superset | In your repo |
| --- | --- |
| Physical datasets | Tables in `catalog/`, semantic models in `semantic/` |
| Metrics and calculated columns | Ossie measures, metrics and dimensions in `semantic/` |
| Virtual datasets (SQL) | `queries/` plus a semantic model, with a TODO to materialize |
| Dashboards | Notebooks in `notebooks/`, select filters as parameters |
| Charts on no dashboard, SQL Lab saved queries | `queries/` |
| Roles, dashboard roles | Cedar policies in `policies/` |
| Row-level security | Row filters in `policies/` |

Tile SQL is what Superset itself compiles for each chart.

## What needs finishing

- Metrics beyond simple aggregates and ratios: split into measures and a ratio or derived metric.
- Integer ratios: decide whether the business meant integer division.
- Jinja in datasets, metrics or RLS clauses: rewrite each one.
- Post-processing like rolling, cumulative or time comparison: add it in SQL or pandas.
- Time and time-grain dashboard filters: add parameters to the tiles named in the TODO.
- Charts saved without a query context: re-save in Superset and extract again, or write the SQL.
- Unsupported chart types and pivot tables, and missing primary keys on datasets.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Select dashboards, charts or datasets on their list page and choose **Export**, or use `superset export-dashboards`. Import the zip (or the unzipped folder):

```bash
unshackle import superset dashboard_export_<timestamp>.zip
unshackle build superset
```

The files can't carry:

- Chart SQL: the export has chart settings, not compiled SQL, so tiles carry a TODO to write it.
- Dashboard filters: without SQL, select filters are listed per tile instead of bound as parameters.
- Permissions: roles and row-level security are not in the export.
- Usage: no view counts and no links back to Superset.

## Check the numbers

Superset's own chart results are the source side: post the chart's saved query to its chart data API as CSV, with the dashboard filter values set. Write each comparison as a case in `migration/parity/` and run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

[source-superset guide](../../skills/source-superset/SKILL.md): written for coding agents and anyone finishing the migration.
