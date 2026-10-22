"""Live check against a real Metabase: Docker up, bootstrap, extract, build, verify.

Skipped unless UNSHACKLE_LIVE_METABASE=1. Needs docker plus docker-compose (or `docker compose`),
and a Postgres driver in this venv (`pip install psycopg2-binary`). Uses host ports 3300
(Metabase) and 56432 (Postgres) and containers named unshackle-mb-*; it starts from a clean
state (`down -v`) and tears down afterwards unless UNSHACKLE_LIVE_KEEP=1.

Parity: the source side is what Metabase itself returns (POST /api/card/{id}/query/csv with
format_rows=false); the target side runs the migrated notebook tiles and the generated Ossie
metric expression on the same Postgres.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

HERE = Path(__file__).parent
BASE = "http://localhost:3300"
WAREHOUSE = "postgresql+psycopg2://dunder:dunder@localhost:56432/dunder"

pytestmark = pytest.mark.skipif(os.environ.get("UNSHACKLE_LIVE_METABASE") != "1", reason="set UNSHACKLE_LIVE_METABASE=1 to run")


def _compose(*args: str) -> None:
    cmd = ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]
    subprocess.run([*cmd, "-p", "unshackle-mb", "-f", str(HERE / "docker-compose.yml"), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def metabase():
    pytest.importorskip("psycopg2")
    sys.path.insert(0, str(HERE))
    import bootstrap

    _compose("down", "-v")
    _compose("up", "-d")
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


def test_extract_build_verify(metabase, tmp_path: Path):
    env = {"METABASE_URL": BASE, "METABASE_API_KEY": metabase["key"], "UNSHACKLE_WAREHOUSE_URL": WAREHOUSE}
    repo = tmp_path / "repo"
    ids = metabase["ids"]
    assert _cli(repo, env, "init").returncode == 0
    assert _cli(repo, env, "extract", "metabase", "--only", f"dashboard:{ids['Sales overview']}",
                "--only", f"card:{ids['Completed orders']}").returncode == 0
    assert (repo / "migration/raw/metabase/_errors.json").read_text().strip() == "[]"
    assert _cli(repo, env, "build", "metabase").returncode == 0
    assert (repo / "notebooks/sales-overview.ipynb").exists()

    data = repo / "migration/parity/data"
    data.mkdir(parents=True)
    http = httpx.Client(base_url=BASE, headers={"X-API-Key": metabase["key"]}, timeout=120)
    for name in ("Monthly revenue", "Orders by status for a region", "Total revenue", "Completed orders"):
        r = http.post(f"/api/card/{ids[name]}/query/csv", data={"format_rows": "false"})
        r.raise_for_status()
        (data / f"{ids[name]}.csv").write_text(r.text)
    # The manager tile with the dashboard's Status filter set, as Metabase runs it on the dashboard.
    dash = json.loads((repo / f"migration/raw/metabase/dashboards/{ids['Sales overview']}.json").read_text())
    dc = next(c for c in dash["dashcards"] if c["card_id"] == ids["Revenue by account manager"])
    mapping = dc["parameter_mappings"][0]
    params = [{"id": mapping["parameter_id"], "type": "string/=", "value": ["COMPLETED"], "target": mapping["target"]}]
    r = http.post(f"/api/dashboard/{dash['id']}/dashcard/{dc['id']}/card/{dc['card_id']}/query/csv",
                  data={"parameters": json.dumps(params), "format_rows": "false"})
    r.raise_for_status()
    (data / "managers-completed.csv").write_text(r.text.lstrip("\ufeff"))
    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    completed = next(m for m in ossie["datasets"][0]["metrics"] if m["name"] == "completed_orders")
    expr = completed["expression"]["dialects"][0]["expression"]
    nb = "notebooks/sales-overview.ipynb"
    cases = [
        {"name": "Monthly revenue", "source": {"csv": f"migration/parity/data/{ids['Monthly revenue']}.csv"},
         "target": {"notebook": nb, "tile": "monthly_revenue"}, "keys": ["purchased_at"],
         "columns": {"Purchased At: Month": "purchased_at", "Sum of Post Discount Revenue ($)": "sum"}},
        {"name": "Orders by status for a region", "source": {"csv": f"migration/parity/data/{ids['Orders by status for a region']}.csv"},
         "target": {"notebook": nb, "tile": "orders_by_status_for_a_region"}, "keys": ["status"], "tolerance": 0},
        {"name": "Total revenue", "source": {"csv": f"migration/parity/data/{ids['Total revenue']}.csv"},
         "target": {"notebook": nb, "tile": "total_revenue"}, "columns": {"Sum of Post Discount Revenue ($)": "sum"}},
        {"name": "Revenue by account manager, Status = COMPLETED", "source": {"csv": "migration/parity/data/managers-completed.csv"},
         "target": {"notebook": nb, "tile": "revenue_by_account_manager", "params": {"status": "COMPLETED"}},
         "keys": ["accounts__via__account_id__account_manager"],
         "columns": {"Account → Account Manager": "accounts__via__account_id__account_manager",
                     "Sum of Post Discount Revenue ($)": "sum", "Count": "count"}},
        {"name": "Completed orders metric (Ossie)", "source": {"csv": f"migration/parity/data/{ids['Completed orders']}.csv"},
         "target": {"sql": f"SELECT {expr} AS count FROM public.orders"}, "columns": {"Count": "count"}, "tolerance": 0},
    ]
    for i, case in enumerate(cases):
        (repo / "migration/parity" / f"{i}.yaml").write_text(yaml.safe_dump(case, sort_keys=False))
    run = _cli(repo, env, "verify")
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(lines) == len(cases) and all(line.startswith("pass") for line in lines)
