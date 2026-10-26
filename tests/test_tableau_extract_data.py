"""Data sources whose data lives only in Tableau (tests/fixtures/tableau_extract/, see its README).

Three kinds: a published data source on a Hyper file (`class='hyper'`), a published one on an application connector
that Tableau keeps in an extract (`<extract enabled='true'>`), and a workbook's own Excel and CSV data sources. The
test builds the files Tableau would package (.hyper with Tableau's Hyper API, .xlsx, .csv) from the Dunder Mifflin
data, serves them like Tableau's REST API does with includeExtract=true, and checks the Parquet copies (rows and
Tableau's column types) and the migrated tiles run on DuckDB over them. Expected numbers are computed in pandas with
Tableau's rules: SUM and AVG skip NULLs, NULL is a mark of its own, an empty Excel cell is NULL.
"""

import io
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pandas as pd
import pytest

from unshackle import build
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources import _tableau_data as tdata
from unshackle.sources import _manual, get_source
from unshackle.sources.tableau import TableauSource, _same_formula, extract_only, read_datasource

FIX = Path(__file__).parent / "fixtures" / "tableau_extract"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
LINES, HIST, WB = "00000000-0000-4000-8000-000000000081", "00000000-0000-4000-8000-000000000082", "00000000-0000-4000-8000-000000000083"
V, SITE = "/api/3.24", "site-1"
ORDER_COLS = ["order_id", "status", "purchased_at", "cancelled_at", "quantity", "post_discount_revenue", "paper_type"]


def _parse(tmp_path: Path):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def _ds(xml: str):
    import xml.etree.ElementTree as ET

    return read_datasource(ET.fromstring(xml), "x", "x.tds")


# ------------------------------------------------------------------ detection (no optional dependencies)


def test_data_only_in_tableau_is_detected(tmp_path: Path):
    b = _parse(tmp_path)
    only = {w.item.split(" datasource ")[-1]: w.message for w in b.warnings if "its data is only in Tableau" in w.message}
    assert sorted(only) == ["Accounts", "Order History", "Order Lines", "Products"]  # the workbook's copies don't repeat it
    assert only["Order Lines"].startswith("connects to hyper:")
    assert only["Order History"].startswith("connects to salesforce through an extract:")
    assert only["Accounts"].startswith("connects to excel-direct:") and "--with-data" in only["Accounts"]
    assert not b.queries  # nothing copied, so no load step


def test_extract_only_cases():
    def ds(conn: str, extract: bool = False) -> str:
        ext = ("<extract count='-1' enabled='true' units='records'><connection class='hyper' dbname='x.hyper'>"
               "<relation name='Extract' table='[Extract].[Extract]' type='table' /></connection></extract>") if extract else ""
        return f"<datasource name='d'><connection class='federated'>{conn}</connection>{ext}</datasource>"

    pg = ("<named-connections><named-connection name='pg'><connection class='postgres' dbname='db' server='db.example.com' />"
          "</named-connection></named-connections><relation connection='pg' name='orders' table='[public].[orders]' type='table' />")
    assert extract_only(_ds(ds(pg))) == ""
    assert extract_only(_ds(ds(pg, extract=True))) == ""  # a warehouse holds it; the extract is a cache
    assert extract_only(_ds(ds("", extract=True))) == "is an extract with no connection behind it"
    sheets = pg.replace("class='postgres'", "class='google-sheets'")
    assert extract_only(_ds(ds(sheets, extract=True))) == "connects to google-sheets through an extract"
    assert extract_only(_ds(ds(pg.replace("class='postgres'", "class='box-file'")))) == "connects to box-file"


def test_extract_settings_on_a_warehouse_are_reported(tmp_path: Path):
    # tableau_published: a Postgres data source published with an extract
    shutil.copytree(Path(__file__).parent / "fixtures" / "tableau_published", tmp_path / "migration" / "raw" / "tableau")
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    (w,) = [w for w in b.warnings if w.message.startswith("Tableau answers from this data source's extract")]
    assert w.severity == "info" and "Aggregate data for visible dimensions" in w.message
    assert not [w for w in b.warnings if "only in Tableau" in w.message]


