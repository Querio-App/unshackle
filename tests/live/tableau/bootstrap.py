"""Create the test content on a Tableau Cloud (or Server) site, and remove it again.

Called by tests/live/test_tableau_live.py. Tableau Cloud can't reach a local database,
so the data goes up as an extract: the Dunder Mifflin orders joined to accounts and
products, written to a `.hyper` file with Tableau's Hyper API (`tableauhyperapi`, Apache
2.0, used only here). The data source's connection is still the Postgres warehouse, as
when a Desktop user extracts from a database and publishes; Tableau serves the extract
and never connects to the warehouse.

Everything goes into one project per run, `unshackle-live-<run id>` (see tableau_site.py; runs can
go on at the same time):

- published data source "Dunder Mifflin Orders" (.tdsx: XML + extract): orders inner
  join accounts and products, calculated fields (a ratio, an IF, DATETRUNC, a FIXED LOD,
  a user filter used as a data source filter), a hierarchy, a default aggregation;
- workbook "Dunder Mifflin Sales" on it: a bar with a filter, a line by month, a KPI,
  a table filtered by a parameter-driven calc, and a dashboard with all four;
- workbook "Dunder Mifflin LOD" on it: FIXED, INCLUDE and EXCLUDE LODs in 12 sheets and a dashboard
  (see LOD_SHEETS);
- group `unshackle-managers-<run id>` (empty) with View on the project and the workbook.

`teardown()` deletes this run's project (and so its content), its group, and the database and tables
Tableau Catalog lists for its data source's connection (external assets, kept after the data source is
gone; the connection's host has the run id in it). `sweep()` does the same for runs older than
UNSHACKLE_LIVE_TABLEAU_STALE_HOURS (default 6), left over by interrupted runs; it never touches a run
that may still be going. Tableau may also create an "External Assets Default Project" for those the
first time; it is Tableau's, so it is left alone.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from tableau_site import PREFIX, Site, run_age, run_id  # noqa: E402

DATA = Path(__file__).parent.parent / "data" / "dunder_mifflin"
RUN = run_id()
PROJECT = f"{PREFIX}-{RUN}"
GROUP_PREFIX = "unshackle-managers"
GROUP = f"{GROUP_PREFIX}-{RUN}"
DATASOURCE = "Dunder Mifflin Orders"
WORKBOOK = "Dunder Mifflin Sales"
SHEETS = ("Revenue by Paper Type", "Monthly Revenue", "Average Order Value", "Large Orders by Account")
DASHBOARD = "Sales Overview"
# What the data source says it connects to (Tableau never reaches it); Tableau Catalog lists it, so it carries the run id.
WAREHOUSE_HOST = f"warehouse-{RUN}.example.com"
STALE = timedelta(hours=float(os.environ.get("UNSHACKLE_LIVE_TABLEAU_STALE_HOURS", "6")))

# (local name, table, remote name, Tableau type). Duplicate column names in a join get " (table)", as in Desktop.
COLUMNS = [
    ("order_id", "orders", "order_id", "string"),
    ("status", "orders", "status", "string"),
    ("purchased_at", "orders", "purchased_at", "date"),
    ("cancelled_at", "orders", "cancelled_at", "date"),
    ("returned_at", "orders", "returned_at", "date"),
    ("product_id", "orders", "product_id", "string"),
    ("quantity", "orders", "quantity", "integer"),
    ("pre_discount_revenue", "orders", "pre_discount_revenue", "real"),
    ("discount_percentage", "orders", "discount_percentage", "real"),
    ("post_discount_revenue", "orders", "post_discount_revenue", "real"),
    ("days_to_close", "orders", "days_to_close", "integer"),
    ("order_contact_name", "orders", "order_contact_name", "string"),
    ("order_contact_email", "orders", "order_contact_email", "string"),
    ("account_id", "orders", "account_id", "string"),
    ("shipping_id", "orders", "shipping_id", "string"),
    ("account_id (accounts)", "accounts", "account_id", "string"),
    ("account_name", "accounts", "account_name", "string"),
    ("business_size", "accounts", "business_size", "integer"),
    ("payment_cycle", "accounts", "payment_cycle", "string"),
    ("account_manager", "accounts", "account_manager", "string"),
    ("product_id (products)", "products", "product_id", "string"),
    ("product_name", "products", "product_name", "string"),
    ("paper_type", "products", "paper_type", "string"),
    ("price", "products", "price", "real"),
]
REMOTE_TYPE = {"string": 129, "integer": 20, "real": 5, "date": 7}
AGG = {"string": "Count", "integer": "Sum", "real": "Sum", "date": "Year"}

# (local name, caption, datatype, role, type, formula)
CALCS = [
    ("Calculation_1001", "Average Order Value", "real", "measure", "quantitative",
     "SUM([post_discount_revenue]) / COUNTD([order_id])"),
    ("Calculation_1002", "Discount Band", "string", "dimension", "nominal",
     "IF [discount_percentage] > 0 THEN \"Discounted\" ELSE \"Full price\" END"),
    ("Calculation_1003", "Order Month", "date", "dimension", "ordinal", "DATETRUNC('month', [purchased_at])"),
    ("Calculation_1004", "Account Lifetime Revenue", "real", "measure", "quantitative",
     "{FIXED [account_id] : SUM([post_discount_revenue])}"),
    ("Calculation_1005", "Manager Access", "boolean", "dimension", "nominal",
     f"IF ISMEMBEROF('{GROUP}') THEN [account_manager] = USERNAME() ELSE TRUE END"),
    ("Calculation_1006", "Total Revenue", "real", "measure", "quantitative", "SUM([post_discount_revenue])"),
]


# ------------------------------------------------------------------ extract


def build_hyper(path: Path) -> int:
    """orders ⋈ accounts ⋈ products as the single Extract.Extract table Tableau writes for one logical table."""
    import duckdb
    from tableauhyperapi import Connection, CreateMode, HyperProcess, Inserter, SqlType, TableDefinition, TableName, Telemetry

    sql_type = {"string": SqlType.text(), "integer": SqlType.big_int(), "real": SqlType.double(), "date": SqlType.date()}
    alias = {"orders": "o", "accounts": "a", "products": "p"}
    select = ", ".join(f'{alias[t]}."{r}"' + ("::double" if typ == "real" else "") for _, t, r, typ in COLUMNS)
    rows = duckdb.sql(
        f"select {select} from '{DATA / 'orders.parquet'}' o "
        f"join '{DATA / 'accounts.parquet'}' a on o.account_id = a.account_id "
        f"join '{DATA / 'products.parquet'}' p on o.product_id = p.product_id"
    ).fetchall()
    table = TableDefinition(TableName("Extract", "Extract"), [TableDefinition.Column(local, sql_type[typ]) for local, _, _, typ in COLUMNS])
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(path.parent)}) as hyper:
        with Connection(hyper.endpoint, str(path), CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            conn.catalog.create_table(table)
            with Inserter(conn, table) as ins:
                ins.add_rows(rows)
                ins.execute()
    return len(rows)


# ------------------------------------------------------------------ XML


def _records(extract: bool = False) -> str:
    """<metadata-record>s of the federated connection, or of the extract (one [Extract] table, remote names = local names)."""
    pad = "        " if extract else "      "
    out = []
    for i, (local, table, remote, typ) in enumerate(COLUMNS, 1):
        remote, parent = (local, "Extract") if extract else (remote, table)
        out.append(
            f"{pad}<metadata-record class='column'>\n"
            f"{pad}  <remote-name>{remote}</remote-name>\n"
            f"{pad}  <remote-type>{REMOTE_TYPE[typ]}</remote-type>\n"
            f"{pad}  <local-name>[{local}]</local-name>\n"
            f"{pad}  <parent-name>[{parent}]</parent-name>\n"
            f"{pad}  <remote-alias>{remote}</remote-alias>\n"
            f"{pad}  <ordinal>{i}</ordinal>\n"
            f"{pad}  <local-type>{typ}</local-type>\n"
            f"{pad}  <aggregation>{AGG[typ]}</aggregation>\n"
            f"{pad}  <contains-null>true</contains-null>\n"
            f"{pad}</metadata-record>"
        )
    return "\n".join(out)


def _calc_columns() -> str:
    return "\n".join(
        f"  <column caption={quoteattr(cap)} datatype='{dt}' name='[{local}]' role='{role}' type='{typ}'>\n"
        f"    <calculation class='tableau' formula={quoteattr(formula)} />\n"
        f"  </column>"
        for local, cap, dt, role, typ, formula in CALCS
    )


def datasource_tds(hyper_name: str = "Data/Extracts/dunder_mifflin.hyper") -> str:
    pg = "postgres.0a1b2c3d4e5f6g"
    join = (
        "<relation join='inner' type='join'>\n"
        "      <clause type='join'><expression op='='><expression op='[orders].[product_id]' /><expression op='[products].[product_id]' /></expression></clause>\n"
        "      <relation join='inner' type='join'>\n"
        "        <clause type='join'><expression op='='><expression op='[orders].[account_id]' /><expression op='[accounts].[account_id]' /></expression></clause>\n"
        f"        <relation connection='{pg}' name='orders' table='[public].[orders]' type='table' />\n"
        f"        <relation connection='{pg}' name='accounts' table='[public].[accounts]' type='table' />\n"
        "      </relation>\n"
        f"      <relation connection='{pg}' name='products' table='[public].[products]' type='table' />\n"
        "    </relation>"
    )
    cols = "\n".join(f"      <map key='[{local}]' value='[{table}].[{remote}]' />" for local, table, remote, _ in COLUMNS)
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='federated.1unshackle0live' inline='true' source-platform='linux' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='{WAREHOUSE_HOST}' name='{pg}'>
        <connection authentication='username-password' class='postgres' dbname='dunder_mifflin' port='5432' server='{WAREHOUSE_HOST}' sslmode='require' username='tableau_reader' />
      </named-connection>
    </named-connections>
    {join}
    <cols>
{cols}
    </cols>
    <metadata-records>
{_records()}
    </metadata-records>
  </connection>
  <aliases enabled='yes' />
  <column aggregation='Avg' datatype='integer' name='[days_to_close]' role='measure' type='quantitative' />
  <column caption='Revenue' datatype='real' name='[post_discount_revenue]' role='measure' type='quantitative'>
    <desc><formatted-text><run>Order revenue after discounts, in USD.</run></formatted-text></desc>
  </column>
{_calc_columns()}
  <drill-paths>
    <drill-path name='Product'>
      <field>[paper_type]</field>
      <field>[product_name]</field>
    </drill-path>
  </drill-paths>
  <filter class='categorical' column='[Calculation_1005]'>
    <groupfilter function='member' level='[Calculation_1005]' member='true' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />
  </filter>
  <extract count='-1' enabled='true' units='records'>
    <connection access_mode='readonly' author-locale='en_US' class='hyper' dbname='{hyper_name}' default-settings='hyper' schema='Extract' sslmode='' tablename='Extract' update-time='10/01/2026 12:00:00 AM' username='tableau_internal_user'>
      <relation name='Extract' table='[Extract].[Extract]' type='table' />
      <refresh increment-key='' incremental-updates='false' />
      <metadata-records>
{_records(extract=True)}
      </metadata-records>
    </connection>
  </extract>
</datasource>
"""


