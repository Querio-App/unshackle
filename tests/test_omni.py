from conftest import notebook_source, todo_for
import json
import shutil
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.omni import OmniSource

FIXTURES = Path(__file__).parent / "fixtures" / "omni"
MODEL = "a1b2c3d4-5e6f-4a0b-9c8d-111111111111"
DASH = "7f3e9a21"
MF_AGGS = {"sum", "count", "count_distinct", "average", "min", "max", "median", "percentile", "sum_boolean"}


def parse(tmp_path: Path):
    shutil.copytree(FIXTURES, tmp_path / "migration" / "raw" / "omni")
    return tmp_path, OmniSource().parse(RawStore(tmp_path, "omni"))


def test_parse_semantic_layer(tmp_path: Path):
    _, b = parse(tmp_path)
    assert {(t.schema, t.name) for t in b.tables} == {("ecomm", "order_items"), ("ecomm", "users")}
    models = {m.name: m for m in b.semantic_models}
    items = models["order_items"]
    assert items.table == "ecomm.order_items" and items.default_time_dimension == "created_at"
    assert [(e.name, e.type, e.expr) for e in items.entities] == [("order_items", "primary", '"ID"'), ("users", "foreign", '"USER_ID"')]
    dims = {d.name: d for d in items.dimensions}
    assert dims["created_at"].type == "time" and dims["status"].type == "categorical"
    assert dims["margin"].expr == '"SALE_PRICE" - "COST"'
    measures = {m.name: m for m in items.measures}
    # `count` exists in both views, so both get prefixed to stay globally unique
    assert measures["order_items_count"].agg == "count" and measures["order_items_count"].expr == '"ID"'
    assert measures["total_revenue"].expr == '"SALE_PRICE"' and measures["average_sale_price"].agg == "average"
    assert measures["total_margin"].expr == '("SALE_PRICE" - "COST")'
    metrics = {m.name: m for m in b.metrics}
    assert metrics["completed_revenue"].filter == "{{ Dimension('order_items__status') }} = 'Complete'"
    assert metrics["non_returned_count"].filter == "{{ Dimension('order_items__status') }} NOT IN ('Returned', 'Cancelled')"
    assert (metrics["margin_pct"].type, metrics["margin_pct"].numerator, metrics["margin_pct"].denominator) == ("ratio", "total_margin", "total_revenue")
    assert metrics["revenue_per_user"].denominator == "users_count"
    assert "list" in metrics["statuses"].todo and metrics["statuses"].source_language == "omni"


def test_parse_knowledge_access_and_queries(tmp_path: Path):
    _, b = parse(tmp_path)
    know = {k.name: k for k in b.knowledge}
    assert know["Ecommerce model context"].context == "always" and "Fiscal year" in know["Ecommerce model context"].body
    assert "Exclude Cancelled" in know["Ecommerce topic"].body and "`users`" in know["Ecommerce topic"].body
    assert "sales, gmv" in know["Order Items field notes"].body
    (rf,) = b.row_filters
    assert rf.table == "ecomm.users" and rf.predicate == '"COUNTRY" = {{ user.country }}' and rf.todo
    perms = {p.resource_type: p for p in b.permissions}
    assert perms["Column"].resource_id == "ecomm.users.email" and 'Group::"team=support"' in perms["Column"].condition
    assert perms["Table"].resource_id == "ecomm.order_items" and 'Group::"department=finance"' in perms["Table"].condition
    queries = {q.name: q for q in b.queries}
    sample = queries["What was revenue by country last month?"]
    assert "SUM(" in sample.sql and sample.aliases == ["Revenue by country"] and not sample.todo
    assert queries["How many items are in each status?"].todo  # no SQL extracted for this sample query
    assert "WHERE status = 'Returned'" in queries["Refund audit (SQL)"].sql  # SQL tab in a workbook
    assert "Returned items check" in queries  # workbook tab not placed on the dashboard


