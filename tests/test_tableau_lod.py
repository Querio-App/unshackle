"""Tableau level of detail expressions (tests/fixtures/tableau_lod/): FIXED grains as SQL models with
relationships, and tile SQL for FIXED, INCLUDE and EXCLUDE.

The workbook's sheets run on the Dunder Mifflin data in DuckDB and are compared with the same numbers
computed in pandas the way Tableau defines them: FIXED is computed over all of the datasource's rows
before dimension filters, and an aggregated FIXED value counts once per distinct combination of the
view's dimensions and the FIXED dimensions; INCLUDE aggregates at the view's dimensions plus its own,
EXCLUDE at the view's dimensions minus its own.
"""

import shutil
from pathlib import Path

import pytest
import yaml
from test_ossie import _check

from unshackle import build
from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_lod"
DATA = Path(__file__).parent / "live" / "data" / "dunder_mifflin"


@pytest.fixture
def bundle(tmp_path: Path):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


def _models(b):
    return {sm.name: sm for sm in b.semantic_models}


def _tiles(b):
    return {t.title: t for d in b.dashboards for t in d.tiles}


def test_fixed_grain_is_a_related_sql_model(bundle):
    models = _models(bundle)
    lod = models["account_id_lod"]
    assert lod.table == "account_id_lod" and lod.sql.startswith(
        "select orders.account_id as account_id, sum(orders.post_discount_revenue) as account_revenue\nfrom public.orders as orders\n"
    )
    assert [(e.name, e.type) for e in lod.entities] == [("account_id", "primary")]
    assert ("account_id", "foreign", "") in [(e.name, e.type, e.expr) for e in models["orders"].entities]
    # the FIXED value as a measure, AVG({FIXED ...}) as a metric on the grain, and a bucket over it as a dimension
    assert [(m.name, m.agg, m.expr) for m in lod.measures] == [("account_revenue", "sum", ""), ("avg_account_revenue", "average", "account_revenue")]
    tier = {d.name: d for d in lod.dimensions}["account_tier"]
    assert tier.expr.startswith("case when account_revenue >= 1000000 then 'Large'") and not tier.todo
    # a FIXED dimension of another table relates that table
    assert ("account_name", "foreign") in [(e.name, e.type) for e in models["accounts"].entities]
    first = {d.name: d for d in models["account_name_lod"].dimensions}["first_order_date"]
    assert (first.type, first.time_granularity) == ("time", "day")


def test_what_stays_todo(bundle):
    todo = {m.name: m.todo for m in bundle.metrics if m.todo}
    assert "INCLUDE LOD depends on the view" in todo["avg_order_revenue"] and "tiles" in todo["avg_order_revenue"]
    assert "EXCLUDE LOD depends on the view" in todo["cycle_revenue"]
    assert "several dimensions (account_id, paper_type)" in todo["account_paper_revenue"]
    assert "mixes row fields with a FIXED LOD value" in todo["share_of_account_revenue"]
    assert "combines row fields and FIXED LOD values" in todo["revenue_share"]
    assert set(todo) == {"avg_order_revenue", "cycle_revenue", "account_paper_revenue", "share_of_account_revenue", "revenue_share"}
    tiles = _tiles(bundle)
    assert not tiles["Revenue Share by Status"].sql and "inside a larger expression" in tiles["Revenue Share by Status"].todo
    assert all(t.sql and not t.todo for name, t in tiles.items() if name != "Revenue Share by Status"), {
        n: t.todo for n, t in tiles.items() if t.todo
    }


def test_tile_sql_shapes(bundle):
    tiles = _tiles(bundle)
    tier = tiles["Revenue by Account Tier"].sql  # a FIXED value as a dimension: joined to each row
    assert "left join account_id_lod on orders.account_id is not distinct from account_id_lod.account_id" in tier
    kpi = tiles["Average Account Revenue"].sql  # AVG of a FIXED value: one value per FIXED key
    assert "select distinct orders.account_id as lod_key_1" in kpi and "avg(account_id_lod.account_revenue)" in kpi
    include = tiles["Order Size by Paper Type"].sql
    assert "orders.order_id as lod_dim_1, sum(orders.post_discount_revenue) as lod_value" in include and "avg(t.lod_value)" in include
    exclude = tiles["Cycle Revenue by Paper Type"].sql  # EXCLUDE [paper_type] from a paper_type x payment_cycle view
    assert "select accounts.payment_cycle as payment_cycle, sum(orders.post_discount_revenue) as cycle_revenue" in exclude
    assert "left join lod_measure_1 on base.payment_cycle is not distinct from lod_measure_1.payment_cycle" in exclude


