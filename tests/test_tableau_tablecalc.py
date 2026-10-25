"""Tableau table calculations (tests/fixtures/tableau_tablecalc/): quick table calcs and table-calc formulas as window SQL
over a sheet's marks.

The sheets run on the Dunder Mifflin data in DuckDB. Expected values are computed here with pandas from Tableau's rules:
a table calculation runs on the sheet's marks; its addressing (Compute Using) picks the dimensions it moves along, in
the sheet's order of their members, and the other dimensions partition it. Table (across) moves along Columns, Table
(down) along Rows, Table (across then down) along both, Rows first; a pane is a cell without the innermost dimension of
each shelf; Specific Dimensions lists the fields outermost first, and Restart Every partitions by the fields before its
`level-break`. A crosstab (dimensions on Rows and Columns) is padded first: every row header with every column header,
NULL where there is no data, but only when a calculation partitions by a header and moves along another. Dimensions on the
Marks card come with the headers they occur with: on both sides when they partition, on the side of the headers the
calculation moves along when it moves along them. Quick table calcs are Tableau formulas: running total RUNNING_SUM(x), difference
ZN(x) - LOOKUP(ZN(x), -1), percent difference (ZN(x) - LOOKUP(ZN(x), -1)) / ABS(LOOKUP(ZN(x), -1)), percent from
ZN(x) / LOOKUP(ZN(x), -1), percent of total x / TOTAL(x) (TOTAL recomputes the aggregate over the partition's rows),
rank and percentile ((modified rank - 1) / (n - 1), 1 alone), moving calculations over a frame of marks.
"""

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_tablecalc"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
REV = "revenue"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tablecalc")
    shutil.copytree(FIX, tmp / "migration" / "raw" / "tableau")
    (tmp / "unshackle.toml").write_text('[datasources.warehouse]\ntype = "duckdb"\n')
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
def rows(con):
    o, a, p = (con.sql(f"select * from public.{t}").df() for t in ("orders", "accounts", "products"))
    r = o.merge(a, on="account_id").merge(p, on="product_id")
    r[REV] = r.post_discount_revenue.astype(float)
    r["discount"] = r[REV] - r.pre_discount_revenue.astype(float)
    r["completed"] = r[REV].where(r.status == "COMPLETED")
    r["year"] = pd.to_datetime(r.purchased_at).dt.year
    r["quarter"] = pd.to_datetime(r.purchased_at).dt.quarter
    r["month"] = pd.to_datetime(r.purchased_at).dt.to_period("M").dt.to_timestamp()
    return r


def _run(con, tile) -> pd.DataFrame:
    assert tile.sql, tile.todo
    df = con.sql(tile.sql).df()
    return df.apply(lambda s: s.astype(float) if s.dtype == object and s.map(lambda v: type(v).__name__ == "Decimal").any() else s)


def marks(rows: pd.DataFrame, dims: list[str], **aggs: tuple[str, str]) -> pd.DataFrame:
    """The sheet's marks: one row per combination of its dimensions."""
    return rows.groupby(dims, as_index=False, dropna=False).agg(**aggs)


def pad(m: pd.DataFrame, row_dims: list[str], col_dims: list[str]) -> pd.DataFrame:
    """A crosstab's marks: every row header with every column header."""
    domain = m[row_dims].drop_duplicates().merge(m[col_dims].drop_duplicates(), how="cross")
    return domain.merge(m, on=row_dims + col_dims, how="left")


def along(m: pd.DataFrame, part: list[str], order: list[str], fn) -> pd.Series:
    """fn(series of one partition, in addressing order) for every partition."""
    s = m.sort_values(order) if order else m
    if not part:
        return fn(s).reindex(m.index)
    return s.groupby(part, dropna=False, group_keys=False).apply(fn).reindex(m.index)


def running(col: str, how: str = "sum"):
    def fn(g):
        x = g[col]
        out = x.fillna(0).cumsum() if how == "sum" else x.expanding().mean() if how == "avg" else x.cummin().ffill()
        return out.where(x.notna().cumsum() > 0)
    return fn


def lookup(col: str, n: int = -1):
    return lambda g: g[col].shift(-n)


def diff(col: str):
    return lambda g: g[col].fillna(0) - g[col].fillna(0).shift(1)


def pct_diff(col: str):
    def fn(g):
        cur, prev = g[col].fillna(0), g[col].fillna(0).shift(1)
        return (cur - prev) / prev.abs().replace(0, np.nan)
    return fn


