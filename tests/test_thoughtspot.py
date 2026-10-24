from conftest import notebook_source, todo_for
import json
import re
import shutil
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.thoughtspot import ThoughtSpotSource

FIX = Path(__file__).parent / "fixtures" / "thoughtspot"
LB = "c4d5e6f7-1111-4a2b-9c3d-4e5f6a7b3001"
ANSWER = "e1e2e3e4-2222-4b3c-8d4e-5f6a7b8c4001"
MODEL = "9b8e1d4a-77c2-4e0f-8f7a-5e2b1c0d2001"
ORDERS_T = "3f1c6a2e-0b1d-4c55-9a51-0d2f8a7e1001"
CUSTOMERS_T = "3f1c6a2e-0b1d-4c55-9a51-0d2f8a7e1002"
FEEDBACK = "f0f1f2f3-3333-4c4d-9e5f-6a7b8c9d5001"


def _store(tmp_path: Path) -> RawStore:
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "thoughtspot")
    return RawStore(tmp_path, "thoughtspot")


def _by_name(items):
    return {i.name: i for i in items}


def test_module_is_clean_room():
    """ThoughtSpot's own libraries forbid building converters to other tools: never import them."""
    src = (Path(__file__).parents[1] / "src/unshackle/sources/thoughtspot.py").read_text()
    code = re.sub(r'"""(.|\n)*?"""', "", src)
    for banned in ("thoughtspot_tml", "cs_tools", "thoughtspot_rest_api", "rest_api_sdk", "rest-api-sdk"):
        assert banned not in code
    assert "clean-room" in src.split('"""')[1].lower()
    import tomllib

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    deps = project.get("dependencies", []) + [d for ds in project.get("optional-dependencies", {}).values() for d in ds]
    assert not [d for d in deps if re.search(r"thoughtspot|cs[-_]tools", d, re.I)]


def test_parse_tables_and_models(tmp_path: Path):
    b = get_source("thoughtspot").parse(_store(tmp_path))
    tables = {t.name: t for t in b.tables}
    assert set(tables) == {"fct_orders", "dim_customers"}
    orders_t = tables["fct_orders"]
    assert orders_t.schema == "public" and orders_t.source_ref == f"thoughtspot:table/{ORDERS_T}"
    cols = {c.name: c for c in orders_t.columns}
    assert (cols["order_date"].type, cols["order_date"].description) == ("date", "Date the order was placed (UTC).")
    assert cols["amount"].type == "double" and cols["amount"].label == "AMOUNT"

    models = _by_name(b.semantic_models)
    assert set(models) == {"orders", "customers"}
    orders = models["orders"]
    assert orders.table == "analytics.public.fct_orders" and orders.default_time_dimension == ""  # several time columns, none declared: no guess
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [("order", "primary", "order_id"), ("customer", "foreign", "customer_id")]
    assert [(e.name, e.type, e.expr) for e in models["customers"].entities] == [("customer", "primary", "customer_id")]
    dims = _by_name(orders.dimensions)
    assert dims["order_date"].type == "time" and dims["order_date"].label == "Order Date"
    assert dims["status"].description == "paid, refunded or pending."
    assert dims["order_size"].expr == "case when amount >= 500 then 'Large' else 'Small' end"
    assert (dims["order_month"].type, dims["order_month"].time_granularity, dims["order_month"].expr) == (
        "time", "month", "date_trunc('month', order_date)")
    assert set(_by_name(models["customers"].dimensions)) == {"customer_name", "segment", "country", "signup_date"}
    meas = _by_name(orders.measures)
    assert (meas["revenue"].agg, meas["revenue"].expr, meas["revenue"].description) == ("sum", "amount", "Gross order amount in USD.")
    assert (meas["order_count"].agg, meas["order_count"].expr) == ("count_distinct", "order_id")
    assert (meas["net_revenue"].agg, meas["net_revenue"].expr) == ("sum", "amount - discount")  # MEASURE over a row formula
    assert _by_name(models["customers"].measures)["customer_count"].agg == "count_distinct"


