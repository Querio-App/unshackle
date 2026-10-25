from conftest import notebook_source, todo_for
import base64
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.powerbi import (
    FABRIC_SCOPE,
    PBI_SCOPE,
    PowerBISource,
    dax_parse,
    layout_rows,
    m_source,
    parse_tmdl,
)

FIX = Path(__file__).parent / "fixtures" / "powerbi"
WS = "3f1e7a52-0c7d-4b8e-9a61-2d5f0b9c1a01"
MODEL = "8d2f4c10-5e6b-4a7d-b1c3-9e0f2a4b6c01"
MODEL_PRO = "8d2f4c10-5e6b-4a7d-b1c3-9e0f2a4b6c02"
R1 = "c41a9e20-7b3d-4f5e-8a1c-0d2e3f4a5b01"


def _store(tmp_path: Path) -> RawStore:
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "powerbi")
    return RawStore(tmp_path, "powerbi")


def _by_name(items):
    return {i.name: i for i in items}


# ---------------------------------------------------------------- building blocks


def test_tmdl_parser_shapes():
    nodes = parse_tmdl(
        "/// Orders\ntable 'Fact Sales'\n\tmeasure 'A' = SUM('Fact Sales'[x])\n\t\tformatString: 0\n\n"
        "\tmeasure B =\n\t\t\tCALCULATE(\n\t\t\t\t[A],\n\t\t\t\tT[c] = \"x\"\n\t\t\t)\n\t\tdisplayFolder: F\n"
        "\tmeasure C = ```\n\t\t\tVAR v = 1\n\t\t\tRETURN v\n\t\t\t```\n\t\tisHidden\n"
        "\tcolumn 'Net Price'\n\t\tdataType: decimal\n\t\tsourceColumn: \"Net Price\"\n\t\tisHidden\n"
        "\tpartition p = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n\t\t\t\t    S = 1\n\t\t\t\tin\n\t\t\t\t    S\n"
    )
    (t,) = nodes
    assert (t.kind, t.name, t.description) == ("table", "Fact Sales", "Orders")
    a, b, c = t.kids("measure")
    assert a.expr == "SUM('Fact Sales'[x])" and a.prop("formatString") == "0"
    assert b.expr.startswith("CALCULATE(\n\t[A],") and b.prop("displayFolder") == "F"
    assert c.expr == "VAR v = 1\nRETURN v" and c.prop("isHidden") == "true"
    col = t.kids("column")[0]
    assert col.prop("sourceColumn") == "Net Price" and col.prop("isHidden") == "true"
    part = t.kids("partition")[0]
    assert part.expr == "m" and part.prop("source").splitlines()[0] == "let"


def test_m_source():
    ms = m_source('let Source = Sql.Database(Server, Database), t = Source{[Schema="dbo",Item="Sales"]}[Data] in t',
                  {"Server": '"srv" meta [IsParameterQuery=true]', "Database": '"DW" meta [IsParameterQuery=true]'})
    assert (ms.connector, ms.server, ms.database, ms.schema, ms.table, ms.problems) == ("Sql.Database", "srv", "DW", "dbo", "Sales", [])
    sf = m_source('let S = Snowflake.Databases("acme.snowflakecomputing.com","WH"), d = S{[Name="ANALYTICS",Kind="Database"]}[Data], '
                  's = d{[Name="PUBLIC",Kind="Schema"]}[Data], t = s{[Name="ORDERS",Kind="Table"]}[Data] in t')
    assert (sf.database, sf.schema, sf.table) == ("ANALYTICS", "PUBLIC", "ORDERS")
    nq = m_source('Value.NativeQuery(Sql.Database("s","d"), "select 1 as ""x""", null)')
    assert nq.sql == 'select 1 as "x"'
    steps = m_source('let S = Sql.Database("s","d"), t = S{[Schema="dbo",Item="T"]}[Data], f = Table.SelectRows(t, each [x] > 1) in f')
    assert "Table.SelectRows" in steps.problems[0]
    assert "entered data" in m_source("Table.FromRows(Json.Document(Binary.Decompress(x)))").problems[0]


def test_dax_parser():
    n = dax_parse("CALCULATE ( [Total Sales], 'Date'[Year] = 2024 && Sales[Channel] IN { \"A\", \"B\" } ) -- comment")
    assert n.name == "CALCULATE" and len(n.args) == 2
    assert n.args[0].name == "Total Sales" and n.args[0].table is None
    assert n.args[1].op == "&&" and n.args[1].left.left.table == "Date"


def test_layout_rows():
    assert layout_rows([("a", 0, 0, 100), ("b", 300, 4, 100), ("c", 0, 120, 50), ("d", 10, 400, 10)]) == [["a", "b"], ["c"], ["d"]]


# ---------------------------------------------------------------- parse fixtures


def test_parse_semantic_models(tmp_path: Path):
    b = get_source("powerbi").parse(_store(tmp_path))
    models = _by_name(b.semantic_models)
    assert set(models) == {"sales", "customer", "date", "returns"}  # 'Key Measures' is measure-only
    sales = models["sales"]
    assert sales.table == "dbo.FactSales" and sales.default_time_dimension == ""  # several time columns, none declared: no guess
    ents = {e.name: (e.type, e.expr) for e in sales.entities}
    assert ents == {"sales": ("primary", "SalesKey"), "customer": ("foreign", "CustomerKey"), "date": ("foreign", "OrderDate")}
    assert {e.name: e.type for e in models["customer"].entities} == {"customer": "primary"}
    assert models["returns"].entities[0].name == "customer" and models["returns"].table == "returns"
    dims = _by_name(sales.dimensions)
    assert dims["order_date"].type == "time" and dims["order_date"].expr == "OrderDate"
    assert dims["amount"].expr == "SalesAmount" and dims["amount"].label == "Amount"
    assert "customer_key" not in dims  # keys become entities
    # calculated column translated; DAX subtraction treats BLANK as 0
    assert dims["line_margin"].expr == (
        "(CASE WHEN SalesAmount IS NULL AND TotalCost IS NULL THEN NULL ELSE COALESCE(SalesAmount, 0) - COALESCE(TotalCost, 0) END)"
    )
    assert "RELATED" in dims["customer_segment"].todo and dims["customer_segment"].source_language == "dax"
    cust = _by_name(models["customer"].dimensions)
    assert cust["name"].expr == "CustomerName"  # Table.RenameColumns reversed
    assert cust["region_group"].expr == "CASE WHEN Region = 'EU' THEN 'Europe' ELSE 'Rest of world' END"
    meas = {m.name: (m.agg, m.expr) for m in sales.measures}
    assert meas["total_sales"] == ("sum", "SalesAmount")
    assert meas["order_count"] == ("count", "1")
    assert meas["active_customers"] == ("count_distinct", "CustomerKey")
    assert meas["avg_unit_price"] == ("average", "UnitPrice")
    assert meas["extended_amount"] == ("sum", "OrderQuantity * UnitPrice")  # same-table SUMX
    tables = {t.name: t for t in b.tables}
    assert set(tables) == {"FactSales", "DimCustomer", "DimDate"} and tables["FactSales"].schema == "dbo"
    cols = {c.name: c for c in tables["FactSales"].columns}
    assert cols["SalesAmount"].type == "number" and cols["SalesAmount"].label == "Amount" and cols["SalesAmount"].hidden
    assert "Sql.Database(contoso-sql.database.windows.net, SalesDW)" in tables["FactSales"].source_ref
    returns_q = _by_name(b.queries)["Returns source"]
    assert "from dbo.FactReturns r" in returns_q.sql and "native SQL" in returns_q.todo


