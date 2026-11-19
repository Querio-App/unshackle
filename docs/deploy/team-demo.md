# Persistent Dunder Mifflin team demo

This recipe leaves a bearer-authenticated Railway service running for a team.
It contains only fictional data. [Connect Claude](claude-team-demo.md) covers the
remote app's request-header beta, Desktop bridge, Claude Code and shared access.
The current owner's endpoint, credential paths and exact resource inventory are
in a private handoff, never in this repository.

This deployment supersedes the old Railway rehearsal's context-repository deletion
instruction: the repository is intentionally retained under new user authorization,
with a dedicated branch preserving its original main/history. Missing GitHub
`delete_repo` scope is not a blocker and repository deletion is not part of shutdown.

The [interactive query table](../mcp-result-table.md) lets an Apps-capable client
sort, search and export returned rows directly. After an app update, start a new
conversation; reconnect the connector if its cached tool metadata still omits the UI.
The same endpoint and bearer credential continue to work.

## Dataset and prompts

The [generator](../../examples/dunder-mifflin-demo/fixture.py) reuses all five existing
Dunder Mifflin Parquet assets as `history_*`: 3,503 orders, 181 accounts, 15 products,
2,812 shippings and 1,655 reviews, January 2024–October 2026. Exported contact emails
use `example.com`. A separate 2025 scenario adds three branches, six sales employees,
12 customers, three reused product identities and 240 orders. These are ten physical
tables; the imported dbt model adds one logical catalog entry. Do not combine the
historical and scenario revenue. Catalog foreign keys, an Apache Ossie revenue metric,
and [business context](../../examples/dunder-mifflin-demo/business-context.md) explain
joins, grain, exclusions and unknown values.

Ask these questions in Claude; all amounts are USD. Full SQL, expected rows and
independent arithmetic live in [questions.json](../../examples/dunder-mifflin-demo/questions.json).

| Question | Verified result |
|---|---|
| Total recognized scenario revenue in 2025? | 92,664 |
| Recognized scenario revenue by branch? | Nashua 48,672; Scranton 13,104; Stamford 30,888 |
| Recognized scenario revenue by calendar month? | January 1,188, increasing by 1,188 monthly to December 14,256 |
| Leading sales rep and recognized revenue? | Danny Cordray, 25,272 |
| Recognized profit and gross margin? | 37,065.60; 0.4 (40%) |
| Excluded order count and net amount by status? | Cancelled 12 / 24,000; returned 12 / 12,000 |
| Leading customer and recognized revenue? | Example Customer 12, 13,608 |
| Historical accounts/orders/products/reviews/shippings row counts? | 181 / 3,503 / 15 / 1,655 / 2,812 |

Revenue arithmetic is independent of query output: month weights sum to 78, effective
rep weights sum to 19.8 after discounts, and product prices sum to 60; 78 × 19.8 × 60
= 92,664. Profit is 40% of this. The tests execute the SQL against freshly generated
DuckDB data and, opt-in, actual PostgreSQL with transaction read-only disabled for privilege checks.

## Fresh deployment

Requires Git, Python 3.11+, uv, jq, authenticated `gh` and Railway CLI (tested 5.75.0).
Use Bash, disable tracing, and keep this shell for subsequent sections. Authenticate
with the providers' normal login flow if needed; never put credentials in arguments.
Inspect `railway whoami --json`, `railway list --json`, and `gh api user` privately
before mutations. Choose the intended workspace explicitly. This creates billable
resources; no plan purchase/upgrade is part of the recipe.

