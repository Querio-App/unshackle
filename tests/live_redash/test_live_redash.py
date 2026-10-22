"""Live check against a real Redash: Docker up, bootstrap, extract, build, verify.

Skipped unless UNSHACKLE_LIVE_REDASH=1. Needs docker plus docker-compose (or `docker compose`),
and a Postgres driver in this venv (`pip install psycopg2-binary`). Uses host ports 5055
(Redash) and 58432 (Postgres) and containers named unshackle-rd-*; it starts from a clean
state (`down -v`) and tears down afterwards unless UNSHACKLE_LIVE_KEEP=1.

Parity: the source side is what Redash itself returns for the saved query with those parameter
values (POST /api/queries/{id}/results, polled until the job finishes); the target side runs
the migrated notebook tiles and verified queries on the same Postgres.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

HERE = Path(__file__).parent
BASE = "http://localhost:5055"
WAREHOUSE = "postgresql+psycopg2://dunder:dunder@localhost:58432/dunder"

pytestmark = pytest.mark.skipif(os.environ.get("UNSHACKLE_LIVE_REDASH") != "1", reason="set UNSHACKLE_LIVE_REDASH=1 to run")


def _compose(*args: str) -> None:
    cmd = ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]
    subprocess.run([*cmd, "-p", "unshackle-rd", "-f", str(HERE / "docker-compose.yml"), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def redash():
    pytest.importorskip("psycopg2")
    sys.path.insert(0, str(HERE))
    import bootstrap

    _compose("down", "-v")
    _compose("up", "-d")
    try:
        yield {**bootstrap.main(BASE), "run": bootstrap.run}
    finally:
        if os.environ.get("UNSHACKLE_LIVE_KEEP") != "1":
            _compose("down", "-v")


def _cli(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    run = subprocess.run([sys.executable, "-m", "unshackle.cli", *args, "--repo", str(repo)], env={**os.environ, **env},
                         capture_output=True, text=True, timeout=600)
    print(run.stdout, run.stderr)
    return run


def test_extract_build_verify(redash, tmp_path: Path):
    env = {"REDASH_URL": BASE, "REDASH_API_KEY": redash["key"], "UNSHACKLE_WAREHOUSE_URL": WAREHOUSE}
    repo = tmp_path / "repo"
    ids = redash["ids"]
    assert _cli(repo, env, "init").returncode == 0
    assert _cli(repo, env, "extract", "redash", "--only", f"dashboard:{ids['Sales overview']}").returncode == 0
    assert (repo / "migration/raw/redash/_errors.json").read_text().strip() == "[]"
    assert _cli(repo, env, "build", "redash").returncode == 0
    assert (repo / "notebooks/sales-overview.ipynb").exists()

    data = repo / "migration/parity/data"
    data.mkdir(parents=True)
    http = httpx.Client(base_url=BASE, headers={"Authorization": f"Key {redash['key']}"}, timeout=120)

    def source(name: str, query: str, params: dict) -> str:
        res = redash["run"](http, ids[query], params)
        cols = [c["name"] for c in res["data"]["columns"]]
        rel = f"migration/parity/data/{name}.csv"
        with open(repo / rel, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([[row.get(c) for c in cols] for row in res["data"]["rows"]])
        return rel

    nb = "notebooks/sales-overview.ipynb"
    period = {"start": "2025-03-01", "end": "2025-09-01"}
    verified = (repo / "queries/revenue-in-period.sql").read_text()
    cases = [
        {"name": "Monthly revenue", "source": {"csv": source("monthly", "Monthly revenue", {})},
         "target": {"notebook": nb, "tile": "monthly_revenue"}, "keys": ["month"]},
        {"name": "Revenue by region, status = COMPLETED (dashboard default)",
         "source": {"csv": source("region-completed", "Revenue by region", {"status": "COMPLETED"})},
         "target": {"notebook": nb, "tile": "revenue_by_region"}, "keys": ["region", "mode"]},
        {"name": "Revenue by region, status = CANCELLED",
         "source": {"csv": source("region-cancelled", "Revenue by region", {"status": "CANCELLED"})},
         "target": {"notebook": nb, "tile": "revenue_by_region", "params": {"status": "CANCELLED"}}, "keys": ["region", "mode"]},
        {"name": "Orders by paper type table, status = RETURNED",
         "source": {"csv": source("paper-returned", "Orders by paper type", {"status": "RETURNED"})},
         "target": {"notebook": nb, "tile": "orders_by_paper_type_table", "params": {"status": "RETURNED"}},
         "keys": ["paper_type"], "tolerance": 0},
        {"name": "Revenue in period counter, 2025-03-01 to 2025-09-01",
         "source": {"csv": source("period", "Revenue in period", {"purchased": period})},
         "target": {"notebook": nb, "tile": "revenue_in_period",
                    "params": {"purchased_start": period["start"], "purchased_end": period["end"]}}},
        {"name": "Revenue in period verified query, default range",
         "source": {"csv": source("period-default", "Revenue in period", {"purchased": {"start": "2025-01-01", "end": "2025-07-01"}})},
         "target": {"sql": verified, "params": {"purchased_start": "2025-01-01", "purchased_end": "2025-07-01"}}},
    ]
    for i, case in enumerate(cases):
        (repo / "migration/parity" / f"{i}.yaml").write_text(yaml.safe_dump(case, sort_keys=False))
    run = _cli(repo, env, "verify")
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(lines) == len(cases) and all(line.startswith("pass") for line in lines)
