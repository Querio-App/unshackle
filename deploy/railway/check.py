#!/usr/bin/env python3
"""Check a deployed MCP endpoint. Only endpoint/token environment variables are read."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import httpx

DEMO = Path(__file__).resolve().parents[2] / "examples/mcp-demo"
PROTOCOL = "2025-11-25"


def normalize(rows):
    def value(v):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return v
    return [[value(v) for v in row] for row in rows]


def check(client, endpoint: str, token: str, *, deadline: float = 30):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    for auth in ({}, {"Authorization": "Bearer deliberately-invalid"}):
        assert client.post(endpoint, headers={**headers, **auth}, json={}).status_code == 401, "auth"
    headers["Authorization"] = "Bearer " + token
    sequence = 0

    def rpc(method, params=None):
        nonlocal sequence
        sequence += 1
        response = client.post(endpoint, headers=headers, json={
            "jsonrpc": "2.0", "id": sequence, "method": method, "params": params or {},
        })
        assert response.status_code == 200, "HTTP status"
        data = response.json()
        assert "result" in data, "JSON-RPC result"
        return data["result"]

    initialized = rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "unshackle-railway-check", "version": "1"}})
    assert initialized["protocolVersion"] == PROTOCOL, "protocol version"
    headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
    notification = client.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202, "initialized notification"
    assert {t["name"] for t in rpc("tools/list")["tools"]} >= {"overview", "search", "read", "run_query"}

    def tool(name, arguments=None):
        return rpc("tools/call", {"name": name, "arguments": arguments or {}})

    overview = tool("overview")["structuredContent"]
    assert overview["warehouse"]["configured"], "warehouse configuration"
    resources = rpc("resources/list")["resources"]
    assert resources and not any(".env" in r["uri"] for r in resources), "context resources"
    assert rpc("resources/read", {"uri": resources[0]["uri"]})["contents"]
    assert not tool("search", {"query": "recognized"}).get("isError"), "search"
    notebook = next(p for p in overview["files"] if p.endswith(".ipynb"))
    assert tool("read", {"path": notebook})["structuredContent"]["tiles"], "notebook"
    assert tool("read", {"path": ".env"}).get("isError"), "private file rejection"
    identity = tool("run_query", {"sql": "SELECT current_user, current_setting('transaction_read_only')"})
    assert identity["structuredContent"]["rows"] == [["example_reader", "on"]], "read-only role"
    for question in json.loads((DEMO / "questions.json").read_text()):
        result = tool("run_query", {"sql": question["sql"]})
        assert not result.get("isError"), question["id"]
        assert normalize(result["structuredContent"]["rows"]) == normalize(question["expected"]), question["id"]
    capped = tool("run_query", {"sql": "SELECT * FROM orders", "limit": 2})["structuredContent"]
    assert capped["row_count"] == 2 and capped["truncated"], "row cap"
    assert tool("run_query", {"sql": "DELETE FROM orders"}).get("isError"), "write rejection"
    start = time.monotonic()
    timed = tool("run_query", {"sql": f"SELECT pg_sleep({deadline + 5})"})
    assert timed.get("isError"), "query deadline"
    assert deadline * .8 <= time.monotonic() - start < deadline + 4, "deadline elapsed time"
    assert tool("run_query", {"sql": "SELECT 1"})["structuredContent"]["rows"] == [[1]], "query after cancel"
    print("PASS: auth, initialize, tools/resources, role, five answers, row cap, write rejection, deadline/recovery")


def main():
    try:
        endpoint = os.environ["UNSHACKLE_MCP_URL"]
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/mcp"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use a public HTTPS /mcp endpoint without credentials")
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            check(client, endpoint, os.environ["UNSHACKLE_MCP_TOKEN"])
    except Exception:
        print("FAIL: hosted MCP checks. Inspect auth, endpoint, fixture/ref and service logs privately; no response or secret printed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
