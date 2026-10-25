"""Tableau aliases, sorts and number formats (tests/fixtures/tableau_display/).

A hand-written workbook over the Dunder Mifflin orders joined to accounts and products. Its sheets alias members, sort
dimensions every way Tableau can, and format numbers. The tiles' SQL runs in DuckDB, and the expected labels and row
order are computed here from Tableau's rules:

- aliases are labels: Tableau shows them instead of the members' values, NULL included when it has one; members without
  one keep their value, and two members with the same label stay two marks;
- natural order (no sort, the data source's order) follows the labels ignoring case (Tableau sorts them itself, also
  on a live connection), NULL first (also when it has an alias); descending reverses it;
- an alphabetic sort compares the labels as text by character code (uppercase before lowercase), NULL read as "Null";
- a manual sort puts the listed members first, in order, whatever its direction, and the rest after them in natural order;
- a computed sort orders the members by an aggregate (shown on the sheet or not); with other dimensions on the sheet,
  each member's aggregate is over the whole view, so its order is the same in every pane;
- a datasource's default sort applies to sheets with no sort of their own on that field.

Formats change no values: they are carried to the notebook's axes, labels and tooltips (d3-format) and to Ossie.
"""

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import pytest
import yaml
from conftest import notebook_source

from unshackle import build
from unshackle.ir import Encoding, Tile
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import SheetSort, TField, alias_sql, d3_format, sheet_sorts
from unshackle.target.notebooks import _encode_args

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
PAPER = {"Copy": "Copy Paper", "Printer": "Printer Paper", "Cardstock": "Zeta Card", "Kraft Roll": "Multipurpose"}
CYCLE = {"": "(no cycle)"}


def _parse(tmp_path: Path, fixture: str = "tableau_display", edit=None):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIXTURES / fixture, raw)
    if edit:
        for p in raw.rglob("*.t[dw][sb]"):
            p.write_text(edit(p.read_text(), p.suffix))
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return _parse(tmp_path_factory.mktemp("display"))


@pytest.fixture(scope="module")
def tiles(bundle) -> dict[str, Tile]:
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
    """The data source's rows: orders inner join accounts and products."""
    return con.sql("select * from public.orders join public.accounts using (account_id) join public.products using (product_id)").df()


def _run(con, tile: Tile) -> list[tuple]:
    assert tile.sql and not tile.todo, tile.todo
    return [tuple(float(v) if type(v).__name__ == "Decimal" else v for v in r) for r in con.sql(tile.sql).fetchall()]


def _marks(rows: pd.DataFrame, by: str | list[str], label=None, agg: str = "sum", col: str = "post_discount_revenue") -> pd.DataFrame:
    """One mark per member (NULL is a member), with its label."""
    g = rows.groupby(by, dropna=False)[col].agg(agg).reset_index(name="value")
    if label is not None:
        g["label"] = g[by].map(lambda v: label.get(v, v) if v is not None and not pd.isna(v) else label.get(None))
    return g


def _natural(labels: list, desc: bool = False) -> list:
    """Tableau's natural order of labels: NULL first, then the labels; descending reverses it."""
    out = [x for x in labels if x is None] + sorted((x for x in labels if x is not None), key=lambda x: (x.lower(), x))
    return out[::-1] if desc else out


def _alphabetic(labels: list, desc: bool = False) -> list:
    return sorted(labels, key=lambda x: "Null" if x is None else x, reverse=desc)


# --------------------------------------------------------------- parsing


def test_sheet_sorts_read_every_kind_and_the_datasource_default():
    view = ET.fromstring(
        "<view><computed-sort column='[ds].[none:a:nk]' direction='DESC' using='[ds].[sum:x:qk]' />"
        "<alphabetic-sort column='[ds].[none:b:nk]' direction='ASC' />"
        "<manual-sort column='[ds].[none:c:nk]' direction='ASC'><dictionary><bucket>&quot;W&quot;</bucket><bucket>%null%</bucket>"
        "</dictionary></manual-sort><natural-sort column='[ds].[none:d:nk]' direction='DESC' /><filter column='[ds].[none:a:nk]' /></view>")
    defaults = ET.fromstring("<default-sorts><natural-sort column='[none:a:nk]' direction='ASC' />"
                             "<alphabetic-sort column='[none:e:nk]' direction='DESC' /></default-sorts>")
    sorts = sheet_sorts(view, list(defaults))
    assert sorts == {
        "none:a:nk": SheetSort("none:a:nk", "computed", "DESC", using="sum:x:qk"),  # the sheet's own wins over the default
        "none:b:nk": SheetSort("none:b:nk", "alphabetic", "ASC"),
        "none:c:nk": SheetSort("none:c:nk", "manual", "ASC", members=['"W"', "%null%"]),
        "none:d:nk": SheetSort("none:d:nk", "natural", "DESC"),
        "none:e:nk": SheetSort("none:e:nk", "alphabetic", "DESC"),
    }
    assert sheet_sorts(None) == {}


