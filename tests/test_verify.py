"""verify: display formats, extra source columns, eval names and `verify --init`."""

from pathlib import Path

import pandas as pd
import pytest
import yaml
from click.testing import CliRunner

from unshackle import build, verify
from unshackle.cli import main
from unshackle.ir import Bundle, Dashboard, DashboardFilter, Encoding, Tile

FIXTURE = Path(__file__).parent / "fixtures" / "verify" / "revenue-by-paper-type.csv"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"


@pytest.mark.parametrize(("fmt", "unit"), [("0.0%", 0.001), ("$#,##0", 1.0), ("$0.0M", 100_000.0), ("0.00", 0.01), ("#,##0K", 1000.0)])
def test_display_unit(fmt, unit):
    assert verify.display_unit(fmt) == pytest.approx(unit)


def test_display_unit_rejects_unknown_formats():
    with pytest.raises(ValueError, match="not understood"):
        verify.display_unit("yyyy-mm")


@pytest.mark.parametrize(("values", "fmt"), [
    (["$1,234", "$980"], "$#,##0"),
    (["11.9%", "-4%"], "0.0%"),
    (["0.1M", "12.25M"], "0.00M"),
    (["1,146", "309"], "#,##0"),
    (["(3.2%)", "1.0%"], "0.0%"),
    (["1200.5", "980"], None),  # plain numbers: compared with `tolerance`
    (["950K", "1.2M"], None),  # the unit changes per value: no single format
    (["East", "West"], None),
])
def test_infer_format(values, fmt):
    assert verify.infer_format(values) == fmt


def test_exported_numbers_parse_with_units():
    parsed = verify._to_num(pd.Series(["$0.1M", "12K", "11.9%", "(3.2)", "1.5B", None]))
    assert parsed[:5].tolist() == pytest.approx([100_000, 12_000, 0.119, -3.2, 1.5e9])
    assert pd.isna(parsed[5])


def test_compare_at_display_precision():
    shown = pd.DataFrame({"k": ["a", "b"], "ratio": ["11.9%", "4.3%"], "revenue": ["$3.8M", "$100.1M"]})
    exact = pd.DataFrame({"k": ["a", "b"], "ratio": [0.11904, 0.04260], "revenue": [3_794_927.81, 100_087_017.97]})
    fmts = {"ratio": "0.0%", "revenue": "$0.0M"}
    assert verify.compare(shown, exact, ["k"])[0] == "differs"  # rounded on screen: off by more than 0.01%
    assert verify.compare(shown, exact, ["k"], formats=fmts)[0] == "pass"
    off = exact.assign(ratio=[0.1196, 0.0426])  # 12.0% when shown
    status, _, note = verify.compare(shown, off, ["k"], formats=fmts)
    assert status == "differs" and "column ratio" in note and "shown as 0.0%" in note


def test_display_precision_counts_exact_ties_as_a_pass():
    """A half-way value rounds either way depending on the tool (half up, half even, binary floats)."""
    shown = pd.DataFrame({"k": ["a", "b"], "share": ["13%", "13%"]})
    ties = pd.DataFrame({"k": ["a", "b"], "share": [0.125, 0.135]})
    assert verify.compare(shown, ties, ["k"], formats={"share": "0%"})[0] == "pass"
    beyond = pd.DataFrame({"k": ["a", "b"], "share": [0.1249, 0.135]})
    assert verify.compare(shown, beyond, ["k"], formats={"share": "0%"})[0] == "differs"


def test_formats_follow_renamed_columns():
    shown = pd.DataFrame({"k": ["a"], "Sum of Revenue": ["$1,234"]})
    target = pd.DataFrame({"k": ["a"], "revenue": [1234.4]})
    assert verify.compare(shown, target, ["k"], columns={"Sum of Revenue": "revenue"}, formats={"Sum of Revenue": "$#,##0"})[0] == "pass"


