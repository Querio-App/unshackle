"""Tableau dashboards: filter actions, filters shown on several sheets, other actions, stories, hidden fields and folders.

A hand-written workbook over the Dunder Mifflin orders joined to accounts (tests/fixtures/tableau_dashboards):

- a dashboard with four sheets. "Filter by Status" (a filter action) filters three of them by the status selected on
  "Revenue by Status"; "Drill into Cycle and Year" (all fields) filters "Quantity by Manager and Cycle" by the payment cycle
  and year selected on "Revenue by Cycle and Year". Payment Cycle is a filter control applied to three sheets (one filter
  group) and Purchased a date range applied to three others. A highlight, a URL and a set action, a web zone, a phone layout;
- a story: the dashboard, a sheet with a captured filter (monthly accounts), and a sheet with a captured shelf change;
- hidden fields, used and unused, Data pane folders and field descriptions.

Tiles bind each filter as a notebook parameter. Each tile runs in DuckDB with parameter values set and is compared with the
same rows filtered in pandas the way Tableau does: a filter action keeps the rows whose fields equal the selected mark's values
(no selection: all rows, as when the selection is cleared); a filter applied to several sheets filters each of them; a date
range keeps both ends.
"""

import re
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml
from conftest import notebook_source

from unshackle import build
from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_dashboards"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
DEFAULTS = {"payment_cycle": None, "purchased_from": "2024-03-01", "purchased_to": "2025-12-31", "selected_status": None,
            "selected_payment_cycle": None, "selected_year_of_purchased": None}


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dashboards")
    shutil.copytree(FIX, tmp / "migration" / "raw" / "tableau")
    b = get_source("tableau").parse(RawStore(tmp, "tableau"))
    build.apply(b, tmp)
    return b, tmp


@pytest.fixture(scope="module")
def bundle(out):
    return out[0]


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path_factory.mktemp("wh") / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema public")
    for t in ("orders", "accounts"):
        con.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    con.close()
    return db


@pytest.fixture(scope="module")
def rows() -> pd.DataFrame:
    duckdb = pytest.importorskip("duckdb")
    r = duckdb.sql(f"select o.*, a.account_manager, a.payment_cycle from '{DATA / 'orders.parquet'}' o "
                   f"join '{DATA / 'accounts.parquet'}' a using (account_id)").df()
    r["post_discount_revenue"] = r.post_discount_revenue.astype(float)
    r["purchased_at"] = pd.to_datetime(r.purchased_at)
    return r


def _board(b, name):
    return next(d for d in b.dashboards if d.name == name)


def _notebook(tmp: Path, slug: str) -> Path:
    return tmp / "notebooks" / f"{slug}.ipynb"


def test_parameters_cell(bundle, out):
    from unshackle.run import notebook_queries

    d = _board(bundle, "Sales Dashboard")
    assert {f.name: f.default for f in d.filters} == DEFAULTS  # the saved selections: all cycles, the saved date range, no marks selected
    notes = {f.name: f.note for f in d.filters}
    assert notes["payment_cycle"] == ("dashboard filter on [Payment Cycle], applied to Revenue by Status, Revenue by Cycle and Year, "
                                      "Orders by Account Manager; one value, None = all")
    assert notes["selected_status"] == "filter action Filter by Status: the selected mark on Revenue by Status; None = no selection (all rows)"
    nb = _notebook(out[1], d.slug)
    src = notebook_source(nb)
    assert "# filter action Filter by Status: the selected mark on Revenue by Status; None = no selection (all rows)\nselected_status = None" in src
    params, _ = notebook_queries(nb)
    assert params == DEFAULTS
    assert not [t.todo for t in d.tiles if t.todo]


def test_binds_are_typed(bundle):
    """Postgres can't type a bare `:x is null`, so every bind is cast."""
    for d in bundle.dashboards:
        for t in d.tiles:
            assert not re.search(r"(?<![(:]):\w+", re.sub(r"cast\(:\w+ as [\w ]+\)", "", t.sql)), t.title


