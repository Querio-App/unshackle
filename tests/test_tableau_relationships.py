"""Tableau relationships (tests/fixtures/tableau_relationships/, see its README).

With relationships (the logical layer), Tableau doesn't join the tables. The tiles' SQL runs on the Dunder Mifflin data
in DuckDB and is compared with numbers computed in pandas from Tableau's rule:
- each measure is aggregated on its own table's rows at the sheet's dimensions; a row counts once for each distinct
  combination of dimensions it relates to (through left joins, so a row with no match counts under NULL dimensions);
- the combinations of the dimensions themselves (their tables inner joined) are marks too, with no value for a measure
  when no row relates to them; a sheet without measures shows just those;
- a sheet over one table queries that table alone.
"""

import math
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_relationships"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
TARGETS = pd.DataFrame({"target_status": ["COMPLETED", "CANCELLED", "LOST"], "target": [100, 50, 7]})  # the custom SQL table


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rel")
    shutil.copytree(FIX, tmp / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp, "tableau"))


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "shippings"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


@pytest.fixture(scope="module")
def t():
    duckdb = pytest.importorskip("duckdb")
    out = {n: duckdb.sql(f"select * from '{DATA / f'{n}.parquet'}'").df() for n in ("orders", "accounts", "shippings")}
    out["targets"] = TARGETS
    return out


def _tiles(b):
    return {t.title: t for d in b.dashboards for t in d.tiles}


def _key(v):
    return "\0NULL" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)


def _rows(con, sql: str, ndims: int = 1) -> dict[tuple, tuple]:
    """{dimension values: measure values} of a tile (dimensions first), NULL dimensions as a marker, numbers as floats."""
    return _as_dict(con.sql(sql).df(), ndims)


def _as_dict(df: pd.DataFrame, ndims: int) -> dict[tuple, tuple]:
    """{dimension values: measure values} of a frame whose first `ndims` columns are the dimensions."""
    out = {}
    for r in df.itertuples(index=False):
        out[tuple(_key(v) for v in r[:ndims])] = tuple(None if pd.isna(v) else float(v) for v in r[ndims:])
    return out


def _same(got: dict, want: dict) -> bool:
    return got.keys() == want.keys() and all(
        all((a is None and b is None) or (a is not None and b is not None and a == pytest.approx(b, rel=1e-9)) for a, b in zip(got[k], want[k]))
        for k in want)


def per_table(table: pd.DataFrame, rowid: str, related: pd.DataFrame, dims: list[str], value: str, agg: str) -> pd.DataFrame:
    """Tableau's rule for one measure: `related` holds, for each row of `table` (by `rowid`), the dimension values it
    reaches through left joins; each row counts once per distinct combination."""
    pairs = related[[rowid] + dims].drop_duplicates()
    rows = table[[rowid, value]].merge(pairs, on=rowid, how="inner")
    return rows.groupby(dims, dropna=False)[value].agg(agg).reset_index()


def marks(domain: pd.DataFrame, dims: list[str], *measures: pd.DataFrame) -> pd.DataFrame:
    """Dimension combinations (the domain and every measure's), each measure joined on them (NULL matches NULL)."""
    keys = pd.concat([domain[dims]] + [m[dims] for m in measures]).drop_duplicates()
    out = keys
    for m in measures:
        out = out.merge(m, on=dims, how="left")
    return out


def test_relationships_become_foreign_entities(bundle):
    models = {sm.name: sm for sm in bundle.semantic_models}
    assert [(e.name, e.type, e.expr) for e in models["orders"].entities] == [
        ("orders", "primary", "order_id"), ("accounts", "foreign", "account_id"), ("shippings", "foreign", "shipping_id")]
    ent = next(e for e in models["orders"].entities if e.name == "accounts")
    assert ent.join_type == "" and ent.extensions["cardinality"].startswith("many-to-many")
    assert "a relationship, not a join" in models["accounts"].description
    # orders.status = targets.target_status: neither side is a key, so no Ossie relationship; said so
    assert any("orders - targets" in w.message for w in bundle.warnings)


def test_ossie_relationship_notes_the_tableau_relationship(bundle, tmp_path):
    from unshackle.target import ossie
    from unshackle.target.layout import Layout

    files = ossie.render(bundle, Layout.load(tmp_path))
    doc = yaml.safe_load(next(v for k, v in files.items() if k.endswith("orders.ossie.yaml")))
    rel = next(r for r in doc["relationships"] if r["to"] == "accounts")
    assert rel["from_columns"] == ["account_id"] and rel["to_columns"] == ["account_id"]
    assert "tableau_relationship" in rel["custom_extensions"][0]["data"]


