# Qlik Sense

unshackle reads your Qlik Cloud apps (load script, master items, sheets, Section Access) and writes them as metrics, notebooks and policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `QLIK_TENANT_URL` | The address you open Qlik Cloud at, no path: `https://<tenant>.<region>.qlikcloud.com` |
| `QLIK_CLIENT_ID` / `QLIK_CLIENT_SECRET` | An OAuth machine-to-machine client: Administration > OAuth > Create new. The secret is shown once |
| `QLIK_API_KEY` | Instead of an OAuth client: your avatar > Profile settings > API keys > Generate new key |

- The OAuth client needs the **Trusted** consent method, or the token request fails with `OAUTH-5`. It acts as its own bot user: give that user the roles and space memberships it needs.
- API keys need a role with **Manage API keys** and API keys enabled for the tenant.
- The account must be able to open each app (at least view rights in its space). For apps with Section Access it needs a row in the access table, with `ACCESS = ADMIN` to read the script.

`unshackle check qlik --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check qlik` tests the connection.

## Run

```bash
unshackle migrate qlik
```

Lists your apps, reads each one through the engine API and writes the repo. It only reads; nothing changes in Qlik. Use `--only app:<id>` to take one app.

## What you get

| In Qlik | In your repo |
| --- | --- |
| Plain loads from files or `SQL SELECT` | Semantic models on warehouse tables (`semantic/`) |
| Shared field names (associations) | Joins between models |
| Master measures (`Sum`, `Count`, `Avg`, `Min`, `Max`, set modifiers with listed values, ratios) | Ossie metrics (`semantic/`) |
| Master dimensions | Dimension labels, or time dimensions for calculated ones |
| Sheets | Notebooks, one tile per chart (`notebooks/`) |
| Filter panes | Notebook parameters |
| Section Access loaded inline | Row filters and Cedar policies (`policies/`) |
| Load script, bookmarks | Markdown (`knowledge/`) |

## What needs finishing

- **The load script.** Resident loads, joins, `GROUP BY`, mapping, `INLINE`, loops: rebuild each table as a SQL or dbt model with the same name and fields.
- **QVD files.** Migrate the app that writes them first; Excel, CSV and Parquet files need landing in the warehouse.
- **Advanced expressions**: `Aggr()`, `TOTAL`, `Above` / `Below` / `RangeSum`, search strings, alternate states, calculated variables.
- **Measures across two tables**, which Qlik evaluates over the association.
- **Section Access users**: `USERID` values are Qlik user subjects; map them to your users. Access tables loaded from files or databases are left to you.
- **Charts**: drill-down dimensions (first field only), "top N + Others" limits, KPI comparisons, maps, extensions.
- **Bookmarks**: listed, not applied; rebuild useful ones as parameter presets.

`unshackle todo` lists them; the guide explains how to finish each.

## Without API access

Run `qlik app unbuild --app <id> --dir <folder>` for each app, then:

```bash
unshackle import qlik <folder>
unshackle build qlik
```

What the files can't carry:

- The data model: `LOAD *` columns, field types and joins are unknown. Add the app's `GET /api/v1/apps/{id}/data/metadata` response as `data-metadata.json` next to `script.qvs` before importing.
- Bookmarks and usage (item views).

## Check the numbers

Qlik's engine is the source side: open the app, read each chart's own data (selecting values first for a filtered case) and save it as a case in `migration/parity/`. A row filter can be checked with a set expression on the reduction field. Then run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-qlik guide](../../skills/source-qlik/SKILL.md) has the full detail, written for coding agents and anyone finishing the migration.