def _expected(r: pd.DataFrame, p: dict) -> dict[str, pd.Series]:
    def keep(df, *checks):
        for field, value in checks:
            if value is not None:
                df = df[df[field] == value]
        return df

    dates = r
    if p["purchased_from"] is not None:
        dates = dates[dates.purchased_at >= pd.Timestamp(p["purchased_from"])]
    if p["purchased_to"] is not None:
        dates = dates[dates.purchased_at <= pd.Timestamp(p["purchased_to"])]
    dates = dates.assign(year=dates.purchased_at.dt.year)
    by_status = keep(r, ("payment_cycle", p["payment_cycle"]))
    by_cycle_year = keep(dates, ("payment_cycle", p["payment_cycle"]), ("status", p["selected_status"]))
    by_manager = keep(dates, ("payment_cycle", p["payment_cycle"]), ("status", p["selected_status"]))
    drilled = keep(dates, ("payment_cycle", p["selected_payment_cycle"]), ("year", p["selected_year_of_purchased"]))
    return {
        "Revenue by Status": by_status.groupby("status").post_discount_revenue.sum(),
        "Revenue by Cycle and Year": by_cycle_year.groupby(["payment_cycle", "year"]).post_discount_revenue.sum(),
        "Orders by Account Manager": by_manager.groupby("account_manager").order_id.nunique(),
        "Quantity by Manager and Cycle": drilled.groupby(["account_manager", "payment_cycle"]).quantity.sum(),
    }


@pytest.mark.parametrize("values", [
    {},
    {"selected_status": "CANCELLED"},
    {"payment_cycle": "WEEK"},
    {"payment_cycle": "", "selected_status": "RETURNED"},  # accounts with an empty payment cycle
    {"purchased_from": None, "purchased_to": "2024-03-01"},  # one end open, the other on a day with orders
    {"selected_payment_cycle": "MONTH", "selected_year_of_purchased": 2025},
    {"selected_year_of_purchased": 2024, "purchased_from": "2024-12-31", "purchased_to": None},
], ids=["default", "status", "cycle", "empty-cycle", "until", "drill", "drill-year"])
def test_filtered_tiles_return_the_filtered_rows(bundle, out, warehouse, rows, values):
    from unshackle.run import notebook_queries, query

    d = _board(bundle, "Sales Dashboard")
    params, sqls = notebook_queries(_notebook(out[1], d.slug))
    params = {**params, **values}
    expected = _expected(rows, params)
    for t in d.tiles:
        if t.kind == "text":
            continue
        df = query(sqls[t.id.replace("-", "_")], params, url=f"duckdb:///{warehouse}")
        keys = list(df.columns[:-1])
        got = df.set_index(keys)[df.columns[-1]].sort_index()
        exp = expected[t.title].sort_index()
        assert [tuple(x) if isinstance(x, tuple) else x for x in got.index] == list(exp.index), (t.title, values)
        assert got.to_numpy(dtype=float) == pytest.approx(exp.to_numpy(dtype=float), rel=1e-9), (t.title, values)


def test_actions_and_what_is_not_migrated(bundle, out):
    d = _board(bundle, "Sales Dashboard")
    notes = "\n".join(d.notes)
    assert ("**Filter by Status** (filter action, on select): selecting marks on Revenue by Status filters Revenue by Cycle and Year, "
            "Orders by Account Manager, Quantity by Manager and Cycle on Status. Here: set `selected_status`") in notes
    assert "**Drill into Cycle and Year** (filter action, on select): selecting marks on Revenue by Cycle and Year filters Quantity by " \
           "Manager and Cycle on all shared fields. Here: set `selected_payment_cycle`, `selected_year_of_purchased`" in notes
    assert "[Manager in CRM](https://crm.example.com/managers?name={account_manager})" in notes  # a URL action is a link
    gaps = "\n".join(d.not_migrated)
    assert "**Highlight Manager** (highlight action, on hover)" in gaps and "on Account Manager" in gaps
    assert "**Pick Top Managers** (set action, on select)" in gaps
    assert "web page zone (https://wiki.example.com/sales)" in gaps and "Phone layout" in gaps
    src = notebook_source(_notebook(out[1], d.slug))
    assert "## Interactions" in src and "## Not migrated" in src
    report = (out[1] / "migration" / "report.md").read_text()
    section = report[report.index("## Not migrated from dashboards"):]
    assert "### Sales Dashboard (tableau)" in section and "### Sales Story (tableau)" in section
    assert "**Pick Top Managers** (set action, on select)" in section
    assert not [w for w in bundle.warnings if "story" in w.message.lower() or "web zone" in w.message]


