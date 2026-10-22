from conftest import notebook_source
import json
import shutil
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.hex import HexSource, jinja_to_binds

FIX = Path(__file__).parent / "fixtures" / "hex"
REV = "5a8591dd-4039-49df-9202-96385ba3eff8"
CHURN = "0f2b6c1e-77aa-4a0e-9d2c-1c1f0b8e3a51"


def _store(tmp_path: Path) -> RawStore:
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "hex")
    return RawStore(tmp_path, "hex")


def _by_name(items):
    return {i.name: i for i in items}


def test_jinja_binds():
    assert jinja_to_binds("where r = '{{ region }}' and d >= {{start}}") == ("where r = :region and d >= :start", "")
    sql, todo = jinja_to_binds("{% if x %}and a in ({{ x | array }}){% endif %}")
    assert "{% if" in sql and todo


def test_parse_project_from_export(tmp_path: Path):
    b = get_source("hex").parse(_store(tmp_path))
    dash = _by_name(b.dashboards)["Revenue Overview"]
    assert dash.views == 812 and dash.slug == "revenue-overview"
    assert dash.layout == [["c-intro"], ["c-kpi", "c-chart"], ["c-growth-chart"]]
    assert [t.id for t in dash.tiles] == ["c-intro", "c-kpi", "c-chart", "c-growth-chart"]
    assert {f.name: f.default for f in dash.filters} == {"region": "EU", "start_date": "2026-01-01"}
    assert _by_name(dash.filters)["start_date"].kind == "date"
    t = {x.id: x for x in dash.tiles}
    assert t["c-intro"].kind == "text" and "amount_usd" in t["c-intro"].text
    chart = t["c-chart"]
    assert chart.mark == "line" and not chart.todo
    assert (chart.encodings["x"].field, chart.encodings["x"].type) == ("month", "temporal")
    assert (chart.encodings["y"].field, chart.encodings["y"].aggregate) == ("revenue", "sum")
    assert chart.encodings["color"].field == "region"
    # chained dataframe SQL is inlined as a CTE of the upstream cell, with Jinja -> binds
    assert chart.sql.startswith("WITH orders_in_scope AS (")
    assert "c.region = :region" in chart.sql and "from orders_in_scope" in chart.sql
    assert t["c-kpi"].kind == "kpi" and t["c-kpi"].encodings["text"].field == "revenue"
    assert "Python cell" in t["c-growth-chart"].todo and t["c-growth-chart"].sql == ""
    q = _by_name(b.queries)
    assert "WITH orders_in_scope AS" in q["Revenue Overview - Monthly revenue"].sql
    assert q["Revenue Overview - Growth"].language == "python" and q["Revenue Overview - Growth"].todo
    assert "Revenue Overview - Scratch" in q  # every SQL cell is kept as a query even if not in the app
    k = _by_name(b.knowledge)["Hex: Revenue Overview"]
    assert "excludes returned orders" in k.body and "**Revenue**" in k.body
    assert any("DuckDB" in w.message for w in b.warnings)


def test_parse_project_api_only(tmp_path: Path):
    b = get_source("hex").parse(_store(tmp_path))
    dash = _by_name(b.dashboards)["Churn deep dive"]
    t = {x.id: x for x in dash.tiles}
    assert t["k-1"].kind == "text"
    assert t["k-2"].kind == "table" and ":since" in t["k-2"].sql
    churned = t["k-3"]
    assert churned.sql.startswith("WITH last_orders AS (") and "Jinja" in churned.todo
    assert "since" in {f.name for f in dash.filters}  # bind with no known input gets a filter + todo
    assert "not in the export" in t["k-2"].todo
    assert _by_name(b.queries)["Churn deep dive - Survival curve"].language == "python"


def test_parse_semantic_yaml(tmp_path: Path):
    b = get_source("hex").parse(_store(tmp_path))
    models = _by_name(b.semantic_models)
    orders = models["orders"]
    assert orders.table == "analytics.fct_orders" and orders.default_time_dimension == ""  # several time columns, none declared: no guess
    assert {(e.name, e.type, e.expr) for e in orders.entities} == {("orders", "primary", "order_id"), ("customers", "foreign", "customer_id")}
    dims = _by_name(orders.dimensions)
    assert dims["amount"].expr == "amount_usd" and dims["order_week"].type == "time"
    meas = _by_name(orders.measures)
    assert (meas["revenue"].agg, meas["revenue"].expr) == ("sum", "amount_usd")
    assert (meas["order_count"].agg, meas["order_count"].expr) == ("count", "order_id")
    assert meas["large_order_count"].expr == "CASE WHEN (amount_usd >= 500) THEN order_id END"
    assert (meas["gross_margin"].agg, meas["gross_margin"].expr) == ("sum", "amount_usd - cost_usd")
    m = _by_name(b.metrics)
    assert m["completed_revenue"].filter == "{{ Dimension('orders__status') }} = 'complete'"
    assert (m["aov"].type, m["aov"].numerator, m["aov"].denominator) == ("ratio", "revenue", "order_count")
    assert m["revenue_per_large_order"].type == "derived"
    assert m["blended_rate"].todo and m["revenue_stddev"].todo
    assert models["active_customers"].table == "active_customers"
    assert "materialize" in _by_name(b.queries)["active_customers base query"].todo
    assert {t.name for t in b.tables} == {"fct_orders", "dim_customers"}


