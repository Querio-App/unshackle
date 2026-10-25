from conftest import notebook_source
import json
import shutil
from pathlib import Path

import httpx
import pytest

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.redash import RedashSource, render

FIXTURES = Path(__file__).parent / "fixtures" / "redash"  # extracted from Redash 26.9.0
RAW = Path("migration/raw/redash")


def repo_with_fixtures(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES, tmp_path / RAW)
    return tmp_path


def parse(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    return repo, RedashSource().parse(RawStore(repo, "redash"))


def edit(repo: Path, rel: str, change) -> None:
    path = repo / RAW / rel
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


def widget(doc: dict, viz_id: int) -> dict:
    return next(w for w in doc["widgets"] if (w.get("visualization") or {}).get("id") == viz_id)


def tiles_after(tmp_path: Path, change) -> dict:
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "dashboards/1.json", change)
    b = RedashSource().parse(RawStore(repo, "redash"))
    return {t.id: t for t in b.dashboards[0].tiles}


def test_render_parameters():
    text = {"name": "status", "type": "text"}
    params = {"status": text, "n": {"name": "n", "type": "number"}, "d": {"name": "d", "type": "date-range"},
              "region": {"name": "region", "type": "enum", "multiValuesOptions": {"prefix": "'", "suffix": "'", "separator": ","}},
              "col": {"name": "col", "type": "text"}}
    binds = {k: k for k in params}
    sql, todos = render("SELECT * FROM t WHERE s = '{{ status }}' AND n > {{n}} AND d >= '{{ d.start }}' AND d < '{{d.end}}'",
                        params, binds, {})
    assert sql == "SELECT * FROM t WHERE s = :status AND n > :n AND d >= :d_start AND d < :d_end" and todos == []
    sql, todos = render("SELECT {{ col }} FROM t WHERE r IN ({{ region }}) AND x = '{{ nope }}' AND y LIKE '%{{ status }}%'",
                        params, binds, {})
    assert sql == "SELECT {{ col }} FROM t WHERE r IN :region AND x = '{{ nope }}' AND y LIKE '%{{ status }}%'"
    assert any("{{ col }} (text) is spliced into the SQL as raw text, without quotes" in t for t in todos)
    assert any("{{ nope }} is not a declared parameter" in t for t in todos)
    assert any("{{ status }} (text) is spliced inside a string literal" in t and "'%' || :status || '%'" in t for t in todos)
    # several values: only IN ({{ p }}) with single quotes is a list bind; anything else stays TODO
    raw = {**params, "ids": {"name": "ids", "type": "enum", "multiValuesOptions": {"prefix": "", "suffix": "", "separator": ","}}}
    sql, todos = render("SELECT * FROM t WHERE r not in ( {{region}} ) AND id IN ({{ ids }}) AND s = '{{ region }}'", raw, binds | {"ids": "ids"}, {})
    assert sql == "SELECT * FROM t WHERE r not in :region AND id IN ({{ ids }}) AND s = '{{ region }}'"
    assert any("{{ ids }} allows several values, which Redash splices into the SQL unquoted" in t for t in todos)
    assert any("{{ region }} allows several values, which Redash splices into the SQL quoted with '" in t for t in todos)
    sql, todos = render("SELECT * FROM t WHERE r IN ({{ region }})", params, {}, {"region": ["West", "South"]})
    assert sql == "SELECT * FROM t WHERE r IN ('West','South')" and todos == []  # static values: joined as Redash does
    sql, todos = render("SELECT {{ col }} FROM t WHERE d >= '{{ d.start }}' AND s = '{{ status }}'", params, {"status": "s"},
                        {"col": "region", "d": {"start": "2025-01-01", "end": "2025-02-01"}})
    assert sql == "SELECT region FROM t WHERE d >= '2025-01-01' AND s = :s" and todos == []  # static values: raw text, as Redash
    assert "mustache sections" in render("SELECT 1 {{# n }}WHERE n = {{ n }}{{/ n }}", params, binds, {})[1][0]


