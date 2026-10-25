"""Qlik, live: a Qlik Cloud tenant (no Docker for the tool) over the Dunder Mifflin data.

1. the warehouse up and loaded (harness.Stack.load_data); the same Parquet files uploaded to the tenant
2. bootstrap (tests/live/qlik/bootstrap.py): space `unshackle-live`, data files, an app with a load script,
   Section Access, master items, two sheets and a bookmark, reloaded
3. `unshackle init / extract / build qlik` in a temp repo
4. parity: what Qlik's engine itself returns (each chart's hypercube via GetHyperCubeData, with a selection
   where a case has a filter, and session hypercubes for metrics and the Section Access reduction) against the
   migrated notebook tiles, the Ossie metric expressions and the generated row filters; `unshackle verify`
5. teardown, always: the app, the data files and the space are deleted

Skipped unless UNSHACKLE_LIVE=1 and QLIK_TENANT_URL is set, with QLIK_API_KEY or QLIK_CLIENT_ID /
QLIK_CLIENT_SECRET. The tenant must not already have a space named `unshackle-live`. See tests/live/README.md.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from pathlib import Path

import httpx
import pytest
import yaml
from harness import DATA, ENABLED, SKIP_REASON, Stack, bootstrap_module, run_unshackle, write_csv

from unshackle.ir import snake
from unshackle.sources.qlik import Engine, engine_url

TENANT = os.environ.get("QLIK_TENANT_URL", "").rstrip("/")
pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON),
              pytest.mark.skipif(not TENANT, reason="live Qlik test: set QLIK_TENANT_URL and Qlik credentials")]


def _token() -> str:
    if os.environ.get("QLIK_API_KEY"):
        return os.environ["QLIK_API_KEY"]
    r = httpx.post(f"{TENANT}/oauth/token", json={"client_id": os.environ["QLIK_CLIENT_ID"], "client_secret": os.environ["QLIK_CLIENT_SECRET"],
                                                  "grant_type": "client_credentials", "scope": "user_default"}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def test_qlik_end_to_end(tmp_path: Path):
    boot = bootstrap_module("qlik")
    token = _token()
    created: dict = {}
    try:
        with Stack("qlik") as stack:
            assert stack.load_data()
            boot.main(TENANT, token, DATA / "dunder_mifflin", created)
            _extract_build_verify(created, token, stack, tmp_path)
    finally:
        left = boot.teardown(TENANT, token, created)
        assert not left, left


def _cell(c: dict, date: bool = False) -> object:
    """A hypercube cell as Qlik computed it: its number, a date for date duals, else its text; "" for null."""
    if c.get("qIsNull"):
        return ""
    num = c.get("qNum")
    if isinstance(num, (int, float)) and not math.isnan(num):
        return (dt.date(1899, 12, 30) + dt.timedelta(days=num)).isoformat() if date else repr(num)
    return c.get("qText", "")


def _cube(e: Engine, doc: int, obj: str, columns: list[str], dates: tuple[int, ...] = ()) -> list[list[object]]:
    h = e.handle(e.call(doc, "GetObject", obj))
    page = e.call(h, "GetHyperCubeData", "/qHyperCubeDef", [{"qTop": 0, "qLeft": 0, "qWidth": len(columns), "qHeight": 2000}])
    return [[_cell(c, i in dates) for i, c in enumerate(row)] for row in page["qDataPages"][0]["qMatrix"]]


def _value(e: Engine, doc: int, expr: str) -> object:
    """One expression over the whole app, from a session hypercube (the number, not Evaluate's formatted text)."""
    h = e.handle(e.call(doc, "CreateSessionObject", {"qInfo": {"qType": "unshackle-parity"}, "qHyperCubeDef": {
        "qMeasures": [{"qDef": {"qDef": expr}}], "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qWidth": 1, "qHeight": 1}]}}))
    cell = e.call(h, "GetLayout")["qLayout"]["qHyperCube"]["qDataPages"][0]["qMatrix"][0][0]
    e.call(doc, "DestroySessionObject", e.call(h, "GetLayout")["qLayout"]["qInfo"]["qId"])
    return _cell(cell)


def _ossie_sql(doc: dict, metric: str) -> str:
    """A metric's Ossie expression as a query on its dataset, joining the related datasets it names."""
    base = doc["datasets"][0]
    m = next(x for x in base.get("metrics", []) + doc.get("metrics", []) if x["name"] == metric)
    expr = m["expression"]["dialects"][0]["expression"]
    sql = f"SELECT {expr} AS {metric} FROM {base['source']} AS {base['name']}"
    sources = {d["name"]: d["source"] for d in doc["datasets"]}
    for r in doc.get("relationships", []):
        if r["from"] == base["name"] and re.search(rf"\b{r['to']}\.", expr):
            on = " AND ".join(f"{r['to']}.{t} = {base['name']}.{f}" for f, t in zip(r["from_columns"], r["to_columns"]))
            sql += f" LEFT JOIN {sources[r['to']]} AS {r['to']} ON {on}"
    return sql


