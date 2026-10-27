# Omni

unshackle reads Omni's shared models, workbooks and dashboards and writes them as semantic-layer files, notebooks, knowledge and policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `OMNI_URL` | `https://<org>.omniapp.co` or your custom domain, without `/api` |
| `OMNI_API_KEY` | An Organization API key: Admin > API keys |
| `OMNI_MODEL_ID` | Optional: one model to export, the id in the model IDE's browser address. Default: every shared model |

- Use an Org API key, not a personal access token. Dashboard export needs it, and a personal token only sees its owner's documents.
- Omni allows 60 requests a minute and unshackle stays under it. A large org takes a while: use `--only` for a first pass.
- Branch and workbook models are skipped by default. Set `OMNI_MODEL_ID` to pull one, or promote the fields in Omni first.

`unshackle check omni --prompt` asks for the non-optional ones and saves them to `.env` (git-ignored). `unshackle check omni` tests the connection.

## Run

```bash
unshackle migrate omni
```

Inventories, extracts and builds in one go. `--only <modelId>` limits models; `--only <documentId|name>` limits documents.

## What you get

| In Omni | In your repo |
| --- | --- |
| Views | Tables in `catalog/`, semantic models in `semantic/` |
| Dimensions, measures, relationships | Ossie dimensions, metrics and entities in `semantic/` |
| Dashboards | Notebooks in `notebooks/`, dashboard filters as parameters |
| Workbook tabs off the dashboard, topic sample queries | Verified queries in `queries/` |
| Topic access filters | Row filters in `policies/` |
| Access grants | Cedar policies in `policies/` |
| AI context, topic descriptions, field synonyms | Markdown in `knowledge/` |

Tile SQL is the SQL Omni would run for each query.

## What needs finishing

- Excel-style table calculations: rewrite each as a column in the tile SQL, usually a window function.
- Measures that aggregate raw columns: split into simple measures and a ratio.
- `list` and distinct-on measures: a verified query, or a SQL model at the key grain.
- Views defined by SQL: materialize as a warehouse view or dbt model.
- Measure filters on relative dates: rewrite as a time filter, or apply at query time.
- Access grant groups: map them to your real identity-provider groups.
- Custom visuals and combo charts: port the spec, or split into one tile per measure.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Import the model's YAML, one folder (or `.zip`) per model. With Omni's Git sync, clone the repository and import the folder that holds `model.yaml`. Without it, copy the model files out of the model IDE into a folder with the same layout.

```bash
unshackle import omni path/to/model-folder
unshackle build omni
```

The files can't carry:

- Dashboards and workbooks: they only come from the API, so no notebooks are built.
- Sample query SQL: topic sample queries have no compiled SQL.
- Usage: no view counts and no links back to Omni.
- Who holds which user attributes: access grants in the YAML are kept, their holders are not.

## Check the numbers

Omni's query API with `planOnly` returns the SQL Omni would run for each saved query, without running it. Pin relative dates first. For numbers from Omni itself, run the query through the API or download the tile. Write each comparison as a case in `migration/parity/` and run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

[source-omni guide](../../skills/source-omni/SKILL.md): written for coding agents and anyone finishing the migration.
