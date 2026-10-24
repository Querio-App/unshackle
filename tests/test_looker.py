from conftest import notebook_source, todo_for
import json
import shutil
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.looker import LookerSource, looker_filter_sql, strip_sql_comments

FIX = Path(__file__).parent / "fixtures" / "looker"


def _store(tmp_path: Path) -> RawStore:
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "looker")
    return RawStore(tmp_path, "looker")


def _by_name(items):
    return {i.name: i for i in items}


def test_parse_lookml_semantics(tmp_path: Path):
    b = get_source("looker").parse(_store(tmp_path))
    models = _by_name(b.semantic_models)
    assert set(models) == {"orders", "customers", "customer_lifetime"}
    orders = models["orders"]
    assert orders.table == "analytics.fct_orders"
    assert orders.default_time_dimension == "created"
    ents = {e.name: e for e in orders.entities}
    assert ents["orders"].type == "primary" and ents["orders"].expr == "order_id"
    assert ents["customers"].type == "foreign" and ents["customers"].expr == "customer_id"
    dims = _by_name(orders.dimensions)
    assert dims["created"].type == "time" and dims["created"].expr == "created_at"
    assert dims["created"].time_granularity == "day"
    assert dims["amount"].expr == "amount_usd"
    assert dims["is_large_order"].expr == "amount_usd >= 500"  # ${amount} resolved
    assert dims["size_bucket"].expr.startswith("CASE WHEN amount_usd < 50 THEN 'small'")
    assert "Liquid" in dims["amount_in_currency"].todo
    meas = _by_name(orders.measures)
    assert (meas["total_revenue"].agg, meas["total_revenue"].expr) == ("sum", "amount_usd")
    assert (meas["order_count"].agg, meas["order_count"].expr) == ("count", "order_id")  # count -> primary key
    assert meas["customer_count"].agg == "count_distinct"
    # Looker's percentile on BigQuery is neither PERCENTILE_CONT nor _DISC; the warehouse is unknown here
    assert "p90_order_value" not in meas and "percentile 90 on BigQuery" in _by_name(b.metrics)["p90_order_value"].todo
    assert _by_name(models["customers"].dimensions)["signed_up"].time_granularity == "day"
    assert any(e.name == "customer_lifetime" for e in models["customers"].entities)
    assert "customers_count" in _by_name(models["customers"].measures)  # bare `count` is qualified


def test_parse_lookml_metrics(tmp_path: Path):
    b = get_source("looker").parse(_store(tmp_path))
    m = _by_name(b.metrics)
    assert m["completed_revenue"].filter == "{{ Dimension('orders__status') }} = 'complete'"
    assert m["large_orders_outside_eu"].filter == (
        "{{ Dimension('orders__is_large_order') }} = true AND ({{ Dimension('customers__region') }} <> 'EU' OR {{ Dimension('customers__region') }} IS NULL)"
    )
    aov = m["average_order_value"]
    assert (aov.type, aov.numerator, aov.denominator) == ("ratio", "total_revenue", "order_count")
    assert m["revenue_per_customer"].type == "ratio"
    net = m["net_revenue"]
    assert net.type == "derived" and net.expr == "total_revenue - refunded_revenue"
    assert net.metrics == ["total_revenue", "refunded_revenue"]
    assert "Liquid" in m["revenue_in_currency"].todo and m["revenue_in_currency"].source_language == "lookml"
    assert "30 days" in m["recent_revenue"].todo
    assert all(x.semantic_model for x in b.metrics)


