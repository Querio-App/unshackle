"""Tableau nested LOD expressions (tests/fixtures/tableau_lod_nested).

A hand-written workbook on one embedded data source, "Orders" (orders joined to accounts and products). Its sheets run on
the Dunder Mifflin data in DuckDB and are compared with the same numbers computed here from Tableau's documented rules:

- an INCLUDE or EXCLUDE inside a FIXED takes its dimensions from the FIXED around it, not from the view:
  {FIXED [a] : AGG({INCLUDE [b] : x})} is {FIXED [a] : AGG({FIXED [a], [b] : x})}, and an EXCLUDE removes dimensions of the
  FIXED (excluding one the FIXED doesn't have changes nothing);
- a FIXED around another FIXED takes the inner value once per distinct combination of the outer and inner dimensions, then
  aggregates per outer key; aggregated in the view, a FIXED value counts once per distinct combination of the view's and
  the FIXED dimensions;
- FIXED is computed before dimension filters and after context filters, inner and outer levels alike.
"""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_lod_nested"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
REL = Path(__file__).parent / "fixtures" / "tableau_relationships"


def _parse(tmp_path: Path, fix: Path = FIX, edit=None):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(fix, raw)
    if edit:
        for p in raw.rglob("*.twb"):
            p.write_text(edit(p.read_text()))
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return _parse(tmp_path_factory.mktemp("nested"))


@pytest.fixture(scope="module")
def tiles(bundle) -> dict:
    return {t.title: t for d in bundle.dashboards for t in d.tiles}


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


@pytest.fixture(scope="module")
def rows(con) -> pd.DataFrame:
    o, a, p = (con.sql(f"select * from public.{t}").df() for t in ("orders", "accounts", "products"))
    r = o.merge(a, on="account_id").merge(p, on="product_id")  # the datasource's inner joins
    r["post_discount_revenue"] = r.post_discount_revenue.astype(float)
    r["tier"] = r.discount_percentage.astype(float).map(lambda d: "High" if d >= 10 else "Low" if d > 0 else None)
    return r


def _run(con, tile, *keys) -> pd.DataFrame:
    assert tile.sql and not tile.todo, tile.todo
    df = con.sql(tile.sql).df()
    return df.set_index(list(keys)) if keys else df


def _same(got: pd.Series, exp: pd.Series) -> None:
    got, exp = got.sort_index(), exp.sort_index()
    assert list(got.index) == list(exp.index)
    assert got.astype(float).values == pytest.approx(exp.astype(float).values, rel=1e-9)


def _fixed(rows: pd.DataFrame, outer: list[str], agg: str, inner: list[str], value: pd.Series) -> pd.Series:
    """{FIXED outer : agg(inner value)}: the inner value (indexed by `inner`) once per distinct (outer, inner), aggregated per outer."""
    domain = rows[list(dict.fromkeys(outer + inner))].drop_duplicates()
    domain = domain.assign(v=value.reindex(pd.MultiIndex.from_frame(domain[inner]) if len(inner) > 1 else domain[inner[0]]).values)
    return domain.groupby(outer, dropna=False).v.agg(agg)


def _view(rows: pd.DataFrame, view: list[str], agg: str, fixed: list[str], value: pd.Series) -> pd.Series:
    """agg of a FIXED value in a view: once per distinct (view, FIXED) combination."""
    return _fixed(rows, view, agg, fixed, value)


def test_include_inside_fixed(con, tiles, rows):
    acct = rows.groupby(["payment_cycle", "account_id"]).post_discount_revenue.sum()  # INCLUDE [account_id] inside FIXED [payment_cycle]
    cycle = acct.groupby("payment_cycle").mean()
    got = _run(con, tiles["include_in_fixed"], "payment_cycle")
    _same(got.avg_cycle_avg_account_revenue, cycle)
    _same(got.revenue, rows.groupby("payment_cycle").post_discount_revenue.sum())
    assert "" in cycle.index  # an empty-string cycle is a key of its own