```bash
set -euo pipefail
set +x
umask 077
git clone --branch feat/193-dunder-mifflin-team-demo https://github.com/Querio-App/unshackle.git
cd unshackle
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
export SOURCE_SHA="$(git rev-parse HEAD)"
export RW_WORKSPACE='REPLACE_WITH_VERIFIED_WORKSPACE_ID'
export RW_ENVIRONMENT=production
export RW_SERVICE=dunder-mifflin-mcp
export CONTEXT_REPO='REPLACE_OWNER/REPLACE_SYNTHETIC_CONTEXT_REPO'
export CONTEXT_BRANCH=feat/218-persistent-team-demo
export STATE="$HOME/.local/state/unshackle/dunder-mifflin-team-demo"
mkdir -p "$STATE"
chmod 700 "$STATE"
# Stop if project.json already exists: restore the saved project for updates instead.
test ! -e "$STATE/project.json"
railway whoami --json > "$STATE/identity.json"
railway list --json > "$STATE/projects-before.json"
railway api 'query($id:String!){workspace(workspaceId:$id){plan preferredRegion}}' \
  --raw-var "id=$RW_WORKSPACE" > "$STATE/plan.json"
railway init --name dunder-mifflin-team-demo --workspace "$RW_WORKSPACE" --json > "$STATE/project.json"
export RW_PROJECT="$(jq -er .id "$STATE/project.json")"
railway link --project "$RW_PROJECT" --environment "$RW_ENVIRONMENT"
railway add --database postgres
railway add --service "$RW_SERVICE"
railway status --json > "$STATE/ownership.json"
export RW_ENV_ID="$(jq -er '.environments.edges[].node | select(.name=="production") | .id' "$STATE/ownership.json")"
export RW_APP_ID="$(jq -er '.services.edges[].node | select(.name=="dunder-mifflin-mcp") | .id' "$STATE/ownership.json")"
export RW_DB_ID="$(jq -er '.services.edges[].node | select(.name=="Postgres") | .id' "$STATE/ownership.json")"
printf '%s\n' "$SOURCE_SHA" > "$STATE/source-sha"
printf '%s\n' "$CONTEXT_REPO" > "$STATE/context-repository"
printf '%s\n' "$CONTEXT_BRANCH" > "$STATE/context-branch"
```

Review the exact new project's services/volume before continuing. The observed
personal account was already Hobby; no upgrade was performed. Configure both services
with 0.5 vCPU / 0.5 GB caps; one app replica, no app volume, no additional database
replicas/backups. Actual deployment used US West for both services and the database
volume. New projects follow your account's region preference; select the same region
for app and database before seeding. The example scale command assumes US West.

```bash
railway api describe ServiceInstanceLimitsUpdateInput
for service in "$RW_APP_ID" "$RW_DB_ID"; do
  railway api 'mutation($input:ServiceInstanceLimitsUpdateInput!){serviceInstanceLimitsUpdate(input:$input)}' \
    --variables "$(jq -nc --arg s "$service" --arg e "$RW_ENV_ID" \
      '{input:{serviceId:$s,environmentId:$e,memoryGB:0.5,vCPUs:0.5}}')"
  railway api 'query($s:String!,$e:String!){serviceInstanceLimits(serviceId:$s,environmentId:$e)}' \
    --raw-var "s=$service" --raw-var "e=$RW_ENV_ID"
done
railway api 'mutation($s:String!,$e:String!,$input:ServiceInstanceUpdateInput!){serviceInstanceUpdate(serviceId:$s,environmentId:$e,input:$input)}' \
  --raw-var "s=$RW_APP_ID" --raw-var "e=$RW_ENV_ID" \
  --var 'input={"startCommand":"/usr/local/bin/unshackle-http --max-rows 1000 --timeout 15","sleepApplication":false,"numReplicas":1}'
railway service list
railway volume list
railway tcp-proxy create --port 5432 --service Postgres --environment "$RW_ENVIRONMENT"
railway variable set --service Postgres --environment "$RW_ENVIRONMENT" --skip-deploys \
  'DATABASE_PUBLIC_URL=postgresql://${{PGUSER}}:${{PGPASSWORD}}@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}/${{PGDATABASE}}'
# Wait until railway service list shows Postgres Online before running the seed once.
railway run --service Postgres --environment "$RW_ENVIRONMENT" \
  python examples/dunder-mifflin-demo/fixture.py --output "$STATE/fixture"
```

