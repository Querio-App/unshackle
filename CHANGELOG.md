# Changelog

## Unreleased

- The PyPI page links to the repository, issues and changelog.
- Redash: multi-value enum and query dropdown parameters in `IN ({{ p }})` translate to `IN :p` with a
  list default (bound with SQLAlchemy's expanding parameters); static widget values with several values are
  joined as Redash does. Parameters spliced as raw SQL or inside a longer string get TODOs that say what to do.
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
  `unshackle[quicksight]`. Fixtures follow the service model; the live test hasn't run yet.
- Heat maps: tiles can use the `rect` mark.

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
