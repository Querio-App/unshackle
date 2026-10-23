"""Dashboards -> Jupyter notebooks: structure for every source, multi-view charts, execution."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import altair as alt
import nbformat
import pandas as pd
import pytest

from unshackle import build
from unshackle.ir import Dashboard, DashboardFilter, Encoding, Layer, Tile
from unshackle.raw import RawStore
from unshackle.run import notebook_queries, run_notebook
from unshackle.sources import TOOLS as SOURCES, get_source
from unshackle.target.notebooks import build_notebook

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TOOLS = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir() and p.name in SOURCES)
TABLEAU_WB = FIXTURES / "tableau" / "workbooks" / "3f2a9c1e-7b4d-4e8a-9c21-5d6e7f809a1b.twb"
DS = "[federated.0hgpf0j1fdpvv316shikk0mmdlec]"


def _parse(repo: Path, tool: str, fixture: Path | None = None):
    shutil.copytree(fixture or FIXTURES / tool, repo / "migration" / "raw" / tool)
    return get_source(tool).parse(RawStore(repo, tool))


@pytest.mark.parametrize("tool", TOOLS)
def test_every_source_writes_valid_notebooks(tmp_path: Path, tool: str):
    bundle = _parse(tmp_path, tool)
    assert bundle.dashboards
    build.apply(bundle, tmp_path)
    paths = sorted((tmp_path / "notebooks").glob("*.ipynb"))
    assert len(paths) == len(bundle.dashboards) and not list((tmp_path / "notebooks").glob("*.py"))
    for d in bundle.dashboards:
        path = tmp_path / "notebooks" / f"{d.slug}.ipynb"
        nb = nbformat.read(str(path), as_version=4)
        nbformat.validate(nb)
        assert nb.nbformat == 4 and nb.metadata.kernelspec.name == "python3"
        cells = nb.cells
        assert cells[0].cell_type == "markdown" and cells[0].source.startswith(f"# {d.name}")
        assert "UNSHACKLE_WAREHOUSE_URL" in cells[1].source
        params = [c for c in cells if "parameters" in c.metadata.get("tags", [])]
        assert len(params) == 1 and params[0] is cells[2]
        for c in cells:
            if c.cell_type == "code":
                assert c.outputs == [] and c.execution_count is None
                compile(c.source, c.id, "exec")
        defaults, sqls = notebook_queries(path)
        assert defaults == {f.name: f.default for f in d.filters if f.name.isidentifier()}
        tiles = [t for t in d.tiles if t.kind != "text"]
        assert len(sqls) == len(tiles)
        assert sorted(s.strip() for s in sqls.values()) == sorted(t.sql.strip().replace("\r\n", "\n") for t in tiles)  # the writer normalises newlines
        markdown = "\n".join(c.source for c in cells if c.cell_type == "markdown")
        assert all(t.title in markdown for t in d.tiles if t.title)


def test_cells_follow_layout_and_bind_filters():
    d = Dashboard(
        "Revenue",
        "revenue",
        tiles=[
            Tile("a", "Second", kind="table", sql="select 2 as n where region = :region"),
            Tile("b", "First", mark="bar", sql="select 1 as n", encodings={"x": Encoding("n", "quantitative")}),
            Tile("c", "", kind="text", text="Read **this** first."),
        ],
        layout=[["c"], ["b", "a"]],
        filters=[DashboardFilter("region", default="EU"), DashboardFilter("class", default=None)],
    )
    nb = build_notebook(d, "demo")
    ids = [c["id"] for c in nb["cells"]]
    assert ids == ["title", "setup", "parameters", "c", "first-title", "first-sql", "first-chart", "second-title", "second-sql"]
    assert nb["cells"][2]["source"].splitlines()[1:] == ["region = 'EU'", "tile_class = None"]
    assert "second = pd.read_sql(text(second_sql), engine, params={'region': region})" in nb["cells"][-1]["source"]
    assert nb["cells"][3]["source"] == "Read **this** first."
    assert nb["metadata"]["unshackle"]["layout"] == [["c"], ["first", "second"]]


def test_long_tile_titles_keep_cell_ids_unique():
    title = "Sales Amount, Sales Amount (LY), Margin, Sales Amount (12M average) by Month"
    d = Dashboard("D", "d", tiles=[Tile("a", title, mark="bar", sql="select 1"), Tile("b", title + " and Year", mark="bar", sql="select 1")])
    nb = build_notebook(d, "demo")
    ids = [c["id"] for c in nb["cells"]]
    assert len(ids) == len(set(ids)) and all(len(i) <= 64 for i in ids)
    nbformat.validate(nbformat.from_dict(nb))


def _chart(tile: Tile) -> dict:
    nb = build_notebook(Dashboard("D", "d", tiles=[tile]), "demo")
    code = next(c["source"] for c in nb["cells"] if c["id"].endswith("-chart"))
    df = pd.DataFrame({"month": pd.to_datetime(["2025-01-01", "2025-02-01"]), "region": ["EU", "US"], "sales": [1, 2], "profit": [3, 4]})
    ns = {"alt": alt, "pd": pd, "t": df}
    exec(code, ns)
    return ns["t_chart"].to_dict()  # validates against the Vega-Lite schema


def test_facet_layer_and_concat_charts_are_valid_vega_lite():
    month, region = Encoding("month", "temporal"), Encoding("region", "nominal")
    sales, profit = Encoding("sales", "quantitative"), Encoding("profit", "quantitative")
    spec = _chart(Tile("t", mark="bar", sql="x", encodings={"x": month, "y": sales, "row": region, "column": region}))
    assert set(spec["encoding"]) == {"x", "y", "row", "column"}
    spec = _chart(Tile("t", mark="line", sql="x", encodings={"x": month, "y": sales, "facet": region}))
    assert spec["encoding"]["facet"]["columns"] == 3
    two = [Layer("bar", {"y": sales}), Layer("line", {"y": profit})]
    spec = _chart(Tile("t", mark="bar", sql="x", encodings={"x": month, "column": region}, layers=two))
    assert spec["facet"]["column"]["field"] == "region" and [l["mark"]["type"] for l in spec["spec"]["layer"]] == ["bar", "line"]
    assert spec["spec"]["layer"][0]["encoding"]["color"] == {"datum": "sales"}  # one legend entry per series
    spec = _chart(Tile("t", mark="bar", sql="x", encodings={"x": month}, layers=two, compose="dual_axis"))
    assert spec["resolve"] == {"scale": {"y": "independent"}}
    spec = _chart(Tile("t", mark="bar", sql="x", encodings={"x": month, "row": region}, layers=two, compose="vconcat"))
    assert len(spec["vconcat"]) == 2 and spec["vconcat"][0]["encoding"]["row"]["field"] == "region"


def _tableau_variant(tmp_path: Path, old: str, new: str) -> Path:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURES / "tableau", fixture)
    wb = fixture / "workbooks" / TABLEAU_WB.name
    text = wb.read_text(encoding="utf-8")
    profit = "<column datatype='real' name='[Profit]' role='measure' type='quantitative' />\n" \
             "            <column-instance column='[Profit]' derivation='Sum' name='[sum:Profit:qk]' pivot='key' type='quantitative' />\n"
    anchor = "<column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />\n"
    start = text.index("<worksheet name='Sales Trend'>")
    end = text.index("</worksheet>", start)
    sheet = text[start:end].replace(anchor, anchor + "            " + profit, 1)
    assert old in sheet
    wb.write_text(text[:start] + sheet.replace(old, new) + text[end:], encoding="utf-8")
    return fixture


def _sales_trend(tmp_path: Path, old: str, new: str) -> Tile:
    repo = tmp_path / "repo"
    b = _parse(repo, "tableau", _tableau_variant(tmp_path, old, new))
    return next(t for d in b.dashboards for t in d.tiles if t.id == "sales-trend")


ROWS = f"<rows>{DS}.[sum:Sales:qk]</rows>"


def test_tableau_trellis_becomes_row_facet(tmp_path: Path):
    tile = _sales_trend(tmp_path, ROWS, f"<rows>({DS}.[none:Region:nk] / {DS}.[sum:Sales:qk])</rows>")
    assert tile.encodings["row"].field == "region" and tile.encodings["y"].field == "sales"
    assert "nested dimensions" not in tile.todo


def test_tableau_measures_on_rows_become_stacked_panels(tmp_path: Path):
    tile = _sales_trend(tmp_path, ROWS, f"<rows>({DS}.[sum:Sales:qk] / {DS}.[sum:Profit:qk])</rows>")
    assert tile.compose == "vconcat" and [l.encodings["y"].field for l in tile.layers] == ["sales", "profit"]
    assert "y" not in tile.encodings and "several measures" not in tile.todo


def test_tableau_dual_axis_becomes_layers(tmp_path: Path):
    second_pane = (
        f"</pane>\n          <pane id='1' y-axis-name='{DS}.[sum:Profit:qk]'>\n"
        "            <mark class='Line' />\n          </pane>"
    )
    repo = tmp_path / "repo"
    fixture = _tableau_variant(tmp_path, ROWS, f"<rows>({DS}.[sum:Sales:qk] + {DS}.[sum:Profit:qk])</rows>")
    wb = fixture / "workbooks" / TABLEAU_WB.name
    text = wb.read_text(encoding="utf-8")
    start = text.index("<worksheet name='Sales Trend'>")
    i = text.index("</pane>", start)
    wb.write_text((text[:i] + second_pane + text[i + len("</pane>"):]).replace("<mark class='Automatic' />", "<mark class='Bar' />", 1), encoding="utf-8")
    tile = next(t for d in _parse(repo, "tableau", fixture).dashboards for t in d.tiles if t.id == "sales-trend")
    assert tile.compose == "dual_axis" and [(l.mark, l.encodings["y"].field) for l in tile.layers] == [("bar", "sales"), ("line", "profit")]
    assert "dual axis" not in tile.todo


def test_notebook_executes_against_demo_warehouse(tmp_path: Path):
    """Build the Tableau fixture with a trellis + stacked-panel sheet and run it end to end on DuckDB."""
    repo = tmp_path / "repo"
    fixture = _tableau_variant(tmp_path, ROWS, f"<rows>({DS}.[sum:Sales:qk] / {DS}.[sum:Profit:qk])</rows>")
    wb = fixture / "workbooks" / TABLEAU_WB.name
    wb.write_text(wb.read_text(encoding="utf-8").replace(
        f"<cols>{DS}.[tmn:Order Date:qk]</cols>", f"<cols>({DS}.[none:Region:nk] / {DS}.[tmn:Order Date:qk])</cols>", 1), encoding="utf-8")
    build.apply(_parse(repo, "tableau", fixture), repo)
    db = tmp_path / "warehouse.duckdb"
    subprocess.run([sys.executable, str(ROOT / "examples/tableau-demo/make_warehouse.py"), str(db)], check=True, capture_output=True)
    path = repo / "notebooks/executive-overview.ipynb"
    before = path.read_text()
    assert "alt.vconcat(" in before and "column=alt.Column('region:N'" in before
    html = run_notebook(path, url=f"duckdb:///{db}").read_text()
    assert path.read_text() == before  # running never writes outputs into the notebook
    specs = [json.loads(s.split(", ", 1)[1].rsplit(");", 1)[0]) for s in html.split("vegaEmbed(")[1:]]
    trend = next(s for s in specs if "vconcat" in s)
    assert [p["encoding"]["y"]["field"] for p in trend["vconcat"]] == ["sales", "profit"]
    data = next(iter(trend["datasets"].values()))
    assert {r["region"] for r in data} == {"East", "West"}  # the sheet's filter still applies


def test_lightdash_combo_chart_becomes_dual_axis_layers(tmp_path: Path):
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURES / "lightdash", fixture)
    chart = fixture / "charts" / "monthly-revenue.yaml"
    chart.write_text(chart.read_text().replace(
        "              field: orders_total_revenue\n",
        "              field: orders_total_revenue\n"
        "        - type: line\n          yAxisIndex: 1\n          encode:\n            xRef:\n              field: orders_created_at_month\n"
        "            yRef:\n              field: orders_order_count\n", 1).replace("        - type: line\n          yAxisIndex: 0", "        - type: bar\n          yAxisIndex: 0", 1))
    b = _parse(tmp_path / "repo", "lightdash", fixture)
    tile = next(t for d in b.dashboards for t in d.tiles if t.title == "Monthly revenue")
    assert tile.compose == "dual_axis"
    assert [(l.mark, l.encodings["y"].field) for l in tile.layers] == [("bar", "orders_total_revenue"), ("line", "orders_order_count")]
    assert "y" not in tile.encodings and "mixed series" not in tile.todo
