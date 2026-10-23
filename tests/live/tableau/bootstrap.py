"""Create the test content on a Tableau Cloud (or Server) site, and remove it again.

Called by tests/live/test_tableau_live.py. Tableau Cloud can't reach a local database,
so the data goes up as an extract: the Dunder Mifflin orders joined to accounts and
products, written to a `.hyper` file with Tableau's Hyper API (`tableauhyperapi`, Apache
2.0, used only here). The data source's connection is still the Postgres warehouse, as
when a Desktop user extracts from a database and publishes; Tableau serves the extract
and never connects to the warehouse.

Everything goes into one project (`unshackle-live` by default):

- published data source "Dunder Mifflin Orders" (.tdsx: XML + extract): orders inner
  join accounts and products, calculated fields (a ratio, an IF, DATETRUNC, a FIXED LOD,
  a user filter used as a data source filter), a hierarchy, a default aggregation;
- workbook "Dunder Mifflin Sales" on it: a bar with a filter, a line by month, a KPI,
  a table filtered by a parameter-driven calc, and a dashboard with all four;
- group `unshackle-managers` (empty) with View on the project and the workbook.

`teardown()` deletes the project (and so its content) and the group.
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

import httpx

DATA = Path(__file__).parent.parent / "data" / "dunder_mifflin"
PROJECT = os.environ.get("UNSHACKLE_LIVE_TABLEAU_PROJECT", "unshackle-live")
GROUP = "unshackle-managers"
DATASOURCE = "Dunder Mifflin Orders"
WORKBOOK = "Dunder Mifflin Sales"
SHEETS = ("Revenue by Paper Type", "Monthly Revenue", "Average Order Value", "Large Orders by Account")
DASHBOARD = "Sales Overview"

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


def _records(parent_of: callable) -> str:
    out = []
    for i, (local, table, remote, typ) in enumerate(COLUMNS, 1):
        out.append(
            "      <metadata-record class='column'>\n"
            f"        <remote-name>{remote}</remote-name>\n"
            f"        <remote-type>{REMOTE_TYPE[typ]}</remote-type>\n"
            f"        <local-name>[{local}]</local-name>\n"
            f"        <parent-name>[{parent_of(table)}]</parent-name>\n"
            f"        <remote-alias>{remote}</remote-alias>\n"
            f"        <ordinal>{i}</ordinal>\n"
            f"        <local-type>{typ}</local-type>\n"
            f"        <aggregation>{AGG[typ]}</aggregation>\n"
            "        <contains-null>true</contains-null>\n"
            "      </metadata-record>"
        )
    return "\n".join(out)


def _extract_records() -> str:
    out = []
    for i, (local, _, _, typ) in enumerate(COLUMNS, 1):
        out.append(
            "        <metadata-record class='column'>\n"
            f"          <remote-name>{local}</remote-name>\n"
            f"          <remote-type>{REMOTE_TYPE[typ]}</remote-type>\n"
            f"          <local-name>[{local}]</local-name>\n"
            "          <parent-name>[Extract]</parent-name>\n"
            f"          <remote-alias>{local}</remote-alias>\n"
            f"          <ordinal>{i}</ordinal>\n"
            f"          <local-type>{typ}</local-type>\n"
            f"          <aggregation>{AGG[typ]}</aggregation>\n"
            "          <contains-null>true</contains-null>\n"
            "        </metadata-record>"
        )
    return "\n".join(out)


def _calc_columns(indent: str = "  ") -> str:
    return "\n".join(
        f"{indent}<column caption={quoteattr(cap)} datatype='{dt}' name='[{local}]' role='{role}' type='{typ}'>\n"
        f"{indent}  <calculation class='tableau' formula={quoteattr(formula)} />\n"
        f"{indent}</column>"
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
      <named-connection caption='warehouse.example.com' name='{pg}'>
        <connection authentication='username-password' class='postgres' dbname='dunder_mifflin' port='5432' server='warehouse.example.com' sslmode='require' username='tableau_reader' />
      </named-connection>
    </named-connections>
    {join}
    <cols>
{cols}
    </cols>
    <metadata-records>
{_records(lambda t: t)}
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
{_extract_records()}
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
        pdeps = ("<datasource-dependencies datasource='Parameters'>"
                 "<column caption='Min Order Value' datatype='real' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='500.0'>"
                 "<calculation class='tableau' formula='500.0' /><range granularity='50.0' max='5000.0' min='0.0' /></column>"
                 "</datasource-dependencies>")
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
    host = httpx.URL(server).host
    path = f"/t/{site}/datasources" if site else "/datasources"
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
    table = _sheet(
        SHEETS[3],
        [_dep("account_name", "string", "dimension", "nominal"), _dep("order_id", "string", "dimension", "nominal"), rev_dep,
         _dep(*large[:1], large[2], large[3], large[4], caption=large[1], formula=large[5]),
         _inst("account_name", "None", "none", "nominal", "nk"), sum_rev,
         _inst("order_id", "CountD", "ctd", "quantitative", "qk"), _inst(large[0], "None", "none", "nominal", "nk")],
        "Text", q("[none:account_name:nk]"), "",
        encodings=f"<text column='{q('[sum:post_discount_revenue:qk]')}' />",
        filters=(f"<filter class='categorical' column='[{DS}].[none:{large[0]}:nk]'>"
                 f"<groupfilter function='member' level='[none:{large[0]}:nk]' member='true' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />"
                 "</filter>"),
        params=True,
    )
    ds_cols = "\n".join(
        f"      <column{' caption=' + quoteattr(c) if c else ''} datatype='{dt}' name='[{local}]' role='{role}' type='{typ}' />"
        for local, c, dt, role, typ in [
            ("post_discount_revenue", "Revenue", "real", "measure", "quantitative"),
            ("days_to_close", "", "integer", "measure", "quantitative"),
        ]
    )
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
      <column caption='Min Order Value' datatype='real' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='500.0'>
        <calculation class='tableau' formula='500.0' />
        <range granularity='50.0' max='5000.0' min='0.0' />
      </column>
    </datasource>
    <datasource caption='{DATASOURCE}' inline='true' name='{DS}' version='18.1'>
      <repository-location derived-from='{server}{path}/{ds_content_url}?rev=1.0' id='{ds_content_url}' path='{path}' revision='1.0' site='{site}' />
      <connection channel='https' class='sqlproxy' dbname='{ds_content_url}' directory='/dataserver' port='443' server='{host}' username=''>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
      </connection>
      <aliases enabled='yes' />
{ds_cols}
      <column caption='Large Order' datatype='boolean' name='[Calculation_2001]' role='dimension' type='nominal'>
        <calculation class='tableau' formula={quoteattr(f"{rev} >= {PARAM}")} />
      </column>
    </datasource>
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


# ------------------------------------------------------------------ REST


class Site:
    """A signed-in REST session (PAT from TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET)."""

    def __init__(self, server: str | None = None, site: str | None = None, version: str | None = None):
        self.server = (server or os.environ["TABLEAU_SERVER_URL"]).rstrip("/")
        self.content_url = site if site is not None else os.environ.get("TABLEAU_SITE", "")
        self.version = version or os.environ.get("TABLEAU_API_VERSION") or "3.24"
        self.http = httpx.Client(base_url=self.server, timeout=300, headers={"Accept": "application/json"})
        creds = self.call("POST", f"/api/{self.version}/auth/signin", json={"credentials": {
            "personalAccessTokenName": os.environ["TABLEAU_PAT_NAME"],
            "personalAccessTokenSecret": os.environ["TABLEAU_PAT_SECRET"],
            "site": {"contentUrl": self.content_url},
        }})["credentials"]
        self.http.headers["X-Tableau-Auth"] = creds["token"]
        self.site_id = creds["site"]["id"]
        self.user_id = creds["user"]["id"]

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path.split('?')[0]} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") and r.content else r

    def api(self, method: str, path: str, **kw: Any) -> Any:
        return self.call(method, f"/api/{self.version}/sites/{self.site_id}{path}", **kw)

    def items(self, path: str, key: str, item: str, **params: Any) -> list[dict[str, Any]]:
        data = self.api("GET", path, params={"pageSize": 1000, **params})
        found = (data.get(key) or {}).get(item) or []
        return [found] if isinstance(found, dict) else found

    def close(self) -> None:
        try:
            self.call("POST", f"/api/{self.version}/auth/signout")
        finally:
            self.http.close()

    def publish(self, kind: str, name: str, project_id: str, filename: str, content: bytes) -> dict[str, Any]:
        """POST .../{workbooks|datasources}?overwrite=true as multipart/mixed (request_payload + file)."""
        tag = kind[:-1]
        payload = f"<tsRequest><{tag} name={quoteattr(name)}><project id='{project_id}' /></{tag}></tsRequest>"
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{boundary}\r\nContent-Disposition: name=\"request_payload\"\r\nContent-Type: text/xml\r\n\r\n{payload}\r\n".encode())
        body.write(f"--{boundary}\r\nContent-Disposition: name=\"tableau_{tag}\"; filename=\"{filename}\"\r\n"
                   f"Content-Type: application/octet-stream\r\n\r\n".encode())
        body.write(content)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        extra = "&skipConnectionCheck=true" if kind == "workbooks" else ""
        return self.api("POST", f"/{kind}?overwrite=true{extra}", content=body.getvalue(),
                        headers={"Content-Type": f"multipart/mixed; boundary={boundary}"})[tag]


def _permission_body(owner: str, owner_id: str, caps: dict[str, str]) -> dict[str, Any]:
    return {"permissions": {"granteeCapabilities": [{owner: {"id": owner_id}, "capabilities": {"capability": [
        {"name": n, "mode": m} for n, m in caps.items()]}}]}}


def wait_for_metadata(site: Site, workbook: str, timeout: float = 600) -> None:
    """The Metadata API indexes new content with a delay; wait until it knows the workbook."""
    query = "query($l: [String]) { workbooksConnection(filter: {luidWithin: $l}) { nodes { luid } } }"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = site.call("POST", "/api/metadata/graphql", json={"query": query, "variables": {"l": [workbook]}})
        nodes = ((data.get("data") or {}).get("workbooksConnection") or {}).get("nodes") or []
        if nodes:
            return
        time.sleep(10)
    raise TimeoutError(f"the Metadata API did not index workbook {workbook} within {timeout:.0f}s")


def main() -> dict[str, Any]:
    """Create the project, data source, workbook, group and permissions. Returns their ids."""
    site = Site()
    try:
        teardown(site)  # start clean after an interrupted run
        project = site.api("POST", "/projects", json={"project": {"name": PROJECT, "description": "unshackle live test (temporary)"}})["project"]
        group = site.api("POST", "/groups", json={"group": {"name": GROUP}})["group"]
        with tempfile.TemporaryDirectory() as tmp:
            tdsx = datasource_tdsx(Path(tmp) / "dunder_mifflin_orders.tdsx", Path(tmp))
            ds = site.publish("datasources", DATASOURCE, project["id"], "dunder_mifflin_orders.tdsx", tdsx.read_bytes())
        twb = workbook_twb(site.server, site.content_url, ds["contentUrl"])
        wb = site.publish("workbooks", WORKBOOK, project["id"], "dunder_mifflin_sales.twb", twb.encode())
        site.api("PUT", f"/projects/{project['id']}/permissions", json=_permission_body("group", group["id"], {"Read": "Allow"}))
        site.api("PUT", f"/workbooks/{wb['id']}/permissions",
                 json=_permission_body("group", group["id"], {"Read": "Allow", "ExportData": "Allow", "Write": "Deny"}))
        views = {v["name"]: v["id"] for v in site.items(f"/workbooks/{wb['id']}/views", "views", "view")}
        wait_for_metadata(site, wb["id"])
        return {"project": project["id"], "group": group["id"], "datasource": ds["id"], "workbook": wb["id"], "views": views}
    finally:
        site.close()


def teardown(site: Site | None = None) -> list[str]:
    """Delete the project (its data sources and workbooks go with it) and the group. Returns what was deleted."""
    own = site is None
    site = site or Site()
    gone: list[str] = []
    try:
        for p in site.items("/projects", "projects", "project", filter=f"name:eq:{PROJECT}"):
            for kind, item in (("workbooks", "workbook"), ("datasources", "datasource")):
                for c in site.items(f"/{kind}", kind, item, filter=f"projectName:eq:{PROJECT}"):
                    site.api("DELETE", f"/{kind}/{c['id']}")
                    gone.append(f"{item} {c['name']}")
            site.api("DELETE", f"/projects/{p['id']}")
            gone.append(f"project {p['name']}")
        for g in site.items("/groups", "groups", "group", filter=f"name:eq:{GROUP}"):
            site.api("DELETE", f"/groups/{g['id']}")
            gone.append(f"group {g['name']}")
    finally:
        if own:
            site.close()
    return gone
