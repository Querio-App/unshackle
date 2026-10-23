"""Published Power BI projects (tests/fixtures/powerbi_real, see its README): `unshackle import powerbi` + `build`
on real TMDL models and PBIR / legacy reports, then checks on the constructs that are easy to get wrong."""

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from conftest import notebook_source
from test_ossie import _check, _ossie

from unshackle.cli import main
from unshackle.ir import Bundle

REAL = Path(__file__).parent / "fixtures" / "powerbi_real"


def _project(tmp_path_factory, name: str, *inputs: str):
    repo = tmp_path_factory.mktemp(name)
    for args in (["import", "powerbi", *[str(REAL / name / i) for i in inputs]], ["build", "powerbi"]):
        r = CliRunner().invoke(main, [*args, "--repo", str(repo)])
        assert r.exit_code == 0, r.output
    return repo, Bundle.from_json((repo / "migration/ir/powerbi.json").read_text())


@pytest.fixture(scope="module")
def sales(tmp_path_factory):
    return _project(tmp_path_factory, "sales", "Sales.pbip")


@pytest.fixture(scope="module")
def fca(tmp_path_factory):
    return _project(tmp_path_factory, "fabric-cost-analysis", "FCA_Core_SM.SemanticModel", "FCA_Core_Report.Report")


@pytest.fixture(scope="module")
def qcc(tmp_path_factory):
    return _project(tmp_path_factory, "query-capacity-correlation", "Query Capacity Correlation.pbip")


def _by_name(items):
    return {i.name: i for i in items}


def _warnings(b: Bundle) -> str:
    return "\n".join(f"{w.item}: {w.message}" for w in b.warnings)


@pytest.mark.parametrize("project", ["sales", "fca", "qcc"])
def test_outputs_are_valid(project: str, request):
    repo, b = request.getfixturevalue(project)
    docs = _ossie(repo)
    assert docs and len(docs) == len(b.semantic_models)
    for doc in docs.values():
        _check(doc)
    notebooks = sorted((repo / "notebooks").glob("*.ipynb"))
    assert len(notebooks) == len(b.dashboards) > 0
    for path in notebooks:
        notebook_source(path)


# ---------------------------------------------------------------- Sales (TMDL + legacy report.json)


def test_sales_measures(sales):
    repo, b = sales
    m = _by_name(b.metrics)
    meas = {x.name: x for sm in b.semantic_models for x in sm.measures}
    assert meas["sales_amount"].expr == 'Quantity * "Net Price"'  # SUMX over its own table
    assert meas["margin"].expr == (  # Quantity * (Net Price - Unit Cost): the parentheses survive
        'Quantity * (CASE WHEN "Net Price" IS NULL AND "Unit Cost" IS NULL THEN NULL ELSE COALESCE("Net Price", 0) - COALESCE("Unit Cost", 0) END)'
    )
    assert (m["count_customers_with_sales"].type, meas["count_customers_with_sales"].agg) == ("simple", "count_distinct")
    assert (m["margin_pct"].type, m["margin_pct"].numerator, m["margin_pct"].denominator) == ("ratio", "margin", "sales_amount")
    ytd = m["sales_amount_ytd"]  # TOTALYTD([Sales Amount], 'Calendar'[Date])
    assert (ytd.type, ytd.measure, ytd.grain_to_date) == ("cumulative", "sales_amount", "year")
    assert (m["sales_amount_ly"].type, m["sales_amount_ly"].offsets) == ("derived", {"sales_amount": "1 year"})
    assert m["sales_amount_ytd_ly"].offsets == {"sales_amount_ytd": "1 year"}
    # var ly = [Sales Amount (LY)] return DIVIDE([Sales Amount] - ly, ly)
    assert m["sales_amount_pct_delta_ly"].expr == "1.0 * (sales_amount - sales_amount_ly) / nullif(sales_amount_ly, 0)"
    for name, needle in [
        ("sales_amount_12m_average", "DATESINPERIOD"),  # rolling 12 months, its period VAR used inside CALCULATE
        ("sales_qty_by_delivery_date", "USERELATIONSHIP"),
        ("margin_pct_overall", "REMOVEFILTERS"),
        ("sales_amount_avg_per_day", "AVERAGEX"),
        ("value", "HASONEVALUE"),
    ]:
        assert needle in m[name].todo, name
        assert m[name].source_language == "dax" and needle in m[name].source_expression.upper()
    ossie = yaml.safe_load((repo / "semantic/sales.ossie.yaml").read_text())
    (ds,) = [d for d in ossie["datasets"] if d["name"] == "sales"]
    mets = {x["name"]: x["expression"]["dialects"][0]["expression"] for x in ds["metrics"]}
    assert mets["margin_pct"] == "1.0 * SUM(margin_value) / nullif(SUM(sales_amount_value), 0)"


