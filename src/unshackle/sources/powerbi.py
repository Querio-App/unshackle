"""Power BI / Microsoft Fabric source: semantic models (TMDL), reports (PBIR or legacy report.json), RLS.

Raw layout under migration/raw/powerbi/:

    _meta.json                               {"admin": bool, "workspaces": [ids]}
    workspaces.json                          GET /v1.0/myorg/groups
    workspaces/<ws>/datasets.json            GET /groups/{ws}/datasets
    workspaces/<ws>/reports.json             GET /groups/{ws}/reports
    workspaces/<ws>/dashboards.json          GET /groups/{ws}/dashboards
    models/<id>/<part path>                  Fabric semanticModels/{id}/getDefinition?format=TMDL parts
                                             (definition/model.tmdl, definition/tables/*.tmdl, relationships.tmdl,
                                             roles/*.tmdl, expressions.tmdl, Copilot/..., definition.pbism)
    reports/<id>/<part path>                 Fabric reports/{id}/getDefinition parts: PBIR
                                             (definition/pages/<page>/page.json, .../visuals/<v>/visual.json)
                                             or legacy report.json
    scanner/<scan id>.json                   admin scanner result (fallback when getDefinition fails; POWERBI_ADMIN=1)
    usage.json                               {report id: {views, last_viewed}} from admin activity events (ViewReport)
    _errors.json                             [{item, error}] what failed to extract; parse() turns them into warnings

Design choices (documented in skills/source-powerbi/SKILL.md):
  * one notebook per report *page* (Power BI pages are separate canvases with their own slicers);
  * measures are attached to the semantic model of the table they aggregate, not their home table
    (so "_Measures" tables work);
  * a PBIR aggregation `Function` is the semantic-query QueryAggregateFunction enum:
    0 Sum, 1 Avg, 2 DistinctCount, 3 Min, 4 Max, 5 CountNonNull, 6 Median, 7 StandardDeviation, 8 Variance.
"""

from __future__ import annotations