def test_parse_lookml_tables_policies(tmp_path: Path):
    b = get_source("looker").parse(_store(tmp_path))
    tables = {t.name: t for t in b.tables}
    assert set(tables) == {"fct_orders", "dim_customers"}
    assert tables["fct_orders"].schema == "analytics"
    assert {"order_id", "amount_usd", "created_at"} <= {c.name for c in tables["fct_orders"].columns}
    rf = _by_name(b.row_filters)
    assert rf["orders_customers_region"].table == "analytics.dim_customers"
    assert rf["orders_customers_region"].predicate == "region = {{ user.sales_region }}"
    assert "explore(s) customers" in rf["orders_customers_region"].todo
    via = rf["orders_customers_region_via_orders"]
    assert via.table == "analytics.fct_orders"
    assert via.predicate == "customer_id IN (SELECT customer_id FROM analytics.dim_customers WHERE region = {{ user.sales_region }})"
    assert rf["customers_always_where"].predicate == "is_test_account = false"
    perms = [(p.principal, p.resource_type, p.resource_id) for p in b.permissions]
    assert ("group:finance", "Table", "analytics.fct_orders") in perms
    assert ("group:executive", "Column", "analytics.dim_customers.email") in perms
    assert all("user attribute" in p.todo for p in b.permissions)
    pdt = _by_name(b.queries)["customer_lifetime derived table"]
    assert "FROM analytics.fct_orders" in pdt.sql and "datagroup_trigger: nightly" in pdt.todo
    assert "orders" in b.knowledge[0].body and "many_to_one" in b.knowledge[0].body


def test_parse_dashboard(tmp_path: Path):
    b = get_source("looker").parse(_store(tmp_path))
    (d,) = b.dashboards
    assert d.name == "Sales Overview" and d.views == 42
    assert d.url == "https://acme.cloud.looker.com/dashboards/12"
    assert d.layout == [["106"], ["103", "101"], ["102", "107"], ["104"], ["105"]]
    assert {f.name for f in d.filters} == {"region", "order_date"}
    t = {x.id: x for x in d.tiles}
    assert t["106"].kind == "text" and "net of tax" in t["106"].text
    line = t["101"]
    assert line.mark == "line"
    assert line.encodings["x"].field == "orders_created_month" and line.encodings["x"].type == "temporal"
    assert line.encodings["y"].field == "orders_total_revenue"
    assert line.encodings["color"].field == "customers_region"  # pivot
    assert 'AS orders_total_revenue' in line.sql and '"orders.total_revenue"' not in line.sql
    assert "dashboard filter" in line.todo
    assert t["102"].mark == "bar" and "completion_rate" in t["102"].todo
    assert t["103"].kind == "kpi" and t["103"].encodings["text"].field == "orders_total_revenue"
    assert t["104"].kind == "table"
    assert t["107"].mark == "arc" and t["107"].encodings["theta"].field == "orders_order_count"
    assert t["105"].mark == "unknown" and "looker_map" in t["105"].todo
    look = _by_name(b.queries)["Weekly orders by status"]
    assert "AS orders_created_week" in look.sql and look.owner == "Dana Ops"


