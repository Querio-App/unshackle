# Host the MCP server

Use HTTP when several clients need the same Git-managed context and warehouse role.
Use local stdio when the repository or extracted data is only on your machine. Client
configurations and security details are in [MCP setup](../mcp.md).

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

Each guide separates account/signup/billing steps from repeatable deployment, uses
an isolated synthetic warehouse, and includes update and scoped teardown commands.
Read the resource/cost section before executing it. These commands create billable
resources when run; documentation preparation creates none. Provider CLIs can be
installed and their help checked before authentication. Interactive login/MFA needs
the account holder; a configured profile alone does not prove usable access.

See the [provider validation matrix](validation.md) for exact local checks and live
steps still untested. Prior [client acceptance](../mcp-acceptance.md) was local.
Do not count a recipe, SDK probe, or CLI exit status as a successful hosted client run.

## Connect clients

Complete the selected provider guide first. It supplies `UNSHACKLE_MCP_URL` (the
public HTTPS endpoint ending `/mcp`) and a private token file or secret-store read
command. Export `UNSHACKLE_MCP_TOKEN` in the client process environment without
echoing it; disable shell tracing. Do not put the token in command arguments, Git,
shell history, screenshots, or shared logs. If the guide produced a token file:

```sh
set +x
export UNSHACKLE_MCP_TOKEN="$(cat "$MCP_TOKEN_FILE")"
export UNSHACKLE_MCP_URL='https://your-deployment.example/mcp'
```

Replace the illustrative URL with the guide's output. The clients need their own
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
restart it from the configured shell, or use the CLI below. Approve the MCP tools
when prompted. Cursor remote servers do not use `envFile`.

## Record hosted acceptance

Run the provider's auth/SDK/role probe first: no bearer must yield 401, valid bearer
must initialize MCP and expose overview/search/read/run_query, and all five fixture
answers must match [questions.json](../../examples/mcp-demo/questions.json).
Include a direct database write denial, row cap, native timeout and successful query
after cancellation. A health probe alone does not check SQL authorization.

Then run each real client against the same hosted URL. From this source checkout,
with the provider-generated context folder available and the clients authenticated:

```sh
umask 077
export MCP_CONTEXT_DIR='/absolute/path/to/generated-context'
export MCP_EVIDENCE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unshackle-hosted.XXXXXX")"
for client in claude codex cursor; do
  .venv/bin/python examples/mcp-demo/client_matrix.py "$client" \
    --repo "$MCP_CONTEXT_DIR" --executable "$PWD/.venv/bin/unshackle" \
    --http-url "$UNSHACKLE_MCP_URL" --output "$MCP_EVIDENCE_DIR/$client"
done
```

The HTTP mode uses the remote server; the required local repo/executable arguments
are retained for the shared harness interface. It records client versions, source
commit and transport, and creates temporary client configurations without changing
persistent settings. Raw output stays private. Compare all five actual answers with
independent expected answers, and inspect successful context-tool and `run_query`
calls. A zero exit code or self-reported answer is insufficient. Record denied calls
and retries separately. Sanitize transcripts before publishing; never publish token,
warehouse URL, account identifiers or unrelated private data.

Record provider/region, exact image digest/source SHA, context SHA, client versions,
auth/probe results, expected-versus-actual answers, update/refresh behavior and
teardown verification in the [validation matrix](validation.md). SDK smoke is separate
from the three model/client runs. Finally execute the selected guide's scoped
teardown, confirm every owned billable resource is gone, remove only demo client
registrations/private files, and unset `UNSHACKLE_MCP_TOKEN`.
