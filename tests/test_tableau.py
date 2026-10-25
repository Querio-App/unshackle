from conftest import notebook_source, todo_for
import io
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import TableauSource, parse_formula, unpack

FIX = Path(__file__).parent / "fixtures" / "tableau"
WB = "3f2a9c1e-7b4d-4e8a-9c21-5d6e7f809a1b"


def _store(tmp_path: Path) -> RawStore:
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    return RawStore(tmp_path, "tableau")


def _by_name(items):
    return {i.name: i for i in items}


def _parse(tmp_path: Path):
    return get_source("tableau").parse(_store(tmp_path))


def test_parse_tables_and_semantic_models(tmp_path: Path):
    b = _parse(tmp_path)
    tables = {t.name: t for t in b.tables}
    assert set(tables) == {"orders", "sales_targets"}  # custom SQL has no physical table
    assert tables["orders"].schema == "analytics"
    assert [c.name for c in tables["orders"].columns][:3] == ["row_id", "order_id", "order_date"]
    assert _by_name(tables["orders"].columns)["sales"].description == "Gross sales in USD, before discounts."
    models = _by_name(b.semantic_models)
    assert set(models) == {"orders", "returns_custom_sql", "sales_targets", "customer_name_lod"}
    orders = models["orders"]
    assert orders.table == "analytics.orders"
    assert orders.default_time_dimension == ""  # several time columns, none declared: no guess
    assert [(e.name, e.type, e.expr) for e in orders.entities] == [("orders", "primary", "row_id"), ("customer_name", "foreign", "")]
    dims = _by_name(orders.dimensions)
    assert dims["order_date"].type == "time" and dims["region"].type == "categorical"
    assert dims["order_size"].expr == "case when sales >= 500 then 'Large' when sales >= 100 then 'Medium' else 'Small' end"
    assert (dims["order_month"].type, dims["order_month"].expr, dims["order_month"].time_granularity) == ("time", "date_trunc('month', order_date)", "month")
    meas = _by_name(orders.measures)
    assert (meas["sales"].agg, meas["discount"].agg) == ("sum", "average")  # default aggregation from metadata-records
    assert (meas["total_sales"].agg, meas["total_sales"].expr) == ("sum", "sales")
    assert (meas["customer_count"].agg, meas["customer_count"].expr) == ("count_distinct", "customer_name")
    assert meas["net_sales"].expr == "sales * (1 - coalesce(discount, 0))"
    assert meas["safe_profit"].expr == "coalesce(profit, 0)"
    returns = models["returns_custom_sql"]
    assert returns.entities[0].expr == "order_id"
    assert _by_name(returns.measures)["returned_orders"].expr == "case when returned = 'Yes' then 1 else 0 end"
    q = _by_name(b.queries)["Returns (Custom SQL) custom SQL"]
    assert "FROM analytics.returns r" in q.sql and "queries/models/returns_custom_sql.sql" in q.todo


def test_parse_metrics_and_todos(tmp_path: Path):
    b = _parse(tmp_path)
    m = _by_name(b.metrics)
    pr = m["profit_ratio"]
    assert (pr.type, pr.numerator, pr.denominator, pr.semantic_model) == ("ratio", "profit", "sales", "orders")
    assert pr.description == "Profit as a share of sales."
    assert (m["profit_per_order"].numerator, m["profit_per_order"].denominator) == ("profit", "orders_order_count")
    assert m["margin_pct"].type == "derived" and m["margin_pct"].expr == "profit_ratio * 100" and m["margin_pct"].metrics == ["profit_ratio"]
    # the same calc name in two datasources is prefixed with the table
    assert {"orders_order_count", "returns_custom_sql_order_count"} <= set(m)
    rr = m["return_rate"]
    assert (rr.type, rr.numerator, rr.denominator) == ("ratio", "returned_orders", "returns_custom_sql_order_count")
    assert m["average_target"].semantic_model == "sales_targets"  # from the published .tds
    rs = m["running_sales"]
    assert "WINDOW_SUM" in rs.todo and "translate-metrics" in rs.todo
    assert rs.source_language == "tableau" and rs.source_expression.startswith("WINDOW_SUM(")
    dims = _by_name(_by_name(b.semantic_models)["orders"].dimensions)
    assert "customer_first_order" not in dims
    # {FIXED [Customer Name] : MIN([Order Date])}: a SQL model at that grain, related to orders on customer_name
    lod = _by_name(b.semantic_models)["customer_name_lod"]
    assert lod.sql == "select customer_name, min(order_date) as customer_first_order\nfrom analytics.orders\ngroup by 1"
    assert [(e.name, e.type) for e in lod.entities] == [("customer_name", "primary")]
    first = _by_name(lod.dimensions)["customer_first_order"]
    assert (first.type, first.time_granularity, first.todo) == ("time", "day", "")
    # an aggregate that reads a parameter: a TODO metric (tiles compute it with :target_margin)
    assert "[Target Margin]" in m["above_target"].todo and ":target_margin" in m["above_target"].todo
    assert m["above_target"].source_expression == "[Profit Ratio] > [Parameters].[Target Margin]"
    assert "ATTR()" in dims["category_label"].todo
    assert "floor(sales / 100) * 100" in dims["sales_bin"].todo
    assert "case when region in ('East', 'West') then 'Coasts' else region end" in dims["region_group"].todo
    assert "set" in dims["top_customers_set"].todo
    assert not any("FIXED" in q.name for q in b.queries)