def test_parse_metrics(tmp_path: Path):
    b = get_source("powerbi").parse(_store(tmp_path))
    m = _by_name(b.metrics)
    assert (m["total_sales"].type, m["total_sales"].measure, m["total_sales"].semantic_model) == ("simple", "total_sales", "sales")
    assert m["total_sales"].label == "Total Sales" and "after discounts" in m["total_sales"].description
    aov = m["avg_order_value"]
    assert (aov.type, aov.numerator, aov.denominator) == ("ratio", "total_sales", "order_count")
    margin = m["margin_pct"]
    assert margin.type == "derived" and margin.expr == (  # BLANK - x is -x in DAX, NULL in SQL
        "1.0 * (CASE WHEN total_sales IS NULL AND total_cost IS NULL THEN NULL ELSE COALESCE(total_sales, 0) - COALESCE(total_cost, 0) END)"
        " / nullif(total_sales, 0)"
    )
    assert margin.metrics == ["total_sales", "total_cost"]
    assert m["eu_sales"].measure == "total_sales" and m["eu_sales"].filter == "{{ Dimension('customer__region') }} = 'EU'"
    assert m["online_sales"].filter == "{{ Dimension('sales__channel') }} IN ('Online', 'Mobile')"
    ytd = m["sales_ytd"]
    assert (ytd.type, ytd.measure, ytd.grain_to_date, ytd.semantic_model) == ("cumulative", "total_sales", "year", "sales")
    assert m["sales_qtd"].grain_to_date == "quarter"
    py = m["sales_py"]
    assert (py.type, py.expr, py.metrics, py.offsets) == ("derived", "total_sales", ["total_sales"], {"total_sales": "1 year"})
    assert m["sales_3m_ago"].offsets == {"total_sales": "3 months"}
    assert m["sales_yoy_pct"].expr == (
        "1.0 * (CASE WHEN total_sales IS NULL AND sales_py IS NULL THEN NULL ELSE COALESCE(total_sales, 0) - COALESCE(sales_py, 0) END)"
        " / nullif(sales_py, 0)"
    )
    dr = m["discount_rate"]  # VAR Gross = SUMX(..) RETURN DIVIDE(Gross - [Total Sales], Gross): inlined, reusing the measure
    assert (dr.type, dr.todo) == ("derived", "")
    assert dr.expr == (
        "1.0 * (CASE WHEN extended_amount IS NULL AND total_sales IS NULL THEN NULL"
        " ELSE COALESCE(extended_amount, 0) - COALESCE(total_sales, 0) END) / nullif(extended_amount, 0)"
    )
    for name, needle in [
        ("sales_all_regions", "ALL"),
        ("ship_date_sales", "USERELATIONSHIP"),
        ("fiscal_ytd", "fiscal"),
    ]:
        assert needle in m[name].todo, name
        assert m[name].source_language == "dax" and m[name].source_expression
        assert m[name].semantic_model == "sales"
    assert not any(x.todo for x in b.metrics if x.name in ("total_sales", "eu_sales", "sales_ytd", "sales_py", "avg_order_value"))


def test_parse_security_copilot_knowledge(tmp_path: Path):
    b = get_source("powerbi").parse(_store(tmp_path))
    rf = _by_name(b.row_filters)
    eu = rf["eu_sales_customer"]
    assert (eu.table, eu.predicate, eu.groups) == ("dbo.DimCustomer", "Region = 'EU'", ["eu-sales@contoso.com"])
    am = rf["account_managers_customer"]
    assert am.predicate == "AccountManagerEmail = {{ user.email }}" and "no members" in am.todo and "unions" in am.todo
    assert rf["account_managers_sales"].predicate == "" and "RLS DAX not translated" in rf["account_managers_sales"].todo
    perms = [(p.principal, p.effect, p.resource_type, p.resource_id) for p in b.permissions]
    assert ("group:eu-sales@contoso.com", "forbid", "Column", "warehouse.dbo.DimCustomer.EmailAddress") in perms  # OLS
    assert ("group:eu-sales@contoso.com", "permit", "Datasource", "warehouse") in perms
    k = _by_name(b.knowledge)
    assert k["Sales Model Copilot instructions"].context == "always" and "30 June" in k["Sales Model Copilot instructions"].body
    assert "EU sales last quarter" in k["Sales Model example questions"].body
    model_doc = k["Power BI model Sales Model"].body
    assert "`Returns` -> `returns` (directquery" in model_doc and "inactive" in model_doc
    warnings = " ".join(f"{w.item}: {w.message}" for w in b.warnings)
    assert "ItemHasProtectedLabel" in warnings and "Finance Model" in warnings
    assert "inactive relationship" in warnings and "bi-directional" in warnings and "pinned tiles" in warnings


def test_parse_pbir_report(tmp_path: Path):
    b = get_source("powerbi").parse(_store(tmp_path))
    d = _by_name(b.dashboards)
    assert set(d) == {"Sales Overview - Overview", "Sales Overview - Details", "Executive Summary - Summary"}
    ov = d["Sales Overview - Overview"]
    assert ov.slug == "sales-overview-overview" and ov.views == 128
    assert ov.url == f"https://app.powerbi.com/groups/{WS}/reports/{R1}/ReportSection1a2b3c4d5e6f7a8b9c0d"
    assert ov.layout == [["kpiTotalSales", "kpiAov"], ["colByRegion", "lineMonthly"], ["textNote"]]
    assert [(f.name, f.kind) for f in ov.filters] == [("channel", "value")]  # slicer
    t = {x.id: x for x in ov.tiles}
    assert set(t) == {"kpiTotalSales", "kpiAov", "colByRegion", "lineMonthly", "textNote"}
    kpi = t["kpiTotalSales"]
    assert kpi.kind == "kpi" and kpi.encodings["text"].field == "total_sales" and kpi.title == "Total Sales"
    assert "SUM(SalesAmount) AS total_sales" in kpi.sql and "(Channel = :channel OR :channel IS NULL)" in kpi.sql
    assert "SUM(SalesAmount) / NULLIF(COUNT(*), 0) AS avg_order_value" in t["kpiAov"].sql
    col = t["colByRegion"]
    assert (col.mark, col.title) == ("bar", "Sales by region")
    assert col.encodings["x"].field == "region" and col.encodings["y"].field == "total_sales"
    assert "LEFT JOIN dbo.DimCustomer AS customer ON sales.CustomerKey = customer.CustomerKey" in col.sql
    assert "customer.Segment IN ('Enterprise', 'SMB')" in col.sql and not col.todo
    line = t["lineMonthly"]
    assert line.mark == "line" and line.encodings["x"].field == "order_date_month" and line.encodings["x"].type == "temporal"
    assert line.encodings["color"].field == "channel" and "DATE_TRUNC('month', OrderDate) AS order_date_month" in line.sql
    assert t["textNote"].kind == "text" and "net of discounts" in t["textNote"].text

    det = d["Sales Overview - Details"]
    assert [f.name for f in det.filters] == ["order_date_from", "order_date_to"]
    t = {x.id: x for x in det.tiles}
    bar = t["barQtyByChannel"]
    assert bar.encodings["y"].field == "channel" and bar.encodings["x"].field == "quantity"  # horizontal
    assert "SUM(sales.OrderQuantity) AS quantity" in bar.sql
    assert "date_t.CalendarYear IN (2025)" in bar.sql  # page filter through the Date relationship
    assert "CAST(:order_date_from AS DATE)" in bar.sql
    assert t["tableCustomers"].kind == "table" and "COUNT(*) AS order_count" in t["tableCustomers"].sql
    assert t["mapCountry"].mark == "unknown" and "filledMap" in t["mapCountry"].todo and t["mapCountry"].sql
    share = t["colShare"]  # CALCULATE([Total Sales], ALL(Customer[Region])) by region: the total over regions, page filters kept
    assert not share.todo and "FROM (\n  SELECT\n    customer.Region AS region\n  FROM dbo.FactSales AS sales" in share.sql
    assert "(SELECT SUM(sales_1.SalesAmount) FROM dbo.FactSales AS sales_1 LEFT JOIN" in share.sql and "customer_1.Region" not in share.sql
    assert "date_t_1.CalendarYear IN (2025)" in share.sql