def test_parse_queries(tmp_path: Path):
    _, b = parse(tmp_path)
    queries = {q.name: q for q in b.queries}
    assert list(queries) == ["Monthly revenue", "Revenue by region", "Revenue in period", "Orders by paper type"]
    assert all(not q.todo and q.datasource == "warehouse" for q in b.queries)
    region = queries["Revenue by region"]
    assert "WHERE o.status = :status" in region.sql and region.source_ref == "redash:query/2"
    assert region.description == ("Revenue and orders per shipping region for one order status. Parameters: :status (text, default "
                                  "'COMPLETED'). Redash visualizations: Revenue by region and mode (column chart).")
    period = queries["Revenue in period"]
    assert "purchased_at >= :purchased_start\n  AND purchased_at < :purchased_end" in period.sql
    assert ":purchased_start / :purchased_end (date-range, default 2025-01-01 to 2025-07-01)" in period.description
    assert "{{" not in "".join(q.sql for q in b.queries) and not b.warnings
    (k,) = b.knowledge
    assert k.name == "Redash query snippets" and "## completed" in k.body and "status = 'COMPLETED'" in k.body


def test_parse_dashboard(tmp_path: Path):
    _, b = parse(tmp_path)
    (d,) = b.dashboards
    assert (d.name, d.slug, d.views, d.url) == ("Sales overview", "sales-overview", 2, "https://redash.example.com/dashboards/1")
    assert d.layout == [["text_1"], ["revenue_in_period", "monthly_revenue"], ["revenue_by_region", "orders_by_paper_type_order_share"],
                        ["orders_by_paper_type_table"]]
    assert [(f.name, f.default, f.kind) for f in d.filters] == [
        ("status", "COMPLETED", "value"), ("purchased_start", "2025-01-01", "date"), ("purchased_end", "2025-07-01", "date")]
    tiles = {t.id: t for t in d.tiles}
    assert tiles["text_1"].kind == "text" and "**post-discount**" in tiles["text_1"].text
    kpi = tiles["revenue_in_period"]
    assert kpi.kind == "kpi" and (kpi.encodings["text"].field, kpi.encodings["text"].title) == ("revenue", "Revenue")
    assert ":purchased_start" in kpi.sql and kpi.source_type == "COUNTER"
    line = tiles["monthly_revenue"]
    assert line.mark == "line" and (line.encodings["x"].field, line.encodings["x"].type) == ("month", "temporal")
    assert line.encodings["y"].title == "Revenue" and line.source_type == "CHART:line"
    bar = tiles["revenue_by_region"]  # unstacked column chart with a series column: grouped bars
    assert bar.mark == "bar" and (bar.encodings["x"].field, bar.encodings["x"].type) == ("region", "nominal")
    assert bar.encodings["color"].field == "mode" and bar.encodings["xOffset"].field == "mode" and "o.status = :status" in bar.sql
    pie = tiles["orders_by_paper_type_order_share"]
    assert pie.title == "Orders by paper type: Order share" and pie.mark == "arc"
    assert (pie.encodings["theta"].field, pie.encodings["color"].field) == ("orders", "paper_type")
    table = tiles["orders_by_paper_type_table"]
    assert table.kind == "table" and table.title == "Orders by paper type: Table"
    assert all(not t.todo for t in d.tiles)


