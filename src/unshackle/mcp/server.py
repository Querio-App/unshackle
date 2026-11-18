"""MCP factory shared by stdio and HTTP; stdout belongs to the protocol."""
from __future__ import annotations

import json
import math
from functools import partial
import anyio
from pathlib import Path
from typing import Callable, Any
from urllib.parse import quote, unquote, urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.types import Resource, ToolAnnotations
from mcp.server.lowlevel.helper_types import ReadResourceContents

from .query import DEFAULT_LIMIT, DEFAULT_TIMEOUT, MAX_LIMIT, run_query as execute
from .repository import Repository


def create_server(repo: Path | Callable[[], Path], *, max_rows: int = MAX_LIMIT,
                  timeout: float = DEFAULT_TIMEOUT, **settings) -> FastMCP:
    if not 1 <= max_rows <= MAX_LIMIT or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Invalid MCP query limits')
    def repository():
        return Repository(repo() if callable(repo) else repo)
    repository()  # Reject unsafe configuration before starting a transport.

    class Server(FastMCP):
        async def list_resources(self):
            return [Resource(uri='unshackle://repo/' + quote(p, safe='/'), name=p, mimeType='application/json')
                    for p in repository().files()]

        async def read_resource(self, uri):
            parsed = urlsplit(str(uri))
            if parsed.scheme != 'unshackle' or parsed.netloc != 'repo' or parsed.query or parsed.fragment:
                raise ValueError('Unknown repository resource')
            content = repository().read(unquote(parsed.path.removeprefix('/')))
            return [ReadResourceContents(content=json.dumps(content), mime_type='application/json')]

    server = Server('unshackle', instructions='Read repository context; TODO(unshackle) marks incomplete conversion. '
                    'Queries use one read-only warehouse role. Policies are context only.', **settings)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def overview() -> dict[str, Any]:
        """Summarize models, metrics, notebooks, tables, TODOs and warehouse configuration."""
        return repository().overview()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def search(query: str) -> list[dict[str, Any]]:
        """Search allowed context files for literal text; return paths, snippets and TODO flags."""
        return repository().search(query)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def read(path: str) -> dict[str, Any]:
        """Read a confined context file or notebook SQL and static chart specifications."""
        return repository().read(path)

    query_slots = anyio.CapacityLimiter(4)

    @server.tool()
    async def run_query(sql: str, limit: int = min(DEFAULT_LIMIT, max_rows)) -> dict[str, Any]:
        """Run one SELECT/WITH, with a row cap and cancellable execution deadline."""
        return await anyio.to_thread.run_sync(partial(execute, sql, limit, max_rows=max_rows, timeout=timeout), limiter=query_slots)

    @server.resource('unshackle://repo/{path}')
    def file_resource(path: str) -> str:
        return json.dumps(repository().read(path), ensure_ascii=False)

    return server
