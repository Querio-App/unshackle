"""The harness on its own: Postgres up, the dataset loaded, `unshackle verify` against it. No BI tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import DATA, ENABLED, SKIP_REASON, Stack, run_unshackle, write_case

pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]


def test_warehouse_loads_dataset_and_verify_runs(tmp_path: Path):
    with Stack("warehouse") as stack:
        counts = stack.load_csvs()
        assert set(counts) == {p.stem for p in (DATA / "dunder_mifflin").glob("*.csv")} and all(counts.values())
        run_unshackle(tmp_path, "init")
        write_case(tmp_path, "orders-by-status", {
            "name": "Orders by status: grouped vs filtered counts",
            "source": {"sql": "select status, count(*) as orders from orders group by 1"},
            "target": {"sql": "select s.status, (select count(*) from orders o where o.status = s.status) as orders from (select distinct status from orders) s"},
            "keys": ["status"],
            "tolerance": 0,
        })
        write_case(tmp_path, "revenue-types", {
            "name": "Loaded types: numeric revenue and timestamps",
            "source": {"sql": "select sum(post_discount_revenue) as revenue, min(purchased_at) as first_order from orders"},
            "target": {"sql": "select sum(pre_discount_revenue * (1 - discount_percentage / 100)) as revenue, min(purchased_at) as first_order from orders"},
            "tolerance": 0.0001,
        })
        out = run_unshackle(tmp_path, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
        results = json.loads((tmp_path / "migration/parity/results.json").read_text())
        assert [r["status"] for r in results] == ["pass", "pass"], out
