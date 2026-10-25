"""Offline tests on a real Tableau Cloud export (tests/fixtures/tableau_live/, see its README).

The view CSVs in the fixture are Tableau's own numbers; the tiles' SQL runs on the same
Dunder Mifflin data in DuckDB and must match them.
"""

import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml
from conftest import notebook_source

from unshackle import build
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.verify import compare

FIX = Path(__file__).parent / "fixtures" / "tableau_live"
DATA = Path(__file__).parent / "live" / "data" / "dunder_mifflin"
WB = "00000000-0000-4000-8000-000000000008"
VIEW = {  # sheet -> view CSV saved by `unshackle extract`
    "Revenue by Paper Type": "00000000-0000-4000-8000-000000000009",
    "Average Order Value": "00000000-0000-4000-8000-000000000011",
    "Monthly Revenue": "00000000-0000-4000-8000-000000000010",
    "Large Orders by Account": "00000000-0000-4000-8000-000000000012",
}


@pytest.fixture
def bundle(tmp_path: Path):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def _tiles(b):
    (d,) = b.dashboards
    return {t.title: t for t in d.tiles if t.kind != "text"}


def test_published_datasource_behind_sqlproxy(bundle):
    # The workbook's `<relation name='sqlproxy' table='[sqlproxy]'>` stub is not a table.
    assert sorted(sm.name for sm in bundle.semantic_models) == ["accounts", "orders", "products"]
    assert sorted(f"{t.schema}.{t.name}" for t in bundle.tables) == ["public.accounts", "public.orders", "public.products"]
    # Calcs of the published datasource are translated once, not again for the workbook's copy.
    assert len(bundle.row_filters) == 1 and len(bundle.queries) == 1
    assert len([m for m in bundle.metrics if m.name == "average_order_value"]) == 1


def test_join_and_default_aggregation(bundle):
    join = next(w.message for w in bundle.warnings if w.message.startswith("joins 3 tables") and w.item.startswith("datasources/"))
    assert join.count("[accounts].[account_id]") == 1  # each join clause listed once
    orders = next(sm for sm in bundle.semantic_models if sm.name == "orders")
    assert next(m for m in orders.measures if m.name == "days_to_close").agg == "average"  # <column aggregation='Avg'>
    assert next(m for m in orders.measures if m.name == "revenue").agg == "sum"


def test_tiles(bundle):
    tiles = _tiles(bundle)
    assert set(tiles) == set(VIEW)
    assert all(t.sql and not t.todo for t in tiles.values()), {t.title: t.todo for t in tiles.values()}
    bar = tiles["Revenue by Paper Type"]
    assert "inner join public.products as products on orders.product_id = products.product_id" in bar.sql
    assert "products.paper_type as paper_type" in bar.sql and "where orders.status = 'COMPLETED'" in bar.sql
    assert (bar.kind, bar.mark) == ("chart", "bar")
    assert (tiles["Monthly Revenue"].mark, tiles["Average Order Value"].kind, tiles["Large Orders by Account"].kind) == ("line", "kpi", "table")
    assert "where (post_discount_revenue >= :min_order_value) = true" in tiles["Large Orders by Account"].sql
    assert tiles["Monthly Revenue"].sql.startswith("select\n  date_trunc('month', purchased_at) as month_of_purchased_at")
    for name, view in VIEW.items():
        assert tiles[name].source_config["parity_csv"] == f"migration/raw/tableau/views/{view}.csv"


def test_security_permissions_knowledge(bundle):
    (rf,) = bundle.row_filters
    assert rf.table.endswith("public.accounts") and "ISMEMBEROF('unshackle-managers')" in rf.todo and not rf.predicate
    lod = bundle.queries[0]
    assert "select account_id, sum(post_discount_revenue) as account_lifetime_revenue" in lod.sql and "FIXED" in lod.todo
    grants = {(p.principal, p.action, p.effect) for p in bundle.permissions}
    assert ("*", "view", "permit") in grants  # All Users, from the project's default permissions
    assert ("group:unshackle-managers", "view", "permit") in grants and ("group:unshackle-managers", "edit", "forbid") in grants
    text = "\n".join(k.body for k in bundle.knowledge)
    assert "hierarchy paper_type > product_name" in text and "{FIXED [account_id] : SUM([Revenue])}" in text


def test_build_and_match_tableau_numbers(tmp_path: Path, bundle):
    import duckdb

    from unshackle.run import notebook_queries, query

    build.apply(bundle, tmp_path)
    nb = tmp_path / "notebooks/sales-overview.ipynb"
    notebook_source(nb)
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        con.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    con.close()
    params, sqls = notebook_queries(nb)
    assert params["min_order_value"] == 500.0
    tiles = _tiles(bundle)
    for name, view in VIEW.items():
        source = pd.read_csv(tmp_path / "migration/raw/tableau/views" / f"{view}.csv")
        target = query(sqls[tiles[name].id.replace("-", "_")], params, url=f"duckdb:///{db}")
        keys = [c for c in target.columns if target[c].dtype == object or "month" in c]
        status, gap, note = compare(source, target, keys=keys)
        assert status == "pass", (name, note)
