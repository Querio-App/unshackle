from conftest import notebook_source, todo_for
import json
import shutil
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.sigma import SigmaSource, _layout_positions

FIX = Path(__file__).parent / "fixtures" / "sigma"
WB = FIX / "workbooks" / "wb-exec"


def _store(tmp_path: Path) -> RawStore:
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "sigma")
    return RawStore(tmp_path, "sigma")


def _by_name(items):
    return {i.name: i for i in items}


def test_parse_data_model_semantics(tmp_path: Path):
    b = get_source("sigma").parse(_store(tmp_path))
    tables = {t.name: t for t in b.tables}
    assert set(tables) == {"fct_orders", "dim_customers"}  # custom SQL is not a catalog table yet
    orders_t = tables["fct_orders"]
    assert orders_t.schema == "public" and orders_t.description.startswith("One row per order")
    cols = {c.name: c for c in orders_t.columns}
    # physical names come from the inode-.../COLUMN id, display names become labels
    assert list(cols) == ["order_id", "customer_id", "created_at", "status", "region", "amount_usd", "discount"]
    assert cols["amount_usd"].label == "Amount" and cols["amount_usd"].description == "Gross order amount in USD."

    models = _by_name(b.semantic_models)
    assert set(models) == {"orders", "customers", "large_orders", "targets"}
    orders = models["orders"]
    assert orders.table == "analytics.public.fct_orders" and orders.default_time_dimension == ""  # several time columns, none declared: no guess
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [("order", "primary", "order_id"), ("customer", "foreign", "customer_id")]
    assert [(e.name, e.type, e.expr) for e in models["customers"].entities] == [("customer", "primary", "customer_id")]
    assert models["customers"].name == "customers"  # unnamed element: name from the table, dim_ prefix dropped
    dims = _by_name(orders.dimensions)
    assert dims["created_at"].type == "time" and dims["created_at"].time_granularity == "day"
    assert dims["amount"].expr == "amount_usd" and dims["amount"].label == "Amount"
    assert dims["net_amount"].expr == "amount_usd - discount"
    assert dims["order_size"].expr == "case when amount_usd >= 500 then 'Large' else 'Small' end"
    assert (dims["order_month"].type, dims["order_month"].time_granularity, dims["order_month"].expr) == (
        "time", "month", "date_trunc('month', created_at)")
    assert "Lookup/Rollup" in dims["customer_name"].todo and dims["customer_name"].source_language == "sigma"
    assert dims["customer_name"].source_expression.startswith("Lookup(")
    assert "passthrough" in dims["customer_hash"].todo
    assert "region_access" not in dims  # RLS helper column is a row filter, not a dimension
    meas = _by_name(orders.measures)
    assert (meas["revenue"].agg, meas["revenue"].expr) == ("sum", "amount_usd")
    assert (meas["orders"].agg, meas["orders"].expr) == ("count_distinct", "order_id")
    assert meas["median_order"].agg == "median"
    # child element and custom SQL element
    large = models["large_orders"]
    assert large.table == "analytics.public.fct_orders"
    assert _by_name(large.dimensions)["amount"].expr == "amount_usd"
    assert _by_name(large.dimensions)["created_at"].type == "time"
    assert models["targets"].table == "sigma_targets"
    assert _by_name(models["targets"].measures)["target"].expr == "target_usd"
    q = _by_name(b.queries)["Targets el-targets (Sigma custom SQL)"]
    assert "finance.monthly_targets" in q.sql and "sigma_targets" in q.todo
    assert any("Large Orders" in w.item and "filters/groupings" in w.message for w in b.warnings)


