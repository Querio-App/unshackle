# Changelog

## 0.3.0 (2026-11-19)

- Serve migrated repository context through MCP: `overview`, `search`, confined `read`,
  file resources and bounded read-only `run_query`, with local stdio client configurations.
- Import warehouse metadata, plain dbt projects, SQL files and static Python SQL literals.
  Imports write usable context immediately; unsupported expressions retain source and TODOs.
- Provide bearer-authenticated Streamable HTTP hosting, Git refresh and deployment guides
  for Railway, AWS and Cloud Run. One warehouse role serves all token holders; policies
  remain context, and git-ignored local data is unavailable to Git-pulled hosts.
- Add an inactive scheduled drift workflow example that updates one PR with schema changes
  and missing-column references, and closes obsolete PRs when drift disappears. Renames
  are reported as removed + added columns; uncertain SQL references stay explicit.
- Add a bundled MCP App for query results, with local sorting, filtering and CSV export
  over returned rows. JSON/text clients keep the same results. Large integers and decimals
  retain exact digits alongside NULL; query caps, cancellation and authentication still apply.
- Add synthetic MCP examples with five hand-derived answers, offline protocol checks and
  a persistent team-demo guide.
- Install MCP support with `pip install "unshackle[mcp]"` on Python 3.11+.
  The wheel includes the prebuilt result table and license notices; Node is not needed
  to run the server.

## 0.2.0 (2026-11-11)

- `verify --init metabase` captures dashboard card CSV baselines and writes strict parity cases;
  `--only dashboard:<id>` limits the capture. Cases pin source filter defaults to validated notebook
  binds, preserve existing cases and baselines, and report unsupported or ambiguous cases as skips.

- `verify` skips empty and comment-only parity YAML files. Malformed YAML and nonmapping
  cases are reported as `not checked`, and remaining valid cases still run and write evals.
- Export Metabase dashboard subscriptions to `schedules/` YAML, preserving each channel's cadence,
  enabled state, visible recipients and delivery details, card attachment flags, and parameter IDs.
  Unresolved filters, timezone and visibility limits stay explicit TODOs. This exports delivery intent;
  it does not install a scheduler or send messages.

- Fix duplicate MetricFlow dimension labels within a semantic model, including joined Metabase model
  fields. Conflicting labels gain the dimension name and, when needed, a numeric suffix so dbt parse
  accepts them without changing dimension names, SQL or source metadata.
- Metabase dashboard filters with duplicate or normalized-colliding slugs keep distinct parameter IDs,
  defaults and SQL binds, including date/range expansions and existing numeric suffixes. Older exports
  with ambiguous text sentinels retain a TODO to re-extract instead of guessing the bindings.

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
