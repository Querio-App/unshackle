# QuickSight fixtures

`bundle/` is a hand-written asset bundle in the layout `StartAssetBundleExportJob` writes with
`ExportFormat: QUICKSIGHT_JSON`, `IncludeAllDependencies`, `IncludePermissions` and
`IncludeTags`, unzipped. It describes content over the fictional Dunder Mifflin tables of
`tests/data/dunder_mifflin/` in schema `dunder_mifflin` (Athena data source). The account id is
`111122223333`, the region `us-east-1`, the owning user `pbeesly` (`user/default/pbeesly`) and the
groups `Sales`, `Analysts` and `Accounting`.

`tests/test_quicksight.py` checks that every file is a valid `Create*` request in botocore's
QuickSight model, with no member the model lacks, and that every member the parser reads exists
in that model. Numbers the tests check are computed from `tests/data/dunder_mifflin/` with DuckDB
and pandas.

## The bundle format

- Layout: one folder per asset type, `analysis/`, `dashboard/`, `dataset/`, `datasource/`, and
  one `<id>.json` per asset. No manifest, no top-level folder in the zip.
- Each file has a `resourceType` (`dashboard`, `analysis`, `dataset`, `datasource`, all lower
  case), the asset's `Create*` members, `permissions` and `tags`.
- Keys are camelCase, with irregular ones: `kpivisual` (not `kpiVisual`), `xaxisLocation` /
  `yaxisLocation` on free-form layout elements. The parser matches keys case-insensitively.
- Timestamps are epoch milliseconds (`timeRangeFilter.rangeMinimumValue.staticValue:
  1751328000000`); the Describe* API returns ISO strings.
- Calculated field expressions may drop the braces around plain field names (`status = 'X'`)
  and write `${visualDimensions}`.
- Text box content is indented markup: a line break and spaces around each tag, with a typed
  space next to a tag kept as a non-breaking space.

## Content

| File | What it covers |
| --- | --- |
| `bundle/datasource/dunder-mifflin.json` | Athena data source, workgroup `analytics`, permissions |
| `bundle/dataset/order-lines.json` | `RelationalTable` orders, accounts and products with two LEFT joins (`UniqueKey`; the right-hand key renamed to `account_id[accounts]` / `product_id[products]`, which QuickSight requires), a rename, a cast, calculated columns (row-level and cross-table), a filter, a projection, column-level security, tag-based RLS |
| `bundle/dataset/shipments.json` | `CustomSql` (orders joined to shippings), a calculated column, user-based RLS (`GRANT_ACCESS`, `VERSION_2`) |
| `bundle/dataset/region-entitlements.json` | the RLS rules dataset (`useAs: RLS_RULES`): `UserName`, `GroupName`, `shipping_region` |
| `bundle/dashboard/paper-sales.json` | KPI, line by month, clustered bar with colour, pie, aggregated table, combo (bars + line), Sankey (TODO), text box; a parameter with a dropdown control, a parameter-bound category filter, a select-all filter list with a control, a numeric range filter, a relative date filter (TODO); 19 calculated fields covering the deterministic and TODO cases |
| `bundle/analysis/paper-sales-analysis.json` | the same sheet plus a Deep dive sheet with a free-form layout: heat map, scatter plot, pivot table, a running-sum table (TODO), a geospatial map (TODO), a static time range filter |