def test_parse_metrics(tmp_path: Path):
    b = get_source("sigma").parse(_store(tmp_path))
    m = _by_name(b.metrics)
    assert (m["Revenue"].type, m["Revenue"].measure, m["Revenue"].semantic_model) == ("simple", "revenue", "orders")
    aov = m["Average Order Value"]
    assert (aov.type, aov.numerator, aov.denominator) == ("ratio", "revenue", "orders")
    paid = m["Paid Revenue"]
    assert paid.measure == "revenue" and paid.filter == "{{ Dimension('order__status') }} = 'paid'"
    rate = m["Discount Rate"]
    assert (rate.type, rate.numerator, rate.denominator) == ("ratio", "sum_discount", "revenue")
    assert m["sum_discount"].measure == "sum_discount"  # helper metric for the ratio's numerator
    net = m["Net Revenue"]  # metric minus a plain aggregate -> derived over a helper metric
    assert (net.type, net.expr, net.metrics) == ("derived", "revenue - sum_discount", ["revenue", "sum_discount"])
    cum = m["Cumulative Revenue"]
    assert cum.type == "cumulative" and cum.measure == "revenue" and "CumulativeSum" in cum.todo
    assert cum.source_expression == "CumulativeSum(Sum([Amount]))" and cum.source_language == "sigma"
    assert m["Customers"].semantic_model == "customers"
    assert all(not x.todo for k, x in m.items() if k != "Cumulative Revenue")
    (k,) = b.knowledge
    assert k.name == "Sigma data model Sales" and "Revenue is gross" in k.body
    assert "**Average Order Value** (`[Revenue] / [Orders]`)" in k.body and "- Status: paid, refunded or pending." in k.body


def test_parse_security(tmp_path: Path):
    b = get_source("sigma").parse(_store(tmp_path))
    (rf,) = b.row_filters
    assert rf.table == "analytics.public.fct_orders" and rf.predicate == "region = {{ user.region }}"
    assert rf.groups == ["*"] and not rf.todo  # the element filters on the column, so it is enforced
    perms = [(p.principal, p.action, p.resource_type, p.resource_id, p.effect) for p in b.permissions]
    assert ("*", "query", "Column", "analytics.public.fct_orders.discount", "forbid") in perms
    cls = next(p for p in b.permissions if p.resource_type == "Column")
    assert cls.condition == '!(principal in Group::"Finance")'
    assert ("group:Finance", "view", "Notebook", "executive-sales", "permit") in perms
    assert ("group:Finance", "edit", "Notebook", "executive-sales", "permit") in perms
    assert ("user:dana@acme.com", "view", "Notebook", "executive-sales", "permit") in perms
    sales = [p for p in b.permissions if p.principal == "group:Sales"]
    assert [p.action for p in sales] == ["view"] and "explore" in sales[0].source_expression


