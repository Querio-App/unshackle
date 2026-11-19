#!/usr/bin/env python3
"""Check an existing MCP endpoint. No cloud mutation or model-service calls."""
import asyncio
from decimal import Decimal, InvalidOperation
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

QUESTIONS = json.loads((Path(__file__).resolve().parents[2] / "examples/mcp-demo/questions.json").read_text())


def normalize(rows):
    def value(item):
        try:
            return Decimal(str(item))
        except InvalidOperation:
            return item
    return [[value(item) for item in row] for row in rows]


async def verify(url, token):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})) or parsed.path != "/mcp" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use HTTPS /mcp without credentials; HTTP allowed only for loopback tests")
    async with httpx.AsyncClient(timeout=120) as client:
        for headers in ({}, {"Authorization": "Bearer deliberately-wrong-token"}):
            response = await client.post(url, headers=headers)
            assert response.status_code == 401, "Expected application 401"
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=120) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert {"overview", "search", "read", "run_query"} <= tools
                overview = await session.call_tool("overview")
                assert not overview.isError and overview.structuredContent["files"]
                resources = (await session.list_resources()).resources
                assert resources and (await session.read_resource(resources[0].uri)).contents
                assert not (await session.call_tool("search", {"query": "recognized"})).isError
                assert not (await session.call_tool("read", {"path": overview.structuredContent["files"][0]})).isError
                assert (await session.call_tool("read", {"path": ".env"})).isError
                assert (await session.call_tool("run_query", {"sql": "DELETE FROM orders"})).isError
                for question in QUESTIONS:
                    answer = await session.call_tool("run_query", {"sql": question["sql"]})
                    assert not answer.isError, question["id"]
                    assert normalize(answer.structuredContent["rows"]) == normalize(question["expected"]), question["id"]
                capped = await session.call_tool("run_query", {"sql": "SELECT * FROM orders", "limit": 2})
                assert capped.structuredContent["row_count"] == 2 and capped.structuredContent["truncated"]
                started = time.monotonic()
                timed = await session.call_tool("run_query", {"sql": "SELECT pg_sleep(45)"})
                assert timed.isError and "deadline" in str(timed.content).lower(), "Expected native query deadline"
                assert time.monotonic() - started < 42, "Query was not cancelled before sleep completed"
                recovered = await session.call_tool("run_query", {"sql": "SELECT 1 AS healthy"})
                assert not recovered.isError and recovered.structuredContent["rows"] == [[1]]
                print(json.dumps({"protocol": initialized.protocolVersion, "auth": "401 missing/wrong; valid accepted",
                                  "questions_passed": 5, "context_resources": "passed", "row_cap": "passed",
                                  "native_timeout_and_recovery": "passed"}))


if __name__ == "__main__":
    asyncio.run(verify(os.environ["UNSHACKLE_MCP_URL"], os.environ["UNSHACKLE_MCP_TOKEN"]))