def test_sales_model_structure(sales):
    repo, b = sales
    sm = _by_name(b.semantic_models)
    assert [(e.name, e.type) for e in sm["sales"].entities] == [("customer", "foreign"), ("calendar", "foreign"), ("product", "foreign"), ("store", "foreign")]
    assert [d.expr for d in sm["calendar"].dimensions if d.name == "date"] == [""]  # Power Query's Column1 rename is not reversed
    assert "date_year_month" in {d.name for d in sm["calendar"].dimensions if d.todo}  # calculated column DATE(...) stays DAX
    w = _warnings(b)
    assert "Time Intelligence: calculation group" in w and "Parameter - Measure: field parameter table" in w
    assert "inactive relationship Sales[Delivery Date] -> Calendar[Date]" in w
    doc = _by_name(b.knowledge)["Power BI model Sales"].body  # named after the project, not database 'Unknown'
    assert "calculation item `YTD`: `CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Calendar'[Date] ) )`" in doc


def test_sales_rls_roles_become_row_filters(sales):
    _, b = sales
    rf = _by_name(b.row_filters)
    one = rf["stores_cluster_1_store"]  # tablePermission Store = 'Store'[Store Code] IN {"1","2","4"}
    assert (one.table, one.predicate, one.groups) == ("store", "\"Store Code\" IN ('1', '2', '4')", ["Stores Cluster 1"])
    assert "unions them" in one.todo and rf["stores_cluster_2_store"].predicate == "\"Store Code\" IN ('10', '11', '15', '8')"


def test_sales_report_pages(sales):
    repo, b = sales
    d = _by_name(b.dashboards)
    page = d["Sales - Sales"]
    assert [f.name for f in page.filters] == ["date_from", "date_to"]  # the Calendar date slicer
    t = {x.title: x for x in page.tiles}
    assert "(calendar.Date >= CAST(:date_from AS DATE) OR :date_from IS NULL)" in t["Sales Amount by Brand, Product, Gender"].sql
    assert "drill hierarchy on brand, product" in t["Sales Amount by Brand, Product, Gender"].todo
    # cards filtered to a calculation item that rewrites the value: no SQL rather than the plain measure
    assert not t["Sales Amount"].sql and "Smart Calcs[Label - ▲ LY]" in t["Sales Amount"].todo
    assert not t["Sales Amount, Sales Amount (LY) by Category, Subcategory, Product"].sql  # time intelligence: query the metric
    detail = d["Sales - Sales Detail"]
    assert [f.name for f in detail.filters] == ["parameter_measure", "parameter_dimension"]  # field parameter slicers
    kpi = d["Sales - KPI"].tiles[0]
    assert "KPI status of measure [Margin]" in kpi.todo
    nb = notebook_source(repo / "notebooks/sales-sales.ipynb")
    assert "date_from = None" in nb and "params={'date_from': date_from, 'date_to': date_to}" in nb


# ---------------------------------------------------------------- Fabric Cost Analysis (Direct Lake + thin PBIR report)


def test_fca_direct_lake_tables(fca):
    _, b = fca
    sm = _by_name(b.semantic_models)
    assert sm["fca"].table == "dbo.focus_fabric"  # partition = entity, entityName focus_fabric, schemaName dbo
    assert not b.queries and len(b.tables) == len(b.semantic_models) == 11
    assert "bi-directional relationship to reservations" in _warnings(b)


