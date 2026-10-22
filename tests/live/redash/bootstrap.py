"""Bootstrap a fresh Redash with the Dunder Mifflin content, through its API.

Called by tests/live/test_redash_live.py once the stack is up and the warehouse loaded: creates
the metadata tables and the admin (`manage.py` in the server container; the API key is read from
Redash's own Postgres), the
warehouse data source, a Sales group with view-only access, a snippet, four queries with
line, column, counter, table and pie visualizations, and one dashboard with a text widget, a
`status` parameter shared by two widgets and a date-range parameter. Returns {"key", "ids"}.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

CHART = {  # what Redash's chart editor saves (viz-lib chart/getOptions.ts defaults)
    "sortX": True, "legend": {"enabled": True, "placement": "auto", "traceorder": "normal"},
    "xAxis": {"type": "-", "labels": {"enabled": True}}, "yAxis": [{"type": "linear"}, {"type": "linear", "opposite": True}],
    "alignYAxesAtZero": False, "error_y": {"type": "data", "visible": True},
    "series": {"stacking": None, "error_y": {"type": "data", "visible": True}}, "valuesOptions": {},
    "direction": {"type": "counterclockwise"}, "sizemode": "diameter", "coefficient": 1, "piesort": True,
    "color_scheme": "Redash", "lineShape": "linear", "numberFormat": "0,0[.]00000", "percentFormat": "0[.]00%",
    "textFormat": "", "enableLink": False, "linkOpenNewTab": True, "linkFormat": "", "missingValuesAsZero": True,
    "showDataLabels": False, "dateTimeFormat": "DD/MM/YY HH:mm",
}
STATUS = {"name": "status", "title": "Status", "type": "text", "value": "COMPLETED", "global": False, "parentQueryId": None, "locals": []}
QUERIES = [
    {"name": "Monthly revenue", "description": "Post-discount revenue per purchase month.",
     "query": "SELECT date_trunc('month', purchased_at) AS month,\n       SUM(post_discount_revenue) AS revenue\n"
              "FROM orders\nGROUP BY 1\nORDER BY 1", "parameters": [], "tags": ["finance"]},
    {"name": "Revenue by region", "description": "Revenue and orders per shipping region for one order status.",
     "query": "SELECT s.shipping_region,\n       s.shipping_mode,\n       SUM(o.post_discount_revenue) AS revenue\nFROM orders o\n"
              "JOIN shippings s ON s.shipping_id = o.shipping_id\nWHERE o.status = '{{ status }}'\nGROUP BY 1, 2\nORDER BY 1, 2",
     "parameters": [STATUS], "tags": ["sales"]},
    {"name": "Revenue in period", "description": "",
     "query": "SELECT SUM(post_discount_revenue) AS revenue,\n       COUNT(*) AS orders\nFROM orders\n"
              "WHERE purchased_at >= '{{ purchased.start }}'\n  AND purchased_at < '{{ purchased.end }}'",
     "parameters": [{"name": "purchased", "title": "Purchased", "type": "date-range",
                     "value": {"start": "2025-01-01", "end": "2025-07-01"}, "global": False, "parentQueryId": None, "locals": []}],
     "tags": []},
    {"name": "Orders by paper type", "description": "",
     "query": "SELECT p.paper_type,\n       COUNT(*) AS orders,\n       SUM(o.quantity) AS units\nFROM orders o\n"
              "JOIN products p ON p.product_id = o.product_id\nWHERE o.status = '{{ status }}'\nGROUP BY 1\nORDER BY 2 DESC",
     "parameters": [STATUS], "tags": ["sales"]},
]


def bootstrap_server(exec_in: Callable[..., str], email: str = "admin@example.com", password: str = "unshackle-admin") -> str:
    """Metadata tables and the admin user; returns the admin's API key. exec_in(service, *cmd) runs cmd in a compose service."""
    exec_in("server", "/app/manage.py", "database", "create_tables")
    exec_in("server", "/app/manage.py", "users", "create_root", email, "Admin", "--password", password)
    return exec_in("metadata", "psql", "-U", "redash", "-tAc", f"SELECT api_key FROM users WHERE email = '{email}'").strip()


