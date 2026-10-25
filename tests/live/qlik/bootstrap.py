"""Build the Qlik test content in a Qlik Cloud tenant, and tear it down again.

Called by tests/live/test_qlik_live.py. Everything lives in one new shared space,
`unshackle-live`: the five Dunder Mifflin tables as Parquet data files, and an app
"Dunder Mifflin sales" with a load script (file loads, a calculated field, a resident
aggregate, an inline Section Access block), 6 master dimensions, 9 master measures (set
analysis, a variable, a ratio, Aggr, TOTAL), a variable, two sheets (KPIs, bar, line,
filter pane, table, pie, a bar on an Aggr measure) and a bookmark.

Section access keeps the calling user (the OAuth client's own bot user) as ADMIN with `*`,
so the app stays readable after the reload. `teardown()` deletes the app, the data files
and the space; it is safe to call with a partly built `created` dict.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from unshackle.sources.qlik import Engine, engine_url

TABLES = ("accounts", "products", "orders", "shippings", "reviews")

SCRIPT = r"""///$tab Main
SET ThousandSep=',';
SET DecimalSep='.';
SET DateFormat='YYYY-MM-DD';
SET TimestampFormat='YYYY-MM-DD hh:mm:ss[.fff]';
SET vCompletedStatus = 'COMPLETED';

///$tab Section
Section Access;
LOAD * INLINE [
ACCESS, USERID, PAPER_TYPE, OMIT
ADMIN, {admin}, *,
USER, DM\JIM, COPY,
USER, DM\JIM, PRINTER,
USER, DM\DWIGHT, MULTIPURPOSE, order_contact_email
USER, DM\DWIGHT, KRAFT ROLL, order_contact_email
USER, DM\ANGELA, CARDSTOCK,
];
Section Application;

