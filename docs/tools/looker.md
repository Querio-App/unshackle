# Looker

unshackle reads your LookML project, dashboards and Looks and turns them into metrics, notebooks, policies and docs in your own Git repo.

## Install

```bash
pip install unshackle      # or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `LOOKER_BASE_URL` | The address you open Looker at, no path: `https://<instance>.cloud.looker.com`. Self-hosted: the API host and port (often `:19999`), shown in Admin > API |
| `LOOKER_CLIENT_ID`, `LOOKER_CLIENT_SECRET` | An API3 key: Admin > Users > edit user > Edit Keys |
| `LOOKER_LOOKML_DIR` | Optional but recommended: a local clone of the LookML project's Git repo (production branch) |

- Use an **Admin** user's key, or one with `see_lookml`, `see_user_dashboards`, `explore`, `see_system_activity` and access to every folder.
- A weak key gives a silently partial export: Looker hides what the user can't see. Skipped content is listed in the report.
- Without `LOOKER_LOOKML_DIR`, access grants, derived table SQL and `case:` blocks are lost. Clone the repo.

`unshackle check looker --prompt` asks for these and saves them to `.env` (git-ignored). `unshackle check looker` tests the connection.

## Run

```bash
unshackle migrate looker
```

Takes inventory, reads LookML, dashboards and Looks with Looker's generated SQL, and writes the repo.

- **Scope:** `--only dashboard:<id>` or `--only look:<id>`.

## What you get

| In Looker | In your repo |
| --- | --- |
| Views (`sql_table_name`), dimensions, dimension groups | `catalog/` tables and semantic models |
| Measures, `type: number` ratios, measure `filters` | Ossie metrics in `semantic/` |
| Explore joins with `sql_on` | Entities relating the semantic models |
| Derived tables | `queries/` SQL file to materialize |
| User-defined and LookML dashboards | Notebooks in `notebooks/`; dashboard filters become parameters |
| Looks | `queries/*.sql` |
| `access_filter`, `sql_always_where` | SQL row filters |
| `access_grant` / `required_access_grants` | Cedar policies in `policies/` |

## What needs finishing

- **Liquid** templating (`{% if %}`, `{% parameter %}`, `{% condition %}`): one metric per branch, a metric filter or a notebook parameter.
- **Dimensions that read another view:** move them to the view that owns the column.
- **Symmetric aggregates** (`sum_distinct`, measures repeated across one-to-many joins): pre-aggregate in a SQL model.
- **Relative date filters** (`"30 days"`, `"last month"`) inside measures, and on tiles outside BigQuery; some timeframes (quarter, day of week, fiscal).
- **Persistent derived tables:** build them as dbt models or scheduled tables.
- **Access grants:** map each user attribute value to a group in your identity provider.
- **Table calculations** and custom fields on dashboard tiles.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Clone the LookML project's Git repo (production branch), or use your Git host's "download ZIP". Then:

```bash
unshackle import looker path/to/lookml-checkout   # or the .zip
unshackle build looker
```

Only `.lkml` and `.lookml` files are copied. LookML dashboards become notebooks, with tile SQL written from the explore. Files can't carry:

- User-defined dashboards and Looks: they only come from the API
- View counts
- Looker's generated SQL for saved queries
- Which users and groups hold each user attribute (access grants are kept)

## Check the numbers

Get Looker's own numbers from the saved query SQL in `migration/raw/looker/queries/`, or as CSV from the API (`run/csv`) saved under `migration/parity/data/`. Write a case in `migration/parity/` per tile, pin relative dates to fixed ones on both sides, then run `unshackle verify`. Looker compiles that SQL with the API user's access filters: use a user without them. See the [verify guide](../../skills/verify/SKILL.md).

## More

[source-looker guide](../../skills/source-looker/SKILL.md): written for coding agents and anyone finishing the migration.