@pytest.mark.parametrize("fmt, d3", [
    ('c"$"#,##0.00;("$"#,##0.00)', "($,.2f"),
    ('c"$"#,##0;-"$"#,##0', "$,.0f"),
    ("n#,##0;-#,##0", ",.0f"),
    ("n0.000", ".3f"),
    ("p0.0%", ".1%"),
    ("p0%", ".0%"),
    ('c"$"#,##0,K;("$"#,##0,K)', ""),  # thousands: d3-format can't scale
    ('n#,##0.0"%";-#,##0.0"%"', ""),  # a literal % sign, not a percentage
    ("n#,##0;(#,##0.00)", ""),  # negatives with other decimals
    ("C1033%", ""),  # locale "standard" formats
    ("*yyyy-mm-dd", ""),
    ("", ""),
])
def test_number_formats_convert_only_when_exact(fmt: str, d3: str):
    assert d3_format(fmt) == d3


def test_alias_sql_keeps_unaliased_members_and_null():
    f = TField("x", datatype="string", aliases=[('"a"', "A"), ("%null%", "none"), ('"it\'s"', "it is")])
    assert alias_sql("x", f) == "case when x = 'a' then 'A' when x is null then 'none' when x = 'it''s' then 'it is' else x end"
    b = TField("b", datatype="boolean", aliases=[("true", "Yes")])
    assert alias_sql("b", b) == "case when b then 'Yes' when not b then 'False' end"
    assert alias_sql("n", TField("n", datatype="integer", aliases=[("1", "one")])) is None


# --------------------------------------------------------------- labels


def test_aliases_label_members_and_keep_the_rest(con, rows, tiles):
    got = _run(con, tiles["Aliases"])
    marks = _marks(rows, "paper_type", PAPER)
    # Kraft Roll is aliased "Multipurpose", like the real Multipurpose: two marks with one label, as in Tableau
    assert sorted(got) == sorted(zip(marks.label, marks.value))
    assert [label for label, _ in got].count("Multipurpose") == 2
    assert {label for label, _ in got} == {"Copy Paper", "Printer Paper", "Zeta Card", "Multipurpose"}
    cycles = dict(_run(con, tiles["Manual"]))
    assert "(no cycle)" in cycles and "" not in cycles  # an alias of the empty string


def test_null_has_its_alias_and_booleans_show_true_false(con, rows, tiles):
    open_status = rows.status.where(rows.status != "COMPLETED")
    marks = rows.assign(s=open_status).groupby("s", dropna=False).post_discount_revenue.sum()
    labels = {"RETURNED": "Sent back"}
    want = {("Still open" if pd.isna(k) else labels.get(k, k)): v for k, v in marks.items()}
    got = _run(con, tiles["Null Alias"])
    assert dict(got) == pytest.approx(want)
    assert [label for label, _ in got] == ["Still open", "CANCELLED", "Sent back"]  # NULL first, also with an alias
    disc = rows.discount_percentage.astype(float)
    want = {"Discounted": rows.post_discount_revenue[disc > 10].sum(), "False": rows.post_discount_revenue[disc <= 10].sum(),
            None: rows.post_discount_revenue[disc.isna()].sum()}  # NULL has no alias: it stays NULL
    assert dict(_run(con, tiles["Discounted"])) == pytest.approx({k: float(v) for k, v in want.items()})


# --------------------------------------------------------------- row order


def test_natural_order_follows_labels(con, rows, tiles):
    labels = list(_marks(rows, "paper_type", PAPER).label)
    assert [r[0] for r in _run(con, tiles["Aliases"])] == _natural(labels)
    assert [r[0] for r in _run(con, tiles["Natural Desc"])] == _natural(labels, desc=True)
    raw = [None if pd.isna(v) else v for v in _marks(rows.assign(s=rows.status.where(rows.status != "COMPLETED")), "s").s]
    assert [r[0] for r in _run(con, tiles["Null Raw Natural Desc"])] == _natural(raw, desc=True) == ["RETURNED", "CANCELLED", None]


