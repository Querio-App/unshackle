"""Offline fixture/protocol acceptance. Real client/cloud runs are recorded separately."""
import json
import os
from pathlib import Path
import sys

import anyio
import duckdb
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import yaml

from unshackle import build
from unshackle.imports import dbt, sql, warehouse
from unshackle.mcp.repository import Repository

DEMO = Path(__file__).parents[1] / "examples/mcp-demo"
QUESTIONS = json.loads((DEMO / "questions.json").read_text())


def normalize(rows):
    """Compare lossless MCP decimal strings and numeric fixture answers."""
    from decimal import Decimal, InvalidOperation
    def value(v):
        if isinstance(v, (str, int, float)):
            try:
                return Decimal(str(v))
            except InvalidOperation:
                pass
        return v
    return [[value(v) for v in row] for row in rows]


def fixture_database(path):
    con = duckdb.connect(str(path))
    con.execute((DEMO / "schema.sql").read_text())
    con.close()


def sqlite_fixture(path):
    import sqlite3
    source = "\n".join(line for line in (DEMO / "schema.sql").read_text().splitlines()
                       if not line.startswith("COMMENT ON"))
    with sqlite3.connect(path) as con:
        con.executescript(source)


def import_all(repo, url):
    build.apply(warehouse.introspect(url), repo)
    build.apply(dbt.parse(DEMO / "dbt"), repo)
    build.apply(sql.parse([DEMO / "inputs"]), repo)


def context_bytes(repo):
    view = Repository(repo)
    return {path: (repo / path).read_bytes() for path in view.files()}


def test_five_hand_derived_questions_against_synthetic_data(tmp_path):
    path = tmp_path / "example.duckdb"
    fixture_database(path)
    with duckdb.connect(str(path)) as con:
        for q in QUESTIONS:
            assert normalize(con.execute(q["sql"]).fetchall()) == normalize(q["expected"]), q["id"]
    assert [q["expected"] for q in QUESTIONS] == [
        [[350]], [["North", 270], ["South", 80]], [["Acme Paper", 150]],
        [["2025-01", 230], ["2025-02", 120]], [[87.5]],
    ]


def test_combined_import_idempotence_annotations_layout_and_no_python_execution(tmp_path):
    path = tmp_path / "example.sqlite"
    sqlite_fixture(path)
    url = f"sqlite:///{path}"
    repo = tmp_path / "context"
    repo.mkdir()
    (repo / "unshackle.toml").write_text('[paths]\ncatalog = "context/catalog"\nnotebooks = "analysis"\n')
    import_all(repo, url)
    catalog = next((repo / "context/catalog").rglob("orders.yml"))
    doc = yaml.safe_load(catalog.read_text())
    doc["reviewer_note"] = "Handwritten annotation"
    catalog.write_text(yaml.safe_dump(doc, sort_keys=False))
    import_all(repo, url)
    before = context_bytes(repo)
    snapshot = (repo / "migration/warehouse-schema.json").read_bytes()
    import_all(repo, url)
    assert context_bytes(repo) == before
    assert (repo / "migration/warehouse-schema.json").read_bytes() == snapshot
    assert yaml.safe_load(catalog.read_text())["reviewer_note"] == "Handwritten annotation"
    view = Repository(repo)
    assert view.overview()["models"] >= 1
    assert view.overview()["metrics"] >= 1
    assert any(item["has_todo"] for item in view.search("dynamic"))
    assert any("CASE WHEN" in text.decode() for text in before.values())


def protocol_checks(repo, url):
    async def check():
        params = StdioServerParameters(
            command=sys.executable, args=["-c", "from unshackle.cli import main; main()", "mcp", "--repo", str(repo)],
            env={**os.environ, "UNSHACKLE_WAREHOUSE_URL": url},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                overview = (await session.call_tool("overview")).structuredContent
                assert overview["warehouse"]["configured"] and not overview["warehouse"]["connection_verified"]
                resources = (await session.list_resources()).resources
                assert resources and not any(".env" in str(r.uri) for r in resources)
                assert (await session.read_resource(resources[0].uri)).contents
                search = await session.call_tool("search", {"query": "recognized"})
                assert not search.isError
                notebook = next(p for p in overview["files"] if p.endswith(".ipynb"))
                read = (await session.call_tool("read", {"path": notebook})).structuredContent
                assert read["tiles"]
                assert (await session.call_tool("read", {"path": ".env"})).isError
                assert (await session.call_tool("run_query", {"sql": "DELETE FROM orders"})).isError
                for question in QUESTIONS:
                    result = await session.call_tool("run_query", {"sql": question["sql"]})
                    assert not result.isError, result
                    assert normalize(result.structuredContent["rows"]) == normalize(question["expected"])
                capped = (await session.call_tool("run_query", {"sql": "SELECT * FROM orders", "limit": 2})).structuredContent
                assert capped["row_count"] == 2 and capped["truncated"]
    anyio.run(check)


def test_offline_stdio_protocol_five_questions(tmp_path):
    path = tmp_path / "example.sqlite"
    sqlite_fixture(path)
    repo = tmp_path / "context"
    url = f"sqlite:///{path}"
    import_all(repo, url)
    protocol_checks(repo, url)


@pytest.mark.skipif(not os.environ.get("UNSHACKLE_ACCEPTANCE_POSTGRES_URL"), reason="explicit local Postgres acceptance URL required")
def test_real_postgres_import_role_and_stdio(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import DBAPIError
    url = os.environ["UNSHACKLE_ACCEPTANCE_POSTGRES_URL"]
    import_all(tmp_path, url)
    protocol_checks(tmp_path, url)
    engine = create_engine(url)
    try:
        with engine.connect() as con:
            with pytest.raises(DBAPIError):
                con.exec_driver_sql("DELETE FROM orders")
    finally:
        engine.dispose()