def test_parse_metrics(tmp_path: Path):
    b = get_source("thoughtspot").parse(_store(tmp_path))
    m = _by_name(b.metrics)
    assert (m["Revenue"].measure, m["Revenue"].semantic_model) == ("revenue", "orders")
    assert (m["Customer Count"].measure, m["Customer Count"].semantic_model) == ("customer_count", "customers")
    aov = m["Average Order Value"]
    assert (aov.type, aov.numerator, aov.denominator, aov.description) == ("ratio", "revenue", "order_count", "Revenue per distinct order.")
    paid = m["Paid Revenue"]
    assert paid.measure == "revenue" and paid.filter == "{{ Dimension('order__status') }} = 'paid'"
    assert m["Net Revenue"].measure == "net_revenue"
    share = m["Region Revenue Share"]
    assert "group_aggregate" in share.todo and "level-of-detail" in share.todo
    assert share.source_language == "thoughtspot" and share.source_expression.startswith("sum ( [FCT_ORDERS::AMOUNT] )")
    run = m["Running Revenue"]
    assert "cumulative_sum" in run.todo and "cumulative metric" in run.todo
    assert {k for k, x in m.items() if x.todo} == {"Region Revenue Share", "Running Revenue"}


def test_unique_count_and_nullif(tmp_path: Path):
    store = _store(tmp_path)
    tml = tmp_path / "migration" / "raw" / "thoughtspot" / "tml" / "model" / f"{MODEL}.tml"
    doc = yaml.safe_load(tml.read_text())
    doc["model"]["formulas"] += [
        {"id": f"f_{name}", "name": name, "expr": expr} for name, expr in [
            ("Unique Customers", "unique count ( [FCT_ORDERS::CUSTOMER_ID] )"),
            ("Revenue per Customer", "sum ( [FCT_ORDERS::AMOUNT] ) / nullif ( unique count ( [FCT_ORDERS::CUSTOMER_ID] ) , 0 )"),
            ("Safe Revenue per Order", "safe_divide ( sum ( [FCT_ORDERS::AMOUNT] ) , nullif ( Unique Count ( [FCT_ORDERS::ORDER_ID] ) , 0 ) )"),
            ("Discount Rate", "[FCT_ORDERS::DISCOUNT] / nullif ( [FCT_ORDERS::AMOUNT] , 0 )"),
            ("Known Region", "nullif ( [FCT_ORDERS::REGION] , 'unknown' )"),
            ("Undiscounted Amount", "nullif ( [FCT_ORDERS::AMOUNT] , [FCT_ORDERS::DISCOUNT] )"),
            ("Literal Flag", "if ( [FCT_ORDERS::STATUS] = 'unique count (x)' ) then 1 else 0"),
        ]
    ]
    tml.write_text(yaml.safe_dump(doc, sort_keys=False))
    b = get_source("thoughtspot").parse(store)
    m = _by_name(b.metrics)
    orders = _by_name(b.semantic_models)["orders"]
    meas = _by_name(orders.measures)
    assert not any(m[n].todo for n in ("Unique Customers", "Revenue per Customer", "Safe Revenue per Order"))
    assert (meas[m["Unique Customers"].measure].agg, meas[m["Unique Customers"].measure].expr) == ("count_distinct", "customer_id")
    rpc = m["Revenue per Customer"]
    assert (rpc.type, rpc.numerator, rpc.denominator) == ("ratio", "revenue", "unique_customers")
    assert (m["Safe Revenue per Order"].type, m["Safe Revenue per Order"].denominator) == ("ratio", "order_count")
    dims = _by_name(orders.dimensions)
    assert dims["discount_rate"].expr == "1.0 * discount / nullif(amount, 0)"
    assert dims["known_region"].expr == "nullif(region, 'unknown')"
    und = dims["undiscounted_amount"].expr
    assert und == "nullif(amount, discount)"
    assert dims["literal_flag"].expr == "case when status = 'unique count (x)' then 1 else 0 end"
    assert not any(d.todo for d in dims.values())

    import duckdb

    con = duckdb.connect()
    rows = [(5, 5), (5, 3), (None, 3), (5, None), (None, None)]
    for a, d in rows:
        got, want = con.execute(
            f"select {und.replace('amount', '$1').replace('discount', '$2')}, nullif($1, $2)", [a, d]).fetchone()
        assert got == want


