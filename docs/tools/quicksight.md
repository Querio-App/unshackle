# Amazon QuickSight

unshackle reads your QuickSight datasets, analyses, dashboards and security rules and writes them as metrics, notebooks and policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | Your usual AWS credentials (profiles, SSO and instance roles work too) |
| `QUICKSIGHT_REGION` | The region the assets live in, as in the QuickSight browser address. Defaults to the profile's region |
| `QUICKSIGHT_ACCOUNT_ID` | The 12-digit AWS account id, in the account menu at the top right of the AWS console. Defaults to the current identity's |
| `QUICKSIGHT_NAMESPACE` | For users and groups. Default `default` |
| `QUICKSIGHT_IDENTITY_REGION` | Where users and groups live, if not `QUICKSIGHT_REGION` |

- The principal needs read-only access: `quicksight:List*`, `quicksight:Describe*` and `quicksight:StartAssetBundleExportJob` (packages existing assets; writes nothing). The guide has the full IAM policy.
- It only sees assets it has permissions on. Use an admin, or share the dashboards and datasets with it first.
- Row-level security, column-level security and groups need **Enterprise edition**. On Standard, no row filters come out.

`unshackle check quicksight --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check quicksight` tests the connection.

## Run

```bash
unshackle migrate quicksight
```

Exports an asset bundle of your dashboards, analyses and datasets and writes the repo. Nothing changes in QuickSight. Use `--only dashboard:<id>` (or `analysis:<id>`, `dataset:<id>`) to narrow it; their dependencies come along.

## What you get

| In QuickSight | In your repo |
| --- | --- |
| Data sources and tables | Table and column descriptions (`catalog/`), semantic models (`semantic/`) |
| Dataset joins | Joins between models |
| Row-level calculated fields | Dimensions with SQL |
| Aggregate calculated fields (`sum`, `countIf`, ratios...) | Ossie metrics (`semantic/`) |
| Custom SQL | `queries/` |
| Dashboards and analyses | Notebooks, with SQL generated per visual (`notebooks/`) |
| Parameters and filters | Notebook parameters and `WHERE` clauses |
| Permissions, row- and column-level security | Cedar policies and row filters (`policies/`) |

## What needs finishing

- **Table calculations** (`runningSum`, `percentOfTotal`, `rank`, `lag`...): write them as window functions or in pandas.
- **Level-aware calculations**: add them as a column of a SQL model.
- **Dialect-dependent functions** (`dateDiff` beyond days, week truncation, `parseDate`, `formatDate`...).
- **Filters**: relative dates, top/bottom, filters on aggregated values, multi-valued parameters.
- **Visuals** with no chart mapping: maps, Sankey, tree map, funnel, waterfall, KPI targets, forecasts.
- **Row-level security**: confirm the user and group attributes map to QuickSight's users and groups.
- **Custom SQL, file and SaaS datasets**: materialize or load them into the warehouse.

`unshackle todo` lists them; the guide explains how to finish each.

## Without API access

Anyone with the permissions above can export an asset bundle (`QUICKSIGHT_JSON`, with dependencies and permissions) using the AWS CLI; the guide has the commands. Then:

```bash
unshackle import quicksight <bundle>.qs      # or the folder it unzips to
unshackle build quicksight
```

What the file can't carry:

- Usage: QuickSight has no view counts in its API, so nothing is sorted by views.
- The users and groups listing; policies use the names in the permissions.
- Permissions, unless the bundle was exported with them.
- Datasets: only those the exported dashboards and analyses use, and only if exported with dependencies.

## Check the numbers

QuickSight has no general API for a visual's rows. Export a table visual to CSV for the agreed filter values (or use a dashboard snapshot job, which needs the Pixel Perfect add-on), save it under `migration/parity/data/` and write a case in `migration/parity/`. On Athena or Redshift, you can also reuse QuickSight's own SQL from the query history. Then run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-quicksight guide](../../skills/source-quicksight/SKILL.md) has the full detail, written for coding agents and anyone finishing the migration.