def pct_from(col: str):
    return lambda g: g[col].fillna(0) / g[col].fillna(0).shift(1).replace(0, np.nan)


def index():
    return lambda g: pd.Series(range(1, len(g) + 1), index=g.index, dtype=float)


def check(df: pd.DataFrame, m: pd.DataFrame, keys: dict[str, str], cols: dict[str, pd.Series]):
    """The tile's rows are the marks (keys: tile column -> mark column), with the expected values."""
    assert len(df) == len(m)
    got = df.set_index(list(keys)).sort_index()
    want = m.rename(columns={v: k for k, v in keys.items()}).set_index(list(keys)).sort_index()
    assert list(got.index) == list(want.index)
    for col, expected in cols.items():
        w = expected.set_axis(m.index).to_frame("v").join(m[list(keys.values())]).rename(columns={v: k for k, v in keys.items()})
        w = w.set_index(list(keys)).sort_index()["v"]
        g = got[col].astype(float)
        assert g.isna().tolist() == w.isna().tolist(), col
        assert g[g.notna()].tolist() == pytest.approx(w[w.notna()].tolist(), rel=1e-9), col


GRID = {"status": "status", "paper_type": "paper_type"}


# ------------------------------------------------------------------ addressing


@pytest.mark.parametrize("sheet, part, order", [
    ("Grid Across", ["status"], ["paper_type"]),  # Table (across): along Columns, restarting every row
    ("Grid Down", ["paper_type"], ["status"]),  # Table (down): along Rows
    ("Grid Table", [], ["status", "paper_type"]),  # Table (across then down)
    ("Specific Status then Paper", [], ["status", "paper_type"]),  # Specific Dimensions, outermost first
    ("Specific Paper then Status", [], ["paper_type", "status"]),
    ("Specific Fields by Name", [], ["status", "paper_type"]),  # fields named by column rather than instance
    ("Restart at First Field", [], ["status", "paper_type"]),  # level-break on the first field: no restart
    ("Restart Every Status", ["status"], ["paper_type"]),  # level-break on paper_type: the fields before it partition
    ("Table Columns Only", [], ["status", "paper_type"]),  # no crosstab: nested dimensions on one shelf
    ("Table Rows Only", [], ["status", "paper_type"]),
    ("Table Down Nested", [], ["status", "paper_type"]),
    ("Pane Down", ["status"], ["paper_type"]),  # Pane (down): the innermost row dimension within each pane
])
def test_running_total_addressing(tiles, con, rows, sheet, part, order):
    m = marks(rows, ["status", "paper_type"], revenue=(REV, "sum"))
    check(_run(con, tiles[sheet]), m, GRID, {"running_sum_of_revenue": along(m, part, order, running(REV))})


def test_running_total_along_a_continuous_month(tiles, con, rows):
    m = marks(rows, ["month"], revenue=(REV, "sum"))
    df = _run(con, tiles["Running Total by Month"])
    check(df, m, {"month_of_purchased_at": "month"}, {"running_sum_of_revenue": along(m, [], ["month"], running(REV))})


def test_colour_partitions_the_running_total(tiles, con, rows):
    """Dimensions on the Marks card partition in Table (across): one running total per status."""
    m = marks(rows, ["month", "status"], revenue=(REV, "sum"))
    df = _run(con, tiles["Running Total by Status"])
    check(df, m, {"month_of_purchased_at": "month", "status": "status"},
          {"running_sum_of_revenue": along(m, ["status"], ["month"], running(REV))})


@pytest.mark.parametrize("sheet, rows_, cols_, part, order", [
    ("Pane Down Grid", ["status", "paper_type"], ["year"], ["status", "year"], ["paper_type"]),
    ("Pane Across", ["status"], ["year", "quarter"], ["status", "year"], ["quarter"]),
    ("Pane Across then Down", ["status", "paper_type"], ["year", "quarter"], ["status", "year"], ["paper_type", "quarter"]),
])
def test_pane_addressing_on_padded_crosstabs(tiles, con, rows, sheet, rows_, cols_, part, order):
    m = pad(marks(rows, rows_ + cols_, revenue=(REV, "sum")), rows_, cols_)
    keys = {"status": "status", "paper_type": "paper_type", "year_of_purchased_at": "year", "quarter_of_purchased_at": "quarter"}
    keys = {k: v for k, v in keys.items() if v in rows_ + cols_}
    check(_run(con, tiles[sheet]), m, keys, {"running_sum_of_revenue": along(m, part, order, running(REV))})