import base64
import json
import re
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from unshackle.http import Client, SourceError, env
from unshackle.ir import (
    Bundle,
    Column,
    Dashboard,
    DashboardFilter,
    Dimension,
    Encoding,
    Entity,
    KnowledgeItem,
    Measure,
    Metric,
    Permission,
    RowFilter,
    SemanticModel,
    Table,
    Tile,
    VerifiedQuery,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source

PBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API = "https://api.fabric.microsoft.com/v1"
LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

COLUMN_TYPE = {
    "string": "string",
    "int64": "integer",
    "decimal": "number",
    "double": "number",
    "datetime": "timestamp",
    "boolean": "boolean",
    "binary": "binary",
}
AGG_FUNCTION = {0: "SUM", 1: "AVG", 2: "COUNT_DISTINCT", 3: "MIN", 4: "MAX", 5: "COUNT", 6: "MEDIAN", 7: "STDDEV", 8: "VARIANCE"}
DAX_AGG = {
    "SUM": "sum",
    "AVERAGE": "average",
    "MIN": "min",
    "MAX": "max",
    "COUNT": "count",
    "COUNTA": "count",
    "DISTINCTCOUNT": "count_distinct",
    "DISTINCTCOUNTNOBLANK": "count_distinct",
    "MEDIAN": "median",
}
DAX_ITER = {"SUMX": "sum", "AVERAGEX": "average", "MINX": "min", "MAXX": "max"}
SQL_AGG = {"sum": "SUM", "average": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT", "median": "MEDIAN"}
TIME_INTEL = {
    "SAMEPERIODLASTYEAR", "DATEADD", "PARALLELPERIOD", "PREVIOUSYEAR", "PREVIOUSMONTH", "PREVIOUSQUARTER",
    "DATESYTD", "DATESQTD", "DATESMTD", "DATESINPERIOD", "DATESBETWEEN", "NEXTYEAR", "NEXTMONTH",
}
TO_DATE = {"DATESYTD": "year", "DATESQTD": "quarter", "DATESMTD": "month", "TOTALYTD": "year", "TOTALQTD": "quarter", "TOTALMTD": "month"}
# construct -> hint for the agent (see skills/source-powerbi "DAX -> MetricFlow / SQL")
DAX_HINT = {
    "ALL": "removes filter context (ALL): its value depends on the visual's grouping, so it is no metric; tile SQL computes it",
    "ALLEXCEPT": "removes filter context (ALLEXCEPT): its value depends on the visual's grouping, so it is no metric; tile SQL computes it",
    "ALLSELECTED": "depends on visual filter context (ALLSELECTED): no metric; tile SQL computes it inside CALCULATE",
    "REMOVEFILTERS": "removes filter context (REMOVEFILTERS): its value depends on the visual's grouping, so it is no metric; tile SQL computes it",
    "FILTER": "table FILTER inside CALCULATE: FILTER(ALL(..)/ALLSELECTED(..), ..) (running totals) is computed in tile SQL; "
              "a column predicate is a simple metric filter; else a SQL model",
    "SUMX": "iterator over another table, VALUES(..) or RELATED columns: tile SQL computes VALUES(T[c]); as a metric, a SQL model joining the tables, then a measure",
    "AVERAGEX": "iterator over another table, VALUES(..) or RELATED columns: tile SQL computes VALUES(T[c]); as a metric, a SQL model, then a measure",
    "MINX": "iterator over another table, VALUES(..) or RELATED columns: tile SQL computes VALUES(T[c]); as a metric, a SQL model, then a measure",
    "MAXX": "iterator over another table, VALUES(..) or RELATED columns: tile SQL computes VALUES(T[c]); as a metric, a SQL model, then a measure",
    "RANKX": "ranking (RANKX): window SQL (RANK() OVER ...) in the tile",
    "TOPN": "TOPN: ORDER BY ... LIMIT in the tile SQL",
    "USERELATIONSHIP": "activates an inactive relationship (USERELATIONSHIP): add a second foreign entity / time dimension",
    "EXTERNALMEASURE": "measure of another semantic model (composite model, DirectQuery to Power BI): migrate that model and use its metric",
    "SELECTEDVALUE": "reads slicer selection (SELECTEDVALUE): a notebook filter parameter",
    "ISINSCOPE": "depends on visual hierarchy level (ISINSCOPE): split tiles or compute in tile SQL",
    "HASONEVALUE": "depends on visual filter context (HASONEVALUE): tile SQL",
    "SUMMARIZE": "builds a table (SUMMARIZE): SQL model",
    "ADDCOLUMNS": "builds a table (ADDCOLUMNS): SQL model",
    "LASTNONBLANK": "semi-additive (LASTNONBLANK): measure with non_additive_dimension",
    "LASTDATE": "semi-additive (LASTDATE): measure with non_additive_dimension",
    "CLOSINGBALANCEMONTH": "semi-additive: measure with non_additive_dimension (window_choice: max)",
    "DATESINPERIOD": "rolling window (DATESINPERIOD): cumulative metric with window",
    "PARALLELPERIOD": "whole-period shift (PARALLELPERIOD): derived metric with offset_window over a period-grain metric",
    "PREVIOUSYEAR": "whole previous year: derived metric with offset_window at year grain",
    "RELATED": "cross-table column (RELATED): move to the related model or build a SQL model",
    "LOOKUPVALUE": "lookup (LOOKUPVALUE): join in a SQL model",
    "EARLIER": "row context nesting (EARLIER): window SQL or SQL model",
}
VISUALS: dict[str, tuple[str, str, str]] = {
    "clusteredColumnChart": ("chart", "bar", "v"),
    "columnChart": ("chart", "bar", "v"),
    "stackedColumnChart": ("chart", "bar", "v"),
    "hundredPercentStackedColumnChart": ("chart", "bar", "v"),
    "clusteredBarChart": ("chart", "bar", "h"),
    "barChart": ("chart", "bar", "h"),
    "stackedBarChart": ("chart", "bar", "h"),
    "hundredPercentStackedBarChart": ("chart", "bar", "h"),
    "funnel": ("chart", "bar", "h"),
    "lineChart": ("chart", "line", "v"),
    "areaChart": ("chart", "area", "v"),
    "stackedAreaChart": ("chart", "area", "v"),
    "lineClusteredColumnComboChart": ("chart", "bar", "v"),
    "lineStackedColumnComboChart": ("chart", "bar", "v"),
    "pieChart": ("chart", "arc", ""),
    "donutChart": ("chart", "arc", ""),
    "scatterChart": ("chart", "point", ""),
    "card": ("kpi", "text", ""),
    "cardVisual": ("kpi", "text", ""),
    "gauge": ("kpi", "text", ""),
    "kpi": ("kpi", "text", ""),
    "multiRowCard": ("table", "table", ""),
    "tableEx": ("table", "table", ""),
    "pivotTable": ("table", "table", ""),
    "map": ("unknown", "unknown", ""),
    "filledMap": ("unknown", "unknown", ""),
    "azureMap": ("unknown", "unknown", ""),
    "shapeMap": ("unknown", "unknown", ""),
    "treemap": ("unknown", "unknown", ""),
    "waterfallChart": ("unknown", "unknown", ""),
    "ribbonChart": ("unknown", "unknown", ""),
    "decompositionTreeVisual": ("unknown", "unknown", ""),
}
DECORATION = {"image", "shape", "basicShape", "actionButton", "pageNavigator", "bookmarkNavigator", "group"}
DIM_ROLES = ("Category", "Axis", "Rows", "Location", "Details", "Columns", "Series", "Legend")
VALUE_ROLES = ("Y", "Values", "X", "Size", "Data", "Value", "Fields")
SQL_RESERVED = {"date", "order", "group", "user", "table", "select", "from", "where", "to", "year", "month", "day", "time", "key", "value"}
SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PowerBISource(Source):
    name = "powerbi"
    label = "Power BI / Microsoft Fabric"
    env_vars = (
        "POWERBI_TENANT_ID",
        "POWERBI_CLIENT_ID",
        "POWERBI_CLIENT_SECRET",
        "POWERBI_ACCESS_TOKEN",
        "POWERBI_WORKSPACES",
        "POWERBI_ADMIN",
    )
    docs = "https://learn.microsoft.com/en-us/rest/api/fabric/semanticmodel/items/get-semantic-model-definition"
    manual_files = "a Power BI project (.pbip file or its folder) with the semantic model saved as TMDL, or its .SemanticModel / .Report folders"
    manual_gaps = (
        "usage: no report view counts (admin activity events)",
        "names: models and reports are named after their project folders; no workspace, dataset ids or links to the service",
        "dashboards: Power BI dashboards (pinned tiles) only exist in the service and are not in a project",
        "permissions: RLS roles and their filters come from the TMDL, but who is in each role is set in the service and is missing",
    )

    def __init__(
        self,
        client: Client | None = None,
        transport: Any = None,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
        today: datetime | None = None,
    ):
        self._client = client
        self._transport = transport
        self._tokens: dict[str, str] = {}
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self._today = today

    # ---------------------------------------------------------------- network

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(PBI_API, per_minute=120, transport=self._transport)
        return self._client

    @property
    def admin(self) -> bool:
        return env("POWERBI_ADMIN", required=False).lower() in ("1", "true", "yes")

    def _token(self, scope: str) -> str:
        if scope in self._tokens:
            return self._tokens[scope]
        pre = env("POWERBI_FABRIC_ACCESS_TOKEN", required=False) if scope == FABRIC_SCOPE else ""
        pre = pre or env("POWERBI_ACCESS_TOKEN", required=False)
        if pre:
            self._tokens[scope] = pre
            return pre
        resp = self.client().post(
            LOGIN.format(tenant=env("POWERBI_TENANT_ID")),
            data={
                "grant_type": "client_credentials",
                "client_id": env("POWERBI_CLIENT_ID"),
                "client_secret": env("POWERBI_CLIENT_SECRET"),
                "scope": scope,
            },
        ).json()
        self._tokens[scope] = resp["access_token"]
        return resp["access_token"]

    def _h(self, scope: str = PBI_SCOPE) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(scope)}"}

    def _get(self, url: str, **kw: Any) -> Any:
        return self.client().get(url, headers=self._h(), **kw).json()

    def _list(self, path: str) -> list[dict[str, Any]]:
        if path != "/groups":  # only /groups pages with $top/$skip; item lists return everything
            return self._get(path).get("value") or []
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            page = self._get(path, params={"$top": 5000, "$skip": skip}).get("value") or []
            out += page
            if len(page) < 5000:
                return out
            skip += 5000

    def _workspaces(self) -> list[dict[str, Any]]:
        wanted = [w.strip() for w in env("POWERBI_WORKSPACES", required=False).split(",") if w.strip()]
        spaces = self._list("/groups")
        if wanted:
            spaces = [w for w in spaces if w["id"] in wanted or w.get("name") in wanted]
        return spaces

    def _sleep(self, resp_or_seconds: Any) -> None:
        wait = self.poll_interval
        if hasattr(resp_or_seconds, "headers"):
            ra = resp_or_seconds.headers.get("retry-after")
            if ra and ra.isdigit() and self.poll_interval:
                wait = min(float(ra), 30.0)
        if wait:
            time.sleep(wait)

    def _long_running(self, url: str) -> Any:
        """POST a Fabric long-running operation; returns the result JSON."""
        c = self.client()
        resp = c.post(url, headers=self._h(FABRIC_SCOPE))
        if resp.status_code == 200:
            return resp.json()
        op = resp.headers.get("x-ms-operation-id") or ""
        location = resp.headers.get("location") or ""
        if not op and location:
            m = re.search(r"/operations/([^/?]+)", location)
            op = m.group(1) if m else ""
        if not op:
            raise SourceError(f"POST {url} -> {resp.status_code} without an operation id")
        started = time.monotonic()
        state: dict[str, Any] = {}
        while time.monotonic() - started < self.max_wait:
            self._sleep(resp)
            resp = c.get(f"{FABRIC_API}/operations/{op}", headers=self._h(FABRIC_SCOPE))
            state = resp.json()
            status = str(state.get("status", "")).lower()
            if status == "succeeded":
                return c.get(f"{FABRIC_API}/operations/{op}/result", headers=self._h(FABRIC_SCOPE)).json()
            if status in ("failed", "undefined", "cancelled"):
                raise SourceError(f"operation {op} {status}: {json.dumps(state.get('error') or state)[:400]}")
        raise SourceError(f"operation {op} did not finish in {self.max_wait:.0f}s (last: {state})")

    def _definition(self, store: RawStore, ws: str, kind: str, item: str, prefix: str, fmt: str = "") -> int:
        url = f"{FABRIC_API}/workspaces/{ws}/{kind}/{item}/getDefinition" + (f"?format={fmt}" if fmt else "")
        result = self._long_running(url)
        parts = ((result or {}).get("definition") or {}).get("parts") or []
        for part in parts:
            path = PurePosixPath(str(part.get("path", "")).lstrip("/"))
            if ".." in path.parts or not str(path):
                continue
            payload = part.get("payload") or ""
            data = base64.b64decode(payload) if part.get("payloadType", "InlineBase64") == "InlineBase64" else payload.encode()
            store.put_bytes(f"{prefix}/{item}/{path}", data)
        return len(parts)

    def _scan(self, store: RawStore, workspaces: list[str]) -> None:
        c = self.client()
        for i in range(0, len(workspaces), 100):
            resp = c.post(
                "/admin/workspaces/getInfo",
                params={"lineage": "True", "datasourceDetails": "True", "datasetSchema": "True", "datasetExpressions": "True"},
                json={"workspaces": workspaces[i : i + 100]},
                headers=self._h(),
            ).json()
            scan = resp["id"]
            started = time.monotonic()
            while True:
                status = self._get(f"/admin/workspaces/scanStatus/{scan}").get("status", "")
                if status == "Succeeded":
                    break
                if status == "Failed" or time.monotonic() - started > self.max_wait:
                    raise SourceError(f"scan {scan} {status or 'timed out'}")
                self._sleep(None)
            store.put_json(f"scanner/{scan}.json", self._get(f"/admin/workspaces/scanResult/{scan}"))

    def _usage(self) -> dict[str, dict[str, Any]]:
        days = int(env("POWERBI_USAGE_DAYS", required=False, default="30") or 30)
        today = (self._today or datetime.now(timezone.utc)).date()
        out: dict[str, dict[str, Any]] = {}
        for back in range(1, min(days, 30) + 1):
            day = today - timedelta(days=back)
            url: str | None = (
                f"/admin/activityevents?startDateTime='{day.isoformat()}T00:00:00.000Z'"
                f"&endDateTime='{day.isoformat()}T23:59:59.999Z'&$filter=Activity eq 'viewreport'"
            )
            while url:
                page = self._get(url)
                for ev in page.get("activityEventEntities") or []:
                    rid = str(ev.get("ReportId") or ev.get("ArtifactId") or "")
                    if not rid:
                        continue
                    cur = out.setdefault(rid, {"views": 0, "last_viewed": ""})
                    cur["views"] += 1
                    cur["last_viewed"] = max(cur["last_viewed"], str(ev.get("CreationTime") or ""))
                url = None if page.get("lastResultSet", True) else page.get("continuationUri")
        return out

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        usage: dict[str, dict[str, Any]] = {}
        if self.admin:
            try:
                usage = self._usage()
            except SourceError as exc:
                inv.notes.append(f"Usage (admin activity events) failed: {str(exc)[:200]}")
        else:
            inv.notes.append("Usage needs a Fabric admin service principal: set POWERBI_ADMIN=1 to read ViewReport activity events.")
        inv.notes.append(
            "The service principal only sees workspaces where it is Member/Contributor/Admin; "
            "getDefinition also needs write permission on each item."
        )
        for ws in self._workspaces():
            wid = ws["id"]
            inv.add("workspace", f"workspace:{wid}", ws.get("name") or wid, extra={"capacity": ws.get("isOnDedicatedCapacity")})
            for ds in self._list(f"/groups/{wid}/datasets"):
                inv.add("semantic_model", f"model:{ds['id']}", ds.get("name") or ds["id"], parent=ws.get("name", ""), url=ds.get("webUrl", ""),
                        extra={"storage": ds.get("targetStorageMode", "")})
            for r in self._list(f"/groups/{wid}/reports"):
                u = usage.get(r["id"], {})
                inv.add("report", f"report:{r['id']}", r.get("name") or r["id"], parent=ws.get("name", ""), url=r.get("webUrl", ""),
                        views=u.get("views") if usage else None, last_viewed=u.get("last_viewed", ""),
                        extra={"datasetId": r.get("datasetId", ""), "reportType": r.get("reportType", "")})
            for d in self._list(f"/groups/{wid}/dashboards"):
                inv.add("dashboard", f"dashboard:{d['id']}", d.get("displayName") or d["id"], parent=ws.get("name", ""), url=d.get("webUrl", ""))
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        spaces = self._workspaces()
        store.put_json("workspaces.json", spaces)
        store.put_json("_meta.json", {"admin": self.admin, "workspaces": [w["id"] for w in spaces]})
        want = _only(only)
        failed: dict[str, list[str]] = {}
        for ws in spaces:
            wid = ws["id"]
            lists: dict[str, list[dict[str, Any]]] = {}
            for kind in ("datasets", "reports", "dashboards"):
                try:
                    lists[kind] = self._list(f"/groups/{wid}/{kind}")
                except SourceError as exc:
                    lists[kind] = []
                    errors.append({"item": f"workspace:{wid} {kind}", "error": str(exc)[:500]})
                store.put_json(f"workspaces/{wid}/{kind}.json", lists[kind])
            reports = [r for r in lists["reports"] if _wanted(want, "report", r["id"], wid)]
            models = {r.get("datasetId") for r in reports if want}
            for ds in lists["datasets"]:
                if not (_wanted(want, "model", ds["id"], wid) or ds["id"] in models):
                    continue
                try:
                    self._definition(store, wid, "semanticModels", ds["id"], "models", "TMDL")
                except SourceError as exc:
                    errors.append({"item": f"model:{ds['id']}", "name": ds.get("name", ""), "workspace": wid, "error": str(exc)[:500]})
                    failed.setdefault(wid, []).append(ds["id"])
            for r in reports:
                if r.get("reportType") == "PaginatedReport":
                    errors.append({"item": f"report:{r['id']}", "name": r.get("name", ""), "error": "paginated report (RDL) is not migrated"})
                    continue
                try:
                    self._definition(store, wid, "reports", r["id"], "reports")
                except SourceError as exc:
                    errors.append({"item": f"report:{r['id']}", "name": r.get("name", ""), "workspace": wid, "error": str(exc)[:500]})
        if failed:
            if self.admin:
                try:
                    self._scan(store, sorted(failed))
                except SourceError as exc:
                    errors.append({"item": "admin scanner", "error": str(exc)[:500]})
            else:
                errors.append({"item": "admin scanner", "error": "getDefinition failed for some models; set POWERBI_ADMIN=1 to fall back to the admin scanner (tables, measures, DAX, M)"})
        if self.admin:
            try:
                store.put_json("usage.json", self._usage())
            except SourceError as exc:
                errors.append({"item": "usage", "error": str(exc)[:500]})
        store.put_json("_errors.json", errors)

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file() and path.suffix.lower() in (".pbix", ".pbit"):
            raise SourceError(
                f"{path.name}: unshackle can't read the binary model in a {path.suffix} file. Open it in Power BI Desktop, "
                "File > Save as > Power BI project (.pbip), and import the project folder"
            )
        root = path.parent if path.is_file() and path.suffix.lower() == ".pbip" else path
        if not root.is_dir():
            raise SourceError(f"{path.name}: Power BI import accepts {self.manual_files}")
        out: dict[str, bytes] = {}
        for rel, p in _manual.walk(root):
            parts = f"{root.name}/{rel}".split("/")
            at = next((i for i, x in enumerate(parts[:-1]) if x.endswith((".SemanticModel", ".Report"))), None)
            if at is None or ".pbi" in parts[at + 1:]:  # .pbi/ is Desktop's local cache (cache.abf holds data) and settings
                continue
            kind, suffix = ("models", ".SemanticModel") if parts[at].endswith(".SemanticModel") else ("reports", ".Report")
            out[f"{kind}/{parts[at].removesuffix(suffix)}/{'/'.join(parts[at + 1:])}"] = p.read_bytes()
        for mid in sorted({rel.split("/")[1] for rel in out if rel.startswith("models/")}):
            if not any(rel.startswith(f"models/{mid}/") and rel.endswith(".tmdl") for rel in out):
                raise SourceError(
                    f"{mid}.SemanticModel has no TMDL (a model.bim project?); unshackle reads TMDL only. In Power BI Desktop turn on "
                    "the preview feature 'Store semantic model using TMDL format', save the project again and re-import"
                )
        return out

    # ---------------------------------------------------------------- parse

    def parse(self, store: RawStore) -> Bundle:
        return _Parser(store).run()


def _only(only: set[str] | None) -> set[str] | None:
    if not only:
        return None
    out = set()
    for item in only:
        kind, sep, rest = item.partition(":")
        out.update({item} if sep else {f"model:{item}", f"report:{item}", f"workspace:{item}"})
    return out


def _wanted(want: set[str] | None, kind: str, id: str, ws: str) -> bool:
    return want is None or f"{kind}:{id}" in want or f"workspace:{ws}" in want


# ==================================================================== TMDL


OBJECT_KEYWORDS = {
    "database", "model", "table", "column", "measure", "partition", "relationship", "role", "tablepermission",
    "columnpermission", "member", "hierarchy", "level", "annotation", "extendedproperty", "culture",
    "linguisticmetadata", "perspective", "perspectivetable", "perspectivecolumn", "perspectivemeasure",
    "perspectivehierarchy", "expression", "datasource", "calculationgroup", "calculationitem", "querygroup",
    "function", "kpi", "variation", "calendar", "translation", "objecttranslation", "dataaccessoptions", "cultureinfo",
}


@dataclass
class TNode:
    kind: str
    name: str = ""
    expr: str = ""
    props: dict[str, str] = field(default_factory=dict)
    children: list["TNode"] = field(default_factory=list)
    description: str = ""
    ref: bool = False

    def kids(self, kind: str) -> list["TNode"]:
        return [c for c in self.children if c.kind == kind]

    def prop(self, key: str, default: str = "") -> str:
        return self.props.get(key.lower(), default)


def _indent(line: str) -> int:
    level, spaces = 0, 0
    for ch in line:
        if ch == "\t":
            level += 1
        elif ch == " ":
            spaces += 1
        else:
            break
    return level + spaces // 4


def _unquote_name(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _unquote_value(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def _split_name(rest: str) -> tuple[str, str]:
    """"'Total Sales' = SUM(...)" -> ("Total Sales", "= SUM(...)")"""
    rest = rest.strip()
    if rest.startswith("'"):
        i = 1
        while i < len(rest):
            if rest[i] == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        return rest[1:i].replace("''", "'"), rest[i + 1 :].strip()
    m = re.match(r"([^\s=]+)\s*(.*)", rest)
    return (m.group(1), m.group(2).strip()) if m else ("", "")


def split_column_ref(ref: str) -> tuple[str, str]:
    """Sales.'Product Key' / 'Dim Customer'.CustomerKey -> (table, column)."""
    ref = ref.strip()
    if ref.startswith("'"):
        table, rest = _split_name(ref)
    else:
        table, _, rest = ref.partition(".")
    return table.strip(), _unquote_name(rest.strip().lstrip("."))


class _Tmdl:
    def __init__(self, text: str):
        self.lines = text.replace("\r\n", "\n").split("\n")
        self.i = 0

    def parse(self) -> list[TNode]:
        return self._children(-1, None)

    def _children(self, parent_level: int, parent: TNode | None) -> list[TNode]:
        nodes: list[TNode] = []
        desc: list[str] = []
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            s = raw.strip()
            if not s:
                self.i += 1
                continue
            level = _indent(raw)
            if level <= parent_level:
                break
            if s.startswith("///"):
                desc.append(s[3:].strip())
                self.i += 1
                continue
            node = self._object(level, s)
            if node is not None:
                node.description = "\n".join(desc)
                nodes.append(node)
                desc = []
                continue
            desc = []
            if parent is not None:
                self._property(level, s, parent)
            else:
                self.i += 1
        return nodes

    def _object(self, level: int, s: str) -> TNode | None:
        m = re.match(r"(ref\s+)?([A-Za-z]+)\b\s*(.*)$", s)
        if not m or m.group(2).lower() not in OBJECT_KEYWORDS:
            return None
        rest = m.group(3)
        if rest.startswith(":") or (rest.startswith("=") and m.group(2).lower() not in ("calculationgroup",)):
            return None  # a property that shares a keyword name (column: X, expression = ...)
        node = TNode(kind=m.group(2).lower(), ref=bool(m.group(1)))
        name, tail = _split_name(rest) if rest else ("", "")
        node.name = name
        self.i += 1
        if tail.startswith("="):
            node.expr = self._expression(tail[1:], level + 2)
        node.children = self._children(level, node)
        return node

    def _property(self, level: int, s: str, parent: TNode) -> None:
        m = re.match(r"([A-Za-z_][\w]*)\s*([:=])\s*(.*)$", s)
        self.i += 1
        if not m:
            parent.props[s.lower()] = "true"
            return
        key, sep, value = m.group(1).lower(), m.group(2), m.group(3)
        if sep == "=":
            parent.props[key] = self._expression(value, level + 1)
        else:
            parent.props[key] = _unquote_value(value)

    def _expression(self, first: str, min_level: int) -> str:
        first = first.strip()
        if first.startswith("```"):
            body: list[str] = []
            after = first[3:]
            if after.strip():
                body.append(after)
            while self.i < len(self.lines):
                raw = self.lines[self.i]
                self.i += 1
                if raw.strip().startswith("```"):
                    break
                body.append(raw)
            return textwrap.dedent("\n".join(body)).strip("\n").rstrip()
        cont: list[str] = []
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if not raw.strip():
                j = self.i + 1
                while j < len(self.lines) and not self.lines[j].strip():
                    j += 1
                if j < len(self.lines) and _indent(self.lines[j]) >= min_level:
                    cont.append("")
                    self.i += 1
                    continue
                break
            if _indent(raw) < min_level:
                break
            cont.append(raw)
            self.i += 1
        text = textwrap.dedent("\n".join(cont)).strip("\n")
        return "\n".join(x for x in (first, text) if x).rstrip()


def parse_tmdl(text: str) -> list[TNode]:
    return _Tmdl(text).parse()


# ---------------------------------------------------------------- model


@dataclass
class PColumn:
    name: str
    data_type: str = ""
    source_column: str = ""
    expression: str = ""
    description: str = ""
    hidden: bool = False
    is_key: bool = False
    format_string: str = ""


@dataclass
class PMeasure:
    name: str
    expression: str
    table: str
    description: str = ""
    format_string: str = ""
    display_folder: str = ""
    hidden: bool = False


@dataclass
class PPartition:
    name: str
    type: str = "m"
    mode: str = ""
    source: str = ""
    entity: str = ""  # entity partitions (Direct Lake, DirectQuery to another model): entityName, schemaName, expressionSource
    schema: str = ""
    expression_source: str = ""


@dataclass
class PTable:
    name: str
    description: str = ""
    columns: list[PColumn] = field(default_factory=list)
    measures: list[PMeasure] = field(default_factory=list)
    partitions: list[PPartition] = field(default_factory=list)
    hierarchies: dict[str, dict[str, str]] = field(default_factory=dict)
    hidden: bool = False
    calc_items: list[tuple[str, str]] = field(default_factory=list)
    is_calc_group: bool = False
    data_category: str = ""  # "Time": marked as a date table

    def column(self, name: str) -> PColumn | None:
        low = name.lower()
        return next((c for c in self.columns if c.name.lower() == low), None)


@dataclass
class PRel:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    active: bool = True
    cross_filter: str = ""
    to_many: bool = False  # toCardinality: many (a many-to-many relationship)

    @property
    def many_to_one(self) -> bool:
        """What a foreign entity or a tile JOIN can follow without duplicating rows."""
        return self.active and not self.to_many


@dataclass
class PRole:
    name: str
    permission: str = "read"
    filters: dict[str, str] = field(default_factory=dict)
    members: list[tuple[str, str]] = field(default_factory=list)
    ols: list[tuple[str, str, str]] = field(default_factory=list)
    description: str = ""


@dataclass
class PModel:
    id: str
    name: str
    tables: dict[str, PTable] = field(default_factory=dict)
    relationships: list[PRel] = field(default_factory=list)
    roles: list[PRole] = field(default_factory=list)
    expressions: dict[str, str] = field(default_factory=dict)
    copilot: dict[str, str] = field(default_factory=dict)
    source: str = ""
    workspace: str = ""

    def table(self, name: str | None) -> PTable | None:
        if not name:
            return None
        low = name.lower()
        return next((t for t in self.tables.values() if t.name.lower() == low), None)

    def measure(self, name: str) -> PMeasure | None:
        low = name.lower()
        for t in self.tables.values():
            for m in t.measures:
                if m.name.lower() == low:
                    return m
        return None


def _bool(node: TNode, key: str) -> bool:
    return node.prop(key).lower() in ("true", "1")


def model_from_tmdl(model_id: str, files: dict[str, str]) -> PModel:
    pm = PModel(id=model_id, name=model_id)
    nodes: list[TNode] = []
    for rel, text in sorted(files.items()):
        nodes += parse_tmdl(text)

    def walk(ns: list[TNode]) -> None:
        for n in ns:
            if n.ref:
                continue
            if n.kind == "database" and n.name and n.name.lower() != "unknown":  # older Desktop versions write "Unknown"
                pm.name = n.name
            elif n.kind == "model":
                walk(n.children)
            elif n.kind == "table":
                pm.tables[n.name] = _table(n)
            elif n.kind == "relationship":
                ft, fc = split_column_ref(n.prop("fromColumn"))
                tt, tc = split_column_ref(n.prop("toColumn"))
                pm.relationships.append(
                    PRel(n.name, ft, fc, tt, tc, n.prop("isActive", "true").lower() != "false", n.prop("crossFilteringBehavior"),
                         n.prop("toCardinality").lower() == "many")
                )
            elif n.kind == "role":
                pm.roles.append(_role(n))
            elif n.kind == "expression":
                pm.expressions[n.name] = n.expr

    walk(nodes)
    return pm


def _table(n: TNode) -> PTable:
    t = PTable(n.name, n.description, hidden=_bool(n, "isHidden"), data_category=n.prop("dataCategory"))
    for c in n.kids("column"):
        t.columns.append(
            PColumn(
                c.name,
                c.prop("dataType").lower(),
                c.prop("sourceColumn"),
                c.expr or (c.prop("expression") if c.prop("type").lower() == "calculated" else ""),
                c.description,
                _bool(c, "isHidden"),
                _bool(c, "isKey"),
                c.prop("formatString"),
            )
        )
    for m in n.kids("measure"):
        t.measures.append(PMeasure(m.name, m.expr, n.name, m.description, m.prop("formatString"), m.prop("displayFolder"), _bool(m, "isHidden")))
    for p in n.kids("partition"):
        t.partitions.append(PPartition(p.name, (p.expr or "m").strip().lower(), p.prop("mode").lower(), p.prop("source"),
                                       p.prop("entityName"), p.prop("schemaName"), p.prop("expressionSource")))
    for h in n.kids("hierarchy"):
        t.hierarchies[h.name] = {lv.name: lv.prop("column") or lv.name for lv in h.kids("level")}
    for cg in n.kids("calculationgroup"):
        t.is_calc_group = True
        t.calc_items += [(ci.name, ci.expr) for ci in cg.kids("calculationitem")]
    return t


def _role(n: TNode) -> PRole:
    r = PRole(n.name, n.prop("modelPermission", "read").lower(), description=n.description)
    for tp in n.kids("tablepermission"):
        if tp.expr:
            r.filters[tp.name] = tp.expr
        if tp.prop("metadataPermission").lower() == "none":
            r.ols.append((tp.name, "", "none"))
        for cp in tp.kids("columnpermission"):
            r.ols.append((tp.name, cp.name, cp.prop("metadataPermission", "none").lower()))
    for mem in n.kids("member"):
        r.members.append((mem.name, mem.prop("memberType", "auto").lower()))
    return r


def model_from_scanner(ds: dict[str, Any], workspace: str) -> PModel:
    pm = PModel(id=ds["id"], name=ds.get("name") or ds["id"], workspace=workspace)
    for t in ds.get("tables") or []:
        pt = PTable(t["name"], t.get("description", ""), hidden=bool(t.get("isHidden")))
        for c in t.get("columns") or []:
            calc = str(c.get("columnType", "")).lower() == "calculated"
            pt.columns.append(
                PColumn(c["name"], str(c.get("dataType", "")).lower(), "" if calc else c["name"], c.get("expression", "") if calc else "",
                        c.get("description", ""), bool(c.get("isHidden")))
            )
        for m in t.get("measures") or []:
            pt.measures.append(PMeasure(m["name"], m.get("expression", ""), t["name"], m.get("description", ""), hidden=bool(m.get("isHidden"))))
        for i, s in enumerate(t.get("source") or []):
            expr = s.get("expression", "")
            pt.partitions.append(PPartition(f"{t['name']}-{i}", "calculated" if not expr.lstrip().lower().startswith("let") and "(" in expr and "Sql." not in expr and "Value." not in expr else "m", "", expr))
        pm.tables[pt.name] = pt
    for e in ds.get("expressions") or []:
        pm.expressions[e["name"]] = e.get("expression", "")
    for r in ds.get("roles") or []:
        role = PRole(r["name"], str(r.get("modelPermission", "read")).lower())
        for tp in r.get("tablePermissions") or []:
            if tp.get("filterExpression"):
                role.filters[tp["name"]] = tp["filterExpression"]
        role.members = [(m.get("memberName", ""), str(m.get("memberType", "auto")).lower()) for m in r.get("members") or []]
        pm.roles.append(role)
    for rel in ds.get("relationships") or []:
        pm.relationships.append(PRel(rel.get("name", ""), rel.get("fromTable", ""), rel.get("fromColumn", ""), rel.get("toTable", ""),
                                     rel.get("toColumn", ""), rel.get("isActive", True) is not False, rel.get("crossFilteringBehavior", "")))
    return pm


# ---------------------------------------------------------------- Power Query M


@dataclass
class MSource:
    connector: str = ""
    server: str = ""
    database: str = ""
    schema: str = ""
    table: str = ""
    sql: str = ""
    renames: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


M_STR = r'"((?:[^"]|"")*)"'
M_CONNECTORS = (
    "Sql.Database", "Sql.Databases", "Snowflake.Databases", "GoogleBigQuery.Database", "Databricks.Catalogs",
    "DatabricksMultiCloud.Catalogs", "Databricks.Query", "PostgreSQL.Database", "AmazonRedshift.Database",
    "Oracle.Database", "MySQL.Database", "Odbc.DataSource", "Odbc.Query", "Fabric.Warehouse", "Lakehouse.Contents",
    "Sql.Database",
)
M_FILES = ("Excel.Workbook", "Csv.Document", "SharePoint.", "Web.Contents", "File.Contents", "Folder.", "Json.Document", "OData.Feed")
M_HARMLESS = {"Table.TransformColumnTypes", "Table.RenameColumns", "Table.RemoveColumns", "Table.SelectColumns", "Table.ReorderColumns"}


def _mstr(s: str) -> str:
    return s.replace('""', '"').replace("#(lf)", "\n").replace("#(cr)", "").replace("#(tab)", "\t")


def _m_args(text: str, start: int) -> list[str]:
    """Top-level arguments of the M call whose "(" is at text[start - 1]."""
    args, depth, cur, i, instr = [], 0, [], start, False
    while i < len(text):
        ch = text[i]
        if instr:
            cur.append(ch)
            if ch == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    cur.append('"')
                    i += 1
                else:
                    instr = False
        elif ch == '"':
            instr = True
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            if depth == 0:
                args.append("".join(cur).strip())
                return args
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    return args


def _m_literal(arg: str) -> str | None:
    m = re.fullmatch(M_STR, arg.strip(), re.S)
    return _mstr(m.group(1)) if m else None


def m_source(expr: str, params: dict[str, str] | None = None) -> MSource:
    ms = MSource()
    text = expr or ""
    for name, value in (params or {}).items():
        lit = re.match(r'\s*"((?:[^"]|"")*)"', value or "")
        if lit:
            text = re.sub(rf'(?<![\w"#.])(?:#"{re.escape(name)}"|{re.escape(name)})(?![\w".=])', '"' + lit.group(1) + '"', text)
    conn = re.search(r"\b(" + "|".join(re.escape(c) for c in M_CONNECTORS) + r")\s*\(", text)
    if conn:
        ms.connector = conn.group(1)
        args = _m_args(text, conn.end())
        lits = [_m_literal(a) for a in args]
        if lits and lits[0] is not None:
            ms.server = lits[0]
        if len(lits) > 1 and lits[1] is not None:
            ms.database = lits[1]
        q = re.search(r"\[\s*Query\s*=\s*" + M_STR, ",".join(args[2:]))
        if q:
            ms.sql = _mstr(q.group(1))
    nq = re.search(r"Value\.NativeQuery\s*\(", text)
    if nq:
        args = _m_args(text, nq.end())
        sql = _m_literal(args[1]) if len(args) > 1 else None
        if sql is not None:
            ms.sql = sql
        else:
            ms.problems.append("Value.NativeQuery with a computed SQL string")
    nav = re.search(r"\{\s*\[\s*Schema\s*=\s*" + M_STR + r"\s*,\s*Item\s*=\s*" + M_STR + r"\s*\]\s*\}", text)
    if nav:
        ms.schema, ms.table = _mstr(nav.group(1)), _mstr(nav.group(2))
    for name, kind in re.findall(r"\[\s*Name\s*=\s*" + M_STR + r"\s*,\s*Kind\s*=\s*" + M_STR + r"\s*\]", text):
        kind = kind.lower()
        if kind == "database":
            ms.database = _mstr(name)
        elif kind == "schema":
            ms.schema = _mstr(name)
        elif kind in ("table", "view"):
            ms.table = _mstr(name)
    for block in re.findall(r"Table\.RenameColumns\s*\((.*?)\)\s*(?:,|\bin\b)", text, re.S):
        for old, new in re.findall(r"\{\s*" + M_STR + r"\s*,\s*" + M_STR + r"\s*\}", block):
            ms.renames[_mstr(new)] = _mstr(old)
    steps = sorted({f for f in re.findall(r"\b(Table\.\w+)\s*\(", text) if f not in M_HARMLESS})
    if steps and (conn or nq):
        ms.problems.append("Power Query steps " + ", ".join(steps) + " reshape the data")
    if not conn and not nq:
        other = next((f for f in M_FILES if f in text), "")
        if "Table.FromRows" in text or "#table" in text or ("Json.Document" in text and "Binary.Decompress" in text):
            ms.problems.append("entered data (no warehouse source)")
        elif other:
            ms.problems.append(f"file/web source ({other.rstrip('.')}); load it into the warehouse first")
        else:
            ms.problems.append("no recognised warehouse connector in the M expression")
    elif not ms.table and not ms.sql:
        ms.problems.append(f"{ms.connector} without a table navigation step")
    return ms


def entity_source(part: PPartition, expressions: dict[str, str]) -> MSource:
    """An entity partition reads a table named in the partition through a shared expression: Direct Lake
    (a lakehouse / warehouse SQL endpoint, or OneLake) or DirectQuery to another Power BI semantic model."""
    expr = expressions.get(_unquote_name(part.expression_source), "")
    if re.search(r"\bAnalysisServices\.Database\s*\(|\bPowerPlatform\.|\bPowerBI\.", expr):
        return MSource(problems=["DirectQuery to another Power BI semantic model (composite model); migrate that model and point this table at its source"])
    if not part.entity:
        return MSource(problems=["entity partition without entityName"])
    ms = m_source(expr) if expr else MSource()
    ms.schema, ms.table, ms.sql, ms.problems = part.schema, part.entity, "", []
    return ms


# ==================================================================== DAX


class DaxError(ValueError):
    pass


@dataclass
class DCol:
    table: str | None
    name: str


@dataclass
class DTable:
    name: str


@dataclass
class DStr:
    value: str


@dataclass
class DNum:
    value: str


@dataclass
class DCall:
    name: str
    args: list[Any] = field(default_factory=list)


@dataclass
class DBin:
    op: str
    left: Any
    right: Any


@dataclass
class DUn:
    op: str
    operand: Any


@dataclass
class DSet:
    items: list[Any]


@dataclass
class DVar:
    """A VAR reference; dax_parse() replaces it with the variable's expression (see _inline_vars)."""

    name: str
    value: Any


@dataclass
class DRaw:
    """SQL already written for a sub-expression (MAX('Date'[Date]) in a FILTER condition, as a scalar subquery)."""

    sql: str


# Calls a variable may sit under and still mean the same thing when inlined: they don't change filter
# or row context. Anything else (CALCULATE, iterators, FILTER, time intelligence) would re-evaluate the
# inlined expression in a different context than the one the VAR captured.
VAR_SAFE = {"DIVIDE", "IF", "SWITCH", "ISBLANK", "NOT", "AND", "OR", "COALESCE", "ROUND", "ABS", "TRUE", "FALSE", "BLANK"}


_DAX_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--[^\n]*|//[^\n]*|/\*.*?\*/)
  | (?P<col>(?:'(?:[^']|'')+'|[A-Za-z_][\w]*)?\[(?:[^\]]|\]\])*\])
  | (?P<tbl>'(?:[^']|'')+')
  | (?P<str>"(?:[^"]|"")*")
  | (?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][\w.]*)
  | (?P<op><=|>=|<>|==|&&|\|\||[=<>+\-*/(),&{}^!])
    """,
    re.X | re.S,
)


def dax_tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _DAX_TOKEN.match(text, pos)
        if not m:
            raise DaxError(f"unexpected character {text[pos]!r}")
        pos = m.end()
        if m.lastgroup != "ws":
            out.append((m.lastgroup or "", m.group()))
    return out


class _DaxParser:
    def __init__(self, text: str):
        self.toks = dax_tokens(text)
        self.i = 0
        self.scopes: list[dict[str, Any]] = []

    def peek(self, value: str | None = None, kind: str | None = None) -> bool:
        if self.i >= len(self.toks):
            return False
        k, v = self.toks[self.i]
        return (value is None or v.upper() == value) and (kind is None or k == kind)

    def take(self) -> tuple[str, str]:
        if self.i >= len(self.toks):
            raise DaxError("unexpected end of expression")
        self.i += 1
        return self.toks[self.i - 1]

    def expect(self, value: str) -> None:
        k, v = self.take()
        if v != value:
            raise DaxError(f"expected {value!r}, got {v!r}")

    def parse(self) -> Any:
        node = self.or_()
        if self.i != len(self.toks):
            raise DaxError(f"unexpected {self.toks[self.i][1]!r}")
        return node

    def _binary(self, sub: Callable[[], Any], ops: tuple[str, ...]) -> Any:
        node = sub()
        while self.i < len(self.toks) and self.toks[self.i][1].upper() in ops and self.toks[self.i][0] in ("op", "ident"):
            op = self.take()[1].upper()
            node = DBin(op, node, sub())
        return node

    def or_(self) -> Any:
        return self._binary(self.and_, ("||",))

    def and_(self) -> Any:
        return self._binary(self.cmp, ("&&",))

    def cmp(self) -> Any:
        return self._binary(self.concat, ("=", "==", "<>", "<", ">", "<=", ">=", "IN"))

    def concat(self) -> Any:
        return self._binary(self.add, ("&",))

    def add(self) -> Any:
        return self._binary(self.mul, ("+", "-"))

    def mul(self) -> Any:
        return self._binary(self.pow, ("*", "/"))

    def pow(self) -> Any:
        return self._binary(self.unary, ("^",))

    def unary(self) -> Any:
        if self.peek("-") or self.peek("+"):
            return DUn(self.take()[1], self.unary())
        if self.peek("NOT", "ident") and not (self.i + 1 < len(self.toks) and self.toks[self.i + 1][1] == "("):
            self.take()
            return DCall("NOT", [self.unary()])
        return self.primary()

    def _let(self) -> Any:
        """VAR a = .. VAR b = .. RETURN body, with each variable inlined where it is used (see VAR_SAFE)."""
        scope: dict[str, Any] = {}
        self.scopes.append(scope)
        try:
            while self.peek("VAR", "ident"):
                self.take()
                kind, name = self.take()
                if kind != "ident":
                    raise DaxError(f"expected a variable name after VAR, got {name!r}")
                self.expect("=")
                scope[name.lower()] = self.or_()
            if not self.peek("RETURN", "ident"):
                raise DaxError("VAR without RETURN")
            self.take()
            return self.or_()
        finally:
            self.scopes.pop()

    def _var(self, name: str) -> Any | None:
        for scope in reversed(self.scopes):
            if name.lower() in scope:
                return scope[name.lower()]
        return None

    def primary(self) -> Any:
        if self.peek("VAR", "ident"):
            return self._let()
        kind, v = self.take()
        if kind == "num":
            return DNum(v)
        if kind == "str":
            return DStr(v[1:-1].replace('""', '"'))
        if kind == "col":
            table, _, col = v.partition("[")
            return DCol(_unquote_name(table) or None, col[:-1].replace("]]", "]"))
        if kind == "tbl":
            return DTable(_unquote_name(v))
        if v == "(":
            node = self.or_()
            self.expect(")")
            return node
        if v == "{":
            items = []
            while not self.peek("}"):
                items.append(self.or_())
                if self.peek(","):
                    self.take()
            self.expect("}")
            return DSet(items)
        if kind == "ident":
            if self.peek("("):
                self.take()
                args: list[Any] = []
                while not self.peek(")"):
                    if self.peek(","):
                        self.take()
                        continue
                    args.append(self.or_())
                self.expect(")")
                return DCall(v.upper(), args)
            if v.upper() in ("TRUE", "FALSE"):
                return DCall(v.upper())
            value = self._var(v)
            return DTable(v) if value is None else DVar(v, value)
        raise DaxError(f"unexpected {v!r}")


def _inline_vars(n: Any, under: str = "") -> Any:
    """Replace each VAR reference by its expression. A variable is evaluated once, where it is defined;
    inlining it under CALCULATE, an iterator or a table function would evaluate it in another context."""
    if isinstance(n, DVar):
        if under and not _is_literal(n.value):  # constants mean the same in any context
            hint = next((f for f in dax_functions(n.value) if f in DAX_HINT), "")
            raise DaxError(f"uses VAR/RETURN variables: {n.name} is used inside {under}, so it can't be inlined"
                           + (f"; it uses {hint}: {DAX_HINT[hint]}" if hint else ""))
        return _inline_vars(n.value, under)
    if isinstance(n, DCall):
        inner = under or ("" if n.name in VAR_SAFE else n.name)
        return DCall(n.name, [_inline_vars(a, inner) for a in n.args])
    if isinstance(n, DBin):
        return DBin(n.op, _inline_vars(n.left, under), _inline_vars(n.right, under))
    if isinstance(n, DUn):
        return DUn(n.op, _inline_vars(n.operand, under))
    if isinstance(n, DSet):
        return DSet([_inline_vars(i, under) for i in n.items])
    return n


def dax_parse(text: str) -> Any:
    if not (text or "").strip():
        raise DaxError("empty expression")
    return _inline_vars(_DaxParser(text).parse())


def dax_functions(node: Any) -> list[str]:
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, DCall):
            out.append(n.name)
            for a in n.args:
                walk(a)
        elif isinstance(n, DBin):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, DUn):
            walk(n.operand)
        elif isinstance(n, DSet):
            for a in n.items:
                walk(a)

    walk(node)
    return out


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _qi(name: str) -> str:
    return name if SIMPLE_IDENT.match(name) else '"' + name.replace('"', '""') + '"'


def _is_literal(n: Any) -> bool:
    return isinstance(n, (DStr, DNum)) or (isinstance(n, DCall) and n.name in ("TRUE", "FALSE", "BLANK") and not n.args) or (
        isinstance(n, DUn) and isinstance(n.operand, DNum)
    )


def _terms(n: Any, sign: int = 1) -> list[tuple[int, Any]]:
    """a - (b + c) -> [(1, a), (-1, b), (-1, c)]"""
    if isinstance(n, DBin) and n.op in ("+", "-"):
        return _terms(n.left, sign) + _terms(n.right, sign if n.op == "+" else -sign)
    return [(sign, n)]


def _sum_sql(terms: list[tuple[int, str, bool]], never_blank: bool) -> str:
    """(sign, sql, never blank) terms -> SQL. DAX + and - treat BLANK as 0 (BLANK + 5 = 5) and are BLANK only
    when every input is (`[M] + 0` never is); in SQL NULL + 5 is NULL."""
    body = " ".join(f"{'+' if sign > 0 else '-'} {sql if sure else f'COALESCE({sql}, 0)'}" for sign, sql, sure in terms)
    body = body.removeprefix("+ ")
    if never_blank:
        return f"({body})"
    return f"(CASE WHEN {' AND '.join(f'{sql} IS NULL' for _, sql, _ in terms)} THEN NULL ELSE {body} END)"


def _is_zero(n: Any) -> bool:
    return isinstance(n, DNum) and float(n.value) == 0


def _lit_sql(n: Any) -> str:
    if isinstance(n, DStr):
        return _sql_str(n.value)
    if isinstance(n, DNum):
        return n.value
    if isinstance(n, DUn):
        return f"-{n.operand.value}"
    return {"TRUE": "TRUE", "FALSE": "FALSE", "BLANK": "NULL"}[n.name]


def _paren(sql: str, n: Any) -> str:
    """Parenthesize an operand that is itself an operation, so DAX precedence survives in SQL."""
    if not (isinstance(n, (DBin, DUn)) or (isinstance(n, DCall) and n.name == "DIVIDE")) or _wrapped(sql):
        return sql
    return f"({sql})"


def _wrapped(sql: str) -> bool:
    """True if sql is one parenthesized group: "(a - b)" but not "(a) - (b)"."""
    if not (sql.startswith("(") and sql.endswith(")")) or "'" in sql:
        return False
    depth = 0
    for i, ch in enumerate(sql):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 0 and i < len(sql) - 1:
            return False
    return True


def _div_sql(num: str, num_node: Any, den: str, alt: str | None = None) -> str:
    """DAX `/` and DIVIDE(): never integer division, NULL (BLANK) on a zero or blank denominator,
    or DIVIDE's alternate result."""
    if alt is None:
        return f"1.0 * {_paren(num, num_node)} / NULLIF({den}, 0)"
    return f"CASE WHEN COALESCE({den}, 0) = 0 THEN {alt} ELSE 1.0 * {_paren(num, num_node)} / NULLIF({den}, 0) END"