def test_build_outputs(tmp_path: Path):
    b = get_source("hex").parse(_store(tmp_path))
    _by_name(b.semantic_models)["orders"].default_time_dimension = "created_at"  # as if declared
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["aov"]["type_params"] == {"numerator": "revenue", "denominator": "order_count"}
    for name in ("revenue-overview", "churn-deep-dive"):
        notebook_source(tmp_path / f"notebooks/{name}.ipynb")
    nb = notebook_source(tmp_path / "notebooks/revenue-overview.ipynb")
    assert "region = 'EU'\nstart_date = '2026-01-01'" in nb
    assert "alt.Y('sum(revenue):Q')" in nb
    assert (tmp_path / "queries/revenue-overview-growth.py").read_text().count("TODO(unshackle)") == 1
    report = (tmp_path / "migration/report.md").read_text()
    assert "Python cell" in report and "func_sql" in report


def test_notebook_runs_chained_sql_on_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import notebook_queries, query, run_notebook

    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema analytics")
    con.execute("create table analytics.dim_customers as select * from (values (1, 'EU'), (2, 'NA')) t(customer_id, region)")
    con.execute(
        "create table analytics.fct_orders as select * from (values "
        "(10, 1, 'complete', timestamp '2026-02-03', 100.0), (11, 1, 'returned', timestamp '2026-02-04', 50.0), "
        "(12, 2, 'complete', timestamp '2026-03-01', 70.0)) t(order_id, customer_id, status, created_at, amount_usd)"
    )
    con.close()
    build.apply(get_source("hex").parse(_store(tmp_path)), tmp_path)
    path = tmp_path / "notebooks/revenue-overview.ipynb"
    params, sqls = notebook_queries(path)
    df = query(sqls["monthly_revenue"], params, f"duckdb:///{db}")
    assert df["revenue"].tolist() == [100.0]
    out = run_notebook(path, url=f"duckdb:///{db}")
    assert "vegaEmbed" in out.read_text()


def _mock_hex(calls: list[str]):
    projects = json.loads((FIX / "projects.json").read_text())

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.url.path}?{req.url.query.decode()}")
        assert req.headers["authorization"] == "Bearer hxtok"
        if req.url.path == "/api/v1/projects":
            if req.url.params.get("after") == "cur1":
                return httpx.Response(200, json={"values": projects[1:], "pagination": {"after": None, "before": "cur1"}})
            return httpx.Response(200, json={"values": projects[:1], "pagination": {"after": "cur1", "before": None}})
        if req.url.path == "/api/v1/cells":
            pid = req.url.params["projectId"]
            return httpx.Response(200, json={"values": json.loads((FIX / "cells" / f"{pid}.json").read_text()), "pagination": {"after": None}})
        return httpx.Response(404)

    return handler


def test_extract_and_inventory_with_mock_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HEX_API_TOKEN", "hxtok")
    calls: list[str] = []
    client = Client(
        "https://app.hex.tech/api/v1", headers={"Authorization": "Bearer hxtok"}, per_minute=0, max_retries=0,
        transport=httpx.MockTransport(_mock_hex(calls)),
    )
    src = HexSource(client=client, export_dir=str(FIX / "export"), semantic_dir=str(FIX / "semantic"))
    inv = src.inventory()
    projects = {i.id: i for i in inv.items if i.kind == "project"}
    assert set(projects) == {REV, CHURN} and projects[REV].views == 812
    assert any(i.kind == "semantic_file" for i in inv.items)

    store = RawStore(tmp_path, "hex")
    src.extract(store)
    store.save()
    assert len(store.get_json("projects.json")) == 2
    assert store.exists(f"cells/{CHURN}.json") and store.exists("export/revenue_overview.yaml")
    assert store.exists("semantic/ecommerce.yaml")
    b = HexSource().parse(RawStore(tmp_path, "hex"))
    assert {d.name for d in b.dashboards} == {"Revenue Overview", "Churn deep dive"}

    only = RawStore(tmp_path / "only", "hex")
    HexSource(client=client).extract(only, {CHURN})
    assert [p["id"] for p in only.get_json("projects.json")] == [CHURN]
    assert not only.exists(f"cells/{REV}.json")
