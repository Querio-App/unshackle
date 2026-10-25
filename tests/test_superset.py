from conftest import notebook_source
import io
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.superset import SupersetSource, _layout, ratio_parts, simple_aggregate

FIXTURES = Path(__file__).parent / "fixtures" / "superset"  # extracted from Superset 6.1.0
RAW = Path("migration/raw/superset")
STATUS = "(status = :status OR :status IS NULL)"


def repo_with_fixtures(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES, tmp_path / RAW)
    return tmp_path


def parse(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    return repo, SupersetSource().parse(RawStore(repo, "superset"))


def edit_yaml(repo: Path, pattern: str, change) -> None:
    (path,) = (repo / RAW).glob(pattern)
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_metric_sql_shapes():
    assert simple_aggregate("SUM(post_discount_revenue)") == ("sum", "post_discount_revenue")
    assert simple_aggregate(" count(*) ") == ("count", "1")
    assert simple_aggregate("COUNT(DISTINCT account_id)") == ("count_distinct", "account_id")
    assert simple_aggregate("(AVG(CASE WHEN a = ')' THEN b END))") == ("average", "CASE WHEN a = ')' THEN b END")
    assert simple_aggregate("SUM(a) + SUM(b)") is None and simple_aggregate("SUM(SUM(a))") is None
    assert simple_aggregate("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY a)") is None
    assert ratio_parts("SUM(a) / NULLIF(COUNT(*), 0)") == ("SUM(a)", "COUNT(*)")
    assert ratio_parts("1.0 * SUM(a) / COUNT(DISTINCT b)") == ("SUM(a)", "COUNT(DISTINCT b)")
    assert ratio_parts("CAST(SUM(a) AS DECIMAL(18, 2)) / SUM(b)") == ("SUM(a)", "SUM(b)")
    assert ratio_parts("SUM(a)::float / SUM(b)") == ("SUM(a)", "SUM(b)")
    assert ratio_parts("1 - SUM(a) / SUM(b)") is None and ratio_parts("SUM(a) / SUM(b) / 2") is None
    assert ratio_parts("SUM(a / b)") is None


def test_parse_semantic_layer(tmp_path: Path):
    _, b = parse(tmp_path)
    assert [t.name for t in b.tables] == ["orders"]  # the virtual dataset has no table until it's materialized
    orders_table = b.tables[0]
    assert orders_table.schema == "public" and {c.name for c in orders_table.columns} >= {"status", "post_discount_revenue"}
    assert "order_size" not in {c.name for c in orders_table.columns}  # calculated column, not a warehouse column
    models = {m.name: m for m in b.semantic_models}
    assert set(models) == {"orders", "shipped_orders"}
    orders = models["orders"]
    assert orders.table == "public.orders" and orders.default_time_dimension == "purchased_at" and orders.description == "One row per order."
    dims = {d.name: d for d in orders.dimensions}
    assert dims["order_size"].expr == "CASE WHEN quantity >= 100 THEN 'large' ELSE 'small' END" and dims["order_size"].label == "Order size"
    assert dims["purchased_at"].type == "time" and dims["status"].type == "categorical"
    measures = {m.name: (m.agg, m.expr) for m in orders.measures}
    assert measures == {"revenue": ("sum", "post_discount_revenue"), "order_count": ("count", "1"),
                        "completed_revenue": ("sum", "CASE WHEN status = 'COMPLETED' THEN post_discount_revenue ELSE 0 END")}
    metrics = {m.name: m for m in b.metrics}
    assert metrics["revenue"].label == "Revenue" and metrics["revenue"].description == "Post-discount revenue, all statuses."
    assert metrics["revenue"].source_ref == "superset:dataset/1#revenue" and not metrics["revenue"].todo
    aov = metrics["average_order_value"]  # ratio of two metrics that already exist: no helper metrics
    assert (aov.type, aov.numerator, aov.denominator, aov.todo) == ("ratio", "revenue", "order_count", "")
    rate = metrics["discount_rate"]
    assert rate.todo.startswith("Superset metric not a single aggregate or a ratio of two")
    assert rate.source_expression == "1 - SUM(post_discount_revenue) / NULLIF(SUM(pre_discount_revenue), 0)" and rate.semantic_model == "orders"
    shipped = models["shipped_orders"]
    assert shipped.table == "shipped_orders" and not shipped.todo and [m.name for m in shipped.measures] == ["shipped_revenue"]
    (q,) = [q for q in b.queries if q.source_ref == "superset:dataset/2"]
    assert q.name == "shipped_orders" and q.sql.startswith("SELECT o.id") and "materialize as a SQL model" in q.todo
    assert any(w.item == "orders" and "no primary key" in w.message for w in b.warnings)


def test_ratio_helpers_and_integer_division(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def add(doc):
        doc["metrics"] += [
            {"metric_name": "units_per_order", "verbose_name": "Units per order", "expression": "SUM(quantity) / COUNT(*)"},
            {"metric_name": "units_per_customer", "expression": "1.0 * SUM(quantity) / NULLIF(COUNT(DISTINCT account_id), 0)"},
            {"metric_name": "per_user", "expression": "SUM(post_discount_revenue) / {{ filter_values('n')[0] }}"},
        ]
    edit_yaml(repo, "export/datasets/*/orders_*.yaml", add)
    b = SupersetSource().parse(RawStore(repo, "superset"))
    metrics = {m.name: m for m in b.metrics}
    assert "warehouse may divide as integers" in metrics["units_per_order"].todo  # Postgres: bigint / bigint truncates
    upc = metrics["units_per_customer"]
    assert (upc.type, upc.numerator, upc.denominator) == ("ratio", "units_per_customer_numerator", "units_per_customer_denominator")
    measures = {m.name: (m.agg, m.expr) for m in next(m for m in b.semantic_models if m.name == "orders").measures}
    assert measures["units_per_customer_denominator"] == ("count_distinct", "account_id")
    assert "Jinja" in metrics["per_user"].todo


def test_jinja_in_virtual_dataset(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit_yaml(repo, "export/datasets/*/shipped_orders_*.yaml",
              lambda d: d.update(sql=d["sql"] + "\nWHERE s.region = '{{ current_username() }}'"))
    b = SupersetSource().parse(RawStore(repo, "superset"))
    assert "Jinja" in next(m for m in b.semantic_models if m.name == "shipped_orders").todo
    assert "Jinja" in next(q for q in b.queries if q.name == "shipped_orders").todo
    tile = next(t for t in b.dashboards[0].tiles if t.id == "revenue_by_region")
    assert "the dataset's SQL uses Jinja" in tile.todo


def test_parse_dashboard(tmp_path: Path):
    _, b = parse(tmp_path)
    (d,) = b.dashboards
    assert (d.name, d.slug, d.views) == ("Sales overview", "sales-overview", 3)
    assert d.url == "https://superset.example.com/superset/dashboard/sales-overview/" and d.source_ref == "superset:dashboard/1"
    assert d.layout == [["average_order_value", "monthly_revenue"], ["revenue_by_region", "orders_by_status"], ["markdown_1", "orders_by_size"]]
    (f,) = d.filters
    assert (f.name, f.field, f.default, f.kind) == ("status", "orders.status", None, "value")
    tiles = {t.id: t for t in d.tiles}
    kpi = tiles["average_order_value"]
    assert kpi.kind == "kpi" and kpi.encodings["text"].field == "average_order_value" and kpi.source_type == "big_number_total"
    assert STATUS in kpi.sql and "unshackle__" not in kpi.sql and not kpi.sql.endswith(";") and not kpi.todo
    line = tiles["monthly_revenue"]
    assert line.mark == "line" and (line.encodings["x"].field, line.encodings["x"].type) == ("purchased_at", "temporal")
    assert line.encodings["y"].title == "Revenue" and "DATE_TRUNC('month', purchased_at)" in line.sql and STATUS in line.sql
    bar = tiles["revenue_by_region"]  # excluded from the filter's scope: plain chart SQL, no :status
    assert bar.mark == "bar" and (bar.encodings["x"].field, bar.encodings["color"].field) == ("region", "mode")
    assert "xOffset" not in bar.encodings and ":status" not in bar.sql and "AS virtual_table" in bar.sql and not bar.todo
    table = tiles["orders_by_status"]
    assert table.kind == "table" and f"WHERE {STATUS} AND ((quantity > 0))" in table.sql and '"Units"' in table.sql
    assert tiles["markdown_1"].kind == "text" and "post-discount" in tiles["markdown_1"].text
    pie = tiles["orders_by_size"]  # saved without a query_context: encodings from form data, SQL is a TODO
    assert pie.mark == "arc" and pie.encodings["theta"].field == "order_count" and pie.encodings["color"].field == "order_size"
    assert not pie.sql and "without a query_context" in pie.todo and pie.source_config["groupby"] == ["order_size"]


def test_chart_variants(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def horizontal(doc):
        doc["params"].update(orientation="horizontal", stack=None, metrics=["shipped_revenue", {
            "expressionType": "SIMPLE", "column": {"column_name": "id"}, "aggregate": "COUNT", "label": "COUNT(id)"}])
    edit_yaml(repo, "export/charts/Revenue_by_region_*.yaml", horizontal)

    def mixed(doc):
        doc["viz_type"] = "mixed_timeseries"
        doc["params"].update(viz_type="mixed_timeseries", metrics_b=["order_count"], seriesType="bar", seriesTypeB="line", yAxisIndexB=1)
        qc = json.loads(doc["query_context"])
        qc["queries"][0]["post_processing"].append({"operation": "rolling", "options": {"rolling_type": "mean", "window": 3}})
        doc["query_context"] = json.dumps(qc)
    edit_yaml(repo, "export/charts/Monthly_revenue_*.yaml", mixed)
    edit_yaml(repo, "export/charts/Orders_by_size_*.yaml", lambda d: d.update(viz_type="sankey_v2"))
    b = SupersetSource().parse(RawStore(repo, "superset"))
    tiles = {t.id: t for t in b.dashboards[0].tiles}
    bar = tiles["revenue_by_region"]
    assert (bar.encodings["y"].field, bar.encodings["x"].field, bar.encodings["color"].field) == ("region", "shipped_revenue", "mode")
    assert "2 bar series" in bar.todo
    assert bar.sql.startswith('SELECT\n  region,\n  mode,\n  shipped_revenue,\n  "COUNT(id)" AS count_id\nFROM (')
    combo = tiles["monthly_revenue"]
    assert combo.compose == "dual_axis" and [(l.mark, l.encodings["y"].field) for l in combo.layers] == [("bar", "revenue"), ("line", "order_count")]
    assert "post-processes this result in Python (rolling)" in combo.todo and "two queries" in combo.todo
    other = tiles["orders_by_size"]
    assert other.kind == "unknown" and "sankey_v2 chart has no Altair mapping" in other.todo


def test_layout_tabs_columns_and_headers():
    pos = {
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["TABS-1"]},
        "TABS-1": {"type": "TABS", "id": "TABS-1", "children": ["TAB-a", "TAB-b"]},
        "TAB-a": {"type": "TAB", "id": "TAB-a", "children": ["ROW-1"], "meta": {"text": "Overview"}},
        "TAB-b": {"type": "TAB", "id": "TAB-b", "children": ["HEADER-x", "ROW-2"], "meta": {"text": "Details"}},
        "ROW-1": {"type": "ROW", "id": "ROW-1", "children": ["CHART-1", "COLUMN-1"]},
        "COLUMN-1": {"type": "COLUMN", "id": "COLUMN-1", "children": ["CHART-2", "MARKDOWN-1"]},
        "CHART-1": {"type": "CHART", "id": "CHART-1", "meta": {"chartId": 1}},
        "CHART-2": {"type": "CHART", "id": "CHART-2", "meta": {"chartId": 2}},
        "MARKDOWN-1": {"type": "MARKDOWN", "id": "MARKDOWN-1", "meta": {"code": "hi"}},
        "HEADER-x": {"type": "HEADER", "id": "HEADER-x", "meta": {"text": "Breakdown"}},
        "ROW-2": {"type": "ROW", "id": "ROW-2", "children": ["CHART-3"]},
        "CHART-3": {"type": "CHART", "id": "CHART-3", "meta": {"chartId": 3}},
    }
    assert [[n["id"] for n in row] for row in _layout(pos)] == [
        ["TAB-a"], ["CHART-1", "CHART-2", "MARKDOWN-1"], ["TAB-b"], ["HEADER-x"], ["CHART-3"]]


def test_other_native_filters_become_todos(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def add(doc):
        doc["metadata"]["native_filter_configuration"].append({
            "id": "NATIVE_FILTER-time", "name": "Purchase date", "filterType": "filter_time", "targets": [{}],
            "defaultDataMask": {"filterState": {"value": "Last quarter"}}, "scope": {"rootPath": ["ROOT_ID"], "excluded": []}})
    edit_yaml(repo, "export/dashboards/*.yaml", add)
    b = SupersetSource().parse(RawStore(repo, "superset"))
    d = b.dashboards[0]
    assert [(f.name, f.kind, f.default) for f in d.filters] == [("status", "value", None), ("purchase_date", "date_range", "Last quarter")]
    line = next(t for t in d.tiles if t.id == "monthly_revenue")
    assert "dashboard time filter purchase_date applies to this chart" in line.todo


def test_parse_permissions_and_row_level_security(tmp_path: Path):
    _, b = parse(tmp_path)
    perms = {(p.principal, p.action, p.resource_type, p.resource_id) for p in b.permissions}
    assert perms == {("group:Alpha", "query", "Datasource", "warehouse"), ("group:Sales", "query", "Table", "public.orders")}
    assert not any("Admin" in p.principal for p in b.permissions)
    (rf,) = b.row_filters
    assert (rf.name, rf.table, rf.predicate, rf.groups, rf.todo) == (
        "sales_sees_completed_orders", "public.orders", "status = 'COMPLETED'", ["Sales"], "")


def test_rls_base_group_keys_and_dashboard_roles(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    rls = repo / RAW / "security/rls.json"
    rules = json.loads(rls.read_text())
    rules += [{**rules[0], "id": 2, "name": "Also returned", "clause": "status = 'RETURNED'", "roles": [{"id": 7, "name": "Returns"}]},
              {**rules[0], "id": 3, "name": "Everyone but finance", "filter_type": "Base", "group_key": "",
               "clause": "account_id = {{ current_user_id() }}", "roles": [{"id": 8, "name": "Finance"}]}]
    rls.write_text(json.dumps(rules))
    dashes = repo / RAW / "dashboards.json"
    dashes.write_text(json.dumps([{**json.loads(dashes.read_text())[0], "roles": [{"id": 6, "name": "Sales"}]}]))
    b = SupersetSource().parse(RawStore(repo, "superset"))
    filters = {r.name: r for r in b.row_filters}
    assert "ORs this with the other filters in group 'status'" in filters["also_returned"].todo and not filters["also_returned"].predicate
    base = filters["everyone_but_finance"]
    assert base.groups == [] and "except roles ['Finance']" in base.todo and "Jinja" in base.todo
    assert ("group:Sales", "view", "Notebook", "sales-overview") in {(p.principal, p.action, p.resource_type, p.resource_id) for p in b.permissions}


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    build.apply(b, repo)
    text = (repo / "semantic/orders.ossie.yaml").read_text()
    ossie = yaml.safe_load(text)
    metrics = {m["name"]: m["expression"]["dialects"][0]["expression"] for m in ossie["datasets"][0]["metrics"]}
    assert metrics == {"revenue": "SUM(post_discount_revenue)", "order_count": "COUNT(1)",
                       "completed_revenue": "SUM(completed_revenue_value)",
                       "average_order_value": "1.0 * SUM(post_discount_revenue) / nullif(COUNT(1), 0)"}
    assert "TODO(unshackle): metric discount_rate" in text
    nb = notebook_source(repo / "notebooks/sales-overview.ipynb")
    assert "status = None" in nb and "params={'status': status}" in nb and nb.count("TODO(unshackle)") == 2  # the pie, twice
    assert "materialize as a SQL model" in (repo / "queries/shipped-orders.sql").read_text()
    cedar = (repo / "policies/superset.cedar").read_text()
    assert 'principal in Group::"Sales"' in cedar and 'resource in Table::"warehouse.public.orders"' in cedar
    rows = yaml.safe_load((repo / "policies/row_filters.superset.yml").read_text())["row_filters"]
    assert rows[0]["predicate"] == "status = 'COMPLETED'" and rows[0]["groups"] == ["Sales"]
    assert "discount_rate" in (repo / "migration/report.md").read_text()


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import notebook_queries, query, run_notebook

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "dunder.duckdb"
    con = duckdb.connect(str(db))
    con.execute(  # Superset compiles public.orders; the virtual dataset says orders: tables in main, views in public
        "create table orders as select * from (values (1, 'COMPLETED', 10, 100.0, 95.0, timestamp '2025-01-03 10:00'),"
        " (2, 'CANCELLED', 20, 200.0, 200.0, timestamp '2025-02-04 11:00')) "
        "t(id, status, quantity, pre_discount_revenue, post_discount_revenue, purchased_at)")
    con.execute("create table shippings as select * from (values (1, 1, 'West', 'EXPRESS'), (2, 2, 'South', 'REGULAR')) t(id, order_id, region, mode)")
    con.execute("create schema public")
    con.execute("create view public.orders as select * from main.orders")
    con.close()
    url = f"duckdb:///{db}"
    html = run_notebook(repo / "notebooks/sales-overview.ipynb", url=url).read_text()
    assert "vegaEmbed" in html and "Revenue by region" in html
    params, sqls = notebook_queries(repo / "notebooks/sales-overview.ipynb")
    assert query(sqls["monthly_revenue"], {**params, "status": "COMPLETED"}, url)["revenue"].tolist() == [95.0]
    assert sorted(query(sqls["monthly_revenue"], params, url)["revenue"].tolist()) == [95.0, 200.0]  # empty filter = no filter
    assert query(sqls["average_order_value"], params, url)["average_order_value"].tolist() == [147.5]


# ------------------------------------------------------------ extract (mock transport)


def _export_zip(top: str, rels: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel in rels:
            data = (FIXTURES / "export" / rel).read_text()
            if rel.startswith("databases/"):
                data += "encrypted_extra: '{\"credentials\": \"secret\"}'\n"
            z.writestr(f"{top}/{rel}", data)
    return buf.getvalue()


def _mock(calls: list[httpx.Request]):
    def j(rel: str):
        return json.loads((FIXTURES / rel).read_text())

    everything = sorted(str(p.relative_to(FIXTURES / "export")) for p in (FIXTURES / "export").rglob("*.yaml"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/v1/security/login":
            return httpx.Response(200, json={"access_token": "jwt-token", "refresh_token": "r"})
        if path == "/version":
            return httpx.Response(200, json={"version_string": "6.1.0"})
        lists = {"/api/v1/dashboard/": "dashboards.json", "/api/v1/chart/": "charts.json", "/api/v1/dataset/": "datasets.json",
                 "/api/v1/security/roles/search/": "security/roles.json", "/api/v1/security/permissions-resources/": "security/permissions.json",
                 "/api/v1/rowlevelsecurity/": "security/rls.json"}
        if path in lists:
            rows = j(lists[path])
            return httpx.Response(200, json={"count": len(rows), "result": rows})
        if path == "/api/v1/assets/export/":
            return httpx.Response(200, content=_export_zip("assets_export_20261022T000000", everything))
        if path == "/api/v1/dashboard/export/":
            return httpx.Response(200, content=_export_zip("dashboard_export_20261022T000000", everything))
        if path == "/api/v1/chart/data":
            body = json.loads(request.content)
            extra = [f for q in body["queries"] for f in q.get("filters") or [] if str(f.get("val")).find("unshackle__") >= 0]
            where = f" WHERE {extra[0]['col']} IN ('{extra[0]['val'][0]}')" if extra else ""
            return httpx.Response(200, json={"result": [{"language": "sql", "query": f"SELECT 1 FROM t{where} LIMIT 10;"}]})
        if path == "/api/v1/log/":
            return httpx.Response(200, json={"count": 2, "result": [
                {"action": "dashboard", "dashboard_id": 1, "slice_id": None, "dttm": "2026-10-20T10:00:00"},
                {"action": "ChartDataRestApi.data", "dashboard_id": 1, "slice_id": 2, "dttm": "2026-10-21T10:00:00"},
                {"action": "_get_data_response", "dashboard_id": 1, "slice_id": 2, "dttm": "2026-10-21T10:00:00"}]})
        return httpx.Response(404, json={"message": "Not found"})

    return handler


def test_extract_with_mock_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[httpx.Request] = []
    monkeypatch.setenv("SUPERSET_URL", "https://ss.example.com")
    monkeypatch.setenv("SUPERSET_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_PASSWORD", "pw")
    monkeypatch.delenv("SUPERSET_API_KEY", raising=False)
    real = Client.__init__
    monkeypatch.setattr(Client, "__init__", lambda self, url, **kw: real(self, url, **{**kw, "per_minute": 0, "max_retries": 0,
                                                                                         "transport": httpx.MockTransport(_mock(calls))}))
    store = RawStore(tmp_path, "superset")
    SupersetSource().extract(store)
    store.save()
    login = json.loads(calls[0].content)
    assert calls[0].url.path == "/api/v1/security/login" and login == {"username": "admin", "password": "pw", "provider": "db", "refresh": True}
    assert all(c.headers["authorization"] == "Bearer jwt-token" for c in calls[1:])
    assert store.get_json("instance.json") == {"url": "https://ss.example.com", "version": "6.1.0"}
    assert store.exists("export/dashboards/Sales_overview_1.yaml") and store.exists("export/datasets/Dunder_Mifflin/orders_1.yaml")
    db = yaml.safe_load(store.get_text("export/databases/Dunder_Mifflin.yaml"))
    assert "encrypted_extra" not in db and db["database_name"] == "Dunder Mifflin"
    charts = {c["slice_name"]: c["uuid"] for c in store.get_json("charts.json")}
    dash = store.get_json("dashboards.json")[0]["uuid"]
    assert store.get_text(f"sql/{charts['Monthly revenue']}.sql") == "SELECT 1 FROM t LIMIT 10;\n"
    assert store.get_text(f"sql/{dash}/{charts['Monthly revenue']}.sql") == "SELECT 1 FROM t WHERE status IN ('unshackle__status') LIMIT 10;\n"
    assert not store.exists(f"sql/{dash}/{charts['Revenue by region']}.sql")  # outside the filter's scope
    assert not store.exists(f"sql/{charts['Orders by size']}.sql")  # no query_context to compile
    assert store.get_json("usage.json") == {"dashboards": {"1": {"events": 1, "last": "2026-10-20"}},
                                            "charts": {"2": {"events": 1, "last": "2026-10-21"}}}
    assert store.get_json("_errors.json") == []
    bodies = [json.loads(c.content) for c in calls if c.url.path == "/api/v1/chart/data"]
    assert all(b["result_type"] == "query" for b in bodies)
    b = SupersetSource().parse(store)  # what was extracted parses
    assert b.dashboards[0].views == 1


def test_extract_only_uses_per_kind_exports_and_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[httpx.Request] = []
    client = Client("https://ss.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock(calls)))
    client.set_header("Authorization", "Bearer sst_key")
    store = RawStore(tmp_path, "superset")
    SupersetSource(client=client).extract(store, only={"dashboard:1"})
    paths = [c.url.path for c in calls]
    assert "/api/v1/dashboard/export/" in paths and "/api/v1/assets/export/" not in paths
    assert next(c for c in calls if c.url.path == "/api/v1/dashboard/export/").url.params["q"] == "!(1)"
    monkeypatch.setenv("SUPERSET_URL", "https://ss.example.com")
    monkeypatch.setenv("SUPERSET_API_KEY", "sst_key")
    assert SupersetSource().client().http.headers["authorization"] == "Bearer sst_key"  # no login call with a key


def test_inventory_with_mock_transport():
    client = Client("https://ss.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock([])))
    inv = SupersetSource(client=client).inventory()
    items = {i.id: i for i in inv.items}
    assert {"dashboard:1", "chart:2", "dataset:1", "dataset:2"} <= set(items)
    assert items["dashboard:1"].views == 1 and items["dashboard:1"].url == "https://ss.example.com/superset/dashboard/sales-overview/"
    assert items["chart:2"].views == 1 and items["chart:1"].views == 0 and items["dataset:2"].extra["kind"] == "virtual"
