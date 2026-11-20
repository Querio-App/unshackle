# Use unshackle

Start with the task you need to finish. Commands run from your context repository
unless a guide says otherwise; `--repo` selects another destination.

1. [Migrate an existing BI tool](migrate.md): connect, choose scope, inspect what
   transferred, finish TODOs and compare the numbers with the source.
2. [Start without a BI export](#start-without-a-bi-export): choose warehouse, dbt
   or existing query inputs.
3. [Ask your first local question](mcp.md): connect Claude Code to your own context
   and warehouse using stdio.
4. [Share with a team](deploy/README.md): run HTTP, authenticate clients and manage updates.
5. [Maintain your context](maintain.md): refresh, inspect drift, verify, troubleshoot,
   roll back or stop the server.

## Start without a BI export

These imports write context immediately. They do not establish numerical parity.
Combine them when they describe the same warehouse, with inputs kept separately
from generated output. Run `unshackle init --repo <destination>` first to create
agent guidance, copied skills and ignore rules; non-BI imports alone do not scaffold
these files. Initialization preserves existing files.

| Input | What it gives you | What it does not do |
| --- | --- | --- |
| [Warehouse metadata](import-warehouse.md) | Visible base tables, columns, keys and comments in `catalog/` | Copy rows, views, business metrics, dashboards or policies |
| [Plain dbt project](import-dbt.md) | Documentation, reference SQL and supported semantic definitions | Run dbt, render raw Jinja or prove compiled artifacts are current |
| [SQL and Python SQL literals](import-sql.md) | Recovered static queries in notebooks; TODOs for uncertain expressions | Execute SQL/Python or recover arbitrary runtime query construction |

Use a separate context destination for dbt and the same complete SQL input roots on
refresh. Inspect generated SQL and TODOs before connecting an agent. For BI exports,
use the [source-specific tool guides](../README.md#supported-tools); Lightdash's dbt
metadata is a different route from plain dbt import.

## Reference and examples

- [Output layout](../spec/layout.md) and [agent migration skill](../skills/migrate/SKILL.md)
- [Parity cases and debugging differences](../skills/verify/SKILL.md)
- [Interactive query table and CSV](mcp-result-table.md)
- [Scheduled drift workflow](drift.md)
- [Local synthetic MCP example](../examples/mcp-demo/README.md)
- Optional hosted synthetic demos: [Railway](deploy/railway.md),
  [AWS](deploy/aws.md), [Cloud Run](deploy/cloud-run.md),
  [persistent team demo](deploy/team-demo.md) and [Claude onboarding](deploy/claude-team-demo.md)
- [Contributing](../CONTRIBUTING.md), [security](../SECURITY.md), [the name](name.md)