def test_cell_leaves_the_crosstab_unpadded(tiles, con, rows):
    m = marks(rows, ["status", "paper_type", "year"], revenue=(REV, "sum"))
    df = _run(con, tiles["Cell"])
    assert len(m) < 45  # RETURNED has no 2024 orders of some paper types: not padded, as nothing moves along them
    check(df, m, {"status": "status", "paper_type": "paper_type", "year_of_purchased_at": "year"}, {"running_sum_of_revenue": m[REV]})


def test_default_addressing_is_table_across(tiles, con, rows):
    m = marks(rows, ["status", "year"], revenue=(REV, "sum"))
    keys = {"status": "status", "year_of_purchased_at": "year"}
    check(_run(con, tiles["Default Addressing"]), m, keys, {"index_default": along(m, ["status"], ["year"], index())})
    # the calculation's own default (in <calculation>) applies when the sheet sets none: Table (down)
    over_three = along(m, ["year"], ["status"], index()).map(lambda i: np.floor(i / 3 - 0.6 + 0.5) + 1)
    check(_run(con, tiles["Rank Over Three"]), m, keys, {"rank_over_three": over_three})


def test_sparse_crosstab_is_padded(tiles, con, rows):
    """Account managers by payment cycle: some have none of a cycle. Tableau pads every combination; the running total
    carries over a padded mark, INDEX counts it and the difference reads it as zero."""
    m = pad(marks(rows, ["account_manager", "payment_cycle"], revenue=(REV, "sum")), ["account_manager"], ["payment_cycle"])
    assert len(m) == 70 and m[REV].isna().sum() == 9
    keys = {"account_manager": "account_manager", "payment_cycle": "payment_cycle"}
    across = (["account_manager"], ["payment_cycle"])
    check(_run(con, tiles["Sparse Grid"]), m, keys, {
        "running_sum_of_revenue": along(m, *across, running(REV)),
        "difference_in_revenue": along(m, *across, diff(REV)),
        "row_index": along(m, *across, index()),
    })
    down = (["payment_cycle"], ["account_manager"])
    check(_run(con, tiles["Sparse Grid Down"]), m, keys, {
        "running_sum_of_revenue": along(m, *down, running(REV)),
        "row_index": along(m, *down, index()),
    })


def test_sheet_sorts_order_the_calculation(tiles, con, rows):
    m = marks(rows, ["paper_type"], revenue=(REV, "sum"))
    manual = m.paper_type.map({"Printer": 0, "Copy": 1, "Cardstock": 2}).fillna(3)
    m["manual"] = list(zip(manual, m.paper_type))
    check(_run(con, tiles["Running by Manual Sort"]), m, {"paper_type": "paper_type"},
          {"running_sum_of_revenue": along(m, [], ["manual"], running(REV)), "row_index": along(m, [], ["manual"], index())})
    m["by_revenue"] = -m[REV]
    df = _run(con, tiles["Running by Revenue Sort"])
    check(df, m, {"paper_type": "paper_type"}, {"running_sum_of_revenue": along(m, [], ["by_revenue"], running(REV)),
                                                "difference_in_revenue": along(m, [], ["by_revenue"], diff(REV))})
    assert df.paper_type.tolist() == m.sort_values("by_revenue").paper_type.tolist()


def test_restart_every_year(tiles, con, rows):
    """Specific Dimensions YEAR then QUARTER, Restart Every YEAR (`level-break` on the quarter)."""
    m = marks(rows, ["year", "quarter"], revenue=(REV, "sum"))
    check(_run(con, tiles["Restart Every Year"]), m, {"year_of_purchased_at": "year", "quarter_of_purchased_at": "quarter"},
          {"running_sum_of_revenue": along(m, ["year"], ["quarter"], running(REV))})


def test_top_n_filter_comes_before_the_table_calcs(tiles, con, rows):
    """A top N filter is a dimension filter: the running total runs over the top three paper types only."""
    m = marks(rows, ["paper_type"], revenue=(REV, "sum")).nlargest(3, REV).reset_index(drop=True)
    check(_run(con, tiles["Top Paper Types Running"]), m, {"paper_type": "paper_type"},
          {"running_sum_of_revenue": along(m, [], ["paper_type"], running(REV))})


