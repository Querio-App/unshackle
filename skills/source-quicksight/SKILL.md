---
name: source-quicksight
description: Use when migrating Amazon QuickSight (Amazon Quick Sight; datasets with joins and calculated fields, analyses, dashboards, parameters and filters, row-level and column-level security) with unshackle - AWS credentials and the read-only IAM policy, the asset bundle export (or a .qs file imported by hand), what `unshackle build quicksight` translates, finishing calculation, filter and visual TODOs, and parity.
---

# Amazon QuickSight source

QuickSight is now called Amazon Quick Sight (part of Quick Suite); the API, the CLI
(`aws quicksight`) and the IAM namespace are still `quicksight`. unshackle reads it with
boto3 and saves QuickSight's own export, the **asset bundle** (`StartAssetBundleExportJob`
with `ExportFormat: QUICKSIGHT_JSON`, `IncludeAllDependencies`, `IncludePermissions`,
`IncludeTags`): one JSON file per dashboard, analysis, dataset and data source. QuickSight
has no API that compiles a visual's SQL, so unshackle **generates tile SQL** from the
dataset definition (joins, calculated fields) and the visual's field wells and filters.

The parser reads only members that exist in botocore's QuickSight API model.

## Access

Install the extra: `uv tool install "unshackle[run,sql,quicksight]"` (adds boto3).

| Variable | Value |
| --- | --- |
| AWS credentials | the standard boto3 chain: `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, SSO, instance roles |
| `QUICKSIGHT_REGION` | the region the assets live in (`us-east-1`, the region in the QuickSight browser address); defaults to the profile's region |
| `QUICKSIGHT_ACCOUNT_ID` | the 12-digit AWS account id (the account menu at the top right of the AWS console); defaults to `sts get-caller-identity` |
| `QUICKSIGHT_NAMESPACE` | for users and groups; default `default` |
| `QUICKSIGHT_IDENTITY_REGION` | where users and groups live when it isn't `QUICKSIGHT_REGION` (QuickSight serves `ListUsers` / `ListGroups` only in the identity region) |

Read-only IAM policy for the extracting principal. `Describe*` and `List*` only read;
`StartAssetBundleExportJob` writes nothing to the account, it packages existing assets
into a file:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "UnshackleReadQuickSight",
      "Effect": "Allow",
      "Action": [
        "quicksight:List*",
        "quicksight:Describe*",
        "quicksight:StartAssetBundleExportJob"
      ],
      "Resource": "*"
    }
  ]
}
```

Or attach the AWS managed policy `AWSQuickSightAssetBundleExportPolicy` for the bundle
export and add `quicksight:ListUsers`, `quicksight:ListGroups`,
`quicksight:ListGroupMemberships` and the `Describe*Permissions` actions. Without the
export permission, extract falls back to `DescribeDashboardDefinition`,
`DescribeAnalysisDefinition`, `DescribeDataSet`, `DescribeDataSource` and their
`Describe*Permissions`, which parse the same way.

- The principal only sees assets it has permissions on. Use an admin, or share the
  dashboards and datasets with the extracting user first.
- **Enterprise edition** is needed for row-level security, column-level security and
  groups. A Standard edition account has none of them, so no row filters or column
  forbids come out.
- Datasets uploaded from files (CSV, Excel) and SaaS sources have no warehouse table:
  their models get a TODO to load the data into the warehouse first.

## Commands

```
unshackle inventory quicksight          # dashboards, analyses, datasets, data sources (no view counts in the API)
unshackle extract quicksight [--only dashboard:<id> --only analysis:<id> --only dataset:<id>]
unshackle build quicksight
unshackle todo
```

`--only` puts those ARNs in the export; `IncludeAllDependencies` adds the datasets, data
sources and RLS rules datasets they use. Extract polls the export job every few seconds
(up to 15 minutes) and downloads the `.qs` file from its presigned URL.

Raw files under `migration/raw/quicksight/`:

| File | From |
| --- | --- |
| `account.json` | account, region, namespace |
| `lists.json` | `ListDashboards`, `ListAnalyses`, `ListDataSets`, `ListDataSources` |
| `bundle/<type>/<id>.json` | the asset bundle, unzipped as is (camelCase keys) |
| `export_job.json` | `DescribeAssetBundleExportJob`: status, errors, warnings |
| `api/<kind>/<id>.json` | the fallback: `Describe*` responses with `Permissions` added (PascalCase keys) |
| `identity/users.json`, `identity/groups.json` | `ListUsers`, `ListGroups` with `ListGroupMemberships` |
| `_errors.json` | calls that failed (a refused export, a permission call) |

## Manual import (no API access for unshackle)

Anyone with the permissions above can run the export with the AWS CLI and hand over
the file:

```bash
aws quicksight start-asset-bundle-export-job --aws-account-id 111122223333 \
  --asset-bundle-export-job-id unshackle-1 --export-format QUICKSIGHT_JSON \
  --include-all-dependencies --include-permissions --include-tags \
  --resource-arns arn:aws:quicksight:us-east-1:111122223333:dashboard/<id>
aws quicksight describe-asset-bundle-export-job --aws-account-id 111122223333 \
  --asset-bundle-export-job-id unshackle-1          # repeat until SUCCESSFUL, then download DownloadUrl
unshackle import quicksight assetbundle-unshackle-1.qs
unshackle build quicksight
```

`import` accepts the `.qs` file (a zip) or the folder it unzips to. It carries the same
definitions as `extract`; it lacks the users and groups listing, and permissions unless
the job used `--include-permissions`.

## What is deterministic

| QuickSight | Becomes |
| --- | --- |
| data source `Type` | warehouse type (`REDSHIFT`, `POSTGRESQL`/`AURORA_POSTGRESQL`, `SNOWFLAKE`, `BIGQUERY`, `DATABRICKS`); Athena, Presto, Trino and Starburst are reported but not a type unshackle sets, so pick one in `unshackle.toml` |
| `RelationalTable` | `catalog/` table + one semantic model per physical table; `InputColumns` -> dimensions (`DATETIME` -> time); columns a `ProjectOperation` drops are hidden |
| `LogicalTableMap` `JoinInstruction` (`OnClause` `{a} = {b[alias]}`, `UniqueKey`) | foreign entity on the many side, primary entity on the unique side; tiles use the join as written (`LEFT`, `INNER`, `RIGHT`, `OUTER`), with filters of a left join's right side in its `ON` |
| `RenameColumnOperation`, `CastColumnTypeOperation` to `STRING` or `INTEGER` -> `DECIMAL` | the dimension name / `cast(x as varchar)` / nothing |
| `CustomSql` | `queries/<name>.sql` (TODO: materialize as a SQL model) + a semantic model named after it |
| calculated field, row-level (`ifelse`, `coalesce`, `isNull`, `in`, `concat`, `toUpper`, `substring`, `truncDate`, `extract`, `dateDiff` in days, arithmetic) | dimension with the SQL; `/` becomes `1.0 * a / nullif(b, 0)`, as QuickSight divides as decimals and returns NULL for zero |
| `sum` `avg` `min` `max` `count` `distinct_count` `median` of a row expression | measure + simple metric |
| `sumIf` `countIf` `avgIf` `distinct_countIf` ... | measure over `case when <condition> then x end` |
| aggregate / aggregate | ratio metric (helper `<name>_numerator` / `_denominator` measures, or the aggregate calculated fields it names) |
| dashboard / analysis | `notebooks/<slug>.ipynb`; each sheet is a `## ` heading when there are several; grid layouts by row then column, free-form by position |
| `BarChartVisual` | bar: category -> x (y when `HORIZONTAL`), first value -> y, colour -> color (+ `xOffset` when `CLUSTERED`), first small-multiple field -> column facet |
| `LineChartVisual` | line (`AREA` / `STACKED_AREA` -> area); `DateGranularity` truncates the x axis in SQL; several values -> layers |
| `PieChartVisual`, `HeatMapVisual`, `ScatterPlotVisual` | arc (theta, color) / rect (x columns, y rows, color value) / point (x, y, size, color or detail) |
| `KPIVisual`, `GaugeChartVisual` | KPI on the first value |
| `TableVisual` (aggregated or not), `PivotTableVisual` | table tiles |
| `ComboChartVisual` | bar layers (`BarValues`) and line layers (`LineValues`), `dual_axis` unless `SingleAxisOptions` puts them on one axis |
| `TextBoxes` | markdown cells (tags stripped) |
| parameters (`ParameterDeclarations`) | notebook parameters (`StatusParam` -> `status_param`), default from `StaticValues`; `${Param}` in calculated fields becomes `:status_param` in tile SQL |
| `FilterGroups` in scope (`AllSheets`, sheets, selected visuals; `SINGLE_DATASET` / `ALL_DATASETS`) | `WHERE` in each tile's SQL: category lists (`IN`, `NOT IN`, `LIKE`), numeric ranges and equality, static time ranges (at the filter's granularity), `NullOption` |
| filter bound to a parameter, or with a filter control | `(col = :name OR :name IS NULL)`: an empty control is no filter, as in QuickSight |
| dashboard / analysis permissions | `permit view` on the notebook; with `UpdateDashboard` / `UpdateAnalysis` also `edit` |
| dataset permissions | `permit query` on its tables; data source permissions -> `query` on the datasource (with a TODO to narrow it) |
| `ColumnLevelPermissionRules` | `forbid query` on each column unless the listed groups/users |
| user-based RLS, one field (`RowLevelPermissionDataSet`, rules dataset on a warehouse table) | row filter: `field in (select ... from rules where user/group matches)` or a rule with an empty field (all values); `DENY_ACCESS` negates it |
| tag-based RLS (`RowLevelPermissionTagConfiguration`) | row filter `col = {{ user.<tag> }}`, with `MatchAllValue` as an "all" escape |

