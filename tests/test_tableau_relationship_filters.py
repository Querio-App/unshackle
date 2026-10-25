"""Filters and top N on Tableau sheets over related tables (tests/fixtures/tableau_relationship_filters/, see its README).

The tiles' SQL runs on the Dunder Mifflin data in DuckDB and is compared with numbers computed here in pandas from
Tableau's rule for relationships, written independently of the SQL:
- each measure is aggregated on its own table's rows: a row counts once for each distinct combination of the sheet's
  dimensions it relates to (left joins), among the combinations that pass the filters. A filter on another table's
  field is read through the same left joins, so a row is kept when some row it relates to passes, and a row with no
  match sees NULL;
- the marks are the combinations of the dimensions (their tables and the filtered tables inner joined, filtered) and
  the measures' own; measure filters then keep the marks whose aggregate (of the measure's own table) passes;
- top N and condition filters are computed first, over the related aggregates at their field's level, after the data
  source and context filters only; the sheet then keeps the rows that relate to their members.
"""

import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_relationship_filters"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
# the custom SQL tables: targets (LOST matches no order, RETURNED has no target); tiers (COMPLETED has two)
TARGETS = pd.DataFrame({"target_status": ["COMPLETED", "CANCELLED", "LOST"], "target": [100, 50, 7]})
TIERS = pd.DataFrame({"tier_status": ["COMPLETED", "COMPLETED", "CANCELLED", "LOST"], "tier": ["A", "B", "B", "B"], "tier_points": [1, 2, 4, 8]})
# relationships, all to orders: table -> (orders column, its column)
KEYS = {"accounts": ("account_id", "account_id"), "shippings": ("shipping_id", "shipping_id"),
        "targets": ("status", "target_status"), "tiers": ("status", "tier_status")}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("relf")
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
def tables():
    duckdb = pytest.importorskip("duckdb")
    out = {n: duckdb.sql(f"select * from '{DATA / f'{n}.parquet'}'").df() for n in ("orders", "accounts", "shippings")}
    out.update(targets=TARGETS, tiers=TIERS)
    return out


# ---------------------------------------------------------------- Tableau's rule, in pandas

@dataclass
class Row:
    """A filter on rows: `test` on the values of `cols` ("table.column")."""
    cols: list[str]
    test: Callable[[pd.DataFrame], pd.Series]
    before: bool = False  # data source and context filters


@dataclass
class Agg:
    """A measure filter: SUM(col) of its own table per mark, kept when `test` holds."""
    col: str
    test: Callable[[pd.Series], pd.Series]


@dataclass
class Top:
    """Top (bottom) N members of `field` by SUM(by)."""
    field: str
    n: int
    by: str
    bottom: bool = False


@dataclass
class Cond:
    """The members of `field` whose SUM(by) passes `test`."""
    field: str
    by: str
    test: Callable[[pd.Series], pd.Series]


@dataclass
class Sheet:
    dims: list[str]
    measures: list[str]  # SUM of each
    filters: list = field(default_factory=list)


def home(col: str) -> str:
    return col.split(".")[0]


