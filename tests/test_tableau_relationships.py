"""Tableau relationships (tests/fixtures/tableau_relationships/, see its README).

With relationships (the logical layer), Tableau doesn't join the tables: it aggregates each measure on its own table's
rows at the sheet's dimensions. A sheet over one logical table queries that table alone; a sheet over several must not
be computed over a join of them, which would repeat rows.
"""

import shutil
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_relationships"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"


@pytest.fixture
def bundle(tmp_path: Path):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "shippings"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


def _tiles(b):
    return {t.title: t for d in b.dashboards for t in d.tiles}


def test_logical_tables_are_models(bundle):
    assert sorted(sm.name for sm in bundle.semantic_models) == ["accounts", "dunder_mifflin_relationships", "orders", "shippings"]


def test_one_table_sheets_query_that_table(bundle, con):
    tiles = _tiles(bundle)
    rows = con.sql(tiles["Total Business Size"].sql).fetchall()
    assert rows == con.sql("select sum(business_size) from public.accounts").fetchall()  # once per account, not per order
    assert not tiles["Revenue by Status"].todo


def test_sheets_over_related_tables_are_not_joined(bundle):
    tiles = _tiles(bundle)
    for name in ("Business Size by Status", "Revenue by Shipping Mode", "Target by Status", "Status by Shipping Mode",
                 "Revenue per Account Size by Status"):
        assert not tiles[name].sql and "related tables" in tiles[name].todo, name
