"""The harness on its own: Postgres up, the dataset loaded, `unshackle verify` against it. No BI tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import ENABLED, SKIP_REASON, Stack, run_unshackle, write_case

pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]


def test_warehouse_loads_dataset_and_verify_runs(tmp_path: Path):
    import psycopg

    with Stack("warehouse") as stack:
        counts = stack.load_data()
        assert counts == {"accounts": 181, "orders": 3503, "products": 15, "reviews": 1655, "shippings": 2812}
        with psycopg.connect(stack.dsn) as conn:
            types = dict(conn.execute("select column_name, data_type from information_schema.columns where table_name = 'orders'").fetchall())
            fks = conn.execute("select count(*) from information_schema.table_constraints where constraint_type = 'FOREIGN KEY'").fetchone()[0]
        assert types["purchased_at"] == "date" and types["post_discount_revenue"] == "numeric" and types["quantity"] == "integer"
        assert fks == 3
        run_unshackle(tmp_path, "init")
        write_case(tmp_path, "orders-by-status", {
            "name": "Orders by status: grouped vs filtered counts",
            "source": {"sql": "select status, count(*) as orders from orders group by 1"},
            "target": {"sql": "select s.status, (select count(*) from orders o where o.status = s.status) as orders from (select distinct status from orders) s"},
            "keys": ["status"],
            "tolerance": 0,
        })
        write_case(tmp_path, "revenue-types", {
            "name": "Loaded types and keys: revenue, dates, joins",
            "source": {"sql": "select sum(post_discount_revenue) as revenue, min(purchased_at) as first_order from orders"},
            "target": {"sql": "select sum(o.post_discount_revenue) as revenue, min(o.purchased_at) as first_order"
                              " from orders o join accounts a using (account_id) join products p using (product_id)"},
            "tolerance": 0,
        })
        out = run_unshackle(tmp_path, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
        results = json.loads((tmp_path / "migration/parity/results.json").read_text())
        assert [r["status"] for r in results] == ["pass", "pass"], out
