import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import todo_for
from jsonschema import Draft202012Validator

from unshackle import build
from unshackle.ir import Bundle, Dimension, Entity, Measure, Metric, SemanticModel
from unshackle.raw import RawStore
from unshackle.sources import TOOLS, get_source

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Draft202012Validator(json.loads((Path(__file__).parent / "ossie" / "ossie-schema.json").read_text()))


def _check(doc: dict) -> None:
    """The vendored Ossie JSON Schema, plus the name rules the schema can't express."""
    errors = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in SCHEMA.iter_errors(doc)]
    assert not errors, errors
    fields = {}
    for ds in doc["datasets"]:
        names = [f["name"] for f in ds.get("fields", [])] + [m["name"] for m in ds.get("metrics", [])]
        assert len(names) == len(set(names)), f"field/metric names clash in {ds['name']}"
        fields[ds["name"]] = {f["name"] for f in ds.get("fields", [])}
        assert set(ds.get("primary_key", [])) <= fields[ds["name"]]
    assert len(fields) == len(doc["datasets"])
    for r in doc.get("relationships", []):
        assert set(r["from_columns"]) <= fields[r["from"]] and set(r["to_columns"]) <= fields[r["to"]]


def _ossie(repo: Path) -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text()) for p in sorted((repo / "semantic").glob("*.ossie.yaml"))}


def _ext(item: dict) -> dict:
    (ext,) = [e for e in item.get("custom_extensions", []) if e["vendor_name"] == "UNSHACKLE"]
    return json.loads(ext["data"])


def _expr(item: dict) -> str:
    (d,) = item["expression"]["dialects"]
    return d["expression"]


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_fixture_writes_valid_ossie(tool: str, tmp_path: Path):
    shutil.copytree(FIXTURES / tool, tmp_path / "migration" / "raw" / tool)
    store = RawStore(tmp_path, tool)
    store.rescan()
    b = get_source(tool).parse(store)
    build.apply(b, tmp_path)
    docs = _ossie(tmp_path)
    assert len(docs) == len(b.semantic_models)
    for doc in docs.values():
        _check(doc)
    assert not list((tmp_path / "semantic").glob("*.yml")), "MetricFlow is only written in dbt projects"


def shop() -> Bundle:
    b = Bundle(tool="demo")
    b.semantic_models += [
        SemanticModel(
            "orders",
            "analytics.fct_orders",
            entities=[Entity("order", "primary", "id"), Entity("customer", "foreign", "customer_id")],
            dimensions=[
                Dimension("status"),
                Dimension("customer_id"),
                Dimension("amount", expr="amount_usd"),
                Dimension("ordered_at", "time", time_granularity="day"),
                Dimension("order_month", "time", "date_trunc('month', ordered_at)", time_granularity="month"),
                Dimension("first_order", todo="Tableau FIXED LOD", source_expression="{FIXED [Customer]: MIN([Date])}", source_language="tableau"),
            ],
            measures=[Measure("revenue", "sum", "amount_usd"), Measure("orders", "count_distinct", "id"), Measure("refunds", "sum", "amount_usd")],
            default_time_dimension="ordered_at",
            source_ref="demo:model/orders",
        ),
        SemanticModel(
            "customers",
            "analytics.dim_customers",
            entities=[Entity("customer", "primary", "customer_id")],
            dimensions=[Dimension("customer_id"), Dimension("region")],
            measures=[Measure("customers", "count_distinct", "customer_id")],
        ),
    ]
    b.metrics += [
        Metric("Revenue", measure="revenue", semantic_model="orders", source_ref="demo:metric/1"),
        Metric("orders", measure="orders", semantic_model="orders"),
        Metric("Refunds", measure="refunds", semantic_model="orders", filter="{{ Dimension('order__status') }} = 'returned'"),
        Metric("Average Order Value", type="ratio", numerator="Revenue", denominator="orders", semantic_model="orders"),
        Metric("Net Revenue", type="derived", expr="revenue - refunds", metrics=["Revenue", "Refunds"], semantic_model="orders"),
        Metric("EU Revenue", measure="revenue", semantic_model="orders", filter="{{ Dimension('customer__region') }} = 'EU'"),
        Metric("Revenue per Customer", type="ratio", numerator="revenue", denominator="customers", semantic_model="orders"),
        Metric("Revenue YTD", type="cumulative", measure="revenue", grain_to_date="year", semantic_model="orders"),
        Metric("Running Revenue", semantic_model="orders", todo="table calculation", source_expression="RUNNING_SUM(SUM([Sales]))", source_language="tableau"),
    ]
    return b


