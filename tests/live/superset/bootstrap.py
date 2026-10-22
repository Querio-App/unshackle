"""Create the test content in a fresh Superset (docker-compose.yml here).

Called by tests/live/test_superset_live.py once the warehouse is loaded.

Creates: the Postgres warehouse connection, a physical dataset (public.orders) with a
calculated column and five metrics (sum, count, a ratio, a CASE WHEN filter, one that
is not a simple aggregate), a virtual SQL dataset, five charts (bar, line by month,
big number, table, and a pie saved without a query_context, as older charts are), a
dashboard with a markdown block and a native select filter, a "Sales" role with access
to orders, and one row-level security rule. Everything goes through the HTTP API, with
the query_context each chart would get from the Superset frontend. Idempotent only on a
fresh instance.
"""

from __future__ import annotations

import json
import re
import time

import httpx

ADMIN = {"username": "admin", "password": "Unshackle-test-1!"}


def wait_up(base: str, timeout: float = 300) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if httpx.get(f"{base}/health", timeout=5).text.strip() == "OK":
                if httpx.post(f"{base}/api/v1/security/login", json={**ADMIN, "provider": "db"}, timeout=10).status_code == 200:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit(f"Superset at {base} did not become healthy")


class Api:
    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base, timeout=120)
        token = self.http.post("/api/v1/security/login", json={**ADMIN, "provider": "db", "refresh": True}).json()["access_token"]
        self.http.headers["Authorization"] = f"Bearer {token}"
        # Writes need a CSRF token (bound to the session cookie the client keeps) and a Referer.
        self.http.headers["X-CSRFToken"] = self.get("/api/v1/security/csrf_token/")["result"]
        self.http.headers["Referer"] = base

    def call(self, method: str, path: str, **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else None

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, body: dict):
        return self.call("POST", path, json=body)

    def put(self, path: str, body: dict):
        return self.call("PUT", path, json=body)


def dataset(api: Api, db: int, schema: str, table: str, sql: str | None = None) -> int:
    body = {"database": db, "schema": schema, "table_name": table}
    if sql:
        body["sql"] = sql
    return api.post("/api/v1/dataset/", body)["id"]


def update_dataset(api: Api, ds: int, metrics: list[dict], extra_columns: list[dict], **fields) -> None:
    cur = api.get(f"/api/v1/dataset/{ds}")["result"]
    keep = ("id", "column_name", "type", "is_dttm", "groupby", "filterable", "expression", "verbose_name", "description")
    columns = [{k: c[k] for k in keep if k in c and c[k] is not None} for c in cur["columns"]] + extra_columns
    api.put(f"/api/v1/dataset/{ds}?override_columns=false", {"metrics": metrics, "columns": columns, **fields})


def query_context(ds: int, form_data: dict, queries: list[dict]) -> str:
    base = {"filters": [], "extras": {"having": "", "where": ""}, "applied_time_extras": {}, "annotation_layers": [],
            "row_limit": form_data.get("row_limit", 10000), "series_limit": 0, "order_desc": True, "url_params": {},
            "custom_params": {}, "custom_form_data": {}, "time_offsets": [], "post_processing": []}
    return json.dumps({"datasource": {"id": ds, "type": "table"}, "force": False,
                       "queries": [{**base, **q} for q in queries], "form_data": form_data,
                       "result_format": "json", "result_type": "full"})


def chart(api: Api, name: str, viz: str, ds: int, params: dict, queries: list[dict] | None, desc: str = "") -> int:
    form_data = {"datasource": f"{ds}__table", "viz_type": viz, **params}
    body = {"slice_name": name, "viz_type": viz, "datasource_id": ds, "datasource_type": "table",
            "params": json.dumps(form_data), "description": desc or None}
    if queries is not None:
        body["query_context"] = query_context(ds, form_data, queries)
    return api.post("/api/v1/chart/", body)["id"]


def axis(col: str, grain: str | None = None) -> dict:
    out = {"columnType": "BASE_AXIS", "sqlExpression": col, "label": col, "expressionType": "SQL"}
    return {**out, "timeGrain": grain} if grain else out


