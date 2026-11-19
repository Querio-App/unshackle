# Connect an agent to your repository

Install `uv pip install 'unshackle[mcp]'`. Migrate and verify a BI export, or import
warehouse/dbt/SQL context, then run `unshackle mcp --repo /absolute/path/to/repo`.
The default stdio transport keeps stdout exclusively for MCP protocol messages.
Set `UNSHACKLE_WAREHOUSE_URL` in the process environment or the repo's ignored `.env`.
For PostgreSQL use `postgresql+psycopg://...`; DuckDB and SQLite also support bounded
queries. Other drivers are rejected for MCP queries until they implement cancellation.

Use stdio for local files and local warehouse access. Use authenticated Streamable
HTTP for a shared deployment with its own warehouse role and Git checkout. Hosted
setup is covered by the deployment guides; the MCP endpoint is `/mcp`.

## Clients

The following configurations are setup examples, not evidence of live client acceptance.
Use absolute executable and repository paths if your client does not inherit PATH.

Claude Code:

```sh
claude mcp add --transport stdio unshackle -- unshackle mcp --repo /absolute/path/to/repo
```

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

For HTTP, configure your client with the deployment URL and bearer header. Keep
secrets in user-local configuration/environment, never a tracked project file.

```sh
claude mcp add --transport http unshackle https://mcp.example.com/mcp \
  --header "Authorization: Bearer ${UNSHACKLE_MCP_TOKEN}"
```

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
