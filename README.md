<h1 align="center">
  <img src="assets/banner.svg" alt="unshackle" width="760">
</h1>

`unshackle` is a command-line tool for moving definitions out of a BI tool and into
files in a Git repository. It extracts the original definitions, translates what
it can, and lists what still needs work. You can run it without a coding agent.

Supported tools are listed below. The generated files include table descriptions,
metrics, queries, dashboards, and permissions when the source provides them.

## Supported tools

Every tool can be extracted through its API. To import downloaded files instead, see
[No API access? Import files](#no-api-access-import-files).

| Tool | How it's tested |
| --- | --- |
| Tableau | **Live:** a Tableau Cloud site; 7 parity checks against Tableau's own view data |
| Metabase | **Live:** Metabase in Docker; 5 parity checks against Metabase's own results |
| Lightdash | **Live:** Lightdash in Docker; 6 parity checks |
| Apache Superset | **Live:** Superset in Docker; 9 parity checks |
| Redash | **Live:** Redash in Docker; 6 parity checks |
| Amazon QuickSight | **Live:** a QuickSight account; the migrated SQL is checked on Athena (QuickSight has no API for chart data) |
| Qlik Sense | **Live:** a Qlik Cloud tenant; 14 parity checks against Qlik's engine |
| Looker | **Real projects:** 18 open-source LookML projects; no live instance |
| Power BI | **Real projects:** published Power BI projects; no live instance |
| Omni | Saved exports |
| Hex | Saved exports |
| ThoughtSpot | Saved exports |
| Sigma | Saved exports |

- **Live** means the tool runs for real with a fictional dataset, unshackle extracts and
  builds it, and `unshackle verify` checks that the migrated SQL returns the same numbers.
- **Real projects** means parsing is tested on real-world definitions, but not against a
  running instance.
- **Saved exports** means parsing is tested on files shaped like the tool's exports. If you
  use one of these tools, an anonymized export (a "Fixture submission" issue) is the most
  useful thing you can contribute.

## Try it without connecting a BI tool

```bash
git clone https://github.com/Querio-App/unshackle
cd unshackle
uv venv
uv pip install -e ".[dev]"
./examples/tableau-demo/run.sh /tmp/acme
```

The demo reads a saved Tableau export, writes the generated files to
`/tmp/acme`, and compares one migrated dashboard query with Tableau's exported
results using a local DuckDB database. Open `/tmp/acme/migration/report.md` to
see what was translated and what remains.

## Use it with your BI tool

```bash
uv tool install "unshackle[run]"
unshackle tools
cd your-analytics-repo
unshackle migrate tableau
```

`unshackle tools` shows the environment variables each source needs. Set the ones
for your tool before running `migrate`; the access instructions are in the
relevant source guide under [`skills/`](skills/). `migrate` creates the folders,
inventories the source, saves its raw definitions, and builds the translated
files.

You can also run those steps separately with `unshackle init`, `unshackle inventory`,
`unshackle extract`, and `unshackle build`. The CLI runs in your terminal and connects
to the source tool's API when inventorying or extracting.

In an interactive terminal you'll see the *Endurance* working its way through the
pack ice while each step runs. Piped output, CI, `NO_COLOR` and `unshackle --quiet`
get plain text.

## No API access? Import files

If you can't get API credentials but can download definitions from the tool, import
the files instead of extracting:

```bash
unshackle init
unshackle import tableau "Superstore Sales.twbx" datasources/
unshackle build tableau
```

| Tool | What to import |
| --- | --- |
| Tableau | `.twb`, `.twbx`, `.tds`, `.tdsx` (extracts inside packaged files are ignored) |
| Power BI | a Power BI project (`.pbip` and its folders), with the semantic model saved as TMDL. Not `.pbix` |
| Looker | the LookML project folder (Git checkout or `.zip`) |
| Lightdash | the dbt project folder, plus `lightdash download` output if you have it |
| Omni | a model's YAML folder (from Git sync), one folder per model |
| ThoughtSpot | `.tml` files or the `.zip` from Export TML |
| Hex | project YAML (Git export) and `hex context semantic-project pull` output |
| Qlik Sense | a `qlik app unbuild` folder per app (add the app's data metadata as `data-metadata.json` for `LOAD *` columns and joins) |
| Sigma | not supported: Sigma has no download of workbook or data model definitions |

`import` replaces `migration/raw/<tool>/` with the files, unpacking `.zip` downloads,
and never runs anything inside them. Files can't carry usage, sharing permissions
or, for most tools, the SQL the tool generates; Looker and Omni files carry no
dashboards. `migration/report.md` lists what is missing for each import.

## What it writes

| Path | Contents |
| --- | --- |
| `semantic/*.ossie.yaml` | [Apache Ossie](https://github.com/apache/ossie) semantic models and metrics, one document per model |
| `models/semantic/*.yml` | MetricFlow semantic models and metrics, only in dbt projects |
| `catalog/` | Table and column descriptions |
| `knowledge/` | Business context in Markdown |
| `queries/` | SQL or Python queries |
| `notebooks/*.ipynb` | Dashboards as Jupyter notebooks: SQL per tile, Altair (Vega-Lite) charts |
| `policies/` | Cedar policies and SQL row filters |
| `migration/raw/` | Definitions extracted from the source tool |
| `migration/report.md` | Counts, warnings, and unfinished translations |

If the target repo has `dbt_project.yml`, semantic files go to `models/semantic/`
and MetricFlow YAML is written next to the Ossie documents. Paths can be changed in `unshackle.toml`. The full
current layout is in [`spec/layout.md`](spec/layout.md).

When unshackle cannot translate something faithfully, it leaves a `TODO(unshackle)`
with the original expression. Run `unshackle todo` to find those items. The
instructions copied to `migration/skills/` can help a person or a coding agent
finish them, but using an agent is optional. Rebuilding preserves hand edits to
generated files and reports where it kept them.

## Check the results

`unshackle` does not automatically prove that every number matches the old tool.
Write comparison cases in `migration/parity/*.yaml`, then run `unshackle verify`
with a warehouse connection. It compares the source results with the migrated
query and records the outcome. See [`skills/verify/SKILL.md`](skills/verify/SKILL.md)
for the case format and process.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Source parsers are tested from saved
exports in `tests/fixtures/`, so most translation work doesn't need a live BI
account. Please report security issues privately; see [SECURITY.md](SECURITY.md).

Apache 2.0. Made by [Querio](https://querio.ai).
