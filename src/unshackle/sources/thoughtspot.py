"""ThoughtSpot source (REST API v2 + TML).

LICENSING (clean-room): ThoughtSpot's own libraries (thoughtspot_tml, cs_tools,
thoughtspot_rest_api_python, the @thoughtspot/rest-api-sdk / thoughtspot-rest-api-sdk
packages) ship under terms that forbid using them to build converters to
non-ThoughtSpot tools. This module must never import, vendor or copy from them.
It calls the documented public REST API v2 with httpx and parses TML (plain YAML)
with pyyaml, from the public docs only. Keep it that way in any change.

Auth: `POST /api/rest/2.0/auth/token/full` with username + secret_key (trusted
auth) or password -> bearer token. THOUGHTSPOT_ORG_ID selects an org on multi-org
clusters.

Raw layout under migration/raw/thoughtspot/:

    metadata/_site.json                   {"url": THOUGHTSPOT_URL} so parse() can build links offline
    metadata/<TYPE>.json                  POST /metadata/search results (LOGICAL_TABLE, LIVEBOARD, ANSWER) with stats, all pages
    tml/<kind>/<guid>.tml                 each TML edoc exactly as exported; <kind> is the TML top-level key
                                          (table, model, worksheet, sql_view, view, liveboard, answer, nls_feedback)
    sql/liveboard/<guid>.json             POST /metadata/liveboard/sql response (SQL per visualization)
    sql/answer/<guid>.json                POST /metadata/answer/sql response
    permissions/<TYPE>.json               POST /security/metadata/fetch-permissions (who objects are shared with)
    _errors.json                          items extract could not fetch (become warnings in parse)

parse() translates deterministically:
  * table TML -> Table (catalog) with db column names and types
  * model / worksheet TML -> one SemanticModel per physical table, joins -> entities,
    ATTRIBUTE -> dimensions, MEASURE + aggregation -> measures + simple metrics,
    formulas: sum/unique_count (or `unique count`)/average/... -> measures, sum_if -> metric
    filters, safe_divide, a / b or a / nullif(b, 0) -> ratio metrics, arithmetic over measures -> derived metrics,
    if/then/else -> CASE dimensions
  * synonyms, ai_context, descriptions, Spotter feedback -> knowledge files
  * liveboards -> notebooks using ThoughtSpot's generated SQL (ca_N aliases renamed to the
    answer's column names), saved answers -> verified queries
  * RLS rules (ts_groups / ts_username) -> row filters, column security -> Cedar forbid,
    sharing (READ_ONLY / MODIFY) on liveboards and models -> Cedar permits
Everything else (group_aggregate and other LOD functions, cumulative_*, moving_*,
cross-table formulas) keeps its ThoughtSpot expression with a TODO(unshackle).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

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
from unshackle.sources import _aggexpr as fx
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source

LANG = "thoughtspot"
API = "/api/rest/2.0"
AGG = {
    "SUM": "sum",
    "COUNT": "count",
    "COUNT_DISTINCT": "count_distinct",
    "UNIQUE_COUNT": "count_distinct",
    "AVERAGE": "average",
    "AVG": "average",
    "MIN": "min",
    "MAX": "max",
    "MEDIAN": "median",
}
TIME_TYPES = {"DATE", "DATE_TIME", "DATETIME", "TIMESTAMP", "TIME"}
HARD_FUNCS = {
    "group_aggregate", "group_sum", "group_count", "group_average", "group_max", "group_min",
    "group_unique_count", "group_stddev", "group_variance",
    "cumulative_sum", "cumulative_average", "cumulative_max", "cumulative_min",
    "moving_sum", "moving_average", "moving_max", "moving_min",
    "rank", "rank_percentile", "query_filters", "query_groups", "query_groups_plus",
    "last_value", "first_value", "sql_int_op", "sql_string_op", "sql_date_op", "sql_number_op",
}
MARKS = {
    "COLUMN": "bar", "STACKED_COLUMN": "bar", "BAR": "bar", "STACKED_BAR": "bar",
    "LINE": "line", "AREA": "area", "STACKED_AREA": "area",
    "PIE": "arc", "DONUT": "arc", "SCATTER": "point", "BUBBLE": "point",
}
BUCKET = re.compile(r"^(hourly|daily|weekly|monthly|quarterly|yearly|day|week|month|quarter|year|hour)\s*\((.+)\)$", re.I)
AGG_PREFIX = re.compile(r"^(total|unique number of|unique count of|number of|count of|average|avg|max|min|minimum|maximum|sum of)\s+", re.I)
TABLE_PREFIX = re.compile(r"^(fct|fact|dim|d|f|stg|int)_", re.I)
TML_KINDS = ("table", "model", "worksheet", "sql_view", "view", "liveboard", "pinboard", "answer", "nls_feedback")


_UNIQUE_COUNT = re.compile(r"""(\[[^\]]*\]|'(?:[^']|'')*'|"(?:[^"]|"")*")|\bunique\s+count(?=\s*\()""", re.I)


def _parse(expr: str) -> fx.Node:
    """fx.parse plus ThoughtSpot spellings: `unique count ( x )` and `nullif ( a , b )`."""
    text = _UNIQUE_COUNT.sub(lambda m: m.group(1) or "unique_count", expr)
    return _unwrap(fx.parse(text))


def _unwrap(node: fx.Node) -> fx.Node:
    """Drop a divisor nullif(x, 0): generated division already uses nullif(x, 0), and the
    bare aggregate lets `sum(a) / nullif(sum(b), 0)` stay a ratio metric."""
    if isinstance(node, fx.Call):
        args = [_unwrap(a) for a in node.args]
        if node.name == "safe_divide" and len(args) == 2:
            args[1] = _unwrap_zero(args[1])
        return fx.Call(node.name, args)
    if isinstance(node, fx.BinOp):
        right = _unwrap(node.right)
        return fx.BinOp(node.op, _unwrap(node.left), _unwrap_zero(right) if node.op == "/" else right)
    if isinstance(node, fx.Unary):
        return fx.Unary(node.op, _unwrap(node.operand))
    return node


def _unwrap_zero(node: fx.Node) -> fx.Node:
    if isinstance(node, fx.Call) and node.name == "nullif" and len(node.args) == 2 and isinstance(node.args[1], fx.Num) and float(node.args[1].value) == 0:
        return node.args[0]
    return node


def _ident(value: str) -> str:
    return value.lower() if value.isupper() else value


def _singular(name: str) -> str:
    s = TABLE_PREFIX.sub("", snake(name))
    if s.endswith("ies"):
        return s[:-3] + "y"
    if s.endswith("s") and not s.endswith("ss"):
        return s[:-1]
    return s


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value) or "_"


def _kind_of(doc: dict) -> str:
    for k in TML_KINDS:
        if k in doc:
            return k
    keys = [k for k in doc if k not in ("guid", "obj_id")]
    return keys[0] if keys else "unknown"


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        v = value.get("name") or value.get("column_id") or value.get("id")
        return [str(v)] if v else []
    if isinstance(value, list):
        return [n for v in value for n in _names(v)]
    return []