def _sn(name: str) -> str:
    """snake_case that also splits CamelCase identifiers: OrderDate -> order_date ('Sales YoY' stays sales_yoy)."""
    name = unicodedata.normalize("NFKD", (name or "").replace("Δ", " delta ").replace("∆", " delta "))
    name = name.encode("ascii", "ignore").decode()  # names must be ASCII identifiers: Año -> ano
    if " " not in name.strip():
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return snake(name)


def _mname(name: str) -> str:
    if " " not in name.strip():  # split CamelCase before '#' and '%' add spaces: #AvailableCapacity -> count_available_capacity
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return _sn(name.replace("%", " pct ").replace("#", " count "))


# ==================================================================== reports (normalised)


@dataclass
class VField:
    kind: str  # column | measure | agg | hier
    entity: str
    prop: str
    func: int | None = None
    level: str = ""
    hierarchy: str = ""
    variation: bool = False
    label: str = ""


@dataclass
class VVisual:
    name: str
    type: str
    title: str = ""
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    roles: dict[str, list[VField]] = field(default_factory=dict)
    filters: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    source: str = ""


@dataclass
class VPage:
    name: str
    display: str
    visuals: list[VVisual] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    hidden: bool = False


def _source_entity(expr: dict[str, Any], aliases: dict[str, str]) -> str:
    ref = (expr or {}).get("SourceRef") or {}
    return ref.get("Entity") or aliases.get(ref.get("Source", ""), ref.get("Source", ""))


