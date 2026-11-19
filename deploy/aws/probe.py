#!/usr/bin/env python3
"""Hosted/local fixture acceptance. Reads URL/token from env; prints no responses."""
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def normalize(rows):
    def value(item):
        try:
            return Decimal(str(item))
        except InvalidOperation:
            return item
    return [[value(item) for item in row] for row in rows]


async def probe():
    url = os.environ["UNSHACKLE_MCP_URL"]
    token = os.environ["UNSHACKLE_MCP_TOKEN"]
    if not url.endswith("/mcp"):
        raise ValueError("URL must end in /mcp")
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1:"):
        raise ValueError("Use HTTPS (loopback HTTP is only for local tests)")
    async with httpx.AsyncClient(timeout=75) as client:
        for headers in ({}, {"Authorization": "Bearer deliberately-invalid"}):
            response = await client.post(url, headers=headers, json={})
            assert response.status_code == 401, "Expected unauthorized response"
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=75) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {"overview", "read", "search", "run_query"} <= {t.name for t in listed.tools}
                overview = await session.call_tool("overview")
                assert not overview.isError and overview.structuredContent["files"]
                resources = (await session.list_resources()).resources
                assert resources and (await session.read_resource(resources[0].uri)).contents
                assert not (await session.call_tool("search", {"query": "recognized"})).isError
                notebook = next(p for p in overview.structuredContent["files"] if p.endswith(".ipynb"))
                assert (await session.call_tool("read", {"path": notebook})).structuredContent["tiles"]
                questions = json.loads((Path(__file__).resolve().parents[2] / "examples/mcp-demo/questions.json").read_text())
                for question in questions:
                    result = await session.call_tool("run_query", {"sql": question["sql"]})
                    assert not result.isError, "Query failed"
                    assert normalize(result.structuredContent["rows"]) == normalize(question["expected"]), question["id"]
                capped = await session.call_tool("run_query", {"sql": "SELECT * FROM orders ORDER BY order_id", "limit": 2})
                assert capped.structuredContent["row_count"] == 2 and capped.structuredContent["truncated"]
                assert (await session.call_tool("run_query", {"sql": "DELETE FROM orders"})).isError
                slow = await session.call_tool("run_query", {"sql": "SELECT pg_sleep(60)"})
                assert slow.isError, "Expected native 30-second cancellation"
                assert "timed out" in str(slow.content).lower() or "deadline" in str(slow.content).lower(), "Expected timeout error"
                after = await session.call_tool("run_query", {"sql": "SELECT COUNT(*) FROM orders"})
                assert not after.isError and after.structuredContent["rows"] == [[6]]
    print("PASS: bearer, initialization, context/resources, five answers, cap, write rejection, timeout and recovery")


if __name__ == "__main__":
    # Do not include raw HTTP/driver exceptions, which may contain credentials.
    try:
        anyio.run(probe)
    except Exception:
        raise SystemExit("FAIL: MCP acceptance; inspect sanitized server logs and rerun locally") from None
