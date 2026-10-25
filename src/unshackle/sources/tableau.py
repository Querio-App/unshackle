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
from connection relations + metadata-records, plain aggregates, row-level calcs
(IF/CASE/ZN/IFNULL/DATETRUNC/arithmetic), ratios of aggregates, worksheets ->
tiles with generated SQL, dashboards -> notebooks with layout. LODs, table calcs,
parameters, sets, groups, bins, blending and ATTR keep their Tableau formula with
a TODO(unshackle) for the agent (skills/source-tableau, skills/translate-metrics).
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
LOD = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.I)
TABLE_CALC = re.compile(
    r"\b(WINDOW_\w+|RUNNING_\w+|RANK\w*|LOOKUP|INDEX|TOTAL|SIZE|FIRST|LAST|PREVIOUS_VALUE|SCRIPT_\w+|MODEL_\w+)\s*\(", re.I
)
USER_FN = re.compile(r"\b(USERNAME|ISMEMBEROF|USERDOMAIN|FULLNAME|ISUSERNAME|ISFULLNAME|USERATTRIBUTE\w*)\s*\(", re.I)
ATTR_FN = re.compile(r"\bATTR\s*\(", re.I)
DATE_LIT = re.compile(r"#(\d{4}-\d{2}-\d{2})#")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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


class _TabParser(ax._Parser):
    """_aggexpr's parser plus Tableau IF .. THEN .. ELSEIF .. ELSE .. END and CASE .. WHEN .. END."""

    def _is(self, word: str) -> bool:
        return self.peek()[1].lower() == word

    def _word(self, word: str) -> None:
        _, v = self.take()
        if v.lower() != word:
            raise ax.FormulaError(f"expected {word.upper()}, got {v or 'end of formula'!r}")

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
        return super().primary()


def parse_formula(text: str) -> ax.Node:
    text = DATE_LIT.sub(lambda m: f"DATE('{m.group(1)}')", text)
    if not text.strip():
        raise ax.FormulaError("empty formula")
    return _normalize(_TabParser(text).parse())


def _normalize(node: ax.Node) -> ax.Node:
    if isinstance(node, ax.Call):
        args = [_normalize(a) for a in node.args]
        if node.name == "iif":
            return ax.Call("if", args[:3])
        if node.name == "datepart" and len(args) == 2 and isinstance(args[0], ax.Str) and args[0].value.lower() in ("year", "month", "day"):
            return ax.Call(args[0].value.lower(), [args[1]])
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
        return ax.Call(node.name, [_replace_user_fns(a) for a in node.args])
    if isinstance(node, ax.BinOp):
        return ax.BinOp(node.op, _replace_user_fns(node.left), _replace_user_fns(node.right))
    if isinstance(node, ax.Unary):
        return ax.Unary(node.op, _replace_user_fns(node.operand))
    return node


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
    # left-deep join tree as steps after the first relation: (join type, right relation name, ON sql); "" = not translatable
    join_steps: list[tuple[str, str, str]] = field(default_factory=list)
    origin: "TDatasource | None" = None  # the published datasource a workbook's sqlproxy stub was linked to
    inherited: set[str] = field(default_factory=set)  # fields taken unchanged from `origin`
    hierarchies: list[tuple[str, list[str]]] = field(default_factory=list)  # <drill-path name>: [field local names]


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
    kind: str  # row | agg | todo | security
    sql: str = ""
    metric: str = ""
    todo: str = ""


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


class _NoSQL(Exception):
    pass