def datasource_tdsx(path: Path, workdir: Path) -> Path:
    hyper = workdir / "dunder_mifflin.hyper"
    build_hyper(hyper)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dunder_mifflin_orders.tds", datasource_tds())
        zf.write(hyper, "Data/Extracts/dunder_mifflin.hyper")
    return path


DS = "sqlproxy.0unshackle0live"
PARAM = "[Parameters].[Parameter 1]"
PARAM_COLUMN = ("<column caption='Min Order Value' datatype='real' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='500.0'>"
                "<calculation class='tableau' formula='500.0' /><range granularity='50.0' max='5000.0' min='0.0' /></column>")


def _dep(local: str, datatype: str, role: str, typ: str, caption: str = "", formula: str = "") -> str:
    cap = f" caption={quoteattr(caption)}" if caption else ""
    if formula:
        return (f"<column{cap} datatype='{datatype}' name='[{local}]' role='{role}' type='{typ}'>"
                f"<calculation class='tableau' formula={quoteattr(formula)} /></column>")
    return f"<column{cap} datatype='{datatype}' name='[{local}]' role='{role}' type='{typ}' />"


def _inst(local: str, deriv: str, prefix: str, typ: str, short: str) -> str:
    return f"<column-instance column='[{local}]' derivation='{deriv}' name='[{prefix}:{local}:{short}]' pivot='key' type='{typ}' />"


