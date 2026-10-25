"""Metabase, live: Metabase v0.64.1 in Docker over the Dunder Mifflin warehouse.

1. warehouse + Metabase up; the dataset loaded (harness.Stack.load_data)
2. bootstrap (tests/live/metabase/bootstrap.py): admin, API key, the warehouse connection and the content
3. `unshackle init / extract / build metabase` in a temp repo
4. parity: what Metabase itself returns (POST /api/card/{id}/query/csv with format_rows=false)
   against the migrated notebook tiles and the generated Ossie metric expression; `unshackle verify`

Needs Docker and docker-compose. See tests/live/README.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import yaml
from harness import ENABLED, SKIP_REASON, Stack, bootstrap_module, run_unshackle

pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]

BASE = f"http://localhost:{os.environ.get('UNSHACKLE_LIVE_METABASE_PORT', '3300')}"


def test_metabase_end_to_end(tmp_path: Path):
    with Stack("metabase") as stack:
        assert stack.load_data()
        metabase = bootstrap_module("metabase").main(BASE, stack.password)
        _extract_build_verify(metabase, stack, tmp_path)


def _extract_build_verify(metabase: dict, stack: Stack, tmp_path: Path) -> None:
    env = {"METABASE_URL": BASE, "METABASE_API_KEY": metabase["key"]}
    repo = tmp_path / "repo"
    ids = metabase["ids"]
    run_unshackle(repo, "init", env=env)
    run_unshackle(repo, "extract", "metabase", "--only", f"dashboard:{ids['Sales overview']}",
                  "--only", f"card:{ids['Completed orders']}", env=env)
    assert (repo / "migration/raw/metabase/_errors.json").read_text().strip() == "[]"
    print(run_unshackle(repo, "build", "metabase", env=env))
    assert (repo / "notebooks/sales-overview.ipynb").exists()

    data = repo / "migration/parity/data"
    data.mkdir(parents=True)
    http = httpx.Client(base_url=BASE, headers={"X-API-Key": metabase["key"]}, timeout=120)
    for name in ("Monthly revenue", "Orders by status for a region", "Total revenue", "Completed orders"):
        r = http.post(f"/api/card/{ids[name]}/query/csv", data={"format_rows": "false"})
        r.raise_for_status()
        (data / f"{ids[name]}.csv").write_text(r.text)
    # The manager tile with dashboard filters set, as Metabase runs it on the dashboard; the notebook gets the same values.
    dash = json.loads((repo / f"migration/raw/metabase/dashboards/{ids['Sales overview']}.json").read_text())
    dc = next(c for c in dash["dashcards"] if c["card_id"] == ids["Revenue by account manager"])
    defs = {p["slug"]: p for p in dash["parameters"]}
    targets = {next(p["slug"] for p in dash["parameters"] if p["id"] == m["parameter_id"]): m for m in dc["parameter_mappings"]}
    filtered = {  # file -> (Metabase parameter values, notebook parameters)
        "managers-completed": ({"status": ["COMPLETED"], "purchased": "2024-01-01~2024-06-30"}, {"status": "COMPLETED"}),
        "managers-range": ({"purchased": "2024-03-01~2024-09-30", "quantity": [20, 100], "contact": ["AL"]},
                           {"purchased_from": "2024-03-01", "purchased_to": "2024-09-30", "quantity_min": 20, "quantity_max": 100,
                            "contact": "AL"}),
        "managers-open-ended": ({"purchased": "2025-03-01~", "quantity": [None, 50]},
                                {"purchased_from": "2025-03-02", "purchased_to": None, "quantity_max": 50}),
    }
    for file, (values, _) in filtered.items():
        # The dashboard sends each parameter with its options (case-insensitive Contact); the API treats it as case-sensitive without.
        params = [{"id": targets[k]["parameter_id"], "type": defs[k]["type"], "value": v, "target": targets[k]["target"],
                   **({"options": defs[k]["options"]} if defs[k].get("options") else {})} for k, v in values.items()]
        r = http.post(f"/api/dashboard/{dash['id']}/dashcard/{dc['id']}/card/{dc['card_id']}/query/csv",
                      data={"parameters": json.dumps(params), "format_rows": "false"})
        r.raise_for_status()
        (data / f"{file}.csv").write_text(r.text.lstrip("\ufeff"))
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
        *({"name": f"Revenue by account manager, {values}", "source": {"csv": f"migration/parity/data/{file}.csv"},
           "target": {"notebook": nb, "tile": "revenue_by_account_manager", "params": params},
           "keys": ["accounts__via__account_id__account_manager"],
           "columns": {"Account → Account Manager": "accounts__via__account_id__account_manager",
                       "Sum of Post Discount Revenue ($)": "sum", "Count": "count"}} for file, (values, params) in filtered.items()),
        {"name": "Completed orders metric (Ossie)", "source": {"csv": f"migration/parity/data/{ids['Completed orders']}.csv"},
         "target": {"sql": f"SELECT {expr} AS count FROM public.orders"}, "columns": {"Count": "count"}, "tolerance": 0},
    ]
    for i, case in enumerate(cases):
        (repo / "migration/parity" / f"{i}.yaml").write_text(yaml.safe_dump(case, sort_keys=False))
    out = run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
    print(out)
    results = json.loads((repo / "migration/parity/results.json").read_text())
    assert len(results) == len(cases) and all(r["status"] == "pass" for r in results), out
