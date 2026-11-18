# Railway

Deploy the unshackle source image, and configure a separate Git repository containing
migrated context. Install/sign in to the Railway CLI, then from the source checkout:

```sh
railway init
railway link
# Use the dashboard's Variables tab for the two secret values:
# UNSHACKLE_WAREHOUSE_URL, UNSHACKLE_MCP_TOKEN
railway variable set 'UNSHACKLE_REPO_URL=https://github.com/example/analytics-context.git'
railway variable set 'UNSHACKLE_REPO_REF=main'
railway variable set 'UNSHACKLE_POLL_INTERVAL=60'
railway up
railway domain
```

Railway detects the root Dockerfile. Set a public domain targeting port 8000 (or the
platform-provided PORT). Keep the service running rather than enabling idle sleep if
Git refresh must continue without incoming requests. Apply staged variable changes
and redeploy. Set a memory budget appropriate for your queries and temporary snapshots.

The warehouse must be reachable from the deployment and use a read-only role; use the
provider's private network when available. Connect clients to
`https://<your-domain>/mcp` with the bearer header. A request without it must return 401:

```sh
curl -i "https://YOUR_DOMAIN/mcp"
curl --fail-with-body "https://YOUR_DOMAIN/mcp" \
  -H "Authorization: Bearer ${UNSHACKLE_MCP_TOKEN}" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The image runs as a non-root user. Railway manages TLS and the container filesystem;
for strict read-only root filesystems use the ECS/Compose configurations. Do not place
secrets in railway config or image layers. Validate actual queries and ref refresh after
deploying; an HTTP health response alone is not acceptance.

References: [Railway CLI deploys](https://docs.railway.com/cli/deploying),
[variable commands](https://docs.railway.com/cli/variable), [domains and up](https://docs.railway.com/cli/up).