Principals: `arn:...:group/<namespace>/<name>` -> `Group::"<name>"`, `user/<namespace>/<name>`
-> `User::"<name>"`, a namespace ARN -> everyone (with a TODO).

## What needs a human

- **Table calculations** (`runningSum`, `percentOfTotal`, `periodOverPeriod*`,
  `periodToDate*`, `rank`, `denseRank`, `lag`, `lead`, `difference`, `windowSum`...):
  they run over the visual's aggregated rows. TODO metric with the original expression;
  write them as window functions over the tile's grouped SQL, or in pandas.
- **Level-aware calculations**: `sum(x, [dims])` and `sumOver(x, [p], PRE_AGG)` translate
  to `sum(x) over (partition by ...)`, but a model can't group by a window: the
  dimension is a TODO with that SQL, to add as a column of a SQL model.
  `PRE_FILTER`, `POST_AGG_FILTER` (the default for `*Over`) and `$visualDimensions`
  depend on the visual: TODO.
- **Dialect-dependent functions**: `dateDiff` translates only in days (`DD`) and only
  when the data source names the dialect; `median` in tile SQL only on Snowflake,
  Redshift and Databricks; `truncDate('WK')` and week granularity (QuickSight weeks start
  on Sunday), `extract('WD')`, `parseDate`, `formatDate`, `addDateTime`, `now`,
  `locate`, `right`: TODO.
- **Cross-table calculated columns** (columns from two joined tables): TODO on the base
  table's model with the translated SQL; put them in a SQL model that joins the tables.
- **Dataset filters** (`FilterOperation`): applied in tile SQL; on the semantic model
  they are a TODO (add them to a SQL model or to each metric).
- **Filters**: relative dates and rolling dates (pin them to a closed range), top/bottom,
  nested and hierarchy filters, filters on aggregated values (`HAVING`), multi-valued
  parameters, a control whose default selects several values: TODO on the tile; the SQL
  is written without that filter.
- **Visuals** with no Altair mapping: geospatial, filled and layer maps, Sankey, tree map,
  funnel, waterfall, histogram, box plot, word cloud, radar, insight, custom content and
  plugin visuals. KPI targets and trends, forecasts, 100% stacked bars, several value
  fields on a bar, pivot column headers: TODOs.
