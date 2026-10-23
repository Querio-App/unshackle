# Changelog

## Unreleased

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