class Rule:
    def __init__(self, t: dict[str, pd.DataFrame]):
        self.t = t

    def table(self, name: str) -> pd.DataFrame:
        df = self.t[name].add_prefix(name + ".")
        df[f"{name}._row"] = range(len(df))
        return df

    def reach(self, root: str, tabs: set[str]) -> pd.DataFrame:
        """Each row of `root` with the rows of `tabs` it relates to (left joins: NULLs when nothing matches)."""
        df = self.table(root)
        if not tabs - {root}:
            return df
        if root != "orders":
            o, x = KEYS[root]
            df = df.merge(self.table("orders"), left_on=f"{root}.{x}", right_on=f"orders.{o}", how="left")
        for tb in sorted(tabs - {root, "orders"}):
            o, x = KEYS[tb]
            df = df.merge(self.table(tb), left_on=f"orders.{o}", right_on=f"{tb}.{x}", how="left")
        return df

    def measure(self, value: str, dims: list[str], rows: list[Row]) -> pd.DataFrame | float:
        """SUM(value) per combination of dims: each row of its table once per distinct combination it relates to, among
        those that pass the filters."""
        t = home(value)
        w = self.reach(t, {home(c) for c in dims + [c for f in rows for c in f.cols]})
        for f in rows:
            w = w[f.test(w)]
        pairs = w[[f"{t}._row"] + dims].drop_duplicates()
        got = self.table(t)[[f"{t}._row", value]].merge(pairs, on=f"{t}._row")
        if not dims:
            return got[value].sum(min_count=1)
        return got.groupby(dims, dropna=False)[value].sum(min_count=1).reset_index()

    def domain(self, dims: list[str], rows: list[Row]) -> pd.DataFrame:
        """The dimensions' combinations: their tables and the filtered tables inner joined, then filtered."""
        tabs = {home(c) for c in dims + [c for f in rows for c in f.cols]}
        w = self.table(next(iter(tabs))) if len(tabs) == 1 else self.table("orders")
        for tb in sorted(tabs - {"orders"}) if len(tabs) > 1 else []:
            o, x = KEYS[tb]
            w = w.merge(self.table(tb), left_on=f"orders.{o}", right_on=f"{tb}.{x}")
        for f in rows:
            w = w[f.test(w)]
        return w[dims].drop_duplicates()

    def members(self, f: Top | Cond, before: list[Row]) -> Row:
        if isinstance(f, Cond):
            m = self.measure(f.by, [f.field], before)
            keep = m[f.test(m[f.by])][f.field]
        else:  # the field's members (its own table's values), ranked; ties go to the first member in ascending order
            d = self.domain([f.field], before).merge(self.measure(f.by, [f.field], before), on=f.field, how="left")
            d = d.sort_values([f.by, f.field], ascending=[f.bottom, True], na_position="last" if not f.bottom else "first", kind="stable")
            keep = d[f.field].head(f.n)
        has_null, vals = keep.isna().any(), set(keep.dropna())
        return Row([f.field], lambda w, c=f.field: w[c].isin(vals) | (w[c].isna() & has_null))

    def sheet(self, s: Sheet, ds_filters: list[Row] = ()) -> dict[tuple, tuple]:
        flts = list(ds_filters) + s.filters
        before = [f for f in flts if isinstance(f, Row) and f.before]
        rows = [f if isinstance(f, Row) else self.members(f, before) for f in flts if not isinstance(f, Agg)]
        aggs = [f for f in flts if isinstance(f, Agg)]
        values = [self.measure(m, s.dims, rows) for m in s.measures]
        tests = [(self.measure(f.col, s.dims, rows), f) for f in aggs]
        if not s.dims:
            if not all(f.test(pd.Series([v]))[0] for v, f in tests):
                return {}
            return {(): tuple(None if pd.isna(v) else float(v) for v in values)}
        marks = pd.concat([self.domain(s.dims, rows)] + [v[s.dims] for v in values]).drop_duplicates()
        for v in values:
            marks = marks.merge(v, on=s.dims, how="left")
        for v, f in tests:
            v = v.rename(columns={f.col: "filter_value"})
            m = marks.merge(v, on=s.dims, how="left")
            marks = marks[f.test(m["filter_value"]).fillna(False).to_numpy()]
        return _as_dict(marks, len(s.dims))


def _key(v):
    return "\0NULL" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)


def _as_dict(df: pd.DataFrame, ndims: int) -> dict[tuple, tuple]:
    out = {}
    for r in df.itertuples(index=False):
        out[tuple(_key(v) for v in r[:ndims])] = tuple(None if pd.isna(v) else float(v) for v in r[ndims:])
    return out


def _same(got: dict, want: dict) -> bool:
    return got.keys() == want.keys() and all(
        all((a is None and b is None) or (a is not None and b is not None and a == pytest.approx(b, rel=1e-9)) for a, b in zip(got[k], want[k]))
        for k in want)


# ---------------------------------------------------------------- the fixture's sheets

ST, CY, MODE, TS, TIER = "orders.status", "accounts.payment_cycle", "shippings.shipping_mode", "targets.target_status", "tiers.tier"
REV, BIZ, COST, TGT, PTS = "orders.post_discount_revenue", "accounts.business_size", "shippings.shipping_cost", "targets.target", "tiers.tier_points"


