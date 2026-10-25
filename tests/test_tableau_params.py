"""Tableau parameters in calculations: what-if calcs, aggregates that read a parameter, and measure / dimension switches.

A hand-written workbook over the Dunder Mifflin orders (tests/fixtures/tableau_params): parameters of every type (real and
integer ranges, string, integer and boolean lists, a date range), calcs that read them, and one sheet per calc. Tiles bind each
parameter as :name from the notebook's parameters cell, whose default is the parameter's current value. Each sheet runs in
DuckDB at the current values and at other values, and the expected numbers are computed here from Tableau's rules:

- a parameter is a constant in the formula: a row-level calc reads it on every row, an aggregate calc next to the aggregate;
- CASE [Parameter] WHEN 'a' THEN x ... END picks one branch for the whole sheet (no match and no ELSE: NULL);
- a date parameter is a date: DATETRUNC('week', [Start Date]) starts the week on the datasource's Week start (Sunday in en_US);
- a comparison with a NULL date is NULL, a mark of its own on a shelf and dropped by a filter on True.
"""

import shutil
from pathlib import Path

import pandas as pd
import pytest
from conftest import notebook_source

from unshackle import build
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import TField, _param_domain, param_bind, read_datasource

FIX = Path(__file__).parent / "fixtures" / "tableau_params"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
DEFAULTS = {"growth_rate": 0.1, "churn_rate": 0.064, "commission_rate": 18.4, "revenue_target": 1500000, "measure": "Revenue",
            "group_by": "Status", "start_date": "2025-01-01", "include_cancelled": False, "max_days": 14}
OTHER = {"growth_rate": 0.25, "churn_rate": 0.1, "commission_rate": 5.0, "revenue_target": 750000, "measure": "Quantity",
         "group_by": "Account", "start_date": "2025-10-01", "include_cancelled": True, "max_days": 30}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("params")
    shutil.copytree(FIX, tmp / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp, "tableau"))


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path_factory.mktemp("wh") / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema public")
    con.execute(f"create table public.orders as select * from '{DATA / 'orders.parquet'}'")
    con.close()
    return db


@pytest.fixture(scope="module")
def orders() -> pd.DataFrame:
    duckdb = pytest.importorskip("duckdb")
    o = duckdb.sql(f"select * from '{DATA / 'orders.parquet'}'").df()
    o["post_discount_revenue"] = o.post_discount_revenue.astype(float)
    o["discount_percentage"] = o.discount_percentage.astype(float)
    return o


@pytest.fixture(scope="module")
def notebook(tmp_path_factory, bundle):
    out = tmp_path_factory.mktemp("out")
    build.apply(bundle, out)
    (nb,) = (out / "notebooks").glob("*.ipynb")
    return nb


def _tiles(b):
    (d,) = b.dashboards
    return {t.title: t for t in d.tiles if t.kind != "text"}


def test_parameters_cell(bundle, notebook):
    from unshackle.run import notebook_queries

    (d,) = bundle.dashboards
    assert {f.name: f.default for f in d.filters} == DEFAULTS  # the current values, as Tableau stores them (0.10000000000000001)
    allowed = {f.name: f.allowed for f in d.filters}
    assert allowed["growth_rate"] == "from 0.0 to 1.0, step 0.01"
    assert allowed["revenue_target"] == "at least 250000, step 250000"  # a range with no maximum
    assert allowed["measure"] == "one of 'Revenue', 'Quantity', 'Orders' (Order count), '% Discounted'"  # alias; \% unescaped
    assert allowed["start_date"] == "from '2024-01-01' to '2026-12-31', step 1 month"
    assert allowed["include_cancelled"] == "one of True, False" and allowed["max_days"] == "one of 7, 14, 30"
    src = notebook_source(notebook)
    assert "growth_rate = 0.1  # from 0.0 to 1.0, step 0.01" in src
    params, _ = notebook_queries(notebook)
    assert params == DEFAULTS
    assert not [t.todo for t in _tiles(bundle).values() if t.todo]


def test_semantic_layer_todos(bundle):
    metrics = {m.name: m for m in bundle.metrics}
    dims = {d.name: d for sm in bundle.semantic_models for d in sm.dimensions}
    forecast = metrics["forecast_revenue"]
    assert ":churn_rate, :growth_rate" in forecast.todo and "[Churn Rate], [Growth Rate]" in forecast.todo
    assert forecast.source_expression.startswith("[post_discount_revenue] * (1 - [Parameters].[Churn Rate])")
    assert "[Commission Rate]" in metrics["commission"].todo and "[Revenue Target]" in metrics["target_band"].todo
    assert "[Growth Rate]" in metrics["forecast_uplift"].todo  # reads the parameters through [Forecast Revenue]
    switch = metrics["selected_measure"].todo
    assert switch.startswith("measure switch on [Measure]: a metric can't change with a parameter, so make one metric per value")
    assert ("'Revenue' -> `sum(post_discount_revenue)`; 'Quantity' -> `sum(quantity)`; 'Orders' -> `count(distinct order_id)`; "
            "'% Discounted' -> `avg(discount_percentage)`") in switch
    assert "dimension switch on [Group By]" in dims["breakdown"].todo and "'Status' -> `status`; otherwise -> `account_id`" in dims["breakdown"].todo
    for name in ("since_start", "counted", "quick_close", "cancelled_since_start"):
        assert ":" in dims[name].todo and dims[name].expr == ""
    assert not [m for m in bundle.semantic_models for x in m.measures if ":" in (x.expr or "")]  # no bind reaches the semantic layer


