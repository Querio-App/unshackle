"""Bootstrap a fresh Metabase (docker-compose.yml here) and create the test content.

Called by tests/live/test_metabase_live.py once the warehouse is loaded.

Creates: the admin user, an API key in the Administrators group, the Postgres warehouse
connection, "Sales" and "Sales/Finance" collections, a snippet, 4 native questions (template
tags, a field filter, the snippet, a card reference), 3 query-builder questions (one combo),
1 model, 3 metrics, an "Analysts" group and a two-tab dashboard with two filters.
Everything goes through the HTTP API, so it doubles as a check of the shapes
unshackle reads. Idempotent only on a fresh instance.
"""

from __future__ import annotations

import time

import httpx

ADMIN = {"email": "admin@example.com", "password": "Unshackle-test-1!", "first_name": "Ada", "last_name": "Admin"}


def wait_up(base: str, timeout: float = 300) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if httpx.get(f"{base}/api/health", timeout=5).json().get("status") == "ok":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(3)
    raise SystemExit(f"Metabase at {base} did not become healthy")


class Api:
    def __init__(self, base: str, headers: dict[str, str]):
        self.http = httpx.Client(base_url=base, headers=headers, timeout=120)

    def call(self, method: str, path: str, **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.content else None

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, body: dict):
        return self.call("POST", path, json=body)

    def put(self, path: str, body: dict):
        return self.call("PUT", path, json=body)


def setup(base: str) -> str:
    """Create the admin and return an API key (X-API-Key) in the Administrators group."""
    token = httpx.get(f"{base}/api/session/properties").json()["setup-token"]
    r = httpx.post(f"{base}/api/setup", json={
        "token": token, "user": {**ADMIN, "site_name": "unshackle test"},
        "prefs": {"site_name": "unshackle test", "site_locale": "en", "allow_tracking": False},
    }, timeout=120)
    if r.status_code >= 400:
        raise SystemExit(f"setup failed: {r.status_code} {r.text[:500]}")
    session = Api(base, {"X-Metabase-Session": r.json()["id"]})
    groups = session.get("/api/permissions/group")
    admin = next(g for g in groups if g["name"] == "Administrators")
    key = session.post("/api/api-key", {"name": "unshackle", "group_id": admin["id"]})
    return key["unmasked_key"]


def add_warehouse(api: Api, password: str) -> tuple[int, dict]:
    details = {"host": "warehouse", "port": 5432, "dbname": "dunder_mifflin", "user": "unshackle", "password": password, "ssl": False}
    db = api.post("/api/database", {"engine": "postgres", "name": "Dunder Mifflin", "details": details, "is_full_sync": True})
    for _ in range(100):
        meta = api.get(f"/api/database/{db['id']}/metadata")
        tables = {t["name"]: t for t in meta["tables"]}
        if {"orders", "accounts", "products", "shippings", "reviews"} <= set(tables) and all(t["fields"] for t in tables.values()):
            fks = [f for f in tables["orders"]["fields"] if f.get("fk_target_field_id")]
            if len(fks) == 2:
                return db["id"], tables
        time.sleep(2)
    raise SystemExit("warehouse sync did not finish")


def field_id(tables: dict, table: str, column: str) -> int:
    return next(f["id"] for f in tables[table]["fields"] if f["name"] == column)


