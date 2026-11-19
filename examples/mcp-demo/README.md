# Try MCP with fictional data

Three fictional customers and six orders demonstrate warehouse, dbt and SQL/Python
imports in one context repo. `questions.json` contains five questions, reference SQL,
and hand-computed results. Paid revenue is 350 USD; refunds and cancellations are
excluded. The Python input deliberately raises if executed and includes one dynamic
query that must remain TODO.

Use Python 3.11+, Docker, and `uv pip install -e '.[dev]'` from the source checkout:

```sh
python examples/mcp-demo/local.py --output /tmp/example-context --keep
unshackle mcp --repo /tmp/example-context
```

This creates a fresh `postgres:16-alpine` container on a random localhost port and
an `example_reader` role with SELECT privileges and read-only transactions. Random
credentials are stored only in the context repo's ignored `.env`, mode 0600. The
script prints the exact cleanup command; it does not print credentials. Omit
`--keep` for setup smoke testing with immediate container cleanup.

Run the automated offline acceptance tests:

```sh
pytest tests/test_mcp_acceptance.py
```

For actual Postgres protocol and database permission tests, load the generated
`.env` into your local shell, set `UNSHACKLE_ACCEPTANCE_POSTGRES_URL` to its warehouse
URL, then run `pytest tests/test_mcp_acceptance.py::test_real_postgres_import_role_and_stdio`.
Never commit the generated credentials or raw client transcripts.

Real client invocations consume the user's authenticated model service. Each uses
temporary MCP configuration and an empty workspace, receives only the questions,
and must use MCP context and `run_query`; expected answers are withheld:

```sh
python examples/mcp-demo/client_matrix.py claude --repo /tmp/example-context \
  --executable "$PWD/.venv/bin/unshackle" --output /tmp/example-claude-evidence
python examples/mcp-demo/client_matrix.py codex --repo /tmp/example-context \
  --executable "$PWD/.venv/bin/unshackle" --output /tmp/example-codex-evidence
python examples/mcp-demo/client_matrix.py cursor --repo /tmp/example-context \
  --executable "$PWD/.venv/bin/unshackle" --output /tmp/example-cursor-evidence
```

The harness records version, source commit, prompt, transcript, exit status and
transport. Review actual MCP tool calls and compare final answers against all five
hand-derived results; a successful exit alone is not acceptance. Authentication
remains with each installed client. Codex ignores user config; Cursor uses a
[temporary config directory](https://cursor.com/docs/cli/reference/configuration);
Claude loads only the explicit MCP configuration and no built-in tools.

For an already running local HTTP server, set `UNSHACKLE_MCP_TOKEN` in the harness
environment and add `--http-url http://127.0.0.1:8765/mcp` to each client invocation.
The token is written only to temporary private MCP configuration (Claude/Cursor)
or referenced through its environment variable (Codex). The harness deletes those
configurations afterward. HTTP evidence records whether the endpoint was local;
never label a loopback run as hosted deployment acceptance.

Next: [use your own context locally](../../docs/mcp.md) or
[share through HTTP](../../docs/deploy/README.md).
