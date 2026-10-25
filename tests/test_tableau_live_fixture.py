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
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
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
    # one model per table, plus the FIXED LOD's grain
    assert sorted(sm.name for sm in bundle.semantic_models) == ["account_id_lod", "accounts", "orders", "products"]
    assert sorted(f"{t.schema}.{t.name}" for t in bundle.tables) == ["public.accounts", "public.orders", "public.products"]
    # Calcs of the published datasource are translated once, not again for the workbook's copy.
    assert sorted(rf.table for rf in bundle.row_filters) == ["public.accounts", "public.orders", "public.products"]
    assert not bundle.queries
    assert len([m for m in bundle.metrics if m.name == "average_order_value"]) == 1


def test_join_and_default_aggregation(bundle):
    assert not any(w.message.startswith("joins ") for w in bundle.warnings)  # every join became a relationship
    orders = next(sm for sm in bundle.semantic_models if sm.name == "orders")
    joins = [(e.name, e.expr, e.join_type) for e in orders.entities if e.type == "foreign" and e.join_type]
    assert joins == [("accounts", "account_id", "inner"), ("products", "product_id", "inner")]
    assert "Tableau drops orders rows with no match in accounts" in orders.description
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
    assert "where (orders.post_discount_revenue >= :min_order_value) = true" in tiles["Large Orders by Account"].sql
    # a sheet on orders alone keeps the inner joins: Tableau computes it over the joined rows
    monthly = tiles["Monthly Revenue"].sql
    assert monthly.startswith("select\n  date_trunc('month', orders.purchased_at) as month_of_purchased_at")
    assert "inner join public.accounts as accounts on orders.account_id = accounts.account_id" in monthly
    for name, view in VIEW.items():
        assert tiles[name].source_config["parity_csv"] == f"migration/raw/tableau/views/{view}.csv"


def test_security_permissions_knowledge(bundle):
    rf = {r.table: r for r in bundle.row_filters}
    pred = "case when 'unshackle-managers' in ({{ user.groups }}) then account_manager = {{ user.email }} else true end"
    assert rf["public.accounts"].predicate == pred and "ISMEMBEROF('unshackle-managers')" in rf["public.accounts"].todo
    # a datasource filter applies to the joined rows: the other tables follow the inner joins
    assert rf["public.orders"].predicate == f"account_id in (select account_id from public.accounts where {pred})"
    assert rf["public.products"].predicate.startswith("product_id in (select product_id from public.orders where account_id in (")
    lod = next(sm for sm in bundle.semantic_models if sm.name == "account_id_lod")
    assert lod.sql.startswith("select orders.account_id as account_id, sum(orders.post_discount_revenue) as account_lifetime_revenue\n")
    assert "inner join public.products" in lod.sql  # computed over the datasource's joined rows
    assert [m.name for m in lod.measures] == ["account_lifetime_revenue"]
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


def test_user_filter_predicates_run(bundle):
    """IF ISMEMBEROF('unshackle-managers') THEN [account_manager] = USERNAME() ELSE TRUE END, as a datasource filter: managers
    see their own accounts' rows, everyone else sees all rows; orders and products follow the inner joins."""
    import duckdb

    con = duckdb.connect()
    con.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        con.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    manager = con.execute("select account_manager from public.accounts order by 1 limit 1").fetchone()[0]
    rf = {r.table: r.predicate for r in bundle.row_filters}

    def visible(table: str, groups: str) -> int:
        pred = rf[table].replace("{{ user.groups }}", groups).replace("{{ user.email }}", f"'{manager}'")
        return con.execute(f"select count(*) from {table} where {pred}").fetchone()[0]

    expected = con.execute(
        "select count(*), count(distinct o.product_id) from public.orders o join public.accounts a using (account_id) "
        f"where a.account_manager = '{manager}'"
    ).fetchone()
    assert visible("public.orders", "'unshackle-managers', 'sales'") == expected[0] > 0
    assert visible("public.products", "'unshackle-managers'") == expected[1]
    assert visible("public.orders", "'sales'") == con.execute("select count(*) from public.orders").fetchone()[0]