def test_chart_variants(tmp_path: Path):
    def change(doc):
        bar = widget(doc, 6)["visualization"]["options"]
        bar.update(swappedAxes=True, series={"stacking": "stack", "percentValues": True})
        line = widget(doc, 5)["visualization"]["options"]
        line["columnMapping"] = {"month": "x", "revenue": "y", "orders": "y"}
        line["seriesOptions"] = {"revenue": {"type": "column", "yAxis": 0}, "orders": {"type": "line", "yAxis": 1, "name": "Orders"}}
        line["xAxis"]["type"] = "-"
        pie = widget(doc, 8)["visualization"]["options"]
        pie.update(globalSeriesType="heatmap")
    tiles = tiles_after(tmp_path, change)
    bar = tiles["revenue_by_region"]
    assert (bar.encodings["y"].field, bar.encodings["x"].field) == ("region", "revenue") and "xOffset" not in bar.encodings
    assert "100% stacked" in bar.todo
    combo = tiles["monthly_revenue"]
    assert combo.encodings["x"].type == "temporal"  # auto axis: the cached result says datetime
    assert combo.compose == "dual_axis" and [(l.mark, l.encodings["y"].field, l.encodings["y"].title) for l in combo.layers] == [
        ("bar", "revenue", ""), ("line", "orders", "Orders")]
    other = tiles["orders_by_paper_type_order_share"]
    assert other.kind == "unknown" and "heatmap chart has no Altair mapping" in other.todo


def test_more_chart_shapes(tmp_path: Path):
    def change(doc):
        bar = widget(doc, 6)["visualization"]["options"]
        bar.update(globalSeriesType="area", columnMapping={"region": "x", "revenue": "y", "mode": "series"})
        pie = widget(doc, 8)["visualization"]["options"]
        pie["columnMapping"] = {"paper_type": "x", "orders": "y", "units": "y"}
        line = widget(doc, 5)["visualization"]["options"]
        line.update(globalSeriesType="bubble", columnMapping={"month": "x", "revenue": "y", "orders": "size", "units": "yError"})
    tiles = tiles_after(tmp_path, change)
    assert "Altair stacks them: add stack=None" in tiles["revenue_by_region"].todo
    assert "one pie per y column (orders, units)" in tiles["orders_by_paper_type_order_share"].todo
    bubble = tiles["monthly_revenue"]
    assert bubble.mark == "point" and bubble.encodings["size"].field == "orders" and "error bars from ['units']" in bubble.todo


def test_counter_table_and_other_visualizations(tmp_path: Path):
    def change(doc):
        widget(doc, 7)["visualization"]["options"].update(rowNumber=2, targetColName="orders")
        widget(doc, 4)["visualization"]["options"]["columns"][2]["visible"] = False
        widget(doc, 8)["visualization"].update(type="SANKEY")
        widget(doc, 6)["visualization"].update(type="PIVOT")
    tiles = tiles_after(tmp_path, change)
    assert "row 2 of the result" in tiles["revenue_in_period"].todo and "target column orders" in tiles["revenue_in_period"].todo
    assert "hides columns ['units']" in tiles["orders_by_paper_type_table"].todo
    assert tiles["orders_by_paper_type_order_share"].kind == "unknown" and "SANKEY visualization" in tiles["orders_by_paper_type_order_share"].todo
    assert tiles["revenue_by_region"].kind == "table" and "pivot" in tiles["revenue_by_region"].todo

    tiles = tiles_after(tmp_path / "again", lambda doc: widget(doc, 7)["visualization"]["options"].update(countRow=True))
    assert "counts the result's rows" in tiles["revenue_in_period"].todo and not tiles["revenue_in_period"].encodings