def test_extract_notes():
    import xml.etree.ElementTree as ET

    from unshackle.sources.tableau import _extract_notes

    ext = ET.fromstring(
        "<extract count='1000' enabled='true' units='records'><connection class='hyper' update-time='10/01/2026 12:00:00 AM'>"
        "<refresh increment-key='[purchased_at]' incremental-updates='true' /></connection></extract>")
    assert _extract_notes(ext) == ["a row limit (count 1000, units records)", "incremental refresh on purchased_at",
                                   "last refreshed 10/01/2026 12:00:00 AM"]


def test_inventory_flags_published_data_sources(monkeypatch):
    monkeypatch.setenv("TABLEAU_PAT_NAME", "unshackle")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "s3cret")
    src = TableauSource(client=_client({}), server_url="https://tableau.example.com", site="example")
    inv = src.inventory()
    flags = {i.name: i.extra["data_only_in_tableau"] for i in inv.items if i.kind == "datasource"}
    assert flags == {"Order Lines": True, "Order History": True}
    assert any("2 published data sources hold their data only in Tableau" in n and "--with-data" in n for n in inv.notes)


# ------------------------------------------------------------------ the copy (Hyper API, openpyxl, DuckDB)


@pytest.fixture(scope="module")
def frames():
    duckdb = pytest.importorskip("duckdb")
    orders = duckdb.sql(
        f"select o.order_id, o.status, o.purchased_at, o.cancelled_at, cast(o.quantity as bigint) as quantity, "
        f"cast(o.post_discount_revenue as double) as post_discount_revenue, p.paper_type "
        f"from '{DATA / 'orders.parquet'}' o join '{DATA / 'products.parquet'}' p using (product_id)").df()
    accounts = duckdb.sql(f"select account_id, account_name, business_size, nullif(payment_cycle, '') as payment_cycle, account_manager "
                          f"from '{DATA / 'accounts.parquet'}'").df()
    products = duckdb.sql(f"select product_id, product_name, paper_type, cast(price as double) as price from '{DATA / 'products.parquet'}'").df()
    return {"orders": orders, "accounts": accounts, "products": products}


def _hyper(path: Path, schema: str, df: pd.DataFrame) -> None:
    hapi = pytest.importorskip("tableauhyperapi")
    t = hapi.SqlType
    types = {"order_id": t.text(), "status": t.text(), "purchased_at": t.date(), "cancelled_at": t.date(), "quantity": t.big_int(),
             "post_discount_revenue": t.double(), "paper_type": t.text()}
    table = hapi.TableDefinition(hapi.TableName(schema, "Extract"), [hapi.TableDefinition.Column(c, types[c]) for c in ORDER_COLS])
    rows = [[None if pd.isna(v) else (v.date() if isinstance(v, pd.Timestamp) else v) for v in r] for r in df[ORDER_COLS].itertuples(index=False)]
    with hapi.HyperProcess(hapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(path.parent)}) as hp:
        with hapi.Connection(hp.endpoint, str(path), hapi.CreateMode.CREATE_AND_REPLACE) as con:
            con.catalog.create_schema(schema)
            con.catalog.create_table(table)
            with hapi.Inserter(con, table) as ins:
                ins.add_rows(rows)
                ins.execute()


def _xlsx(df: pd.DataFrame) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Accounts"
    ws.append(list(df.columns))
    for r in df.itertuples(index=False):
        ws.append([None if pd.isna(v) else v for v in r])  # an empty cell for a missing payment cycle
    ws.append([None] * len(df.columns))  # a blank row below the data is not a record
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return out.getvalue()


@pytest.fixture(scope="module")
def packages(frames, tmp_path_factory):
    """What Tableau sends for each item with includeExtract=true."""
    tmp = tmp_path_factory.mktemp("pkg")
    lines, hist = tmp / "order_lines.hyper", tmp / "order_history.hyper"
    _hyper(lines, "Extract", frames["orders"])
    _hyper(hist, "Extract", frames["orders"])
    tds = {d: (FIX / "datasources" / f"{d}.tds").read_bytes() for d in (LINES, HIST)}
    twb = (FIX / "workbooks" / f"{WB}.twb").read_bytes()
    return {
        LINES: _zip({"Order Lines.tds": tds[LINES], "Data/Extracts/order_lines.hyper": lines.read_bytes()}),
        HIST: _zip({"Order History.tds": tds[HIST], "Data/Extracts/order_history.hyper": hist.read_bytes()}),
        WB: _zip({"Extract Sales.twb": twb, "Data/Accounts.xlsx": _xlsx(frames["accounts"]),
                  "Data/Products/products.csv": frames["products"].to_csv(index=False).encode()}),
    }


