# Changelog

## Unreleased

- The PyPI page links to the repository, issues and changelog.
- Redash: multi-value enum and query dropdown parameters in `IN ({{ p }})` translate to `IN :p` with a
  list default (bound with SQLAlchemy's expanding parameters); static widget values with several values are
  joined as Redash does. Parameters spliced as raw SQL or inside a longer string get TODOs that say what to do.
- Qlik Sense source (Qlik Cloud): `unshackle extract qlik` through the engine (QIX) and REST APIs with an
  OAuth machine-to-machine client or an API key, `unshackle import qlik` for `qlik app unbuild` folders. Plain
  script loads, master measures (set modifiers with listed values, ratios, variables), sheets and inline
  Section Access translate; the rest of the load script, Aggr/TOTAL and searches are TODOs. Verified live.
- ThoughtSpot: formulas with the two-word `unique count ( x )` parse, and `nullif ( a , b )`
  translates (a divisor `nullif ( b , 0 )` keeps the formula a ratio metric).
- Superset: `unshackle import superset` reads the export ZIP Superset's UI downloads (or
  the unzipped folder). Chart SQL, roles, row-level security and usage aren't in the
  export; tiles get a TODO and the report lists those gaps.
- Metabase: date, number and string dashboard filters on query-builder questions bind as
  notebook parameters (dates as `<slug>_from` / `<slug>_to`, number ranges as `_min` / `_max`),
  checked against Metabase's own dashboard results in the live test. Relative date defaults,
  `timestamptz` columns and the time-grouping filter keep a TODO.
- The CLI shows a banner and a ship pushing through the ice while `migrate`, `inventory`,
  `extract`, `import` and `build` run, then a one-line summary. Interactive terminals only;
  `--quiet`, `NO_COLOR`, CI and piped output stay plain.
- The README banner no longer draws window buttons.
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
  `unshackle[quicksight]`. Fixtures are a real export; the live test checks the migrated
  SQL on Athena.
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