class ThoughtSpotSource(Source):
    name = "thoughtspot"
    label = "ThoughtSpot"
    env_vars = ("THOUGHTSPOT_URL", "THOUGHTSPOT_USERNAME", "THOUGHTSPOT_PASSWORD", "THOUGHTSPOT_SECRET_KEY", "THOUGHTSPOT_ORG_ID")
    docs = "https://developers.thoughtspot.com/docs/rest-apiv2-reference"
    manual_files = "TML files exported from ThoughtSpot (.tml, or the .zip that Export TML downloads), or folders of them"
    manual_gaps = (
        "generated SQL: liveboard and answer SQL only comes from the API, so their tiles carry a TODO to write it",
        "usage: no view counts or last-viewed dates",
        "permissions: sharing (who can view or edit) is not in TML; RLS rules in table TML are kept",
        "links: no cluster URL, so nothing links back to ThoughtSpot",
    )

    def __init__(self, client: Client | None = None):
        self._client_obj = client
        self._expires = 0.0

    # ------------------------------------------------------------------ network

    def _client(self) -> Client:
        if self._client_obj is None:
            # ThoughtSpot Cloud does not publish REST v2 limits; stay polite (TML export is the heavy call).
            self._client_obj = Client(env("THOUGHTSPOT_URL"), per_minute=300, headers={"Accept": "application/json"})
        if time.monotonic() >= self._expires:
            self._auth()
        return self._client_obj

    def _auth(self) -> None:
        c = self._client_obj
        assert c is not None
        body: dict[str, Any] = {"username": env("THOUGHTSPOT_USERNAME"), "validity_time_in_sec": 3600}
        secret = env("THOUGHTSPOT_SECRET_KEY", required=False)
        if secret:
            body["secret_key"] = secret
        else:
            body["password"] = env("THOUGHTSPOT_PASSWORD", required=False)
            if not body["password"]:
                raise SourceError("set THOUGHTSPOT_SECRET_KEY (trusted auth) or THOUGHTSPOT_PASSWORD")
        org = env("THOUGHTSPOT_ORG_ID", required=False) or env("THOUGHTSPOT_ORG", required=False)
        if org:
            body["org_id"] = int(org) if org.isdigit() else org
        data = c.post_json(f"{API}/auth/token/full", json=body)
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise SourceError("ThoughtSpot auth returned no token")
        c.set_header("Authorization", f"Bearer {token}")
        life = 3600.0
        if data.get("expiration_time_in_millis") and data.get("creation_time_in_millis"):
            life = (float(data["expiration_time_in_millis"]) - float(data["creation_time_in_millis"])) / 1000
        self._expires = time.monotonic() + max(life - 300, 60)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            return self._client().post_json(f"{API}{path}", json=body)
        except SourceError as exc:
            if " -> 401" not in str(exc):
                raise
            self._expires = 0.0  # token expired or revoked: log in again once
            return self._client().post_json(f"{API}{path}", json=body)

    def _search(self, mtype: str, page_size: int = 200) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            data = self._post("/metadata/search", {"metadata": [{"type": mtype}], "include_stats": True,
                                                   "record_size": page_size, "record_offset": offset})
            page = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
            out += page
            if len(page) < page_size:
                return out
            offset += page_size

    def _base_url(self) -> str:
        c = self._client_obj
        return c.base_url if c is not None else env("THOUGHTSPOT_URL", required=False).rstrip("/")

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        base = ""
        for mtype in ("LOGICAL_TABLE", "LIVEBOARD", "ANSWER"):
            for item in self._search(mtype):
                base = base or self._base_url()
                kind = _item_kind(item)
                guid = item.get("metadata_id", "")
                views, last = _stats(item)
                inv.add(kind, guid, item.get("metadata_name", ""), views=views, last_viewed=last, url=_url(base, kind, guid),
                        extra={"author": (item.get("metadata_header") or {}).get("authorName", "")})
        inv.notes.append("Views come from include_stats on /metadata/search. Tables arrive with their models via export_associated.")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        found: dict[str, list[dict]] = {}
        for mtype in ("LOGICAL_TABLE", "LIVEBOARD", "ANSWER"):
            try:
                found[mtype] = self._search(mtype)
            except SourceError as exc:
                found[mtype] = []
                errors.append({"item": f"search {mtype}", "message": str(exc)})
            store.put_json(f"metadata/{mtype}.json", found[mtype])
        store.put_json("metadata/_site.json", {"url": self._base_url()})

        def wanted(item: dict, kind: str) -> bool:
            guid = str(item.get("metadata_id", ""))
            if only:
                return guid in only
            return kind not in ("table",)  # tables come with their models via export_associated

        targets = [(i, _item_kind(i)) for items in found.values() for i in items]
        targets = [(i, k) for i, k in targets if wanted(i, k)]
        done: set[str] = set()
        for item, _kind in sorted(targets, key=lambda t: ("liveboard", "answer").index(t[1]) if t[1] in ("liveboard", "answer") else -1):
            guid = str(item.get("metadata_id", ""))
            if not guid or guid in done:
                continue
            try:
                for edoc_guid in self._export(store, guid, errors):
                    done.add(edoc_guid)
                done.add(guid)
            except SourceError as exc:
                errors.append({"item": f"tml {item.get('metadata_name', guid)} ({guid})", "message": str(exc)})
        for item, kind in targets:
            guid = str(item.get("metadata_id", ""))
            if kind not in ("liveboard", "answer"):
                continue
            key = "metadata_identifier"
            try:
                data = self._post(f"/metadata/{kind}/sql", {key: guid})
                store.put_json(f"sql/{kind}/{_safe(guid)}.json", data)
            except SourceError as exc:
                errors.append({"item": f"sql {kind} {item.get('metadata_name', guid)} ({guid})", "message": str(exc)})
        self._permissions(store, targets, errors)
        store.put_json("_errors.json", errors)

    def _permissions(self, store: RawStore, targets: list[tuple[dict, str]], errors: list[dict[str, str]]) -> None:
        by_type: dict[str, list[str]] = {}
        for item, kind in targets:
            if kind in ("liveboard", "model", "worksheet", "answer"):
                mtype = {"liveboard": "LIVEBOARD", "answer": "ANSWER"}.get(kind, "LOGICAL_TABLE")
                by_type.setdefault(mtype, []).append(str(item.get("metadata_id", "")))
        for mtype, guids in by_type.items():
            details: list[Any] = []
            try:
                for i in range(0, len(guids), 50):
                    data = self._post("/security/metadata/fetch-permissions",
                                      {"metadata": [{"identifier": g, "type": mtype} for g in guids[i:i + 50]],
                                       "include_dependent_objects": False})
                    details.append(data)
                store.put_json(f"permissions/{mtype}.json", details)
            except SourceError as exc:
                errors.append({"item": f"permissions {mtype}", "message": f"{exc}; sharing not migrated (needs admin or can-manage-sharing)"})

    def _export(self, store: RawStore, guid: str, errors: list[dict[str, str]]) -> list[str]:
        body: dict[str, Any] = {
            "metadata": [{"identifier": guid}],
            "export_associated": True,
            "export_fqn": True,
            "edoc_format": "YAML",
            # 10.7+: Spotter feedback (nls_feedback TML); 10.12+: column security rules (needs export_associated)
            "export_options": {"export_with_associated_feedbacks": True, "export_column_security_rules": True},
        }
        try:
            data = self._post("/metadata/tml/export", body)
        except SourceError as exc:
            if " -> 400" not in str(exc):
                raise
            # older clusters reject export_options
            body.pop("export_options")
            data = self._post("/metadata/tml/export", body)
            errors.append({"item": f"tml {guid}", "message": "cluster rejected export_options; Spotter feedback and column security not exported"})
        written = []
        for entry in data if isinstance(data, list) else []:
            info = entry.get("info") or {}
            status = (info.get("status") or {}).get("status_code", "OK")
            edoc = entry.get("edoc")
            if status != "OK" or not edoc:
                msg = (info.get("status") or {}).get("error_message", "no edoc")
                errors.append({"item": f"tml {info.get('name', '')} ({info.get('id') or guid})", "message": f"export failed: {msg}"})
                continue
            try:
                doc = yaml.safe_load(edoc) or {}
            except yaml.YAMLError:
                doc = {}
            edoc_guid = str(info.get("id") or doc.get("guid") or guid)
            kind = _kind_of(doc) if isinstance(doc, dict) else str(info.get("type", "unknown")).lower()
            store.put_text(f"tml/{kind}/{_safe(edoc_guid)}.tml", edoc if edoc.endswith("\n") else edoc + "\n")
            written.append(edoc_guid)
        return written

    # ------------------------------------------------------------------ parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file() and path.suffix != ".tml":
            raise SourceError(f"{path.name}: ThoughtSpot import accepts {self.manual_files}")
        out: dict[str, bytes] = {}
        for _, p in _manual.files_of(path):
            if p.suffix != ".tml":
                continue
            data = p.read_bytes()
            try:
                doc = yaml.safe_load(data.decode("utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                raise SourceError(f"{p.name}: not readable TML: {exc}") from exc
            if not isinstance(doc, dict) or not any(k in doc for k in TML_KINDS):
                raise SourceError(f"{p.name}: not a TML object (expected one of {', '.join(TML_KINDS)})")
            out[f"tml/{_kind_of(doc)}/{_safe(str(doc.get('guid') or p.name.split('.')[0]))}.tml"] = data
        return out

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool=self.name)
        if store.exists("_errors.json"):
            for e in store.get_json("_errors.json") or []:
                b.warn(e.get("item", "?"), e.get("message", ""))
        site = store.get_json("metadata/_site.json").get("url", "") if store.exists("metadata/_site.json") else ""
        stats: dict[str, tuple[int | None, str]] = {}
        for rel in store.glob("metadata/*.json"):
            if rel.endswith("_site.json"):
                continue
            for item in store.get_json(rel) or []:
                if isinstance(item, dict):
                    stats[str(item.get("metadata_id"))] = _stats(item)
        docs: dict[str, list[tuple[str, dict]]] = {}
        for rel in store.glob("tml/*/*.tml"):
            try:
                doc = yaml.safe_load(store.get_text(rel))
            except yaml.YAMLError as exc:
                b.warn(rel, f"cannot read TML: {exc}")
                continue
            if not isinstance(doc, dict):
                b.warn(rel, "TML is not a mapping")
                continue
            guid = str(doc.get("guid") or rel.rsplit("/", 1)[1][:-4])
            docs.setdefault(_kind_of(doc), []).append((guid, doc))
        ctx = _Ctx(b, site)
        for guid, doc in docs.get("table", []):
            _guard(b, f"table {guid}", lambda: ctx.table(guid, doc["table"]))
        for kind in ("sql_view", "view"):
            for guid, doc in docs.get(kind, []):
                b.warn(f"{kind} {doc[kind].get('name', guid)}", f"ThoughtSpot {kind} not migrated; recreate it as a warehouse view", "info")
                if kind == "sql_view" and doc[kind].get("sql_query"):
                    b.queries.append(VerifiedQuery(name=f"{doc[kind].get('name', guid)} (ThoughtSpot SQL view)", sql=doc[kind]["sql_query"],
                                                   datasource=b.datasource, source_ref=f"thoughtspot:sql_view/{guid}",
                                                   todo="create this as a warehouse view and point a semantic model at it"))
        for kind in ("model", "worksheet"):
            for guid, doc in docs.get(kind, []):
                _guard(b, f"{kind} {guid}", lambda g=guid, d=doc, k=kind: ctx.model(g, d[k], k))
        for kind in ("liveboard", "pinboard"):
            for guid, doc in docs.get(kind, []):
                _guard(b, f"liveboard {guid}", lambda g=guid, d=doc, k=kind: ctx.liveboard(g, d[k], store, stats.get(g, (None, ""))[0]))
        for guid, doc in docs.get("answer", []):
            _guard(b, f"answer {guid}", lambda g=guid, d=doc: ctx.answer(g, d["answer"], store))
        for guid, doc in docs.get("nls_feedback", []):
            _guard(b, f"feedback {guid}", lambda g=guid, d=doc: ctx.feedback(g, d["nls_feedback"]))
        _guard(b, "permissions", lambda: ctx.permissions(store))
        return b.settle_time_dimensions()


def _guard(b: Bundle, item: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - one bad object must not stop the rest
        b.warn(item, f"skipped: {type(exc).__name__}: {exc}")


def _item_kind(item: dict) -> str:
    mtype = str(item.get("metadata_type", "")).upper()
    header = item.get("metadata_header") or {}
    if mtype == "LIVEBOARD":
        return "liveboard"
    if mtype == "ANSWER":
        return "answer"
    sub = str(header.get("type", "")).upper()
    if sub == "WORKSHEET":
        return "model" if str(header.get("worksheetVersion", "")).upper() == "V2" else "worksheet"
    if sub in ("SQL_VIEW", "AGGR_WORKSHEET"):
        return "view"
    if sub == "MODEL":
        return "model"
    return "table"


def _stats(item: dict) -> tuple[int | None, str]:
    s = item.get("stats") or {}
    views = s.get("views", s.get("view_count"))
    last = s.get("last_accessed") or s.get("lastAccessed") or ""
    if isinstance(last, (int, float)) and last:
        last = datetime.fromtimestamp(last / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return (int(views) if isinstance(views, (int, float)) else None), str(last or "")


def _alias(name: str) -> str:
    base = re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_") or "col"
    return f"c_{base}" if base[0].isdigit() else base


def _html_text(html: str) -> str:
    """Note tile HTML -> Markdown-ish text: keep bold/italic/links/line breaks, drop the rest."""
    t = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    t = re.sub(r"</\s*(p|div|h\d|li)\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<\s*li[^>]*>", "- ", t, flags=re.I)
    t = re.sub(r"<\s*(b|strong)\s*>(.*?)<\s*/\s*(b|strong)\s*>", r"**\2**", t, flags=re.I | re.S)
    t = re.sub(r"<\s*(i|em)\s*>(.*?)<\s*/\s*(i|em)\s*>", r"*\2*", t, flags=re.I | re.S)
    t = re.sub(r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return "\n".join(line.strip() for line in t.strip().splitlines()).strip()


def _permission_details(data: Any, key: str = "") -> list[tuple[str, dict]]:
    """(guid, detail) for every object in fetch-permissions responses (dict keyed by guid, or lists)."""
    out: list[tuple[str, dict]] = []
    if isinstance(data, list):
        for d in data:
            out += _permission_details(d)
    elif isinstance(data, dict):
        if "principal_permission_info" in data:
            out.append((str(data.get("metadata_id") or key), data))
        else:
            for k, v in data.items():
                out += _permission_details(v, k)
    return out


def _url(base: str, kind: str, guid: str) -> str:
    if not base or not guid:
        return ""
    path = {"liveboard": "pinboard", "answer": "saved-answer"}.get(kind, "data/tables")
    return f"{base.rstrip('/')}/#/{path}/{guid}"


class _TableInfo:
    def __init__(self, guid: str, tml: dict):
        self.guid = guid
        self.tml = tml
        self.name = str(tml.get("name", guid))
        self.db_table = _ident(str(tml.get("db_table") or self.name))
        self.schema = _ident(str(tml.get("schema", "")))
        self.db = _ident(str(tml.get("db", "")))
        self.cols: dict[str, dict] = {str(c.get("name", "")).casefold(): c for c in tml.get("columns", []) or [] if isinstance(c, dict)}
        self.joins = {str(j.get("name", "")).casefold(): j for j in tml.get("joins_with", []) or [] if isinstance(j, dict)}

    @property
    def relation(self) -> str:
        return ".".join(p for p in (self.db, self.schema, self.db_table) if p)

    def phys(self, col: str) -> str:
        c = self.cols.get(col.casefold())
        return _ident(str((c or {}).get("db_column_name") or col))

    def data_type(self, col: str) -> str:
        c = self.cols.get(col.casefold()) or {}
        return str((c.get("db_column_properties") or {}).get("data_type", "")).upper()


class _Sem:
    """A semantic model under construction, one per physical table."""

    def __init__(self, model: SemanticModel, info: _TableInfo | None):
        self.model = model
        self.info = info
        self.entity = ""
        self.by_agg: dict[tuple[str, str], str] = {}
        self.simple: dict[tuple[str, str], str] = {}


class _Ctx:
    def __init__(self, b: Bundle, site: str):
        self.b = b
        self.site = site
        self.tables: dict[str, _TableInfo] = {}
        self.tables_by_name: dict[str, _TableInfo] = {}
        self.sems: dict[str, _Sem] = {}  # relation -> semantic model
        self.model_cols: dict[str, dict[str, Any]] = {}  # model name (casefold) -> {column name casefold: col}
        self.model_by_guid: dict[str, str] = {}
        self.model_tables: dict[str, list[str]] = {}  # model guid -> table relations
        self.liveboard_slugs: dict[str, str] = {}
        self.metric_names: set[str] = set()

    # -------------------------------------------------------------- tables

    def table(self, guid: str, t: dict) -> None:
        info = _TableInfo(guid, t)
        self.tables[guid] = info
        self.tables_by_name[info.name.casefold()] = info
        cols = []
        for c in t.get("columns", []) or []:
            if not isinstance(c, dict):
                continue
            props = c.get("properties") or {}
            cols.append(Column(
                name=_ident(str(c.get("db_column_name") or c.get("name", ""))),
                type=str((c.get("db_column_properties") or {}).get("data_type", "")).lower(),
                description=str(c.get("description") or props.get("description") or ""),
                label=str(c.get("name", "")),
                hidden=bool(props.get("is_hidden")),
            ))
        self.b.tables.append(Table(self.b.datasource, info.schema, info.db_table, str(t.get("description", "") or ""), cols,
                                   source_ref=f"thoughtspot:table/{guid}"))
        self.rls(info, t)
        self.column_security(info, t)

    def rls(self, info: _TableInfo, t: dict) -> None:
        rules = t.get("rls_rules") or {}
        paths = {str(p.get("id", "")).casefold(): str(p.get("table", "")) for p in rules.get("table_paths", []) or [] if isinstance(p, dict)}
        for rule in rules.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            expr = str(rule.get("expr", ""))
            rf = RowFilter(name=snake(f"{info.db_table}_{rule.get('name', 'rls')}"), table=info.relation, groups=["*"],
                           source_expression=expr, source_ref=f"thoughtspot:table/{info.guid}/rls/{rule.get('name', '')}")

            def ref(name: str) -> str:
                low = name.lower()
                if low in ("ts_groups", "ts_groups_int"):
                    return "__TS_GROUPS__"
                if low == "ts_username":
                    return "{{ user.email }}"
                alias, _, col = name.rpartition("::")
                target = self.tables_by_name.get(paths.get(alias.casefold(), alias).casefold(), info)
                return target.phys(col or name)

            try:
                pred = fx.strip_outer(fx.to_sql(_parse(expr), ref))
                pred = re.sub(r"(\S+) = __TS_GROUPS__", r"\1 in ({{ user.groups }})", pred)
                pred = re.sub(r"__TS_GROUPS__ = (\S+)", r"\1 in ({{ user.groups }})", pred)
                if "__TS_GROUPS__" in pred:
                    raise fx.FormulaError("ts_groups used outside a simple equality")
                rf.predicate = pred
                if "{{ user.groups }}" in pred:
                    rf.todo = ("ts_groups is the set of ThoughtSpot groups the user is in, matched against column values; "
                               "confirm your platform expands {{ user.groups }} to a SQL list with the same group names, "
                               "or write one row filter per group")
            except fx.FormulaError as exc:
                rf.todo = f"ThoughtSpot RLS rule not translated ({exc})"
            self.b.row_filters.append(rf)

    def column_security(self, info: _TableInfo, t: dict) -> None:
        rules = t.get("column_security_rules") or t.get("column_level_security") or []
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict):
                continue
            cols = _names(rule.get("column") or rule.get("columns") or rule.get("column_name") or rule.get("name"))
            groups = _names(rule.get("groups") or rule.get("group_access") or rule.get("allowed_groups") or rule.get("principals"))
            cond = " || ".join(f"principal in Group::{json.dumps(g)}" for g in groups)
            for col in cols:
                self.b.permissions.append(Permission(
                    principal="*", action="query", resource_type="Column",
                    resource_id=f"{info.relation}.{info.phys(col)}", effect="forbid",
                    condition=f"!({cond})" if cond else "",
                    source_expression=f"column security: visible to groups {', '.join(groups) or '(none)'}",
                    source_ref=f"thoughtspot:table/{info.guid}/column_security/{col}",
                    todo="" if groups else "ThoughtSpot column security rule without groups; check who may see this column",
                ))

    # -------------------------------------------------------------- models

    def sem_for(self, info: _TableInfo | None, alias: str) -> _Sem:
        relation = info.relation if info else _ident(alias)
        if relation in self.sems:
            return self.sems[relation]
        name = snake(TABLE_PREFIX.sub("", info.db_table if info else alias)) or snake(alias)
        if any(s.model.name == name for s in self.sems.values()):
            name = snake(relation)
        model = SemanticModel(name=name, table=relation, description=str((info.tml.get("description") if info else "") or ""),
                              datasource=self.b.datasource, source_ref=f"thoughtspot:table/{info.guid}" if info else "")
        sem = _Sem(model, info)
        self.sems[relation] = sem
        self.b.semantic_models.append(model)
        if info:
            want = _singular(info.db_table)
            for key, c in info.cols.items():
                if snake(key) in (f"{want}_id", "id") or snake(str(c.get("db_column_name", ""))) == f"{want}_id":
                    sem.entity = want
                    model.entities.append(Entity(want, "primary", info.phys(key)))
                    break
        if not info:
            self.b.warn(f"table {alias}", "table TML not exported; semantic model uses the ThoughtSpot name as the table")
        return sem

    def model(self, guid: str, m: dict, kind: str) -> None:
        mname = str(m.get("name", guid))
        where = f"{kind} {mname}"
        aliases: dict[str, tuple[str, _TableInfo | None]] = {}
        for t in m.get("model_tables", []) or m.get("tables", []) or []:
            if not isinstance(t, dict):
                continue
            alias = str(t.get("name") or t.get("id") or "")
            info = self.tables.get(str(t.get("fqn", ""))) or self.tables_by_name.get(alias.casefold())
            aliases[alias.casefold()] = (alias, info)
            if t.get("id"):
                aliases[str(t["id"]).casefold()] = (alias, info)
        for p in m.get("table_paths", []) or []:
            if isinstance(p, dict) and str(p.get("table", "")).casefold() in aliases:
                aliases[str(p.get("id", "")).casefold()] = aliases[str(p["table"]).casefold()]
        sems = {k: self.sem_for(info, alias) for k, (alias, info) in aliases.items()}
        cols = [c for c in m.get("columns", []) or m.get("worksheet_columns", []) or [] if isinstance(c, dict)]
        by_name = {str(c.get("name", "")).casefold(): c for c in cols}
        by_colid = {str(c.get("column_id", "")).casefold(): c for c in cols if c.get("column_id")}
        formulas = {}
        for f in m.get("formulas", []) or []:
            if isinstance(f, dict):
                formulas[str(f.get("id") or f.get("name")).casefold()] = f
                formulas.setdefault(str(f.get("name", "")).casefold(), f)
        self.model_by_guid[guid] = mname.casefold()
        self.model_tables[guid] = sorted({info.relation for _, info in aliases.values() if info})
        self.model_cols[mname.casefold()] = {"__aliases__": aliases, **by_name}
        self.joins(m, aliases, sems, where)
        used: list[str] = []

        def col_type(c: dict) -> str:
            return str((c.get("properties") or {}).get("column_type", "ATTRIBUTE")).upper()

        def is_measure_col(c: dict) -> bool:
            if col_type(c) == "MEASURE":
                return True
            f = formulas.get(str(c.get("formula_id", "")).casefold())
            if f:
                try:
                    return any(x in fx.AGGS[LANG] for x in fx.calls(_parse(str(f.get("expr", "")))))
                except fx.FormulaError:
                    return False
            return False

        def ref_sql(name: str, depth: int = 0) -> str:
            if depth > 20:
                raise fx.FormulaError("formula references loop")
            if "::" in name:
                alias, _, col = name.partition("::")
                if alias.casefold() not in aliases:
                    raise fx.FormulaError(f"unknown table {alias}")
                used.append(alias.casefold())
                info = aliases[alias.casefold()][1]
                return info.phys(col) if info else _ident(col)
            c = by_name.get(name.casefold())
            if c is None:
                f = formulas.get(name.casefold())
                if f:
                    return fx.strip_outer(fx.to_sql(_parse(str(f.get("expr", ""))), lambda n: ref_sql(n, depth + 1)))
                raise fx.FormulaError(f"unknown column [{name}]")
            if c.get("column_id"):
                return ref_sql(str(c["column_id"]), depth + 1)
            f = formulas.get(str(c.get("formula_id", "")).casefold())
            if f is None:
                raise fx.FormulaError(f"column [{name}] has no column_id or formula")
            if is_measure_col(c):
                raise fx.FormulaError(f"[{name}] is a measure")
            return fx.strip_outer(fx.to_sql(_parse(str(f.get("expr", ""))), lambda n: ref_sql(n, depth + 1)))

        def metric_ref(name: str) -> str | None:
            c = by_name.get(name.casefold())
            return snake(str(c.get("name"))) if c is not None and is_measure_col(c) else None

        def dim_ref(name: str) -> str:
            alias = ""
            if "::" in name:
                alias = name.partition("::")[0].casefold()
                c = by_colid.get(name.casefold())
                dim = snake(str(c.get("name"))) if c else snake(name.partition("::")[2])
            else:
                c = by_name.get(name.casefold())
                if c is None:
                    raise fx.FormulaError(f"unknown column [{name}]")
                if c.get("column_id"):
                    alias = str(c["column_id"]).partition("::")[0].casefold()
                dim = snake(str(c.get("name")))
            sem = sems.get(alias) or next(iter(sems.values()))
            return f"{sem.entity or sem.model.name}__{dim}"

        knowledge: list[str] = []
        if m.get("description"):
            knowledge += [str(m["description"]), ""]
        # plain columns first, then formulas (which may refer to them)
        for c in sorted(cols, key=lambda c: 0 if c.get("column_id") else 1):
            cname = str(c.get("name", ""))
            props = c.get("properties") or {}
            desc = str(c.get("description") or props.get("description") or "")
            syn = [str(s) for s in props.get("synonyms", []) or c.get("synonyms", []) or []]
            ai = str(props.get("ai_context") or c.get("ai_context") or "")
            if syn or ai or desc:
                bits = [f"- **{cname}**"]
                if desc:
                    bits.append(f": {desc}")
                if syn:
                    bits.append(f" Also called: {', '.join(syn)}.")
                if ai:
                    bits.append(f" AI context: {ai}")
                knowledge.append("".join(bits))
            if c.get("column_id"):
                self.plain_column(c, cname, desc, aliases, sems, where)
            elif c.get("formula_id"):
                f = formulas.get(str(c["formula_id"]).casefold())
                if not f:
                    self.b.warn(where, f"column {cname} points at missing formula {c['formula_id']}")
                    continue
                self.formula_column(c, cname, desc, str(f.get("expr", "")), is_measure_col(c), sems, used, ref_sql, metric_ref, dim_ref, where)
        used_formulas = {str(c.get("formula_id", "")).casefold() for c in cols}
        for key, f in formulas.items():
            if key in used_formulas or str(f.get("id", "")).casefold() in used_formulas or key != str(f.get("id") or f.get("name")).casefold():
                continue
            fake = {"name": f.get("name", key), "formula_id": f.get("id", key)}
            self.formula_column(fake, str(f.get("name", key)), "", str(f.get("expr", "")), None, sems, used, ref_sql, metric_ref, dim_ref, where)
        if knowledge:
            self.b.knowledge.append(KnowledgeItem(
                name=f"ThoughtSpot model {mname}",
                body="\n".join(knowledge),
                description=f"Column descriptions, synonyms and AI context from the ThoughtSpot {kind} {mname}.",
                datasource=self.b.datasource,
                source_ref=f"thoughtspot:{kind}/{guid}",
            ))

    def joins(self, m: dict, aliases: dict, sems: dict[str, _Sem], where: str) -> None:
        pairs: list[tuple[str, str, str]] = []  # (source alias, on, cardinality)
        for t in m.get("model_tables", []) or m.get("tables", []) or []:
            if not isinstance(t, dict):
                continue
            src = str(t.get("name") or t.get("id") or "")
            info = aliases.get(src.casefold(), ("", None))[1]
            for j in t.get("joins", []) or []:
                if not isinstance(j, dict):
                    continue
                on = j.get("on")
                if not on and j.get("referencing_join") and info:
                    on = (info.joins.get(str(j["referencing_join"]).casefold()) or {}).get("on")
                pairs.append((src, str(on or ""), str(j.get("cardinality", "MANY_TO_ONE"))))
        for j in m.get("joins", []) or []:  # legacy worksheet joins refer to joins_with on the table
            if not isinstance(j, dict):
                continue
            src = str(j.get("source", ""))
            info = aliases.get(src.casefold(), ("", None))[1]
            on = (info.joins.get(str(j.get("name", "")).casefold()) or {}).get("on") if info else None
            pairs.append((src, str(on or j.get("on") or ""), "ONE_TO_ONE" if j.get("is_one_to_one") else "MANY_TO_ONE"))
        for src, on, card in pairs:
            eq = re.findall(r"\[([^\]:]+)::([^\]]+)\]\s*=\s*\[([^\]:]+)::([^\]]+)\]", on)
            if len(eq) != 1 or " and " in on.lower():
                self.b.warn(f"{where} join from {src}", f"join condition {on!r} is not a single-column equality; add a surrogate key entity by hand")
                continue
            a1, c1, a2, c2 = eq[0]
            if a1.casefold() != src.casefold():
                a1, c1, a2, c2 = a2, c2, a1, c1
            if card.upper() == "ONE_TO_MANY":
                a1, c1, a2, c2 = a2, c2, a1, c1
            s, t = sems.get(a1.casefold()), sems.get(a2.casefold())
            if not s or not t:
                self.b.warn(f"{where} join from {src}", f"join refers to unknown table in {on!r}")
                continue
            si, ti = aliases[a1.casefold()][1], aliases[a2.casefold()][1]
            s_expr = si.phys(c1) if si else _ident(c1)
            t_expr = ti.phys(c2) if ti else _ident(c2)
            if not t.entity:
                t.entity = _singular(ti.db_table if ti else a2)
                t.model.entities.append(Entity(t.entity, "primary", t_expr))
            if not any(e.name == t.entity for e in s.model.entities):
                s.model.entities.append(Entity(t.entity, "foreign", s_expr))

    def plain_column(self, c: dict, cname: str, desc: str, aliases: dict, sems: dict[str, _Sem], where: str) -> None:
        alias, _, col = str(c["column_id"]).partition("::")
        if alias.casefold() not in sems:
            self.b.warn(where, f"column {cname} refers to unknown table {alias}")
            return
        sem = sems[alias.casefold()]
        info = aliases[alias.casefold()][1]
        phys = info.phys(col) if info else _ident(col)
        props = c.get("properties") or {}
        name = snake(cname)
        if str(props.get("column_type", "ATTRIBUTE")).upper() == "MEASURE":
            agg_raw = str(props.get("aggregation", "SUM")).upper()
            agg = AGG.get(agg_raw)
            if any(x.name == name for x in sem.model.measures):
                return
            meas = Measure(name=name, agg=agg or "sum", expr="" if phys == name else phys, label=cname if name != cname else "", description=desc)
            if not agg:
                meas.todo = f"ThoughtSpot aggregation {agg_raw} has no MetricFlow agg"
                meas.source_expression, meas.source_language = f"{agg_raw}({c['column_id']})", LANG
            sem.model.measures.append(meas)
            sem.by_agg.setdefault((meas.agg, phys), name)
            if agg:
                sem.simple.setdefault((agg, phys), name)
            self.b.metrics.append(Metric(name=cname, label=cname, description=desc, measure=name, semantic_model=sem.model.name,
                                         source_ref=f"{sem.model.source_ref}#{cname}", todo=meas.todo,
                                         source_expression=meas.source_expression, source_language=meas.source_language))
            return
        if any(d.name == name for d in sem.model.dimensions):
            return
        dim = Dimension(name=name, expr="" if phys == name else phys, label=cname if name != cname else "", description=desc)
        if info and info.data_type(col) in TIME_TYPES:
            dim.type, dim.time_granularity = "time", "day"
        sem.model.dimensions.append(dim)

    def measure_for(self, sem: _Sem, agg: fx.Agg, wanted: str) -> str:
        key = (agg.agg, agg.expr)
        if key in sem.by_agg:
            return sem.by_agg[key]
        name = wanted if not any(m.name == wanted for m in sem.model.measures) else f"{wanted}_{agg.agg}"
        sem.model.measures.append(Measure(name=name, agg=agg.agg, expr="" if agg.expr == name else agg.expr))
        sem.by_agg[key] = name
        return name

    def formula_column(self, c, cname, desc, expr, measure_hint, sems, used, ref_sql, metric_ref, dim_ref, where) -> None:
        name = snake(cname)
        first = next(iter(sems.values()), None)
        if first is None:
            self.b.warn(where, f"formula {cname} skipped: model has no tables")
            return
        metric = Metric(name=cname, label=cname, description=desc, semantic_model=first.model.name,
                        source_ref=f"{first.model.source_ref}#{cname}")

        def tables_of(node) -> list[_Sem]:
            used.clear()
            try:
                fx.to_sql(node, lambda n: ref_sql(n) if not metric_ref(n) else "0")
            except fx.FormulaError:
                pass
            return [sems[a] for a in dict.fromkeys(used) if a in sems]

        def side(node, fallback: str) -> str:
            c2 = fx.classify(node, LANG, ref_sql, metric_ref)
            if c2.kind == "derived" and len(c2.metrics) == 1 and c2.expr == c2.metrics[0]:
                return c2.metrics[0]
            if c2.kind != "agg" or not c2.agg:
                raise fx.FormulaError("ratio side is not a single aggregate or metric")
            sts = tables_of(node)
            if len(sts) > 1:
                raise fx.FormulaError("ratio side spans several tables")
            sem = sts[0] if sts else first
            a = c2.agg
            if a.condition is None and (a.agg, a.expr) in sem.simple:
                return sem.simple[(a.agg, a.expr)]
            mname = fallback if a.condition is not None else f"{a.agg}_{snake(a.expr)}"
            meas = self.measure_for(sem, a, mname)
            flt = fx.strip_outer(fx.filter_jinja(a.condition, dim_ref)) if a.condition is not None else ""
            self.b.metrics.append(Metric(name=mname, measure=meas, filter=flt, semantic_model=sem.model.name,
                                         description="Helper metric for a ThoughtSpot ratio.", source_ref=metric.source_ref))
            if not flt:
                sem.simple[(a.agg, a.expr)] = mname
            return mname

        try:
            node = _parse(expr)
        except fx.FormulaError as exc:
            node = None
            err = str(exc)
        has_agg = bool(node is not None and (any(x in fx.AGGS[LANG] for x in fx.calls(node)) or any(metric_ref(r) for r in fx.refs(node))))
        is_agg = has_agg or bool(measure_hint)
        if node is not None:
            hard = sorted(set(fx.calls(node)) & HARD_FUNCS)
            try:
                if hard:
                    raise fx.FormulaError(f"{', '.join(hard)}() is a ThoughtSpot LOD/window function")
                if has_agg:
                    cl = fx.classify(node, LANG, ref_sql, metric_ref)
                    if cl.kind == "agg" and cl.agg:
                        sts = tables_of(node)
                        if len(sts) > 1:
                            raise fx.FormulaError(f"aggregate spans tables {', '.join(s.model.name for s in sts)}; MetricFlow measures live on one table")
                        sem = sts[0] if sts else first
                        metric.semantic_model = sem.model.name
                        if cl.agg.condition is None:
                            metric.measure = self.measure_for(sem, cl.agg, name)
                            sem.simple.setdefault((cl.agg.agg, cl.agg.expr), name)
                        else:
                            metric.measure = self.measure_for(sem, cl.agg, f"{cl.agg.agg}_{snake(cl.agg.expr)}")
                            metric.filter = fx.strip_outer(fx.filter_jinja(cl.agg.condition, dim_ref))
                    elif cl.kind == "ratio":
                        parts = fx._ratio(node)
                        assert parts
                        metric.type = "ratio"
                        metric.numerator = side(parts[0], f"{name}_numerator")
                        metric.denominator = side(parts[1], f"{name}_denominator")
                    else:
                        metric.type, metric.expr, metric.metrics = "derived", fx.strip_outer(cl.expr), cl.metrics
                    self.b.metrics.append(metric)
                    return
                if measure_hint:  # MEASURE column over a row-level formula: aggregate it with the column's aggregation
                    agg_raw = str((c.get("properties") or {}).get("aggregation", "SUM")).upper()
                    if agg_raw not in AGG:
                        raise fx.FormulaError(f"aggregation {agg_raw} has no MetricFlow agg")
                    row = fx.strip_outer(fx.to_sql(node, ref_sql))
                    sts = tables_of(node)
                    if len(sts) > 1:
                        raise fx.FormulaError(f"formula spans tables {', '.join(s.model.name for s in sts)}")
                    sem = sts[0] if sts else first
                    metric.semantic_model = sem.model.name
                    metric.measure = self.measure_for(sem, fx.Agg(AGG[agg_raw], row), name)
                    self.b.metrics.append(metric)
                    return
                grain = fx.time_grain(node)
                sts = tables_of(node)
                if len(sts) > 1:
                    raise fx.FormulaError(f"formula spans tables {', '.join(s.model.name for s in sts)}")
                sem = sts[0] if sts else first
                dim = Dimension(name=name, label=cname if name != cname else "", description=desc)
                if grain:
                    dim.type, dim.time_granularity = "time", grain[0]
                    dim.expr = f"date_trunc('{grain[0]}', {fx.to_sql(grain[1], ref_sql)})"
                else:
                    dim.expr = fx.strip_outer(fx.to_sql(node, ref_sql))
                if not any(d.name == name for d in sem.model.dimensions):
                    sem.model.dimensions.append(dim)
                return
            except fx.FormulaError as exc:
                err = str(exc)
        why = err
        if is_agg or measure_hint:
            metric.todo = f"ThoughtSpot formula not translated ({why})"
            if "group_aggregate" in why or "group_" in why:
                metric.todo += "; group_aggregate is a level-of-detail calc: model it as a separate semantic model at that grain, or a derived metric"
            if "cumulative" in why or "moving" in why:
                metric.todo += "; use a MetricFlow cumulative metric (window / grain_to_date)"
            metric.source_expression, metric.source_language = expr, LANG
            self.b.metrics.append(metric)
        else:
            dim = Dimension(name=name, label=cname if name != cname else "", description=desc,
                            todo=f"ThoughtSpot formula not translated ({why})", source_expression=expr, source_language=LANG)
            if not any(d.name == name for d in first.model.dimensions):
                first.model.dimensions.append(dim)

    # -------------------------------------------------------------- answers & liveboards

    def _phys_of_answer_col(self, name: str, models: list[str]) -> set[str]:
        base = name
        m = BUCKET.match(base)
        if m:
            base = m.group(2)
        cand = [base, AGG_PREFIX.sub("", base)]
        for mname in models or list(self.model_cols):
            cols = self.model_cols.get(mname, {})
            aliases = cols.get("__aliases__", {})
            for cn in cand:
                c = cols.get(cn.casefold())
                if not isinstance(c, dict):
                    continue
                if c.get("column_id"):
                    alias, _, col = str(c["column_id"]).partition("::")
                    info = aliases.get(alias.casefold(), ("", None))[1]
                    return {(info.phys(col) if info else col).lower()}
        return set()

    def _models_of(self, a: dict) -> list[str]:
        out = []
        for t in a.get("tables", []) or []:
            if not isinstance(t, dict):
                continue
            g = str(t.get("fqn", ""))
            if g in self.model_by_guid:
                out.append(self.model_by_guid[g])
            elif str(t.get("name", "")).casefold() in self.model_cols:
                out.append(str(t["name"]).casefold())
        return out

    def rename_aliases(self, sql: str, columns: list[str], models: list[str], snake_case: bool = False) -> tuple[str, str]:
        """ThoughtSpot SQL names its outputs ca_1..ca_n. Wrap it so outputs carry the answer's column names.

        snake_case=True (notebook tiles) aliases them as snake_case identifiers instead of the display names,
        because Altair shorthand cannot take "Month(Order Date)" or "Unique Number of Order Id".
        """
        if not sql or not columns:
            return sql, ""
        if all(f'"{c}"' in sql for c in columns) and not snake_case:
            return sql, ""
        aliases = list(dict.fromkeys(re.findall(r'"(ca_\d+)"', sql)))
        if len(aliases) != len(columns):
            return sql, f"ThoughtSpot SQL output aliases ({', '.join(aliases) or 'none'}) could not be matched to {', '.join(columns)}; alias them by hand"
        confirmed = True
        prev = 0
        for alias, col in zip(aliases, columns):
            pos = sql.find(f'"{alias}"')
            segment = sql[prev:pos].lower()
            prev = pos + len(alias) + 2
            phys = self._phys_of_answer_col(col, models)
            if not phys or not any(p in segment for p in phys):
                confirmed = False
        select = ",\n  ".join(f'"{a}" as {_alias(c) if snake_case else json.dumps(c)}' for a, c in zip(aliases, columns))
        wrapped = f"select\n  {select}\nfrom (\n{sql.strip().rstrip(';')}\n) ts"
        note = "" if confirmed else f"ca_N aliases mapped to answer columns in order ({', '.join(f'{a}={c}' for a, c in zip(aliases, columns))}); confirm against ThoughtSpot"
        return wrapped, note

    def _sql_for(self, store: RawStore, kind: str, guid: str) -> list[dict]:
        rel = f"sql/{kind}/{_safe(guid)}.json"
        if not store.exists(rel):
            return []
        out: list[dict] = []

        def walk(node: Any) -> None:
            # {metadata_id, metadata_name, sql_queries: [{metadata_id, metadata_name, sql_query}]}; tolerate
            # a list of those and deeper nesting (liveboard -> visualization -> queries)
            if isinstance(node, list):
                for n in node:
                    walk(n)
            elif isinstance(node, dict):
                if isinstance(node.get("sql_query"), str):
                    out.append(node)
                walk(node.get("sql_queries"))

        walk(store.get_json(rel))
        return out

    def answer_tile(self, tid: str, a: dict, sql: str, filters: list[DashboardFilter] | None = None) -> Tile:
        title = str(a.get("name", tid))
        columns = _names(a.get("answer_columns"))
        chart = a.get("chart") or {}
        ctype = str(chart.get("type", "")).upper()
        mode = str(a.get("display_mode", "CHART_MODE")).upper()
        models = self._models_of(a)
        todos: list[str] = []
        for f in a.get("formulas", []) or []:
            if isinstance(f, dict):
                try:
                    hard = sorted(set(fx.calls(_parse(str(f.get("expr", ""))))) & HARD_FUNCS)
                except fx.FormulaError:
                    hard = ["(unparsed)"]
                if hard:
                    todos.append(f"answer formula {f.get('name')!r} uses {', '.join(hard)}; ThoughtSpot's SQL computes it, add a metric if it should be reusable")
        sql2, note = self.rename_aliases(sql, columns, models, snake_case=True)
        if note:
            todos.append(note)
        if sql and filters:
            todos.append("liveboard filter(s) " + ", ".join(f"{f.field} (:{f.name})" for f in filters)
                         + " are baked into ThoughtSpot's SQL as literals; replace them with the :params")
        tile = Tile(id=tid, title=title, sql=sql2.strip(), source_type=ctype or mode,
                    source_config={"search_query": a.get("search_query", ""), "chart_type": ctype, "display_mode": mode,
                                   "answer_columns": columns, "source_sql": bool(sql)})
        if mode == "TABLE_MODE" or ctype in ("TABLE", "PIVOT_TABLE", "GRID_TABLE"):
            tile.kind, tile.mark = "table", "table"
            if ctype == "PIVOT_TABLE":
                todos.append("pivot table: pivot the rows in pandas if needed")
            tile.todo = "; ".join(todos)
            return tile
        if ctype == "KPI":
            tile.kind, tile.mark = "kpi", "text"
        else:
            tile.mark = MARKS.get(ctype, "unknown")  # type: ignore[assignment]
            if tile.mark == "unknown":
                todos.append(f"ThoughtSpot chart type {ctype or '?'} has no Altair mark mapping here; pick the closest chart")
        axes: dict[str, list[str]] = {}
        for cfg in chart.get("axis_configs", []) or []:
            if isinstance(cfg, dict):
                for ch, v in cfg.items():
                    axes.setdefault(str(ch), []).extend(_names(v))
        if not axes and columns:
            meas = [c for c in columns if self._is_measure_name(c, models)]
            dims = [c for c in columns if c not in meas]
            axes = {"y": meas[:1] or columns[-1:], **({"x": dims[:1]} if dims and ctype != "KPI" else {})}
            if len(dims) > 1 and ctype != "KPI":
                axes["color"] = dims[1:2]
            todos.append("axis_configs missing; chart encodings guessed from answer columns")
        if ctype in ("BAR", "STACKED_BAR"):
            axes["x"], axes["y"] = axes.get("y", []), axes.get("x", [])
        for ch, names in axes.items():
            target = {"x": "x", "y": "y", "color": "color", "size": "size", "tooltip": "tooltip"}.get(ch.lower())
            if not target or not names:
                continue
            if tile.kind == "kpi":
                target = "text" if target == "y" else ""
            elif tile.mark == "arc":
                target = {"x": "color", "y": "theta"}.get(target, target)
            if not target:
                continue
            if len(names) > 1 and target != "tooltip":
                todos.append(f"{len(names)} columns on {target}; the Altair tile keeps the first")
            n = names[0]
            if BUCKET.match(n) and target in ("x", "color", "y"):
                typ = "temporal"
            elif target in ("theta", "text", "size") or self._is_measure_name(n, models) or (target == "y" and ctype not in ("BAR", "STACKED_BAR")):
                typ = "quantitative"
            elif target == "x" and ctype in ("BAR", "STACKED_BAR"):
                typ = "quantitative"
            else:
                typ = "nominal"
            aliased = bool(sql) and sql2 != sql
            tile.encodings[target] = Encoding(field=_alias(n) if aliased else n, type=typ, title=n if aliased else "")  # type: ignore[arg-type]
        tile.todo = "; ".join(todos)
        return tile

    def _is_measure_name(self, name: str, models: list[str]) -> bool:
        if BUCKET.match(name):
            return False
        if AGG_PREFIX.match(name):
            return True
        for mname in models or list(self.model_cols):
            c = self.model_cols.get(mname, {}).get(name.casefold())
            if isinstance(c, dict) and str((c.get("properties") or {}).get("column_type", "")).upper() == "MEASURE":
                return True
        return False

    def liveboard(self, guid: str, lb: dict, store: RawStore, views: int | None) -> None:
        name = str(lb.get("name", guid))
        queries = self._sql_for(store, "liveboard", guid)
        by_id = {str(q.get("metadata_id")): q.get("sql_query", "") for q in queries}
        by_name = {str(q.get("metadata_name", "")).casefold(): q.get("sql_query", "") for q in queries}
        d = Dashboard(name=name, slug=slugify(name), description=str(lb.get("description", "") or ""), datasource=self.b.datasource,
                      source_ref=f"thoughtspot:liveboard/{guid}", url=_url(self.site, "liveboard", guid), views=views)
        for f in lb.get("filters", []) or []:
            if not isinstance(f, dict):
                continue
            col = (_names(f.get("column")) or [""])[0]
            date = f.get("date_filter") if isinstance(f.get("date_filter"), dict) else None
            kind = "date_range" if date or str(f.get("oper", "")).lower() == "between" else "value"
            vals = f.get("values")
            default: Any = vals if not isinstance(vals, list) or len(vals) != 1 else vals[0]
            if date:
                period = str(date.get("datePeriod") or date.get("date_period") or "").lower()
                default = " ".join(str(x) for x in (str(date.get("type", "")).lower().replace("_n_period", "").replace("_", " "),
                                                    date.get("number", ""), period) if x) or None
            d.filters.append(DashboardFilter(name=snake(f.get("display_name") or col or "filter"), field=col, default=default, kind=kind))  # type: ignore[arg-type]
        for v in lb.get("visualizations", []) or []:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("id") or v.get("viz_guid") or f"viz_{len(d.tiles) + 1}")
            if v.get("note_tile"):
                note = v["note_tile"]
                text = note.get("html_parsed_string") if isinstance(note, dict) else str(note)
                d.tiles.append(Tile(vid, "", kind="text", text=_html_text(str(text or "")), source_type="note"))
                continue
            a = v.get("answer") or {}
            sql = by_id.get(str(v.get("viz_guid", ""))) or by_name.get(str(a.get("name", "")).casefold(), "")
            if not sql:
                self.b.warn(f"liveboard {name} / {a.get('name', vid)}", "no generated SQL extracted for this visualization")
            d.tiles.append(self.answer_tile(vid, a, sql or "", d.filters))
        layout = lb.get("layout") or {}
        tabs = layout.get("tabs") if isinstance(layout.get("tabs"), list) else None
        groups = [(str(t.get("name", "")), t.get("tiles", []) or []) for t in tabs] if tabs else [("", layout.get("tiles", []) or [])]
        placed: set[str] = set()
        known = {t.id for t in d.tiles}
        for tab_name, tiles in groups:
            if tabs and tab_name:
                tid = f"tab-{slugify(tab_name)}"
                d.tiles.append(Tile(tid, tab_name, kind="text", text=f"## {tab_name}", source_type="tab"))
                d.layout.append([tid])
            rows: dict[Any, list[tuple[Any, str]]] = {}
            for t in tiles:
                if isinstance(t, dict) and str(t.get("visualization_id")) in known:
                    rows.setdefault(t.get("y", 0), []).append((t.get("x", 0), str(t["visualization_id"])))
            for y in sorted(rows, key=lambda v: (v is None, v)):
                row = [i for _, i in sorted(rows[y], key=lambda p: p[0] or 0)]
                placed.update(row)
                d.layout.append(row)
        d.layout += [[t.id] for t in d.tiles if t.id not in placed and t.id in known]
        self.liveboard_slugs[guid] = d.slug
        self.b.dashboards.append(d)

    def answer(self, guid: str, a: dict, store: RawStore) -> None:
        name = str(a.get("name", guid))
        queries = self._sql_for(store, "answer", guid)
        sql = next((q.get("sql_query", "") for q in queries if q.get("sql_query")), "")
        if not sql:
            self.b.warn(f"answer {name}", "no generated SQL extracted; verified query skipped")
            return
        sql2, note = self.rename_aliases(sql, _names(a.get("answer_columns")), self._models_of(a))
        search = str(a.get("search_query", "") or "")
        desc = " ".join(x for x in (str(a.get("description", "") or ""), f"ThoughtSpot search: {search}" if search else "") if x)
        self.b.queries.append(VerifiedQuery(name=name, sql=sql2.strip(), description=desc, datasource=self.b.datasource,
                                            source_ref=f"thoughtspot:answer/{guid}", todo=note))

    def permissions(self, store: RawStore) -> None:
        """Sharing from /security/metadata/fetch-permissions -> Cedar permits."""
        skipped_answers = 0
        for rel in store.glob("permissions/*.json"):
            for guid, detail in _permission_details(store.get_json(rel)):
                mtype = str(detail.get("metadata_type", "")).upper()
                if guid in self.liveboard_slugs:
                    resources = [("Notebook", self.liveboard_slugs[guid])]
                elif guid in self.model_tables:
                    resources = [("Table", t) for t in self.model_tables[guid]]
                else:
                    skipped_answers += mtype == "ANSWER"
                    continue
                name = str(detail.get("metadata_name") or guid)
                for info in detail.get("principal_permission_info") or []:
                    for pp in (info.get("principal_permissions") or []) if isinstance(info, dict) else []:
                        if not isinstance(pp, dict):
                            continue
                        perm = str(pp.get("permission", "")).upper()
                        if perm in ("NO_ACCESS", ""):
                            continue
                        ptype = str(pp.get("principal_type") or info.get("principal_type") or "").upper()
                        pname = str(pp.get("principal_name") or pp.get("principal_id") or "")
                        todo = ""
                        if ptype in ("USER_GROUP", "GROUP"):
                            principal = "*" if pname.lower() in ("all", "all_group") else f"group:{pname}"
                            if principal != "*":
                                todo = "ThoughtSpot group; confirm it matches an IdP group of the same name"
                        else:
                            principal = f"user:{pname}"
                            if "@" not in pname:
                                todo = f"ThoughtSpot username {pname!r}; row filters identify users by email, replace with the user's email"
                        for rtype, rid in resources:
                            actions = ["view", "edit"] if perm in ("MODIFY", "FULL") and rtype == "Notebook" else \
                                ["view"] if rtype == "Notebook" else ["query"]
                            for action in actions:
                                self.b.permissions.append(Permission(
                                    principal=principal, action=action, resource_type=rtype, resource_id=rid,  # type: ignore[arg-type]
                                    source_expression=f"shared {perm} on {mtype.lower() or 'object'} {name}",
                                    source_ref=f"thoughtspot:{mtype.lower() or 'object'}/{guid}/sharing", todo=todo,
                                ))
        if skipped_answers:
            self.b.warn("sharing", f"{skipped_answers} saved answer(s) have sharing rules; verified queries carry no ACL", "info")

    def feedback(self, guid: str, fb: dict) -> None:
        items = [f for f in fb.get("feedback", []) or [] if isinstance(f, dict)]
        terms = [f for f in items if str(f.get("type", "")).upper() == "BUSINESS_TERM"]
        refs = [f for f in items if str(f.get("type", "")).upper() != "BUSINESS_TERM"]
        lines: list[str] = []
        if terms:
            lines += ["## Business terms", ""]
            lines += [f"- **{f.get('feedback_phrase', '')}** means `{f.get('search_tokens', '')}`" for f in terms]
            lines.append("")
        if refs:
            lines += ["## Reference questions", "", "Questions users confirmed, with the ThoughtSpot search that answers them:", ""]
            for f in refs:
                rating = f" ({str(f.get('rating')).lower()})" if f.get("rating") else ""
                lines.append(f"- {f.get('feedback_phrase') or f.get('parent_question', '')}{rating}: `{f.get('search_tokens', '')}`")
            lines.append("")
        if lines:
            self.b.knowledge.append(KnowledgeItem(
                name="ThoughtSpot Spotter feedback" + (f" {fb.get('name')}" if fb.get("name") else ""),
                body="\n".join(lines),
                description="Business terms and reference questions users taught ThoughtSpot Spotter.",
                context="always" if terms else "reference",
                datasource=self.b.datasource,
                source_ref=f"thoughtspot:nls_feedback/{guid}",
            ))
