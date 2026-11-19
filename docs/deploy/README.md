# Host the MCP server

Use HTTP when several clients need the same Git-managed context and warehouse role.
Use local stdio when the repository or extracted data is only on your machine. Client
configurations and security details are in [MCP setup](../mcp.md).

Start with **your context repository and your read-only warehouse role**. Finish the
[local first-question check](../mcp.md) before sharing access. Review context files for
sensitive comments and queries, and use a repository-scoped read-only deploy key for
private Git. Choose a PostgreSQL read replica for shared production access when available.
The server does not implement per-user authorization or enforce policy files.

Required secrets:

- `UNSHACKLE_WAREHOUSE_URL`: SQLAlchemy URL for a read-only role. PostgreSQL uses
  `postgresql+psycopg://.../database?connect_timeout=10` (add TLS parameters as required).
- `UNSHACKLE_MCP_TOKEN`: random ASCII bearer token, at least 32 characters. The
  provider recipes generate it into a private file or secret store without printing it.

The context repository URL/ref/poll interval are ordinary configuration:
`UNSHACKLE_REPO_URL`, `UNSHACKLE_REPO_REF` (default `main`), and
`UNSHACKLE_POLL_INTERVAL` (seconds, default 60). Branch names, tags and exact commit
SHAs are supported. Do not embed credentials in the URL; private repositories can
use a separately mounted SSH key/agent and known_hosts. Git uses no interactive
prompts or credential helpers. HTTPS public repositories need only the two secrets.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'unshackle[mcp]'
# Load UNSHACKLE_WAREHOUSE_URL privately from your secret store first.
set +x
export UNSHACKLE_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
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
your runtime where available. For a mounted snapshot, use a separate `docker run` or
Compose configuration: the supplied Compose file requires REPO_URL. Omit REPO_URL in
your configuration and mount the snapshot read-only at `/repo`, with directories searchable and files readable by UID
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

## Optional synthetic demos

For fictional data only, use the [persistent Dunder Mifflin team demo](team-demo.md)
and its [Claude onboarding](claude-team-demo.md). Keep its ownership/state private;
it stays running until its owner shuts it down.

Provider examples provision isolated synthetic databases and include scoped teardown:

- [Railway](railway.md)
- [AWS ECS Fargate](aws.md)
- [Google Cloud Run](cloud-run.md)

These helpers are not general warehouse provisioners. Do not point fixture seeders at
your own warehouse. For your own data, use the CLI/container setup above and supply your
networking, TLS and secret store. Starting a provider demo creates billable resources;
review its scope and prerequisites before running it.

## Connect clients

Obtain the server’s HTTPS endpoint ending `/mcp` and its token from the deployment
owner through a private channel. Clients need no warehouse credentials. Set
`UNSHACKLE_MCP_URL` and load the bearer from a private token file or secret store. Export `UNSHACKLE_MCP_TOKEN` in the client process environment without
echoing it; disable shell tracing. Do not put the token in command arguments, Git,
shell history, screenshots, or shared logs. If the guide produced a token file:

```sh
set +x
export UNSHACKLE_MCP_TOKEN="$(cat "$MCP_TOKEN_FILE")"
export UNSHACKLE_MCP_URL='https://your-deployment.example/mcp'
```

Replace the illustrative URL with your deployment URL. The clients need their own
installed, authenticated model accounts; see [Claude Code setup](https://code.claude.com/docs/en/setup),
[Codex CLI](https://developers.openai.com/codex/cli/), and
[Cursor CLI installation](https://cursor.com/docs/cli/installation).
Login and model requests may need browser interaction and consume your plan.

For **Claude Code**, save this JSON as `unshackle.mcp.json` in a dedicated client
folder (it contains environment references, not credentials), then launch from the
shell holding the variables:

```json
{"mcpServers":{"unshackle":{"type":"http","url":"${UNSHACKLE_MCP_URL}","headers":{"Authorization":"Bearer ${UNSHACKLE_MCP_TOKEN}"}}}}
```

```sh
claude --strict-mcp-config --mcp-config ./unshackle.mcp.json
```

Use `/mcp` to inspect connectivity and approve the four unshackle tools when
prompted. [Claude's environment expansion](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson)
keeps this configuration reusable.

For **Codex CLI**, register the endpoint once; only the environment variable's name
is persisted. Start Codex from the same environment:

```sh
codex mcp add unshackle --url "$UNSHACKLE_MCP_URL" \
  --bearer-token-env-var UNSHACKLE_MCP_TOKEN
codex
# After the demo, remove only this registration:
# codex mcp remove unshackle
```

Use a different server name if `unshackle` already exists. The
[Codex MCP configuration](https://developers.openai.com/codex/mcp/)
uses bearer authentication here; this server does not offer OAuth login.

For **Cursor**, put this in a new client folder's `.cursor/mcp.json`, then open that
folder from an environment containing the variables. Do not overwrite an existing
MCP configuration:

```json
{"mcpServers":{"unshackle":{"url":"${env:UNSHACKLE_MCP_URL}","headers":{"Authorization":"Bearer ${env:UNSHACKLE_MCP_TOKEN}"}}}}
```

[Cursor interpolates environment variables in URL and headers](https://cursor.com/docs/mcp#config-interpolation).
A desktop app already running may not inherit newly exported shell variables;
restart it from the configured shell. Approve the MCP tools
when prompted. Cursor remote servers do not use `envFile`.

## Check access and the first answer

Confirm a request without a token receives 401. Then use your chosen client to call
`overview`, find/read a known definition, and run one small query with an independently
known answer. `overview` is a context check, not a warehouse connection test. Confirm
read-only privileges at the database role, including functions it can invoke; SQL
parsing alone does not prevent side effects. Do not test writes against business data.

A result table is optional host behavior; JSON/text results remain usable. If a token
holder should see less than this server's warehouse role, stop: configure a separate
restricted role/server or choose an authorization design that supports that requirement.
A three-client benchmark is not needed to onboard a teammate. Optional synthetic
client checks are documented with the [runnable example](../../examples/mcp-demo/README.md).

## Updates, access and shutdown

Publish reviewed context changes through your repository's normal review process.
Moving refs refresh on the configured interval; a pinned SHA requires changing the
ref and restarting the server. Confirm changed text through `search`/`read` and rerun
an affected question. To roll back, restart with a reviewed earlier context ref; this
does not roll back the runtime image or warehouse. See [maintenance](../maintain.md).

Runtime updates use a reviewed package/image revision and preserve the same context
and secret configuration. Restart periodically to reclaim retained snapshots. Generate
a new random token in your secret store, restart all instances with it, update clients,
and confirm the old token returns 401 after old instances drain. Removing one client
registration does not revoke a copied token; rotation revokes shared access for all.

Stop a foreground process with Ctrl-C. For the Compose example, from the same checkout
and project run `docker compose -f deploy/compose.yml down`. For a hosted deployment,
use its saved resource inventory and provider teardown section; inspect the exact
project/services/storage before deletion. Client removal alone does not stop billing.
Never run a demo's teardown against a retained or unrelated environment. Remove only
that deployment's local connector entries and private credentials after shutdown.