def test_mapping(tmp_path: Path):
    b = shop()
    build.apply(b, tmp_path)
    docs = _ossie(tmp_path)
    assert set(docs) == {"orders.ossie.yaml", "customers.ossie.yaml"}
    doc = docs["orders.ossie.yaml"]
    _check(doc)
    orders, customers = doc["datasets"]
    assert (doc["version"], doc["name"], orders["name"], orders["source"]) == ("0.2.0.dev0", "orders", "orders", "analytics.fct_orders")
    assert orders["primary_key"] == ["id"] and customers["primary_key"] == ["customer_id"]
    assert _ext(orders)["source_ref"] == "demo:model/orders" and _ext(orders)["default_time_dimension"] == "ordered_at"

    # relationship: foreign entity -> the dataset with that primary entity, copied in without its metrics
    assert doc["relationships"] == [
        {"name": "orders_to_customers", "from": "orders", "to": "customers", "from_columns": ["customer_id"], "to_columns": ["customer_id"]}
    ]
    assert "metrics" not in customers and _ext(customers) == {"defined_in": "customers.ossie.yaml"}
    assert [m["name"] for m in docs["customers.ossie.yaml"]["datasets"][0]["metrics"]] == ["customers"]

    # time dimensions
    fields = {f["name"]: f for f in orders["fields"]}
    assert fields["ordered_at"]["dimension"] == {"is_time": True} and fields["status"]["dimension"] == {"is_time": False}
    assert _expr(fields["order_month"]) == "date_trunc('month', ordered_at)" and _ext(fields["order_month"]) == {"time_granularity": "month"}

    # dataset-scoped metrics read fields by single-part name; model-scoped ones by dataset.field
    metrics = {m["name"]: m for m in orders["metrics"]}
    assert _expr(metrics["revenue"]) == "SUM(amount)" and _ext(metrics["revenue"]) == {"label": "Revenue", "source_ref": "demo:metric/1"}
    assert _expr(metrics["refunds"]) == "SUM(CASE WHEN status = 'returned' THEN amount END)"
    assert _expr(metrics["average_order_value"]) == "1.0 * SUM(amount) / nullif(COUNT(DISTINCT id), 0)"
    assert _expr(metrics["net_revenue"]) == "SUM(amount) - SUM(CASE WHEN status = 'returned' THEN amount END)"
    model = {m["name"]: m for m in doc["metrics"]}
    assert set(model) == {"eu_revenue", "revenue_per_customer"}
    assert _expr(model["eu_revenue"]) == "SUM(CASE WHEN customers.region = 'EU' THEN orders.amount END)"
    assert _expr(model["revenue_per_customer"]) == "1.0 * SUM(orders.amount) / nullif(COUNT(DISTINCT customers.customer_id), 0)"


def test_untranslatable_items_stay_in_custom_extensions(tmp_path: Path):
    b = shop()
    build.apply(b, tmp_path)
    text = (tmp_path / "semantic/orders.ossie.yaml").read_text()
    doc = yaml.safe_load(text)
    orders = doc["datasets"][0]
    assert "first_order" not in {f["name"] for f in orders["fields"]}
    (dim,) = _ext(orders)["pending"]
    assert dim["kind"] == "dimension" and dim["todo"] == "Tableau FIXED LOD" and dim["source_expression"].startswith("{FIXED")
    assert "[tableau] {FIXED" in todo_for(text, "dimension", "first_order")

    names = {m["name"] for m in orders["metrics"]} | {m["name"] for m in doc["metrics"]}
    assert not names & {"revenue_ytd", "running_revenue"}
    pending = {p["name"]: p for p in _ext(doc)["pending"]}
    assert pending["Running Revenue"]["source_language"] == "tableau" and pending["Running Revenue"]["todo"] == "table calculation"
    assert pending["Revenue YTD"]["grain_to_date"] == "year" and "cumulative" in pending["Revenue YTD"]["todo"]
    assert todo_for(text, "metric", "running_revenue") and todo_for(text, "metric", "revenue_ytd")
    # only Ossie's own gaps warn; the source already flagged the rest
    assert [w.item for w in b.warnings if not w.item.startswith("datasource")] == ["metric Revenue YTD"]
    assert "running_revenue" in (tmp_path / "migration/report.md").read_text()


def test_dialect_follows_the_warehouse(tmp_path: Path):
    (tmp_path / "unshackle.toml").write_text('[datasources.warehouse]\ntype = "snowflake"\n')
    build.apply(shop(), tmp_path)
    doc = yaml.safe_load((tmp_path / "semantic/orders.ossie.yaml").read_text())
    dialects = {d["dialect"] for ds in doc["datasets"] for f in ds["fields"] for d in f["expression"]["dialects"]}
    assert dialects == {"SNOWFLAKE"} and _ext(doc)["warehouse"] == "snowflake"


def test_rerun_keeps_hand_edits(tmp_path: Path):
    build.apply(shop(), tmp_path)
    path = tmp_path / "semantic/orders.ossie.yaml"
    path.write_text(path.read_text() + "# reviewed\n")
    result = build.apply(shop(), tmp_path)
    assert "semantic/orders.ossie.yaml" in result.writes.kept_hand_edits
    assert path.read_text().endswith("# reviewed\n")
    assert "semantic/customers.ossie.yaml" in result.writes.unchanged


def test_dbt_project_gets_ossie_and_metricflow(tmp_path: Path):
    (tmp_path / "dbt_project.yml").write_text("name: x\n")
    (tmp_path / ".dbtignore").write_text("scratch/")
    build.apply(shop(), tmp_path)
    build.apply(shop(), tmp_path)
    # dbt reads every YAML file under models/, so Ossie files are ignored there
    assert (tmp_path / ".dbtignore").read_text() == "scratch/\n*.ossie.yaml\n"
    assert (tmp_path / "models/semantic/orders.ossie.yaml").exists()
    assert yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())["semantic_models"][0]["name"] == "orders"
