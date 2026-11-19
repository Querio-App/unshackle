# Railway: CLI deployment from a fresh account

**Preparation only.** CLI 5.75.0 command help and isolated local Postgres/HTTP
checks have been exercised. Railway authentication, provisioning, build, HTTPS,
client acceptance, update and deletion are **UNTESTED**. Signup is complete for
this preparation session; authentication and billing eligibility remain pending.
Do not treat this recipe as executed cloud acceptance. See the
[validation matrix](validation.md) and [hosted evidence checklist](README.md#record-hosted-acceptance).

This recipe creates one disposable project, one `production` environment, one MCP
service, one Postgres service/volume and one generated synthetic GitHub context
repository. Railway builds/uploads the image internally from a Railway-compatible copy of the root
[Dockerfile](../../Dockerfile); a Docker registry account and separate `docker push`
are unnecessary. Source code and context are different repositories.

## One-time account and workstation setup

Use macOS/Linux or Windows WSL with Bash, Git, Python 3.11+, uv, jq and GitHub CLI.
Docker is needed only for the optional local test. Install the Railway CLI using
one of its [official installation methods](https://docs.railway.com/cli):

```bash
brew install railway                  # macOS; already installed in this prep session
# Alternative on a machine with Node: npm install -g @railway/cli@5.75.0
railway --version                     # recipe checked against 5.75.0
railway login                         # USER action: completes signup/login in browser
railway whoami                        # inspect account/workspace locally; don't publish output
# Headless machine only: railway login --browserless
```

Login is a human browser flow; never paste an account token into this document,
a command argument or chat. Future CI can use a separately provisioned project
token in `RAILWAY_TOKEN`, delivered by the CI secret store. Project tokens cannot
perform all account/project-creation operations. See [login](https://docs.railway.com/cli/login)
and [CLI authentication](https://docs.railway.com/cli#authentication).

In the Railway dashboard, select the intended workspace and complete any required
account verification/payment setup. Review its current [plan and usage prices](https://docs.railway.com/pricing/plans)
and [cost controls](https://docs.railway.com/pricing/cost-control) before creating
services. Trial eligibility/credits are account-dependent; this is not a free-hosting
promise. Start with one app replica and one single-node Postgres, no HA, replicas
or backups for this disposable dataset. Set a deliberate workspace budget in the
console; a hard limit affects **all** workloads in that workspace. After creating
the services, set their per-replica CPU/memory caps under Settings → Deploy → Replica
Limits (for example 1 vCPU/1 GB initially, then measure). Volume size and egress also
cost money. Keep app Serverless/sleep disabled while testing periodic Git refresh.

Before adding Postgres, set the account's preferred region to **EU West / Amsterdam**
(or your chosen region) in Account Settings. New services use that preference.
The CLI scale command below pins the app to one EU West replica. Check the database
and volume region in service settings before seeding; moving an existing volume
requires migration/downtime. Region choices and identifiers are documented in
[Regions](https://docs.railway.com/deployments/regions).

Sign into GitHub separately (`gh auth login`) if needed. Configure local Git author
identity and permission to create/delete a repository. These credentials serve the
local Git push; they are not sent to the runtime. If Git push asks for credentials,
`gh auth setup-git` configures the local GitHub credential helper.

## Prepare names and source

Run the following sections in the **same Bash session**. Replace the workspace and
GitHub owner placeholders. All cloud commands below are deferred until deployment
is explicitly authorized. No command here is an all-account cleanup.

Use a fresh source clone containing this guide. Until epic #193 merges, the feature
branch contains runtime changes absent from `main`; record the exact commit used:

```bash
git clone --branch feat/193-railway-cli https://github.com/Querio-App/unshackle.git
cd unshackle
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
set -euo pipefail                      # never enable xtrace / set -x
umask 077
export SOURCE_DIR="$PWD"
export SOURCE_SHA="$(git rev-parse HEAD)"
export RW_WORKSPACE='REPLACE_WITH_YOUR_WORKSPACE_ID'
export GH_OWNER='REPLACE_WITH_YOUR_GITHUB_OWNER'
export PREFIX="unshackle-railway-$(date +%Y%m%d)-$(python -c 'import secrets; print(secrets.token_hex(3))')"
export RW_SERVICE="$PREFIX-mcp"
export RW_DB_SERVICE=Postgres           # name used by railway add --database postgres
export RW_ENVIRONMENT=production
export CONTEXT_REPO="$GH_OWNER/$PREFIX-context"
mkdir -p "$HOME/.cache"
export STATE="$(mktemp -d "$HOME/.cache/unshackle-railway.XXXXXX")"
printf '%s\n' "$SOURCE_SHA" > "$STATE/source-sha"
printf '%s\n' "$CONTEXT_REPO" > "$STATE/context-repo"
```

`STATE` is private and outside both Git repositories and the upload directory. Keep
it until teardown succeeds. Commands use `--service` and `--environment` explicitly;
`railway add` changes the linked service. Never use `railway variable list`, `railway
run env`, `--verbose`, shell tracing or raw credential output as troubleshooting.

## Create the isolated project and database — UNTESTED

[init](https://docs.railway.com/cli/init) creates and links a project;
[add](https://docs.railway.com/cli/add) immediately deploys a database. This is where
cloud spending begins. Record ownership before adding anything:

```bash
railway init --name "$PREFIX" --workspace "$RW_WORKSPACE" --json > "$STATE/project.json"
export RW_PROJECT="$(jq -er '.id' "$STATE/project.json")"
railway link --project "$RW_PROJECT" --environment "$RW_ENVIRONMENT"
railway add --database postgres
railway add --service "$RW_SERVICE"
railway status
railway service list
railway volume list
```

Check that `Postgres` belongs to this new project. In service settings confirm the
chosen database/volume region and resource caps, and wait for Postgres to be running.
The [database template](https://docs.railway.com/databases/postgresql) provides
`DATABASE_URL` on the private network. A temporary
[TCP proxy](https://docs.railway.com/cli/tcp-proxy) lets the local fixture importer
connect; remove it after seeding and context generation:

```bash
railway tcp-proxy create --port 5432 --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT"
# Reference expressions only, not resolved credentials; single quotes are required.
railway variable set --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  'DATABASE_PUBLIC_URL=postgresql://${{PGUSER}}:${{PGPASSWORD}}@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}/${{PGDATABASE}}'
railway run --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT" \
  python deploy/railway/fixture.py --output "$STATE/fixture"
export CONTEXT="$STATE/fixture/context"
```

[railway run](https://docs.railway.com/cli/run) injects credentials directly into the
child environment. The helper uses the existing fictional customers/orders,
warehouse/dbt/SQL importers, and creates `example_reader`: SELECT on just those two
tables, no database/schema creation, no temp tables, and read-only transactions.
It checks UPDATE/DELETE/CREATE/TEMP denial with read-only mode disabled; transaction defaults alone
are not authorization. It refuses an existing reader role or nonempty database.
The private warehouse URL uses `postgresql+psycopg`, the included driver with native
cancellation, and `connect_timeout=10`. Both connections require TLS. `sslmode=require`
encrypts but does not verify server identity; production warehouses should supply a
trusted CA and `verify-full` with the matching hostname. This recipe is synthetic only.

Three 0600 files remain outside context: `warehouse-url`, `reader-public-url`,
`mcp-token`. No admin credential is saved. On failure, stop and retain those private
files; do not rerun against a partially seeded database or silently drop objects.
For this disposable project, teardown and recreate it with a new prefix.

## Publish and check the synthetic context

Only `context/` is published; the surrounding directory contains secrets. Review
`git diff --cached` before pushing. Do not put any customer material in this public
fixture repository. The intentionally dynamic Python query stays TODO and is never
executed by the importer.

```bash
git -C "$CONTEXT" init --initial-branch=main
git -C "$CONTEXT" add .
git -C "$CONTEXT" diff --cached --stat
git -C "$CONTEXT" diff --cached          # review contents locally
git -C "$CONTEXT" commit -s -m 'Add synthetic unshackle MCP context'
gh repo create "$CONTEXT_REPO" --public --source "$CONTEXT" --remote origin --push
export UNSHACKLE_REPO_URL="https://github.com/$CONTEXT_REPO.git"
export UNSHACKLE_REPO_REF=main
# Exercise the same noninteractive URL/ref fetching as the server; no user Git helper.
python - <<'PY'
import os
from unshackle.mcp.sync import GitRefresh
sync = GitRefresh(os.environ['UNSHACKLE_REPO_URL'], os.environ['UNSHACKLE_REPO_REF'])
try:
    sync.refresh()
    print('Context Git URL/ref fetched and validated')
finally:
    sync.close()
PY
railway tcp-proxy list --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT"
railway tcp-proxy delete 5432 --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT" --yes
rm "$STATE/fixture/reader-public-url"
```

**Private context repositories:** the stock runtime strips Git environment-based
credentials and disables global credential helpers and prompts. An HTTPS token in
the URL is rejected; Railway's source-GitHub connection is not runtime clone access.
This recipe deliberately uses a public synthetic repository. A private SSH repository
requires separately provisioned read-only deploy-key/agent access and verified
`known_hosts`, readable by UID 10001, at runtime on every restart. Do not bake keys into
the image, use build args, disable host-key checking or assume an environment PAT
works. SSH mounts are optional infrastructure outside this tested recipe; validate
private clone access inside the deployed container before switching the URL. See
[Git refresh behavior](README.md#git-refresh-behavior).

## Configure secrets, build and publish HTTPS — UNTESTED

[Variable stdin](https://docs.railway.com/cli/variable) keeps secret values out of
process arguments/history. Do not substitute `$(cat ...)` into a `KEY=value` argument.
Set non-secret configuration normally; wait to deploy until both secrets exist:

```bash
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  UNSHACKLE_WAREHOUSE_URL --stdin < "$STATE/fixture/warehouse-url"
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  UNSHACKLE_MCP_TOKEN --stdin < "$STATE/fixture/mcp-token"
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  "UNSHACKLE_REPO_URL=$UNSHACKLE_REPO_URL" "UNSHACKLE_REPO_REF=$UNSHACKLE_REPO_REF" \
  UNSHACKLE_POLL_INTERVAL=60 PORT=8000
# Change which region has 1; keep all other regions at 0 (scale merges assignments).
railway scale --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" \
  eu-west=1 us-west=0 us-east=0 southeast-asia=0
# Upload only tracked source, never local credential/config files or .venv.
mkdir "$STATE/upload"
git archive "$SOURCE_SHA" | tar -x -C "$STATE/upload"
# Railway rejects Docker VOLUME; remove only the root image's /tmp declaration.
git show "$SOURCE_SHA:Dockerfile" | sed '/^VOLUME \["\/tmp"\]$/d' > "$STATE/upload/Dockerfile"
railway up "$STATE/upload" --path-as-root --project "$RW_PROJECT" \
  --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --ci
railway service list
railway domain --service "$RW_SERVICE" --port 8000 --environment "$RW_ENVIRONMENT"
```

[up](https://docs.railway.com/cli/up) uploads the source and uses its Dockerfile for
Railway's remote image build/push/deploy. Railway rejects Docker `VOLUME` instructions;
the temporary upload removes exactly `VOLUME ["/tmp"]` while retaining the existing
entrypoint, dependencies, non-root user and writable temporary directory. The shared
Dockerfile remains unchanged; this server does not need a persistent `/tmp` volume. `--ci` waits for build output, **not** hosted
acceptance. Do not use `--new` on updates. The root entrypoint binds `0.0.0.0:$PORT`
and runs as UID/GID 10001. No persistent app volume is needed; Postgres owns the data
volume. Runtime Git snapshots use temporary disk, retained until restart.

[domain](https://docs.railway.com/cli/domain) supplies an HTTPS `*.up.railway.app`
address with managed TLS. Copy that exact hostname into the next step. No purchased
domain or DNS step is needed. A custom domain is optional: it requires your DNS
provider's record changes and certificate validation, and those external records must
also be removed during teardown.

Leave Railway's HTTP Healthcheck Path **unset**: every app route requires bearer
auth, so an unauthenticated Railway health request gets 401. There is no public
`/health` endpoint. Railway expects 2xx for its configured
[HTTP healthcheck](https://docs.railway.com/deployments/healthchecks); the Dockerfile's
socket check is not hosted protocol acceptance.

## Endpoint and client checks — UNTESTED on Railway

```bash
export UNSHACKLE_MCP_URL='https://REPLACE_WITH_GENERATED_HOSTNAME/mcp'
export UNSHACKLE_MCP_TOKEN="$(cat "$STATE/fixture/mcp-token")"
python deploy/railway/check.py
```

The Python probe sends secrets only in request headers, never command arguments.
It rejects HTTP and redirects, requires 401 for missing/wrong bearer, initializes
MCP protocol `2025-11-25`, sends `notifications/initialized`, lists tools/resources,
reads context/notebooks, rejects `.env`, verifies `example_reader` and read-only
mode, checks a two-row cap, rejects a DELETE, and runs `pg_sleep(35)` to check the
30-second deadline followed by a successful query and zero active sleep queries. The fixture seed separately
proved database write denial, beyond the MCP SQL parser. Native psycopg cancellation
and Postgres statement timeout bound execution; connection time has its own timeout.

The probe compares every answer against [questions.json](../../examples/mcp-demo/questions.json):

| Question | Hand-derived result |
| --- | --- |
| Paid revenue | 100 + 50 + 80 + 120 = **350 USD** |
| Revenue by customer region | North 100 + 50 + 120 = **270**, South **80** |
| Top customer | Acme Paper 100 + 50 = **150** |
| Revenue by month | January 100 + 50 + 80 = **230**, February **120** |
| Average paid order | 350 / 4 = **87.5** |

Refunded 40 and cancelled 30 are excluded. Decimal strings are compared numerically.
A probe pass is HTTP/SQL evidence, not a real client/model run. Use the exact
[Claude, Codex and Cursor HTTP configurations](README.md#connect-clients) with the
exported endpoint/token, then the existing
[client_matrix harness and evidence steps](README.md#record-hosted-acceptance).
Run clients from this shell so the environment reaches them; restart GUI clients
with an appropriate private environment if necessary. Those client runs may consume
the user's model subscription and remain deferred during preparation.

## Logs, updates and troubleshooting — UNTESTED on Railway

```bash
railway logs --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --lines 100
railway logs --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --build --lines 100
railway logs --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT" --lines 100
```

See [logs](https://docs.railway.com/cli/logs). Keep raw logs/transcripts private and
review them before publishing. Never print service variables to debug connection
failures. A 401 means bearer mismatch/missing header; 502 means check deployment,
port 8000/domain target and initial Git fetch. An authenticated GET is not a valid
MCP tool call. A warehouse error means check private DNS, the shared project/environment,
TLS and role; `overview` reports configuration without proving connectivity. Failed
Git refresh keeps the last good snapshot and emits a generic warning. A missing ref
at first startup prevents the server from starting.

For a context update, commit/push a harmless file change under `knowledge/`, wait
at least 60 seconds, then confirm it via MCP `search`/`read`; the branch `main` tracks
updates, while a pinned commit intentionally does not. Revert the change and check
refresh again. For application updates, select a reviewed new source commit, create a **new**
upload directory, apply the same Dockerfile adaptation, and target the saved service:

```bash
export SOURCE_SHA="$(git rev-parse HEAD)"   # after selecting the reviewed revision
export UPDATE_UPLOAD="$(mktemp -d "$STATE/upload-update.XXXXXX")"
git archive "$SOURCE_SHA" | tar -x -C "$UPDATE_UPLOAD"
git show "$SOURCE_SHA:Dockerfile" | sed '/^VOLUME \["\/tmp"\]$/d' > "$UPDATE_UPLOAD/Dockerfile"
railway up "$UPDATE_UPLOAD" --path-as-root --project "$RW_PROJECT" \
  --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --ci
railway service list
python deploy/railway/check.py
```

Record both source and context SHAs, and repeat probe/client checks. To roll back, use the initial recorded SHA and the same adaptation (the rollback
sequence is locally checked; an actual Railway rollback was not exercised):

```bash
export SOURCE_SHA="$(cat "$STATE/source-sha")"
export ROLLBACK_UPLOAD="$(mktemp -d "$STATE/upload-rollback.XXXXXX")"
git archive "$SOURCE_SHA" | tar -x -C "$ROLLBACK_UPLOAD"
git show "$SOURCE_SHA:Dockerfile" | sed '/^VOLUME \["\/tmp"\]$/d' > "$ROLLBACK_UPLOAD/Dockerfile"
railway up "$ROLLBACK_UPLOAD" --path-as-root --project "$RW_PROJECT" \
  --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --ci
railway service list
python deploy/railway/check.py
```

Restart periodically to reclaim old Git snapshots if context changes frequently.

For bearer rotation, create a new 0600 token file with Python's `secrets.token_urlsafe(32)`,
set it with `railway variable set ... --stdin`, and redeploy/recheck clients. Avoid
`--skip-deploys` during rotation unless you explicitly redeploy afterward. The server
reads its bearer at startup. A shared bearer grants access to the deployment's one
warehouse role; it does not identify individual users or enforce policy files.

## Scoped teardown of everything created — UNTESTED

The project contains only this demo. Confirm its saved ID/name and inspect services,
volumes, proxy and domain before deletion. Run this from the same source session;
if resuming, restore `STATE` and the saved identifiers first, then re-link explicitly.
Do not use a broad account-level list/delete loop.

```bash
export RW_PROJECT="$(jq -er '.id' "$STATE/project.json")"
export CONTEXT_REPO="$(cat "$STATE/context-repo")"
railway link --project "$RW_PROJECT" --environment "$RW_ENVIRONMENT"
railway status
railway service list
railway volume list
railway domain list --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT"
railway tcp-proxy list --service "$RW_DB_SERVICE" --environment "$RW_ENVIRONMENT"
# Human confirms the exact dedicated project; interactive prompt can handle MFA.
railway delete --project "$RW_PROJECT"
# Deletion can be scheduled: inspect until this exact project is absent.
railway list
# Delete ONLY the generated public synthetic context repository.
gh repo delete "$CONTEXT_REPO" --yes
railway unlink
```

[Project deletion](https://docs.railway.com/cli/delete) removes the project's services,
deployments and data, including this Postgres volume and its fixture role/tables;
its generated service domain/proxy belong to the project. CLI 5.75.0 reports that
deletion may be scheduled: successful submission is not proof cleanup has finished.
Verify removal in the project list/dashboard and billing usage before deleting the
local ownership record. If deletion needs MFA, complete it in the human terminal or
dashboard; do not pass codes through command arguments/logs. `railway down` alone
is insufficient: storage/project resources remain.

Also remove any optional external DNS records, repo deploy keys or project tokens
you created; none are required by the baseline. Remove only this server's client
configuration entries (see the shared client guide). The GitHub delete may require
the `delete_repo` scope; grant it interactively only if needed, then retry that exact
repository. If either deletion fails, retain `STATE` and finish cleanup first.

After verified remote cleanup, remove private local artifacts and unset credentials:

```bash
case "$STATE" in
  "$HOME"/.cache/unshackle-railway.*) rm -rf -- "$STATE" ;;
  *) printf '%s\n' 'Unexpected STATE path; refusing local cleanup' >&2; exit 1 ;;
esac
unset UNSHACKLE_MCP_TOKEN UNSHACKLE_WAREHOUSE_URL UNSHACKLE_MCP_URL
unset DATABASE_URL DATABASE_PUBLIC_URL RAILWAY_TOKEN RAILWAY_API_TOKEN
```

Local validation is opt-in and automatically removes its synthetic Docker container,
anonymous volume and private files, including on failure:

```bash
UNSHACKLE_RAILWAY_LOCAL_TEST=1 pytest -q tests/test_deploy_railway.py
```

Official references above were checked 2026-11-19 against Railway CLI 5.75.0.
No cloud execution or client acceptance is implied by the local validation.