def test_parse_legacy_report(tmp_path: Path):
    b = get_source("powerbi").parse(_store(tmp_path))
    d = _by_name(b.dashboards)["Executive Summary - Summary"]
    t = {x.title: x for x in d.tiles}
    assert d.layout[0] == ["a1b2c3d4e5f6a7b8c9d0", "b2c3d4e5f6a7b8c9d0e1"]
    assert t["Margin %"].kind == "kpi" and (
        "1.0 * (CASE WHEN SUM(SalesAmount) IS NULL AND SUM(TotalCost) IS NULL THEN NULL"
        " ELSE COALESCE(SUM(SalesAmount), 0) - COALESCE(SUM(TotalCost), 0) END) / NULLIF(SUM(SalesAmount), 0)"
    ) in t["Margin %"].sql
    donut = t["Sales by channel"]
    assert donut.mark == "arc" and donut.encodings["theta"].field == "total_sales" and donut.encodings["color"].field == "channel"
    matrix = next(x for x in d.tiles if x.source_type == "pivotTable")
    assert matrix.kind == "table" and "pivot" in matrix.todo and "date_t.CalendarYear AS year" in matrix.sql


def test_copilot_verified_answer(tmp_path: Path):
    store = _store(tmp_path)
    store.put_json(
        f"models/{MODEL}/Copilot/VerifiedAnswers/definitions/va1/definition.json",
        {
            "displayName": "Sales by channel",
            "triggerPhrases": ["sales by channel", "channel revenue"],
            "visual": {
                "visualType": "clusteredColumnChart",
                "query": {"queryState": {
                    "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Channel"}}, "queryRef": "Sales.Channel"}]},
                    "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total Sales"}}, "queryRef": "Sales.Total Sales"}]},
                }},
            },
        },
    )
    b = PowerBISource().parse(store)
    q = _by_name(b.queries)["Sales by channel"]
    assert q.aliases == ["sales by channel", "channel revenue"] and "SUM(SalesAmount) AS total_sales" in q.sql and not q.todo


# ---------------------------------------------------------------- build


def _duckdb(path: Path) -> None:
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("create schema dbo")
    con.execute(
        "create table dbo.FactSales as select * from (values "
        "(1, timestamp '2025-01-05', timestamp '2025-01-07', 10, 'Online', 100.0, 60.0, 2, 50.0),"
        "(2, timestamp '2025-01-20', timestamp '2025-01-22', 11, 'Retail', 50.0, 20.0, 1, 50.0),"
        "(3, timestamp '2025-02-03', timestamp '2025-02-04', 10, 'Online', 30.0, 10.0, 3, 10.0)"
        ") t(SalesKey, OrderDate, ShipDate, CustomerKey, Channel, SalesAmount, TotalCost, OrderQuantity, UnitPrice)"
    )
    con.execute(
        "create table dbo.DimCustomer as select * from (values "
        "(10, 'Acme', 'EU', 'Germany', 'Enterprise', 'a@acme.com', 'am@contoso.com'),"
        "(11, 'Globex', 'US', 'United States', 'SMB', 'g@globex.com', 'am2@contoso.com')"
        ") t(CustomerKey, CustomerName, Region, Country, Segment, EmailAddress, AccountManagerEmail)"
    )
    con.execute(
        "create table dbo.DimDate as select d::timestamp as Date, year(d) as CalendarYear, monthname(d) as MonthName, month(d) as MonthNumber "
        "from range(date '2025-01-01', date '2025-03-01', interval 1 day) r(d)"
    )
    con.close()


