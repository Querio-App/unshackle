#!/usr/bin/env bash
# Run from the source checkout. Mutates GCP only when explicitly invoked.
set -eu
umask 077
export CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true
: "${PROJECT:?}" "${REGION:?}" "${PREFIX:?}" "${GCP_STATE:?}" "${REPO_URL:?}" "${REPO_REF:?}" "${IMAGE_TAG:?}"
export PROJECT REGION PREFIX GCP_STATE REPO_URL REPO_REF
python - <<'PY'
import json, os, re
from pathlib import Path
from urllib.parse import urlsplit
p = urlsplit(os.environ['REPO_URL'])
assert p.scheme in {'https', 'ssh'} and p.hostname, 'Use an HTTPS or ssh:// Git URL'
assert not p.password and not p.query and not p.fragment, 'Never embed Git credentials'
assert p.scheme != 'https' or not p.username, 'HTTPS credentials are unsupported; use a read-only SSH deploy key'
assert re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', os.environ['PROJECT']), 'Invalid project ID'
assert re.fullmatch(r'[a-z][a-z0-9-]{1,18}', os.environ['PREFIX']), 'Use a short resource prefix'
ref = os.environ['REPO_REF']
assert ref and not ref.startswith('-') and not any(c in ref for c in '\n\r\0'), 'Invalid Git ref'
state = Path(os.environ['GCP_STATE'])
assert state.is_dir(), 'Create private state first'
(state / 'run-env.json').write_text(json.dumps({'UNSHACKLE_REPO_URL': os.environ['REPO_URL'], 'UNSHACKLE_REPO_REF': ref, 'UNSHACKLE_POLL_INTERVAL': '60'}))
PY
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$PREFIX-images/mcp:$IMAGE_TAG"
SECRETS="UNSHACKLE_WAREHOUSE_URL=$PREFIX-warehouse:${WAREHOUSE_VERSION:-1},UNSHACKLE_MCP_TOKEN=$PREFIX-token:${TOKEN_VERSION:-1}"
case "$REPO_URL" in
  ssh://*) SECRETS="$SECRETS,/var/run/git-key/key=$PREFIX-git-key:${GIT_VERSION:-1},/var/run/git-hosts/known_hosts=$PREFIX-git-hosts:${GIT_HOSTS_VERSION:-1}" ;;
esac
docker buildx build --platform linux/amd64 --load -t unshackle-gcp-base .
docker buildx build --platform linux/amd64 --load -t "$IMAGE" deploy/gcp
docker push "$IMAGE"
gcloud run deploy "$PREFIX-mcp" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --port 8000 --execution-environment gen2 \
  --service-account "$PREFIX-runtime@$PROJECT.iam.gserviceaccount.com" \
  --add-cloudsql-instances "$PROJECT:$REGION:$PREFIX-pg" \
  --set-secrets "$SECRETS" --env-vars-file "$GCP_STATE/run-env.json" \
  --memory "${RUN_MEMORY:-1Gi}" --cpu 1 --concurrency 4 \
  --min-instances "${MIN_INSTANCES:-1}" --max-instances 1 \
  --no-cpu-throttling --timeout 120 --ingress all --no-invoker-iam-check
gcloud run services describe "$PREFIX-mcp" --project "$PROJECT" --region "$REGION" --format='value(status.url)'