def test_parse_workbook(tmp_path: Path):
    b = get_source("sigma").parse(_store(tmp_path))
    (d,) = b.dashboards
    assert d.name == "Executive Sales" and d.slug == "executive-sales"
    assert d.url.endswith("Executive-Sales-wb-exec") and d.description.startswith("Weekly exec")
    assert d.layout == [
        ["page-pg-overview"], ["el-title"], ["el-kpi-rev", "el-kpi-aov"], ["el-rev-month"], ["el-rev-region", "el-status-donut"],
        ["page-pg-detail"], ["el-orders-table"], ["el-rolling", "el-map"],
    ]
    assert "el-orders-src" not in {t.id for t in d.tiles}  # hidden data page
    f = _by_name(d.filters)
    assert (f["order_date"].kind, f["order_date"].default, f["order_date"].field) == ("date_range", "last 12 month", "Month of Created At")
    assert (f["region"].kind, f["region"].default, f["region"].field) == ("value", ["EMEA"], "Region")
    t = {x.id: x for x in d.tiles}
    assert t["el-title"].kind == "text" and "gross, before refunds" in t["el-title"].text
    kpi = t["el-kpi-rev"]
    assert (kpi.kind, kpi.mark, kpi.title) == ("kpi", "text", "Total Revenue")
    assert list(kpi.encodings) == ["text"] and kpi.encodings["text"].field == "revenue"  # timeline column ignored
    assert kpi.source_config["source_sql"] is True and '"Revenue" as revenue' in kpi.sql
    line = t["el-rev-month"]
    assert line.mark == "line"
    assert (line.encodings["x"].field, line.encodings["x"].type, line.encodings["x"].title) == (
        "month_of_created_at", "temporal", "Month of Created At")
    assert (line.encodings["y"].field, line.encodings["y"].type) == ("revenue", "quantitative")
    assert (line.encodings["color"].field, line.encodings["color"].type) == ("region", "nominal")
    assert line.sql.startswith("select\n  \"Month of Created At\" as month_of_created_at") and 'sum(Q1."AMOUNT_USD")' in line.sql
    assert ":order_date" in line.todo
    bar = t["el-rev-region"]  # horizontal: category on y, value on x; single-colour `color` ignored
    assert bar.mark == "bar" and set(bar.encodings) == {"x", "y"}
    assert (bar.encodings["y"].field, bar.encodings["y"].type) == ("region", "nominal")
    assert (bar.encodings["x"].field, bar.encodings["x"].type, bar.encodings["x"].title) == ("revenue_usd", "quantitative", "Revenue (USD)")
    assert ":region" in bar.todo
    donut = t["el-status-donut"]
    assert donut.mark == "arc" and donut.encodings["theta"].field == "revenue" and donut.encodings["color"].field == "status"
    assert (t["el-orders-table"].kind, t["el-orders-table"].mark) == ("table", "table")
    assert "movingavg" in t["el-rolling"].todo and t["el-rolling"].encodings["x"].type == "temporal"
    m = t["el-map"]
    assert m.mark == "unknown" and not m.sql and "region-map" in m.todo and "guessed" in m.todo
    assert any("el-map" in w.item and "Maps are not supported" in w.message for w in b.warnings)


def test_parse_legacy_contents(tmp_path: Path):
    store = RawStore(tmp_path, "sigma")
    store.put_json("workbooks/wb1/workbook.json", {"workbookId": "wb1", "name": "Legacy"})
    store.put_json("workbooks/wb1/contents.json", {
        "_unshackle_source": "legacy-endpoints",
        "pages": [{"id": "p1", "name": "Page 1", "elements": [
            {"id": "e1", "name": "Revenue by Region", "type": "visualization", "vizType": "bar",
             "columns": [{"id": "Region", "name": "Region", "formula": "[Orders/Region]"},
                         {"id": "Revenue", "name": "Revenue", "formula": "Sum([Orders/Amount])"}]},
            {"id": "e2", "name": "Total", "type": "visualization", "vizType": "kpi",
             "columns": [{"id": "Total", "name": "Total", "formula": "Sum([Orders/Amount])"}]},
            {"id": "e3", "name": "Notes", "type": "text", "text": "hello"},
        ]}],
    })
    store.put_text("workbooks/wb1/sql/e1.sql", 'select "REGION" "Region", sum("AMOUNT") "Revenue" from t group by 1\n')
    b = SigmaSource().parse(store)
    (d,) = b.dashboards
    t = {x.id: x for x in d.tiles}
    assert t["e1"].mark == "bar" and t["e1"].encodings["x"].field == "region" and t["e1"].encodings["y"].field == "revenue"
    assert "guessed" in t["e1"].todo
    assert t["e2"].kind == "kpi" and t["e2"].encodings["text"].field == "Total" and not t["e2"].sql
    assert t["e3"].kind == "text"
    assert d.layout == [["e1"], ["e2"], ["e3"]]


def test_layout_xml_nested_containers():
    pos = _layout_positions(
        '<?xml version="1.0"?><Page id="p" gridTemplateColumns="repeat(24, 1fr)">'
        '<Container elementId="c" gridColumn="13 / 25" gridRow="5 / 9" gridTemplateColumns="repeat(2, 1fr)">'
        '<Element elementId="b" gridColumn="2 / 3" gridRow="1 / 2"/><Element elementId="a" gridColumn="1 / 2" gridRow="1 / 2"/>'
        "</Container></Page>"
    )
    items = {eid: (y, x) for y, x, eid in pos["p"]}
    assert items["a"] == (4.0, 0.5) and items["b"] == (4.0, 0.75)


