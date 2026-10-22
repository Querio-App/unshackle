"""Live check against a real Superset: Docker up, bootstrap, extract, build, verify.

Skipped unless UNSHACKLE_LIVE_SUPERSET=1. Needs docker plus docker-compose (or `docker compose`),
and a Postgres driver in this venv (`pip install psycopg2-binary`). Uses host ports 8188
(Superset) and 57432 (Postgres) and containers named unshackle-ss-*; it starts from a clean
state (`down -v`) and tears down afterwards unless UNSHACKLE_LIVE_KEEP=1.

Parity: the source side is what Superset itself returns (POST /api/v1/chart/data with the chart's
query_context, result_type "full", CSV; the pivot that only reshapes for ECharts is dropped so rows
stay long, like the SQL); the target side runs the migrated notebook tiles and the generated Ossie
metric expressions on the same Postgres.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

HERE = Path(__file__).parent
BASE = "http://localhost:8188"
WAREHOUSE = "postgresql+psycopg2://dunder:dunder@localhost:57432/dunder"
LOGIN = {"username": "admin", "password": "Unshackle-test-1!"}

pytestmark = pytest.mark.skipif(os.environ.get("UNSHACKLE_LIVE_SUPERSET") != "1", reason="set UNSHACKLE_LIVE_SUPERSET=1 to run")


def _compose(*args: str) -> None:
    cmd = ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]
    subprocess.run([*cmd, "-p", "unshackle-ss", "-f", str(HERE / "docker-compose.yml"), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def superset():
    pytest.importorskip("psycopg2")
    sys.path.insert(0, str(HERE))
    import bootstrap

    _compose("down", "-v")
    _compose("up", "-d", "--build")
    try:
        yield bootstrap.main(BASE)
    finally:
        if os.environ.get("UNSHACKLE_LIVE_KEEP") != "1":
            _compose("down", "-v")


def _cli(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    run = subprocess.run([sys.executable, "-m", "unshackle.cli", *args, "--repo", str(repo)], env={**os.environ, **env},
                         capture_output=True, text=True, timeout=600)
    print(run.stdout, run.stderr)
    return run


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


def test_extract_build_verify(superset, tmp_path: Path):
    env = {"SUPERSET_URL": BASE, "SUPERSET_USERNAME": LOGIN["username"], "SUPERSET_PASSWORD": LOGIN["password"],
           "UNSHACKLE_WAREHOUSE_URL": WAREHOUSE}
    repo = tmp_path / "repo"
    ids = superset
    assert _cli(repo, env, "init").returncode == 0
    assert _cli(repo, env, "extract", "superset").returncode == 0
    assert json.loads((repo / "migration/raw/superset/_errors.json").read_text()) == []
    assert _cli(repo, env, "build", "superset").returncode == 0
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
         "target": {"notebook": nb, "tile": "revenue_by_region"}, "keys": ["region", "mode"]},
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
    run = _cli(repo, env, "verify")
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(lines) == len(cases) and all(line.startswith("pass") for line in lines)


def snake(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")
