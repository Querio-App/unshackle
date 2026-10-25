"""Tableau's analytics pane (tests/fixtures/tableau_analytics/): reference lines and bands, totals, trend lines and
forecasts.

The sheets run on the Dunder Mifflin data in DuckDB. Expected values are computed here with pandas from Tableau's rules:
a reference line aggregates the sheet's marks (average, median, min, max or sum of the mark values; Total recomputes the
aggregate) within its scope: the table, a pane (the cell without the innermost dimension of a shelf that ends with
one) or a cell (one combination of the discrete dimensions on Rows and Columns). Totals recompute the sheet's
aggregate at the total's level.
"""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from unshackle.ir import Dashboard
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.target.notebooks import build_notebook

FIX = Path(__file__).parent / "fixtures" / "tableau_analytics"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
REV = "post_discount_revenue"


def _parse(tmp_path: Path, warehouse: str = "duckdb"):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    if warehouse:
        (tmp_path / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{warehouse}"\n')
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


@pytest.fixture
def bundle(tmp_path: Path):
    return _parse(tmp_path)


@pytest.fixture
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
    r[REV] = r[REV].astype(float)
    return r


def _run(con, tile) -> pd.DataFrame:
    df = con.sql(tile.sql.replace(":min_order_value", "500.0")).df()
    return df.apply(lambda s: s.astype(float) if s.dtype == object and s.map(lambda v: type(v).__name__ == "Decimal").any() else s)


def _marks(rows: pd.DataFrame, by: list[str], agg: str = "sum") -> pd.DataFrame:
    return rows.groupby(by, as_index=False)[REV].agg(agg)


def test_table_scope_lines(tiles, con, rows):
    df = _run(con, tiles["RL Table"])
    marks = _marks(rows, ["paper_type"])[REV]
    expected = {"average": marks.mean(), "median": marks.median(), "min": marks.min(), "max": marks.max(), "sum": marks.sum(),
                "total": rows[REV].sum()}  # Total: SUM recomputed over all rows (the marks add up to it)
    for formula, value in expected.items():
        assert df[f"{REV}_{formula}"].tolist() == pytest.approx([value] * len(df), rel=1e-9), formula
    assert len(df) == 5 and df[REV].tolist() == pytest.approx(marks.tolist(), rel=1e-9)


def test_pane_and_cell_scopes(tiles, con, rows):
    # Rows: payment_cycle / paper_type, Columns: SUM: a pane per payment_cycle, a cell per bar
    df = _run(con, tiles["RL Pane Rows"])
    marks = _marks(rows, ["payment_cycle", "paper_type"])
    pane = marks.groupby("payment_cycle")[REV].mean()
    assert df[f"{REV}_average_per_pane"].tolist() == pytest.approx(df.payment_cycle.map(pane).tolist(), rel=1e-9)
    assert df[f"{REV}_average_per_cell"].tolist() == pytest.approx(df[REV].tolist(), rel=1e-9)
    assert df[f"{REV}_average"].tolist() == pytest.approx([marks[REV].mean()] * len(df), rel=1e-9)
    # Rows: payment_cycle / SUM, Columns: paper_type: the pane still spans the paper types of a payment cycle
    grid = _run(con, tiles["RL Grid"])
    median = marks.groupby("payment_cycle")[REV].median()
    assert grid[f"{REV}_median_per_pane"].tolist() == pytest.approx(grid.payment_cycle.map(median).tolist(), rel=1e-9)
    assert grid[f"{REV}_max_per_cell"].tolist() == pytest.approx(grid[REV].tolist(), rel=1e-9)
    assert grid[f"{REV}_min"].tolist() == pytest.approx([marks[REV].min()] * len(grid), rel=1e-9)


def test_median_of_an_even_number_of_marks(tiles, con, rows):
    df = _run(con, tiles["RL Median Even"])
    marks = _marks(rows[rows.paper_type != "Multipurpose"], ["paper_type"])[REV].sort_values().tolist()
    assert len(df) == 4 and df[f"{REV}_median"].iloc[0] == pytest.approx((marks[1] + marks[2]) / 2, rel=1e-9)
    assert "where products.paper_type in ('Cardstock', 'Copy', 'Kraft Roll', 'Printer')" in tiles["RL Median Even"].sql


def test_continuous_axis_splits_nothing(tiles, con, rows):
    df = _run(con, tiles["RL Line"])
    marks = _marks(rows.assign(m=rows.purchased_at.dt.to_period("M")), ["m"])[REV]
    assert df[f"{REV}_average_per_cell"].tolist() == pytest.approx([marks.mean()] * len(df), rel=1e-9)
    assert df[f"{REV}_max_per_pane"].tolist() == pytest.approx([marks.max()] * len(df), rel=1e-9)


def test_lines_over_averages_and_parameters(tiles, con, rows):
    df = _run(con, tiles["RL Avg"])
    marks = _marks(rows, ["paper_type"], "mean")[REV]
    assert df[f"avg_{REV}_average"].iloc[0] == pytest.approx(marks.mean(), rel=1e-9)  # the average of the averages
    assert df[f"avg_{REV}_sum"].iloc[0] == pytest.approx(marks.sum(), rel=1e-9)
    # Total of an AVG is the average over all rows, not something the marks give: TODO
    assert "Total of Avg. post_discount_revenue per table not translated" in tiles["RL Avg"].todo
    param = _run(con, tiles["RL Param"])
    assert ":min_order_value as min_order_value_line" in tiles["RL Param"].sql and set(param.min_order_value_line) == {500.0}


def test_percentage_band(tiles, con, rows):
    t = tiles["RL Band"]
    df = _run(con, t)
    avg = _marks(rows, ["paper_type"])[REV].mean()
    assert df[f"{REV}_average_50_pct"].iloc[0] == pytest.approx(0.5 * avg, rel=1e-9)
    assert df[f"{REV}_average_80_pct"].iloc[0] == pytest.approx(0.8 * avg, rel=1e-9)
    assert [(r.field, r.label, r.band_to) for r in t.references] == [
        (f"{REV}_average_50_pct", "50% of Average", ""), (f"{REV}_average_80_pct", "80% of Average", ""),
        (f"{REV}_average_50_pct", "", f"{REV}_average_80_pct")]


def test_references_on_the_tile(tiles):
    refs = {(r.field, r.channel, r.scope, r.label) for r in tiles["RL Pane Rows"].references}
    assert refs == {(f"{REV}_average_per_pane", "x", "pane", "pane= {value}"), (f"{REV}_average_per_cell", "x", "cell", "cell= {value}"),
                    (f"{REV}_average", "x", "table", "table= {value}")}
    assert {r.label for r in tiles["RL Median Even"].references} == {"med= {value}", "Average"}  # automatic: the computation
    assert tiles["RL Grid"].references[0].channel == "y"


def test_what_stays_todo(tiles):
    assert "split inside each cell by status" in tiles["RL Cell Color"].todo and not tiles["RL Cell Color"].references
    assert "quantiles distribution band" in tiles["RL Quantiles"].todo and "tile-count=4" in tiles["RL Quantiles"].todo
    assert "dimensions off Rows and Columns" in tiles["Totals Detail"].todo and "grouping sets" not in tiles["Totals Detail"].sql
    forecast = tiles["Forecast"].todo
    assert "Tableau forecast of post_discount_revenue over Month of purchased_at" in forecast
    assert "trend: ets-additive" in forecast and "season: ets-additive" in forecast and "length: auto" in forecast
    assert "last 1 period(s) of actual values" in forecast and "ExponentialSmoothing" in forecast
    trend = tiles["Trend"].todo
    assert "logarithmic model of post_discount_revenue over Month of purchased_at" in trend and "method='log'" in trend
    clean = {"RL Table", "RL Pane Rows", "RL Grid", "RL Median Even", "RL Line", "RL Param", "RL Band",
             "Totals Sum", "Totals Avg", "Totals Rows Only", "Totals Cols Only"}
    assert {n: t.todo for n, t in tiles.items() if n in clean and t.todo} == {}


def test_forecast_keeps_the_actual_values(tiles, con, rows):
    df = _run(con, tiles["Forecast"])
    marks = _marks(rows.assign(m=rows.purchased_at.dt.to_period("M").dt.to_timestamp()), ["paper_type", "m"])
    got = sorted(zip(df.paper_type, df.month_of_purchased_at, df[REV]))
    assert [g[:2] for g in got] == sorted(zip(marks.paper_type, marks.m))
    assert [g[2] for g in got] == pytest.approx(marks.sort_values(["paper_type", "m"])[REV].tolist(), rel=1e-9)


def _totals(rows: pd.DataFrame, dims: list[str], sets: list[list[str]], aggs: dict[str, tuple[str, str]]) -> dict:
    """{dimension values with 'All' on total rows: {column: value}} from pandas, one group by per grouping set."""
    out = {}
    for s in sets:
        g = rows.assign(_all="All").groupby(s or ["_all"])
        res = g.agg(**{col: (src, fn) for col, (src, fn) in aggs.items()})
        for key, r in res.iterrows():
            key = key if isinstance(key, tuple) else (key,)
            values = dict(zip(s, key))
            out[tuple(values.get(d, "All") for d in dims)] = {c: float(r[c]) for c in aggs}
    return out


def _got(df: pd.DataFrame, dims: list[str], cols: list[str]) -> dict:
    return {tuple("All" if r.get(f"{d}_total") == 1 else r[d] for d in dims): {c: float(r[c]) for c in cols} for r in df.to_dict("records")}


@pytest.mark.parametrize("sheet,dims,sets", [
    # Rows payment_cycle / paper_type (grand total row, subtotals of payment_cycle), Columns status (grand total column)
    ("Totals Sum", ["payment_cycle", "paper_type", "status"],
     [["payment_cycle", "paper_type", "status"], ["payment_cycle", "paper_type"], ["payment_cycle", "status"], ["payment_cycle"], ["status"], []]),
    ("Totals Rows Only", ["paper_type", "status"], [["paper_type", "status"], ["status"]]),
    ("Totals Cols Only", ["paper_type", "status"], [["paper_type", "status"], ["paper_type"]]),
])
def test_sum_totals(tiles, con, rows, sheet, dims, sets):
    df = _run(con, tiles[sheet])
    want = _totals(rows, dims, sets, {REV: (REV, "sum")})
    got = _got(df, dims, [REV])
    assert got.keys() == want.keys()
    for k, v in want.items():
        assert got[k] == pytest.approx(v, rel=1e-9), k


def test_totals_recompute_avg_and_countd(tiles, con, rows):
    """Tableau's automatic totals: the average over the total's rows (not the average of the averages), distinct counts
    over the total's rows."""
    df = _run(con, tiles["Totals Avg"])
    dims = ["payment_cycle", "paper_type"]
    want = _totals(rows, dims, [dims, ["payment_cycle"], []],
                   {f"avg_{REV}": (REV, "mean"), "distinct_count_of_order_id": ("order_id", "nunique")})
    got = _got(df, dims, [f"avg_{REV}", "distinct_count_of_order_id"])
    assert got.keys() == want.keys()
    for k, v in want.items():
        assert got[k] == pytest.approx(v, rel=1e-9), k
    assert got[("All", "All")][f"avg_{REV}"] != pytest.approx(df[df.payment_cycle_total == 0][f"avg_{REV}"].mean())


def test_totals_sort_after_their_group(tiles, con):
    df = _run(con, tiles["Totals Sum"])
    keys = [("~" if r.payment_cycle_total else r.payment_cycle, "~" if r.paper_type_total else r.paper_type, "~" if r.status_total else r.status)
            for r in df.itertuples()]
    assert keys == sorted(keys) and keys[-1] == ("~", "~", "~")


def test_report_lists_each_object(bundle):
    info = [w.item + ": " + w.message for w in bundle.warnings if w.severity == "info"]
    assert "sheet RL Table: reference line: Average of post_discount_revenue per table: column post_discount_revenue_average of the tile SQL, drawn as a rule" in info
    assert any(i.startswith("sheet Totals Sum: totals (a grand total row, a grand total column, subtotals of payment_cycle)") for i in info)


@pytest.mark.parametrize("warehouse", ["postgres", "redshift", "snowflake", "bigquery", "databricks"])
def test_median_lines_per_dialect(tmp_path, warehouse):
    t = {t.title: t for d in _parse(tmp_path, warehouse).dashboards for t in d.tiles}["RL Grid"]
    if warehouse == "bigquery":  # its percentile is an analytic function
        assert "percentile_cont(cast(tile.post_discount_revenue as float64), 0.5) over (partition by tile.payment_cycle)" in t.sql
    else:  # the median per pane in its own CTE (Postgres has no window median)
        assert "reference_1 as (\nselect payment_cycle, " in t.sql and "left join reference_1 on tile.payment_cycle is not distinct from" in t.sql
    assert not t.todo


def test_notebook_draws_the_lines(bundle):
    altair = pytest.importorskip("altair")
    d = next(d for d in bundle.dashboards)
    nb = build_notebook(Dashboard(d.name, d.slug, tiles=[t for t in d.tiles if t.title in ("RL Pane Rows", "RL Band", "RL Table")]), "tableau")
    charts = {c["id"]: c["source"] for c in nb["cells"] if c["id"].endswith("-chart")}
    pane = charts["rl_pane_rows-chart"]
    assert ".facet(" in pane and "mark_tick(" in pane and "mark_rule(" in pane  # faceted per payment_cycle; a tick per cell
    assert "format(datum['post_discount_revenue_average_per_pane'], ',.2~f')" in pane
    assert "mark_rect(opacity=0.15)" in charts["rl_band-chart"]
    df = pd.DataFrame({"payment_cycle": ["A", "A", "B"], "paper_type": ["x", "y", "x"], REV: [1.0, 2.0, 3.0],
                       f"{REV}_average_per_pane": [1.5, 1.5, 3.0], f"{REV}_average_per_cell": [1.0, 2.0, 3.0], f"{REV}_average": [2.0] * 3})
    ns = {"alt": altair, "rl_pane_rows": df}
    exec(pane, ns)
    ns["rl_pane_rows_chart"].to_dict()
