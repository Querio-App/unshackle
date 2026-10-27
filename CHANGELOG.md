# Changelog

## 0.1.0 (2026-10-27)

First release.

- **Sources (13):** Tableau, Power BI, Looker, Qlik Sense, Amazon QuickSight, Metabase, Lightdash, Apache Superset,
  Redash, Omni, Hex, ThoughtSpot and Sigma, through their APIs (`unshackle migrate`), or from downloaded files
  (`unshackle import`) for all but Metabase, Redash and Sigma.
- **Outputs:** Apache Ossie semantic models and metrics; dashboards as Jupyter notebooks (SQL per tile, Altair
  charts); SQL queries and models; Cedar policies and SQL row filters; table descriptions and Markdown knowledge.
- **Nothing guessed:** what can't be translated faithfully stays a `TODO(unshackle)` with the original expression.
  `migration/report.md` shows per dashboard how many tiles have SQL and what blocks the rest; `unshackle todo` lists
  every item.
- **Commands:** `check` (credentials, with `--prompt` to save them to `.env`), `migrate` (`init`, `inventory`,
  `extract` and `build` in one go), `import`, `run` (execute the notebooks), `verify` (migrated numbers against the
  source's; `verify --init tableau` writes the cases from Tableau's view data), `todo`, `status`, `report`.
- **Data that lives only in Tableau** (extracts, Excel, CSV) can be copied to Parquet with `--with-data` and loaded
  into a local DuckDB file the notebooks read. `.hyper` extracts need `pip install "unshackle[hyper]"`.
- **Install:** `pip install unshackle` (or `uv tool install unshackle`).

Changes before the first release are in the git history.