def test_parse_row_filters_permissions_knowledge(tmp_path: Path):
    b = _parse(tmp_path)
    rf = _by_name(b.row_filters)
    mgr = rf["regional_manager_filter"]
    assert (mgr.table, mgr.predicate, mgr.groups) == ("analytics.orders", "regional_manager = {{ user.email }}", ["*"])
    assert "USERNAME()" in mgr.todo and "(datasource filter)" in mgr.source_ref
    exec_ = rf["manager_or_executive"]
    assert exec_.predicate == "'Executives' in ({{ user.groups }}) or regional_manager = {{ user.email }}"
    assert exec_.groups == ["*"] and "{{ user.groups }}" in exec_.todo
    perms = {(p.principal, p.action, p.resource_id, p.effect) for p in b.permissions}
    assert ("group:Sales Leadership", "view", "executive-overview", "permit") in perms
    assert ("group:Sales Leadership", "edit", "executive-overview", "permit") in perms
    assert ("group:Finance", "view", "superstore-sales-sheets", "permit") in perms
    assert not any(p.action == "edit" and p.principal == "group:Finance" for p in b.permissions)
    assert all("email" in p.todo for p in b.permissions if p.principal.startswith("user:"))
    k = _by_name(b.knowledge)
    body = k["Superstore Sales glossary"].body
    assert "Profit as a share of sales." in body and "`[Profit Ratio] * 100`" in body
    assert "Monthly sales target" in k["Tableau published datasources"].body
    assert "Sales for the East and West regions" in body and "`[analytics].[returns]`" in body
    assert any("Board Pack" in w.item for w in b.warnings)


