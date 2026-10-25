from conftest import notebook_source, todo_for
import json
import shutil
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.lightdash import LightdashSource

FIXTURES = Path(__file__).parent / "fixtures" / "lightdash"
PROJECT = "3675b69e-8324-4110-bdca-059031aa8da3"
MF_AGGS = {"sum", "count", "count_distinct", "average", "min", "max", "median", "percentile", "sum_boolean"}


def repo_with_fixtures(tmp_path: Path, subdir: str = "") -> Path:
    dest = tmp_path / "migration" / "raw" / "lightdash"
    shutil.copytree(FIXTURES / subdir, dest / subdir if subdir else dest)
    return tmp_path


def parse(tmp_path: Path, subdir: str = ""):
    repo = repo_with_fixtures(tmp_path, subdir)
    return repo, LightdashSource().parse(RawStore(repo, "lightdash"))


def test_parse_semantic_layer(tmp_path: Path):
    _, b = parse(tmp_path)
    assert {t.name for t in b.tables} == {"orders", "customers"}
    orders_table = next(t for t in b.tables if t.name == "orders")
    assert orders_table.schema == "analytics" and "USD" in orders_table.description
    assert {c.name for c in orders_table.columns} >= {"order_id", "status", "amount", "created_at"}

    models = {m.name: m for m in b.semantic_models}
    orders = models["orders"]
    assert orders.table == "jaffle.analytics.orders"
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [("orders", "primary", "order_id"), ("customers", "foreign", "customer_id")]
    dims = {d.name: d for d in orders.dimensions}
    assert "created_at_month" not in dims  # interval dims collapse into the time dimension
    assert dims["created_at"].type == "time" and orders.default_time_dimension == "created_at"
    assert dims["order_size"].expr == "CASE WHEN amount >= 100 THEN 'large' ELSE 'small' END"
    assert models["customers"].entities[0].expr == "customer_id"
    measures = {m.name: m for m in orders.measures}
    assert measures["total_revenue"].agg == "sum" and measures["total_revenue"].expr == "amount"
    assert measures["p90_order_amount"].agg_params == {"percentile": 0.9, "use_discrete_percentile": False}

    metrics = {m.name: m for m in b.metrics}
    assert metrics["completed_revenue"].filter == "{{ Dimension('orders__status') }} = 'completed'"
    assert metrics["eu_revenue"].filter == "{{ Dimension('customers__region') }} = 'EU'"
    aov = metrics["average_order_value"]
    assert (aov.type, aov.numerator, aov.denominator) == ("ratio", "total_revenue", "order_count")
    assert metrics["revenue_per_customer"].denominator == "customer_count"
    todo = metrics["repeat_order_rate"]
    assert todo.todo and todo.source_language == "lightdash" and "order_rank" in todo.source_expression


def test_parse_access_knowledge_and_warnings(tmp_path: Path):
    _, b = parse(tmp_path)
    (rf,) = b.row_filters
    assert rf.table == "jaffle.analytics.customers" and rf.predicate == "region = {{ user.region }}"
    (perm,) = b.permissions
    assert perm.resource_type == "Column" and perm.resource_id.endswith("customers.email") and perm.effect == "forbid"
    assert 'Group::"is_admin=true"' in perm.condition and perm.todo
    hints = {k.name: k for k in b.knowledge}
    assert "plan, not tier" in hints["Customers AI hints"].body
    assert "completed for recognised revenue" in hints["Orders AI hints"].body
    assert any("stg_payments" in w.item for w in b.warnings)


