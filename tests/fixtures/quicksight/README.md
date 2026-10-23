# QuickSight fixtures

**`bundle/` is a real asset bundle exported from a live QuickSight account** (Enterprise
edition, `us-east-1`) by `tests/live/test_quicksight_live.py`: `StartAssetBundleExportJob`
with `ExportFormat: QUICKSIGHT_JSON`, `IncludeAllDependencies`, `IncludePermissions` and
`IncludeTags`, run by `unshackle extract quicksight` and unzipped as is. The data is the
synthetic Dunder Mifflin dataset of `tests/live/data/dunder_mifflin/` in Athena.

What was changed after the export, and nothing else:

- the account id is `123456789012` and the owning user is `jhalpert` (`user/default/jhalpert`);
- each file is pretty-printed (the export writes one minified line per file); key order and
  key casing are as exported.

Rerunning the live test with `QUICKSIGHT_LIVE_SAVE_FIXTURE=1` creates the content from these
files and writes the new export over them. `tests/test_quicksight.py` checks that every file
is a valid `Create*` request in botocore's QuickSight model and that every member the parser
reads exists in that model.

## What the export looks like

- Layout: one folder per asset type, `analysis/`, `dashboard/`, `dataset/`, `datasource/`, and
  one `<id>.json` per asset. No manifest, no top-level folder in the zip.
- Each file has a `resourceType` (`dashboard`, `analysis`, `dataset`, `datasource`, all lower
  case), the asset's `Create*` members, `permissions` and `tags`.
- Keys are camelCase, with irregular ones: `kpivisual` (not `kpiVisual`), `xaxisLocation` /
  `yaxisLocation` on free-form layout elements. The parser matches keys case-insensitively.
- Timestamps are epoch milliseconds (`timeRangeFilter.rangeMinimumValue.staticValue:
  1735689600000`); the Describe* API returns ISO strings.
- Calculated fields are sorted by name, and their expressions are rewritten: braces are dropped
  around plain field names (`{status}` becomes `status`) and `$visualDimensions` becomes
  `${visualDimensions}`.
- `filterOperation` has `enabled` and `inverse`, which are not in the API model.
  `enabled: false` comes back on filters created through the API that QuickSight does apply.
- Text box content is indented markup: a line break and spaces around each tag, with a typed
  space next to a tag kept as `&nbsp;`.
- Defaults are filled in: `subtitle`, `actions`, `sortConfiguration`, `columnHierarchies`,
  `dataSetUsageConfiguration`, `dashboardPublishOptions` and similar.

## Content

| File | What it covers |
| --- | --- |
| `bundle/datasource/dunder-mifflin-athena.json` | Athena data source, workgroup `primary`, permissions |
| `bundle/dataset/orders.json` | `RelationalTable` orders, accounts and products with two LEFT joins (`UniqueKey`; the right-hand key renamed to `account_id[accounts]` / `product_id[products]`, which QuickSight requires), a rename, a cast, calculated columns (row-level and cross-table), a filter, a projection, column-level security, tag-based RLS |
| `bundle/dataset/shipped-orders.json` | `CustomSql` (orders joined to shippings), a calculated column, user-based RLS (`GRANT_ACCESS`, `VERSION_1`: `UserName` / `GroupName` columns) |
| `bundle/dataset/region-access-rules.json` | the RLS rules dataset (`useAs: RLS_RULES`): `UserName`, `GroupName`, `shipping_region` |
| `bundle/dashboard/sales-overview.json` | KPI, line by month, clustered bar with colour, pie, aggregated table, combo (bars + line), Sankey (TODO), text box; a parameter with a dropdown control, a parameter-bound category filter, a select-all filter list with a control, a numeric range filter, a relative date filter (TODO); 19 calculated fields covering the deterministic and TODO cases |
| `bundle/analysis/sales-analysis.json` | the same sheet plus an Exploration sheet with a free-form layout: heat map, scatter plot, pivot table, a running-sum table (TODO), a geospatial map (TODO), a static time range filter |