def test_build_outputs_and_notebooks_run(tmp_path: Path):
    b = get_source("powerbi").parse(_store(tmp_path))
    next(sm for sm in b.semantic_models if sm.name == "sales").default_time_dimension = "order_date"  # as if declared
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/sales.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('FactSales')"
    ossie = yaml.safe_load((tmp_path / "models/semantic/sales.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "dbo.FactSales"
    assert model["defaults"] == {"agg_time_dimension": "order_date"}
    metrics = {m["name"]: m for m in sem["metrics"]}
    assert metrics["avg_order_value"]["type_params"] == {"numerator": "total_sales", "denominator": "order_count"}
    assert metrics["sales_ytd"]["type_params"]["cumulative_type_params"] == {"grain_to_date": "year"}
    assert metrics["sales_py"]["type_params"]["metrics"] == [{"name": "total_sales", "offset_window": "1 year"}]
    assert metrics["eu_sales"]["filter"] == "{{ Dimension('customer__region') }} = 'EU'"
    assert "sales_all_regions" not in metrics and "[dax]" in todo_for((tmp_path / "models/semantic/sales.yml").read_text(), "metric", "sales_all_regions")
    rows = yaml.safe_load((tmp_path / "policies/row_filters.powerbi.yml").read_text())["row_filters"]
    assert any(r.get("predicate") == "Region = 'EU'" and r["table"] == "warehouse.dbo.DimCustomer" for r in rows)
    cedar = (tmp_path / "policies/powerbi.cedar").read_text()
    assert "forbid (" in cedar and 'Column::"warehouse.dbo.DimCustomer.EmailAddress"' in cedar
    assert (tmp_path / "queries/returns-source.sql").exists()
    assert (tmp_path / "knowledge/sales-model-copilot-instructions.md").exists()
    assert (tmp_path / "catalog/warehouse/dbo/FactSales.yml").exists()
    report = (tmp_path / "migration/report.md").read_text()
    assert "USERELATIONSHIP" in report and "filledMap" in report
    names = ["sales-overview-overview", "sales-overview-details", "executive-summary-summary"]
    for n in names:
        notebook_source(tmp_path / f"notebooks/{n}.ipynb")
    nb = notebook_source(tmp_path / "notebooks/sales-overview-overview.ipynb")
    assert "mark_line" in nb and "\nchannel = None" in nb

    from unshackle.run import notebook_queries, query, run_notebook

    db = tmp_path / "wh.duckdb"
    _duckdb(db)
    url = f"duckdb:///{db}"
    for n in names:
        html = run_notebook(tmp_path / f"notebooks/{n}.ipynb", url=url).read_text()
        assert "vegaEmbed" in html
    _, sqls = notebook_queries(tmp_path / "notebooks/sales-overview-overview.ipynb")
    by_region = query(sqls["sales_by_region"], {"channel": None}, url)
    assert dict(zip(by_region["region"], by_region["total_sales"])) == {"EU": 130.0, "US": 50.0}
    online = query(sqls["sales_by_region"], {"channel": "Online"}, url)
    assert list(online["total_sales"]) == [130.0]
    monthly = query(sqls["monthly_sales_by_channel"], {"channel": None}, url)
    assert len(monthly) == 3
    _, det = notebook_queries(tmp_path / "notebooks/sales-overview-details.ipynb")
    units = query(det["units_by_channel"], {"order_date_from": "2025-01-10", "order_date_to": None}, url)
    assert units.to_dict("records") == [{"channel": "Online", "quantity": 3}]


# ---------------------------------------------------------------- extract (mock API)


def _parts(root: Path) -> list[dict[str, str]]:
    return [
        {"path": str(p.relative_to(root)), "payload": base64.b64encode(p.read_bytes()).decode(), "payloadType": "InlineBase64"}
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


def _mock(calls: list[str]):
    polls = {"op-model": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        calls.append(f"{req.method} {host}{path}")
        if host == "login.microsoftonline.com":
            assert path == "/tenant-1/oauth2/v2.0/token"
            form = parse_qs(req.content.decode())
            assert form["grant_type"] == ["client_credentials"] and form["client_secret"] == ["s3cret"]
            tok = {PBI_SCOPE: "pbi-token", FABRIC_SCOPE: "fabric-token"}[form["scope"][0]]
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3599, "access_token": tok})
        auth = req.headers.get("authorization")
        if host == "api.fabric.microsoft.com":
            assert auth == "Bearer fabric-token"
            if path == f"/v1/workspaces/{WS}/semanticModels/{MODEL}/getDefinition":
                assert req.url.params["format"] == "TMDL"
                return httpx.Response(202, headers={"Location": "https://api.fabric.microsoft.com/v1/operations/op-model",
                                                    "x-ms-operation-id": "op-model", "Retry-After": "1"})
            if path == f"/v1/workspaces/{WS}/semanticModels/{MODEL_PRO}/getDefinition":
                return httpx.Response(400, json={"errorCode": "ItemHasProtectedLabel", "message": "protected sensitivity label"})
            if path == "/v1/operations/op-model":
                polls["op-model"] += 1
                status = "Running" if polls["op-model"] < 2 else "Succeeded"
                return httpx.Response(200, json={"status": status, "percentComplete": 50 if status == "Running" else 100})
            if path == "/v1/operations/op-model/result":
                return httpx.Response(200, json={"definition": {"parts": _parts(FIX / "models" / MODEL)}})
            if path == f"/v1/workspaces/{WS}/reports/{R1}/getDefinition":
                return httpx.Response(200, json={"definition": {"parts": _parts(FIX / "reports" / R1)}})
            return httpx.Response(404)
        assert host == "api.powerbi.com" and auth == "Bearer pbi-token"
        p = path.removeprefix("/v1.0/myorg")
        if p == "/groups":
            return httpx.Response(200, json={"value": [{"id": WS, "name": "Sales Analytics", "isOnDedicatedCapacity": True}]})
        if p == f"/groups/{WS}/datasets":
            return httpx.Response(200, json={"value": json.loads((FIX / "workspaces" / WS / "datasets.json").read_text())})
        if p == f"/groups/{WS}/reports":
            return httpx.Response(200, json={"value": json.loads((FIX / "workspaces" / WS / "reports.json").read_text())[:1]})
        if p == f"/groups/{WS}/dashboards":
            return httpx.Response(200, json={"value": []})
        if p == "/admin/workspaces/getInfo":
            assert req.url.params["datasetSchema"] == "True" and req.url.params["datasetExpressions"] == "True"
            assert json.loads(req.content) == {"workspaces": [WS]}
            return httpx.Response(202, json={"id": "scan-1", "createdDateTime": "2026-09-30T10:00:00Z", "status": "NotStarted"})
        if p == "/admin/workspaces/scanStatus/scan-1":
            return httpx.Response(200, json={"id": "scan-1", "status": "Succeeded"})
        if p == "/admin/workspaces/scanResult/scan-1":
            return httpx.Response(200, json={"workspaces": [{"id": WS, "name": "Sales Analytics", "datasets": [{
                "id": MODEL_PRO, "name": "Finance Model",
                "tables": [{"name": "GL", "source": [{"expression": 'let S = Sql.Database("fin-sql", "Finance"), t = S{[Schema="gl",Item="Entries"]}[Data] in t'}],
                            "columns": [{"name": "Account", "dataType": "String", "columnType": "Data"},
                                        {"name": "PostedAt", "dataType": "DateTime", "columnType": "Data"},
                                        {"name": "Amount", "dataType": "Double", "columnType": "Data"}],
                            "measures": [{"name": "Balance", "expression": "SUM(GL[Amount])"},
                                         {"name": "Closing Balance", "expression": "CALCULATE([Balance], LASTDATE(GL[PostedAt]))"}]}],
                "roles": [{"name": "Controllers", "modelPermission": "Read", "members": [{"memberName": "controllers@contoso.com", "memberType": "Group"}],
                           "tablePermissions": [{"name": "GL", "filterExpression": "[Account] <> \"Payroll\""}]}],
            }]}]})
        if p == "/admin/activityevents":
            assert req.url.params["$filter"] == "Activity eq 'viewreport'"
            return httpx.Response(200, json={"activityEventEntities": [
                {"Activity": "ViewReport", "ReportId": R1, "CreationTime": "2026-09-29T08:00:00"},
                {"Activity": "ViewReport", "ReportId": R1, "CreationTime": "2026-09-29T09:30:00"},
            ], "lastResultSet": True})
        return httpx.Response(404)

    return handler


def _source(monkeypatch, calls, admin: bool) -> PowerBISource:
    for k, v in {"POWERBI_TENANT_ID": "tenant-1", "POWERBI_CLIENT_ID": "app-1", "POWERBI_CLIENT_SECRET": "s3cret", "POWERBI_USAGE_DAYS": "2"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("POWERBI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("POWERBI_WORKSPACES", raising=False)
    monkeypatch.setenv("POWERBI_ADMIN", "1" if admin else "0")
    client = Client("https://api.powerbi.com/v1.0/myorg", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock(calls)))
    return PowerBISource(client=client, poll_interval=0, today=datetime(2026, 9, 30, tzinfo=timezone.utc))


def test_extract_with_mock_api(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    src = _source(monkeypatch, calls, admin=True)
    store = RawStore(tmp_path, "powerbi")
    store.begin()
    src.extract(store)
    store.save()
    assert sum(1 for c in calls if c.startswith("POST login")) == 2  # one token per resource
    assert calls.count(f"GET api.fabric.microsoft.com/v1/operations/op-model") == 2  # polled until Succeeded
    assert store.exists(f"models/{MODEL}/definition/tables/Sales.tmdl")
    assert store.get_text(f"models/{MODEL}/definition/tables/Sales.tmdl") == (FIX / "models" / MODEL / "definition/tables/Sales.tmdl").read_text()
    assert store.exists(f"reports/{R1}/definition/pages/ReportSection1a2b3c4d5e6f7a8b9c0d/visuals/colByRegion/visual.json")
    assert store.exists(f"workspaces/{WS}/datasets.json") and store.exists("workspaces.json")
    assert store.exists("scanner/scan-1.json")
    assert store.get_json("usage.json") == {R1: {"views": 4, "last_viewed": "2026-09-29T09:30:00"}}  # 2 days x 2 events
    errors = store.get_json("_errors.json")
    assert [e["item"] for e in errors] == [f"model:{MODEL_PRO}"] and "ItemHasProtectedLabel" in errors[0]["error"]

    b = PowerBISource().parse(RawStore(tmp_path, "powerbi"))
    models = _by_name(b.semantic_models)
    assert "sales" in models and models["gl"].table == "gl.Entries"
    m = _by_name(b.metrics)
    assert m["balance"].measure == "balance" and "LASTDATE" in m["closing_balance"].todo
    assert _by_name(b.row_filters)["controllers_gl"].predicate == "Account <> 'Payroll'"
    assert _by_name(b.row_filters)["controllers_gl"].groups == ["controllers@contoso.com"]
    assert {d.name for d in b.dashboards} == {"Sales Overview - Overview", "Sales Overview - Details"}
    assert _by_name(b.dashboards)["Sales Overview - Overview"].views == 4
    assert any("admin scanner" in w.message for w in b.warnings)


def test_extract_without_admin_and_only(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    src = _source(monkeypatch, calls, admin=False)
    store = RawStore(tmp_path, "powerbi")
    src.extract(store, {f"report:{R1}"})
    assert store.exists(f"reports/{R1}/definition/report.json") and store.exists(f"models/{MODEL}/definition/model.tmdl")
    assert not any("getDefinition" in c and MODEL_PRO in c for c in calls)  # only the report's model
    assert not any("admin" in c for c in calls) and not store.exists("usage.json")


def test_inventory_and_access_token(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    src = _source(monkeypatch, calls, admin=True)
    inv = src.inventory()
    kinds = {i.kind for i in inv.items}
    assert kinds == {"workspace", "semantic_model", "report"}
    report = next(i for i in inv.items if i.kind == "report")
    assert report.views == 4 and report.extra["datasetId"] == MODEL
    monkeypatch.setenv("POWERBI_ACCESS_TOKEN", "pbi-token")
    monkeypatch.setenv("POWERBI_FABRIC_ACCESS_TOKEN", "fabric-token")
    calls.clear()
    fresh = PowerBISource(client=src.client(), poll_interval=0)
    fresh.extract(RawStore(tmp_path, "powerbi"), {f"model:{MODEL}"})
    assert not any(c.startswith("POST login") for c in calls)


# ---------------------------------------------------------------- regressions found on published projects (tests/fixtures/powerbi_real)


def _m(item: str) -> str:
    return (
        "\tpartition P = m\n\t\tmode: import\n\t\tsource =\n"
        f'\t\t\t\tlet Source = Sql.Database("srv", "DW"), T = Source{{[Schema="dbo",Item="{item}"]}}[Data] in T\n'
    )


def _col(name: str, dtype: str = "double", extra: str = "") -> str:
    return f"\tcolumn {name}\n\t\tdataType: {dtype}\n\t\tsourceColumn: {name.strip(chr(39))}\n{extra}\n"


SALES_TMDL = (
    "table Sales\n"
    "\tmeasure Margin = SUMX(Sales, Sales[Qty] * (Sales[Price] - Sales[Cost]))\n"
    "\tmeasure Revenue = SUM(Sales[Price])\n"
    "\tmeasure 'Unit Price' = DIVIDE(SUM(Sales[Price]), SUM(Sales[Qty]))\n"
    "\tmeasure 'Total' = CALCULATE(SUM(Sales[Price]))\n"
    "\tmeasure 'Red' = CALCULATE(SUM(Sales[Price]), Product[Color] = \"Red\") + 0\n"
    "\tmeasure 'Red Share' = DIVIDE([Red], [Revenue])\n"
    "\tmeasure 'Rev Δ' =\n\t\t\tVAR r = [Revenue]\n\t\t\tVAR m = [Margin]\n\t\t\tRETURN DIVIDE(r - m, r)\n"
    "\tmeasure 'Rev Red' =\n\t\t\tVAR r = [Revenue]\n\t\t\tRETURN CALCULATE(r, Product[Color] = \"Red\")\n"
    "\tmeasure 'Half' = CALCULATE([Revenue], Sales[Qty] > .5)\n"
    "\tmeasure '#NetCost' = SUM(Sales[Cost])\n"
    + _col("Qty", "int64") + _col("Price") + _col("Cost") + _col("ProductKey", "int64")
    + "\tcolumn 'Line Margin' = Sales[Price] - Sales[Cost] / Sales[Qty]\n\t\tdataType: double\n\n"
    + _m("FactSales")
)
PRODUCT_TMDL = "table Product\n" + _col("ProductKey", "int64", "\t\tisKey") + _col("Color", "string") + _m("DimProduct")
REL_TMDL = "relationship r1\n\tfromColumn: Sales.ProductKey\n\ttoColumn: Product.ProductKey\n"


def _mini(tmp_path: Path, files: dict[str, str]):
    """A small project imported by hand: models/<id>/definition/... and reports/<id>/..."""
    raw = tmp_path / "migration" / "raw" / "powerbi"
    for rel, text in files.items():
        (raw / rel).parent.mkdir(parents=True, exist_ok=True)
        (raw / rel).write_text(text)
    return get_source("powerbi").parse(RawStore(tmp_path, "powerbi"))


def _sales(tmp_path: Path, **extra: str):
    return _mini(tmp_path, {
        "models/Shop/definition/database.tmdl": "database Unknown\n\tcompatibilityLevel: 1601\n",
        "models/Shop/definition/tables/Sales.tmdl": SALES_TMDL,
        "models/Shop/definition/tables/Product.tmdl": PRODUCT_TMDL,
        "models/Shop/definition/relationships.tmdl": REL_TMDL,
        **extra,
    })


def test_dax_operator_precedence_and_division(tmp_path: Path):
    b = _sales(tmp_path)
    meas = {m.name: m for sm in b.semantic_models for m in sm.measures}
    assert meas["margin"].expr == "Qty * (CASE WHEN Price IS NULL AND Cost IS NULL THEN NULL ELSE COALESCE(Price, 0) - COALESCE(Cost, 0) END)"
    dims = {d.name: d for sm in b.semantic_models for d in sm.dimensions}
    assert dims["line_margin"].expr == (
        "(CASE WHEN Price IS NULL AND 1.0 * Cost / NULLIF(Qty, 0) IS NULL THEN NULL ELSE COALESCE(Price, 0) - COALESCE(1.0 * Cost / NULLIF(Qty, 0), 0) END)"
    )
    assert dax_parse("[A] < .5").right.value == ".5"


def test_dax_vars_inlined_only_where_context_is_the_same(tmp_path: Path):
    m = _by_name(_sales(tmp_path).metrics)
    assert (m["rev_delta"].type, m["rev_delta"].todo) == ("derived", "")
    assert m["rev_delta"].expr == "1.0 * (CASE WHEN revenue IS NULL AND margin IS NULL THEN NULL ELSE COALESCE(revenue, 0) - COALESCE(margin, 0) END) / nullif(revenue, 0)"
    assert "r is used inside CALCULATE" in m["rev_red"].todo and m["rev_red"].source_expression.startswith("VAR r")
    assert dax_parse("VAR x = 1 RETURN CALCULATE([a], T[c] = x)").args[1].right.value == "1"  # constants are safe anywhere


def test_dax_calculate_without_filters_and_plus_zero(tmp_path: Path):
    b = _sales(tmp_path)
    m = _by_name(b.metrics)
    meas = {x.name: x for sm in b.semantic_models for x in sm.measures}
    assert (m["total"].type, meas[m["total"].measure].agg, meas[m["total"].measure].expr) == ("simple", "sum", "Price")  # CALCULATE(SUM(..)) is SUM(..)
    red = m["red"]  # CALCULATE(SUM(..), filter) + 0: a filtered input metric, BLANK shown as 0
    assert (red.type, red.expr, red.metrics) == ("derived", "coalesce(red_1, 0)", ["red_1"])
    assert (m["red_1"].measure, m["red_1"].filter) == ("revenue", "{{ Dimension('product__color') }} = 'Red'")
    assert (m["red_share"].type, m["red_share"].numerator, m["red_share"].denominator) == ("ratio", "red", "revenue")
    assert m["half"].filter == "{{ Dimension('sales__qty') }} > .5"
    assert "count_net_cost" in m and "rev_delta" in m  # names are ASCII snake_case: '#NetCost', 'Rev Δ'


def test_model_named_after_folder_when_database_is_unknown(tmp_path: Path):
    b = _sales(tmp_path)
    assert any(k.name == "Power BI model Shop" for k in b.knowledge)


def _visual(name: str, vtype: str, select: list[dict], filters: list[dict] | None = None) -> str:
    return json.dumps({
        "name": name, "position": {"x": 0, "y": 0, "width": 100, "height": 100},
        "visual": {"visualType": vtype, "query": {"queryState": {"Values": {"projections": select}}}},
        **({"filterConfig": {"filters": filters}} if filters else {}),
    })


def _measure(entity: str, prop: str) -> dict:
    return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}, "queryRef": f"{entity}.{prop}"}


def test_thin_report_by_connection_and_tile_sql(tmp_path: Path):
    pbir = {"version": "4.0", "datasetReference": {"byConnection": {
        "connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;initial catalog=Shop;integrated security=ClaimsToken"}}}
    page = "reports/Thin/definition/pages/p1"
    b = _sales(tmp_path, **{
        "reports/Thin/definition.pbir": json.dumps(pbir),
        f"{page}/page.json": json.dumps({"name": "p1", "displayName": "Page"}),
        f"{page}/visuals/v1/visual.json": _visual("v1", "card", [_measure("Sales", "Red")]),
        f"{page}/visuals/v2/visual.json": _visual("v2", "card", [_measure("Sales", "Unit Price")]),
    })
    t = {x.id: x for x in b.dashboards[0].tiles}
    # the model imported next to the thin report is found; the fact table is the FROM even though the filter touches Product first
    assert "FROM dbo.FactSales AS sales\nLEFT JOIN dbo.DimProduct AS product" in t["v1"].sql
    assert "(COALESCE(SUM(CASE WHEN product.Color = 'Red' THEN sales.Price END), 0)) AS red" in t["v1"].sql
    assert "1.0 * SUM(Price) / NULLIF(SUM(Qty), 0) AS unit_price" in t["v2"].sql


def test_calculation_group_filter_on_a_visual(tmp_path: Path):
    calc = (
        "table 'Time Calc'\n\tcalculationGroup\n\t\tcalculationItem Current = SELECTEDMEASURE()\n\n"
        "\t\tcalculationItem PY = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))\n\n"
        "\tcolumn Item\n\t\tdataType: string\n\t\tsourceColumn: Name\n"
    )

    def flt(item: str) -> dict:
        return {"name": f"f{item}", "type": "Categorical", "filter": {"Version": 2, "From": [{"Name": "t", "Entity": "Time Calc", "Type": 0}],
                "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": "Item"}}],
                                                 "Values": [[{"Literal": {"Value": f"'{item}'"}}]]}}}]}}

    page = "reports/R/definition/pages/p1"
    b = _sales(tmp_path, **{
        "models/Shop/definition/tables/Time Calc.tmdl": calc,
        "reports/R/definition.pbir": json.dumps({"datasetReference": {"byPath": {"path": "../Shop.SemanticModel"}}}),
        f"{page}/page.json": json.dumps({"name": "p1", "displayName": "Page"}),
        f"{page}/visuals/cur/visual.json": _visual("cur", "card", [_measure("Sales", "Revenue")], [flt("Current")]),
        f"{page}/visuals/py/visual.json": _visual("py", "card", [_measure("Sales", "Revenue")], [flt("PY")]),
    })
    t = {x.id: x for x in b.dashboards[0].tiles}
    assert "SUM(Price) AS revenue" in t["cur"].sql and not t["cur"].todo  # SELECTEDMEASURE() only: values unchanged
    assert not t["py"].sql and "calculation item(s) Time Calc[PY]" in t["py"].todo