def isin(col: str, *vals, before: bool = False) -> Row:
    return Row([col], lambda w: w[col].isin(vals), before)


def notin(col: str, *vals) -> Row:  # Tableau keeps NULL rows when it excludes values
    return Row([col], lambda w: ~w[col].isin(vals) | w[col].isna())


def isnull(col: str) -> Row:
    return Row([col], lambda w: w[col].isna())


def at_least(col: str, x: float) -> Agg:
    return Agg(col, lambda v: v >= x)


SHEETS = {
    # dimension filters on each table's fields
    "Business Size by Status, Annual Accounts": Sheet([ST], [BIZ], [isin(CY, "ANNUAL")]),
    "Business Size by Status, Express Shipping": Sheet([ST], [BIZ], [isin(MODE, "EXPRESS")]),
    "Business Size by Status, Not Returned": Sheet([ST], [BIZ], [notin(ST, "RETURNED")]),
    "Revenue by Payment Cycle, Completed": Sheet([CY], [REV], [isin(ST, "COMPLETED")]),
    "Revenue by Payment Cycle, Not Express": Sheet([CY], [REV], [notin(MODE, "EXPRESS")]),
    "Revenue by Payment Cycle, Express or Regular": Sheet([CY], [REV], [isin(MODE, "EXPRESS", "REGULAR")]),
    "Total Business Size, Returned": Sheet([], [BIZ], [isin(ST, "RETURNED")]),
    "Revenue by Status, Weekly Accounts": Sheet([ST], [REV], [isin(CY, "WEEK")]),
    "Business Size by Status, No Shipping Mode": Sheet([ST], [BIZ], [isnull(MODE)]),
    "Target by Status, Completed or Returned": Sheet([ST], [TGT], [isin(ST, "COMPLETED", "RETURNED")]),
    "Revenue by Status, Completed Target": Sheet([ST], [REV], [isin(TS, "COMPLETED")]),
    "Revenue by Target Status, Not Cancelled": Sheet([TS], [REV], [notin(ST, "CANCELLED")]),
    "Revenue and Shipping Cost by Payment Cycle, Completed Regular": Sheet([CY], [REV, COST], [isin(ST, "COMPLETED"), isin(MODE, "REGULAR")]),
    "Status by Shipping Mode, Biweekly Accounts": Sheet([ST, MODE], [], [isin(CY, "BIWEEK")]),
    "Revenue by Status, Business Size 500 to 2000": Sheet([ST], [REV], [Row([BIZ], lambda w: w[BIZ].between(500, 2000))]),
    "Business Size by Payment Cycle, Completed Target": Sheet([CY], [BIZ], [isin(TS, "COMPLETED")]),
    "Business Size by Status, Six Months to June 2026": Sheet([ST], [BIZ], [Row(["orders.purchased_at"], lambda w: (
        pd.to_datetime(w["orders.purchased_at"]) >= "2026-01-01") & (pd.to_datetime(w["orders.purchased_at"]) < "2026-07-01"))]),
    "Revenue by Status, Tier B": Sheet([ST], [REV], [isin(TIER, "B")]),
    "Revenue by Status, Cycle Contains WEEK": Sheet([ST], [REV], [Row([CY], lambda w: w[CY].str.contains("WEEK", regex=False, na=False))]),
    # measure filters
    "Revenue and Business Size by Status, Business Size over 110000": Sheet([ST], [REV, BIZ], [at_least(BIZ, 110000)]),
    "Business Size by Status, Annual, over 60000": Sheet([ST], [BIZ], [isin(CY, "ANNUAL"), at_least(BIZ, 60000)]),
    "Revenue by Payment Cycle, Shipping Cost at least 25000": Sheet([CY], [REV], [at_least(COST, 25000)]),
    "Status, Business Size over 120000": Sheet([ST], [], [at_least(BIZ, 120000)]),
    "Total Revenue, Business Size at least 100000": Sheet([], [REV], [at_least(BIZ, 100000)]),
    # top N and conditions
    "Revenue by Status, Top 2 by Business Size": Sheet([ST], [REV], [Top(ST, 2, BIZ)]),
    "Revenue by Payment Cycle, Top 2 by Revenue, Completed": Sheet([CY], [REV], [Top(CY, 2, REV), isin(ST, "COMPLETED")]),
    "Business Size by Payment Cycle, Bottom 2 by Shipping Cost": Sheet([CY], [BIZ], [Top(CY, 2, COST, bottom=True)]),
    "Revenue by Payment Cycle, Shipping Cost over 20000": Sheet([CY], [REV], [Cond(CY, COST, lambda v: v > 20000)]),
    "Revenue by Status, Business Size over 120000": Sheet([ST], [REV], [Cond(ST, BIZ, lambda v: v > 120000)]),
    "Revenue by Status, Top 2 Payment Cycles": Sheet([ST], [REV], [Top(CY, 2, REV)]),
    "Revenue by Payment Cycle, Top 2 Statuses by Points": Sheet([CY], [REV], [Top(ST, 2, PTS)]),
    "Revenue by Payment Cycle, Top 2, Returned in Context": Sheet([CY], [REV], [Top(CY, 2, REV), isin(ST, "RETURNED", before=True)]),
}
DS_FILTERS = {  # data source filters, by sheet name prefix
    "Annual and Weekly: ": [isin(CY, "ANNUAL", "WEEK", before=True)],
    "Tier B: ": [isin(TIER, "B", before=True)],
}
DS_SHEETS = {
    "Annual and Weekly: Revenue by Status": Sheet([ST], [REV]),
    "Annual and Weekly: Business Size by Status": Sheet([ST], [BIZ]),
    "Annual and Weekly: Target by Status": Sheet([ST], [TGT]),
    "Annual and Weekly: Shipping Cost by Shipping Mode": Sheet([MODE], [COST]),
    "Annual and Weekly: Total Target": Sheet([], [TGT]),
    "Annual and Weekly: Revenue by Payment Cycle": Sheet([CY], [REV]),
    "Tier B: Revenue by Status": Sheet([ST], [REV]),
    "Tier B: Business Size by Status": Sheet([ST], [BIZ]),
    "Tier B: Revenue by Status, Top 1 by Revenue": Sheet([ST], [REV], [Top(ST, 1, REV)]),
    "Tier B: Points by Tier": Sheet([TIER], [PTS]),
}