def test_filter_on_a_table_calc_keeps_top_n_per_pane(tiles, con, rows):
    """A range filter on RANK() addressed Pane (down): the top managers of each status. Tableau ranks all the marks, then
    hides the others; a calculation only on the Filters shelf is not a column of the tile."""
    m = marks(rows, ["status", "account_manager"], revenue=(REV, "sum"))
    m["rank"] = m.groupby("status")[REV].rank(method="min", ascending=False)
    keys = {"status": "status", "account_manager": "account_manager"}
    top3 = m[m["rank"] <= 3].reset_index(drop=True)
    check(_run(con, tiles["Top Managers per Status"]), top3, keys, {"revenue": top3[REV], "revenue_rank": top3["rank"]})
    top2 = m[m["rank"] <= 2].reset_index(drop=True)
    tile = tiles["Top Managers per Status Filter Only"]
    df = _run(con, tile)
    assert list(df.columns) == ["status", "account_manager", "revenue"] and "revenue_rank" not in tile.source_config["columns"]
    check(df, top2, keys, {"revenue": top2[REV]})


# ------------------------------------------------------------------ quick table calcs


def test_difference_and_percent_difference_along_quarters(tiles, con, rows):
    m = marks(rows, ["year", "quarter"], revenue=(REV, "sum"))
    keys = {"year_of_purchased_at": "year", "quarter_of_purchased_at": "quarter"}
    order = ([], ["year", "quarter"])
    check(_run(con, tiles["Quarterly Difference"]), m, keys, {
        "difference_in_revenue": along(m, *order, diff(REV)),
        "pct_difference_in_revenue": along(m, *order, pct_diff(REV)),
        "pct_of_revenue": along(m, *order, pct_from(REV)),
    })


def test_differences_with_nulls_and_negative_values(tiles, con, rows):
    """Completed Revenue is NULL outside COMPLETED (ZN reads it as 0); Discount Amount is negative (the percent difference
    divides by the absolute previous value, the percent from by the signed one)."""
    m = marks(rows, ["status", "paper_type"], discount=("discount", "sum"), completed=("completed", "sum"))
    m["completed"] = m.completed.where(m.status == "COMPLETED")
    across, down = (["status"], ["paper_type"]), (["paper_type"], ["status"])
    check(_run(con, tiles["Discount Change"]), m, GRID, {
        "pct_difference_in_discount_amount": along(m, *across, pct_diff("discount")),
        "pct_of_discount_amount": along(m, *across, pct_from("discount")),
        "difference_in_completed_revenue": along(m, *down, diff("completed")),
        "pct_difference_in_completed_revenue": along(m, *down, pct_diff("completed")),
    })


def test_percent_of_total(tiles, con, rows):
    m = marks(rows, ["paper_type"], revenue=(REV, "sum"))
    check(_run(con, tiles["Share by Paper Type"]), m, {"paper_type": "paper_type"}, {"pct_of_total_revenue": m[REV] / m[REV].sum()})
    g = marks(rows, ["status", "paper_type"], revenue=(REV, "sum"))
    share = g[REV] / g.groupby("paper_type")[REV].transform("sum")
    check(_run(con, tiles["Share Within Paper Type"]), g, GRID, {"pct_of_total_revenue": share})


def test_total_recomputes_the_aggregate(tiles, con, rows):
    """Percent of total of an average divides by the average of all rows, not by the sum of the marks' averages."""
    m = marks(rows, ["paper_type"], quantity=("quantity", "mean"), orders=("order_id", "count"))
    check(_run(con, tiles["Average Share"]), m, {"paper_type": "paper_type"}, {
        "pct_of_total_avg_quantity": m.quantity / rows.quantity.mean(),
        "pct_of_total_count_of_order_id": m.orders / m.orders.sum(),
    })


def test_measure_filters_come_first(tiles, con, rows):
    """A filter on SUM(Revenue) removes marks before the table calculations run on the rest."""
    m = marks(rows, ["paper_type"], revenue=(REV, "sum"), quantity=("quantity", "mean"), n=("quantity", "count"), q=("quantity", "sum"))
    m = m[m[REV] >= 3_000_000].reset_index(drop=True)
    assert len(m) == 3
    check(_run(con, tiles["Share With Measure Filter"]), m, {"paper_type": "paper_type"}, {
        "pct_of_total_revenue": m[REV] / m[REV].sum(),
        "running_sum_of_revenue": along(m, [], ["paper_type"], running(REV)),
    })
    check(_run(con, tiles["Average Share Filtered"]), m, {"paper_type": "paper_type"},
          {"pct_of_total_avg_quantity": m.quantity / (m.q.sum() / m.n.sum())})


