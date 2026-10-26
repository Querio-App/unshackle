# Tableau

unshackle reads your Tableau workbooks and published data sources and turns them into metrics, notebooks, policies and docs in your own Git repo.

## Install

```bash
uv tool install "unshackle[run,tableau]"   # tableau: copies data that lives only in Tableau
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `TABLEAU_SERVER_URL` | The start of the browser address, no path: `https://tableau.example.com` on Server. On Cloud, the pod address (like `https://prod-useast-b.online.tableau.com`), not Tableau Cloud Manager |
| `TABLEAU_SITE` | The part after `#/site/` in the browser address. Can differ from the site's display name. Empty for the Server default site |
| `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` | Your avatar > My Account Settings > Personal Access Tokens > Create new token. The secret is shown once |
| `TABLEAU_API_VERSION` | Optional. Default `3.24`; older Server: `3.19` (2023.1), `3.15` (2022.1) |

- Use a **Site Administrator Creator** or Server Admin. Other roles only see what they can view, and need Download Workbook/Save a Copy on every workbook. What fails is listed in the report's warnings.
- On Tableau Server, turn on the Metadata API (`tsm maintenance metadata-services enable`) for lineage. Without it, migration still works.
- One token holds one session: don't run two migrations with the same token at once. Tokens expire after 15 days unused.

`unshackle check tableau --prompt` asks for these and saves them to `.env` (git-ignored). `unshackle check tableau` tests the connection.

## Run

```bash
unshackle migrate tableau
```

Takes inventory, downloads workbooks and data sources, and writes the repo.

- **Data only in Tableau:** when data sources hold extracts, Excel or CSV with no warehouse table, `migrate` asks whether to copy that data to `data/` (git-ignored). `--with-data` copies it without asking.
- **Scope:** `--only workbook:<luid>`, `--only view:<luid>` or `--only datasource:<luid>`. Published data sources the chosen workbooks use come along.

## What you get

| In Tableau | In your repo |
| --- | --- |
| Tables, columns, relationships, joins | `catalog/` tables and semantic models with entities |
| Measures, calculated fields, ratios, FIXED LODs | Ossie metrics and dimensions in `semantic/` |
| Custom SQL | `queries/` SQL file to materialize |
| Worksheets, dashboards, stories | Notebooks in `notebooks/`: one tile per sheet, filters and parameters as notebook parameters |
| Group permissions on workbooks | Cedar policies in `policies/` |
| User filters (`USERNAME()`, `ISMEMBEROF()`) | SQL row filters |
| Formulas, descriptions, parameters, hierarchies, folders | `knowledge/<workbook>-glossary.md` |

## What needs finishing

- **Table calculations** (running totals, rank, percent of total): tiles compute them; the metric stays TODO.
- **INCLUDE / EXCLUDE LODs** and FIXED LODs on several dimensions.
- **Parameters** inside metrics: the semantic layer has none. Pin a value or make one metric per value.
- **Custom SQL:** materialize it as a SQL model.
- **Some blends** (a filter on the secondary source, its LODs), **cross-database joins**, and some sets, groups and bins.
- **Permissions:** individual user grants need an email or group; project defaults aren't exported.
- **User filters:** check the user attribute and group names match your identity provider.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Download workbooks and data sources (the `...` menu > Download, or Desktop's File > Export Packaged Workbook), then:

```bash
unshackle import tableau "Sales.twbx" "Targets.tdsx" more-workbooks/
unshackle build tableau
```

Accepts `.twb`, `.twbx`, `.tds`, `.tdsx` and folders of them. Name each file after its workbook or data source. Add `--with-data` to copy data packed in `.twbx`/`.tdsx`. Files can't carry:

- View counts, so nothing is sorted by popularity
- Who may view each workbook, so no Cedar permits (user filters are kept)
- Project, owner, description or links to the site
- Metadata API lineage
- View data for number checks: download a view's data as CSV and point a parity case at it

## Check the numbers

```bash
unshackle verify --init tableau   # writes cases from Tableau's saved view data
unshackle verify                  # compares them with your warehouse
```

`migrate` saves the data of the 20 most viewed views. Use a token whose user sees all rows: user filters shape that data. See the [verify guide](../../skills/verify/SKILL.md).

## More

[source-tableau guide](../../skills/source-tableau/SKILL.md): written for coding agents and anyone finishing the migration.