def test_story_is_one_notebook_with_a_section_per_point(bundle, out, warehouse, rows):
    from unshackle.run import notebook_queries, query

    s = _board(bundle, "Sales Story")
    assert s.description == "How the sales year went"
    heads = [t.title for t in s.tiles if t.id.startswith("story-point-")]
    assert heads == ["1. The dashboard, as it opens.", "2. Monthly accounts bring the most revenue.", "3. Discounts are mostly on completed orders."]
    assert [t.title for t in s.tiles if t.id.startswith("p1-") and t.kind != "text"] == [
        "Revenue by Status", "Revenue by Cycle and Year", "Orders by Account Manager", "Quantity by Manager and Cycle"]
    assert s.layout[0] == ["story-point-1"] and [r for r in s.layout if "story-point-2" in r] == [["story-point-2"]]
    # the second point shows the sheet with the filter it captured (monthly accounts), not the dashboard's control
    p2 = next(t for t in s.tiles if t.id == "p2-revenue-by-status")
    assert "accounts.payment_cycle = 'MONTH'" in p2.sql and ":payment_cycle" not in p2.sql
    params, sqls = notebook_queries(_notebook(out[1], s.slug))
    assert params == DEFAULTS
    df = query(sqls["revenue_by_status_2"], params, url=f"duckdb:///{warehouse}")  # the sheet's second appearance in the notebook
    monthly = rows[rows.payment_cycle == "MONTH"].groupby("status").post_discount_revenue.sum()
    assert dict(zip(df.status, df.revenue)) == pytest.approx(monthly.to_dict(), rel=1e-9)
    assert "story point 3 (Discount by Status): captured rows changes on Discount by Status" in s.not_migrated
    # sheets in the story are not repeated in the notebook of sheets on no dashboard
    (stray,) = [d for d in bundle.dashboards if d.name.endswith(" sheets")]
    assert [t.title for t in stray.tiles] == ["Shipped Orders"]


def test_hidden_fields_get_no_metrics_unless_used(bundle, out):
    dims = {d.name for sm in bundle.semantic_models for d in sm.dimensions}
    metrics = {m.name for m in bundle.metrics}
    assert "contact_email" not in dims and "business_size" not in metrics and "old_margin" not in metrics  # hidden, unused
    assert "pre_discount_revenue" in metrics and "shipping" in dims  # hidden, used by a calc
    orders = yaml.safe_load((out[1] / "catalog" / "warehouse" / "public" / "orders.yml").read_text())
    cols = {c["name"]: c for c in orders["columns"]}
    assert cols["order_contact_email"].get("visible") is False and cols["shipping_id"].get("visible") is False
    (glossary,) = [k for k in bundle.knowledge if k.name.endswith("glossary")]
    assert "## Folders\n\n- **Revenue** (Dunder Mifflin Orders): Revenue, Pre-Discount Revenue, Discount Given" in glossary.body
    assert "- **Contact Email** (Dunder Mifflin Orders): not used by any sheet, calculation or filter, so no metric or dimension was made" in glossary.body
    assert "- **Shipping** (Dunder Mifflin Orders): used by sheets or calculations" in glossary.body


def test_descriptions_reach_the_semantic_layer(bundle, out):
    by_name = {m.name: m for m in bundle.metrics}
    assert by_name["revenue"].description == "Order revenue after discounts, in USD"
    assert by_name["discount_given"].description == "Revenue given away in discounts"
    status = next(d for sm in bundle.semantic_models for d in sm.dimensions if d.name == "status")
    assert status.description == "COMPLETED, CANCELLED or RETURNED"
    ossie = "\n".join(p.read_text() for p in out[1].rglob("*.ossie.yaml"))
    assert "Order revenue after discounts, in USD" in ossie and "COMPLETED, CANCELLED or RETURNED" in ossie


@pytest.mark.parametrize("warehouse", ["redshift", "bigquery", "databricks", "snowflake"])
def test_bound_filters_in_each_dialect(tmp_path, warehouse):
    """The casts and comparisons of bound filters in each warehouse's form return the same rows, with values set."""
    sqlglot = pytest.importorskip("sqlglot")
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("create schema public")
    for t in ("orders", "accounts"):
        con.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    values = {"payment_cycle": "'WEEK'", "purchased_from": "'2024-03-01'", "purchased_to": "'2025-12-31'", "selected_status": "'CANCELLED'",
              "selected_payment_cycle": "'MONTH'", "selected_year_of_purchased": "2025"}

    def tiles(where: Path, wh: str = "") -> dict[str, str]:
        shutil.copytree(FIX, where / "migration" / "raw" / "tableau")
        if wh:
            (where / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{wh}"\n')
        b = get_source("tableau").parse(RawStore(where, "tableau"))
        return {t.title: re.sub(r":(\w+)", lambda m: values[m.group(1)], t.sql) for t in _board(b, "Sales Dashboard").tiles if t.sql}

    base, forms = tiles(tmp_path / "default"), tiles(tmp_path / warehouse, warehouse)
    for title, sql in forms.items():
        if warehouse != "redshift":
            sql = sqlglot.transpile(sql, read=warehouse, write="duckdb")[0]
        assert sorted(con.sql(sql).fetchall()) == sorted(con.sql(base[title]).fetchall()), title