def test_widget_level_static_and_dynamic_parameters(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def change(doc):
        widget(doc, 6)["options"]["parameterMappings"]["status"].update(type="widget-level")
        widget(doc, 4)["options"]["parameterMappings"]["status"].update(type="static-value", value="RETURNED")
        widget(doc, 7)["options"]["parameterMappings"] = {}  # no mapping: widget-level, as Redash's UI treats it
        doc["options"]["parameters"] = [{"name": "status", "value": "CANCELLED"}]  # a value saved on the dashboard wins
    edit(repo, "dashboards/1.json", change)
    edit(repo, "queries/3.json", lambda q: q["options"]["parameters"][0].update(value="d_last_month"))
    b = RedashSource().parse(RawStore(repo, "redash"))
    d = b.dashboards[0]
    tiles = {t.id: t for t in d.tiles}
    assert "o.status = :revenue_by_region_status" in tiles["revenue_by_region"].sql
    assert "o.status = 'RETURNED'" in tiles["orders_by_paper_type_table"].sql
    assert ":revenue_in_period_purchased_start" in tiles["revenue_in_period"].sql
    assert "relative date 'd_last_month'" in tiles["revenue_in_period"].todo
    filters = {f.name: f.default for f in d.filters}
    assert filters == {"status": "CANCELLED", "revenue_in_period_purchased_start": None, "revenue_in_period_purchased_end": None,
                       "revenue_by_region_status": "COMPLETED"}


def test_non_sql_sources_and_filter_columns(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "data_sources.json", lambda ds: ds.append({"id": 2, "name": "Results", "type": "results", "syntax": "sql"}))
    edit(repo, "queries/1.json", lambda q: q.update(data_source_id=2))
    edit(repo, "dashboards/1.json", lambda d: widget(d, 5)["visualization"]["query"].update(data_source_id=2))
    edit(repo, "queries/2.json", lambda q: q.update(query=q["query"].replace("s.region,", 's.region AS "region::filter",')))
    b = RedashSource().parse(RawStore(repo, "redash"))
    queries = {q.name: q for q in b.queries}
    assert "Query Results data source" in queries["Monthly revenue"].todo
    tiles = {t.id: t for t in b.dashboards[0].tiles}
    assert "Query Results data source" in tiles["monthly_revenue"].todo
    assert "['region::filter'] are Redash dashboard filters" in tiles["revenue_by_region"].todo
    edit(repo, "data_sources.json", lambda ds: ds[1].update(type="mongodb", syntax="json"))
    edit(repo, "queries/4.json", lambda q: q.update(data_source_id=3))
    edit(repo, "data_sources.json", lambda ds: ds.append({"id": 3, "name": "Replica", "type": "pg", "syntax": "sql"}))
    b = RedashSource().parse(RawStore(repo, "redash"))
    assert "mongodb data source (Results), not SQL" in next(q for q in b.queries if q.name == "Monthly revenue").todo
    assert any("2 Redash data sources (Dunder Mifflin, Replica)" in w.message for w in b.warnings)


def test_archived_queries_and_dashboards_are_skipped(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "queries/1.json", lambda q: q.update(is_archived=True))
    edit(repo, "dashboards/1.json", lambda d: d.update(is_archived=True))
    b = RedashSource().parse(RawStore(repo, "redash"))
    assert "Monthly revenue" not in {q.name for q in b.queries} and not b.dashboards


def test_parse_permissions(tmp_path: Path):
    _, b = parse(tmp_path)
    perms = {(p.principal, p.action, p.resource_type, p.resource_id, p.todo) for p in b.permissions}
    assert perms == {("*", "query", "Datasource", "warehouse", ""), ("group:Sales", "view", "Datasource", "warehouse", ""),
                     ("*", "view", "Notebook", "sales-overview", ""), ("group:Sales", "view", "Notebook", "sales-overview", ""),
                     ("user:dwight@example.com", "edit", "Notebook", "sales-overview", "")}
    assert not any("admin" in p.principal for p in b.permissions)


def test_partial_data_source_access_and_query_acl(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "data_sources.json", lambda ds: ds.append({"id": 3, "name": "Replica", "type": "pg", "syntax": "sql"}))
    edit(repo, "dashboards/1.json", lambda d: widget(d, 5)["visualization"]["query"].update(data_source_id=3))
    edit(repo, "security/acl.json", lambda a: a["queries"].update({"2": {"modify": [{"id": 2, "email": "dwight@example.com"}]}}))
    b = RedashSource().parse(RawStore(repo, "redash"))
    sales = next(p for p in b.permissions if p.principal == "group:Sales" and p.resource_type == "Notebook")
    assert sales.todo == "Redash hides this group the widgets on data sources ['Replica']"
    assert any(w.item == "query Revenue by region" and "dwight@example.com" in w.message for w in b.warnings)

def test_multi_value_parameter(tmp_path: Path):
    import duckdb
    from sqlalchemy import bindparam, create_engine, text

    repo = repo_with_fixtures(tmp_path)

    def multi(q):
        q["query"] = q["query"].replace("o.status = '{{ status }}'", "o.status IN ({{ status }})")
        q["options"]["parameters"][0].update(type="enum", enumOptions="COMPLETED\nCANCELLED\nRETURNED", value=["COMPLETED", "CANCELLED"],
                                             multiValuesOptions={"prefix": "'", "suffix": "'", "separator": ","})
    edit(repo, "queries/2.json", multi)
    edit(repo, "dashboards/1.json", lambda doc: widget(doc, 6)["options"]["parameterMappings"]["status"].update(type="widget-level"))
    b = RedashSource().parse(RawStore(repo, "redash"))
    q = next(q for q in b.queries if q.source_ref == "redash:query/2")
    assert "o.status IN :status" in q.sql and not q.todo
    assert ":status (enum, a list of values, default ['COMPLETED', 'CANCELLED'])" in q.description
    d = b.dashboards[0]
    tile = next(t for t in d.tiles if t.id == "revenue_by_region")
    assert "o.status IN :revenue_by_region_status" in tile.sql
    assert "bindparam('revenue_by_region_status', expanding=True)" in tile.todo
    assert {f.name: f.default for f in d.filters}["revenue_by_region_status"] == ["COMPLETED", "CANCELLED"]
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table orders as select * from (values (1, 'COMPLETED', 10.0), (2, 'CANCELLED', 20.0), (3, 'RETURNED', 40.0))"
                " t(id, status, post_discount_revenue)")
    con.execute("create table shippings as select * from (values (1, 'West', 'A'), (2, 'West', 'A'), (3, 'West', 'A')) t(order_id, region, mode)")
    con.close()
    engine = create_engine(f"duckdb:///{db}")
    with engine.connect() as conn:
        sql = text(tile.sql).bindparams(bindparam("revenue_by_region_status", expanding=True))
        rows = conn.execute(sql, {"revenue_by_region_status": ["COMPLETED", "CANCELLED"]}).all()
    engine.dispose()
    assert [tuple(r) for r in rows] == [("West", "A", 30.0)]


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    build.apply(b, repo)
    nb = notebook_source(repo / "notebooks/sales-overview.ipynb")
    assert "status = 'COMPLETED'" in nb and "purchased_start = '2025-01-01'" in nb and "TODO(unshackle)" not in nb
    assert "params={'purchased_start': purchased_start, 'purchased_end': purchased_end}" in nb
    sql = (repo / "queries/revenue-by-region.sql").read_text()
    assert "-- source: redash:query/2" in sql and "o.status = :status" in sql
    cedar = (repo / "policies/redash.cedar").read_text()
    assert 'principal in Group::"Sales",\n  action == Action::"view",\n  resource in Datasource::"warehouse"' in cedar
    assert 'principal == User::"dwight@example.com"' in cedar
    assert "status = 'COMPLETED'" in (repo / "knowledge/redash-query-snippets.md").read_text()


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import notebook_queries, query, run_notebook

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "dunder.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table orders as select * from (values (1, 1, 'COMPLETED', 10, 95.0, timestamp '2025-01-03 10:00'),"
                " (2, 2, 'CANCELLED', 20, 200.0, timestamp '2025-02-04 11:00'), (3, 1, 'COMPLETED', 5, 50.0, timestamp '2025-08-01 09:00'))"
                " t(id, product_id, status, quantity, post_discount_revenue, purchased_at)")
    con.execute("create table shippings as select * from (values (1, 1, 'West', 'EXPRESS'), (2, 2, 'South', 'REGULAR'),"
                " (3, 3, 'West', 'REGULAR')) t(id, order_id, region, mode)")
    con.execute("create table products as select * from (values (1, 'Copy'), (2, 'Card stock')) t(id, paper_type)")
    con.close()
    url = f"duckdb:///{db}"
    html = run_notebook(repo / "notebooks/sales-overview.ipynb", url=url).read_text()
    assert "vegaEmbed" in html and "Revenue by region" in html
    params, sqls = notebook_queries(repo / "notebooks/sales-overview.ipynb")
    assert query(sqls["revenue_in_period"], params, url)["revenue"].tolist() == [295.0]
    assert query(sqls["revenue_in_period"], {**params, "purchased_end": "2025-12-31"}, url)["revenue"].tolist() == [345.0]
    region = query(sqls["revenue_by_region"], {**params, "status": "CANCELLED"}, url)
    assert region[["region", "mode", "revenue"]].values.tolist() == [["South", "REGULAR", 200.0]]