def _sheet(name: str, deps: list[str], mark: str, rows: str, cols: str, encodings: str = "", filters: str = "", params: bool = False) -> str:
    pdeps = ""
    if params:
        pdeps = f"<datasource-dependencies datasource='Parameters'>{PARAM_COLUMN}</datasource-dependencies>"
    return f"""    <worksheet name={quoteattr(name)}>
      <table>
        <view>
          <datasources>
            <datasource caption='{DATASOURCE}' name='{DS}' />
            {"<datasource name='Parameters' />" if params else ""}
          </datasources>
          {pdeps}
          <datasource-dependencies datasource='{DS}'>
            {''.join(deps)}
          </datasource-dependencies>
          {filters}
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane selection-relaxation-option='selection-relaxation-allow'>
            <view><breakdown value='auto' /></view>
            <mark class='{mark}' />
            {f"<encodings>{encodings}</encodings>" if encodings else ""}
          </pane>
        </panes>
        <rows>{rows}</rows>
        <cols>{cols}</cols>
      </table>
      <simple-id uuid='{{{str(uuid.uuid5(uuid.NAMESPACE_URL, name)).upper()}}}' />
    </worksheet>"""


def workbook_twb(server: str, site: str, ds_content_url: str) -> str:
    rev = "[post_discount_revenue]"
    rev_dep = _dep("post_discount_revenue", "real", "measure", "quantitative", caption="Revenue")
    sum_rev = _inst("post_discount_revenue", "Sum", "sum", "quantitative", "qk")
    q = lambda inst: f"[{DS}].{inst}"  # noqa: E731
    bar = _sheet(
        SHEETS[0],
        [_dep("paper_type", "string", "dimension", "nominal"), rev_dep, _dep("status", "string", "dimension", "nominal"),
         _inst("paper_type", "None", "none", "nominal", "nk"), sum_rev, _inst("status", "None", "none", "nominal", "nk")],
        "Bar", q("[none:paper_type:nk]"), q("[sum:post_discount_revenue:qk]"),
        filters=(f"<filter class='categorical' column='[{DS}].[none:status:nk]'>"
                 "<groupfilter function='member' level='[none:status:nk]' member='&quot;COMPLETED&quot;' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />"
                 "</filter>"),
    )
    line = _sheet(
        SHEETS[1],
        [_dep("purchased_at", "date", "dimension", "ordinal"), rev_dep,
         _inst("purchased_at", "Month-Trunc", "tmn", "quantitative", "qk"), sum_rev],
        "Line", q("[sum:post_discount_revenue:qk]"), q("[tmn:purchased_at:qk]"),
    )
    aov = CALCS[0]
    kpi = _sheet(
        SHEETS[2],
        [_dep(aov[0], aov[2], aov[3], aov[4], caption=aov[1], formula=aov[5]), _inst(aov[0], "User", "usr", "quantitative", "qk")],
        "Text", "", "", encodings=f"<text column='{q(f'[usr:{aov[0]}:qk]')}' />",
    )
    large = ("Calculation_2001", "Large Order", "boolean", "dimension", "nominal", f"{rev} >= {PARAM}")
    large_dep = _dep(large[0], large[2], large[3], large[4], caption=large[1], formula=large[5])
    table = _sheet(
        SHEETS[3],
        [_dep("account_name", "string", "dimension", "nominal"), _dep("order_id", "string", "dimension", "nominal"), rev_dep,
         large_dep,
         _inst("account_name", "None", "none", "nominal", "nk"), sum_rev,
         _inst("order_id", "CountD", "ctd", "quantitative", "qk"), _inst(large[0], "None", "none", "nominal", "nk")],
        "Text", q("[none:account_name:nk]"), "",
        encodings=f"<text column='{q('[sum:post_discount_revenue:qk]')}' />",
        filters=(f"<filter class='categorical' column='[{DS}].[none:{large[0]}:nk]'>"
                 f"<groupfilter function='member' level='[none:{large[0]}:nk]' member='true' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />"
                 "</filter>"),
        params=True,
    )
    ds_cols = ("      <column caption='Revenue' datatype='real' name='[post_discount_revenue]' role='measure' type='quantitative' />\n"
               "      <column datatype='integer' name='[days_to_close]' role='measure' type='quantitative' />\n"
               f"      {large_dep}")
    zones = [
        ("<zone h='8000' id='2' type-v2='text' w='100000' x='0' y='0'><formatted-text><run bold='true'>Dunder Mifflin sales. </run>"
         "<run>Revenue is after discounts.</run></formatted-text></zone>"),
        f"<zone h='8000' id='3' mode='slider' param='{PARAM}' type-v2='paramctrl' w='30000' x='70000' y='0' />",
        f"<zone h='42000' id='4' name={quoteattr(SHEETS[2])} w='30000' x='0' y='8000' />",
        f"<zone h='42000' id='5' name={quoteattr(SHEETS[1])} w='70000' x='30000' y='8000' />",
        f"<zone h='50000' id='6' name={quoteattr(SHEETS[0])} w='50000' x='0' y='50000' />",
        f"<zone h='50000' id='7' name={quoteattr(SHEETS[3])} w='50000' x='50000' y='50000' />",
    ]
    windows = "\n".join(f"    <window class='worksheet' name={quoteattr(s)} />" for s in SHEETS)
    viewpoints = "".join(f"<viewpoint name={quoteattr(s)} />" for s in SHEETS)
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook original-version='18.1' source-build='2024.2.0 (20242.24.0613.0835)' source-platform='mac' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <repository-location id='DunderMifflinSales' path='/t/{site}/workbooks' revision='' site='{site}' />
  <datasources>
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      {PARAM_COLUMN}
    </datasource>
{_sqlproxy(server, site, ds_content_url, ds_cols)}
  </datasources>
  <worksheets>
{bar}
{line}
{kpi}
{table}
  </worksheets>
  <dashboards>
    <dashboard name='{DASHBOARD}'>
      <style />
      <size maxheight='800' maxwidth='1200' minheight='800' minwidth='1200' />
      <datasources>
        <datasource name='Parameters' />
        <datasource caption='{DATASOURCE}' name='{DS}' />
      </datasources>
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          {''.join(zones)}
        </zone>
      </zones>
      <simple-id uuid='{{{str(uuid.uuid5(uuid.NAMESPACE_URL, DASHBOARD)).upper()}}}' />
    </dashboard>
  </dashboards>
  <windows source-height='30'>
{windows}
    <window class='dashboard' maximized='true' name='{DASHBOARD}'>
      <viewpoints>{viewpoints}</viewpoints>
      <active id='-1' />
    </window>
  </windows>