def test_extra_source_columns_are_noted_unless_strict():
    source = pd.DataFrame({"k": ["a", "b"], "v": [1, 2], "tooltip only": ["x", "y"]})
    target = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    status, _, note = verify.compare(source, target, ["k"])
    assert status == "pass" and "not compared: tooltip_only" in note
    assert verify.compare(source, target, ["k"], strict=True)[0] == "differs"
    assert verify.compare(source, target, ["k"], ignore=["tooltip only"], strict=True) == ("pass", "0.0000%", "")
    # nothing but the keys in common: not a pass
    status, _, note = verify.compare(source, target.drop(columns=["v"]), ["k"])
    assert status == "differs" and "not compared" in note


def test_eval_files_are_named_by_id_or_case_file(tmp_path: Path):
    parity = tmp_path / "migration/parity"
    parity.mkdir(parents=True)
    (tmp_path / "a.csv").write_text("k,v\na,1\n")
    long_name = "Executive overview / monthly revenue by region and product line, completed orders only"
    (parity / "revenue.yaml").write_text(yaml.safe_dump({"name": long_name, "source": {"csv": "a.csv"}, "target": {"csv": "a.csv"}, "keys": ["k"]}))
    (parity / "other.yaml").write_text(yaml.safe_dump({"id": "Q3 margin", "name": long_name + " 2", "source": {"csv": "a.csv"}, "target": {"csv": "a.csv"}, "keys": ["k"]}))
    assert [r.status for r in verify.run_checks(tmp_path)] == ["pass", "pass"]
    assert sorted(p.name for p in (tmp_path / "evals").iterdir()) == ["q3-margin.yaml", "revenue.yaml"]
    assert yaml.safe_load((tmp_path / "evals/revenue.yaml").read_text())["id"] == "revenue"
    assert verify.eval_id({"name": long_name}) == "executive-overview-monthly-revenue-by-re"


def _bundle() -> Bundle:
    sql = (
        "select\n  p.paper_type,\n  count(*) as orders,\n  sum(o.post_discount_revenue) as revenue,\n"
        "  1.0 * sum(o.pre_discount_revenue - o.post_discount_revenue) / nullif(sum(o.pre_discount_revenue), 0) as discount_share\n"
        "from dunder_mifflin.orders as o\njoin dunder_mifflin.products as p on o.product_id = p.product_id\n"
        "where o.status = :order_status\ngroup by 1\norder by 1"
    )
    columns = {"paper_type": "Paper Type", "orders": "Orders", "revenue": "Revenue", "discount_share": "Discount Share"}
    tile = Tile(
        "revenue-by-paper-type", "Revenue by Paper Type", "chart", "bar", sql,
        {"x": Encoding("paper_type", "nominal"), "y": Encoding("revenue", "quantitative"), "tooltip": Encoding("discount_share", "quantitative")},
        source_config={"parity_csv": "migration/raw/tableau/views/v-revenue.csv", "columns": columns},
    )
    no_csv = Tile("orders-kpi", "Orders", "kpi", "text", "select count(*) as orders from dunder_mifflin.orders", {"text": Encoding("orders", "quantitative")})
    no_sql = Tile("map", "Map", "chart", "geoshape", "", source_config={"parity_csv": "migration/raw/tableau/views/v-map.csv"})
    dash = Dashboard("Sales Review", "sales-review", tiles=[tile, no_csv, no_sql], layout=[[tile.id, no_csv.id, no_sql.id]],
                     filters=[DashboardFilter("order_status", "status", "COMPLETED"), DashboardFilter("region", "region", None)])
    return Bundle("tableau", dashboards=[dash])