def test_parse_dashboards_and_tiles(tmp_path: Path):
    b = _parse(tmp_path)
    d = _by_name(b.dashboards)
    ov = d["Executive Overview"]
    assert ov.views == 120 and ov.url == "https://tableau.acme.com/#/site/acme/views/SuperstoreSales/ExecutiveOverview"
    # phone device layout and legend/filter zones are not tiles
    assert ov.layout == [["text-9"], ["total-sales-kpi", "sales-trend"], ["top-customers-by-profit", "sales-map"]]
    assert {(f.name, f.field, f.default, f.kind) for f in ov.filters} == {("region", "region", None, "value"), ("target_margin", "", 0.15, "number")}
    t = {x.id: x for x in ov.tiles}
    assert "Numbers are before returns" in t["text-9"].text

    kpi = t["total-sales-kpi"]
    assert (kpi.kind, kpi.mark) == ("kpi", "text") and kpi.encodings["text"].field == "total_sales"
    assert kpi.sql == "select\n  sum(sales) as total_sales\nfrom analytics.orders"

    line = t["sales-trend"]
    assert (line.kind, line.mark) == ("chart", "line")  # Automatic + month + measure
    assert (line.encodings["x"].field, line.encodings["x"].type, line.encodings["x"].title) == ("month_of_order_date", "temporal", "Month of Order Date")
    assert (line.encodings["y"].field, line.encodings["color"].field) == ("sales", "category")
    assert line.sql == (
        "select\n  date_trunc('month', order_date) as month_of_order_date,\n  category,\n  sum(sales) as sales\n"
        "from analytics.orders\nwhere region in ('East', 'West')\ngroup by 1, 2\norder by 1, 2 nulls first"
    )
    assert ":region" in line.todo
    cfg = line.source_config
    assert cfg["parity_csv"] == "migration/raw/tableau/views/a1000000-0000-4000-8000-000000000002.csv"
    assert cfg["view_id"] == "a1000000-0000-4000-8000-000000000002"
    assert cfg["csv_columns"] == ["Category", "Month of Order Date", "Sales"]
    assert cfg["columns"]["month_of_order_date"] == "Month of Order Date"

    bar = t["top-customers-by-profit"]
    assert bar.mark == "bar"
    assert (bar.encodings["x"].field, bar.encodings["x"].type) == ("profit", "quantitative")  # horizontal: measure on x
    assert (bar.encodings["y"].field, bar.encodings["y"].type) == ("customer_name", "nominal")
    assert bar.encodings["tooltip"].field == "profit_ratio"
    assert "sum(profit) / nullif(sum(sales), 0) as profit_ratio" in bar.sql
    assert bar.sql.endswith("order by sum(profit) desc\nlimit 10") and not bar.todo

    geo = t["sales-map"]
    assert (geo.kind, geo.mark) == ("unknown", "unknown") and "map" in geo.todo and "parameter" not in geo.todo
    assert "(cast(1.0 as double precision) * sum(profit) / nullif(sum(sales), 0)) > :target_margin as above_target" in geo.sql  # the parameter as a bind
    assert geo.encodings["detail"].field == "state" and "state" in geo.sql

    sheets = d["Superstore Sales sheets"]
    st = {x.id: x for x in sheets.tiles}
    assert sheets.views == 26 and sheets.layout == [["ventes-par-catégorie"], ["running-sales"], ["returns-by-region"]]
    pie = st["ventes-par-catégorie"]
    assert pie.mark == "arc" and pie.encodings["theta"].field == "sales" and pie.encodings["color"].field == "category"
    assert st["running-sales"].sql == "" and "WINDOW_SUM" in st["running-sales"].todo
    ret = st["returns-by-region"]
    assert ret.mark == "bar" and ret.sql.startswith("with custom_sql as (\nSELECT r.order_id")
    assert "from custom_sql" in ret.sql and "count(distinct order_id)" in ret.sql