def test_ranks(tiles, con, rows):
    """Competition (1, 1, 1, 4), dense, unique (ties in the sheet's order), modified (3, 3, 3, 4) and percentile ranks of
    the managers' largest order; seven managers tie at 210 and three at 228."""
    m = marks(rows, ["account_manager"], max_q=("quantity", "max"), revenue=(REV, "sum"))
    keys = {"account_manager": "account_manager"}
    n = len(m)
    pct = (m.max_q.rank(method="max") - 1) / (n - 1)
    check(_run(con, tiles["Rank by Manager"]), m, keys, {
        "rank_of_max_quantity": m.max_q.rank(method="min", ascending=False),
        "percentile_of_max_quantity": pct,
        "rank_of_revenue": m[REV].rank(method="first"),
    })
    check(_run(con, tiles["Rank Calcs by Manager"]), m, keys, {
        "max_quantity_rank": m.max_q.rank(method="min", ascending=False),
        "max_quantity_dense_rank": m.max_q.rank(method="dense", ascending=False),
        "max_quantity_unique_rank": m.sort_values("account_manager").max_q.rank(method="first", ascending=False).reindex(m.index),
        "max_quantity_modified_rank": m.max_q.rank(method="max", ascending=False),
        "max_quantity_percentile": pct,
    })
    check(_run(con, tiles["Dense Rank"]), m, keys, {"rank_of_max_quantity": m.max_q.rank(method="dense", ascending=False)})
    # Table (across) with nothing on Columns: each manager alone, the percentile of one mark is 1
    alone = pd.Series(1.0, index=m.index)
    check(_run(con, tiles["Percentile Alone"]), m, keys, {"percentile_of_max_quantity": alone, "max_quantity_percentile": alone})


def test_moving_average(tiles, con, rows):
    m = marks(rows, ["month"], revenue=(REV, "sum"))
    keys = {"month_of_purchased_at": "month"}
    check(_run(con, tiles["Moving Average by Month"]), m, keys,
          {"moving_average_of_revenue": along(m, [], ["month"], lambda g: g[REV].rolling(3, min_periods=1).mean())})
    # without IncludeCurrent: the two marks before
    check(_run(con, tiles["Moving Average Without Current"]), m, keys,
          {"moving_average_of_revenue": along(m, [], ["month"], lambda g: g[REV].shift(1).rolling(2, min_periods=1).mean())})


# ------------------------------------------------------------------ table-calc functions


def test_formula_table_calcs(tiles, con, rows):
    """Rows: Status; Columns: YEAR. Calcs addressed Table (across) move along the years of a status; the ones addressed
    Table (down) along the statuses of a year."""
    m = marks(rows, ["status", "year"], revenue=(REV, "sum"), completed=("completed", "sum"), qavg=("quantity", "mean"))
    m["completed"] = m.completed.where(m.status == "COMPLETED")
    across, down = (["status"], ["year"]), (["year"], ["status"])
    total = lambda part, col: m.groupby(part)[col].transform("sum")  # noqa: E731
    qtotal = rows.groupby("status").quantity.mean()
    rank_asc = m.groupby("year")[REV].rank(method="min")
    size = m.groupby("status")[REV].transform("size").astype(float)
    check(_run(con, tiles["Formula Calcs"]), m, {"status": "status", "year_of_purchased_at": "year"}, {
        "running_revenue": along(m, *across, running(REV)),
        "revenue_share": m[REV] / total("status", REV),
        "quantity_vs_average": m.qavg / m.status.map(qtotal),
        "centered_average": along(m, *across, lambda g: g[REV].rolling(3, center=True, min_periods=1).mean()),
        "partition_max": m.groupby("status")[REV].transform("max"),
        "previous_revenue": along(m, *across, lookup(REV, -1)),
        "next_revenue": along(m, *across, lookup(REV, 1)),
        "row_index": along(m, *across, index()),
        "partition_size": size,
        "revenue_rank_asc": rank_asc,
        "revenue_to_date": along(m, *down, running(REV)),
        "offset_from_first": 1 - along(m, *across, index()),
        "offset_to_last": m.groupby("year")[REV].transform("size") - along(m, *down, index()),
        "running_avg_completed": along(m, *across, running("completed", "avg")),
        "completed_rank": m.groupby("year").completed.rank(method="min", ascending=False),
        "revenue_from_here": along(m, *across, lambda g: g[REV][::-1].cumsum()[::-1]),
        "first_revenue": along(m, *down, lambda g: pd.Series(g[REV].iloc[0], index=g.index)),
    })