</workbook>
"""


# The LOD workbook: the sheets of tests/fixtures/tableau_lod (same formulas), plus sheets for what that
# fixture can't settle offline: an aggregate of a calc over FIXED values, a nested FIXED, and a FIXED value
# under a dimension filter. Its calcs are the workbook's own, on the published data source.
LOD_WORKBOOK = "Dunder Mifflin LOD"
LOD_DASHBOARD = "LOD Checks"
LOD_CALCS = [
    ("Calculation_3101", "Account Revenue", "real", "measure", "quantitative", "{FIXED [account_id] : SUM([post_discount_revenue])}"),
    ("Calculation_3102", "Account Tier", "string", "dimension", "nominal",
     'IF [Calculation_3101] >= 1000000 THEN "Large" ELSEIF [Calculation_3101] >= 250000 THEN "Mid" ELSE "Small" END'),
    ("Calculation_3103", "Avg Account Revenue", "real", "measure", "quantitative", "AVG({FIXED [account_id] : SUM([post_discount_revenue])})"),
    ("Calculation_3104", "First Order Date", "date", "dimension", "ordinal", "{FIXED [account_name] : MIN([purchased_at])}"),
    ("Calculation_3105", "Avg Order Revenue", "real", "measure", "quantitative", "AVG({INCLUDE [order_id] : SUM([post_discount_revenue])})"),
    ("Calculation_3106", "Cycle Revenue", "real", "measure", "quantitative", "{EXCLUDE [paper_type] : SUM([post_discount_revenue])}"),
    ("Calculation_3107", "Account Paper Revenue", "real", "measure", "quantitative",
     "{FIXED [account_id], [paper_type] : SUM([post_discount_revenue])}"),
    ("Calculation_3108", "Share of Account Revenue", "real", "measure", "quantitative", "[post_discount_revenue] / [Calculation_3101]"),
    ("Calculation_3109", "Revenue Share", "real", "measure", "quantitative", "SUM([post_discount_revenue]) / SUM([Calculation_3101])"),
    ("Calculation_3110", "Large Account Orders", "integer", "measure", "quantitative", "IF [Calculation_3101] >= 1000000 THEN 1 ELSE 0 END"),
    ("Calculation_3111", "Cycle Avg Account Revenue", "real", "measure", "quantitative",
     "{FIXED [payment_cycle] : AVG({FIXED [account_id] : SUM([post_discount_revenue])})}"),
]
# (sheet, mark, rows, cols, text, tooltip, status filter): fields as (local name, derivation)
LOD_SHEETS = [
    ("Revenue by Account Tier", "Bar", [("Calculation_3102", "None")], [("post_discount_revenue", "Sum")], None, None, False),
    ("Average Account Revenue", "Text", [], [], ("Calculation_3103", "User"), None, False),
    ("Account Revenue by Payment Cycle", "Bar", [("payment_cycle", "None")], [("Calculation_3101", "Avg")], None, ("post_discount_revenue", "Sum"), False),
    ("Order Size by Paper Type", "Bar", [("paper_type", "None")], [("Calculation_3105", "User")], None, None, False),
    ("Cycle Revenue by Paper Type", "Text", [("paper_type", "None"), ("payment_cycle", "None")], [], ("Calculation_3106", "Sum"),
     ("post_discount_revenue", "Sum"), False),
    ("New Accounts by Month", "Line", [("account_name", "CountD")], [("Calculation_3104", "Month-Trunc")], None, None, False),
    ("Top Account per Paper Type", "Bar", [("paper_type", "None")], [("Calculation_3107", "Max")], None, None, False),
    ("Share of Account Revenue by Status", "Bar", [("status", "None")], [("Calculation_3108", "Sum")], None, None, False),
    ("Revenue Share by Status", "Bar", [("status", "None")], [("Calculation_3109", "User")], None, None, False),
    ("Large Account Orders by Payment Cycle", "Bar", [("payment_cycle", "None")], [("Calculation_3110", "Sum")], None, None, False),
    ("Cycle Avg Account Revenue", "Bar", [("payment_cycle", "None")], [("Calculation_3111", "Avg")], None, None, False),
    ("Account Revenue by Payment Cycle (Completed)", "Bar", [("payment_cycle", "None")], [("Calculation_3101", "Avg")], None, None, True),
]
_DERIV = {"None": ("none", "nk"), "Sum": ("sum", "qk"), "Avg": ("avg", "qk"), "Max": ("max", "qk"), "User": ("usr", "qk"),
          "CountD": ("ctd", "qk"), "Month-Trunc": ("tmn", "qk")}
_BASE = {"post_discount_revenue": ("real", "measure", "quantitative", "Revenue"), "payment_cycle": ("string", "dimension", "nominal", ""),
         "paper_type": ("string", "dimension", "nominal", ""), "status": ("string", "dimension", "nominal", ""),
         "account_name": ("string", "dimension", "nominal", ""), "account_id": ("string", "dimension", "nominal", ""),
         "order_id": ("string", "dimension", "nominal", ""), "purchased_at": ("date", "dimension", "ordinal", "")}


def _sqlproxy(server: str, site: str, ds_content_url: str, columns: str) -> str:
    """The workbook's copy of the published data source, as Desktop writes it."""
    path = f"/t/{site}/datasources" if site else "/datasources"
    return f"""    <datasource caption='{DATASOURCE}' inline='true' name='{DS}' version='18.1'>
      <repository-location derived-from='{server}{path}/{ds_content_url}?rev=1.0' id='{ds_content_url}' path='{path}' revision='1.0' site='{site}' />
      <connection channel='https' class='sqlproxy' dbname='{ds_content_url}' directory='/dataserver' port='443' server='{httpx.URL(server).host}' username=''>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
      </connection>
      <aliases enabled='yes' />
{columns}
    </datasource>"""


