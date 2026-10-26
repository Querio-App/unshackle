# Hex

unshackle takes your Hex projects, apps and semantic models out of Hex and turns them into notebooks, SQL queries and Ossie metrics in your repo.

## Install

```bash
uv tool install "unshackle[run]"
```

Hex needs no extra.

## Connect

| Variable | Where to find it |
| --- | --- |
| `HEX_API_TOKEN` | A workspace token (Settings > API keys) or a personal token (User settings > API keys) |
| `HEX_BASE_URL` | Optional. Default `https://app.hex.tech/api/v1`. Single-tenant or EU: the address you open Hex at, plus `/api/v1` |
| `HEX_EXPORT_DIR` | Optional. Folder of project YAML from Hex's Git export (charts, inputs, app layout) |
| `HEX_SEMANTIC_DIR` | Optional. Output of `hex context semantic-project pull` (semantic models) |

- The API needs a **Team or Enterprise** plan.
- A workspace token with read scope sees every project. A personal token only sees what that user can see.
- The API has no chart settings or app layout. Without `HEX_EXPORT_DIR`, every SQL cell becomes a table tile.
- The API returns the **draft** version of cells, which may differ from the published app.

`unshackle check hex --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check hex` tests the connection.

## Run

```bash
unshackle migrate hex
```

Lists your projects, pulls their cells (plus export and semantic YAML if set) and writes the repo. Add `--only <projectId>` to migrate one project.

## What you get

| In Hex | In your repo |
| --- | --- |
| Project / app | Notebook in `notebooks/`, tiles in app layout order |
| SQL cell | Verified query in `queries/` |
| Chained SQL on a dataframe | Upstream SQL cells inlined as CTEs |
| `{{ param }}` in SQL | A named bind, set in the notebook's parameters cell |
| Input cell (dropdown, date, number, ...) | Dashboard filter with its default |
| Chart, single value, table cell | Bar, line, area, point or pie tile; KPI; table |
| Markdown / text cell | Text tile, plus `knowledge/hex-<project>.md` |
| Python cell | `queries/*.py` with a TODO |
| Semantic model (dimensions, measures, relations) | Catalog table and Ossie semantic model with metrics |

## What needs finishing

- **Jinja beyond a plain variable** (`{% if %}`, lists, `sqlsafe`): rewrite as binds.
- **Dataframe SQL**: Hex runs it in DuckDB. Once inlined it runs on your warehouse, so check date and list functions and casts.
- **Python cells**: rewrite as SQL if they only reshape data, or keep them as Python.
- **Inputs missing from the export**: the filter gets no default; set one.
- **Charts with several y series or combo charts**: layer or reshape by hand.
- **Semantic measures with several aggregates, stddev/variance, or non-key joins**: split into simpler metrics or a SQL model.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Without a token (or a Team/Enterprise plan), import YAML files instead:

```bash
unshackle import hex project-exports/ semantic/
unshackle build hex
```

- Project YAML from Hex's Git export (Project > Git sync, or the `hex` CLI).
- Semantic model YAML from `hex context semantic-project pull`.

Files can't carry:

- App view counts.
- Projects you didn't export, the project list, categories and links back to Hex.
- Project sharing.

## Check the numbers

Hex's SQL cells are the source of truth. Re-run the original SQL on your warehouse, or download a cell's result as CSV from Hex, and compare it with the migrated tile. For semantic models, compare a Hex Explore with the same measures, group-by and filters. Pin the date window on both sides.

Write each comparison as a case in `migration/parity/` and run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-hex guide](../../skills/source-hex/SKILL.md) has the full details, written for coding agents and anyone finishing the migration.
