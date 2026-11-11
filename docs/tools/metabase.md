# Metabase

unshackle reads your Metabase questions, models, metrics, dashboards and permissions and writes them as SQL, metrics, notebooks and policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `METABASE_URL` | The address you open Metabase at, no path: `https://metabase.example.com` |
| `METABASE_API_KEY` | Admin > Settings > Authentication > API keys |

- Put the key in the **Administrators** group. In another group it only sees what that group sees, and permissions need admin.
- API keys exist since Metabase v0.49. On older versions, upgrade first.
- Extraction asks Metabase to compile query-builder questions to SQL without running them.
  Parity capture runs saved questions through Metabase to get baseline results.

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
| Dashboard subscriptions | Delivery specifications (`schedules/`), one YAML file per subscription |
| Groups, data and collection permissions | Cedar policies (`policies/`) |
| Collections | Where each item lived (`knowledge/`) |

In dbt projects, duplicate dimension labels within a MetricFlow semantic model are disambiguated:
the first keeps its label, and later duplicates gain the dimension name, such as `User ID (name)`.
Existing labels and unlabelled dimensions' names are reserved; a numeric suffix is added if needed.
Dimension names, SQL and source metadata are preserved.

## Dashboard subscriptions

Extraction reads active and archived subscriptions through `GET /api/pulse`, using the endpoint's
default visibility scope. With `--only dashboard:<id>` or a collection selection, requests are limited
to the selected dashboards. A card-only selection does not request all subscriptions.

Each `schedules/metabase-pulse-<id>.yml` references the migrated notebook and preserves independent
channel schedules, enabled states, visible recipients, PDF/email settings, and each card's CSV/XLS
attachment and formatting flags. Filter overrides map by Metabase parameter ID to the notebook's
unique Python parameter names, including reserved names (`engine` becomes `tile_engine`) and date/numeric
ranges. Original parameter IDs and SQL filter names remain alongside them.
`[paths].schedules` and `[paths].notebooks` control output locations; reruns preserve hand edits.

These files describe delivery intent. An external orchestrator must execute notebooks, render attachments,
apply `skip_if_empty`, and deliver results. unshackle does not create jobs or send messages. Unknown
timezone, unsupported filters/channels/settings, missing notebook references, and restricted attachment
permissions remain `TODO(unshackle)` items. No timezone or cron expression is guessed.

Recipient lists contain only what the API returns. Non-admin, sandboxed or tenant-scoped callers may see
fewer subscriptions or recipients; omitted lists remain unknown, not empty. Completeness is explicitly
unknown even after a successful empty response. Previously exported subscriptions absent from a later
visible list are retained with a stale-data TODO. Review them before using an external scheduler.
The export keeps destination addresses but excludes connection credentials and delivery URLs.

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

```bash
unshackle build metabase
unshackle verify --init metabase --only dashboard:2
unshackle verify
```

Omit `--only` to capture all built dashboards, or repeat it for several dashboards.
Initialization uses your Metabase credentials to export each supported card with its dashboard
defaults. It saves CSVs and capture context under `migration/parity/data/`, then writes strict
cases with display headers mapped to SQL columns and target filter values pinned.

Existing cases and baselines are kept. A CSV can be reused without API access when its saved
context still matches. Missing metadata, ambiguous headers, unsupported filter defaults, and
notebooks whose SQL or parameter mapping differs from the build are reported as skips.
For those cases, export the source results and write a case manually using the
[verify guide](../../skills/verify/SKILL.md). `verify` needs your target warehouse connection.

## More

The [source-metabase guide](../../skills/source-metabase/SKILL.md) has the full detail, written for coding agents and anyone finishing the migration.