def test_build_outputs(tmp_path: Path):
    b = get_source("sigma").parse(_store(tmp_path))
    next(sm for sm in b.semantic_models if sm.name == "orders").default_time_dimension = "created_at"  # as if declared
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('fct_orders')"
    ossie = yaml.safe_load((tmp_path / "models/semantic/orders.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "analytics.public.fct_orders"
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["average_order_value"]["type_params"] == {"numerator": "revenue", "denominator": "orders"}
    assert metrics["net_revenue"]["type_params"]["expr"] == "revenue - sum_discount"
    text = (tmp_path / "models/semantic/orders.yml").read_text()
    assert "cumulative_revenue" not in metrics and todo_for(text, "metric", "cumulative_revenue")
    assert todo_for(text, "dimension", "customer_name")
    nb = notebook_source(tmp_path / "notebooks/executive-sales.ipynb")
    assert "mark_line" in nb and "mark_arc" in nb and "month_of_created_at:T" in nb
    rows = yaml.safe_load((tmp_path / "policies/row_filters.sigma.yml").read_text())["row_filters"]
    assert rows[0]["predicate"] == "region = {{ user.region }}"
    cedar = (tmp_path / "policies/sigma.cedar").read_text()
    assert 'forbid (' in cedar and 'resource in Column::"warehouse.public.fct_orders.discount"' in cedar
    assert 'principal in Group::"Finance"' in cedar and 'principal == User::"dana@acme.com"' in cedar
    assert (tmp_path / "catalog/warehouse/public/fct_orders.yml").exists()
    assert (tmp_path / "knowledge/sigma-data-model-sales.md").exists()
    assert (tmp_path / "queries/targets-el-targets-sigma-custom-sql.sql").exists()
    report = (tmp_path / "migration/report.md").read_text()
    assert "Lookup/Rollup" in report and "region-map" in report


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import run_notebook

    db = tmp_path / "analytics.duckdb"  # Sigma SQL says ANALYTICS.PUBLIC.FCT_ORDERS: catalog = file name
    con = duckdb.connect(str(db))
    con.execute("create schema public")
    con.execute(
        "create table public.fct_orders as select * from (values "
        "('o1', 'c1', timestamp '2025-11-03 10:00', 'paid', 'EMEA', 120.0, 5.0), "
        "('o2', 'c2', timestamp '2025-11-20 12:00', 'paid', 'AMER', 80.0, 0.0), "
        "('o3', 'c1', timestamp '2025-12-02 09:00', 'refunded', 'EMEA', 600.0, 20.0)) "
        "t(order_id, customer_id, created_at, status, region, amount_usd, discount)"
    )
    con.close()
    build.apply(get_source("sigma").parse(_store(tmp_path)), tmp_path)
    out = run_notebook(tmp_path / "notebooks/executive-sales.ipynb", url=f"duckdb:///{db}")
    html = out.read_text()
    assert html.count("vegaEmbed(") == 6  # the map has no SQL or mark: an empty table
    assert "<table" in html and "o3" in html


# ---------------------------------------------------------------------------- extract


def _mock_sigma(calls: list[str], legacy: bool = False):
    spec = json.loads((FIX / "data_models/dm-sales/spec.json").read_text())
    contents = json.loads((WB / "contents.json").read_text())
    queries = json.loads((WB / "queries.json").read_text())["entries"]
    wb = json.loads((WB / "workbook.json").read_text())
    state = {"tokens": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        q = dict(req.url.params)
        calls.append(f"{req.method} {path}" + (f"?page={q['page']}" if "page" in q else ""))
        if path == "/v2/auth/token":
            form = parse_qs(req.content.decode())
            assert form == {"grant_type": ["client_credentials"], "client_id": ["cid"], "client_secret": ["sec"]}
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok{state['tokens']}", "token_type": "bearer", "expires_in": 3600})
        if req.headers.get("authorization") == "Bearer tok1" and path == "/v2/workbooks/wb-exec/grants":
            return httpx.Response(401, json={"message": "token expired"})  # forces one re-auth
        assert req.headers["authorization"].startswith("Bearer tok")
        if path == "/v2/dataModels":
            if q.get("page") == "p2":
                return httpx.Response(200, json={"entries": [{"dataModelId": "dm-gone", "name": "Gone"}], "nextPage": None})
            return httpx.Response(200, json={"entries": [{"dataModelId": "dm-sales", "name": "Sales", "url": spec["url"]}], "nextPage": "p2"})
        if path == "/v2/dataModels/dm-sales/spec":
            assert q.get("format") == "json"
            return httpx.Response(200, json=spec)
        if path == "/v2/dataModels/dm-gone/spec":
            return httpx.Response(403, json={"message": "forbidden"})
        if path == "/v2/workbooks":
            return httpx.Response(200, json={"entries": [{k: wb[k] for k in ("workbookId", "name", "url", "path")}], "nextPage": None})
        if path == "/v2/workbooks/wb-exec":
            assert q.get("includeContents") == "true"
            return httpx.Response(200, json={**wb, **({} if legacy else {"contents": contents})})
        if path == "/v2/workbooks/wb-exec/queries":
            if q.get("page") == "q2":
                return httpx.Response(200, json={"entries": queries[4:], "nextPage": None})
            # the first page lacks el-rev-month, which must come from the per-element endpoint
            first = [e for e in queries[:4] if e["elementId"] != "el-rev-month"]
            return httpx.Response(200, json={"entries": first, "nextPage": "q2"})
        if path == "/v2/workbooks/wb-exec/elements/el-rev-month/query":
            sql = next(e["sql"] for e in queries if e["elementId"] == "el-rev-month")
            return httpx.Response(200, json={"elementId": "el-rev-month", "sql": sql})
        if path.startswith("/v2/workbooks/wb-exec/elements/") and path.endswith("/query"):
            el = path.split("/")[-2]
            e = next((x for x in queries if x["elementId"] == el), None)
            return httpx.Response(200, json=e) if e else httpx.Response(404, json={"message": "no element"})
        if path == "/v2/workbooks/wb-exec/grants":
            return httpx.Response(200, json={"entries": json.loads((WB / "grants.json").read_text()), "nextPage": None})
        if path == "/v2/teams":
            return httpx.Response(200, json={"entries": [{"teamId": "t-fin", "name": "Finance", "description": "x"}, {"teamId": "t-sales", "name": "Sales"}]})
        if path == "/v2/members":
            return httpx.Response(200, json={"entries": [{"memberId": "m-dana", "email": "dana@acme.com", "firstName": "Dana"}]})
        if legacy and path == "/v2/workbooks/wb-exec/pages":
            return httpx.Response(200, json={"entries": [{"pageId": "p1", "name": "Overview"}]})
        if legacy and path == "/v2/workbooks/wb-exec/pages/p1/elements":
            return httpx.Response(200, json={"entries": [
                {"elementId": "el-rev-month", "name": "Revenue by Month", "type": "visualization", "vizualizationType": "line",
                 "columns": ["Month of Created At", "Revenue"]}]})
        if legacy and path == "/v2/workbooks/wb-exec/elements/el-rev-month/columns":
            return httpx.Response(200, json={"entries": [
                {"columnId": "c1", "label": "Month of Created At", "formula": 'DateTrunc("month", [Orders/Created At])'},
                {"columnId": "c2", "label": "Revenue", "formula": "[Orders/Revenue]"}]})
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    return handler


def _client(handler) -> Client:
    return Client("https://aws-api.sigmacomputing.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(handler))


def test_extract_with_mock_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGMA_CLIENT_ID", "cid")
    monkeypatch.setenv("SIGMA_CLIENT_SECRET", "sec")
    calls: list[str] = []
    src = SigmaSource(client=_client(_mock_sigma(calls)))
    inv = src.inventory()
    assert {(i.kind, i.id) for i in inv.items} == {("data_model", "dm-sales"), ("data_model", "dm-gone"), ("workbook", "wb-exec")}
    assert "audit" in inv.notes[0].lower()

    store = RawStore(tmp_path, "sigma")
    store.begin()
    src.extract(store)
    store.save()
    assert calls.count("POST /v2/auth/token") == 2  # first login + one refresh after the 401
    assert "GET /v2/dataModels?page=p2" in calls and "GET /v2/workbooks/wb-exec/queries?page=q2" in calls
    assert store.exists("data_models/dm-sales/spec.json") and not store.exists("data_models/dm-gone/spec.json")
    assert store.exists("workbooks/wb-exec/contents.json")
    assert "contents" not in store.get_json("workbooks/wb-exec/workbook.json")
    assert store.exists("workbooks/wb-exec/sql/el-rev-month.sql")  # from the per-element fallback
    assert "GET /v2/workbooks/wb-exec/elements/el-map/query" not in calls  # queries already said why there is no SQL
    assert not store.exists("workbooks/wb-exec/sql/el-map.sql")
    assert store.get_json("teams.json") == [{"teamId": "t-fin", "name": "Finance"}, {"teamId": "t-sales", "name": "Sales"}]
    assert store.get_json("members.json") == [{"memberId": "m-dana", "email": "dana@acme.com"}]
    errors = store.get_json("_errors.json")
    assert any(e["item"] == "dataModel dm-gone" and "403" in e["message"] for e in errors)
    assert any("el-map" in e["item"] and "Maps are not supported" in e["message"] for e in errors)

    b = SigmaSource().parse(RawStore(tmp_path, "sigma"))
    assert len(b.dashboards[0].tiles) == 11 and b.dashboards[0].tiles[4].sql
    assert any(w.item == "dataModel dm-gone" for w in b.warnings)
    assert ("user:dana@acme.com", "view") in {(p.principal, p.action) for p in b.permissions}

    only = RawStore(tmp_path / "only", "sigma")
    calls.clear()
    SigmaSource(client=_client(_mock_sigma(calls))).extract(only, {"wb-exec"})
    assert only.exists("workbooks/wb-exec/contents.json") and not only.glob("data_models/*/spec.json")


def test_extract_legacy_endpoints(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGMA_CLIENT_ID", "cid")
    monkeypatch.setenv("SIGMA_CLIENT_SECRET", "sec")
    calls: list[str] = []
    store = RawStore(tmp_path, "sigma")
    SigmaSource(client=_client(_mock_sigma(calls, legacy=True))).extract(store, {"wb-exec"})
    contents = store.get_json("workbooks/wb-exec/contents.json")
    assert contents["_unshackle_source"] == "legacy-endpoints"
    (el,) = contents["pages"][0]["elements"]
    assert el["vizType"] == "line" and [c["name"] for c in el["columns"]] == ["Month of Created At", "Revenue"]
    b = SigmaSource().parse(store)
    (tile,) = b.dashboards[0].tiles
    assert tile.mark == "line" and tile.encodings["x"].type == "temporal" and tile.encodings["x"].field == "month_of_created_at"
    assert tile.sql


def test_join_and_unsupported_elements(tmp_path: Path):
    store = RawStore(tmp_path, "sigma")
    wh = lambda *p: {"kind": "warehouse-table", "connectionId": "c", "path": list(p)}  # noqa: E731
    store.put_json("data_models/dm1/spec.json", {"name": "Flights", "pages": [{"id": "p", "elements": [
        {"id": "f", "kind": "table", "source": wh("DB", "RAW", "FLIGHTS"),
         "columns": [{"id": "inode-a/ORIGIN", "formula": "[FLIGHTS/Origin Airport]"}, {"id": "inode-a/FLIGHT_ID", "formula": "[FLIGHTS/Flight Id]"}],
         "metrics": [{"id": "m1", "name": "Flights", "formula": "Count()"}]},
        {"id": "a", "kind": "table", "source": wh("DB", "RAW", "AIRPORTS"),
         "columns": [{"id": "inode-b/IATA_CODE", "formula": "[AIRPORTS/Iata Code]"}, {"id": "inode-b/CITY", "formula": "[AIRPORTS/City]"}]},
        {"id": "j", "kind": "table", "name": "Flights with airports",
         "source": {"kind": "join", "primarySource": {"elementId": "f", "kind": "table"},
                    "joins": [{"left": {"elementId": "f", "kind": "table"}, "right": {"elementId": "a", "kind": "table"},
                               "columns": [{"left": "[Origin Airport]", "right": "[Iata Code]"}], "joinType": "left"}]},
         "columns": [{"id": "x", "formula": "[FLIGHTS/Origin Airport]"}]},
        {"id": "u", "kind": "table", "source": {"kind": "union", "sources": []}, "columns": []},
        {"id": "csv", "kind": "table", "source": {"kind": "csv"}, "columns": []},
        {"id": "chart", "kind": "bar-chart", "source": {"kind": "table", "elementId": "f"}},
    ]}]})
    b = SigmaSource().parse(store)
    models = _by_name(b.semantic_models)
    assert set(models) == {"flights", "airports", "u"}
    assert "no sources" in models["u"].todo and models["u"].table == "sigma_u"
    assert ("airport", "foreign", "origin") in [(e.name, e.type, e.expr) for e in models["flights"].entities]
    assert ("airport", "primary", "iata_code") in [(e.name, e.type, e.expr) for e in models["airports"].entities]
    assert _by_name(b.metrics)["Flights"].measure and (_by_name(models["flights"].measures)["flights"].agg, _by_name(models["flights"].measures)["flights"].expr) == ("count", "1")
    msgs = " ".join(w.message for w in b.warnings)
    assert "join element" in msgs and "union" in msgs and "CSV upload" in msgs


def _wh(*path):
    return {"kind": "warehouse-table", "connectionId": "c", "path": list(path)}


NAMES = {"id": "n", "kind": "table", "source": _wh("EXAMPLES", "USA_NAMES", "USA_1910_CURRENT"),
         "columns": [{"id": "inode-a/STATE", "formula": "[USA_1910_CURRENT/State]"},
                     {"id": "inode-a/NAME_TEXT", "formula": "[USA_1910_CURRENT/Name]", "name": "Name"},
                     {"id": "inode-a/NUMBER", "formula": "[USA_1910_CURRENT/Number]"},
                     {"id": "c-twice", "formula": "[Number] * 2", "name": "Twice"}]}


def _union(sources, matches, **extra):
    return {"id": "u", "kind": "table", "source": {"kind": "union", "sources": sources, "matches": matches, **extra},
            "columns": [{"id": f"c{i}", "formula": f"[Union of 2 Sources/{m['outputColumnName']}]"} for i, m in enumerate(matches)]
            + [{"id": "c-big", "formula": "If([Number] > 100, \"Big\", \"Small\")", "name": "Size"}]}


def test_union_columns_line_up(tmp_path: Path):
    # Shape of Sigma's documented example: a warehouse table and a table element of the same data model.
    store = RawStore(tmp_path, "sigma")
    matches = [{"outputColumnName": "State", "sourceColumns": ["[State]", "[State]"]},
               {"outputColumnName": "Name", "sourceColumns": ["[Name]", "[Name]"]},
               {"outputColumnName": "Number", "sourceColumns": ["[Number]", "[Twice]"]}]
    store.put_json("data_models/dm1/spec.json", {"dataModelId": "dm1", "name": "Names", "pages": [{"id": "p", "elements": [
        _union([_wh("EXAMPLES", "USA_NAMES", "USA_1910_CURRENT"), {"elementId": "n", "kind": "table"}], matches), NAMES]}]})
    b = SigmaSource().parse(store)
    models = _by_name(b.semantic_models)
    union = models["union_of_2_sources"]
    assert union.table == "sigma_union_of_2_sources" and not union.todo
    dims = {d.name: d.expr for d in union.dimensions}
    assert dims["state"] == "" and dims["number"] == "" and "case when number > 100" in dims["size"].lower()
    (q,) = [q for q in b.queries if "union" in q.name.lower()]
    assert q.sql == (
        "select state as state, name_text as name, number as number\nfrom examples.usa_names.usa_1910_current\n"
        "union all\n"
        "select state as state, name_text as name, number * 2 as number\nfrom examples.usa_names.usa_1910_current"
    )
    assert "materialize" in q.todo and "sigma_union_of_2_sources" in q.todo


def test_union_that_does_not_line_up_keeps_todo(tmp_path: Path):
    store = RawStore(tmp_path, "sigma")
    filtered = dict(NAMES, id="f", name="Texas", filters=[{"columnId": "inode-a/STATE", "values": ["TX"]}])
    store.put_json("data_models/dm1/spec.json", {"dataModelId": "dm1", "name": "Names", "pages": [{"id": "p", "elements": [
        NAMES, filtered,
        _union([{"elementId": "n", "kind": "table"}, {"elementId": "f", "kind": "table"}],
               [{"outputColumnName": "State", "sourceColumns": ["[State]", "[State]"]}]),
        dict(_union([{"elementId": "n", "kind": "table"}, _wh("EXAMPLES", "USA_NAMES", "OTHER")],
                    [{"outputColumnName": "State", "sourceColumns": ["[State]", None]}]), id="u2"),
    ]}]})
    b = SigmaSource().parse(store)
    todos = {m.source_ref.rpartition("#")[2]: m.todo for m in b.semantic_models}
    assert "element Texas has its own filters/groupings" in todos["u"]
    assert "'State' has no matching column in source 2" in todos["u2"]
    q = next(q for q in b.queries if q.source_ref.endswith("#u2"))
    assert q.sql.startswith("-- Sigma union source, not translated:") and '"union"' in q.sql and "UNION ALL" in q.todo


def test_data_model_source(tmp_path: Path):
    store = RawStore(tmp_path, "sigma")
    # dm-0 sorts first but builds on dm-1, so dm-1 has to be parsed first.
    store.put_json("data_models/dm-0/spec.json", {"dataModelId": "dm-0", "name": "Downstream", "pages": [{"id": "p", "elements": [
        {"id": "d", "kind": "table", "name": "Big names",
         "source": {"kind": "data-model", "dataModelId": "dm-1", "elementId": "n"},
         "columns": [{"id": "x1", "formula": "[Names/State]"}, {"id": "x2", "formula": "[Names/Twice]"},
                     {"id": "x3", "formula": "[Twice] + 1", "name": "Plus One"}]},
        {"id": "gone", "kind": "table", "source": {"kind": "data-model", "dataModelId": "dm-missing", "elementId": "z"}, "columns": []},
        _union([{"kind": "data-model", "dataModelId": "dm-1", "elementId": "n"}, _wh("EXAMPLES", "USA_NAMES", "USA_1910_CURRENT")],
               [{"outputColumnName": "Number", "sourceColumns": ["[Twice]", "[Number]"]}]),
    ]}]})
    store.put_json("data_models/dm-1/spec.json", {"dataModelId": "dm-1", "name": "Upstream", "pages": [{"id": "p", "elements": [
        dict(NAMES, name="Names")]}]})
    b = SigmaSource().parse(store)
    models = _by_name(b.semantic_models)
    big = models["big_names"]
    assert big.table == "examples.usa_names.usa_1910_current"
    dims = {d.name: (d.expr, d.todo) for d in big.dimensions}
    assert dims["state"] == ("", "") and dims["twice"] == ("number * 2", "") and dims["plus_one"] == ("number * 2 + 1", "")
    assert not any(m.source_ref.endswith("#gone") for m in b.semantic_models)
    assert any("dm-missing" in w.message and "not extracted" in w.message for w in b.warnings)
    (q,) = [q for q in b.queries if q.source_ref.endswith("dm-0#u")]
    assert q.sql.startswith("select number * 2 as number\nfrom examples.usa_names.usa_1910_current\nunion all\nselect number as number")
