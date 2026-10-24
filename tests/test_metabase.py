from conftest import notebook_source
import json
import shutil
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.metabase import MetabaseSource, _bind_filter, _clause, _date_bounds, _native, _stages

FIXTURES = Path(__file__).parent / "fixtures" / "metabase"  # extracted from Metabase v0.64.1 (tests/live/test_metabase_live.py)
STATUS = '("public"."orders"."status" = :status OR :status IS NULL)'


def repo_with_fixtures(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES, tmp_path / "migration" / "raw" / "metabase")
    return tmp_path


def parse(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    return repo, MetabaseSource().parse(RawStore(repo, "metabase"))


def test_parse_semantic_layer(tmp_path: Path):
    _, b = parse(tmp_path)
    assert {t.name for t in b.tables} == {"accounts", "orders", "products", "reviews", "shippings"}
    orders_table = next(t for t in b.tables if t.name == "orders")
    assert orders_table.schema == "public" and {c.name for c in orders_table.columns} >= {"status", "post_discount_revenue"}
    models = {m.name: m for m in b.semantic_models}
    assert set(models) == {"orders", "accounts", "products", "order_facts"}  # metric tables, their FK targets, models
    orders = models["orders"]
    assert orders.table == "public.orders" and orders.default_time_dimension == ""  # several time columns, none declared
    assert b.warehouse == "postgres"
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [
        ("orders", "primary", "id"), ("accounts", "foreign", "account_id"), ("products", "foreign", "product_id")]
    measures = {m.name: m for m in orders.measures}
    assert (measures["total_revenue"].agg, measures["total_revenue"].expr) == ("sum", "post_discount_revenue")
    assert (measures["completed_orders"].agg, measures["completed_orders"].expr) == ("count", "id")

    metrics = {m.name: m for m in b.metrics}
    assert metrics["total_revenue"].label == "Total revenue" and metrics["total_revenue"].source_ref == "metabase:metric/48"
    assert not metrics["total_revenue"].filter and not metrics["total_revenue"].todo
    assert metrics["completed_orders"].filter == "{{ Dimension('orders__status') }} = 'COMPLETED'"
    rate = metrics["average_discount_rate"]
    assert rate.todo.startswith("Metabase metric is not a simple single-table aggregation (aggregation '/'")
    assert rate.semantic_model == "orders" and rate.source_language == "sql" and "NULLIF" in rate.source_expression
    (q,) = [q for q in b.queries if q.metric]
    assert q.name == "Average discount rate" and q.sql.startswith("SELECT") and not q.todo


def test_parse_native_questions_and_models(tmp_path: Path):
    _, b = parse(tmp_path)
    qs = {q.name: q for q in b.queries}
    assert set(qs) == {"Orders by status for a region", "Revenue by paper type", "Completed revenue by shipping mode",
                       "Orders by region from the model", "Order facts", "Average discount rate"}
    region = qs["Orders by status for a region"]
    assert "WHERE s.region = :region" in region.sql and "{{" not in region.sql and "since" not in region.sql
    assert "Parameters: :region (text, default 'West')" in region.description and "optional clause dropped" in region.description
    mode = qs["Completed revenue by shipping mode"]  # snippet inlined, field filter is optional like in Metabase
    assert "WHERE o.status = 'COMPLETED' AND (\"public\".\"shippings\".\"mode\" = :mode OR :mode IS NULL)" in mode.sql
    from_model = qs["Orders by region from the model"]  # {{#44-order-facts}} -> the model's SQL as a subquery
    assert "FROM (\nSELECT o.id AS order_id" in from_model.sql and ") AS f" in from_model.sql and not from_model.todo
    facts = qs["Order facts"]
    assert facts.source_ref == "metabase:model/44" and "materialize as a SQL model (queries/models/order_facts.sql" in facts.todo
    model = next(m for m in b.semantic_models if m.name == "order_facts")
    assert model.table == "order_facts" and {d.name: d.type for d in model.dimensions}["purchased_at"] == "time"
    assert any(w.item == "model Order facts" and "Entity Key" in w.message for w in b.warnings)


def test_parse_dashboard(tmp_path: Path):
    _, b = parse(tmp_path)
    (d,) = b.dashboards
    assert (d.name, d.slug, d.views) == ("Sales overview", "sales-overview", 0)
    assert d.url == "https://metabase.example.com/dashboard/2" and d.source_ref == "metabase:dashboard/2"
    assert d.layout == [
        ["tab_overview"], ["total_revenue", "monthly_revenue"], ["revenue_by_account_manager", "orders_by_status_for_a_region"],
        ["tab_details"], ["revenue_by_paper_type", "revenue_and_orders_by_month"], ["text_43"],
    ]
    filters = {f.name: f for f in d.filters}
    assert (filters["region"].default, filters["region"].kind) == ("West", "value")
    assert filters["status"].default is None and filters["status"].field == "orders.status"
    assert [(n, filters[n].default, filters[n].kind, filters[n].field) for n in ("purchased_from", "purchased_to")] == [
        ("purchased_from", "2025-01-01", "date", "orders.purchased_at"), ("purchased_to", "2025-01-31", "date", "orders.purchased_at")]
    assert (filters["quantity_min"].default, filters["quantity_max"].kind) == (None, "number") and "quantity" not in filters
    assert filters["time_grouping"].kind == "unknown"
    tiles = {t.id: t for t in d.tiles}
    assert tiles["tab_details"].text == "## Details" and tiles["text_43"].kind == "text" and "post-discount" in tiles["text_43"].text
    kpi = tiles["total_revenue"]
    assert kpi.kind == "kpi" and kpi.encodings["text"].field == "sum" and kpi.source_type == "scalar"
    line = tiles["monthly_revenue"]
    assert line.mark == "line" and (line.encodings["x"].field, line.encodings["x"].type) == ("purchased_at", "temporal")
    assert STATUS in line.sql and "unshackle__" not in line.sql
    assert line.todo == ("dashboard filter time_grouping (time grouping) is fixed at the card's own unit in the SQL; "
                         "Metabase lets viewers change the unit")
    row = tiles["revenue_by_account_manager"]  # row chart: bars with the axes swapped; field is the SQL alias
    assert row.mark == "bar" and row.encodings["y"].field == "accounts__via__account_id__account_manager"
    assert row.encodings["x"].field == "sum" and f"AND ({STATUS})" in row.sql  # Metabase wraps the filter in parens
    col = '"public"."orders"."purchased_at"'  # a timestamp: Metabase compiles [from, to + 1 day)
    assert (f"(({col} >= :purchased_from OR :purchased_from IS NULL) AND "
            f"(CAST({col} AS date) <= :purchased_to OR :purchased_to IS NULL))") in row.sql
    assert ('(("public"."orders"."quantity" >= :quantity_min OR :quantity_min IS NULL) AND '
            '("public"."orders"."quantity" <= :quantity_max OR :quantity_max IS NULL))') in row.sql
    assert "1903" not in row.sql and "901237" not in row.sql and not row.todo
    native = tiles["orders_by_status_for_a_region"]
    assert "s.region = :region" in native.sql and "since" not in native.sql and not native.todo
    assert (native.encodings["x"].field, native.encodings["y"].field) == ("status", "orders")
    pie = tiles["revenue_by_paper_type"]
    assert pie.mark == "arc" and pie.encodings["theta"].field == "revenue" and pie.encodings["color"].field == "paper_type"
    combo = tiles["revenue_and_orders_by_month"]
    assert combo.compose == "dual_axis" and [(l.mark, l.encodings["y"].field) for l in combo.layers] == [("bar", "sum"), ("line", "count")]


def test_parse_permissions_collections_and_paid_gaps(tmp_path: Path):
    _, b = parse(tmp_path)
    perms = {(p.principal, p.action, p.resource_type, p.resource_id): p for p in b.permissions}
    assert ("*", "query", "Datasource", "warehouse") in perms  # All Users: unrestricted + native SQL
    analysts = perms[("group:Analysts", "query", "Datasource", "warehouse")]
    assert "query builder only" in analysts.todo
    assert ("group:Analysts", "view", "Notebook", "sales-overview") in perms
    assert ("*", "edit", "Notebook", "sales-overview") in perms
    assert not any("Administrators" in p.principal for p in b.permissions)
    (k,) = b.knowledge
    assert "## Sales" in k.body and "### Finance" in k.body and "- metric: Completed orders" in k.body
    assert "dashboard: Sales overview -> notebooks/sales-overview.ipynb" in k.body
    paid = next(w for w in b.warnings if w.item == "row and column security")
    assert paid.severity == "info" and "paid Metabase feature" in paid.message and not b.row_filters


def test_table_level_permissions_and_blocked(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    path = repo / "migration/raw/metabase/permissions/graph.json"
    graph = json.loads(path.read_text())
    graph["groups"]["5"]["2"] = {"view-data": "unrestricted", "create-queries": {"public": {"10": "query-builder", "11": "no"}}}
    graph["groups"]["1"]["2"] = {"view-data": "blocked", "create-queries": "no"}
    path.write_text(json.dumps(graph))
    b = MetabaseSource().parse(RawStore(repo, "metabase"))
    analysts = [p for p in b.permissions if p.principal == "group:Analysts" and p.action == "query"]
    assert [(p.resource_type, p.resource_id) for p in analysts] == [("Table", "public.orders")]
    assert any(p.principal == "*" and p.effect == "forbid" and p.resource_type == "Datasource" for p in b.permissions)


def test_mbql4_and_mbql5_read_the_same(tmp_path: Path):
    """Metabase before v0.57 (and ?legacy-mbql=true) returns MBQL 4; these are the v0.64.1 legacy forms of the fixture cards."""
    repo = repo_with_fixtures(tmp_path)
    cards = repo / "migration/raw/metabase/cards"
    legacy = {
        41: {"database": 2, "type": "native", "native": {"template-tags": {
            "region": {"default": "West", "id": "6b8b4567-0000-4000-8000-000000000001", "name": "region", "display-name": "Region", "type": "text", "required": True},
            "since": {"display-name": "Since", "id": "6b8b4567-0000-4000-8000-000000000002", "name": "since", "type": "date"}},
            "query": "SELECT o.status, COUNT(*) AS orders\nFROM orders o JOIN shippings s ON s.order_id = o.id\nWHERE s.region = {{region}}\n"
                     "[[AND o.purchased_at >= {{since}}]]\nGROUP BY o.status\nORDER BY o.status"}},
        49: {"database": 2, "type": "query", "query": {"aggregation": [["count"]], "source-table": 10,
                                                        "filter": ["=", ["field", 82, {"base-type": "type/Text"}], "COMPLETED"]}},
        48: {"database": 2, "type": "query", "query": {"source-table": 10, "aggregation": [
            ["aggregation-options", ["sum", ["field", 86, None]], {"name": "revenue"}]]}},
    }
    modern = MetabaseSource().parse(RawStore(repo, "metabase"))
    for cid, dq in legacy.items():
        card = json.loads((cards / f"{cid}.json").read_text())
        assert _stages(card["dataset_query"])[0]["lib/type"]  # stored as MBQL 5
        card["dataset_query"] = dq
        (cards / f"{cid}.json").write_text(json.dumps(card))
    old = MetabaseSource().parse(RawStore(repo, "metabase"))
    pick = lambda b: ({m.name: (m.filter, m.measure, m.todo) for m in b.metrics}, {q.name: q.sql for q in b.queries})  # noqa: E731
    assert pick(old) == pick(modern)
    assert _clause(["field", 82, {"base-type": "type/Text"}]) == ("field", [82], {"base-type": "type/Text"})
    assert _clause(["field", {"lib/uuid": "u", "base-type": "type/Text"}, 82]) == ("field", [82], {"lib/uuid": "u", "base-type": "type/Text"})
    assert _native(legacy[41])[1]["region"]["default"] == "West"


def test_metric_filters_follow_metabase_semantics(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    path = repo / "migration/raw/metabase/cards/49.json"
    card = json.loads(path.read_text())
    field = ["field", 82, None]
    card["dataset_query"] = {"database": 2, "type": "query", "query": {"source-table": 10, "aggregation": [["count"]], "filter": [
        "and", ["!=", field, "CANCELLED", "RETURNED"], [">=", ["field", 83, None], 100]]}}
    path.write_text(json.dumps(card))
    b = MetabaseSource().parse(RawStore(repo, "metabase"))
    m = next(m for m in b.metrics if m.name == "completed_orders")
    # Metabase's != keeps NULLs; the compiled SQL is `status NOT IN (...) OR status IS NULL`
    assert m.filter == ("({{ Dimension('orders__status') }} NOT IN ('CANCELLED', 'RETURNED') OR {{ Dimension('orders__status') }} IS NULL)"
                        " AND {{ Dimension('orders__quantity') }} >= 100")
    card["dataset_query"]["query"]["filter"] = ["contains", field, "COMP"]
    path.write_text(json.dumps(card))
    b = MetabaseSource().parse(RawStore(repo, "metabase"))
    m = next(m for m in b.metrics if m.name == "completed_orders")
    assert "filter is not a simple comparison" in m.todo and m.source_expression.startswith("SELECT")


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    build.apply(b, repo)
    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    metrics = {m["name"]: m["expression"]["dialects"][0]["expression"] for m in ossie["datasets"][0]["metrics"]}
    assert metrics == {"total_revenue": "SUM(post_discount_revenue)", "completed_orders": "COUNT(CASE WHEN status = 'COMPLETED' THEN id END)"}
    assert "TODO(unshackle): metric average_discount_rate" in (repo / "semantic/orders.ossie.yaml").read_text()
    nb = notebook_source(repo / "notebooks/sales-overview.ipynb")
    assert "region = 'West'" in nb and "params={'status': status}" in nb and "resolve_scale(y='independent')" in nb
    assert "purchased_from = '2025-01-01'" in nb and "'quantity_max': quantity_max" in nb
    assert nb.count("TODO(unshackle)") == 1 and "TODO(unshackle): dashboard filter time_grouping (time grouping)" in nb
    assert ":region" in (repo / "queries/orders-by-status-for-a-region.sql").read_text()
    assert "TODO(unshackle)" in (repo / "queries/order-facts.sql").read_text()
    cedar = (repo / "policies/metabase.cedar").read_text()
    assert 'principal in Group::"Analysts"' in cedar and 'resource in Notebook::"sales-overview"' in cedar
    assert (repo / "knowledge/metabase-collections.md").exists()
    report = (repo / "migration/report.md").read_text()
    assert "paid Metabase feature" in report and "average_discount_rate" in report


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb

    from unshackle.run import notebook_queries, query, run_notebook

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "dunder.duckdb"
    con = duckdb.connect(str(db))
    con.execute(  # Metabase compiles "public"."orders"; native SQL says orders: tables in main, views in public
        "create table orders as select * from (values (1, 1, 1, 'COMPLETED', 10, 100.0, 5.0, 95.0, timestamp '2025-01-03 10:00'),"
        " (2, 2, 1, 'CANCELLED', 20, 200.0, 0.0, 200.0, timestamp '2025-02-04 11:00')) "
        "t(id, account_id, product_id, status, quantity, pre_discount_revenue, discount_percentage, post_discount_revenue, purchased_at)"
    )
    con.execute("create table accounts as select *, 'x' as name, 'x' as industry, 1 as business_size, 'x' as payment_cycle,"
                " timestamp '2024-01-01' as created_at from (values (1, 'Dwight Schrute'), (2, 'Jim Halpert')) t(id, account_manager)")
    con.execute("create table products as select * from (values (1, 'Copy')) t(id, paper_type)")
    con.execute("create table shippings as select * from (values (1, 1, 'West', 'EXPRESS'), (2, 2, 'South', 'REGULAR')) t(id, order_id, region, mode)")
    con.execute("create schema public")
    for t in ("orders", "accounts", "products", "shippings"):
        con.execute(f"create view public.{t} as select * from main.{t}")
    con.close()
    url = f"duckdb:///{db}"
    html = run_notebook(repo / "notebooks/sales-overview.ipynb", url=url).read_text()
    assert "vegaEmbed" in html and "Revenue by account manager" in html
    params, sqls = notebook_queries(repo / "notebooks/sales-overview.ipynb")
    assert query(sqls["monthly_revenue"], {**params, "status": "COMPLETED"}, url)["sum"].tolist() == [95.0]
    assert query(sqls["monthly_revenue"], params, url)["sum"].tolist() == [95.0, 200.0]  # empty filter = no filter
    managers = lambda **kw: query(sqls["revenue_by_account_manager"], {**params, **kw}, url)["count"].tolist()  # noqa: E731
    assert managers() == [1]  # the default range, January 2025, keeps the order from 2025-01-03 10:00
    assert managers(purchased_from="2025-01-03", purchased_to="2025-01-03") == [1]  # a single day keeps its afternoon
    assert managers(purchased_from="2025-01-04", purchased_to=None) == [] and managers(quantity_min=11) == []
    assert managers(purchased_from=None, purchased_to=None, quantity_min=5, quantity_max=10) == [1]


def _mock(calls: list[httpx.Request]):
    def j(rel: str):
        return json.loads((FIXTURES / rel).read_text())

    search = [{"model": "dashboard", "id": 2, "name": "Sales overview", "collection": {"id": 5}},
              {"model": "card", "id": 42, "name": "Monthly revenue", "collection": {"id": 5}},
              {"model": "card", "id": 99, "name": "Elsewhere", "collection": {"id": 8}},
              {"model": "dataset", "id": 44, "name": "Order facts", "collection": {"id": 5}}]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/session/properties":
            return httpx.Response(200, json={"version": {"tag": "v0.64.1"}, "setup-token": None})
        if path == "/api/search":
            return httpx.Response(200, json={"data": search, "total": len(search)})
        if path == "/api/collection/tree":
            return httpx.Response(200, json=j("collections.json"))
        if path.startswith("/api/dashboard/"):
            return httpx.Response(200, json=j("dashboards/2.json"))
        if path.startswith("/api/card/"):
            cid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={**j(f"cards/{cid}.json"), "creator": {"email": "a@example.com"},
                                             "result_metadata": [{"name": "x", "fingerprint": {"global": {}}}]})
        if path == "/api/dataset/native":
            body = json.loads(request.content)
            value = body.get("parameters", [{}])[0].get("value", [""])[0] if body.get("parameters") else ""
            return httpx.Response(200, json={"query": f"SELECT 1 WHERE x = '{value}'" if value else "SELECT 1", "params": None})
        if path.startswith("/api/database/"):
            return httpx.Response(200, json={**j("databases/2.json"), "details": {"host": "db.internal", "password": "**MetabasePass**"}})
        if path == "/api/native-query-snippet":
            return httpx.Response(200, json=j("snippets.json"))
        if path == "/api/permissions/group":
            return httpx.Response(200, json=j("permissions/groups.json"))
        if path == "/api/permissions/graph":
            return httpx.Response(200, json=j("permissions/graph.json"))
        if path == "/api/collection/graph":
            return httpx.Response(200, json=j("permissions/collections.json"))
        return httpx.Response(404, json="API endpoint does not exist.")

    return handler


def test_extract_with_mock_transport(tmp_path: Path):
    calls: list[httpx.Request] = []
    client = Client("https://mb.example.com", headers={"X-API-Key": "secret"}, per_minute=0, max_retries=0,
                    transport=httpx.MockTransport(_mock(calls)))
    store = RawStore(tmp_path, "metabase")
    MetabaseSource(client=client).extract(store, only={"collection:5"})
    store.save()
    assert calls[0].headers["x-api-key"] == "secret"
    assert store.get_json("instance.json") == {"url": "https://mb.example.com", "version": "v0.64.1"}
    assert not store.exists("cards/99.json")  # other collection
    assert store.exists("cards/48.json") and store.exists("cards/41.json")  # pulled in by the dashboard
    assert store.exists("cards/42.sql") and not store.exists("cards/41.sql")  # only query-builder cards are compiled
    assert store.get_text("dashboards/2/38.sql") == "SELECT 1 WHERE x = 'unshackle__status'\n"
    compiled = [json.loads(c.content).get("parameters") for c in calls if c.url.path == "/api/dataset/native"]
    assert [(p["type"], p["value"]) for p in max(compiled, key=lambda ps: len(ps or []))] == [
        ("string/=", ["unshackle__status"]), ("date/all-options", "1903-02-03~1903-04-05"), ("number/between", [-901237.5, 901237.5])]
    assert ["string/="] in [[p["type"] for p in ps] for ps in compiled if ps]  # time grouping without a default isn't sent
    card = store.get_json("cards/42.json")
    assert "creator" not in card and "fingerprint" not in card["result_metadata"][0]
    assert "details" not in store.get_json("databases/2.json")
    assert "404" in store.get_json("paid_features.json")["row_and_column_security"]
    assert store.get_json("_errors.json") == []
    compile_bodies = [json.loads(c.content) for c in calls if c.url.path == "/api/dataset/native"]
    assert all(b["pretty"] is True for b in compile_bodies)


def test_inventory_with_mock_transport():
    client = Client("https://mb.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock([])))
    inv = MetabaseSource(client=client).inventory()
    kinds = {(i.kind, i.id) for i in inv.items}
    assert {("dashboard", "dashboard:2"), ("question", "card:42"), ("model", "card:44"), ("collection", "collection:5")} <= kinds
    assert next(i for i in inv.items if i.id == "dashboard:2").url == "https://mb.example.com/dashboard/2"


# Predicates as Metabase v0.64.1 compiles them on Postgres (POST /api/dataset/native with the extract sentinels).
COMPILED = {
    "date column": ("date/range", 0, '"public"."orders"."purchased_at" BETWEEN date \'1901-02-03\'\n  AND date \'1901-04-05\'',
                    '(("public"."orders"."purchased_at" >= :p_from OR :p_from IS NULL) AND '
                    '("public"."orders"."purchased_at" <= :p_to OR :p_to IS NULL))'),
    "timestamp, no parens": ("date/single", 1, "t.ts >= timestamp \'1902-02-03 00:00:00.000\' AND t.ts < timestamp \'1902-04-06 00:00:00.000\'",
                             "((t.ts >= :p_from OR :p_from IS NULL) AND (CAST(t.ts AS date) <= :p_to OR :p_to IS NULL))"),
    "number =": ("number/=", 0, '("public"."orders"."quantity" = -901234.5)',
                 '(("public"."orders"."quantity" = :p OR :p IS NULL))'),
    "number !=": ("number/!=", 2, '(("o"."q" <> -901236.5)\n    OR ("o"."q" IS NULL))',
                  '(("o"."q" <> :p OR "o"."q" IS NULL OR :p IS NULL))'),
    "number >=": ("number/>=", 0, '("o"."q" >= -901234.5)', '(("o"."q" >= :p OR :p IS NULL))'),
    "string !=": ("string/!=", 0, '(("o"."s" <> \'unshackle__p\') OR ("o"."s" IS NULL))',
                  '(("o"."s" <> :p OR "o"."s" IS NULL OR :p IS NULL))'),
    "contains": ("string/contains", 0, '("o"."s" LIKE \'%unshackle\\_\\_p%\')', '((POSITION(:p IN "o"."s") > 0 OR :p IS NULL))'),
    "contains, case-insensitive": ("string/contains", 0, 'LOWER("o"."s") LIKE \'%unshackle\\_\\_p%\'',
                                   '(POSITION(LOWER(:p) IN LOWER("o"."s")) > 0 OR :p IS NULL)'),
    "does not contain": ("string/does-not-contain", 0, '(NOT ("o"."s" LIKE \'%unshackle\\_\\_p%\') OR ("o"."s" IS NULL))',
                         '((NOT (POSITION(:p IN "o"."s") > 0) OR "o"."s" IS NULL OR :p IS NULL))'),
    "starts with": ("string/starts-with", 0, '("o"."s" LIKE \'unshackle\\_\\_p%\')', '((POSITION(:p IN "o"."s") = 1 OR :p IS NULL))'),
    "ends with": ("string/ends-with", 0, '("o"."s" LIKE \'%unshackle\\_\\_p\')',
                  '((RIGHT("o"."s", LENGTH(:p)) = :p OR :p IS NULL))'),
}


def test_dashboard_filters_bind_what_metabase_compiles():
    for case, (t, i, compiled, bound) in COMPILED.items():
        assert _bind_filter(f"WHERE {compiled}", t, "p", i) == (f"WHERE {bound}", "bound"), case
    # timestamptz compiles in the report time zone; CAST(... AS date) would use the session's, so it stays a TODO
    tz = '"o"."t" >= timestamp with time zone \'1901-02-03 00:00:00.000 +00:00\' AND "o"."t" < timestamp with time zone \'1901-04-06 00:00:00.000 +00:00\''
    assert _bind_filter(tz, "date/range", "p", 0) == (tz, "unbound")
    assert _bind_filter("SELECT 1", "number/=", "p", 0)[1] == "absent"


def test_dashboard_filter_predicates_run(tmp_path: Path):
    import duckdb

    con = duckdb.connect()
    con.execute("create table o as select * from (values (1, 'COMPLETED', 5), (2, 'cancelled', 20), (3, NULL, NULL)) t(id, s, q)")
    run = lambda sql, **kw: [r[0] for r in con.execute(f"select id from o where {sql} order by id", kw).fetchall()]  # noqa: E731
    pred = lambda case: COMPILED[case][3].replace('"o".', "").replace(":p", "$p")  # noqa: E731
    assert run(pred("contains"), p=None) == [1, 2, 3] and run(pred("contains"), p="LETE") == [1]
    assert run(pred("contains, case-insensitive"), p="CANC") == [2]
    assert run(pred("string !="), p="COMPLETED") == [2, 3] and run(pred("does not contain"), p="ED") == [2, 3]
    assert run(pred("starts with"), p="COMP") == [1] and run(pred("ends with"), p="led") == [2]
    assert run(pred("number !="), p=5) == [2, 3] and run(pred("number >="), p=10) == [2]


def test_date_filter_defaults():
    assert _date_bounds("2024-01-01~2024-06-30") == ("2024-01-01", "2024-06-30")
    assert _date_bounds(["2024-02-29"]) == ("2024-02-29", "2024-02-29")
    assert _date_bounds("~2024-03-01") == (None, "2024-02-29")  # before and after exclude the day, as Metabase does
    assert _date_bounds("2024-03-01~") == ("2024-03-02", None)
    assert _date_bounds("2024-02") == ("2024-02-01", "2024-02-29") and _date_bounds("Q4-2024") == ("2024-10-01", "2024-12-31")
    assert _date_bounds("past30days") is None and _date_bounds("exclude-days-Mon") is None and _date_bounds(None) is None


def test_relative_date_default_is_a_todo(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    rel = repo / "migration/raw/metabase/dashboards/2.json"
    d = json.loads(rel.read_text())
    next(p for p in d["parameters"] if p["slug"] == "purchased")["default"] = "past30days"
    rel.write_text(json.dumps(d))
    (dash,) = MetabaseSource().parse(RawStore(repo, "metabase")).dashboards
    assert {f.name: f.default for f in dash.filters}["purchased_from"] is None
    row = next(t for t in dash.tiles if t.id == "revenue_by_account_manager")
    assert "defaults to 'past30days' in Metabase, which isn't a fixed range" in row.todo and ":purchased_from, :purchased_to" in row.todo