def test_direct_lake_and_composite_entity_partitions(tmp_path: Path):
    lake = "table fca\n" + _col("Cost") + (
        "\tpartition fca = entity\n\t\tmode: directLake\n\t\tsource\n"
        "\t\t\tentityName: focus_cost\n\t\t\tschemaName: dbo\n\t\t\texpressionSource: DatabaseQuery\n"
    )
    remote = "table Items\n" + _col("Name", "string") + (
        "\tpartition Items = entity\n\t\tmode: directQuery\n\t\tsource\n"
        "\t\t\tentityName: Items\n\t\t\texpressionSource: 'DirectQuery to AS - Metrics'\n"
    )
    exprs = (
        'expression DatabaseQuery =\n\t\tlet\n\t\t    database = Sql.Database("abc.datawarehouse.fabric.microsoft.com", "5548f1dd")\n'
        "\t\tin\n\t\t    database\n\n"
        "expression 'DirectQuery to AS - Metrics' =\n\t\tlet\n\t\t    Source = AnalysisServices.Database(\"powerbi://x\", \"Metrics\")\n"
        "\t\tin\n\t\t    Source\n"
    )
    b = _mini(tmp_path, {
        "models/M/definition/tables/fca.tmdl": lake,
        "models/M/definition/tables/Items.tmdl": remote,
        "models/M/definition/expressions.tmdl": exprs,
    })
    sm = _by_name(b.semantic_models)
    assert sm["fca"].table == "dbo.focus_cost" and [t.name for t in b.tables] == ["focus_cost"]
    (q,) = b.queries
    assert q.name == "Items source" and "DirectQuery to another Power BI semantic model" in q.todo