def test_build_and_run_notebook(tmp_path: Path):
    import duckdb

    b = _parse(tmp_path)
    next(sm for sm in b.semantic_models if sm.name == "orders").default_time_dimension = "order_date"  # as if declared
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    sem = yaml.safe_load((tmp_path / "models/semantic/orders.yml").read_text())
    model = sem["semantic_models"][0]
    assert model["model"] == "ref('orders')"
    ossie = yaml.safe_load((tmp_path / "models/semantic/orders.ossie.yaml").read_text())
    assert ossie["datasets"][0]["source"] == "analytics.orders"
    assert model["defaults"] == {"agg_time_dimension": "order_date"}
    metrics = {x["name"]: x for x in sem["metrics"]}
    assert metrics["profit_ratio"]["type_params"] == {"numerator": "profit", "denominator": "sales"}
    text = (tmp_path / "models/semantic/orders.yml").read_text()
    assert "running_sales" not in metrics and todo_for(text, "metric", "running_sales")
    assert not todo_for(text, "dimension", "customer_first_order")
    lod = yaml.safe_load((tmp_path / "models/semantic/customer_name_lod.yml").read_text())["semantic_models"][0]
    assert lod["model"] == "ref('customer_name_lod')" and lod["dimensions"][0]["name"] == "customer_first_order"
    assert "min(order_date) as customer_first_order" in (tmp_path / "models/unshackle/customer_name_lod.sql").read_text()
    assert (tmp_path / "models/semantic/returns_custom_sql.yml").exists()
    assert (tmp_path / "catalog/warehouse/analytics/orders.yml").exists()
    assert (tmp_path / "knowledge/superstore-sales-glossary.md").exists()
    rows = yaml.safe_load((tmp_path / "policies/row_filters.tableau.yml").read_text())["row_filters"]
    assert any(r["predicate"] == "regional_manager = {{ user.email }}" for r in rows)
    cedar = (tmp_path / "policies/tableau.cedar").read_text()
    assert 'principal in Group::"Sales Leadership"' in cedar and 'resource in Notebook::"executive-overview"' in cedar
    assert sorted(p.name for p in (tmp_path / "notebooks").glob("*.ipynb")) == ["executive-overview.ipynb", "superstore-sales-sheets.ipynb"]
    for nb in (tmp_path / "notebooks").glob("*.ipynb"):
        notebook_source(nb)
    ov = notebook_source(tmp_path / "notebooks/executive-overview.ipynb")
    assert "mark_line(point=True)" in ov and "mark_text()" in ov and "TODO(unshackle)" in ov
    report = (tmp_path / "migration/report.md").read_text()
    assert "WINDOW_SUM" in report and "Board Pack" in report

    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema analytics")
    con.execute(
        "create table analytics.orders as select * from (values "
        "(1, 'O-1', date '2025-01-05', 'Ann Lee', 'California', 'West', 'wes@acme.com', 'Furniture', 1200.5, 2, 0.1, 300.0),"
        "(2, 'O-2', date '2025-01-20', 'Bo Chen', 'New York', 'East', 'eve@acme.com', 'Technology', 2200.0, 1, 0.0, 450.0),"
        "(3, 'O-3', date '2025-02-11', 'Ann Lee', 'Texas', 'Central', 'cal@acme.com', 'Furniture', 980.0, 3, 0.2, -20.0)"
        ") t(row_id, order_id, order_date, customer_name, state, region, regional_manager, category, sales, quantity, discount, profit)"
    )
    con.execute("create table analytics.returns as select * from (values ('O-1', 'Yes'), ('O-3', 'No')) t(order_id, returned)")
    con.close()
    from unshackle.run import notebook_queries, query, run_notebook

    path = tmp_path / "notebooks/executive-overview.ipynb"
    out = run_notebook(path, url=f"duckdb:///{db}")
    html = out.read_text()
    assert "vegaEmbed" in html and "Sales Map" in html  # the map tile shows its data instead of a chart
    params, sqls = notebook_queries(path)
    trend = query(sqls["sales_trend"], params, url=f"duckdb:///{db}")
    assert list(trend.columns) == ["month_of_order_date", "category", "sales"] and len(trend) == 2
    sheets = tmp_path / "notebooks/superstore-sales-sheets.ipynb"
    params, sqls = notebook_queries(sheets)
    returns = query(sqls["returns_by_region"], params, url=f"duckdb:///{db}")
    assert dict(zip(returns["region"], returns["returned_orders"])) == {"West": 1, "Central": 0}
    run_notebook(sheets, url=f"duckdb:///{db}")


def test_formula_parser():
    from unshackle.sources import _aggexpr as ax
    from unshackle.sources._tableau_calc import Renderer

    node = parse_formula("CASE [Region] WHEN 'East' THEN 1 WHEN 'West' THEN 2 ELSE 0 END")
    assert ax.to_sql(node, lambda r: r.lower()) == "case when region = 'East' then 1 when region = 'West' then 2 else 0 end"
    node = parse_formula("IIF([Profit] > 0, 'gain', 'loss')")  # NULL when the test is NULL
    assert Renderer(lambda r: r.lower())(node) == "case when profit > 0 then 'gain' when not (profit > 0) then 'loss' end"
    node = parse_formula("IF [a] > #2024-01-01# THEN 1 END")
    assert ax.to_sql(node, lambda r: r) == "case when a > cast('2024-01-01' as date) then 1 end"


def test_missing_workbook_xml_falls_back_to_metadata(tmp_path: Path):
    store = _store(tmp_path)
    (store.root / "workbooks" / f"{WB}.twb").unlink()
    b = TableauSource().parse(RawStore(tmp_path, "tableau"))
    m = _by_name(b.metrics)
    assert "superstore_sales_customer_first_order" in m and "Metadata API" in m["superstore_sales_customer_first_order"].todo
    assert not b.dashboards and any("XML was not downloaded" in w.message for w in b.warnings)