def test_window_variants(tiles, con, rows):
    m = marks(rows, ["status", "year"], revenue=(REV, "sum"), completed=("completed", "sum"), q=("quantity", "sum"),
              mn=(REV, "min"), n=("order_id", "count"))
    m["completed"] = m.completed.where(m.status == "COMPLETED")
    across, down = (["status"], ["year"]), (["year"], ["status"])
    ratio = m[REV] / m.q
    by_status = rows.groupby("status")
    pct_desc = m.groupby("year")[REV].transform(lambda s: (s.rank(method="max", ascending=False) - 1) / (len(s) - 1) if len(s) > 1 else 1.0)
    check(_run(con, tiles["Window Variants"]), m.assign(ratio=ratio), {"status": "status", "year_of_purchased_at": "year"}, {
        "running_count": along(m, *across, index()),
        "running_min": along(m, *across, running(REV, "min")),
        "window_count_back": along(m, *across, lambda g: g[REV].rolling(2, min_periods=1).count()),
        "last_revenue": along(m, *across, lambda g: pd.Series(g[REV].iloc[-1], index=g.index)),
        "min_share": m.mn / m.status.map(by_status[REV].min()),
        "orders_share": m.n / m.status.map(by_status.order_id.count()),
        "ratio_running": along(m.assign(ratio=ratio), *across, running("ratio")),
        "ratio_share": ratio / m.status.map(by_status[REV].sum() / by_status.quantity.sum()),
        "pct_of_completed_revenue": along(m, *down, pct_from("completed")),
        "percentile_of_revenue": pct_desc,
        "moving_sum_of_revenue": along(m, *across, lambda g: g[REV].shift(-1)),  # 0 to 1 without the current mark: the next one
    })


# ------------------------------------------------------------------ Marks card, aliased and sorted dimensions in crosstabs

MGR, CYC, ST, PT = "account_manager", "payment_cycle", "status", "paper_type"
DIMS = {MGR: MGR, CYC: CYC, ST: ST, PT: PT, "product_name": "label"}


def pad_with(m: pd.DataFrame, row_side: list[str], col_side: list[str]) -> pd.DataFrame:
    """A crosstab's padded marks: its row headers with its column headers, each side with the Marks card dimensions it
    carries, matched on the ones both carry."""
    shared = [d for d in row_side if d in col_side]
    r, c = m[row_side].drop_duplicates(), m[col_side].drop_duplicates()
    domain = r.merge(c, on=shared) if shared else r.merge(c, how="cross")
    return domain.merge(m, on=list(dict.fromkeys(row_side + col_side)), how="left")


def size():
    return lambda g: pd.Series(len(g), index=g.index, dtype=float)


def three(m: pd.DataFrame, part: list[str], order: list[str]) -> dict[str, pd.Series]:
    return {"running_sum_of_revenue": along(m, part, order, running(REV)), "row_index": along(m, part, order, index()),
            "partition_size": along(m, part, order, size())}


@pytest.mark.parametrize("sheet, row_side, col_side, part, order", [
    # a Marks card dimension the calculation partitions by: the padding is per member (both sides carry it)
    ("Color Across", [MGR, ST], [CYC, ST], [MGR, ST], [CYC]),
    ("Color Down", [MGR, ST], [CYC, ST], [CYC, ST], [MGR]),
    ("Color Pane Down", [MGR, ST], [CYC, ST], [CYC, ST], [MGR]),
    ("Size Down", [MGR, ST], [CYC, ST], [CYC, ST], [MGR]),
    ("Detail Across", [MGR, PT], [CYC, PT], [MGR, PT], [CYC]),
    ("Color Detail Across", [MGR, ST, PT], [CYC, ST, PT], [MGR, ST, PT], [CYC]),
    # one it moves along: on the side of the headers it moves along
    ("Color Specific Cycle Status", [MGR], [CYC, ST], [MGR], [CYC, ST]),
    ("Color Specific Status Restart", [MGR], [CYC, ST], [MGR], [ST, CYC]),  # Restart Every manager
    ("Color Detail Specific", [MGR, ST], [CYC, PT, ST], [MGR, ST], [CYC, PT]),
    ("Color Detail Specific Status", [MGR, PT], [CYC, ST, PT], [MGR, PT], [CYC, ST]),
    ("Detail Addressed Down", [MGR, PT, ST], [CYC, ST], [CYC, ST], [MGR, PT]),
    # nested headers: every row header with every column header (a pane is padded with the whole shelf, not per pane)
    ("Sparse Nested Pane Down", [MGR, CYC], [ST], [MGR, ST], [CYC]),
])
def test_marks_card_dimensions_pad_with_their_headers(tiles, con, rows, sheet, row_side, col_side, part, order):
    """Account managers by payment cycle (61 of 70 combinations) with status on colour, paper type on detail or both
    (536 of 1,050 combinations). The running total carries over a padded mark, INDEX and SIZE count it."""
    dims = list(dict.fromkeys(row_side + col_side))
    m = pad_with(marks(rows, dims, revenue=(REV, "sum")), row_side, col_side)
    assert m[REV].isna().any()
    check(_run(con, tiles[sheet]), m, {d: d for d in dims}, three(m, part, order))