def _tiles(b):
    return {t.title: t for d in b.dashboards for t in d.tiles}


@pytest.mark.parametrize("name", list(SHEETS) + list(DS_SHEETS))
def test_sheet_matches_tableaus_rule(bundle, con, tables, name):
    tile = _tiles(bundle)[name]
    assert tile.sql and not tile.todo, tile.todo
    spec = SHEETS.get(name) or DS_SHEETS[name]
    ds = next((f for p, f in DS_FILTERS.items() if name.startswith(p)), [])
    want = Rule(tables).sheet(spec, ds)
    got = _as_dict(con.sql(tile.sql).df(), len(spec.dims))
    assert _same(got, want), (got, want)


def test_the_rule_is_not_a_join(tables):
    """A filter on another table limits the rows that relate to a passing row, and counts each once: not a join's rows."""
    r = Rule(tables)
    o, a = tables["orders"], tables["accounts"]
    got = r.sheet(SHEETS["Business Size by Status, Annual Accounts"])
    joined = a[a.payment_cycle == "ANNUAL"].merge(o, on="account_id").groupby("status").business_size.sum()
    assert got[("COMPLETED",)][0] < joined["COMPLETED"]
    # orders with no shipping row: kept when a shipping mode is excluded (NULL is not excluded), dropped when modes are listed
    sh = tables["shippings"]
    unshipped = o[~o.shipping_id.isin(sh.shipping_id)].post_discount_revenue.sum()
    shipped = o.merge(sh, on="shipping_id")
    kept = sum(v[0] for v in r.sheet(SHEETS["Revenue by Payment Cycle, Not Express"]).values())
    listed = sum(v[0] for v in r.sheet(SHEETS["Revenue by Payment Cycle, Express or Regular"]).values())
    assert unshipped > 0 and kept == pytest.approx(shipped[shipped.shipping_mode != "EXPRESS"].post_discount_revenue.sum() + unshipped)
    assert listed == pytest.approx(shipped[shipped.shipping_mode.isin(["EXPRESS", "REGULAR"])].post_discount_revenue.sum())
    # the target no order has (LOST) is not a mark once orders are filtered; RETURNED orders (no target) under NULL
    assert set(r.sheet(SHEETS["Revenue by Target Status, Not Cancelled"])) == {("\0NULL",), ("COMPLETED",)}
    # duplicate keys: COMPLETED has tiers A and B, and tier B keeps its orders
    assert set(r.sheet(DS_SHEETS["Tier B: Revenue by Status"], DS_FILTERS["Tier B: "])) == {("CANCELLED",), ("COMPLETED",)}
    # top N ignores the sheet's dimension filters, and ranks within the context
    assert set(r.sheet(SHEETS["Revenue by Payment Cycle, Top 2 by Revenue, Completed"])) == {("ANNUAL",), ("BIWEEK",)}
    assert set(r.sheet(SHEETS["Revenue by Payment Cycle, Top 2, Returned in Context"])) == {("BIWEEK",), ("WEEK",)}