def _extract_build_verify(created: dict, token: str, stack: Stack, tmp_path: Path) -> None:
    app, objects = created["app"], created["objects"]
    repo = tmp_path / "repo"
    run_unshackle(repo, "init")
    run_unshackle(repo, "extract", "qlik", "--only", f"app:{app}")
    assert json.loads((repo / "migration/raw/qlik/_errors.json").read_text()) == []
    print(run_unshackle(repo, "build", "qlik"))
    for nb in ("sales-overview", "orders-detail"):
        assert (repo / f"notebooks/{nb}.ipynb").exists()

    data = repo / "migration/parity/data"
    charts = {  # chart title -> (csv columns, which are dates)
        "Revenue": (["revenue"], ()),
        "Orders": (["orders"], ()),
        "Revenue by account manager": (["account_manager", "revenue"], ()),
        "Monthly revenue": (["purchase_month", "revenue", "completed_revenue"], (0,)),
        "Orders by status and paper type": (["status", "paper_type", "orders", "revenue", "average_order_value"], ()),
        "Revenue by shipping region": (["shipping_region", "revenue"], ()),
    }
    metrics = {"completed_revenue": "Sum({<status={'COMPLETED'}>} post_discount_revenue)",
               "completed_orders": "Count({<status={$(vCompletedStatus)}>} DISTINCT order_id)",
               "revenue_outside_the_west": "Sum({<shipping_region-={'West'}>} post_discount_revenue)",
               "average_order_value": "Sum(post_discount_revenue) / Count(DISTINCT order_id)",
               "revenue_vs_target": "Sum(post_discount_revenue) / $(vRevenueTarget)"}
    # Section Access reduces the whole associated model; a set expression on the reduction field does the same.
    reduced = {"revenue": "Sum({<PAPER_TYPE={'COPY','PRINTER'}>} post_discount_revenue)",
               "accounts": "Count({<PAPER_TYPE={'COPY','PRINTER'}>} DISTINCT account_id)"}
    with Engine(engine_url(TENANT, app), token) as e:
        doc = e.handle(e.call(-1, "OpenDoc", app))
        for title, (cols, dates) in charts.items():
            write_csv(data / f"{snake(title)}.csv", cols, _cube(e, doc, objects[title], cols, dates))
        for name, expr in {**metrics, **{f"reduced_{k}": v for k, v in reduced.items()}}.items():
            write_csv(data / f"{name}.csv", [name], [[_value(e, doc, expr)]])
        status = e.handle(e.call(doc, "GetField", "status"))
        e.call(status, "SelectValues", [{"qText": "COMPLETED"}], False, False)
        write_csv(data / "managers_completed.csv", ["account_manager", "revenue"],
                  _cube(e, doc, objects["Revenue by account manager"], ["account_manager", "revenue"]))
        e.call(doc, "ClearAll", False)

    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    filters = {f["name"]: f for f in yaml.safe_load((repo / "policies/row_filters.qlik.yml").read_text())["row_filters"]}
    jim = {t: filters[f"{t}_dm_jim"]["predicate"] for t in ("orders", "accounts")}
    nb1, nb2 = "notebooks/sales-overview.ipynb", "notebooks/orders-detail.ipynb"
    csv = lambda name: {"csv": f"migration/parity/data/{name}.csv"}  # noqa: E731
    cases = [
        {"name": "KPI Revenue", "source": csv("revenue"), "target": {"notebook": nb1, "tile": "revenue"}},
        {"name": "KPI Orders", "source": csv("orders"), "target": {"notebook": nb1, "tile": "orders"}, "tolerance": 0},
        {"name": "Revenue by account manager", "source": csv("revenue_by_account_manager"),
         "target": {"notebook": nb1, "tile": "revenue_by_account_manager"}, "keys": ["account_manager"]},
        {"name": "Revenue by account manager, status = COMPLETED selected", "source": csv("managers_completed"),
         "target": {"notebook": nb1, "tile": "revenue_by_account_manager", "params": {"status": "COMPLETED"}}, "keys": ["account_manager"]},
        {"name": "Monthly revenue (Revenue, Completed revenue)", "source": csv("monthly_revenue"),
         "target": {"notebook": nb1, "tile": "monthly_revenue"}, "keys": ["purchase_month"]},
        {"name": "Orders by status and paper type", "source": csv("orders_by_status_and_paper_type"),
         "target": {"notebook": nb2, "tile": "orders_by_status_and_paper_type"}, "keys": ["status", "paper_type"]},
        {"name": "Revenue by shipping region (orders without a shipping row in the null row)", "source": csv("revenue_by_shipping_region"),
         "target": {"notebook": nb2, "tile": "revenue_by_shipping_region"}, "keys": ["shipping_region"]},
    ] + [
        {"name": f"{name} metric (Ossie)", "source": csv(name), "target": {"sql": _ossie_sql(ossie, name)},
         **({"tolerance": 0} if name == "completed_orders" else {})}
        for name in metrics
    ] + [
        {"name": "Row filter DM\\JIM on orders: revenue", "source": csv("reduced_revenue"),
         "target": {"sql": f"SELECT SUM(post_discount_revenue) AS reduced_revenue FROM orders WHERE {jim['orders']}"}},
        {"name": "Row filter DM\\JIM on accounts: accounts", "source": csv("reduced_accounts"),
         "target": {"sql": f"SELECT COUNT(*) AS reduced_accounts FROM accounts WHERE {jim['accounts']}"}, "tolerance": 0},
    ]
    for i, case in enumerate(cases):
        (repo / "migration/parity" / f"{i:02d}.yaml").write_text(yaml.safe_dump(case, sort_keys=False))
    out = run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
    print(out)
    results = json.loads((repo / "migration/parity/results.json").read_text())
    assert len(results) == len(cases) and all(r["status"] == "pass" for r in results), out