def test_null_marks_card_member_is_padded_too(tiles, con, rows):
    """Open Status (NULL for completed orders) on colour: the NULL member is padded like the others."""
    r = rows.assign(open_status=rows[ST].where(rows[ST] != "COMPLETED"))
    m = pad_with(marks(r, [MGR, CYC, "open_status"], revenue=(REV, "sum")), [MGR, "open_status"], [CYC, "open_status"])
    assert len(m) == 210 and m.open_status.isna().sum() == 70
    check(_run(con, tiles["Color Null Across"]), m, {MGR: MGR, CYC: CYC, "open_status": "open_status"}, three(m, [MGR, "open_status"], [CYC]))


def test_one_padding_calculation_pads_the_sheet(tiles, con, rows):
    """A running total across pads the crosstab; INDEX and SIZE along the whole table then count the padded marks too."""
    m = pad(marks(rows, [MGR, CYC], revenue=(REV, "sum")), [MGR], [CYC])
    assert len(m) == 70
    check(_run(con, tiles["Mixed Addressing"]), m, {MGR: MGR, CYC: CYC}, {
        "running_sum_of_revenue": along(m, [MGR], [CYC], running(REV)),
        "row_index": along(m, [], [MGR, CYC], index()), "partition_size": along(m, [], [MGR, CYC], size())})


def test_running_with_colour_in_a_grid(tiles, con, rows):
    m = pad_with(marks(rows, [ST, PT, CYC], revenue=(REV, "sum")), [ST, CYC], [PT, CYC])
    check(_run(con, tiles["Running with Color in Grid"]), m, {ST: ST, PT: PT, CYC: CYC},
          {"running_sum_of_revenue": along(m, [ST, CYC], [PT], running(REV))})


@pytest.mark.parametrize("sheet, dims, part, order", [
    ("Color Table", [MGR, CYC, ST], [ST], [MGR, CYC]),  # Table (across then down): no header partitions it
    ("Sparse Grid Table", [MGR, CYC], [], [MGR, CYC]),
    ("Color Specific All", [MGR, CYC, ST], [], [MGR, CYC, ST]),
    ("Color Specific Status", [MGR, CYC, ST], [MGR, CYC], [ST]),  # it moves along no header
    ("Color Cell", [MGR, CYC, ST], [MGR, CYC, ST], []),
    ("Color No Crosstab", [MGR, ST], [], [MGR, ST]),
])
def test_crosstabs_left_unpadded(tiles, con, rows, sheet, dims, part, order):
    m = marks(rows, dims, revenue=(REV, "sum"))
    want = three(m, part, order) if order else {"running_sum_of_revenue": m[REV], "row_index": pd.Series(1.0, index=m.index)}
    check(_run(con, tiles[sheet]), m, {d: d for d in dims}, want)


ALIASES = {"Ledger 24lb Paper, 11 x 17 Tabloid, Bright White Wove, 500 Sheets/Ream": "alpha ledger",
           "Premium 24lb Paper, 8.5 x 11, Ivory Wove, 100 Sheets/Pack": "Beta Premium",
           "Copy Paper, 20 lbs., 92 Brightness, 500 Sheets/Ream, 40 Cases/Pallet": "Zeta Copy",
           "Bulk Copy Paper, 92 Brightness, 20lb, 8-1/2 X 11, White, 200,000 Sheets/CT": "Copy Bulk"}


@pytest.mark.parametrize("sheet, dims, part", [
    ("Aliased Down", ["product_name", CYC], [CYC]),  # crosstabs over the products' labels
    ("Aliased Across", [ST, "product_name"], [ST]),
    ("Aliased Color", [ST, CYC, "product_name"], [ST, CYC]),  # along the aliased colour: it moves along no header, no padding
])
def test_aliased_dimensions_move_in_label_order(tiles, con, rows, sheet, dims, part):
    """Products with aliases (Default Properties > Aliases) show and order by their labels, ignoring case."""
    r = rows.assign(label=rows.product_name.map(ALIASES).fillna(rows.product_name))
    m = marks(r, [DIMS[d] for d in dims], revenue=(REV, "sum"))
    m["by_label"] = m.label.str.lower()
    keys = {d: DIMS[d] for d in dims}
    check(_run(con, tiles[sheet]), m, keys, three(m, part, ["by_label"]))
    assert m.label.str.lower().sort_values().iloc[0] == "alpha ledger"