def test_parse_dashboard(tmp_path: Path):
    _, b = parse(tmp_path)
    (d,) = b.dashboards
    assert d.name == "Sales Overview" and d.views == 128 and d.url == f"https://acme.omniapp.co/dashboards/{DASH}"
    assert d.layout == [["read_me"], ["total_revenue", "revenue_by_month"], ["revenue_by_country_and_status", "margin_distribution"],
                        ["running_revenue", "top_users"]]
    tiles = {t.id: t for t in d.tiles}
    line = tiles["revenue_by_month"]
    assert line.mark == "line" and not line.todo
    assert (line.encodings["x"].field, line.encodings["x"].type) == ("order_items_created_at_month", "temporal")
    assert '"order_items.created_at[month]" AS order_items_created_at_month' in line.sql and ") AS omni_query" in line.sql
    assert "LIMIT 1000;" not in line.sql  # trailing semicolon stripped before wrapping
    bar = tiles["revenue_by_country_and_status"]
    assert bar.mark == "bar" and bar.encodings["x"].field == "users_country" and bar.encodings["color"].field == "order_items_status"
    assert tiles["total_revenue"].kind == "kpi" and tiles["total_revenue"].encodings["text"].type == "quantitative"
    assert tiles["top_users"].kind == "table"
    assert "from_dict" in tiles["margin_distribution"].todo and tiles["margin_distribution"].source_config["spec"]["mark"] == "boxplot"
    assert "Running total" in tiles["running_revenue"].todo
    assert tiles["read_me"].kind == "text" and "net of returns" in tiles["read_me"].text
    filters = {f.name: f for f in d.filters}
    assert filters["order_date"].kind == "date_range" and filters["country"].default == "United Kingdom"


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    (repo / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, repo)
    sem = yaml.safe_load((repo / "models/semantic/order_items.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('order_items')"
    ossie = yaml.safe_load((repo / "models/semantic/order_items.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "ecomm.order_items"
    assert all(m["agg"] in MF_AGGS for m in model["measures"])
    assert {e["name"] for e in model["entities"]} == {"order_items", "users"}
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["completed_revenue"]["filter"] == "{{ Dimension('order_items__status') }} = 'Complete'"
    assert "statuses" not in metrics and todo_for((repo / "models/semantic/order_items.yml").read_text(), "metric", "statuses")
    nb = notebook_source(repo / "notebooks/sales-overview.ipynb")
    assert nb.count("TODO(unshackle)") >= 2 and "alt.Chart.from_dict" in nb
    assert (repo / "queries/refund-audit-sql.sql").exists()
    assert (repo / "knowledge/ecommerce-model-context.md").exists()
    assert "department=finance" in (repo / "policies/omni.cedar").read_text()
    report = (repo / "migration/report.md").read_text()
    assert "from_dict" in report and "Running total" in report


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import run_notebook

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema ecomm")
    con.execute(
        "create table ecomm.order_items as select * from (values (1, 100, 7, 'Complete', timestamp '2026-03-02 09:00', 40.0, 25.0),"
        " (2, 101, 8, 'Returned', timestamp '2026-04-10 12:00', 15.0, 9.0)) t(ID, ORDER_ID, USER_ID, STATUS, CREATED_AT, SALE_PRICE, COST)"
    )
    con.execute("create table ecomm.users as select * from (values (7, 'a@x.com', 'United Kingdom'), (8, 'b@x.com', 'France')) t(ID, EMAIL, COUNTRY)")
    con.close()
    html = run_notebook(repo / "notebooks/sales-overview.ipynb", url=f"duckdb:///{db}").read_text()
    assert "order_items_created_at_month" in html and "vegaEmbed(" in html


def _mock_omni(calls: list[httpx.Request]):
    files = {}
    for p in sorted((FIXTURES / "models" / MODEL).rglob("*.yaml")):
        key = str(p.relative_to(FIXTURES / "models" / MODEL)).removesuffix(".yaml")
        files[key] = p.read_text()
    docs = json.loads((FIXTURES / "documents/_index.json").read_text())
    queries = json.loads((FIXTURES / f"documents/{DASH}/queries.json").read_text())
    export = json.loads((FIXTURES / f"documents/{DASH}/export.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/v1/models":
            if request.url.params.get("cursor") == "p2":
                return httpx.Response(200, json={"records": [{"id": "wb-1", "modelKind": "WORKBOOK"}], "pageInfo": {"hasNextPage": False}})
            return httpx.Response(200, json={"records": [{"id": MODEL, "name": "Ecommerce", "modelKind": "SHARED"}],
                                             "pageInfo": {"hasNextPage": True, "nextCursor": "p2"}})
        if path == f"/api/v1/models/{MODEL}/yaml":
            assert request.url.params.get("fullyResolved") == "true"
            return httpx.Response(200, json={"files": files, "viewNames": ["order_items", "users"], "checksums": {}})
        if path == "/api/v1/query/run":
            body = json.loads(request.content)
            assert body["planOnly"] is True and body["query"]["modelId"] == MODEL
            fields = body["query"].get("fields", [])
            sql = "SELECT " + ", ".join(f'1 AS "{f}"' for f in fields)
            # a streamed-looking response: one JSON document per line
            return httpx.Response(200, text=json.dumps({"jobs_submitted": {}}) + "\n" + json.dumps({"plan": {"query": {"sql": sql}}}))
        if path == "/api/v1/documents":
            return httpx.Response(200, json={"records": docs[:1], "pageInfo": {"hasNextPage": False}})
        if path == f"/api/v1/documents/{DASH}/queries":
            return httpx.Response(200, json={"queries": queries})
        if path == f"/api/unstable/documents/{DASH}/export":
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/api/v1/content/export-dashboard":
            assert json.loads(request.content) == {"dashboardId": DASH}
            return httpx.Response(200, json=export)
        return httpx.Response(404, json={"detail": path})

    return handler


def test_extract_with_mock_transport(tmp_path: Path):
    calls: list[httpx.Request] = []
    client = Client("https://acme.omniapp.co", headers={"Authorization": "Bearer k"}, per_minute=0,
                    transport=httpx.MockTransport(_mock_omni(calls)))
    src = OmniSource(client=client)
    store = RawStore(tmp_path, "omni")
    src.extract(store)
    store.save()
    assert calls[0].headers["authorization"] == "Bearer k"
    assert store.exists(f"models/{MODEL}/ecomm/order_items.view.yaml") and store.exists(f"models/{MODEL}/relationships.yaml")
    assert store.exists(f"models/{MODEL}/sample_queries/ecommerce__revenue-by-country.sql")
    assert store.get_text(f"documents/{DASH}/sql/q1.sql").startswith('SELECT 1 AS "order_items.created_at[month]"')
    assert store.get_json(f"documents/{DASH}/export.json")["document"]["identifier"] == DASH
    assert store.get_json("_errors.json") == []
    b = OmniSource().parse(RawStore(tmp_path, "omni"))
    (d,) = b.dashboards
    assert len([t for t in d.tiles if t.kind != "text"]) == 6
    assert "order_items_total_revenue" in next(t for t in d.tiles if t.id == "total_revenue").sql
    inv = src.inventory()
    assert ("dashboard", DASH) in {(i.kind, i.id) for i in inv.items}
    assert ("model", "wb-1") in {(i.kind, i.id) for i in inv.items}
