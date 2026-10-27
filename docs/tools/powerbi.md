# Power BI

unshackle reads your Power BI semantic models and reports and turns them into metrics, notebooks, policies and docs in your own Git repo.

## Install

```bash
pip install unshackle      # or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `POWERBI_TENANT_ID` | Entra admin center > App registrations > your app > Overview: **Directory (tenant) ID** |
| `POWERBI_CLIENT_ID` | Same page: **Application (client) ID** |
| `POWERBI_CLIENT_SECRET` | The app's Certificates & secrets > New client secret. Copy the **Value** (shown once), not the Secret ID |
| `POWERBI_ACCESS_TOKEN` | Instead of the three above: a bearer token, e.g. from `az account get-access-token --resource https://analysis.windows.net/powerbi/api` |
| `POWERBI_WORKSPACES` | Optional. Comma list of workspace ids (after `/groups/` in the address) or names. Default: every workspace the app sees |
| `POWERBI_ADMIN` | Optional. `1` to use admin APIs for usage and a fallback when a model can't be read |

- A Fabric admin turns on **Service principals can use Fabric APIs** (Tenant settings > Developer settings) for a security group holding the app.
- Add the app to each workspace as **Member** or **Contributor**. Viewer is not enough.
- With `POWERBI_ADMIN=1`, also enable the read-only admin APIs and enhanced metadata scanning (detailed metadata, DAX and mashup expressions), or the scan returns no tables or DAX.

`unshackle check powerbi --prompt` asks for these and saves them to `.env` (git-ignored). `unshackle check powerbi` tests the connection.

## Run

```bash
unshackle migrate powerbi
```

Takes inventory, downloads model and report definitions, and writes the repo.

- **Scope:** `--only report:<id>` (brings its semantic model too), `--only model:<id>` or `--only workspace:<id>`.
- **Thin reports** often use a model in another workspace: include that workspace too.

## What you get

| In Power BI | In your repo |
| --- | --- |
| Tables (Power Query sources), columns, relationships | `catalog/` tables and semantic models with entities |
| DAX measures (`SUM`, `DIVIDE`, `CALCULATE` filters, YTD, prior year) | Ossie metrics in `semantic/` |
| Native queries in Power Query | `queries/` SQL file to materialize |
| Report pages | One notebook per page in `notebooks/`; slicers become notebook parameters |
| RLS roles | SQL row filters (`USERPRINCIPALNAME()` becomes the user's email) |
| Role members, object-level security | Cedar policies in `policies/` |
| Copilot instructions and example prompts | `knowledge/` |

## What needs finishing

- **Measures that depend on the visual** (`ALL`, `ALLSELECTED`, `REMOVEFILTERS`, running totals): tile SQL is written; the metric stays TODO.
- **Other DAX:** iterators over other tables or `RELATED`, `USERELATIONSHIP`, `SELECTEDVALUE`, `RANKX`, `TOPN`, rolling windows.
- **Power Query steps** beyond picking a table: rewrite as a SQL model.
- **Role members** are usually set in the service: replace role names with your identity provider's groups.
- **Charts** for maps, treemaps, waterfalls and custom visuals; matrix columns.
- **Composite models:** migrate the remote model first.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

In Power BI Desktop, File > Save as > Power BI project files (`.pbip`). A `.pbix` opens in Desktop and saves this way. Then:

```bash
unshackle import powerbi "Sales Overview.pbip"   # or the project folder
unshackle build powerbi
```

The model must be saved as TMDL. Older projects save `model.bim`: turn on the preview feature "Store semantic model using TMDL format", save again, re-import. `.pbix` and `.pbit` are rejected. Files can't carry:

- Report view counts
- Workspace and dataset names, ids and links: models and reports are named after their folders
- Power BI dashboards (pinned tiles): they only exist in the service
- Who is in each RLS role (the role filters are kept)

## Check the numbers

Get Power BI's own numbers by running DAX against the model (the `executeQueries` API, the XMLA endpoint, or "Export data" on a visual). Save them as CSV under `migration/parity/data/` and write a case in `migration/parity/` per tile. Then run `unshackle verify`. The API needs the Execute Queries tenant setting and Build permission on the dataset. See the [verify guide](../../skills/verify/SKILL.md).

## More

[source-powerbi guide](../../skills/source-powerbi/SKILL.md): written for coding agents and anyone finishing the migration.