def _client(packages: dict[str, bytes], seen: list[str] | None = None) -> Client:
    lists = {"workbooks": json.loads((FIX / "workbooks.json").read_text()), "datasources": json.loads((FIX / "datasources.json").read_text()),
             "views": [], "groups": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"{V}/auth/signin":
            return httpx.Response(200, json={"credentials": {"token": "tok", "site": {"id": SITE, "contentUrl": "example"}}})
        if path == f"{V}/auth/signout":
            return httpx.Response(204)
        if path == "/api/metadata/graphql":
            body = json.loads(req.content)
            key = "customSQLTablesConnection" if "customSQLTablesConnection" in body["query"] else "workbooksConnection"
            nodes = [{"luid": luid, "name": "Extract Sales"} for luid in body["variables"].get("luids") or []]
            return httpx.Response(200, json={"data": {key: {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}})
        rest = path.removeprefix(f"{V}/sites/{SITE}/")
        if rest in lists:
            items = lists[rest]
            return httpx.Response(200, json={"pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": str(len(items))},
                                             rest: {rest[:-1]: items}})
        kind, _, rest = rest.partition("/")
        item, _, what = rest.partition("/")
        if what == "permissions":
            return httpx.Response(200, json={"permissions": {}})
        if what == "content":
            if seen is not None:
                seen.append(f"{item} includeExtract={req.url.params['includeExtract']}")
            if req.url.params["includeExtract"] == "true":
                return httpx.Response(200, content=packages[item])
            return httpx.Response(200, content=(FIX / kind / f"{item}.{'tds' if kind == 'datasources' else 'twb'}").read_bytes())
        return httpx.Response(404, json={"error": {"summary": f"no route {path}"}})

    return Client("https://tableau.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(handler))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch, packages):
    monkeypatch.setenv("TABLEAU_PAT_NAME", "unshackle")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "s3cret")
    seen: list[str] = []
    src = TableauSource(client=_client(packages, seen), server_url="https://tableau.example.com", site="example", with_data=True)
    store = RawStore(tmp_path, "tableau")
    store.begin()
    src.extract(store)
    store.save()
    # every item is fetched once as XML; those whose data is only in Tableau again with their data
    assert sorted(seen) == sorted([f"{i} includeExtract=false" for i in (WB, LINES, HIST)] + [f"{i} includeExtract=true" for i in (WB, LINES, HIST)])
    assert store.get_json("_errors.json") == []
    return tmp_path


def test_parquet_copies_keep_rows_and_tableau_types(repo: Path, frames):
    duckdb = pytest.importorskip("duckdb")
    copies = {t["table"]: t for rel in RawStore(repo, "tableau").glob("extracts/*.json")
              for t in RawStore(repo, "tableau").get_json(rel)["tables"]}
    assert sorted(copies) == ["accounts", "order_history", "order_lines", "products"]
    assert {t: copies[t]["from"] for t in copies} == {"order_lines": "connection", "order_history": "extract", "accounts": "connection",
                                                      "products": "connection"}

    def types(table: str) -> dict[str, str]:
        return {r[0]: r[1] for r in duckdb.sql(f"describe select * from '{repo / copies[table]['parquet']}'").fetchall()}

    want = {"order_id": "VARCHAR", "status": "VARCHAR", "purchased_at": "DATE", "cancelled_at": "DATE", "quantity": "BIGINT",
            "post_discount_revenue": "DOUBLE", "paper_type": "VARCHAR"}
    assert types("order_lines") == want and types("order_history") == want
    assert types("accounts") == {"account_id": "VARCHAR", "account_name": "VARCHAR", "business_size": "BIGINT", "payment_cycle": "VARCHAR",
                                 "account_manager": "VARCHAR"}
    assert types("products") == {"product_id": "VARCHAR", "product_name": "VARCHAR", "paper_type": "VARCHAR", "price": "DOUBLE"}
    for table, frame in (("order_lines", "orders"), ("accounts", "accounts"), ("products", "products")):
        assert copies[table]["rows"] == len(frames[frame])  # the blank row under the Excel data is not a record
    cancelled = duckdb.sql(f"select count(cancelled_at) from '{repo / copies['order_lines']['parquet']}'").fetchall()[0][0]
    assert cancelled == frames["orders"]["cancelled_at"].notna().sum() < len(frames["orders"])  # NULL dates stay NULL
    blank = duckdb.sql(f"select count(*) - count(payment_cycle) from '{repo / copies['accounts']['parquet']}'").fetchall()[0][0]
    assert blank == frames["accounts"]["payment_cycle"].isna().sum() > 0  # empty cells are NULL


def test_models_read_the_copies_and_tiles_match(repo: Path, frames, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    b = get_source("tableau").parse(RawStore(repo, "tableau"))
    assert sorted(sm.table for sm in b.semantic_models) == [f"tableau_data.{t}" for t in ("accounts", "order_history", "order_lines", "products")]
    assert b.warehouse == "duckdb"
    (load,) = b.queries
    assert load.name == "Tableau data" and "choose where" in load.todo and "COPY INTO" in load.todo
    assert "create or replace table tableau_data.order_lines as select * from read_parquet('data/tableau/order_lines.parquet');" in load.sql
    copied = [w.message for w in b.warnings if "its data was copied" in w.message]
    assert len(copied) == 4 and not [w for w in b.warnings if "only in Tableau" in w.message]
    assert any("already applied (last refreshed 10/01/2026 12:00:00 AM)" in m for m in copied)  # Order History, from its extract

    monkeypatch.chdir(repo)  # the load step reads the Parquet files relative to the repo root
    con = duckdb.connect()
    con.execute(load.sql)
    tiles = {t.title: t for d in b.dashboards for t in d.tiles if t.sql}

    def run(title: str) -> dict:
        return {r[0]: r[1] for r in con.sql(tiles[title].sql).fetchall()}

    o, a, p = frames["orders"], frames["accounts"], frames["products"]
    done = o[o.status == "COMPLETED"]
    assert run("Revenue by Paper Type") == pytest.approx(done.groupby("paper_type").post_discount_revenue.sum().to_dict())
    monthly = o.groupby(o.purchased_at.dt.to_period("M").dt.to_timestamp()).order_id.nunique()
    assert {pd.Timestamp(k): v for k, v in run("Monthly Orders").items()} == monthly.to_dict()
    # Order History: its extract's columns (the connector's own names are gone)
    assert "from tableau_data.order_history" in tiles["Cancelled Revenue by Status"].sql and "__c" not in tiles["Cancelled Revenue by Status"].sql
    assert run("Cancelled Revenue by Status") == pytest.approx(o.groupby("status").post_discount_revenue.sum().to_dict())
    # NULL payment cycle is a mark of its own
    want = a.groupby("payment_cycle", dropna=False).business_size.sum(min_count=1)
    want = {None if pd.isna(k) else k: v for k, v in want.items()}
    assert run("Business Size by Payment Cycle") == pytest.approx(want)
    assert run("Average Price by Paper Type") == pytest.approx(p.groupby("paper_type").price.mean().to_dict())


def test_a_copy_of_the_extract_has_its_filters_applied(repo: Path):
    """The models read the extract's own rows, so its extract filters are not applied again (a relative date one would count
    from another day); a data source filter still is."""
    tds = next((repo / "migration" / "raw" / "tableau" / "datasources").glob("*82.tds"))
    extract_filter = ("<filter class='categorical' column='[status]'>"
                      "<groupfilter function='member' level='[status]' member='&quot;COMPLETED&quot;' /></filter>")
    ds_filter = ("<filter class='categorical' column='[status]'><groupfilter function='except'>"
                 "<groupfilter function='level-members' level='[status]' />"
                 "<groupfilter function='member' level='[status]' member='&quot;RETURNED&quot;' /></groupfilter></filter>")
    xml = tds.read_text().replace("</extract>", extract_filter + "</extract>").replace("</datasource>", ds_filter + "</datasource>")
    tds.write_text(xml)
    b = get_source("tableau").parse(RawStore(repo, "tableau"))
    sql = next(t.sql for d in b.dashboards for t in d.tiles if t.title == "Cancelled Revenue by Status")
    assert "'COMPLETED'" not in sql and "where (status <> 'RETURNED' or status is null)" in sql
    assert not [rf for rf in b.row_filters if rf.name.endswith("_extract")]


def test_build_writes_the_load_step(repo: Path):
    pytest.importorskip("duckdb")
    build.apply(get_source("tableau").parse(RawStore(repo, "tableau")), repo)
    text = (repo / "queries" / "tableau-data.sql").read_text()
    assert "TODO(unshackle): choose where the 4 tables copied from Tableau live" in text
    assert "copied from the Tableau extract to Parquet" in (repo / "migration" / "report.md").read_text()


def test_notebooks_run_on_the_copies_from_their_own_folder(repo: Path, frames, monkeypatch):
    """The steps the report gives (load queries/tableau-data.sql into a DuckDB file from the repo root, point
    UNSHACKLE_WAREHOUSE_URL at it) are enough to run a notebook with notebooks/ as the working directory."""
    pytest.importorskip("duckdb_engine")
    pytest.importorskip("nbclient")
    import re
    import subprocess
    import sys

    from unshackle.run import run_notebook

    b = get_source("tableau").parse(RawStore(repo, "tableau"))
    build.apply(b, repo)
    report = (repo / "migration" / "report.md").read_text()
    assert "data/tableau/tableau.duckdb" in report and "UNSHACKLE_WAREHOUSE_URL=duckdb:///" in report
    ((cmd, load),) = re.findall(r"`(uv run --no-project --with duckdb python -c \"(.+?)\")`",
                                next(w.message for w in b.warnings if "tableau.duckdb" in w.message))
    assert cmd.startswith("uv run --no-project --with duckdb python -c")
    subprocess.run([sys.executable, "-c", load], cwd=repo, check=True)  # its python part: uv would fetch duckdb, tests stay offline
    db = repo / "data" / "tableau" / "tableau.duckdb"
    assert db.exists()

    monkeypatch.chdir(repo / "notebooks")
    path = next(p for p in (repo / "notebooks").glob("*.ipynb") if "Revenue by Paper Type" in p.read_text())
    html = run_notebook(path, url=f"duckdb:///{db}").read_text()
    o = frames["orders"]
    want = o[o.status == "COMPLETED"].groupby("paper_type").post_discount_revenue.sum()
    assert "Error" not in html
    got = {k: float(v) for k, v in re.findall(r'"paper_type": "([^"]+)", "revenue": ([-0-9.e+]+)', html)}  # the chart's data
    assert got == pytest.approx(want.to_dict())


def test_unreadable_files_are_errors_not_guesses(tmp_path: Path, packages):
    pytest.importorskip("duckdb")
    import xml.etree.ElementTree as ET

    twb = (FIX / "workbooks" / f"{WB}.twb").read_text().replace("cleaning='no'", "cleaning='yes'")
    el = next(e for e in ET.fromstring(twb).findall("datasources/datasource") if e.get("caption") == "Accounts")
    tables, errors = tdata.write_tables(packages[WB], [("x", el, el.find("connection"), "Accounts")], tmp_path, "data/tableau", set())
    assert tables == [] and errors == ["Accounts / Accounts: read with Tableau's Data Interpreter, which cleans the file in ways unshackle can't repeat"]
    tables, errors = tdata.write_tables(b"<?xml version='1.0' ?><workbook />", [], tmp_path, "data/tableau", set())
    assert errors == ["the download is not a packaged file: Tableau sent no extract or data file"]


def test_with_data_needs_the_extra(monkeypatch, tmp_path: Path):
    import importlib.util

    from unshackle.http import SourceError

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "tableauhyperapi" else real(name, *a))
    src = TableauSource(client=_client({}), server_url="https://tableau.example.com", site="example", with_data=True)
    with pytest.raises(SourceError, match=r"--with-data needs the `tableau` extra.*missing: tableauhyperapi"):
        src.extract(RawStore(tmp_path, "tableau"))


def test_types_the_author_changed(tmp_path: Path):
    """A column the author changed in Tableau (text shown as a number) gets Tableau's type; values that don't convert are
    NULL, as Tableau shows them. A change unshackle can't repeat as Tableau does (a date shown as text) keeps the file's type."""
    hapi = pytest.importorskip("tableauhyperapi")
    duckdb = pytest.importorskip("duckdb")
    import datetime
    import xml.etree.ElementTree as ET

    hyper = tmp_path / "loads.hyper"
    t = hapi.SqlType
    table = hapi.TableDefinition(hapi.TableName("public", "Extract"), [
        hapi.TableDefinition.Column("duration", t.text()), hapi.TableDefinition.Column("requested_on", t.date())])
    with hapi.HyperProcess(hapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp_path)}) as hp:
        with hapi.Connection(hp.endpoint, str(hyper), hapi.CreateMode.CREATE_AND_REPLACE) as con:
            con.catalog.create_table(table)  # "public" is there already
            with hapi.Inserter(con, table) as ins:
                ins.add_rows([["1.5", datetime.date(2026, 1, 2)], ["n/a", None], [None, datetime.date(2026, 1, 3)], ["2", None]])
                ins.execute()
    tds = """<datasource name='federated.0loads'><connection class='federated'><named-connections><named-connection name='hyper.0loads'>
      <connection class='hyper' dbname='Data/Extracts/loads.hyper' /></named-connection></named-connections>
      <relation connection='hyper.0loads' name='Extract' table='[public].[Extract]' type='table' />
      <metadata-records>
        <metadata-record class='column'><remote-name>duration</remote-name><local-name>[duration]</local-name><parent-name>[Extract]</parent-name>
          <ordinal>0</ordinal><local-type>string</local-type></metadata-record>
        <metadata-record class='column'><remote-name>requested_on</remote-name><local-name>[requested_on]</local-name><parent-name>[Extract]</parent-name>
          <ordinal>1</ordinal><local-type>date</local-type></metadata-record>
      </metadata-records></connection>
      <column datatype='real' datatype-customized='true' name='[duration]' role='measure' type='quantitative' />
      <column datatype='string' datatype-customized='true' name='[requested_on]' role='dimension' type='nominal' />
    </datasource>"""
    el = ET.fromstring(tds)
    tables, errors = tdata.write_tables(_zip({"loads.tds": tds.encode(), "Data/Extracts/loads.hyper": hyper.read_bytes()}),
                                        [("", el, el.find("connection"), "Viz Loads")], tmp_path / "out", "data/tableau", set())
    assert errors == []
    (copy,) = tables
    rows = duckdb.sql(f"select duration, requested_on from '{tmp_path / 'out' / 'viz_loads.parquet'}'").fetchall()
    assert [r[0] for r in rows] == [1.5, None, None, 2.0]
    assert dict(map(tuple, copy["columns"])) == {"duration": "DOUBLE", "requested_on": "DATE"}
    assert copy["notes"] == ["requested_on: Tableau shows it as string, the extract holds DATE; kept as DATE, convert it as Tableau does"]


