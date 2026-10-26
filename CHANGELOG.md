# Changelog

## Unreleased

- Tableau hardening: generated SQL follows Tableau's semantics, and anything that can't be translated exactly stays a
  `TODO(unshackle)` instead of a wrong number.
  - Warehouse SQL: tile and model SQL follow the warehouse type in `unshackle.toml` (else the export's): BigQuery and
    Databricks quoting and date functions, Postgres `median`, Redshift and Postgres averages and division in double
    precision. Initial SQL and connection options are reported. Field names that are SQL reserved words are quoted.
  - Dates: weeks follow the data source's Week start (or its locale), weekday numbers run 1 (Sunday) to 7; results that
    depend on a fiscal year or an unknown week start stay TODO.
  - Calculations: NULLs, `IIF`, string `+`, `= TRUE`, the `NULL` literal and `ROUND` follow Tableau's rules per
    connection (live vs extract); `DATEDIFF`, `DATEADD`, `DATEPART`/`DATENAME`, `MAKEDATE`, `MAKEDATETIME`, `TODAY`,
    `IN`, two-argument `MIN`/`MAX`, string, conversion, math and `REGEXP_` functions translate. A data-driven case
    table covers every supported function.
  - Parameters in calculations (what-if, thresholds, measure and dimension switches) become `:name` binds from the
    notebook's parameters cell, which lists each parameter's values or range; custom SQL parameters become binds too.
  - Filters follow Tableau's order of operations (extract, data source, context, FIXED, top N and conditions,
    dimension, measure): relative date filters (`:as_of`), excludes that keep NULLs, exact top N with ties, condition
    and wildcard filters, range bounds within Tableau's tolerance, and extract and data source filters, including
    inside FIXED LODs. Context filters apply inside FIXED grains.
  - Relationships (the logical layer): one model per logical table related by Ossie relationships; tiles aggregate
    each related table on its own rows, so nothing is repeated, and filters and top N on those sheets follow Tableau.
  - Data blending: each secondary source is aggregated at its linking fields and left-joined to the primary; a
    secondary string dimension shows its value or `*`.
  - Table calculations become window SQL over the sheet's marks: running totals, percent of total, differences,
    ranks, moving calculations, `WINDOW_*`, `LOOKUP`, `INDEX`, `TOTAL`, with Compute Using and the sheet's sort;
    crosstabs are padded the way Tableau pads them, including Marks card, aliased and computed-sorted dimensions.
  - LODs: an INCLUDE or EXCLUDE nested in a FIXED, and nested LODs inside larger expressions and with context
    filters, translate.
  - Sets, groups and bins (fixed, conditional and top/bottom N sets; Include Other; NULL members) translate as
    dimensions, In / Out and filters.
  - Display: aliases show as labels, sheet and default sorts become the tile's `order by`, and number formats go to
    notebook axes, tooltips and Ossie.
  - Charts: Measure Names/Values sheets unpivot in Tableau's measure order; dual axis from the folded axis; square
    marks as `mark_rect`; maps for US states, countries and latitude/longitude (shapes from a pinned CDN); text
    sheets with several measures as tables. Reference lines and bands become window columns and rule layers; totals,
    trend lines and forecasts are TODOs that hold the SQL or name the model.
  - Dashboards: dashboard filter controls and filter actions become notebook parameters bound in their target tiles;
    URL actions become links; stories become one notebook with a section per story point; hidden fields no sheet
    uses get no metrics; folders go to the glossary; what isn't migrated is listed per dashboard in the report.
  - Data that lives only in Tableau (Hyper files, Excel/CSV, application connectors, extracts with no connection) is
    flagged; `extract --with-data` and `import --with-data` (new `tableau` extra) copy it to Parquet with Tableau's
    column types, `build` points the models at the copies, and `init` keeps the copies out of Git. Live Excel and
    text files follow the extract calculation rules.
  - Formats: workbooks from Tableau 8.2 to 2024 parse; cross-database joins are reported, with TODOs where SQL would
    read two databases, and same-named tables from different databases get separate models.
  - Robustness: a failure in one workbook, data source, calculation, sheet or dashboard becomes a warning naming it
    and the rest migrates; `migration/report.md` counts unread XML and TODOs by kind; a sheet with Aggregate Measures
    off and a second calculation with an existing name become TODOs instead of wrong SQL.
- Looker: dashboards built from LookML files get tile SQL in more cases.
  - Relative date filters on BigQuery follow Looker's grammar for dates and timestamps: `N <unit>`, `N <unit> ago`,
    `N <unit> ago for M <unit>`, `before` / `after N <unit> ago` and `this` / `last` / `next <unit>`, from second to
    year, with weeks on the model's `week_start_day`; comma-separated values are OR. Seconds count from the current
    moment. Forms Looker rejects or leaves empty (`this` / `next` / `last` second, minute, hour), other warehouses
    and sub-day units on dates stay TODO.
  - Tiles on persistent derived tables read the materialized table instead of rendering its build-time Liquid; in
    the derived-table query, `{% incrementcondition %}` becomes a full rebuild with a TODO about `increment_key`.
  - `migrate` checks a source's required variables before writing anything and, when API credentials are missing,
    points to `unshackle import`; `unshackle tools` shows Looker's required and optional variables.
  - Text tiles write their heading once, and simple HTML (bold, italic, line breaks, paragraphs, links) becomes
    Markdown.
