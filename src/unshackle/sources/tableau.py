"""Tableau source: workbooks (.twb XML), published datasources (.tds), usage, Metadata API.

Raw layout under migration/raw/tableau/:

    _site.json               {"server", "site_id", "content_url", "api_version"}
    workbooks.json           GET /api/{v}/sites/{site}/workbooks (all pages)
    views.json               GET .../views?includeUsageStatistics=true (usage.totalViewCount)
    datasources.json         GET .../datasources (published datasources)
    groups.json              GET .../groups (names for permission grantees)
    workbooks/<id>.twb       GET .../workbooks/{id}/content?includeExtract=false (.twbx unzipped)
    datasources/<id>.tds     GET .../datasources/{id}/content?includeExtract=false (.tdsx unzipped)
    metadata/<id>.json       Metadata API (POST /api/metadata/graphql): calculated fields,
                             upstream tables and custom SQL for the workbook
    views/<id>.csv           GET .../views/{id}/data for the top-N most viewed views (parity)
    permissions/<id>.json    GET .../workbooks/{id}/permissions
    _errors.json             [{"item", "error"}] anything extract could not fetch

parse() reads the XML only (never the network). Deterministic: tables and columns
from connection relations + metadata-records, joins -> relationships, plain aggregates,
row-level calcs (IF/CASE/ZN/IFNULL/DATETRUNC/arithmetic), ratios of aggregates, FIXED
LODs -> SQL models at their grain, user filters -> row filters, worksheets -> tiles with
generated SQL (FIXED, INCLUDE and EXCLUDE LODs included), dashboards -> notebooks with
layout. Table calcs, parameters, sets, groups, bins, blending, ATTR and the LODs the
tiles can't place keep their Tableau formula with a TODO(unshackle) for the agent
(skills/source-tableau, skills/translate-metrics).
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    Layer,
    Measure,
    Metric,
    Permission,
    Reference,
    RowFilter,
    SemanticModel,
    Table,
    Tile,
    VerifiedQuery,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources import _aggexpr as ax
from unshackle.sources import _dialect as dialect
from unshackle.sources import _tableau_analytics as ta
from unshackle.sources import _tableau_calc as tc
from unshackle.sources._semyaml import grid_rows
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source

LANG = "tableau"

# Tableau aggregate -> (MetricFlow agg, value arg index, condition arg index), for _aggexpr.classify
TABLEAU_AGGS = {
    "sum": ("sum", 0, None),
    "avg": ("average", 0, None),
    "count": ("count", 0, None),
    "countd": ("count_distinct", 0, None),
    "min": ("min", 0, None),
    "max": ("max", 0, None),
    "median": ("median", 0, None),
}
ax.AGGS.setdefault(LANG, TABLEAU_AGGS)
OTHER_AGGS = {"attr", "stdev", "stdevp", "var", "varp", "percentile", "collect", "corr", "covar", "covarp"}
AGG_SQL = {
    "sum": "sum({})",
    "average": "avg({})",
    "count": "count({})",
    "count_distinct": "count(distinct {})",
    "min": "min({})",
    "max": "max({})",
    "median": "median({})",
}
DEFAULT_AGG = {"sum": "sum", "avg": "average", "count": "count", "countd": "count_distinct", "min": "min", "max": "max", "median": "median"}

# shelf derivations: [sum:Sales:qk], [tmn:Order Date:qk], [yr:Order Date:ok], [usr:Calculation_1:qk]
DERIV_AGG = {"sum": "sum", "avg": "average", "cnt": "count", "ctd": "count_distinct", "min": "min", "max": "max", "med": "median"}
DERIV_LABEL = {"sum": "{}", "avg": "Avg. {}", "cnt": "Count of {}", "ctd": "Distinct count of {}", "min": "Min. {}", "max": "Max. {}", "med": "Median {}"}
TRUNC = {"tyr": "year", "tqr": "quarter", "tmn": "month", "twk": "week", "tdy": "day", "thr": "hour", "tmi": "minute", "my": "month", "mdy": "day", "qy": "quarter"}
PART = {"yr": "year", "qr": "quarter", "mn": "month", "wk": "week", "dy": "day", "hr": "hour", "mi": "minute", "wd": "dow"}
UNIT_LABEL = {"year": "Year", "quarter": "Quarter", "month": "Month", "week": "Week", "day": "Day", "hour": "Hour", "minute": "Minute", "dow": "Weekday"}
ALTAIR_UNIT = {"year": "year", "quarter": "yearquarter", "month": "yearmonth", "week": "yearweek", "day": "yearmonthdate", "hour": "yearmonthdatehours", "minute": "yearmonthdatehoursminutes"}
KNOWN_DERIV = set(DERIV_AGG) | set(TRUNC) | set(PART) | {"none", "usr", "attr", "std", "stp", "var", "vrp", "sum"}
MARKS = {"bar": ("chart", "bar"), "line": ("chart", "line"), "area": ("chart", "area"), "circle": ("chart", "point"), "shape": ("chart", "point"), "pie": ("chart", "arc"), "ganttbar": ("chart", "bar")}
MAP_MARKS = {"multipolygon", "polygon", "map", "filledmap", "density"}
FILE_CONNECTIONS = {"excel-direct", "textscan", "hyper", "dataengine", "ogrdirect", "stat-direct", "google-sheets", "excel", "msaccess"}
COLUMN_TYPE = {"integer": "integer", "real": "number", "string": "string", "date": "date", "datetime": "timestamp", "boolean": "boolean"}
NUMERIC = {"integer", "real"}
TIME_TYPES = {"date", "datetime"}
CAPABILITY_ACTION = {"Read": "view", "Write": "edit"}
INTERNAL_FIELDS = {"Number of Records", "__tableau_internal_object_id__", ":Measure Names", "Multiple Values", ":Measure Values"}

BRACKETED = re.compile(r"\[((?:[^\]]|\]\])*)\]")
DS_REF = re.compile(r"\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]")
PARAM_REF = re.compile(r"\[Parameters\]\.\[((?:[^\]]|\]\])+)\]")
PARAM_ARG = "\x01"  # a workbook parameter in a parsed formula: [\x01<its local name>]
LOD = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.I)
LOD_PARTS = re.compile(r"\s*\{\s*(FIXED|INCLUDE|EXCLUDE)\s+((?:\[(?:[^\]]|\]\])+\]\s*,?\s*)+):(.+)\}\s*", re.S | re.I)
LOD_REF = "__unshackle_lod_"
TABLE_CALC = re.compile(
    r"\b(WINDOW_\w+|RUNNING_\w+|RANK\w*|LOOKUP|INDEX|TOTAL|SIZE|FIRST|LAST|PREVIOUS_VALUE|SCRIPT_\w+|MODEL_\w+)\s*\(", re.I
)
USER_FN = re.compile(r"\b(USERNAME|ISMEMBEROF|USERDOMAIN|FULLNAME|ISUSERNAME|ISFULLNAME|USERATTRIBUTE\w*)\s*\(", re.I)
ATTR_FN = re.compile(r"\bATTR\s*\(", re.I)
DATE_LIT = re.compile(r"#(\d{4}-\d{2}-\d{2})( \d{2}:\d{2}:\d{2})?#")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# connection attributes that change what the warehouse returns, and how the migration carries them
CONNECTION_SETTINGS = {
    "one-time-sql": "Initial SQL (Tableau runs it on every connection it opens)",
    "odbc-connect-string-extras": "extra connection options",
}
INITIAL_SQL_VARS = re.compile(r"\[(TableauServerUser(?:Full)?|TableauApp|TableauVersion|WorkbookName)\]")
NODE_LIMIT = "NODE_LIMIT_EXCEEDED"
SKILL = "skills/source-tableau"

GQL_WORKBOOKS = """
query unshackleWorkbooks($luids: [String], $first: Int, $after: String) {
  workbooksConnection(filter: {luidWithin: $luids}, first: $first, after: $after) {
    nodes {
      luid
      name
      projectName
      description
      updatedAt
      upstreamTables { name schema fullName connectionType database { name connectionType } }
      upstreamDatasources { luid name }
      embeddedDatasources {
        name
        hasExtracts
        upstreamTables { name schema fullName database { name connectionType } }
        fields {
          __typename
          name
          description
          isHidden
          ... on CalculatedField { formula role dataType }
          ... on ColumnField { role dataType }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

GQL_CUSTOM_SQL = """
query unshackleCustomSql($first: Int, $after: String) {
  customSQLTablesConnection(first: $first, after: $after) {
    nodes { id name query isUnsupportedCustomSql database { name connectionType } downstreamWorkbooks { luid } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class TableauSource(Source):
    name = "tableau"
    label = "Tableau"
    env_vars = ("TABLEAU_SERVER_URL", "TABLEAU_SITE", "TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET", "TABLEAU_API_VERSION")
    docs = "https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api.htm"
    manual_files = "Tableau workbooks (.twb, .twbx) and data sources (.tds, .tdsx), or folders of them"
    manual_gaps = (
        "usage: no view counts, so nothing is sorted by popularity (REST views list)",
        "permissions: who may view each workbook (and group names) is not in the files, so no Cedar permits; user filters in the workbook are kept",
        "names: workbooks are named after their file, without project, owner, description or links to the site",
        "lineage: no Metadata API (upstream tables, custom SQL lineage)",
        "parity: no view data saved; download a view's data as CSV and point a parity case at it",
    )

    def __init__(
        self,
        client: Client | None = None,
        server_url: str | None = None,
        site: str | None = None,
        api_version: str | None = None,
        top_views: int = 20,
        page_size: int = 100,
        metadata_batch: int = 10,
    ):
        self._client = client
        self._server = server_url
        self._site = site
        self._version = api_version
        self.top_views = top_views
        self.page_size = page_size
        self.metadata_batch = metadata_batch
        self._token: str | None = None
        self.site_id = ""
        self.site_content_url = ""

    # ------------------------------------------------------------ network

    def server(self) -> str:
        if self._server:
            return self._server.rstrip("/")
        if self._client is not None and not env("TABLEAU_SERVER_URL", required=False):
            return self._client.base_url
        return env("TABLEAU_SERVER_URL").rstrip("/")

    def site(self) -> str:
        return self._site if self._site is not None else env("TABLEAU_SITE", required=False)

    def version(self) -> str:
        return self._version or env("TABLEAU_API_VERSION", required=False, default="3.24") or "3.24"

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.server(), headers={"Accept": "application/json"}, per_minute=120)
        if self._token is None:
            self._signin()
        return self._client

    def _signin(self) -> None:
        assert self._client is not None
        self._client.set_header("Accept", "application/json")
        body = {
            "credentials": {
                "personalAccessTokenName": env("TABLEAU_PAT_NAME"),
                "personalAccessTokenSecret": env("TABLEAU_PAT_SECRET"),
                "site": {"contentUrl": self.site()},
            }
        }
        data = self._client.post_json(f"/api/{self.version()}/auth/signin", json=body)
        creds = data.get("credentials") or {}
        token = creds.get("token")
        if not token:
            raise SourceError("Tableau sign-in returned no token")
        self._token = token
        self.site_id = (creds.get("site") or {}).get("id", "")
        self.site_content_url = (creds.get("site") or {}).get("contentUrl", self.site())
        self._client.set_header("X-Tableau-Auth", token)

    def _signout(self) -> None:
        if self._token is None or self._client is None:
            return
        try:
            self._client.post(f"/api/{self.version()}/auth/signout")
        except (SourceError, OSError):
            pass
        self._token = None

    def _api(self, path: str) -> str:
        return f"/api/{self.version()}/sites/{self.site_id}{path}"

    def _paged(self, path: str, key: str, item: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.client().get_json(self._api(path), params={**params, "pageSize": self.page_size, "pageNumber": page})
            items = (data.get(key) or {}).get(item) or []
            if isinstance(items, dict):
                items = [items]
            out += items
            pag = data.get("pagination") or {}
            total = int(pag.get("totalAvailable") or 0)
            size = int(pag.get("pageSize") or self.page_size)
            if not items or page * size >= total:
                return out
            page += 1

    def _list(self, path: str, key: str, item: str, errors: list[dict[str, str]], **params: Any) -> list[dict[str, Any]]:
        try:
            return self._paged(path, key, item, **params)
        except SourceError as exc:
            errors.append({"item": f"list {key}", "error": str(exc)[:500]})
            return []

    def _content(self, path: str, ext: str) -> bytes:
        resp = self.client().get(self._api(path), params={"includeExtract": "false"}, headers={"Accept": "*/*"})
        return unpack(resp.content, ext)

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        errors: list[dict[str, str]] = []
        workbooks = self._list("/workbooks", "workbooks", "workbook", errors)
        views = self._list("/views", "views", "view", errors, includeUsageStatistics="true")
        datasources = self._list("/datasources", "datasources", "datasource", errors)
        wb_names = {w["id"]: w.get("name", "") for w in workbooks}
        totals: dict[str, int] = {}
        for v in views:
            wid = (v.get("workbook") or {}).get("id", "")
            totals[wid] = totals.get(wid, 0) + _views(v)
        for w in workbooks:
            inv.add(
                "workbook", f"workbook:{w['id']}", w.get("name", w["id"]), parent=(w.get("project") or {}).get("name", ""),
                views=totals.get(w["id"], 0), url=w.get("webpageUrl", ""),
                extra={"updated_at": w.get("updatedAt", ""), "owner": (w.get("owner") or {}).get("id", "")},
            )
        for v in views:
            wid = (v.get("workbook") or {}).get("id", "")
            inv.add("view", f"view:{v['id']}", v.get("name", v["id"]), parent=wb_names.get(wid, wid), views=_views(v),
                    url=self._view_url(v.get("contentUrl", "")))
        for d in datasources:
            inv.add("datasource", f"datasource:{d['id']}", d.get("name", d["id"]), parent=(d.get("project") or {}).get("name", ""),
                    extra={"type": d.get("type", ""), "has_extracts": d.get("hasExtracts", False)})
        inv.notes.append("Views are totalViewCount from the REST API (lifetime); workbook views are the sum of their views.")
        inv.notes.append(
            "Extract also calls the Metadata API; on Tableau Server enable it with `tsm maintenance metadata-services enable`."
        )
        inv.notes += [f"{e['item']}: {e['error'][:200]}" for e in errors]
        self._signout()
        return inv

    def _view_url(self, content_url: str) -> str:
        if not content_url:
            return ""
        site = f"site/{self.site_content_url}/" if self.site_content_url else ""
        return f"{self.server()}/#/{site}views/{content_url.replace('/sheets/', '/')}"

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        try:
            self._extract(store, only, errors)
        finally:
            store.put_json("_errors.json", errors)
            self._signout()

    def _extract(self, store: RawStore, only: set[str] | None, errors: list[dict[str, str]]) -> None:
        self.client()
        store.put_json("_site.json", {"server": self.server(), "site_id": self.site_id, "content_url": self.site_content_url, "api_version": self.version()})
        workbooks = self._list("/workbooks", "workbooks", "workbook", errors)
        views = self._list("/views", "views", "view", errors, includeUsageStatistics="true")
        datasources = self._list("/datasources", "datasources", "datasource", errors)
        groups = self._list("/groups", "groups", "group", errors)
        store.put_json("workbooks.json", workbooks)
        store.put_json("views.json", views)
        store.put_json("datasources.json", datasources)
        store.put_json("groups.json", groups)

        ids = {item.partition(":")[2] or item for item in only} if only else None
        view_wb = {v["id"]: (v.get("workbook") or {}).get("id", "") for v in views}
        wanted_wb = {w["id"] for w in workbooks if ids is None or w["id"] in ids or any(view_wb.get(i) == w["id"] for i in ids)}
        extracted: list[str] = []
        proxies: set[str] = set()
        for w in workbooks:
            if w["id"] not in wanted_wb:
                continue
            try:
                twb = self._content(f"/workbooks/{w['id']}/content", ".twb")
                store.put_bytes(f"workbooks/{w['id']}.twb", twb)
                extracted.append(w["id"])
                proxies |= set(re.findall(rb"class='sqlproxy'[^>]*?dbname='([^']+)'", twb)) | set(re.findall(rb"dbname='([^']+)'[^>]*?class='sqlproxy'", twb))
            except (SourceError, zipfile.BadZipFile) as exc:
                errors.append({"item": f"workbook {w.get('name', w['id'])} ({w['id']})", "error": f"download: {str(exc)[:400]}"})
                continue
            try:
                store.put_json(f"permissions/{w['id']}.json", self.client().get_json(self._api(f"/workbooks/{w['id']}/permissions")))
            except SourceError as exc:
                errors.append({"item": f"workbook {w['id']} permissions", "error": str(exc)[:400]})
        proxy_names = {p.decode("utf-8", "replace") for p in proxies}
        for d in datasources:
            if ids is not None and d["id"] not in ids and d.get("contentUrl") not in proxy_names:
                continue
            try:
                store.put_bytes(f"datasources/{d['id']}.tds", self._content(f"/datasources/{d['id']}/content", ".tds"))
            except (SourceError, zipfile.BadZipFile) as exc:
                errors.append({"item": f"datasource {d.get('name', d['id'])} ({d['id']})", "error": f"download: {str(exc)[:400]}"})
        if extracted:
            self._metadata(store, extracted, errors)
        ranked = sorted((v for v in views if view_wb.get(v["id"]) in set(extracted)), key=lambda v: (-_views(v), v.get("name", "")))
        for v in ranked[: self.top_views]:
            try:
                # Tableau answers 406 to Accept: text/csv; the CSV comes back with */*.
                resp = self.client().get(self._api(f"/views/{v['id']}/data"), headers={"Accept": "*/*"})
                store.put_bytes(f"views/{v['id']}.csv", resp.content)
            except SourceError as exc:
                errors.append({"item": f"view {v.get('name', v['id'])} ({v['id']}) data", "error": str(exc)[:400]})

    # ------------------------------------------------------------ Metadata API

    def _graphql(self, query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        resp = self.client().post_json("/api/metadata/graphql", json={"query": query, "variables": variables})
        errs = resp.get("errors") or []
        limited = any(NODE_LIMIT in str(e) for e in errs)
        hard = [e for e in errs if NODE_LIMIT not in str(e) and (e.get("extensions") or {}).get("severity") != "WARNING"]
        if hard:
            raise SourceError("Metadata API: " + "; ".join(str(e.get("message", e)) for e in hard)[:400])
        return resp.get("data") or {}, limited

    def _workbook_nodes(self, batch: list[str]) -> tuple[list[dict[str, Any]], bool]:
        nodes: list[dict[str, Any]] = []
        after = None
        while True:
            data, limited = self._graphql(GQL_WORKBOOKS, {"luids": batch, "first": max(len(batch), 1), "after": after})
            conn = data.get("workbooksConnection") or {}
            nodes += conn.get("nodes") or []
            page = conn.get("pageInfo") or {}
            if limited or not page.get("hasNextPage") or not page.get("endCursor"):
                return nodes, limited
            after = page["endCursor"]

    def _metadata(self, store: RawStore, luids: list[str], errors: list[dict[str, str]]) -> None:
        found: dict[str, dict[str, Any]] = {}
        size = max(self.metadata_batch, 1)
        queue = [luids[i:i + size] for i in range(0, len(luids), size)]
        try:
            while queue:
                batch = queue.pop(0)
                nodes, limited = self._workbook_nodes(batch)
                if limited and len(batch) > 1:
                    half = len(batch) // 2
                    queue[:0] = [batch[:half], batch[half:]]
                    continue
                if limited:
                    errors.append({"item": f"metadata {batch[0]}", "error": f"{NODE_LIMIT} even for one workbook; metadata is partial"})
                for n in nodes:
                    if n.get("luid"):
                        found[n["luid"]] = n
            sql_tables: list[dict[str, Any]] = []
            after = None
            while True:
                data, _ = self._graphql(GQL_CUSTOM_SQL, {"first": 100, "after": after})
                conn = data.get("customSQLTablesConnection") or {}
                sql_tables += conn.get("nodes") or []
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage") or not page.get("endCursor"):
                    break
                after = page["endCursor"]
        except SourceError as exc:
            errors.append({"item": "metadata api", "error": f"{str(exc)[:300]} (Server: `tsm maintenance metadata-services enable`)"})
            sql_tables = []
        else:
            for luid in luids:
                if luid not in found:
                    errors.append({"item": f"metadata {luid}", "error": "workbook not in the Metadata API yet (new and changed content is "
                                   "indexed with a delay; on Server the metadata services may be off): no lineage; re-run extract later"})
        for t in sql_tables:
            for wb in t.get("downstreamWorkbooks") or []:
                if wb.get("luid") in found:
                    found[wb["luid"]].setdefault("customSQLTables", []).append({k: t.get(k) for k in ("id", "name", "query", "database")})
        for luid, node in found.items():
            store.put_json(f"metadata/{luid}.json", node)

    def import_path(self, path: Path) -> dict[str, bytes]:
        kinds = {".twb": ("workbooks", ".twb"), ".twbx": ("workbooks", ".twb"), ".tds": ("datasources", ".tds"), ".tdsx": ("datasources", ".tds")}
        out: dict[str, bytes] = {}
        for _, p in _manual.files_of(path):
            kind = kinds.get(p.suffix.lower())
            if kind is None:
                if path.is_file():
                    raise SourceError(f"{p.name}: Tableau import accepts {self.manual_files}")
                continue
            try:
                out[f"{kind[0]}/{p.stem}{kind[1]}"] = unpack(p.read_bytes(), kind[1])
            except zipfile.BadZipFile as exc:
                raise SourceError(f"{p.name}: not a valid packaged file ({exc})") from exc
        return out

    # ------------------------------------------------------------ parse

    def parse(self, store: RawStore) -> Bundle:
        return _Parser(store).run()


# ==================================================================== helpers


def readable(formula: str, fields: dict[str, "TField"], params: dict[str, "TField"] | None = None) -> str:
    """Replace internal names ([Calculation_123], [Parameters].[Parameter 1]) with captions."""

    def param(m: re.Match) -> str:
        p = (params or {}).get(_unesc(m.group(1)))
        return f"[Parameters].[{p.label}]" if p is not None else m.group(0)

    def sub(m: re.Match) -> str:
        f = fields.get(_unesc(m.group(1)))
        return f"[{f.label}]" if f is not None and f.caption and f.caption != f.local else m.group(0)

    out = PARAM_REF.sub(param, formula)
    return re.sub(r"(?<!\]\.)\[((?:[^\]]|\]\])+)\](?!\.)", sub, out)


def _views(v: dict[str, Any]) -> int:
    try:
        return int((v.get("usage") or {}).get("totalViewCount") or 0)
    except (TypeError, ValueError):
        return 0


def unpack(data: bytes, ext: str) -> bytes:
    """.twbx / .tdsx are zips; return the .twb / .tds inside. Plain XML passes through."""
    if data[:2] != b"PK":
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted((n for n in zf.namelist() if n.lower().endswith(ext)), key=lambda n: (n.count("/"), n))
        if not names:
            raise SourceError(f"packaged file has no {ext} inside")
        return zf.read(names[0])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _unbracket(s: str | None) -> str:
    s = (s or "").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return s.replace("]]", "]")


def _esc(s: str) -> str:
    return s.replace("]", "]]")


def _qualify(sql: str, from_sql: str) -> str:
    """A bare column name qualified with the FROM clause's (first) table, so it can't be read as an output column."""
    if not from_sql or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|"(?:[^"]|"")+"', sql):
        return sql
    m = re.match(r"(\S+)(?:\s+as\s+(\S+))?", from_sql.split("\n")[0].strip())
    return f"{(m.group(2) or m.group(1).rsplit('.', 1)[-1]) if m else ''}.{sql}".lstrip(".")


def _unesc(s: str) -> str:
    return s.replace("]]", "]")


def _name_parts(s: str) -> list[str]:
    found = BRACKETED.findall(s or "")
    if found:
        return [_unesc(p) for p in found]
    return [p.strip('"`') for p in (s or "").split(".") if p]


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    runs = [r.text or "" for r in el.iter() if _local(r.tag) == "run"]
    text = "".join(runs) if runs else "".join(el.itertext())
    text = text.replace("Æ", "").replace("\r", "")
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _name(s: str) -> str:
    n = snake(_fold((s or "").replace("%", " pct")))
    if n == "untitled" and s:
        n = snake(s) if snake(s) != "untitled" else "field"
    if n[0].isdigit():
        n = f"f_{n}"
    return n


def _ident(name: str) -> str:
    return name if IDENT_RE.match(name) else '"' + name.replace('"', '""') + '"'


def _paren(sql: str) -> str:
    return sql if re.fullmatch(r'[\w."]+', sql) else f"({sql})"


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _param_value(raw: str | None, datatype: str) -> Any:
    v = (raw or "").strip()
    if not v:
        return None
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('""', '"')
    if v.startswith("#") and v.endswith("#"):
        return v[1:-1].split(" ")[0] if datatype == "date" else v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v) if datatype == "integer" or re.fullmatch(r"-?\d+", v) else float(v)
    except ValueError:
        return v


def _param_unesc(v: str) -> str:
    return re.sub(r"\\(.)", r"\1", v)


def _param_domain(p: "TField") -> str:
    """A parameter's allowed values, as the parameters cell's comment: its list (with aliases) or range."""
    members, aliases = p.extra.get("members") or [], p.extra.get("aliases") or {}
    if p.extra.get("domain") == "list" and members:
        shown = [f"{_param_value(m, p.datatype)!r}" + (f" ({aliases[m]})" if aliases.get(m) else "") for m in members]
        return "one of " + ", ".join(shown)
    rng = p.extra.get("range") or {}
    lo, hi = (_param_value(rng.get(k), p.datatype) for k in ("min", "max"))
    if p.extra.get("domain") != "range" or (lo is None and hi is None):
        return ""
    out = f"from {lo!r} to {hi!r}" if lo is not None and hi is not None else f"at least {lo!r}" if lo is not None else f"at most {hi!r}"
    step = rng.get("granularity")
    if step:
        out += f", step {_param_value(step, 'real' if '.' in step else 'integer')!r}" + (f" {rng['period-type-v2']}" if rng.get("period-type-v2") else "")
    return out


def param_bind(p: "TField") -> str:
    """A workbook parameter in SQL: the bind :name, set in the notebook's parameters cell (dates come as text, so cast)."""
    bind = ":" + _name(p.label)
    return f"cast({bind} as {'date' if p.datatype == 'date' else 'timestamp'})" if p.datatype in TIME_TYPES else bind


def _member_sql(v: str | None) -> str | None:
    """Tableau member literal ("West", 2024, #2024-01-01#, %null%) -> SQL literal; None for null."""
    if v is None or v == "%null%":
        return None
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return "'" + v[1:-1].replace('""', '"').replace("'", "''") + "'"
    if v.startswith("#") and v.endswith("#"):
        return f"'{v[1:-1]}'"
    if re.fullmatch(r"-?\d+(?:\.\d+)?", v) or v.lower() in ("true", "false"):
        return v.lower() if v.lower() in ("true", "false") else v
    return "'" + v.replace("'", "''") + "'"


SORT_TAGS = {"computed-sort": "computed", "alphabetic-sort": "alphabetic", "manual-sort": "manual", "natural-sort": "natural"}


@dataclass
class SheetSort:
    """How a sheet orders one dimension's members (Sort in Tableau): `computed` by an aggregate (`using`, a shelf
    instance), `alphabetic` by the members' labels, `manual` in the listed order (`members`, Tableau member literals),
    `natural` in the data source's order."""

    column: str  # shelf instance of the sorted dimension, e.g. "none:paper_type:nk"
    kind: str
    direction: str = "ASC"
    using: str = ""
    members: list[str] = field(default_factory=list)


def sheet_sorts(view: ET.Element | None, defaults: list[ET.Element] | None = None) -> dict[str, SheetSort]:
    """A sheet's sorts by shelf instance: its own (<computed-sort>, <alphabetic-sort>, <manual-sort>, <natural-sort> in
    <view>), else its datasource's Default Properties > Sort (`defaults`, <default-sorts>)."""
    out: dict[str, SheetSort] = {}
    own = [el for el in (view if view is not None else []) if el.tag in SORT_TAGS]
    for el in [*own, *(defaults or [])]:
        if el.tag not in SORT_TAGS:
            continue
        refs = DS_REF.findall(el.get("column", ""))
        inst = _unesc(refs[0][1]) if refs else _unbracket(el.get("column"))
        if not inst or inst in out:
            continue
        using = DS_REF.findall(el.get("using", ""))
        out[inst] = SheetSort(inst, SORT_TAGS[el.tag], (el.get("direction") or "ASC").upper(),
                              using=_unesc(using[0][1]) if using else _unbracket(el.get("using")),
                              members=[(b.text or "") for b in el.findall("dictionary/bucket")])
    return out


# Tableau number formats (`default-format`, a sheet's text-format): a type letter (n number, c currency, p percentage)
# and an Excel-style pattern, positive[;negative]. Only patterns d3-format writes exactly are converted.
_NUMBER_PATTERN = re.compile(r'(?P<pre>"\$"|\$)?(?P<group>#,##)?0(?:\.(?P<dec>0+))?(?P<pct>%)?')


def d3_format(fmt: str) -> str:
    """A Tableau number format as a d3-format specifier (what Altair/Vega-Lite axes and tooltips take), or "" when
    d3-format has no exact form for it (scaled units like K/M, literal text, locale formats, dates)."""
    if not fmt or fmt[0] not in "ncp":
        return ""
    pos, _, neg = fmt[1:].partition(";")
    m = _NUMBER_PATTERN.fullmatch(pos)
    if m is None or bool(m.group("pct")) != (fmt[0] == "p") or (m.group("pre") and fmt[0] == "p"):
        return ""
    sign = ""
    if neg:
        if neg == f"({pos})":
            sign = "("
        elif neg != f"-{pos}":
            return ""
    return f"{sign}{'$' if m.group('pre') else ''}{',' if m.group('group') else ''}.{len(m.group('dec') or '')}{'%' if m.group('pct') else 'f'}"


def alias_sql(expr: str, f: TField) -> str | None:
    """`expr` shown with a field's aliases (Tableau's labels for its members); None when its type has no exact form.
    Members without an alias, and NULL unless it has one, keep their value; booleans show as True / False."""
    if f.datatype == "string":
        whens = []
        for key, label in f.aliases:
            lit = _member_sql(key)
            whens.append(f"when {expr} is null then {_sql_str(label)}" if lit is None else f"when {expr} = {lit} then {_sql_str(label)}")
        return f"case {' '.join(whens)} else {expr} end"
    if f.datatype == "boolean":
        labels = {k.lower(): v for k, v in f.aliases}
        null = f" when {expr} is null then {_sql_str(labels['%null%'])}" if "%null%" in labels else ""
        return (f"case when {expr} then {_sql_str(labels.get('true', 'True'))} when not {expr} then "
                f"{_sql_str(labels.get('false', 'False'))}{null} end")
    return None


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def scan_formula(formula: str) -> tuple[str, str, str, list[str]]:
    """-> (formula without // comments, code with strings blanked, code with strings and [fields] blanked, comments)."""
    clean: list[str] = []
    code: list[str] = []
    bare: list[str] = []
    comments: list[str] = []
    i, n = 0, len(formula)
    quote = ""
    bracket = False
    while i < n:
        ch = formula[i]
        if quote:
            clean.append(ch)
            blank = ch if ch == quote else " "
            code.append(blank)
            bare.append(blank)
            if ch == quote:
                if i + 1 < n and formula[i + 1] == quote:
                    clean.append(ch)
                    code.append(" ")
                    bare.append(" ")
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if bracket:
            clean.append(ch)
            code.append(ch)
            bare.append(ch if ch == "]" else " ")
            if ch == "]":
                if i + 1 < n and formula[i + 1] == "]":
                    clean.append("]")
                    code.append("]")
                    bare.append(" ")
                    i += 2
                    continue
                bracket = False
            i += 1
            continue
        if formula.startswith("//", i):
            j = formula.find("\n", i)
            j = n if j < 0 else j
            comments.append(formula[i + 2:j].strip())
            i = j
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "[":
            bracket = True
        clean.append(ch)
        code.append(ch)
        bare.append(ch)
        i += 1
    return "".join(clean).strip(), "".join(code), "".join(bare), [c for c in comments if c]


def _nested_fixed(text: str) -> tuple[str, str, str] | None:
    """{FIXED [a] : AGG({FIXED [b] : ...})} -> (agg, the inner FIXED's text, its body); None for any other nesting."""
    m = LOD_PARTS.fullmatch(text)
    if m is None or m.group(1).upper() != "FIXED":
        return None
    body = re.fullmatch(r"\s*(\w+)\s*\((.*)\)\s*", m.group(3), re.S)
    if body is None or body.group(1).lower() not in TABLEAU_AGGS or TABLEAU_AGGS[body.group(1).lower()][2] is not None:
        return None
    inner = body.group(2).strip()
    spans = lod_spans(inner)
    im = LOD_PARTS.fullmatch(inner)
    if spans != [(0, len(inner), False)] or im is None or im.group(1).upper() != "FIXED":
        return None
    return body.group(1).lower(), inner, im.group(3)


def lod_spans(clean: str) -> list[tuple[int, int, bool]]:
    """Top-level { ... } expressions in a comment-free formula: (start, end, has a nested { }). Braces in
    strings and field names don't count."""
    _, _, bare, _ = scan_formula(clean)
    bare = bare.ljust(len(clean))
    out: list[tuple[int, int, bool]] = []
    depth, start, nested = 0, 0, False
    for i, ch in enumerate(bare):
        if ch == "{":
            if depth == 0:
                start, nested = i, False
            else:
                nested = True
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                out.append((start, i + 1, nested))
    return out


class _TabParser(ax._Parser):
    """_aggexpr's parser plus Tableau IF .. THEN .. ELSEIF .. ELSE .. END and CASE .. WHEN .. END."""

    def _is(self, word: str) -> bool:
        return self.peek()[1].lower() == word

    def _word(self, word: str) -> None:
        _, v = self.take()
        if v.lower() != word:
            raise ax.FormulaError(f"expected {word.upper()}, got {v or 'end of formula'!r}")

    def compare(self) -> ax.Node:
        left = super().compare()
        if self._is("in"):  # [x] IN (a, b, ...)
            self.take()
            if self.peek()[1] != "(":
                raise ax.FormulaError("IN takes a list of values in parentheses (IN with a set is not translated)")
            left = ax.Call("in", [left, *self.call_args()])
        return left

    def primary(self) -> ax.Node:
        kind, value = self.peek()
        if kind == "kw" and value == "if":
            self.take()
            args: list[ax.Node] = []
            cond = self.expr()
            self._word("then")
            args += [cond, self.expr()]
            while self._is("elseif"):
                self.take()
                cond = self.expr()
                self._word("then")
                args += [cond, self.expr()]
            if self._is("else"):
                self.take()
                args.append(self.expr())
            self._word("end")
            return ax.Call("if", args)
        if kind == "ident" and value.lower() == "case":
            self.take()
            subject = None if self._is("when") else self.expr()
            args = []
            while self._is("when"):
                self.take()
                when = self.expr()
                self._word("then")
                args += [ax.BinOp("=", subject, when) if subject is not None else when, self.expr()]
            if not args:
                raise ax.FormulaError("CASE without WHEN")
            if self._is("else"):
                self.take()
                args.append(self.expr())
            self._word("end")
            return ax.Call("if", args)
        if kind == "ident" and value.lower() == "null" and self.peek(1)[1] != "(":
            self.take()
            return ax.Num("null")  # the NULL literal (renders as SQL null)
        return super().primary()


def parse_formula(text: str, dates: "_DateOpts | None" = None) -> ax.Node:
    """`dates`: the datasource's Date Properties, which DATETRUNC/DATEPART/DATENAME of weeks, quarters and years follow."""
    text = DATE_LIT.sub(lambda m: f"DATETIME('{m.group(1)}{m.group(2)}')" if m.group(2) else f"DATE('{m.group(1)}')", text)
    if not text.strip():
        raise ax.FormulaError("empty formula")
    return _normalize(_dates(_TabParser(text).parse(), dates or _DateOpts()))


# --------------------------------------------------------------------------- Date Properties

WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")  # index = extract(dow)
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
# First day of the week by locale region (CLDR), for a datasource with no Week start of its own: Tableau then uses the locale.
SUNDAY_REGIONS = {"US", "CA", "MX", "BR", "JP", "KR", "TW", "HK", "IL", "PH"}
MONDAY_REGIONS = {"GB", "IE", "AU", "NZ", "DE", "AT", "CH", "FR", "BE", "LU", "NL", "ES", "IT", "SE", "NO", "DK", "FI", "PL", "CZ", "RU"}
DATE_UNITS = ("week", "weekday", "quarter", "year")


@dataclass(frozen=True)
class _DateOpts:
    """A datasource's Date Properties as they apply to date functions and shelf dates."""

    week_start: str = ""  # sunday..saturday; "" not known (no Week start, no locale)
    fiscal: int = 1  # first month of the fiscal year
    calendar: str = ""  # "" or Gregorian; anything else (ISO-8601 week-based) is not translated
    language: str = ""  # of the locale (day names); "" not known


def _locale_week_start(locale: str) -> str:
    region = re.split(r"[-_]", locale)[1].upper() if re.match(r"^[A-Za-z]{2,3}[-_][A-Za-z]{2}\b", locale) else ""
    return "sunday" if region in SUNDAY_REGIONS else "monday" if region in MONDAY_REGIONS else ""


def date_opts(ds: "TDatasource | None") -> _DateOpts:
    """The Date Properties a datasource's calculations and sheets use: a workbook on a published datasource uses the published
    one's (its own copy under <overridable-settings> is only the fallback when that wasn't extracted); with no Week start, the
    locale's first day of the week."""
    chain = [d for d in (ds.origin if ds is not None else None, ds) if d is not None]
    week = next((d.week_start for d in chain if d.week_start in WEEKDAYS), "")
    week = week or next((s for d in chain if (s := _locale_week_start(d.locale))), "")
    fiscal = next((d.fiscal_start for d in chain if d.fiscal_start), "")
    calendar = next((d.calendar for d in chain if d.calendar), "")
    language = next((d.locale[:2].lower() for d in chain if d.locale), "")
    month = 1 if not fiscal else MONTHS.index(fiscal) + 1 if fiscal in MONTHS else 0
    return _DateOpts(week, month, "" if calendar.lower() == "gregorian" else calendar, language)


def _shift(x: str, months: int) -> str:
    return x if not months else f"{x} {'+' if months > 0 else '-'} interval '{abs(months)} month'"


def _week_trunc(x: str, start: str) -> str:
    """The first day of x's week, weeks starting on `start` (date_trunc('week') starts them on Monday)."""
    k = (1 - WEEKDAYS.index(start)) % 7
    return f"date_trunc('week', {x})" if not k else f"(date_trunc('week', {x} + interval '{k} day') - interval '{k} day')"


def _year_start(x: str, fiscal: int) -> str:
    return f"date_trunc('year', {x})" if fiscal == 1 else f"(date_trunc('year', {_shift(x, 1 - fiscal)}) + interval '{fiscal - 1} month')"


def date_sql(fn: str, unit: str, x: str, opts: _DateOpts, start: str = "", fiscal_field: bool = False) -> str | None:
    """Tableau's DATETRUNC (fn "trunc"), DATEPART ("part") or DATENAME ("name") of a week, weekday, quarter or year of the SQL
    date x, following the Date Properties. Weeks start on `start` (the function's start_of_week) or the Week start; week numbers
    count from the week holding the first day of the year. A datasource's Fiscal year start applies only to date fields set to
    the fiscal calendar (`fiscal_field`: quarters and years start on it, week numbers count from it, and a fiscal year is named
    after the calendar year it ends in). None: the plain date_trunc is already right. Raises FormulaError when the result
    depends on something unknown."""
    if opts.fiscal == 0:
        raise ax.FormulaError("the datasource's Fiscal year start is not a month name")
    weeks = unit == "week"  # DATEPART('weekday') is 1 on Sunday whatever the Week start and start_of_week
    if unit in ("week", "weekday") and opts.calendar:
        raise ax.FormulaError(f"the datasource uses the {opts.calendar} calendar (Date Properties); week-based dates are not translated")
    start = start or opts.week_start
    if weeks and not start:
        raise ax.FormulaError("weeks start on the datasource's Week start (Date Properties), which this datasource does not set, "
                              "and no locale says which day it is: Tableau uses its locale's (Sunday in en_US, Monday in most of "
                              "Europe). For Sunday weeks write `date_trunc('week', d + interval '1 day') - interval '1 day'`")
    f = opts.fiscal
    same = fn == "trunc" and unit == "quarter" and (f - 1) % 3 == 0  # fiscal quarters that start with the calendar ones
    if f != 1 and not fiscal_field and not same and (unit in ("quarter", "year") or (unit == "week" and fn != "trunc")):
        d = "[date]"
        calendar = date_sql(fn, unit, d, _DateOpts(start, 1, opts.calendar, opts.language), start) or f"date_trunc('{unit}', {d})"
        fiscal = date_sql(fn, unit, d, opts, start, fiscal_field=True) or f"date_trunc('{unit}', {d})"
        raise ax.FormulaError(f"the datasource's Fiscal year start is {MONTHS[f - 1].capitalize()}, which Tableau applies only to date "
                              "fields set to the fiscal calendar (Default Properties > Fiscal Year Start): for such a field write "
                              f"`{fiscal}`, for a calendar one `{calendar}`")
    if fn == "trunc":
        if unit == "week":
            return None if start == "monday" else _week_trunc(x, start)
        if unit == "quarter":
            o = (f - 1) % 3
            return None if not o else f"(date_trunc('quarter', {_shift(x, -o)}) + interval '{o} month')"
        if unit == "year":
            return None if f == 1 else _year_start(x, f)
        raise ax.FormulaError(f"DATETRUNC('{unit}') is not translated")
    if unit == "week":
        if f != 1:  # a fiscal field (TODO hints only): whole weeks between the fiscal year's first week and x's week
            part = f"cast((cast({_week_trunc(x, start)} as date) - cast({_week_trunc(_year_start(x, f), start)} as date)) / 7 + 1 as integer)"
        else:
            # day of year minus x's place in its week (0 on the Week start) is the day of year of its week's first day
            # (<= 0 in week 1): floor((doy - place + 5) / 7) + 1, with no date arithmetic
            s = WEEKDAYS.index(start)
            place = f"extract(dow from {x})" if not s else f"(extract(dow from {x}) + {7 - s} - 7 * floor((extract(dow from {x}) + {7 - s}) / 7))"
            part = f"cast(floor((extract(doy from {x}) - {place} + 5) / 7) + 1 as integer)"
    elif unit == "weekday":
        dow = f"extract(dow from {x})"
        if fn == "name":
            if opts.language != "en":
                raise ax.FormulaError("DATENAME('weekday') gives the day's name in the locale's language, and no English locale is "
                                      "recorded: write `case extract(dow from d) when 0 then '<Sunday>' ... end` in that language")
            return "case " + dow + " " + " ".join(f"when {i} then '{d.capitalize()}'" for i, d in enumerate(WEEKDAYS)) + " end"
        part = f"cast({dow} + 1 as integer)"
    elif unit == "quarter":
        part = (f"cast(extract(quarter from {x}) as integer)" if f == 1
                else f"cast(floor(((extract(month from {x}) + {12 - f}) % 12) / 3) + 1 as integer)")
    else:
        part = f"cast(extract(year from {_shift(x, 13 - f if f > 1 else 0)}) as integer)"
    return part if fn == "part" else f"cast({part} as varchar)"


def _dates(node: ax.Node, opts: _DateOpts) -> ax.Node:
    """DATETRUNC / DATEPART / DATENAME of weeks, weekdays, quarters and years -> Tableau-only row functions whose SQL follows
    the Date Properties (registered in the shared ROW_FUNCS under a `tableau:` name no other source's parser produces)."""
    if isinstance(node, ax.Call):
        args = [_dates(a, opts) for a in node.args]
        if node.name == "datename" and len(args) == 2 and isinstance(args[0], ax.Str) and args[0].value.lower() == "month":
            if opts.language != "en":
                raise ax.FormulaError("DATENAME('month') gives the month's name in the locale's language, and no English locale is "
                                      "recorded: write `case extract(month from d) when 1 then '<January>' ... end` in that language")
            return _tableau_fn("case extract(month from {0}) " + " ".join(f"when {i} then '{m.capitalize()}'" for i, m in enumerate(MONTHS, 1))
                               + " end", [args[1]])
        if node.name in ("datetrunc", "datepart", "datename") and args and isinstance(args[0], ax.Str) and args[0].value.lower() in DATE_UNITS:
            unit = args[0].value.lower()
            if len(args) not in (2, 3):
                raise ax.FormulaError(f"{node.name.upper()} takes a date part, a date and an optional start of week")
            start = ""
            if len(args) == 3:
                if not isinstance(args[2], ax.Str) or args[2].value.lower() not in WEEKDAYS:
                    raise ax.FormulaError(f"{node.name.upper()}'s start_of_week must be a literal day name")
                start = args[2].value.lower()
            fn = node.name[4:]
            tmpl = date_sql(fn, unit, "{0}", opts, start)
            if tmpl is None:
                return ax.Call(node.name, args[:2])
            return _tableau_fn(tmpl, [args[1]])
        if node.name == "datediff" and len(args) in (3, 4) and isinstance(args[0], ax.Str) and args[0].value.lower() == "week":
            start = ""
            if len(args) == 4:
                if not isinstance(args[3], ax.Str) or args[3].value.lower() not in WEEKDAYS:
                    raise ax.FormulaError("DATEDIFF's start_of_week must be a literal day name")
                start = args[3].value.lower()
            # whole weeks between the weeks' first days: week boundaries crossed
            week = date_sql("trunc", "week", "{0}", opts, start) or "date_trunc('week', {0})"
            return _tableau_fn(f"cast((cast({week.replace('{0}', '{1}')} as date) - cast({week} as date)) / 7 as integer)", args[1:3])
        return ax.Call(node.name, args)
    if isinstance(node, ax.BinOp):
        return ax.BinOp(node.op, _dates(node.left, opts), _dates(node.right, opts))
    if isinstance(node, ax.Unary):
        return ax.Unary(node.op, _dates(node.operand, opts))
    return node


def _tableau_fn(tmpl: str, args: list[ax.Node]) -> ax.Call:
    """A Tableau-only row function with this SQL template, registered in the shared ROW_FUNCS."""
    name = "tableau:" + hashlib.sha1(tmpl.encode()).hexdigest()[:12]  # one name per SQL template
    ax.ROW_FUNCS.setdefault(name, (tmpl, len(args)))
    return ax.Call(name, args)


def _normalize(node: ax.Node) -> ax.Node:
    if isinstance(node, ax.Call):
        args = [_normalize(a) for a in node.args]
        if node.name == "datepart" and len(args) == 2 and isinstance(args[0], ax.Str) and args[0].value.lower() in ("year", "month", "day"):
            return ax.Call(args[0].value.lower(), [args[1]])
        if node.name in ("min", "max") and len(args) == 2:  # MIN(a, b): a row-level function, not the aggregate
            return ax.Call(node.name + "2", args)
        return ax.Call(node.name, args)
    if isinstance(node, ax.BinOp):
        return ax.BinOp(node.op, _normalize(node.left), _normalize(node.right))
    if isinstance(node, ax.Unary):
        return ax.Unary(node.op, _normalize(node.operand))
    return node


def _has_agg(node: ax.Node) -> bool:
    if isinstance(node, ax.Call):
        if (node.name in TABLEAU_AGGS and len(node.args) == 1) or node.name in OTHER_AGGS:
            return True
        return any(_has_agg(a) for a in node.args)
    if isinstance(node, ax.BinOp):
        return _has_agg(node.left) or _has_agg(node.right)
    if isinstance(node, ax.Unary):
        return _has_agg(node.operand)
    return False


def _find_calls(node: ax.Node, names: set[str]) -> list[ax.Call]:
    out: list[ax.Call] = []
    if isinstance(node, ax.Call):
        if node.name in names:
            out.append(node)
        for a in node.args:
            out += _find_calls(a, names)
    elif isinstance(node, ax.BinOp):
        out += _find_calls(node.left, names) + _find_calls(node.right, names)
    elif isinstance(node, ax.Unary):
        out += _find_calls(node.operand, names)
    return out


def _replace_user_fns(node: ax.Node) -> ax.Node:
    if isinstance(node, ax.Call):
        if node.name in ("username", "userdomain", "fullname") and not node.args:
            return ax.Ref("\x00user." + {"username": "email", "userdomain": "domain", "fullname": "name"}[node.name])
        if node.name == "ismemberof":
            if len(node.args) != 1 or not isinstance(node.args[0], ax.Str):
                raise ax.FormulaError("ISMEMBEROF() needs a group name")
            return ax.Ref("\x00group:" + node.args[0].value)
        if node.name in ("isusername", "isfullname", "userattribute", "userattributeincludes"):
            raise ax.FormulaError(f"{node.name.upper()}() has no row filter translation yet")
        return ax.Call(node.name, [_replace_user_fns(a) for a in node.args])
    if isinstance(node, ax.BinOp):
        return ax.BinOp(node.op, _replace_user_fns(node.left), _replace_user_fns(node.right))
    if isinstance(node, ax.Unary):
        return ax.Unary(node.op, _replace_user_fns(node.operand))
    return node


def _user_sql(ref: str) -> str:
    """Placeholder from _replace_user_fns -> row filter template: {{ user.email }}, or a group test on {{ user.groups }}."""
    if ref.startswith("\x00group:"):
        return "'" + ref[len("\x00group:"):].replace("'", "''") + "' in ({{ user.groups }})"
    return "{{ " + ref[1:] + " }}"


# ==================================================================== raw model


@dataclass
class TField:
    local: str
    caption: str = ""
    datatype: str = ""
    role: str = ""
    ftype: str = ""
    formula: str | None = None
    calc_class: str = ""
    remote: str = ""
    relation: str = ""
    default_agg: str = ""
    description: str = ""
    hidden: bool = False
    param: bool = False
    value: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    aliases: list[tuple[str, str]] = field(default_factory=list)  # <alias key= value=>: (member literal, label)
    format: str = ""  # Default Properties > Number Format (`default-format`, e.g. 'c"$"#,##0.00;("$"#,##0.00)')

    @property
    def label(self) -> str:
        return self.caption or self.local

    @property
    def is_calc(self) -> bool:
        return self.formula is not None or self.calc_class in ("bin", "categorical-bin", "set")


@dataclass
class TRelation:
    name: str
    kind: str  # table | text
    schema: str = ""
    table: str = ""
    database: str = ""
    sql: str = ""
    connection_class: str = ""


@dataclass
class TObject:
    """A logical table of a data source with relationships (<object-graph>): one physical table or custom SQL, or a join."""

    id: str
    caption: str
    relations: list[str] = field(default_factory=list)  # names of the physical relations in it
    join: bool = False


@dataclass
class TRelationship:
    """A relationship between two logical tables: ON pairs of field local names (one per logical table, in either order);
    [] when the clause is not equalities ANDed together. `ends`: attributes of the end points besides object-id
    (Performance Options), keyed "first" / "second"."""

    first: str  # object ids
    second: str
    pairs: list[tuple[str, str]] = field(default_factory=list)
    ends: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class TDatasource:
    uid: str
    name: str
    caption: str
    src: str
    workbook: str = ""
    relations: dict[str, TRelation] = field(default_factory=dict)
    fields: dict[str, TField] = field(default_factory=dict)
    joins: list[str] = field(default_factory=list)
    filters: list[ET.Element] = field(default_factory=list)
    connection_class: str = ""
    published: str = ""  # sqlproxy dbname (published datasource content url)
    # left-deep join tree as steps after the first relation: (join type, right relation name, ON column pairs
    # [((relation, column), (relation, column))]); [] = not only equalities, not translatable
    join_steps: list[tuple[str, str, list[tuple[tuple[str, str], tuple[str, str]]]]] = field(default_factory=list)
    origin: "TDatasource | None" = None  # the published datasource a workbook's sqlproxy stub was linked to
    inherited: set[str] = field(default_factory=set)  # fields taken unchanged from `origin`
    hierarchies: list[tuple[str, list[str]]] = field(default_factory=list)  # <drill-path name>: [field local names]
    settings: dict[str, list[str]] = field(default_factory=dict)  # CONNECTION_SETTINGS attribute -> values on its connections
    extract: bool = False  # <extract enabled='true'>: Tableau computes on its extract (Hyper), not on the warehouse
    # Date Properties: <date-options start-of-week='monday' fiscal-year-start='july' /> (in a workbook's copy of a published
    # datasource, under <overridable-settings>), <default-calendar-type>, and a locale the datasource was authored in
    week_start: str = ""
    fiscal_start: str = ""
    calendar: str = ""
    locale: str = ""
    # Relationships (the logical layer, <object-graph>): logical tables by object id, and the relationships between them
    objects: dict[str, TObject] = field(default_factory=dict)
    relationships: list[TRelationship] = field(default_factory=list)

    @property
    def related(self) -> bool:
        """Several logical tables related by relationships (not one table or one join tree)."""
        return len(self.objects) > 1

    def object_of(self, relation: str) -> TObject | None:
        return next((o for o in self.objects.values() if relation in o.relations), None)
    default_sorts: list[ET.Element] = field(default_factory=list)  # Default Properties > Sort (<default-sorts>)


@dataclass
class _Workbook:
    id: str
    name: str
    src: str
    root: ET.Element
    datasources: dict[str, TDatasource] = field(default_factory=dict)
    params: dict[str, TField] = field(default_factory=dict)
    project: str = ""
    description: str = ""


@dataclass
class _Model:
    key: str
    rel: TRelation
    sm: SemanticModel
    table: Table | None
    pk: str = ""
    elements: set[str] = field(default_factory=set)

    @property
    def from_sql(self) -> tuple[str, str]:
        """(FROM clause, CTE body)."""
        if self.rel.kind == "text":
            return "custom_sql", self.rel.sql
        return self.sm.table, ""


@dataclass
class _Calc:
    kind: str  # row | agg | lod (a FIXED LOD field) | todo | security
    sql: str = ""
    metric: str = ""
    todo: str = ""
    # where the SQL lives: "" the datasource's rows, a FIXED grain's name (its SQL model's columns),
    # or "mixed" (row fields and FIXED values together: tile SQL only, through a join)
    lod: str = ""
    params: tuple[str, ...] = ()  # workbook parameters it reads (itself or through other calcs): SQL for tiles only, with binds


@dataclass(eq=False)
class _Lod:
    """A FIXED LOD grain of a datasource: one SQL model grouped by the FIXED dimensions."""

    name: str
    keys: list[tuple[str, str]]  # (SQL over the datasource rows, output column)
    from_sql: str
    cte: str = ""
    dims: list[str] = field(default_factory=list)  # the FIXED fields' local names
    cols: dict[str, str] = field(default_factory=dict)  # output column -> aggregate SQL
    model: "_Model | None" = None

    def select(self) -> str:
        items = [sql if sql == alias else f"{sql} as {alias}" for sql, alias in self.keys]
        items += [f"{sql} as {alias}" for alias, sql in self.cols.items()]
        return (f"select {', '.join(items)}\nfrom {self.from_sql}\n"
                f"group by {', '.join(str(i) for i in range(1, len(self.keys) + 1))}")

    def sql(self) -> str:
        return (f"with custom_sql as (\n{self.cte}\n)\n" if self.cte else "") + self.select()


@dataclass
class _Special:
    """A tile measure that is not one aggregate over the sheet's rows: an aggregated LOD."""

    kind: str  # fixed | include | exclude | expr
    outer: str  # aggregation applied in the view (MetricFlow name)
    lod: _Lod | None = None  # fixed: the grain and its column
    col: str = ""
    inner: str = ""  # include / exclude: aggregate SQL over the rows
    dims: list[str] = field(default_factory=list)  # include: SQL of the added dimensions; exclude: excluded field names
    value: str = ""  # fixed: SQL over the grain's columns (qualified) when it is not just the column, e.g. a calc over it
    # expr: SQL with a placeholder per aggregate, each computed at its own level: ("row", aggregate SQL over the
    # sheet's rows) or ("fixed", _Special of a FIXED aggregate)
    expr: str = ""
    parts: list[tuple[str, Any]] = field(default_factory=list)


@dataclass
class _SheetCtx:
    ds: TDatasource | None
    row_lods: list[_Lod] = field(default_factory=list)  # FIXED grains joined to the rows
    specials: dict[str, _Special] = field(default_factory=dict)  # shelf instance -> how to compute it
    # relationships: an aggregate calculation over several related tables, per shelf instance: (SQL with a {n} placeholder
    # per aggregate, [(relation, aggregate SQL over that relation's rows)])
    rel_parts: dict[str, tuple[str, list[tuple[str, str]]]] = field(default_factory=dict)
    sorts: dict[str, SheetSort] = field(default_factory=dict)  # dimension shelf instance -> its sort (sheet_sorts)
    sort_by: dict[str, tuple[str, str]] = field(default_factory=dict)  # dimension shelf instance -> its computed sort's (derivation, aggregate SQL)
    labels: dict[str, str] = field(default_factory=dict)  # aliased dimension shelf instance -> its value SQL (grouped on)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Ref:
    ds: TDatasource | None
    dsname: str
    instance: str
    deriv: str = ""
    field: TField | None = None
    suffix: str = ""
    special: str = ""
    table_calc: str = ""
    forecast: str = ""  # forecast-column-type of a column-instance Tableau's forecast adds to


class _NoSQL(Exception):
    pass


def read_datasource(el: ET.Element, uid: str, src: str, workbook: str = "", caption: str = "", locale: str = "") -> TDatasource:
    name = el.get("name") or el.get("formatted-name") or caption or "datasource"
    ds = TDatasource(uid=uid, name=name, caption=el.get("caption") or caption or el.get("formatted-name") or name, src=src, workbook=workbook)
    conn = el.find("connection")
    if conn is not None:
        ds.connection_class = conn.get("class", "")
        ext = el.find("extract")
        ds.extract = ext is not None and ext.get("enabled", "true") != "false"
        if ds.connection_class == "sqlproxy":
            ds.published = conn.get("dbname", "")
        named = {nc.get("name", ""): nc.find("connection") for nc in conn.iter("named-connection")}
        for c in [conn, *(c for c in named.values() if c is not None)]:
            for attr in CONNECTION_SETTINGS:
                value = (c.get(attr) or "").strip()
                if value and value not in ds.settings.get(attr, []):
                    ds.settings.setdefault(attr, []).append(value)
        top = next((c for c in conn if _local(c.tag) == "relation"), None)
        if top is None:
            top = next((c for c in conn if _local(c.tag).endswith("relation")), None)
        if ds.connection_class == "sqlproxy":
            top = None  # <relation name='sqlproxy' table='[sqlproxy]'> is a stub; the tables are in the published .tds
        for rel in _leaf_relations(top, ds):
            nc = named.get(rel.get("connection", "")) if rel.get("connection") else None
            cattrs = nc if nc is not None else conn
            parts = _name_parts(rel.get("table", ""))
            r = TRelation(
                name=rel.get("name") or (parts[-1] if parts else "table"),
                kind="text" if rel.get("type") == "text" else "table",
                table=parts[-1] if parts else "",
                schema=parts[-2] if len(parts) >= 2 else (cattrs.get("schema", "") if cattrs is not None else ""),
                database=parts[-3] if len(parts) >= 3 else (cattrs.get("dbname", "") if cattrs is not None else ""),
                sql=(rel.text or "").strip() if rel.get("type") == "text" else "",
                connection_class=(cattrs.get("class", "") if cattrs is not None else ""),
            )
            ds.relations.setdefault(r.name, r)
        for mr in conn.iter("metadata-record"):
            if mr.get("class") != "column":
                continue
            local = _unbracket(mr.findtext("local-name"))
            if not local:
                continue
            f = ds.fields.setdefault(local, TField(local))
            f.remote = (mr.findtext("remote-name") or "").strip() or f.remote
            f.relation = _unbracket(mr.findtext("parent-name")) or f.relation
            f.datatype = (mr.findtext("local-type") or "").strip() or f.datatype
            f.default_agg = (mr.findtext("aggregation") or "").strip().lower() or f.default_agg
        for m in conn.iter("map"):
            local = _unbracket(m.get("key"))
            parts = _name_parts(m.get("value", ""))
            if local and parts:
                f = ds.fields.setdefault(local, TField(local))
                f.remote = f.remote or parts[-1]
                if len(parts) >= 2:
                    f.relation = f.relation or parts[-2]
    for col in el.findall("column"):
        local = _unbracket(col.get("name"))
        if not local:
            continue
        f = ds.fields.setdefault(local, TField(local))
        f.caption = col.get("caption") or f.caption
        f.datatype = col.get("datatype") or f.datatype
        f.role = col.get("role") or f.role
        f.ftype = col.get("type") or f.ftype
        f.hidden = col.get("hidden") == "true"
        f.default_agg = (col.get("aggregation") or "").lower() or f.default_agg  # Default Properties > Aggregation, set by the author
        f.description = _text(col.find("desc")) or f.description
        f.format = col.get("default-format") or f.format
        f.aliases = [(a.get("key", ""), a.get("value", "")) for a in col.findall("aliases/alias")] or f.aliases
        calc = col.find("calculation")
        if calc is not None:
            f.calc_class = calc.get("class", "tableau")
            if f.calc_class == "tableau":
                f.formula = calc.get("formula", "")
            elif f.calc_class == "bin":
                f.extra = {"of": _unbracket(calc.get("formula")), "size": calc.get("size", ""), "peg": calc.get("peg", "0")}
            elif f.calc_class == "categorical-bin":
                bins = []
                for b in calc.findall("bin"):
                    bins.append((b.get("value", ""), [v.text or "" for v in b.findall("value")]))
                f.extra = {"of": _unbracket(calc.get("column")), "bins": bins}
        if col.get("param-domain-type") or el.get("name") == "Parameters":
            f.param = True
            # the value attribute escapes % as \%; with no value attribute the current value is the calculation's literal
            f.value = _param_unesc(col.get("value", "")) if col.get("value") is not None else f.formula or ""
            rng = col.find("range")
            f.extra = {**f.extra, "members": [_param_unesc(m.get("value", "")) for m in col.iter("member")],
                       "aliases": {_param_unesc(m.get("value", "")): m.get("alias", "") for m in col.iter("member") if m.get("alias")},
                       "domain": col.get("param-domain-type", ""), "range": dict(rng.attrib) if rng is not None else {}}
    for grp in el.findall("group"):
        local = _unbracket(grp.get("name"))
        if local and local not in ds.fields:
            ds.fields[local] = TField(local, caption=grp.get("caption") or local, datatype="boolean", role="dimension", calc_class="set",
                                      extra={"xml": ET.tostring(grp, encoding="unicode")[:400]})
    for f in ds.fields.values():
        if not f.role:
            f.role = "measure" if f.datatype in NUMERIC and not f.formula else "dimension"
        if not f.relation and ds.relations and not f.is_calc:
            f.relation = next(iter(ds.relations))
    ds.filters = el.findall("filter")
    ds.default_sorts = [s for d in el.findall("default-sorts") for s in d]
    for dp in el.findall("drill-paths/drill-path"):
        ds.hierarchies.append((dp.get("name", ""), [_unbracket(x.text) for x in dp.findall("field") if x.text]))
    for opts in el.findall("date-options") + el.findall("overridable-settings/date-options"):
        ds.week_start = (opts.get("start-of-week") or "").lower() or ds.week_start
        ds.fiscal_start = (opts.get("fiscal-year-start") or "").lower() or ds.fiscal_start
    ds.calendar = (el.findtext("overridable-settings/default-calendar-type") or el.findtext("default-calendar-type") or "").strip()
    ds.locale = locale or next((v for c in el.iter() if (v := c.get("author-locale") or c.get("locale"))), "")
    _read_object_graph(el, ds)
    return ds


def _read_object_graph(el: ET.Element, ds: TDatasource) -> None:
    """<object-graph> (Tableau 2020.2+): logical tables and the relationships between them. Desktop may write it twice,
    as `_.fcp.ObjectModelEncapsulateLegacy.true...object-graph` and a legacy copy; the current one wins."""
    graphs = [c for c in el if _local(c.tag).endswith("object-graph")]
    graph = next((g for g in graphs if ".true..." in g.tag), graphs[0] if graphs else None)
    if graph is None:
        return
    for obj in graph.iter("object"):
        oid = obj.get("id", "")
        props = obj.find("properties")
        top = next((c for c in (props if props is not None else []) if _local(c.tag).endswith("relation")), None)
        rels: list[ET.Element] = []
        stack = [top] if top is not None else []
        while stack:
            r = stack.pop()
            if r.get("type") in ("table", "text"):
                rels.append(r)
            stack += [c for c in r if _local(c.tag).endswith("relation")]
        names = [r.get("name") or (_name_parts(r.get("table", "")) or ["table"])[-1] for r in rels]
        ds.objects[oid] = TObject(oid, obj.get("caption", "") or oid, names, join=top is not None and top.get("type") == "join")
    for rs in graph.iter("relationship"):
        ends = {k: dict(e.attrib) for k in ("first", "second") if (e := rs.find(f"{k}-end-point")) is not None}
        first, second = (ends.get(k, {}).pop("object-id", "") for k in ("first", "second"))
        ds.relationships.append(TRelationship(first, second, _field_pairs(rs.find("expression")), {k: v for k, v in ends.items() if v}))
    if ds.related:
        # A join inside one logical table was read as if it joined the whole data source; relationships are not joins.
        ds.joins, ds.join_steps = [], []


def _field_pairs(expr: ET.Element | None) -> list[tuple[str, str]]:
    """Relationship clause -> [(field, field)] (local names) when it is equalities ANDed together, else []."""
    if expr is None:
        return []
    op, kids = expr.get("op", ""), list(expr.findall("expression"))
    if op == "AND" and len(kids) >= 2:
        parts = [_field_pairs(k) for k in kids]
        return [p for ps in parts for p in ps] if all(parts) else []
    if op == "=" and len(kids) == 2 and all(k.get("op", "").startswith("[") and not list(k) for k in kids):
        a, b = (_unbracket(k.get("op")) for k in kids)
        return [(a, b)]
    return []


def _leaf_relations(rel: ET.Element | None, ds: TDatasource) -> list[ET.Element]:
    if rel is None:
        return []
    typ = rel.get("type", "")
    if typ in ("table", "text"):
        return [rel]
    out: list[ET.Element] = []
    children = [c for c in rel if _local(c.tag).endswith("relation")]
    for child in children:
        out += _leaf_relations(child, ds)
    if typ == "join":
        clause = rel.find("clause")
        cond = clause.find("expression") if clause is not None else None
        ops = [e.get("op", "") for e in (cond.iter("expression") if cond is not None else []) if e.get("op") not in ("=", "AND", None)]
        ds.joins.append(f"{rel.get('join', 'inner')} join on {' = '.join(ops)}")
        right = children[-1] if len(children) == 2 and children[-1].get("type") in ("table", "text") else None
        name = (right.get("name") or (_name_parts(right.get("table", "")) or ["table"])[-1]) if right is not None else ""
        ds.join_steps.append((rel.get("join", "inner"), name, _join_pairs(cond) if name else []))
    return out


def _join_pairs(expr: ET.Element | None) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """ON clause -> [((relation, column), (relation, column))] when it is equalities ANDed together, else []."""
    if expr is None:
        return []
    op, kids = expr.get("op", ""), list(expr.findall("expression"))
    if op == "AND" and len(kids) >= 2:
        parts = [_join_pairs(k) for k in kids]
        return [p for ps in parts for p in ps] if all(parts) else []
    if op == "=" and len(kids) == 2:
        sides = [_name_parts(k.get("op", "")) if k.get("op", "").startswith("[") and not list(k) else [] for k in kids]
        if all(len(x) == 2 for x in sides):
            return [((sides[0][0], sides[0][1]), (sides[1][0], sides[1][1]))]
    return []


def _join_on(pairs: list[tuple[tuple[str, str], tuple[str, str]]]) -> str:
    """[((orders, account_id), (accounts, account_id))] -> orders.account_id = accounts.account_id."""
    return " and ".join(f"{_ident(ra)}.{_ident(ca)} = {_ident(rb)}.{_ident(cb)}" for (ra, ca), (rb, cb) in pairs)


# ==================================================================== parser


class _Parser:
    def __init__(self, store: RawStore):
        self.store = store
        self.b = Bundle(tool="tableau")
        self.site: dict[str, Any] = self._json("_site.json") or {}
        self.wb_meta = {w.get("id"): w for w in self._json("workbooks.json") or [] if isinstance(w, dict)}
        self.published = {d.get("id"): d for d in self._json("datasources.json") or [] if isinstance(d, dict)}
        self.groups = {g.get("id"): g.get("name") for g in self._json("groups.json") or [] if isinstance(g, dict)}
        self.view_index: dict[tuple[str, str], dict[str, Any]] = {}
        for v in self._json("views.json") or []:
            if isinstance(v, dict):
                self.view_index[((v.get("workbook") or {}).get("id", ""), v.get("name", ""))] = v
        self.models: dict[str, _Model] = {}
        self.ds_models: dict[str, dict[str, _Model]] = {}
        self.cache: dict[tuple[str, str], _Calc] = {}
        self.metric_sql: dict[str, str] = {}
        self.calc_notes: dict[str, set[str]] = {}  # datasource uid -> where its calcs' results depend on the connection
        self.owner: dict[str, str] = {}  # measure/metric name -> model key
        self.counts: dict[str, set[str]] = {}
        self.defs: dict[tuple[str, str], str] = {}
        self.slugs: set[str] = set()
        self.wb_slugs: dict[str, list[str]] = {}
        self.workbooks: list[_Workbook] = []
        self.tds: list[TDatasource] = []
        self._joined: TDatasource | None = None  # tile SQL over a join: qualify physical columns with their relation
        self._sheet: _SheetCtx | None = None  # set while a worksheet's SQL is built
        self.lods: dict[tuple, _Lod] = {}  # (FROM, CTE, FIXED dimension SQL) -> grain
        self.lod_names: dict[str, _Lod] = {}
        self.lod_fields: dict[tuple[str, str], tuple[_Lod, str]] = {}  # FIXED field -> (grain, column)
        self.inline: dict[str, tuple[_Lod, str]] = {}  # FIXED LOD inside a formula, by its placeholder reference
        self.prepped: dict[tuple[str, str], str] = {}  # formula with its FIXED LODs replaced by placeholder references
        self.view_lods: dict[tuple[str, str], tuple[str, list[str], str, str | None]] = {}  # INCLUDE/EXCLUDE: (kind, dims, inner, outer agg)

    def _json(self, rel: str) -> Any:
        if not self.store.exists(rel):
            return None
        try:
            return self.store.get_json(rel)
        except ValueError as exc:
            self.b.warn(rel, f"unreadable JSON: {exc}")
            return None

    def _xml(self, rel: str) -> ET.Element | None:
        try:
            return ET.fromstring(self.store.path(rel).read_bytes())
        except ET.ParseError as exc:
            self.b.warn(rel, f"XML did not parse: {exc}")
            return None

    def run(self) -> Bundle:
        for rel in self.store.glob("datasources/*.tds"):
            root = self._xml(rel)
            if root is None:
                continue
            did = Path(rel).stem
            meta = self.published.get(did) or {}
            ds = read_datasource(root, f"{rel}#", rel, caption=meta.get("name", ""))
            ds.caption = meta.get("name") or ds.caption
            ds.published = meta.get("contentUrl", "") or ds.published
            self.tds.append(ds)
        for rel in self.store.glob("workbooks/*.twb"):
            root = self._xml(rel)
            if root is None:
                continue
            wid = Path(rel).stem
            meta = self.wb_meta.get(wid) or {}
            wb = _Workbook(wid, meta.get("name") or wid, rel, root, project=(meta.get("project") or {}).get("name", ""),
                           description=meta.get("description") or "")
            for el in root.findall("datasources/datasource"):
                ds = read_datasource(el, f"{rel}#{el.get('name', '')}", rel, workbook=wid, locale=root.get("locale", ""))
                if ds.name == "Parameters":
                    wb.params.update(ds.fields)
                    continue
                wb.datasources[ds.name] = ds
            self.workbooks.append(wb)
        data_sources = list(self.tds) + [ds for wb in self.workbooks for ds in wb.datasources.values()]
        for ds in data_sources:
            self._link_published(ds)
        self.b.detect_warehouse(
            c for ds in data_sources for c in [ds.connection_class, *(r.connection_class for r in ds.relations.values())]
            if c not in ("federated", "sqlproxy")
        )
        self.dialect = self.store.warehouse(self.b.datasource) or self.b.warehouse  # unshackle.toml wins over the export
        for ds in data_sources:
            self._assign_models(ds)
        self._count_names(data_sources)
        for ds in data_sources:
            self._base_fields(ds)
        for ds in data_sources:
            for f in list(ds.fields.values()):
                if f.is_calc and not f.param:
                    self._calc(ds, f)
        for ds in data_sources:
            for note in sorted(self.calc_notes.get(ds.uid, ())):
                for m in (self.ds_models.get(ds.uid) or {}).values():
                    self._describe(m, note)
            self._display(ds)
        for lod in self.lod_names.values():
            if lod.model is not None:
                lod.model.sm.sql = lod.sql()
        for m in self.models.values():
            if m.sm.measures and not any(d.type == "time" for d in m.sm.dimensions):
                self.b.warn(f"semantic model {m.sm.name}", "has measures but no date column; MetricFlow needs defaults.agg_time_dimension", "info")
        for wb in self.workbooks:
            try:
                self._dashboards(wb)
            except Exception as exc:  # noqa: BLE001 - one bad workbook must not stop the rest
                self.b.warn(wb.src, f"dashboards skipped: {exc}")
            self._knowledge(wb)
        if self.tds:
            self._tds_knowledge()
        self._metadata_only()
        self._in_dialect(data_sources)
        self._permissions()
        for e in self._json("_errors.json") or []:
            if isinstance(e, dict):
                self.b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return self.b.settle_time_dimensions()

    def _in_dialect(self, data_sources: list[TDatasource]) -> None:
        """Tile, model and semantic SQL in the warehouse's dialect (sources/_dialect.py). Custom SQL stays as written."""
        w = self.dialect
        if w and w not in dialect.DIALECTS:
            self.b.warn("warehouse type", f"{w!r}: tile and model SQL is written for {', '.join(dialect.DIALECTS)}; translate it for {w}")
            return
        keep = tuple(r.sql for ds in data_sources for r in ds.relations.values() if r.kind == "text" and r.sql)

        def put(obj: Any, attr: str) -> None:
            sql = getattr(obj, attr)
            if not sql:
                return
            try:
                setattr(obj, attr, dialect.render(sql, w, keep))
            except dialect.Unsupported as exc:
                note = f"{w}: {exc}; rewrite this part of the SQL for {w}"
                obj.todo = f"{obj.todo}; {note}" if obj.todo else note

        for sm in self.b.semantic_models:
            put(sm, "sql")
            for x in [*sm.dimensions, *sm.measures]:
                put(x, "expr")
        for m in self.b.metrics:
            put(m, "expr")
        for rf in self.b.row_filters:
            put(rf, "predicate")
        for d in self.b.dashboards:
            for t in d.tiles:
                put(t, "sql")

    # ------------------------------------------------------------ datasources -> models

    def _link_published(self, ds: TDatasource) -> None:
        if ds.connection_class != "sqlproxy" or ds.relations:
            return
        target = next((t for t in self.tds if ds.published and t.published == ds.published), None)
        target = target or next((t for t in self.tds if t.caption == ds.caption), None)
        if target is None:
            self.b.warn(f"{ds.src} datasource {ds.caption}", f"uses published datasource {ds.published or ds.caption!r}, which was not extracted; its fields have no table", "warn")
            return
        ds.relations = dict(target.relations)
        ds.joins = list(target.joins)
        ds.join_steps = list(target.join_steps)
        ds.objects = dict(target.objects)
        ds.relationships = list(target.relationships)
        ds.hierarchies = ds.hierarchies or list(target.hierarchies)
        ds.default_sorts = ds.default_sorts or list(target.default_sorts)
        ds.origin = target
        for local, tf in target.fields.items():
            mine = ds.fields.get(local)
            if mine is None or not mine.formula:
                ds.inherited.add(local)
            if mine is None:
                ds.fields[local] = copy.deepcopy(tf)
            elif not mine.formula:
                mine.remote = mine.remote or tf.remote
                mine.relation = tf.relation or mine.relation
                mine.formula = tf.formula
                mine.calc_class = mine.calc_class or tf.calc_class
                mine.extra = mine.extra or tf.extra
                mine.description = mine.description or tf.description
                mine.default_agg = mine.default_agg or tf.default_agg
            if mine is not None and local in ds.inherited:
                # a published field's aliases are the published datasource's (a workbook's copy has no say);
                # a number format set in the workbook wins
                mine.aliases = list(tf.aliases)
                mine.format = mine.format or tf.format

    def _assign_models(self, ds: TDatasource) -> None:
        item = f"{ds.src} datasource {ds.caption}"
        if ds.connection_class in FILE_CONNECTIONS or any(r.connection_class in FILE_CONNECTIONS for r in ds.relations.values()):
            self.b.warn(item, "connects to a file/extract, not a warehouse; point the semantic model at the warehouse table holding this data")
        self._connection_settings(ds, item)
        mine: dict[str, _Model] = {}
        for r in ds.relations.values():
            key = f"table:{r.schema.lower()}.{r.table.lower()}" if r.kind == "table" else "sql:" + hashlib.sha1(" ".join(r.sql.split()).encode()).hexdigest()[:12]
            model = self.models.get(key)
            if model is None:
                base = _name(r.table if r.kind == "table" else ds.caption)
                name, n = base, 2
                taken = {m.sm.name for m in self.models.values()}
                while name in taken:
                    name, n = f"{base}_{n}", n + 1
                src = f"{ds.src} datasource: {ds.caption}"
                table_sql = ".".join(_ident(p) for p in (r.schema, r.table) if p) if r.kind == "table" else name
                sm = SemanticModel(name=name, table=table_sql, datasource=self.b.datasource, source_ref=src,
                                   description="" if r.kind == "table" else f"Tableau custom SQL from datasource {ds.caption}")
                tbl = Table(self.b.datasource, r.schema, r.table, source_ref=src) if r.kind == "table" else None
                model = _Model(key, r, sm, tbl)
                pk = self._guess_pk(ds, r)
                model.pk = pk
                sm.entities.append(Entity(name, "primary", "" if pk == name else pk))
                if not pk:
                    self.b.warn(f"semantic model {name}", "no id column found; set the primary entity expr to the table's key", "info")
                if r.kind == "text":
                    todo = (
                        f"Tableau custom SQL: materialize as a SQL model (queries/models/{name}.sql, dbt: models/unshackle/{name}.sql) "
                        f"so semantic model `{name}` has a table; see skills/translate-metrics"
                    )
                    if PARAM_REF.search(r.sql) or "<[Parameters]" in r.sql:
                        todo += "; it uses Tableau parameters (<[Parameters].[x]>), replace them with fixed values or :binds"
                    self.b.queries.append(VerifiedQuery(f"{ds.caption} custom SQL", r.sql, f"Custom SQL relation of Tableau datasource {ds.caption}",
                                                        datasource=self.b.datasource, source_ref=src, todo=todo))
                self.models[key] = model
                self.b.semantic_models.append(sm)
                if tbl is not None:
                    self.b.tables.append(tbl)
            mine[r.name] = model
        self.ds_models[ds.uid] = mine
        if ds.related and not (ds.origin is not None and ds.relationships == ds.origin.relationships):
            left = self._relate_logical(ds, mine)
            if left:
                self.b.warn(item, f"relates {len(ds.objects)} logical tables; no relationship was made for {'; '.join(left)}: add foreign "
                            "entities by hand", "info")
        if ds.joins and not (ds.origin is not None and ds.join_steps == ds.origin.join_steps):
            left = self._relate(ds, mine)
            if left:
                self.b.warn(item, f"joins {len(ds.relations)} tables ({'; '.join(ds.joins)}); no relationship was made for "
                            f"{'; '.join(left)}: add foreign entities by hand", "info")

    def _connection_settings(self, ds: TDatasource, item: str) -> None:
        """Initial SQL and connection options change what the warehouse returns; the migrated SQL doesn't carry them."""
        for attr, values in ds.settings.items():
            for value in values:
                if attr == "one-time-sql":
                    msg = (f"{CONNECTION_SETTINGS[attr]}: `{' '.join(value.split())[:300]}`. The migrated SQL doesn't run it: run it in the "
                           "warehouse session before the migrated queries (a connect hook on the notebooks' engine, dbt on-run-start), "
                           "or fold its effect into the models")
                    used = sorted(set(INITIAL_SQL_VARS.findall(value))) + (["parameters"] if PARAM_REF.search(value) else [])
                    if used:
                        msg += (f"; it uses {', '.join(used)}, which only Tableau fills in (per viewer or workbook): replace them "
                                "(per-user filtering: skills/translate-permissions)")
                else:
                    msg = f"{CONNECTION_SETTINGS[attr]} `{value}`: set the same options on the warehouse connection the migrated SQL uses"
                self.b.warn(item, msg)

    @staticmethod
    def _settings_lines(ds: TDatasource) -> list[str]:
        lines: list[str] = []
        for attr, values in ds.settings.items():
            for value in values:
                lines += ["", f"{CONNECTION_SETTINGS[attr][0].upper()}{CONNECTION_SETTINGS[attr][1:]}:", "", "```sql", value, "```"]
        return lines

    def _relate(self, ds: TDatasource, mine: dict[str, _Model]) -> list[str]:
        """Join clauses -> foreign entities between the per-table models, with the join semantics in their descriptions.
        Returns the joins that could not become a relationship."""
        left: list[str] = []
        for kind, right, pairs in ds.join_steps:
            kind = kind.lower()
            if len(pairs) != 1:
                left.append(f"{kind} join to {right}" + (" (several columns)" if pairs else " (not a column equality)"))
                continue
            (ra, ca), (rb, cb) = pairs[0]
            ma, mb = mine.get(ra), mine.get(rb)
            if ma is None or mb is None or ma is mb:
                left.append(f"{kind} join {ra}.{ca} = {rb}.{cb}")
                continue
            if mb.pk and cb.lower() == mb.pk.lower():
                many, fk, one, key = ma, ca, mb, cb
            elif ma.pk and ca.lower() == ma.pk.lower():
                many, fk, one, key = mb, cb, ma, ca
            else:
                left.append(f"{kind} join {ra}.{ca} = {rb}.{cb} (neither column is its table's key)")
                continue
            ent = one.sm.name
            if not any(e.name == ent for e in many.sm.entities):
                many.sm.entities.append(Entity(ent, "foreign", "" if fk == ent else fk, join_type=kind))
            clause = f"{many.sm.name}.{fk} = {one.sm.name}.{key}"
            if kind == "inner":
                drop_many = f"; Tableau drops {many.sm.name} rows with no match in {one.sm.name}, a relationship (left join) keeps them"
                drop_one = f" and drops {one.sm.name} rows with no {many.sm.name} row"
            else:
                drop_many = drop_one = ""
            self._describe(many, f"In Tableau datasource {ds.caption}, {many.sm.name} {kind} joins {one.sm.name} on {clause}{drop_many}.")
            self._describe(one, f"In Tableau datasource {ds.caption}, {one.sm.name} is {kind} joined to {many.sm.name} on {clause}: Tableau "
                                f"aggregates {one.sm.name} columns over the joined rows, once per {many.sm.name} row{drop_one}; "
                                f"measures here aggregate {one.sm.name} rows.")
        return left

    def _relate_logical(self, ds: TDatasource, mine: dict[str, _Model]) -> list[str]:
        """Relationships between logical tables -> foreign entities (the "many" side refers to the side whose column is its
        key), with Tableau's semantics in the models' descriptions. Returns the relationships that could not become one."""
        left: list[str] = []
        edges = self._rel_edges(ds)
        for rs in ds.relationships:
            a, b = ds.objects.get(rs.first), ds.objects.get(rs.second)
            label = " - ".join(o.caption if o is not None else "?" for o in (a, b))
            if a is None or b is None or a.join or b.join or len(a.relations) != 1 or len(b.relations) != 1:
                left.append(f"{label} (a logical table that joins tables)")
                continue
            ra, rb = a.relations[0], b.relations[0]
            cols = next((c for n, c in edges.get(ra, []) if n == rb), None)
            if not cols or len(cols) != 1:
                left.append(f"{label} ({'several columns' if cols else 'not column equalities'})")
                continue
            (ca, cb), ma, mb = cols[0], mine.get(ra), mine.get(rb)
            if ma is None or mb is None or ma is mb:
                left.append(label)
                continue
            if mb.pk and cb.lower() == mb.pk.lower():
                many, fk, one, key = ma, ca, mb, cb
            elif ma.pk and ca.lower() == ma.pk.lower():
                many, fk, one, key = mb, cb, ma, ca
            else:
                left.append(f"{label} on {ra}.{ca} = {rb}.{cb} (neither column is its table's key)")
                continue
            # Performance Options (cardinality, referential integrity) are attributes of the end points; with none set Tableau
            # assumes many-to-many and some records match, and the numbers don't depend on them when they are true of the data.
            ext: dict[str, Any] = {"tableau_relationship": f"{a.caption} - {b.caption}"}
            if rs.ends:
                ext["performance_options"] = {(a if k == "first" else b).caption: v for k, v in rs.ends.items()}
            else:
                ext["cardinality"] = "many-to-many, some records match (Tableau's default)"
            ent = one.sm.name
            if not any(e.name == ent for e in many.sm.entities):
                many.sm.entities.append(Entity(ent, "foreign", "" if fk == ent else fk, extensions=ext))
            clause = f"{many.sm.name}.{fk} = {one.sm.name}.{key}"
            for m in (many, one):
                self._describe(m, f"In Tableau datasource {ds.caption}, {many.sm.name} is related to {one.sm.name} on {clause} (a relationship, "
                                  "not a join): Tableau aggregates each table's measures on its own rows at the view's dimensions, so "
                                  f"{one.sm.name} measures are not repeated per {many.sm.name} row, and rows with no match are kept "
                                  "(with NULL for the other table's dimensions).")
        return left

    @staticmethod
    def _describe(model: _Model, line: str) -> None:
        if line not in model.sm.description:
            model.sm.description = f"{model.sm.description}\n{line}".strip()

    def _guess_pk(self, ds: TDatasource, rel: TRelation) -> str:
        cols = [f.remote for f in ds.fields.values() if f.remote and f.relation == rel.name and not f.is_calc]
        lower = {c.lower(): c for c in cols}
        singular = rel.table.lower().rstrip("s")
        for cand in ("row_id", "id", f"{singular}_id"):
            if cand in lower:
                return lower[cand]
        ids = [c for c in cols if c.lower().endswith("_id")]
        return ids[0] if len(ids) == 1 else ""

    def _field_model(self, ds: TDatasource, f: TField, seen: frozenset[str] = frozenset()) -> _Model | None:
        models = self.ds_models.get(ds.uid) or {}
        if not models:
            return None
        if f.relation and f.relation in models:
            return models[f.relation]
        if f.formula and f.local not in seen:
            for r in BRACKETED.findall(f.formula):
                g = ds.fields.get(_unesc(r))
                if g is not None and g is not f:
                    m = self._field_model(ds, g, seen | {f.local})
                    if m is not None:
                        return m
        if f.extra.get("of") and f.extra["of"] in ds.fields:
            return self._field_model(ds, ds.fields[f.extra["of"]], seen | {f.local})
        return next(iter(models.values()))

    def _tables_of(self, ds: TDatasource, f: TField, seen: frozenset[str] = frozenset()) -> set[str]:
        """The physical relations a field reads, through the calculations it references (parameters read none)."""
        if f.param or f.local in seen:
            return set()
        if not f.is_calc:
            return {f.relation} if f.relation else set()
        refs = [_unesc(r) for r in BRACKETED.findall(f.formula or "")] + ([f.extra["of"]] if f.extra.get("of") else [])
        out: set[str] = set()
        for r in refs:
            g = ds.fields.get(r)
            if g is not None and g is not f:
                out |= self._tables_of(ds, g, seen | {f.local})
        return out

    def _count_names(self, sources: list[TDatasource]) -> None:
        for ds in sources:
            for f in ds.fields.values():
                if f.param or f.local in INTERNAL_FIELDS:
                    continue
                if f.is_calc or (f.role == "measure" and f.datatype in NUMERIC):
                    m = self._field_model(ds, f)
                    if m is not None:
                        self.counts.setdefault(_name(f.label), set()).add(m.key)

    def _namer(self, model: _Model, label: str) -> str:
        cand = _name(label)
        if len(self.counts.get(cand, set())) > 1 or self.owner.get(cand, model.key) != model.key:
            cand = f"{model.sm.name}_{cand}"
        return cand

    def _exists(self, model: _Model, name: str, ds: TDatasource, f: TField) -> bool:
        """True when `name` is already in the model (merged datasources); warns if the formula differs."""
        if name not in model.elements:
            self.defs[(model.key, name)] = f.formula or ""
            return False
        prev = self.defs.get((model.key, name))
        if prev is not None and f.formula is not None and " ".join(prev.split()) != " ".join(f.formula.split()):
            self.b.warn(f"{ds.src} datasource {ds.caption} field {f.label}",
                        f"`{name}` already exists in semantic model {model.sm.name} with a different formula; this one was skipped")
        return True

    def _register(self, model: _Model, name: str) -> None:
        self.owner[name] = model.key
        model.elements.add(name)

    def _grain_metric(self, lm: _Model, ds: TDatasource, f: TField, agg: str, expr: str, src: str, note: str = "") -> str:
        """A measure and metric on a FIXED grain's model (once per name); returns the name."""
        name = self._namer(lm, f.label)
        if not self._exists(lm, name, ds, f):
            lm.sm.measures.append(Measure(name, agg, "" if expr == name else expr, f.description, f.label))
            self.b.metrics.append(Metric(name, label=f.label, description=" ".join(x for x in (f.description, note) if x), measure=name,
                                         semantic_model=lm.sm.name, source_ref=src))
            self.metric_sql[name] = AGG_SQL[agg].format(expr)
            self._register(lm, name)
        return name

    def _base_fields(self, ds: TDatasource) -> None:
        for f in ds.fields.values():
            if f.is_calc or f.param or f.local in INTERNAL_FIELDS or not f.remote:
                continue
            m = self._field_model(ds, f)
            if m is None:
                continue
            if m.table is not None and not any(c.name == f.remote for c in m.table.columns):
                m.table.columns.append(Column(f.remote, COLUMN_TYPE.get(f.datatype, ""), f.description, f.label, f.hidden))
            if f.remote == m.pk:
                continue
            src = f"{ds.src} datasource: {ds.caption} field: {f.label}"
            if f.role == "measure" and f.datatype in NUMERIC:
                name = self._namer(m, f.label)
                if name in m.elements:
                    continue
                agg = DEFAULT_AGG.get(f.default_agg, "sum")
                m.sm.measures.append(Measure(name, agg, "" if f.remote == name else _ident(f.remote), f.description, f.label))
                self.b.metrics.append(Metric(name, label=f.label, description=f.description, measure=name, semantic_model=m.sm.name, source_ref=src))
                self.metric_sql[name] = AGG_SQL[agg].format(_ident(f.remote))
                self._register(m, name)
                continue
            name = _name(f.label)
            if name in m.elements or name == m.sm.name:
                continue
            expr = "" if f.remote == name else _ident(f.remote)
            if f.datatype in TIME_TYPES:
                m.sm.dimensions.append(Dimension(name, "time", expr, f.description, f.label, "day"))
            else:
                m.sm.dimensions.append(Dimension(name, "categorical", expr, f.description, f.label))
            m.elements.add(name)

    def _display(self, ds: TDatasource) -> None:
        """Number formats and aliases (Default Properties) on the dimensions, measures and metrics made from the fields."""
        for f in ds.fields.values():
            if not (f.format or f.aliases) or f.param:
                continue
            fmt = {"format": d3_format(f.format), "source_format": f.format}
            labels = [{"value": _param_value(k, f.datatype) if k != "%null%" else None, "label": v} for k, v in f.aliases
                      ] if f.datatype in ("string", "boolean") else []
            model = self._field_model(ds, f)
            for el in [*(model.sm.dimensions + model.sm.measures if model is not None else []), *self.b.metrics]:  # type: ignore[operator]
                mine = (el.source_ref == f"{ds.src} datasource: {ds.caption} field: {f.label}" if isinstance(el, Metric)
                        else el.label == f.label)
                if not mine or el.source_format:
                    continue
                if f.format:
                    el.format, el.source_format = fmt["format"], fmt["source_format"]
                if labels and isinstance(el, Dimension):
                    el.value_labels = labels

    # ------------------------------------------------------------ calculated fields

    def _calc(self, ds: TDatasource, f: TField, stack: tuple = ()) -> _Calc:
        key = (ds.uid, f.local)
        if key in self.cache:
            return self.cache[key]
        if key in stack:
            return _Calc("todo", todo=f"circular reference through [{f.label}]")
        if ds.origin is not None and f.local in ds.inherited:
            # A calc of the published datasource: translate it once, there (no duplicate metrics, LOD queries or row filters).
            res = self._calc(ds.origin, ds.origin.fields[f.local], stack)
            self.cache[key] = res
            for table in (self.lod_fields, self.prepped, self.view_lods):
                if (ds.origin.uid, f.local) in table:
                    table[key] = table[(ds.origin.uid, f.local)]  # type: ignore[index]
            return res
        res = self._translate(ds, f, stack + (key,))
        self.cache[key] = res
        return res

    def params_for(self, ds: TDatasource) -> dict[str, TField]:
        wb = next((w for w in self.workbooks if w.id == ds.workbook), None)
        return wb.params if wb is not None else {}

    def _is_joined(self, ds: TDatasource | None) -> bool:
        j = self._joined
        return j is not None and ds is not None and (ds is j or ds is j.origin or ds.origin is j)

    def _row_ref(self, ds: TDatasource, name: str) -> str:
        if name.startswith(PARAM_ARG):
            p = self.params_for(ds).get(name[1:].replace("\x02", "]"))
            if p is None:
                raise ax.FormulaError(f"unknown parameter [{name[1:]}]")
            return param_bind(p)
        if name in self.inline:
            return self._lod_col(*self.inline[name])
        g = ds.fields.get(name)
        if g is None:
            raise ax.FormulaError(f"unknown field [{name}]")
        if g.is_calc:
            c = self.cache.get((ds.uid, g.local))
            if c is not None and c.kind == "lod" and (ds.uid, g.local) in self.lod_fields:
                return self._lod_col(*self.lod_fields[(ds.uid, g.local)])
            if c is not None and c.kind == "row":
                if self._sheet is not None and (c.lod or self._is_joined(ds)):
                    return _paren(self._row_sql(ds, g))  # in this sheet's FROM: qualified, FIXED values joined
                if c.lod == "mixed":
                    raise ax.FormulaError(f"[{g.label}] mixes row fields with a FIXED LOD value")
                if self._is_joined(ds) and not c.lod:
                    return _paren(self._row_sql(ds, g))
                return _paren(c.sql)
            raise ax.FormulaError(f"[{g.label}] is an aggregate or not translated")
        if g.remote:
            if self._is_joined(ds) and g.relation:
                return f"{_ident(g.relation)}.{_ident(g.remote)}"
            return _ident(g.remote)
        raise ax.FormulaError(f"[{g.label}] has no physical column")

    def _sql(self, ds: TDatasource, node: ax.Node, ref: Any, dtype: Any = None) -> str:
        """Row-level SQL with Tableau's semantics (_tableau_calc), in the Postgres form the dialect pass rewrites."""
        hyper = ds.extract or (ds.origin is not None and ds.origin.extract)
        r = tc.Renderer(ref, dtype or (lambda name: self._dtype(ds, name)), hyper=hyper)
        sql = r(node)
        if r.notes:
            self.calc_notes.setdefault(ds.uid, set()).update(r.notes)
        return sql

    def _classify(self, ds: TDatasource, node: ax.Node, ref: Any, metric_ref: Any) -> ax.Classified:
        return ax.classify(node, LANG, ref, metric_ref, render=lambda n, rf: self._sql(ds, n, rf))

    def _dtype(self, ds: TDatasource, name: str) -> str:
        if name.startswith(PARAM_ARG):
            p = self.params_for(ds).get(name[1:].replace("\x02", "]"))
            return p.datatype if p is not None else ""
        g = ds.fields.get(name)
        return g.datatype if g is not None else ""

    def _row_sql(self, ds: TDatasource, g: TField) -> str:
        """A row-level calc's SQL again, in the current context (joined columns qualified, FIXED values joined in tiles)."""
        text = self.prepped.get((ds.uid, g.local)) or scan_formula(g.formula or "")[0]
        return ax.strip_outer(self._sql(ds, parse_formula(text, date_opts(ds)), lambda r: self._row_ref(ds, r)))

    def _lod_col(self, lod: _Lod, col: str) -> str:
        """A FIXED value: its column in the grain's SQL model, or in a tile, the grain joined to the sheet's rows."""
        if self._sheet is None:
            return _ident(col)
        if lod not in self._sheet.row_lods:
            self._sheet.row_lods.append(lod)
        return f"{lod.name}.{_ident(col)}"

    def _metric_ref(self, ds: TDatasource, name: str) -> str | None:
        g = ds.fields.get(name)
        if g is not None and g.is_calc:
            c = self.cache.get((ds.uid, g.local))
            if c is not None and c.kind == "agg":
                return c.metric
        return None

    def _todo(self, ds: TDatasource, f: TField, model: _Model | None, msg: str, aggregate: bool | None = None) -> _Calc:
        src = f"{ds.src} datasource: {ds.caption} field: {f.label}"
        expr = readable(" ".join(scan_formula(f.formula)[0].split()), ds.fields, self.params_for(ds)) if f.formula is not None else f"{f.calc_class}: {f.extra}"
        is_measure = aggregate if aggregate is not None else (f.role == "measure")
        if is_measure or model is None:
            name = self._namer(model, f.label) if model else _name(f.label)
            if model is None or not self._exists(model, name, ds, f):
                self.b.metrics.append(Metric(name, label=f.label, description=f.description, semantic_model=model.sm.name if model else "",
                                             source_expression=expr, source_language=LANG, source_ref=src, todo=msg))
                if model is not None:
                    self._register(model, name)
        else:
            name = _name(f.label)
            if not self._exists(model, name, ds, f):
                model.sm.dimensions.append(Dimension(name, "categorical", label=f.label, description=f.description,
                                                     source_expression=expr, source_language=LANG, todo=msg))
                model.elements.add(name)
        return _Calc("todo", todo=msg)

    def _translate(self, ds: TDatasource, f: TField, stack: tuple) -> _Calc:
        model = self._field_model(ds, f)
        if f.calc_class == "bin":
            of, size = f.extra.get("of", ""), f.extra.get("size", "")
            col = ds.fields.get(of)
            hint = f"floor({_ident(col.remote)} / {size}) * {size}" if col is not None and col.remote and size else "floor(<col> / <size>) * <size>"
            return self._todo(ds, f, model, f"Tableau bin of [{of}] (size {size}): make a dimension with expr `{hint}`; see {SKILL}", False)
        if f.calc_class == "categorical-bin":
            of = f.extra.get("of", "")
            col = ds.fields.get(of)
            ref = _ident(col.remote) if col is not None and col.remote else f"[{of}]"
            whens = " ".join(
                f"when {ref} in ({', '.join(filter(None, (_member_sql(v) for v in values)))}) then {_member_sql(label) or 'null'}"
                for label, values in f.extra.get("bins", [])
            )
            return self._todo(ds, f, model, f"Tableau group on [{of}]: make a dimension with expr `case {whens} else {ref} end`; see {SKILL}", False)
        if f.calc_class == "set":
            return self._todo(ds, f, model, f"Tableau set: a boolean dimension (`<col> in (...)` or a top-N subquery); see {SKILL}", False)
        clean, code, bare, _ = scan_formula(f.formula or "")
        if USER_FN.search(bare):
            self._row_filter(ds, f, model, clean)
            return _Calc("security", todo="user filter")
        if PARAM_REF.search(code):
            return self._param_calc(ds, f, model, stack)
        spans = lod_spans(clean)
        if spans:
            res = self._lods_in(ds, f, model, clean, spans, stack)
            if isinstance(res, _Calc):
                return res
            clean = res  # each FIXED LOD replaced by a reference to its column
            self.prepped[(ds.uid, f.local)] = clean
            _, code, bare, _ = scan_formula(clean)
        tc = TABLE_CALC.search(bare)
        if tc:
            return self._todo(ds, f, model, f"Tableau table calculation {tc.group(1).upper()}() depends on the view: compute it as window SQL in the notebook tile (skills/translate-dashboards), not a metric; see skills/translate-metrics")
        other = sorted({_unesc(d) for d, _ in DS_REF.findall(code) if _unesc(d) != "Parameters"})
        if other:
            return self._todo(ds, f, model, f"references another datasource ({', '.join(other)}): data blending; join the tables in SQL or through entities; see {SKILL}")
        if ATTR_FN.search(bare):
            return self._todo(ds, f, model, f"ATTR() returns the value only when it is constant in the view: use the dimension itself or min()/max(); see {SKILL}")
        try:
            node = parse_formula(clean, date_opts(ds))
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"formula not translated ({exc}); see skills/translate-metrics")
        dep_agg = False
        homes: set[str] = set()  # where each input lives: "" the rows, else a FIXED grain
        for r in ax.refs(node):
            if r in self.inline:
                homes.add(self.inline[r][0].name)
                continue
            g = ds.fields.get(r)
            if g is None:
                return self._todo(ds, f, model, f"references unknown field [{r}]; see skills/translate-metrics")
            if g.is_calc and not g.param:
                dep = self._calc(ds, g, stack)
                if dep.kind in ("todo", "security"):
                    return self._todo(ds, f, model, f"depends on [{g.label}], which is not translated yet; finish that first (skills/translate-metrics)")
                if dep.params:
                    return self._param_calc(ds, f, model, stack)
                if dep.kind == "agg" and dep.lod:
                    return self._todo(ds, f, model, f"depends on [{g.label}], an aggregate of FIXED LOD values: write it in the tile SQL "
                                                    "or as a metric on the FIXED grain's semantic model; see skills/translate-metrics")
                dep_agg = dep_agg or dep.kind == "agg"
                homes |= {"", "mixed"} if dep.lod == "mixed" else {dep.lod}
            else:
                homes.add("")
        if model is None:
            return self._todo(ds, f, None, "datasource has no table; see skills/translate-metrics")
        tables = sorted(self._tables_of(ds, f)) if ds.related else []
        if len(tables) > 1:
            if _has_agg(node) or dep_agg:
                return self._todo(ds, f, model, f"aggregates fields of related tables ({', '.join(tables)}): with relationships Tableau aggregates "
                                                "each table's fields on that table's own rows at the view's dimensions, then combines the "
                                                f"results, so it is not one metric on one table; see {SKILL}", True)
            return self._todo(ds, f, model, f"a row-level calculation over fields of related tables ({', '.join(tables)}): Tableau computes it "
                                            "over those tables joined by their relationship; write that join in a SQL model and put the "
                                            f"calculation there; see {SKILL}")
        src = f"{ds.src} datasource: {ds.caption} field: {f.label}"
        grains = homes - {"", "mixed"}
        if len(grains) > 1:
            return self._todo(ds, f, model, "combines FIXED LODs of different grains: join each grain's SQL model on its dimensions in the tile SQL; see skills/translate-metrics")
        lod = self.lod_names[next(iter(grains))] if grains else None
        mixed = "mixed" in homes or (lod is not None and "" in homes)
        if _has_agg(node) or dep_agg:
            if lod is not None or mixed:
                return self._lod_aggregate(ds, f, model, node, src, lod, mixed)
            return self._aggregate(ds, f, model, node, src)
        if mixed:
            msg = ("mixes row fields with a FIXED LOD value: notebook tiles join the FIXED grain to the rows in their SQL; "
                   "a semantic model can't hold it on one table (add it to a SQL model that joins the grain); see skills/translate-metrics")
            self._todo(ds, f, model, msg)
            return _Calc("row", lod="mixed", todo=msg)
        try:
            sql = ax.strip_outer(self._sql(ds, node, lambda r: self._row_ref(ds, r)))
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"formula not translated ({exc}); see skills/translate-metrics")
        if lod is not None:
            return self._lod_row(ds, f, model, lod, node, sql)
        if f.role == "measure" and f.datatype in NUMERIC:
            name = self._namer(model, f.label)
            agg = DEFAULT_AGG.get(f.default_agg, "sum")
            if not self._exists(model, name, ds, f):
                model.sm.measures.append(Measure(name, agg, sql, f.description, f.label))
                self.b.metrics.append(Metric(name, label=f.label, description=f.description, measure=name, semantic_model=model.sm.name, source_ref=src))
                self.metric_sql[name] = AGG_SQL[agg].format(sql)
                self._register(model, name)
            return _Calc("row", sql=sql, metric=name)
        name = _name(f.label)
        if name != model.sm.name and not self._exists(model, name, ds, f):
            if f.datatype in TIME_TYPES:
                grain = ax.time_grain(node)
                model.sm.dimensions.append(Dimension(name, "time", sql, f.description, f.label, grain[0] if grain else "day"))
            else:
                model.sm.dimensions.append(Dimension(name, "categorical", sql, f.description, f.label))
            model.elements.add(name)
        return _Calc("row", sql=sql)

    # ------------------------------------------------------------ parameters

    def _param_calc(self, ds: TDatasource, f: TField, model: _Model | None, stack: tuple) -> _Calc:
        """A calc that reads workbook parameters, itself or through another calc. Tiles get its SQL with each parameter as a
        bind (:name, set in the notebook's parameters cell); the semantic layer gets a TODO, as a metric can't take one."""
        clean, code, bare, _ = scan_formula(f.formula or "")
        wbp = self.params_for(ds)
        direct = [wbp[p].label if p in wbp else p for p in (_unesc(x) for x in PARAM_REF.findall(code))]
        named = ", ".join(f"[{p}]" for p in direct) or "a parameter"
        if LOD.search(bare) or "{" in bare or TABLE_CALC.search(bare) or ATTR_FN.search(bare) or any(
                _unesc(d) != "Parameters" for d, _ in DS_REF.findall(code)):
            return self._todo(ds, f, model, f"uses {named} with an LOD, table calculation, ATTR() or another datasource: write it in the "
                                            f"tile SQL with :binds from the parameters cell; see {SKILL}")
        text = PARAM_REF.sub(lambda m: f"[{PARAM_ARG}{_unesc(m.group(1)).replace(']', chr(2))}]", clean)
        try:
            node = parse_formula(text, date_opts(ds))
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"formula with {named} not translated ({exc}); see skills/translate-metrics")
        used, agg = list(direct), _has_agg(node)
        for r in ax.refs(node):
            if r.startswith(PARAM_ARG):
                continue
            g = ds.fields.get(r)
            if g is None:
                return self._todo(ds, f, model, f"references unknown field [{r}]; see skills/translate-metrics")
            if g.is_calc and not g.param:
                dep = self._calc(ds, g, stack)
                if dep.kind not in ("row", "agg") or dep.lod:
                    return self._todo(ds, f, model, f"depends on [{g.label}], which is not translated for tiles yet; finish that first "
                                                    "(skills/translate-metrics)")
                used += dep.params
                agg = agg or dep.kind == "agg"
        self.prepped[(ds.uid, f.local)] = text
        try:
            sql = self._param_sql(ds, node)
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"formula with {named} not translated ({exc}); see skills/translate-metrics")
        used = list(dict.fromkeys(used))
        msg = self._switch_todo(ds, node, agg or f.role == "measure") or (
            f"uses parameter(s) {', '.join(f'[{p}]' for p in used)}: notebook tiles compute it with "
            f"{', '.join(':' + _name(p) for p in used)} from the parameters cell; the semantic layer has no parameters, so pin the "
            f"current value here or keep it in the tile SQL; see {SKILL}")
        self._todo(ds, f, model, msg, True if agg else None)
        return _Calc("agg" if agg else "row", sql=sql, todo=msg, params=tuple(used))

    def _param_sql(self, ds: TDatasource, node: ax.Node) -> str:
        """SQL of a parameter calc (row-level or aggregate) in the current context: parameters as binds, aggregates of row SQL."""
        aggs: dict[str, str] = {}
        types: dict[str, str] = {}

        def dtype(r: str) -> str:
            return types[r] if r in types else self._dtype(ds, r)

        def lift(n: ax.Node) -> ax.Node:  # each aggregate call -> a placeholder ref holding its SQL
            if isinstance(n, ax.Call):
                if n.name in OTHER_AGGS or (n.name in TABLEAU_AGGS and len(n.args) != 1):
                    raise ax.FormulaError(f"{n.name.upper()}() is not translated")
                if n.name in TABLEAU_AGGS:
                    key, agg = f"\x03{len(aggs)}", TABLEAU_AGGS[n.name][0]
                    aggs[key] = AGG_SQL[agg].format(ax.strip_outer(self._sql(ds, n.args[0], ref, dtype)))
                    arg = tc.type_of(n.args[0], dtype)
                    types[key] = ("integer" if agg in ("count", "count_distinct") or (agg == "sum" and arg == "integer")
                                  else arg if agg in ("min", "max") else "real")
                    return ax.Ref(key)
                return ax.Call(n.name, [lift(a) for a in n.args])
            if isinstance(n, ax.BinOp):
                return ax.BinOp(n.op, lift(n.left), lift(n.right))
            if isinstance(n, ax.Unary):
                return ax.Unary(n.op, lift(n.operand))
            return n

        def ref(r: str) -> str:
            if r in aggs:
                return aggs[r]
            g = ds.fields.get(r)
            c = self.cache.get((ds.uid, g.local)) if g is not None and g.is_calc and not g.param else None
            if c is not None and c.kind == "agg":
                try:
                    return _paren(self._agg_sql(ds, g, c))  # type: ignore[arg-type]
                except _NoSQL as exc:
                    raise ax.FormulaError(str(exc)) from None
            return self._row_ref(ds, r)

        return ax.strip_outer(self._sql(ds, lift(node), ref, dtype))

    def _switch_todo(self, ds: TDatasource, node: ax.Node, measure: bool) -> str:
        """CASE [Parameter] WHEN 'a' THEN x ... END (or IF [Parameter] = 'a' THEN ...): the semantic-layer TODO lists what each
        value picks. '' when the formula is not a switch on one parameter."""
        if not isinstance(node, ax.Call) or node.name != "if" or len(node.args) < 3:
            return ""
        pairs, other = list(zip(node.args[0::2], node.args[1::2])), node.args[-1] if len(node.args) % 2 else None
        param, picks = "", []
        for cond, val in pairs:
            sides = (cond.left, cond.right) if isinstance(cond, ax.BinOp) and cond.op in ("=", "==") else ()
            p = next((x.name for x in sides if isinstance(x, ax.Ref) and x.name.startswith(PARAM_ARG)), "")
            lit = next((x for x in sides if isinstance(x, (ax.Str, ax.Num, ax.Bool))), None)
            if not p or lit is None or param not in ("", p):
                return ""
            param = p
            picks.append((ax.to_sql(lit, str), val))
        p = self.params_for(ds).get(param[1:].replace("\x02", "]"))
        if p is None:
            return ""

        def pick(val: ax.Node) -> str:
            g = ds.fields.get(val.name) if isinstance(val, ax.Ref) else None
            c = self.cache.get((ds.uid, g.local)) if g is not None and g.is_calc else None
            if c is not None and c.metric:
                return f"metric {c.metric}"
            try:
                return f"`{self._param_sql(ds, val)}`"
            except ax.FormulaError:
                return "(not translated)"

        listed = "; ".join(f"{lit} -> {pick(v)}" for lit, v in picks) + (f"; otherwise -> {pick(other)}" if other is not None else "")
        what, kind = ("measure", "metric") if measure else ("dimension", "dimension")
        return (f"{what} switch on [{p.label}]: a {kind} can't change with a parameter, so make one {kind} per value and choose it in "
                f"the notebook ({listed}); notebook tiles compute it as a case on :{_name(p.label)}; see {SKILL}")

    # ------------------------------------------------------------ level of detail expressions

    def _lods_in(self, ds: TDatasource, f: TField, model: _Model | None, clean: str, spans: list[tuple[int, int, bool]],
                 stack: tuple) -> _Calc | str:
        """LOD expressions in a formula. A formula that is one FIXED LOD becomes a column of the grain's SQL model; FIXED
        LODs inside a formula are replaced by references to their columns (returns the new formula); anything else is TODO."""
        if any(nested and _nested_fixed(clean[a:b]) is None for a, b, nested in spans):
            return self._todo(ds, f, model, "nested LOD expressions: build the inner one first (a FIXED grain as a SQL model), then the outer one; see skills/translate-metrics")
        kinds = [(m.group(1).upper() if (m := LOD.match(clean[a:b])) else "") for a, b, _ in spans]
        if "" in kinds:
            return self._todo(ds, f, model, "table-scoped LOD ({ AGG(...) } = grand total): a window over all rows (`sum(x) over ()`) in the tile SQL; see skills/translate-metrics")
        if any(LOD_PARTS.fullmatch(clean[a:b]) is None for a, b, _ in spans):
            return self._todo(ds, f, model, "LOD whose dimensions are expressions, or with no dimensions: write its grain as a SQL model "
                                            "(or `sum(x) over ()` for no dimensions) in the tile SQL; see skills/translate-metrics")
        if any(k != "FIXED" for k in kinds):
            kind = next(k for k in kinds if k != "FIXED")
            tiles = self._view_lod(ds, f, clean, spans)
            how = ("notebook tiles that show it compute it from the sheet's dimensions" if tiles
                   else "write it as window or grouped-subquery SQL in the notebook tile")
            return self._todo(ds, f, model, f"Tableau {kind} LOD depends on the view's dimensions, so it can't be a metric: {how}; see skills/translate-metrics")
        if ds.related:
            tables = self._tables_of(ds, f)
            first = next(iter((self.ds_models.get(ds.uid) or {}).values()), None)
            if len(tables) > 1 or (first is not None and tables and tables != {first.rel.name}):
                return self._todo(ds, f, model, f"FIXED LOD on a data source with relationships ({', '.join(sorted(tables))}): Tableau computes it "
                                                "over the related tables its fields come from; pre-aggregate it in a SQL model on those tables; "
                                                "see skills/translate-metrics")
        whole = len(spans) == 1 and spans[0][:2] == (0, len(clean))
        try:
            if whole:
                lod, col = self._fixed(ds, clean, _name(f.label), stack)
                return self._fixed_field(ds, f, model, lod, col)
            out, last = [], 0
            for a, b, _ in spans:
                inner = LOD_PARTS.fullmatch(clean[a:b]).group(3)  # type: ignore[union-attr]
                lod, col = self._fixed(ds, clean[a:b], _name(readable(inner, ds.fields))[:40], stack)
                ref = f"{LOD_REF}{len(self.inline)}"
                self.inline[ref] = (lod, col)
                out += [clean[last:a], f"[{ref}]"]
                last = b
            return "".join(out) + clean[last:]
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"Tableau FIXED LOD not translated ({exc}): pre-aggregate at that grain in a SQL model "
                                            "(queries/models/), then a semantic model on it; see skills/translate-metrics")

    def _rows(self, ds: TDatasource) -> tuple[str, str, bool]:
        """(FROM clause, custom SQL CTE body, joined) for all of a datasource's rows, as Tableau sees them."""
        joined = self._ds_join(ds)
        if joined:
            return joined, "", True
        models = self.ds_models.get(ds.uid) or {}
        model = next(iter(models.values()), None)
        if model is None or (len(models) > 1 and ds.join_steps):
            raise ax.FormulaError("the datasource's join can't be written as SQL")
        return (*model.from_sql, False)

    def _fixed(self, ds: TDatasource, text: str, hint: str, stack: tuple) -> tuple[_Lod, str]:
        """{FIXED [a], [b] : AGG(x)} -> (the grain's SQL model, the column holding the value)."""
        m = LOD_PARTS.fullmatch(text)
        if m is None:
            raise ax.FormulaError("not a FIXED LOD")
        dims = [_unesc(d) for d in BRACKETED.findall(m.group(2))]
        nested = _nested_fixed(text)
        node = parse_formula("1" if nested else m.group(3).strip(), date_opts(ds))
        for name in dims + ax.refs(node):
            g = ds.fields.get(name)
            if g is None:
                raise ax.FormulaError(f"unknown field [{name}]")
            if g.is_calc and not g.param:
                dep = self._calc(ds, g, stack)
                if dep.kind not in ("row", "agg") or dep.lod or dep.params:
                    raise ax.FormulaError(f"[{g.label}] is not a translated row-level field" + (" (it reads a parameter)" if dep.params else ""))
        inner: tuple[_Lod, str] | None = None
        if nested:
            inner = self._fixed(ds, nested[1], _name(readable(nested[2], ds.fields))[:40], stack)
        from_sql, cte, joined = self._rows(ds)
        prev, self._joined = self._joined, (ds if joined else None)
        try:
            keys = [(self._row_ref(ds, d), _name(ds.fields[d].label)) for d in dims]
            if inner is None:
                cl = self._classify(ds, node, lambda r: self._row_ref(ds, r), lambda r: None)
            else:
                inner_keys = [self._row_ref(ds, d) for d in inner[0].dims]
        finally:
            self._joined = prev
        if len({a for _, a in keys}) != len(keys):
            raise ax.FormulaError("two FIXED dimensions have the same name")
        if inner is not None:
            # {FIXED [a] : AGG({FIXED [b] : ...})}: Tableau takes the inner value once per distinct (a, b) of the
            # datasource's rows, then aggregates per a.
            lod_in, col_in = inner
            domain = ", ".join([f"{s} as {a}" for s, a in keys] + [f"{k} as lod_key_{j}" for j, k in enumerate(inner_keys, 1)])
            on = " and ".join(f"d.lod_key_{j} is not distinct from {lod_in.name}.{a}" for j, (_, a) in enumerate(lod_in.keys, 1))
            from_sql = (f"(\nselect distinct {domain}\nfrom {from_sql}\n) as d\n"
                        f"left join (\n{lod_in.select()}\n) as {lod_in.name} on {on}")
            keys = [(f"d.{a}", a) for _, a in keys]
            agg_sql = AGG_SQL[TABLEAU_AGGS[nested[0]][0]].format(f"{lod_in.name}.{_ident(col_in)}")  # type: ignore[index]
        elif cl.kind != "agg" or cl.agg is None or cl.agg.condition is not None:
            raise ax.FormulaError("the expression after ':' is not one aggregate")
        else:
            agg_sql = AGG_SQL[cl.agg.agg].format(cl.agg.expr)
        key = (from_sql, cte, tuple(k for k, _ in keys))
        lod = self.lods.get(key)
        if lod is None:
            taken = {mm.sm.name for mm in self.models.values()} | set(self.lod_names)
            base = "_".join(a for _, a in keys) + "_lod"
            name, n = base, 2
            while name in taken:
                name, n = f"{base}_{n}", n + 1
            lod = _Lod(name, keys, from_sql, cte, dims)
            self.lods[key] = lod
            self.lod_names[name] = lod
            if len(dims) == 1:
                self._lod_model(ds, lod, ds.fields[dims[0]])
        col = next((a for a, s in lod.cols.items() if s == agg_sql), "")
        if not col:
            used = set(lod.cols) | {a for _, a in keys}
            col, n = hint or "value", 2
            while col in used:
                col, n = f"{hint}_{n}", n + 1
            lod.cols[col] = agg_sql
        return lod, col

    def _lod_model(self, ds: TDatasource, lod: _Lod, g: TField) -> None:
        """Semantic model on a single-dimension FIXED grain, related to the model that holds the dimension."""
        owner = self._field_model(ds, g)
        if owner is None:
            return
        if g.is_calc:
            c = self.cache.get((ds.uid, g.local))
            if c is None or c.kind != "row" or c.lod:
                return
            expr = c.sql
        else:
            expr = _ident(g.remote)
        alias = lod.keys[0][1]
        primaries = {e.name for mm in self.models.values() for e in mm.sm.entities if e.type == "primary"}
        ent = alias if alias not in primaries else lod.name
        src = f"{ds.src} datasource: {ds.caption} FIXED [{g.label}]"
        sm = SemanticModel(
            name=lod.name, table=lod.name, datasource=self.b.datasource, source_ref=src,
            description=(f"Tableau FIXED level of detail at [{g.label}] in datasource {ds.caption}: one row per {g.label}, computed over all "
                         f"of the datasource's rows before dimension filters. Aggregated in a view, Tableau takes one value per distinct "
                         f"combination of the view's dimensions and {g.label}, not one per row."),
        )
        sm.entities.append(Entity(ent, "primary", "" if ent == alias else alias))
        model = _Model(f"lod:{lod.name}", TRelation(lod.name, "lod"), sm, None, pk=alias)
        self.models[model.key] = model
        self.b.semantic_models.append(sm)
        if not any(e.name == ent for e in owner.sm.entities):
            owner.sm.entities.append(Entity(ent, "foreign", "" if expr == ent else expr))
        lod.model = model

    def _fixed_field(self, ds: TDatasource, f: TField, model: _Model | None, lod: _Lod, col: str) -> _Calc:
        self.lod_fields[(ds.uid, f.local)] = (lod, col)
        lm = lod.model
        if lm is None:
            keys = ", ".join(a for _, a in lod.keys)
            msg = (f"Tableau FIXED LOD on several dimensions ({keys}): notebook tiles join it in their SQL; for the semantic layer save "
                   f"this grain as a SQL model with a key on ({keys}) and relate it by hand: {' '.join(lod.sql().split())}; see skills/translate-metrics")
            self._todo(ds, f, model, msg)
            return _Calc("lod", sql=col, lod=lod.name, todo=msg)
        src = f"{ds.src} datasource: {ds.caption} field: {f.label}"
        if f.role == "measure" and f.datatype in NUMERIC:
            name = self._grain_metric(lm, ds, f, DEFAULT_AGG.get(f.default_agg, "sum"), col, src)
            return _Calc("lod", sql=col, lod=lod.name, metric=name)
        name = _name(f.label)
        if name != lm.sm.name and not self._exists(lm, name, ds, f):
            time = f.datatype in TIME_TYPES
            lm.sm.dimensions.append(Dimension(name, "time" if time else "categorical", "" if col == name else col, f.description, f.label,
                                              "day" if time else ""))
            lm.elements.add(name)
        return _Calc("lod", sql=col, lod=lod.name)

    def _lod_row(self, ds: TDatasource, f: TField, model: _Model, lod: _Lod, node: ax.Node, sql: str) -> _Calc:
        """A row-level calc over FIXED values of one grain: a dimension of the grain's semantic model."""
        lm = lod.model
        if lm is None:
            msg = "uses a FIXED LOD on several dimensions; notebook tiles join it in their SQL"
            self._todo(ds, f, model, f"{msg}; see skills/translate-metrics")
            return _Calc("row", sql=sql, lod=lod.name, todo=msg)
        if f.role == "measure" and f.datatype in NUMERIC:
            # Tableau aggregates it once per distinct combination of the view's and the FIXED dimensions, as it does the
            # FIXED value itself: a measure on the grain's model, one row per FIXED key.
            keys = ", ".join(a for _, a in lod.keys)
            name = self._grain_metric(lm, ds, f, DEFAULT_AGG.get(f.default_agg, "sum"), sql, f"{ds.src} datasource: {ds.caption} field: {f.label}",
                                      f"Tableau aggregates it over the distinct combinations of a view's dimensions and the FIXED dimensions ({keys}), not over rows.")
            return _Calc("row", sql=sql, lod=lod.name, metric=name)
        name = _name(f.label)
        if name != lm.sm.name and not self._exists(lm, name, ds, f):
            if f.datatype in TIME_TYPES:
                grain = ax.time_grain(node)
                lm.sm.dimensions.append(Dimension(name, "time", sql, f.description, f.label, grain[0] if grain else "day"))
            else:
                lm.sm.dimensions.append(Dimension(name, "categorical", sql, f.description, f.label))
            lm.elements.add(name)
        return _Calc("row", sql=sql, lod=lod.name)

    def _lod_aggregate(self, ds: TDatasource, f: TField, model: _Model, node: ax.Node, src: str, lod: _Lod | None, mixed: bool) -> _Calc:
        """AGG({FIXED ...}) or AGG([a FIXED field]): a metric on the grain's model. Other aggregates over FIXED values: tiles or TODO."""
        hit = self._lod_arg(ds, node)
        if hit is not None and lod is not None and not mixed:
            agg = TABLEAU_AGGS[node.name][0]  # type: ignore[attr-defined]
            col = hit[1]
            lm = lod.model
            if lm is None:
                msg = "aggregates a FIXED LOD on several dimensions: notebook tiles compute it in their SQL; see skills/translate-metrics"
                self._todo(ds, f, model, msg, True)
                return _Calc("agg", lod=lod.name, todo=msg)
            name = self._grain_metric(lm, ds, f, agg, col, src, f"Tableau aggregates the FIXED values over the distinct combinations of a "
                                                                f"view's dimensions and the FIXED dimensions ({', '.join(a for _, a in lod.keys)}).")
            return _Calc("agg", metric=name, lod=lod.name)
        if mixed:
            msg = ("combines row fields and FIXED LOD values in an aggregate: notebook tiles compute row-level mixes over the rows joined "
                   "to the FIXED grain; a metric needs a SQL model with that join; see skills/translate-metrics")
            self._todo(ds, f, model, msg, True)
            return _Calc("agg", lod="mixed", todo=msg)
        msg = ("aggregates a calculation of FIXED LOD values: Tableau counts each FIXED value once per distinct combination of the "
               "view's and the FIXED dimensions, not once per row; make it a metric on the FIXED grain's semantic model; "
               "see skills/translate-metrics")
        self._todo(ds, f, model, msg, True)
        return _Calc("agg", lod=lod.name if lod else "mixed", todo=msg)

    def _home(self, ds: TDatasource, ref: str) -> str:
        """Where a formula reference lives: "" the rows, a FIXED grain's name, or "mixed"."""
        if ref in self.inline:
            return self.inline[ref][0].name
        g = ds.fields.get(ref)
        c = self.cache.get((ds.uid, g.local)) if g is not None and g.is_calc else None
        return c.lod if c is not None else ""

    def _lod_arg(self, ds: TDatasource, node: ax.Node) -> tuple[_Lod, str] | None:
        """AGG(ref) where ref is a FIXED LOD (inline or a FIXED field) -> (grain, column)."""
        if not (isinstance(node, ax.Call) and node.name in TABLEAU_AGGS and len(node.args) == 1 and isinstance(node.args[0], ax.Ref)):
            return None
        ref = node.args[0].name
        if ref in self.inline:
            return self.inline[ref]
        g = ds.fields.get(ref)
        return self.lod_fields.get((ds.uid, g.local)) if g is not None else None

    def _view_lod(self, ds: TDatasource, f: TField, clean: str, spans: list[tuple[int, int, bool]]) -> bool:
        """Remember an INCLUDE / EXCLUDE LOD that tiles can compute: the whole formula, or one aggregate around it."""
        if len(spans) != 1:
            return False
        a, b, _ = spans[0]
        m = LOD_PARTS.fullmatch(clean[a:b])
        head, tail = clean[:a].strip(), clean[b:].strip()
        outer = None
        if head or tail:
            hm = re.fullmatch(r"(\w+)\s*\(", head)
            if not hm or tail != ")" or hm.group(1).lower() not in TABLEAU_AGGS:
                return False
            outer = TABLEAU_AGGS[hm.group(1).lower()][0]
        self.view_lods[(ds.uid, f.local)] = (m.group(1).upper(), [_unesc(d) for d in BRACKETED.findall(m.group(2))], m.group(3).strip(), outer)  # type: ignore[union-attr]
        return True

    def _aggregate(self, ds: TDatasource, f: TField, model: _Model, node: ax.Node, src: str) -> _Calc:
        try:
            cl = self._classify(ds, node, lambda r: self._row_ref(ds, r), lambda r: self._metric_ref(ds, r))
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"aggregate not translated ({exc}); see skills/translate-metrics", True)
        name = self._namer(model, f.label)
        if self._exists(model, name, ds, f):
            return _Calc("agg", sql=self.metric_sql.get(name, ""), metric=name)
        kw = dict(label=f.label, description=f.description, semantic_model=model.sm.name, source_ref=src)
        if cl.kind == "agg" and cl.agg is not None:
            if cl.agg.condition is not None:
                return self._todo(ds, f, model, "conditional aggregate; see skills/translate-metrics", True)
            model.sm.measures.append(Measure(name, cl.agg.agg, cl.agg.expr, f.description, f.label))
            metric = Metric(name, measure=name, **kw)
            sql = AGG_SQL[cl.agg.agg].format(cl.agg.expr)
        elif cl.kind == "ratio" and cl.numerator is not None and cl.denominator is not None:
            num, nsql = self._side(model, cl.numerator, f"{f.label} numerator")
            den, dsql = self._side(model, cl.denominator, f"{f.label} denominator")
            metric = Metric(name, type="ratio", numerator=num, denominator=den, **kw)
            sql = f"1.0 * {nsql} / nullif({dsql}, 0)"
        else:
            metric = Metric(name, type="derived", expr=cl.expr, metrics=cl.metrics, **kw)
            names = sorted(cl.metrics, key=len, reverse=True)
            sql = re.sub(r"\b(" + "|".join(map(re.escape, names)) + r")\b", lambda m: f"({self.metric_sql.get(m.group(1), m.group(1))})", cl.expr)
        self.b.metrics.append(metric)
        self.metric_sql[name] = sql
        self._register(model, name)
        return _Calc("agg", sql=sql, metric=name)

    def _side(self, model: _Model, side: Any, hint: str) -> tuple[str, str]:
        if isinstance(side, ax.MetricRef):
            return side.name, self.metric_sql.get(side.name, side.name)
        for m in model.sm.measures:
            if m.agg == side.agg and (m.expr or m.name) == side.expr and not m.agg_params:
                return m.name, AGG_SQL[side.agg].format(side.expr)
        label = f"{side.agg} {side.expr}" if IDENT_RE.match(side.expr) else hint
        name = self._namer(model, label)
        model.sm.measures.append(Measure(name, side.agg, side.expr, f"Implied by a Tableau ratio ({hint})."))
        self.b.metrics.append(Metric(name, measure=name, semantic_model=model.sm.name, description=f"Implied by a Tableau ratio ({hint})."))
        self.metric_sql[name] = AGG_SQL[side.agg].format(side.expr)
        self._register(model, name)
        return name, self.metric_sql[name]

    def _row_filter(self, ds: TDatasource, f: TField, model: _Model | None, clean: str) -> None:
        table = model.sm.table if model else ""
        applied = any(_unbracket(fl.get("column")) == f.local for fl in ds.filters)
        predicate, problems = "", []
        groups: list[str] = []
        users = True
        try:
            node = parse_formula(clean, date_opts(ds))
            groups = [a.args[0].value for a in _find_calls(node, {"ismemberof"}) if a.args and isinstance(a.args[0], ax.Str)]
            users = bool(_find_calls(node, {"username", "fullname", "userdomain"}))
            tables = {id(m) for r in ax.refs(node) if (g := ds.fields.get(r)) is not None and (m := self._field_model(ds, g)) is not None}
            if len(tables) > 1:
                raise ax.FormulaError("it reads columns of several joined tables; write one rule per table")
            if any((g := ds.fields.get(r)) is not None and g.is_calc and not g.param and self._calc(ds, g).params for r in ax.refs(node)):
                raise ax.FormulaError("it reads a workbook parameter, which a row filter can't")
            predicate = ax.strip_outer(self._sql(ds, _replace_user_fns(node), lambda r: _user_sql(r) if r.startswith("\x00") else self._row_ref(ds, r)))
        except ax.FormulaError as exc:
            problems.append(f"not translated ({exc})")
        if groups:
            problems.append(
                f"ISMEMBEROF({', '.join(repr(g) for g in groups)}) became `'<group>' in ({{{{ user.groups }}}})`: confirm your platform expands "
                "{{ user.groups }} to a SQL list of the user's group names, and that they match the Tableau group names; see skills/translate-permissions"
            )
        if users:
            problems.append(
                "Tableau USERNAME() is the Tableau user name (the email on Tableau Cloud, often a login on Server); "
                "confirm {{ user.email }} is the right attribute; see skills/translate-permissions"
            )
        if not applied:
            problems.append("this calc was not a datasource filter; check which worksheets filter on it before enforcing")
        if model is not None and model.rel.kind == "text":
            problems.append(f"table is Tableau custom SQL ({model.sm.name}); point the rule at the table(s) behind it")
        src = f"{ds.src} datasource: {ds.caption} field: {f.label}" + (" (datasource filter)" if applied else "")
        rf = RowFilter(_name(f.label), table, predicate, ["*"], source_expression=clean, source_ref=src, todo="; ".join(problems))
        self.b.row_filters.append(rf)
        if applied and predicate and model is not None and ds.join_steps:
            self._join_filters(ds, f, model, rf, problems)

    def _join_filters(self, ds: TDatasource, f: TField, model: _Model, rf: RowFilter, notes: list[str]) -> None:
        """A datasource filter applies to the joined rows: over inner joins, a row of another table is visible only when the row it
        joins to is. One rule per joined table, as a subquery on the join keys."""
        by_rel = self.ds_models.get(ds.uid) or {}
        start = next((name for name, m in by_rel.items() if m is model), None)
        if start is None:
            return
        preds = {start: rf.predicate}
        edges = [(kind.lower(), *pairs[0]) for kind, _, pairs in ds.join_steps if len(pairs) == 1]
        queue, missed = [start], [name for _, name, pairs in ds.join_steps if len(pairs) != 1]
        while queue:
            here = queue.pop(0)
            for kind, a, b in edges:
                for (hr, hc), (tr, tc) in ((a, b), (b, a)):
                    if hr != here or tr in preds or tr not in by_rel:
                        continue
                    if kind != "inner" or by_rel[tr].rel.kind != "table" or by_rel[here].rel.kind != "table":
                        missed.append(tr)
                        continue
                    preds[tr] = f"{_ident(tc)} in (select {_ident(hc)} from {by_rel[here].sm.table} where {preds[here]})"
                    queue.append(tr)
        for rel, pred in preds.items():
            if rel == start:
                continue
            target = by_rel[rel].sm
            self.b.row_filters.append(RowFilter(
                f"{rf.name}_{target.name}", target.table, pred, ["*"], source_expression=rf.source_expression,
                source_ref=f"{rf.source_ref}, through the join to {model.sm.name}",
                todo="; ".join([f"Tableau applies this datasource filter to the joined rows, so rows of {target.name} are visible only "
                                f"through visible rows of {model.sm.name} (inner joins); this rule follows the join keys"] + notes),
            ))
        missed = sorted(set(missed) - set(preds))
        if missed:
            rf.todo += (f"; Tableau also filters {', '.join(missed)} through the datasource's joins (left or non-key joins here): "
                        "add rules for those tables")

    # ------------------------------------------------------------ worksheets -> tiles

    def _shelf(self, wb: _Workbook, text: str | None, deps: dict[tuple[str, str], ET.Element]) -> list[_Ref]:
        out: list[_Ref] = []
        for m in DS_REF.finditer(text or ""):
            dsname, inst = _unesc(m.group(1)), _unesc(m.group(2))
            ds = wb.datasources.get(dsname)
            ref = _Ref(ds, dsname, inst)
            parts = inst.split(":")
            if dsname == "Parameters":
                ref.special = "parameter"
                ref.field = wb.params.get(inst)
            elif len(parts) == 1:
                if "(generated)" in inst and ("Latitude" in inst or "Longitude" in inst):
                    ref.special = "geo"
                elif inst == "Multiple Values":
                    ref.special = "measure_values"
                ref.field = ds.fields.get(inst) if ds else None
                ref.deriv = "none"
            elif parts[0] == "":
                ref.special = "measure_names" if "Measure Names" in inst else "measure_values"
            else:
                prefix, suffix, middle = parts[0].lower(), parts[-1], parts[1:-1]
                if len(middle) >= 2 and middle[0].lower() in KNOWN_DERIV:
                    ref.table_calc, prefix, middle = prefix, middle[0].lower(), middle[1:]
                fname = ":".join(middle)
                ci = deps.get((dsname, inst))
                if ci is not None:
                    col = ci.get("column") or ""
                    if "__tableau_internal_object_id__" in col:
                        ref.special = "records"
                    elif col:
                        fname = _unbracket(col)
                    if ci.find("table-calc") is not None and not ref.table_calc:
                        ref.table_calc = "table-calc"
                    forecast = ci.get("forecast-column-type") or ""
                    if forecast == "forecast-value":  # the measure with Tableau's forecast appended: its actual values
                        ref.table_calc, ref.forecast = "", forecast
                    elif forecast:  # Forecast Indicator and the like: only exist in Tableau's forecast
                        ref.special, ref.forecast = "forecast", forecast
                if fname in ("Number of Records",) or fname.startswith("__tableau_internal_object_id__"):
                    ref.special = "records"
                ref.deriv, ref.suffix = prefix, suffix
                ref.field = ds.fields.get(fname) if ds else None
            out.append(ref)
        return out

    def _is_measure(self, r: _Ref) -> bool:
        if r.special == "records":
            return True
        if r.special:
            return False
        if r.deriv in DERIV_AGG or r.deriv in ("usr", "attr"):
            return True
        if r.field is not None and r.ds is not None and r.field.is_calc:
            c = self.cache.get((r.ds.uid, r.field.local))
            return bool(c and c.kind == "agg")
        return False

    def _is_temporal(self, r: _Ref) -> bool:
        return r.deriv in TRUNC or (r.deriv == "none" and r.field is not None and r.field.datatype in TIME_TYPES)

    def _column(self, r: _Ref) -> tuple[str, str, bool, str, str, str]:
        """-> (alias, sql, is_agg, display name, encoding type, time unit)."""
        if r.table_calc:
            label = r.field.label if r.field else r.instance
            if r.field is not None and r.ds is not None and r.field.is_calc and self._calc(r.ds, r.field).kind in ("todo", "security"):
                raise _NoSQL(f"[{label}] is not translated yet: {self._calc(r.ds, r.field).todo}")
            raise _NoSQL(f"quick table calculation ({r.table_calc}) on [{label}]: add it as window SQL (skills/translate-dashboards)")
        if r.special == "records":
            return "number_of_records", "count(*)", True, "Number of Records", "quantitative", ""
        if r.special in ("measure_names", "measure_values"):
            raise _NoSQL("Measure Names/Values: unpivot the measures in SQL (one row per measure name) and colour by measure name")
        if r.special == "parameter":
            raise _NoSQL(f"parameter [{r.field.label if r.field else r.instance}] on a shelf")
        f, ds = r.field, r.ds
        if f is None or ds is None:
            raise _NoSQL(f"unknown field {r.instance} in datasource {r.dsname}")
        label = f.label
        calc = self._calc(ds, f) if f.is_calc else None
        d = r.deriv
        if calc is not None and self._sheet is not None:
            special = self._special(ds, f, calc, d)
            if special is not None:
                self._sheet.specials[r.instance] = special
                display = DERIV_LABEL[d].format(label) if d in DERIV_AGG else label
                return _name(display), "", True, display, "quantitative", ""
        if calc is not None and calc.kind in ("todo", "security"):
            raise _NoSQL(f"[{label}] is not translated yet: {calc.todo}")
        try:
            if calc is not None and calc.kind == "lod":
                if d in DERIV_AGG or d == "usr":
                    raise _NoSQL(f"[{label}] (a FIXED LOD) aggregated outside a sheet")
                base = self._row_ref(ds, f.local)  # the FIXED value joined to each row
            elif calc is not None and calc.kind == "row":
                if calc.lod not in ("", "mixed") and (d in DERIV_AGG or d == "usr"):
                    raise _NoSQL(f"[{label}] aggregates a calculation of FIXED LOD values: Tableau counts each FIXED value once per "
                                 "distinct combination of the view's and the FIXED dimensions, not once per row; write it in the tile "
                                 "SQL (skills/source-tableau)")
                base = self._row_sql(ds, f) if self._sheet is not None and (calc.lod or self._is_joined(ds)) else calc.sql
            else:
                base = _ident(f.remote) if f.remote else ""
                if base and self._is_joined(ds) and f.relation:
                    base = f"{_ident(f.relation)}.{base}"
        except ax.FormulaError as exc:
            raise _NoSQL(f"[{label}] not translated for this sheet ({exc})") from None
        if calc is None and not base:
            raise _NoSQL(f"[{label}] has no physical column")
        if d == "usr" or (calc is not None and calc.kind == "agg"):
            if calc is not None and calc.kind == "agg":
                sql = self._agg_sql(ds, f, calc)
            else:
                sql = AGG_SQL[DEFAULT_AGG.get(f.default_agg, "sum")].format(base)
            etype = "nominal" if f.datatype in ("string", "boolean") else "quantitative"
            return _name(label), sql, True, label, etype, ""
        if d in DERIV_AGG:
            display = DERIV_LABEL[d].format(label)
            return _name(display), AGG_SQL[DERIV_AGG[d]].format(base), True, display, "quantitative", ""
        if d == "attr":
            raise _NoSQL(f"ATTR([{label}]) on a shelf: group by the dimension or use min()/max()")
        if d in TRUNC or d in PART:
            unit = TRUNC.get(d) or PART[d]
            display = f"{UNIT_LABEL[unit]} of {label}"
            try:  # weeks, quarters and years follow the datasource's Date Properties
                sql = date_sql("trunc" if d in TRUNC else "part", "weekday" if unit == "dow" else unit, base, date_opts(ds)) if unit in DATE_UNITS + ("dow",) else None
            except ax.FormulaError as exc:
                raise _NoSQL(f"{display}: {exc}") from None
            if d in TRUNC:
                return _name(display), sql or f"date_trunc('{unit}', {base})", False, display, "temporal", unit
            return _name(display), sql or f"extract({unit} from {base})", False, display, "ordinal", ""
        if d == "none" or not d:
            if f.datatype in TIME_TYPES:
                etype = "temporal"
            elif f.datatype in NUMERIC and r.suffix == "qk":
                etype = "quantitative"
            elif r.suffix == "ok":
                etype = "ordinal"
            else:
                etype = "nominal"
            return _name(label), base, False, label, etype, ""
        raise _NoSQL(f"derivation {d!r} on [{label}] not supported")

    def _special(self, ds: TDatasource, f: TField, calc: _Calc, d: str) -> _Special | None:
        """How a sheet computes an aggregated LOD (None: an ordinary column)."""
        key = (ds.uid, f.local)
        if calc.kind == "lod":
            if d in DERIV_AGG and key in self.lod_fields:
                lod, col = self.lod_fields[key]
                return _Special("fixed", DERIV_AGG[d], lod, col)
            return None
        if calc.kind == "row" and calc.lod not in ("", "mixed") and (d in DERIV_AGG or d == "usr"):
            # A row calc over one grain's FIXED values, aggregated: Tableau takes one value per distinct combination of the
            # view's and the FIXED dimensions, as for the FIXED value itself.
            outer = DERIV_AGG[d] if d in DERIV_AGG else DEFAULT_AGG.get(f.default_agg, "sum")
            return _Special("fixed", outer, self.lod_names[calc.lod], value=self._grain_sql(ds, lambda: self._row_sql(ds, f)))
        if calc.kind == "agg" and calc.lod:
            try:
                node: ax.Node | None = parse_formula(self.prepped.get(key) or scan_formula(f.formula or "")[0], date_opts(ds))
                hit = self._lod_arg(ds, node)  # type: ignore[arg-type]
            except ax.FormulaError:
                hit, node = None, None
            if hit is not None and isinstance(node, ax.Call):
                return _Special("fixed", TABLEAU_AGGS[node.name][0], hit[0], hit[1])
            if node is not None:
                expr = self._lod_expr(ds, f, node)
                if expr is not None:
                    return expr
            if calc.lod == "mixed":
                return None  # an aggregate over the rows joined to the FIXED grain: _agg_sql
            raise _NoSQL(f"[{f.label}] is not translated yet: {calc.todo}")
        view = self.view_lods.get(key)
        if calc.kind != "todo" or view is None:
            return None
        kind, dims, inner, outer = view
        if outer is None:
            if d not in DERIV_AGG:
                raise _NoSQL(f"[{f.label}] ({kind} LOD) on a shelf without an aggregation")
            outer = DERIV_AGG[d]
        try:
            cl = self._classify(ds, parse_formula(inner, date_opts(ds)), lambda r: self._row_ref(ds, r), lambda r: None)
            if cl.kind != "agg" or cl.agg is None or cl.agg.condition is not None:
                raise ax.FormulaError("the expression after ':' is not one aggregate")
            inner_sql = AGG_SQL[cl.agg.agg].format(cl.agg.expr)
            if kind == "INCLUDE":
                return _Special("include", outer, inner=inner_sql, dims=[self._row_ref(ds, x) for x in dims])
        except ax.FormulaError as exc:
            raise _NoSQL(f"[{f.label}] ({kind} LOD) not translated for this sheet ({exc})") from None
        if outer not in ("sum", "average", "min", "max", "median"):
            raise _NoSQL(f"[{f.label}]: {outer} of an EXCLUDE LOD; write it in the tile SQL")
        locals_ = []
        for x in dims:
            g = ds.fields.get(x) or next((h for h in ds.fields.values() if h.caption == x), None)
            if g is None:
                raise _NoSQL(f"[{f.label}] excludes unknown field [{x}]")
            locals_.append(g.local)
        return _Special("exclude", outer, inner=inner_sql, dims=locals_)

    def _grain_sql(self, ds: TDatasource, render: Any) -> str:
        """SQL over a FIXED grain's columns, qualified with the grain's name, without joining the grain to the sheet's rows."""
        ctx = self._sheet
        self._sheet = _SheetCtx(ctx.ds if ctx is not None else ds)
        try:
            return render()
        except ax.FormulaError as exc:
            raise _NoSQL(f"FIXED values not translated for this sheet ({exc})") from None
        finally:
            self._sheet = ctx

    def _lod_expr(self, ds: TDatasource, f: TField, node: ax.Node) -> _Special | None:
        """An aggregate expression with aggregates of FIXED values inside (SUM([Sales]) / SUM([Account Revenue])): each
        aggregate at its own level (over the sheet's rows, or once per distinct view and FIXED dimensions), then the
        expression over them per mark. None if it has no such aggregate."""
        parts: list[tuple[str, Any]] = []

        def part(call: ax.Call) -> ax.Node:
            if call.name not in TABLEAU_AGGS or len(call.args) != 1 or TABLEAU_AGGS[call.name][2] is not None:
                raise _NoSQL(f"[{f.label}]: {call.name.upper()}() in an expression with FIXED aggregates; write it in the tile SQL")
            homes = {self._home(ds, r) for r in ax.refs(call)}
            agg = TABLEAU_AGGS[call.name][0]
            if len(homes) == 1 and not homes & {"", "mixed"}:
                lod = self.lod_names[next(iter(homes))]
                value = self._grain_sql(ds, lambda: ax.strip_outer(self._sql(ds, call.args[0], lambda r: self._row_ref(ds, r))))
                parts.append(("fixed", _Special("fixed", agg, lod, value=value)))
            else:
                try:
                    parts.append(("row", AGG_SQL[agg].format(ax.strip_outer(self._sql(ds, call.args[0], lambda r: self._row_ref(ds, r))))))
                except ax.FormulaError as exc:
                    raise _NoSQL(f"[{f.label}] not translated for this sheet ({exc})") from None
            return ax.Ref(f"{LOD_REF}part_{len(parts) - 1}")

        def walk(n: ax.Node) -> ax.Node:
            if isinstance(n, ax.Call) and n.name in TABLEAU_AGGS:
                return part(n)
            if isinstance(n, ax.Call):
                return ax.Call(n.name, [walk(a) for a in n.args])
            if isinstance(n, ax.BinOp):
                return ax.BinOp(n.op, walk(n.left), walk(n.right))
            if isinstance(n, ax.Unary):
                return ax.Unary(n.op, walk(n.operand))
            if isinstance(n, ax.Ref):
                raise _NoSQL(f"[{f.label}]: a field outside an aggregate in an expression with FIXED aggregates; write it in the tile SQL")
            return n

        shape = walk(node)
        if not any(kind == "fixed" for kind, _ in parts):
            return None
        try:
            expr = ax.strip_outer(self._sql(ds, shape, lambda r: "{" + r[len(LOD_REF) + 5:] + "}"))
        except ax.FormulaError as exc:
            raise _NoSQL(f"[{f.label}] not translated for this sheet ({exc})") from None
        return _Special("expr", "", expr=expr, parts=parts)

    def _agg_sql(self, ds: TDatasource, f: TField, calc: _Calc) -> str:
        """An aggregate calc's SQL in this sheet: re-derived when the sheet's columns are qualified or FIXED values are joined."""
        if calc.params:
            try:
                return self._param_sql(ds, parse_formula(self.prepped[(ds.uid, f.local)], date_opts(ds)))
            except ax.FormulaError as exc:
                raise _NoSQL(f"[{f.label}] not translated for this sheet ({exc})") from None
        if self._sheet is None or not (calc.lod or self._is_joined(ds)):
            if calc.lod:
                raise _NoSQL(f"[{f.label}] is not translated yet: {calc.todo}")
            return calc.sql
        text = self.prepped.get((ds.uid, f.local)) or scan_formula(f.formula or "")[0]
        deps: dict[str, TField] = {}

        def metric_ref(r: str) -> str | None:
            g = ds.fields.get(r)
            c = self.cache.get((ds.uid, g.local)) if g is not None and g.is_calc else None
            if g is None or c is None or c.kind != "agg":
                return None
            token = f"unshackle_metric_{len(deps)}"
            deps[token] = g
            return token

        def dep_sql(token: str) -> str:
            g = deps[token]
            return _paren(self._agg_sql(ds, g, self.cache[(ds.uid, g.local)]))

        def side(x: Any) -> str:
            if isinstance(x, ax.MetricRef):
                return dep_sql(x.name)
            if x.condition is not None:
                raise ax.FormulaError("conditional aggregate")
            return AGG_SQL[x.agg].format(x.expr)

        try:
            node = parse_formula(text, date_opts(ds))
            for call in _find_calls(node, set(TABLEAU_AGGS)):
                homes = {self._home(ds, r) for r in ax.refs(call)}
                if homes - {"", "mixed"} and not homes & {"", "mixed"}:
                    raise ax.FormulaError("an aggregate of FIXED LOD values inside a larger expression; write it in the tile SQL")
            cl = self._classify(ds, node, lambda r: self._row_ref(ds, r), metric_ref)
            if cl.kind == "agg" and cl.agg is not None:
                return side(cl.agg)
            if cl.kind == "ratio":
                return f"1.0 * {side(cl.numerator)} / nullif({side(cl.denominator)}, 0)"
            return re.sub(r"\bunshackle_metric_\d+\b", lambda m: dep_sql(m.group(0)), cl.expr)
        except (ax.FormulaError, KeyError) as exc:
            raise _NoSQL(f"[{f.label}] not translated for this sheet ({exc})") from None

    def _tile(self, wb: _Workbook, ws: ET.Element) -> tuple[Tile, dict[str, Any]]:
        name = ws.get("name") or "sheet"
        tid = slugify(name)
        meta: dict[str, Any] = {"params": [], "filters": {}}
        table = ws.find("table")
        view = table.find("view") if table is not None else None
        if table is None or view is None:
            return Tile(tid, name, "unknown", "unknown", todo="worksheet has no <table>/<view> in the XML"), meta
        todos: list[str] = []
        deps: dict[tuple[str, str], ET.Element] = {}
        for dd in view.findall("datasource-dependencies"):
            dsn = dd.get("datasource", "")
            if dsn == "Parameters":
                meta["params"] += [_unbracket(c.get("name")) for c in dd.findall("column")]
                continue
            for ci in dd.findall("column-instance"):
                deps[(dsn, _unbracket(ci.get("name")))] = ci
        data_ds = [d.get("name", "") for d in view.findall("datasources/datasource") if d.get("name") != "Parameters"]
        rows = self._shelf(wb, table.findtext("rows"), deps)
        cols = self._shelf(wb, table.findtext("cols"), deps)
        panes = table.findall("panes/pane")
        mark_el = panes[0].find("mark") if panes else None
        mark_cls = (mark_el.get("class") if mark_el is not None else "") or "Automatic"
        enc: dict[str, list[_Ref]] = {}
        for pane in panes:
            e = pane.find("encodings")
            for child in (e if e is not None else []):
                for ref in self._shelf(wb, child.get("column"), deps):
                    if ref.special == "forecast":
                        continue
                    lst = enc.setdefault(_local(child.tag), [])
                    if all(x.instance != ref.instance for x in lst):
                        lst.append(ref)
        dual = len(panes) > 1 and len({(p.find("mark").get("class") if p.find("mark") is not None else "") for p in panes}) > 1
        pane_marks: dict[str, str] = {}  # measure instance -> its pane's mark class (dual axis)
        for pane in panes:
            pm = pane.find("mark")
            for ref in self._shelf(wb, pane.get("y-axis-name") or pane.get("x-axis-name"), deps):
                if pm is not None:
                    pane_marks[ref.instance] = (pm.get("class") or "").lower()

        def split(refs: list[_Ref]) -> tuple[list[_Ref], list[_Ref]]:
            return [r for r in refs if not self._is_measure(r) and r.special not in ("geo",)], [r for r in refs if self._is_measure(r)]

        row_d, row_m = split(rows)
        col_d, col_m = split(cols)
        geo = any(r.special == "geo" for r in rows + cols)
        cls = mark_cls.lower()
        if cls in MAP_MARKS or geo:
            kind, mark = "unknown", "unknown"
            todos.append(f"Tableau map ({mark_cls}, generated latitude/longitude): no Altair mark in the notebook target; keep the SQL and choose a bar/table, or link to the source view ({SKILL})")
        elif cls == "text":
            kind, mark = ("kpi", "text") if not (row_d or col_d) else ("table", "table")
        elif cls == "square":
            kind, mark = "unknown", "unknown"
            todos.append("Tableau square marks (heatmap/highlight table): use mark_rect() with x, y and color (skills/translate-dashboards)")
        elif cls in MARKS:
            kind, mark = MARKS[cls]
            if cls == "ganttbar":
                todos.append("Gantt bar: use mark_bar() with x/x2 (start/end) in the chart cell")
        else:
            dims, meas = row_d + col_d, row_m + col_m
            if not meas and not dims:
                kind, mark = ("kpi", "text") if any(self._is_measure(r) for r in enc.get("text", [])) else ("unknown", "unknown")
            elif not meas:
                kind, mark = "table", "table"
            elif not dims:
                kind, mark = ("chart", "point") if row_m and col_m else ("kpi", "text")
            elif any(self._is_temporal(r) and r.suffix == "qk" for r in dims) or any(r.deriv in TRUNC for r in dims):
                kind, mark = "chart", "line"
            else:
                kind, mark = "chart", "bar"
        chosen: dict[str, _Ref] = {}
        layer_refs: list[tuple[str, str, _Ref]] = []  # (mark, channel, measure) per series
        compose = "layer"

        def put(ch: str, r: _Ref | None) -> None:
            if r is not None and ch not in chosen:
                chosen[ch] = r

        all_meas = row_m + col_m + [r for rs in enc.values() for r in rs if self._is_measure(r)]
        if mark == "arc":
            put("theta", (enc.get("wedge-size") or all_meas or [None])[0])
            put("color", next((r for r in enc.get("color", []) + row_d + col_d if not self._is_measure(r)), None))
        elif kind == "kpi":
            put("text", next((r for r in enc.get("text", []) + all_meas if self._is_measure(r)), None))
            if len({r.instance for r in all_meas}) > 1:
                todos.append("several measures in one text sheet: one KPI tile per measure")
        elif kind != "table":
            horizontal = bool(row_d and col_m and not col_d)
            if mark == "point" and row_m and col_m:
                put("x", col_m[0])
                put("y", row_m[0])
            elif horizontal:
                put("x", col_m[0])
                put("y", row_d[-1])
            else:
                put("x", (col_d[-1:] or col_m or [None])[0])
                put("y", (row_m or row_d or [None])[0])
            # Outer dimensions on the shelves are Tableau's panes: row/column facets (trellis).
            used = {r.instance for r in chosen.values()}
            for ch, shelf in (("row", row_d), ("column", col_d)):
                put(ch, next((r for r in shelf if r.instance not in used), None))
            used = {r.instance for r in chosen.values()}
            extra = [r for r in row_d + col_d if r.instance not in used]
            if extra and not enc.get("color"):
                put("color", extra.pop(0))
            if extra:
                todos.append(f"nested dimensions on the shelves ({', '.join(x.instance for x in extra)}): more than one facet per shelf; concatenate them into one column in SQL or use xOffset/detail")
            axis = "y" if len(row_m) > 1 else "x" if len(col_m) > 1 else ""
            series = row_m if axis == "y" else col_m
            if axis and chosen.get(axis) is series[0]:
                # Several measures on one shelf: Tableau draws one pane per measure (or overlays them on a dual axis).
                del chosen[axis]
                layer_refs = [(MARKS.get(pane_marks.get(r.instance, cls), ("chart", mark))[1], axis, r) for r in series]
                compose = ("dual_axis" if axis == "y" else "layer") if dual else ("vconcat" if axis == "y" else "hconcat")
                if dual and axis == "x":
                    todos.append("dual axis on columns: add .resolve_scale(x='independent') to the layered chart")
            elif axis:
                todos.append("several measures on one axis: only the first is encoded; add the others as layers (skills/translate-dashboards)")
            elif dual:
                todos.append("combined marks on one axis: set each layer's mark in the chart cell (skills/translate-dashboards)")
            for ch in ("color", "size"):
                put(ch, (enc.get(ch) or [None])[0])
            put("detail", (enc.get("lod") or [None])[0])
        tooltip = [r for r in enc.get("tooltip", []) if r.instance not in {x.instance for x in chosen.values()}]
        if tooltip:
            put("tooltip", tooltip[0])

        # ---- SQL
        ordered: list[tuple[_Ref, bool]] = []  # (ref, critical)
        seen: set[str] = set()
        for r in rows + cols + [r for ch in ("color", "size", "text", "wedge-size", "lod", "shape", "path") for r in enc.get(ch, [])]:
            if r.instance not in seen and r.special != "geo":
                ordered.append((r, True))
                seen.add(r.instance)
        for r in enc.get("tooltip", []):
            if r.instance not in seen:
                ordered.append((r, False))
                seen.add(r.instance)
        tile_ds = wb.datasources.get(data_ds[0]) if len(data_ds) == 1 else None
        rel_tile = len(self._related_tables(tile_ds, ordered)) > 1  # fields of several logical tables of a data source with relationships
        join_from, join_todo = self._tile_join(tile_ds, ordered)
        if join_todo:
            todos.append(join_todo)
        qualify_one = False  # one table, its columns qualified (FIXED grains are joined to it)

        sorts = sheet_sorts(view, tile_ds.default_sorts if tile_ds is not None else [])

        def build() -> tuple[list[str], list, set[str], list[str], list[str], list[str], list[str], str]:
            self._sheet = _SheetCtx(tile_ds, sorts=sorts)
            self._joined = tile_ds if (join_from or qualify_one or rel_tile) else None
            problems, columns, dropped, notes = [], [], set(), []
            for r, critical in ordered:
                try:
                    alias, sql, is_agg, display, etype, unit = self._rel_column(r) if rel_tile else self._column(r)
                except _NoSQL as exc:
                    if critical:
                        problems.append(str(exc))
                    else:
                        notes.append(f"tooltip field left out: {exc}")
                        dropped.add(r.instance)
                    continue
                columns.append((r, alias, sql, is_agg, display, etype, unit))
            notes += self._aliases(columns) + self._sort_aggregates(wb, columns, deps)
            where, having, order, limit = [], [], [], ""
            for flt in view.findall("filter"):
                res = self._filter(wb, flt, deps, meta)
                if res is None:
                    continue
                k, v = res
                if k == "where":
                    where.append(v)
                elif k == "having":
                    having.append(v)
                elif k == "top":
                    order, limit = [v[0]], v[1]
                else:
                    notes.append(v)
            return problems, columns, dropped, notes, where, having, order, limit

        problems, columns, dropped, notes, where, having, order, limit = build()
        uses_lod = bool(self._sheet.row_lods) or any(sp.kind == "fixed" for sp in self._sheet.specials.values())  # type: ignore[union-attr]
        if uses_lod and not join_from and tile_ds is not None and not rel_tile:
            qualify_one = True
            problems, columns, dropped, notes, where, having, order, limit = build()
        todos += notes
        if (uses_lod or self._sheet.specials) and any(flt.get("context") == "true" for flt in view.findall("filter")):  # type: ignore[union-attr]
            problems.append("context filters: Tableau applies them before FIXED LODs; add them inside the LOD subqueries of the tile SQL")
        aliases: dict[str, str] = {}
        taken: set[str] = set()
        for i, (r, alias, *rest) in enumerate(columns):
            a, n = alias, 2
            while a in taken:
                a, n = f"{alias}_{n}", n + 1
            taken.add(a)
            aliases[r.instance] = a
            columns[i] = (r, a, *rest)
        models = {id(m): m for r, *_ in columns if r.ds and r.field and (m := self._field_model(r.ds, r.field)) is not None}
        if not models and columns and data_ds and data_ds[0] in wb.datasources:
            first = next(iter((self.ds_models.get(wb.datasources[data_ds[0]].uid) or {}).values()), None)
            if first is not None:
                models = {id(first): first}
        if len(data_ds) > 1:
            problems.append(f"blends {len(data_ds)} datasources ({', '.join(data_ds)}): join them in the tile SQL ({SKILL})")
        elif len(models) > 1 and not join_from and not rel_tile:
            problems.append("fields come from several joined tables: write the join in the tile SQL")
        sql = totals_sql = ""
        if problems:
            todos += problems
        elif rel_tile and tile_ds is not None:
            columns = [c for c in columns if not c[3]] + [c for c in columns if c[3]]
            try:
                sql = self._rel_tile_sql(tile_ds, columns, where, having, order, limit)
            except _NoSQL as exc:
                todos.append(str(exc))
        elif columns and models:
            model = next(iter(models.values()))
            from_sql, cte = (join_from, "") if join_from else model.from_sql
            if qualify_one:
                from_sql = f"{from_sql} as {_ident(model.rel.name)}"
            dims = [c for c in columns if not c[3]]
            columns = dims + [c for c in columns if c[3]]
            totals, note = self._totals(name, kind, table, rows, cols, columns, bool(having or limit))
            try:
                sql = self._tile_sql(columns, from_sql, cte, where, having, order, limit)
                if totals:  # Tableau's Automatic totals; Total Using isn't in the file, so they stay a TODO with this SQL
                    totals_sql = self._tile_sql(columns, from_sql, cte, where, having, order, limit, totals)
                    if self.dialect in dialect.DIALECTS:
                        keep = tuple(r.sql for r in (tile_ds.relations.values() if tile_ds else []) if r.kind == "text" and r.sql)
                        try:
                            totals_sql = dialect.render(totals_sql, self.dialect, keep)
                        except dialect.Unsupported:
                            pass
                    note += f": {totals_sql}"
            except _NoSQL as exc:
                todos.append(str(exc))
            if note:
                todos.append(note)
            todos += self._sheet.notes  # type: ignore[union-attr]
            if cte and ("<[Parameters]" in cte or PARAM_REF.search(cte)):
                todos.append("custom SQL uses Tableau parameters (<[Parameters].[x]>): replace them with :binds from the parameters cell")
        elif not columns and not problems:
            todos.append("no fields on the sheet's shelves")
        by_inst = {c[0].instance: c for c in columns}
        styles = {(rule.get("element", ""), _unesc(ref[0][1])): fm.get("value", "")  # the sheet's Format > Numbers
                  for rule in table.findall("style/style-rule") for fm in rule.findall("format")
                  if fm.get("attr") == "text-format" and (ref := DS_REF.findall(fm.get("field", "")))}

        def number_format(r: _Ref, channel: str) -> str:
            """The sheet's format for the field (axis, or marks and tooltips), else the field's default; counts have none."""
            own = styles.get(("axis" if channel in ("x", "y") else "cell", r.instance))
            if own or r.field is None or r.deriv in ("cnt", "ctd"):
                return own or ""
            return r.field.format

        def to_encoding(r: _Ref, channel: str) -> Encoding | None:
            c = by_inst.get(r.instance)
            if c is not None:
                return Encoding(c[1], c[5], title=c[4], time_unit="" if sql else ALTAIR_UNIT.get(c[6], ""),  # type: ignore[arg-type]
                                sort="none" if sql and not c[3] and r.instance in sorts else "", format=d3_format(number_format(r, channel)))
            if r.instance in dropped:
                return None
            try:
                alias, _, _, display, etype, unit = self._column(r)
            except _NoSQL:
                label = r.field.label if r.field else r.instance
                alias, display, etype, unit = _name(label), label, "quantitative" if self._is_measure(r) else "nominal", ""
            return Encoding(alias, etype, title=display, time_unit=ALTAIR_UNIT.get(unit, ""), format=d3_format(number_format(r, channel)))  # type: ignore[arg-type]

        encodings = {ch: e for ch, r in chosen.items() if (e := to_encoding(r, ch)) is not None}
        layers = [Layer(m, {ch: e}) for m, ch, r in layer_refs if (e := to_encoding(r, ch)) is not None]  # type: ignore[arg-type]
        sql, references = self._analytics(wb, name, table, panes, deps, rows, cols, columns, chosen, layer_refs, sql, todos)
        meta["params"] += [k for k, pf in wb.params.items() if k not in meta["params"] and re.search(rf"(?<!:):{_name(pf.label)}\b", sql)]
        for p in meta["params"]:
            pf = wb.params.get(p)
            if pf is not None and not re.search(rf":{_name(pf.label)}\b", sql):  # already bound by a filter: nothing left to do
                todos.append(f"uses parameter [{pf.label}] (:{_name(pf.label)} in the parameters cell)")
        config: dict[str, Any] = {
            "workbook": wb.id,
            "worksheet": name,
            "datasource": ", ".join((wb.datasources[d].caption if d in wb.datasources else d) for d in data_ds),
            "mark": mark_cls,
            "rows": (table.findtext("rows") or "").strip(),
            "cols": (table.findtext("cols") or "").strip(),
            "columns": {c[1]: c[4] for c in columns},
        }
        if totals_sql:
            config["totals_sql"] = totals_sql
        formats = {c[1]: fmt for c in columns if (fmt := number_format(c[0], "text"))}
        if formats:
            config["formats"] = formats  # Tableau number formats of the columns (marks, tooltips, tables)
        title = _text(ws.find("layout-options/title"))
        if title and title != "<Sheet Name>":
            config["title"] = title.replace("<Sheet Name>", name)
        v = self.view_index.get((wb.id, name))
        if v and v.get("id"):
            config["view_id"] = v["id"]
            rel = f"views/{v['id']}.csv"
            if self.store.exists(rel):
                config["parity_csv"] = f"migration/raw/tableau/{rel}"
                try:
                    header = next(csv.reader(io.StringIO(self.store.path(rel).read_text(encoding="utf-8-sig"))), [])
                except (OSError, UnicodeDecodeError):
                    header = []
                config["csv_columns"] = header
        self._joined = None
        self._sheet = None
        tile = Tile(tid, name, kind, mark, sql, encodings, source_type=mark_cls, source_config=config,  # type: ignore[arg-type]
                    todo="; ".join(dict.fromkeys(todos)), layers=layers, compose=compose, references=references)  # type: ignore[arg-type]
        meta["aliases"] = aliases
        return tile, meta

    # ------------------------------------------------------------ relationships (the logical layer)

    def _rel_column(self, r: _Ref) -> tuple[str, str, bool, str, str, str]:
        """_column() for a sheet over related tables: an aggregate calculation over several of them is split into one
        aggregate per table (placed by _rel_tile_sql); anything else is an ordinary column, qualified with its table."""
        f, ds = r.field, r.ds
        if f is None or ds is None or not f.is_calc or len(self._tables_of(ds, f)) < 2 or r.deriv not in ("usr", ""):
            return self._column(r)
        parts: list[tuple[str, str]] = []

        def walk(n: ax.Node, g: TField) -> ax.Node:
            if isinstance(n, ax.Call) and n.name in TABLEAU_AGGS:
                agg, _, cond = TABLEAU_AGGS[n.name]
                tables = {t for x in ax.refs(n) if (h := ds.fields.get(x)) is not None for t in self._tables_of(ds, h)}
                if len(n.args) != 1 or cond is not None or len(tables) != 1:
                    raise _NoSQL(f"[{f.label}]: {n.name.upper()}() over fields of several related tables ({', '.join(sorted(tables))}): Tableau "
                                 f"computes it over those tables joined by their relationship; write it in the tile SQL ({SKILL})")
                try:
                    parts.append((next(iter(tables)), AGG_SQL[agg].format(ax.strip_outer(self._sql(ds, n.args[0], lambda x: self._row_ref(ds, x))))))
                except ax.FormulaError as exc:
                    raise _NoSQL(f"[{f.label}] not translated for this sheet ({exc})") from None
                return ax.Ref(f"{LOD_REF}part_{len(parts) - 1}")
            if isinstance(n, ax.Call):
                return ax.Call(n.name, [walk(a, g) for a in n.args])
            if isinstance(n, ax.BinOp):
                return ax.BinOp(n.op, walk(n.left, g), walk(n.right, g))
            if isinstance(n, ax.Unary):
                return ax.Unary(n.op, walk(n.operand, g))
            if isinstance(n, ax.Ref):
                h = ds.fields.get(n.name)
                if h is not None and h.formula is not None and h is not g and (c := self._calc(ds, h)).kind in ("agg", "todo"):
                    return walk(self._rel_formula(ds, h), h)  # an aggregate calculation inside: its aggregates are placed too
                raise _NoSQL(f"[{f.label}]: a row-level field outside an aggregate, over related tables; write it in the tile SQL ({SKILL})")
            return n

        shape = walk(self._rel_formula(ds, f), f)
        try:
            expr = ax.strip_outer(self._sql(ds, shape, lambda x: "{" + x[len(LOD_REF) + 5:] + "}"))
        except ax.FormulaError as exc:
            raise _NoSQL(f"[{f.label}] not translated for this sheet ({exc})") from None
        assert self._sheet is not None
        self._sheet.rel_parts[r.instance] = (expr, parts)
        return _name(f.label), "", True, f.label, "quantitative", ""

    def _rel_formula(self, ds: TDatasource, f: TField) -> ax.Node:
        clean, code, bare, _ = scan_formula(f.formula or "")
        if lod_spans(clean) or TABLE_CALC.search(bare) or PARAM_REF.search(code) or USER_FN.search(bare):
            raise _NoSQL(f"[{f.label}] over related tables uses LODs, table calculations, parameters or user functions; write it in the tile SQL ({SKILL})")
        try:
            return parse_formula(clean, date_opts(ds))
        except ax.FormulaError as exc:
            raise _NoSQL(f"[{f.label}] not translated ({exc})") from None

    def _rel_edges(self, ds: TDatasource) -> dict[str, list[tuple[str, list[tuple[str, str]]]]]:
        """Relationships between physical relations: relation -> [(related relation, [(its column, related column)])].
        Only relationships whose clause is column equalities between two single-table logical tables."""
        edges: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {}
        for rs in ds.relationships:
            a, b = ds.objects.get(rs.first), ds.objects.get(rs.second)
            if a is None or b is None or a.join or b.join or len(a.relations) != 1 or len(b.relations) != 1 or not rs.pairs:
                continue
            ra, rb = a.relations[0], b.relations[0]
            cols: list[tuple[str, str]] = []
            for x, y in rs.pairs:
                fx, fy = ds.fields.get(x), ds.fields.get(y)
                if fx is None or fy is None or not fx.remote or not fy.remote:
                    break
                if (fx.relation, fy.relation) == (rb, ra):
                    fx, fy = fy, fx
                if (fx.relation, fy.relation) != (ra, rb):
                    break
                cols.append((fx.remote, fy.remote))
            else:
                edges.setdefault(ra, []).append((rb, cols))
                edges.setdefault(rb, []).append((ra, [(y, x) for x, y in cols]))
        return edges

    def _rel_tree(self, ds: TDatasource, root: str, targets: set[str]) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """The relationships that connect `root` to `targets`, as (from, to, [(from column, to column)]) away from root, in an
        order that joins each table after the one it hangs from."""
        edges = self._rel_edges(ds)
        parent: dict[str, tuple[str, list[tuple[str, str]]]] = {}
        order, seen = [root], {root}
        for node in order:
            for nxt, cols in edges.get(node, []):
                if nxt not in seen:
                    seen.add(nxt)
                    parent[nxt] = (node, cols)
                    order.append(nxt)
        need: set[str] = set()
        for t in targets - {root}:
            if t not in seen:
                raise _NoSQL(f"no relationship unshackle can write links {root} and {t} (a clause that is not column equalities, or "
                             f"a logical table that joins tables): write the tile SQL by hand ({SKILL})")
            while t != root:
                need.add(t)
                t = parent[t][0]
        return [(parent[t][0], t, parent[t][1]) for t in order if t in need]

    def _rel_from(self, ds: TDatasource, rel: str) -> str:
        obj = ds.object_of(rel)
        if obj is not None and (obj.join or len(obj.relations) != 1):
            raise _NoSQL(f"logical table {obj.caption} joins tables ({', '.join(obj.relations)}): Tableau computes its fields over the joined "
                         f"rows, then relates it to the others; write the tile SQL by hand ({SKILL})")
        model = (self.ds_models.get(ds.uid) or {}).get(rel)
        if model is None:
            raise _NoSQL(f"table {rel} has no model")
        if model.rel.kind == "text":
            return f"(\n{model.rel.sql}\n) as {_ident(rel)}"
        return f"{model.sm.table} as {_ident(rel)}"

    def _rel_tile_sql(self, ds: TDatasource, columns: list, where: list[str], having: list[str], order: list[str], limit: str) -> str:
        """A sheet over related logical tables, as Tableau computes it with relationships:
        - each measure is aggregated on its own table's rows at the sheet's dimensions. A row counts once per distinct
          combination of the dimensions it relates to: the table is joined to the distinct (its relationship columns,
          dimensions of the other tables) reached through left joins, so rows with no match get NULL dimensions;
        - the dimensions' own combinations (the dimension tables inner joined) are marks too, with NULL measures when no row
          relates to them, and a sheet without measures shows just those;
        - the per-table results are joined on the dimensions (NULL matches NULL)."""
        ctx = self._sheet
        assert ctx is not None
        if where or having or order or limit:
            raise _NoSQL(f"filters or a top N on a sheet over related tables: Tableau applies them to each table's query through the "
                         f"relationships; write them in the tile SQL ({SKILL})")
        if ctx.specials or ctx.row_lods:
            raise _NoSQL(f"LOD expressions on a sheet over related tables: write the tile SQL by hand ({SKILL})")

        def home(c: tuple) -> str:
            r = c[0]
            if r.special == "records":
                raise _NoSQL("Number of Records on a data source with relationships: each logical table has its own count; count the rows of one table")
            tables = self._tables_of(ds, r.field) if r.field is not None else set()
            if len(tables) != 1:
                raise _NoSQL(f"[{c[4]}] reads {'no table' if not tables else 'several related tables (' + ', '.join(sorted(tables)) + ')'}; write the tile SQL by hand ({SKILL})")
            return next(iter(tables))

        dims = [c for c in columns if not c[3]]
        dim_home = {c[1]: home(c) for c in dims}
        groups: dict[str, list[tuple[str, str]]] = {}  # relation -> [(aggregate SQL, column)]
        picks: dict[str, str] = {}

        def cte(rel: str) -> str:
            return f"measures_{_name(rel)}"

        for c in columns:
            if not c[3]:
                continue
            split = ctx.rel_parts.get(c[0].instance)
            if split is None:
                rel = home(c)
                groups.setdefault(rel, []).append((c[2], c[1]))
                picks[c[1]] = f"{cte(rel)}.{c[1]}"
                continue
            expr, parts = split
            refs = []
            for j, (rel, agg) in enumerate(parts, 1):
                groups.setdefault(rel, []).append((agg, f"{c[1]}_part_{j}"))
                refs.append(f"{cte(rel)}.{c[1]}_part_{j}")
            picks[c[1]] = re.sub(r"\{(\d+)\}", lambda m: refs[int(m.group(1))], expr)
        group_by = ("\ngroup by " + ", ".join(str(i) for i in range(1, len(dims) + 1))) if dims else ""
        ctes: list[tuple[str, str]] = []
        if dims:
            rels = list(dict.fromkeys(dim_home.values()))
            tree = self._rel_tree(ds, rels[0], set(rels))
            joins = "".join(f"\ninner join {self._rel_from(ds, b)} on {self._rel_on(a, b, cols)}" for a, b, cols in tree)
            ctes.append(("dimensions", f"select distinct {', '.join(f'{c[2]} as {c[1]}' for c in dims)}\nfrom {self._rel_from(ds, rels[0])}{joins}"))
        for rel, aggs in groups.items():
            others = {dim_home[c[1]] for c in dims} - {rel}
            items = [f"{c[2]} as {c[1]}" if dim_home[c[1]] == rel else f"rel_dims.{c[1]}" for c in dims] + [f"{sql} as {a}" for sql, a in aggs]
            frm = self._rel_from(ds, rel)
            if others:
                tree = self._rel_tree(ds, rel, others)
                keys = list(dict.fromkeys(fc for a, _, cols in tree if a == rel for fc, _ in cols))
                joins = "".join(f"\nleft join {self._rel_from(ds, b)} on {self._rel_on(a, b, cols)}" for a, b, cols in tree)
                inner = (f"select distinct {', '.join([f'{_ident(rel)}.{_ident(k)} as rel_key_{i}' for i, k in enumerate(keys, 1)] + [f'{c[2]} as {c[1]}' for c in dims if dim_home[c[1]] != rel])}\n"
                         f"from {frm}{joins}")
                on = " and ".join(f"{_ident(rel)}.{_ident(k)} is not distinct from rel_dims.rel_key_{i}" for i, k in enumerate(keys, 1))
                frm += f"\ninner join (\n{inner}\n) as rel_dims on {on}"
            ctes.append((cte(rel), f"select {', '.join(items)}\nfrom {frm}{group_by}"))
        return self._join_aggregates(ctes, [n for n, _ in ctes], [cte(rel) for rel in groups], [c[1] for c in dims],
                                     [picks[c[1]] for c in columns if c[3]], [c[1] for c in columns if c[3]])

    @staticmethod
    def _join_aggregates(ctes: list[tuple[str, str]], marks_from: list[str], measures: list[str], dims: list[str], values: list[str],
                         aliases: list[str]) -> str:
        """Aggregates computed separately (one CTE each, with the sheet's dimension columns `dims` and its values), joined on
        the dimensions: the marks are the distinct dimension values of the CTEs in `marks_from`, each CTE in `measures`
        left joined to them (NULL matches NULL); `values` are SQL over the CTEs' columns, output as `aliases`. Without
        dimensions each CTE is one row, cross joined. Used for relationships (one CTE per related table)."""
        ctes = list(ctes)
        if dims:
            ctes.append(("marks", "\nunion\n".join(f"select {', '.join(dims)} from {n}" for n in marks_from)))
            frm = "marks" + "".join(f"\nleft join {m} on " + " and ".join(f"marks.{a} is not distinct from {m}.{a}" for a in dims) for m in measures)
            tail = "\norder by " + ", ".join(str(i) for i in range(1, len(dims) + 1))
        else:
            frm, tail = "\ncross join ".join(measures), ""
        items = [f"marks.{d}" for d in dims] + [f"{v} as {a}" for v, a in zip(values, aliases)]
        body = "select\n" + ",\n".join(f"  {x}" for x in items) + f"\nfrom {frm}{tail}"
        return "with " + ",\n".join(f"{n} as (\n{b}\n)" for n, b in ctes) + "\n" + body

    @staticmethod
    def _rel_on(a: str, b: str, cols: list[tuple[str, str]]) -> str:
        return " and ".join(f"{_ident(a)}.{_ident(x)} = {_ident(b)}.{_ident(y)}" for x, y in cols)

    def _related_tables(self, ds: TDatasource | None, ordered: list[tuple[_Ref, bool]]) -> list[str]:
        """On a data source with relationships, the logical tables a sheet's fields read."""
        if ds is None or not ds.related:
            return []
        rels = {t for r, _ in ordered if r.ds is ds and r.field is not None for t in self._tables_of(ds, r.field)}
        if any(r.special == "records" for r, _ in ordered):
            rels.add("Number of Records")  # with relationships, each logical table has its own count
        return sorted({o.caption if (o := ds.object_of(t)) is not None else t for t in rels})

    def _totals_wanted(self, table: ET.Element, rows: list[_Ref], cols: list[_Ref]) -> tuple[bool, bool, set[str], str]:
        """(grand total row, grand total column, subtotalled dimension instances, a description; "" when none)."""
        rows_el, cols_el = table.find("rows"), table.find("cols")
        rows_total = rows_el is not None and rows_el.get("total") == "true"
        cols_total = cols_el is not None and cols_el.get("total") == "true"
        subtotals = {_unesc(m.group(2)) for c in table.findall("subtotals/column") for m in DS_REF.finditer(c.text or "")}
        names = [r.field.label if r.field else r.instance for r in rows + cols if r.instance in subtotals and not self._is_measure(r)]
        what = ", ".join(x for x, on in (("a grand total row", rows_total), ("a grand total column", cols_total),
                                         (f"subtotals of {', '.join(names)}", bool(names))) if on)
        return rows_total, cols_total, subtotals, what

    def _totals(self, sheet: str, kind: str, table: ET.Element, rows: list[_Ref], cols: list[_Ref], columns: list,
                filtered: bool) -> tuple[list[list[str]], str]:
        """Grouping sets for the sheet's totals (sources/_tableau_analytics.py), or a TODO when the SQL can't hold them."""
        rows_total, cols_total, subtotals, what = self._totals_wanted(table, rows, cols)
        if not what:
            return [], ""
        row_d = [r for r in rows if not self._is_measure(r)]
        col_d = [r for r in cols if not self._is_measure(r)]
        shelf = {r.instance for r in row_d + col_d}
        problem = ""
        if self._sheet is not None and self._sheet.specials:
            problem = "aggregated LODs"
        elif filtered:
            problem = "a filter on an aggregate or a Top N (whether Tableau totals the shown marks or all rows is not settled)"
        elif any(r.special or r.suffix == "qk" for r in row_d + col_d):
            problem = "Measure Names or a continuous field on the shelves"
        elif {c[0].instance for c in columns if not c[3]} != shelf:
            problem = "dimensions off Rows and Columns (colour, detail, tooltip), which totals don't group by"
        if problem:
            return [], (f"Tableau totals ({what}) not added: the sheet has {problem}; add them as grouping sets of the tile "
                        f"query, each the sheet's aggregate at the total's level ({SKILL})")
        sets = ta.grouping_sets([r.instance for r in row_d], [r.instance for r in col_d], rows_total, cols_total, subtotals)
        if not sets:
            return [], ""
        return sets, (f"Tableau totals ({what}) not added: the workbook file doesn't say how they are computed; check Analysis > "
                      "Totals > Total All Using (and each measure's Total Using) in Tableau. With Automatic, each total is the "
                      "sheet's aggregate recomputed at the total's level: the tile SQL as grouping sets, <dimension>_total = 1 "
                      "on total rows (also in source_config totals_sql); with Sum, Average, Minimum or Maximum, that function "
                      "of the marks instead")

    def _analytics(self, wb: _Workbook, sheet: str, table: ET.Element, panes: list[ET.Element], deps: dict, rows: list[_Ref],
                   cols: list[_Ref], columns: list, chosen: dict[str, _Ref], layer_refs: list, sql: str,
                   todos: list[str]) -> tuple[str, list[Reference]]:
        """The analytics pane: forecasts and trend lines become TODOs; reference lines become window columns over the tile's
        rows (sources/_tableau_analytics.py) and Reference layers, or TODOs."""
        by_inst = {c[0].instance: c for c in columns}
        shelf = rows + cols

        def title(r: _Ref | None) -> str:
            if r is None:
                return "?"
            c = by_inst.get(r.instance)
            return c[4] if c else (r.field.label if r.field else r.instance)

        fs = table.find("forecast-specification")
        if fs is not None and fs.get("enabled") != "false":
            measures = list(dict.fromkeys(title(r) for r in shelf if r.forecast == "forecast-value"))
            dates = [title(r) for r in shelf if self._is_temporal(r)]
            todos.append(ta.forecast_todo(fs, measures, dates))
        for pane in panes:
            tl = pane.find("trendline")
            if tl is not None and tl.get("enabled") != "false":
                todos.append(ta.trend_todo(tl, title(chosen.get("x")), title(chosen.get("y"))))
        what = self._totals_wanted(table, rows, cols)[3]
        if what and not any(t.startswith("Tableau totals") for t in todos):  # no SQL yet, or a sheet over related tables
            todos.append(f"Tableau totals ({what}) not added: {'the tile has no SQL yet' if not sql else 'unshackle writes no totals for this sheet'}; "
                         f"check Analysis > Totals > Total All Using in Tableau and add them as grouping sets of the tile query ({SKILL})")
        lines = ta.reference_lines(panes)
        if not lines:
            return sql, []
        header = [r for r in shelf if not self._is_measure(r) and not r.special and r.suffix != "qk"]
        inner = {sh[-1].instance for sh in (rows, cols) if sh and any(sh[-1] is h for h in header)}  # lays the marks out in a pane
        parts = {"table": [], "pane": [r for r in header if r.instance not in inner], "cell": header}
        split = [c[4] for c in columns if not c[3] and c[0].instance not in {r.instance for r in shelf}]
        taken = {c[1] for c in columns}
        items: list[str] = []
        grouped: list[tuple[str, str, list[str]]] = []
        refs: list[Reference] = []

        def alias(text: str) -> str:
            a, n = _name(text), 2
            while a in taken:
                a, n = f"{_name(text)}_{n}", n + 1
            taken.add(a)
            return a

        for rl in lines:
            formula = rl.get("formula") or ""
            comp = ta.COMPUTATION.get(formula, formula)
            scope = ta.SCOPE.get(rl.get("scope") or "per-table", "")
            value = next(iter(self._shelf(wb, rl.get("value-column"), deps)), None)
            axis = next(iter(self._shelf(wb, rl.get("axis-column"), deps)), None)
            c = by_inst.get(value.instance) if value is not None else None
            param = value.field if value is not None and value.special == "parameter" else None
            desc = f"reference line: {'parameter ' + param.label if param else comp + ' of ' + title(value)} per {scope or rl.get('scope')}"
            channel = next((ch for ch, r in chosen.items() if ch in ("x", "y") and axis is not None and r.instance == axis.instance), "")
            channel = channel or next((ch for _, ch, r in layer_refs if axis is not None and r.instance == axis.instance), "")
            percents = [x.get("percentage") or "" for x in rl.findall("reference-line-value")]
            band = rl.get("percentage-bands") == "true"
            problem = ""
            if not sql:
                problem = "the tile has no SQL yet"
            elif formula in ta.DISTRIBUTIONS or rl.get("tile-count") or (percents and not band) or "" in percents:
                opts = ", ".join(f"{k}={v}" for k, v in sorted(rl.attrib.items()) if k not in ("axis-column", "value-column", "id", "z-order"))
                problem = f"a {ta.DISTRIBUTIONS.get(formula, formula)} distribution band ({opts}) has no SQL form here"
            elif not scope:
                problem = f"scope {rl.get('scope')!r} is not one unshackle knows"
            elif not channel:
                problem = "its axis is not a measure axis of the tile"
            elif param is None and (c is None or not c[3]):
                problem = f"its measure ({title(value)}) is not a column of the tile"
            elif param is None and split:
                problem = (f"marks are split inside each cell by {', '.join(split)}: Tableau computes the line on each cell's "
                           "total (the stacked mark), not on the split marks")
            elif param is None and formula not in ta.WINDOW and not (formula == "total" and c[0].deriv in ta.TOTAL_OF_MARKS):
                problem = (f"{comp} recomputes {title(value)} over the scope's rows" if formula == "total"
                           else f"formula {formula!r} is not one unshackle knows")
            elif param is None and formula == "total" and re.search(r"\nlimit \d+$", sql):
                problem = "Total with a Top N filter"
            if problem:
                todos.append(f"Tableau {desc} not translated: {problem}; add it as a window column over the tile's rows "
                             f"and a rule layer in the chart ({SKILL})")
                continue
            if param is not None:
                expr, name = param_bind(param), alias(f"{param.label} line")
            else:
                fn = ta.WINDOW.get(formula) or ta.TOTAL_OF_MARKS[c[0].deriv]
                by = [by_inst[r.instance][1] for r in parts[scope]]
                over = ", ".join(f"tile.{k}" for k in by)
                over = f"partition by {over}" if over else ""
                if fn != "median":
                    expr = f"{fn}(tile.{c[1]}) over ({over})"
                elif self.dialect == "bigquery":  # its percentile is a window function only
                    expr = f"percentile_cont(cast(tile.{c[1]} as double precision), 0.5) over ({over})"
                else:  # no window median in Postgres: the median per partition in its own CTE, joined back
                    grouped.append((f"reference_{len(grouped) + 1}", f"median(cast({c[1]} as double precision))", by))  # a float, as Tableau's
                    expr = f"{grouped[-1][0]}.value"
                name = alias(f"{c[1]} {formula}" + ("" if scope == "table" else f" per {scope}"))
            if band and percents:
                cols_ = [alias(f"{name} {p} pct") for p in percents]
                taken.discard(name)
                items += [f"{float(p) / 100:g} * {expr} as {a}" for p, a in zip(percents, cols_)]
                refs += [Reference(a, channel, scope, ta.label(rl, comp, p)) for p, a in zip(percents, cols_)]  # type: ignore[arg-type]
                if len(cols_) > 1:
                    refs.append(Reference(cols_[0], channel, scope, band_to=cols_[-1]))  # type: ignore[arg-type]
                where = ", ".join(cols_)
            else:
                items.append(f"{expr} as {name}")
                refs.append(Reference(name, channel, scope, ta.label(rl, "" if param is not None else comp)))  # type: ignore[arg-type]
                where = name
            self.b.warn(f"sheet {sheet}", f"{desc}: column {where} of the tile SQL, drawn as a rule", "info")
        if items:
            sql = ta.wrap(sql, items, grouped, len([c for c in columns if not c[3]]))
        return sql, refs

    def _aliases(self, columns: list) -> list[str]:
        """Aliased dimensions show Tableau's labels: the column's SQL becomes the mapping (rows are still grouped on the
        values, so two members with one label stay two rows, as in Tableau). Returns notes for the ones left as values."""
        ctx = self._sheet
        assert ctx is not None
        notes = []
        for i, (r, alias, sql, is_agg, display, etype, unit) in enumerate(columns):
            f = r.field
            if is_agg or not sql or f is None or not f.aliases or r.deriv not in ("none", ""):
                continue
            label = alias_sql(_paren(sql), f)
            if label is None:
                notes.append(f"[{f.label}] has aliases (Default Properties > Aliases), which Tableau shows instead of its {f.datatype} values: "
                             f"map them in the SQL ({', '.join(f'{k} -> {v!r}' for k, v in f.aliases)})")
                continue
            ctx.labels[r.instance] = sql
            columns[i] = (r, alias, label, is_agg, display, "nominal" if f.datatype == "boolean" else etype, unit)
        return notes

    def _sort_aggregates(self, wb: _Workbook, columns: list, deps: dict[tuple[str, str], ET.Element]) -> list[str]:
        """SQL of the aggregate each computed sort orders by (Sort > Field), when it is one aggregate over the sheet's rows."""
        ctx = self._sheet
        assert ctx is not None
        notes = []
        by_inst = {c[0].instance: c for c in columns}
        for inst, srt in ctx.sorts.items():
            dim = by_inst.get(inst)
            if srt.kind != "computed" or dim is None or dim[3]:
                continue
            ref = next(iter(self._shelf(wb, f"[{_esc(dim[0].dsname)}].[{_esc(srt.using)}]", deps)), None)
            used = by_inst.get(srt.using)
            try:
                if used is None and ref is not None:
                    used = (ref, *self._column(ref))
            except _NoSQL:
                used = None
            # an aggregated LOD (no SQL here) is ordered by its value in the tile SQL, when the sheet shows it
            if used is None or not used[3] or not (used[2] or srt.using in by_inst):
                notes.append(f"sort of {dim[4]} by {srt.using} ({srt.direction.lower()}) not applied: add it to the tile's order by")
                continue
            ctx.sort_by[inst] = (used[0].deriv, used[2])
        return notes

    def _sort_order(self, dims: list, refer: Any, from_sql: str = "") -> list[str]:
        """ORDER BY items for the sheet's dimensions, outermost first, each in its sort's order (see _sort_items)."""
        return [x for items in self._sort_items(dims, refer, from_sql) for x in items]

    def _sort_items(self, dims: list, refer: Any, from_sql: str = "", windows: bool = True) -> list[list[str]]:
        """ORDER BY items per dimension, each in its sort's order. `refer(i, c)` gives a dimension's (key, label SQL, value
        SQL): key is its output position or label SQL. A computed sort with other dimensions orders each member by its
        aggregate over the whole view (a window over the sheet's marks; not with `windows=False`, e.g. totals rows)."""
        ctx = self._sheet
        assert ctx is not None
        items: list[list[str]] = []
        for i, c in enumerate(dims, 1):
            key, label, raw = refer(i, c)
            srt = ctx.sorts.get(c[0].instance)
            using = ""
            if srt is not None and srt.kind == "computed":
                deriv, agg = ctx.sort_by.get(c[0].instance, ("", ""))
                outer = {"sum": "sum", "cnt": "sum", "min": "min", "max": "max"}.get(deriv, "")
                if agg and len(dims) == 1:
                    using = agg
                elif agg and outer and windows:
                    using = f"{outer}({agg}) over (partition by {_qualify(raw, from_sql)})"
                elif agg:
                    ctx.notes.append(f"sort of {c[4]} by {srt.using} ({srt.direction.lower()}) with other dimensions on the sheet not applied: "
                                     "order the members by the aggregate over the whole view")
                if not using:
                    srt = None
            string = c[0].instance in ctx.labels or (c[0].field is not None and c[0].field.datatype == "string" and c[0].deriv in ("none", ""))
            null_label = c[0].instance in ctx.labels and any(k == "%null%" for k, _ in c[0].field.aliases)
            items.append(self._dim_order(srt, key, label, raw, string, using, null_label))
        return items

    def _dim_order(self, srt: SheetSort | None, key: str, label: str, raw: str, string: bool, using: str = "",
                   null_label: bool = False) -> list[str]:
        """ORDER BY items for one dimension's members in Tableau's order (also usable in a window's ORDER BY, with `key`
        the label SQL). Natural order (no sort, or data source order) follows the labels ignoring case (Tableau sorts them
        itself, whatever the warehouse's collation), NULL first (also when it has an alias: `null_label`); an alphabetic sort compares the labels as text by character code (uppercase first), NULL as
        'Null'; a manual sort lists members in order (its direction is ignored), the rest after them in natural order; a
        computed sort orders by `using`, ties in natural order."""
        if srt is not None and srt.kind == "alphabetic" and not string:
            srt = None  # Tableau sorts only text alphabetically; on numbers and dates it changes nothing
        desc = srt is not None and srt.direction == "DESC" and srt.kind != "manual"
        nulls = [f"case when {raw} is null then 0 else 1 end"] if null_label else []
        natural = nulls + ([f"lower({label}) nulls first", key] if string else [key])
        if srt is None or srt.kind == "natural":
            return [f"{x} desc" for x in nulls] + ([f"lower({label}) desc nulls last", f"{key} desc"] if string else [f"{key} desc"]) if desc else natural
        if srt.kind == "alphabetic":
            binary = ' collate "C"' if self.dialect == "postgres" else ""  # Postgres compares text in the database's collation
            return [f"coalesce({label}, 'Null'){binary}{' desc' if desc else ''}"]
        if srt.kind == "manual":
            members = [m for m in srt.members if m != "%all%"]
            whens = " ".join(f"when {raw} is null then {n}" if (lit := _member_sql(m)) is None else f"when {raw} = {lit} then {n}"
                             for n, m in enumerate(members, 1))
            return ([f"case {whens} else {len(members) + 1} end"] if members else []) + natural
        return [f"{using}{' desc' if desc else ''}", *natural]

    def _ds_join(self, ds: TDatasource | None) -> str:
        """FROM clause with a datasource's whole join tree (relations aliased by their Tableau names); "" if it can't be written."""
        if ds is None or not ds.join_steps or not all(pairs for _, _, pairs in ds.join_steps):
            return ""
        by_rel = self.ds_models.get(ds.uid) or {}
        if any(m.rel.kind != "table" for m in by_rel.values()):
            return ""
        first = next(iter(ds.relations))
        if first not in by_rel or any(name not in by_rel for _, name, _ in ds.join_steps):
            return ""
        sql = f"{by_rel[first].sm.table} as {_ident(first)}"
        for kind, name, pairs in ds.join_steps:
            sql += f"\n{kind.lower()} join {by_rel[name].sm.table} as {_ident(name)} on {_join_on(pairs)}"
        return sql

    def _tile_join(self, ds: TDatasource | None, ordered: list[tuple[_Ref, bool]]) -> tuple[str, str]:
        """(FROM with the datasource's joins, TODO). Tableau computes a sheet over the joined rows: inner joins drop unmatched
        rows and joins can repeat rows, even for fields of one table. The join is left out only when it can't change the numbers:
        the sheet uses the first table only and every join is a left join on the other table's key."""
        if ds is None or not ds.join_steps:
            return "", ""
        join = self._ds_join(ds)
        if not join:
            return "", (f"the datasource joins tables ({'; '.join(ds.joins)}) in a way unshackle can't write as SQL: Tableau computes this "
                        f"sheet over the joined rows, which can drop or repeat rows; add the join to the tile SQL ({SKILL})")
        by_rel = self.ds_models.get(ds.uid) or {}
        used = {id(m) for r, _ in ordered if r.ds is ds and r.field and (m := self._field_model(ds, r.field)) is not None}
        first = by_rel[next(iter(ds.relations))]
        if used <= {id(first)} and not any(self._needs_rows(ds, r) for r, _ in ordered):
            safe = True
            for kind, name, pairs in ds.join_steps:
                right = by_rel[name]
                cols = [c for (ra, ca), (rb, cb) in pairs for rel, c in ((ra, ca), (rb, cb)) if rel == name]
                if kind.lower() != "left" or len(pairs) != 1 or not right.pk or [c.lower() for c in cols] != [right.pk.lower()]:
                    safe = False
            if safe:
                return "", ""
        return join, ""

    def _needs_rows(self, ds: TDatasource, r: _Ref) -> bool:
        """A FIXED LOD is computed over the joined rows, so a sheet that shows one keeps the join."""
        return r.field is not None and r.field.is_calc and bool((c := self.cache.get((ds.uid, r.field.local))) and c.lod)

    def _tile_sql(self, columns: list, from_sql: str, cte: str, where: list[str], having: list[str], order: list[str], limit: str,
                  totals: list[list[str]] | None = None) -> str:
        """SELECT for a sheet. Aggregated LODs are computed in their own CTEs at the sheet's grain and joined on its dimensions.
        `totals`: grouping sets (dimension instances) for the sheet's totals; each totalled dimension gets a `<alias>_total`
        column (1 on its total rows), and total rows sort after their group."""
        ctx = self._sheet
        assert ctx is not None
        dims = [c for c in columns if not c[3]]
        specials = [(c, ctx.specials[c[0].instance]) for c in columns if c[3] and c[0].instance in ctx.specials]
        plain = [c for c in columns if c[3] and c[0].instance not in ctx.specials]
        fixed_parts = [p for _, sp in specials for kind, p in sp.parts if kind == "fixed"]
        lods = list(dict.fromkeys(ctx.row_lods + [sp.lod for _, sp in specials if sp.lod is not None] + [p.lod for p in fixed_parts]))
        ctes = ([("custom_sql", cte)] if cte else []) + [(lod.name, lod.select()) for lod in lods]
        rows = from_sql + "".join(f"\nleft join {lod.name} on {self._lod_on(lod)}" for lod in ctx.row_lods)
        where_sql = ("\nwhere " + "\n  and ".join(where)) if where else ""

        def group(n: int) -> str:
            return ("\ngroup by " + ", ".join(str(i) for i in range(1, n + 1))) if n else ""

        def item(c: tuple) -> str:
            return f"  {c[2]}" if c[2] == c[1] else f"  {c[2]} as {c[1]}"

        def value(c: tuple) -> str:  # what an aliased dimension groups on: its values, not its labels
            return _qualify(ctx.labels[c[0].instance], from_sql) if c[0].instance in ctx.labels else c[2]

        if order and any(c[0].instance in ctx.sorts for c in dims):
            ctx.notes.append("the Top N filter's order is kept; Tableau shows its rows in the sheet's sort: order the result again")
        if not specials and totals:
            totalled = [c for c in dims if any(c[0].instance not in s for s in totals)]
            expr = {c[0].instance: value(c) for c in dims}
            flags = [f"  grouping({expr[c[0].instance]}) as {c[1]}_total" for c in totalled]
            sets = ", ".join("(" + ", ".join(expr[i] for i in s) + ")" for s in totals)
            flag_pos = {id(c): len(columns) + k for k, c in enumerate(totalled, 1)}
            shown = [f"  case when grouping({expr[c[0].instance]}) = 1 then null else {c[2]} end as {c[1]}"
                     if c[0].instance in ctx.labels and id(c) in flag_pos else item(c) for c in columns]  # no label on a total row
            per_dim = self._sort_items(dims, lambda i, c: (str(i), c[2], value(c)), from_sql, windows=False)
            lines = ["select", ",\n".join(shown + flags), f"from {rows}{where_sql}", f"group by grouping sets ({sets})",
                     "order by " + ", ".join(x for i, c in enumerate(dims) for x in ([str(flag_pos[id(c)])] if id(c) in flag_pos else []) + per_dim[i])]
            body = "\n".join(lines)
        elif not specials:
            keys = [value(c) if c[0].instance in ctx.labels else str(i) for i, c in enumerate(dims, 1)]
            lines = ["select", ",\n".join(item(c) for c in columns), f"from {rows}{where_sql}" + (f"\ngroup by {', '.join(keys)}" if keys else "")]
            if having:
                lines.append("having " + "\n  and ".join(having))
            if order:
                lines.append("order by " + ", ".join(order))
            elif dims:
                lines.append("order by " + ", ".join(self._sort_order(dims, lambda i, c: (str(i), c[2], value(c)), from_sql)))
            if limit:
                lines.append(f"limit {limit}")
            body = "\n".join(lines)
        else:
            labeled = {c[0].instance: c for c in dims if c[0].instance in ctx.labels}
            dims = [(c[0], c[1], ctx.labels[c[0].instance], *c[3:]) if c[0].instance in labeled else c for c in dims]  # values until the end
            dim_items = [item(c).strip() for c in dims]
            joins: list[tuple[str, list[tuple]]] = []
            # aggregates over the sheet's rows inside expression specials: hidden columns of the base CTE
            hidden: dict[tuple[int, int], str] = {}
            hidden_items: list[str] = []
            for i, (c, sp) in enumerate(specials, 1):
                for j, (kind, p) in enumerate(sp.parts):
                    if kind == "row":
                        hidden[(i, j)] = f"{c[1]}_part_{j + 1}"
                        hidden_items.append(f"{p} as {hidden[(i, j)]}")
            has_base = bool(dims or plain or hidden)
            if has_base:
                base = ["select " + ", ".join(dim_items + [item(c).strip() for c in plain] + hidden_items),
                        f"from {rows}{where_sql}{group(len(dims))}"]
                if having:
                    base.append("having " + "\n  and ".join(having))
                if order:
                    base.append("order by " + ", ".join(order))
                if limit:
                    base.append(f"limit {limit}")
                ctes.append(("base", "\n".join(base)))
            elif having or limit:
                raise _NoSQL("filters on aggregates with an aggregated LOD: add them to the tile SQL")
            source: dict[int, str] = {}
            picks_sql: dict[int, str] = {}

            def fixed_cte(name: str, sp: _Special, alias: str) -> None:
                """One value per distinct combination of the sheet's dimensions and the FIXED dimensions (from the filtered
                rows), the grain joined on the FIXED dimensions, then the view's aggregate per mark."""
                assert sp.lod is not None
                keys = self._lod_keys(sp.lod)
                domain = (f"select distinct {', '.join(dim_items + [f'{k} as lod_key_{j}' for j, k in enumerate(keys, 1)])}\n"
                          f"from {rows}{where_sql}")
                on = " and ".join(f"d.lod_key_{j} is not distinct from {sp.lod.name}.{a}" for j, (_, a) in enumerate(sp.lod.keys, 1))
                value = AGG_SQL[sp.outer].format(sp.value or f"{sp.lod.name}.{_ident(sp.col)}")
                ctes.append((name, f"select {', '.join([f'd.{d[1]}' for d in dims] + [f'{value} as {alias}'])}\n"
                                   f"from (\n{domain}\n) as d\nleft join {sp.lod.name} on {on}{group(len(dims))}"))
                joins.append((name, dims))

            for i, (c, sp) in enumerate(specials, 1):
                name = f"lod_measure_{i}"
                source[id(c)] = name
                if sp.kind == "fixed" and sp.lod is not None:
                    fixed_cte(name, sp, c[1])
                elif sp.kind == "expr":
                    refs: list[str] = []
                    for j, (kind, p) in enumerate(sp.parts):
                        if kind == "row":
                            refs.append(f"base.{hidden[(i, j)]}")
                        else:
                            fixed_cte(f"{name}_{j + 1}", p, "value")
                            refs.append(f"{name}_{j + 1}.value")
                    picks_sql[id(c)] = re.sub(r"\{(\d+)\}", lambda m: refs[int(m.group(1))], sp.expr)
                elif sp.kind == "include":
                    inner = (f"select {', '.join(dim_items + [f'{x} as lod_dim_{j}' for j, x in enumerate(sp.dims, 1)] + [f'{sp.inner} as lod_value'])}\n"
                             f"from {rows}{where_sql}{group(len(dims) + len(sp.dims))}")
                    value = AGG_SQL[sp.outer].format("t.lod_value")
                    ctes.append((name, f"select {', '.join([f't.{d[1]}' for d in dims] + [f'{value} as {c[1]}'])}\n"
                                       f"from (\n{inner}\n) as t{group(len(dims))}"))
                    joins.append((name, dims))
                else:  # exclude: the aggregate without the excluded dimensions, repeated on each of the sheet's marks
                    keep = []
                    for d in dims:
                        if d[0].field is not None and d[0].field.local in sp.dims:
                            if d[0].deriv not in ("none", ""):
                                raise _NoSQL(f"EXCLUDE of [{d[0].field.label}] while the sheet shows it at another level ({d[4]}): write it in the tile SQL")
                            continue
                        keep.append(d)
                    ctes.append((name, f"select {', '.join([item(d).strip() for d in keep] + [f'{sp.inner} as {c[1]}'])}\n"
                                       f"from {rows}{where_sql}{group(len(keep))}"))
                    joins.append((name, keep))
            head = "base" if has_base else joins[0][0]
            frm = head
            for name, on_dims in joins:
                if name == head:
                    continue
                if on_dims and head == "base" and dims:
                    frm += f"\nleft join {name} on " + " and ".join(f"base.{d[1]} is not distinct from {name}.{d[1]}" for d in on_dims)
                else:
                    frm += f"\ncross join {name}"
            exprs = {c[0].instance: picks_sql.get(id(c)) or f"{source.get(id(c), head)}.{c[1]}" for c in columns}
            labels = {inst: alias_sql(exprs[inst], c[0].field) or exprs[inst] for inst, c in labeled.items()}
            picks = [f"  {labels.get(c[0].instance) or picks_sql.get(id(c))} as {c[1]}" if c[0].instance in labels or id(c) in picks_sql
                     else f"  {exprs[c[0].instance]}" for c in columns]
            for inst, (deriv, _) in list(ctx.sort_by.items()):
                srt = ctx.sorts[inst]
                if srt.using in exprs:  # the sorted-by measure is on the sheet: order the marks by its value
                    ctx.sort_by[inst] = (deriv, exprs[srt.using])
                else:
                    del ctx.sort_by[inst]
                    ctx.notes.append(f"sort by {srt.using} ({srt.direction.lower()}) not applied: add it to the tile's order by")
            order_by = self._sort_order(dims, lambda i, c: (str(i), labels.get(c[0].instance, exprs[c[0].instance]), exprs[c[0].instance]), head)
            body = "\n".join(["select", ",\n".join(picks), f"from {frm}"] + (["order by " + ", ".join(order_by)] if dims else []))
        if not ctes:
            return body
        return "with " + ",\n".join(f"{n} as (\n{b}\n)" for n, b in ctes) + "\n" + body

    def _lod_keys(self, lod: _Lod) -> list[str]:
        """The FIXED dimensions as SQL over the sheet's rows."""
        ctx = self._sheet
        if ctx is None or ctx.ds is None:
            raise _NoSQL("FIXED LOD outside a sheet")
        try:
            return [self._row_ref(ctx.ds, d) for d in lod.dims]
        except ax.FormulaError as exc:
            raise _NoSQL(f"FIXED dimensions not translated for this sheet ({exc})") from None

    def _lod_on(self, lod: _Lod) -> str:
        return " and ".join(f"{k} is not distinct from {lod.name}.{alias}" for k, (_, alias) in zip(self._lod_keys(lod), lod.keys))

    def _filter(self, wb: _Workbook, flt: ET.Element, deps: dict, meta: dict[str, Any]) -> tuple[str, Any] | None:
        refs = self._shelf(wb, flt.get("column"), deps)
        if not refs:
            return None
        r = refs[0]
        if r.special in ("measure_names", "measure_values", "geo"):
            return None
        label = r.field.label if r.field else r.instance
        if r.field is not None and r.ds is not None and r.field.is_calc:
            c = self.cache.get((r.ds.uid, r.field.local))
            if c is not None and c.kind == "security":
                return None
        if label.startswith("Action ("):
            return ("todo", f"dashboard action filter {label}: interactions are not migrated")
        try:
            _, expr, is_agg, *_ = self._column(r)
        except _NoSQL as exc:
            return ("todo", f"filter on [{label}] not applied: {exc}")
        if self._sheet is not None and r.instance in self._sheet.specials:
            return ("todo", f"filter on [{label}] (an aggregated LOD) not applied; add it to the SQL")
        if r.field is not None:
            meta["filters"][(r.dsname, r.field.local)] = label
        cls = flt.get("class", "")
        clause = "having" if is_agg else "where"
        if cls == "categorical":
            gf = flt.find("groupfilter")
            if gf is None:
                return None
            res = self._members(gf)
            if res is None:
                return ("todo", f"filter on [{label}] ({gf.get('function')}) not translated; add it to the SQL")
            k, vals = res
            if k == "all":
                return None
            if k == "top":
                count, expression, end = vals
                try:
                    osql = self._expression_sql(r.ds, expression)
                except ax.FormulaError as exc:
                    return ("todo", f"top-N filter on [{label}] by {expression} not translated ({exc})")
                return ("top", (f"{osql} {'desc' if end != 'bottom' else 'asc'}", count))
            lits = [_member_sql(v) for v in vals]
            nonnull = [x for x in lits if x is not None]
            expr = _paren(expr)
            parts = []
            if nonnull:
                op = "=" if len(nonnull) == 1 else "in"
                rhs = nonnull[0] if len(nonnull) == 1 else f"({', '.join(nonnull)})"
                if k == "not in":
                    op = "<>" if op == "=" else "not in"
                parts.append(f"{expr} {op} {rhs}")
            if len(nonnull) < len(lits):
                parts.append(f"{expr} is {'not ' if k == 'not in' else ''}null")
            if not parts:
                return None
            joiner = " and " if k == "not in" else " or "
            return (clause, parts[0] if len(parts) == 1 else "(" + joiner.join(parts) + ")")
        if cls == "quantitative":
            lo, hi = flt.findtext("min"), flt.findtext("max")
            inc = flt.get("included-values", "in-range")
            parts = []
            for bound, op in ((lo, ">="), (hi, "<=")):
                if bound is not None and bound.strip():
                    parts.append(f"{expr} {op} {_member_sql(bound.strip()) or 'null'}")
            if inc == "non-null":
                parts.append(f"{expr} is not null")
            if not parts:
                return None
            cond = " and ".join(parts)
            if inc == "in-range-or-null":
                cond = f"({cond} or {expr} is null)"
            return (clause, cond)
        if cls == "relative-date":
            return ("todo", f"relative date filter on [{label}] (last {flt.get('period-type-v2') or flt.get('period-type', '')} "
                            f"{flt.get('first-period', '')}..{flt.get('last-period', '')}): pin a fixed window or add :date_from/:date_to binds")
        return ("todo", f"{cls} filter on [{label}] not translated")

    def _members(self, gf: ET.Element) -> tuple[str, Any] | None:
        fn = gf.get("function", "")
        children = gf.findall("groupfilter")
        if fn == "member":
            return ("in", [gf.get("member")])
        if fn == "union":
            vals: list[Any] = []
            for c in children:
                res = self._members(c)
                if res is None or res[0] != "in":
                    return None
                vals += res[1]
            return ("in", vals)
        if fn == "level-members":
            return ("all", [])
        if fn == "except" and len(children) == 2 and children[0].get("function") == "level-members":
            res = self._members(children[1])
            return ("not in", res[1]) if res and res[0] == "in" else None
        if fn == "end":
            order = next((c for c in gf.iter("groupfilter") if c.get("function") == "order"), None)
            if order is not None and order.get("expression") and gf.get("count", "").isdigit():
                return ("top", (int(gf.get("count", "0")), order.get("expression", ""), gf.get("end", "top")))
        return None

    def _expression_sql(self, ds: TDatasource | None, expression: str) -> str:
        if ds is None:
            raise ax.FormulaError("no datasource")
        node = parse_formula(scan_formula(expression)[0], date_opts(ds))
        cl = self._classify(ds, node, lambda r: self._row_ref(ds, r), lambda r: self._metric_ref(ds, r))
        if cl.kind == "agg" and cl.agg is not None and cl.agg.condition is None:
            return AGG_SQL[cl.agg.agg].format(cl.agg.expr)
        if cl.kind == "derived" and len(cl.metrics) == 1 and cl.expr == cl.metrics[0]:
            return self.metric_sql[cl.metrics[0]]
        raise ax.FormulaError("not a single aggregate")

    # ------------------------------------------------------------ dashboards

    def _slug(self, wb: _Workbook, name: str) -> str:
        slug = re.sub(r"-+", "-", slugify(name))
        if slug in self.slugs:
            slug = re.sub(r"-+", "-", slugify(f"{wb.name} {name}"))
        base, n = slug, 2
        while slug in self.slugs:
            slug, n = f"{base}-{n}", n + 1
        self.slugs.add(slug)
        self.wb_slugs.setdefault(wb.id, []).append(slug)
        return slug

    def _param_filter(self, p: TField) -> DashboardFilter:
        kind = "date" if p.datatype in TIME_TYPES else "number" if p.datatype in NUMERIC else "value"
        return DashboardFilter(_name(p.label), "", _param_value(p.value, p.datatype), kind, allowed=_param_domain(p))  # type: ignore[arg-type]

    def _dashboards(self, wb: _Workbook) -> None:
        worksheets = {ws.get("name", ""): ws for ws in wb.root.findall("worksheets/worksheet")}
        built: dict[str, tuple[Tile, dict[str, Any]]] = {}

        def tile_for(name: str) -> tuple[Tile, dict[str, Any]]:
            if name not in built:
                try:
                    built[name] = self._tile(wb, worksheets[name])
                except Exception as exc:  # noqa: BLE001
                    built[name] = (Tile(slugify(name), name, "unknown", todo=f"worksheet not parsed: {exc}"), {"params": [], "filters": {}})
                finally:
                    self._sheet, self._joined = None, None
            t, m = built[name]
            return copy.deepcopy(t), m

        placed: set[str] = set()
        for db in wb.root.findall("dashboards/dashboard"):
            dname = db.get("name") or "Dashboard"
            if db.get("type") == "storyboard":
                self.b.warn(f"{wb.src} story {dname}", "Tableau stories are not migrated; each story point's sheet is migrated on its own", "info")
                continue
            zones_el = db.find("zones")
            zones = list(zones_el.iter("zone")) if zones_el is not None else []
            tiles: list[Tile] = []
            grid: list[tuple[str, float, float]] = []
            filters: dict[str, DashboardFilter] = {}
            metas: list[tuple[Tile, dict[str, Any]]] = []
            quick: dict[tuple[str, str], str] = {}
            for z in zones:
                t2 = z.get("type-v2") or z.get("type") or ""
                zname = z.get("name") or ""
                x, y = _num(z.get("x")), _num(z.get("y"))
                if t2 in ("", "viz") and zname in worksheets:
                    if any(t.id == slugify(zname) for t in tiles):
                        continue
                    tile, meta = tile_for(zname)
                    tiles.append(tile)
                    metas.append((tile, meta))
                    grid.append((tile.id, x, y))
                    placed.add(zname)
                elif t2 == "text":
                    text = _text(z.find("formatted-text"))
                    if text:
                        tile = Tile(f"text-{z.get('id', len(tiles))}", "", "text", "text", text=text, source_type="text")
                        tiles.append(tile)
                        grid.append((tile.id, x, y))
                elif t2 == "filter":
                    refs = self._shelf(wb, z.get("param"), {})
                    if refs and refs[0].field is not None:
                        r = refs[0]
                        try:
                            alias, *_ = self._column(r)
                        except _NoSQL:
                            alias = _name(r.field.label)
                        kind = "date_range" if r.field.datatype in TIME_TYPES else "number" if r.field.datatype in NUMERIC and r.deriv == "none" else "value"
                        fname = _name(r.field.label)
                        filters.setdefault(fname, DashboardFilter(fname, alias, None, kind))  # type: ignore[arg-type]
                        quick[(r.dsname, r.field.local)] = fname
                elif t2 == "paramctrl":
                    p = wb.params.get(_name_parts(z.get("param", ""))[-1] if z.get("param") else "")
                    if p is not None:
                        pf = self._param_filter(p)
                        filters.setdefault(pf.name, pf)
                elif t2 in ("web", "bitmap"):
                    self.b.warn(f"{wb.src} dashboard {dname}", f"{t2} zone not migrated", "info")
            for tile, meta in metas:
                for p in meta["params"]:
                    if p in wb.params:
                        pf = self._param_filter(wb.params[p])
                        filters.setdefault(pf.name, pf)
                wired = [(fname, label) for key, label in meta["filters"].items() if (fname := quick.get(key))]
                if wired:
                    note = "; ".join(
                        f"dashboard filter :{fname} on [{label}] is baked in with the saved selection; use (:{fname} is null or <col> = :{fname}) (skills/translate-dashboards)"
                        for fname, label in wired
                    )
                    tile.todo = f"{tile.todo}; {note}" if tile.todo else note
            v = self.view_index.get((wb.id, dname)) or {}
            desc = _text(db.find("layout-options/caption")) or ""
            self.b.dashboards.append(Dashboard(
                name=dname, slug=self._slug(wb, dname), description=desc, tiles=tiles, layout=grid_rows(grid),
                filters=list(filters.values()), datasource=self.b.datasource, source_ref=f"tableau workbook {wb.id} dashboard {dname}",
                url=self._url(v) or (self.wb_meta.get(wb.id) or {}).get("webpageUrl", ""), views=_views(v) if v else None,
            ))
        stray = [n for n in worksheets if n not in placed]
        if stray:
            tiles, filters = [], {}
            total = 0
            for n in stray:
                tile, meta = tile_for(n)
                tiles.append(tile)
                total += _views(self.view_index.get((wb.id, n)) or {})
                for p in meta["params"]:
                    if p in wb.params:
                        pf = self._param_filter(wb.params[p])
                        filters.setdefault(pf.name, pf)
            name = f"{wb.name} sheets"
            self.b.dashboards.append(Dashboard(
                name=name, slug=self._slug(wb, name), description=f"Worksheets of the Tableau workbook {wb.name} that are not on a dashboard.",
                tiles=tiles, layout=[[t.id] for t in tiles], filters=list(filters.values()), datasource=self.b.datasource,
                source_ref=f"tableau workbook {wb.id} worksheets", url=(self.wb_meta.get(wb.id) or {}).get("webpageUrl", ""), views=total,
            ))

    def _url(self, v: dict[str, Any]) -> str:
        server, content = self.site.get("server", ""), v.get("contentUrl", "")
        if not server or not content:
            return ""
        site = f"site/{self.site.get('content_url')}/" if self.site.get("content_url") else ""
        return f"{server}/#/{site}views/{content.replace('/sheets/', '/')}"

    # ------------------------------------------------------------ knowledge, metadata, permissions

    def _field_lines(self, ds: TDatasource) -> tuple[list[str], list[str]]:
        calcs, described = [], []
        for f in ds.fields.values():
            if f.param or f.local in INTERNAL_FIELDS:
                continue
            desc = f" - {f.description}" if f.description else ""
            if f.formula is not None:
                one = readable(" ".join(scan_formula(f.formula)[0].split()), ds.fields, self.params_for(ds))
                comments = scan_formula(f.formula)[3]
                note = f" ({'; '.join(comments)})" if comments else ""
                calcs.append(f"- **{f.label}** ({ds.caption}): `{one}`{desc}{note}")
            elif f.description:
                described.append(f"- **{f.label}** ({ds.caption}): {f.description}")
        for name, path in ds.hierarchies:
            labels = [ds.fields[x].label if x in ds.fields else x for x in path]
            described.append(f"- **{name}** ({ds.caption}): hierarchy {' > '.join(labels)}")
        return calcs, described

    def _knowledge(self, wb: _Workbook) -> None:
        lines = [f"Tableau workbook **{wb.name}**" + (f" in project {wb.project}" if wb.project else "") + "."]
        if wb.description:
            lines += ["", wb.description]
        calcs, described = [], []
        for ds in wb.datasources.values():
            c, d = self._field_lines(ds)
            calcs += c
            described += d
        if wb.params:
            lines += ["", "## Parameters", ""]
            for p in wb.params.values():
                members = [m for m in p.extra.get("members", []) if m]
                lines.append(f"- **{p.label}** ({p.datatype}): current value `{_param_value(p.value, p.datatype)}`"
                             + (f", allowed {', '.join(members)}" if members else ""))
        if calcs:
            lines += ["", "## Calculated fields", "", *calcs]
        if described:
            lines += ["", "## Field descriptions", "", *described]
        for ds in wb.datasources.values():
            if ds.settings:
                lines += ["", f"## Connection settings of {ds.caption}", *self._settings_lines(ds)]
        captions = []
        for ws in wb.root.findall("worksheets/worksheet"):
            cap = _text(ws.find("layout-options/caption"))
            if cap:
                captions.append(f"- **{ws.get('name')}**: {cap}")
        if captions:
            lines += ["", "## Sheet captions", "", *captions]
        meta = self._json(f"metadata/{wb.id}.json") or {}
        lineage = sorted({t.get("fullName") or t.get("name", "") for eds in meta.get("embeddedDatasources") or [] for t in eds.get("upstreamTables") or []} |
                         {t.get("fullName") or t.get("name", "") for t in meta.get("upstreamTables") or []})
        if lineage:
            lines += ["", "## Upstream tables (Metadata API)", "", *[f"- `{t}`" for t in lineage if t]]
        for t in meta.get("customSQLTables") or []:
            lines += ["", f"Custom SQL `{t.get('name', '')}`:", "", "```sql", (t.get("query") or "").strip(), "```"]
        if len(lines) > 1:
            self.b.knowledge.append(KnowledgeItem(
                f"{wb.name} glossary", "\n".join(lines), description=f"Calculated fields, parameters, field descriptions and sheet captions from the Tableau workbook {wb.name}",
                datasource=self.b.datasource, source_ref=wb.src,
            ))

    def _tds_knowledge(self) -> None:
        lines = ["Published Tableau datasources."]
        for ds in self.tds:
            calcs, described = self._field_lines(ds)
            tables = ", ".join(f"`{r.schema + '.' if r.schema else ''}{r.table}`" if r.kind == "table" else "custom SQL" for r in ds.relations.values())
            lines += ["", f"## {ds.caption}", "", f"Tables: {tables or 'none'}.", *self._settings_lines(ds)]
            if calcs:
                lines += ["", *calcs]
            if described:
                lines += ["", *described]
        self.b.knowledge.append(KnowledgeItem("Tableau published datasources", "\n".join(lines),
                                              description="Tables, calculated fields and descriptions of Tableau published datasources",
                                              datasource=self.b.datasource, source_ref="tableau datasources"))

    def _metadata_only(self) -> None:
        have = {wb.id for wb in self.workbooks}
        for rel in self.store.glob("metadata/*.json"):
            wid = Path(rel).stem
            if wid in have:
                continue
            node = self._json(rel) or {}
            name = node.get("name") or wid
            self.b.warn(f"workbook {name}", "workbook XML was not downloaded; only calculated fields from the Metadata API were kept (as TODO metrics)")
            for eds in node.get("embeddedDatasources") or []:
                for fl in eds.get("fields") or []:
                    if fl.get("__typename") != "CalculatedField" or not fl.get("formula"):
                        continue
                    mname = _name(f"{name} {fl.get('name', '')}")
                    if any(m.name == mname for m in self.b.metrics):
                        continue
                    self.b.metrics.append(Metric(
                        mname, label=fl.get("name", ""), description=fl.get("description") or "", source_expression=fl["formula"],
                        source_language=LANG, source_ref=f"{rel} datasource: {eds.get('name', '')}",
                        todo="from the Metadata API only (workbook XML missing): re-run `unshackle extract tableau` or translate by hand; see skills/translate-metrics",
                    ))

    def _permissions(self) -> None:
        seen: set[tuple] = set()
        for rel in self.store.glob("permissions/*.json"):
            wid = Path(rel).stem
            slugs = self.wb_slugs.get(wid, [])
            data = self._json(rel) or {}
            perms = data.get("permissions", data) if isinstance(data, dict) else {}
            for gc in perms.get("granteeCapabilities") or []:
                todo = ""
                if gc.get("group"):
                    gid = gc["group"].get("id", "")
                    gname = self.groups.get(gid)
                    principal = f"group:{gname or gid}"
                    if not gname:
                        todo = f"Tableau group id {gid} not in groups.json; name it"
                    elif gname == "All Users":
                        principal = "*"
                else:
                    uid = (gc.get("user") or {}).get("id", "")
                    principal = f"user:{uid}"
                    todo = f"Tableau user id {uid}: replace with the user's email (GET /sites/{{site}}/users/{uid}) or move the grant to a group"
                caps = (gc.get("capabilities") or {}).get("capability") or []
                if isinstance(caps, dict):
                    caps = [caps]
                for cap in caps:
                    action = CAPABILITY_ACTION.get(cap.get("name", ""))
                    if not action:
                        continue
                    effect = "forbid" if cap.get("mode") == "Deny" else "permit"
                    for slug in slugs:
                        key = (principal, action, slug, effect)
                        if key in seen:
                            continue
                        seen.add(key)
                        self.b.permissions.append(Permission(
                            principal, action, "Notebook", slug, effect,  # type: ignore[arg-type]
                            source_expression=f"{cap.get('name')}: {cap.get('mode')}", source_ref=f"tableau workbook {wid} permissions",
                            todo=todo,
                        ))
        if self.b.permissions:
            self.b.warn("permissions", "Tableau project default permissions and locked projects are not exported; check them before enforcing", "info")
