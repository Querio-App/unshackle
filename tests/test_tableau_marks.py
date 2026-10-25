"""Tableau chart patterns: Measure Names / Measure Values, dual axis, square marks, maps, text sheets with several measures.

A hand-written workbook over the Dunder Mifflin data (tests/fixtures/tableau_marks): orders, accounts and products joined
(inner joins), and the shippings table for the maps. The tile SQL runs in DuckDB, and the expected rows are computed here
from Tableau's rules:

- Measure Values holds the measures the sheet's Measure Names filter keeps. Each mark becomes one row per measure
  (measure_name, measure_value), NULL values included. A measure is named by its Measure Names alias, else by Tableau's
  name for it: the field's name for its default aggregation (SUM unless Default Properties > Aggregation says otherwise),
  "Avg. x", "Distinct count of x" for others, a calculation's name. The measures follow the Measure Names sort
  (<manual-sort>, the ones it doesn't list after), else their names in order.
- Dual Axis folds a measure's axis onto the previous one (<encoding attr='space' fold='true'>): one chart with a scale
  each, or one scale when the axes are synchronized. Measures on one shelf without it are separate panels.
- Square marks are mark_rect: the innermost dimensions on x and y, the outer ones facet.
- A map places each mark by the finest geographic field in the view: US states and countries are drawn as shapes looked
  up by name, real latitude/longitude fields as points; other places (cities, ZIP codes) as a table.
"""

import datetime as dt
import re
import shutil
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import _split_with

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
WB = "e7800000-0000-4000-8000-000000000001.twb"


def _parse(tmp_path: Path, edit=None):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIXTURES / "tableau_marks", raw)
    if edit:
        p = raw / "workbooks" / WB
        p.write_text(edit(p.read_text()))
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def _tiles(b) -> dict:
    return {t.title: t for d in b.dashboards for t in d.tiles}


@pytest.fixture(scope="module")
def tiles(tmp_path_factory):
    return _tiles(_parse(tmp_path_factory.mktemp("marks")))


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "products", "shippings"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


@pytest.fixture(scope="module")
def rows(con):
    """The data source's rows: orders with their account and product."""
    o, a, p = (con.sql(f"select * from public.{t}").df() for t in ("orders", "accounts", "products"))
    r = o.merge(a, on="account_id").merge(p, on="product_id")
    for c in ("post_discount_revenue", "pre_discount_revenue"):
        r[c] = r[c].astype(float)
    return r


def _measures(df, by: list[str], measures: dict) -> list[tuple]:
    """Tableau's long format: per mark (sorted on `by`) one row per measure, in the given order."""
    groups = df.groupby(by, sort=True) if by else [((), df)]
    out = []
    for key, g in groups:
        key = key if isinstance(key, tuple) else (key,)
        for name, fn in measures.items():
            out.append((*key, name, fn(g)))
    return out


def _same(got: list[tuple], want: list[tuple]) -> bool:
    def norm(v):
        if v is None or v != v:  # NULL / NaN
            return None
        if isinstance(v, dt.datetime):
            return v.date()
        return float(v) if isinstance(v, (int, float)) or type(v).__name__ == "Decimal" else v
    got = [tuple(norm(v) for v in r) for r in got]
    want = [tuple(norm(v) for v in r) for r in want]
    return len(got) == len(want) and all(
        len(a) == len(b) and all(x == pytest.approx(y, rel=1e-9) if isinstance(y, float) and x is not None else x == y for x, y in zip(a, b))
        for a, b in zip(got, want))


def qty(g):
    return g.quantity.sum()


def orders(g):
    return g.order_id.nunique()


def avg_rev(g):
    return g.post_discount_revenue.mean()


def net(g):
    return g.post_discount_revenue.sum()


def gross(g):
    return g.pre_discount_revenue.sum()


# ---------------------------------------------------------------- Measure Names / Measure Values


