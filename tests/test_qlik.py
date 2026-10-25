from conftest import notebook_source
import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from unshackle import build
from unshackle.http import Client, SourceError
from unshackle.raw import RawStore
from unshackle.sources import _manual, qlik
from unshackle.sources.qlik import (
    QlikSource,
    Untranslatable,
    inline_table,
    parse_expr,
    parse_script,
    parse_set,
    row_sql,
    set_predicate,
)

FIXTURES = Path(__file__).parent / "fixtures" / "qlik"  # a real Qlik Cloud extract (tests/live/test_qlik_live.py); see README
APP = "00000000-0000-4000-8000-000000000001"
RAW = Path("migration/raw/qlik")
DATA = Path(__file__).parent / "live" / "data" / "dunder_mifflin"


def repo_with_fixtures(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES, tmp_path / RAW, ignore=shutil.ignore_patterns("README.md"))
    return tmp_path


def parse(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    return repo, QlikSource().parse(RawStore(repo, "qlik"))


def col(f: str) -> str:
    return f"t.{f}"


# ------------------------------------------------------------ expressions


def test_expressions_to_sql():
    sql = lambda text, **kw: row_sql(parse_expr(text, kw.pop("variables", None)), col, **kw)  # noqa: E731
    assert sql("If(rating >= 4, 'Positive', 'Negative')") == "CASE WHEN t.rating >= 4 THEN 'Positive' ELSE 'Negative' END"
    assert sql("=MonthStart([purchased_at])") == "DATE_TRUNC('month', t.purchased_at)"
    assert sql("Upper(paper_type)") == "UPPER(t.paper_type)"
    assert sql("price * (1 - discount) / 2") == "1.0 * (t.price * (1 - t.discount)) / nullif(2, 0)"
    assert sql("If(status = 'A' and not IsNull(x), 1)") == "CASE WHEN (t.status = 'A') AND NOT (t.x IS NULL) THEN 1 ELSE NULL END"
    assert sql("\"Order Date\" // a comment\n + 1") == "t.Order Date + 1"
    assert sql("$(vField) * 2", variables={"vField": "quantity"}) == "t.quantity * 2"
    for text, why in (("status = 'A'", "comparison used as a number"), ("a & b", "operator '&'"), ("Month(d)", r"month\(\)"),
                      ("If(name < 'M', 1, 0)", "ordering comparison"), ("Aggr(Sum(x), y)", "Aggr()"),
                      ("$(=Max(Year))", "dynamic dollar expansion"), ("$(vMissing)", "not defined"),
                      ("$(vCalc)", "calculated expression")):
        with pytest.raises(Untranslatable, match=why):
            sql(text, variables={"vCalc": "=Sum(x)"})


def test_set_analysis():
    assert parse_set(None) == ([], False, [])
    filters, ignores, cleared = parse_set("<status={'COMPLETED','RETURNED'}, Year={2025}, Region-={'EU'}, Month=>")
    assert not ignores and cleared == ["Month"]
    preds = [set_predicate(f, col) for f in filters]
    assert preds == ["t.status IN ('COMPLETED', 'RETURNED')", "t.Year = 2025", "t.Region <> 'EU'"]
    (f,), ignores, _ = parse_set("1<status={completed}>")  # unquoted: Qlik matches the whole value, ignoring case
    assert ignores and set_predicate(f, col) == "UPPER(t.status) = 'COMPLETED'"
    assert set_predicate(parse_set('$<status={"done"}>')[0][0], col) == "UPPER(t.status) = 'DONE'"
    assert parse_set("1") == ([], True, [])
    for raw, why in (("<Year={\">2020\"}>", "search strings"), ("<status={'A'}>*<x={'b'}>", "set modifier"),
                     ("State1<x={'a'}>", "alternate states"), ("<Year=P(Year)>", "implicit field value"),
                     ("<Year+={2024}>", "combines with the current selection"), ("<Year={\"*\"}>", "search strings")):
        with pytest.raises(Untranslatable, match=why):
            parse_set(raw)


def test_parser_reads_qualifiers_and_rejects_garbage():
    node = parse_expr("Count({<status={'A'}>} DISTINCT TOTAL <account_id> order_id)")
    assert (node.name, node.distinct, node.total, node.set) == ("count", True, True, "<status={'A'}>")
    with pytest.raises(Untranslatable):
        parse_expr("Sum(x")
    with pytest.raises(Untranslatable):
        parse_expr("")


# ------------------------------------------------------------ load script


def test_load_script_statements():
    script = parse_script(r"""
///$tab Main
SET vStatus = 'COMPLETED';
LET vToday = Today();
REM a remark;
LIB CONNECT TO 'Snowflake_prod';
/* block comment; with a semicolon */
Orders:
LOAD order_id, amount AS revenue, If(amount > 100, 'big', 'small') AS size;
SQL SELECT order_id, amount FROM "ANALYTICS"."PUBLIC"."ORDERS";
Customers:
LOAD * ;
SELECT * FROM analytics.public.customers WHERE active = 1;
LOAD Customer, Region FROM [lib://Data:DataFiles/Regions.xlsx] (ooxml, embedded labels, table is Sheet1);  // a comment
Concatenate (Orders) LOAD order_id, amount AS revenue FROM [lib://Data:DataFiles/Orders 2023.qvd] (qvd);
Map: Mapping LOAD a, b INLINE [a, b
1, 2];
QUALIFY *;
Q: LOAD a FROM [lib://x/q.csv] (txt);
UNQUALIFY *;
FOR i = 1 TO 3
NEXT i
Section Access;
LOAD * INLINE [ACCESS, USERID
ADMIN, DM\MICHAEL];
Section Application;
""")
    assert script.variables == {"vStatus": "COMPLETED", "vToday": "=Today()"}
    assert [ld.name for ld in script.loads] == ["Orders", "Customers", "Regions", "Orders 2023", "Map", "Q"]
    assert script.loads[5].problems == ["QUALIFY prefixes field names with the table name"]
    orders = script.loads[0]
    assert (orders.name, orders.source, orders.path, orders.problems) == ("Orders", "sql", "ANALYTICS.PUBLIC.ORDERS", [])
    assert orders.fields == [("order_id", "order_id"), ("amount", "revenue"), ("If(amount > 100, 'big', 'small')", "size")]
    customers = script.loads[1]
    assert customers.star and customers.problems == ["SQL SELECT with more than a column list from one table"]
    regions = script.loads[2]
    assert (regions.name, regions.source, regions.path) == ("Regions", "file", "lib://Data:DataFiles/Regions.xlsx")
    concat = script.loads[3]
    assert concat.target == "Orders" and any("QVD" in p for p in concat.problems)
    assert script.loads[4].source == "mapping"
    assert any(n.startswith("control statement FOR") for n in script.notes)
    assert len(script.section_access) == 1 and inline_table(script.section_access[0]) == (["ACCESS", "USERID"], [["ADMIN", "DM\\MICHAEL"]])


def test_inline_table_delimiter():
    assert inline_table("LOAD * INLINE [a;b\n1;2] (delimiter is ';')") == (["a", "b"], [["1", "2"]])
    assert inline_table("LOAD * INLINE [A, B\n 1 , 2\n\n]") == (["A", "B"], [["1", "2"]])
    assert inline_table("LOAD * FROM x.qvd (qvd)") is None


# ------------------------------------------------------------ the fixture app


def test_parse_semantic_layer(tmp_path: Path):
    _, b = parse(tmp_path)
    models = {m.name: m for m in b.semantic_models}
    assert list(models) == ["accounts", "products", "orders", "shippings", "reviews", "monthly_targets"]
    assert {t.name for t in b.tables} == set(models)
    orders = models["orders"]
    assert orders.table == "orders" and not orders.todo
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [
        ("orders", "primary", "order_id"), ("shippings", "foreign", "shipping_id"), ("accounts", "foreign", "account_id"),
        ("products", "foreign", "product_id")]
    dims = {d.name: d for d in orders.dimensions}
    assert dims["status"].label == "Order status" and dims["purchased_at"].type == "time"
    month = dims["purchase_month"]  # a master dimension with a calculated field definition
    assert (month.type, month.expr, month.time_granularity, month.label) == ("time", "DATE_TRUNC('month', purchased_at)", "month", "Purchase month")
    products = {d.name: d for d in models["products"].dimensions}
    assert products["paper_type"].expr == "" and products["paper_type_2"].expr == "UPPER(paper_type)"  # PAPER_TYPE
    reviews = {d.name: d for d in models["reviews"].dimensions}
    assert reviews["sentiment"].expr == "CASE WHEN rating >= 4 THEN 'Positive' ELSE 'Negative' END"
    assert [e.name for e in models["reviews"].entities] == ["reviews", "orders"]  # reviews.order_id -> orders, one row each
    assert models["monthly_targets"].todo.startswith("the load script builds this table in a way unshackle doesn't translate")
    assert "RESIDENT orders" in next(w.message for w in b.warnings if w.item == "monthly_targets")
    assert next(w for w in b.warnings if w.item == "orders").severity == "info"  # a file load: land it

    metrics = {m.name: m for m in b.metrics}
    assert {n: (m.type, m.measure, m.filter) for n, m in metrics.items() if m.type == "simple" and not m.todo} == {
        "orders": ("simple", "orders", ""), "revenue": ("simple", "revenue", ""),
        "completed_revenue": ("simple", "completed_revenue", "{{ Dimension('orders__status') }} = 'COMPLETED'"),
        "completed_orders": ("simple", "completed_orders", "UPPER({{ Dimension('orders__status') }}) = 'COMPLETED'"),  # $(vCompletedStatus)
        "revenue_outside_the_west": ("simple", "revenue_outside_the_west", "{{ Dimension('shippings__shipping_region') }} <> 'West'"),
    }
    aov = metrics["average_order_value"]  # reuses the master measures it divides
    assert (aov.type, aov.numerator, aov.denominator) == ("ratio", "revenue", "orders")
    target = metrics["revenue_vs_target"]
    assert (target.type, target.expr, target.metrics) == ("derived", "1.0 * revenue / nullif(1000000, 0)", ["revenue"])
    assert metrics["average_revenue_per_account"].todo.startswith("Qlik expression: Aggr()")
    assert metrics["average_revenue_per_account"].source_expression == "Avg(Aggr(Sum(post_discount_revenue), account_id))"
    assert metrics["share_of_revenue"].todo.startswith("Qlik expression: TOTAL")
    measures = {m.name: (m.agg, m.expr) for m in orders.measures if not m.todo}
    assert measures["orders"] == ("count_distinct", "order_id") and measures["revenue"] == ("sum", "post_discount_revenue")


def test_parse_dashboards(tmp_path: Path):
    _, b = parse(tmp_path)
    dashes = {d.slug: d for d in b.dashboards}
    assert list(dashes) == ["sales-overview", "orders-detail"]
    overview = dashes["sales-overview"]
    assert overview.url == f"https://example.eu.qlikcloud.com/sense/app/{APP}/sheet/id000033" and overview.views == 1
    assert overview.layout == [["revenue", "orders"], ["revenue_by_account_manager"], ["monthly_revenue"]]
    assert [(f.name, f.field, f.kind) for f in overview.filters] == [("status", "status", "value"), ("paper_type", "paper_type", "value")]
    tiles = {t.id: t for d in b.dashboards for t in d.tiles}
    kpi = tiles["revenue"]
    assert (kpi.kind, kpi.mark, kpi.encodings["text"].field) == ("kpi", "text", "revenue")
    assert kpi.sql == ("SELECT\n  SUM(orders.post_discount_revenue) AS revenue\nFROM orders\n"
                       "LEFT JOIN products ON products.product_id = orders.product_id\n"
                       "WHERE (orders.status = :status OR :status IS NULL)\n  AND (products.paper_type = :paper_type OR :paper_type IS NULL)")
    bar = tiles["revenue_by_account_manager"]
    assert (bar.mark, bar.encodings["x"].field, bar.encodings["y"].field, bar.encodings["x"].title) == (
        "bar", "account_manager", "revenue", "Account manager")
    assert "LEFT JOIN accounts ON accounts.account_id = orders.account_id" in bar.sql and bar.sql.endswith("GROUP BY 1\nORDER BY 1")
    line = tiles["monthly_revenue"]
    assert line.mark == "line" and line.encodings["x"].type == "temporal" and [l.encodings["y"].field for l in line.layers] == [
        "revenue", "completed_revenue"]
    assert "SUM(CASE WHEN orders.status = 'COMPLETED' THEN orders.post_discount_revenue END) AS completed_revenue" in line.sql
    assert "ignores selections on status" in line.todo  # a set modifier on a filtered field
    table = tiles["orders_by_status_and_paper_type"]
    assert table.kind == "table" and "1.0 * SUM(orders.post_discount_revenue) / nullif(COUNT(DISTINCT orders.order_id), 0)" in table.sql
    pie = tiles["revenue_by_shipping_region"]
    assert pie.mark == "arc" and set(pie.encodings) == {"theta", "color"} and "LEFT JOIN shippings" in pie.sql
    aggr = tiles["average_revenue_per_account_by_region"]
    assert aggr.sql == "" and aggr.todo.startswith("Qlik expression not translated: Aggr()")
    assert aggr.encodings["y"].field == "shipping_region"  # horizontal bars


def test_section_access(tmp_path: Path):
    _, b = parse(tmp_path)
    rf = {r.name: r for r in b.row_filters}
    assert len(rf) == 20  # four users x the five tables associated with PAPER_TYPE; monthly_targets is an island
    jim = rf["orders_dm_jim"]
    assert jim.groups == ["DM\\JIM"] and jim.table == "orders"
    assert jim.predicate == "product_id IN (SELECT product_id FROM products WHERE UPPER(paper_type) IN ('COPY', 'PRINTER'))"
    assert rf["products_dm_jim"].predicate == "UPPER(paper_type) IN ('COPY', 'PRINTER')"
    assert rf["accounts_dm_jim"].predicate.startswith("account_id IN (SELECT account_id FROM orders WHERE product_id IN (")
    assert rf["shippings_dm_angela"].predicate.endswith("UPPER(paper_type) IN ('CARDSTOCK')))")
    admin = rf["products_dm_michael"]  # ADMIN with *: every value listed in the column (Qlik reduces ADMIN too)
    assert admin.predicate == "UPPER(paper_type) IN ('CARDSTOCK', 'COPY', 'KRAFT ROLL', 'MULTIPURPOSE', 'PRINTER')"
    assert all("USERID values are Qlik user ids" in r.todo for r in rf.values())
    omit = [p for p in b.permissions if p.resource_type == "Column"]
    assert [(p.principal, p.resource_id, p.effect) for p in omit] == [("user:DM\\DWIGHT", "orders.order_contact_email", "forbid")]
    deny = [p for p in b.permissions if p.resource_type in ("Notebook", "Table")]
    assert {p.resource_id for p in deny} == {"sales-overview", "orders-detail", "accounts", "products", "orders", "shippings",
                                             "reviews", "monthly_targets"}
    assert all(p.principal == "*" and p.effect == "forbid" and 'principal == User::"DM\\\\JIM"' in p.condition for p in deny)


def test_section_access_variants(tmp_path: Path):
    repo = repo_with_fixtures(tmp_path)
    script = repo / RAW / "apps" / APP / "script.qvs"
    text = script.read_text()
    start, end = text.index("Section Access;"), text.index("Section Application;")
    script.write_text(text[:start] + "Section Access;\nLOAD * INLINE [\nACCESS, GROUP, PAPER_TYPE\nUSER, Sales, COPY\nUSER, *, PRINTER\n];\n"
                      + text[end:])
    b = QlikSource().parse(RawStore(repo, "qlik"))
    rf = {r.name: r for r in b.row_filters}
    assert rf["products_sales"].groups == ["Sales"] and not rf["products_sales"].todo
    assert rf["products_everyone"].groups == ["*"] and rf["products_everyone"].predicate == "UPPER(paper_type) IN ('PRINTER')"
    assert not [p for p in b.permissions if p.resource_type == "Notebook"]  # * lets every user open the app
    script.write_text(text[:start] + "Section Access;\nLOAD * FROM [lib://Data:DataFiles/access.qvd] (qvd);\n" + text[end:])
    (only,) = QlikSource().parse(RawStore(repo, "qlik")).row_filters
    assert only.todo.startswith("Section Access loaded from a file") and not only.predicate


def test_build_outputs(tmp_path: Path):
    repo, b = parse(tmp_path)
    build.apply(b, repo)
    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    metrics = {m["name"]: m["expression"]["dialects"][0]["expression"] for m in ossie["datasets"][0]["metrics"]}
    assert metrics == {
        "orders": "COUNT(DISTINCT order_id)", "revenue": "SUM(post_discount_revenue)",
        "completed_revenue": "SUM(CASE WHEN status = 'COMPLETED' THEN post_discount_revenue END)",
        "completed_orders": "COUNT(DISTINCT CASE WHEN UPPER(status) = 'COMPLETED' THEN order_id END)",
        "revenue_vs_target": "1.0 * SUM(post_discount_revenue) / nullif(1000000, 0)",
        "average_order_value": "1.0 * SUM(post_discount_revenue) / nullif(COUNT(DISTINCT order_id), 0)"}
    (west,) = ossie["metrics"]
    assert west["expression"]["dialects"][0]["expression"] == (
        "SUM(CASE WHEN shippings.shipping_region <> 'West' THEN orders.post_discount_revenue END)")
    text = (repo / "semantic/orders.ossie.yaml").read_text()
    assert "TODO(unshackle): metric average_revenue_per_account" in text and "TODO(unshackle): metric share_of_revenue" in text
    nb = notebook_source(repo / "notebooks/sales-overview.ipynb")
    assert "status = None" in nb and "params={'status': status, 'paper_type': paper_type}" in nb
    assert "TODO(unshackle)" in notebook_source(repo / "notebooks/orders-detail.ipynb")
    rows = yaml.safe_load((repo / "policies/row_filters.qlik.yml").read_text())["row_filters"]
    assert {r["table"] for r in rows} == {f"warehouse.main.{t}" for t in ("accounts", "products", "orders", "shippings", "reviews")}
    cedar = (repo / "policies/qlik.cedar").read_text()
    assert 'resource in Column::"warehouse.main.orders.order_contact_email"' in cedar and cedar.count("forbid (") == 9
    script = (repo / "knowledge/dunder-mifflin-sales-load-script.md").read_text()
    assert "LOAD * FROM [lib://unshackle-live:DataFiles/orders.parquet]" in script and "DM\\JIM" not in script  # access rows stay out
    assert "status (selected values" in (repo / "knowledge/dunder-mifflin-sales-bookmarks.md").read_text()


def test_tiles_and_metrics_return_qlik_numbers_on_duckdb(tmp_path: Path):
    """The live test's Qlik results, recomputed from the fixture's SQL on the same data in DuckDB."""
    import duckdb

    from unshackle.run import notebook_queries, query

    repo, b = parse(tmp_path)
    build.apply(b, repo)
    db = tmp_path / "dunder.duckdb"
    con = duckdb.connect(str(db))
    for t in ("accounts", "products", "orders", "shippings", "reviews"):
        con.execute(f"create table {t} as select * from read_parquet('{DATA / t}.parquet')")
    con.close()
    url = f"duckdb:///{db}"
    params, sqls = notebook_queries(repo / "notebooks/sales-overview.ipynb")
    assert query(sqls["revenue"], params, url)["revenue"].tolist() == [pytest.approx(313685779.95)]
    assert query(sqls["orders"], params, url)["orders"].tolist() == [3503]
    managers = query(sqls["revenue_by_account_manager"], {**params, "status": "COMPLETED"}, url)
    assert dict(zip(managers["account_manager"], managers["revenue"]))["Kevin Malone"] == pytest.approx(14519934.86)
    monthly = query(sqls["monthly_revenue"], params, url)
    assert len(monthly) == 34 and monthly["completed_revenue"].sum() == pytest.approx(201016932.41)
    _, detail = notebook_queries(repo / "notebooks/orders-detail.ipynb")
    regions = query(detail["revenue_by_shipping_region"], {}, url)
    assert regions[regions["shipping_region"].isna()]["revenue"].tolist() == [pytest.approx(73156717.21)]  # Qlik's null row
    rf = {r.name: r.predicate for r in b.row_filters}
    assert query(f"select sum(post_discount_revenue) as r from orders where {rf['orders_dm_jim']}", {}, url)["r"].tolist() == [
        pytest.approx(196857073.65)]
    assert query(f"select count(*) as n from accounts where {rf['accounts_dm_jim']}", {}, url)["n"].tolist() == [179]


# ------------------------------------------------------------ data model sources


def test_rest_metadata_fallback_is_conservative(tmp_path: Path):
    """Without GetTablesAndKeys a field counts as unique only where distinct values = rows of the table."""
    repo = repo_with_fixtures(tmp_path)
    app = repo / RAW / "apps" / APP
    tk = json.loads((app / "tables-and-keys.json").read_text())
    (app / "tables-and-keys.json").unlink()
    fields: dict[str, dict] = {}
    for t in tk["qtr"]:
        for f in t["qFields"]:
            fields.setdefault(f["qName"], {"name": f["qName"], "src_tables": [], "cardinal": f["qnTotalDistinctValues"], "tags": f["qTags"]})
            fields[f["qName"]]["src_tables"].append(t["qName"])
    (app / "data-metadata.json").write_text(json.dumps({"tables": [{"name": t["qName"], "no_of_rows": t["qNoOfRows"]} for t in tk["qtr"]]
                                                        + [{"name": "$$SysTable 1", "no_of_rows": 3}], "fields": list(fields.values())}))
    b = QlikSource().parse(RawStore(repo, "qlik"))
    orders = next(m for m in b.semantic_models if m.name == "orders")
    assert "shippings" not in [e.name for e in orders.entities]  # 3503 shipping ids, 2812 shipping rows: not provably unique
    assert {m.name for m in b.semantic_models} == {"accounts", "products", "orders", "shippings", "reviews", "monthly_targets"}
    metrics = {m.name: m for m in b.metrics}
    assert not metrics["revenue"].todo and metrics["revenue_outside_the_west"].todo


def test_sql_loads_and_warehouse_detection(tmp_path: Path):
    store = RawStore(tmp_path, "qlik")
    store.put_text("apps/a1/script.qvs", 'LIB CONNECT TO \'Snowflake\';\nORDERS:\nLOAD ORDER_ID, AMOUNT, STATUS, ApplyMap(\'Names\', STATUS) AS STATUS_NAME;\n'
                                         'SQL SELECT ORDER_ID, AMOUNT, STATUS FROM "ANALYTICS"."PUBLIC"."ORDERS";\n'
                                         'BIG:\nSQL SELECT * FROM ANALYTICS.PUBLIC.ORDERS WHERE AMOUNT > 100;\n')
    store.put_text("apps/a1/connections.yml", yaml.safe_dump({"connections": {"Snowflake": {
        "type": "QvOdbcConnectorPackage.exe", "connectionstring": 'CUSTOM CONNECT TO "provider=QvOdbcConnectorPackage.exe;driver=snowflake;host=x;"'}}}))
    store.put_json("apps/a1/measures.json", [{"qInfo": {"qId": "m1", "qType": "measure"}, "qMetaDef": {"title": "Revenue"},
                                              "qMeasure": {"qDef": "Sum(AMOUNT)", "qLabel": "Revenue"}}])
    b = QlikSource().parse(store)
    assert b.warehouse == "snowflake"
    models = {m.name: m for m in b.semantic_models}
    assert models["orders"].table == "ANALYTICS.PUBLIC.ORDERS" and not models["orders"].todo
    assert [m.expr for m in models["orders"].measures] == ['"AMOUNT"']
    name = next(d for d in models["orders"].dimensions if d.name == "status_name")
    assert name.todo.startswith("the load script computes STATUS_NAME") and name.source_expression == "ApplyMap('Names', STATUS)"
    assert models["big"].todo and "WHERE AMOUNT > 100" in b.queries[0].sql and "materialize" in b.queries[0].todo
    assert any("no data model" in w.message for w in b.warnings)


# ------------------------------------------------------------ manual import


def test_import_unbuild_folder(tmp_path: Path):
    unbuild = tmp_path / "unbuild"
    app = FIXTURES / "apps" / APP
    shutil.copytree(app, unbuild, ignore=shutil.ignore_patterns("tables-and-keys.json", "bookmarks.json"))
    (unbuild / "config.yml").write_text("script: script.qvs\nmeasures: measures.json\n")
    store = RawStore(tmp_path / "repo", "qlik")
    files = _manual.run(QlikSource(), store, [unbuild])
    assert "apps/dunder-mifflin-sales/script.qvs" in files and "apps/dunder-mifflin-sales/config.yml" not in files
    assert any(f.startswith("apps/dunder-mifflin-sales/objects/sheet-") for f in files)
    b = QlikSource().parse(RawStore(tmp_path / "repo", "qlik"))
    assert len(b.dashboards) == 2 and any("no data model" in w.message for w in b.warnings)
    metrics = {m.name: m for m in b.metrics}
    assert "not in the app's data model" in metrics["revenue"].todo  # orders is LOAD *: its columns are unknown
    with pytest.raises(SourceError, match="nothing Qlik Sense can use"):
        _manual.run(QlikSource(), store, [FIXTURES / "tenant.json"])


def test_import_raw_extract_folder(tmp_path: Path):
    files = QlikSource().import_path(FIXTURES)
    assert f"apps/{APP}/tables-and-keys.json" in files and "apps.json" in files and "README.md" not in files


# ------------------------------------------------------------ extract (fake engine)


class FakeEngine:
    """Answers the QIX calls extract makes from the fixture app."""

    def __init__(self, url: str, token: str, timeout: float = 300):
        assert url.startswith("wss://qlik.example.com/app/") and "/identity/" in url and token == "jwt"
        self.app = FIXTURES / "apps" / APP
        self.handles: dict[int, tuple[str, object]] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _new(self, kind: str, obj: object = None) -> dict:
        h = len(self.handles) + 1
        self.handles[h] = (kind, obj)
        return {"qReturn": {"qHandle": h}}

    handle = staticmethod(qlik.Engine.handle)

    def call(self, handle: int, method: str, *params):
        j = lambda rel: json.loads((self.app / rel).read_text())  # noqa: E731
        kind, obj = self.handles.get(handle, ("global", None))
        if method == "EngineVersion":
            return {"qVersion": {"qComponentVersion": "12.2969.0"}}
        if method == "OpenDoc":
            return self._new("doc", len(params) > 4 and params[4])
        if method == "GetTablesAndKeys":
            if obj is not False:
                raise AssertionError("tables and keys need the app opened with data")
            return j("tables-and-keys.json")
        if method == "GetAppProperties":
            return {"qProp": j("app-properties.json")}
        if method == "GetScript":
            return {"qScript": (self.app / "script.qvs").read_text()}
        if method == "GetConnections":
            return {"qConnections": [{"qName": "pg", "qType": "postgres", "qConnectionString": "CUSTOM CONNECT TO \"driver=postgres;\""}]}
        if method == "GetAllInfos":
            infos = [i["qInfo"] for rel in ("dimensions.json", "measures.json", "bookmarks.json") for i in j(rel)]
            infos += [json.loads(p.read_text())["qProperty"]["qInfo"] for p in (self.app / "objects").glob("*.json")]
            return {"qInfos": infos + [{"qId": "x", "qType": "appprops"}]}
        if method in ("GetDimension", "GetMeasure"):
            rel = "dimensions.json" if method == "GetDimension" else "measures.json"
            return self._new("prop", next(i for i in j(rel) if i["qInfo"]["qId"] == params[0]))
        if method == "GetVariableById":
            return self._new("prop", next(v for v in j("variables.json") if v["qInfo"]["qId"] == params[0]))
        if method == "GetProperties":
            return {"qProp": {k: v for k, v in obj.items() if k != "qIsScriptCreated"}}
        if method == "CreateSessionObject":
            return self._new("varlist")
        if method == "GetLayout" and kind == "varlist":
            return {"qLayout": {"qVariableList": {"qItems": [{"qInfo": v["qInfo"], "qIsScriptCreated": v.get("qIsScriptCreated", False)}
                                                             for v in j("variables.json")]}}}
        if method == "GetObject":
            return self._new("tree", next(json.loads(p.read_text()) for p in (self.app / "objects").glob("*.json")
                                          if json.loads(p.read_text())["qProperty"]["qInfo"]["qId"] == params[0]))
        if method == "GetFullPropertyTree":
            return {"qPropEntry": obj}
        if method == "GetBookmark":
            return self._new("bookmark", next(m for m in j("bookmarks.json") if m["qInfo"]["qId"] == params[0]))
        if method == "GetLayout" and kind == "bookmark":
            return {"qLayout": {"qInfo": obj["qInfo"], "qMeta": {**obj["qMetaDef"], "owner": "someone"}, "qBookmark": obj["qBookmark"]}}
        raise AssertionError(f"unexpected call {method}")


def _rest(calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "jwt", "expires_in": 21600})
        if path == "/api/v1/items":
            if "next" in request.url.params:
                return httpx.Response(200, json={"data": [], "links": {}})
            return httpx.Response(200, json={"data": [{"resourceId": APP, "name": "Dunder Mifflin sales", "spaceId": "s1",
                                                       "itemViews": {"total": 7}, "resourceAttributes": {"lastReloadTime": "2026-10-23",
                                                                                                         "hasSectionAccess": True}}],
                                             "links": {"next": {"href": "https://qlik.example.com/api/v1/items?next=abc"}}})
        if path == "/api/v1/spaces":
            return httpx.Response(200, json={"data": [{"id": "s1", "name": "Sales", "type": "shared", "ownerId": "u1"}], "links": {}})
        if path == f"/api/v1/apps/{APP}/data/metadata":
            return httpx.Response(200, json={"tables": [], "fields": [{"name": "a", "hash": "h", "byte_size": 1}]})
        return httpx.Response(404, json={"errors": [{"title": "not found"}]})

    return handler