def test_renames_kept_for_sources_rewritten_as_sql(tmp_path: Path):
    cal = "table Calendar\n" + _col("Date", "dateTime") + (
        "\tpartition Calendar = m\n\t\tmode: import\n\t\tsource =\n"
        "\t\t\t\tlet Source = List.Dates(#date(2020,1,1), 10, #duration(1,0,0,0)), T = Table.FromList(Source, Splitter.SplitByNothing()),\n"
        '\t\t\t\t    R = Table.RenameColumns(T,{{"Column1", "Date"}}) in R\n'
    )
    b = _mini(tmp_path, {"models/M/definition/tables/Calendar.tmdl": cal})
    (sm,) = b.semantic_models
    assert sm.table == "calendar" and [d.expr for d in sm.dimensions] == [""]  # the SQL model to write exposes Date, not Column1


def test_kpi_status_field_explained(tmp_path: Path):
    page = "reports/R/definition/pages/p1"
    b = _sales(tmp_path, **{
        "reports/R/definition.pbir": json.dumps({"datasetReference": {"byPath": {"path": "../Shop.SemanticModel"}}}),
        f"{page}/page.json": json.dumps({"name": "p1", "displayName": "Page"}),
        f"{page}/visuals/k/visual.json": _visual("k", "tableEx", [_measure("Sales", "_Margin Status")]),
    })
    assert "KPI status of measure [Margin]" in b.dashboards[0].tiles[0].todo


def test_many_to_many_and_non_key_relationships_are_not_entities(tmp_path: Path):
    items = "table Items\n" + _col("'Unique key'", "string") + _col("'Item key'", "string") + _m("Items")
    detail = "table Detail\n" + _col("'Unique key'", "string") + _col("CU") + _m("Detail")
    memory = "table Memory\n" + _col("'Item key'", "string") + _col("MB") + _m("Memory")
    other = "table Other\n" + _col("'Item key'", "string") + _m("Other")
    rels = (
        "relationship a\n\tfromColumn: Detail.'Unique key'\n\ttoColumn: Items.'Unique key'\n\n"
        "relationship b\n\ttoCardinality: many\n\tfromColumn: Memory.'Item key'\n\ttoColumn: Items.'Item key'\n\n"
        "relationship c\n\tfromColumn: Other.'Item key'\n\ttoColumn: Items.'Item key'\n"
    )
    b = _mini(tmp_path, {"models/M/definition/tables/Items.tmdl": items, "models/M/definition/tables/Detail.tmdl": detail,
                         "models/M/definition/tables/Memory.tmdl": memory, "models/M/definition/tables/Other.tmdl": other,
                         "models/M/definition/relationships.tmdl": rels})
    sm = _by_name(b.semantic_models)
    assert [(e.name, e.type, e.expr) for e in sm["items"].entities] == [("items", "primary", '"Unique key"')]
    assert [e.name for e in sm["detail"].entities] == ["items"]
    assert sm["memory"].entities == [] and sm["other"].entities == []  # would join Item key to Unique key
    warnings = " ".join(w.message for w in b.warnings)
    assert "many-to-many relationship Memory[Item key] -> Items[Item key]" in warnings and "is not on Items's key [Unique key]" in warnings