def test_tiles_match_tableau_semantics(bundle, con):
    tiles = _tiles(bundle)
    o, a, p = (con.sql(f"select * from public.{t}").df() for t in ("orders", "accounts", "products"))
    rows = o.merge(a, on="account_id").merge(p, on="product_id")  # the datasource's inner joins
    acct = rows.groupby("account_id").post_discount_revenue.sum()

    def run(title, *keys):
        df = con.sql(tiles[title].sql).df()
        return df.set_index(list(keys)) if keys else df

    def same(got, exp):
        got, exp = got.sort_index(), exp.sort_index()
        assert list(got.index) == list(exp.index)
        assert got.values == pytest.approx(exp.values, rel=1e-9)

    tier = acct.map(lambda v: "Large" if v >= 1e6 else "Mid" if v >= 250000 else "Small")
    same(run("Revenue by Account Tier", "account_tier").post_discount_revenue,
         rows.assign(t=rows.account_id.map(tier)).groupby("t").post_discount_revenue.sum())
    assert run("Average Account Revenue").iloc[0, 0] == pytest.approx(acct.mean())
    domain = rows[["payment_cycle", "account_id"]].drop_duplicates()
    cycle = run("Account Revenue by Payment Cycle", "payment_cycle")
    same(cycle.avg_account_revenue, domain.assign(v=domain.account_id.map(acct)).groupby("payment_cycle").v.mean())
    same(cycle.post_discount_revenue, rows.groupby("payment_cycle").post_discount_revenue.sum())
    same(run("Order Size by Paper Type", "paper_type").avg_order_revenue,
         rows.groupby(["paper_type", "order_id"]).post_discount_revenue.sum().groupby("paper_type").mean())
    marks = rows[["paper_type", "payment_cycle"]].drop_duplicates()
    per_cycle = rows.groupby("payment_cycle").post_discount_revenue.sum()
    same(run("Cycle Revenue by Paper Type", "paper_type", "payment_cycle").cycle_revenue,
         marks.assign(v=marks.payment_cycle.map(per_cycle)).set_index(["paper_type", "payment_cycle"]).v)
    first = rows.groupby("account_name").purchased_at.min()
    month = rows.account_name.map(first).astype("datetime64[ns]").dt.to_period("M").dt.to_timestamp()
    new = run("New Accounts by Month")
    new.index = new.month_of_first_order_date.astype("datetime64[ns]")
    same(new.distinct_count_of_account_name, rows.assign(m=month).groupby("m").account_name.nunique())
    same(run("Top Account per Paper Type", "paper_type").max_account_paper_revenue,
         rows.groupby(["account_id", "paper_type"]).post_discount_revenue.sum().groupby("paper_type").max())
    same(run("Share of Account Revenue by Status", "status").share_of_account_revenue,
         rows.assign(s=rows.post_discount_revenue / rows.account_id.map(acct)).groupby("status").s.sum())
    for sm in bundle.semantic_models:
        if sm.sql:  # one row per FIXED key
            df = con.sql(sm.sql).df()
            assert df[sm.entities[0].name].is_unique and len(df) == rows[sm.entities[0].name].nunique()


def test_ossie_reads_the_grain_query(tmp_path: Path, bundle, con):
    build.apply(bundle, tmp_path)
    docs = {p.name: yaml.safe_load(p.read_text()) for p in (tmp_path / "semantic").glob("*.ossie.yaml")}
    for doc in docs.values():
        _check(doc)
    lod = docs["account_id_lod.ossie.yaml"]["datasets"][0]
    assert lod["source"] == _models(bundle)["account_id_lod"].sql and lod["primary_key"] == ["account_id"]
    con.sql(lod["source"])  # the dataset source runs as is
    orders = docs["orders.ossie.yaml"]
    rels = {(r["from"], r["to"]): r for r in orders["relationships"]}
    assert rels[("orders", "account_id_lod")]["from_columns"] == ["account_id"]
    assert "join_type" in rels[("orders", "accounts")]["custom_extensions"][0]["data"]
    assert {d["name"] for d in orders["datasets"]} >= {"orders", "accounts", "account_id_lod", "account_name_lod"}