def test_exclude_inside_fixed(con, tiles, rows):
    acct = rows.groupby("account_id").post_discount_revenue.sum()  # EXCLUDE [paper_type] inside FIXED [account_id], [paper_type]
    per = _fixed(rows, ["account_id", "paper_type"], "sum", ["account_id"], acct)  # one inner value per (account, paper type)
    _same(per.groupby("account_id").first(), acct)
    got = _run(con, tiles["exclude_in_fixed"], "paper_type")
    _same(got.avg_account_revenue_per_paper, _view(rows, ["paper_type"], "mean", ["account_id", "paper_type"], per))
    _same(got.account_revenue_per_paper, _view(rows, ["paper_type"], "sum", ["account_id", "paper_type"], per))
    got = _run(con, tiles["exclude_in_fixed_mgr_paper"], "account_manager", "paper_type")
    _same(got.min_account_revenue_per_paper, _view(rows, ["account_manager", "paper_type"], "min", ["account_id", "paper_type"], per))


def test_include_of_two_dimensions_and_count(con, tiles, rows):
    qty = rows.groupby(["account_manager", "account_id", "paper_type"]).quantity.sum()
    got = _run(con, tiles["include_two_dims"], "account_manager")
    _same(got.max_manager_max_account_paper_qty, qty.groupby("account_manager").max())
    # COUNT of the INCLUDE values: one per (manager, account), so the manager's accounts
    _same(got.avg_manager_accounts, rows.groupby("account_manager").account_id.nunique())


def test_exclude_of_a_dimension_the_fixed_does_not_have(con, tiles, rows):
    got = _run(con, tiles["exclude_absent_dim"], "payment_cycle")
    _same(got.cycle_orders, rows.groupby("payment_cycle").order_id.nunique())


def test_two_dimension_fixed_inside_fixed(con, tiles, rows):
    inner = rows.groupby(["account_id", "paper_type"]).post_discount_revenue.sum()
    got = _run(con, tiles["fixed_two_dims_nested"], "payment_cycle")
    _same(got.avg_cycle_avg_account_paper_revenue, _fixed(rows, ["payment_cycle"], "mean", ["account_id", "paper_type"], inner))


def test_nested_lods_inside_larger_expressions(con, tiles, rows):
    cycle = rows.groupby(["payment_cycle", "account_id"]).post_discount_revenue.sum().groupby("payment_cycle").mean()
    # a row-level calc over the nested value, summed by status: once per (status, cycle)
    got = _run(con, tiles["nested_row_expr"], "status")
    _same(got.cycle_avg_account_revenue_k, _view(rows, ["status"], "sum", ["payment_cycle"], cycle / 1000))
    # a dimension over the nested value
    band = rows.payment_cycle.map(cycle).ge(1700000).map({True: "High", False: "Low"})
    _same(_run(con, tiles["nested_band"], "cycle_band").revenue, rows.assign(b=band).groupby("b").post_discount_revenue.sum())
    # SUM([Revenue]) / SUM(nested FIXED): each aggregate at its own level
    got = _run(con, tiles["nested_ratio"], "status")
    _same(got.revenue_vs_cycle_avg,
          rows.groupby("status").post_discount_revenue.sum() / _view(rows, ["status"], "sum", ["payment_cycle"], cycle))


def test_null_fixed_key(con, tiles, rows):
    acct = rows.groupby(["tier", "account_id"], dropna=False).quantity.sum()
    exp = acct.groupby("tier", dropna=False).mean()
    assert exp.index.isna().any()  # orders without a discount: a NULL tier, a FIXED key of its own
    got = _run(con, tiles["include_null_key"], "discount_tier")
    _same(got.avg_tier_avg_account_qty.rename(index=lambda x: x if isinstance(x, str) else "∅"),
          exp.rename(index=lambda x: x if isinstance(x, str) else "∅"))