def test_live_files_follow_the_extract_rules(tmp_path: Path):
    """Tableau answers a live Excel or text file from a hidden extract, so its calcs follow the extract (Hyper) rules:
    ROUND(x) rounds halves away from zero, as `round(x, 0)`; on a live warehouse it is the warehouse's round()."""
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIX, raw)
    twb = raw / "workbooks" / f"{WB}.twb"
    calc = ("<column caption='Size Band' datatype='real' name='[Calculation_8001]' role='measure' type='quantitative'>"
            "<calculation class='tableau' formula='ROUND([business_size] / 3)' /></column>")
    twb.write_text(twb.read_text().replace("    </datasource>\n    <datasource caption='Products'", f"      {calc}\n    </datasource>\n    <datasource caption='Products'"))
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    accounts = next(sm for sm in b.semantic_models if sm.name == "accounts")
    (band,) = [m for m in accounts.measures if m.name == "size_band"]
    assert band.expr == "round(1.0 * business_size / nullif(3, 0), 0)"
    for cls in ("excel-direct", "textscan", "hyper"):
        conn = (f"<connection class='federated'><named-connections><named-connection name='c'><connection class='{cls}' filename='f' />"
                "</named-connection></named-connections><relation connection='c' name='t' table='[t$]' type='table' /></connection>")
        assert _ds(f"<datasource name='d'>{conn}</datasource>").extract, cls
    assert not _ds("<datasource name='d'><connection class='postgres' dbname='db'><relation name='t' table='[public].[t]' type='table' />"
                   "</connection></datasource>").extract


