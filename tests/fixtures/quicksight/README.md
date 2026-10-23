# QuickSight fixtures

**These files follow the QuickSight service model; they are not a real export yet.**
They were written by hand against the QuickSight API model that ships with botocore
(`botocore/data/quicksight/2018-04-01/service-2.json`), and `tests/test_quicksight.py`
validates every file against the matching `Create*` request shape. They will be
replaced by a real asset bundle exported from a live account
(`tests/live/test_quicksight_live.py`) once that test has run.

The layout is what `unshackle extract quicksight` and `unshackle import quicksight
<file.qs>` write to `migration/raw/quicksight/`: `bundle/` is an unzipped
`QUICKSIGHT_JSON` asset bundle, one folder per asset type and one JSON file per asset,
with camelCase keys and a `resourceType` field. The folder names and the exact key casing
of a real `.qs` file are the parts most likely to differ; the parser reads assets by
`resourceType` (falling back to the id key and the folder name) and matches keys
case-insensitively, so both casings parse.

Content, over the Dunder Mifflin tables of `tests/live/data/dunder_mifflin/` in Athena
(database `dunder_mifflin`, account `111122223333`, `us-east-1`):

| File | What it covers |
| --- | --- |
| `bundle/datasource/dunder-mifflin-athena.json` | Athena data source, workgroup `primary`, permissions |
| `bundle/dataset/orders.json` | `RelationalTable` orders, accounts and products with two LEFT joins (`UniqueKey`), a rename, a cast, calculated columns (row-level and cross-table), a filter, a projection, column-level security, tag-based RLS |
| `bundle/dataset/shipped-orders.json` | `CustomSql` (orders joined to shippings), a calculated column, user-based RLS (`GRANT_ACCESS`, `VERSION_2`) |
| `bundle/dataset/region-access-rules.json` | the RLS rules dataset (`UseAs: RLS_RULES`): `UserName`, `GroupName`, `shipping_region` |
| `bundle/dashboard/sales-overview.json` | KPI, line by month, clustered bar with colour, pie, aggregated table, combo (bars + line), Sankey (TODO), text box; a parameter with a dropdown control, a parameter-bound category filter, a select-all filter with a control, a numeric range filter, a relative date filter (TODO); 19 calculated fields covering the deterministic and TODO cases |
| `bundle/analysis/sales-analysis.json` | the same sheet plus an Exploration sheet with a free-form layout: heat map, scatter plot, pivot table, a running-sum table (TODO), a geospatial map (TODO), a static time range filter |
| `bundle/theme/dunder-mifflin-theme.json` | a theme, which parse() ignores |
