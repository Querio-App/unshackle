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
uv tool install "unshackle[run]"      # add your tool's extra: "unshackle[run,tableau]"
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

| Tool | Extra | Import files instead of the API | Guide |
| --- | --- | --- | --- |
| Tableau | `tableau` | `.twb`, `.twbx`, `.tds`, `.tdsx` | [source-tableau](skills/source-tableau/SKILL.md) |
| Power BI | | Power BI project (`.pbip`, model as TMDL) | [source-powerbi](skills/source-powerbi/SKILL.md) |
| Looker | `looker` | LookML project (Git checkout or `.zip`) | [source-looker](skills/source-looker/SKILL.md) |
| Qlik Sense | `qlik` | `qlik app unbuild` folder per app | [source-qlik](skills/source-qlik/SKILL.md) |
| Amazon QuickSight | `quicksight` | `.qs` asset bundle | [source-quicksight](skills/source-quicksight/SKILL.md) |
| Metabase | | API only | [source-metabase](skills/source-metabase/SKILL.md) |
| Lightdash | | dbt project folder | [source-lightdash](skills/source-lightdash/SKILL.md) |
| Apache Superset | | export `.zip` | [source-superset](skills/source-superset/SKILL.md) |
| Redash | | API only | [source-redash](skills/source-redash/SKILL.md) |
| Omni | | model YAML folder (Git sync) | [source-omni](skills/source-omni/SKILL.md) |
| Hex | | project YAML (Git export) | [source-hex](skills/source-hex/SKILL.md) |
| ThoughtSpot | | `.tml` files or Export TML `.zip` | [source-thoughtspot](skills/source-thoughtspot/SKILL.md) |
| Sigma | | API only | [source-sigma](skills/source-sigma/SKILL.md) |

Each guide covers access, what's translated and how to finish the TODOs.

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
