<h1 align="center">
  <img src="assets/banner.svg" alt="unshackle" width="760">
</h1>

unshackle is a command-line tool for moving definitions out of a BI tool and into files in a Git repository.

It extracts the original definitions, translates what it can into open formats
(metrics, table descriptions, queries, dashboards as notebooks, permissions), and
marks everything else with a `TODO(unshackle)`. You can run it without a coding agent.

## Get started

1. **Install** with `run` plus your tool's extra, if it has one (see the [table](#supported-tools)):

   ```bash
   uv tool install "unshackle[run,tableau]"
   ```

2. **Set credentials.** `unshackle tools` lists the environment variables for each
   tool; your tool's [section](#supported-tools) says what you need.

3. **Migrate** from inside the Git repo that should hold the result:

   ```bash
   cd your-analytics-repo
   unshackle migrate tableau
   ```

   No API access? [Import downloaded files](#api-or-file-import) instead.

4. **Look at the result.** `migration/report.md` has counts, warnings and what's left;
   `unshackle todo` lists every unfinished item by file and line.

5. **Check the numbers.** Write comparison cases in `migration/parity/*.yaml` and run
   `unshackle verify` against your warehouse. See [Check the results](#check-the-results).

## Supported tools

| Tool | Install | API | File import |
| --- | --- | --- | --- |
| [Tableau](#tableau) | `unshackle[run,tableau]` | yes | yes |
| [Power BI](#power-bi) | `unshackle[run]` | yes | yes |
| [Looker](#looker) | `unshackle[run,looker]` | yes | yes |
| [Qlik Sense](#qlik-sense) | `unshackle[run,qlik]` | yes | yes |
| [Amazon QuickSight](#amazon-quicksight) | `unshackle[run,quicksight]` | yes | yes |
| [Metabase](#metabase) | `unshackle[run]` | yes | no |
| [Lightdash](#lightdash) | `unshackle[run]` | yes | yes |
| [Apache Superset](#apache-superset) | `unshackle[run]` | yes | yes |
| [Redash](#redash) | `unshackle[run]` | yes | no |
| [Omni](#omni) | `unshackle[run]` | yes | yes |
| [Hex](#hex) | `unshackle[run]` | yes | yes |
| [ThoughtSpot](#thoughtspot) | `unshackle[run]` | yes | yes |
| [Sigma](#sigma) | `unshackle[run]` | yes | no |

Install with `uv tool install "<install>"`. To add an extra later, run it again with
`--force` and the full list of extras.

## API or file import

**API** (`unshackle migrate <tool>`, or `inventory` + `extract` + `build`) reads
everything the credentials can see: definitions, dashboards people built in the tool,
usage, sharing, and for most tools the SQL the tool itself generates, which
becomes the tile SQL.

**File import** works when you can download definitions but can't get API credentials:

```bash
unshackle init
unshackle import tableau "Superstore Sales.twbx" datasources/
unshackle build tableau
```

`import` replaces `migration/raw/<tool>/` with the files, unpacks `.zip` downloads, and
never runs anything inside them. Files carry less than the API; each tool's section
says what's missing, and `migration/report.md` lists it after the build.

## Per tool

Each section links the source guide under [`skills/`](skills/), which covers access
in detail, what is translated, and how to finish the TODOs. That guide is the next step
after the first build.

### Tableau

- **Install:** `unshackle[run,tableau]`
- **Credentials:** `TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` (optional `TABLEAU_API_VERSION`)
- **API gives:** workbooks and published data sources (calculations, LODs, table calcs, parameters, user filters), dashboards, usage, permissions, and each view's data as CSV for parity checks. `--with-data` copies extracts, Excel and CSV data to Parquet in `data/tableau/` (git-ignored).
- **Files:** `.twb`, `.twbx`, `.tds`, `.tdsx`. Missing: usage, permissions, project and owner names, lineage, view data. `import --with-data` copies data packaged in `.twbx`/`.tdsx`.
- **Next:** [`skills/source-tableau`](skills/source-tableau/SKILL.md)

### Power BI

- **Install:** `unshackle[run]`
- **Credentials:** `POWERBI_TENANT_ID`, `POWERBI_CLIENT_ID`, `POWERBI_CLIENT_SECRET`, or a ready token in `POWERBI_ACCESS_TOKEN`; optional `POWERBI_WORKSPACES`, `POWERBI_ADMIN=1` (usage)
- **API gives:** semantic models as TMDL (DAX measures, Power Query M, RLS/OLS roles), reports, dashboards, and usage with admin access.
- **Files:** a Power BI project (`.pbip` and its folders) with the semantic model saved as TMDL; not `.pbix`. Missing: usage, workspace names, dashboards, role membership.
- **Next:** [`skills/source-powerbi`](skills/source-powerbi/SKILL.md)

### Looker

- **Install:** `unshackle[run,looker]`
- **Credentials:** `LOOKER_BASE_URL`, `LOOKER_CLIENT_ID`, `LOOKER_CLIENT_SECRET`; optional `LOOKER_LOOKML_DIR`
- **API gives:** LookML (views, explores, measures, LookML dashboards), user-built dashboards and Looks, usage, and the SQL Looker generates for each query.
- **Files:** the LookML project folder (Git checkout or `.zip`). Gives views, explores, measures and LookML dashboards. Missing: user-built dashboards and Looks, usage, Looker's generated SQL, who holds user attributes.
- **Next:** [`skills/source-looker`](skills/source-looker/SKILL.md)

### Qlik Sense

- **Install:** `unshackle[run,qlik]`
- **Credentials:** `QLIK_TENANT_URL`, then `QLIK_CLIENT_ID` + `QLIK_CLIENT_SECRET`, or `QLIK_API_KEY`
- **API gives:** apps' load scripts, master dimensions and measures, variables, sheets, bookmarks, Section Access, the data model and usage.
- **Files:** a `qlik app unbuild` folder per app. Missing: the data model (add the app's data metadata as `data-metadata.json` for `LOAD *` columns and joins), bookmarks, usage.
- **Next:** [`skills/source-qlik`](skills/source-qlik/SKILL.md)

### Amazon QuickSight

- **Install:** `unshackle[run,quicksight]`
- **Credentials:** AWS credentials (`AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`); optional `QUICKSIGHT_REGION`, `QUICKSIGHT_ACCOUNT_ID` (default to the profile's), `QUICKSIGHT_NAMESPACE`, `QUICKSIGHT_IDENTITY_REGION`
- **API gives:** datasets with joins and calculated fields, analyses, dashboards, parameters and filters, row- and column-level security, users and groups.
- **Files:** a `.qs` asset bundle (QuickSight JSON format). Missing: users and groups; permissions unless exported with them; datasets the dashboards don't depend on.
- **Next:** [`skills/source-quicksight`](skills/source-quicksight/SKILL.md)

### Metabase

- **Install:** `unshackle[run]`
- **Credentials:** `METABASE_URL`, `METABASE_API_KEY`
- **API gives:** tables and fields, questions (native and query builder, with Metabase's compiled SQL), models, metrics, dashboards with filters, collections and permissions.
- **Files:** not supported.
- **Next:** [`skills/source-metabase`](skills/source-metabase/SKILL.md)

### Lightdash

- **Install:** `unshackle[run]`
- **Credentials:** `LIGHTDASH_URL`, `LIGHTDASH_API_KEY`, `LIGHTDASH_PROJECT`
- **API gives:** compiled explores, metrics, charts and dashboards with their SQL, access rules, usage.
- **Files:** the dbt project folder, plus `lightdash download` output if you have it. Missing: compiled explores, chart SQL, usage, space and project access.
- **Next:** [`skills/source-lightdash`](skills/source-lightdash/SKILL.md)

### Apache Superset

- **Install:** `unshackle[run]`
- **Credentials:** `SUPERSET_URL`, then `SUPERSET_API_KEY` or `SUPERSET_USERNAME` + `SUPERSET_PASSWORD`
- **API gives:** datasets and their metrics, charts with the SQL Superset compiles, dashboards with native filters, roles, row-level security.
- **Files:** the export `.zip` from the Dashboards, Charts or Datasets list. Missing: chart SQL, filter bindings, roles and row-level security, usage.
- **Next:** [`skills/source-superset`](skills/source-superset/SKILL.md)

### Redash

- **Install:** `unshackle[run]`
- **Credentials:** `REDASH_URL`, `REDASH_API_KEY`
- **API gives:** saved queries with parameters, visualizations, dashboards with parameter mappings, groups and data source access, query snippets.
- **Files:** not supported.
- **Next:** [`skills/source-redash`](skills/source-redash/SKILL.md)

### Omni

- **Install:** `unshackle[run]`
- **Credentials:** `OMNI_URL`, `OMNI_API_KEY`
- **API gives:** shared models (views, topics, relationships, access filters and grants, AI context), workbooks and dashboards.
- **Files:** a model's YAML folder (from Git sync), one folder per model. Missing: dashboards and workbooks, sample query SQL, usage, who holds user attributes.
- **Next:** [`skills/source-omni`](skills/source-omni/SKILL.md)

### Hex

- **Install:** `unshackle[run]`
- **Credentials:** `HEX_API_TOKEN` (optional `HEX_BASE_URL`); `HEX_EXPORT_DIR` and `HEX_SEMANTIC_DIR` point at exported files
- **API gives:** projects and apps (SQL, Python, chart and input cells), semantic models, usage, sharing.
- **Files:** project YAML (Git export) and `hex context semantic-project pull` output. Missing: usage, the project list, sharing.
- **Next:** [`skills/source-hex`](skills/source-hex/SKILL.md)

### ThoughtSpot

- **Install:** `unshackle[run]`
- **Credentials:** `THOUGHTSPOT_URL`, `THOUGHTSPOT_USERNAME`, and `THOUGHTSPOT_PASSWORD` or `THOUGHTSPOT_SECRET_KEY`; optional `THOUGHTSPOT_ORG_ID`
- **API gives:** tables, models and worksheets, liveboards and answers with ThoughtSpot's generated SQL, formulas, RLS, sharing, usage.
- **Files:** `.tml` files or the `.zip` from Export TML. Missing: generated SQL, usage, sharing.
- **Next:** [`skills/source-thoughtspot`](skills/source-thoughtspot/SKILL.md)

### Sigma

- **Install:** `unshackle[run]`
- **Credentials:** `SIGMA_API_URL`, `SIGMA_CLIENT_ID`, `SIGMA_CLIENT_SECRET`
- **API gives:** data models, workbooks, metrics, controls, column-level security, team grants.
- **Files:** not supported; Sigma has no download of workbook or data model definitions.
- **Next:** [`skills/source-sigma`](skills/source-sigma/SKILL.md)

## What it writes

A folder is created only when something is written to it.

| Path | Purpose |
| --- | --- |
| `semantic/*.ossie.yaml` | Metrics and semantic models in [Apache Ossie](https://github.com/apache/ossie), one document per model |
| `catalog/` | Table and column descriptions, one YAML file per table |
| `knowledge/` | Business context in Markdown: glossary, rules, notes for AI |
| `queries/` | Saved SQL or Python queries; `queries/models/` holds SQL models |
| `notebooks/*.ipynb` | Dashboards as Jupyter notebooks: SQL per tile, Altair charts |
| `policies/` | Who can see what: Cedar policies and SQL row filters |
| `evals/` | Question and expected answer for each passing parity case, written by `unshackle verify` |
| `data/` | Data copied out of the tool with `--with-data`, git-ignored |
| `migration/` | unshackle's own area: raw exports, `report.md`, the lock file, guides for finishing the work, parity cases |
| `AGENTS.md`, `unshackle.toml` | Notes for coding agents, and folder paths and warehouse type |

In a dbt project (`dbt_project.yml` at the root), semantic files go to
`models/semantic/` with MetricFlow YAML next to the Ossie documents. Paths can be
changed in `unshackle.toml`; the full layout is in [`spec/layout.md`](spec/layout.md).

## Finish the migration

When unshackle can't translate something faithfully, it leaves a `TODO(unshackle)` with
the original expression. `unshackle todo` lists them. The guides copied to
`migration/skills/` explain how to finish each kind, for a person or a coding agent.
Rebuilding keeps hand edits to generated files and reports where it kept them.

`migrate` runs `init`, `inventory`, `extract` and `build` in one go; run them separately
to review the inventory or limit the scope with `extract --only`. `unshackle status`
shows what changed in the source since the last build.

## Check the results

unshackle does not prove on its own that every number matches the old tool. Write
comparison cases in `migration/parity/*.yaml`, then run `unshackle verify` with
`UNSHACKLE_WAREHOUSE_URL` (or `--url`) set to your warehouse. It compares the source's
results with the migrated query and records the outcome in `migration/report.md`. See
[`skills/verify/SKILL.md`](skills/verify/SKILL.md) for the case format.

## Try it without a BI tool

```bash
git clone https://github.com/Querio-App/unshackle
cd unshackle
uv venv && uv pip install -e ".[dev]"
./examples/tableau-demo/run.sh /tmp/acme
```

The demo builds a saved Tableau export into `/tmp/acme` and compares one migrated
dashboard query with Tableau's exported results in a local DuckDB database.

## More

- [Contributing](CONTRIBUTING.md) and [how sources are tested](docs/testing.md)
- [Security issues](SECURITY.md): report them privately
- [The name](docs/name.md)

Apache 2.0. Built and maintained by [Querio](https://querio.ai).