def _expected_display() -> pd.DataFrame:
    """The fixture's numbers from the orders themselves, formatted the way the view shows them."""
    import duckdb

    orders = duckdb.sql(f"select * from '{DATA / 'orders.parquet'}'").df()
    products = duckdb.sql(f"select * from '{DATA / 'products.parquet'}'").df()
    rows = orders[orders.status == "COMPLETED"].merge(products, on="product_id")
    rows["post"] = rows.post_discount_revenue.astype(float)
    rows["pre"] = rows.pre_discount_revenue.astype(float)
    g = rows.groupby("paper_type").agg(orders=("order_id", "size"), revenue=("post", "sum"), pre=("pre", "sum"))
    return pd.DataFrame({
        "Paper Type": g.index,
        "Orders": [f"{n:,}" for n in g.orders],
        "Revenue": [f"${r / 1e6:.1f}M" for r in g.revenue],
        "Discount Share": [f"{(p - r) / p:.1%}" for p, r in zip(g.pre, g.revenue)],
    }).reset_index(drop=True)


def test_init_writes_cases_for_view_csvs_and_they_pass(tmp_path: Path):
    import duckdb

    fixture = pd.read_csv(FIXTURE, dtype=str, encoding="utf-8-sig")
    assert fixture.drop(columns=["Number of Records"]).equals(_expected_display())
    build.apply(_bundle(), tmp_path)
    views = tmp_path / "migration/raw/tableau/views"
    views.mkdir(parents=True)
    (views / "v-revenue.csv").write_bytes(FIXTURE.read_bytes())

    run = CliRunner().invoke(main, ["verify", "--init", "tableau", "--repo", str(tmp_path)])
    assert run.exit_code == 0, run.output
    assert "wrote migration/parity/revenue-by-paper-type.yaml" in run.output
    assert "skipped Sales Review / Map: the tile has no SQL yet" in run.output
    case = yaml.safe_load((tmp_path / "migration/parity/revenue-by-paper-type.yaml").read_text())
    assert case == {
        "id": "revenue-by-paper-type",
        "name": "Sales Review / Revenue by Paper Type",
        "source": {"csv": "migration/raw/tableau/views/v-revenue.csv"},
        "target": {"notebook": "notebooks/sales-review.ipynb", "tile": "revenue_by_paper_type", "params": {"order_status": "COMPLETED"}},
        "keys": ["paper_type"],
        "formats": {"Orders": "#,##0", "Revenue": "$0.0M", "Discount Share": "0.0%"},
        "tolerance": 0.0001,
    }

    db = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema dunder_mifflin")
    for t in ("orders", "products"):
        con.execute(f"create table dunder_mifflin.{t} as select * from '{DATA / f'{t}.parquet'}'")
    con.close()
    run = CliRunner().invoke(main, ["verify", "--repo", str(tmp_path), "--url", f"duckdb:///{db}"])
    assert run.exit_code == 0, run.output
    assert "pass" in run.output and "not compared: number_of_records" in run.output
    assert (tmp_path / "evals/revenue-by-paper-type.yaml").exists()

    # a second --init keeps the edited case
    path = tmp_path / "migration/parity/revenue-by-paper-type.yaml"
    path.write_text(path.read_text() + "# checked by hand\n")
    run = CliRunner().invoke(main, ["verify", "--init", "tableau", "--repo", str(tmp_path)])
    assert "exists, kept" in run.output and "# checked by hand" in path.read_text()


def test_init_on_the_tableau_fixture(tmp_path: Path):
    import shutil

    shutil.copytree(Path(__file__).parent / "fixtures" / "tableau", tmp_path / "migration/raw/tableau")
    assert CliRunner().invoke(main, ["build", "tableau", "--repo", str(tmp_path)]).exit_code == 0
    run = CliRunner().invoke(main, ["verify", "--init", "tableau", "--repo", str(tmp_path)])
    assert run.exit_code == 0, run.output
    case = yaml.safe_load((tmp_path / "migration/parity/sales-trend.yaml").read_text())
    assert case["target"] == {"notebook": "notebooks/executive-overview.ipynb", "tile": "sales_trend"}
    assert case["keys"] == ["category", "month_of_order_date"] and "formats" not in case


def test_init_needs_a_build_first(tmp_path: Path):
    run = CliRunner().invoke(main, ["verify", "--init", "looker", "--repo", str(tmp_path)])
    assert run.exit_code != 0 and "unshackle build looker" in run.output