def test_tile_sql(bundle):
    t = _tiles(bundle)
    assert "sum((post_discount_revenue * (1 - :churn_rate)) * (1 + :growth_rate)) as forecast_revenue" in t["What If Forecast"].sql
    assert ("case when :measure = 'Revenue' then sum(post_discount_revenue) when :measure = 'Quantity' then sum(quantity) "
            "when :measure = 'Orders' then count(distinct order_id)") in t["Selected Measure by Status"].sql
    assert "case when :group_by = 'Status' then status else account_id end as breakdown" in t["Revenue by Breakdown"].sql
    assert "(date_trunc('week', cast(:start_date as date) + interval '1 day') - interval '1 day')" in t["Revenue Since Start"].sql


def _week_start(day: str) -> pd.Timestamp:
    d = pd.Timestamp(day)
    return d - pd.Timedelta(days=(d.day_of_week + 1) % 7)  # Sunday weeks


def _expected(o: pd.DataFrame, p: dict) -> dict[str, pd.Series]:
    rev = o.post_discount_revenue
    forecast = rev * (1 - p["churn_rate"]) * (1 + p["growth_rate"])
    by_acct = o.groupby("account_id").post_discount_revenue.sum()
    target = p["revenue_target"]
    band = by_acct.map(lambda r: "Above target" if r >= target else "Halfway" if r >= 0.5 * target else "Below")
    measure = {"Revenue": o.groupby("status").post_discount_revenue.sum(), "Quantity": o.groupby("status").quantity.sum(),
               "Orders": o.groupby("status").order_id.nunique(), "% Discounted": o.groupby("status").discount_percentage.mean()}
    breakdown = o.status if p["group_by"] == "Status" else o.account_id
    start = pd.Timestamp(p["start_date"])
    since = o[pd.to_datetime(o.purchased_at) >= _week_start(p["start_date"])]
    counted = o[p["include_cancelled"] | (o.status != "CANCELLED")]
    cancelled = pd.to_datetime(o.cancelled_at)
    cancelled_since = cancelled.ge(start).map({True: "True", False: "False"}).where(cancelled.notna(), "<null>")
    return {
        "What If Forecast": forecast.groupby(o.status).sum(),
        "Commission by Account": by_acct * p["commission_rate"] / 100,
        "Target Band by Account": band,
        "Selected Measure by Status": measure[p["measure"]],
        "Revenue by Breakdown": rev.groupby(breakdown).sum(),
        "Revenue Since Start": since.groupby("status").post_discount_revenue.sum(),
        "Counted Orders": counted.groupby("status").order_id.nunique(),
        "Quick Close Orders": o.groupby((o.days_to_close <= p["max_days"]).map({True: "True", False: "False"})).order_id.nunique(),
        "Cancelled Since Start": o.groupby(cancelled_since).order_id.nunique(),
        "Forecast Uplift by Status": (forecast - rev).groupby(o.status).sum(),
    }


@pytest.mark.parametrize("values", [DEFAULTS, OTHER, {**DEFAULTS, "measure": "Orders", "start_date": "2024-12-01"},
                                    {**DEFAULTS, "measure": "% Discounted"}], ids=["default", "other", "orders", "discounted"])
def test_tiles_compute_tableau_numbers(bundle, notebook, warehouse, orders, values):
    from unshackle.run import notebook_queries, query

    params, sqls = notebook_queries(notebook)
    params = {**params, **values}
    expected = _expected(orders, params)
    for title, tile in _tiles(bundle).items():
        df = query(sqls[tile.id.replace("-", "_")], params, url=f"duckdb:///{warehouse}")
        key, value = df.columns[0], df.columns[1]
        if df[key].dtype == bool or df[key].isna().any() or title == "Quick Close Orders":
            df[key] = df[key].map({True: "True", False: "False"}).fillna("<null>")
        got = df.set_index(key)[value].sort_index()
        exp = expected[title].sort_index()
        assert list(got.index) == list(exp.index), title
        if not pd.api.types.is_numeric_dtype(got):
            assert list(got) == list(exp), title
        else:
            assert got.to_numpy(dtype=float) == pytest.approx(exp.to_numpy(dtype=float), rel=1e-9), title


def _param(xml: str) -> TField:
    import xml.etree.ElementTree as ET

    ds = read_datasource(ET.fromstring(f"<datasource name='Parameters' hasconnection='false'>{xml}</datasource>"), "test", "test.twb")
    (p,) = ds.fields.values()
    return p


def test_parameter_shapes():
    # no value attribute: the current value is the calculation's literal
    p = _param("<column caption='Target Margin' datatype='real' name='[Parameter 1]' param-domain-type='range' role='measure'>"
               "<calculation class='tableau' formula='0.15' /></column>")
    assert (p.value, _param_domain(p)) == ("0.15", "")
    p = _param("<column caption='Sheet' datatype='string' name='[Parameter 2]' param-domain-type='list' role='measure' "
               "value='&quot;Share (\\% passed)&quot;'><calculation class='tableau' formula='&quot;Share (% passed)&quot;' />"
               "<members><member value='&quot;Share (\\% passed)&quot;' /><member value='&quot;Total&quot;' /></members></column>")
    assert p.value == '"Share (% passed)"' and _param_domain(p) == "one of 'Share (% passed)', 'Total'"
    p = _param("<column caption='Cutoff' datatype='datetime' name='[Parameter 3]' param-domain-type='any' role='measure' "
               "value='#2021-01-21 00:00:00#'><calculation class='tableau' formula='#2021-01-21 00:00:00#' /></column>")
    assert (param_bind(p), _param_domain(p)) == ("cast(:cutoff as timestamp)", "")
