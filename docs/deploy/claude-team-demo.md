# Connect Claude to the retained team demo

First deploy the [Dunder Mifflin demo](team-demo.md). Keep its HTTPS `/mcp` URL
and private bearer-token file. The service stays running until its owner shuts it
down; only fictional data is available. Never put the token in the URL or a chat.

## Claude web or Desktop: remote connector

Open **Customize > Connectors > Add > Add custom connector** and enter the URL.
For Team/Enterprise, an Owner adds it through **Organization settings > Connectors
> Add > Custom > Web**, then members connect and enable it in their chats.

Choose **No sign-in**, then add a **required** `authorization` request header whose
value is `Bearer ` followed by the token. The prefix and space are required.
“No sign-in” disables OAuth, not this server's bearer authentication. Request
headers may be under Advanced. Enable the connector through **+ > Connectors**
in the conversation.

**Availability matters:** request-header authentication is a limited beta. If the
field is absent, this bearer-only server cannot be connected directly through that
account's web connector. Use the Desktop bridge below or Claude Code. Do not remove
server authentication to make the URL work. A web-only user without the beta needs
beta access or a separately designed OAuth gateway; this demo does not provide one.
See [Anthropic's current connector instructions](https://claude.com/docs/connectors/custom/add-unlisted),
including the beta qualification. Actual UI availability can differ by organization.

## Desktop fallback: local bridge to hosted HTTP

This uses Desktop's local MCP configuration and does not enable claude.ai web or
cloud Cowork. Your organization must allow local servers. Node.js is required.
[mcp-remote](https://github.com/punkpeye/mcp-remote) bridges stdio to authenticated
HTTP. The commands pin a bridge version and keep credentials out
of process arguments and the Desktop configuration.

Run from a shell with Node and npm available. Replace the two illustrative paths:

```sh
set +x
umask 077
export UNSHACKLE_MCP_URL='https://your-demo.example/mcp'
export MCP_TOKEN_FILE='/absolute/private/path/mcp-token'
export DEMO_CLIENT_DIR="$HOME/.local/state/unshackle/dunder-mifflin-team-demo"
mkdir -p "$DEMO_CLIENT_DIR"
chmod 700 "$DEMO_CLIENT_DIR"
npm install --prefix "$DEMO_CLIENT_DIR/desktop-bridge" --ignore-scripts \
  --omit=dev --no-audit --no-fund mcp-remote@0.16.7
export DEMO_NODE="$(node -p 'process.execPath')"
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['DEMO_CLIENT_DIR']).resolve()
token = Path(os.environ['MCP_TOKEN_FILE']).read_text().strip()
if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
    raise SystemExit('Invalid token file')
headers = root / 'mcp-headers.txt'
with os.fdopen(os.open(headers, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
    f.write('Authorization: Bearer ' + token + '\n')
headers.chmod(0o600)
fragment = {'mcpServers': {'dunder-mifflin': {
    'command': os.environ['DEMO_NODE'],
    'args': [str(root / 'desktop-bridge/node_modules/mcp-remote/dist/proxy.js'),
             os.environ['UNSHACKLE_MCP_URL'], '--header-file', str(headers),
             '--transport', 'http-only', '--silent']}}}
output = root / 'claude-desktop-fragment.json'
output.write_text(json.dumps(fragment, indent=2) + '\n')
output.chmod(0o600)
print('Config fragment:', output)
PY
```

In Claude Desktop, open **Settings > Developer > Edit Config**. Merge the
`dunder-mifflin` entry from that fragment into the existing `mcpServers` object;
preserve other servers/settings. On macOS the configuration is
`~/Library/Application Support/Claude/claude_desktop_config.json`.
Use a different entry name if it already exists. Fully quit and reopen Desktop,
then enable the tools for your conversation. The fragment itself has no token.

Keep `--silent` and do not enable debug logs with real credentials. If connecting
fails, first check the URL and private file exist, Node's absolute path is still
valid, and the hosted service responds to the authenticated provider probe. A
successful bridge/SDK check does not by itself prove a Desktop conversation worked.

## Claude Code

Use a separate client folder; keep this configuration free of literal secrets:

```json
{"mcpServers":{"dunder-mifflin":{"type":"http","url":"${UNSHACKLE_MCP_URL}","headers":{"Authorization":"Bearer ${UNSHACKLE_MCP_TOKEN}"}}}}
```

Save it as `dunder-mifflin.mcp.json`, then start from the shell holding the URL and
private token path:

```sh
set +x
export UNSHACKLE_MCP_TOKEN="$(cat "$MCP_TOKEN_FILE")"
claude --strict-mcp-config --mcp-config ./dunder-mifflin.mcp.json
```

Use `/mcp` to inspect connectivity. Approve the context and query tools when needed.
[Claude Code documents environment expansion](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson).
The [demo guide](team-demo.md) includes exploratory prompts and expected fixture answers.

## Share and revoke access

One server token means one shared access level, with no per-person identity or
revocation. Team owners can configure the shared connector once; individual
accounts or Desktop bridges need the token delivered through a private channel.
Do not send the warehouse URL, administrator credentials or the whole deployment
state directory. Nobody needs database login details to use MCP.

Rotate the server token using the demo guide, update private header files and client
configuration, and re-add remote connectors with the new header value. This revokes
the old token for everyone. Removing one local connector only removes that client's
setup; it does not revoke copies of its credential. Removing all demo services and
storage is a separate owner action described in the deployment guide.