def test_measure_values_follow_the_measure_names_sort(tiles, con, rows):
    t = tiles["Measures by Paper Type"]
    assert not t.todo and (t.kind, t.mark) == ("chart", "bar")
    order = ["Distinct count of order_id", "quantity", "Average Order Value", "Avg. Revenue"]  # the <manual-sort>, not the filter's order
    assert (t.encodings["x"].field, t.encodings["y"].field) == ("paper_type", "measure_value")
    assert (t.encodings["color"].field, t.encodings["color"].sort) == ("measure_name", order)
    want = _measures(rows, ["paper_type"], {"Distinct count of order_id": orders, "quantity": qty,
                                            "Average Order Value": lambda g: net(g) / orders(g), "Avg. Revenue": avg_rev})
    assert _same(con.sql(t.sql).fetchall(), want)  # in Tableau's order: by paper type, then the measures' order
    assert t.source_config["columns"] == {"paper_type": "paper_type", "measure_name": "Measure Names", "measure_value": "Measure Values"}
    assert t.source_config["measure_names"] == order


def test_measure_values_keep_null_values_and_order_by_name(tiles, con, rows):
    """No Measure Names sort: the measures by name. Cancelled Quantity is NULL for the other statuses; the row stays."""
    t = tiles["Measures by Status"]
    assert (t.kind, t.mark) == ("table", "table") and not t.todo
    cancelled = lambda g: g.quantity[g.status == "CANCELLED"].sum() if (g.status == "CANCELLED").any() else None  # noqa: E731
    want = _measures(rows, ["status"], {"Cancelled Quantity": cancelled, "Net Revenue": net, "quantity": qty})
    got = con.sql(t.sql).fetchall()
    assert _same(got, want)
    assert [r[2] for r in got if r[1] == "Cancelled Quantity"].count(None) == 2  # COMPLETED and RETURNED


def test_measure_names_aliases_and_no_dimensions(tiles, con, rows):
    t = tiles["Order Totals"]
    assert (t.kind, t.mark) == ("table", "table")
    want = _measures(rows, [], {"Distinct count of order_id": orders, "Gross Revenue": gross, "Net Revenue": net, "quantity": qty})
    assert _same(con.sql(t.sql).fetchall(), want)  # aliases name (and so order) the two revenues


def test_measure_names_on_columns_facet_the_measure_values_axis(tiles, con, rows):
    t = tiles["Cycle Overview"]
    assert (t.encodings["x"].field, t.encodings["y"].field, t.encodings["column"].field) == ("measure_value", "payment_cycle", "measure_name")
    assert t.encodings["column"].sort == ["quantity", "Net Revenue"]
    assert _same(con.sql(t.sql).fetchall(), _measures(rows, ["payment_cycle"], {"quantity": qty, "Net Revenue": net}))


def test_measure_names_filter_rules_are_todo(tmp_path: Path):
    def mv_filter(x):
        start = x.index("<worksheet name=\"Order Totals\">")
        return start, x.index("</worksheet>", start)

    def edit_union(x):
        a, b = mv_filter(x)
        return x[:a] + x[a:b].replace("function='union'", "function='except'", 1) + x[b:]

    def drop_filter(x):
        a, b = mv_filter(x)
        return x[:a] + re.sub(r"<filter class='categorical'.*?</filter>\n", "", x[a:b], flags=re.S) + x[b:]

    for name, edit in (("except", edit_union), ("none", drop_filter)):
        t = _tiles(_parse(tmp_path / name, edit))["Order Totals"]
        assert t.sql == "" and "Measure Values" in t.todo and "unpivot" in t.todo, name


def test_measure_values_of_text_are_todo(tmp_path: Path):
    def edit(x):  # MAX of a string field on Measure Values
        a = x.index("<worksheet name=\"Order Totals\">")
        b = x.index("</worksheet>", a)
        sheet = x[a:b].replace("sum:quantity:qk", "max:paper_type:qk").replace("derivation='Sum' name='[max:paper_type", "derivation='Max' name='[max:paper_type")
        sheet = sheet.replace("<column datatype='integer' name='[quantity]' role='measure' type='quantitative' />",
                              "<column datatype='string' name='[paper_type]' role='dimension' type='nominal' />")
        sheet = sheet.replace("column='[quantity]' derivation='Max'", "column='[paper_type]' derivation='Max'")
        return x[:a] + sheet + x[b:]

    t = _tiles(_parse(tmp_path, edit))["Order Totals"]
    assert t.sql == "" and "non-numeric" in t.todo


