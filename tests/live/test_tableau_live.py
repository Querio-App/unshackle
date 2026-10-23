"""Tableau, live: a real Tableau Cloud (or Server) site, with the Dunder Mifflin data as an extract.

1. warehouse up and the dataset loaded (harness.Stack.load_data): the migrated SQL runs there
2. bootstrap (tests/live/tableau/bootstrap.py): one project with a published data source (.tdsx with a
   .hyper extract of the same data), a workbook with four sheets and a dashboard, a group with permissions
3. `unshackle init / extract / build tableau` in a temp repo
4. parity: Tableau's own numbers (the view CSVs `extract` saved from GET /views/{id}/data, the same with
   a parameter set through vf_<parameter>, and VizQL Data Service queries on the published data source)
   against the migrated notebook tiles and the generated Ossie expressions; `unshackle verify`
5. the project and the group are deleted, even when a step fails, and the deletion is checked

Needs Docker, docker-compose and a site: TABLEAU_SERVER_URL, TABLEAU_SITE, TABLEAU_PAT_NAME and
TABLEAU_PAT_SECRET of a Site Administrator Creator. See tests/live/README.md.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml
from harness import ENABLED, SKIP_REASON, Stack, bootstrap_module, run_unshackle, write_case, write_csv

SITE_VARS = ("TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not ENABLED, reason=SKIP_REASON),
    pytest.mark.skipif(not all(os.environ.get(v) for v in SITE_VARS), reason=f"live Tableau test: set {', '.join(SITE_VARS)} and TABLEAU_SITE"),
]


def test_tableau_end_to_end(tmp_path: Path):
    tableau = bootstrap_module("tableau")
    with Stack("tableau") as stack:
        assert stack.load_data()
        try:
            ids = tableau.main()
            _extract_build_verify(tableau, ids, stack, tmp_path)
        finally:
            print("deleted:", tableau.teardown())
            _assert_gone(tableau)


def _assert_gone(tableau) -> None:
    site = tableau.Site()
    try:
        assert site.items("/projects", "projects", "project", filter=f"name:eq:{tableau.PROJECT}") == []
        assert site.items("/workbooks", "workbooks", "workbook", filter=f"projectName:eq:{tableau.PROJECT}") == []
        assert site.items("/datasources", "datasources", "datasource", filter=f"projectName:eq:{tableau.PROJECT}") == []
        assert site.items("/groups", "groups", "group", filter=f"name:eq:{tableau.GROUP}") == []
    finally:
        site.close()


def _ossie_sql(ossie: dict, select: dict[str, str], table: str, group: bool) -> str:
    """SELECT over the dataset's table with each Ossie field/metric expression, derived fields inlined."""
    ds = ossie["datasets"][0]
    fields = {f["name"]: f["expression"]["dialects"][0]["expression"] for f in ds["fields"]}
    exprs = {**fields, **{m["name"]: m["expression"]["dialects"][0]["expression"] for m in ds["metrics"]}}
    out = []
    for alias, name in select.items():
        expr = exprs[name]
        for field, sql in fields.items():
            if field != name and sql != field:
                expr = re.sub(rf"\b{field}\b", f"({sql})", expr)
        out.append(f"{expr} AS {alias}")
    tail = " GROUP BY 1" if group else ""
    return f"SELECT {', '.join(out)} FROM {table}{tail}"