def test_one_table_sheets_query_that_table(bundle, con, t):
    tiles = _tiles(bundle)
    assert con.sql(tiles["Total Business Size"].sql).fetchall() == [(t["accounts"].business_size.sum(),)]  # once per account
    assert "measures_" not in tiles["Revenue by Status"].sql


def test_measure_of_the_one_side_is_not_repeated(bundle, con, t):
    """SUM and AVG of accounts.business_size by order status: each account once per status it has orders in, not once per order."""
    o, a = t["orders"], t["accounts"]
    related = a[["account_id"]].merge(o[["account_id", "status"]], on="account_id", how="left")
    domain = o[["status"]].drop_duplicates()
    tiles = _tiles(bundle)
    for name, agg in (("Business Size by Status", "sum"), ("Avg Business Size by Status", "mean")):
        want = marks(domain, ["status"], per_table(a, "account_id", related, ["status"], "business_size", agg))
        assert _same(_rows(con, tiles[name].sql), _as_dict(want, 1)), name
        joined = a.merge(o, on="account_id").groupby("status").business_size.agg(agg)
        if agg == "sum":
            assert joined["COMPLETED"] > want.set_index("status").business_size["COMPLETED"]  # what a join would show


def test_unmatched_rows_on_both_sides(bundle, con, t):
    o, tg = t["orders"], t["targets"]
    tiles = _tiles(bundle)
    # targets by order status: the target no order has (LOST) under a NULL status; RETURNED (no target) shown empty
    related = tg[["target_status"]].merge(o[["status"]].drop_duplicates(), left_on="target_status", right_on="status", how="left")
    want = marks(o[["status"]].drop_duplicates(), ["status"], per_table(tg, "target_status", related, ["status"], "target", "sum"))
    got = _rows(con, tiles["Target by Status"].sql)
    assert _same(got, _as_dict(want, 1))
    assert got[("\0NULL",)] == (7.0,) and got[("RETURNED",)] == (None,)
    # revenue by target status: RETURNED revenue under a NULL target status, LOST with no revenue
    related = o[["order_id", "status"]].merge(tg[["target_status"]], left_on="status", right_on="target_status", how="left")
    want = marks(tg[["target_status"]], ["target_status"], per_table(o, "order_id", related, ["target_status"], "post_discount_revenue", "sum"))
    got = _rows(con, tiles["Revenue by Target Status"].sql)
    assert _same(got, _as_dict(want, 1)) and got[("LOST",)] == (None,)
    # orders with no shipping row: under a NULL shipping mode
    s = t["shippings"]
    related = o[["order_id", "shipping_id"]].merge(s[["shipping_id", "shipping_mode"]], on="shipping_id", how="left")
    want = marks(s[["shipping_mode"]].drop_duplicates(), ["shipping_mode"],
                 per_table(o, "order_id", related, ["shipping_mode"], "post_discount_revenue", "sum"))
    got = _rows(con, tiles["Revenue by Shipping Mode"].sql)
    assert _same(got, _as_dict(want, 1)) and ("\0NULL",) in got


def test_measures_from_several_tables(bundle, con, t):
    o, a, s, tg = t["orders"], t["accounts"], t["shippings"], t["targets"]
    tiles = _tiles(bundle)
    # revenue (orders) and shipping cost (shippings, two relationships away) by payment cycle (accounts)
    rev = per_table(o, "order_id", o[["order_id", "account_id"]].merge(a, on="account_id", how="left"), ["payment_cycle"], "post_discount_revenue", "sum")
    rel_s = s[["shipping_id"]].merge(o[["shipping_id", "account_id"]], on="shipping_id", how="left").merge(a, on="account_id", how="left")
    cost = per_table(s, "shipping_id", rel_s, ["payment_cycle"], "shipping_cost", "sum")
    want = marks(a[["payment_cycle"]].drop_duplicates(), ["payment_cycle"], rev, cost)
    assert _same(_rows(con, tiles["Revenue and Shipping Cost by Payment Cycle"].sql), _as_dict(want, 1))
    # revenue (orders) and target (targets) by status
    related = tg[["target_status"]].merge(o[["status"]].drop_duplicates(), left_on="target_status", right_on="status", how="left")
    want = marks(o[["status"]].drop_duplicates(), ["status"], o.groupby("status").post_discount_revenue.sum().reset_index(),
                 per_table(tg, "target_status", related, ["status"], "target", "sum"))
    assert _same(_rows(con, tiles["Revenue and Target by Status"].sql), _as_dict(want, 1))
    # SUM(revenue) / SUM(business size): each aggregate on its own table, then divided per status
    related = a[["account_id"]].merge(o[["account_id", "status"]], on="account_id", how="left")
    size = per_table(a, "account_id", related, ["status"], "business_size", "sum")
    both = o.groupby("status").post_discount_revenue.sum().reset_index().merge(size, on="status")
    both["ratio"] = both.post_discount_revenue / both.business_size
    assert _same(_rows(con, tiles["Revenue per Account Size by Status"].sql), _as_dict(both[["status", "ratio"]], 1))


