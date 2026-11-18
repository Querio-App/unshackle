# Google Cloud Run

Prerequisites: authenticated gcloud, a project with billing/Artifact Registry/Cloud Run
and Secret Manager enabled, an Artifact Registry Docker repository, and two Secret
Manager secrets containing the warehouse URL and bearer token. Grant the runtime
service account `roles/secretmanager.secretAccessor` on those two secrets. Supply
warehouse network access (VPC connector/direct VPC egress when private).

Set these non-secret shell variables for your environment:

```sh
PROJECT=your-project
REGION=europe-west1
ARTIFACT_REPO=containers
RUNTIME_ACCOUNT=unshackle-runtime
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$ARTIFACT_REPO/unshackle-mcp:latest"
gcloud auth configure-docker "$REGION-docker.pkg.dev"
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
gcloud run deploy unshackle-mcp --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --port 8000 --memory 1Gi --cpu 1 \
  --service-account "$RUNTIME_ACCOUNT@$PROJECT.iam.gserviceaccount.com" \
  --set-secrets 'UNSHACKLE_WAREHOUSE_URL=unshackle-warehouse-url:latest,UNSHACKLE_MCP_TOKEN=unshackle-mcp-token:latest' \
  --set-env-vars 'UNSHACKLE_REPO_URL=https://github.com/example/analytics-context.git,UNSHACKLE_REPO_REF=main,UNSHACKLE_POLL_INTERVAL=60' \
  --min-instances 1 --max-instances 1 --no-cpu-throttling --timeout 120 \
  --allow-unauthenticated
```

`--allow-unauthenticated` removes Google's IAM login requirement so standard MCP clients
can reach the endpoint; the application still requires its bearer token on every HTTP
request. If organization policy forbids public services, use an approved identity-aware
proxy and verify client compatibility. Keep TLS enabled on the public service URL.

Instance-based CPU allocation and a minimum instance allow timer refresh while idle.
These settings incur ongoing cost. Cloud Run's writable filesystem is ephemeral and
counts against memory; budget retained snapshots and restart revisions periodically.
The image runs non-root, but Cloud Run does not provide the Compose read-only-root
setting. Use the ECS guide if that runtime control is required.

Use the service URL plus `/mcp`, test 401 without the token, then initialize a client
and query the read-only warehouse. Confirm a Git commit becomes visible after polling.
These commands are provided as a recipe; no Cloud Run deployment was executed here.

References: [deploy containers](https://docs.cloud.google.com/run/docs/deploying),
[minimum instances](https://docs.cloud.google.com/run/docs/configuring/min-instances),
[CPU allocation](https://docs.cloud.google.com/run/docs/configuring/cpu-allocation).