def test_alphabetic_sort_compares_labels_by_character_code(con, rows, tiles):
    labels = list(_marks(rows, "paper_type", PAPER).label)
    assert [r[0] for r in _run(con, tiles["Alpha Desc"])] == _alphabetic(labels, desc=True)
    # NULL sorts as "Null": between CANCELLED and RETURNED
    assert [r[0] for r in _run(con, tiles["Null Raw Desc"])] == ["RETURNED", None, "CANCELLED"]
    mixed = list(rows.status.replace({"CANCELLED": "cancelled"}).unique())
    # natural order ignores case; alphabetic doesn't
    assert [r[0] for r in _run(con, tiles["Mixed Case"])] == _natural(mixed) == ["cancelled", "COMPLETED", "RETURNED"]
    assert [r[0] for r in _run(con, tiles["Mixed Case Desc"])] == _alphabetic(mixed, desc=True) == ["cancelled", "RETURNED", "COMPLETED"]


def test_manual_sort_lists_members_then_the_rest(con, rows, tiles):
    listed = ["WEEK", "MONTH", "ANNUAL"]  # the sheet's sort is DESC: a manual sort ignores it
    rest = _natural([CYCLE.get(c, c) for c in rows.payment_cycle.unique() if c not in listed])
    assert [r[0] for r in _run(con, tiles["Manual"])] == listed + rest == ["WEEK", "MONTH", "ANNUAL", "(no cycle)", "BIWEEK"]
    # the datasource's default sort (Default Properties > Sort) on a sheet without one
    assert [r[0] for r in _run(con, tiles["Default Sort"])] == ["RETURNED", "COMPLETED", "CANCELLED"]


def test_computed_sort_orders_by_the_aggregate(con, rows, tiles):
    marks = _marks(rows, "paper_type", PAPER)
    want = list(marks.sort_values("value", ascending=False, kind="stable").label)
    assert [r[0] for r in _run(con, tiles["Computed Desc"])] == want
    # by a measure the sheet doesn't show
    orders = _marks(rows, "paper_type", PAPER, agg="nunique", col="order_id").sort_values(["value", "label"], kind="stable")
    got = _run(con, tiles["Computed Hidden"])
    assert [r[0] for r in got] == list(orders.label)
    quantity = _marks(rows, "paper_type", PAPER, col="quantity")
    assert sorted(got) == pytest.approx(sorted(zip(quantity.label, quantity.value)))
    # an aggregated LOD the sheet shows: avg({FIXED [paper_type] : SUM(revenue)}) per paper type is the type's revenue
    got = _run(con, tiles["Paper Revenue Share"])
    assert [r[0] for r in got] == want and [r[1] for r in got] == pytest.approx(sorted(marks.value, reverse=True))


def test_nested_computed_sort_uses_the_whole_view(con, rows, tiles):
    got = _run(con, tiles["Nested Computed"])
    overall = list(rows.groupby("paper_type").post_discount_revenue.sum().sort_values(ascending=False).index)
    cycles = _natural(list(dict.fromkeys(CYCLE.get(c, c) for c in rows.payment_cycle)))
    assert list(dict.fromkeys(r[0] for r in got)) == cycles
    for raw_cycle in rows.payment_cycle.unique():
        present = set(rows.paper_type[rows.payment_cycle == raw_cycle])
        want = [PAPER.get(p, p) for p in overall if p in present]  # the same order in every pane
        assert [r[1] for r in got if r[0] == CYCLE.get(raw_cycle, raw_cycle)] == want
    # an average isn't a sum of the panes' averages: that sort stays a TODO, the rows keep natural order
    assert "with other dimensions on the sheet not applied" in tiles["Nested Computed Avg"].todo


def test_sorted_dimensions_keep_the_rows_order_in_charts(tiles):
    assert tiles["Computed Desc"].encodings["y"].sort == "none"
    assert tiles["Aliases"].encodings["y"].sort == ""  # natural: Altair's ascending order of the labels
    args, _ = _encode_args({"y": tiles["Computed Desc"].encodings["y"]})
    assert args == ["y=alt.Y('paper_type:N', title='paper_type', sort=None)"]