# ------------------------------------------------------------ extract (mock transport)


def _mock(calls: list[httpx.Request]):
    def j(rel: str):
        return json.loads((FIXTURES / rel).read_text())

    queries = {int(p.stem): json.loads(p.read_text()) for p in (FIXTURES / "queries").glob("*.json")}
    groups = j("security/groups.json")

    def secret(q):
        return {**q, "api_key": "query-secret", "user": {"id": 1, "email": "admin@example.com", "api_key": "user-secret"}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/events":
            return httpx.Response(200, json={"count": 3, "page": 1, "page_size": 250, "results": [
                {"action": "view", "object_type": "dashboard", "object_id": "1", "created_at": "2026-10-21T10:00:00Z", "user_name": "A"},
                {"action": "execute_query", "object_type": "data_source", "object_id": "1", "created_at": "2026-10-21T09:00:00Z"},
                {"action": "view", "object_type": "query", "object_id": "2", "created_at": "2026-10-20T10:00:00Z"},
                {"action": "view", "object_type": "query", "object_id": "2", "created_at": "2025-01-01T10:00:00Z"}]})
        if path == "/api/session":
            return httpx.Response(200, json={"user": {}, "client_config": {"version": "26.9.0"}})
        if path == "/api/data_sources":
            return httpx.Response(200, json=j("data_sources.json"))
        if path == "/api/dashboards":
            return httpx.Response(200, json={"count": 1, "page": 1, "page_size": 250, "results": [{"id": 1, "name": "Sales overview"}]})
        if path == "/api/queries":
            rows = [secret(q) for q in queries.values() if q["id"] != 4]  # the paper type query only comes with the dashboard
            return httpx.Response(200, json={"count": len(rows), "page": 1, "page_size": 250, "results": rows})
        if path == "/api/dashboards/1":
            d = j("dashboards/1.json")
            for w in d["widgets"]:
                if w.get("visualization"):
                    w["visualization"]["query"] = secret(w["visualization"]["query"])
            return httpx.Response(200, json=d)
        if path.startswith("/api/queries/") and path.count("/") == 3 and int(path.rsplit("/", 1)[1]) in queries:
            return httpx.Response(200, json=secret(queries[int(path.rsplit("/", 1)[1])]))
        if path.startswith("/api/query_results/"):
            qid = next(q["id"] for q in queries.values() if q["latest_query_data_id"] == int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"query_result": {"data": {**j(f"results/{qid}.json"), "rows": [{"x": 1}]},
                                                              "retrieved_at": j(f"results/{qid}.json")["retrieved_at"]}})
        if path == "/api/query_snippets":
            return httpx.Response(200, json=j("snippets.json"))
        if path == "/api/groups":
            return httpx.Response(200, json=[{k: v for k, v in g.items() if k not in ("members", "data_sources")} for g in groups])
        if path.startswith("/api/groups/"):
            g = next(g for g in groups if g["id"] == int(path.split("/")[3]))
            members = [{**m, "api_key": "x", "profile_image_url": "y"} for m in g["members"]]
            return httpx.Response(200, json=members if path.endswith("/members") else g["data_sources"])
        if path.endswith("/acl"):
            return httpx.Response(200, json=j("security/acl.json")["dashboards"].get("1", {}) if path == "/api/dashboards/1/acl" else {})
        return httpx.Response(404, json={"message": "Not found"})

    return handler


