"""Tableau groups, bins and sets (tests/fixtures/tableau_groups/): every sheet runs on the Dunder Mifflin data in DuckDB
and is compared with the same numbers computed in pandas the way Tableau defines them:

- a group maps its members to the group's name; other members keep their value, or with "Include Other" take the Other
  name (NULL too, unless a group holds %null%);
- a bin is floor(value / size) * size, negative values included; NULL stays NULL;
- a set is true for its members: fixed members, members whose condition is true over all of the datasource's rows (NULL
  is out), or the top / bottom N by an aggregate. Top N keeps exactly N: `direction` sorts the members and `end` takes
  the first or last N of that order, NULL ranks below every value, and ties go to the first members in ascending order.
  On a shelf, `io:` shows In / Out, the set itself shows its members; a filter on `io:` keeps true (In) or false (Out).
"""

import shutil
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_groups"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("groups")
    shutil.copytree(FIX, tmp / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp, "tableau"))


@pytest.fixture(scope="module")
def tiles(bundle):
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
def rows():
    duckdb = pytest.importorskip("duckdb")
    o, a, p = (duckdb.sql(f"select * from '{DATA / f'{t}.parquet'}'").df() for t in ("orders", "accounts", "products"))
    df = o.merge(a, on="account_id").merge(p, on="product_id")
    df["revenue"] = df["post_discount_revenue"].astype(float)
    disc = df["discount_percentage"].astype(float)
    df["discount_cycle"] = df["payment_cycle"].where(disc > 0)  # IF [discount_percentage] > 0 THEN [payment_cycle] END
    df["returned_revenue"] = df["revenue"].where(df["status"] == "RETURNED")
    df["days_shifted"] = df["days_to_close"] - 30
    return df


def _run(con, tile) -> list[tuple]:
    assert tile.sql and not tile.todo, tile.todo
    return con.execute(tile.sql).fetchall()


def _same(got: list[tuple], want: list[tuple]) -> None:
    def norm(rs):
        out = [tuple("" if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2) if isinstance(x, (int, float)) or
                     hasattr(x, "as_tuple") else str(x) for x in r) for r in rs]
        return sorted(out, key=lambda r: tuple((isinstance(x, str), x) for x in r))
    assert norm(got) == norm(want)


def _by(df, key: str, value: str = "revenue", how: str = "sum") -> list[tuple]:
    g = df.groupby(key, dropna=False)[value]
    out = g.sum() if how == "sum" else g.nunique()
    return [(None if pd.isna(k) else k, v) for k, v in out.items()]


# ---------------------------------------------------------------- groups


def _group(values, groups: dict, other=None):
    def one(v):
        for name, members in groups.items():
            if (pd.isna(v) and None in members) or (not pd.isna(v) and v in members):
                return name
        return v if other is None else other
    return values.map(one)


@pytest.mark.parametrize("sheet, col, groups, other", [
    ("G Paper Other", "paper_type", {"Office": {"Copy", "Printer"}}, "Other"),
    ("G Paper Keep", "paper_type", {"Office": {"Copy", "Printer"}}, None),
    ("G Cycle Other", "discount_cycle", {"Frequent": {"WEEK", "BIWEEK"}}, "Other"),  # NULL goes to Other
    ("G Cycle Keep", "discount_cycle", {"Frequent": {"WEEK", "BIWEEK"}}, None),  # NULL and '' stay
    ("G Cycle Null", "discount_cycle", {"Unknown": {None, ""}}, None),  # %null% groups NULL
])
def test_groups(con, tiles, rows, sheet, col, groups, other):
    df = rows.assign(g=_group(rows[col], groups, other))
    want = [(k, r, n) for (k, r), (_, n) in zip(_by(df, "g"), _by(df, "g", "order_id", "countd"))]
    _same(_run(con, tiles[sheet]), want)


def test_group_sql(bundle):
    dims = {d.name: d for sm in bundle.semantic_models for d in sm.dimensions}
    assert dims["paper_other"].expr == "case when (paper_type = 'Copy' or paper_type = 'Printer') then 'Office' else 'Other' end"
    assert dims["cycle_null"].expr.startswith("case when (((case when discount_percentage > 0 then payment_cycle end) is null) or ")


# ---------------------------------------------------------------- bins


@pytest.mark.parametrize("sheet, col, size", [
    ("B Days Bin", "days_to_close", 10),
    ("B Days Shifted Bin", "days_shifted", 7),  # a calc with negative values: -30 goes in the bin at -35
    ("B Returned Bin", "returned_revenue", 100000),  # NULL stays NULL
])
def test_bins(con, tiles, rows, sheet, col, size):
    df = rows.assign(b=np.floor(rows[col].astype(float) / size) * size)
    assert sheet != "B Days Shifted Bin" or df["b"].min() < 0
    _same(_run(con, tiles[sheet]), _by(df, "b", "order_id", "countd"))


@pytest.mark.parametrize("sheet, words", [
    ("B Discount Bin", "fractional size"),
    ("B Days Peg Bin", "peg 5"),
    ("B Days Param Bin", "[Parameters].[Bin Size]"),
])
def test_bins_left_todo(tiles, sheet, words):
    assert words in tiles[sheet].todo and not tiles[sheet].sql


# ---------------------------------------------------------------- sets