def test_parse_liveboard(tmp_path: Path):
    b = get_source("thoughtspot").parse(_store(tmp_path))
    (d,) = b.dashboards
    assert (d.name, d.slug, d.views) == ("Sales Overview", "sales-overview", 412)
    assert d.url == f"https://acme.thoughtspot.cloud/#/pinboard/{LB}"
    assert d.layout == [["Viz_1", "Viz_6"], ["Viz_2"], ["Viz_3", "Viz_5"], ["Viz_4"]]
    f = _by_name(d.filters)
    assert (f["region"].kind, f["region"].default) == ("value", ["EMEA", "AMER"])
    assert (f["order_date"].kind, f["order_date"].default, f["order_date"].field) == ("date_range", "last 12 month", "Order Date")
    t = {x.id: x for x in d.tiles}
    assert t["Viz_6"].kind == "text" and t["Viz_6"].text == "Revenue is **gross**, before refunds."
    kpi = t["Viz_1"]
    assert (kpi.kind, kpi.mark) == ("kpi", "text")
    assert (kpi.encodings["text"].field, kpi.encodings["text"].title) == ("total_revenue", "Total Revenue")
    assert '"ca_1" as total_revenue' in kpi.sql and kpi.source_config["source_sql"] is True
    line = t["Viz_2"]
    assert line.mark == "line" and line.source_config["search_query"] == "[Revenue] [Order Date].monthly [Region]"
    assert (line.encodings["x"].field, line.encodings["x"].type, line.encodings["x"].title) == ("month_order_date", "temporal", "Month(Order Date)")
    assert (line.encodings["y"].field, line.encodings["y"].type) == ("total_revenue", "quantitative")
    assert (line.encodings["color"].field, line.encodings["color"].type) == ("region", "nominal")
    assert line.sql.startswith('select\n  "ca_1" as month_order_date,\n  "ca_2" as region,\n  "ca_3" as total_revenue\nfrom (')
    assert "ca_N aliases" not in line.todo  # the mapping was confirmed from the SQL
    assert ":region" in line.todo and ":order_date" in line.todo  # liveboard filters baked into the SQL
    col = t["Viz_3"]
    assert col.mark == "bar" and col.encodings["x"].field == "segment" and col.encodings["x"].type == "nominal"
    table = t["Viz_4"]
    assert (table.kind, table.mark) == ("table", "table") and "LIMIT 10" in table.sql
    pie = t["Viz_5"]
    assert pie.mark == "arc" and pie.encodings["theta"].field == "region_share" and pie.encodings["color"].field == "region"
    assert "group_aggregate" in pie.todo and "ca_N aliases mapped" in pie.todo


def test_parse_answer_and_knowledge(tmp_path: Path):
    b = get_source("thoughtspot").parse(_store(tmp_path))
    (q,) = b.queries
    assert q.name == "Revenue by Country" and q.source_ref == f"thoughtspot:answer/{ANSWER}"
    assert '"ca_1" as "Country"' in q.sql and '"ca_2" as "Total Revenue"' in q.sql
    assert "ThoughtSpot search: [Revenue] [Country]" in q.description and "Gross revenue per customer country" in q.description
    k = _by_name(b.knowledge)
    model = k["ThoughtSpot model Sales"].body
    assert model.startswith("Orders joined to customers.")
    assert "- **Region**: Sales region of the order. Also called: territory, geo." in model
    assert "Also called: sales, gmv. AI context: Gross, before refunds and discounts." in model
    fb = k["ThoughtSpot Spotter feedback"]
    assert fb.context == "always"
    assert "- **big orders** means `[Order Size] = 'Large'`" in fb.body
    assert "top regions by revenue last quarter (upvote): `[Revenue] [Region] top 5" in fb.body


