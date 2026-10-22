from conftest import notebook_source, todo_for
from pathlib import Path

import pandas as pd
import yaml

from unshackle import build, scaffold, verify
from unshackle.ir import (
    Bundle,
    Column,
    Dashboard,
    DashboardFilter,
    Dimension,
    Encoding,
    Entity,
    KnowledgeItem,
    Measure,
    Metric,
    Permission,
    RowFilter,
    SemanticModel,
    Table,
    Tile,
    VerifiedQuery,
)


def sample_bundle() -> Bundle:
    b = Bundle(tool="demo")
    b.tables.append(
        Table("warehouse", "analytics", "fct_orders", "One row per order", [Column("id", "integer", "Order id"), Column("amount", "number")])
    )
    b.semantic_models.append(
        SemanticModel(
            name="orders",
            table="analytics.fct_orders",
            description="Orders",
            entities=[Entity("order", "primary", "id")],
            dimensions=[Dimension("order_date", "time", time_granularity="day"), Dimension("region")],
            measures=[Measure("revenue", "sum", "amount", label="Revenue"), Measure("order_count", "count", "id")],
            default_time_dimension="order_date",
        )
    )
    b.metrics += [
        Metric("Revenue", measure="revenue", semantic_model="orders"),
        Metric("Average order value", type="ratio", numerator="revenue", denominator="order_count", semantic_model="orders"),
        Metric("Weird", measure="revenue", todo="uses LOD", source_expression="{FIXED [x]: SUM([y])}", source_language="tableau"),
    ]
    b.dashboards.append(
        Dashboard(
            name="Revenue overview",
            slug="revenue-overview",
            tiles=[
                Tile(
                    "t1",
                    "Monthly revenue",
                    mark="line",
                    sql="select month, region, revenue from rev where month >= :date_from",
                    encodings={"x": Encoding("month", "temporal"), "y": Encoding("revenue", "quantitative"), "color": Encoding("region")},
                ),
                Tile("t2", "Orders table", kind="table", sql="select * from rev"),
                Tile("t3", "Map", mark="unknown", source_type="filled_map", sql="select 1 as x"),
            ],
            layout=[["t1"], ["t2", "t3"]],
            filters=[DashboardFilter("date_from", "month", "2025-01-01", "date")],
        )
    )
    b.queries.append(VerifiedQuery("Top customers", "select 1", description="Top 10 by revenue", aliases=["best customers"]))
    b.knowledge.append(KnowledgeItem("Glossary", "**Net revenue** excludes refunds.", context="always"))
    b.permissions.append(Permission("group:finance", "view", "Notebook", "revenue-overview"))
    b.row_filters.append(RowFilter("region_scope", "analytics.fct_orders", "region = {{ user.region }}", ["sales"]))
    return b


def test_build_writes_structure(tmp_path: Path):
    scaffold.init(tmp_path)
    result = build.apply(sample_bundle(), tmp_path)
    files = set(result.writes.written)
    assert "semantic/orders.ossie.yaml" in files and "semantic/orders.yml" not in files
    assert "catalog/warehouse/analytics/fct_orders.yml" in files
    assert "notebooks/revenue-overview.ipynb" in files
    assert "mark_line(point=True)" in notebook_source(tmp_path / "notebooks/revenue-overview.ipynb")
    assert "policies/demo.cedar" in files
    assert "policies/row_filters.demo.yml" in files
    assert "queries/top-customers.sql" in files
    assert "knowledge/glossary.md" in files
    text = (tmp_path / "semantic/orders.ossie.yaml").read_text()
    (orders,) = yaml.safe_load(text)["datasets"]
    assert orders["source"] == "analytics.fct_orders"
    assert {m["name"] for m in orders["metrics"]} == {"revenue", "average_order_value", "order_count"}
    assert "uses LOD" in todo_for(text, "metric", "weird")
    report = (tmp_path / "migration/report.md").read_text()
    assert "Still to translate" in report and "uses LOD" in report
    cedar = (tmp_path / "policies/demo.cedar").read_text()
    assert 'principal in Group::"finance"' in cedar and 'resource in Notebook::"revenue-overview"' in cedar


