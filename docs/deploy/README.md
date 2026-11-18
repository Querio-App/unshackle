# Host the MCP server

Use HTTP when several clients need the same Git-managed context and warehouse role.
Use local stdio when the repository or extracted data is only on your machine. Client
configurations and security details are in [MCP setup](../mcp.md).

Required secrets:

- `UNSHACKLE_WAREHOUSE_URL`: SQLAlchemy URL for a read-only role. PostgreSQL uses
  `postgresql+psycopg://.../database?connect_timeout=10` (add TLS parameters as required).
- `UNSHACKLE_MCP_TOKEN`: random ASCII bearer token, at least 32 characters. Generate
  with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.

The context repository URL/ref/poll interval are ordinary configuration:
`UNSHACKLE_REPO_URL`, `UNSHACKLE_REPO_REF` (default `main`), and
`UNSHACKLE_POLL_INTERVAL` (seconds, default 60). Branch names, tags and exact commit
SHAs are supported. Do not embed credentials in the URL; private repositories can
use a separately mounted SSH key/agent and known_hosts. Git uses no interactive
prompts or credential helpers. HTTPS public repositories need only the two secrets.

```sh
uv pip install 'unshackle[mcp]'
unshackle mcp --transport http --host 127.0.0.1 --port 8000 \
  --repo-url https://github.com/example/analytics-context.git --repo-ref main
```

Every HTTP route requires `Authorization: Bearer <token>`, including tool calls and
resource reads. `/mcp` serves Streamable HTTP, with stateless requests and JSON
responses. No OAuth, cookies, user identity, or unauthenticated context endpoint.
Terminate TLS at your platform/reverse proxy; bind directly only on trusted networks.

## Container

From the unshackle source checkout:

```sh
docker build -t unshackle-mcp .
# Export both secrets and UNSHACKLE_REPO_URL in your shell first.
docker compose -f deploy/compose.yml up --build -d
```

The image includes PostgreSQL (psycopg), DuckDB and SQLite drivers. Other warehouses
are not supported for timed MCP queries yet. It runs as UID/GID 10001; the Compose
example also makes the root filesystem read-only, drops Linux capabilities, and
allows writes only to a bounded temporary filesystem. Use the same restrictions in
your runtime where available. To serve a pre-existing snapshot, omit REPO_URL and
mount it read-only at `/repo`, with directories searchable and files readable by UID
10001; never mount your whole home directory. With Colima, place bind-mounted context
under a shared host path (normally your home directory), not an unshared macOS temp directory.

## Git refresh behavior

The initial fetch must succeed before HTTP starts. Every timer tick fetches the
selected ref into a separate checkout, disables hooks/filters/global Git config,
never initializes submodules or executes repository code, and validates the context
before atomically publishing its path. Failed fetches or invalid layout/notebooks
retain the last good snapshot. The process logs a generic warning without Git URLs,
credentials or command output. Empty repositories are valid; monitor context changes
through `overview` and your Git history.

Requests already reading an older snapshot finish against that snapshot. Snapshots
are retained until process shutdown, then cleaned up; unchanged revisions create no
new snapshot. Budget temporary disk for changed revisions and restart periodically
for long-running/high-churn repositories. If disk fills, refresh fails while the last
snapshot keeps serving. A pinned SHA intentionally never advances. Separate replicas
can briefly serve different commits while polling.

Git-ignored `data/` and `.env` never arrive from Git. There is one warehouse role per
server; token holders can access everything it can read. SQL parsing does not enforce
policies or prevent side effects of database functions. Local DuckDB/SQLite execute
with the server process's filesystem permissions and are not a sandbox. Prefer a
read-only PostgreSQL role and production read replica for shared deployments.

## Platform guides

- [Railway](railway.md)
- [AWS ECS Fargate](aws.md)
- [Google Cloud Run](cloud-run.md)

The guides are deployment recipes, not claims of executed cloud acceptance. Client
and Railway acceptance are recorded separately by the epic acceptance slice.