- **RLS**: confirm that `{{ user.email }}` holds the QuickSight user name and
  `{{ user.groups }}` the QuickSight groups (both are always a TODO); rules on several
  fields need a correlated `EXISTS`; `FormatVersion: VERSION_1` rules may treat empty
  cells differently; tag-based rules apply to embedding sessions, so map each tag to a
  user attribute.
- **Datasets on datasets** (`LogicalTableSource.DataSetArn`), the newer data
  preparation steps (`DataPrepConfiguration` without a `LogicalTableMap`), file and SaaS
  sources: warnings; rebuild the model from the raw JSON.
- **Analyses** become notebooks too (slug from the analysis name). When an analysis is
  just the authoring copy of a dashboard, delete one of them.

## Parity

QuickSight has no API that returns a visual's rows except `StartDashboardSnapshotJob`,
which needs the Pixel Perfect add-on (and capacity pricing for anonymous users) and
only exports table and pivot table visuals as CSV:

```python
qs.start_dashboard_snapshot_job(
    AwsAccountId=acct, DashboardId="sales-overview", SnapshotJobId="parity-1",
    UserConfiguration={"AnonymousUsers": [{"RowLevelPermissionTags": [{"Key": "account_manager", "Value": "*"}]}]},
    SnapshotConfiguration={"FileGroups": [{"Files": [{"FormatType": "CSV", "SheetSelections": [
        {"SheetId": "overview", "SelectionScope": "SELECTED_VISUALS", "VisualIds": ["table-accounts"]}]}]}],
        "DestinationConfiguration": {"S3Destinations": [{"BucketConfiguration": {
            "BucketName": "my-bucket", "BucketPrefix": "parity", "BucketRegion": "us-east-1"}}]}})
# poll describe_dashboard_snapshot_job until COMPLETED, then describe_dashboard_snapshot_job_result for the S3 URI
```

The CSV headers are QuickSight's display names: map them with `columns:`. Otherwise:

- Export the visual from the dashboard (**Export to CSV** on a table visual) for the
  agreed filter values, save it under `migration/parity/data/`.
- On Athena or Redshift, QuickSight's own queries are in the warehouse's query history
  (Athena: `ListQueryExecutions` in the workgroup the data source uses) after someone
  opens the dashboard: run that SQL as the case's `source.sql`.

```yaml
name: Sales overview - Revenue by account, Status = COMPLETED
source:
  csv: migration/parity/data/revenue_by_account_completed.csv
target:
  notebook: notebooks/sales-overview.ipynb
  tile: revenue_by_account
  params: {status_param: COMPLETED}
keys: [account_name]
columns: {Revenue: revenue, "Average order value": average_order_value}
```

## Gotchas

- Direct query and SPICE datasets parse the same; numbers from SPICE are as of the last
  refresh, so pin parity to data older than the refresh.
- The asset bundle has one folder per asset type (`dashboard/`, `analysis/`, `dataset/`,
  `datasource/`) and one minified JSON file per asset with camelCase keys, some irregular
  (`kpivisual`, `xaxisLocation`). unshackle reads assets by `resourceType` and matches keys
  case-insensitively to the service model.
- The export writes timestamps as epoch milliseconds, drops the braces around plain field
  names in calculated fields (`status = 'X'`), writes `${visualDimensions}`, and indents
  text box markup. unshackle reads all of these.
- A dataset `filterOperation` in the export carries `enabled: false` (a member the API
  doesn't have) even when QuickSight applies the filter, so the flag can't tell an active
  filter from one switched off in the console. unshackle applies every dataset filter (and
  lists it in the model's TODO); check them against the dataset in QuickSight.
- A join whose right table has a column with the same name as one on the left needs a
  `RenameColumnOperation` to `name[<right table alias>]` on the right table; that's how
  `OnClause` refers to it. unshackle drops the suffix in the right table's model.
- A filter list (`FilterListConfiguration`, the checkbox list) only takes `CONTAINS` (keep
  the listed values) and `DOES_NOT_CONTAIN` (drop them): `IN` / `NOT IN`, not substrings.
- Export jobs and their download URLs expire; re-run extract rather than reusing a URL.