def test_init_keeps_copied_data_out_of_git(tmp_path: Path):
    from unshackle import scaffold

    fresh = tmp_path / "fresh"
    scaffold.init(fresh)
    assert "/data/tableau/" in (fresh / ".gitignore").read_text().splitlines()

    custom = tmp_path / "custom"  # [paths] data in unshackle.toml
    custom.mkdir()
    (custom / "unshackle.toml").write_text('[paths]\ndata = "warehouse_files"\n')
    (custom / ".gitignore").write_text(".unshackle/\n")
    scaffold.init(custom)
    assert (custom / ".gitignore").read_text().splitlines() == [".unshackle/", "/warehouse_files/tableau/"]

    # the data belongs in Git: the line is removed after copying, and init doesn't add it back
    (fresh / ".gitignore").write_text((fresh / ".gitignore").read_text().replace("/data/tableau/\n", ""))
    (fresh / "data" / "tableau").mkdir(parents=True)
    scaffold.init(fresh)
    assert "/data/tableau/" not in (fresh / ".gitignore").read_text()


# ------------------------------------------------------------------ `unshackle import --with-data`: packaged files


def _downloads(tmp_path: Path, packages) -> list[Path]:
    """The packages as a user downloads them from Tableau (File > Download > Packaged Workbook / Data Source)."""
    out = []
    for name, item in (("Extract Sales.twbx", WB), ("Order Lines.tdsx", LINES), ("Order History.tdsx", HIST)):
        p = tmp_path / "downloads" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(packages[item])
        out.append(p)
    return out


