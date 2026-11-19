# Google Cloud Run: CLI deployment rehearsal

**Preparation only. All GCP login, API enablement, provisioning, deployment, update
and teardown commands below are UNTESTED against GCP.** No cloud resources were
created. The helpers have offline/mock and opt-in local Docker checks; those do not
prove Cloud Run, Cloud SQL, billing, IAM or hosted client acceptance. See the shared
[validation record](validation.md) for the current evidence.

This recipe uses one **new disposable project** for one synthetic demonstration:
Artifact Registry, Cloud Run, Cloud SQL PostgreSQL 16 and Secret Manager. Run commands
in Bash from the unshackle checkout unless a block says otherwise. Never use an
existing production project. Cloud Run supplies a managed `run.app` HTTPS hostname;
no custom domain, DNS registration or certificate purchase is needed.

## One-time signup, tools and login

Google account creation, payment verification and acceptance of billing terms require
the account owner in the browser. [Create/manage the billing account](https://docs.cloud.google.com/billing/docs/how-to/manage-billing-account).
Existing accounts still need an active billing account and permission to attach projects.
Organization policies may require an administrator to approve project creation,
regions or public Cloud Run access. Do not bypass those policies.

Install Git, Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
[Docker with buildx](https://docs.docker.com/build/install-buildx/) and
[GitHub CLI](https://cli.github.com/manual/installation). Docker must be running.
Install the [Google Cloud CLI](https://docs.cloud.google.com/sdk/docs/install-sdk)
without replacing an existing installation; example for **macOS Apple Silicon**:

```bash
mkdir -p "$HOME/.local/unshackle-gcp-tools"
cd "$HOME/.local/unshackle-gcp-tools"
curl -fLO https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-arm.tar.gz
tar -xf google-cloud-cli-darwin-arm.tar.gz
./google-cloud-sdk/install.sh --quiet --path-update=false --command-completion=false
export PATH="$HOME/.local/unshackle-gcp-tools/google-cloud-sdk/bin:$PATH"
# Choose linux.amd64, linux.arm64 or darwin.amd64 on another platform.
curl -fLo cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.28.0/cloud-sql-proxy.darwin.arm64
chmod 755 cloud-sql-proxy
export PATH="$HOME/.local/unshackle-gcp-tools:$PATH"
gcloud version
cloud-sql-proxy --version
```

Use the official installer table for other platforms and current CLI checksums.
The [Cloud SQL Auth Proxy installation and authentication guide](https://docs.cloud.google.com/sql/docs/postgres/connect-auth-proxy)
provides platform binaries. The proxy normally uses Application Default Credentials (ADC),
which are separate from the gcloud account. This short rehearsal uses its supported
`--gcloud-auth` mode to reuse the selected CLI identity without a second login.
The proxy labels that flag legacy; prefer ADC for ongoing local development
(and omit `--gcloud-auth` when using ADC). These **interactive** commands are for
the future operator, never for an unattended preparation run:

```bash
gcloud auth login
# Optional ADC alternative: gcloud auth application-default login
gh auth login
```

If already signed in, inspect `gcloud auth list --filter=status:ACTIVE --format='value(account)'`
and `gh auth status` privately. Select the intended account with
`gcloud config set account YOUR_ACCOUNT_EMAIL`; do not publish that identifier.
No service-account key download is needed. Do not use `--log-http`, debug logging,
`set -x`, shell recording or commands that print access tokens.

## Choose the scope and cost

Return to the source checkout, install dependencies and keep all state outside Git:

```bash
uv venv
uv pip install -e '.[dev]'
source .venv/bin/activate
umask 077
export CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true
export PROJECT="unshackle-demo-$(python -c 'import secrets; print(secrets.token_hex(4))')"
export REGION=europe-west1
export PREFIX=ush193
export SQL_TIER=db-f1-micro
export RUN_MEMORY=1Gi MIN_INSTANCES=1
export GCP_STATE="$HOME/.local/state/$PROJECT"
export CONTEXT="$GCP_STATE/context"
python deploy/gcp/fixture.py secrets --state "$GCP_STATE"
printf '%s\n' "$PROJECT" > "$GCP_STATE/project-id"
chmod 600 "$GCP_STATE/project-id"
```

Keep the non-secret project/region/prefix choices in your own private session notes
so teardown can target precisely the same project. The project ID is globally unique;
if creation fails because it is taken, choose a new ID **before any provisioning**.
Resource names are `$PREFIX-mcp`, `$PREFIX-pg`, `$PREFIX-images`, `$PREFIX-runtime`,
`$PREFIX-warehouse` and `$PREFIX-token`; optional SSH secrets are `$PREFIX-git-key`
and `$PREFIX-git-hosts`.

Cloud SQL is a continuously billed, zonal Enterprise instance with 10 GiB SSD and
no automatic backups/PITR/final backup: **synthetic disposable data only**. Choose a
larger `SQL_TIER` if the region does not support `db-f1-micro`. Cloud Run defaults to
one warm CPU instance, 1 GiB memory, max one instance and four concurrent requests.
Always-allocated CPU supports the 60-second Git refresh timer while idle. Setting
`MIN_INSTANCES=0` reduces idle compute but permits scale-to-zero, cold starts and no
polling while stopped. SQL/storage/registry/secrets/logging charges remain. Prices
vary by region: [SQL](https://cloud.google.com/sql/pricing),
[Run](https://cloud.google.com/run/pricing),
[minimum instances](https://docs.cloud.google.com/run/docs/configuring/min-instances),
[CPU allocation](https://docs.cloud.google.com/run/docs/configuring/cpu-allocation).
Budgets are alerts, not a hard spending cap. Delete the rehearsal promptly.

## Provision once per disposable project — UNTESTED, billable

For an individually owned new project, the creator needs project creation/deletion,
service enablement, IAM and service-account management, Cloud SQL administration,
Artifact Registry administration, Secret Manager administration and Cloud Run
administration (including permission to act as the runtime account). Billing linkage
also needs billing-account access. An organization administrator can grant the
corresponding roles; runtime permissions below are much narrower.

```bash
gcloud billing accounts list --filter=open=true --format='table(name,displayName)'
# Substitute the chosen billing account ID locally; it is not a credential.
export BILLING_ACCOUNT=REPLACE_WITH_BILLING_ACCOUNT_ID
gcloud projects create "$PROJECT" --name="unshackle synthetic rehearsal"
gcloud billing projects link "$PROJECT" --billing-account "$BILLING_ACCOUNT"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  sqladmin.googleapis.com secretmanager.googleapis.com iam.googleapis.com \
  --project "$PROJECT"
# ADC alternative only: gcloud auth application-default set-quota-project "$PROJECT"
gcloud artifacts repositories create "$PREFIX-images" --project "$PROJECT" \
  --location "$REGION" --repository-format docker
gcloud iam service-accounts create "$PREFIX-runtime" --project "$PROJECT"
export RUNTIME_ACCOUNT="$PREFIX-runtime@$PROJECT.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$RUNTIME_ACCOUNT" --role=roles/cloudsql.client --condition=None

gcloud sql instances create "$PREFIX-pg" --project "$PROJECT" --region "$REGION" \
  --database-version POSTGRES_16 --edition ENTERPRISE --tier "$SQL_TIER" \
  --availability-type ZONAL --storage-type SSD --storage-size 10 \
  --no-storage-auto-increase --assign-ip --connector-enforcement REQUIRED \
  --no-backup --no-final-backup \
  --no-retain-backups-on-delete --no-deletion-protection
gcloud sql users set-password postgres --instance "$PREFIX-pg" --project "$PROJECT" \
  --flags-file "$GCP_STATE/admin.json"
gcloud sql databases create example --instance "$PREFIX-pg" --project "$PROJECT"
```

References: [billing link](https://docs.cloud.google.com/sdk/gcloud/reference/billing/projects/link),
[SQL instance flags](https://docs.cloud.google.com/sdk/gcloud/reference/sql/instances/create),
[password command](https://docs.cloud.google.com/sdk/gcloud/reference/sql/users/set-password),
[restricted flags files](https://docs.cloud.google.com/sdk/gcloud/reference/topic/flags-file).
The admin password travels in a mode-0600 JSON flags file, never command arguments.
The [CLI file-logging setting](https://docs.cloud.google.com/sdk/gcloud/reference/topic/configurations)
above also prevents parsed flags from reaching local gcloud log files.
The instance has a public address for the authenticated proxy path; there is no
`0.0.0.0/0` authorized network. Cloud Run uses its service identity and the managed
Cloud SQL socket, avoiding VPC connectors/NAT. See [Cloud Run to SQL](https://docs.cloud.google.com/sql/docs/postgres/connect-run).

## Seed synthetic Postgres and generate context

The local proxy binds only loopback. Start it in this shell and retain its PID:

```bash
cloud-sql-proxy --gcloud-auth --address 127.0.0.1 --port 15432 "$PROJECT:$REGION:$PREFIX-pg" \
  > "$GCP_STATE/proxy.log" 2>&1 &
PROXY_PID=$!
export PROXY_PID
# Wait until proxy.log says it is ready; if it exited, resolve that error first.
python deploy/gcp/fixture.py seed --state "$GCP_STATE" --output "$CONTEXT" \
  --connection-name "$PROJECT:$REGION:$PREFIX-pg"
kill "$PROXY_PID"
wait "$PROXY_PID" || true
unset PROXY_PID
```

If seeding fails, stop the proxy before troubleshooting/retrying. The seed runs only
against a fresh `example` database; do not rerun it to update an existing fixture.
It reuses [`examples/mcp-demo`](../../examples/mcp-demo/README.md), grants
`example_reader` SELECT only, revokes public CREATE/TEMP, denies DELETE even after
turning off the role's default read-only setting, and checks psycopg's native
cancellation with `pg_sleep` followed by a successful query. `postgresql+psycopg`
is required; a generic/unsupported driver must not silently lose timeout support.
Only the reader connection string reaches Cloud Run. Admin/reader passwords and
the bearer stay in the mode-0700 state directory, outside context and Docker inputs.

Create a separate synthetic GitHub repository. Replace only `REPLACE_WITH_OWNER`:

```bash
export CONTEXT_GITHUB=REPLACE_WITH_OWNER/unshackle-gcp-demo
export REPO_REF=main
git -C "$CONTEXT" init -b main
git -C "$CONTEXT" add .
git -C "$CONTEXT" diff --cached --stat
# Inspect staged files: generated context only, no state/passwords/connection URLs.
git -C "$CONTEXT" commit -s -m 'Add synthetic MCP context'
gh repo create "$CONTEXT_GITHUB" --public --source "$CONTEXT" --remote origin --push
export REPO_URL="https://github.com/$CONTEXT_GITHUB.git"
# This checks the same noninteractive public fetch/ref and context validation as runtime.
python - <<'PY'
import os
from unshackle.mcp.sync import GitRefresh
r = GitRefresh(os.environ['REPO_URL'], os.environ['REPO_REF'])
try:
    assert r.refresh()
    print('Git URL, ref and generated context validated')
finally:
    r.close()
PY
```

An exact commit SHA or tag also works; a pinned SHA never advances on polling. Check
that the generated repository, not the unshackle source repository, is configured.
No GitHub repository is created by the preparation worker.

## Store secrets, build and deploy — UNTESTED

```bash
gcloud secrets create "$PREFIX-warehouse" --project "$PROJECT" \
  --replication-policy automatic --data-file "$GCP_STATE/warehouse-url"
gcloud secrets create "$PREFIX-token" --project "$PROJECT" \
  --replication-policy automatic --data-file "$GCP_STATE/token"
for secret in "$PREFIX-warehouse" "$PREFIX-token"; do
  gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
    --member="serviceAccount:$RUNTIME_ACCOUNT" --role=roles/secretmanager.secretAccessor
 done
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
export IMAGE_TAG="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
bash deploy/gcp/deploy.sh
export UNSHACKLE_MCP_URL="$(gcloud run services describe "$PREFIX-mcp" \
  --project "$PROJECT" --region "$REGION" --format='value(status.url)')/mcp"
export UNSHACKLE_MCP_TOKEN="$(cat "$GCP_STATE/token")"
python deploy/gcp/verify.py
```

The helper builds the existing root Dockerfile for `linux/amd64`, adds only the
optional SSH startup wrapper, pushes the image and deploys it. Secret references
pin numeric versions (initially 1). Cloud Run resolves the image tag to a digest
for the revision; save that digest when recording evidence. The service disables
Google's invoker IAM check so standard MCP clients can send the application's
bearer. **Every application HTTP route still requires that bearer.** A missing or
wrong token must produce application 401, while the correct token must complete
MCP initialization. Organization policy can forbid this public mode; that is an
administrator prerequisite, not a reason to remove application authentication.
References: [Artifact Registry auth](https://docs.cloud.google.com/artifact-registry/docs/docker/authentication),
[Cloud Run deploy flags](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy),
[public access](https://docs.cloud.google.com/run/docs/securing/managing-access),
[secret versions and mounts](https://docs.cloud.google.com/run/docs/configuring/services/secrets).

The image runs as UID 10001. Cloud Run's writable filesystem is ephemeral and
counts toward memory; this does not claim Compose's read-only-root restriction.
Retained Git snapshots consume memory until revision restart. Increase `RUN_MEMORY`
for large context or periodically redeploy. Do not bake passwords/SSH keys into images.

## Optional private Git — SSH deploy key only

For a private synthetic context, use `--private` in the repository-creation step and
skip the public preflight. Runtime intentionally disables HTTPS credential helpers,
askpass and interactive prompts; embedded HTTPS tokens are rejected. Use a separate
read-only deploy key. Generate an unencrypted key for unattended runtime, protected
by Secret Manager; never use your personal SSH key:

```bash
ssh-keygen -q -t ed25519 -N '' -C unshackle-gcp-demo -f "$GCP_STATE/git-key"
gh repo deploy-key add "$GCP_STATE/git-key.pub" --repo "$CONTEXT_GITHUB" --title unshackle-gcp-demo
# Get GitHub's published host keys over HTTPS, not an unverified ssh-keyscan.
gh api meta --jq '.ssh_keys[] | "github.com " + .' > "$GCP_STATE/known_hosts"
chmod 600 "$GCP_STATE/known_hosts"
export REPO_URL="ssh://git@github.com/$CONTEXT_GITHUB.git"
GIT_SSH_COMMAND="ssh -i $GCP_STATE/git-key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$GCP_STATE/known_hosts" \
  git ls-remote --exit-code "$REPO_URL" refs/heads/main
for pair in git-key:git-key git-hosts:known_hosts; do
  gcloud secrets create "$PREFIX-${pair%%:*}" --project "$PROJECT" \
    --replication-policy automatic --data-file "$GCP_STATE/${pair#*:}"
  gcloud secrets add-iam-policy-binding "$PREFIX-${pair%%:*}" --project "$PROJECT" \
    --member="serviceAccount:$RUNTIME_ACCOUNT" --role=roles/secretmanager.secretAccessor
done
bash deploy/gcp/deploy.sh
```

`ls-remote` above checks `main`; substitute your branch/tag if changing `REPO_REF`.
The wrapper copies root-owned secret mounts to the runtime user's `.ssh`, chmods
the key 0600 and requires strict host checking. Private fetch must succeed at startup
and again on refresh before claiming acceptance. If using a non-GitHub host, supply
its independently verified `known_hosts` entry and deploy-key configuration.
See [GitHub deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
and [GitHub metadata API](https://docs.github.com/en/rest/meta/meta#get-github-meta-information).

## Validate and connect clients

`UNSHACKLE_MCP_URL` is the public HTTPS URL ending `/mcp`;
`UNSHACKLE_MCP_TOKEN` contains the bearer loaded above. To reload it on another
operator shell without displaying it:

```bash
export UNSHACKLE_MCP_TOKEN="$(gcloud secrets versions access 1 \
  --secret "$PREFIX-token" --project "$PROJECT")"
python deploy/gcp/verify.py
```

The SDK probe requires missing/wrong bearer 401s, negotiated Streamable HTTP
initialization, four context/query tools, resource listing/reading, hidden `.env`,
SQL write rejection, a two-row cap, and all five answers. It then runs `pg_sleep(45)`:
the default 30-second query deadline must fail before 42 seconds and `SELECT 1`
must succeed afterward. This takes roughly 30 seconds plus connection overhead.
Direct database DELETE denial is checked during seeding, independently of SQL parsing.

| Question | Hand-derived answer |
| --- | --- |
| Recognized revenue | 100 + 50 + 80 + 120 = 350 USD |
| By region | North 100 + 50 + 120 = 270; South 80 |
| Top customer | Acme Paper 100 + 50 = 150 |
| By month | January 100 + 50 + 80 = 230; February 120 |
| Average paid order | 350 / 4 = 87.5 |

Refunded 40 and cancelled 30 are excluded. The verifier reads the existing
[`questions.json`](../../examples/mcp-demo/questions.json); expected answers are
not placed in the generated context or client prompt.

Follow [exact Claude Code, Codex and Cursor configurations](README.md#connect-clients)
and [the existing real-client harness/evidence procedure](README.md#record-hosted-acceptance).
A passing SDK probe does not count as three hosted client runs. Client sign-in is a
separate one-time step, and model calls can consume a subscription. Keep raw
transcripts/configurations private. Record source SHA, context SHA, image digest,
revision, CLI/client versions and actual answers without publishing credentials.

## Logs, update and troubleshooting — cloud commands UNTESTED

```bash
gcloud run services logs read "$PREFIX-mcp" --project "$PROJECT" --region "$REGION" --limit 80
gcloud run revisions list --service "$PREFIX-mcp" --project "$PROJECT" --region "$REGION"
gcloud sql operations list --instance "$PREFIX-pg" --project "$PROJECT" --limit 10
```

[Cloud Run logs command](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/logs/read).
Startup failure: check the Git URL/ref, private key/host mounts and secret-accessor
bindings. IAM 403 before application 401: inspect public-invoker policy. Warehouse
failure: check Cloud SQL Client role, instance connection name, proxy readiness,
reader secret version and `postgresql+psycopg` URL. Proxy credentials: use `--gcloud-auth` with the authenticated CLI, or perform
the operator ADC login for the ADC alternative; never pass a token in arguments. No timer refresh: check warm
instance/CPU settings; verify a new context commit by reading its changed text.
Do not dump environment variables, secrets, HTTP headers or raw driver exceptions.

For context updates, commit/push the generated context and wait at least 60 seconds;
read the changed file over MCP. A pinned ref needs a deliberate `REPO_REF` change.
For runtime updates, check out the reviewed source commit, set a new `IMAGE_TAG` and
rerun `deploy.sh`, then the verifier and real clients. For bearer rotation:

```bash
python - <<'PY'
import os, secrets
from pathlib import Path
p = Path(os.environ['GCP_STATE']) / 'token-next'
with p.open('x', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
    f.write(secrets.token_urlsafe(32))
PY
gcloud secrets versions add "$PREFIX-token" --project "$PROJECT" --data-file "$GCP_STATE/token-next"
# Set TOKEN_VERSION to the new numeric version reported by the command.
export TOKEN_VERSION=2
export IMAGE_TAG="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
bash deploy/gcp/deploy.sh
export UNSHACKLE_MCP_TOKEN="$(cat "$GCP_STATE/token-next")"
python deploy/gcp/verify.py
```

Update client environments too. After success, disable the old secret version with
`gcloud secrets versions disable 1 --secret "$PREFIX-token" --project "$PROJECT"`.
Existing old revisions may retain their loaded token; remove obsolete revisions once
rollback is no longer required. Database credential rotation also requires changing
the Postgres role password, adding a matching warehouse secret version and setting
`WAREHOUSE_VERSION` before redeployment; changing Secret Manager alone is insufficient.

## Scoped teardown — UNTESTED, destructive

These commands delete only this rehearsal's named resources. Stop any proxy started
by this session, remove any configured client entry, then review the saved project
ID against the intended disposable project. Do not use an all-project/account loop.

```bash
: "${PROJECT:?}" "${REGION:?}" "${PREFIX:?}" "${GCP_STATE:?}"
test "$(cat "$GCP_STATE/project-id")" = "$PROJECT"
gcloud projects describe "$PROJECT" --format='table(projectId,name,lifecycleState)'
# Verify this is the disposable rehearsal before executing the following lines.
gcloud run services delete "$PREFIX-mcp" --project "$PROJECT" --region "$REGION" --quiet
gcloud sql instances delete "$PREFIX-pg" --project "$PROJECT" --quiet
gcloud artifacts repositories delete "$PREFIX-images" --project "$PROJECT" --location "$REGION" --quiet
for secret in "$PREFIX-warehouse" "$PREFIX-token"; do
  gcloud secrets delete "$secret" --project "$PROJECT" --quiet
done
# Only if the optional SSH secrets were created:
# gcloud secrets delete "$PREFIX-git-key" --project "$PROJECT" --quiet
# gcloud secrets delete "$PREFIX-git-hosts" --project "$PROJECT" --quiet
gcloud projects remove-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$RUNTIME_ACCOUNT" --role=roles/cloudsql.client --condition=None
gcloud iam service-accounts delete "$RUNTIME_ACCOUNT" --project "$PROJECT" --quiet
# Includes project-created service identities, default networking, logs and enabled APIs.
gcloud projects delete "$PROJECT" --quiet
gcloud projects describe "$PROJECT" --format='value(lifecycleState)'
```

Service deletion covers its revisions. Registry deletion covers all images in that
repository. SQL deletion covers its database/roles; this recipe explicitly disabled
backups, final backups and retained backups. If you changed those flags, inspect and
delete this instance's retained backups too before closing billing review. Project
shutdown is asynchronous (expect `DELETE_REQUESTED`), not proof of immediate zero
charges; inspect final billing for the specific project. See
[SQL deletion](https://docs.cloud.google.com/sdk/gcloud/reference/sql/instances/delete)
and [project deletion](https://docs.cloud.google.com/sdk/gcloud/reference/projects/delete).
If a resource is already absent after partial cleanup, continue only for the same
recorded project. Never delete the billing account or other projects.

The newly created synthetic GitHub repository is also owned by this rehearsal:
`gh repo delete "$CONTEXT_GITHUB" --yes` deletes it and its deploy key (may need
GitHub's `delete_repo` scope). If keeping that repository, revoke its deploy key
using `gh repo deploy-key list --repo "$CONTEXT_GITHUB"` and
`gh repo deploy-key delete KEY_ID --repo "$CONTEXT_GITHUB"` instead, and record it
as intentionally retained. Remove only this session's Docker images with
`docker image rm "$REGION-docker.pkg.dev/$PROJECT/$PREFIX-images/mcp:$IMAGE_TAG" unshackle-gcp-base`;
do not prune unrelated images/build caches. Remove any other tags built by updates
by their recorded exact names. No local Postgres container is created by the cloud
recipe; the opt-in test cleans its own container in `finally`.

Finally remove the exact private state directory (after preserving sanitized
evidence) and unset the token:

```bash
python - <<'PY'
import os, shutil
from pathlib import Path
p = Path(os.environ['GCP_STATE']).resolve()
assert p == Path.home() / '.local/state' / os.environ['PROJECT']
assert (p / 'project-id').read_text().strip() == os.environ['PROJECT']
shutil.rmtree(p)
PY
unset UNSHACKLE_MCP_TOKEN
```

Delete temporary client configurations and the provider's Docker credential-helper
entry only if it was added for this rehearsal; retain other registry entries.
Keep existing Google/GitHub logins and tools if they predated the rehearsal. If login
was created solely for this exercise, revoke that exact account using
`gcloud auth revoke YOUR_ACCOUNT_EMAIL` and `gcloud auth application-default revoke`;
ADC revocation affects other local programs using that ADC identity. Do not remove
someone else's shared credentials or Docker configuration.