def read_datasource(el: ET.Element, uid: str, src: str, workbook: str = "", caption: str = "") -> TDatasource:
    name = el.get("name") or el.get("formatted-name") or caption or "datasource"
    ds = TDatasource(uid=uid, name=name, caption=el.get("caption") or caption or el.get("formatted-name") or name, src=src, workbook=workbook)
    conn = el.find("connection")
    if conn is not None:
        ds.connection_class = conn.get("class", "")
        if ds.connection_class == "sqlproxy":
            ds.published = conn.get("dbname", "")
        named = {nc.get("name", ""): nc.find("connection") for nc in conn.iter("named-connection")}
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
            f.value = col.get("value", "")
            f.extra = {**f.extra, "members": [m.get("value", "") for m in col.iter("member")]}
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
    for dp in el.findall("drill-paths/drill-path"):
        ds.hierarchies.append((dp.get("name", ""), [_unbracket(x.text) for x in dp.findall("field") if x.text]))
    return ds


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
        ds.join_steps.append((rel.get("join", "inner"), name, _join_on(cond) if name else ""))
    return out


def _join_on(expr: ET.Element | None) -> str:
    """<expression op='='><expression op='[orders].[account_id]' />... -> orders.account_id = accounts.account_id ("" if not that simple)."""
    if expr is None:
        return ""
    op, kids = expr.get("op", ""), list(expr.findall("expression"))
    if op in ("=", "AND") and len(kids) >= 2:
        parts = [_join_on(k) for k in kids]
        if not all(parts) or (op == "=" and len(parts) != 2):
            return ""
        return f" {op.lower()} ".join(f"({p})" if " and " in p else p for p in parts) if op == "AND" else f"{parts[0]} = {parts[1]}"
    names = _name_parts(op) if op.startswith("[") and not kids else []
    return ".".join(_ident(n) for n in names) if len(names) == 2 else ""


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
        self.owner: dict[str, str] = {}  # measure/metric name -> model key
        self.counts: dict[str, set[str]] = {}
        self.defs: dict[tuple[str, str], str] = {}
        self.slugs: set[str] = set()
        self.wb_slugs: dict[str, list[str]] = {}
        self.workbooks: list[_Workbook] = []
        self.tds: list[TDatasource] = []
        self._joined: TDatasource | None = None  # tile SQL over a join: qualify physical columns with their relation

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
                ds = read_datasource(el, f"{rel}#{el.get('name', '')}", rel, workbook=wid)
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
        for ds in data_sources:
            self._assign_models(ds)
        self._count_names(data_sources)
        for ds in data_sources:
            self._base_fields(ds)
        for ds in data_sources:
            for f in list(ds.fields.values()):
                if f.is_calc and not f.param:
                    self._calc(ds, f)
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
        self._permissions()
        for e in self._json("_errors.json") or []:
            if isinstance(e, dict):
                self.b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return self.b.settle_time_dimensions()

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
        ds.hierarchies = ds.hierarchies or list(target.hierarchies)
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

    def _assign_models(self, ds: TDatasource) -> None:
        item = f"{ds.src} datasource {ds.caption}"
        if ds.connection_class in FILE_CONNECTIONS or any(r.connection_class in FILE_CONNECTIONS for r in ds.relations.values()):
            self.b.warn(item, "connects to a file/extract, not a warehouse; point the semantic model at the warehouse table holding this data")
        if ds.joins:
            self.b.warn(item, f"joins {len(ds.relations)} tables ({'; '.join(ds.joins)}); one semantic model per table was made, add foreign entities by hand", "info")
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
            return res
        res = self._translate(ds, f, stack + (key,))
        self.cache[key] = res
        return res

    def params_for(self, ds: TDatasource) -> dict[str, TField]:
        wb = next((w for w in self.workbooks if w.id == ds.workbook), None)
        return wb.params if wb is not None else {}

    def _row_ref(self, ds: TDatasource, name: str) -> str:
        g = ds.fields.get(name)
        if g is None:
            raise ax.FormulaError(f"unknown field [{name}]")
        if g.is_calc:
            c = self.cache.get((ds.uid, g.local))
            if c is not None and c.kind == "row":
                return _paren(c.sql)
            raise ax.FormulaError(f"[{g.label}] is an aggregate or not translated")
        if g.remote:
            return _ident(g.remote)
        raise ax.FormulaError(f"[{g.label}] has no physical column")

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
        wbp = self.params_for(ds)
        params = [wbp[p].label if p in wbp else p for p in (_unesc(x) for x in PARAM_REF.findall(code))]
        if params:
            return self._todo(ds, f, model, f"uses parameter(s) {', '.join(f'[{p}]' for p in params)}: a parameter becomes a notebook parameter (:name, set in the parameters cell) or one metric per value; see {SKILL}")
        lod = LOD.search(bare)
        if lod:
            kind = lod.group(1).upper()
            if kind == "FIXED":
                self._lod_query(ds, f, model, clean)
                msg = "Tableau FIXED LOD: pre-aggregate at that grain in a SQL model (queries/models/), then a semantic model on it; see skills/translate-metrics"
            else:
                msg = f"Tableau {kind} LOD is view-dependent: write it as window SQL in the notebook tile; see skills/translate-metrics"
            return self._todo(ds, f, model, msg)
        if "{" in bare:
            return self._todo(ds, f, model, "table-scoped LOD ({ AGG(...) } = grand total): a window over all rows (`sum(x) over ()`) in the tile SQL; see skills/translate-metrics")
        tc = TABLE_CALC.search(bare)
        if tc:
            return self._todo(ds, f, model, f"Tableau table calculation {tc.group(1).upper()}() depends on the view: compute it as window SQL in the notebook tile (skills/translate-dashboards), not a metric; see skills/translate-metrics")
        other = sorted({_unesc(d) for d, _ in DS_REF.findall(code) if _unesc(d) != "Parameters"})
        if other:
            return self._todo(ds, f, model, f"references another datasource ({', '.join(other)}): data blending; join the tables in SQL or through entities; see {SKILL}")
        if ATTR_FN.search(bare):
            return self._todo(ds, f, model, f"ATTR() returns the value only when it is constant in the view: use the dimension itself or min()/max(); see {SKILL}")
        try:
            node = parse_formula(clean)
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"formula not translated ({exc}); see skills/translate-metrics")
        dep_agg = False
        for r in ax.refs(node):
            g = ds.fields.get(r)
            if g is None:
                return self._todo(ds, f, model, f"references unknown field [{r}]; see skills/translate-metrics")
            if g.is_calc and not g.param:
                dep = self._calc(ds, g, stack)
                if dep.kind in ("todo", "security"):
                    return self._todo(ds, f, model, f"depends on [{g.label}], which is not translated yet; finish that first (skills/translate-metrics)")
                dep_agg = dep_agg or dep.kind == "agg"
        if model is None:
            return self._todo(ds, f, None, "datasource has no table; see skills/translate-metrics")
        src = f"{ds.src} datasource: {ds.caption} field: {f.label}"
        if _has_agg(node) or dep_agg:
            return self._aggregate(ds, f, model, node, src)
        try:
            sql = ax.strip_outer(ax.to_sql(node, lambda r: self._row_ref(ds, r)))
        except ax.FormulaError as exc:
            return self._todo(ds, f, model, f"formula not translated ({exc}); see skills/translate-metrics")
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

    def _aggregate(self, ds: TDatasource, f: TField, model: _Model, node: ax.Node, src: str) -> _Calc:
        try:
            cl = ax.classify(node, LANG, lambda r: self._row_ref(ds, r), lambda r: self._metric_ref(ds, r))
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

    def _lod_query(self, ds: TDatasource, f: TField, model: _Model | None, clean: str) -> None:
        m = re.fullmatch(r"\s*\{\s*FIXED\s+(.*?)\s*:\s*(.+?)\s*\}\s*", clean, re.S | re.I)
        if not m or model is None:
            return
        try:
            dims = [self._row_ref(ds, _unesc(d)) for d in BRACKETED.findall(m.group(1))]
            cl = ax.classify(parse_formula(m.group(2)), LANG, lambda r: self._row_ref(ds, r), lambda r: None)
        except ax.FormulaError:
            return
        if cl.kind != "agg" or cl.agg is None or not dims:
            return
        from_sql, cte = model.from_sql
        alias = _name(f.label)
        lines = [f"with custom_sql as (\n{cte}\n)"] if cte else []
        lines += [f"select {', '.join(dims)}, {AGG_SQL[cl.agg.agg].format(cl.agg.expr)} as {alias}", f"from {from_sql}",
                  f"group by {', '.join(str(i) for i in range(1, len(dims) + 1))}"]
        self.b.queries.append(VerifiedQuery(
            f"{f.label} (FIXED LOD)", "\n".join(lines), f"Tableau {clean}", datasource=self.b.datasource,
            source_ref=f"{ds.src} datasource: {ds.caption} field: {f.label}",
            todo=(f"Tableau FIXED LOD at grain ({', '.join(dims)}): save as a SQL model (queries/models/{alias}.sql; dbt: models/unshackle/{alias}.sql) "
                  f"and add a semantic model on it with a primary entity on those columns; see skills/translate-metrics"),
        ))

    def _row_filter(self, ds: TDatasource, f: TField, model: _Model | None, clean: str) -> None:
        table = model.sm.table if model else ""
        applied = any(_unbracket(fl.get("column")) == f.local for fl in ds.filters)
        predicate, problems = "", []
        groups: list[str] = []
        try:
            node = parse_formula(clean)
            groups = [a.args[0].value for a in _find_calls(node, {"ismemberof"}) if a.args and isinstance(a.args[0], ax.Str)]
            if groups:
                problems.append(
                    f"ISMEMBEROF({', '.join(repr(g) for g in groups)}): members of those groups bypass this filter in Tableau; "
                    "an unshackle row filter applies to its `groups`, so apply it to everyone else (or split the rule)"
                )
            else:
                predicate = ax.to_sql(_replace_user_fns(node), lambda r: "{{ " + r[1:] + " }}" if r.startswith("\x00") else self._row_ref(ds, r))
                predicate = ax.strip_outer(predicate)
        except ax.FormulaError as exc:
            problems.append(f"not translated ({exc})")
        problems.append(
            "Tableau USERNAME() is the Tableau user name (the email on Tableau Cloud, often a login on Server); "
            "confirm {{ user.email }} is the right attribute; see skills/translate-permissions"
        )
        if not applied:
            problems.append("this calc was not a datasource filter; check which worksheets filter on it before enforcing")
        if model is not None and model.rel.kind == "text":
            problems.append(f"table is Tableau custom SQL ({model.sm.name}); point the rule at the table(s) behind it")
        self.b.row_filters.append(RowFilter(
            _name(f.label), table, predicate, ["*"], source_expression=clean,
            source_ref=f"{ds.src} datasource: {ds.caption} field: {f.label}" + (" (datasource filter)" if applied else ""),
            todo="; ".join(problems),
        ))

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
        if calc is not None and calc.kind in ("todo", "security"):
            raise _NoSQL(f"[{label}] is not translated yet: {calc.todo}")
        base = calc.sql if calc is not None and calc.kind == "row" else (_ident(f.remote) if f.remote else "")
        if calc is None and base and ds is self._joined and f.relation:
            base = f"{_ident(f.relation)}.{base}"
        if calc is None and not base:
            raise _NoSQL(f"[{label}] has no physical column")
        d = r.deriv
        if d == "usr" or (calc is not None and calc.kind == "agg"):
            if calc is not None and calc.kind == "agg":
                sql = calc.sql
            else:
                sql = AGG_SQL[DEFAULT_AGG.get(f.default_agg, "sum")].format(base)
            etype = "nominal" if f.datatype in ("string", "boolean") else "quantitative"
            return _name(label), sql, True, label, etype, ""
        if d in DERIV_AGG:
            display = DERIV_LABEL[d].format(label)
            return _name(display), AGG_SQL[DERIV_AGG[d]].format(base), True, display, "quantitative", ""
        if d == "attr":
            raise _NoSQL(f"ATTR([{label}]) on a shelf: group by the dimension or use min()/max()")
        if d in TRUNC:
            unit = TRUNC[d]
            display = f"{UNIT_LABEL[unit]} of {label}"
            return _name(display), f"date_trunc('{unit}', {base})", False, display, "temporal", unit
        if d in PART:
            unit = PART[d]
            display = f"{UNIT_LABEL[unit]} of {label}"
            return _name(display), f"extract({unit} from {base})", False, display, "ordinal", ""
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
        join_from = self._tile_join(wb, ordered, data_ds)
        self._joined = wb.datasources[data_ds[0]] if join_from else None  # set for this tile's _column/_filter calls
        problems: list[str] = []
        columns: list[tuple[_Ref, str, str, bool, str, str, str]] = []
        dropped: set[str] = set()
        for r, critical in ordered:
            try:
                alias, sql, is_agg, display, etype, unit = self._column(r)
            except _NoSQL as exc:
                if critical:
                    problems.append(str(exc))
                else:
                    todos.append(f"tooltip field left out: {exc}")
                    dropped.add(r.instance)
                continue
            columns.append((r, alias, sql, is_agg, display, etype, unit))
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
        elif len(models) > 1 and not join_from:
            problems.append("fields come from several joined tables: write the join in the tile SQL")
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
                todos.append(v)
        sql = ""
        if problems:
            todos += problems
        elif columns and models:
            model = next(iter(models.values()))
            from_sql, cte = (join_from, "") if join_from else model.from_sql
            dims = [c for c in columns if not c[3]]
            meas = [c for c in columns if c[3]]
            columns = dims + meas
            lines = [f"with custom_sql as (\n{cte}\n)"] if cte else []
            lines.append("select")
            lines.append(",\n".join(f"  {c[2]}" if c[2] == c[1] else f"  {c[2]} as {c[1]}" for c in columns))
            lines.append(f"from {from_sql}")
            if where:
                lines.append("where " + "\n  and ".join(where))
            if dims:
                lines.append("group by " + ", ".join(str(i) for i in range(1, len(dims) + 1)))
            if having:
                lines.append("having " + "\n  and ".join(having))
            if order:
                lines.append("order by " + ", ".join(order))
            elif dims:
                lines.append("order by " + ", ".join(str(i) for i in range(1, len(dims) + 1)))
            if limit:
                lines.append(f"limit {limit}")
            sql = "\n".join(lines)
            if cte and ("<[Parameters]" in cte or PARAM_REF.search(cte)):
                todos.append("custom SQL uses Tableau parameters (<[Parameters].[x]>): replace them with :binds from the parameters cell")
        elif not columns and not problems:
            todos.append("no fields on the sheet's shelves")
        by_inst = {c[0].instance: c for c in columns}

        def to_encoding(r: _Ref) -> Encoding | None:
            c = by_inst.get(r.instance)
            if c is not None:
                return Encoding(c[1], c[5], title=c[4], time_unit="" if sql else ALTAIR_UNIT.get(c[6], ""))  # type: ignore[arg-type]
            if r.instance in dropped:
                return None
            try:
                alias, _, _, display, etype, unit = self._column(r)
            except _NoSQL:
                label = r.field.label if r.field else r.instance
                alias, display, etype, unit = _name(label), label, "quantitative" if self._is_measure(r) else "nominal", ""
            return Encoding(alias, etype, title=display, time_unit=ALTAIR_UNIT.get(unit, ""))  # type: ignore[arg-type]

        encodings = {ch: e for ch, r in chosen.items() if (e := to_encoding(r)) is not None}
        layers = [Layer(m, {ch: e}) for m, ch, r in layer_refs if (e := to_encoding(r)) is not None]  # type: ignore[arg-type]
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
        tile = Tile(tid, name, kind, mark, sql, encodings, source_type=mark_cls, source_config=config,  # type: ignore[arg-type]
                    todo="; ".join(dict.fromkeys(todos)), layers=layers, compose=compose)  # type: ignore[arg-type]
        meta["aliases"] = aliases
        return tile, meta

    def _tile_join(self, wb: _Workbook, ordered: list[tuple[_Ref, bool]], data_ds: list[str]) -> str:
        """The FROM clause when the sheet's fields span several tables of one datasource's join tree, else ""."""
        ds = wb.datasources.get(data_ds[0]) if len(data_ds) == 1 else None
        if ds is None or not ds.join_steps or not all(on for _, _, on in ds.join_steps):
            return ""
        by_rel = self.ds_models.get(ds.uid) or {}
        used = {id(m) for r, _ in ordered if r.ds is ds and r.field and (m := self._field_model(ds, r.field)) is not None}
        if len(used) < 2 or any(m.rel.kind != "table" for m in by_rel.values()):
            return ""
        first = next(iter(ds.relations))
        if first not in by_rel or any(name not in by_rel for _, name, _ in ds.join_steps):
            return ""
        sql = f"{by_rel[first].sm.table} as {_ident(first)}"
        for kind, name, on in ds.join_steps:
            sql += f"\n{kind.lower()} join {by_rel[name].sm.table} as {_ident(name)} on {on}"
        return sql

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
            bound = self._param_bound(wb, r)
            if bound is None:
                return ("todo", f"filter on [{label}] not applied: {exc}")
            expr, is_agg = bound, False
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

    def _param_bound(self, wb: _Workbook, r: _Ref) -> str | None:
        """A row-level calc whose only blocker is a workbook parameter, with each [Parameters].[x] as a :x bind (tile SQL only)."""
        f, ds = r.field, r.ds
        if f is None or ds is None or not f.formula or r.deriv not in ("none", ""):
            return None
        clean, code, bare, _ = scan_formula(f.formula)
        if not PARAM_REF.search(code) or LOD.search(bare) or "{" in bare or TABLE_CALC.search(bare) or USER_FN.search(bare):
            return None
        binds: dict[str, str] = {}

        def bind(m: re.Match) -> str:
            p = wb.params.get(_unesc(m.group(1)))
            if p is None:
                raise ax.FormulaError(f"unknown parameter [{m.group(1)}]")
            key = f"unshackle_param_{len(binds)}"
            binds[key] = ":" + _name(p.label)
            return f"[{key}]"

        try:
            node = parse_formula(PARAM_REF.sub(bind, clean))
            if _has_agg(node):
                return None
            return ax.strip_outer(ax.to_sql(node, lambda n: binds[n] if n in binds else self._row_ref(ds, n)))
        except ax.FormulaError:
            return None

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
        node = parse_formula(scan_formula(expression)[0])
        cl = ax.classify(node, LANG, lambda r: self._row_ref(ds, r), lambda r: self._metric_ref(ds, r))
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
        return DashboardFilter(_name(p.label), "", _param_value(p.value, p.datatype), kind)  # type: ignore[arg-type]

    def _dashboards(self, wb: _Workbook) -> None:
        worksheets = {ws.get("name", ""): ws for ws in wb.root.findall("worksheets/worksheet")}
        built: dict[str, tuple[Tile, dict[str, Any]]] = {}

        def tile_for(name: str) -> tuple[Tile, dict[str, Any]]:
            if name not in built:
                try:
                    built[name] = self._tile(wb, worksheets[name])
                except Exception as exc:  # noqa: BLE001
                    built[name] = (Tile(slugify(name), name, "unknown", todo=f"worksheet not parsed: {exc}"), {"params": [], "filters": {}})
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
            lines += ["", f"## {ds.caption}", "", f"Tables: {tables or 'none'}."]
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