def test_dimensions_from_several_tables(bundle, con, t):
    o, a, s = t["orders"], t["accounts"], t["shippings"]
    tiles = _tiles(bundle)
    # dimensions only: the combinations that exist (inner join)
    want = o.merge(s, on="shipping_id")[["status", "shipping_mode"]].drop_duplicates()
    got = con.sql(tiles["Status by Shipping Mode"].sql).fetchall()
    assert sorted(got) == sorted(want.itertuples(index=False, name=None))
    # a measure of the dimension's own table and another's: orders with no shipping row under a NULL shipping mode
    related = o[["order_id", "status", "shipping_id"]].merge(s[["shipping_id", "shipping_mode"]], on="shipping_id", how="left")
    want = marks(want, ["status", "shipping_mode"], per_table(o, "order_id", related, ["status", "shipping_mode"], "post_discount_revenue", "sum"))
    assert _same(_rows(con, tiles["Revenue by Status and Shipping Mode"].sql, 2), _as_dict(want, 2))
    # shipping cost by status and payment cycle: shippings -> orders -> accounts
    domain = o.merge(a, on="account_id")[["status", "payment_cycle"]].drop_duplicates()
    rel_s = s[["shipping_id"]].merge(o[["shipping_id", "status", "account_id"]], on="shipping_id", how="left").merge(a, on="account_id", how="left")
    want = marks(domain, ["status", "payment_cycle"], per_table(s, "shipping_id", rel_s, ["status", "payment_cycle"], "shipping_cost", "sum"))
    assert _same(_rows(con, tiles["Shipping Cost by Status and Payment Cycle"].sql, 2), _as_dict(want, 2))
    # business size (accounts) by shipping mode (shippings), two relationships away: each account once per mode
    rel_a = a[["account_id"]].merge(o[["account_id", "shipping_id"]], on="account_id", how="left").merge(s, on="shipping_id", how="left")
    want = marks(s[["shipping_mode"]].drop_duplicates(), ["shipping_mode"], per_table(a, "account_id", rel_a, ["shipping_mode"], "business_size", "sum"))
    assert _same(_rows(con, tiles["Business Size by Shipping Mode"].sql), _as_dict(want, 1))


def test_every_sheet_has_sql(bundle):
    assert {name: tile.todo for name, tile in _tiles(bundle).items() if not tile.sql or tile.todo} == {}


def _edited(tmp_path: Path, edit):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    (twb,) = (tmp_path / "migration" / "raw" / "tableau" / "workbooks").glob("*.twb")
    twb.write_text(edit(twb.read_text()))
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def test_what_stays_todo(tmp_path):
    flt = ("<filter class='categorical' column='[federated.rel0demo].[none:status:nk]'><groupfilter function='member' "
           "level='[none:status:nk]' member='&quot;COMPLETED&quot;' /></filter>")
    calcs = ("<column caption='Revenue x Size' datatype='real' name='[Calculation_301]' role='measure' type='quantitative'>"
             "<calculation class='tableau' formula='[post_discount_revenue] * [business_size]' /></column>"
             "<column caption='Account Size Fixed' datatype='real' name='[Calculation_302]' role='measure' type='quantitative'>"
             "<calculation class='tableau' formula='{FIXED [payment_cycle] : SUM([business_size])}' /></column>")

    def edit(x: str) -> str:
        sheet = x.index('<worksheet name="Business Size by Status">')
        end = x.index("<aggregation value='true' />", sheet)
        x = x[:end] + flt + x[end:]
        return x.replace("<object-graph>", calcs + "<object-graph>", 1)

    b = _edited(tmp_path, edit)
    tile = _tiles(b)["Business Size by Status"]
    assert tile.sql and not tile.todo  # filters on sheets over related tables: tests/test_tableau_relationship_filters.py
    todo = {m.label: m.todo for m in b.metrics if m.todo}
    assert "row-level calculation over fields of related tables (accounts, orders)" in todo["Revenue x Size"]
    assert "FIXED LOD on a data source with relationships" in todo["Account Size Fixed"]
