# Changelog

## Unreleased

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
