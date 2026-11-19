# Ask a question about your data locally

Use local stdio when your context repository and warehouse are accessible from your
machine. Claude Code launches the server; no hosting account, remote server or proxy
is needed. You need Python 3.11+, an installed/authenticated Claude Code client, and a
[migrated](migrate.md) or [imported context repository](index.md#start-without-a-bi-export).

## Install and connect Claude Code

Run from your context repository. Use an existing virtual environment if you have one:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'unshackle[mcp]'
```

Set `UNSHACKLE_WAREHOUSE_URL` in the process environment or this repository's ignored
`.env`. Use a read-only warehouse role. PostgreSQL uses
`postgresql+psycopg://.../database?connect_timeout=10`; psycopg is included in the MCP
extra. SQLite and DuckDB queries are also supported. Other MCP query drivers are
rejected until they support native cancellation. This is narrower than the set of
SQLAlchemy drivers usable for imports or parity checks.

Register using absolute paths, from the project where you will use Claude:

```sh
claude mcp add --transport stdio unshackle -- \
  /absolute/path/to/context-repo/.venv/bin/unshackle mcp \
  --repo /absolute/path/to/context-repo
claude
```

Replace both paths with yours. Use another server name if `unshackle` is already
registered. In Claude, run `/mcp` and confirm the server connects with `overview`,
`search`, `read` and `run_query`. The stdio process reserves stdout for MCP messages;
a quiet terminal by itself is not a failure. See [Claude's MCP setup](https://code.claude.com/docs/en/mcp).

## Ask your first real question

Pick one actual table or business metric in your context. For example:

> Use unshackle to inspect the revenue definition and its source tables. What was
> recognized revenue last calendar month, grouped by region? Show the period and
> exclusions, flag unresolved TODOs, and base the answer on run_query results.

Expect `overview` to describe available files, `search`/`read` to locate the metric,
grains and joins, then a bounded `run_query` against the configured warehouse. The
answer should state the SQL-backed result and assumptions. If you only imported
metadata, first ask for a row count from a named catalog table; business meaning
may still need to be supplied. `overview` does not connect to the database, so a
successful query is the first SQL-access check. Missing credentials, unsupported
drivers or unresolved definitions should stop the answer instead of producing a
plausible number. Use [parity checks](../skills/verify/SKILL.md) for agreement with BI.

Want a disposable example first? Use the [local synthetic fixture](../examples/mcp-demo/README.md).
Its paid revenue question should return 350 USD after excluding refunds/cancellations.
For failures, follow [troubleshooting](maintain.md#troubleshoot-before-retrying).

## Other clients and shared HTTP

Use absolute executable and repository paths. Keep secrets in the environment or
ignored `.env`, never in tracked client configuration.

Cursor, in `.cursor/mcp.json` (no secrets in this tracked configuration):

```json
{"mcpServers":{"unshackle":{"command":"unshackle","args":["mcp","--repo","/absolute/path/to/repo"]}}}
```

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.unshackle]
command = "unshackle"
args = ["mcp", "--repo", "/absolute/path/to/repo"]
```

For HTTP, use the [hosted client instructions](deploy/README.md#connect-clients).
The endpoint ends in `/mcp` and requires a bearer header. Keep credentials in private
configuration or environment variables.

Cursor supports `url` and `headers` in its user MCP settings:

```json
{"mcpServers":{"unshackle":{"url":"https://mcp.example.com/mcp","headers":{"Authorization":"Bearer ${env:UNSHACKLE_MCP_TOKEN}"}}}}
```

Codex:

```toml
[mcp_servers.unshackle]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "UNSHACKLE_MCP_TOKEN"
```

Client references: [Claude Code](https://code.claude.com/docs/en/mcp),
[Cursor](https://cursor.com/docs/context/mcp),
[Codex](https://developers.openai.com/codex/mcp/).

## Tools and limits

- `overview()` counts models, metrics, notebooks, tables and TODO markers. It reports
  warehouse configuration separately from verified connectivity; overview does not
  connect to the warehouse and never returns a connection URL.
- `search(query)` returns up to 100 literal, case-insensitive matches with paths,
  snippets and `has_todo`.
- `read(path)` returns context files. Notebooks return parameters, tile SQL and
  declarative chart specs. Neither notebook nor imported Python code is executed.
  Unsupported chart syntax retains `source_expression` and an explicit TODO.
  Dynamic, conditional or invalid notebook SQL assignments likewise retain their
  source with a TODO and return empty SQL, rather than a stale literal or no tile.
- `run_query(sql, limit=1000)` accepts one parsed SELECT/WITH/UNION query, with at most
  10,000 rows and a 30-second execution deadline. `--max-rows` and `--timeout` configure
  these server limits. The deadline starts after connection establishment; configure
  driver connection timeouts separately (for PostgreSQL append `?connect_timeout=10`).
  Queries fetch at most limit+1 rows, expose `truncated`, and cancel execution natively.
  Decimal values and integers outside JavaScript’s safe range (±(2**53−1)) are strings
  to preserve precision. Safe integers and booleans keep their JSON types. Dates use
  ISO strings, nulls/non-finite floats become JSON null, and binary values are hex strings.

Apps-capable clients can display an [interactive result table](mcp-result-table.md)
from the actual `run_query` response. It sorts, searches and exports only returned
rows, with a visible truncation notice; clients without Apps keep JSON/text results.
The bundled `ui://unshackle/query-results/v1.html` resource is separate from repository
files and contains no credentials or warehouse access of its own.

The same files are resources under `unshackle://repo/<path>`. Layout overrides in
`unshackle.toml` and dbt's `models/semantic` are respected. Only semantic, catalog,
notebook, knowledge, policy and reference-query paths are readable; hidden files,
symlinks, traversal, raw exports, local data files and files over 2 MB are excluded.
Put credentials only in environment variables or ignored `.env`, never context files.
A file's TODO flag includes static chart extraction failures.

## Security and known limits

The warehouse role enforces read-only access. The SQL parser is a seatbelt: SELECT
functions can have side effects. Use a restricted read-only role and a production
read replica. Policy files are context for the agent; `run_query` does not enforce
Cedar or row filters. Bearer auth protects a deployment, not individual users.
Everyone holding its token can query everything its one warehouse role can see.

One warehouse per server. Review each file's datasource/provenance before querying;
context from another connection does not switch the server's warehouse. Local data
under ignored `data/` can back a local DuckDB URL but is absent from a Git-pulled
server. This server does not execute saved queries by name or run an agent/model.