def test_split_with():
    sql = "with a as (\nselect ')(' as x, \"q)\" from t where (1 = 1)\n),\nb as (\nselect 1\n)\nselect * from a, b"
    assert _split_with(sql) == ("a as (\nselect ')(' as x, \"q)\" from t where (1 = 1)\n),\nb as (\nselect 1\n)", "select * from a, b")
    assert _split_with("select 1") == ("", "select 1")


# ---------------------------------------------------------------- dual axis, panels, squares, text


def test_dual_axis_layers_with_independent_scales(tiles, con, rows):
    t = tiles["Revenue and Orders"]
    assert t.compose == "dual_axis" and [(l.mark, l.encodings["y"].field) for l in t.layers] == [("bar", "revenue"), ("line", "distinct_count_of_order_id")]
    assert t.encodings["x"].field == "month_of_purchased_at" and not t.todo
    month = rows.purchased_at.astype("datetime64[ns]").dt.to_period("M").dt.to_timestamp()
    want = [(m.date(), net(g), orders(g)) for m, g in rows.groupby(month)]
    got = [(r[0], r[1], r[2]) for r in con.sql(t.sql).fetchall()]
    assert _same(got, want)


def test_synchronized_dual_axis_on_columns_shares_one_scale(tiles):
    t = tiles["Revenue Before and After Discount"]
    assert t.compose == "layer" and [(l.mark, l.encodings["x"].field) for l in t.layers] == [("bar", "pre_discount_revenue"), ("point", "revenue")]
    assert t.encodings["y"].field == "paper_type" and not t.todo


def test_measures_without_dual_axis_are_panels(tiles):
    t = tiles["Revenue and Quantity Panels"]
    assert t.compose == "vconcat" and [(l.mark, l.encodings["y"].field) for l in t.layers] == [("bar", "revenue"), ("line", "quantity")]


def test_square_marks_are_rect(tiles, con, rows):
    t = tiles["Revenue Heatmap"]
    assert (t.kind, t.mark) == ("chart", "rect") and not t.todo
    assert {ch: e.field for ch, e in t.encodings.items()} == {"x": "paper_type", "y": "payment_cycle", "color": "revenue"}
    want = sorted((c, p, net(g)) for (p, c), g in rows.groupby(["paper_type", "payment_cycle"]))
    assert _same(sorted(con.sql(t.sql).fetchall()), want)
    h = tiles["Paper Highlights"]  # a highlight table of Measure Values
    assert h.mark == "rect" and (h.encodings["x"].field, h.encodings["y"].field, h.encodings["color"].field) == ("measure_name", "paper_type", "measure_value")
    assert h.encodings["x"].sort == ["Avg. Revenue", "Distinct count of order_id", "quantity"]
    assert _same(con.sql(h.sql).fetchall(), _measures(rows, ["paper_type"], {"Avg. Revenue": avg_rev, "Distinct count of order_id": orders,
                                                                             "quantity": qty}))


def test_text_sheet_with_several_measures_is_one_row(tiles, con, rows):
    t = tiles["Order KPIs"]
    assert (t.kind, t.mark) == ("table", "table") and not t.todo
    assert _same(con.sql(t.sql).fetchall(), [(net(rows), orders(rows), qty(rows))])
    assert list(t.source_config["columns"].values()) == ["Revenue", "Distinct count of order_id", "quantity"]


def test_default_aggregation_names_the_measure(tmp_path: Path):
    """With Default Properties > Aggregation = Average, AVG(Revenue) is named "Revenue", not "Avg. Revenue"."""
    b = _parse(tmp_path, lambda x: x.replace("<column caption='Revenue' datatype='real' name='[post_discount_revenue]'",
                                             "<column aggregation='Avg' caption='Revenue' datatype='real' name='[post_discount_revenue]'", 1))
    t = _tiles(b)["Paper Highlights"]
    assert t.encodings["x"].sort == ["Distinct count of order_id", "Revenue", "quantity"]


# ---------------------------------------------------------------- maps


def test_filled_map_of_us_states(tiles, con):
    t = tiles["Shipping Cost by State"]
    assert (t.kind, t.mark, t.geo) == ("chart", "geoshape", "us_states")
    assert (t.encodings["key"].field, t.encodings["color"].field) == ("shipping_state", "avg_shipping_cost")
    assert "US state names" in t.todo  # the data has state codes, which the shapes' names don't match
    s = con.sql("select * from public.shippings").df()
    want = sorted((k, g.shipping_cost.astype(float).mean()) for k, g in s.groupby("shipping_state"))
    assert _same(sorted(con.sql(t.sql).fetchall()), want)