def test_parse_security(tmp_path: Path):
    b = get_source("thoughtspot").parse(_store(tmp_path))
    (rf,) = b.row_filters
    assert rf.table == "analytics.public.fct_orders" and rf.predicate == "region in ({{ user.groups }})"
    assert rf.source_expression == "[FCT_ORDERS_1::REGION] = ts_groups" and "ts_groups" in rf.todo
    perms = {(p.principal, p.action, p.resource_type, p.resource_id, p.effect) for p in b.permissions}
    assert ("*", "query", "Column", "analytics.public.fct_orders.discount", "forbid") in perms
    cls = next(p for p in b.permissions if p.resource_type == "Column")
    assert cls.condition == '!(principal in Group::"Finance")' and not cls.todo
    assert ("group:Sales Leadership", "view", "Notebook", "sales-overview", "permit") in perms
    assert ("group:Finance", "edit", "Notebook", "sales-overview", "permit") in perms
    assert ("user:dana@acme.com", "view", "Notebook", "sales-overview", "permit") in perms
    assert not any("former.employee" in p.principal for p in b.permissions)  # NO_ACCESS is not a grant
    assert ("*", "query", "Table", "analytics.public.fct_orders", "permit") in perms  # model shared with All
    analyst = [p for p in b.permissions if p.principal == "user:analyst"]
    assert {p.resource_id for p in analyst} == {"analytics.public.fct_orders", "analytics.public.dim_customers"}
    assert all("email" in p.todo for p in analyst)


def test_worksheet_tml_and_nested_sql(tmp_path: Path):
    store = RawStore(tmp_path, "thoughtspot")
    store.put_text(f"tml/table/{ORDERS_T}.tml", (FIX / f"tml/table/{ORDERS_T}.tml").read_text())
    store.put_text("tml/worksheet/ws1.tml", yaml.safe_dump({
        "guid": "ws1",
        "worksheet": {
            "name": "Orders WS",
            "tables": [{"name": "FCT_ORDERS", "fqn": ORDERS_T}],
            "table_paths": [{"id": "FCT_ORDERS_1", "table": "FCT_ORDERS", "join_path": [{}]}],
            "formulas": [{"name": "Big", "expr": "count_if ( [FCT_ORDERS_1::AMOUNT] > 100 , [FCT_ORDERS_1::ORDER_ID] )"},
                         {"name": "Weird", "expr": "sql_string_op ( 'md5({0})' , [FCT_ORDERS_1::STATUS] )"}],
            "worksheet_columns": [
                {"name": "Amount", "column_id": "FCT_ORDERS_1::AMOUNT", "properties": {"column_type": "MEASURE", "aggregation": "AVERAGE"}},
                {"name": "Status", "column_id": "FCT_ORDERS_1::STATUS", "properties": {"column_type": "ATTRIBUTE"}},
                {"name": "Big", "formula_id": "Big", "properties": {"column_type": "MEASURE"}},
                {"name": "Weird", "formula_id": "Weird", "properties": {"column_type": "ATTRIBUTE"}},
            ],
        },
    }))
    store.put_text("tml/liveboard/lb2.tml", yaml.safe_dump({"guid": "lb2", "liveboard": {"name": "WS board", "visualizations": [
        {"id": "Viz_1", "viz_guid": "v1", "answer": {"name": "By status", "tables": [{"name": "Orders WS", "fqn": "ws1"}],
                                                      "answer_columns": [{"name": "Status"}, {"name": "Average Amount"}],
                                                      "chart": {"type": "BAR", "axis_configs": [{"x": ["Status"], "y": ["Average Amount"]}]}}}],
        "layout": {"tabs": [{"name": "Main", "tiles": [{"visualization_id": "Viz_1", "x": 0, "y": 0}]}]}}}))
    # a list wrapper and a nested sql_queries level: parse must still find the query
    store.put_json("sql/liveboard/lb2.json", [{"metadata_id": "lb2", "sql_queries": [{"metadata_id": "v1", "metadata_name": "By status",
        "sql_queries": [{"metadata_id": "v1", "metadata_name": "By status",
                         "sql_query": 'SELECT "ta_1"."STATUS" "ca_1", avg("ta_1"."AMOUNT") "ca_2" FROM "FCT_ORDERS" "ta_1" GROUP BY "ca_1"'}]}]}])
    b = ThoughtSpotSource().parse(store)
    (sm,) = b.semantic_models
    assert _by_name(sm.measures)["amount"].agg == "average"
    m = _by_name(b.metrics)
    assert m["Big"].measure == "count_order_id" and m["Big"].filter == "{{ Dimension('order__amount') }} > 100"
    assert "sql_string_op" in _by_name(sm.dimensions)["weird"].todo
    (d,) = b.dashboards
    assert d.layout == [["tab-main"], ["Viz_1"]]
    tile = next(t for t in d.tiles if t.id == "Viz_1")
    # BAR is horizontal in ThoughtSpot: the measure goes on x
    assert tile.encodings["x"].field == "average_amount" and tile.encodings["x"].type == "quantitative"
    assert tile.encodings["y"].field == "status" and '"ca_2" as average_amount' in tile.sql