def test_what_stays_todo(bundle, tmp_path):
    tile = _tiles(bundle)["Revenue by Status, Top 2 in Context"]
    assert not tile.sql and "in the context of a sheet over related tables" in tile.todo

    calc = ("<column caption='Revenue per Account Size' datatype='real' name='[Calculation_201]' role='measure' type='quantitative'>"
            "<calculation class='tableau' formula='SUM([post_discount_revenue]) / SUM([business_size])' /></column>")

    def edit(x: str) -> str:
        x = x.replace("      <object-graph>", calc + "\n      <object-graph>", 1)
        sheet = x.index('<worksheet name="Revenue by Status, Top 2 by Business Size">')
        x = x[:sheet] + x[sheet:].replace('expression="SUM([business_size])"', 'expression="SUM([post_discount_revenue]) / SUM([business_size])"', 1)
        sheet = x.index('<worksheet name="Business Size by Status, Annual Accounts">')
        x = x[:sheet] + x[sheet:].replace("<aggregation value='true' />", (
            "<column caption='Revenue per Account Size' datatype='real' name='[Calculation_201]' role='measure' type='quantitative'>"
            "<calculation class='tableau' formula='SUM([post_discount_revenue]) / SUM([business_size])' /></column>"
            "<column-instance column='[Calculation_201]' derivation='User' name='[usr:Calculation_201:qk]' pivot='key' type='quantitative' />"
            "<filter class='quantitative' column='[federated.rel0filt].[usr:Calculation_201:qk]' included-values='in-range'><min>1</min></filter>"
            "<aggregation value='true' />"), 1)
        # an extract with a filter, on the tier B data source
        tier = x.index("name='federated.rel0tier'")
        end = x.index("</object-graph>", tier) + len("</object-graph>")
        return x[:end] + ("<extract count='-1' enabled='true' units='records'><connection class='hyper' dbname='Data/Extracts/rel.hyper' "
                          "schema='Extract' tablename='Extract'><relation name='Extract' table='[Extract].[Extract]' type='table' /></connection>"
                          "<filter class='quantitative' column='[tier_points]' included-values='in-range'><min>2</min></filter></extract>") + x[end:]

    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    (twb,) = (tmp_path / "migration" / "raw" / "tableau" / "workbooks").glob("*.twb")
    twb.write_text(edit(twb.read_text()))
    tiles = _tiles(get_source("tableau").parse(RawStore(tmp_path, "tableau")))
    assert "its aggregate reads accounts, orders" in tiles["Revenue by Status, Top 2 by Business Size"].todo
    assert "filter on [Revenue per Account Size] not applied" in tiles["Business Size by Status, Annual Accounts"].todo
    assert "extract filter on [tier_points] of a data source with relationships" in tiles["Tier B: Revenue by Status"].todo
