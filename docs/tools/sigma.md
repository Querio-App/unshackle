# Sigma

unshackle takes your Sigma data models, workbooks, metrics, controls, column security and team grants out of Sigma and turns them into Ossie metrics, notebooks and Cedar policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `SIGMA_API_URL` | API host of your Sigma cloud, no path, e.g. `https://api.sigmacomputing.com` or `https://aws-api.sigmacomputing.com`. Administration > Developer access shows it |
| `SIGMA_CLIENT_ID`, `SIGMA_CLIENT_SECRET` | Administration > Developer access > Create new: **REST API** client credentials |

- Create the credentials as (or for) an **Admin** with access to every folder and workspace.
- Credentials act as their owner. Anything the owner can't open is missing and listed in the report.
- The API host depends on your Sigma cloud (GCP, AWS, Azure, region). Use the one Developer access shows.

`unshackle check sigma --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check sigma` tests the connection.

## Run

```bash
unshackle migrate sigma
```

Lists data models and workbooks, pulls their definitions, generated SQL and grants, and writes the repo. Add `--only <workbookId|dataModelId|name>` to migrate one; repeat it for several.

The API has no view counts. To pick the most used workbooks, count views per workbook in Sigma's audit logs.

## What you get

| In Sigma | In your repo |
| --- | --- |
| Data model element on a warehouse table | Catalog entry and Ossie semantic model |
| Custom SQL or union element | Query in `queries/` to materialize, plus a semantic model on it |
| Columns and calculated columns | Dimensions |
| Relationships and joins | Entities between models |
| Metrics (sum, count, ...; with conditions; ratios; cumulative) | Simple, filtered, ratio, derived or cumulative metrics |
| Row security column (`CurrentUserAttributeText`) | Row filter |
| Column-level security | Cedar `forbid` on the column |
| Workbook grants (view, explore, edit) | Cedar `permit` on the notebook |
| Workbook | Notebook: KPI, bar, line, area, scatter, pie, table and text tiles running Sigma's own SQL, in page layout order |
| Controls | Dashboard filters with their defaults |

## What needs finishing

- **Controls**: their value is baked into the tile SQL. Swap it for the named bind and set a default.
- **`Lookup` and `Rollup`**: use the migrated relationships, or a measure on the child model.
- **Window and date/text functions** (`CumulativeSum`, `MovingAvg`, `Rank`, `DateDiff`, `Contains`, ...): rewrite in your warehouse's SQL.
- **Row security with teams or email**: map Sigma teams and user attributes to your identity provider's groups.
- **Column-level security**: check each sensitive column in the Sigma UI and add the rule if none was written.
- **Unions** that can't be lined up, and **pivots, maps, funnels and combo charts**: write by hand or pick the closest chart.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

API only. Sigma's downloads are data (CSV, Excel, PDF), not workbook or data model definitions, so `unshackle import sigma` isn't supported.

## Check the numbers

Sigma's generated SQL for each element is the reference; unshackle keeps a raw copy of it. Alias it like the tile, pin the date window, and compare. You can also compare against a CSV downloaded from the element in Sigma. Pin relative controls on both sides.

Write each comparison as a case in `migration/parity/` and run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-sigma guide](../../skills/source-sigma/SKILL.md) has the full details, written for coding agents and anyone finishing the migration.
