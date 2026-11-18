"""Streamable HTTP with deployment bearer authentication; no OAuth or user identity."""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Callable

from starlette.responses import JSONResponse

from .server import create_server
from .sync import GitRefresh


def _token() -> str:
    token = os.environ.get('UNSHACKLE_MCP_TOKEN', '')
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('Set UNSHACKLE_MCP_TOKEN to a random ASCII token of at least 32 characters')
    return token


class BearerAuth:
    def __init__(self, app, token: str):
        self.app = app
        self.expected = ('Bearer ' + token).encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = [value for name, value in scope.get('headers', []) if name.lower() == b'authorization']
            if len(headers) != 1 or not hmac.compare_digest(headers[0], self.expected):
                await JSONResponse({'error': 'Unauthorized'}, status_code=401,
                                   headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})(scope, receive, send)
                return
        elif scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        await self.app(scope, receive, send)


def create_app(repo: Path | Callable[[], Path], *, host: str = '127.0.0.1', max_rows: int = 10000,
               timeout: float = 30):
    token = _token()
    server = create_server(repo, max_rows=max_rows, timeout=timeout, host=host,
                           stateless_http=True, json_response=True)
    app = server.streamable_http_app()
    app.add_middleware(BearerAuth, token=token)
    return app


def serve(repo: Path, *, host: str = '127.0.0.1', port: int = 8000, max_rows: int = 10000,
          timeout: float = 30, repo_url: str | None = None, repo_ref: str = 'main',
          poll_interval: float = 60) -> None:
    import uvicorn
    _token()  # Fail before network access if authentication is not configured.
    refresh = GitRefresh(repo_url, repo_ref, poll_interval) if repo_url else None
    try:
        if refresh:
            refresh.start()
        app = create_app(refresh.current if refresh else repo, host=host, max_rows=max_rows, timeout=timeout)
        # Access logging is disabled: URLs/query strings could contain accidental secrets.
        uvicorn.run(app, host=host, port=port, access_log=False)
    finally:
        if refresh:
            refresh.close()