def _extract_build_verify(tableau, ids: dict, stack: Stack, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    raw = repo / "migration/raw/tableau"
    run_unshackle(repo, "init")
    print(run_unshackle(repo, "extract", "tableau", "--only", f"workbook:{ids['workbook']}"))
    assert json.loads((raw / "_errors.json").read_text()) == []
    assert (raw / f"datasources/{ids['datasource']}.tds").exists()  # the published data source behind the sqlproxy
    print(run_unshackle(repo, "build", "tableau"))
    nb = "notebooks/sales-overview.ipynb"
    assert (repo / nb).exists()
    assert sorted(p.name for p in (repo / "semantic").glob("*.ossie.yaml")) == ["accounts.ossie.yaml", "orders.ossie.yaml", "products.ossie.yaml"]

    views = ids["views"]
    data = repo / "migration/parity/data"
    site = tableau.Site()
    try:
        # The table sheet with its parameter set to 1000, as a viewer would with the slider.
        r = site.api("GET", f"/views/{views['Large Orders by Account']}/data", params={"vf_Min Order Value": "1000", "maxAge": 1},
                     headers={"Accept": "*/*"})
        data.mkdir(parents=True, exist_ok=True)
        (data / "large-orders-1000.csv").write_text(r.text)

        def vds(slug: str, fields: list[dict]) -> str:
            resp = site.call("POST", "/api/v1/vizql-data-service/query-datasource",
                             json={"datasource": {"datasourceLuid": ids["datasource"]}, "query": {"fields": fields}})
            rows = (resp if isinstance(resp, dict) else resp.json())["data"]  # not always sent as application/json
            cols = list(rows[0])
            write_csv(data / f"{slug}.csv", cols, [[row[c] for c in cols] for row in rows])
            return f"migration/parity/data/{slug}.csv"

        by_band = vds("revenue-by-discount-band", [{"fieldCaption": "Discount Band"}, {"fieldCaption": "Revenue", "function": "SUM"},
                                                   {"fieldCaption": "days_to_close", "function": "AVG"}])
        by_month = vds("aov-by-order-month", [{"fieldCaption": "Order Month"}, {"fieldCaption": "Average Order Value"}])
    finally:
        site.close()

    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    joined = "public.orders"  # every order has its account and product, so the inner joins keep all rows
    view_csv = lambda name: f"migration/raw/tableau/views/{views[name]}.csv"  # noqa: E731 - saved by `unshackle extract`
    cases = [
        {"name": "Revenue by Paper Type (Status = COMPLETED, join to products)", "source": {"csv": view_csv("Revenue by Paper Type")},
         "target": {"notebook": nb, "tile": "revenue_by_paper_type"}, "keys": ["paper_type"]},
        {"name": "Monthly Revenue", "source": {"csv": view_csv("Monthly Revenue")},
         "target": {"notebook": nb, "tile": "monthly_revenue"}, "keys": ["month_of_purchased_at"]},
        {"name": "Average Order Value KPI", "source": {"csv": view_csv("Average Order Value")},
         "target": {"notebook": nb, "tile": "average_order_value"}},
        {"name": "Large Orders by Account (Min Order Value = 500, the default)", "source": {"csv": view_csv("Large Orders by Account")},
         "target": {"notebook": nb, "tile": "large_orders_by_account"}, "keys": ["account_name"]},
        {"name": "Large Orders by Account (Min Order Value = 1000)", "source": {"csv": "migration/parity/data/large-orders-1000.csv"},
         "target": {"notebook": nb, "tile": "large_orders_by_account", "params": {"min_order_value": 1000}}, "keys": ["account_name"]},
        {"name": "Revenue and avg days to close by Discount Band (Ossie: IF dimension, default aggregation Avg)",
         "source": {"csv": by_band},
         "target": {"sql": _ossie_sql(ossie, {"discount_band": "discount_band", "sum_revenue": "revenue", "avg_days_to_close": "days_to_close"},
                                      joined, True)},
         "keys": ["discount_band"], "columns": {"SUM(Revenue)": "sum_revenue", "AVG(days_to_close)": "avg_days_to_close"}},
        {"name": "Average Order Value by Order Month (Ossie: ratio metric, DATETRUNC dimension)", "source": {"csv": by_month},
         "target": {"sql": _ossie_sql(ossie, {"order_month": "order_month", "average_order_value": "average_order_value"}, joined, True)},
         "keys": ["order_month"]},
    ]
    for i, case in enumerate(cases):
        write_case(repo, f"case-{i}", case)
    out = run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)
    print(out)
    results = json.loads((repo / "migration/parity/results.json").read_text())
    assert len(results) == len(cases) and all(r["status"] == "pass" for r in results), out