def test_dimension_and_context_filters(con, tiles, rows):
    cycle = rows.groupby(["payment_cycle", "account_id"]).post_discount_revenue.sum().groupby("payment_cycle").mean()
    done = rows[rows.status == "COMPLETED"]
    # a dimension filter comes after FIXED: the nested value is over all rows (the marks are the filtered rows')
    _same(_run(con, tiles["include_dim_filter"], "payment_cycle").avg_cycle_avg_account_revenue,
          _view(done, ["payment_cycle"], "mean", ["payment_cycle"], cycle))
    # a context filter comes before FIXED: inner and outer levels are over the rows it keeps
    ctx = done.groupby(["payment_cycle", "account_id"]).post_discount_revenue.sum().groupby("payment_cycle").mean()
    assert not ctx.equals(cycle)
    _same(_run(con, tiles["include_context"], "payment_cycle").avg_cycle_avg_account_revenue, ctx)
    acct = done.groupby("account_id").post_discount_revenue.sum()
    per = _fixed(done, ["account_id", "paper_type"], "sum", ["account_id"], acct)
    _same(_run(con, tiles["exclude_context"], "paper_type").avg_account_revenue_per_paper,
          _view(done, ["paper_type"], "mean", ["account_id", "paper_type"], per))
    assert "where orders.status = 'COMPLETED'" in tiles["include_context"].sql.split("left join")[1]  # the inner grain too


def test_what_stays_todo(bundle, tiles):
    todo = {m.label: m.todo for m in bundle.metrics if m.todo}
    # EXCLUDE of every dimension of the FIXED around it: a value over all rows
    assert "EXCLUDE of every dimension of the FIXED around it" in todo["Cycle Grand Qty"]
    assert "one-row subquery" in tiles["exclude_all_dims"].todo
    # a FIXED of several dimensions has no semantic model: the TODO carries the grain's SQL
    assert "several dimensions (account_id, paper_type)" in todo["Account Revenue per Paper"]
    assert "select d.account_id as account_id, d.paper_type as paper_type, sum(account_id_lod." in todo["Account Revenue per Paper"]
    assert set(todo) == {"Cycle Grand Qty", "Account Revenue per Paper", "Revenue vs Cycle Avg"}
    assert [n for n, t in tiles.items() if t.todo] == ["exclude_all_dims"]


def test_nested_fixed_in_the_semantic_layer(bundle, con, rows):
    """A nested LOD inside a FIXED of one dimension is a grain of its own: a semantic model keyed on that dimension."""
    models = {sm.name: sm for sm in bundle.semantic_models}
    sm = next(m for m in models.values() if any(x.name == "cycle_avg_account_revenue" for x in m.measures))
    assert [e.name for e in sm.entities if e.type == "primary"] == ["payment_cycle"]
    got = dict(con.execute(f"select payment_cycle, cycle_avg_account_revenue from ({sm.sql})").fetchall())
    exp = rows.groupby(["payment_cycle", "account_id"]).post_discount_revenue.sum().groupby("payment_cycle").mean()
    assert got == pytest.approx(exp.to_dict(), rel=1e-9)
    tier = next(m for m in models.values() if any(x.name == "tier_avg_account_qty" for x in m.measures))
    assert [e.name for e in tier.entities if e.type == "primary"] == ["discount_tier"]


def test_nested_lod_with_relationships_stays_todo(tmp_path):
    """On a data source with relationships, Tableau computes a FIXED over the related tables its fields come from."""
    calc = ("<column caption='Cycle Avg Size' datatype='real' name='[Calculation_351]' role='measure' type='quantitative'>"
            "<calculation class='tableau' formula='{FIXED [payment_cycle] : AVG({INCLUDE [account_id] : SUM([business_size])})}' />"
            "</column>")
    b = _parse(tmp_path, REL, lambda x: x.replace("<object-graph>", calc + "<object-graph>", 1))
    todo = {m.label: m.todo for m in b.metrics if m.todo}
    assert "FIXED LOD on a data source with relationships" in todo["Cycle Avg Size"]
