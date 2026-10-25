from conftest import notebook_source
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from unshackle import build
from unshackle.http import SourceError
from unshackle.raw import RawStore
from unshackle.sources import _manual
from unshackle.sources.quicksight import (
    SHAPES,
    QuickSightSource,
    Sql,
    Translator,
    Untranslatable,
    normalize,
    unpack_bundle,
)

FIXTURES = Path(__file__).parent / "fixtures" / "quicksight"  # a hand-written asset bundle; see its README
RAW = Path("migration/raw/quicksight")
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
ACCOUNT = "111122223333"
CREATE = {"dataset": "CreateDataSet", "datasource": "CreateDataSource", "dashboard": "CreateDashboard", "analysis": "CreateAnalysis"}
STATUS = "(t1.status = :order_status OR :order_status IS NULL)"


def repo_with_fixtures(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES, tmp_path / RAW)
    return tmp_path


def parse(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    return repo, QuickSightSource().parse(RawStore(repo, "quicksight"))


def edit(repo: Path, rel: str, change) -> None:
    path = repo / RAW / "bundle" / rel
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


def qs_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def fixture_qs() -> bytes:
    root = FIXTURES / "bundle"
    return qs_zip({str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.json"))})


# ------------------------------------------------------------------ the service model


def _model():
    botocore = pytest.importorskip("botocore.session")
    return botocore.get_session().get_service_model("quicksight")


def _to_model(value, shape, path="$"):
    """Keys matched case-insensitively to the shape's members; fails on a key the model doesn't have.
    Timestamps in epoch milliseconds, as the bundle writes them, become datetimes."""
    if shape.type_name == "timestamp" and isinstance(value, int):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if shape.type_name == "structure":
        members = {k.lower(): (k, s) for k, s in shape.members.items()}
        out = {}
        for k, v in value.items():
            assert k.lower() in members, f"{path}.{k} is not a member of {shape.name}"
            name, s = members[k.lower()]
            out[name] = _to_model(v, s, f"{path}.{name}")
        return out
    if shape.type_name == "list":
        return [_to_model(v, shape.member, f"{path}[]") for v in value]
    if shape.type_name == "map":
        return {k: _to_model(v, shape.value, f"{path}.{k}") for k, v in value.items()}
    return value


def _keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def test_parser_reads_only_service_model_members():
    """Every member parse() reads exists in botocore's QuickSight model, so API drift fails here first."""
    model = _model()
    for shape_name, members in SHAPES.items():
        shape = model.shape_for(shape_name)
        missing = [m for m in members if m not in shape.members]
        assert not missing, f"{shape_name} has no member(s) {missing}"


def test_fixture_follows_service_model():
    """Every file of the bundle is a valid Create* request once its keys take the model's casing (the format has a few
    irregular ones: `kpivisual`, `xaxisLocation`), with no member the model lacks, and normalize() gives the model's
    names for every member the parser reads."""
    from botocore.validate import ParamValidator

    model = _model()
    read = {m for members in SHAPES.values() for m in members}
    files = sorted((FIXTURES / "bundle").rglob("*.json"))
    assert [str(p.relative_to(FIXTURES / "bundle")) for p in files] == [
        "analysis/paper-sales-analysis.json", "dashboard/paper-sales.json", "dataset/order-lines.json",
        "dataset/region-entitlements.json", "dataset/shipments.json", "datasource/dunder-mifflin.json"]
    for path in files:
        raw = json.loads(path.read_text())
        assert raw.pop("resourceType")
        shape = model.operation_model(CREATE[path.parent.name]).input_shape
        doc = _to_model(raw, shape)
        report = ParamValidator().validate({"AwsAccountId": ACCOUNT, **doc}, shape)
        assert not report.has_errors(), f"{path.name}: {report.generate_report()}"
        assert (_keys(doc) & read) <= _keys(normalize(raw)), path.name


# ------------------------------------------------------------------ expressions


def translate(expr: str, dialect: str = "trino", params: bool = True) -> Sql:
    def ref(name: str) -> Sql:
        if name == "nope":
            raise Untranslatable("no such field")
        return Sql(name if name.isidentifier() else f'"{name}"')

    def param(name: str) -> str:
        if not params:
            raise Untranslatable("no parameters")
        return f":{name.lower()}"

    return Translator(ref, param, dialect)(expr)


def test_expressions_deterministic():
    assert translate("ifelse({quantity} >= 100, 'large', isNull({x}), 'none', 'small')").sql == \
        "case when quantity >= 100 then 'large' when (x is null) then 'none' else 'small' end"
    assert translate("{a} / {b}").sql == "1.0 * a / nullif(b, 0)"
    assert translate("truncDate('MM', {purchased_at})").sql == "date_trunc('month', purchased_at)"
    assert translate("extract('YYYY', {d})").sql == "extract(year from d)"
    assert translate("dateDiff({a}, {b}, 'DD')").sql == "date_diff('day', a, b)"
    assert translate("dateDiff({a}, {b})", "postgres").sql == "(cast(b as date) - cast(a as date))"
    assert translate("concat({first name}, ' ', toUpper({last}))").sql == "concat(\"first name\", ' ', upper(last))"
    assert translate("coalesce({a}, 0) + 2 ^ 3").sql == "(coalesce(a, 0) + power(2, 3))"
    assert translate("in({status}, ['A', 'B'])").sql == "status in ('A', 'B')"
    assert translate("contains({name}, '50%', CASE_INSENSITIVE)").sql == "lower(name) like lower('%50\\%%') escape '\\'"
    assert translate("{status} = ${StatusParam} AND NOT {x} <> 1").sql == "(status = :statusparam and not (x <> 1))"
    agg = translate("sumIf({revenue}, {status} = 'COMPLETED')")
    assert (agg.sql, agg.kind) == ("sum(case when status = 'COMPLETED' then revenue end)", "agg")
    assert translate("distinct_count({account_id})").sql == "count(distinct account_id)"
    assert translate("countIf({order_id}, {status} = 'X')").sql == "count(case when status = 'X' then order_id end)"
    lac = translate("sum({revenue}, [{account_id}, {region}])")
    assert (lac.sql, lac.kind) == ("sum(revenue) over (partition by account_id, region)", "window")
    assert translate("avgOver({revenue}, [{account_id}], PRE_AGG)").sql == "avg(revenue) over (partition by account_id)"
    assert translate("1 - sum({a}) / sum({b})").sql == "(1 - 1.0 * sum(a) / nullif(sum(b), 0))"
    # the export drops the braces around plain field names: countIf({order_id}, status = 'COMPLETED')
    assert translate("countIf({order_id}, status = 'COMPLETED')").sql == "count(case when status = 'COMPLETED' then order_id end)"


@pytest.mark.parametrize("expr, why", [
    ("runningSum(sum({revenue}), [{d} ASC])", "table calculation"),
    ("percentOfTotal(sum({revenue}))", "table calculation"),
    ("periodOverPeriodPercentDifference(sum({r}), {d}, MONTH, 1)", "table calculation"),
    ("rank([sum({revenue}) DESC])", "table calculation"),
    ("sumOver(sum({revenue}), [$visualDimensions])", "POST_AGG_FILTER"),
    ("sum({revenue}, [$visualDimensions])", "$visualDimensions"),
    ("sum({revenue}) / max(sum({revenue}, [${visualDimensions}]))", "$visualDimensions"),  # as the export writes it
    ("countOver({order_id}, [{status}], PRE_FILTER)", "PRE_FILTER"),
    ("sumOver({revenue}, [{account}])", "POST_AGG_FILTER"),
    ("truncDate('WK', {d})", "Sunday"),
    ("extract('WD', {d})", "WD"),
    ("dateDiff({a}, {b}, 'MM')", "units other than days"),
    ("avg(sum({revenue}, [{account}]))", "nested aggregation"),
    ("median({revenue})", "median"),
    ("parseDate({s}, 'yyyy-MM-dd')", "parsedate"),
    ("{nope} + 1", "no such field"),
    ("ifelse({a} > 1, 'x'", "expected"),
])
def test_expressions_left_as_todo(expr: str, why: str):
    with pytest.raises(Untranslatable, match=__import__("re").escape(why)):
        translate(expr)


def test_dialect_specific_functions():
    with pytest.raises(Untranslatable, match="dialect"):
        translate("dateDiff({a}, {b})", dialect="")
    assert translate("median({x})", "snowflake").sql == "median(x)"
    assert translate("dateDiff({a}, {b})", "snowflake").sql == "datediff(day, a, b)"


# ------------------------------------------------------------------ semantic layer


def test_parse_semantic_layer(tmp_path: Path):
    _, b = parse(tmp_path)
    assert b.warehouse == "" and any("athena" in w.message for w in b.warnings if w.item == "warehouse type")
    assert [(t.schema, t.name) for t in b.tables] == [("dunder_mifflin", "orders"), ("dunder_mifflin", "accounts"), ("dunder_mifflin", "products")]
    accounts_table = b.tables[1]
    assert [c.name for c in accounts_table.columns][0] == "account_id" and accounts_table.columns[0].hidden  # projected away in QuickSight
    models = {m.name: m for m in b.semantic_models}
    assert set(models) == {"orders", "accounts", "products", "shipments"}  # the RLS rules dataset is not a model
    orders = models["orders"]
    assert orders.table == "dunder_mifflin.orders" and orders.source_ref == "quicksight:dataset/order-lines/pt-orders"
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [
        ("orders", "primary", "order_id"), ("accounts", "foreign", "account_id"), ("products", "foreign", "product_id")]
    assert "{status} <> 'VOID'" in orders.todo and not models["accounts"].todo  # the dataset filter reads orders only
    dims = {d.name: d for d in orders.dimensions}
    assert dims["order_size"].expr == "case when quantity >= 150 then 'bulk' else 'standard' end"
    assert (dims["purchase_month"].type, dims["purchase_month"].expr) == ("time", "date_trunc('month', purchased_at)")
    assert dims["days_to_cancel"].expr == "date_diff('day', purchased_at, cancelled_at)" and dims["days_to_cancel"].label == "Days to cancel"
    assert dims["purchase_year"].expr == "extract(year from purchased_at)"
    assert dims["bulk_order_flag"].expr.startswith("case when (quantity >= 150 and status = 'COMPLETED') then 'Bulk completed'")
    lac = dims["account_revenue"]  # level-aware aggregation: a window, which a model can't group by
    assert "sum(post_discount_revenue) over (partition by account_id)" in lac.todo and lac.source_expression == "sum({post_discount_revenue}, [{account_id}])"
    assert "sum(post_discount_revenue) over (partition by account_id)" in dims["account_revenue_window"].todo  # sumOver(..., PRE_AGG)
    assert "several tables (orders, products)" in dims["revenue_per_unit_price"].todo
    assert "parameter ${OrderStatus}" in dims["matches_status_parameter"].todo
    assert models["accounts"].dimensions[-1].expr == "concat(account_name, ' - ', cast(business_size as varchar))"
    assert {d.name: d.expr for d in models["products"].dimensions}["unit_price"] == "price"  # renamed in the dataset
    measures = {m.name: (m.agg, m.expr) for m in orders.measures}
    assert measures["revenue"] == ("sum", "post_discount_revenue") and measures["order_count"] == ("count", "order_id")
    assert measures["completed_revenue"] == ("sum", "case when status = 'COMPLETED' then post_discount_revenue end")
    assert measures["completed_orders"] == ("count", "case when status = 'COMPLETED' then order_id end")
    assert measures["unique_accounts"] == ("count_distinct", "account_id")
    metrics = {m.name: m for m in b.metrics}
    aov = metrics["average_order_value"]
    assert (aov.type, aov.numerator, aov.denominator, aov.label, aov.todo) == (
        "ratio", "average_order_value_numerator", "average_order_value_denominator", "Average order value", "")
    assert metrics["revenue"].label == "Revenue" and metrics["revenue"].source_ref == "quicksight:dataset/order-lines/pt-orders#revenue"
    todo = {n: metrics[n] for n in ("discount_rate", "running_revenue", "share_of_revenue", "revenue_mom", "revenue_rank",
                                     "visual_share", "orders_before_filters")}
    assert all(m.todo and m.source_expression and m.source_language == "quicksight" for m in todo.values())
    assert todo["running_revenue"].source_expression.startswith("runningSum(") and "table calculation" in todo["running_revenue"].todo
    assert "SQL: (1 - 1.0 * sum(post_discount_revenue) / nullif(sum(pre_discount_revenue), 0))" in todo["discount_rate"].todo
    shipments = models["shipments"]
    assert shipments.table == "shipments" and {d.name: d.expr for d in shipments.dimensions}["cost_share"] == \
        "1.0 * shipping_cost / nullif(post_discount_revenue, 0)"
    (q,) = b.queries
    assert q.name == "Shipments" and q.sql.startswith("SELECT o.order_id") and "materialize as a SQL model" in q.todo


def test_warehouse_and_dialect_from_the_data_source(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "datasource/dunder-mifflin.json", lambda d: d.update(
        type="AURORA_POSTGRESQL", dataSourceParameters={"auroraPostgreSqlParameters": {"host": "db", "port": 5432, "database": "dm"}}))
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    assert b.warehouse == "postgres"
    dims = {d.name: d.expr for m in b.semantic_models for d in m.dimensions}
    assert dims["days_to_cancel"] == "(cast(cancelled_at as date) - cast(purchased_at as date))"


# ------------------------------------------------------------------ dashboards


def test_parse_dashboard(tmp_path: Path):
    _, b = parse(tmp_path)
    d = next(x for x in b.dashboards if x.source_ref == "quicksight:dashboard/paper-sales")
    assert (d.name, d.slug, d.views) == ("Paper sales", "paper-sales", None)
    assert d.url == "https://us-east-1.quicksight.aws.amazon.com/sn/dashboards/paper-sales"
    assert d.layout == [["text_notes"], ["total_revenue", "revenue_per_month"], ["revenue_by_region", "orders_by_status"],
                        ["accounts", "revenue_and_orders_per_month"], ["payment_cycle_to_status"]]
    assert [(f.name, f.field, f.default, f.kind) for f in d.filters] == [
        ("order_status", "parameter OrderStatus", None, "value"), ("shipping_mode", "shipments.shipping_mode", None, "value")]
    tiles = {t.id: t for t in d.tiles}
    kpi = tiles["total_revenue"]
    assert (kpi.kind, kpi.title, kpi.encodings["text"].field) == ("kpi", "Total revenue", "post_discount_revenue")
    assert kpi.sql == ("SELECT\n  sum(t1.post_discount_revenue) AS post_discount_revenue\n"
                       "FROM dunder_mifflin.orders AS t1\n"
                       "LEFT JOIN dunder_mifflin.accounts AS t2 ON t1.account_id = t2.account_id\n"
                       "LEFT JOIN dunder_mifflin.products AS t3 ON t1.product_id = t3.product_id\n"
                       f"WHERE t1.status <> 'VOID'\n  AND {STATUS}") and not kpi.todo
    line = tiles["revenue_per_month"]
    assert line.mark == "line" and (line.encodings["x"].field, line.encodings["x"].type) == ("purchased_at", "temporal")
    assert "date_trunc('month', t1.purchased_at) AS purchased_at" in line.sql and line.sql.endswith("GROUP BY 1\nORDER BY 1")
    bar = tiles["revenue_by_region"]  # the Shipments dataset: the status filter is SINGLE_DATASET on Order lines
    assert bar.mark == "bar" and (bar.encodings["x"].field, bar.encodings["color"].field, bar.encodings["xOffset"].field) == (
        "shipping_region", "shipping_mode", "shipping_mode")
    assert ":order_status" not in bar.sql and "FROM (\nSELECT o.order_id" in bar.sql and ") AS t1" in bar.sql
    assert "(shipping_mode = :shipping_mode OR :shipping_mode IS NULL) AND shipping_mode IS NOT NULL" in bar.sql
    pie = tiles["orders_by_status"]
    assert pie.mark == "arc" and pie.encodings["theta"].field == "order_id" and "count(t1.order_id) AS order_id" in pie.sql
    assert "relative date filter (LAST 3 YEAR)" in pie.todo
    table = tiles["accounts"]
    assert table.kind == "table" and "t2.account_name AS account_name" in table.sql
    assert "sum(t1.post_discount_revenue) AS revenue" in table.sql
    assert "1.0 * sum(t1.post_discount_revenue) / nullif(count(t1.order_id), 0) AS average_order_value" in table.sql
    assert "count(distinct t1.order_id) AS order_id" in table.sql and "AND t1.quantity >= 10" in table.sql
    combo = tiles["revenue_and_orders_per_month"]
    assert combo.compose == "dual_axis" and [(l.mark, l.encodings["y"].field) for l in combo.layers] == [
        ("bar", "post_discount_revenue"), ("line", "order_id")]
    sankey = tiles["payment_cycle_to_status"]
    assert sankey.kind == "unknown" and "SankeyDiagramVisual" in sankey.todo
    assert tiles["text_notes"].text == "Revenue is after discounts. Returned orders still count."


def test_parse_analysis(tmp_path: Path):
    _, b = parse(tmp_path)
    a = next(x for x in b.dashboards if x.source_ref == "quicksight:analysis/paper-sales-analysis")
    assert a.url.endswith("/sn/analyses/paper-sales-analysis") and "analysis" in a.description
    assert a.layout[0] == ["sheet_summary"] and a.layout[-4:] == [
        ["sheet_deep_dive"], ["revenue_by_status_and_payment_cycle", "units_and_revenue_per_account"],
        ["cumulative_revenue", "orders_by_size_and_year"], ["where_accounts_are"]]
    tiles = {t.id: t for t in a.tiles}
    heat = tiles["revenue_by_status_and_payment_cycle"]
    assert heat.mark == "rect" and (heat.encodings["x"].field, heat.encodings["y"].field, heat.encodings["color"].field) == (
        "payment_cycle", "status", "post_discount_revenue")
    assert "date_trunc('day', t1.purchased_at) >= TIMESTAMP '2025-07-01 00:00:00'" in heat.sql
    scatter = tiles["units_and_revenue_per_account"]
    assert scatter.mark == "point" and scatter.encodings["detail"].field == "account_name" and "sum(t1.quantity) AS quantity" in scatter.sql
    pivot = tiles["orders_by_size_and_year"]
    assert "pivot_table" in pivot.todo and "extract(year from t1.purchased_at) AS purchase_year" in pivot.sql
    running = tiles["cumulative_revenue"]
    assert not running.sql and "runningsum() is a table calculation" in running.todo
    assert "GeospatialMapVisual" in tiles["where_accounts_are"].todo


def test_visual_variants(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def change(doc):
        sheet = doc["definition"]["sheets"][0]
        for v in sheet["visuals"]:
            if "barChartVisual" in v:
                conf = v["barChartVisual"]["chartConfiguration"]
                conf.update(orientation="HORIZONTAL", barsArrangement="STACKED_PERCENT")
            if "lineChartVisual" in v:
                wells = v["lineChartVisual"]["chartConfiguration"]["fieldWells"]["lineChartAggregatedFieldWells"]
                wells["values"].append({"numericalMeasureField": {"fieldId": "x", "column": {"dataSetIdentifier": "order_lines", "columnName": "quantity"},
                                                                  "aggregationFunction": {"simpleNumericalAggregation": "AVERAGE"}}})
                wells["category"][0]["dateDimensionField"]["dateGranularity"] = "WEEK"
            if "comboChartVisual" in v:
                v["comboChartVisual"]["chartConfiguration"]["singleAxisOptions"] = {"yAxisOptions": {"yAxis": "PRIMARY_Y_AXIS"}}
        doc["definition"]["filterGroups"][0]["status"] = "DISABLED"
    edit(repo, "dashboard/paper-sales.json", change)
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    tiles = {t.id: t for t in next(d for d in b.dashboards if d.slug == "paper-sales").tiles}
    bar = tiles["revenue_by_region"]
    assert (bar.encodings["y"].field, bar.encodings["x"].field) == ("shipping_region", "post_discount_revenue") and "xOffset" not in bar.encodings
    assert "100% stacked" in bar.todo
    line = tiles["revenue_per_month"]
    assert not line.sql and "weeks start on Sunday" in line.todo
    assert tiles["revenue_and_orders_per_month"].compose == "layer"
    assert ":order_status" not in tiles["total_revenue"].sql  # disabled filter group


# ------------------------------------------------------------------ permissions and row-level security


def test_permissions_and_security(tmp_path: Path):
    _, b = parse(tmp_path)
    perms = {(p.effect, p.principal, p.action, p.resource_type, p.resource_id) for p in b.permissions}
    assert {("permit", "group:Sales", "view", "Notebook", "paper-sales"), ("permit", "user:pbeesly", "edit", "Notebook", "paper-sales"),
            ("permit", "user:pbeesly", "edit", "Notebook", "paper-sales-analysis"),
            ("permit", "group:Analysts", "query", "Table", "dunder_mifflin.orders"),
            ("permit", "group:Analysts", "query", "Table", "dunder_mifflin.accounts"),
            ("permit", "group:Sales", "query", "Table", "shipments"),
            ("permit", "group:Analysts", "query", "Datasource", "warehouse")} <= perms
    assert ("permit", "group:Sales", "edit", "Notebook", "paper-sales") not in perms
    cls = [p for p in b.permissions if p.effect == "forbid"]
    assert [(p.resource_type, p.resource_id, p.condition) for p in cls] == [
        ("Column", "dunder_mifflin.orders.order_contact_name", '!(principal in Group::"Accounting")'),
        ("Column", "dunder_mifflin.orders.order_contact_email", '!(principal in Group::"Accounting")')]
    rf = {r.name: r for r in b.row_filters}
    user = rf["shipments_region_entitlements"]
    assert user.table == "shipments" and user.groups == []
    assert user.predicate == (
        "(shipping_region in (select r.shipping_region from dunder_mifflin.region_entitlements r where "
        "(r.\"UserName\" = {{ user.email }} or r.\"GroupName\" in ({{ user.groups }})) and r.shipping_region is not null) "
        "or exists (select 1 from dunder_mifflin.region_entitlements r where "
        "(r.\"UserName\" = {{ user.email }} or r.\"GroupName\" in ({{ user.groups }})) and r.shipping_region is null))")
    assert "{{ user.email }}" in user.todo and "VERSION_1" not in user.todo and "GRANT_ACCESS" in user.source_expression
    tag = rf["order_lines_tag_account_manager"]
    assert tag.table == "dunder_mifflin.accounts" and tag.predicate == \
        "({{ user.account_manager }} = '*' or account_manager = {{ user.account_manager }})" and "session tag" in tag.todo


def test_rls_variants(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "dataset/shipments.json", lambda d: d["rowLevelPermissionDataSet"].update(permissionPolicy="DENY_ACCESS", formatVersion="VERSION_1"))
    edit(repo, "dataset/order-lines.json", lambda d: d["rowLevelPermissionTagConfiguration"]["tagRules"][0].update(tagMultiValueDelimiter=","))
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    rf = {r.name: r for r in b.row_filters}
    deny = rf["shipments_region_entitlements"]
    assert deny.predicate.startswith("not coalesce((shipping_region in (select") and "VERSION_1" in deny.todo
    assert not rf["order_lines_tag_account_manager"].predicate and "separated by ','" in rf["order_lines_tag_account_manager"].todo

    def two_fields(d):
        d["physicalTableMap"]["pt-entitlements"]["relationalTable"]["inputColumns"].append({"name": "shipping_mode", "type": "STRING"})
    edit(repo, "dataset/region-entitlements.json", two_fields)
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    multi = next(r for r in b.row_filters if r.name == "shipments_region_entitlements")
    assert not multi.predicate and "2 fields (shipping_region, shipping_mode)" in multi.todo
    (repo / RAW / "bundle/dataset/region-entitlements.json").unlink()
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    missing = next(r for r in b.row_filters if r.name.startswith("shipments_"))
    assert not missing.predicate and "not in the export" in missing.todo


# ------------------------------------------------------------------ build


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    build.apply(b, repo)
    text = (repo / "semantic/orders.ossie.yaml").read_text()
    ossie = yaml.safe_load(text)
    metrics = {m["name"]: m["expression"]["dialects"][0]["expression"] for m in ossie["datasets"][0]["metrics"]}
    assert metrics["revenue"] == "SUM(post_discount_revenue)"
    assert metrics["average_order_value"] == "1.0 * SUM(post_discount_revenue) / nullif(COUNT(order_id), 0)"
    assert "TODO(unshackle): metric running_revenue" in text and "runningSum(" in text
    for slug, todos in (("paper-sales", 4), ("paper-sales-analysis", 10)):
        nb = notebook_source(repo / f"notebooks/{slug}.ipynb")
        assert "order_status = None" in nb and "shipping_mode = None" in nb and nb.count("TODO(unshackle)") == todos, slug
    assert "mark_rect()" in notebook_source(repo / "notebooks/paper-sales-analysis.ipynb")
    cedar = (repo / "policies/quicksight.cedar").read_text()
    assert 'principal in Group::"Sales"' in cedar and 'resource in Column::"warehouse.dunder_mifflin.orders.order_contact_email"' in cedar
    rows = yaml.safe_load((repo / "policies/row_filters.quicksight.yml").read_text())["row_filters"]
    assert {r["name"] for r in rows} == {"shipments_region_entitlements", "order_lines_tag_account_manager"}
    assert "materialize as a SQL model" in (repo / "queries/shipments.sql").read_text()


def test_notebook_runs_against_duckdb(tmp_path: Path):
    import duckdb
    import pandas as pd

    from unshackle.run import notebook_queries, query, run_notebook

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "dunder.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema dunder_mifflin")
    for p in sorted(DATA.glob("*.parquet")):
        con.execute(f"create table dunder_mifflin.{p.stem} as select * from read_parquet('{p}')")
    orders = con.execute("select * from dunder_mifflin.orders").df()
    accounts = con.execute("select * from dunder_mifflin.accounts").df()
    shippings = con.execute("select * from dunder_mifflin.shippings").df()
    con.close()
    url = f"duckdb:///{db}"
    nb = repo / "notebooks/paper-sales.ipynb"
    html = run_notebook(nb, url=url).read_text()
    assert "vegaEmbed" in html and "Revenue by region" in html
    params, sqls = notebook_queries(nb)
    by_status = query(sqls["orders_by_status"], params, url).set_index("status")["order_id"].to_dict()
    assert by_status == orders.groupby("status")["order_id"].count().to_dict()
    completed = query(sqls["revenue_per_month"], {**params, "order_status": "COMPLETED"}, url)
    want = orders[orders.status == "COMPLETED"].groupby(pd.to_datetime(orders.purchased_at).dt.to_period("M"))["post_discount_revenue"].sum()
    assert [float(x) for x in completed["post_discount_revenue"]] == pytest.approx([float(x) for x in want.values])
    total = query(sqls["total_revenue"], params, url)["post_discount_revenue"].iloc[0]
    assert float(total) == pytest.approx(float(orders.post_discount_revenue.sum()))
    # the joined table with its numeric range filter (quantity >= 10) and the ratio calculated field
    got = query(sqls["accounts"], {**params, "order_status": "RETURNED"}, url).set_index("account_name")
    lines = orders[(orders.status == "RETURNED") & (orders.quantity >= 10)].merge(accounts, on="account_id", how="left")
    by_account = lines.groupby("account_name").agg(revenue=("post_discount_revenue", "sum"), n=("order_id", "count"))
    assert sorted(got.index) == sorted(by_account.index)
    assert [float(got.revenue[a]) for a in by_account.index] == pytest.approx([float(x) for x in by_account.revenue])
    assert [float(got.average_order_value[a]) for a in by_account.index] == pytest.approx(
        [float(r) / n for r, n in zip(by_account.revenue, by_account.n)])
    # the custom SQL dataset as a subquery, with the shipping mode filter control set
    got = query(sqls["revenue_by_region"], {**params, "shipping_mode": "EXPRESS"}, url).set_index("shipping_region")
    shipped = orders.merge(shippings, on="shipping_id")
    want = shipped[shipped.shipping_mode == "EXPRESS"].groupby("shipping_region")["post_discount_revenue"].sum()
    assert set(got.shipping_mode) == {"EXPRESS"} and sorted(got.index) == sorted(want.index)
    assert [float(got.post_discount_revenue[r]) for r in want.index] == pytest.approx([float(x) for x in want.values])


# ------------------------------------------------------------------ import


def test_import_qs_bundle(tmp_path: Path):
    qs = tmp_path / "assetbundle-sales.qs"
    qs.write_bytes(fixture_qs())
    store = RawStore(tmp_path / "repo", "quicksight")
    files = _manual.run(QuickSightSource(), store, [qs])
    assert "bundle/dashboard/paper-sales.json" in files and len(files) == 6
    meta = store.get_json("_import.json")
    assert meta["origin"] == "manual" and any("IncludePermissions" in g for g in meta["gaps"])
    b = QuickSightSource().parse(store)
    assert {d.slug for d in b.dashboards} == {"paper-sales", "paper-sales-analysis"}
    folder = tmp_path / "unzipped"
    shutil.copytree(FIXTURES / "bundle", folder)
    assert len(_manual.run(QuickSightSource(), store, [folder])) == 6


def test_import_rejects_other_files(tmp_path: Path):
    src = QuickSightSource()
    bad = tmp_path / "x.pbix"
    bad.write_bytes(b"x")
    with pytest.raises(SourceError, match="QuickSight import accepts"):
        src.import_path(bad)
    with pytest.raises(SourceError, match="not a zip"):
        unpack_bundle(b"not a zip", "x.qs")
    with pytest.raises(SourceError, match="unsafe path"):
        unpack_bundle(qs_zip({"../evil.json": b"{}"}), "x.qs")
    with pytest.raises(SourceError, match="no QuickSight dashboards"):
        unpack_bundle(qs_zip({"theme/t.json": json.dumps({"resourceType": "theme"}).encode()}), "x.qs")


# ------------------------------------------------------------------ the describe-API layout


def test_api_layout_parses_like_the_bundle(tmp_path: Path):
    """The fallback writes Describe* responses (PascalCase); they parse to the same bundle as the export."""
    model = _model()
    repo = tmp_path / "api"
    for path in sorted((FIXTURES / "bundle").rglob("*.json")):
        kind = path.parent.name
        raw = json.loads(path.read_text())
        raw.pop("resourceType")
        doc = _to_model(raw, model.operation_model(CREATE[kind]).input_shape)
        if kind in ("dataset", "datasource"):
            rid = doc.get("DataSetId") or doc["DataSourceId"]
            doc["Arn"] = f"arn:aws:quicksight:us-east-1:{ACCOUNT}:{kind}/{rid}"
        out = repo / RAW / "api" / kind / path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, default=str))  # as extract writes Describe* responses: timestamps as ISO strings
    api = QuickSightSource().parse(RawStore(repo, "quicksight"))
    _, bundle = parse(tmp_path / "b")
    assert api.to_json() == bundle.to_json()


# ------------------------------------------------------------------ extract (botocore Stubber)


def _stubbed():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    client = boto3.client("quicksight", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    return client, Stubber(client)


def _arn(kind: str, rid: str) -> str:
    return f"arn:aws:quicksight:us-east-1:{ACCOUNT}:{kind}/{rid}"


def _lists(stub) -> None:
    acct = {"AwsAccountId": ACCOUNT}
    stub.add_response("list_dashboards", {"DashboardSummaryList": [
        {"Arn": _arn("dashboard", "paper-sales"), "DashboardId": "paper-sales", "Name": "Paper sales"}], "NextToken": "p2"}, acct)
    stub.add_response("list_dashboards", {"DashboardSummaryList": []}, {**acct, "NextToken": "p2"})
    stub.add_response("list_analyses", {"AnalysisSummaryList": [
        {"Arn": _arn("analysis", "paper-sales-analysis"), "AnalysisId": "paper-sales-analysis", "Name": "Paper sales analysis",
         "Status": "CREATION_SUCCESSFUL"},
        {"Arn": _arn("analysis", "old"), "AnalysisId": "old", "Name": "Old", "Status": "DELETED"}]}, acct)
    stub.add_response("list_data_sets", {"DataSetSummaries": [
        {"Arn": _arn("dataset", "order-lines"), "DataSetId": "order-lines", "Name": "Order lines", "ImportMode": "DIRECT_QUERY"}]}, acct)
    stub.add_response("list_data_sources", {"DataSources": [
        {"Arn": _arn("datasource", "dunder-mifflin"), "DataSourceId": "dunder-mifflin", "Name": "DM", "Type": "ATHENA"}]}, acct)


def _identity(stub) -> None:
    acct = {"AwsAccountId": ACCOUNT, "Namespace": "default"}
    stub.add_response("list_users", {"UserList": [{"UserName": "pbeesly", "Email": "pam@example.com", "Role": "AUTHOR"}]}, acct)
    stub.add_response("list_groups", {"GroupList": [{"GroupName": "Sales", "Arn": _arn("group", "default/Sales")}]}, acct)
    stub.add_response("list_group_memberships", {"GroupMemberList": [{"MemberName": "pbeesly"}]}, {**acct, "GroupName": "Sales"})


def test_inventory_with_stubber():
    client, stub = _stubbed()
    _lists(stub)
    with stub:
        inv = QuickSightSource(client=client, account=ACCOUNT, region="us-east-1").inventory()
    items = {i.id: i for i in inv.items}
    assert set(items) == {"dashboard:paper-sales", "analysis:paper-sales-analysis", "dataset:order-lines", "datasource:dunder-mifflin"}
    assert items["dashboard:paper-sales"].url == "https://us-east-1.quicksight.aws.amazon.com/sn/dashboards/paper-sales"
    assert items["dashboard:paper-sales"].views is None and "no view counts" in inv.notes[0]


def test_extract_asset_bundle_with_stubber(tmp_path: Path):
    from botocore.stub import ANY

    client, stub = _stubbed()
    _lists(stub)
    arns = [_arn("dashboard", "paper-sales"), _arn("analysis", "paper-sales-analysis"), _arn("dataset", "order-lines"),
            _arn("datasource", "dunder-mifflin")]
    stub.add_response("start_asset_bundle_export_job", {"AssetBundleExportJobId": "j", "Status": 202}, {
        "AwsAccountId": ACCOUNT, "AssetBundleExportJobId": ANY, "ResourceArns": arns, "IncludeAllDependencies": True,
        "ExportFormat": "QUICKSIGHT_JSON", "IncludePermissions": True, "IncludeTags": True})
    stub.add_response("describe_asset_bundle_export_job", {"JobStatus": "IN_PROGRESS"}, {"AwsAccountId": ACCOUNT, "AssetBundleExportJobId": ANY})
    stub.add_response("describe_asset_bundle_export_job", {"JobStatus": "SUCCESSFUL", "DownloadUrl": "https://s3.example/bundle.qs",
                                                           "Warnings": []}, {"AwsAccountId": ACCOUNT, "AssetBundleExportJobId": ANY})
    _identity(stub)
    sleeps, urls = [], []
    src = QuickSightSource(client=client, account=ACCOUNT, region="us-east-1", sleep=sleeps.append,
                           download=lambda url: urls.append(url) or fixture_qs())
    store = RawStore(tmp_path, "quicksight")
    with stub:
        src.extract(store)
    stub.assert_no_pending_responses()
    store.save()
    assert sleeps == [src.POLL_SECONDS] and urls == ["https://s3.example/bundle.qs"]
    assert store.get_json("account.json") == {"account": ACCOUNT, "region": "us-east-1", "namespace": "default"}
    assert store.exists("bundle/dashboard/paper-sales.json") and store.get_json("export_job.json")["JobStatus"] == "SUCCESSFUL"
    assert store.get_json("identity/groups.json")[0]["Members"] == [{"MemberName": "pbeesly"}]
    assert store.get_json("_errors.json") == [] and not store.glob("api/**/*.json")
    assert len(QuickSightSource().parse(store).dashboards) == 2


def test_extract_falls_back_to_describe_calls(tmp_path: Path):
    from botocore.stub import ANY

    model = _model()
    client, stub = _stubbed()
    _lists(stub)
    stub.add_client_error("start_asset_bundle_export_job", "AccessDeniedException", "not authorized", 401)

    def api(kind: str, rid: str) -> dict:
        raw = json.loads((FIXTURES / "bundle" / kind / f"{rid}.json").read_text())
        raw.pop("resourceType")
        return _to_model(raw, model.operation_model(CREATE[kind]).input_shape)

    perms = lambda doc: {"Permissions": doc.pop("Permissions", [])}  # noqa: E731
    dash = api("dashboard", "paper-sales")
    p = perms(dash)
    stub.add_response("describe_dashboard_definition", {"DashboardId": "paper-sales", "Name": dash["Name"], "Definition": dash["Definition"]},
                      {"AwsAccountId": ACCOUNT, "DashboardId": "paper-sales"})
    stub.add_response("describe_dashboard_permissions", p, {"AwsAccountId": ACCOUNT, "DashboardId": "paper-sales"})
    analysis = api("analysis", "paper-sales-analysis")
    p = perms(analysis)
    stub.add_response("describe_analysis_definition", {"AnalysisId": "paper-sales-analysis", "Name": analysis["Name"],
                                                       "Definition": analysis["Definition"]},
                      {"AwsAccountId": ACCOUNT, "AnalysisId": "paper-sales-analysis"})
    stub.add_response("describe_analysis_permissions", p, {"AwsAccountId": ACCOUNT, "AnalysisId": "paper-sales-analysis"})
    for did in ("order-lines", "shipments", "region-entitlements"):  # the selected dataset, then the dashboards', then the RLS rules
        ds = api("dataset", did)
        p = perms(ds)
        ds = {k: v for k, v in ds.items() if k not in ("Tags", "FolderArns")}
        stub.add_response("describe_data_set", {"DataSet": {"Arn": _arn("dataset", did), **ds}}, {"AwsAccountId": ACCOUNT, "DataSetId": did})
        stub.add_response("describe_data_set_permissions", p, {"AwsAccountId": ACCOUNT, "DataSetId": did})
    source = api("datasource", "dunder-mifflin")
    p = perms(source)
    source.pop("Tags")
    stub.add_response("describe_data_source", {"DataSource": {"Arn": _arn("datasource", "dunder-mifflin"), **source}},
                      {"AwsAccountId": ACCOUNT, "DataSourceId": "dunder-mifflin"})
    stub.add_client_error("describe_data_source_permissions", "AccessDeniedException", "no", 401,
                          expected_params={"AwsAccountId": ACCOUNT, "DataSourceId": ANY})
    _identity(stub)
    store = RawStore(tmp_path, "quicksight")
    with stub:
        QuickSightSource(client=client, account=ACCOUNT, region="us-east-1").extract(store)
    stub.assert_no_pending_responses()
    store.save()
    errors = store.get_json("_errors.json")
    assert [e["item"] for e in errors] == ["asset bundle export", "api/datasource/dunder-mifflin.json permissions"]
    assert "AccessDenied" in errors[0]["error"] and not store.glob("bundle/**/*.json")
    assert sorted(store.glob("api/*/*.json")) == [
        "api/analysis/paper-sales-analysis.json", "api/dashboard/paper-sales.json", "api/dataset/order-lines.json",
        "api/dataset/region-entitlements.json", "api/dataset/shipments.json", "api/datasource/dunder-mifflin.json"]
    b = QuickSightSource().parse(store)
    assert {d.slug for d in b.dashboards} == {"paper-sales", "paper-sales-analysis"} and len(b.row_filters) == 2
    assert any("extract failed" in w.message for w in b.warnings)


def test_normalize_keeps_ids_and_matches_any_casing():
    doc = normalize({"kpiVisual": {"visualId": "v"}, "physicalTableMap": {"orders-pt": {"relationalTable": {"name": "orders"}}},
                     "fieldFolders": {"Revenue": {"columns": ["a"]}}, "somethingNew": {"innerKey": 1}})
    assert doc == {"KPIVisual": {"VisualId": "v"}, "PhysicalTableMap": {"orders-pt": {"RelationalTable": {"Name": "orders"}}},
                   "FieldFolders": {"Revenue": {"columns": ["a"]}}, "SomethingNew": {"InnerKey": 1}}
    assert normalize({"KPIVISUAL": {}}) == {"KPIVisual": {}}


def test_datasets_unshackle_cannot_model(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def data_prep(d):
        d.pop("logicalTableMap")
        d["dataPrepConfiguration"] = {"sourceTableMap": {}, "transformStepMap": {}, "destinationTableMap": {}}
    edit(repo, "dataset/shipments.json", data_prep)

    def s3_and_nested(d):
        d["physicalTableMap"]["files-pt"] = {"s3Source": {"dataSourceArn": "arn:aws:quicksight:us-east-1:1:datasource/s3", "inputColumns": [
            {"name": "target", "type": "DECIMAL"}]}}
        lt = d["logicalTableMap"]
        lt["files-lt"] = {"alias": "targets", "source": {"physicalTableId": "files-pt"}}
        lt["other-lt"] = {"alias": "other", "source": {"dataSetArn": "arn:aws:quicksight:us-east-1:1:dataset/other"}}
        lt["with-files"] = {"alias": "with files", "source": {"joinInstruction": {
            "leftOperand": "lt-order-lines", "rightOperand": "files-lt", "type": "LEFT", "onClause": "{order_id} = {target}"}}}
        lt["with-other"] = {"alias": "with other", "source": {"joinInstruction": {
            "leftOperand": "with-files", "rightOperand": "other-lt", "type": "LEFT", "onClause": "{order_id} = {order_id}"}}}
    edit(repo, "dataset/order-lines.json", s3_and_nested)
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    msgs = {w.message for w in b.warnings if w.item.startswith("dataset ")}
    assert any("DataPrepConfiguration" in m for m in msgs)
    assert any("files-pt (S3Source) is read by QuickSight itself" in m for m in msgs)
    assert any("reads another dataset (other)" in m for m in msgs)
    assert "shipments" not in {m.name for m in b.semantic_models}
    tile = next(t for t in b.dashboards[0].tiles if t.id == "total_revenue")
    assert not tile.sql and "files-pt (S3Source) is not in the warehouse" in tile.todo and "other-lt has no warehouse source" in tile.todo
    assert not any("has no unique key" in w.message for w in b.warnings)


def test_row_level_security_in_the_semantic_model_configuration(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)

    def move(d):
        rls = d.pop("rowLevelPermissionDataSet")
        d["semanticModelConfiguration"] = {"tableMap": {"t": {"alias": "Shipments", "destinationTableId": "x",
                                                              "rowLevelPermissionConfiguration": {"rowLevelPermissionDataSet": rls}}}}
    edit(repo, "dataset/shipments.json", move)
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    assert next(r for r in b.row_filters if r.name == "shipments_region_entitlements").predicate.startswith("(shipping_region in (")


# ------------------------------------------------------------------ what the bundle format looks like


def test_export_timestamps_and_text_boxes():
    """The bundle writes Timestamp members as epoch milliseconds and indents text box markup."""
    from unshackle.sources.quicksight import _plain, _timestamp

    assert _timestamp(1735689600000) == _timestamp("1735689600000") == "2025-01-01 00:00:00"
    assert _timestamp("2025-01-01T00:00:00Z") == _timestamp("2025-01-01 00:00:00+00:00") == "2025-01-01 00:00:00"
    assert _plain("<text-box>\n  Revenue is\u00a0\n  <b>post-discount</b>\n  . Done.<br/>Next\n</text-box>") == \
        "Revenue is post-discount. Done.\nNext"


def test_datetime_parameter_default_from_the_bundle(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    edit(repo, "dashboard/paper-sales.json", lambda d: d["definition"]["parameterDeclarations"].append(
        {"dateTimeParameterDeclaration": {"name": "Since", "defaultValues": {"staticValues": [1735689600000]}}}))
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    d = next(x for x in b.dashboards if x.slug == "paper-sales")
    assert ("since", "2025-01-01 00:00:00", "date") in [(f.name, f.default, f.kind) for f in d.filters]


def test_filter_list_operators(tmp_path: Path):
    """A FilterListConfiguration only takes CONTAINS (keep the listed values) and DOES_NOT_CONTAIN (drop them):
    equality, not substrings. The fixture's select-all list with a control is CONTAINS and binds the control."""
    raw = json.loads((FIXTURES / "bundle/dashboard/paper-sales.json").read_text())
    conf = next(fg for fg in raw["definition"]["filterGroups"] if fg["filterGroupId"] == "fg-shipping-mode")["filters"][0]["categoryFilter"]
    assert conf["configuration"]["filterListConfiguration"]["matchOperator"] == "CONTAINS"
    repo = repo_with_fixtures(tmp_path)

    def exclude(d):
        for fg in d["definition"]["filterGroups"]:
            if fg["filterGroupId"] == "fg-shipping-mode":
                fg["filters"][0]["categoryFilter"]["configuration"]["filterListConfiguration"].update(
                    matchOperator="DOES_NOT_CONTAIN", categoryValues=["EXPRESS"], selectAllOptions=None)
        for sheet in d["definition"]["sheets"]:
            sheet["filterControls"] = []
    edit(repo, "dashboard/paper-sales.json", exclude)
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    bar = next(t for d in b.dashboards if d.slug == "paper-sales" for t in d.tiles if t.id == "revenue_by_region")
    assert "shipping_mode NOT IN ('EXPRESS')" in bar.sql and "LIKE" not in bar.sql


def test_dataset_filter_marked_disabled_in_the_bundle_still_applies(tmp_path: Path):
    """An asset bundle can carry `enabled: false` (not in the API model) on a FilterOperation that QuickSight applies,
    so the flag can't tell an active filter from a switched-off one. parse() ignores it and keeps the filter."""
    repo = repo_with_fixtures(tmp_path)

    def flag(d):
        for op in d["logicalTableMap"]["lt-order-lines"]["dataTransforms"]:
            if "filterOperation" in op:
                op["filterOperation"].update(enabled=False, inverse=False)
    edit(repo, "dataset/order-lines.json", flag)
    b = QuickSightSource().parse(RawStore(repo, "quicksight"))
    kpi = next(t for d in b.dashboards if d.slug == "paper-sales" for t in d.tiles if t.id == "total_revenue")
    assert "WHERE t1.status <> 'VOID'" in kpi.sql
    assert "{status} <> 'VOID'" in next(m for m in b.semantic_models if m.name == "orders").todo