def _top(df, level: str, value, n: int, desc: bool) -> set:
    """The members Tableau keeps: `value` (per member, over all rows) sorted, NULL below every value, ties by member."""
    per = df.groupby(level, dropna=False).apply(value, include_groups=False).reset_index(name="v")
    per["null"] = per["v"].isna()
    per["key"] = per[level].fillna("")  # no ties among NULL keys here
    order = per.sort_values(["null", "v", "key"], ascending=[desc, not desc, True])
    return {None if pd.isna(x) else x for x in order[level].head(n)}


def _members(df) -> dict:
    """name -> (level, set of members)."""
    rev = lambda g: g["revenue"].sum()  # noqa: E731
    ret = lambda g: g["returned_revenue"].sum(min_count=1)  # SUM of NULLs is NULL  # noqa: E731
    by_acct = df.groupby("account_name")
    managers = set(df["account_manager"])
    return {
        "Managers Fixed": ("account_manager", {"Dwight Schrute", "Creed Patton", "Pam Beesly"}),
        "Managers Except": ("account_manager", managers - {"Dwight Schrute"}),
        "Cycle Set": ("discount_cycle", {None, "WEEK"}),
        "Big Accounts": ("account_name", {k for k, g in by_acct if rev(g) > 2000000}),
        "Busy Accounts": ("account_name", {k for k, g in by_acct if rev(g) > 1500000 and g["order_id"].nunique() > 20}),
        "Low Returned": ("account_name", {k for k, g in by_acct if (v := ret(g)) == v and v < 1000000}),  # NULL < x is not true
        "Top Managers": ("account_manager", _top(df, "account_manager", lambda g: g["account_id"].nunique(), 4, True)),
        "Bottom Managers": ("account_manager", _top(df, "account_manager", lambda g: g["account_id"].nunique(), 3, False)),
        "Top Returned": ("account_name", _top(df, "account_name", ret, 120, True)),
        "Bottom Returned": ("account_name", _top(df, "account_name", ret, 10, False)),
        "Asc Top Revenue": ("account_manager", _top(df, "account_manager", rev, 3, False)),  # ASC sorted, top end: the smallest
        "Top Cycles": ("discount_cycle", _top(df, "discount_cycle", lambda g: g["order_id"].nunique(), 2, True)),
    }


@pytest.fixture(scope="module")
def sets(rows):
    return _members(rows)


def _in(df, level: str, members: set):
    return df[level].map(lambda v: (None if pd.isna(v) else v) in members)


def test_set_rules_have_the_edge_cases(rows, sets):
    by_count = rows.groupby("account_manager")["account_id"].nunique()
    assert sorted(by_count)[-4] == sorted(by_count)[-5]  # a tie at the 4th place: the first of them in ascending order wins
    assert "Andy Bernard" in sets["Top Managers"][1] and "Kevin Malone" not in sets["Top Managers"][1]
    returned = rows.groupby("account_name")["returned_revenue"].sum(min_count=1)
    assert returned.notna().sum() < 120 < len(returned)  # top 120 takes members whose value is NULL, after every value
    assert all(pd.isna(returned[m]) for m in sets["Bottom Returned"][1])  # and bottom N takes them first
    assert None in sets["Top Cycles"][1] and None in sets["Cycle Set"][1]


@pytest.mark.parametrize("name", [
    "Managers Fixed", "Managers Except", "Cycle Set", "Big Accounts", "Busy Accounts", "Low Returned", "Top Managers", "Bottom Managers",
    "Top Returned", "Bottom Returned", "Asc Top Revenue", "Top Cycles"])
def test_sets_on_shelves(con, tiles, rows, sets, name):
    level, members = sets[name]
    df = rows.assign(io=_in(rows, level, members).map({True: "In", False: "Out"}))
    _same(_run(con, tiles[f"S {name}"]), _by(df, "io"))  # In / Out
    per = df.groupby([level, "io"], dropna=False)["revenue"].sum()
    _same(_run(con, tiles[f"SM {name}"]), [(None if pd.isna(k) else k, io, v) for (k, io), v in per.items()])  # each member
    inside = df[df["io"] == "In"]
    _same(_run(con, tiles[f"SD {name}"]), _by(inside, level))  # the set itself: its members


@pytest.mark.parametrize("name", ["Top Managers", "Big Accounts", "Managers Except"])
@pytest.mark.parametrize("kind", ["true", "false", "except"])
def test_set_filters(con, tiles, rows, sets, name, kind):
    level, members = sets[name]
    keep = _in(rows, level, members)
    df = rows[keep if kind == "true" else ~keep]
    _same(_run(con, tiles[f"F {name} {kind}"]), _by(df, "paper_type"))


def test_set_sql(bundle):
    lod = {sm.name: sm for sm in bundle.semantic_models}["account_manager_lod"]
    assert ("row_number() over (order by count(distinct orders.account_id) asc nulls first, accounts.account_manager) <= 3 as bottom_managers"
            in lod.sql)
    dims = {d.name: d for d in lod.dimensions}
    assert dims["top_managers"].todo == "" and dims["asc_top_revenue"].todo == ""
    assert {d.name: d for d in {sm.name: sm for sm in bundle.semantic_models}["account_name_lod"].dimensions}["big_accounts"].expr == (
        "case when sum_post_discount_revenue > 2000000 then true else false end")


def test_sets_left_todo(tiles):
    assert "[Parameters].[Top N]" in tiles["S Param Top"].todo  # N from a parameter
    assert "several dimensions" in tiles["S Pairs"].todo