def test_top_n_keeps_its_order_and_notes_the_sort(tmp_path: Path):
    top = ("<filter class='categorical' column='[federated.display0demo].[none:paper_type:nk]'><groupfilter count='3' end='top' "
           "function='end' units='records' user:ui-marker='end' user:ui-top-by-field='true' xmlns:user='http://www.tableausoftware.com/xml/user'>"
           "<groupfilter direction='DESC' expression='SUM([post_discount_revenue])' function='order'>"
           "<groupfilter function='level-members' level='[none:paper_type:nk]' /></groupfilter></groupfilter></filter>\n")
    b = _parse(tmp_path, edit=lambda x, _: x.replace(
        "          <alphabetic-sort column='[federated.display0demo].[none:paper_type:nk]' direction='DESC' />\n",
        "          <alphabetic-sort column='[federated.display0demo].[none:paper_type:nk]' direction='DESC' />\n" + top))
    tile = next(t for d in b.dashboards for t in d.tiles if t.title == "Alpha Desc")
    assert tile.sql.endswith("limit 3") and "Top N filter's order is kept" in tile.todo


# --------------------------------------------------------------- formats


def test_formats_reach_axes_tooltips_and_the_tile(tiles):
    e = tiles["Computed Desc"].encodings
    assert e["x"].format == "($,.2f" and e["y"].format == ""
    assert tiles["Computed Desc"].source_config["formats"] == {"revenue": 'c"$"#,##0.00;("$"#,##0.00)'}
    q = tiles["Quantity"]
    assert q.encodings["x"].format == ",.0f"
    assert q.encodings["tooltip"].format == ""  # a count of Revenue isn't currency
    assert q.source_config["formats"] == {"quantity": "n#,##0;-#,##0"}
    f = tiles["Formats"]  # the sheet's own format wins; a format d3 can't write stays only in source_config
    assert f.encodings["tooltip"].format == ".1%" and f.source_config["formats"]["avg_discount_percentage"] == "p0.0%"
    assert tiles["Paper Revenue Share"].encodings["x"].format == ".1%"


def test_notebook_formats_axes_and_tooltips():
    args, _ = _encode_args({"x": Encoding("revenue", "quantitative", title="Revenue", format="$,.0f"),
                            "text": Encoding("n", "quantitative", format=",.0f"),
                            "tooltip": Encoding("share", "quantitative", format=".1%")})
    assert args == ["x=alt.X('revenue:Q', title='Revenue', axis=alt.Axis(format='$,.0f'))", "text=alt.Text('n:Q', format=',.0f')",
                    "tooltip=[alt.Tooltip('share:Q', format='.1%')]"]


def test_formats_and_aliases_reach_ossie(bundle, tmp_path: Path):
    shutil.copytree(FIXTURES / "tableau_display", tmp_path / "migration" / "raw" / "tableau")
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    build.apply(b, tmp_path)
    docs = {p.name: yaml.safe_load(p.read_text()) for p in (tmp_path / "semantic").glob("*.ossie.yaml")}

    def ext(item: dict) -> dict:
        return json.loads(next(e["data"] for e in item.get("custom_extensions", []) if e["vendor_name"] == "UNSHACKLE"))

    orders = next(ds for ds in docs["orders.ossie.yaml"]["datasets"] if ds["name"] == "orders")
    revenue = next(f for f in orders["fields"] if f.get("label") == "Revenue")
    assert ext(revenue) == {"format": "($,.2f", "source_format": 'c"$"#,##0.00;("$"#,##0.00)'}
    metric = next(m for m in orders["metrics"] if m["name"].startswith("revenue"))
    assert ext(metric)["format"] == "($,.2f"
    discount = next(f for f in orders["fields"] if f["name"].startswith("discount_percentage"))
    assert ext(discount) == {"source_format": 'c"$"#,##0,K'}  # no exact d3 form: only Tableau's
    products = next(ds for ds in docs["products.ossie.yaml"]["datasets"] if ds["name"] == "products")
    paper = next(f for f in products["fields"] if f["name"] == "paper_type")
    assert ext(paper)["value_labels"] == [{"value": k, "label": v} for k, v in PAPER.items()]
    dims = {d.label: d for sm in bundle.semantic_models for d in sm.dimensions}
    assert dims["Open Status"].value_labels == [{"value": None, "label": "Still open"}, {"value": "RETURNED", "label": "Sent back"}]
    notebook_source(next((tmp_path / "notebooks").glob("*.ipynb")))