def test_import_copies_packaged_data(tmp_path: Path, packages, frames, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    from click.testing import CliRunner

    from unshackle.cli import main

    repo = tmp_path / "repo"
    res = CliRunner().invoke(main, ["import", "tableau", *map(str, _downloads(tmp_path, packages)), "--repo", str(repo), "--with-data"])
    assert res.exit_code == 0, res.output
    assert "duckdb.connect('data/tableau/tableau.duckdb')" in res.output  # how to run the notebooks on the copies
    store = RawStore(repo, "tableau")
    assert sorted(store.glob("extracts/*.json")) == ["extracts/Extract Sales.json", "extracts/Order History.json", "extracts/Order Lines.json"]
    b = get_source("tableau").parse(store)
    assert sorted(sm.table for sm in b.semantic_models) == [f"tableau_data.{t}" for t in ("accounts", "order_history", "order_lines", "products")]
    assert not [w for w in b.warnings if "only in Tableau" in w.message or "not copied" in w.message]
    (load,) = b.queries
    monkeypatch.chdir(repo)
    con = duckdb.connect()
    con.execute(load.sql)
    tiles = {t.title: t for d in b.dashboards for t in d.tiles if t.sql}
    o, p = frames["orders"], frames["products"]
    done = o[o.status == "COMPLETED"]
    assert {r[0]: r[1] for r in con.sql(tiles["Revenue by Paper Type"].sql).fetchall()} == pytest.approx(
        done.groupby("paper_type").post_discount_revenue.sum().to_dict())
    assert {r[0]: r[1] for r in con.sql(tiles["Average Price by Paper Type"].sql).fetchall()} == pytest.approx(
        p.groupby("paper_type").price.mean().to_dict())


def test_import_without_data_says_how_to_copy_it(tmp_path: Path, packages):
    _manual.run(get_source("tableau"), RawStore(tmp_path, "tableau"), _downloads(tmp_path, packages))
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    only = [w.message for w in b.warnings if "its data is only in Tableau" in w.message]
    assert len(only) == 4 and all("`unshackle import tableau --with-data`" in m for m in only)
    assert not RawStore(tmp_path, "tableau").glob("extracts/*.json")


def test_import_lists_what_it_could_not_copy(tmp_path: Path, packages):
    pytest.importorskip("duckdb")
    pytest.importorskip("tableauhyperapi")
    broken = tmp_path / "Order Lines.tdsx"  # the extract is missing from the package
    with zipfile.ZipFile(io.BytesIO(packages[LINES])) as zf:
        broken.write_bytes(_zip({n: zf.read(n) for n in zf.namelist() if n.endswith(".tds")}))
    src = get_source("tableau")
    src.with_data = True
    _manual.run(src, RawStore(tmp_path, "tableau"), [broken])
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    (w,) = [w for w in b.warnings if "not copied" in w.message]
    assert w.item == "extracts/Order Lines.json data" and "order_lines.hyper is not in the downloaded package" in w.message
    assert any("its data is only in Tableau" in w.message for w in b.warnings)


def test_import_with_data_needs_a_tool_with_data(tmp_path: Path):
    from click.testing import CliRunner

    from unshackle.cli import main

    f = tmp_path / "model.lkml"
    f.write_text("view: x {}")
    res = CliRunner().invoke(main, ["import", "looker", str(f), "--repo", str(tmp_path), "--with-data"])
    assert res.exit_code != 0 and "carry no data of their own" in res.output


# ------------------------------------------------------------------ several extracts (tests/fixtures/tableau_extract_models/)


def _parse_models(tmp_path: Path, twb_edit=None):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIX.parent / "tableau_extract_models", raw)
    if twb_edit:
        twb = next((raw / "workbooks").glob("*.twb"))
        twb.write_text(twb_edit(twb.read_text()))
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def test_each_extract_is_its_own_model(tmp_path: Path):
    """Every extract stores its rows in [Extract].[Extract]: two extract data sources are two tables, not one, and their
    models are named after the data source as `--with-data` names the copies."""
    b = _parse_models(tmp_path)
    models = {sm.name: sm for sm in b.semantic_models}
    assert sorted(models) == ["order_lines", "product_catalog"]
    assert {d.name for d in models["order_lines"].dimensions} == {"purchased_at"}
    assert {d.name for d in models["product_catalog"].dimensions} == {"paper_type"}
    assert models["order_lines"].entities[0].expr == "order_id" and models["product_catalog"].entities[0].expr == "product_id"
    # the same calculation name with two formulas: one metric per model, nothing dropped
    ratios = {m.semantic_model: m.name for m in b.metrics if m.label == "Profit Ratio"}
    assert set(ratios) == {"order_lines", "product_catalog"} and len(set(ratios.values())) == 2
    assert not [w for w in b.warnings if "already exists in semantic model" in w.message or "already a column" in w.message]
    assert len([t for t in b.tables if (t.schema, t.name) == ("Extract", "Extract")]) == 1  # the catalog lists the table once


