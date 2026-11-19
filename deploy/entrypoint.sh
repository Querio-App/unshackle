#!/bin/sh
set -eu
exec unshackle mcp --transport http --host 0.0.0.0 \
  --port "${PORT:-8000}" --repo "${UNSHACKLE_REPO_PATH:-/repo}" "$@"