def test_published_datasource_link(tmp_path: Path):
    store = RawStore(tmp_path, "tableau")
    shutil.copytree(FIX / "datasources", store.root / "datasources")
    shutil.copy(FIX / "datasources.json", store.root / "datasources.json")
    store.put_text(
        "workbooks/wb9.twb",
        """<?xml version='1.0' encoding='utf-8' ?>
<workbook version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <datasources>
    <datasource caption='Sales Targets' inline='true' name='sqlproxy.1abc' version='18.1'>
      <repository-location id='SalesTargets' path='/t/acme/datasources' revision='1.2' site='acme' />
      <connection channel='https' class='sqlproxy' dbname='SalesTargets' port='443' server='tableau.acme.com' server-ds-friendly-name='Sales Targets'>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
      </connection>
      <column caption='Target (k)' datatype='real' name='[Calculation_77]' role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([Target]) / 1000' />
      </column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Targets by Region'>
      <table>
        <view>
          <datasources><datasource caption='Sales Targets' name='sqlproxy.1abc' /></datasources>
          <aggregation value='true' />
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[sqlproxy.1abc].[sum:Target:qk]</rows>
        <cols>[sqlproxy.1abc].[none:Region:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>""",
    )
    b = TableauSource().parse(RawStore(tmp_path, "tableau"))
    assert [sm.name for sm in b.semantic_models] == ["sales_targets"]  # one model for the .tds and the workbook proxy
    (d,) = b.dashboards
    (tile,) = d.tiles
    assert tile.sql == "select\n  region,\n  sum(target_usd) as target\nfrom finance.sales_targets\ngroup by 1\norder by 1 nulls first"
    assert "aggregate not translated" in _by_name(b.metrics)["target_k"].todo


# ------------------------------------------------------------------ extract with a mock API

SITE = "site-luid-1"
V = "/api/3.24"


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Data/Extracts/federated.hyper", b"\x00binary")
        zf.writestr(name, data)
    return buf.getvalue()