def test_a_workbook_copy_shares_its_extract_model(tmp_path: Path):
    """A workbook's copy of a published extract data source is the same data source: same model, and a calculation it
    redefines under the published name is reported."""
    calc = ("<column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure' type='quantitative'>"
            "<calculation class='tableau' formula='SUM([quantity]) / COUNTD([order_id])' /></column>")
    b = _parse_models(tmp_path, lambda xml: xml.replace("</datasource>", calc + "</datasource>"))
    assert sorted(sm.name for sm in b.semantic_models) == ["order_lines", "product_catalog"]
    assert [w for w in b.warnings if "already exists in semantic model order_lines with a different formula" in w.message]


# ------------------------------------------------------------------ files (tests/fixtures/tableau_file_models/)


def test_each_file_data_source_is_its_own_model(tmp_path: Path):
    """A sheet or file name (Orders, Sheet1, targets.csv) is the data source's own table, not a shared one: two data
    sources reading a sheet `Orders` are two models, each named after its data source as `--with-data` names the copies."""
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIX.parent / "tableau_file_models", raw)
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    models = {sm.name: sm for sm in b.semantic_models}
    assert sorted(models) == ["budget", "regional_orders", "sales_targets", "store_orders"]
    assert {d.name for d in models["regional_orders"].dimensions} == {"region"}
    assert {d.name for d in models["store_orders"].dimensions} == {"store"}
    # one Profit Ratio per data source; the workbook's copy writes the published formula in other case and spacing
    ratios = {m.semantic_model for m in b.metrics if m.label == "Profit Ratio"}
    assert ratios == {"regional_orders", "store_orders"}
    assert not [w for w in b.warnings if "already exists in semantic model" in w.message or "already a column" in w.message]


def test_formulas_differing_in_case_and_spacing_are_one_calculation():
    assert _same_formula("SUM([Profit])/SUM([Sales])", "sum([Profit]) / sum( [Sales] )")
    assert _same_formula("IF [Region] = 'East' THEN 1 END // east only", "if [Region]='East' then 1 end")
    assert not _same_formula("SUM([Profit])", "SUM([profit])")  # field names and strings as written
    assert not _same_formula("[Region] = 'East'", "[Region] = 'EAST'")
    assert not _same_formula("SUM([Profit])/SUM([Sales])", "SUM([Profit])/COUNTD([Sales])")