- Tableau end-to-end fixes (extracts, `--with-data`, installs):
  - Each data source that is an extract gets its own semantic model, named after the data source with or without
    `--with-data`, so same-named calculations in two extracts no longer collide.
  - Calculations typed on a shelf (`usr:Calculation_...:qk`) translate in their sheet's tile SQL, including quick
    percent of total and running total, filters, Color and Label, without becoming metrics; an LOD typed on a shelf
    stays a TODO with its formula.
  - A sort on a boolean calculation no longer writes a chained comparison (`a > 0 = true`).
  - Aliases apply to members containing quotes, and members aliased to one label stay separate marks.
  - After `--with-data`, `queries/tableau-data.sql` loads DuckDB tables, so notebooks run from any folder; the CLI and
    `migration/report.md` give the steps, and the `tableau` extra includes `duckdb-engine`.
- `build` removes files an earlier build of the same tool wrote and this one no longer generates, and the folders that
  leaves empty. Files edited by hand are kept and listed in `migration/report.md`; files that existed before unshackle
  are never removed; `--dry-run` lists what would go.
- Install: every missing-extra error names the extra and the `uv tool install --force` command, `unshackle tools`
  shows each source's extra, and the README lists the install per tool.
- The PyPI page links to the repository, issues and changelog.
- Tests run offline only: the Dunder Mifflin data moved to `tests/data/dunder_mifflin/`, and the
  `live` extra and test marker are gone.
- Redash: multi-value enum and query dropdown parameters in `IN ({{ p }})` translate to `IN :p` with a
  list default (bound with SQLAlchemy's expanding parameters); static widget values with several values are
  joined as Redash does. Parameters spliced as raw SQL or inside a longer string get TODOs that say what to do.
- Qlik Sense source (Qlik Cloud): `unshackle extract qlik` through the engine (QIX) and REST APIs with an
  OAuth machine-to-machine client or an API key, `unshackle import qlik` for `qlik app unbuild` folders. Plain
  script loads, master measures (set modifiers with listed values, ratios, variables), sheets and inline
  Section Access translate; the rest of the load script, Aggr/TOTAL and searches are TODOs.
- ThoughtSpot: formulas with the two-word `unique count ( x )` parse, and `nullif ( a , b )`
  translates (a divisor `nullif ( b , 0 )` keeps the formula a ratio metric).
- Superset: `unshackle import superset` reads the export ZIP Superset's UI downloads (or
  the unzipped folder). Chart SQL, roles, row-level security and usage aren't in the
  export; tiles get a TODO and the report lists those gaps.
- Metabase: date, number and string dashboard filters on query-builder questions bind as
  notebook parameters (dates as `<slug>_from` / `<slug>_to`, number ranges as `_min` / `_max`).
  Relative date defaults,
  `timestamptz` columns and the time-grouping filter keep a TODO.
- Tableau: three more LOD shapes translate: an aggregate of a calc over FIXED values (`SUM(IF [Account Revenue] >= 1000000 THEN 1
  ELSE 0 END)`; a metric on the FIXED grain's model, and tiles), a FIXED aggregate inside
  an expression (`SUM([Sales]) / SUM([Account Revenue])`; tiles), and a FIXED nested in a
  FIXED (a `_lod` model of its own). Tableau takes the FIXED values once per distinct
  combination of the view's and the FIXED dimensions, and so does the generated SQL.
- The CLI shows a banner and a ship pushing through the ice while `migrate`, `inventory`,
  `extract`, `import` and `build` run, then a one-line summary. Interactive terminals only;
  `--quiet`, `NO_COLOR`, CI and piped output stay plain.
- The README banner no longer draws window buttons.
- Tableau: fixed: view CSVs failed
  with `406` (`Accept: text/csv`); workbooks on a published data source got no tables
  (the `sqlproxy` stub relation was read as a table); calcs of a published data source
  were translated twice (duplicate LOD queries and row filters); a column's default
  aggregation set in Desktop was ignored (`Avg` became `SUM`); join clauses were listed
  twice. Sheets over several joined tables now get the join in their SQL, filters on a
  calc that compares with a parameter bind `:parameter`, hierarchies go to the glossary,
  and workbooks the Metadata API hasn't indexed yet are reported.
- Power BI: tested on published projects (`tests/fixtures/powerbi_real`). DAX `VAR`/`RETURN`
  is inlined where that keeps its meaning; `CALCULATE` without filters and the `+ 0` idiom
  translate; Direct Lake tables map to their SQL endpoint table; thin reports find a model
  imported next to them. Fixed: lost parentheses and integer division in generated SQL,
  BLANK handling in `+`/`-`, entities on many-to-many or non-key relationships, tiles
  filtered to a calculation item, duplicate notebook cell ids.
- Amazon QuickSight source: the asset bundle export (`StartAssetBundleExportJob`,
  `QUICKSIGHT_JSON`) with `Describe*` calls as a fallback, `unshackle import quicksight
  <file.qs>`, tile SQL generated from datasets, calculated fields and filter groups,
  permissions, column-level security and row-level security. Install with
  `unshackle[quicksight]`.
- Heat maps: tiles can use the `rect` mark.
- Sigma: data model elements sourced from another data model's element become semantic models on
  that element's table; union elements become a `UNION ALL` query plus a semantic model when
  their columns line up, and keep a TODO otherwise. Both were skipped before.

## 0.1.0

First release.

- Sources: Looker, Tableau, Power BI, Lightdash, Omni, Hex, Sigma, ThoughtSpot,
  Metabase, Apache Superset and Redash, through their APIs (`unshackle extract`)
  or from files downloaded by hand (`unshackle import`).
- Outputs: Apache Ossie semantic models (plus MetricFlow in dbt projects),
  Jupyter notebooks with Altair/Vega-Lite charts, `.sql` queries, Cedar
  policies with SQL row filters, and Markdown.
- `unshackle verify` compares the source tool's numbers with the migrated SQL.
- Live end-to-end tests for Metabase, Lightdash, Superset and Redash.
