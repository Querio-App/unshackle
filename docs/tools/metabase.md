# Metabase

unshackle reads your Metabase questions, models, metrics, dashboards and permissions and writes them as SQL, metrics, notebooks and policies in your repo.

## Install

```bash
pip install unshackle      # or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `METABASE_URL` | The address you open Metabase at, no path: `https://metabase.example.com` |
| `METABASE_API_KEY` | Admin > Settings > Authentication > API keys |

- Put the key in the **Administrators** group. In another group it only sees what that group sees, and permissions need admin.
- API keys exist since Metabase v0.49. On older versions, upgrade first.
- unshackle never runs queries on your warehouse. Metabase compiles each query-builder question to SQL.

`unshackle check metabase --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check metabase` tests the connection.

## Run

```bash
unshackle migrate metabase
```

Reads everything through the API and writes the repo. Nothing changes in Metabase. Use `--only dashboard:<id>`, `--only card:<id>` or `--only collection:<id>` to narrow it.

## What you get

| In Metabase | In your repo |
| --- | --- |
| Databases, tables, fields, descriptions | Table and column descriptions (`catalog/`), semantic models (`semantic/`) |
| Simple metrics (one count, sum, average... with filters) | Ossie metrics (`semantic/`) |
| Native SQL and query-builder questions | `queries/`, with the SQL Metabase runs |
| Models | `queries/` plus a semantic model |
| Dashboards | Notebooks, one tile per card (`notebooks/`) |
| Dashboard filters | Notebook parameters |
| Groups, data and collection permissions | Cedar policies (`policies/`) |
| Collections | Where each item lived (`knowledge/`) |

## What needs finishing

- **Custom-expression metrics** (`sum(a) / sum(b)`, `count-where`, cumulative): split into simple measures and a ratio.
- **Models**: materialize their SQL as a model, then mark the key column.
- **Relative date defaults** (`past30days`, `thisyear`): set the dates you want.
- **Time zones and time grouping**: date filters on `timestamptz` columns and the time-grouping filter.
- **Filters unshackle can't place** in the compiled SQL, and native field filters other than `=`.
- **Charts**: funnel, waterfall, map, Sankey, pivot, goals and comparisons, overlaid series.
- **Row and column security** (paid plans): write each predicate from the user attribute mapping.

`unshackle todo` lists them; the guide explains how to finish each.

## Without API access

Metabase is API only: there is no file import.

## Check the numbers

Metabase's own results are the source side: download a question's or dashboard tile's results as CSV through the API (with filter values if needed), save it under `migration/parity/data/` and write a case in `migration/parity/`. Map display-name headers with `columns:`. Then run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-metabase guide](../../skills/source-metabase/SKILL.md) has the full detail, written for coding agents and anyone finishing the migration.