def lod_workbook_twb(server: str, site: str, ds_content_url: str) -> str:
    calcs = {c[0]: c for c in LOD_CALCS}

    def dep(local: str) -> str:
        if local in calcs:
            _, cap, dt, role, typ, formula = calcs[local]
            return _dep(local, dt, role, typ, caption=cap, formula=formula)
        dt, role, typ, cap = _BASE[local]
        return _dep(local, dt, role, typ, caption=cap)

    def inst(local: str, deriv: str) -> tuple[str, str]:
        prefix, short = _DERIV[deriv]
        typ = "nominal" if short == "nk" else "quantitative"
        return _inst(local, deriv, prefix, typ, short), f"[{DS}].[{prefix}:{local}:{short}]"

    def reads(local: str) -> list[str]:
        """A field and every column and calc its formula reads, recursively."""
        if local not in calcs:
            return [local]
        found = re.findall(r"\[([A-Za-z_][\w ]*)\]", calcs[local][5])
        return [local] + [r for f in found if f in calcs or f in _BASE for r in reads(f)]

    sheets = []
    for name, mark, rows, cols, text, tooltip, completed in LOD_SHEETS:
        used = list(dict.fromkeys(rows + cols + [f for f in (text, tooltip) if f] + ([("status", "None")] if completed else [])))
        refs = {f: inst(*f) for f in used}
        deps = [dep(loc) for loc in dict.fromkeys(r for loc, _ in used for r in reads(loc))]
        encodings = "".join(f"<{kind} column='{refs[f][1]}' />" for kind, f in (("text", text), ("tooltip", tooltip)) if f)
        filters = (f"<filter class='categorical' column='[{DS}].[none:status:nk]'>"
                   "<groupfilter function='member' level='[none:status:nk]' member='&quot;COMPLETED&quot;' user:ui-domain='database' "
                   "user:ui-enumeration='inclusive' user:ui-marker='enumerate' /></filter>") if completed else ""
        sheets.append(_sheet(name, deps + [r[0] for r in refs.values()], mark, " / ".join(refs[f][1] for f in rows),
                             " / ".join(refs[f][1] for f in cols), encodings=encodings, filters=filters))
    columns = "\n".join(
        f"      <column caption={quoteattr(cap)} datatype='{dt}' name='[{local}]' role='{role}' type='{typ}'>\n"
        f"        <calculation class='tableau' formula={quoteattr(formula)} />\n      </column>"
        for local, cap, dt, role, typ, formula in LOD_CALCS
    )
    names = [s[0] for s in LOD_SHEETS]
    zones = "".join(
        f"<zone h='33333' id='{i + 2}' name={quoteattr(n)} w='25000' x='{(i % 4) * 25000}' y='{(i // 4) * 33333}' />"
        for i, n in enumerate(names)  # a 4 x 3 grid
    )
    windows = "\n".join(f"    <window class='worksheet' name={quoteattr(s)} />" for s in names)
    viewpoints = "".join(f"<viewpoint name={quoteattr(s)} />" for s in names)
    worksheets = "\n".join(sheets)
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook original-version='18.1' source-build='2024.2.0 (20242.24.0613.0835)' source-platform='mac' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <repository-location id='DunderMifflinLOD' path='/t/{site}/workbooks' revision='' site='{site}' />
  <datasources>
{_sqlproxy(server, site, ds_content_url, columns)}
  </datasources>
  <worksheets>
{worksheets}
  </worksheets>
  <dashboards>
    <dashboard name='{LOD_DASHBOARD}'>
      <style />
      <size maxheight='900' maxwidth='1600' minheight='900' minwidth='1600' />
      <datasources>
        <datasource caption='{DATASOURCE}' name='{DS}' />
      </datasources>
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>{zones}</zone>
      </zones>
      <simple-id uuid='{{{str(uuid.uuid5(uuid.NAMESPACE_URL, LOD_DASHBOARD)).upper()}}}' />
    </dashboard>
  </dashboards>
  <windows source-height='30'>
{windows}
    <window class='dashboard' maximized='true' name='{LOD_DASHBOARD}'>
      <viewpoints>{viewpoints}</viewpoints>
      <active id='-1' />
    </window>
  </windows>