def test_same_field_sliced_twice_is_one_parameter(tmp_path: Path):
    color = {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Product"}}, "Property": "Color"}}, "queryRef": "Product.Color"}
    page = "reports/R/definition/pages/p1"
    b = _sales(tmp_path, **{
        "reports/R/definition.pbir": json.dumps({"datasetReference": {"byPath": {"path": "../Shop.SemanticModel"}}}),
        f"{page}/page.json": json.dumps({"name": "p1", "displayName": "Page"}),
        f"{page}/visuals/s1/visual.json": _visual("s1", "slicer", [color]),
        f"{page}/visuals/s2/visual.json": _visual("s2", "slicer", [color]),
        f"{page}/visuals/v/visual.json": _visual("v", "card", [_measure("Sales", "Revenue")]),
    })
    (d,) = b.dashboards
    assert [f.name for f in d.filters] == ["color"]
    assert d.tiles[0].sql.count(":color") == 2 and "color_2" not in d.tiles[0].sql


# ---------------------------------------------------------------- filter context in tiles (ALL, ALLEXCEPT, ALLSELECTED, FILTER, VALUES)

CTX_SALES = (
    "table Sales\n"
    "\tmeasure Revenue = SUM(Sales[Price])\n"
    "\tmeasure 'Share of Color' = DIVIDE([Revenue], CALCULATE([Revenue], ALL(Product[Color])))\n"
    "\tmeasure 'Share of Product' = DIVIDE([Revenue], CALCULATE([Revenue], REMOVEFILTERS(Product)))\n"
    "\tmeasure 'Category Revenue' = CALCULATE([Revenue], ALLEXCEPT(Product, Product[Category]))\n"
    "\tmeasure Red = CALCULATE([Revenue], Product[Color] = \"Red\")\n"
    "\tmeasure Running = CALCULATE([Revenue], FILTER(ALLSELECTED('Calendar'[Date]), 'Calendar'[Date] <= MAX('Calendar'[Date])))\n"
    "\tmeasure 'Running All' = CALCULATE([Revenue], FILTER(ALL('Calendar'[Date]), 'Calendar'[Date] <= MAX('Calendar'[Date])))\n"
    "\tmeasure 'Avg per Day' = AVERAGEX(VALUES('Calendar'[Date]), [Revenue])\n"
    "\tmeasure 'Avg per Day 0' = AVERAGEX(VALUES('Calendar'[Date]), [Revenue] + 0)\n"
    "\tmeasure Margin = [Revenue] - SUM(Sales[Cost])\n"
    "\tmeasure 'Per Unit' = DIVIDE([Revenue], SUM(Sales[Qty]), 0)\n"
    + _col("OrderDate", "dateTime") + _col("ProductKey", "int64") + _col("Price") + _col("Qty", "int64") + _col("Cost") + _m("FactSales")
)
CTX_PRODUCT = "table Product\n" + _col("ProductKey", "int64", "\t\tisKey") + _col("Color", "string") + _col("Category", "string") + _m("DimProduct")
CTX_CALENDAR = (
    "table Calendar\n\tdataCategory: Time\n\n" + _col("Date", "dateTime", "\t\tisKey") + _col("YearMonth", "string") + _col("MonthName", "string")
    + _m("DimCalendar")
)
CTX_RELS = (
    "relationship r1\n\tfromColumn: Sales.ProductKey\n\ttoColumn: Product.ProductKey\n\n"
    "relationship r2\n\tfromColumn: Sales.OrderDate\n\ttoColumn: Calendar.Date\n"
)


def _col_field(entity: str, prop: str) -> dict:
    return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}, "queryRef": f"{entity}.{prop}"}


def _chart(name: str, cats: list[tuple[str, str]], measures: list[str], vtype: str = "tableEx") -> str:
    state = {"Values": {"projections": [_measure("Sales", m) for m in measures]}}
    if cats:
        state["Category"] = {"projections": [_col_field(e, p) for e, p in cats]}
    return json.dumps({"name": name, "position": {"x": 0, "y": 0, "width": 100, "height": 100},
                       "visual": {"visualType": vtype, "query": {"queryState": state}}})


def _ctx_project(tmp_path: Path, calendar: str = CTX_CALENDAR, rels: str = CTX_RELS):
    page = "reports/R/definition/pages/p1"
    visuals = {
        "byColor": _chart("byColor", [("Product", "Color")], ["Revenue", "Share of Color", "Share of Product", "Red"]),
        "byColorCat": _chart("byColorCat", [("Product", "Color"), ("Product", "Category")], ["Category Revenue"]),
        "byMonth": _chart("byMonth", [("Calendar", "YearMonth")], ["Revenue", "Running", "Running All", "Avg per Day", "Margin", "Per Unit"]),
        "byMonthName": _chart("byMonthName", [("Calendar", "MonthName")], ["Running"]),
        "byDate": _chart("byDate", [("Calendar", "Date")], ["Running"]),
        "card": _chart("card", [], ["Running", "Avg per Day"], "card"),
        "zeros": _chart("zeros", [], ["Avg per Day 0"], "card"),
        "sCategory": _chart("sCategory", [("Product", "Category")], [], "slicer"),
        "sDate": _chart("sDate", [("Calendar", "Date")], [], "slicer"),
    }
    return _mini(tmp_path, {
        "models/Ctx/definition/tables/Sales.tmdl": CTX_SALES,
        "models/Ctx/definition/tables/Product.tmdl": CTX_PRODUCT,
        "models/Ctx/definition/tables/Calendar.tmdl": calendar,
        "models/Ctx/definition/relationships.tmdl": rels,
        "reports/R/definition.pbir": json.dumps({"datasetReference": {"byPath": {"path": "../Ctx.SemanticModel"}}}),
        f"{page}/page.json": json.dumps({"name": "p1", "displayName": "Page"}),
        **{f"{page}/visuals/{k}/visual.json": v for k, v in visuals.items()},
    })


def _ctx_duckdb(path: Path) -> str:
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("create schema dbo")
    con.execute(
        "create table dbo.FactSales as select * from (values "
        "(timestamp '2024-01-05', 1, 10.0, 1, 4.0), (timestamp '2024-01-20', 2, 20.0, 2, null), (timestamp '2024-02-10', 3, 30.0, 0, 5.0),"
        "(timestamp '2025-01-15', 2, 40.0, 4, 10.0), (timestamp '2024-03-03', 1, 50.0, 5, null), (timestamp '2024-01-05', 2, 6.0, 1, 1.0)"
        ") t(OrderDate, ProductKey, Price, Qty, Cost)"
    )
    con.execute("create table dbo.DimProduct as select * from (values (1, 'Red', 'Bikes'), (2, 'Blue', 'Bikes'), (3, 'Red', 'Clothes')) t(ProductKey, Color, Category)")
    con.execute(
        "create table dbo.DimCalendar as select d::timestamp as Date, strftime(d, '%Y-%m') as YearMonth, strftime(d, '%b') as MonthName from ("
        "select unnest(range(date '2024-01-01', date '2024-04-01', interval 1 day)) d union all "
        "select unnest(range(date '2025-01-01', date '2025-02-01', interval 1 day)))"
    )
    con.close()
    return f"duckdb:///{path}"


