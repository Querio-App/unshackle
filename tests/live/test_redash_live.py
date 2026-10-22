"""Redash, live: Redash 26.9.0 in Docker over the Dunder Mifflin warehouse.

1. warehouse + Redash up; the dataset loaded (harness.Stack.load_data)
2. bootstrap (tests/live/redash/bootstrap.py): admin, API key, the warehouse data source and the content
3. `unshackle init / extract / build redash` in a temp repo
4. parity: what Redash itself returns for the saved query with those parameter values (POST
   /api/queries/{id}/results, polled until the job finishes) against the migrated notebook tiles and
   verified queries; `unshackle verify`

Needs Docker and docker-compose. See tests/live/README.md.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import httpx
import pytest
import yaml
from harness import ENABLED, SKIP_REASON, Stack, bootstrap_module, run_unshackle

pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]

BASE = f"http://localhost:{os.environ.get('UNSHACKLE_LIVE_REDASH_PORT', '5055')}"


def test_redash_end_to_end(tmp_path: Path):
    with Stack("redash") as stack:
        assert stack.load_data()
        bootstrap = bootstrap_module("redash")
        exec_in = lambda service, *cmd: stack.compose("exec", "-T", service, *cmd).stdout  # noqa: E731
        redash = {**bootstrap.main(BASE, exec_in, stack.password), "run": bootstrap.run}
        _extract_build_verify(redash, stack, tmp_path)


def _extract_build_verify(redash: dict, stack: Stack, tmp_path: Path) -> None:
    env = {"REDASH_URL": BASE, "REDASH_API_KEY": redash["key"]}
    repo = tmp_path / "repo"
    ids = redash["ids"]
    run_unshackle(repo, "init", env=env)
    run_unshackle(repo, "extract", "redash", "--only", f"dashboard:{ids['Sales overview']}", env=env)
    assert (repo / "migration/raw/redash/_errors.json").read_text().strip() == "[]"
    print(run_unshackle(repo, "build", "redash", env=env))
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
         "target": {"notebook": nb, "tile": "revenue_by_region"}, "keys": ["shipping_region", "shipping_mode"]},
        {"name": "Revenue by region, status = CANCELLED",
         "source": {"csv": source("region-cancelled", "Revenue by region", {"status": "CANCELLED"})},
         "target": {"notebook": nb, "tile": "revenue_by_region", "params": {"status": "CANCELLED"}}, "keys": ["shipping_region", "shipping_mode"]},
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
    out = run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
    print(out)
    results = json.loads((repo / "migration/parity/results.json").read_text())
    assert len(results) == len(cases) and all(r["status"] == "pass" for r in results), out