</workbook>
"""


# ------------------------------------------------------------------ REST


def _permission_body(group_id: str, caps: dict[str, str]) -> dict[str, Any]:
    return {"permissions": {"granteeCapabilities": [{"group": {"id": group_id}, "capabilities": {"capability": [
        {"name": n, "mode": m} for n, m in caps.items()]}}]}}


def main() -> dict[str, Any]:
    """Create the project, data source, workbook, group and permissions. Returns their ids."""
    site = Site()
    try:
        sweep(site)  # leftovers of interrupted runs, never a run that may still be going
        project = site.api("POST", "/projects", json={"project": {"name": PROJECT, "description": "unshackle live test (temporary)"}})["project"]
        group = site.api("POST", "/groups", json={"group": {"name": GROUP}})["group"]
        with tempfile.TemporaryDirectory() as tmp:
            tdsx = datasource_tdsx(Path(tmp) / "dunder_mifflin_orders.tdsx", Path(tmp))
            ds = site.publish("datasources", DATASOURCE, project["id"], "dunder_mifflin_orders.tdsx", tdsx.read_bytes())
        twb = workbook_twb(site.server, site.content_url, ds["contentUrl"])
        wb = site.publish("workbooks", WORKBOOK, project["id"], "dunder_mifflin_sales.twb", twb.encode())
        lod_twb = lod_workbook_twb(site.server, site.content_url, ds["contentUrl"])
        lod = site.publish("workbooks", LOD_WORKBOOK, project["id"], "dunder_mifflin_lod.twb", lod_twb.encode())
        site.api("PUT", f"/projects/{project['id']}/permissions", json=_permission_body(group["id"], {"Read": "Allow"}))
        site.api("PUT", f"/workbooks/{wb['id']}/permissions",
                 json=_permission_body(group["id"], {"Read": "Allow", "ExportData": "Allow", "Write": "Deny"}))
        views = {v["name"]: v["id"] for w in (wb, lod) for v in site.items(f"/workbooks/{w['id']}/views", "views", "view")}
        for w in (wb, lod):
            site.wait_for_metadata(w["id"])
        return {"project": project["id"], "group": group["id"], "datasource": ds["id"], "workbook": wb["id"], "lod_workbook": lod["id"],
                "views": views}
    finally:
        site.close()


def teardown(site: Site | None = None, run: str | None = None) -> list[str]:
    """Delete one run's project (its data sources and workbooks go with it), group and Catalog database: this run's by
    default. Returns what was deleted."""
    run = run or RUN
    return _delete(site, lambda name: name == f"{PREFIX}-{run}", lambda name: name == f"{GROUP_PREFIX}-{run}",
                   lambda host: host == f"warehouse-{run}.example.com")


def sweep(site: Site | None = None, older_than: timedelta | None = None) -> list[str]:
    """Delete what live runs older than `older_than` (default UNSHACKLE_LIVE_TABLEAU_STALE_HOURS) left behind, found by
    name prefix and the age in the run id. Names from before run ids (no id in them) count as stale."""
    limit = STALE if older_than is None else older_than

    def stale(name: str, prefix: str) -> bool:
        if name == prefix:
            return True
        if not name.startswith(f"{prefix}-"):
            return False
        age = run_age(name)
        return age is not None and age > limit

    return _delete(site, lambda n: stale(n, PREFIX), lambda n: stale(n, GROUP_PREFIX),
                   lambda h: h.endswith(".example.com") and stale(h.removesuffix(".example.com"), "warehouse"))


def _delete(site: Site | None, project, group, host) -> list[str]:
    own = site is None
    site = site or Site()
    gone: list[str] = []
    try:
        for p in site.items("/projects", "projects", "project"):
            if not project(p["name"]):
                continue
            for kind, item in (("workbooks", "workbook"), ("datasources", "datasource")):
                for c in site.items(f"/{kind}", kind, item, filter=f"projectName:eq:{p['name']}"):
                    site.api("DELETE", f"/{kind}/{c['id']}")
                    gone.append(f"{item} {c['name']} ({p['name']})")
            site.api("DELETE", f"/projects/{p['id']}")
            gone.append(f"project {p['name']}")
        for g in site.items("/groups", "groups", "group"):
            if group(g["name"]):
                site.api("DELETE", f"/groups/{g['id']}")
                gone.append(f"group {g['name']}")
        # Tableau Catalog adds the data source's database and tables as external assets; they outlive the data source.
        for db in site.items("/databases", "databases", "database"):
            if host(db.get("hostName") or "") and not db.get("isEmbedded"):
                site.api("DELETE", f"/databases/{db['id']}")
                gone.append(f"database {db['name']} on {db.get('hostName')} (external asset, with its tables)")
    finally:
        if own:
            site.close()
    return gone