def test_context_changing_dax_in_tiles_matches_dax(tmp_path: Path):
    """Hand-computed DAX results on six sales rows (Revenue 156: Red 90, Blue 66; Bikes 126, Clothes 30)."""
    from unshackle.run import query

    b = _ctx_project(tmp_path)
    t = {x.id: x for x in b.dashboards[0].tiles}
    url = _ctx_duckdb(tmp_path / "wh.duckdb")
    none = {"category": None, "date_from": None, "date_to": None}

    def rows(tile: str, key: str, **params) -> dict:
        assert t[tile].sql, t[tile].todo
        df = query(t[tile].sql, {**none, **params}, url)
        return {r[key]: r for r in df.to_dict("records")}

    by_color = rows("byColor", "color")
    assert {c: round(r["share_of_color"], 4) for c, r in by_color.items()} == {"Blue": round(66 / 156, 4), "Red": round(90 / 156, 4)}
    assert {c: r["red"] for c, r in by_color.items()} == {"Blue": 90.0, "Red": 90.0}  # the filter replaces the grouping on Color
    bikes = rows("byColor", "color", category="Bikes")  # ALL(Product[Color]) keeps the Category slicer, REMOVEFILTERS(Product) doesn't
    assert {c: (round(r["share_of_color"], 4), round(r["share_of_product"], 4)) for c, r in bikes.items()} == {
        "Blue": (round(66 / 126, 4), round(66 / 156, 4)), "Red": (round(60 / 126, 4), round(60 / 156, 4))}
    by_cat = query(t["byColorCat"].sql, none, url)
    assert sorted(zip(by_cat["color"], by_cat["category"], by_cat["category_revenue"])) == [
        ("Blue", "Bikes", 126.0), ("Red", "Bikes", 126.0), ("Red", "Clothes", 30.0)]  # ALLEXCEPT(Product, Product[Category])

    month = rows("byMonth", "year_month")
    assert {m: r["running"] for m, r in month.items()} == {"2024-01": 36.0, "2024-02": 66.0, "2024-03": 116.0, "2025-01": 156.0}
    assert {m: r["avg_per_day"] for m, r in month.items()} == {"2024-01": 18.0, "2024-02": 30.0, "2024-03": 50.0, "2025-01": 40.0}
    assert month["2024-03"]["margin"] == 50.0  # Revenue - BLANK cost = Revenue
    assert (month["2024-02"]["per_unit"], month["2024-01"]["per_unit"]) == (0.0, 9.0)  # DIVIDE(.., 0 units, 0)
    since_feb = rows("byMonth", "year_month", date_from="2024-02-01")
    assert {m: (r["running"], r["running_all"]) for m, r in since_feb.items()} == {
        "2024-02": (30.0, 66.0), "2024-03": (80.0, 116.0), "2025-01": (120.0, 156.0)}  # ALLSELECTED starts at the slicer, ALL doesn't
    # MAX('Calendar'[Date]) of a MonthName group is its last calendar day, whatever the groups' order
    assert {m: r["running"] for m, r in rows("byMonthName", "month_name").items()} == {"Jan": 156.0, "Feb": 66.0, "Mar": 116.0}
    by_date = rows("byDate", "date")
    assert [by_date[k]["running"] for k in sorted(by_date)] == [16.0, 36.0, 66.0, 116.0, 156.0]
    card = query(t["card"].sql, none, url).to_dict("records")
    assert card == [{"running": 156.0, "avg_per_day": 31.2}]
    assert not t["zeros"].sql and "never BLANK" in t["zeros"].todo  # days without sales would count as 0

    m = _by_name(b.metrics)  # as metrics they depend on the visual's grouping: still TODO with the DAX
    for name, needle in [("share_of_color", "ALL"), ("category_revenue", "ALLEXCEPT"), ("running", "FILTER"), ("avg_per_day", "AVERAGEX")]:
        assert needle in m[name].todo and m[name].source_language == "dax", name


def test_context_sql_shape_and_limits(tmp_path: Path):
    b = _ctx_project(tmp_path)
    t = {x.id: x for x in b.dashboards[0].tiles}
    sql = t["byMonth"].sql
    assert sql.startswith("SELECT\n  g.year_month AS year_month,\n  g._a1 AS revenue,") and ") AS g\nORDER BY 1" in sql
    # marked date table: a filter on 'Calendar'[Date] also drops the YearMonth grouping; MAX() reads the group's calendar days
    running = next(line for line in sql.splitlines() if line.endswith("AS running,"))
    assert "calendar_2.Date <= (SELECT MAX(calendar_1.Date) FROM dbo.DimCalendar AS calendar_1 WHERE" in running
    assert "calendar_1.YearMonth IS NOT DISTINCT FROM g.year_month" in running and "calendar_2.YearMonth" not in running
    assert "1.0 * g._a1 / NULLIF((SELECT SUM(sales_1.Price) FROM dbo.FactSales AS sales_1 LEFT JOIN" in t["byColor"].sql
    # not a date table: the YearMonth grouping stays, so the "running total" is the month's own revenue, as in DAX
    plain = _ctx_project(tmp_path / "plain", calendar=CTX_CALENDAR.replace("\tdataCategory: Time\n\n", "").replace("dateTime", "string"))
    running = next(line for line in {x.id: x for x in plain.dashboards[0].tiles}["byMonth"].sql.splitlines() if line.endswith("AS running,"))
    assert "calendar_2.YearMonth IS NOT DISTINCT FROM g.year_month" in running
    # a bi-directional relationship lets the fact table filter Calendar: no SQL
    bidi = _ctx_project(tmp_path / "bidi", rels=CTX_RELS + "\tcrossFilteringBehavior: bothDirections\n")
    tile = {x.id: x for x in bidi.dashboards[0].tiles}["byMonth"]
    assert not tile.sql and "bi-directional relationship Sales - Calendar" in tile.todo


def test_derived_metrics_follow_dax_blank_and_divide_alternate(tmp_path: Path):
    import duckdb

    m = _by_name(_ctx_project(tmp_path).metrics)
    margin, per_unit = m["margin"], m["per_unit"]
    assert (margin.type, margin.metrics) == ("derived", ["revenue", "sum_cost"])
    assert margin.expr == "(CASE WHEN revenue IS NULL AND sum_cost IS NULL THEN NULL ELSE COALESCE(revenue, 0) - COALESCE(sum_cost, 0) END)"
    assert per_unit.type == "derived" and per_unit.expr == "CASE WHEN coalesce(sum_qty, 0) = 0 THEN 0 ELSE 1.0 * revenue / nullif(sum_qty, 0) END"

    def ev(expr: str, **values) -> object:
        cols = ", ".join(f"{v if v is not None else 'NULL::DOUBLE'} AS {k}" for k, v in values.items())
        return duckdb.connect().execute(f"SELECT {expr} AS v FROM (SELECT {cols})").fetchone()[0]

    assert ev(margin.expr, revenue=50, sum_cost=None) == 50  # BLANK cost: DAX 50, plain SQL NULL
    assert ev(margin.expr, revenue=None, sum_cost=5) == -5
    assert ev(margin.expr, revenue=None, sum_cost=None) is None
    assert ev(per_unit.expr, revenue=30, sum_qty=0) == 0 and ev(per_unit.expr, revenue=30, sum_qty=None) == 0
    assert ev(per_unit.expr, revenue=None, sum_qty=5) is None  # DIVIDE(BLANK, 5, 0) is BLANK, not 0
    assert ev(per_unit.expr, revenue=36, sum_qty=4) == 9