def norm_field(d: dict[str, Any], aliases: dict[str, str] | None = None) -> VField | None:
    aliases = aliases or {}
    if not isinstance(d, dict):
        return None
    if "Column" in d:
        c = d["Column"]
        return VField("column", _source_entity(c.get("Expression"), aliases), c.get("Property", ""))
    if "Measure" in d:
        c = d["Measure"]
        return VField("measure", _source_entity(c.get("Expression"), aliases), c.get("Property", ""))
    if "Aggregation" in d:
        a = d["Aggregation"]
        inner = norm_field(a.get("Expression") or {}, aliases)
        if inner is None or inner.kind != "column":
            return None
        inner.kind, inner.func = "agg", int(a.get("Function", 0))
        return inner
    if "HierarchyLevel" in d:
        hl = d["HierarchyLevel"]
        h = (hl.get("Expression") or {}).get("Hierarchy") or {}
        src = h.get("Expression") or {}
        if "PropertyVariationSource" in src:
            pvs = src["PropertyVariationSource"]
            return VField("hier", _source_entity(pvs.get("Expression"), aliases), pvs.get("Property", ""), level=hl.get("Level", ""),
                          hierarchy=h.get("Hierarchy", ""), variation=True)
        return VField("hier", _source_entity(src, aliases), "", level=hl.get("Level", ""), hierarchy=h.get("Hierarchy", ""))
    return None


