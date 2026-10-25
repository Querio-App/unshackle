"""Cross-database joins (tests/fixtures/tableau_cross_db/, see its README): a data source whose tables come from several
named connections. Tableau joins them itself; one SQL query can't, so whatever reads tables of both databases is a TODO,
and what reads one database keeps its SQL. Tile SQL runs in DuckDB on the Dunder Mifflin data and is compared with numbers
computed in pandas from Tableau's rule: a sheet over a left join on the other table's key, with fields of the first table
only, has the first table's rows."""

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import connections, read_datasource

FIX = Path(__file__).parent / "fixtures" / "tableau_cross_db"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
CROSS = "that Tableau joins across databases"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("xdb")
    shutil.copytree(FIX, tmp / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp, "tableau"))


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


def _tiles(b):
    return {t.title: t for d in b.dashboards for t in d.tiles}


def test_connections_of_each_table():
    root = ET.fromstring((FIX / "workbooks" / "e8100000-0000-4000-8000-000000000001.twb").read_bytes())
    by_caption = {el.get("caption"): read_datasource(el, "x", "x.twb") for el in root.findall("datasources/datasource")}
    xdb = by_caption["Orders and CRM Accounts"]
    assert connections(xdb) == ["postgres crm.example.com:5432 crm", "postgres warehouse.example.com:5432 sales"]
    assert connections(xdb, ["orders"]) == []  # one table reads one database
    assert connections(by_caption["Orders and Accounts"]) == []
    assert len(connections(by_caption["Related across Databases"])) == 2


def test_the_report_names_the_connections(bundle):
    warns = [w for w in bundle.warnings if CROSS in w.message]
    assert sorted(w.item.split(" datasource ")[-1] for w in warns) == ["Orders and CRM Accounts", "Related across Databases", "Sales and CRM Accounts"]
    assert all("crm.example.com:5432 crm" in w.message and "warehouse.example.com:5432 sales" in w.message for w in warns)
    # no entity joins the two databases; the one-database data source keeps its foreign entity
    across = [w.message for w in bundle.warnings if "(across databases)" in w.message]
    assert len(across) == 3
    foreign = {(sm.name, e.name) for sm in bundle.semantic_models for e in sm.entities if e.type == "foreign"}
    assert foreign == {("orders", "accounts")}


def test_sheets_over_both_databases_are_todo(bundle):
    tiles = _tiles(bundle)
    for title in ("Revenue by Business Size (two databases)", "Related Revenue by Business Size (two databases)"):
        assert not tiles[title].sql and CROSS in tiles[title].todo, title
    (lod,) = [m for m in bundle.metrics if m.label == "Account Revenue"]
    assert CROSS in lod.todo and lod.source_expression == "{FIXED [account_name] : SUM([post_discount_revenue])}"


def test_sheets_over_one_database_keep_their_sql(bundle, con):
    import pandas as pd

    orders = con.sql("select * from public.orders").df()
    accounts = con.sql("select * from public.accounts").df()
    tiles = _tiles(bundle)
    by_status = orders.groupby("status").post_discount_revenue.sum().astype(float).to_dict()
    for title in ("Revenue by Status (two databases)", "Related Revenue by Status (two databases)"):
        t = tiles[title]
        assert not t.todo and "accounts" not in t.sql, title
        assert {r[0]: float(r[1]) for r in con.sql(t.sql).fetchall()} == pytest.approx(by_status)
    # the same join inside one database is SQL: a left join, so orders without an account count under NULL
    joined = orders.merge(accounts, on="account_id", how="left")
    want = joined.groupby("business_size", dropna=False).post_discount_revenue.sum()
    want = {None if pd.isna(k) else int(k): float(v) for k, v in want.items()}
    t = tiles["Revenue by Business Size"]
    assert not t.todo and "left join public.accounts" in t.sql
    assert {r[0]: float(r[1]) for r in con.sql(t.sql).fetchall()} == pytest.approx(want)


def test_count_of_a_logical_table(bundle, con):
    """"<table> (Count)" on a data source with relationships counts that table's rows, each once per distinct status it
    relates to (an account with orders of three statuses counts three times, once under each)."""
    import pandas as pd

    orders = con.sql("select * from public.orders").df()
    accounts = con.sql("select * from public.accounts").df()
    tiles = _tiles(bundle)

    def run(title):
        assert not tiles[title].todo, tiles[title].todo
        return {r[0]: r[1] for r in con.sql(tiles[title].sql).fetchall()}

    assert run("Orders by Status") == orders.groupby("status", dropna=False).size().to_dict()
    pairs = accounts.merge(orders[["account_id", "status"]].drop_duplicates(), on="account_id", how="left")
    want = {None if pd.isna(k) else k: v for k, v in pairs.groupby("status", dropna=False).size().items()}
    assert run("Accounts by Status") == want


def test_same_table_in_two_databases_is_two_models(bundle, con):
    """public.accounts in the sales database and in the crm database: one model each, named after its database, never
    merged; the same table of the same database is one model across data sources. In one warehouse the two have one
    name, so the second model says so."""
    accounts = {sm.name: sm for sm in bundle.semantic_models if sm.table == "public.accounts"}
    assert sorted(accounts) == ["accounts", "accounts_crm"]
    sales, crm = accounts["accounts"], accounts["accounts_crm"]
    assert not sales.todo
    assert "crm.example.com:5432 crm" in crm.todo and "In one warehouse they are one table" in crm.todo
    dims = {name: {d.name for d in sm.dimensions} for name, sm in accounts.items()}
    assert "account_manager" in dims["accounts_crm"] and "account_manager" not in dims["accounts"]  # only the crm table has it
    assert "payment_cycle" in dims["accounts"] and "payment_cycle" not in dims["accounts_crm"]
    assert [sm.name for sm in bundle.semantic_models if sm.table == "public.orders"] == ["orders"]  # both read the sales orders
    assert [(t.schema, t.name) for t in bundle.tables].count(("public", "accounts")) == 1  # the catalog lists the warehouse table once
    tiles = _tiles(bundle)
    assert CROSS in tiles["Business Size by Account Manager (two databases)"].todo
    t = tiles["Business Size by Payment Cycle (sales accounts)"]
    a = con.sql("select * from public.accounts").df()
    want = {None if k != k else k: float(v) for k, v in a.groupby("payment_cycle", dropna=False).business_size.sum().items()}
    assert not t.todo and {r[0]: float(r[1]) for r in con.sql(t.sql).fetchall()} == pytest.approx(want)
