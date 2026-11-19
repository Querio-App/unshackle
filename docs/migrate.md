# Migrate and verify a BI tool

You need Python 3.11+, an analytics Git repository, and either authorized source API
access or a supported downloaded export. Install in a virtual environment as shown
in the [README](../README.md#start-with-your-task). Create a branch before writing.
Choose your [tool guide](../README.md#supported-tools) for credentials, export formats,
dependencies and translation limits.

## Connect and choose scope

```sh
unshackle tools <tool>
unshackle check <tool> --prompt
unshackle inventory <tool>
```

`check --prompt` saves credentials in the repository's ignored `.env`; never put them
in tracked configuration. Review `migration/inventory.<tool>.md` and choose the objects
to migrate. The inventory shows only what the credential can see. If expected content
is missing, stop and ask its owner for the required scoped access or a supported export.
Do not automatically elevate to an admin token. Source guides describe API permission
requirements; agree any broader access explicitly before proceeding.

```sh
unshackle migrate <tool> --only <id> --only <another-id>
```

Replace IDs with inventory identifiers. Omit `--only` only for an agreed full migration.
The source skill explains whether dependencies are included automatically. `migrate`
runs initialization, inventory, extraction and build; it does not finish TODOs or verify
parity. For files instead of API access:

```sh
unshackle import <tool> /path/to/export
unshackle build <tool>
```

File imports can lack usage, sharing, generated SQL or user-built dashboards. Read the
report's manual-import gaps. Re-importing replaces that tool's raw export: supply all
intended inputs again. For Tableau extracts/Excel/CSV with no warehouse behind them,
use `--with-data` on import or migrate; `.hyper` needs `unshackle[hyper]`. Copied data
stays under ignored `data/` and does not travel with a Git-hosted MCP deployment.

## See what transferred and finish it

Read `migration/report.md` for counts, blocking warnings, missing source information
and hand edits preserved by the writers. The [layout](../spec/layout.md) explains each
output. Then list unfinished translations:

```sh
unshackle todo
```

For an assisting agent, load `migration/skills/migrate/SKILL.md` in the initialized
customer repository, then `migration/skills/source-<tool>/SKILL.md`. Route metric/query,
notebook and policy TODOs to the existing translation skills listed there. In this
source checkout those same files live under `skills/`; do not invent a second workflow.
Keep `source_expression` and the TODO until a faithful translation is established.
Stop on ambiguous formulas, missing dependencies or access rules you cannot preserve.

## Check the numbers

Set `UNSHACKLE_WAREHOUSE_URL` in the environment or ignored `.env`, with the correct
SQLAlchemy driver and a read-only database role. Run only reviewed notebooks:

```sh
unshackle run
# Choose the initializer for your source, when supported:
unshackle verify --init tableau
# Or: unshackle verify --init metabase --only dashboard:<id>
unshackle verify
unshackle report
```

Tableau initialization uses extracted view CSVs; Metabase captures dashboard card
baselines through its API. Inspect pinned filters, column mappings and date windows.
For other tools or skipped cases, follow the existing [verify skill](../skills/verify/SKILL.md)
to write `migration/parity/*.yaml` using source SQL or exported numbers. An empty case
set or `not checked` result is not a successful verification. Passing cases write to
`evals/`; explain every difference or unresolved case without loosening tolerances.

Review the diff, source visibility gaps, remaining TODOs and parity results before
opening your migration PR. Resolve what blocks the intended questions, then
[connect an agent](mcp.md) or [refresh later](maintain.md). After upgrading unshackle,
`unshackle init --update-skills` refreshes copied skills/specs; it preserves an existing
`AGENTS.md`, so review that page separately.