///$tab Data
accounts:
LOAD account_id, account_name, business_size, payment_cycle, account_manager
FROM [lib://{space}:DataFiles/accounts.parquet] (parquet);

products:
LOAD *, Upper(paper_type) AS PAPER_TYPE
FROM [lib://{space}:DataFiles/products.parquet] (parquet);

orders:
LOAD * FROM [lib://{space}:DataFiles/orders.parquet] (parquet);

shippings:
LOAD
    shipping_id,
    shipping_address,
    shipping_city,
    shipping_state,
    shipping_zip,
    shipping_region,
    shipping_latitude,
    shipping_longitude,
    shipping_mode,
    shipping_cost,
    days_to_ship
FROM [lib://{space}:DataFiles/shippings.parquet] (parquet);

reviews:
LOAD review_id, order_id, review, reviewed_at, rating,
    If(rating >= 4, 'Positive', 'Negative') AS sentiment
FROM [lib://{space}:DataFiles/reviews.parquet] (parquet);

// Monthly targets: 10% over what was sold, aggregated in the script.
monthly_targets:
LOAD MonthStart(purchased_at) AS target_month, Sum(post_discount_revenue) * 1.1 AS revenue_target
RESIDENT orders
GROUP BY MonthStart(purchased_at);
"""

DIMENSIONS = {  # title -> field definition
    "Account manager": "account_manager",
    "Paper type": "paper_type",
    "Purchase month": "=MonthStart(purchased_at)",
    "Shipping region": "shipping_region",
    "Order status": "status",
    "Review sentiment": "sentiment",
}
MEASURES = {  # title -> expression
    "Revenue": "Sum(post_discount_revenue)",
    "Orders": "Count(DISTINCT order_id)",
    "Completed revenue": "Sum({<status={'COMPLETED'}>} post_discount_revenue)",
    "Completed orders": "Count({<status={$(vCompletedStatus)}>} DISTINCT order_id)",
    "Revenue outside the West": "Sum({<shipping_region-={'West'}>} post_discount_revenue)",
    "Average order value": "Sum(post_discount_revenue) / Count(DISTINCT order_id)",
    "Revenue vs target": "Sum(post_discount_revenue) / $(vRevenueTarget)",
    "Average revenue per account": "Avg(Aggr(Sum(post_discount_revenue), account_id))",
    "Share of revenue": "Sum(post_discount_revenue) / Sum(TOTAL post_discount_revenue)",
}
VARIABLES = {"vRevenueTarget": ("1000000", "Annual revenue target")}


def _id() -> str:
    return secrets.token_hex(4)


class Rest:
    def __init__(self, tenant: str, token: str):
        self.http = httpx.Client(base_url=tenant, headers={"Authorization": f"Bearer {token}"}, timeout=120, follow_redirects=True)

    def call(self, method: str, path: str, ok: tuple[int, ...] = (), **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400 and r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None


def engine(tenant: str, token: str, app: str) -> Engine:
    return Engine(engine_url(tenant, app), token)


def _dimension() -> dict[str, Any]:
    return {"qDef": {"qGrouping": "N", "qFieldDefs": [], "qFieldLabels": [], "autoSort": True, "cId": _id(),
                     "othersLabel": "Others"},
            "qNullSuppression": False, "qOtherTotalSpec": {"qOtherMode": "OTHER_OFF", "qTotalMode": "TOTAL_OFF"},
            "qOtherLabel": {"qv": "Others"}}


def _hypercube(dims: list[str], measures: list[str], library: dict[str, str]) -> dict[str, Any]:
    """Dims and measures name master items (by title) or, prefixed with "=", are inline."""
    qdims = []
    for d in dims:
        x = _dimension()
        if d in library:
            x["qLibraryId"] = library[d]
        else:
            x["qDef"]["qFieldDefs"], x["qDef"]["qFieldLabels"] = [d], [""]
        qdims.append(x)
    qmeas = []
    for m in measures:
        if m in library:
            qmeas.append({"qLibraryId": library[m], "qDef": {"qLabel": "", "qDef": "", "autoSort": True, "cId": _id()},
                          "qSortBy": {"qSortByNumeric": -1, "qSortByLoadOrder": 1}})
        else:
            label, _, expr = m.partition("=")
            qmeas.append({"qDef": {"qLabel": label, "qDef": expr, "autoSort": True, "cId": _id(),
                                   "qNumFormat": {"qType": "U", "qnDec": 10, "qUseThou": 0}},
                          "qSortBy": {"qSortByNumeric": -1, "qSortByLoadOrder": 1}})
    return {"qDimensions": qdims, "qMeasures": qmeas, "qInterColumnSortOrder": list(range(len(qdims) + len(qmeas))),
            "qSuppressMissing": True, "qSuppressZero": False, "qInitialDataFetch": [{"qLeft": 0, "qTop": 0, "qWidth": 17, "qHeight": 500}],
            "qMode": "S", "qReductionMode": "N", "qNoOfLeftDims": -1, "qPseudoDimPos": -1}


def _chart(kind: str, title: str, dims: list[str], measures: list[str], library: dict[str, str], **extra: Any) -> dict[str, Any]:
    versions = {"barchart": "2.0.10", "linechart": "2.0.10", "kpi": "2.6.0", "table": "2.4.1", "piechart": "2.0.10"}
    return {"qInfo": {"qId": _id(), "qType": kind}, "qMetaDef": {}, "visualization": kind, "version": versions.get(kind, "1.0.0"),
            "showTitles": True, "title": title, "subtitle": "", "footnote": "",
            "qHyperCubeDef": _hypercube(dims, measures, library), **extra}


def _filterpane(fields: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pane = {"qInfo": {"qId": _id(), "qType": "filterpane"}, "qMetaDef": {}, "visualization": "filterpane", "version": "1.1.2",
            "showTitles": False, "title": "", "qChildListDef": {"qData": {}}}
    boxes = [{"qInfo": {"qId": _id(), "qType": "listbox"}, "qMetaDef": {}, "visualization": "listbox", "showTitles": True,
              "title": "", "qListObjectDef": {"qDef": {"qGrouping": "N", "qFieldDefs": [f], "qFieldLabels": [f], "autoSort": True,
                                                       "cId": _id()}, "qShowAlternatives": True, "qInitialDataFetch": []}}
             for f in fields]
    return pane, boxes


def _sheet(e: Engine, doc: int, title: str, rank: int, grid: list[tuple[dict[str, Any], int, int, int, int]],
           objects: dict[str, str]) -> str:
    """grid: (object properties, col, row, colspan, rowspan) on Qlik's default 24 x 12 grid. Chart ids go into `objects`."""
    cells = [{"name": o["qInfo"]["qId"], "type": o["visualization"], "col": c, "row": r, "colspan": w, "rowspan": h}
             for o, c, r, w, h in grid]
    sheet = {"qInfo": {"qId": _id(), "qType": "sheet"}, "qMetaDef": {"title": title, "description": ""}, "rank": rank,
             "columns": 24, "rows": 12, "cells": cells, "qChildListDef": {"qData": {"title": "/title"}}}
    h = e.handle(e.call(doc, "CreateObject", sheet))
    for o, *_ in grid:
        if o.get("title"):
            objects[o["title"]] = o["qInfo"]["qId"]
        children = o.pop("_children", [])
        ch = e.handle(e.call(h, "CreateChild", o))
        for c in children:
            e.call(ch, "CreateChild", c)
    return sheet["qInfo"]["qId"]


def reload(rest: Rest, app: str, timeout: float = 600) -> dict[str, Any]:
    job = rest.call("POST", "/api/v1/reloads", json={"appId": app})
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = rest.call("GET", f"/api/v1/reloads/{job['id']}")
        if r["status"] in ("SUCCEEDED", "FAILED", "EXCEEDED_LIMIT", "CANCELED"):
            if r["status"] != "SUCCEEDED":
                raise RuntimeError(f"reload {r['status']}: {r.get('log', '')[-1500:]}")
            return r
        time.sleep(3)
    raise TimeoutError(f"reload of {app} did not finish in {timeout:.0f}s")


def main(tenant: str, token: str, data: Path, created: dict[str, Any]) -> dict[str, Any]:
    """Build everything; ids go into `created` as they exist, so teardown() can clean up after a failure."""
    rest = Rest(tenant, token)
    me = rest.call("GET", "/api/v1/users/me")
    space_name = "unshackle-live"
    space = rest.call("POST", "/api/v1/spaces", json={"name": space_name, "type": "shared",
                                                      "description": "unshackle live test; deleted at the end of the run"})
    created["space"] = space["id"]
    conn = None
    for _ in range(20):  # the space's DataFiles connection appears shortly after the space
        conn = next((c for c in rest.call("GET", "/api/v1/data-files/connections")["data"] if c.get("spaceId") == space["id"]), None)
        if conn:
            break
        time.sleep(2)
    if conn is None:
        raise RuntimeError("no DataFiles connection for the new space")
    created["files"] = []
    for t in TABLES:
        with (data / f"{t}.parquet").open("rb") as f:
            r = rest.call("POST", "/api/v1/data-files", files={"File": (f"{t}.parquet", f, "application/octet-stream")},
                          data={"Json": f'{{"name": "{t}.parquet", "connectionId": "{conn["id"]}"}}'})
        created["files"].append(r["id"])
    app = rest.call("POST", "/api/v1/apps", json={"attributes": {"name": "Dunder Mifflin sales", "spaceId": space["id"],
                                                                "description": "unshackle live test"}})["attributes"]["id"]
    created["app"] = app

    with engine(tenant, token, app) as e:
        doc = e.handle(e.call(-1, "OpenDoc", app))
        e.call(doc, "SetScript", SCRIPT.replace("{space}", space_name).replace("{admin}", me["subject"]))
        e.call(doc, "DoSave")
    created["reload"] = reload(rest, app)

    library: dict[str, str] = {}
    with engine(tenant, token, app) as e:
        doc = e.handle(e.call(-1, "OpenDoc", app))
        for name, (definition, comment) in VARIABLES.items():
            e.call(doc, "CreateVariableEx", {"qInfo": {"qType": "variable"}, "qName": name, "qDefinition": definition, "qComment": comment})
        for title, field in DIMENSIONS.items():
            r = e.call(doc, "CreateDimension", {"qInfo": {"qType": "dimension"}, "qMetaDef": {"title": title, "description": "", "tags": []},
                                               "qDim": {"qGrouping": "N", "qFieldDefs": [field], "qFieldLabels": [title], "title": title}})
            library[title] = r["qInfo"]["qId"]
        for title, expr in MEASURES.items():
            r = e.call(doc, "CreateMeasure", {"qInfo": {"qType": "measure"},
                                             "qMetaDef": {"title": title, "description": f"{title} (unshackle live test)", "tags": []},
                                             "qMeasure": {"qLabel": title, "qDef": expr, "qGrouping": "N",
                                                          "qNumFormat": {"qType": "U", "qnDec": 10, "qUseThou": 0}}})
            library[title] = r["qInfo"]["qId"]
        pane, boxes = _filterpane(["status", "paper_type"])
        pane["_children"] = boxes
        overview = [
            (_chart("kpi", "Revenue", [], ["Revenue"], library), 0, 0, 6, 2),
            (_chart("kpi", "Orders", [], ["Orders"], library), 6, 0, 6, 2),
            (pane, 12, 0, 12, 4),
            (_chart("barchart", "Revenue by account manager", ["Account manager"], ["Revenue"], library,
                    orientation="vertical", barGrouping={"grouping": "grouped"}), 0, 2, 12, 5),
            (_chart("linechart", "Monthly revenue", ["Purchase month"], ["Revenue", "Completed revenue"], library), 0, 7, 24, 5),
        ]
        detail = [
            (_chart("table", "Orders by status and paper type", ["status", "Paper type"], ["Orders", "Revenue", "Average order value"],
                    library), 0, 0, 24, 5),
            (_chart("piechart", "Revenue by shipping region", ["Shipping region"], ["Revenue=Sum(post_discount_revenue)"], library,
                    donut={"showAsDonut": False}), 0, 5, 12, 7),
            (_chart("barchart", "Average revenue per account by region", ["Shipping region"], ["Average revenue per account"], library,
                    orientation="horizontal", barGrouping={"grouping": "grouped"}), 12, 5, 12, 7),
        ]
        created["objects"] = {}
        created["sheets"] = {"Sales overview": _sheet(e, doc, "Sales overview", 0, overview, created["objects"]),
                             "Orders detail": _sheet(e, doc, "Orders detail", 1, detail, created["objects"])}
        status = e.handle(e.call(doc, "GetField", "status"))
        e.call(status, "SelectValues", [{"qText": "COMPLETED"}], False, False)
        e.call(doc, "CreateBookmark", {"qInfo": {"qType": "bookmark"}, "qMetaDef": {"title": "Completed orders", "description": ""},
                                       "creationDate": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())})
        e.call(doc, "ClearAll", False)
        e.call(doc, "DoSave")
    created["library"] = library
    created["space_name"] = space_name
    return created


def teardown(tenant: str, token: str, created: dict[str, Any]) -> list[str]:
    """Delete the app, the data files and the space. Returns what could not be deleted."""
    rest = Rest(tenant, token)
    left = []
    for kind, path in ([("app", f"/api/v1/apps/{created['app']}")] if created.get("app") else []) + \
                      [("data file", f"/api/v1/data-files/{f}") for f in created.get("files", [])] + \
                      ([("space", f"/api/v1/spaces/{created['space']}")] if created.get("space") else []):
        try:
            rest.call("DELETE", path, ok=(404,))
        except RuntimeError as exc:
            left.append(f"{kind}: {exc}")
    return left