def test_symbol_map_of_cities_is_a_table(tiles, con):
    t = tiles["Shipments by City"]
    assert (t.kind, t.mark, t.geo) == ("table", "table", "") and "[City].[Name]" in t.todo and t.sql
    assert len(con.sql(t.sql).fetchall()) == con.sql("select count(distinct shipping_city) from public.shippings").fetchall()[0][0]


def test_latitude_longitude_fields_are_points(tiles, con):
    t = tiles["Shipping Points"]
    assert (t.kind, t.mark) == ("chart", "point") and not t.todo
    assert {ch: e.field for ch, e in t.encodings.items()} == {
        "latitude": "avg_shipping_latitude", "longitude": "avg_shipping_longitude", "size": "distinct_count_of_shipping_id", "detail": "shipping_region"}
    s = con.sql("select * from public.shippings").df()
    want = sorted((r, g.shipping_latitude.astype(float).mean(), g.shipping_longitude.astype(float).mean(), g.shipping_id.nunique())
                  for r, g in s.groupby("shipping_region"))
    got = sorted(con.sql(t.sql).fetchall())
    assert _same(got, want)


def test_map_geography_rules(tmp_path: Path):
    ship = "<datasource caption='Shipments' inline='true' name='federated.ship0demo'"
    # a country field: countries; a State field without the US as the default country: a table
    countries = _tiles(_parse(tmp_path / "c", lambda x: x.replace('semantic-role="[State].[Name]"', 'semantic-role="[Country].[ISO3166_2]"')))
    t = countries["Shipping Cost by State"]
    assert (t.mark, t.geo) == ("geoshape", "countries") and "country names" in t.todo
    elsewhere = _tiles(_parse(tmp_path / "s", lambda x: x[:x.index(ship)] + x[x.index(ship):].replace("&quot;United States&quot;", "&quot;Canada&quot;", 1)))
    t = elsewhere["Shipping Cost by State"]
    assert (t.kind, t.geo) == ("table", "") and "[State].[Name]" in t.todo and t.sql
    # a symbol map of states: drawn filled, with a TODO for the circles
    symbol = _tiles(_parse(tmp_path / "y", lambda x: x.replace("<mark class='Multipolygon' />", "<mark class='Automatic' />")))
    assert "symbol map" in symbol["Shipping Cost by State"].todo


# ---------------------------------------------------------------- notebook


def test_notebook_runs_with_every_chart(tmp_path: Path, con):
    """The migrated notebook runs on DuckDB and every chart is valid Vega-Lite."""
    from unshackle.run import run_notebook
    from unshackle import build

    repo = tmp_path / "repo"
    b = _parse(repo)
    build.apply(b, repo)
    db = tmp_path / "warehouse.duckdb"
    con.execute(f"attach '{db}' as wh")
    con.execute("create schema wh.public")
    for t in ("orders", "accounts", "products", "shippings"):
        con.execute(f"create table wh.public.{t} as select * from public.{t}")
    con.execute("detach wh")
    (nb,) = (repo / "notebooks").glob("*.ipynb")
    src = nb.read_text()
    assert "mark_rect()" in src and "mark_geoshape(" in src and "us-atlas@3.0.1/states-10m.json" in src and ".project(type='mercator')" in src
    assert "resolve_scale(y='independent')" in src and "sort=['Distinct count of order_id', 'quantity'" in src
    html = run_notebook(nb, url=f"duckdb:///{db}").read_text()
    charts = sum(1 for t in b.dashboards[0].tiles if t.kind == "chart")
    assert html.count("vegaEmbed(") == charts


def test_dual_axis_on_columns_resolves_x():
    from unshackle.ir import Encoding, Layer, Tile
    from unshackle.target.notebooks import _composed_chart

    tile = Tile("t", mark="bar", sql="x", encodings={"y": Encoding("paper", "nominal")}, compose="dual_axis",
                layers=[Layer("bar", {"x": Encoding("a", "quantitative")}), Layer("point", {"x": Encoding("b", "quantitative")})])
    assert "    .resolve_scale(x='independent')" in _composed_chart("t", tile)