def test_computed_sorts_order_the_padded_crosstab(tiles, con, rows):
    """Managers sorted by revenue (descending): the calculation moves down them in that order, each by its revenue over the
    whole view, padded marks included."""
    by_mgr = rows.groupby(MGR)[REV].sum()
    m = pad(marks(rows, [MGR, CYC], revenue=(REV, "sum")), [MGR], [CYC])
    m["mgr_key"] = -m[MGR].map(by_mgr)
    df = _run(con, tiles["Sorted Grid Down"])
    check(df, m, {MGR: MGR, CYC: CYC}, three(m, [CYC], ["mgr_key"]))
    assert list(dict.fromkeys(df[MGR])) == by_mgr.sort_values(ascending=False).index.tolist()
    m = pad_with(marks(rows, [MGR, CYC, ST], revenue=(REV, "sum")), [MGR, ST], [CYC, ST])
    m["mgr_key"] = -m[MGR].map(by_mgr)
    check(_run(con, tiles["Sorted Grid Color"]), m, {MGR: MGR, CYC: CYC, ST: ST}, three(m, [CYC, ST], ["mgr_key"]))


def test_computed_sorts_on_several_dimensions(tiles, con, rows):
    """Each sorted dimension orders by its aggregate over the whole view (a nested one too: one order across panes)."""
    m = marks(rows, [MGR, CYC], revenue=(REV, "sum"))
    m["mgr_key"] = -m[MGR].map(rows.groupby(MGR)[REV].sum())
    m["cyc_key"] = m[CYC].map(rows.groupby(CYC).quantity.sum())  # payment cycles by quantity, ascending
    check(_run(con, tiles["Sorted Both Table"]), m, {MGR: MGR, CYC: CYC}, three(m, [], ["mgr_key", "cyc_key"]))
    m = marks(rows, [ST, PT], revenue=(REV, "sum"))
    m["st_key"], m["pt_key"] = -m[ST].map(rows.groupby(ST)[REV].sum()), -m[PT].map(rows.groupby(PT)[REV].sum())
    df = _run(con, tiles["Nested Sorted Down"])
    check(df, m, {ST: ST, PT: PT}, three(m, [], ["st_key", "pt_key"]))
    assert df[PT].tolist() == m.sort_values(["st_key", "pt_key"])[PT].tolist()
    m = marks(rows, [ST, PT, CYC], revenue=(REV, "sum"))
    m["pt_key"] = m[PT].map(rows.groupby(PT)[REV].sum())  # ascending; status in natural order
    check(_run(con, tiles["Sorted Nested Grid"]), m, {ST: ST, PT: PT, CYC: CYC}, three(m, [], [ST, "pt_key", CYC]))


# ------------------------------------------------------------------ what stays TODO


@pytest.mark.parametrize("sheet, says", [
    ("Nested Calcs", ["nested table calculations", "PREVIOUS_VALUE()", "SCRIPT_*()"]),
    ("At the Level", ["At the level"]),
    ("Custom Sort Addressing", ["custom sort"]),
    ("Moving Average Null If Incomplete", ["NullIfIncomplete"]),
])
def test_untranslated_table_calcs_stay_todo(tiles, sheet, says):
    t = tiles[sheet]
    assert not t.sql
    for s in says:
        assert s in t.todo, t.todo


def test_table_calc_fields_are_not_metrics(bundle):
    m = {x.label: x for x in bundle.metrics}
    for label, formula in (("Running Revenue", "RUNNING_SUM(SUM([Revenue]))"), ("Revenue Share", "SUM([Revenue]) / TOTAL(SUM([Revenue]))")):
        assert "window SQL over the sheet's marks" in m[label].todo
        assert m[label].source_expression == formula


def test_numbered_instances_keep_their_own_settings(tiles):
    """Each distinct table calculation of a field has its own instance name (`cum:sum:x:qk:2`) with its own settings."""
    twb = next((FIX / "workbooks").glob("*.twb")).read_text()
    assert "name='[cum:sum:post_discount_revenue:qk]'" in twb and "name='[cum:sum:post_discount_revenue:qk:2]'" in twb
    assert "partition by d.paper_type" in tiles["Grid Down"].sql and "partition by d.status" in tiles["Grid Across"].sql