The fixture refuses nonempty databases/existing reader roles. A failed seed is not a
signal to drop objects or blindly rerun: preserve private recovery state and diagnose.
`example_reader` has SELECT only, no database/schema CREATE or temporary tables. Tests
attempt UPDATE/DELETE/CREATE/TEMP with transaction read-only disabled. URLs require TLS
(`sslmode=require`, encryption without CA identity verification; synthetic use only).
Admin credentials are injected by Railway and are not saved. Three private 0600 files
hold the reader URLs and shared token. Never copy the parent directory into Git.

Reuse an authorized synthetic repository on a new dedicated branch, preserving its
history. Review the staged generated content before publishing. The following removes
tracked files only on that new branch; it does not delete the repository or old branches.

```bash
git clone "https://github.com/$CONTEXT_REPO.git" "$STATE/context-repo"
git -C "$STATE/context-repo" switch -c "$CONTEXT_BRANCH"
git -C "$STATE/context-repo" rm -r .
cp -R "$STATE/fixture/context/." "$STATE/context-repo/"
git -C "$STATE/context-repo" add .
git -C "$STATE/context-repo" diff --cached --stat
# Review locally for credentials, unrelated content and non-fictional information.
git -C "$STATE/context-repo" diff --cached
git -C "$STATE/context-repo" commit -s -m 'Add fictional persistent team demo context'
git -C "$STATE/context-repo" push -u origin "$CONTEXT_BRANCH"
git -C "$STATE/context-repo" rev-parse HEAD > "$STATE/context-sha"
railway tcp-proxy delete 5432 --service Postgres --environment "$RW_ENVIRONMENT" --yes
rm "$STATE/fixture/reader-public-url"
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  UNSHACKLE_WAREHOUSE_URL --stdin < "$STATE/fixture/warehouse-url"
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  UNSHACKLE_MCP_TOKEN --stdin < "$STATE/fixture/mcp-token"
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --skip-deploys \
  "UNSHACKLE_REPO_URL=https://github.com/$CONTEXT_REPO.git" "UNSHACKLE_REPO_REF=$CONTEXT_BRANCH" \
  UNSHACKLE_POLL_INTERVAL=60 PORT=8000
railway scale --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" \
  us-west=1 us-east=0 eu-west=0 southeast-asia=0
export UPLOAD="$(mktemp -d "$STATE/upload.XXXXXX")"
git archive "$SOURCE_SHA" | tar -x -C "$UPLOAD"
git show "$SOURCE_SHA:Dockerfile" | sed '/^VOLUME \["\/tmp"\]$/d' > "$UPLOAD/Dockerfile"
shasum -a 256 "$UPLOAD/Dockerfile" > "$STATE/dockerfile-sha256"
railway up "$UPLOAD" --path-as-root --project "$RW_PROJECT" \
  --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --ci
railway domain --service "$RW_SERVICE" --port 8000 --environment "$RW_ENVIRONMENT"
railway status --json > "$STATE/ownership.json"
```

Railway rejects the root Dockerfile's `VOLUME /tmp`; the private archive removes only
that exact line. Core runtime, Dockerfile and entrypoint remain unchanged. Leave the
HTTP healthcheck path unset because every route requires bearer authentication.
Copy the generated HTTPS URL below, including `/mcp`, then verify readiness:

```bash
export UNSHACKLE_MCP_URL='https://REPLACE_GENERATED_DOMAIN/mcp'
printf '%s\n' "$UNSHACKLE_MCP_URL" > "$STATE/mcp-url"
export MCP_TOKEN_FILE="$STATE/fixture/mcp-token"
export UNSHACKLE_MCP_TOKEN="$(cat "$MCP_TOKEN_FILE")"
python examples/dunder-mifflin-demo/check.py
python examples/dunder-mifflin-demo/client_check.py claude \
  --repo "$STATE/context-repo" --executable "$PWD/.venv/bin/unshackle" \
  --output "$STATE/claude-acceptance" --http-url "$UNSHACKLE_MCP_URL"
```