def test_parse_dashboard(tmp_path: Path):
    _, b = parse(tmp_path)
    (d,) = b.dashboards
    assert d.name == "Sales overview" and d.views == 342
    assert d.url.endswith(f"/projects/{PROJECT}/dashboards/b1c2d3e4-0000-4000-8000-000000000001")
    tiles = {t.id: t for t in d.tiles}
    assert d.layout == [["markdown_1"], ["total_revenue", "monthly_revenue"], ["revenue_by_region", "order_funnel"], ["recent_orders"], ["loom_7"]]
    line = tiles["monthly_revenue"]
    assert line.mark == "line" and line.encodings["x"].field == "orders_created_at_month" and line.encodings["x"].type == "temporal"
    assert line.encodings["y"].type == "quantitative" and "DATE_TRUNC" in line.sql and not line.todo
    bar = tiles["revenue_by_region"]
    assert bar.mark == "bar" and bar.encodings["color"].field == "orders_status" and "xOffset" in bar.encodings
    kpi = tiles["total_revenue"]
    assert kpi.kind == "kpi" and kpi.encodings["text"].field == "orders_total_revenue" and kpi.title == "Revenue (all time)"
    assert tiles["recent_orders"].kind == "table"
    assert tiles["order_funnel"].kind == "unknown" and "funnel" in tiles["order_funnel"].todo
    assert tiles["markdown_1"].kind == "text" and "net of discounts" in tiles["markdown_1"].text
    filters = {f.name: f for f in d.filters}
    assert filters["order_month"].kind == "date_range" and filters["customers_region"].default == "EU"
    (q,) = b.queries
    assert q.name == "Top customers by revenue" and "LIMIT 10" in q.sql


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    (repo / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, repo)
    sem = yaml.safe_load((repo / "models/semantic/orders.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('orders')"
    ossie = yaml.safe_load((repo / "models/semantic/orders.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "jaffle.analytics.orders"
    assert model["defaults"] == {"agg_time_dimension": "created_at"}
    assert all(m["agg"] in MF_AGGS for m in model["measures"])
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["average_order_value"]["type_params"] == {"numerator": "total_revenue", "denominator": "order_count"}
    assert "repeat_order_rate" not in metrics and todo_for((repo / "models/semantic/orders.yml").read_text(), "metric", "repeat_order_rate")
    nb = notebook_source(repo / "notebooks/sales-overview.ipynb")
    assert "TODO(unshackle)" in nb and "mark_line" in nb and "xOffset=alt.XOffset('orders_status:N')" in nb
    assert (repo / "catalog/warehouse/analytics/customers.yml").exists()
    assert "{{ user.region }}" in (repo / "policies/row_filters.lightdash.yml").read_text()
    assert (repo / "queries/top-customers-by-revenue.sql").exists()
    report = (repo / "migration/report.md").read_text()
    assert "funnel" in report and "aggregates raw columns" in report


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import run_notebook

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "jaffle.duckdb"  # catalog name matches "jaffle"."analytics"."orders"
    con = duckdb.connect(str(db))
    con.execute("create schema analytics")
    con.execute(
        "create table analytics.orders as select * from (values (1, 10, 'completed', 120.0, timestamp '2026-01-03 10:00'),"
        " (2, 11, 'shipped', 30.0, timestamp '2026-02-04 11:00')) t(order_id, customer_id, status, amount, created_at)"
    )
    con.execute("create table analytics.customers as select * from (values (10, 'EU'), (11, 'US')) t(customer_id, region)")
    con.close()
    html = run_notebook(repo / "notebooks/sales-overview.ipynb", url=f"duckdb:///{db}").read_text()
    assert "vegaEmbed" in html and "Order funnel" in html


def test_dbt_only_parse(tmp_path: Path):
    _, b = parse(tmp_path, "dbt")
    models = {m.name: m for m in b.semantic_models}
    assert set(models) == {"orders", "customers"}
    assert models["orders"].table == "analytics.orders"
    assert [e.name for e in models["orders"].entities] == ["orders", "customers"]
    metrics = {m.name: m for m in b.metrics}
    assert metrics["completed_revenue"].filter == "{{ Dimension('orders__status') }} = 'completed'"
    assert metrics["large_open_revenue"].filter == (
        "({{ Dimension('orders__status') }} <> 'returned') AND ({{ Dimension('orders__amount') }} >= 100)"
    )
    assert metrics["average_order_value"].type == "ratio"
    measures = {m.name: m for m in models["orders"].measures}
    assert measures["total_revenue"].expr == "amount" and measures["order_count"].agg == "count"
    assert b.row_filters[0].predicate == "region = {{ user.region }}"
    table_perm = next(p for p in b.permissions if p.resource_type == "Table")
    assert 'Group::"department=sales"' in table_perm.condition and table_perm.resource_id == "analytics.customers"


def _mock_lightdash(calls: list[httpx.Request]):
    chart = yaml.safe_load((FIXTURES / "charts/monthly-revenue.yaml").read_text())
    kpi = yaml.safe_load((FIXTURES / "charts/total-revenue.yaml").read_text())
    dash = {"name": "Sales overview", "slug": "sales-overview", "tiles": [
        {"type": "saved_chart", "x": 0, "y": 0, "w": 12, "h": 6, "properties": {"chartSlug": "total-revenue"}},
        {"type": "saved_chart", "x": 12, "y": 0, "w": 24, "h": 6, "properties": {"chartSlug": "monthly-revenue"}},
    ]}
    explore = json.loads((FIXTURES / "explores/orders.json").read_text())
    base = f"/api/v1/projects/{PROJECT}"

    def ok(results):
        return httpx.Response(200, json={"status": "ok", "results": results})

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == base:
            return ok({"projectUuid": PROJECT, "name": "Jaffle Shop"})
        if path == f"{base}/explores":
            return ok([{"name": "orders", "label": "Orders"}, {"name": "broken", "errors": [{"message": "x"}]}])
        if path == f"{base}/explores/orders":
            return ok(explore)
        if path == f"{base}/code/charts":
            offset = int(request.url.params.get("offset", 0))
            page = [chart, kpi][offset:offset + 1]
            return ok({"charts": page, "missingIds": [], "total": 2, "offset": offset})
        if path == f"{base}/code/dashboards":
            return ok({"dashboards": [dash], "missingIds": [], "total": 1, "offset": 0})
        if path == f"{base}/explores/orders/compileQuery":
            body = json.loads(request.content)
            return ok({"query": f"SELECT {', '.join(body['metrics'])} FROM orders"})
        if path == f"{base}/dashboards":
            return ok([{"uuid": "d-1", "name": "Sales overview", "views": 5}])
        if path == f"{base}/charts":
            return ok([{"uuid": "c-1", "name": "Monthly revenue", "spaceName": "Sales"}])
        return httpx.Response(404, json={"error": path})

    return handler


def test_extract_with_mock_transport(tmp_path: Path):
    calls: list[httpx.Request] = []
    client = Client("https://ld.example.com", headers={"Authorization": "ApiKey secret"}, per_minute=0,
                    transport=httpx.MockTransport(_mock_lightdash(calls)))
    src = LightdashSource(client=client, project=PROJECT)
    store = RawStore(tmp_path, "lightdash")
    src.extract(store)
    store.save()
    assert calls[0].headers["authorization"] == "ApiKey secret"
    assert store.exists("explores/orders.json") and not store.exists("explores/broken.json")
    assert store.get_text("charts/monthly-revenue.sql").strip() == "SELECT orders_total_revenue FROM orders"
    assert store.exists("charts/total-revenue.yaml") and store.exists("dashboards/sales-overview.yaml")
    assert store.get_json("project.json")["url"] == "https://ld.example.com"
    assert any("broken" in e["item"] for e in store.get_json("_errors.json"))
    b = LightdashSource().parse(RawStore(tmp_path, "lightdash"))
    (d,) = b.dashboards
    assert d.views == 5 and [t.id for t in d.tiles] == ["total_revenue", "monthly_revenue"]
    assert d.tiles[1].sql.startswith("SELECT orders_total_revenue")

    inv = src.inventory()
    kinds = {(i.kind, i.id) for i in inv.items}
    assert ("dashboard", "d-1") in kinds and ("explore", "orders") in kinds and ("chart", "c-1") in kinds


def test_extract_only_limits_to_dashboard_charts(tmp_path: Path):
    calls: list[httpx.Request] = []
    client = Client("https://ld.example.com", per_minute=0, transport=httpx.MockTransport(_mock_lightdash(calls)))
    store = RawStore(tmp_path, "lightdash")
    LightdashSource(client=client, project=PROJECT).extract(store, only={"sales-overview"})
    assert store.exists("charts/monthly-revenue.yaml") and store.exists("explores/orders.json")


def test_live_capture_one_to_one_join_keyed_on_joined_table(tmp_path: Path):
    """Extracted from self-hosted Lightdash 3.23.1: orders joins shippings one-to-one on
    shippings.order_id, which is not shippings' primary key. The relationship must still reach the output."""
    shutil.copytree(FIXTURES.parent / "lightdash_extract", tmp_path / "migration" / "raw" / "lightdash")
    b = LightdashSource().parse(RawStore(tmp_path, "lightdash"))
    models = {m.name: m for m in b.semantic_models}
    assert [(e.name, e.type, e.expr) for e in models["shippings"].entities] == [("shippings", "primary", "shipping_id"), ("orders", "foreign", "order_id")]
    assert not [w for w in b.warnings if "not its primary key" in w.message]
    build.apply(b, tmp_path)
    ossie = yaml.safe_load((tmp_path / "semantic/shippings.ossie.yaml").read_text())
    assert {(r["from"], r["to"], tuple(r["from_columns"])) for r in ossie["relationships"]} >= {("shippings", "orders", ("order_id",))}
    (d,) = b.dashboards
    assert [t.id for t in d.tiles] == ["markdown_1", "total_revenue", "monthly_revenue", "revenue_by_region"]
    assert all(t.sql and not t.todo for t in d.tiles[1:])