def create_content(api: Api, warehouse_password: str) -> dict[str, int]:
    ids: dict[str, int] = {}
    uri = f"postgresql+psycopg2://unshackle:{warehouse_password}@warehouse:5432/dunder_mifflin"
    db = ids["database"] = api.post("/api/v1/database/", {
        "database_name": "Dunder Mifflin", "sqlalchemy_uri": uri, "expose_in_sqllab": True})["id"]

    orders = ids["orders"] = dataset(api, db, "public", "orders")
    update_dataset(api, orders, [
        {"metric_name": "revenue", "verbose_name": "Revenue", "expression": "SUM(post_discount_revenue)",
         "description": "Post-discount revenue, all statuses.", "d3format": "$,.0f"},
        {"metric_name": "order_count", "verbose_name": "Orders", "expression": "COUNT(*)"},
        {"metric_name": "average_order_value", "verbose_name": "Average order value",
         "expression": "SUM(post_discount_revenue) / NULLIF(COUNT(*), 0)"},
        {"metric_name": "completed_revenue", "verbose_name": "Completed revenue",
         "expression": "SUM(CASE WHEN status = 'COMPLETED' THEN post_discount_revenue ELSE 0 END)"},
        {"metric_name": "discount_rate", "verbose_name": "Discount rate",
         "expression": "1 - SUM(post_discount_revenue) / NULLIF(SUM(pre_discount_revenue), 0)"},
    ], [{"column_name": "order_size", "expression": "CASE WHEN quantity >= 100 THEN 'large' ELSE 'small' END",
         "type": "VARCHAR", "groupby": True, "filterable": True, "verbose_name": "Order size"}],
        main_dttm_col="purchased_at", description="One row per order.")

    shipped = ids["shipped_orders"] = dataset(api, db, "public", "shipped_orders", sql=(
        "SELECT o.order_id, o.status, o.post_discount_revenue, o.purchased_at, s.shipping_region, s.shipping_mode\n"
        "FROM orders o\nJOIN shippings s ON s.shipping_id = o.shipping_id"))
    update_dataset(api, shipped, [
        {"metric_name": "shipped_revenue", "verbose_name": "Shipped revenue", "expression": "SUM(post_discount_revenue)"},
    ], [], description="Orders with their shipping region and mode.")

    ids["Revenue by region"] = chart(api, "Revenue by region", "echarts_timeseries_bar", shipped, {
        "x_axis": "shipping_region", "metrics": ["shipped_revenue"], "groupby": ["shipping_mode"], "orientation": "vertical",
        "adhoc_filters": [], "row_limit": 10000, "order_desc": True, "stack": "Stack"},
        [{"columns": [axis("shipping_region"), "shipping_mode"], "metrics": ["shipped_revenue"], "orderby": [["shipped_revenue", False]],
          "series_columns": ["shipping_mode"],
          "post_processing": [{"operation": "pivot", "options": {"index": ["shipping_region"], "columns": ["shipping_mode"],
                                                                  "aggregates": {"shipped_revenue": {"operator": "mean"}},
                                                                  "drop_missing_columns": False}}, {"operation": "flatten"}]}])
    ids["Monthly revenue"] = chart(api, "Monthly revenue", "echarts_timeseries_line", orders, {
        "x_axis": "purchased_at", "time_grain_sqla": "P1M", "metrics": ["revenue"], "groupby": [],
        "adhoc_filters": [{"clause": "WHERE", "subject": "purchased_at", "operator": "TEMPORAL_RANGE",
                           "comparator": "No filter", "expressionType": "SIMPLE"}], "row_limit": 10000},
        [{"columns": [axis("purchased_at", "P1M")], "metrics": ["revenue"], "orderby": [["revenue", False]],
          "filters": [{"col": "purchased_at", "op": "TEMPORAL_RANGE", "val": "No filter"}],
          "post_processing": [{"operation": "pivot", "options": {"index": ["purchased_at"], "columns": [],
                                                                  "aggregates": {"revenue": {"operator": "mean"}},
                                                                  "drop_missing_columns": False}}, {"operation": "flatten"}]}])
    ids["Average order value"] = chart(api, "Average order value", "big_number_total", orders, {
        "metric": "average_order_value", "adhoc_filters": [], "header_font_size": 0.4},
        [{"columns": [], "metrics": ["average_order_value"]}])
    units = {"expressionType": "SIMPLE", "column": {"column_name": "quantity"}, "aggregate": "SUM", "label": "Units"}
    ids["Orders by status"] = chart(api, "Orders by status", "table", orders, {
        "query_mode": "aggregate", "groupby": ["status"], "metrics": ["order_count", "revenue", units],
        "adhoc_filters": [{"clause": "WHERE", "expressionType": "SQL", "sqlExpression": "quantity > 0"}],
        "row_limit": 1000, "order_desc": True},
        [{"columns": ["status"], "metrics": ["order_count", "revenue", units], "orderby": [["order_count", False]],
          "filters": [], "extras": {"having": "", "where": "(quantity > 0)"}, "row_limit": 1000}])
    ids["Orders by size"] = chart(api, "Orders by size", "pie", orders, {
        "groupby": ["order_size"], "metric": "order_count", "adhoc_filters": [], "row_limit": 100}, None,
        desc="Saved before Superset stored query contexts.")

    position = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": "Sales overview"}},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": ["ROW-1", "ROW-2", "ROW-3"], "parents": ["ROOT_ID"]},
    }

    def row(rid: str, items: list[tuple[str, dict]]) -> None:
        position[rid] = {"type": "ROW", "id": rid, "children": [i for i, _ in items], "parents": ["ROOT_ID", "GRID_ID"],
                         "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for iid, item in items:
            position[iid] = {"id": iid, "children": [], "parents": ["ROOT_ID", "GRID_ID", rid], **item}

    charts = api.get("/api/v1/chart/", params={"q": "(columns:!(id,uuid),page_size:100)"})["result"]
    uuids = {c["id"]: c["uuid"] for c in charts}

    def cell(name: str, width: int) -> tuple[str, dict]:  # the frontend saves the chart uuid too; export relies on it
        return f"CHART-{ids[name]}", {"type": "CHART", "meta": {"chartId": ids[name], "uuid": uuids[ids[name]], "width": width,
                                                                "height": 50, "sliceName": name}}

    row("ROW-1", [cell("Average order value", 4), cell("Monthly revenue", 8)])
    row("ROW-2", [cell("Revenue by region", 6), cell("Orders by status", 6)])
    row("ROW-3", [("MARKDOWN-1", {"type": "MARKDOWN", "meta": {"width": 4, "height": 50, "code":
                   "## Notes\nRevenue is **post-discount**. The status filter does not apply to the region chart."}}),
                  cell("Orders by size", 8)])
    status_filter = {
        "id": "NATIVE_FILTER-status", "name": "Status", "filterType": "filter_select", "type": "NATIVE_FILTER",
        "targets": [{"datasetId": orders, "column": {"name": "status"}}],
        "controlValues": {"multiSelect": True, "enableEmptyFilter": False, "defaultToFirstItem": False, "searchAllOptions": False},
        "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
        "cascadeParentIds": [], "scope": {"rootPath": ["ROOT_ID"], "excluded": [ids["Revenue by region"]]},
        "chartsInScope": [ids[n] for n in ("Monthly revenue", "Average order value", "Orders by status", "Orders by size")],
        "description": "",
    }
    meta = {"native_filter_configuration": [status_filter], "color_scheme": "", "refresh_frequency": 0,
            "cross_filters_enabled": False, "label_colors": {}, "shared_label_colors": [], "timed_refresh_immune_slices": []}
    dash = ids["Sales overview"] = api.post("/api/v1/dashboard/", {
        "dashboard_title": "Sales overview", "slug": "sales-overview", "published": True,
        "json_metadata": json.dumps(meta)})["id"]
    for name in ("Average order value", "Monthly revenue", "Revenue by region", "Orders by status", "Orders by size"):
        api.put(f"/api/v1/chart/{ids[name]}", {"dashboards": [dash]})
    api.put(f"/api/v1/dashboard/{dash}", {"position_json": json.dumps(position)})

    # A role that can query orders only, and a row-level security rule for it.
    role = ids["Sales"] = api.post("/api/v1/security/roles/", {"name": "Sales"})["id"]
    perms, page = [], 0
    while page == 0 or len(perms) == page * 100:  # the API caps page_size at 100
        perms += api.get("/api/v1/security/permissions-resources/", params={"q": f"(page:{page},page_size:100)"})["result"]
        page += 1
    access = [p["id"] for p in perms if p["permission"]["name"] == "datasource_access"
              and p["view_menu"]["name"].endswith(f"(id:{orders})")]
    api.post(f"/api/v1/security/roles/{role}/permissions", {"permission_view_menu_ids": access})
    ids["rls"] = api.post("/api/v1/rowlevelsecurity/", {
        "name": "Sales sees completed orders", "filter_type": "Regular", "tables": [orders], "roles": [role],
        "clause": "status = 'COMPLETED'", "group_key": "status", "description": "Sales only works with completed orders."})["id"]

    # A little usage: dashboard page loads (a browser session), and chart data requests with the slice and dashboard
    # ids the Superset frontend sends, which the action log records.
    browser = httpx.Client(base_url=str(api.http.base_url), timeout=60, follow_redirects=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', browser.get("/login/").text)
    browser.post("/login/", data={**ADMIN, "csrf_token": token.group(1) if token else ""})
    for _ in range(3):
        browser.get(f"/superset/dashboard/{dash}/").raise_for_status()
    for _ in range(3):
        for name in ("Average order value", "Monthly revenue"):
            api.get(f"/api/v1/chart/{ids[name]}/data/", params={"dashboard_id": dash, "form_data": json.dumps({"slice_id": ids[name]})})
    return ids


def main(base: str, warehouse_password: str) -> dict[str, int]:
    wait_up(base)
    return create_content(Api(base), warehouse_password)