@pytest.fixture
def qlik_env(monkeypatch: pytest.MonkeyPatch):
    calls: list[httpx.Request] = []
    monkeypatch.setenv("QLIK_TENANT_URL", "qlik.example.com")
    monkeypatch.setenv("QLIK_CLIENT_ID", "cid")
    monkeypatch.setenv("QLIK_CLIENT_SECRET", "secret")
    monkeypatch.delenv("QLIK_API_KEY", raising=False)
    real = Client.__init__
    monkeypatch.setattr(Client, "__init__", lambda self, url, **kw: real(self, url, **{**kw, "per_minute": 0, "max_retries": 0,
                                                                                         "transport": httpx.MockTransport(_rest(calls))}))
    monkeypatch.setattr(qlik, "Engine", FakeEngine)
    return calls


def test_extract_with_fake_engine(tmp_path: Path, qlik_env: list[httpx.Request]):
    store = RawStore(tmp_path, "qlik")
    QlikSource().extract(store, only={f"app:{APP}"})
    store.save()
    token = json.loads(qlik_env[0].content)
    assert qlik_env[0].url.path == "/oauth/token" and token == {"client_id": "cid", "client_secret": "secret",
                                                                "grant_type": "client_credentials", "scope": "user_default"}
    assert all(c.headers["authorization"] == "Bearer jwt" for c in qlik_env[1:])
    assert store.get_json("_errors.json") == [] and store.get_json("tenant.json") == {"url": "https://qlik.example.com", "engine": "12.2969.0"}
    assert store.get_json("apps.json")[0]["itemViews"] == {"total": 7} and store.get_json("spaces.json") == [{"id": "s1", "name": "Sales", "type": "shared"}]
    p = f"apps/{APP}/"
    for rel in ("script.qvs", "measures.json", "dimensions.json", "variables.json", "bookmarks.json", "tables-and-keys.json",
                "objects/sheet-sales-overview-id000033.json"):
        assert store.exists(p + rel), rel
    assert not store.exists(p + "data-metadata.json")
    assert yaml.safe_load(store.get_text(p + "connections.yml")) == {"connections": {"pg": {"type": "postgres",
                                                                                            "connectionstring": 'CUSTOM CONNECT TO "driver=postgres;"'}}}
    assert [v.get("qIsScriptCreated", False) for v in store.get_json(p + "variables.json")] == [True, False]
    b = QlikSource().parse(store)
    assert b.warehouse == "postgres" and len(b.dashboards) == 2 and b.dashboards[0].views == 7