def _client(calls: list[httpx.Request]) -> Client:
    return Client("https://redash.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock(calls)))


def test_extract_with_mock_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[httpx.Request] = []
    monkeypatch.setenv("REDASH_URL", "https://redash.example.com")
    monkeypatch.setenv("REDASH_API_KEY", "admin-key")
    real = Client.__init__
    monkeypatch.setattr(Client, "__init__", lambda self, url, **kw: real(self, url, **{**kw, "per_minute": 0, "max_retries": 0,
                                                                                         "transport": httpx.MockTransport(_mock(calls))}))
    store = RawStore(tmp_path, "redash")
    RedashSource().extract(store)
    store.save()
    assert calls[0].url.path == "/api/events"  # before extract's own reads add view events
    assert all(c.headers["authorization"] == "Key admin-key" for c in calls)
    assert store.get_json("instance.json") == {"url": "https://redash.example.com", "version": "26.9.0"}
    assert sorted(Path(r).name for r in store.glob("queries/*.json")) == ["1.json", "2.json", "3.json", "4.json"]
    raw = "".join(store.get_text(r) for r in store.glob("**/*.json"))
    assert "secret" not in raw and "api_key" not in raw and "profile_image_url" not in raw
    assert store.get_json("results/2.json")["columns"][2] == {"friendly_name": "revenue", "name": "revenue", "type": "float"}
    assert "rows" not in store.get_text("results/2.json")
    assert store.get_json("usage.json") == {"dashboards": {"1": {"events": 1, "last": "2026-10-21"}},
                                            "queries": {"2": {"events": 1, "last": "2026-10-20"}}}
    groups = store.get_json("security/groups.json")
    assert groups[2]["name"] == "Sales" and groups[2]["data_sources"] == [{"id": 1, "name": "Dunder Mifflin", "view_only": True}]
    assert groups[2]["members"] == [{"id": 2, "name": "Dwight Schrute", "email": "dwight@example.com"}]
    assert store.get_json("security/acl.json") == json.loads((FIXTURES / "security/acl.json").read_text())
    assert store.get_json("_errors.json") == []
    b = RedashSource().parse(store)  # what was extracted parses
    assert b.dashboards[0].views == 1 and len(b.queries) == 4


def test_extract_only_a_dashboard(tmp_path: Path):
    calls: list[httpx.Request] = []
    store = RawStore(tmp_path, "redash")
    RedashSource(client=_client(calls)).extract(store, only={"dashboard:1", "query:9"})
    assert {Path(r).stem for r in store.glob("queries/*.json")} == {"1", "2", "3", "4"}  # the dashboard's widgets
    errors = store.get_json("_errors.json")
    assert [e["item"] for e in errors] == ["query 9"] and "404" in errors[0]["error"]


def test_inventory_with_mock_transport():
    inv = RedashSource(client=_client([])).inventory()
    items = {i.id: i for i in inv.items}
    assert {"dashboard:1", "query:1", "query:2", "query:3", "data_source:1"} <= set(items)
    assert items["dashboard:1"].views == 1 and items["dashboard:1"].url == "https://redash.example.com/dashboards/1"
    assert items["query:2"].views == 1 and items["query:1"].views == 0 and items["query:2"].parent == "Dunder Mifflin"
    assert items["query:2"].url == "https://redash.example.com/queries/2"