def test_dbt_layout(tmp_path: Path):
    (tmp_path / "dbt_project.yml").write_text("name: x\n")
    build.apply(sample_bundle(), tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())
    assert sem["semantic_models"][0]["model"] == "ref('fct_orders')"
    assert {m["name"] for m in sem["metrics"]} == {"revenue", "average_order_value", "order_count"}
    loose = (tmp_path / "models/semantic/_demo_metrics.yml").read_text()
    assert "uses LOD" in todo_for(loose, "metric", "weird") and not (yaml.safe_load(loose) or {}).get("metrics")
    assert (tmp_path / "models/semantic/orders.ossie.yaml").exists()


def test_rerun_keeps_hand_edits_and_human_descriptions(tmp_path: Path):
    build.apply(sample_bundle(), tmp_path)
    nb = tmp_path / "notebooks/revenue-overview.ipynb"
    nb.write_text(nb.read_text().replace("then run all cells.", "then run all cells, edited by a human."))
    cat = tmp_path / "catalog/warehouse/analytics/fct_orders.yml"
    doc = yaml.safe_load(cat.read_text())
    doc["columns"][1]["description"] = "Gross amount in USD"
    cat.write_text(yaml.safe_dump(doc))
    result = build.apply(sample_bundle(), tmp_path)
    assert "notebooks/revenue-overview.ipynb" in result.writes.kept_hand_edits
    assert "edited by a human" in nb.read_text()
    assert yaml.safe_load(cat.read_text())["columns"][1]["description"] == "Gross amount in USD"


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table rev as select * from (values (date '2025-02-01','EU',10.0),(date '2025-03-01','US',20.0)) t(month, region, revenue)")
    con.close()
    build.apply(sample_bundle(), tmp_path)
    from unshackle.run import run_notebook

    out = run_notebook(tmp_path / "notebooks/revenue-overview.ipynb", url=f"duckdb:///{db}")
    html = out.read_text()
    assert html.count("vegaEmbed(") == 1 and html.count("<table") == 2  # the map has no mark: its data is shown


def test_compare():
    a = pd.DataFrame({"Month": ["jan", "feb"], "revenue": [100.0, 200.0]})
    b = pd.DataFrame({"month": ["feb", "jan"], "revenue": [200.00001, 100.0]})
    assert verify.compare(a, b, keys=["month"])[0] == "pass"
    c = pd.DataFrame({"month": ["feb", "jan"], "revenue": [210.0, 100.0]})
    assert verify.compare(a, c, keys=["month"])[0] == "differs"


def test_compare_edge_cases():
    import math

    empty = pd.DataFrame({"x": []})
    assert verify.compare(empty, empty)[0] == "differs"
    a = pd.DataFrame({"region": ["EU", "US"], "Sum of Revenue": [1.0, 2.0]})
    b = pd.DataFrame({"region": ["EU", "US"], "revenue": [5.0, 9.0]})
    assert verify.compare(a, b, ["region"])[0] == "differs"
    b_ok = pd.DataFrame({"region": ["US", "EU"], "revenue": [2.0, 1.0]})
    assert verify.compare(a, b_ok, ["region"], columns={"Sum of Revenue": "revenue"})[0] == "pass"
    n = pd.DataFrame({"k": ["a", "b"], "v": [math.nan, 1.0]})
    assert verify.compare(n, n.copy(), ["k"])[0] == "pass"
    dup_a = pd.DataFrame({"k": ["a", "a"], "v": [1.0, 2.0]})
    dup_b = pd.DataFrame({"k": ["a", "a"], "v": [2.0, 1.0]})
    assert verify.compare(dup_a, dup_b, ["k"])[0] == "pass"
    fmt = pd.DataFrame({"k": ["a", "b"], "v": ["1,234.50", "12.5%"]})
    raw = pd.DataFrame({"k": ["a", "b"], "v": [1234.5, 0.125]})
    assert verify.compare(fmt, raw, ["k"])[0] == "pass"
    assert verify.compare(a, b_ok, ["nope"], columns={"Sum of Revenue": "revenue"})[0] == "differs"


def test_compare_keeps_leading_zeros():
    a = pd.DataFrame({"zip": ["007", "010"], "n": [1, 2]})
    b = pd.DataFrame({"zip": ["7", "10"], "n": [1, 2]})
    assert verify.compare(a, b, ["zip"])[0] == "differs"