The client harness invokes Claude's model and can consume subscription usage. It uses
temporary isolated config and saves raw transcripts privately. Review/sanitize evidence
before sharing. The SDK checks 401s, all tools/resources/context, eight answers, hard row
cap, native deadline, recovery and absence of lingering sleep queries. No team invitations
or messages are sent. Share only the endpoint and token through a private channel, never
warehouse credentials. See the [Claude connection instructions](claude-team-demo.md).

## Updates, access and costs

Restore saved state in Bash before updates; explicitly re-link, avoiding stale CLI links:

```bash
export STATE="$HOME/.local/state/unshackle/dunder-mifflin-team-demo"
export RW_PROJECT="$(jq -er .id "$STATE/project.json")"
export RW_SERVICE=dunder-mifflin-mcp RW_ENVIRONMENT=production
export UNSHACKLE_MCP_URL="$(cat "$STATE/mcp-url")"
export MCP_TOKEN_FILE="$STATE/fixture/mcp-token"
export UNSHACKLE_MCP_TOKEN="$(cat "$MCP_TOKEN_FILE")"
railway link --project "$RW_PROJECT" --environment "$RW_ENVIRONMENT"
railway status
export SOURCE_SHA="$(git rev-parse HEAD)" # reviewed, signed source revision
export UPLOAD="$(mktemp -d "$STATE/upload-update.XXXXXX")"
git archive "$SOURCE_SHA" | tar -x -C "$UPLOAD"
git show "$SOURCE_SHA:Dockerfile" | sed '/^VOLUME \["\/tmp"\]$/d' > "$UPLOAD/Dockerfile"
railway up "$UPLOAD" --path-as-root --project "$RW_PROJECT" \
  --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" --ci
python examples/dunder-mifflin-demo/check.py
```

For rollback, set `SOURCE_SHA` to the saved initial source SHA and repeat the complete
archive/sed/up sequence with a fresh upload directory. Runtime rollback does not roll
back the warehouse or context branch. Context changes use signed commits/pushes on the
saved branch; search/read should reflect them within the 60-second polling interval.
Never reseed the live database as an application update. The generator is for fresh
isolated databases. Restart periodically if frequent context changes accumulate snapshots.

One token grants every team member the same SELECT role; no per-person identity,
row filtering or individual revocation is provided. Policies in context are not enforced.
Removing a connector does not invalidate copied tokens. Rotate to revoke everyone:

```bash
set +x
umask 077
python - <<'PY'
import os,secrets
from pathlib import Path
path=Path(os.environ['STATE'])/'fixture/mcp-token-next'
with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
    f.write(secrets.token_urlsafe(32))
PY
railway variable set --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT" \
  UNSHACKLE_MCP_TOKEN --stdin < "$STATE/fixture/mcp-token-next"
# Wait for the new deployment to be Online, then verify using the new token.
export UNSHACKLE_MCP_TOKEN="$(cat "$STATE/fixture/mcp-token-next")"
python examples/dunder-mifflin-demo/check.py
python - <<'PYREVOKE'
import os
from pathlib import Path
import httpx
old=(Path(os.environ['STATE'])/'fixture/mcp-token').read_text().strip()
response=httpx.post(os.environ['UNSHACKLE_MCP_URL'],
                   headers={'Authorization':'Bearer '+old},json={},timeout=15)
assert response.status_code==401, 'Previous token still works; wait for old deployment to drain'
print('Previous token revoked')
PYREVOKE
mv "$STATE/fixture/mcp-token-next" "$STATE/fixture/mcp-token"
```

