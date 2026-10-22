"""Superset, live: Apache Superset 6.1.0 in Docker over the Dunder Mifflin warehouse.

1. warehouse + Superset up; the dataset loaded (harness.Stack.load_data)
2. bootstrap (tests/live/superset/bootstrap.py): the warehouse connection, datasets, charts, a dashboard, a role, RLS
3. `unshackle init / extract / build superset` in a temp repo
4. parity: what Superset itself returns (POST /api/v1/chart/data with the chart's query_context, result_type
   "full", CSV; the pivot that only reshapes for ECharts is dropped so rows stay long, like the SQL) against the
   migrated notebook tiles and the generated Ossie metric expressions; `unshackle verify`

Needs Docker and docker-compose. See tests/live/README.md.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest
import yaml
from harness import ENABLED, SKIP_REASON, Stack, bootstrap_module, run_unshackle

pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]

BASE = f"http://localhost:{os.environ.get('UNSHACKLE_LIVE_SUPERSET_PORT', '8188')}"
LOGIN = {"username": "admin", "password": "Unshackle-test-1!"}


def _superset_csv(http: httpx.Client, qc: dict, extra_filters: list[dict] | None = None) -> str:
    """Superset's own result for a query context, as CSV (no ECharts pivot)."""
    qc = json.loads(json.dumps(qc))
    for q in qc["queries"]:
        q["post_processing"] = [p for p in q.get("post_processing") or [] if p.get("operation") not in ("pivot", "flatten")]
        q["filters"] = list(q.get("filters") or []) + (extra_filters or [])
    r = http.post("/api/v1/chart/data", json={**qc, "result_type": "full", "result_format": "csv"})
    r.raise_for_status()
    return r.text


def _ossie_sql(ossie: dict, metric: str) -> str:
    """The metric's Ossie expression with derived fields inlined, as a query on the dataset's table."""
    ds = ossie["datasets"][0]
    fields = {f["name"]: f["expression"]["dialects"][0]["expression"] for f in ds["fields"]}
    expr = next(m for m in ds["metrics"] if m["name"] == metric)["expression"]["dialects"][0]["expression"]
    for name, sql in fields.items():
        if sql != name:
            expr = re.sub(rf"\b{name}\b", f"({sql})", expr)
    return f"SELECT {expr} AS {metric} FROM {ds['source']}"


def test_superset_end_to_end(tmp_path: Path):
    with Stack("superset") as stack:
        assert stack.load_data()
        ids = bootstrap_module("superset").main(BASE, stack.password)
        _extract_build_verify(ids, stack, tmp_path)


def _extract_build_verify(ids: dict[str, int], stack: Stack, tmp_path: Path) -> None:
    env = {"SUPERSET_URL": BASE, "SUPERSET_USERNAME": LOGIN["username"], "SUPERSET_PASSWORD": LOGIN["password"]}
    repo = tmp_path / "repo"
    run_unshackle(repo, "init", env=env)
    run_unshackle(repo, "extract", "superset", env=env)
    assert json.loads((repo / "migration/raw/superset/_errors.json").read_text()) == []
    print(run_unshackle(repo, "build", "superset", env=env))
    assert (repo / "notebooks/sales-overview.ipynb").exists()

    http = httpx.Client(base_url=BASE, timeout=120)
    http.headers["Authorization"] = "Bearer " + http.post("/api/v1/security/login", json={**LOGIN, "provider": "db"}).json()["access_token"]
    data = repo / "migration/parity/data"
    data.mkdir(parents=True)
    charts = {}
    for name in ("Monthly revenue", "Revenue by region", "Average order value", "Orders by status"):
        charts[name] = json.loads(http.get(f"/api/v1/chart/{ids[name]}").json()["result"]["query_context"])
        (data / f"{snake(name)}.csv").write_text(_superset_csv(http, charts[name]))
    completed = [{"col": "status", "op": "IN", "val": ["COMPLETED"]}]  # what the dashboard's Status filter adds
    (data / "orders_by_status_completed.csv").write_text(_superset_csv(http, charts["Orders by status"], completed))
    (data / "monthly_revenue_completed.csv").write_text(_superset_csv(http, charts["Monthly revenue"], completed))
    for metric in ("average_order_value", "completed_revenue", "order_count"):
        qc = {"datasource": {"id": ids["orders"], "type": "table"}, "queries": [{"columns": [], "metrics": [metric]}]}
        (data / f"metric_{metric}.csv").write_text(_superset_csv(http, qc))

    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    nb = "notebooks/sales-overview.ipynb"
    orders = {"Orders": "order_count"}  # Superset's CSV names metric columns by their verbose name
    cases = [
        {"name": "Monthly revenue", "source": {"csv": "migration/parity/data/monthly_revenue.csv"},
         "target": {"notebook": nb, "tile": "monthly_revenue"}, "keys": ["purchased_at"]},
        {"name": "Monthly revenue, Status = COMPLETED", "source": {"csv": "migration/parity/data/monthly_revenue_completed.csv"},
         "target": {"notebook": nb, "tile": "monthly_revenue", "params": {"status": "COMPLETED"}}, "keys": ["purchased_at"]},
        {"name": "Revenue by region", "source": {"csv": "migration/parity/data/revenue_by_region.csv"},
         "target": {"notebook": nb, "tile": "revenue_by_region"}, "keys": ["shipping_region", "shipping_mode"]},
        {"name": "Average order value", "source": {"csv": "migration/parity/data/average_order_value.csv"},
         "target": {"notebook": nb, "tile": "average_order_value"}},
        {"name": "Orders by status", "source": {"csv": "migration/parity/data/orders_by_status.csv"},
         "target": {"notebook": nb, "tile": "orders_by_status"}, "keys": ["status"], "columns": orders},
        {"name": "Orders by status, Status = COMPLETED", "source": {"csv": "migration/parity/data/orders_by_status_completed.csv"},
         "target": {"notebook": nb, "tile": "orders_by_status", "params": {"status": "COMPLETED"}}, "keys": ["status"],
         "columns": orders},
    ] + [
        {"name": f"{metric} metric (Ossie)", "source": {"csv": f"migration/parity/data/metric_{metric}.csv"},
         "target": {"sql": _ossie_sql(ossie, metric)}, "columns": {label: metric}, "tolerance": 1e-9}
        for metric, label in (("average_order_value", "Average order value"), ("completed_revenue", "Completed revenue"),
                              ("order_count", "Orders"))
    ]
    for i, case in enumerate(cases):
        (repo / "migration/parity" / f"{i}.yaml").write_text(yaml.safe_dump(case, sort_keys=False))
    out = run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
    print(out)
    results = json.loads((repo / "migration/parity/results.json").read_text())
    assert len(results) == len(cases) and all(r["status"] == "pass" for r in results), out


def snake(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")
