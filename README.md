<h1 align="center">
  <img src="assets/banner.svg" alt="unshackle" width="760">
</h1>

unshackle moves your metrics, dashboards and permissions out of a BI tool and into files in a Git repository you own.

## What you get

| You had in your BI tool | You get in your repo |
| --- | --- |
| Metrics, measures, calculated fields | Metrics in [Apache Ossie](https://github.com/apache/ossie), an open semantic-layer format (`semantic/`) |
| Dashboards | Jupyter notebooks: one SQL query and one chart per tile (`notebooks/`) |
| Tables and data sources | Table and column descriptions (`catalog/`) |
| Permissions, row-level security | [Cedar](https://www.cedarpolicy.com) policies and SQL row filters (`policies/`) |
| Descriptions, glossaries | Markdown (`knowledge/`) |

What unshackle can't translate faithfully is marked `TODO(unshackle)`, with the
original formula next to it. It doesn't guess.

## How it works

1. **Extract:** reads definitions through your tool's API, or from files you download.
2. **Translate:** writes the files above. Nothing changes in your BI tool.
3. **Report:** `migration/report.md` shows, per dashboard, what was translated and what blocks the rest.
4. **Check:** compares the migrated queries with your tool's own numbers.

## Get started

You need [uv](https://docs.astral.sh/uv/) and access to your BI tool.

```bash
uv tool install "unshackle[run]"      # some tools need more: see Install in Supported tools
cd your-analytics-repo
unshackle check <tool> --prompt       # asks for credentials, saves them to .env (git-ignored)
unshackle migrate <tool>
```

Then:

```bash
unshackle run       # run the notebooks; see which tiles return data
unshackle verify    # compare migrated numbers with your tool's
unshackle todo      # list what's left to finish
```

`unshackle tools <tool>` says where to find each credential.

## Supported tools

| Tool | Maturity | Install (`uv tool install "…"`) | Import files instead of the API |
| --- | --- | --- | --- |
| [Tableau](docs/tools/tableau.md) | Most complete | `unshackle[run,tableau]` | `.twb`, `.twbx`, `.tds`, `.tdsx` |
| [Power BI](docs/tools/powerbi.md) | Real projects | `unshackle[run]` | Power BI project (`.pbip`, model as TMDL) |
| [Looker](docs/tools/looker.md) | Real projects | `unshackle[run,looker]` | LookML project (Git checkout or `.zip`) |
| [Qlik Sense](docs/tools/qlik.md) | Saved exports | `unshackle[run,qlik]` | `qlik app unbuild` folder per app |
| [Amazon QuickSight](docs/tools/quicksight.md) | Saved exports | `unshackle[run,quicksight]` | `.qs` asset bundle |
| [Metabase](docs/tools/metabase.md) | Saved exports | `unshackle[run]` | API only |
| [Lightdash](docs/tools/lightdash.md) | Saved exports | `unshackle[run]` | dbt project folder |
| [Apache Superset](docs/tools/superset.md) | Saved exports | `unshackle[run]` | export `.zip` |
| [Redash](docs/tools/redash.md) | Saved exports | `unshackle[run]` | API only |
| [Omni](docs/tools/omni.md) | Early | `unshackle[run]` | model YAML folder (Git sync) |
| [Hex](docs/tools/hex.md) | Early | `unshackle[run]` | project YAML (Git export) |
| [ThoughtSpot](docs/tools/thoughtspot.md) | Early | `unshackle[run]` | `.tml` files or Export TML `.zip` |
| [Sigma](docs/tools/sigma.md) | Early | `unshackle[run]` | API only |

**Maturity:** *Most complete*: the most translated and tested. *Real projects*: also tested on
open-source projects people built. *Saved exports*: tested on exports over a fictional dataset.
*Early*: the basics; expect more TODOs.

Each tool's page covers connecting, what you get, what's left to finish and how to check the numbers.

## API or files

**API** gets the most: definitions, dashboards people built in the tool, usage,
sharing, and for most tools the SQL the tool itself runs.

**Files** work when you can't get API credentials. They carry less (usually no usage
or sharing, and for Looker and Omni no user-built dashboards); the report lists
what's missing.

```bash
unshackle import <tool> <files or folder>
unshackle build <tool>
```

## Data that lives only in your BI tool

Tableau extracts, Excel and CSV files have no warehouse behind them. `migrate` asks
whether to copy that data to `data/` (git-ignored) so the notebooks can run on it
locally.

## Check the numbers

`unshackle verify` runs the cases in `migration/parity/` against your warehouse and
compares them with the source tool's numbers. For Tableau, `unshackle verify --init tableau`
writes the cases from Tableau's own view data. For other tools, write them by hand:
see the [verify guide](skills/verify/SKILL.md).

## How it's tested

- **Against numbers, not snapshots.** Most tests build a migration from a saved export, run the
  generated SQL in DuckDB on a fictional dataset, and compare the result with the same numbers
  computed independently, following the source tool's own rules.
- About 1,270 tests run offline: `uv pip install -e ".[dev]" && pytest`.
- `unshackle verify` does the same on your data: migrated SQL against your tool's own numbers.

unshackle is built with [Claude Code](https://claude.com/claude-code); every change goes through
those tests.

## With a coding agent

unshackle runs fine on its own. If you use a coding agent (Claude Code, Codex,
Cursor), point it at the repo: `AGENTS.md` explains the layout, and the guides in
`migration/skills/` walk it through finishing each kind of TODO.

## Try it without a BI tool

```bash
git clone https://github.com/Querio-App/unshackle && cd unshackle
uv venv && uv pip install -e ".[dev]"
./examples/tableau-demo/run.sh /tmp/acme
```

The demo migrates a saved Tableau export into `/tmp/acme` and checks a dashboard's
numbers against Tableau's.

## More

- [Repo layout](spec/layout.md): every folder and file unshackle writes
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)
- [The name](docs/name.md): why a BI migration tool is named after Shackleton

Apache 2.0. Built and maintained by [Querio](https://querio.ai).