After rotation, regenerate private bridge/header files and update each connector.
Confirm the previous token returns 401 before considering revocation complete. During
redeployment allow for old instances draining; revocation is not instantaneous.

Both observed service limits are 0.5 CPU / 500,000,000 memory bytes, with one app replica.
The database has the template's 5,000 MB volume; no app volume is needed. Limits bound
resource use, not total spend. Idle memory, storage and egress cost money. The observed
plan was Hobby: its $5 monthly minimum includes $5 of resource usage, **not a spending
cap**, per [official pricing](https://docs.railway.com/pricing/plans). Free/trial restrictions
can affect persistence; see [Free plan](https://docs.railway.com/pricing/free-plan).
Do not upgrade without an owner decision. Any usage snapshot is only observed usage,
not a final bill or monthly forecast. Workspace hard spending limits affect unrelated
projects too; this recipe does not change them. Services remain running until shutdown.

## Shutdown only this deployment

Restore `STATE`, `RW_PROJECT`, `RW_SERVICE`, and `RW_ENVIRONMENT` above. Verify the saved
project ID/name and its exact two services, one volume and domain. Do not use an account
cleanup loop. This deletes fictional warehouse data and stops the retained demo.

```bash
railway link --project "$RW_PROJECT" --environment "$RW_ENVIRONMENT"
railway status
railway service list
railway volume list
railway domain list --service "$RW_SERVICE" --environment "$RW_ENVIRONMENT"
railway tcp-proxy list --service Postgres --environment "$RW_ENVIRONMENT"
railway unlink --yes
railway delete --project "$RW_PROJECT"
# CLI deletion may schedule a 48-hour grace period. For this exact owned project only:
railway api describe projectScheduleDeleteForce
railway api 'mutation($id:String!){projectScheduleDeleteForce(id:$id)}' --raw-var "id=$RW_PROJECT"
railway list --json > "$STATE/projects-after-delete.json"
python - <<'PY'
import json,os
from pathlib import Path
projects=json.loads((Path(os.environ['STATE'])/'projects-after-delete.json').read_text())
assert not any(p['id']==os.environ['RW_PROJECT'] for p in projects)
print('Exact owned project is absent')
PY
```

Verify the endpoint no longer serves MCP and the project's services/volume are gone.
This stops cloud resources; removing local credentials or connectors alone does not.
Retain the ownership record. After verifying no service uses it, optionally delete only
the demo's dedicated context branch (preserve the repository and original history):

```bash
export CONTEXT_BRANCH="$(cat "$STATE/context-branch")"
test "$CONTEXT_BRANCH" = feat/218-persistent-team-demo
git -C "$STATE/context-repo" push origin --delete "$CONTEXT_BRANCH"
```

Remove only this demo's connector entries and private credentials/header copies after
cloud shutdown. Preserve unrelated Desktop entries and any reusable bridge installation.
Do not delete the context repository without a separate explicit owner decision.

## Recorded acceptance and limits

2026-11-19: actual hosted SDK passed all eight questions, bearer 401 checks, four tools,
resources/context, cap/deadline/recovery and no lingering query. Actual Claude Code
2.1.308 completed eleven SQL calls: three discovery queries and eight answer queries;
all final answers reconcile to successful MCP rows. Direct database role denial passed
with read-only transaction defaults disabled. Resource limits were read back from Railway.
The owner reported a successful Claude **web app** conversation with context reads and
three SQL calls; the displayed branch totals match the fixture. This is user-observed
app acceptance, not an eight-question web matrix. Desktop-compatible bridge acceptance
was checked separately; no actual Desktop app conversation is claimed.

Source/runtime/image hashes and sanitized evidence are in the
[example evidence](../../examples/dunder-mifflin-demo/evidence.json). No OAuth gateway,
per-user access, production data, HA, custom domain or plan upgrade was implemented.
Local reproduction: `UNSHACKLE_TEAM_DEMO_LOCAL_TEST=1 pytest -q tests/test_team_demo.py`.
