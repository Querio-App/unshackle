# MCP acceptance evidence

Epic #193 requires five correct answers from Claude Code, Cursor and Codex over
stdio and Railway HTTP, plus a real drift PR after a column rename. Offline tests
and local HTTP protocol tests do not satisfy those client/deployment gates.

The synthetic dataset and independently written expected results live in
[`examples/mcp-demo`](../examples/mcp-demo/README.md). The input Python raises if
executed; importing it must still succeed. No real customer data is used.

## Current evidence

- Offline arithmetic: five hand-derived answers verified against DuckDB.
- Offline integration: SQLite warehouse + dbt + SQL imports, repeat-byte stability,
  preserved annotations, Layout overrides, TODOs and stdio SDK tools/resources.
- Actual local Postgres: warehouse import, five questions through MCP stdio, row cap,
  and a direct DELETE denied by the read-only database role passed.
- Actual client matrix: in progress; only recorded transcripts count.
- Railway HTTP: not run; deployment credentials and authorization unavailable.
- Rename-triggered GitHub drift PR: not yet run; F implementation pending.

The installed DuckDB SQLAlchemy reflection driver fails when querying `pg_collation`
on this environment. Warehouse import aborts safely without publishing partial
metadata. DuckDB query/arithmetic checks passed; DuckDB introspection is not claimed.
Postgres is the live fixture warehouse; SQLite supplies offline adapter integration.

No AWS, Cloud Run or Railway deployment was performed by this acceptance worker.