def _mock_tableau(calls: list[str], gql: list[dict]):
    twb = (FIX / "workbooks" / f"{WB}.twb").read_bytes()
    tds = (FIX / "datasources" / "d2000000-0000-4000-8000-000000000001.tds").read_bytes()
    tiny = b"<?xml version='1.0' encoding='utf-8' ?><workbook version='18.1'><datasources/><worksheets/></workbook>"
    workbooks = [
        {"id": WB, "name": "Superstore Sales", "project": {"name": "Sales"}, "webpageUrl": "https://tableau.acme.com/#/site/acme/workbooks/4821"},
        {"id": "wb2", "name": "Tiny", "project": {"name": "Ops"}},
        {"id": "wb3", "name": "Secret", "project": {"name": "HR"}},
    ]
    views = json.loads((FIX / "views.json").read_text()) + [
        {"id": "v-tiny", "name": "Tiny Sheet", "contentUrl": "Tiny/sheets/TinySheet", "workbook": {"id": "wb2"}, "usage": {"totalViewCount": "99"}},
    ]
    datasources = json.loads((FIX / "datasources.json").read_text())
    groups = json.loads((FIX / "groups.json").read_text())

    def page(req: httpx.Request, key: str, item: str, rows: list) -> httpx.Response:
        size, n = int(req.url.params["pageSize"]), int(req.url.params["pageNumber"])
        chunk = rows[(n - 1) * size: n * size]
        return httpx.Response(200, json={"pagination": {"pageNumber": str(n), "pageSize": str(size), "totalAvailable": str(len(rows))}, key: {item: chunk}})

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        calls.append(f"{req.method} {path}" + (f"?page={req.url.params['pageNumber']}" if "pageNumber" in req.url.params else ""))
        if path == f"{V}/auth/signin":
            creds = json.loads(req.content)["credentials"]
            assert creds["personalAccessTokenName"] == "unshackle" and creds["personalAccessTokenSecret"] == "s3cret"
            assert creds["site"] == {"contentUrl": "acme"}
            return httpx.Response(200, json={"credentials": {"token": "tok", "site": {"id": SITE, "contentUrl": "acme"}, "user": {"id": "u1"}}})
        assert req.headers["x-tableau-auth"] == "tok"
        if path == f"{V}/auth/signout":
            return httpx.Response(204)
        if path == "/api/metadata/graphql":
            body = json.loads(req.content)
            gql.append(body["variables"])
            if "customSQLTablesConnection" in body["query"]:
                return httpx.Response(200, json={"data": {"customSQLTablesConnection": {
                    "nodes": [{"id": "c1", "name": "Custom SQL Query", "query": "SELECT 1", "downstreamWorkbooks": [{"luid": WB}]}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
            luids = body["variables"]["luids"]
            if len(luids) > 1:
                return httpx.Response(200, json={
                    "data": {"workbooksConnection": {"nodes": [], "pageInfo": {"hasNextPage": False}}},
                    "errors": [{"message": "Showing partial results. The request exceeded the 20000 node limit.",
                                "extensions": {"severity": "WARNING", "code": "NODE_LIMIT_EXCEEDED", "properties": {"nodeLimit": 20000}}}]})
            node = json.loads((FIX / "metadata" / f"{WB}.json").read_text()) if luids[0] == WB else {"luid": luids[0], "name": "Tiny"}
            node.pop("customSQLTables", None)
            return httpx.Response(200, json={"data": {"workbooksConnection": {"nodes": [node], "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
        base = f"{V}/sites/{SITE}"
        if path == f"{base}/workbooks":
            return page(req, "workbooks", "workbook", workbooks)
        if path == f"{base}/views":
            assert req.url.params["includeUsageStatistics"] == "true"
            return page(req, "views", "view", views)
        if path == f"{base}/datasources":
            return page(req, "datasources", "datasource", datasources)
        if path == f"{base}/groups":
            return page(req, "groups", "group", groups)
        if path == f"{base}/workbooks/{WB}/content":
            assert req.url.params["includeExtract"] == "false"
            return httpx.Response(200, content=_zip("Superstore Sales.twb", twb), headers={"content-type": "application/octet-stream"})
        if path == f"{base}/workbooks/wb2/content":
            return httpx.Response(200, content=tiny, headers={"content-type": "application/xml"})
        if path == f"{base}/workbooks/wb3/content":
            return httpx.Response(403, json={"error": {"code": "403004", "summary": "Forbidden"}})
        if path == f"{base}/workbooks/{WB}/permissions":
            return httpx.Response(200, json=json.loads((FIX / "permissions" / f"{WB}.json").read_text()))
        if path.startswith(f"{base}/datasources/") and path.endswith("/content"):
            return httpx.Response(200, content=_zip("Sales Targets.tds", tds))
        if path == f"{base}/views/a1000000-0000-4000-8000-000000000001/data":
            return httpx.Response(404, json={"error": {"summary": "Not found"}})
        if path.startswith(f"{base}/views/") and path.endswith("/data"):
            if req.headers.get("accept") == "text/csv":  # what Tableau Cloud does
                return httpx.Response(406, text="<!doctype html><title>HTTP Status 406 – Not Acceptable</title>")
            return httpx.Response(200, content=b"Category,Sales\r\nFurniture,10\r\n", headers={"content-type": "text/csv"})
        return httpx.Response(404, json={"error": {"summary": f"no route {path}"}})

    return handler


def _source(monkeypatch, calls, gql, **kw) -> TableauSource:
    monkeypatch.setenv("TABLEAU_PAT_NAME", "unshackle")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "s3cret")
    client = Client("https://tableau.acme.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(_mock_tableau(calls, gql)))
    return TableauSource(client=client, server_url="https://tableau.acme.com", site="acme", page_size=2, **kw)


def test_extract_with_mock_api(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    gql: list[dict] = []
    src = _source(monkeypatch, calls, gql, top_views=3)
    store = RawStore(tmp_path, "tableau")
    store.begin()
    src.extract(store)
    store.save()
    assert calls[0] == f"POST {V}/auth/signin" and calls.count(f"POST {V}/auth/signin") == 1
    assert f"GET {V}/sites/{SITE}/workbooks?page=2" in calls  # pagination
    assert calls[-1] == f"POST {V}/auth/signout"
    assert len(store.get_json("workbooks.json")) == 3 and len(store.get_json("views.json")) == 9
    twb = store.path(f"workbooks/{WB}.twb").read_bytes()
    assert twb.startswith(b"<?xml") and b"Superstore" in twb  # unzipped from .twbx
    assert store.exists("workbooks/wb2.twb") and not store.exists("workbooks/wb3.twb")
    assert store.path("datasources/d2000000-0000-4000-8000-000000000001.tds").read_bytes().startswith(b"<?xml")
    assert store.exists(f"permissions/{WB}.json")
    # NODE_LIMIT_EXCEEDED on the batch of two -> retried one workbook at a time
    assert [len(v["luids"]) for v in gql if "luids" in v] == [2, 1, 1]
    meta = store.get_json(f"metadata/{WB}.json")
    assert meta["customSQLTables"][0]["query"] == "SELECT 1" and store.exists("metadata/wb2.json")
    # top 3 viewed views of extracted workbooks: Executive Overview (404), Tiny Sheet, Sales Trend
    assert store.exists("views/v-tiny.csv") and store.exists("views/a1000000-0000-4000-8000-000000000002.csv")
    errors = store.get_json("_errors.json")
    items = " ".join(e["item"] for e in errors)
    assert "Secret (wb3)" in items and "a1000000-0000-4000-8000-000000000001" in items and "wb2 permissions" in items
    b = TableauSource().parse(RawStore(tmp_path, "tableau"))
    assert "Executive Overview" in {d.name for d in b.dashboards}
    assert any("Secret" in w.item for w in b.warnings)
    trend = next(t for d in b.dashboards for t in d.tiles if t.id == "sales-trend")
    assert trend.source_config["parity_csv"].endswith("a1000000-0000-4000-8000-000000000002.csv")


def test_extract_only_and_inventory(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    src = _source(monkeypatch, calls, [])
    store = RawStore(tmp_path, "tableau")
    src.extract(store, {"view:v-tiny"})
    assert store.glob("workbooks/*.twb") == ["workbooks/wb2.twb"]
    assert not store.glob("datasources/*.tds")  # the tiny workbook uses no published datasource

    inv = _source(monkeypatch, [], []).inventory()
    wbs = {i.id: i for i in inv.items if i.kind == "workbook"}
    assert wbs[f"workbook:{WB}"].views == 323 and wbs[f"workbook:{WB}"].parent == "Sales"
    view = next(i for i in inv.items if i.id == "view:a1000000-0000-4000-8000-000000000002")
    assert view.views == 85 and view.url == "https://tableau.acme.com/#/site/acme/views/SuperstoreSales/SalesTrend"
    assert any(i.kind == "datasource" for i in inv.items)
    assert any("metadata-services" in n for n in inv.notes)


def test_unpack_plain_and_zip():
    assert unpack(b"<workbook/>", ".twb") == b"<workbook/>"
    assert unpack(_zip("a/b.twb", b"<workbook/>"), ".twb") == b"<workbook/>"


def test_same_table_in_two_workbooks_merges(tmp_path: Path):
    store = _store(tmp_path)
    twb = (store.root / "workbooks" / f"{WB}.twb").read_text(encoding="utf-8")
    other = twb.replace("formula='SUM([Sales])'", "formula='SUM([Sales]) * 0.9'", 1)
    (store.root / "workbooks" / "wb-copy.twb").write_text(other, encoding="utf-8")
    b = TableauSource().parse(RawStore(tmp_path, "tableau"))
    assert sorted(sm.name for sm in b.semantic_models) == ["customer_name_lod", "orders", "returns_custom_sql", "sales_targets"]
    assert [m.name for m in b.metrics].count("total_sales") == 1
    assert any("different formula" in w.message and "Total Sales" in w.item for w in b.warnings)
    assert {d.slug for d in b.dashboards} >= {"executive-overview", "wb-copy-executive-overview"}


def test_workbook_missing_from_metadata_api_is_reported(tmp_path: Path, monkeypatch):
    """Tableau indexes new content with a delay: a workbook the Metadata API doesn't know yet is an error, not silence."""
    monkeypatch.setenv("TABLEAU_PAT_NAME", "unshackle")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "s3cret")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/signin"):
            return httpx.Response(200, json={"credentials": {"token": "tok", "site": {"id": SITE, "contentUrl": "acme"}, "user": {"id": "u1"}}})
        body = json.loads(req.content)
        key = "customSQLTablesConnection" if "customSQLTablesConnection" in body["query"] else "workbooksConnection"
        return httpx.Response(200, json={"data": {key: {"nodes": [], "pageInfo": {"hasNextPage": False}}}})

    client = Client("https://tableau.acme.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(handler))
    src = TableauSource(client=client, server_url="https://tableau.acme.com", site="acme")
    errors: list[dict] = []
    src._metadata(RawStore(tmp_path, "tableau"), ["wb-new"], errors)
    assert [e["item"] for e in errors] == ["metadata wb-new"] and "indexed with a delay" in errors[0]["error"]