def create_content(api: Api, db: int, tables: dict) -> dict[str, int]:
    """Legacy (MBQL 4) queries are what the API accepts most portably; Metabase stores them as MBQL 5."""
    ids: dict[str, int] = {}
    col = api.post("/api/collection", {"name": "Sales", "color": "#509EE3"})["id"]
    fin = api.post("/api/collection", {"name": "Finance", "color": "#509EE3", "parent_id": col})["id"]
    orders = tables["orders"]["id"]
    f = lambda t, c: ["field", field_id(tables, t, c), None]  # noqa: E731
    fk = lambda via, t, c: ["field", field_id(tables, t, c), {"source-field": field_id(tables, "orders", via)}]  # noqa: E731

    def card(name: str, kind: str, display: str, query: dict, viz: dict | None = None, collection: int = col, desc: str = "") -> int:
        body = {"name": name, "type": kind, "display": display, "dataset_query": {"database": db, **query},
                "visualization_settings": viz or {}, "collection_id": collection, "description": desc or None}
        ids[name] = api.post("/api/card", body)["id"]
        return ids[name]

    card("Revenue by paper type", "question", "pie", {"type": "native", "native": {"query": (
        "SELECT p.paper_type, SUM(o.post_discount_revenue) AS revenue\n"
        "FROM orders o JOIN products p ON p.product_id = o.product_id\n"
        "WHERE o.status = 'COMPLETED'\nGROUP BY p.paper_type\nORDER BY revenue DESC"), "template-tags": {}}},
        {"pie.dimension": "paper_type", "pie.metric": "revenue"}, desc="Completed revenue split by paper type.")
    card("Orders by status for a region", "question", "bar", {"type": "native", "native": {"query": (
        "SELECT o.status, COUNT(*) AS orders\n"
        "FROM orders o JOIN shippings s ON s.shipping_id = o.shipping_id\n"
        "WHERE s.shipping_region = {{region}}\n"
        "[[AND o.purchased_at >= {{since}}]]\n"
        "GROUP BY o.status\nORDER BY o.status"),
        "template-tags": {
            "region": {"id": "6b8b4567-0000-4000-8000-000000000001", "name": "region", "display-name": "Region",
                       "type": "text", "default": "West", "required": True},
            "since": {"id": "6b8b4567-0000-4000-8000-000000000002", "name": "since", "display-name": "Since", "type": "date"},
        }}},
        {"graph.dimensions": ["status"], "graph.metrics": ["orders"]})
    card("Monthly revenue", "question", "line", {"type": "query", "query": {
        "source-table": orders, "aggregation": [["sum", f("orders", "post_discount_revenue")]],
        "breakout": [["field", field_id(tables, "orders", "purchased_at"), {"temporal-unit": "month"}]]}},
        {"graph.dimensions": ["purchased_at"], "graph.metrics": ["sum"]})
    card("Revenue by account manager", "question", "row", {"type": "query", "query": {
        "source-table": orders, "aggregation": [["sum", f("orders", "post_discount_revenue")], ["count"]],
        "breakout": [fk("account_id", "accounts", "account_manager")],
        "filter": ["!=", f("orders", "status"), "CANCELLED"],
        "order-by": [["desc", ["aggregation", 0]]]}},
        {"graph.dimensions": ["account_manager"], "graph.metrics": ["sum"]})
    card("Order facts", "model", "table", {"type": "native", "native": {"query": (
        "SELECT o.order_id, o.account_id, o.status, o.quantity, o.post_discount_revenue,\n"
        "       o.purchased_at, s.shipping_mode, s.shipping_region\n"
        "FROM orders o JOIN shippings s ON s.shipping_id = o.shipping_id"), "template-tags": {}}},
        desc="One row per order with its shipping details.")
    snippet = api.post("/api/native-query-snippet", {"name": "completed only", "content": "o.status = 'COMPLETED'"})
    card("Completed revenue by shipping mode", "question", "bar", {"type": "native", "native": {"query": (
        "SELECT shippings.shipping_mode, SUM(o.post_discount_revenue) AS revenue\n"
        "FROM orders o JOIN shippings ON shippings.shipping_id = o.shipping_id\n"
        "WHERE {{snippet: completed only}} AND {{mode}}\n"
        "GROUP BY shippings.shipping_mode\nORDER BY shippings.shipping_mode"),
        "template-tags": {
            "snippet: completed only": {"id": "6b8b4567-0000-4000-8000-000000000003", "name": "snippet: completed only",
                                        "display-name": "Snippet: completed only", "type": "snippet", "snippet-name": "completed only",
                                        "snippet-id": snippet["id"]},
            "mode": {"id": "6b8b4567-0000-4000-8000-000000000004", "name": "mode", "display-name": "Shipping mode",
                     "type": "dimension", "dimension": f("shippings", "shipping_mode"), "widget-type": "string/="},
        }}})
    card("Orders by region from the model", "question", "bar", {"type": "native", "native": {"query": (
        f"SELECT shipping_region, COUNT(*) AS orders\nFROM {{{{#{ids['Order facts']}-order-facts}}}} AS f\n"
        "GROUP BY shipping_region\nORDER BY shipping_region"),
        "template-tags": {f"#{ids['Order facts']}-order-facts": {
            "id": "6b8b4567-0000-4000-8000-000000000005", "name": f"#{ids['Order facts']}-order-facts",
            "display-name": "Order facts", "type": "card", "card-id": ids["Order facts"]}}}})
    card("Revenue and orders by month", "question", "combo", {"type": "query", "query": {
        "source-table": orders, "aggregation": [["sum", f("orders", "post_discount_revenue")], ["count"]],
        "breakout": [["field", field_id(tables, "orders", "purchased_at"), {"temporal-unit": "month"}]]}},
        {"graph.dimensions": ["purchased_at"], "graph.metrics": ["sum", "count"],
         "series_settings": {"sum": {"display": "bar"}, "count": {"display": "line", "axis": "right"}}})
    card("Total revenue", "metric", "scalar", {"type": "query", "query": {
        "source-table": orders, "aggregation": [["sum", f("orders", "post_discount_revenue")]]}},
        collection=fin, desc="Post-discount revenue, all statuses.")
    card("Completed orders", "metric", "scalar", {"type": "query", "query": {
        "source-table": orders, "aggregation": [["count"]], "filter": ["=", f("orders", "status"), "COMPLETED"]}},
        collection=fin)
    card("Average discount rate", "metric", "scalar", {"type": "query", "query": {
        "source-table": orders, "aggregation": [["aggregation-options",
            ["/", ["-", ["sum", f("orders", "pre_discount_revenue")], ["sum", f("orders", "post_discount_revenue")]],
             ["sum", f("orders", "pre_discount_revenue")]], {"name": "discount_rate", "display-name": "Discount rate"}]]}},
        collection=fin, desc="Share of list price given away as discount.")

    dash = api.post("/api/dashboard", {"name": "Sales overview", "collection_id": col, "description": "Revenue and orders."})
    params = [
        {"id": "a1b2c3d4", "name": "Region", "slug": "region", "type": "string/=", "sectionId": "string", "default": ["West"]},
        {"id": "e5f6a7b8", "name": "Status", "slug": "status", "type": "string/=", "sectionId": "string"},
    ]
    status = ["dimension", f("orders", "status"), {"stage-number": 0}]
    dashcards = [
        {"id": -1, "card_id": ids["Total revenue"], "row": 0, "col": 0, "size_x": 6, "size_y": 3},
        {"id": -2, "card_id": ids["Monthly revenue"], "row": 0, "col": 6, "size_x": 18, "size_y": 6,
         "parameter_mappings": [{"parameter_id": "e5f6a7b8", "card_id": ids["Monthly revenue"], "target": status}]},
        {"id": -3, "card_id": ids["Revenue by account manager"], "row": 6, "col": 0, "size_x": 12, "size_y": 6,
         "parameter_mappings": [{"parameter_id": "e5f6a7b8", "card_id": ids["Revenue by account manager"], "target": status}]},
        {"id": -4, "card_id": ids["Orders by status for a region"], "row": 6, "col": 12, "size_x": 12, "size_y": 6,
         "parameter_mappings": [{"parameter_id": "a1b2c3d4", "card_id": ids["Orders by status for a region"],
                                 "target": ["variable", ["template-tag", "region"]]}]},
        {"id": -5, "card_id": ids["Revenue by paper type"], "row": 0, "col": 0, "size_x": 12, "size_y": 6, "dashboard_tab_id": -2},
        {"id": -7, "card_id": ids["Revenue and orders by month"], "row": 0, "col": 12, "size_x": 12, "size_y": 6, "dashboard_tab_id": -2},
        {"id": -6, "card_id": None, "row": 6, "col": 0, "size_x": 24, "size_y": 2, "dashboard_tab_id": -2,
         "visualization_settings": {"virtual_card": {"name": None, "display": "text", "visualization_settings": {},
                                                     "dataset_query": {}, "archived": False},
                                    "text": "Revenue is **post-discount**. Cancelled orders are excluded from the manager chart."}},
    ]
    for dc in dashcards:
        dc.setdefault("dashboard_tab_id", -1)
    tabs = [{"id": -1, "name": "Overview"}, {"id": -2, "name": "Details"}]
    api.put(f"/api/dashboard/{dash['id']}", {"parameters": params, "dashcards": dashcards, "tabs": tabs})
    ids["Sales overview"] = dash["id"]
    # A little usage so view counts are non-zero.
    for _ in range(3):
        api.get(f"/api/dashboard/{dash['id']}")
    api.get(f"/api/card/{ids['Monthly revenue']}")
    # A restricted group: query builder only, read access to Sales.
    analysts = api.post("/api/permissions/group", {"name": "Analysts"})["id"]
    graph = api.get("/api/permissions/graph")
    graph["groups"] = {str(analysts): {str(db): {"view-data": "unrestricted", "create-queries": "query-builder"}}}
    api.put("/api/permissions/graph", graph)
    cgraph = api.get("/api/collection/graph")
    cgraph["groups"] = {str(analysts): {str(col): "read"}}
    api.put("/api/collection/graph", cgraph)
    return ids


def main(base: str, warehouse_password: str) -> dict[str, object]:
    wait_up(base)
    key = setup(base)
    api = Api(base, {"X-API-Key": key})
    db, tables = add_warehouse(api, warehouse_password)
    ids = create_content(api, db, tables)
    return {"key": key, "database": db, "ids": ids}

