# Published Power BI projects

Real Power BI projects (TMDL semantic models with PBIR or legacy reports), used by
`tests/test_powerbi_real.py` to run `unshackle import powerbi` and `unshackle build powerbi`
on models that people actually built. Each folder keeps the project's definition files
and its license, unchanged; local caches (`.pbi/`), bookmarks, themes and images were
left out. None of them carry data.

| Project | Source | Commit | License | What it exercises |
| --- | --- | --- | --- | --- |
| `sales` | [microsoft/Analysis-Services](https://github.com/microsoft/Analysis-Services/tree/master/pbidevmode/fabricps-pbip/SamplePBIP) `pbidevmode/fabricps-pbip/SamplePBIP` | `d3ccb5032c9029b097276b88412f7b61df6e73b8` | MIT | `.pbip` with legacy `report.json` (5 pages); DAX from `SUM`/`DISTINCTCOUNT`/`COUNTROWS` to `SUMX`, `DIVIDE`, `VAR`/`RETURN`, `TOTALYTD`, `SAMEPERIODLASTYEAR`, `DATESINPERIOD`, `USERELATIONSHIP`, `REMOVEFILTERS`, `HASONEVALUE`; two calculation groups; field parameters; a KPI on a measure; inactive relationship; hierarchies; calculated column and calculated table; two RLS roles; Power Query over CSV files; slicers and visual filters on a calculation group |
| `fabric-cost-analysis` | [microsoft/fabric-toolbox](https://github.com/microsoft/fabric-toolbox/tree/main/monitoring/fabric-cost-analysis/src) `monitoring/fabric-cost-analysis/src` (`FCA_Core_SM.SemanticModel`, `FCA_Core_Report.Report`) | `29264d36e5fdf483498faf97013ecf4dd0bd8a10` | MIT | Direct Lake model (entity partitions on a Fabric SQL endpoint); thin PBIR report bound `byConnection` (9 pages, 160 visuals, many slicers incl. duplicated ones); `CALCULATE` with filters on related tables, the `+ 0` idiom, `TOTALMTD`, running totals with `FILTER(ALLSELECTED(..))`, `SUMMARIZE` in a `VAR`; display folders; a bidirectional relationship |
| `query-capacity-correlation` | [microsoft/fabric-toolbox](https://github.com/microsoft/fabric-toolbox/tree/main/monitoring/query-capacity-correlation/Template) `monitoring/query-capacity-correlation/Template` | `29264d36e5fdf483498faf97013ecf4dd0bd8a10` | MIT | `.pbip` with PBIR report (`byPath`); composite model (DirectQuery to another semantic model, 349 `EXTERNALMEASURE` measures in a measure table); many-to-many and bidirectional relationships; relationships that don't target a table's key; date-range and value slicers |

Looked at and not used:

- `microsoft/powerbi-desktop-samples` (MIT): every sample is a `.pbix`, which unshackle
  doesn't read (binary model).
- `microsoft/fabric-samples` "Bank Customer Churn Analysis" (MIT): TMDL + legacy report,
  but one table and four measures; the projects above cover it.
- `pbi-tools/sales-sample`: the same Sales model as `sales`, without a license.
- Repositories saved in the pbi-tools folder layout (`src/Model`, `src/Report/sections`)
  rather than as a `.pbip` project.