def wait(base: str, timeout: float = 300) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(f"{base}/ping", timeout=5).text.strip() == "PONG.":
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError(f"Redash at {base} did not start")


def run(http: httpx.Client, query_id: int, parameters: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    """Execute a saved query with these parameter values: Redash's own query_result ({"query", "data": {columns, rows}})."""
    r = http.post(f"/api/queries/{query_id}/results", json={"parameters": parameters, "max_age": 0})
    r.raise_for_status()
    res = r.json()
    end = time.time() + timeout
    while "job" in res:
        job = res["job"]
        if job["status"] == 3:
            res = http.get(f"/api/query_results/{job['query_result_id']}").json()
        elif job["status"] in (4, 5) or time.time() > end:
            raise RuntimeError(f"query {query_id} failed: {job.get('error')}")
        else:
            time.sleep(0.5)
            res = http.get(f"/api/jobs/{job['id']}").json()
    return res["query_result"]


def main(base: str, exec_in: Callable[..., str], warehouse_password: str) -> dict[str, Any]:
    wait(base)
    key = bootstrap_server(exec_in)
    http = httpx.Client(base_url=base, headers={"Authorization": f"Key {key}"}, timeout=60)

    def call(method: str, path: str, body: Any = None) -> Any:
        r = http.request(method, path, json=body)
        r.raise_for_status()
        return r.json() if r.content else None

    ds = call("POST", "/api/data_sources", {"name": "Dunder Mifflin", "type": "pg", "options": {
        "host": "warehouse", "port": 5432, "user": "unshackle", "password": warehouse_password, "dbname": "dunder_mifflin"}})
    sales = call("POST", "/api/groups", {"name": "Sales"})
    call("POST", f"/api/groups/{sales['id']}/data_sources", {"data_source_id": ds["id"]})
    call("POST", f"/api/groups/{sales['id']}/data_sources/{ds['id']}", {"view_only": True})
    analyst = call("POST", "/api/users", {"name": "Dwight Schrute", "email": "dwight@example.com"})
    call("POST", f"/api/groups/{sales['id']}/members", {"user_id": analyst["id"]})
    call("POST", "/api/query_snippets", {"trigger": "completed", "description": "Orders that count as revenue",
                                         "snippet": "status = 'COMPLETED'"})

    ids: dict[str, int] = {}
    viz: dict[str, int] = {}
    for q in QUERIES:
        res = call("POST", "/api/queries", {"name": q["name"], "description": q["description"], "query": q["query"],
                                            "data_source_id": ds["id"], "options": {"parameters": q["parameters"]},
                                            "tags": q["tags"]})
        ids[q["name"]] = res["id"]
        call("POST", f"/api/queries/{res['id']}", {"is_draft": False})
        viz[f"{q['name']}/Table"] = next(v["id"] for v in res["visualizations"] if v["type"] == "TABLE")

    def add_viz(query: str, vtype: str, name: str, options: dict[str, Any]) -> None:
        viz[f"{query}/{name}"] = call("POST", "/api/visualizations", {"query_id": ids[query], "type": vtype, "name": name,
                                                                     "description": "", "options": options})["id"]

    add_viz("Monthly revenue", "CHART", "Revenue trend", {
        **CHART, "globalSeriesType": "line", "xAxis": {"type": "datetime", "labels": {"enabled": True}},
        "columnMapping": {"month": "x", "revenue": "y"},
        "seriesOptions": {"revenue": {"type": "line", "yAxis": 0, "zIndex": 0, "index": 0, "name": "Revenue"}}})
    add_viz("Revenue by region", "CHART", "Revenue by region and mode", {
        **CHART, "globalSeriesType": "column", "xAxis": {"type": "-", "labels": {"enabled": True}},
        "columnMapping": {"shipping_region": "x", "revenue": "y", "shipping_mode": "series"}, "seriesOptions": {}})
    add_viz("Revenue in period", "COUNTER", "Revenue", {
        "counterLabel": "Revenue", "counterColName": "revenue", "rowNumber": 1, "targetRowNumber": 1, "stringDecimal": 0,
        "stringDecChar": ".", "stringThouSep": ",", "tooltipFormat": "0,0.000", "stringPrefix": "$"})
    add_viz("Orders by paper type", "CHART", "Order share", {
        **CHART, "globalSeriesType": "pie", "showDataLabels": True, "columnMapping": {"paper_type": "x", "orders": "y"},
        "seriesOptions": {"orders": {"type": "pie", "yAxis": 0, "zIndex": 0, "index": 0}}})
    tbl = viz["Orders by paper type/Table"]
    call("POST", f"/api/visualizations/{tbl}", {"name": "Table", "type": "TABLE", "options": {
        "itemsPerPage": 25, "columns": [
            {"name": "paper_type", "title": "Paper type", "type": "string", "displayAs": "string", "visible": True, "order": 100000,
             "alignContent": "left", "allowSearch": False},
            {"name": "orders", "title": "Orders", "type": "integer", "displayAs": "number", "numberFormat": "0,0", "visible": True,
             "order": 100001, "alignContent": "right", "allowSearch": False},
            {"name": "units", "title": "Units", "type": "integer", "displayAs": "number", "numberFormat": "0,0", "visible": True,
             "order": 100002, "alignContent": "right", "allowSearch": False}]}})

    dash = call("POST", "/api/dashboards", {"name": "Sales overview"})
    ids["Sales overview"] = dash["id"]
    status_map = {"status": {"name": "status", "type": "dashboard-level", "mapTo": "status", "value": None, "title": ""}}
    widgets = [
        ("text", "## Sales overview\nRevenue is **post-discount**. Status filters the region and paper type widgets.",
         {"col": 0, "row": 0, "sizeX": 6, "sizeY": 2}, {}),
        ("Revenue in period/Revenue", None, {"col": 0, "row": 2, "sizeX": 2, "sizeY": 5},
         {"purchased": {"name": "purchased", "type": "dashboard-level", "mapTo": "purchased", "value": None, "title": ""}}),
        ("Monthly revenue/Revenue trend", None, {"col": 2, "row": 2, "sizeX": 4, "sizeY": 8}, {}),
        ("Revenue by region/Revenue by region and mode", None, {"col": 0, "row": 10, "sizeX": 3, "sizeY": 8}, status_map),
        ("Orders by paper type/Order share", None, {"col": 3, "row": 10, "sizeX": 3, "sizeY": 8}, status_map),
        ("Orders by paper type/Table", None, {"col": 0, "row": 18, "sizeX": 6, "sizeY": 6}, status_map),
    ]
    for ref, text, pos, mappings in widgets:
        body: dict[str, Any] = {"dashboard_id": dash["id"], "width": 1, "options": {
            "isHidden": False, "position": {**pos, "autoHeight": False, "minSizeX": 1, "maxSizeX": 6, "minSizeY": 1, "maxSizeY": 1000},
            "parameterMappings": mappings}}
        if text is not None:
            body.update(text=text, visualization_id=None)
        else:
            body.update(text="", visualization_id=viz[ref])
        call("POST", "/api/widgets", body)
    call("POST", f"/api/dashboards/{dash['id']}", {"is_draft": False, "dashboard_filters_enabled": False,
                                                   "tags": ["sales"], "options": {"globalParamOrder": ["status", "purchased"]}})
    call("POST", f"/api/dashboards/{dash['id']}/acl", {"access_type": "modify", "user_id": analyst["id"]})
    for q in QUERIES:  # cache a result per query, as a used instance has
        run(http, ids[q["name"]], {p["name"]: p["value"] for p in q["parameters"]})
    for name, qid in ids.items():  # a few usage events
        call("GET", f"/api/{'dashboards' if name == 'Sales overview' else 'queries'}/{qid}")
    call("GET", f"/api/dashboards/{dash['id']}")
    return {"key": key, "ids": ids, "data_source": ds["id"]}

