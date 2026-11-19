# MCP acceptance evidence

Railway hosted acceptance now has three actual clients with all five answers correct,
in addition to the earlier stdio and local HTTP runs. Deployment, Git refresh and
service update passed; remaining cleanup and provider gates are recorded in the
[cloud validation matrix](deploy/validation.md). No scheduled GitHub Actions execution
is claimed, and the epic and draft PRs remain open.

## Dataset and independent expectations

[`examples/mcp-demo`](../examples/mcp-demo/README.md) contains three fictional
customers, six orders, plain dbt models/semantics, SQL files and embedded Python SQL.
The Python file raises if executed; static import succeeds and leaves its dynamic
query as TODO. [`questions.json`](../examples/mcp-demo/questions.json) includes
hand derivations, kept out of the client prompt and MCP context.

| Question | Hand-derived expected answer |
| --- | --- |
| Total recognized revenue | 100 + 50 + 80 + 120 = 350 USD |
| Revenue by customer region | North: 100 + 50 + 120 = 270; South: 80 |
| Highest-revenue customer | Acme Paper: 100 + 50 = 150 USD |
| Monthly revenue | January 230 USD; February 120 USD |
| Average paid order value | 350 / 4 = 87.5 USD |

Refunded 40 USD and cancelled 30 USD are excluded throughout.

## Actual client runs — 2026-11-18

| Client/version | Stdio | Authenticated local HTTP | Railway HTTP |
| --- | --- | --- | --- |
| Claude Code 2.1.308 | [5/5 correct](../examples/mcp-demo/evidence/claude-stdio.json) | [5/5 correct](../examples/mcp-demo/evidence/claude-local-http.json) | Not run |
| Codex CLI 0.169.0 | [5/5 correct](../examples/mcp-demo/evidence/codex-stdio.json) | [5/5 correct](../examples/mcp-demo/evidence/codex-local-http.json) | Not run |
| Cursor CLI 2026.11.17-f532c45 | [5/5 correct](../examples/mcp-demo/evidence/cursor-stdio.json) | [5/5 correct](../examples/mcp-demo/evidence/cursor-local-http.json) | Not run |

Each evidence file records the exact source commit, fixture hash, prompt, client
version, context-tool calls, query calls/results, and actual versus expected answers.
Claude made six queries, including schema discovery; Codex and Cursor made five.
All used overview/search/read before computing answers through run_query against
local Postgres 16. Expected answers were not supplied. Account IDs, private paths,
authentication data and internal reasoning were omitted from published evidence.

The harness used empty temporary workspaces and per-invocation MCP settings. Claude
had no built-in tools. Codex ignored user configuration and used a read-only sandbox.
Cursor used a temporary config directory with direct file/shell/web actions denied;
it attempted to read its global response-style skill, which was denied. That denied
call is recorded separately; all successful data access was through MCP. Authentication
remained with the installed clients. No persistent user MCP configuration was changed.

Initial Codex and Cursor attempts were rejected by noninteractive tool approvals;
Cursor also required workspace trust. Those attempts were not counted as passes.
The successful harness explicitly approves only this fixture's four MCP tools.
Cursor updated itself between attempts; the table records the successful version.

## Actual rename and GitHub PR

[Draft evidence PR #207](https://github.com/Querio-App/unshackle/pull/207) targets
`epic-193-drift-acceptance-base`, not main. Its synthetic baseline derives from tested
integration `bf97712`; no real credentials or raw client transcripts were published.

A separate local Postgres fixture ran `ALTER TABLE orders RENAME COLUMN amount TO
net_amount`. The F drift CLI reran warehouse/dbt/SQL imports and generated:

- Removed `amount`, added `net_amount`, with no guessed rename mapping.
- Missing `amount` references in three notebooks and one dbt reference query.
- Explicit uncertainty for the dynamic Python expression.
- Exactly three staged workflow artifacts: catalog, warehouse snapshot, lock.

Baseline refresh and the second post-rename refresh both returned `changed=false`.
The PR contains the real generated diff and report. Commands were run manually,
equivalent to the workflow's refresh/add-paths/PR steps. **No scheduled GitHub Actions
run was executed.** The template remains outside active repository workflows.

## Automated checks and remaining gates

`tests/test_mcp_acceptance.py` exercises five offline checks: hand-derived DuckDB
answers; combined imports/annotations/Layout/idempotence; stdio questions/tools/
resources; rename/reference drift; and authenticated HTTP questions. The opt-in
Postgres check also ran and passed: actual metadata import, all five stdio answers,
row cap, and direct DELETE denied by the read-only warehouse role.

The installed DuckDB/SQLAlchemy reflection driver fails while querying `pg_collation`
on this environment. Warehouse import aborts without partial metadata. DuckDB query
and arithmetic checks passed; DuckDB introspection is not claimed. SQLite supplies
offline adapter integration; Postgres supplies actual database/client acceptance.

The initial 2026-11-18 acceptance run had no cloud access. The separately authorized
2026-11-19 Railway rehearsal subsequently passed all three clients over hosted HTTP,
plus authentication, resource/tool access, five reference queries, caps, native
cancellation, direct database write denial, service update and Git refresh. See the
[committed sanitized evidence](../deploy/railway/evidence/README.md) for exact source,
image, context and client versions and cleanup limitations. Actual scheduled workflow
execution remains unverified; PR #207 proves manual workflow-equivalent generation.

## Cloud deployment follow-up

CLI-first preparation covers [Railway](deploy/railway.md), [GCP Cloud Run](deploy/cloud-run.md)
and [AWS ECS Fargate](deploy/aws.md). The [provider validation matrix](deploy/validation.md)
separates local preparation evidence from authentication, deployment, actual clients,
updates and teardown. No cloud acceptance is inferred from the six local runs above.
See [hosted acceptance commands](deploy/README.md#record-hosted-acceptance) for the
repeatable client harness and evidence requirements after authorized deployment.