# --------------------------------------------------------------- published datasources


def test_published_fields_take_the_published_aliases(tmp_path: Path):
    """A workbook can't alias a published datasource's fields: the published aliases apply. Its number formats do apply."""
    def edit(xml: str, suffix: str) -> str:
        if suffix == ".tds":
            return xml.replace("<column caption='Revenue'", "<column datatype='string' name='[paper_type]' role='dimension' type='nominal'>"
                               "<aliases><alias key='&quot;Copy&quot;' value='Copy Paper' /></aliases></column>\n  <column caption='Revenue'", 1)
        return xml.replace("<column caption='Revenue' datatype='real' name='[post_discount_revenue]' role='measure' type='quantitative' />",
                           "<column caption='Revenue' datatype='real' default-format='n#,##0' name='[post_discount_revenue]' role='measure' "
                           "type='quantitative' /><column datatype='string' name='[paper_type]' role='dimension' type='nominal'><aliases>"
                           "<alias key='&quot;Printer&quot;' value='Printers' /></aliases></column>", 1)

    b = _parse(tmp_path, "tableau_published", edit)
    tile = next(t for d in b.dashboards for t in d.tiles if t.title == "Revenue by Paper Type")
    assert "'Copy Paper'" in tile.sql and "Printers" not in tile.sql
    assert tile.encodings["x"].format == ",.0f"


@pytest.mark.parametrize("warehouse", ["redshift", "snowflake", "bigquery", "databricks"])
def test_every_dialect_returns_the_same_order(con, tmp_path: Path, warehouse: str):
    sqlglot = pytest.importorskip("sqlglot")
    base = {t.title: t.sql for d in _parse(tmp_path / "default").dashboards for t in d.tiles if t.sql}
    toml = f'[datasources.warehouse]\ntype = "{warehouse}"\n'
    (tmp_path / warehouse).mkdir()
    (tmp_path / warehouse / "unshackle.toml").write_text(toml)
    forms = {t.title: t.sql for d in _parse(tmp_path / warehouse).dashboards for t in d.tiles if t.sql}
    assert forms.keys() == base.keys()
    for name, sql in forms.items():
        if warehouse != "redshift":  # DuckDB reads the Redshift form as written
            sql = sqlglot.transpile(sql, read=warehouse, write="duckdb")[0]
        assert [r[0] for r in con.sql(sql).fetchall()] == [r[0] for r in con.sql(base[name]).fetchall()], (warehouse, name)


def test_relationship_tiles_label_and_sort_too(tmp_path: Path):
    """A sheet over related tables (measures per table, joined on the dimensions) keeps the values until the last select:
    two members with one label stay two marks, and the sort orders the joined marks."""
    duckdb = pytest.importorskip("duckdb")
    sheet = "Revenue and Shipping Cost by Payment Cycle"
    aliases = "<aliases><alias key='&quot;WEEK&quot;' value='MONTH' /><alias key='&quot;&quot;' value='(no cycle)' /></aliases>"
    sort = ("<computed-sort column='[federated.rel0demo].[none:payment_cycle:nk]' direction='DESC' "
            "using='[federated.rel0demo].[sum:post_discount_revenue:qk]' />")

    def edit(xml: str, _: str) -> str:
        xml = xml.replace("<column datatype='string' name='[payment_cycle]' role='dimension' type='nominal' />",
                          f"<column datatype='string' name='[payment_cycle]' role='dimension' type='nominal'>{aliases}</column>", 1)
        i = xml.index(f'<worksheet name="{sheet}">')
        j = xml.index("<aggregation value='true' />", i)
        return xml[:j] + sort + xml[j:]

    plain = next(t for d in _parse(tmp_path / "plain", "tableau_relationships").dashboards for t in d.tiles if t.title == sheet)
    tile = next(t for d in _parse(tmp_path / "edited", "tableau_relationships", edit).dashboards for t in d.tiles if t.title == sheet)
    assert tile.sql and not tile.todo, tile.todo
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "shippings"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    want = sorted(((({"WEEK": "MONTH", "": "(no cycle)"}.get(r[0], r[0])),) + tuple(r[1:]) for r in c.sql(plain.sql).fetchall()),
                  key=lambda r: -(r[1] or 0))
    got = c.sql(tile.sql).fetchall()
    assert [r[0] for r in got].count("MONTH") == 2
    assert got == want