def test_fca_measures(fca):
    _, b = fca
    m = _by_name(b.metrics)
    # CALCULATE(SUM(fca[EffectiveCost]), meters[Included]=TRUE(), ...) + 0
    payg = m["count_capacity_payg"]
    assert (payg.type, payg.expr, payg.metrics) == ("derived", "coalesce(count_capacity_payg_1, 0)", ["count_capacity_payg_1"])
    assert m["count_capacity_payg_1"].filter == (
        "{{ Dimension('meters__included') }} = TRUE AND {{ Dimension('fca__commitment_discount_type') }} <> 'Reservation'"
        " AND {{ Dimension('fca__billing_type') }} <> 'Capacity Pause/Delete Surcharge'"
    )
    assert (m["count_total_cost"].type, m["count_total_cost"].filter) == ("simple", "")  # CALCULATE(SUM(..)) without filters
    assert (m["count_usage_ratio_capacity"].numerator, m["count_usage_ratio_capacity"].denominator) == ("count_used_capacity", "count_provisioned_capacity")
    assert (m["total_cost_mtd"].type, m["total_cost_mtd"].grain_to_date) == ("cumulative", "month")
    assert "FILTER" in m["total_cost_ytd"].todo and "ALLSELECTED" in m["total_cost_ytd"].source_expression
    assert "SUMMARIZE" in m["count_regions"].todo
    assert "MIN" in m["score_ri_coverage"].todo  # VAR used inside MIN(..): not inlined


def test_fca_thin_report_uses_the_imported_model(fca):
    repo, b = fca
    assert len(b.dashboards) == 9
    home = _by_name(b.dashboards)["FCA_Core_Report - Home"]
    assert {"resource_name", "date_from", "date_to"} <= {f.name for f in home.filters}
    sqls = [t.sql for t in home.tiles if t.sql]
    assert len(sqls) >= 10 and all("FROM dbo." in s for s in sqls) and sum("FROM dbo.focus_fabric AS fca" in s for s in sqls) >= 5
    other = next(t.sql for t in home.tiles if "AS count_other_payg" in (t.sql or ""))
    # [#PauseCost]+[#SparkAutoScale]+[#CapacityAutoScale], each CALCULATE(..)+0: BLANK counts as 0
    assert "(COALESCE(SUM(CASE WHEN fca.BillingType = 'Capacity Pause/Delete Surcharge' THEN fca.EffectiveCost END), 0))" in other
    assert "LEFT JOIN dbo.meters AS meters ON fca.MeterKey = meters.MeterKey" in other


# ---------------------------------------------------------------- Query Capacity Correlation (composite model, many-to-many)


def test_qcc_composite_model(qcc):
    _, b = qcc
    m = _by_name(b.metrics)
    ext = m["count_of_background_operations_preview"]
    assert "EXTERNALMEASURE" in ext.todo and ext.source_expression.startswith('EXTERNALMEASURE("Count of background operations preview"')
    assert all(m.todo for m in b.metrics)  # every measure reads the remote model or slicer state
    q = _by_name(b.queries)["Items source"]
    assert "DirectQuery to another Power BI semantic model" in q.todo


def test_qcc_relationships(qcc):
    _, b = qcc
    sm = _by_name(b.semantic_models)
    assert sm["max_memory_by_item"].entities == []  # Item key -> Items[Item key] is many-to-many
    assert [(e.name, e.type, e.expr) for e in sm["items"].entities if e.type == "primary"] == [("items", "primary", '"Unique key"')]
    w = _warnings(b)
    assert "many-to-many relationship Query Executions[Warehouse Item ID] -> Items[Item Id]" in w
    assert "is not on Timepoints's key [Timepoint]" in w


def test_qcc_report(qcc):
    repo, b = qcc
    pages = _by_name(b.dashboards)
    overview = pages["Query Capacity Correlation - Capacity and Query Overview"]
    assert [f.name for f in overview.filters] == ["timestamp_from", "timestamp_to", "warehouse"]
    assert all(t.todo for t in overview.tiles if t.kind != "text")
    meta = json.loads((repo / "migration/raw/powerbi/_import.json").read_text())
    assert meta["origin"] == "manual" and any(f.startswith("reports/Query Capacity Correlation/definition/pages/") for f in meta["files"])