def test_build_outputs(tmp_path: Path):
    b = get_source("thoughtspot").parse(_store(tmp_path))
    next(sm for sm in b.semantic_models if sm.name == "orders").default_time_dimension = "order_date"  # as if declared
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('fct_orders')"
    ossie = yaml.safe_load((tmp_path / "models/semantic/orders.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "analytics.public.fct_orders"
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["average_order_value"]["type_params"] == {"numerator": "revenue", "denominator": "order_count"}
    assert metrics["paid_revenue"]["filter"] == "{{ Dimension('order__status') }} = 'paid'"
    assert "region_revenue_share" not in metrics and todo_for((tmp_path / "models/semantic/orders.yml").read_text(), "metric", "region_revenue_share")
    nb = notebook_source(tmp_path / "notebooks/sales-overview.ipynb")
    assert "mark_line" in nb and "mark_arc" in nb and "month_order_date:T" in nb and "title='Month(Order Date)'" in nb
    rows = yaml.safe_load((tmp_path / "policies/row_filters.thoughtspot.yml").read_text())["row_filters"]
    assert rows[0]["predicate"] == "region in ({{ user.groups }})" and "TODO(unshackle)" in rows[0]["todo"]
    cedar = (tmp_path / "policies/thoughtspot.cedar").read_text()
    assert 'resource in Notebook::"sales-overview"' in cedar and 'principal in Group::"Sales Leadership"' in cedar
    assert (tmp_path / "queries/revenue-by-country.sql").read_text().count('"Total Revenue"') == 1
    assert (tmp_path / "knowledge/thoughtspot-spotter-feedback.md").exists()
    assert (tmp_path / "knowledge/thoughtspot-model-sales.md").exists()
    assert (tmp_path / "catalog/warehouse/public/dim_customers.yml").exists()
    report = (tmp_path / "migration/report.md").read_text()
    assert "group_aggregate" in report and "ts_groups" in report


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import run_notebook

    db = tmp_path / "analytics.duckdb"  # ThoughtSpot SQL says "ANALYTICS"."PUBLIC"."FCT_ORDERS"
    con = duckdb.connect(str(db))
    con.execute("create schema public")
    con.execute(
        "create table public.fct_orders as select * from (values "
        "('o1', date '2025-11-03', 'c1', 'paid', 'EMEA', 120.0, 5.0), ('o2', date '2025-11-20', 'c2', 'paid', 'AMER', 80.0, 0.0), "
        "('o3', date '2025-12-02', 'c1', 'refunded', 'EMEA', 600.0, 20.0), ('o4', date '2025-12-05', 'c3', 'paid', 'APAC', 50.0, 0.0)) "
        "t(order_id, order_date, customer_id, status, region, amount, discount)"
    )
    con.execute(
        "create table public.dim_customers as select * from (values ('c1', 'Ada', 'SMB', 'UK', date '2024-01-01'), "
        "('c2', 'Bo', 'Enterprise', 'US', date '2024-02-01'), ('c3', 'Cy', 'SMB', 'JP', date '2024-03-01'))"
        " t(customer_id, customer_name, segment, country, signup_date)"
    )
    con.close()
    build.apply(get_source("thoughtspot").parse(_store(tmp_path)), tmp_path)
    out = run_notebook(tmp_path / "notebooks/sales-overview.ipynb", url=f"duckdb:///{db}")
    html = out.read_text()
    assert html.count("vegaEmbed(") == 4 and "<table" in html and "Ada" in html
    assert "APAC" not in html  # the liveboard filter baked into ThoughtSpot's SQL still applies


# ---------------------------------------------------------------------------- extract


def _tml(kind: str, guid: str) -> str:
    return (FIX / "tml" / kind / f"{guid}.tml").read_text()


def _edoc(kind: str, guid: str, name: str) -> dict:
    return {"info": {"id": guid, "name": name, "type": kind, "status": {"status_code": "OK"}}, "edoc": _tml(kind, guid)}


def _mock_ts(calls: list[dict], reject_export_options: bool = False):
    search = {t: json.loads((FIX / "metadata" / f"{t}.json").read_text()) for t in ("LOGICAL_TABLE", "LIVEBOARD", "ANSWER")}
    state = {"logins": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/rest/2.0")
        body = json.loads(req.content or b"{}")
        calls.append({"path": path, "body": body})
        if path == "/auth/token/full":
            assert body["username"] == "svc" and body["secret_key"] == "s3cr3t" and body["org_id"] == 42
            state["logins"] += 1
            return httpx.Response(200, json={"token": f"tok{state['logins']}", "creation_time_in_millis": 0, "expiration_time_in_millis": 3_600_000})
        if req.headers.get("authorization") == "Bearer tok1" and path == "/metadata/answer/sql":
            return httpx.Response(401, json={"error": "expired"})
        assert req.headers["authorization"].startswith("Bearer tok")
        if path == "/metadata/search":
            items = search[body["metadata"][0]["type"]]
            off, size = body["record_offset"], body["record_size"]
            assert body["include_stats"] is True
            return httpx.Response(200, json=items[off:off + size])
        if path == "/metadata/tml/export":
            if reject_export_options and "export_options" in body:
                return httpx.Response(400, json={"error": {"message": "Unrecognized field export_options"}})
            assert body["export_associated"] is True and body["export_fqn"] is True and body["edoc_format"] == "YAML"
            if not reject_export_options:
                assert body["export_options"] == {"export_with_associated_feedbacks": True, "export_column_security_rules": True}
            guid = body["metadata"][0]["identifier"]
            tables = [_edoc("table", ORDERS_T, "FCT_ORDERS"), _edoc("table", CUSTOMERS_T, "DIM_CUSTOMERS")]
            model = [_edoc("model", MODEL, "Sales")] + tables + ([] if reject_export_options else [_edoc("nls_feedback", FEEDBACK, "Sales")])
            if guid == MODEL:
                return httpx.Response(200, json=model)
            if guid == LB:
                return httpx.Response(200, json=[_edoc("liveboard", LB, "Sales Overview")] + model)
            if guid == ANSWER:
                return httpx.Response(200, json=[_edoc("answer", ANSWER, "Revenue by Country"),
                                                 {"info": {"id": "x", "name": "Broken", "status": {"status_code": "ERROR", "error_message": "no access"}}}])
            return httpx.Response(404, json={"error": "unknown"})
        if path == "/metadata/liveboard/sql":
            assert body == {"metadata_identifier": LB}
            return httpx.Response(200, json=json.loads((FIX / f"sql/liveboard/{LB}.json").read_text()))
        if path == "/metadata/answer/sql":
            return httpx.Response(200, json=json.loads((FIX / f"sql/answer/{ANSWER}.json").read_text()))
        if path == "/security/metadata/fetch-permissions":
            mtype = body["metadata"][0]["type"]
            if mtype == "ANSWER":
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=json.loads((FIX / f"permissions/{mtype}.json").read_text())[0])
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    return handler


def _client(handler) -> Client:
    return Client("https://acme.thoughtspot.cloud", per_minute=0, max_retries=0, transport=httpx.MockTransport(handler))


def _env(monkeypatch):
    monkeypatch.setenv("THOUGHTSPOT_USERNAME", "svc")
    monkeypatch.setenv("THOUGHTSPOT_SECRET_KEY", "s3cr3t")
    monkeypatch.setenv("THOUGHTSPOT_ORG_ID", "42")
    monkeypatch.delenv("THOUGHTSPOT_PASSWORD", raising=False)


def test_extract_with_mock_api(tmp_path: Path, monkeypatch):
    _env(monkeypatch)
    calls: list[dict] = []
    src = ThoughtSpotSource(client=_client(_mock_ts(calls)))
    inv = src.inventory()
    kinds = {(i.kind, i.id): i for i in inv.items}
    assert set(kinds) == {("model", MODEL), ("table", ORDERS_T), ("table", CUSTOMERS_T), ("liveboard", LB), ("answer", ANSWER)}
    assert kinds[("liveboard", LB)].views == 412 and kinds[("liveboard", LB)].last_viewed == "2025-09-25"
    assert kinds[("liveboard", LB)].url == f"https://acme.thoughtspot.cloud/#/pinboard/{LB}"
    assert len(src._search("LOGICAL_TABLE", page_size=2)) == 3  # pagination over record_offset

    store = RawStore(tmp_path, "thoughtspot")
    store.begin()
    src.extract(store)
    store.save()
    paths = [c["path"] for c in calls]
    assert paths.count("/auth/token/full") == 2  # login + one refresh after the 401
    exported = [c["body"]["metadata"][0]["identifier"] for c in calls if c["path"] == "/metadata/tml/export"]
    assert exported == [MODEL, LB, ANSWER]  # tables come along with their model (export_associated)
    for rel in (f"tml/model/{MODEL}.tml", f"tml/table/{ORDERS_T}.tml", f"tml/liveboard/{LB}.tml", f"tml/answer/{ANSWER}.tml",
                f"tml/nls_feedback/{FEEDBACK}.tml", f"sql/liveboard/{LB}.json", f"sql/answer/{ANSWER}.json",
                "permissions/LIVEBOARD.json", "permissions/LOGICAL_TABLE.json", "metadata/LIVEBOARD.json", "metadata/_site.json"):
        assert store.exists(rel), rel
    assert not store.exists("permissions/ANSWER.json")
    errors = store.get_json("_errors.json")
    assert any("Broken" in e["item"] and "no access" in e["message"] for e in errors)
    assert any(e["item"] == "permissions ANSWER" and "500" in e["message"] for e in errors)

    b = ThoughtSpotSource().parse(RawStore(tmp_path, "thoughtspot"))
    assert b.dashboards[0].views == 412 and len(b.dashboards[0].tiles) == 6
    assert {q.name for q in b.queries} == {"Revenue by Country"}
    assert ("group:Finance", "edit") in {(p.principal, p.action) for p in b.permissions}
    assert any(w.item == "permissions ANSWER" for w in b.warnings)

    only = RawStore(tmp_path / "only", "thoughtspot")
    calls.clear()
    ThoughtSpotSource(client=_client(_mock_ts(calls))).extract(only, {ANSWER})
    assert [c["body"]["metadata"][0]["identifier"] for c in calls if c["path"] == "/metadata/tml/export"] == [ANSWER]
    assert only.exists(f"tml/answer/{ANSWER}.tml") and not only.glob("tml/liveboard/*.tml")


def test_extract_falls_back_without_export_options(tmp_path: Path, monkeypatch):
    _env(monkeypatch)
    calls: list[dict] = []
    store = RawStore(tmp_path, "thoughtspot")
    ThoughtSpotSource(client=_client(_mock_ts(calls, reject_export_options=True))).extract(store, {MODEL})
    exports = [c["body"] for c in calls if c["path"] == "/metadata/tml/export"]
    assert "export_options" in exports[0] and "export_options" not in exports[1]
    assert store.exists(f"tml/model/{MODEL}.tml") and not store.glob("tml/nls_feedback/*.tml")
    assert any("export_options" in e["message"] for e in store.get_json("_errors.json"))


def test_password_auth(monkeypatch):
    monkeypatch.setenv("THOUGHTSPOT_USERNAME", "svc")
    monkeypatch.delenv("THOUGHTSPOT_SECRET_KEY", raising=False)
    monkeypatch.delenv("THOUGHTSPOT_ORG_ID", raising=False)
    monkeypatch.delenv("THOUGHTSPOT_ORG", raising=False)
    monkeypatch.setenv("THOUGHTSPOT_PASSWORD", "pw")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token/full"):
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(200, json=[])

    ThoughtSpotSource(client=_client(handler)).inventory()
    assert seen == {"username": "svc", "password": "pw", "validity_time_in_sec": 3600}