def test_build_outputs(tmp_path: Path):
    b = get_source("looker").parse(_store(tmp_path))
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('fct_orders')"
    ossie = yaml.safe_load((tmp_path / "models/semantic/orders.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "analytics.fct_orders"
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["average_order_value"]["type_params"] == {"numerator": "total_revenue", "denominator": "order_count"}
    assert "revenue_in_currency" not in metrics and todo_for((tmp_path / "models/semantic/orders.yml").read_text(), "metric", "revenue_in_currency")
    nb = notebook_source(tmp_path / "notebooks/sales-overview.ipynb")
    assert "mark_line" in nb and "mark_arc" in nb
    rows = yaml.safe_load((tmp_path / "policies/row_filters.looker.yml").read_text())["row_filters"]
    assert any(r["predicate"] == "region = {{ user.sales_region }}" for r in rows)
    cedar = (tmp_path / "policies/looker.cedar").read_text()
    assert 'principal in Group::"finance"' in cedar and "TODO(unshackle)" in cedar
    assert (tmp_path / "queries/customer-lifetime-derived-table.sql").exists()
    assert (tmp_path / "knowledge/looker-model-ecommerce.md").exists()
    report = (tmp_path / "migration/report.md").read_text()
    assert "Liquid" in report and "looker_map" in report


def test_filter_expressions():
    r = "{{ Dimension('v__f') }}"
    assert looker_filter_sql(r, "a,b") == f"{r} IN ('a', 'b')"
    assert looker_filter_sql(r, "-a") == f"({r} <> 'a' OR {r} IS NULL)"
    assert looker_filter_sql(r, "%foo%") == f"{r} LIKE '%foo%'"
    assert looker_filter_sql(r, "NULL") == f"{r} IS NULL"
    assert looker_filter_sql(r, ">=10", "number") == f"{r} >= 10"
    assert looker_filter_sql(r, "no", "yesno") == f"NOT COALESCE({r}, false)"
    assert looker_filter_sql(r, "7 days", "time") is None


def test_strip_sql_comments():
    assert strip_sql_comments("a -- note\nb") == "a \nb"
    assert strip_sql_comments("a -- note") == "a "
    assert strip_sql_comments("'x -- y' -- z") == "'x -- y' "  # single-quoted
    assert strip_sql_comments('"x -- y" -- z') == '"x -- y" '  # double-quoted
    assert strip_sql_comments("`x -- y` -- z") == "`x -- y` "  # backquoted
    assert strip_sql_comments("'it''s -- y' -- z") == "'it''s -- y' "  # doubled quote
    assert strip_sql_comments(r"'it\'s -- y' -- z") == r"'it\'s -- y' "  # backslash-escaped quote
    assert strip_sql_comments(r'"a\"-- y" -- z') == r'"a\"-- y" '
    assert strip_sql_comments("'open -- y") == "'open -- y"  # unterminated: left alone


def test_negated_filters_keep_nulls():
    """Matches the SQL Looker generated (BigQuery) for these filters, and its counts on the same rows."""
    import duckdb

    r = "{{ Dimension('v__f') }}"
    assert looker_filter_sql(r, "-a,-b") == f"({r} NOT IN ('a', 'b') OR {r} IS NULL)"
    assert looker_filter_sql(r, "-%foo%") == f"({r} NOT LIKE '%foo%' OR {r} IS NULL)"
    assert looker_filter_sql(r, "-a,b") == f"{r} = 'b' AND ({r} <> 'a' OR {r} IS NULL)"
    assert looker_filter_sql(r, "<>5", "number") == f"{r} <> 5"
    assert looker_filter_sql(r, "-NULL") == f"{r} IS NOT NULL"
    assert looker_filter_sql(r, "-EMPTY") == f"({r} IS NOT NULL AND {r} <> '')"
    assert looker_filter_sql(r, "--a") is None

    con = duckdb.connect()
    con.execute("create table t as select * from (values ('a', 1, true), ('b', 5, false), ('c', 7, null), (null, null, null)) v(s, n, y)")

    def count(col: str, value: str, kind: str = "string") -> int:
        return con.execute(f"select count(*) from t where {looker_filter_sql(col, value, kind)}").fetchone()[0]

    # Looker's results on the same four rows
    looker = {
        ("s", "-a", "string"): 3,
        ("s", "-a,-b", "string"): 2,
        ("s", "-%b%", "string"): 3,
        ("s", "-NULL", "string"): 3,
        ("s", "-EMPTY", "string"): 3,
        ("s", "-a,b", "string"): 1,
        ("n", "<>5", "number"): 2,
        ("n", "!=5", "number"): 2,
        ("y", "no", "yesno"): 3,
        ("y", "yes", "yesno"): 1,
    }
    assert {k: count(*k) for k in looker} == looker


def test_parse_explore_json_fallback(tmp_path: Path):
    store = RawStore(tmp_path, "looker")
    store.put_json(
        "api/explores/ecommerce/orders.json",
        {
            "name": "orders",
            "model_name": "ecommerce",
            "view_name": "orders",
            "sql_table_name": "analytics.fct_orders",
            "joins": [],
            "access_filters": [{"field": "orders.status", "user_attribute": "status_scope"}],
            "fields": {
                "dimensions": [
                    {"name": "orders.id", "view": "orders", "type": "number", "sql": "${TABLE}.order_id", "primary_key": True},
                    {"name": "orders.status", "view": "orders", "type": "string", "sql": "${TABLE}.status"},
                    {"name": "orders.created_date", "view": "orders", "type": "date_date", "sql": "${TABLE}.created_at", "dimension_group": "orders.created"},
                    {"name": "orders.created_month", "view": "orders", "type": "date_month", "sql": "${TABLE}.created_at", "dimension_group": "orders.created"},
                ],
                "measures": [{"name": "orders.total_revenue", "view": "orders", "type": "sum", "sql": "${TABLE}.amount_usd"}],
            },
        },
    )
    b = LookerSource().parse(store)
    (sm,) = b.semantic_models
    assert sm.table == "analytics.fct_orders" and sm.entities[0].expr == "order_id"
    assert _by_name(sm.dimensions)["created"].time_granularity == "day"
    assert _by_name(sm.measures)["total_revenue"].expr == "amount_usd"
    assert b.row_filters[0].predicate == "status = {{ user.status_scope }}"


def _mock_looker(calls: list[str]):
    dash = json.loads((FIX / "dashboards/12.json").read_text())
    look = json.loads((FIX / "looks/7.json").read_text())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/4.0")
        calls.append(f"{req.method} {path}")
        if path == "/login":
            assert b"client_id=cid" in req.content
            return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
        assert req.headers["authorization"] == "token tok"
        if path == "/lookml_models":
            return httpx.Response(200, json=[{"name": "ecommerce", "label": "E-commerce", "explores": [{"name": "orders", "label": "Orders"}]}])
        if path == "/dashboards":
            return httpx.Response(200, json=[{"id": "12", "title": "Sales Overview", "folder": {"name": "Sales"}}, {"id": "13", "title": "Secret"}])
        if path == "/dashboards/12":
            return httpx.Response(200, json=dash)
        if path == "/dashboards/13":
            return httpx.Response(404, json={"message": "Not found"})
        if path == "/looks":
            return httpx.Response(200, json=[{"id": "7", "title": look["title"]}])
        if path == "/looks/7":
            return httpx.Response(200, json=look)
        if path.startswith("/queries/") and path.endswith("/run/sql"):
            qid = path.split("/")[2]
            f = FIX / "queries" / f"{qid}.sql"
            return httpx.Response(200, text=f.read_text()) if f.exists() else httpx.Response(404)
        if path == "/content_view/search":
            return httpx.Response(200, json=json.loads((FIX / "content_usage.json").read_text()))
        return httpx.Response(404)

    return handler


def test_extract_and_inventory_with_mock_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOOKER_CLIENT_ID", "cid")
    monkeypatch.setenv("LOOKER_CLIENT_SECRET", "secret")
    calls: list[str] = []
    client = Client("https://acme.cloud.looker.com/api/4.0", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock_looker(calls)))
    src = LookerSource(client=client, lookml_dir=str(FIX / "lookml"), base_url="https://acme.cloud.looker.com")
    inv = src.inventory()
    dashboards = [i for i in inv.items if i.kind == "dashboard"]
    assert {d.id for d in dashboards} == {"dashboard:12", "dashboard:13"}
    assert next(d for d in dashboards if d.id == "dashboard:12").views == 42
    assert any("404" in n for n in inv.notes)

    store = RawStore(tmp_path, "looker")
    src.extract(store)
    store.save()
    assert calls.count("POST /login") == 1
    assert store.exists("lookml/orders.view.lkml") and store.exists("dashboards/12.json")
    assert store.exists("queries/5001.sql") and store.exists("looks/7.json") and store.exists("content_usage.json")
    assert "/dashboards/13" in store.get_json("_skipped.json")
    b = LookerSource().parse(RawStore(tmp_path, "looker"))
    assert b.dashboards[0].views == 42 and len(b.dashboards[0].tiles) == 7
    assert any("/dashboards/13" in w.item for w in b.warnings)

    only = RawStore(tmp_path / "only", "looker")
    src.extract(only, {"look:7"})
    assert only.exists("looks/7.json") and not only.glob("dashboards/*.json")


# ---------------------------------------------------------------- regressions from real-world LookML


def _lookml(tmp_path: Path, files: dict[str, str]):
    store = RawStore(tmp_path, "looker")
    for name, text in files.items():
        store.put_text(f"lookml/{name}", text)
    return LookerSource().parse(store)


ORDERS = """view: orders {
  sql_table_name: analytics.orders ;;
  dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
  dimension: status { sql: ${TABLE}.status ;; }
  measure: count { type: count }
}
"""


def test_manifest_constants_are_substituted(tmp_path: Path):
    b = _lookml(tmp_path, {
        "manifest.lkml": 'constant: SCHEMA { value: "@{DB}.analytics" }\nconstant: DB { value: "prod" }\n'
                         'constant: CUR { value: "{% if _user_attributes[\'c\'] %} 1 {% endif %}" }\n',
        "v.view.lkml": """view: orders {
  sql_table_name: @{SCHEMA}.orders ;;
  dimension: id { primary_key: yes }
  dimension: amount { label: "@{CUR}" sql: ${TABLE}.amount * @{CUR} ;; }
  dimension: region { sql: @{MISSING} ;; }
}""",
    })
    (sm,) = b.semantic_models
    assert sm.table == "prod.analytics.orders"
    dims = _by_name(sm.dimensions)
    assert "Liquid" in dims["amount"].todo and dims["amount"].label == ""  # a constant holding Liquid stays TODO
    assert "@{MISSING}" in dims["region"].todo and dims["region"].expr == ""


def test_sql_table_name_that_is_sql_becomes_a_query(tmp_path: Path, monkeypatch):
    b = _lookml(tmp_path, {"v.view.lkml": """view: status {
  sql_table_name: {% if _dialect._name == 'postgres' %} (SELECT 1 AS id) {% else %} perf.global_status {% endif %} ;;
  dimension: id { primary_key: yes }
}"""})
    (sm,) = b.semantic_models
    assert sm.table == "status" and not b.tables
    (q,) = b.queries
    assert "sql_table_name that is SQL" in q.todo and "Liquid" in q.todo and q.sql.startswith("{% if")
    build.apply(b, tmp_path)  # no table file named after the SQL


def test_refinement_extends_apply_before_view_extends(tmp_path: Path):
    b = _lookml(tmp_path, {
        "base.view.lkml": """view: common_ext {
  extension: required
  dimension: fiscal_year { sql: ${TABLE}.FiscalYear ;; }
  dimension: amount { hidden: yes }
}
view: ledger { sql_table_name: fin.ledger ;; dimension: amount { sql: ${TABLE}.Amt ;; } }
view: ledger_copy { extends: [ledger] }""",
        "ledger_rfn.view.lkml": "view: +ledger { extends: [common_ext] dimension: note { sql: ${TABLE}.note_text ;; } }",
    })
    models = _by_name(b.semantic_models)
    assert "common_ext" not in models
    for name in ("ledger", "ledger_copy"):
        dims = _by_name(models[name].dimensions)
        assert dims["fiscal_year"].expr == "FiscalYear" and dims["note"].expr == "note_text"
        assert dims["amount"].expr == "Amt"  # the view's own field wins over the extension's


def test_explores_in_included_files_refinements_and_extends(tmp_path: Path):
    b = _lookml(tmp_path, {
        "shop.model.lkml": 'connection: "wh"\ninclude: "explores/*.explore"\n'
                           "explore: +orders { join: users { sql_on: ${orders.user_id} = ${users.id} ;; relationship: many_to_one } }",
        "explores/orders.explore.lkml": "explore: orders_base { extension: required access_filter: { field: orders.status user_attribute: st } }\n"
                                        "explore: orders { extends: [orders_base] }",
        "orders.view.lkml": ORDERS.replace("dimension: status", "dimension: user_id { sql: ${TABLE}.user_id ;; }\n  dimension: status"),
        "users.view.lkml": "view: users { sql_table_name: analytics.users ;; dimension: id { primary_key: yes } }",
    })
    (k,) = b.knowledge
    assert k.name == "Looker model shop"
    assert "## Explore: orders\n" in k.body and "+orders" not in k.body and "orders_base" not in k.body
    assert "Joins `users`" in k.body
    assert any(e.name == "users" and e.expr == "user_id" for e in _by_name(b.semantic_models)["orders"].entities)
    assert [r.predicate for r in b.row_filters] == ["status = {{ user.st }}"]


def test_explore_file_in_several_models_gives_one_policy(tmp_path: Path):
    b = _lookml(tmp_path, {
        "a.model.lkml": 'connection: "wh"\ninclude: "/orders.explore.lkml"',
        "b.model.lkml": 'connection: "wh"\ninclude: "/*.explore"',
        "orders.explore.lkml": "explore: orders { sql_always_where: ${status} <> 'test' ;; }",
        "orders.view.lkml": ORDERS,
    })
    assert len(b.knowledge) == 2
    assert [r.predicate for r in b.row_filters] == ["status <> 'test'"]


def test_duplicate_view_prefers_the_included_file(tmp_path: Path):
    b = _lookml(tmp_path, {
        "m.model.lkml": 'connection: "wh"\ninclude: "/views/raw/*.view"\nexplore: orders {}',
        "views/raw/orders.view.lkml": ORDERS,
        "views/zz_unused/orders.view.lkml": ORDERS.replace("analytics.orders", "old.orders"),
    })
    assert b.semantic_models[0].table == "analytics.orders"
    assert any("defined in both" in w.message for w in b.warnings)


def test_unresolved_primary_key_gives_no_entity(tmp_path: Path):
    b = _lookml(tmp_path, {"v.view.lkml": """view: balances {
  sql_table_name: bank.balances ;;
  dimension: pk { primary_key: yes sql: CONCAT(${TABLE}.account_id, ${balance_month}) ;; }
  dimension_group: balance { type: time timeframes: [date, month] sql: ${TABLE}.ts ;; }
  measure: count { type: count }
}"""})
    (sm,) = b.semantic_models
    assert sm.entities == []
    assert "timeframe" in _by_name(sm.dimensions)["pk"].todo
    assert _by_name(sm.measures)["balances_count"].expr == "1"


def test_joins_without_an_equality_say_why(tmp_path: Path):
    b = _lookml(tmp_path, {
        "m.model.lkml": """connection: "wh"
explore: orders {
  join: orders__items { sql: LEFT JOIN UNNEST(${orders.items}) AS orders__items ;; relationship: one_to_many }
  join: totals { type: cross relationship: many_to_one }
}""",
        "orders.view.lkml": ORDERS,
        "totals.view.lkml": "view: totals { sql_table_name: analytics.totals ;; dimension: n {} }",
    })
    msgs = {w.item: w.message for w in b.warnings}
    assert "UNNEST" in msgs["explore orders join orders__items"]
    assert "cross join" in msgs["explore orders join totals"]


def test_numeric_dates_and_odd_measures_stay_todo(tmp_path: Path):
    b = _lookml(tmp_path, {"v.view.lkml": """view: events {
  sql_table_name: raw.events ;;
  dimension: id { primary_key: yes }
  dimension: step { sql: ${TABLE}.step ;; }
  dimension: day { type: date sql: ${TABLE}.day ;; }
  dimension: spot { type: location sql_latitude: ${TABLE}.lat ;; sql_longitude: ${TABLE}.lon ;; }
  dimension_group: created { type: time timeframes: [date] datatype: yyyymmdd sql: ${TABLE}.created ;; }
  dimension: title { label: "{% if _user_attributes['locale'] == 'de' %}Titel{% else %}Title{% endif %}" }
  measure: purchases { type: count filters: [step: "(5) Purchase,(4) Cart"] }
  measure: known { type: count filters: [step: "NOT NULL"] }
  measure: recent { type: count filters: [day: "7 days"] }
  measure: steps { type: list list_field: step }
}"""})
    (sm,) = b.semantic_models
    dims = _by_name(sm.dimensions)
    assert "yyyymmdd" in dims["created"].todo and dims["created"].expr == ""
    assert dims["title"].label == ""
    assert "location" in dims["spot"].todo and "sql_latitude" in dims["spot"].source_expression  # no `sql:`, still the original
    m = _by_name(b.metrics)
    assert m["purchases"].filter == "{{ Dimension('events__step') }} IN ('(5) Purchase', '(4) Cart')"
    assert m["known"].filter == "{{ Dimension('events__step') }} IS NOT NULL"
    assert "7 days" in m["recent"].todo  # a date-typed dimension is not compared as a string
    assert m["steps"].source_expression == "type: list list_field: step" and m["steps"].todo


def test_access_rules_on_derived_tables_and_expressions(tmp_path: Path):
    b = _lookml(tmp_path, {
        "m.model.lkml": """connection: "wh"
access_grant: pii { user_attribute: dept allowed_values: ["hr"] }
explore: orders {
  join: stores { sql_on: ${orders.store_id} = ${stores.id} ;; relationship: many_to_one }
  access_filter: { field: stores.name user_attribute: store }
}""",
        "orders.view.lkml": ORDERS.replace("dimension: status", "dimension: store_id { sql: ${TABLE}.store_id ;; }\n  dimension: status"),
        "stores.view.lkml": """view: stores {
  derived_table: { sql: SELECT id, name FROM raw.stores ;; }
  dimension: id { primary_key: yes }
  dimension: name { required_access_grants: [pii] sql: CONCAT(${TABLE}.name, '!') ;; }
}""",
    })
    rf = _by_name(b.row_filters)
    assert rf["orders_stores_name"].table == "stores"
    assert rf["orders_stores_name_via_orders"].predicate == "store_id IN (SELECT id FROM stores WHERE CONCAT(name, '!') = {{ user.store }})"
    (p,) = b.permissions
    assert (p.resource_type, p.resource_id) == ("Column", "stores.name") and "expression" in p.todo


def test_derived_table_without_sql(tmp_path: Path):
    b = _lookml(tmp_path, {"v.view.lkml": """view: model_fit {
  derived_table: { datagroup_trigger: daily create_process: { sql_step: CREATE MODEL ${SQL_TABLE_NAME} ;; } }
  dimension: x {}
}"""})
    (q,) = b.queries
    assert "create_process" in q.todo and "explore_source" not in q.todo


def test_qualified_count_does_not_reuse_a_measure_name(tmp_path: Path):
    b = _lookml(tmp_path, {"v.view.lkml": ORDERS.replace("measure: count { type: count }",
                                                          "measure: count { type: count }\n  measure: orders_count { type: count_distinct sql: ${id} ;; }")})
    assert sorted(m.name for m in b.metrics) == ["orders_count", "orders_count_2"]
    meas = _by_name(b.semantic_models[0].measures)
    assert (meas["orders_count"].agg, meas["orders_count_2"].agg) == ("count_distinct", "count")  # the real orders_count keeps its name