def _literal_text(obj: Any) -> str:
    try:
        v = obj["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return ""
    return v[1:-1].replace("''", "'") if isinstance(v, str) and v.startswith("'") else str(v)


def _title(objects: dict[str, Any]) -> str:
    for t in (objects or {}).get("title") or []:
        text = _literal_text(((t or {}).get("properties") or {}).get("text"))
        if text:
            return text
    return ""


def _paragraphs(objects: dict[str, Any]) -> str:
    for g in (objects or {}).get("general") or []:
        paras = ((g or {}).get("properties") or {}).get("paragraphs")
        if isinstance(paras, dict):
            paras = paras.get("expr", {}).get("Literal", {}).get("Value")
        if isinstance(paras, str):
            try:
                paras = json.loads(paras)
            except ValueError:
                return paras
        if isinstance(paras, list):
            lines = []
            for p in paras:
                runs = p.get("textRuns") or []
                lines.append("".join(str(r.get("value", "")) for r in runs))
            return "\n\n".join(lines).strip()
    return ""


def _json_str(v: Any, default: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v) if v.strip() else default
        except ValueError:
            return default
    return v if v is not None else default


def visual_from_pbir(data: dict[str, Any], rel: str) -> VVisual:
    v = data.get("visual") or {}
    pos = data.get("position") or {}
    vis = VVisual(
        name=str(data.get("name") or PurePosixPath(rel).parent.name),
        type=v.get("visualType", "unknown") if v else ("group" if data.get("visualGroup") else "unknown"),
        x=float(pos.get("x", 0)), y=float(pos.get("y", 0)), w=float(pos.get("width", 0)), h=float(pos.get("height", 0)),
        source=rel,
    )
    vis.title = _title(v.get("visualContainerObjects") or {}) or _title(v.get("objects") or {})
    vis.text = _paragraphs(v.get("objects") or {})
    for role, state in ((v.get("query") or {}).get("queryState") or {}).items():
        fields = []
        for p in (state or {}).get("projections") or []:
            f = norm_field(p.get("field") or {})
            if f:
                f.label = p.get("displayName") or ""
                fields.append(f)
        vis.roles[role] = fields
    vis.filters = list((data.get("filterConfig") or {}).get("filters") or [])
    return vis


def visual_from_legacy(container: dict[str, Any], rel: str, idx: int) -> VVisual:
    cfg = _json_str(container.get("config"), {})
    sv = cfg.get("singleVisual") or {}
    layout = ((cfg.get("layouts") or [{}])[0].get("position")) or {}
    vis = VVisual(
        name=str(cfg.get("name") or f"visual{idx}"),
        type=sv.get("visualType", "group" if cfg.get("singleVisualGroup") else "unknown"),
        x=float(container.get("x", layout.get("x", 0))), y=float(container.get("y", layout.get("y", 0))),
        w=float(container.get("width", layout.get("width", 0))), h=float(container.get("height", layout.get("height", 0))),
        source=rel,
    )
    vis.title = _title(sv.get("vcObjects") or {}) or _title(sv.get("objects") or {})
    vis.text = _paragraphs(sv.get("objects") or {})
    proto = sv.get("prototypeQuery") or {}
    aliases = {f.get("Name", ""): f.get("Entity", "") for f in proto.get("From") or []}
    by_ref: dict[str, VField] = {}
    for sel in proto.get("Select") or []:
        f = norm_field(sel, aliases)
        if f:
            by_ref[sel.get("Name", "")] = f
    for role, projs in (sv.get("projections") or {}).items():
        vis.roles[role] = [by_ref[p["queryRef"]] for p in projs or [] if p.get("queryRef") in by_ref]
    vis.filters = list(_json_str(container.get("filters"), []) or [])
    return vis


# ==================================================================== SQL resolvers


class _SameTable:
    """Column resolver for the semantic layer: only the table's own columns, unqualified."""

    def __init__(self, table: str):
        self.table = table

    def touch(self, table: str) -> None:
        if table.lower() != self.table.lower():
            raise DaxError(f"references another table ({table}): {DAX_HINT['RELATED']}")

    def col(self, table: str, physical: str) -> str:
        self.touch(table)
        return _qi(physical)


class _Recorder:
    def __init__(self) -> None:
        self.tables: list[str] = []
        self.cols: set[tuple[str, str]] = set()  # (table, column lower) of the model columns an expression reads

    def touch(self, table: str) -> None:
        if table not in self.tables:
            self.tables.append(table)

    def col(self, table: str, physical: str) -> str:
        self.touch(table)
        return _qi(physical)


class _Qualified(_Recorder):
    def __init__(self, aliases: dict[str, str]):
        super().__init__()
        self.aliases = aliases

    def col(self, table: str, physical: str) -> str:
        self.touch(table)
        if table not in self.aliases:
            raise DaxError(f"table {table} is not joined")
        return f"{self.aliases[table]}.{_qi(physical)}"


def _alias(table: str) -> str:
    a = snake(table)
    return f"{a}_t" if a in SQL_RESERVED else a


ALL_KINDS = frozenset({"where", "group", "calc"})
SELECTED_KINDS = frozenset({"group"})  # ALLSELECTED: the visual's own grouping; slicers and filters stay
REMOVERS = {"ALL", "REMOVEFILTERS", "ALLEXCEPT", "ALLSELECTED"}


@dataclass
class _Pred:
    """One filter of a tile's filter context: a report/page/visual filter or slicer ("where"), the visual's
    grouping on one field ("group", an equality to the outer row in subqueries) or a CALCULATE filter ("calc").
    `cols`: the model columns it reads, (table, column lower). `enforced`: the query being written applies it
    already (WHERE, GROUP BY); otherwise aggregates apply it with CASE WHEN."""

    sql: Callable[[Any], str]
    cols: frozenset[tuple[str, str]]
    kind: str
    enforced: bool = True


class _TileCtx:
    """The query a tile's SQL is built in. A measure that changes the filter context (ALL, ALLEXCEPT,
    ALLSELECTED, FILTER(ALL(..)), a CALCULATE filter on a grouped column, an iterator over VALUES) becomes a
    correlated subquery. Then the tile groups in a derived table `g` first (`grouped`), its aggregates are
    hoisted there, and the subqueries compare against g's plain columns."""

    def __init__(self, parser: "_Parser", pm: PModel, base: str, joins: dict[str, PRel], main: Any):
        self.parser, self.pm, self.base, self.joins, self.main = parser, pm, base, joins, main
        self.tables = [base] + list(joins)
        self.preds: list[_Pred] = []
        self.grouped = False
        self.leaves: dict[str, str] = {}
        self.used = False
        self.n = 0
        both = [r for r in pm.relationships if r.cross_filter.lower() == "bothdirections" and {r.from_table, r.to_table} <= set(self.tables)]
        self.bidi = f"{both[0].from_table} - {both[0].to_table}" if both else ""

    def need(self) -> None:
        self.used = True
        if self.bidi:
            raise DaxError(f"changes the filter context across the bi-directional relationship {self.bidi}; write the tile SQL by hand")

    def sub(self, tables: list[str] | None = None) -> _Qualified:
        self.n += 1
        return _Qualified({t: f"{_alias(t)}_{self.n}" for t in tables or self.tables})

    def from_sql(self, q: Any) -> str:
        return " ".join(self.parser._from_lines(self.pm, self.base, self.joins, q))

    def outer(self, alias: str) -> str:
        return f"g.{alias}"

    def hoist(self, sql: str, q: Any) -> str:
        """An aggregate of the tile's own query: a column of g when the tile groups in g."""
        if not self.grouped or q is not self.main:
            return sql
        if sql not in self.leaves:
            self.leaves[sql] = f"_a{len(self.leaves) + 1}"
        return f"g.{self.leaves[sql]}"


def layout_rows(items: list[tuple[str, float, float, float]]) -> list[list[str]]:
    """[(id, x, y, height)] -> rows (top to bottom) of ids (left to right)."""
    rows: list[dict[str, Any]] = []
    for tid, x, y, h in sorted(items, key=lambda i: (i[2], i[1])):
        if rows and y < rows[-1]["top"] + max(rows[-1]["h"], 1) / 2:
            rows[-1]["items"].append((x, tid))
            rows[-1]["h"] = min(rows[-1]["h"], h) if h else rows[-1]["h"]
        else:
            rows.append({"top": y, "h": h, "items": [(x, tid)]})
    return [[tid for _, tid in sorted(r["items"])] for r in rows]


# ==================================================================== parser


@dataclass
class MT:
    metric: str
    kind: str
    measure: str = ""
    filters: list[str] = field(default_factory=list)
    model: str = ""


class _Parser:
    def __init__(self, store: RawStore):
        self.store = store
        self.b = Bundle(tool="powerbi")
        self.models: dict[str, PModel] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.sm: dict[tuple[str, str], SemanticModel] = {}  # (model id, table) -> semantic model
        self.phys: dict[tuple[str, str], str] = {}  # (model id, table) -> schema.table
        self.msrc: dict[tuple[str, str], MSource] = {}
        self.dim_names: dict[tuple[str, str, str], str] = {}  # (model, table, column lower) -> dimension name
        self.used: set[str] = set()
        self.mt: dict[tuple[str, str], MT] = {}
        self._busy: set[tuple[str, str]] = set()
        self.usage: dict[str, dict[str, Any]] = store.get_json("usage.json") if store.exists("usage.json") else {}

    # ------------------------------------------------------------ loading

    def run(self) -> Bundle:
        for rel in self.store.glob("workspaces/*/datasets.json"):
            ws = rel.split("/")[1]
            for d in self.store.get_json(rel) or []:
                self.datasets[d["id"]] = {**d, "workspace": ws}
        for rel in self.store.glob("workspaces/*/reports.json"):
            ws = rel.split("/")[1]
            for r in self.store.get_json(rel) or []:
                self.reports[r["id"]] = {**r, "workspace": ws}
        model_ids = sorted({rel.split("/")[1] for rel in self.store.glob("models/*/**/*") if rel.count("/") >= 2})
        for mid in model_ids:
            files = {rel: self.store.get_text(rel) for rel in self.store.glob(f"models/{mid}/**/*.tmdl")}
            if not files:
                if self.store.glob(f"models/{mid}/**/model.bim"):
                    self.b.warn(f"model:{mid}", "definition is TMSL (model.bim), not TMDL; re-extract with format=TMDL", "warn")
                continue
            try:
                pm = model_from_tmdl(mid, files)
            except Exception as exc:  # noqa: BLE001 - one broken model must not stop the rest
                self.b.warn(f"model:{mid}", f"TMDL did not parse: {exc}")
                continue
            meta = self.datasets.get(mid, {})
            pm.name = meta.get("name") or pm.name
            pm.workspace = meta.get("workspace", "")
            pm.source = f"models/{mid}"
            for rel in self.store.glob(f"models/{mid}/Copilot/**/*"):
                pm.copilot[rel.removeprefix(f"models/{mid}/")] = self.store.get_text(rel)
            self.models[mid] = pm
        for rel in self.store.glob("scanner/*.json"):
            for ws in (self.store.get_json(rel) or {}).get("workspaces") or []:
                for ds in ws.get("datasets") or []:
                    if ds.get("id") in self.models or not ds.get("tables"):
                        continue
                    pm = model_from_scanner(ds, ws.get("id", ""))
                    pm.source = rel
                    self.models[pm.id] = pm
                    self.b.warn(f"model:{pm.id}", "rebuilt from the admin scanner (no sourceColumn, relationships or OLS); check column names", "info")
        self._name_models()
        for pm in self.models.values():
            self._model(pm)
        self.b.detect_warehouse(ms.connector for ms in self.msrc.values())
        for pm in self.models.values():
            for t in pm.tables.values():
                for m in t.measures:
                    self._translate(pm, m)
            self._roles(pm)
            self._copilot(pm)
            self._knowledge(pm)
        for rid in sorted({rel.split("/")[1] for rel in self.store.glob("reports/*/**/*")}):
            try:
                self._report(rid)
            except Exception as exc:  # noqa: BLE001
                self.b.warn(f"report:{rid}", f"skipped: {exc}")
        dashboards = sum(len(self.store.get_json(r) or []) for r in self.store.glob("workspaces/*/dashboards.json"))
        if dashboards:
            self.b.warn("dashboards", f"{dashboards} Power BI dashboard(s) (pinned tiles) are not migrated; their tiles come from reports, which are", "info")
        if self.store.exists("_errors.json"):
            for e in self.store.get_json("_errors.json") or []:
                self.b.warn(e.get("item", "?") + (f" ({e['name']})" if e.get("name") else ""), e.get("error", ""))
        return self.b.settle_time_dimensions()

    def _name_models(self) -> None:
        counts: dict[str, int] = {}
        for pm in self.models.values():
            for t in pm.tables:
                counts[_sn(t)] = counts.get(_sn(t), 0) + 1
        self.sm_name: dict[tuple[str, str], str] = {}
        for pm in self.models.values():
            for t in pm.tables:
                n = _sn(t)
                self.sm_name[(pm.id, t)] = f"{_sn(pm.name)}_{n}" if counts[n] > 1 else n

    def _unique(self, base: str) -> str:
        name, i = base or "measure", 2
        while name in self.used:
            name, i = f"{base}_{i}", i + 1
        self.used.add(name)
        return name

    # ------------------------------------------------------------ tables -> semantic models

    def _physical_column(self, pm: PModel, table: str, col: PColumn) -> str:
        ms = self.msrc.get((pm.id, table))
        src = col.source_column or col.name
        # renames point back at a warehouse table or native query; a source someone rewrites as SQL keeps Power BI's names
        return ms.renames.get(src, src) if ms and (ms.table or ms.sql) else src

    def _model(self, pm: PModel) -> None:
        keys: dict[str, set[str]] = {}
        for r in pm.relationships:
            if r.many_to_one:
                keys.setdefault(r.from_table, set()).add(r.from_column.lower())
                keys.setdefault(r.to_table, set()).add(r.to_column.lower())
        for t in pm.tables.values():
            item = f"{pm.name} table {t.name}"
            src = f"{pm.source} table: {t.name}"
            if t.is_calc_group:
                self.b.warn(item, "calculation group: apply each item to the metrics it targets as separate derived/cumulative metrics (see source-powerbi skill)")
                continue
            part = t.partitions[0] if t.partitions else PPartition("", "m")
            source = part.source
            if part.type == "calculated" and "NAMEOF" in source.upper():
                self.b.warn(item, "field parameter table (NAMEOF list); becomes a notebook parameter that switches columns/metrics, not a model", "info")
                continue
            if part.type == "m":
                ms = m_source(source, pm.expressions)
            elif part.type == "entity":
                ms = entity_source(part, pm.expressions)
            else:
                ms = MSource(problems=[f"{part.type} partition"])
            self.msrc[(pm.id, t.name)] = ms
            has_data_cols = any(not c.expression for c in t.columns if c.name.lower() not in ("value", "column1"))
            if not t.columns or (not has_data_cols and t.measures) or (t.measures and "entered data" in " ".join(ms.problems) and len(t.columns) <= 1):
                if t.measures:
                    self.b.warn(item, "measure-only table; its measures were attached to the tables they aggregate", "info")
                continue
            name = self.sm_name[(pm.id, t.name)]
            todo = ""
            physical_ok = bool(ms.table and not ms.sql and not [p for p in ms.problems if "Power Query steps" not in p])
            if physical_ok:
                phys = ".".join(x for x in (ms.schema, ms.table) if x)
            else:
                phys = name
                if part.type == "calculated":
                    todo = "calculated table (DAX): rewrite as SQL and materialize it"
                    body = "-- " + source.replace("\n", "\n-- ")
                elif ms.sql:
                    todo = "Power Query native SQL: materialize as a SQL model"
                    body = ms.sql
                else:
                    todo = "Power Query source not translatable (" + "; ".join(ms.problems) + "): rewrite as SQL"
                    body = "-- " + (source or "(no source)").replace("\n", "\n-- ")
                self.b.queries.append(
                    VerifiedQuery(name=f"{t.name} source", sql=body, description=f"Power BI table {t.name} in {pm.name}",
                                  datasource=self.b.datasource, source_ref=src,
                                  todo=f"{todo}; name it `{name}` (semantic model `{name}` points at it)")
                )
            if ms.problems and physical_ok:
                self.b.warn(item, "; ".join(ms.problems) + "; replicate them in a SQL model if they change rows or values")
            self.phys[(pm.id, t.name)] = phys
            sm = SemanticModel(name=name, table=phys, description=t.description, datasource=self.b.datasource, source_ref=src)
            via = f" via {ms.connector}({', '.join(x for x in (ms.server, ms.database) if x)})" if ms.connector else ""
            tbl = Table(self.b.datasource, ms.schema, ms.table, t.description, source_ref=src + via) if physical_ok else None
            primary = self._primary(pm, t)
            if primary:
                sm.entities.append(Entity(name, "primary", self._ent_expr(pm, t, primary, name)))
            for r in pm.relationships:
                if r.from_table != t.name:
                    continue
                if not r.active:
                    self.b.warn(item, f"inactive relationship {r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}] (used with USERELATIONSHIP); add a second entity/time dimension by hand if needed", "info")
                    continue
                rel = f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"
                if r.to_many:
                    self.b.warn(item, f"many-to-many relationship {rel}: not an entity (MetricFlow joins many-to-one only); join through a bridge table or in SQL")
                    continue
                pk = self._primary(pm, pm.table(r.to_table)) if pm.table(r.to_table) else None
                if pk is not None and pk.name.lower() != r.to_column.lower():
                    self.b.warn(item, f"relationship {rel} is not on {r.to_table}'s key [{pk.name}]: not an entity; add one by hand if you need the join")
                    continue
                target = self.sm_name.get((pm.id, r.to_table), _sn(r.to_table))
                col = t.column(r.from_column)
                if col and not any(e.name == target for e in sm.entities):
                    sm.entities.append(Entity(target, "foreign", self._ent_expr(pm, t, col, target)))
                if r.cross_filter.lower() == "bothdirections":
                    self.b.warn(item, f"bi-directional relationship to {r.to_table}; MetricFlow joins many-to-one only, check measures that relied on it", "info")
            taken = {e.name for e in sm.entities}
            for c in t.columns:
                key = c.name.lower() in keys.get(t.name, set()) or c.is_key
                phys_col = self._physical_column(pm, t.name, c) if not c.expression else ""
                if tbl is not None and phys_col:
                    tbl.columns.append(Column(phys_col, COLUMN_TYPE.get(c.data_type, c.data_type), c.description, c.name, c.hidden))
                if key and c.data_type != "datetime":
                    continue
                dname = _sn(c.name)
                if dname in taken:
                    dname = f"{dname}_dim"
                taken.add(dname)
                self.dim_names[(pm.id, t.name, c.name.lower())] = dname
                dim = Dimension(dname, "time" if c.data_type == "datetime" else "categorical", description=c.description, label=c.name)
                if dim.type == "time":
                    dim.time_granularity = "day"
                if c.expression:
                    try:
                        dim.expr = self.row_sql(pm, dax_parse(c.expression), t.name, _SameTable(t.name))
                    except DaxError as exc:
                        dim.todo, dim.source_expression, dim.source_language = f"calculated column: {exc}", c.expression, "dax"
                elif phys_col.lower() != dname:
                    dim.expr = _qi(phys_col)
                sm.dimensions.append(dim)
            self.sm[(pm.id, t.name)] = sm
            self.b.semantic_models.append(sm)
            if tbl is not None:
                self.b.tables.append(tbl)

    @staticmethod
    def _primary(pm: PModel, t: PTable) -> PColumn | None:
        """The column a table's primary entity is on: the target of its first many-to-one relationship, else isKey."""
        to_keys = [r for r in pm.relationships if r.many_to_one and r.to_table == t.name]
        return (t.column(to_keys[0].to_column) if to_keys else None) or next((c for c in t.columns if c.is_key), None)

    def _ent_expr(self, pm: PModel, t: PTable, col: PColumn, name: str) -> str:
        if col.expression:
            return ""
        phys = self._physical_column(pm, t.name, col)
        return "" if phys.lower() == name else _qi(phys)

    # ------------------------------------------------------------ column / row SQL

    def column_sql(self, pm: PModel, table: str, name: str, q: Any, depth: int = 0) -> str:
        t = pm.table(table)
        col = t.column(name) if t else None
        if col is None:
            raise DaxError(f"unknown column {table}[{name}]")
        if depth == 0 and isinstance(q, _Recorder):
            q.cols.add((t.name, col.name.lower()))
        if col.expression:
            if depth > 5:
                raise DaxError(f"calculated column {table}[{name}] is too deeply nested")
            return "(" + self.row_sql(pm, dax_parse(col.expression), t.name, q, depth + 1) + ")"
        if (pm.id, t.name) not in self.phys:
            raise DaxError(f"table {t.name} has no SQL source")
        return q.col(t.name, self._physical_column(pm, t.name, col))

    def _col_node(self, pm: PModel, node: DCol, ctx: str | None) -> tuple[str, str]:
        table = node.table or ctx
        t = pm.table(table)
        if t is None or t.column(node.name) is None:
            if pm.measure(node.name):
                raise DaxError(f"[{node.name}] is a measure, not a column")
            raise DaxError(f"unknown column {table}[{node.name}]")
        return t.name, node.name

    def row_sql(self, pm: PModel, n: Any, ctx: str | None, q: Any, depth: int = 0) -> str:
        """DAX row-context expression -> SQL (calculated columns, RLS, iterator bodies, filters)."""
        rs = lambda x: self.row_sql(pm, x, ctx, q, depth)  # noqa: E731
        if isinstance(n, DCol):
            table, name = self._col_node(pm, n, ctx)
            return self.column_sql(pm, table, name, q, depth)
        if _is_literal(n):
            return _lit_sql(n)
        if isinstance(n, DRaw):
            return n.sql
        if isinstance(n, DUn):
            return f"{n.op}{_paren(rs(n.operand), n.operand)}"
        if isinstance(n, DSet):
            return "(" + ", ".join(rs(i) for i in n.items) + ")"
        if isinstance(n, DBin):
            if n.op in ("=", "==", "<>") and _is_literal(n.right) and isinstance(n.right, DCall) and n.right.name == "BLANK":
                return f"{_paren(rs(n.left), n.left)} IS {'NOT ' if n.op == '<>' else ''}NULL"
            op = {"&": "||", "&&": "AND", "||": "OR", "==": "=", "IN": "IN", "^": "^"}.get(n.op, n.op)
            left, right = rs(n.left), rs(n.right)
            if op in ("AND", "OR"):
                return f"({left} {op} {right})"
            if op == "/":
                return _div_sql(left, n.left, right)
            if op in ("+", "-"):
                const = lambda x: _is_literal(x) and not (isinstance(x, DCall) and x.name == "BLANK")  # noqa: E731
                terms = [(sign, rs(x), const(x)) for sign, x in _terms(n) if not _is_zero(x)]
                return _sum_sql(terms or [(1, "0", True)], any(const(x) for _, x in _terms(n)))
            return f"{_paren(left, n.left)} {op} {_paren(right, n.right)}"
        if isinstance(n, DCall):
            a = n.args
            f = n.name
            if f in ("USERPRINCIPALNAME", "USERNAME", "CUSTOMDATA") and not a:
                return "{{ user.email }}" if f != "CUSTOMDATA" else "{{ user.customdata }}"
            if f == "IF" and len(a) in (2, 3):
                return f"CASE WHEN {rs(a[0])} THEN {rs(a[1])}" + (f" ELSE {rs(a[2])}" if len(a) == 3 else "") + " END"
            if f == "SWITCH" and len(a) >= 3:
                if isinstance(a[0], DCall) and a[0].name == "TRUE":
                    pairs = [f"WHEN {rs(a[i])} THEN {rs(a[i + 1])}" for i in range(1, len(a) - 1, 2)]
                else:
                    subject = rs(a[0])
                    pairs = [f"WHEN {subject} = {rs(a[i])} THEN {rs(a[i + 1])}" for i in range(1, len(a) - 1, 2)]
                tail = f" ELSE {rs(a[-1])}" if len(a) % 2 == 0 else ""
                return "CASE " + " ".join(pairs) + tail + " END"
            if f == "NOT" and len(a) == 1:
                return f"NOT ({rs(a[0])})"
            if f == "AND" and len(a) == 2:
                return f"({rs(a[0])} AND {rs(a[1])})"
            if f == "OR" and len(a) == 2:
                return f"({rs(a[0])} OR {rs(a[1])})"
            if f == "ISBLANK" and len(a) == 1:
                return f"{_paren(rs(a[0]), a[0])} IS NULL"
            if f == "DIVIDE" and len(a) in (2, 3):
                return _div_sql(rs(a[0]), a[0], rs(a[1]), rs(a[2]) if len(a) == 3 else None)
            if f in ("YEAR", "MONTH", "DAY", "QUARTER", "HOUR") and len(a) == 1:
                return f"EXTRACT({f} FROM {rs(a[0])})"
            simple = {"UPPER": "UPPER", "LOWER": "LOWER", "TRIM": "TRIM", "LEN": "LENGTH", "ABS": "ABS", "ROUND": "ROUND",
                      "LEFT": "LEFT", "RIGHT": "RIGHT", "COALESCE": "COALESCE", "INT": "FLOOR", "MID": "SUBSTRING"}
            if f in simple and a:
                return f"{simple[f]}({', '.join(rs(x) for x in a)})"
            if f == "RELATED" and len(a) == 1 and isinstance(a[0], DCol) and a[0].table:
                q.touch(a[0].table)
                return self.column_sql(pm, a[0].table, a[0].name, q, depth)
            raise DaxError(f"uses {f}" + (f" ({DAX_HINT[f]})" if f in DAX_HINT else ""))
        if isinstance(n, DTable):
            raise DaxError(f"table reference {n.name} in a row expression")
        raise DaxError("unsupported expression")

    # ------------------------------------------------------------ measures -> metrics

    def _agg_of(self, pm: PModel, n: Any) -> tuple[str, str, str] | None:
        """SUM(T[c]) / COUNTROWS(T) / SUMX(T, row expr on T) -> (agg, expr, table)."""
        if not isinstance(n, DCall):
            return None
        if n.name in DAX_AGG and len(n.args) == 1 and isinstance(n.args[0], DCol):
            table, col = self._col_node(pm, n.args[0], None)
            return DAX_AGG[n.name], self.column_sql(pm, table, col, _SameTable(table)), table
        if n.name == "COUNTROWS" and len(n.args) == 1 and isinstance(n.args[0], DTable):
            t = pm.table(n.args[0].name)
            if t is None:
                raise DaxError(f"unknown table {n.args[0].name}")
            return "count", "1", t.name
        if n.name in DAX_ITER and len(n.args) == 2 and isinstance(n.args[0], DTable):
            t = pm.table(n.args[0].name)
            if t is None:
                raise DaxError(f"unknown table {n.args[0].name}")
            try:
                expr = self.row_sql(pm, n.args[1], t.name, _SameTable(t.name))
            except DaxError as exc:
                raise DaxError(f"{n.name} over {t.name}: {exc}") from exc
            return DAX_ITER[n.name], expr, t.name
        return None

    def _add_measure(self, pm: PModel, name: str, agg: str, expr: str, table: str, pmeas: PMeasure | None) -> tuple[str, str]:
        sm = self.sm.get((pm.id, table))
        if sm is None:
            raise DaxError(f"aggregates table {table}, which has no semantic model")
        sm.measures.append(Measure(name, agg, expr, pmeas.description if pmeas else "", pmeas.name if pmeas else ""))
        return name, sm.name

    def _measure_ref(self, pm: PModel, n: Any) -> PMeasure | None:
        if isinstance(n, DCol):
            t = pm.table(n.table) if n.table else None
            if t is not None and t.column(n.name):
                return None
            return pm.measure(n.name)
        return None

    def _translate(self, pm: PModel, m: PMeasure) -> MT:
        key = (pm.id, m.name.lower())
        if key in self.mt:
            return self.mt[key]
        if key in self._busy:
            raise DaxError(f"circular reference to [{m.name}]")
        self._busy.add(key)
        name = self._unique(_mname(m.name))
        metric = Metric(name=name, label=m.name, description=m.description, source_ref=f"{pm.source} measure: {m.table}[{m.name}]")
        home = self.sm.get((pm.id, m.table))
        try:
            node = dax_parse(m.expression)
            result = self._classify(pm, m, node, metric)
        except Exception as exc:  # noqa: BLE001 - DaxError, or a shape we did not expect: keep the DAX
            metric.type, metric.measure, metric.numerator, metric.denominator = "simple", "", "", ""
            metric.expr, metric.metrics, metric.offsets, metric.filter, metric.grain_to_date = "", [], {}, "", ""
            metric.todo, metric.source_expression, metric.source_language = str(exc), m.expression, "dax"
            if not metric.semantic_model:
                metric.semantic_model = home.name if home else self._first_model(pm, m.expression)
            result = MT(name, "todo", model=metric.semantic_model)
        self._busy.discard(key)
        self.b.metrics.append(metric)
        self.mt[key] = result
        return result

    def _first_model(self, pm: PModel, expr: str) -> str:
        for ref in re.findall(r"(?<![\w'])\[([^\]]+)\]", expr or ""):
            t = self.mt.get((pm.id, ref.lower()))
            if t and t.model:
                return t.model
        m = re.search(r"(?:'((?:[^']|'')+)'|\b([A-Za-z_]\w*))\[", expr or "")
        if m:
            t = pm.table(_unquote_name(m.group(1) or m.group(2)))
            if t and (pm.id, t.name) in self.sm:
                return self.sm[(pm.id, t.name)].name
        return ""

    def _metric_of(self, pm: PModel, n: Any) -> MT:
        ref = self._measure_ref(pm, n)
        if ref is None:
            raise DaxError("not a measure reference")
        t = self._translate(pm, ref)
        if t.kind == "todo":
            raise DaxError(f"depends on [{ref.name}], which is not translated yet")
        return t

    def _simple_of(self, pm: PModel, n: Any, hint: str) -> MT:
        ref = self._measure_ref(pm, n)
        if ref is not None:
            t = self._metric_of(pm, n)
            if t.kind != "simple":
                raise DaxError(f"CALCULATE over [{ref.name}], a {t.kind} metric: write a filtered copy of its inputs by hand")
            return t
        agg = self._agg_of(pm, n)
        if agg:
            sm = self.sm.get((pm.id, agg[2]))
            same = next((x for x in sm.measures if (x.agg, x.expr) == agg[:2]), None) if sm else None
            if same is not None:  # SUM(T[c]) written out again: reuse the measure
                return MT("", "simple", same.name, [], sm.name)
            col = agg[1][1:-1].replace('""', '"') if agg[1].startswith('"') else agg[1]
            if SIMPLE_IDENT.match(agg[1]) or agg[1].startswith('"'):  # shared by other metrics: name it after what it sums
                hint = _sn(f"{agg[0]} {_sn(col)}")
            mname, model = self._add_measure(pm, self._unique(hint), agg[0], agg[1], agg[2], None)
            return MT("", "simple", mname, [], model)
        if isinstance(n, DCall) and n.name == "CALCULATE" and n.args:
            inner = self._simple_of(pm, n.args[0], hint)
            if any(self._is_time_intel(f) for f in n.args[1:]):
                raise DaxError("nested time intelligence")
            return MT("", "simple", inner.measure, inner.filters + [self.filter_jinja(pm, f) for f in n.args[1:]], inner.model)
        raise DaxError(self._reason(n))

    @staticmethod
    def _is_time_intel(n: Any) -> bool:
        return isinstance(n, DCall) and n.name in TIME_INTEL

    def _offset(self, n: DCall) -> str:
        if n.name == "SAMEPERIODLASTYEAR" and len(n.args) == 1:
            return "1 year"
        if n.name == "DATEADD" and len(n.args) == 3 and isinstance(n.args[2], DTable):
            amount = n.args[1]
            unit = n.args[2].name.lower()
            if isinstance(amount, DUn) and amount.op == "-" and isinstance(amount.operand, DNum) and unit in ("year", "quarter", "month", "day"):
                k = int(float(amount.operand.value))
                return f"{k} {unit}" + ("s" if k > 1 else "")
        raise DaxError(f"time intelligence {n.name}" + (f" ({DAX_HINT[n.name]})" if n.name in DAX_HINT else ""))

    def _classify(self, pm: PModel, m: PMeasure, n: Any, metric: Metric) -> MT:
        name = metric.name
        if isinstance(n, DCall) and n.name == "CALCULATE" and len(n.args) == 1:  # no filters: the expression itself
            return self._classify(pm, m, n.args[0], metric)
        agg = self._agg_of(pm, n)
        if agg:
            mname, model = self._add_measure(pm, name, agg[0], agg[1], agg[2], m)
            metric.measure, metric.semantic_model = mname, model
            return MT(name, "simple", mname, [], model)
        if isinstance(n, DCall) and n.name == "CALCULATE" and len(n.args) >= 2:
            inner, filters = n.args[0], n.args[1:]
            ti = [f for f in filters if self._is_time_intel(f)]
            if ti and len(filters) == 1:
                f = ti[0]
                if f.name in TO_DATE:
                    s = self._simple_of(pm, inner, f"{name}_base")
                    metric.type, metric.measure, metric.grain_to_date = "cumulative", s.measure, TO_DATE[f.name]
                    metric.filter, metric.semantic_model = " AND ".join(s.filters), s.model
                    return MT(name, "cumulative", model=s.model)
                offset = self._offset(f)
                t = self._metric_of(pm, inner)
                metric.type, metric.expr, metric.metrics, metric.offsets = "derived", t.metric, [t.metric], {t.metric: offset}
                metric.semantic_model = t.model
                return MT(name, "derived", model=t.model)
            if ti:
                raise DaxError("CALCULATE mixes time intelligence with other filters")
            s = self._simple_of(pm, inner, f"{name}_base")
            flt = s.filters + [self.filter_jinja(pm, f) for f in filters]
            metric.measure, metric.filter, metric.semantic_model = s.measure, " AND ".join(flt), s.model
            return MT(name, "simple", s.measure, flt, s.model)
        if isinstance(n, DCall) and n.name in ("TOTALYTD", "TOTALQTD", "TOTALMTD"):
            if len(n.args) != 2:
                raise DaxError(f"{n.name} with a filter or fiscal year-end argument: fiscal calendars need a fiscal dimension or a verified query")
            s = self._simple_of(pm, n.args[0], f"{name}_base")
            metric.type, metric.measure, metric.grain_to_date = "cumulative", s.measure, TO_DATE[n.name]
            metric.filter, metric.semantic_model = " AND ".join(s.filters), s.model
            return MT(name, "cumulative", model=s.model)
        ratio = self._ratio_sides(n)
        if ratio:
            sides = []
            for i, side in enumerate(ratio):
                if self._measure_ref(pm, side) is not None:
                    sides.append(self._metric_of(pm, side))
                elif self._agg_of(pm, side):
                    s = self._simple_of(pm, side, f"{name}_{'num' if i == 0 else 'den'}")
                    sides.append(MT(s.measure, "simple", s.measure, [], s.model))
                else:
                    sides = []
                    break
            if sides:
                metric.type, metric.numerator, metric.denominator = "ratio", sides[0].metric, sides[1].metric
                metric.semantic_model = sides[0].model
                return MT(name, "ratio", model=sides[0].model)
        refs: list[MT] = []
        aux: list[Metric] = []
        expr = self._derived(pm, n, refs, name, aux)
        if not refs:
            raise DaxError(self._reason(n))
        self.b.metrics += aux
        metric.type, metric.expr = "derived", expr
        metric.metrics = list(dict.fromkeys(r.metric for r in refs))
        metric.semantic_model = refs[0].model
        return MT(name, "derived", model=refs[0].model)

    @staticmethod
    def _ratio_sides(n: Any) -> tuple[Any, Any] | None:
        """DIVIDE(a, b) / DIVIDE(a, b, BLANK()) / a / b. A ratio metric is NULL where b is 0 or empty, so a DIVIDE
        with another alternate result is a derived metric (see _derived)."""
        if isinstance(n, DCall) and n.name == "DIVIDE" and len(n.args) in (2, 3):
            if len(n.args) == 3 and not (_is_literal(n.args[2]) and _lit_sql(n.args[2]) == "NULL"):
                return None
            return n.args[0], n.args[1]
        if isinstance(n, DBin) and n.op == "/":
            return n.left, n.right
        return None

    def _derived(self, pm: PModel, n: Any, refs: list[MT], name: str, aux: list[Metric]) -> str:
        sub = lambda x: self._derived(pm, x, refs, name, aux)  # noqa: E731
        if self._measure_ref(pm, n) is not None:
            t = self._metric_of(pm, n)
            refs.append(t)
            return t.metric
        if _is_literal(n) and not isinstance(n, DStr):
            return _lit_sql(n)
        if isinstance(n, DUn) and n.op == "-":
            return f"-{_paren(sub(n.operand), n.operand)}"
        zero = next((x for x in ((n.left, n.right) if isinstance(n, DBin) and n.op == "+" else ()) if _is_zero(x)), None)
        if zero is not None:  # `[M] + 0`: BLANK becomes 0
            return f"coalesce({sub(n.right if zero is n.left else n.left)}, 0)"
        if isinstance(n, DBin) and n.op in ("+", "-"):  # BLANK counts as 0 unless every input is BLANK, as in tiles
            terms = [(sign, sub(x), self._never_blank(pm, x)) for sign, x in _terms(n) if not _is_zero(x)]
            return _sum_sql(terms or [(1, "0", True)], self._never_blank(pm, n))
        if isinstance(n, DBin) and n.op in ("*", "/"):
            left, right = sub(n.left), sub(n.right)
            return f"{_paren(left, n.left)} {n.op} {_paren(right, n.right)}"
        if isinstance(n, DCall) and n.name == "DIVIDE" and len(n.args) in (2, 3):
            a, b = sub(n.args[0]), sub(n.args[1])
            a = _paren(a, n.args[0])
            if len(n.args) == 3 and not (_is_literal(n.args[2]) and _lit_sql(n.args[2]) == "NULL"):
                # the alternate result where b is 0 or BLANK; a BLANK numerator over a non-zero b stays BLANK
                return f"CASE WHEN coalesce({b}, 0) = 0 THEN {sub(n.args[2])} ELSE 1.0 * {a} / nullif({b}, 0) END"
            return f"1.0 * {a} / nullif({b}, 0)"
        if isinstance(n, DCall) and n.name == "COALESCE" and len(n.args) >= 2:
            return f"coalesce({', '.join(sub(x) for x in n.args)})"
        if self._agg_of(pm, n):
            s = self._simple_of(pm, n, f"{name}_{len(refs) + 1}")
            refs.append(MT(s.measure, "simple", s.measure, [], s.model))
            return s.measure
        if isinstance(n, DCall) and n.name == "CALCULATE" and n.args:  # a filtered input: its own simple metric
            s = self._simple_of(pm, n, f"{name}_{len(refs) + 1}_base")
            if not s.filters:
                refs.append(MT(s.measure, "simple", s.measure, [], s.model))
                return s.measure
            part = self._unique(f"{name}_{len(refs) + 1}")
            aux.append(Metric(name=part, measure=s.measure, filter=" AND ".join(s.filters), semantic_model=s.model,
                              description=f"input of {name}", source_ref=f"{pm.source} part of measure for {name}"))
            refs.append(MT(part, "simple", s.measure, s.filters, s.model))
            return part
        raise DaxError(self._reason(n))

    def _reason(self, n: Any) -> str:
        fns = dax_functions(n)
        hinted = [f for f in fns if f in DAX_HINT]
        for f in [f for f in hinted if f not in DAX_ITER] + hinted:  # MINX(ALLSELECTED(..), ..) is about ALLSELECTED
            return f"uses {f}: {DAX_HINT[f]}"
        return "DAX not translated deterministically" + (f" (uses {', '.join(dict.fromkeys(fns))})" if fns else "")

    def _dim_ref(self, pm: PModel, n: DCol) -> str:
        table, col = self._col_node(pm, n, None)
        sm = self.sm.get((pm.id, table))
        if sm is None:
            raise DaxError(f"filter on {table}, which has no semantic model")
        ent = next((e.name for e in sm.entities if e.type == "primary"), sm.name)
        dname = self.dim_names.get((pm.id, table, col.lower()))
        if dname is None:
            return f"{{{{ Entity('{ent}') }}}}"
        dim = next(d for d in sm.dimensions if d.name == dname)
        if dim.type == "time":
            return f"{{{{ TimeDimension('{ent}__{dname}', 'day') }}}}"
        return f"{{{{ Dimension('{ent}__{dname}') }}}}"

    def filter_jinja(self, pm: PModel, n: Any) -> str:
        """CALCULATE filter argument -> MetricFlow filter. Column predicates only."""
        if isinstance(n, DCall) and n.name == "KEEPFILTERS" and len(n.args) == 1:
            return self.filter_jinja(pm, n.args[0])
        if isinstance(n, DCall) and n.name == "NOT" and len(n.args) == 1:
            return f"NOT ({self.filter_jinja(pm, n.args[0])})"
        if isinstance(n, DBin) and n.op in ("&&", "||"):
            return f"({self.filter_jinja(pm, n.left)} {'AND' if n.op == '&&' else 'OR'} {self.filter_jinja(pm, n.right)})"
        if isinstance(n, DBin) and n.op in ("=", "==", "<>", "<", ">", "<=", ">="):
            col, lit, op = n.left, n.right, n.op
            if isinstance(lit, DCol) and _is_literal(col):
                col, lit = lit, col
                op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
            if isinstance(col, DCol) and _is_literal(lit):
                ref = self._dim_ref(pm, col)
                value = _lit_sql(lit)
                if value == "NULL":
                    return f"{ref} IS {'NOT ' if op == '<>' else ''}NULL"
                return f"{ref} {'=' if op == '==' else op} {value}"
        if isinstance(n, DBin) and n.op == "IN" and isinstance(n.left, DCol) and isinstance(n.right, DSet) and all(_is_literal(i) for i in n.right.items):
            return f"{self._dim_ref(pm, n.left)} IN ({', '.join(_lit_sql(i) for i in n.right.items)})"
        raise DaxError("CALCULATE filter is not a simple column predicate: " + self._reason(n).removeprefix("DAX not translated deterministically"))

    # ------------------------------------------------------------ measure -> aggregate SQL (tiles)

    def agg_sql(self, pm: PModel, n: Any, q: Any, preds: list[_Pred], depth: int = 0, tile: _TileCtx | None = None) -> str:
        """Measure DAX -> aggregate SQL in the filter context `preds` (see _Pred). Filters the query doesn't
        apply itself become CASE WHEN inside the aggregates."""
        if depth > 8:
            raise DaxError("measure nesting too deep")
        rec = lambda x, p=preds: self.agg_sql(pm, x, q, p, depth + 1, tile)  # noqa: E731
        open_ = [p for p in preds if not p.enforced]

        def cond(expr: str) -> str:
            return f"CASE WHEN {' AND '.join(p.sql(q) for p in open_)} THEN {expr} END" if open_ else expr

        def leaf(sql: str) -> str:
            return tile.hoist(sql, q) if tile else sql

        ref = self._measure_ref(pm, n)
        if ref is not None:
            return rec(dax_parse(ref.expression))
        if isinstance(n, DCall):
            f, a = n.name, n.args
            if f in DAX_AGG and len(a) == 1 and isinstance(a[0], DCol):
                table, col = self._col_node(pm, a[0], None)
                expr = self.column_sql(pm, table, col, q)
                agg = DAX_AGG[f]
                if agg == "count_distinct":
                    return leaf(f"COUNT(DISTINCT {cond(expr)})")
                return leaf(f"{SQL_AGG[agg]}({cond(expr)})")
            if f == "COUNTROWS" and len(a) == 1 and isinstance(a[0], DTable):
                t = pm.table(a[0].name)
                if t is None:
                    raise DaxError(f"unknown table {a[0].name}")
                q.touch(t.name)
                return leaf(f"COUNT({cond('1')})" if open_ else "COUNT(*)")
            if f in DAX_ITER and len(a) == 2 and isinstance(a[0], DTable):
                t = pm.table(a[0].name)
                if t is None:
                    raise DaxError(f"unknown table {a[0].name}")
                q.touch(t.name)
                return leaf(f"{SQL_AGG[DAX_ITER[f]]}({cond(self.row_sql(pm, a[1], t.name, q))})")
            if f in DAX_ITER and len(a) == 2 and isinstance(a[0], DCall) and a[0].name == "VALUES" and len(a[0].args) == 1 and isinstance(a[0].args[0], DCol):
                return self._iter_values(pm, n, q, preds, depth, tile)
            if f == "DIVIDE" and len(a) in (2, 3):
                alt = rec(a[2]) if len(a) == 3 and not (_is_literal(a[2]) and _lit_sql(a[2]) == "NULL") else None
                return _div_sql(rec(a[0]), a[0], rec(a[1]), alt)
            if f == "COALESCE" and len(a) >= 2:
                return f"COALESCE({', '.join(rec(x) for x in a)})"
            if f == "CALCULATE" and a:
                if any(self._is_time_intel(x) for x in a[1:]):
                    raise DaxError("time intelligence has no single-query SQL; use the metric (mf query) or a window")
                ctx, lifted = self._calculate(pm, a[1:], preds, tile)
                if lifted and tile is not None:  # removes a filter this query applies: evaluate it on its own
                    return self._context_sql(pm, a[0], ctx, depth, tile)
                return rec(a[0], ctx)
        if isinstance(n, DNum):
            return n.value
        if isinstance(n, DUn) and n.op == "-":
            return f"-{rec(n.operand)}"
        if isinstance(n, DBin) and n.op in ("+", "-"):
            terms = [(sign, rec(x), self._never_blank(pm, x)) for sign, x in _terms(n) if not _is_zero(x)]
            return _sum_sql(terms or [(1, "0", True)], self._never_blank(pm, n))
        if isinstance(n, DBin) and n.op == "*":
            return f"({rec(n.left)} {n.op} {rec(n.right)})"
        if isinstance(n, DBin) and n.op == "/":
            return _div_sql(rec(n.left), n.left, rec(n.right))
        raise DaxError(self._reason(n))

    def _never_blank(self, pm: PModel, n: Any, depth: int = 0) -> bool:
        """A literal, or a sum with one: `[M] + 0` is 0 where [M] is BLANK."""
        if _is_literal(n):
            return not (isinstance(n, DCall) and n.name == "BLANK")
        if isinstance(n, DBin) and n.op in ("+", "-"):
            return any(self._never_blank(pm, x, depth) for _, x in _terms(n))
        ref = self._measure_ref(pm, n)
        if ref is not None and depth < 8:
            try:
                return self._never_blank(pm, dax_parse(ref.expression), depth + 1)
            except DaxError:
                return False
        return False

    # ------------------------------------------------------------ filter context in tiles

    def _col_key(self, pm: PModel, n: DCol) -> tuple[str, str]:
        table, col = self._col_node(pm, n, None)
        return table, col.lower()

    def _expanded(self, pm: PModel, table: str) -> set[str]:
        """DAX's expanded table: the table plus every table it reaches through many-to-one relationships.
        Removing filters from a table (ALL(Sales)) removes them from all of these."""
        t = pm.table(table)
        if t is None:
            raise DaxError(f"unknown table {table}")
        out = [t.name]
        for cur in out:
            out += [r.to_table for r in pm.relationships if r.many_to_one and r.from_table == cur and r.to_table not in out]
        return set(out)

    def _removal(self, pm: PModel, call: DCall) -> tuple[Callable[[tuple[str, str]], bool], frozenset[str]]:
        """ALL / REMOVEFILTERS / ALLEXCEPT / ALLSELECTED -> (which columns lose their filters, which kinds of filter)."""
        kinds = SELECTED_KINDS if call.name == "ALLSELECTED" else ALL_KINDS
        a = call.args
        if not a:
            return (lambda c: True), kinds
        if call.name == "ALLEXCEPT" and len(a) >= 2 and isinstance(a[0], DTable) and all(isinstance(x, DCol) for x in a[1:]):
            exp = self._expanded(pm, a[0].name)
            keep = {self._col_key(pm, x) for x in a[1:]}
            return (lambda c: c[0] in exp and c not in keep), kinds
        if call.name != "ALLEXCEPT" and len(a) == 1 and isinstance(a[0], DTable):
            exp = self._expanded(pm, a[0].name)
            return (lambda c: c[0] in exp), kinds
        if call.name != "ALLEXCEPT" and all(isinstance(x, DCol) for x in a):
            cols = {self._col_key(pm, x) for x in a}
            return (lambda c: c in cols), kinds
        raise DaxError(f"{call.name} with these arguments")

    def _auto_all(self, pm: PModel, cols: frozenset[tuple[str, str]], kinds: frozenset[str]) -> list[tuple[Callable[[tuple[str, str]], bool], frozenset[str]]]:
        """A filter on the date column of a date table (marked as one, or the date key of a relationship) also
        removes the filters on the rest of that table: CALCULATE(.., 'Date'[Date] <= x) ignores a Year grouping."""
        out = []
        for table, name in cols:
            t = pm.table(table)
            col = t.column(name) if t else None
            if col is None:
                continue
            marked = t.data_category.lower() == "time" and col.is_key
            date_key = col.data_type == "datetime" and any(r.active and r.to_table == t.name and r.to_column.lower() == name for r in pm.relationships)
            if marked or date_key:
                exp = self._expanded(pm, t.name)
                out.append(((lambda c, exp=exp: c[0] in exp), kinds))
        return out

    def _calculate(self, pm: PModel, args: list[Any], preds: list[_Pred], tile: _TileCtx | None) -> tuple[list[_Pred], bool]:
        """CALCULATE's filter arguments applied to the context `preds`: (new context, whether it removes a filter
        the current query enforces). Column filters replace the filters on their columns (KEEPFILTERS adds)."""
        removals: list[tuple[Callable[[tuple[str, str]], bool], frozenset[str]]] = []
        added: list[_Pred] = []
        lifted = False
        for x in args:
            keep = isinstance(x, DCall) and x.name == "KEEPFILTERS" and len(x.args) == 1
            if keep:
                x = x.args[0]
            if isinstance(x, DCall) and x.name in REMOVERS:
                if keep:
                    raise DaxError(f"KEEPFILTERS({x.name}(..))")
                removals.append(self._removal(pm, x))
            elif isinstance(x, DCall) and x.name == "FILTER" and len(x.args) == 2 and isinstance(x.args[0], DCall) and x.args[0].name in ("ALL", "ALLSELECTED") and x.args[0].args:
                if keep:
                    raise DaxError("KEEPFILTERS(FILTER(..))")
                over = x.args[0]
                matcher, kinds = self._removal(pm, over)
                table, cols = self._filter_table(pm, over)
                removals += [(matcher, kinds), *self._auto_all(pm, cols, kinds)]
                added.append(self._filter_pred(pm, x.args[1], table, cols, preds, tile))
                lifted = True  # its MAX()/MIN() compare to the outer row, so it can't sit in the outer query's own aggregates
            else:
                self.filter_jinja(pm, x)  # a plain column predicate, else DaxError
                r = _Recorder()
                self.row_sql(pm, x, None, r)
                cols = frozenset(r.cols)
                if not keep:
                    removals += [((lambda c, cols=cols: c in cols), ALL_KINDS), *self._auto_all(pm, cols, ALL_KINDS)]
                added.append(_Pred(lambda q, x=x: self.row_sql(pm, x, None, q), cols, "calc", False))
        kept: list[_Pred] = []
        for p in preds:
            hit: set[str] = set()
            for matcher, kinds in removals:
                if any(matcher(c) for c in p.cols):
                    hit |= kinds
            if p.kind in hit:
                lifted = lifted or p.enforced
                continue
            if hit and p.kind == "calc":
                raise DaxError("ALLSELECTED over a column that an enclosing CALCULATE filters")
            kept.append(p)
        return kept + added, lifted

    def _filter_table(self, pm: PModel, over: DCall) -> tuple[str, frozenset[tuple[str, str]]]:
        """The table and columns of FILTER(ALL(..), ..)'s first argument."""
        if len(over.args) == 1 and isinstance(over.args[0], DTable):
            t = pm.table(over.args[0].name)
            if t is None:
                raise DaxError(f"unknown table {over.args[0].name}")
            return t.name, frozenset((t.name, c.name.lower()) for c in t.columns)
        cols = frozenset(self._col_key(pm, x) for x in over.args if isinstance(x, DCol))
        tables = {t for t, _ in cols}
        if len(cols) != len(over.args) or len(tables) != 1:
            raise DaxError(f"FILTER over {over.name} of several tables")
        return tables.pop(), cols

    def _filter_pred(self, pm: PModel, cond: Any, table: str, cols: frozenset[tuple[str, str]], preds: list[_Pred], tile: _TileCtx | None) -> _Pred:
        """FILTER(ALL(T[c]), <row condition>) as a filter on T[c]. MAX(T[c]) / MIN(T[c]) in the condition are
        evaluated in the outer filter context (FILTER's arguments are), as scalar subqueries."""

        def walk(n: Any) -> Any:
            if isinstance(n, DCall) and n.name in ("MAX", "MIN") and len(n.args) == 1 and isinstance(n.args[0], DCol):
                return DRaw(self._scalar(pm, n.name, n.args[0], preds, tile))
            if isinstance(n, DCol):
                if self._col_key(pm, DCol(n.table or table, n.name)) not in cols:
                    raise DaxError(f"FILTER condition reads {n.table or table}[{n.name}], which the filtered table doesn't have")
                return DCol(n.table or table, n.name)
            if isinstance(n, DCall):
                return DCall(n.name, [walk(x) for x in n.args])
            if isinstance(n, DBin):
                return DBin(n.op, walk(n.left), walk(n.right))
            if isinstance(n, DUn):
                return DUn(n.op, walk(n.operand))
            if isinstance(n, DSet):
                return DSet([walk(x) for x in n.items])
            if isinstance(n, DTable):
                raise DaxError(f"FILTER condition with table {n.name}")
            return n

        row = walk(cond)
        self.row_sql(pm, row, table, _Recorder())  # raises if the condition is not plain row SQL
        return _Pred(lambda q: self.row_sql(pm, row, table, q), cols, "calc", False)

    def _scalar(self, pm: PModel, fn: str, n: DCol, preds: list[_Pred], tile: _TileCtx | None) -> str:
        """MAX/MIN of a column in the filter context `preds`: only filters on the column's table and the tables
        it reaches filter it (relationships filter from the one side to the many side)."""
        table, col = self._col_node(pm, n, None)
        if tile is None:
            return "NULL"  # recording which tables a tile reads: this subquery reads its own
        tile.need()
        exp = self._expanded(pm, table)
        use = []
        for p in preds:
            tables = {c[0] for c in p.cols}
            if tables <= exp:
                use.append(p)
            elif tables & exp:
                raise DaxError(f"a filter reads {table} and other tables")
        if table == tile.base:
            sq = tile.sub()
            frm = tile.from_sql(sq)
        else:
            if any({c[0] for c in p.cols} - {table} for p in use):
                raise DaxError(f"{fn}({table}[{col}]) under filters on tables {table} relates to")
            if (pm.id, table) not in self.phys:
                raise DaxError(f"table {table} has no SQL source")
            sq = tile.sub([table])
            frm = f"FROM {self.phys[(pm.id, table)]} AS {sq.aliases[table]}"
        where = [p.sql(sq) for p in use]
        return f"(SELECT {fn}({self.column_sql(pm, table, col, sq)}) {frm}" + (f" WHERE {' AND '.join(where)}" if where else "") + ")"

    def _context_sql(self, pm: PModel, inner: Any, ctx: list[_Pred], depth: int, tile: _TileCtx) -> str:
        """CALCULATE that lifts a filter the query applies: a correlated subquery over the tile's tables with
        the filters that are left (the visual's grouping as equalities to the outer row)."""
        tile.need()
        sq = tile.sub()
        sub_ctx = [replace(p, enforced=True) for p in ctx]
        body = self.agg_sql(pm, inner, sq, sub_ctx, depth + 1, tile)
        where = [p.sql(sq) for p in sub_ctx]
        return f"(SELECT {body} {tile.from_sql(sq)}" + (f" WHERE {' AND '.join(where)}" if where else "") + ")"

    def _iter_values(self, pm: PModel, n: DCall, q: Any, preds: list[_Pred], depth: int, tile: _TileCtx | None) -> str:
        """SUMX/AVERAGEX/MINX/MAXX(VALUES(T[c]), <measure>): the measure per value of T[c] in the context, then
        aggregated; BLANKs are skipped, so values of T[c] without data drop out as they do in SQL."""
        vals, body = n.args
        table, col = self._col_node(pm, vals.args[0], None)
        used = self._functions_deep(pm, body) & (REMOVERS | {"CALCULATE", "CALCULATETABLE", "FILTER", "KEEPFILTERS", "VALUES", "USERELATIONSHIP"} | TIME_INTEL)
        if used:
            raise DaxError(f"{n.name}(VALUES(..)) over an expression that uses {', '.join(sorted(used))}")
        if self._never_blank(pm, body):
            raise DaxError(f"{n.name}(VALUES(..)) over an expression that is never BLANK: values of {table}[{col}] without rows count too")
        if tile is None:
            self.column_sql(pm, table, col, q)  # the tile joins T
            return self.agg_sql(pm, body, q, preds, depth + 1, None)
        tile.need()
        sq = tile.sub()
        sub_ctx = [replace(p, enforced=True) for p in preds]
        key = self.column_sql(pm, table, col, sq)
        value = self.agg_sql(pm, body, sq, sub_ctx, depth + 1, tile)
        where = [p.sql(sq) for p in sub_ctx]
        return (f"(SELECT {SQL_AGG[DAX_ITER[n.name]]}(_v) FROM (SELECT {value} AS _v {tile.from_sql(sq)}"
                + (f" WHERE {' AND '.join(where)}" if where else "") + f" GROUP BY {key}) AS _values_{tile.n})")

    def _functions_deep(self, pm: PModel, n: Any, depth: int = 0) -> set[str]:
        """The DAX functions an expression uses, through the measures it references."""
        out: set[str] = set()

        def walk(x: Any) -> None:
            ref = self._measure_ref(pm, x)
            if ref is not None:
                if depth > 8:
                    raise DaxError("measure nesting too deep")
                out.update(self._functions_deep(pm, dax_parse(ref.expression), depth + 1))
            elif isinstance(x, DCall):
                out.add(x.name)
                for y in x.args:
                    walk(y)
            elif isinstance(x, DBin):
                walk(x.left)
                walk(x.right)
            elif isinstance(x, DUn):
                walk(x.operand)
            elif isinstance(x, DSet):
                for y in x.items:
                    walk(y)

        walk(n)
        return out

    # ------------------------------------------------------------ roles -> row filters / permissions

    def _roles(self, pm: PModel) -> None:
        if not pm.roles:
            return
        by_table: dict[str, list[str]] = {}
        for r in pm.roles:
            for table in r.filters:
                by_table.setdefault(table, []).append(r.name)
        self.b.warn(
            f"{pm.name} roles",
            "Power BI RLS: a viewer in no role sees nothing, and a viewer in several roles sees the union of their rows. "
            "unshackle row filters apply to their groups only and are ANDed; review policies/row_filters.powerbi.yml", "info",
        )
        for r in pm.roles:
            src = f"{pm.source} role: {r.name}"
            groups = [m for m, _ in r.members] or [r.name]
            no_members = "" if r.members else (
                f"role '{r.name}' has no members in the model (they are assigned in the Power BI service, Security page); "
                "set `groups` to the IdP group(s) assigned there"
            )
            for table, dax in r.filters.items():
                t = pm.table(table)
                phys = self.phys.get((pm.id, t.name)) if t else None
                todo = [no_members] if no_members else []
                if len(by_table.get(table, [])) > 1:
                    todo.append(f"roles {', '.join(by_table[table])} all filter {table}; Power BI unions them, write one predicate with OR if a user can be in several")
                predicate = ""
                try:
                    if phys is None:
                        raise DaxError(f"table {table} has no SQL source")
                    predicate = self.row_sql(pm, dax_parse(dax), t.name, _SameTable(t.name))  # type: ignore[union-attr]
                except DaxError as exc:
                    todo.insert(0, f"RLS DAX not translated: {exc}")
                self.b.row_filters.append(
                    RowFilter(_sn(f"{r.name}_{table}"), phys or _sn(table), predicate, list(groups), source_expression=f"tablePermission {table} = {dax}",
                              source_ref=src, todo="; ".join(x for x in todo if x))
                )
            principals = [f"{'user' if kind == 'user' or (kind == 'auto' and '@' in m) else 'group'}:{m}" for m, kind in r.members] or [f"group:{r.name}"]
            if r.members:
                for p in principals:
                    self.b.permissions.append(
                        Permission(p, "query", "Datasource", self.b.datasource, source_expression=f"role {r.name} modelPermission: {r.permission}",
                                   source_ref=src, todo=f"Power BI role membership grants read on the whole semantic model {pm.name}; narrow to its tables if the datasource holds more")
                    )
            for table, col, perm in r.ols:
                t = pm.table(table)
                phys = self.phys.get((pm.id, t.name), snake(table)) if t else snake(table)
                c = t.column(col) if (t and col) else None
                rtype = "Column" if col else "Table"
                rid = f"{self.b.datasource}.{phys}" + (f".{self._physical_column(pm, t.name, c) if c and t else col}" if col else "")
                for p in principals:
                    self.b.permissions.append(
                        Permission(p, "query", rtype, rid, effect="forbid",  # type: ignore[arg-type]
                                   source_expression=f"role {r.name} OLS {table}{'[' + col + ']' if col else ''} metadataPermission: {perm}",
                                   source_ref=src, todo="" if r.members else f"map role '{r.name}' to the IdP group assigned in the Power BI service")
                    )

    # ------------------------------------------------------------ Copilot + knowledge

    def _copilot(self, pm: PModel) -> None:
        for rel, text in sorted(pm.copilot.items()):
            low = rel.lower()
            if low.endswith("instructions.md") and text.strip():
                self.b.knowledge.append(KnowledgeItem(f"{pm.name} Copilot instructions", text.strip(), f"AI instructions written for Copilot on {pm.name}",
                                                      "always", self.b.datasource, f"{pm.source}/{rel}"))
            elif low.endswith("exampleprompts.json"):
                data = _json_str(text, [])
                items = data.get("examplePrompts", data.get("prompts", [])) if isinstance(data, dict) else data
                prompts = [p if isinstance(p, str) else (p.get("prompt") or p.get("text") or "") for p in items or []]
                if any(prompts):
                    body = "Questions users ask about this model:\n\n" + "\n".join(f"- {p}" for p in prompts if p)
                    self.b.knowledge.append(KnowledgeItem(f"{pm.name} example questions", body, "Copilot example prompts", "reference", self.b.datasource, f"{pm.source}/{rel}"))
            elif "verifiedanswers" in low and low.endswith(".json") and "version.json" not in low:
                self._verified_answer(pm, rel, _json_str(text, {}))

    def _verified_answer(self, pm: PModel, rel: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        name = data.get("displayName") or data.get("name") or data.get("title") or PurePosixPath(rel).parent.name
        phrases: list[str] = []
        for key in ("triggerPhrases", "phrases", "questions", "triggers"):
            for p in data.get(key) or []:
                phrases.append(p if isinstance(p, str) else str(p.get("text") or p.get("phrase") or ""))
        visual = None
        stack = [data]
        while stack and visual is None:
            cur = stack.pop()
            if isinstance(cur, dict):
                if "queryState" in cur or "visualType" in cur:
                    visual = cur
                    break
                stack += list(cur.values())
            elif isinstance(cur, list):
                stack += cur
        sql, todo = "", ""
        if visual is not None:
            v = visual_from_pbir({"name": name, "visual": visual if "visualType" in visual else {"visualType": "tableEx", "query": visual}}, rel)
            tile = self._tile(pm, v, [], [])
            sql, todo = tile.sql if isinstance(tile, Tile) else "", tile.todo if isinstance(tile, Tile) else ""
        if not sql:
            todo = todo or "Copilot verified answer: rebuild the answer's visual as SQL (fields are in the source JSON)"
            sql = "-- " + json.dumps(data, default=str)[:2000]
        self.b.queries.append(VerifiedQuery(name=str(name), sql=sql, description=f"Copilot verified answer on {pm.name}",
                                            datasource=self.b.datasource, aliases=[p for p in phrases if p], source_ref=f"{pm.source}/{rel}", todo=todo))

    def _knowledge(self, pm: PModel) -> None:
        lines = [f"Power BI semantic model `{pm.name}` (id `{pm.id}`).", "", "## Tables", ""]
        for t in pm.tables.values():
            ms = self.msrc.get((pm.id, t.name))
            where = self.phys.get((pm.id, t.name), "not modelled")
            mode = (t.partitions[0].mode if t.partitions else "") or "import"
            conn = f" via {ms.connector} ({ms.server}/{ms.database})" if ms and ms.connector else ""
            lines.append(f"- `{t.name}` -> `{where}` ({mode}{conn})" + (f": {t.description}" if t.description else ""))
            if t.is_calc_group:
                lines += [f"  - calculation item `{n}`: `{' '.join(e.split())}`" for n, e in t.calc_items]
        if pm.relationships:
            lines += ["", "## Relationships", ""]
            for r in pm.relationships:
                flags = ", ".join(x for x in ("inactive" if not r.active else "", "many-to-many" if r.to_many else "",
                                              r.cross_filter if r.cross_filter.lower() == "bothdirections" else "") if x)
                lines.append(f"- `{r.from_table}[{r.from_column}]` -> `{r.to_table}[{r.to_column}]`" + (f" ({flags})" if flags else ""))
        if pm.roles:
            lines += ["", "## Roles (RLS)", ""]
            lines += [f"- `{r.name}`: " + ("; ".join(f"`{t}`: `{d}`" for t, d in r.filters.items()) or "no table filters") for r in pm.roles]
        self.b.knowledge.append(KnowledgeItem(f"Power BI model {pm.name}", "\n".join(lines), f"Tables, sources and relationships of the Power BI semantic model {pm.name}",
                                              datasource=self.b.datasource, source_ref=pm.source))

    # ------------------------------------------------------------ reports

    def _report(self, rid: str) -> None:
        meta = self.reports.get(rid, {})
        root = f"reports/{rid}"
        name = meta.get("name") or rid
        mid = meta.get("datasetId", "")
        if not mid and self.store.exists(f"{root}/definition.pbir"):
            pbir = self.store.get_json(f"{root}/definition.pbir")
            m = re.search(r"semanticmodelid=([0-9a-fA-F-]{36})", json.dumps(pbir), re.I)
            mid = m.group(1) if m else ""
            by_path = ((pbir.get("datasetReference") or {}).get("byPath") or {}).get("path", "")
            if not mid and by_path:  # a .pbip project: "../Sales.SemanticModel" (imported as models/Sales)
                mid = PurePosixPath(by_path.replace("\\", "/")).name.removesuffix(".SemanticModel")
            conn = ((pbir.get("datasetReference") or {}).get("byConnection") or {}).get("connectionString") or ""
            catalog = re.search(r"initial catalog=([^;]+)", conn, re.I)
            if not mid and catalog:  # a thin report on a published model; the model may have been imported next to it
                want = catalog.group(1).strip().strip('"').lower()
                mid = next((k for k, v in self.models.items() if want in (k.lower(), v.name.lower())), "")
        pm = self.models.get(mid)
        pages: list[VPage] = []
        report_filters: list[dict[str, Any]] = []
        if self.store.glob(f"{root}/definition/pages/*/page.json"):
            order = []
            if self.store.exists(f"{root}/definition/pages/pages.json"):
                order = self.store.get_json(f"{root}/definition/pages/pages.json").get("pageOrder") or []
            if self.store.exists(f"{root}/definition/report.json"):
                report_filters = list((self.store.get_json(f"{root}/definition/report.json").get("filterConfig") or {}).get("filters") or [])
            folders = [r.split("/")[-2] for r in self.store.glob(f"{root}/definition/pages/*/page.json")]
            for folder in sorted(folders, key=lambda f: (order.index(f) if f in order else len(order), f)):
                pj = self.store.get_json(f"{root}/definition/pages/{folder}/page.json")
                page = VPage(pj.get("name") or folder, pj.get("displayName") or folder,
                             filters=list((pj.get("filterConfig") or {}).get("filters") or []),
                             hidden=pj.get("visibility") == "HiddenInViewMode")
                for vrel in self.store.glob(f"{root}/definition/pages/{folder}/visuals/*/visual.json"):
                    page.visuals.append(visual_from_pbir(self.store.get_json(vrel), vrel))
                pages.append(page)
        elif self.store.exists(f"{root}/report.json"):
            rj = self.store.get_json(f"{root}/report.json")
            report_filters = list(_json_str(rj.get("filters"), []) or [])
            for s in sorted(rj.get("sections") or [], key=lambda s: s.get("ordinal", 0)):
                page = VPage(s.get("name", ""), s.get("displayName") or s.get("name", ""), filters=list(_json_str(s.get("filters"), []) or []))
                for i, vc in enumerate(s.get("visualContainers") or []):
                    page.visuals.append(visual_from_legacy(vc, f"{root}/report.json section {s.get('name')}", i))
                pages.append(page)
        else:
            self.b.warn(f"report:{rid} ({name})", "no PBIR pages or report.json in the definition")
            return
        base_url = meta.get("webUrl", "")
        u = self.usage.get(rid, {})
        for page in pages:
            self._page(rid, name, meta, pm, mid, page, report_filters, base_url, u)

    def _page(self, rid, rname, meta, pm, mid, page: VPage, report_filters, base_url, usage) -> None:
        item = f"report {rname} page {page.display}"
        if page.hidden:
            self.b.warn(item, "page is hidden in view mode (tooltip/drillthrough/helper page); migrated anyway", "info")
        filters: list[DashboardFilter] = []
        slicers: list[tuple[str, VField, str]] = []
        tiles: list[Tile] = []
        positions: list[tuple[str, float, float, float]] = []
        used_params: set[str] = set()
        for v in page.visuals:
            if v.type != "slicer":
                continue
            fld = next((f for fs in v.roles.values() for f in fs), None)
            if fld is None or fld.kind not in ("column", "hier"):
                continue
            if any((f.entity, f.prop, f.hierarchy, f.level) == (fld.entity, fld.prop, fld.hierarchy, fld.level) for _, f, _ in slicers):
                continue  # the same field sliced twice on a page (e.g. a slicer panel shown by a bookmark): one parameter
            pcol = self._pcol(pm, fld) if pm else None
            param = _sn(fld.prop or fld.level)
            while param in used_params:
                param += "_2"
            used_params.add(param)
            if pcol is not None and pcol.data_type == "datetime":
                filters += [DashboardFilter(f"{param}_from", param, None, "date"), DashboardFilter(f"{param}_to", param, None, "date")]
                slicers.append((param, fld, "date"))
            else:
                filters.append(DashboardFilter(param, param, None, "value"))
                slicers.append((param, fld, "value"))
        for v in page.visuals:
            if v.type == "slicer" or v.type in DECORATION:
                continue
            if pm is None:
                tile = Tile(v.name, v.title or v.type, "unknown", "unknown", source_type=v.type,
                            todo=f"semantic model {mid or '?'} was not extracted (see _errors.json); rebuild this visual by hand")
            else:
                tile = self._tile(pm, v, slicers, report_filters + page.filters)
            tiles.append(tile)
            positions.append((tile.id, v.x, v.y, v.h))
        slug = slugify(f"{rname} {page.display}")
        self.b.dashboards.append(
            Dashboard(
                name=f"{rname} - {page.display}",
                slug=slug,
                description=meta.get("description") or f"Power BI report {rname}, page {page.display}",
                tiles=tiles,
                layout=layout_rows(positions),
                filters=filters,
                datasource=self.b.datasource,
                source_ref=f"powerbi report {rid} page {page.name}",
                url=f"{base_url}/{page.name}" if base_url else "",
                views=usage.get("views") if usage else None,
            )
        )

    def _pcol(self, pm: PModel, f: VField) -> PColumn | None:
        t = pm.table(f.entity)
        if t is None:
            return None
        if f.kind == "hier" and not f.variation:
            col = t.hierarchies.get(f.hierarchy, {}).get(f.level, f.level)
            return t.column(col)
        return t.column(f.prop)

    def _field_sql(self, pm: PModel, f: VField, q: Any, tile: _TileCtx | None = None) -> str:
        t = pm.table(f.entity)
        if t is None:
            raise DaxError(f"unknown table {f.entity}")
        if f.kind == "measure":
            m = pm.measure(f.prop)
            kpi = re.fullmatch(r"_(.+) (Status|Goal|Trend)", f.prop)
            if m is None and kpi and pm.measure(kpi.group(1)):
                raise DaxError(f"{f.prop} is the KPI {kpi.group(2).lower()} of measure [{kpi.group(1)}] (a kpi expression in the model); "
                               "compute it in the tile from that measure")
            if m is None:
                raise DaxError(f"unknown measure {f.prop} (report-level measure? see reportExtensions.json)")
            try:
                return self.agg_sql(pm, dax_parse(m.expression), q, tile.preds if tile else [], 0, tile)
            except DaxError as exc:
                raise DaxError(f"measure [{m.name}]: {exc}") from exc
        if f.kind == "hier":
            col = self._pcol(pm, f)
            if col is None:
                raise DaxError(f"unknown hierarchy level {f.entity}.{f.hierarchy}.{f.level}")
            expr = self.column_sql(pm, t.name, col.name, q)
            if f.variation:
                grain = f.level.lower()
                if grain not in ("year", "quarter", "month", "day"):
                    raise DaxError(f"date hierarchy level {f.level}")
                return f"DATE_TRUNC('{grain}', {expr})"
            return expr
        expr = self.column_sql(pm, t.name, f.prop, q)
        if f.kind == "agg":
            fn = AGG_FUNCTION.get(f.func or 0, "")
            if not fn:
                raise DaxError(f"aggregation function {f.func}")
            sql = f"COUNT(DISTINCT {expr})" if fn == "COUNT_DISTINCT" else f"{fn}({expr})"
            return tile.hoist(sql, q) if tile else sql
        return expr

    def _from_lines(self, pm: PModel, base: str, joins: dict[str, PRel], q: Any) -> list[str]:
        if not isinstance(q, _Qualified):
            return [f"FROM {self.phys[(pm.id, base)]}"]
        lines = [f"FROM {self.phys[(pm.id, base)]} AS {q.aliases[base]}"]
        for t, rel in joins.items():
            fk = self.column_sql(pm, base, rel.from_column, q)
            pk = self.column_sql(pm, t, rel.to_column, q)
            lines.append(f"LEFT JOIN {self.phys[(pm.id, t)]} AS {q.aliases[t]} ON {fk} = {pk}")
        return lines

    def _alias_name(self, f: VField, used: set[str]) -> str:
        if f.kind == "hier":
            base = f"{_sn(f.prop)}_{_sn(f.level)}" if f.variation else _sn(f.level)
        else:
            base = _mname(f.prop) if f.kind == "measure" else _sn(f.prop)
        name, i = base, 2
        while name in used:
            name, i = f"{base}_{i}", i + 1
        used.add(name)
        return name

    def _cond_sql(self, pm: PModel, cond: dict[str, Any], aliases: dict[str, str], q: Any) -> str:
        if "In" in cond:
            c = cond["In"]
            exprs = c.get("Expressions") or []
            if len(exprs) != 1:
                raise DaxError("multi-column IN filter")
            col = self._filter_field(pm, exprs[0], aliases, q)
            vals = [self._literal(row[0]) for row in c.get("Values") or [] if row]
            if not vals:
                raise DaxError("IN filter without literal values (probably a table reference)")
            return f"{col} IN ({', '.join(vals)})"
        if "Not" in cond:
            return f"NOT ({self._cond_sql(pm, cond['Not'].get('Expression') or {}, aliases, q)})"
        if "And" in cond or "Or" in cond:
            key = "And" if "And" in cond else "Or"
            c = cond[key]
            return f"({self._cond_sql(pm, c['Left'], aliases, q)} {key.upper()} {self._cond_sql(pm, c['Right'], aliases, q)})"
        if "Comparison" in cond:
            c = cond["Comparison"]
            op = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}.get(int(c.get("ComparisonKind", -1)))
            if op is None:
                raise DaxError(f"comparison kind {c.get('ComparisonKind')}")
            return f"{self._filter_field(pm, c.get('Left') or {}, aliases, q)} {op} {self._literal(c.get('Right') or {})}"
        raise DaxError(f"filter condition {', '.join(cond)}")

    def _filter_field(self, pm: PModel, expr: dict[str, Any], aliases: dict[str, str], q: Any) -> str:
        f = norm_field(expr, aliases)
        if f is None or f.kind not in ("column", "hier"):
            raise DaxError("filter on a measure or expression (HAVING); add it by hand")
        return self._field_sql(pm, f, q)

    @staticmethod
    def _literal(d: dict[str, Any]) -> str:
        v = str(((d or {}).get("Literal") or {}).get("Value", ""))
        if v.startswith("'"):
            return _sql_str(v[1:-1].replace("''", "'"))
        if v.startswith("datetime'"):
            return "TIMESTAMP '" + v[9:-1].replace("T", " ") + "'"
        if re.fullmatch(r"-?\d+(\.\d+)?[LDM]?", v):
            return v.rstrip("LDM")
        if v in ("true", "false", "null"):
            return v.upper()
        raise DaxError(f"literal {v!r}")

    def _vfilters(self, pm: PModel, filters: list[dict[str, Any]], scope: str) -> tuple[list[tuple[str, Callable[[Any], str]]], list[str]]:
        preds, problems = [], []
        for f in filters:
            flt = f.get("filter")
            if not flt:
                continue
            label = f"{scope} filter {f.get('name', '')}".strip()
            ftype = str(f.get("type", "Categorical"))
            if ftype not in ("Categorical", "Advanced", "Basic"):
                problems.append(f"{label} ({ftype}) not translated")
                continue
            aliases = {x.get("Name", ""): x.get("Entity", "") for x in flt.get("From") or []}
            conds = [w.get("Condition") or {} for w in flt.get("Where") or []]
            fn = lambda q, conds=conds, aliases=aliases: " AND ".join(self._cond_sql(pm, c, aliases, q) for c in conds)  # noqa: E731
            try:
                fn(_Recorder())
            except DaxError as exc:
                problems.append(f"{label}: {exc}")
                continue
            preds.append((label, fn))
        return preds, problems

    def _calc_group_items(self, pm: PModel, f: dict[str, Any]) -> list[str] | None:
        """None if the filter is not on a calculation group; else the picked items that change values
        (an item that is just SELECTEDMEASURE() only sets a format string)."""
        flt = f.get("filter") or {}
        groups = [pm.table(x.get("Entity", "")) for x in flt.get("From") or []]
        groups = [t for t in groups if t is not None and t.is_calc_group]
        if not groups:
            return None
        exprs = dict(groups[0].calc_items)
        picked = [self._literal(row[0]).strip("'") for w in flt.get("Where") or []
                  for row in ((w.get("Condition") or {}).get("In") or {}).get("Values") or [] if row]
        if not picked or len(flt.get("Where") or []) != 1 or "Not" in json.dumps(flt.get("Where")):
            return [f"of {groups[0].name}"]
        identity = lambda e: re.sub(r"\s+", "", e or "").upper() == "SELECTEDMEASURE()"  # noqa: E731
        return [f"{groups[0].name}[{p}]" for p in picked if not identity(exprs.get(p))]

    def _tile(self, pm: PModel, v: VVisual, slicers: list[tuple[str, VField, str]], scope_filters: list[dict[str, Any]]) -> Tile:
        if v.type == "textbox":
            return Tile(v.name, v.title, "text", "text", text=v.text, source_type=v.type)
        kind, mark, orient = VISUALS.get(v.type, ("unknown", "unknown", ""))
        todos: list[str] = []
        if kind == "unknown":
            todos.append(f"Power BI visual {v.type!r} has no Altair equivalent here; pick a chart (SQL is ready)")
        if "Combo" in v.type:
            todos.append("combo chart (columns + line): split the line series into its own tile")
        if v.type == "pivotTable" and v.roles.get("Columns"):
            todos.append("matrix with column headers: pivot in SQL or with df.pivot_table in the tile cell")
        dims: list[tuple[str, VField]] = []
        vals: list[tuple[str, VField]] = []
        roles_of: dict[str, list[str]] = {}
        used: set[str] = set()
        for role in list(DIM_ROLES) + [r for r in v.roles if r not in DIM_ROLES and r != "Tooltips"]:
            for f in v.roles.get(role) or []:
                alias = self._alias_name(f, used)
                roles_of.setdefault(role, []).append(alias)
                (vals if f.kind in ("measure", "agg") else dims).append((alias, f))
        if not dims and not vals:
            todos.append("visual has no fields in its query")
        # SQL
        plain, items = [], []
        for f in scope_filters + v.filters:
            group = self._calc_group_items(pm, f)
            if group is None:
                plain.append(f)
            else:
                items += group
        preds, problems = self._vfilters(pm, plain, "visual/page")
        todos += problems
        sql = ""
        try:
            if items:  # a calculation item rewrites every measure in the visual
                raise DaxError(f"a filter picks calculation item(s) {', '.join(items)}, which change the measures; "
                               "apply the item's DAX to each measure (see the model's knowledge file)")
            sql = self._tile_sql(pm, dims, vals, preds, slicers, todos)
        except DaxError as exc:
            todos.append(f"no SQL: {exc}")
        # encodings
        enc: dict[str, Encoding] = {}
        ftype = lambda f: "quantitative" if f.kind in ("measure", "agg") else ("temporal" if f.kind == "hier" and f.variation or (self._pcol(pm, f) and self._pcol(pm, f).data_type == "datetime") else "nominal")  # noqa: E731
        by_alias = dict(dims + vals)

        def e(alias: str) -> Encoding:
            f = by_alias[alias]
            return Encoding(alias, ftype(f), title=f.label or (f.prop or f.level))  # type: ignore[arg-type]

        cats = roles_of.get("Category", []) + roles_of.get("Axis", []) + roles_of.get("Location", []) + roles_of.get("Rows", [])
        series = roles_of.get("Series", []) + roles_of.get("Legend", [])
        measures = [a for a, f in vals if a not in roles_of.get("X", []) and a not in roles_of.get("Size", [])] if v.type == "scatterChart" else [a for a, _ in vals]
        if kind == "kpi":
            if measures:
                enc["text"] = e(measures[0])
        elif mark == "arc":
            if measures:
                enc["theta"] = e(measures[0])
            if cats or series:
                enc["color"] = e((cats or series)[0])
        elif mark == "point":
            xs, ys = roles_of.get("X", []), roles_of.get("Y", [])
            if xs:
                enc["x"] = e(xs[0])
            if ys:
                enc["y"] = e(ys[0])
            if roles_of.get("Size"):
                enc["size"] = e(roles_of["Size"][0])
            if series:
                enc["color"] = e(series[0])
            if cats:
                enc["detail"] = e(cats[0])
        elif kind == "chart":
            cat_ch, val_ch = ("y", "x") if orient == "h" else ("x", "y")
            if cats:
                enc[cat_ch] = e(cats[0])
            if measures:
                enc[val_ch] = e(measures[0])
            if series:
                enc["color"] = e(series[0])
            if len(measures) > 1:
                todos.append(f"visual plots {len(measures)} values ({', '.join(measures)}); only the first is encoded, unpivot in SQL and color by measure, or split tiles")
            if len(cats) > 1:
                todos.append(f"drill hierarchy on {', '.join(cats)}; only the top level is encoded")
        title = v.title or (", ".join(f.label or f.prop for _, f in vals) + (" by " + ", ".join(f.label or f.prop or f.level for _, f in dims) if dims else "") if (dims or vals) else v.name)
        return Tile(
            v.name, title, kind, mark, sql, enc, source_type=v.type,
            source_config={"visualType": v.type, "roles": {r: a for r, a in roles_of.items()}, "position": {"x": v.x, "y": v.y, "width": v.w, "height": v.h}, "source": v.source},
            todo="; ".join(dict.fromkeys(todos)),
        )

    def _tile_sql(self, pm: PModel, dims, vals, preds, slicers, todos: list[str]) -> str:
        mrec = _Recorder()
        for _, f in vals:
            self._field_sql(pm, f, mrec)
        rec = _Recorder()
        for _, f in dims + vals:
            self._field_sql(pm, f, rec)
        candidates = list(dict.fromkeys(mrec.tables or rec.tables))
        if not candidates:
            raise DaxError("no fields")

        def reaches(b: str) -> bool:  # every table is b itself or one active many-to-one hop from it
            return all(t == b or any(r.many_to_one and r.from_table == b and r.to_table == t for r in pm.relationships)
                       for t in mrec.tables + rec.tables)

        # the fact table a measure aggregates, not a dimension its CALCULATE filters happened to touch first
        base = next((b for b in candidates if reaches(b)), candidates[0])
        if (pm.id, base) not in self.phys:
            raise DaxError(f"table {base} has no SQL source")
        joins: dict[str, PRel] = {}

        def joinable(table: str) -> bool:
            if table == base or table in joins:
                return True
            rel = next((r for r in pm.relationships if r.many_to_one and r.from_table == base and r.to_table == table), None)
            if rel is None or (pm.id, table) not in self.phys:
                return False
            joins[table] = rel
            return True

        for t in mrec.tables + rec.tables:
            if not joinable(t):
                raise DaxError(f"fields span {base} and {t} without a direct many-to-one relationship from {base}; write the join by hand")
        where: list[Callable[[Any], str]] = []
        for label, fn in preds:
            r = _Recorder()
            fn(r)
            if all(joinable(t) for t in r.tables):
                where.append(fn)
            else:
                todos.append(f"{label} is on a table not related to {base}; add it by hand")
        for param, f, kind in slicers:
            r = _Recorder()
            try:
                self._field_sql(pm, f, r)
            except DaxError:
                continue
            if not all(joinable(t) for t in r.tables):
                continue
            if kind == "date":
                where.append(lambda q, f=f, p=param: f"({self._field_sql(pm, f, q)} >= CAST(:{p}_from AS DATE) OR :{p}_from IS NULL) AND ({self._field_sql(pm, f, q)} < CAST(:{p}_to AS DATE) OR :{p}_to IS NULL)")
            else:
                where.append(lambda q, f=f, p=param: f"({self._field_sql(pm, f, q)} = :{p} OR :{p} IS NULL)")
        tables = [base] + list(joins)
        q: Any = _Recorder() if len(tables) == 1 else _Qualified({t: _alias(t) for t in tables})
        tile = _TileCtx(self, pm, base, joins, q)
        for fn in where:
            r = _Recorder()
            fn(r)
            tile.preds.append(_Pred(fn, frozenset(r.cols), "where"))
        for a, f in dims:
            r = _Recorder()
            self._field_sql(pm, f, r)
            tile.preds.append(_Pred(lambda sq, f=f, a=a: f"{self._field_sql(pm, f, sq)} IS NOT DISTINCT FROM {tile.outer(a)}", frozenset(r.cols), "group"))
        values = [(a, self._field_sql(pm, f, q, tile)) for a, f in vals]
        if tile.used:  # a measure changes the filter context: group in g, then the subqueries per row of g
            tile.grouped, tile.n = True, 0
            values = [(a, self._field_sql(pm, f, q, tile)) for a, f in vals]
            inner = [f"{self._field_sql(pm, f, q)} AS {a}" for a, f in dims] + [f"{sql} AS {name}" for sql, name in tile.leaves.items()]
            sql = self._select(pm, base, joins, q, inner or ["COUNT(*) AS _rows"], [fn(q) for fn in where], len(dims))
            outer = ["SELECT", "  " + ",\n  ".join([f"g.{a} AS {a}" for a, _ in dims] + [f"{v} AS {a}" for a, v in values]),
                     "FROM (", textwrap.indent(sql, "  "), ") AS g"]
            if dims:
                outer.append("ORDER BY 1")
            return "\n".join(outer)
        select = [f"{self._field_sql(pm, f, q)} AS {a}" for a, f in dims] + [f"{v} AS {a}" for a, v in values]
        lines = self._select(pm, base, joins, q, select, [fn(q) for fn in where], len(dims) if vals else 0).split("\n")
        if dims:
            if not vals:
                lines[0] = "SELECT DISTINCT"
            lines.append("ORDER BY 1")
        return "\n".join(lines)

    def _select(self, pm: PModel, base: str, joins: dict[str, PRel], q: Any, select: list[str], conds: list[str], group: int) -> str:
        lines = ["SELECT", "  " + ",\n  ".join(select), *self._from_lines(pm, base, joins, q)]
        if conds:
            lines.append("WHERE " + "\n  AND ".join(conds))
        if group:
            lines.append("GROUP BY " + ", ".join(str(i) for i in range(1, group + 1)))
        return "\n".join(lines)