def test_extract_falls_back_to_rest_metadata(tmp_path: Path, qlik_env: list[httpx.Request], monkeypatch: pytest.MonkeyPatch):
    call = FakeEngine.call

    def no_data(self, handle, method, *params):
        if method == "OpenDoc" and not (len(params) > 4 and params[4]):
            raise SourceError("Qlik engine OpenDoc: Access denied (code 5)")
        return call(self, handle, method, *params)

    monkeypatch.setattr(FakeEngine, "call", no_data)
    store = RawStore(tmp_path, "qlik")
    QlikSource().extract(store)
    assert store.get_json(f"apps/{APP}/data-metadata.json") == {"tables": [], "fields": [{"name": "a"}]}
    assert not store.exists(f"apps/{APP}/tables-and-keys.json") and store.get_json("_errors.json") == []


def test_inventory(qlik_env: list[httpx.Request]):
    inv = QlikSource().inventory()
    (item,) = inv.items
    assert (item.kind, item.id, item.name, item.parent, item.views) == ("app", f"app:{APP}", "Dunder Mifflin sales", "Sales", 7)
    assert item.url == f"https://qlik.example.com/sense/app/{APP}" and item.extra == {"last_reload": "2026-10-23", "section_access": True}


def test_api_key_skips_the_token_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QLIK_TENANT_URL", "https://qlik.example.com/")
    monkeypatch.setenv("QLIK_API_KEY", "key")
    src = QlikSource()
    assert src.client().http.headers["authorization"] == "Bearer key" and src.client().base_url == "https://qlik.example.com"
