"""Sigma Computing source.

Auth: OAuth2 client credentials (`POST /v2/auth/token`, form-encoded grant_type,
client_id, client_secret) against the API host of your Sigma cloud (SIGMA_API_URL:
https://api.sigmacomputing.com (GCP), https://aws-api.sigmacomputing.com,
https://api.us.azure.sigmacomputing.com, https://api.eu.aws.sigmacomputing.com, ...).
Tokens last an hour; the source refreshes them before they expire and once on a 401.

Raw layout under migration/raw/sigma/:

    data_models/_index.json                    GET /v2/dataModels (all pages, list entries)
    data_models/<dataModelId>/model.json       the list entry for that data model
    data_models/<dataModelId>/spec.json        GET /v2/dataModels/{id}/spec?format=json (spec.yaml if YAML came back)
    workbooks/_index.json                      GET /v2/workbooks (all pages, list entries)
    workbooks/<workbookId>/workbook.json       GET /v2/workbooks/{id}?includeContents=true, minus `contents`
    workbooks/<workbookId>/contents.json|yaml  the `contents` code representation (flat `elements`, `pages`,
                                               XML `layout`). When includeContents is not available it is
                                               rebuilt from /pages, /pages/{p}/elements, /elements/{e}/columns
                                               and marked "_unshackle_source": "legacy-endpoints"
    workbooks/<workbookId>/queries.json        GET /v2/workbooks/{id}/queries (all pages: elementId, sql | error)
    workbooks/<workbookId>/sql/<elementId>.sql Sigma's generated SQL per element (queries, else /elements/{e}/query)
    workbooks/<workbookId>/grants.json         GET /v2/workbooks/{id}/grants (who may view / explore / edit)
    teams.json, members.json                   GET /v2/teams, GET /v2/members (id -> name / email for grants)
    _errors.json                               items extract could not fetch (become warnings in parse)

parse() reads only these files. It translates deterministically:
  * warehouse-table and custom-SQL elements of data models -> Table (catalog) + SemanticModel
  * columns -> dimensions (direct columns, DateTrunc(), arithmetic, If() -> CASE)
  * metrics -> measures + simple / ratio / derived metrics, SumIf/CountIf -> metric filters
    (Sigma metrics cannot use window or join functions, so most map cleanly)
  * relationships and join elements -> entities; column security -> Cedar forbid;
    CurrentUserAttributeText()/CurrentUserEmail() RLS columns -> row filters; workbook grants -> Cedar permits
  * workbook elements -> notebook tiles carrying Sigma's own SQL, controls -> dashboard filters, layout -> rows
Anything else (Lookup, Rollup, window functions, cross-element refs, CurrentUserInTeam, passthrough
functions) keeps its Sigma formula with a TODO(unshackle).
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

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
from unshackle.sources.base import Inventory, Source

LANG = "sigma"
TIME_TYPES = {"datetime", "date", "timestamp", "time"}
WINDOW_FUNCS = {
    "cumulativesum", "cumulativecount", "cumulativeavg", "cumulativemax", "cumulativemin",
    "movingavg", "movingsum", "movingcount", "movingmax", "movingmin", "movingcorrelation",
    "rank", "rankdense", "rankpercentile", "rownumber", "lag", "lead", "first", "last", "nth",
    "firstnonnull", "lastnonnull", "percentoftotal", "grandtotal", "subtotal", "lookup", "rollup",
    "fill", "interpolate",
}
PASSTHROUGH_FUNCS = {"callnumber", "calltext", "calldatetime", "calllogical", "callvariant", "aggnumber", "aggtext", "aggdatetime"}
USER_FUNCS = {"currentuserattributetext", "currentuserattributenumber", "currentuserattributelogical",
              "currentuseremail", "currentuserfullname", "currentuserinteam", "currentuserid"}
MARKS = {
    "bar": "bar", "column": "bar", "stacked-bar": "bar", "grouped-bar": "bar",
    "line": "line", "area": "area", "scatter": "point", "point": "point",
    "pie": "arc", "donut": "arc",
}
CHANNELS = {
    "x": "x", "xaxis": "x", "x-axis": "x", "y": "y", "yaxis": "y", "y-axis": "y",
    "color": "color", "colorby": "color", "size": "size", "tooltip": "tooltip", "tooltips": "tooltip",
    "value": "value", "values": "value",
}
CONTROL_KINDS = {
    "date-range": "date_range", "daterange": "date_range", "date": "date",
    "number-range": "number", "slider": "number", "range-slider": "number", "number": "number",
    "list": "value", "text": "value", "segmented": "value", "checkbox": "value", "switch": "value",
    "boolean": "value", "top-n": "number", "hierarchy": "value",
}
SKIP_KINDS = {"container", "image", "button", "divider", "spacer", "embed", "tabbed-container", "tab"}
TABLE_PREFIX = re.compile(r"^(fct|fact|dim|d|f|stg|int)_", re.I)
INODE = re.compile(r"^inode-[^/]+/(.+)$")
PURE_REF = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _ident(value: str) -> str:
    """Warehouse identifier: Snowflake-style upper case becomes lower case (unquoted is case-insensitive)."""
    return value.lower() if value.isupper() else value


def _phys(display: str) -> str:
    """Sigma shows ORDER_ID as "Order Id"; turn a display name back into a column name."""
    return snake(display)


def _singular(name: str) -> str:
    s = TABLE_PREFIX.sub("", snake(name))
    if s.endswith("ies"):
        return s[:-3] + "y"
    if s.endswith("s") and not s.endswith("ss"):
        return s[:-1]
    return s


def _text(value: Any) -> str:
    """Sigma names are strings, or {text: ..., fontSize: ...} in the contents representation."""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    return "" if value is None else str(value)


def _load(store: RawStore, rel: str) -> Any:
    text = store.get_text(rel)
    return json.loads(text) if rel.endswith(".json") else yaml.safe_load(text)


def _entries(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for key in ("entries", "data", "items", "results"):
            if isinstance(data.get(key), list):
                return [e for e in data[key] if isinstance(e, dict)]
    return []


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value) or "_"


class SigmaSource(Source):
    name = "sigma"
    label = "Sigma Computing"
    env_vars = ("SIGMA_API_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET")
    docs = "https://help.sigmacomputing.com/reference/get-started-sigma-api"
    manual_files = "nothing yet: Sigma has no UI download of workbook or data model definitions that unshackle reads"

    def __init__(self, client: Client | None = None):
        self._client_obj = client
        self._expires = 0.0

    # ------------------------------------------------------------------ network

    def _client(self) -> Client:
        if self._client_obj is None:
            # Sigma publishes no per-minute limit for reads; exports are ~400/min. Stay well below.
            self._client_obj = Client(env("SIGMA_API_URL"), per_minute=300)
        if time.monotonic() >= self._expires:
            self._auth()
        return self._client_obj

    def _auth(self) -> None:
        c = self._client_obj
        assert c is not None
        resp = c.post(
            "/v2/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": env("SIGMA_CLIENT_ID"),
                "client_secret": env("SIGMA_CLIENT_SECRET"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise SourceError("Sigma auth returned no access_token")
        c.set_header("Authorization", f"Bearer {token}")
        self._expires = time.monotonic() + max(float(body.get("expires_in", 3600)) - 300, 60)

    def _request(self, path: str, **params: Any) -> Any:
        """GET with one re-auth on 401 (token revoked or expired early). Returns the httpx response."""
        try:
            return self._client().get(path, params=params or None)
        except SourceError as exc:
            if " -> 401" not in str(exc):
                raise
            self._expires = 0.0
            return self._client().get(path, params=params or None)

    def _get(self, path: str, **params: Any) -> Any:
        return self._request(path, **params).json()

    def _list(self, path: str, **params: Any) -> list[dict]:
        out: list[dict] = []
        page: Any = None
        for _ in range(10_000):
            q = {"limit": 500, **params}
            if page:
                q["page"] = page
            data = self._get(path, **q)
            out += _entries(data)
            page = data.get("nextPage") if isinstance(data, dict) else None
            if not page:
                break
        return out

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        inv.notes.append(
            "Sigma's REST API has no view counts. Usage lives in Sigma's audit logs "
            "(Administration > Audit logs, or the audit log connection written to your warehouse); "
            "query that table to rank workbooks before choosing --only."
        )
        for dm in self._list("/v2/dataModels"):
            inv.add("data_model", dm.get("dataModelId") or dm.get("id"), dm.get("name", ""),
                    url=dm.get("url", ""), parent=dm.get("path", ""), extra={"updatedAt": dm.get("updatedAt", "")})
        for wb in self._list("/v2/workbooks"):
            inv.add("workbook", wb.get("workbookId") or wb.get("id"), wb.get("name", ""),
                    url=wb.get("url", ""), parent=wb.get("path", ""), extra={"updatedAt": wb.get("updatedAt", "")})
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []

        def picked(entry: dict, *keys: str) -> bool:
            return not only or any(str(entry.get(k) or "") in only for k in keys)

        try:
            dms = self._list("/v2/dataModels")
        except SourceError as exc:
            dms = []
            errors.append({"item": "dataModels", "message": str(exc)})
        store.put_json("data_models/_index.json", dms)
        for dm in dms:
            dm_id = str(dm.get("dataModelId") or dm.get("id") or "")
            if not dm_id or not picked(dm, "dataModelId", "id", "name", "dataModelUrlId"):
                continue
            try:
                resp = self._request(f"/v2/dataModels/{dm_id}/spec", format="json")
                try:
                    store.put_json(f"data_models/{_safe(dm_id)}/spec.json", resp.json())
                except ValueError:
                    store.put_text(f"data_models/{_safe(dm_id)}/spec.yaml", resp.text)
                store.put_json(f"data_models/{_safe(dm_id)}/model.json", dm)
            except SourceError as exc:
                errors.append({"item": f"dataModel {dm_id}", "message": str(exc)})

        try:
            wbs = self._list("/v2/workbooks")
        except SourceError as exc:
            wbs = []
            errors.append({"item": "workbooks", "message": str(exc)})
        store.put_json("workbooks/_index.json", wbs)
        granted = False
        for wb in wbs:
            wb_id = str(wb.get("workbookId") or wb.get("id") or "")
            if not wb_id or not picked(wb, "workbookId", "id", "name", "workbookUrlId"):
                continue
            try:
                granted |= self._extract_workbook(store, wb_id, wb, errors)
            except SourceError as exc:
                errors.append({"item": f"workbook {wb_id}", "message": str(exc)})
        if granted:
            for rel, path, keys in (("teams.json", "/v2/teams", ("teamId", "name")), ("members.json", "/v2/members", ("memberId", "email"))):
                try:
                    store.put_json(rel, [{k: e.get(k) for k in keys} for e in self._list(path)])
                except SourceError as exc:
                    errors.append({"item": path, "message": f"{exc}; grants will show ids instead of names"})
        store.put_json("_errors.json", errors)

    def _extract_workbook(self, store: RawStore, wb_id: str, entry: dict, errors: list[dict[str, str]]) -> bool:
        base = f"workbooks/{_safe(wb_id)}"
        try:
            detail = self._get(f"/v2/workbooks/{wb_id}", includeContents="true")
        except SourceError as exc:
            errors.append({"item": f"workbook {wb_id} contents", "message": f"{exc}; using the legacy page/element endpoints"})
            detail = dict(entry)
        contents = detail.pop("contents", None) if isinstance(detail, dict) else None
        store.put_json(f"{base}/workbook.json", {**entry, **(detail or {})})
        if isinstance(contents, str):
            store.put_text(f"{base}/contents.yaml", contents)
            contents = yaml.safe_load(contents)
        elif not contents:
            contents = self._legacy_contents(wb_id)
            store.put_json(f"{base}/contents.json", contents)
        else:
            store.put_json(f"{base}/contents.json", contents)

        have_sql: set[str] = set()
        try:
            queries = self._list(f"/v2/workbooks/{wb_id}/queries")
            store.put_json(f"{base}/queries.json", {"entries": queries})
            for q in queries:
                el = str(q.get("elementId") or "")
                if el and q.get("sql"):
                    store.put_text(f"{base}/sql/{_safe(el)}.sql", q["sql"].rstrip() + "\n")
                    have_sql.add(el)
                elif el and q.get("error"):
                    errors.append({"item": f"workbook {wb_id} element {el}", "message": f"no SQL: {q['error']}"})
                    have_sql.add(el)  # Sigma already said why; the per-element endpoint would say the same
        except SourceError as exc:
            errors.append({"item": f"workbook {wb_id} queries", "message": f"{exc}; falling back to per-element query"})
        for _page, el in _elements(contents):
            el_id = str(el.get("id") or el.get("elementId") or "")
            if not el_id or el_id in have_sql or _etype(el) in SKIP_KINDS | {"text", "control"}:
                continue
            try:
                data = self._get(f"/v2/workbooks/{wb_id}/elements/{el_id}/query")
                sql = data if isinstance(data, str) else (data or {}).get("sql", "")
                if sql:
                    store.put_text(f"{base}/sql/{_safe(el_id)}.sql", sql.rstrip() + "\n")
                elif isinstance(data, dict) and data.get("error"):
                    errors.append({"item": f"workbook {wb_id} element {el_id}", "message": f"no SQL: {data['error']}"})
            except SourceError as exc:
                errors.append({"item": f"workbook {wb_id} element {el_id}", "message": f"no SQL: {exc}"})
        try:
            grants = self._list(f"/v2/workbooks/{wb_id}/grants")
            store.put_json(f"{base}/grants.json", grants)
            return bool(grants)
        except SourceError as exc:
            errors.append({"item": f"workbook {wb_id} grants", "message": str(exc)})
            return False

    def _legacy_contents(self, wb_id: str) -> dict[str, Any]:
        pages = []
        for p in self._list(f"/v2/workbooks/{wb_id}/pages"):
            page_id = p.get("pageId") or p.get("id")
            elements = []
            for e in self._list(f"/v2/workbooks/{wb_id}/pages/{page_id}/elements"):
                el_id = e.get("elementId") or e.get("id")
                try:
                    cols = self._list(f"/v2/workbooks/{wb_id}/elements/{el_id}/columns")
                except SourceError:
                    cols = [{"label": c} if isinstance(c, str) else c for c in e.get("columns") or []]
                elements.append(
                    {
                        "id": el_id,
                        "name": e.get("name", ""),
                        "type": e.get("type", ""),
                        "vizType": e.get("vizualizationType") or e.get("visualizationType") or e.get("vizType", ""),
                        "columns": [
                            {"id": c.get("columnId") or c.get("id") or c.get("label"), "name": c.get("label") or c.get("name", ""),
                             "formula": c.get("formula", "")}
                            for c in cols if isinstance(c, dict)
                        ],
                    }
                )
            pages.append({"id": page_id, "name": p.get("name", ""), "elements": elements})
        return {"_unshackle_source": "legacy-endpoints", "pages": pages}

    # ------------------------------------------------------------------ parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        raise SourceError(
            "Sigma has no UI download of workbook or data model definitions that unshackle can read (its exports are data: "
            "CSV, Excel, PDF). An admin can create REST API client credentials (Administration > Developer access); "
            "then run `unshackle extract sigma`"
        )

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool=self.name)
        if store.exists("_errors.json"):
            for e in store.get_json("_errors.json") or []:
                b.warn(e.get("item", "?"), e.get("message", ""))
        fields: dict[str, str] = {}  # data model column/metric display name (casefold) -> time | metric | dim
        for rel in store.glob("data_models/*/spec.*"):
            dm_id = rel.split("/")[1]
            try:
                spec = _load(store, rel)
            except (ValueError, yaml.YAMLError) as exc:
                b.warn(rel, f"cannot read spec: {exc}")
                continue
            if not isinstance(spec, dict):
                b.warn(rel, "spec is not an object")
                continue
            try:
                fields.update(_DataModel(b, dm_id, spec).parse())
            except Exception as exc:  # noqa: BLE001 - one bad model must not stop the rest
                b.warn(f"data model {dm_id}", f"skipped: {type(exc).__name__}: {exc}")
        principals = _principals(store)
        for rel in store.glob("workbooks/*/workbook.json"):
            wb_dir = rel.rsplit("/", 1)[0]
            try:
                _Workbook(b, store, wb_dir, fields, principals).parse()
            except Exception as exc:  # noqa: BLE001
                b.warn(wb_dir, f"skipped: {type(exc).__name__}: {exc}")
        return b.settle_time_dimensions()


# ---------------------------------------------------------------------- workbook helpers


def _principals(store: RawStore) -> dict[str, str]:
    out: dict[str, str] = {}
    if store.exists("teams.json"):
        for t in _entries(store.get_json("teams.json")):
            if t.get("teamId"):
                out[f"team:{t['teamId']}"] = f"group:{t.get('name') or t['teamId']}"
    if store.exists("members.json"):
        for m in _entries(store.get_json("members.json")):
            if m.get("memberId"):
                out[f"member:{m['memberId']}"] = f"user:{m.get('email') or m['memberId']}"
    return out


def _etype(el: dict) -> str:
    t = str(el.get("kind") or el.get("type") or "").lower()
    viz = str(el.get("vizType") or el.get("visualizationType") or el.get("vizualizationType") or "").lower()
    if t in ("visualization", "viz", "chart", ""):
        t = viz or ("chart" if t else "")
    t = t.removesuffix("-chart")
    return {"pivot-table": "pivot", "input-table": "table", "kpi": "kpi"}.get(t, t)


def _grid_span(value: str | None) -> tuple[float, float]:
    """gridColumn="13 / 25" -> (13, 25)."""
    nums = re.findall(r"\d+", value or "")
    if not nums:
        return 1.0, 2.0
    start = float(nums[0])
    return start, float(nums[1]) if len(nums) > 1 else start + 1


def _grid_cols(value: str | None) -> float:
    m = re.search(r"repeat\(\s*(\d+)", value or "")
    if m:
        return float(m.group(1))
    parts = [p for p in (value or "").split() if p]
    return float(len(parts) or 24)


def _layout_positions(xml: str) -> dict[str, list[tuple[float, float, str]]]:
    """contents.layout XML -> {page id: [(row, x, elementId)]}, container offsets resolved."""
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml or "").strip()
    if not text:
        return {}
    root = ET.fromstring(f"<root>{text}</root>")
    out: dict[str, list[tuple[float, float, str]]] = {}

    def walk(node: ET.Element, page: str, x0: float, y0: float, width: float, cols: float) -> None:
        for child in node:
            cs, ce = _grid_span(child.get("gridColumn"))
            rs, _ = _grid_span(child.get("gridRow"))
            x = x0 + (cs - 1) / cols * width
            y = y0 + (rs - 1)
            if child.get("elementId"):
                out.setdefault(page, []).append((y, x, child.get("elementId", "")))
            if len(child):
                walk(child, page, x, y, width * (ce - cs) / cols, _grid_cols(child.get("gridTemplateColumns")))

    for page in root.iter("Page"):
        walk(page, str(page.get("id") or ""), 0.0, 0.0, 1.0, _grid_cols(page.get("gridTemplateColumns")))
    return out


def _elements(contents: Any) -> list[tuple[dict, dict]]:
    """(page, element) pairs for both representations: flat `elements` + XML layout, or legacy pages[].elements[]."""
    if not isinstance(contents, dict):
        return []
    if isinstance(contents.get("elements"), list):
        pages = [p for p in contents.get("pages") or [] if isinstance(p, dict)]
        by_id = {str(p.get("id")): p for p in pages}
        try:
            positions = _layout_positions(str(contents.get("layout") or ""))
        except ET.ParseError:
            positions = {}
        page_of: dict[str, str] = {eid: pid for pid, items in positions.items() for _, _, eid in items}
        default = pages[0] if len(pages) == 1 else {"id": "", "name": ""}
        return [(by_id.get(page_of.get(str(e.get("id")), ""), default), e) for e in contents["elements"] if isinstance(e, dict)]
    return [(p, e) for p in contents.get("pages", []) or [] if isinstance(p, dict)
            for e in p.get("elements", []) or [] if isinstance(e, dict)]


def _col_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [str(value)]
    if isinstance(value, dict):
        if isinstance(value.get("columnIds"), list):
            return [str(v) for v in value["columnIds"]]
        v = value.get("columnId") or value.get("id") or value.get("column")
        return [str(v)] if v else []
    if isinstance(value, list):
        return [i for v in value for i in _col_ids(v)]
    return []


def _channel_ids(el: dict) -> dict[str, list[str]]:
    raw: dict[str, Any] = {}
    for key in ("encodings", "axes"):
        if isinstance(el.get(key), dict):
            raw.update(el[key])
    raw.update({k: v for k, v in el.items() if k.lower() in CHANNELS})
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        ch = CHANNELS.get(str(key).lower())
        if ch:
            out.setdefault(ch, []).extend(_col_ids(value))
    return {k: v for k, v in out.items() if v}


def _col_name(col: dict) -> str:
    name = _text(col.get("name"))
    if name:
        return name
    m = PURE_REF.match(str(col.get("formula") or ""))
    if m:
        return m.group(1).rpartition("/")[2]
    return str(col.get("id") or col.get("columnId") or "")


class _Workbook:
    def __init__(self, b: Bundle, store: RawStore, wb_dir: str, fields: dict[str, str], principals: dict[str, str]):
        self.b = b
        self.store = store
        self.wb_dir = wb_dir
        self.fields = fields
        self.principals = principals

    def field_kind(self, col: dict) -> str:
        """time | metric | dim for a workbook column."""
        formula = str(col.get("formula") or "")
        if str(col.get("type", "")).lower() in TIME_TYPES:
            return "time"
        try:
            node = fx.parse(formula)
        except fx.FormulaError:
            return "dim"
        if fx.time_grain(node) is not None:
            return "time"
        if any(c in fx.AGGS["sigma"] or c in WINDOW_FUNCS for c in fx.calls(node)):
            return "metric"
        if isinstance(node, fx.Ref):
            return self.fields.get(node.name.rpartition("/")[2].casefold(), "dim")
        if any(self.fields.get(r.rpartition("/")[2].casefold()) == "metric" for r in fx.refs(node)):
            return "metric"
        return "dim"

    def parse(self) -> None:
        store, b, wb_dir = self.store, self.b, self.wb_dir
        wb = store.get_json(f"{wb_dir}/workbook.json")
        wb_id = str(wb.get("workbookId") or wb.get("id") or wb_dir.split("/")[-1])
        contents: Any = None
        for name in ("contents.json", "contents.yaml"):
            if store.exists(f"{wb_dir}/{name}"):
                contents = _load(store, f"{wb_dir}/{name}")
                break
        if isinstance(contents, dict) and isinstance(contents.get("contents"), dict):
            contents = contents["contents"]
        if not isinstance(contents, dict):
            b.warn(f"workbook {wb_id}", "no contents extracted; nothing to build")
            return
        sql_by_el: dict[str, str] = {}
        if store.exists(f"{wb_dir}/queries.json"):
            for q in _entries(store.get_json(f"{wb_dir}/queries.json")):
                if q.get("elementId") and q.get("sql"):
                    sql_by_el[_safe(str(q["elementId"]))] = q["sql"]
        for rel in store.glob(f"{wb_dir}/sql/*.sql"):
            sql_by_el[rel.rsplit("/", 1)[1][:-4]] = store.get_text(rel)

        name = _text(wb.get("name")) or wb_id
        d = Dashboard(
            name=name,
            slug=slugify(name),
            description=_text(wb.get("description")),
            datasource=b.datasource,
            source_ref=f"sigma:workbook/{wb_id}",
            url=wb.get("url", "") or "",
        )
        pairs = _elements(contents)
        all_els = {str(e.get("id") or e.get("elementId") or ""): e for _, e in pairs}
        positions = {}
        if isinstance(contents.get("layout"), str):
            try:
                positions = {eid: (y, x) for items in _layout_positions(contents["layout"]).values() for y, x, eid in items}
            except ET.ParseError:
                b.warn(f"workbook {wb_id}", "layout XML did not parse; tiles keep export order")
        controls: list[tuple[DashboardFilter, set[str]]] = []
        groups: dict[int, tuple[dict, list[dict]]] = {}
        for page, el in pairs:
            groups.setdefault(id(page), (page, []))[1].append(el)
        order = [id(p) for p in contents.get("pages") or [] if isinstance(p, dict)]
        pages = sorted(groups.values(), key=lambda g: order.index(id(g[0])) if id(g[0]) in order else len(order))
        visible = []
        for p, els in pages:
            if str(p.get("visibility", "")).lower() == "hidden":
                b.warn(f"workbook {wb_id} page {_text(p.get('name')) or p.get('id')}", "hidden page (usually source data) not migrated as tiles", "info")
            else:
                visible.append((p, els))
        for page, els in visible:
            if len(visible) > 1 and any(_etype(el) not in SKIP_KINDS | {"control"} for el in els):
                pid = f"page-{page.get('id') or slugify(_text(page.get('name')))}"
                d.tiles.append(Tile(pid, _text(page.get("name")), kind="text", text=f"## {_text(page.get('name'))}", source_type="page"))
                d.layout.append([pid])
            rows: list[list[str]] = []
            positioned: dict[float, list[tuple[float, str]]] = {}
            row: list[str] = []
            for el in els:
                kind = _etype(el)
                el_id = str(el.get("id") or el.get("elementId") or "")
                if kind == "control":
                    controls.append(self.control(el, all_els))
                    continue
                if kind in SKIP_KINDS:
                    continue
                sql = sql_by_el.get(_safe(el_id)) or sql_by_el.get(el_id, "")
                tile = self.tile(el, kind, sql)
                if tile is None:
                    b.warn(f"workbook {wb_id} element {el_id}", f"element kind {kind!r} not migrated", "info")
                    continue
                if tile.kind != "text" and not tile.sql:
                    b.warn(f"workbook {wb_id} element {el_id}", "no generated SQL extracted for this element")
                d.tiles.append(tile)
                pos = positions.get(el_id)
                legacy_pos = el.get("layout") if isinstance(el.get("layout"), dict) and "y" in el["layout"] else None
                if pos is not None:
                    positioned.setdefault(pos[0], []).append((pos[1], tile.id))
                elif legacy_pos:
                    positioned.setdefault(float(legacy_pos.get("y") or 0), []).append((float(legacy_pos.get("x") or 0), tile.id))
                elif tile.kind == "kpi":
                    row.append(tile.id)
                else:
                    if row:
                        rows.append(row)
                        row = []
                    rows.append([tile.id])
            if row:
                rows.append(row)
            rows += [[t for _, t in sorted(positioned[y])] for y in sorted(positioned)]
            d.layout += rows
        tiles_by_id = {t.id: t for t in d.tiles}
        for flt, targets in controls:
            d.filters.append(flt)
            for el_id, el in all_els.items():
                src = el.get("source") if isinstance(el.get("source"), dict) else {}
                if el_id in tiles_by_id and (el_id in targets or str(src.get("elementId", "")) in targets):
                    t = tiles_by_id[el_id]
                    note = (f"Sigma control {flt.name!r} filters this element on {flt.field or '?'}; the extracted SQL has the "
                            f"control's value at extract time baked in - replace that literal with :{flt.name}")
                    t.todo = "; ".join(x for x in (t.todo, note) if x)
        self.grants(d, wb_id)
        b.dashboards.append(d)

    def control(self, el: dict, all_els: dict[str, dict]) -> tuple[DashboardFilter, set[str]]:
        ctype = str(el.get("controlType") or el.get("subtype") or "").lower()
        field, targets = "", set()
        for t in (el.get("filters") or []) + (el.get("targets") or []):
            if not isinstance(t, dict):
                continue
            src = t.get("source") if isinstance(t.get("source"), dict) else {}
            target_el = str(src.get("elementId") or t.get("elementId") or "")
            targets.add(target_el)
            if not field:
                col_id = str(t.get("columnId") or "")
                cols = {str(c.get("id")): c for c in (all_els.get(target_el) or {}).get("columns", []) or [] if isinstance(c, dict)}
                field = str(t.get("columnName") or t.get("column") or (_col_name(cols[col_id]) if col_id in cols else col_id))
        default = el.get("value", el.get("values", el.get("defaultValue")))
        if el.get("mode") in ("last", "next") and el.get("unit") and isinstance(default, (int, float)):
            default = f"{el['mode']} {default} {el['unit']}"
        if default in ([], ""):
            default = None
        return DashboardFilter(
            name=snake(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(el.get("controlId") or "")) or _text(el.get("name")) or el.get("id") or "filter"),
            field=field,
            default=default,
            kind=CONTROL_KINDS.get(ctype, "unknown"),  # type: ignore[arg-type]
        ), targets

    def grants(self, d: Dashboard, wb_id: str) -> None:
        rel = f"{self.wb_dir}/grants.json"
        if not self.store.exists(rel):
            return
        for g in _entries(self.store.get_json(rel)):
            grantee = g.get("grantee") if isinstance(g.get("grantee"), dict) else g
            if grantee.get("teamId"):
                key, fallback = f"team:{grantee['teamId']}", f"group:{grantee['teamId']}"
            elif grantee.get("memberId"):
                key, fallback = f"member:{grantee['memberId']}", f"user:{grantee['memberId']}"
            else:
                continue
            principal = self.principals.get(key, fallback)
            perm = str(g.get("permission") or "view").lower()
            todo = "" if key in self.principals else f"Sigma {key} not resolved to a name (teams/members not extracted); map it to an IdP group or email"
            if principal.startswith("group:") and not todo:
                todo = "Sigma team; confirm it matches an IdP group of the same name"
            actions = ["view", "edit"] if perm in ("edit", "manage", "owner") else ["view"]
            for action in actions:
                self.b.permissions.append(Permission(
                    principal=principal, action=action, resource_type="Notebook", resource_id=d.slug,  # type: ignore[arg-type]
                    source_expression=f"workbook grant: {perm}" + (" (explore lets them change the analysis; the policy model has no separate level)" if perm == "explore" else ""),
                    source_ref=f"sigma:workbook/{wb_id}/grant/{g.get('grantId', '')}", todo=todo,
                ))

    def tile(self, el: dict, kind: str, sql: str) -> Tile | None:
        el_id = str(el.get("id") or el.get("elementId") or "")
        title = _text(el.get("name")) or _text(el.get("title"))
        if kind == "text":
            return Tile(el_id, title, kind="text", text=str(el.get("body") or el.get("text") or el.get("content") or ""), source_type="text")
        chart_kinds = set(MARKS) | {"kpi", "table", "pivot", "chart", "combo", "region-map", "point-map", "geography-map",
                                    "funnel", "sankey", "waterfall", "gauge", "box", "boxplot", "histogram"}
        if kind not in chart_kinds:
            return None
        cols = [c for c in el.get("columns", []) or [] if isinstance(c, dict)]
        by_id = {str(c.get("id") or c.get("columnId")): c for c in cols}
        todos: list[str] = []
        for c in cols:
            formula = str(c.get("formula") or "")
            try:
                used = set(fx.calls(fx.parse(formula))) if formula else set()
            except fx.FormulaError:
                used = set()
            hard = sorted(used & (WINDOW_FUNCS | PASSTHROUGH_FUNCS))
            if hard:
                todos.append(f"column {_col_name(c)!r} uses {', '.join(hard)}(); Sigma's SQL computes it, add a cumulative/derived "
                             "metric or a window in this SQL if it should live in the semantic layer")
        names = [_col_name(c) for c in cols]
        tile_sql, aliases, missing = fx.alias_wrap(sql, names, "sigma_q") if sql else ("", {}, [])
        tile = Tile(
            id=el_id,
            title=title,
            sql=tile_sql.strip(),
            source_type=kind,
            source_config={"elementId": el_id, "kind": kind, "columns": {_col_name(c): c.get("formula", "") for c in cols},
                           "source_sql": bool(sql), "column_aliases": aliases},
        )
        if kind in ("table", "pivot"):
            tile.kind, tile.mark = "table", "table"
            if kind == "pivot":
                todos.append("pivot table: Sigma SQL returns the unpivoted rows; pivot in pandas if needed")
            tile.todo = "; ".join(todos)
            return tile
        if kind == "kpi":
            tile.kind, tile.mark = "kpi", "text"
        else:
            tile.mark = MARKS.get(kind, "unknown")  # type: ignore[assignment]
            if tile.mark == "unknown":
                todos.append(f"Sigma {kind!r} chart has no Altair mark mapping here; pick the closest chart")
        channels = _channel_ids(el)
        if not channels and cols:
            kinds = {str(c.get("id") or c.get("columnId")): self.field_kind(c) for c in cols}
            dims = [i for i, k in kinds.items() if k != "metric"]
            meas = [i for i, k in kinds.items() if k == "metric"]
            if tile.kind == "kpi":
                channels["value"] = (meas or list(kinds))[:1]
            else:
                if dims:
                    channels["x"] = dims[:1]
                if meas:
                    channels["y"] = meas[:1]
                if len(dims) > 1:
                    channels["color"] = dims[1:2]
            if channels:
                todos.append("chart encodings were not in the export; guessed from column formulas, check against Sigma")
        horizontal = str(el.get("orientation", "")).lower() == "horizontal" and tile.mark == "bar"
        for ch, ids in channels.items():
            target = ch
            if ch == "value":
                target = "text" if tile.kind == "kpi" else ("theta" if tile.mark == "arc" else "y")
            if tile.mark == "arc":
                target = {"y": "theta", "x": "color"}.get(target, target)
            elif tile.kind == "kpi" and target != "text":
                continue  # KPI timeline / comparison columns are not part of a single-value tile
            if horizontal:
                target = {"x": "y", "y": "x"}.get(target, target)
            ids = [i for i in ids if i in by_id]
            if not ids:
                continue
            if len(ids) > 1 and target != "tooltip":
                todos.append(f"{len(ids)} columns on {target}; Altair tile keeps the first, fold or split the rest")
            for i in ids if target == "tooltip" else ids[:1]:
                col = by_id[i]
                name = _col_name(col)
                fk = self.field_kind(col)
                if fk == "time" and target in ("x", "y", "color"):
                    typ = "temporal"
                elif target in ("theta", "size", "text") or fk == "metric" or (target == "y" and not horizontal) or (target == "x" and horizontal):
                    typ = "quantitative"
                else:
                    typ = "nominal"
                enc = Encoding(field=aliases.get(name, name), type=typ, title=name if name in aliases else "")  # type: ignore[arg-type]
                if target == "tooltip":
                    tile.encodings.setdefault("tooltip", enc)
                else:
                    tile.encodings[target] = enc
        if sql:
            gone = [n for n in missing if any(aliases.get(n, n) == e.field for e in tile.encodings.values())]
            if gone:
                todos.append(f"Sigma SQL has no output column named {', '.join(gone)}; alias the SELECT columns to match the chart fields")
        tile.todo = "; ".join(todos)
        return tile


# ---------------------------------------------------------------------- data models


class _Element:
    def __init__(self, el: dict, model: SemanticModel | None, name: str, prefix: str, parent: _Element | None = None):
        self.el = el
        self.model = model
        self.name = name  # how other formulas refer to it: [Name/Column]
        self.prefix = prefix  # source table name as it appears in [FCT_ORDERS/Col] ("Custom SQL" for SQL sources)
        self.parent = parent
        self.cols = [c for c in el.get("columns", []) or [] if isinstance(c, dict)]
        self.by_id = {str(c.get("id") or c.get("columnId")): c for c in self.cols}
        self.by_name = {_col_name(c).casefold(): c for c in self.cols}
        self.by_formula = {str(c.get("formula") or "").strip().casefold(): c for c in self.cols if c.get("formula")}
        self.metrics = [m for m in el.get("metrics", []) or [] if isinstance(m, dict)]
        self.metric_by_name = {_text(m.get("name")).casefold(): m for m in self.metrics}
        self.entity = ""


class _DataModel:
    def __init__(self, b: Bundle, dm_id: str, spec: dict):
        self.b = b
        self.dm_id = dm_id
        self.spec = spec
        self.name = _text(spec.get("name")) or dm_id
        self.elements: dict[str, _Element] = {}
        self.element_names: set[str] = set()
        self.extra_rels: list[dict] = []

    # ---------------------------------------------------------- column SQL

    def phys(self, e: _Element, col: dict) -> str:
        explicit = col.get("columnName") or col.get("sourceColumn") or col.get("dbColumn")
        if explicit:
            return _ident(str(explicit))
        m = INODE.match(str(col.get("id") or ""))
        if m:
            return _ident(m.group(1))
        m = PURE_REF.match(str(col.get("formula") or ""))
        if m:
            return _phys(m.group(1).rpartition("/")[2])
        return _phys(_col_name(col))

    def is_source_col(self, e: _Element, col: dict) -> bool:
        formula = str(col.get("formula") or "").strip()
        if e.parent is not None:
            return False  # child element columns point at the parent element, not at a table
        if not formula:
            return True
        m = PURE_REF.match(formula)
        return bool(m and m.group(1).rpartition("/")[0].casefold() == e.prefix.casefold())

    def ref_ctx(self, e: _Element) -> Callable[..., str]:
        def own(name: str, depth: int) -> str:
            col = e.by_name.get(name.casefold())
            if col is None:
                if name.casefold() in e.metric_by_name:
                    raise fx.FormulaError(f"[{name}] is a metric; aggregating a metric needs a derived metric")
                if e.parent is not None:
                    return self.ref_ctx(e.parent)(name, depth + 1)
                return _phys(name)
            return self.col_expr(e, col, depth + 1)

        def col_sql(name: str, depth: int = 0) -> str:
            if depth > 20:
                raise fx.FormulaError("column references loop")
            prefix, _, rest = name.rpartition("/")
            if not prefix:
                return own(name, depth)
            pf = prefix.casefold()
            if pf == e.prefix.casefold():
                col = e.by_formula.get(f"[{name}]".casefold())
                return self.phys(e, col) if col is not None else _phys(rest)
            if pf == e.name.casefold():
                return own(rest, depth)
            anc = e.parent
            while anc is not None:
                if pf in (anc.name.casefold(), anc.prefix.casefold()):
                    return self.ref_ctx(anc)(name, depth + 1)
                anc = anc.parent
            if pf in self.element_names:
                raise fx.FormulaError(f"references another element ({prefix}); needs a join through an entity")
            return _phys(rest)

        return col_sql

    def col_expr(self, e: _Element, col: dict, depth: int = 0) -> str:
        if self.is_source_col(e, col) or col.get("columnName") or col.get("sourceColumn") or col.get("dbColumn"):
            return self.phys(e, col)
        node = fx.parse(str(col.get("formula") or ""))
        col_sql = self.ref_ctx(e)
        return fx.strip_outer(fx.to_sql(node, lambda n: col_sql(n, depth)))

    # ---------------------------------------------------------- elements

    def parse(self) -> dict[str, str]:
        raw = [e for p in self.spec.get("pages", []) or [] if isinstance(p, dict) for e in p.get("elements", []) or [] if isinstance(e, dict)]
        raw += [e for e in self.spec.get("elements", []) or [] if isinstance(e, dict)]
        by_id = {str(e.get("id") or e.get("elementId")): e for e in raw}
        self.element_names = {self.display_name(e).casefold() for e in raw}
        fields: dict[str, str] = {}
        order = sorted(raw, key=lambda e: self.depth(e, by_id))  # parents before children
        taken: set[str] = set()
        for el in order:
            el_id = str(el.get("id") or el.get("elementId") or "")
            where = f"data model {self.name} / element {self.display_name(el) or el_id}"
            kind = str(el.get("kind") or el.get("type") or "table").lower()
            if kind not in ("table", "element", "", "data"):
                continue  # charts, controls and text inside a data model are not semantic objects
            src = el.get("source") if isinstance(el.get("source"), dict) else {}
            skind = str(src.get("kind") or src.get("type") or "").lower()
            if skind == "join":
                self.join_element(el, where)
                continue
            parent = self.elements.get(str(src.get("elementId", ""))) if skind in ("table", "element", "data-model", "datamodel") else None
            table = self.table_for(el, src, skind, parent, where)
            if table is None:
                continue
            name = self.model_name(el, taken)
            model = SemanticModel(
                name=name, table=table, description=_text(el.get("description")), datasource=self.b.datasource,
                source_ref=f"sigma:dataModel/{self.dm_id}#{el_id}",
            )
            prefix = str(src["path"][-1]) if isinstance(src.get("path"), list) and src["path"] else ("Custom SQL" if skind == "sql" else "")
            e = _Element(el, model, self.display_name(el), "" if parent else prefix, parent)
            if parent is not None and (el.get("filters") or el.get("groupings")):
                self.b.warn(where, f"child of {parent.name} with its own filters/groupings; the semantic model {name} ignores them "
                                   "(model the element as a SQL model if they matter)")
            self.elements[el_id] = e
            self.b.semantic_models.append(model)
            if isinstance(src.get("path"), list) and src["path"]:
                self.catalog(e, table, src)
            self.primary_entity(e)
        for e in self.elements.values():
            self.columns(e)
            for c in e.cols:
                fields[_col_name(c).casefold()] = "time" if self.is_time(e, c) else "dim"
        self.relationships()
        for e in self.elements.values():
            fields.update({_text(m.get("name")).casefold(): "metric" for m in e.metrics})
            self.metrics(e)
            self.security(e)
            assert e.model is not None
            where = f"data model {self.name} / element {e.name}"
            if not e.model.entities:
                self.b.warn(where, f"no key column found for {e.model.name}; add a primary entity (MetricFlow needs one to join and filter)", "info")
            if e.model.measures and not any(d.type == "time" for d in e.model.dimensions):
                self.b.warn(where, f"{e.model.name} has measures but no time dimension; set defaults.agg_time_dimension", "info")
        self.knowledge()
        return fields

    def depth(self, el: dict, by_id: dict[str, dict], n: int = 0) -> int:
        src = el.get("source") if isinstance(el.get("source"), dict) else {}
        parent = by_id.get(str(src.get("elementId", "")))
        if parent is None or parent is el or n > 10:
            return n
        return self.depth(parent, by_id, n + 1)

    @staticmethod
    def display_name(el: dict) -> str:
        name = _text(el.get("name"))
        if name:
            return name
        src = el.get("source") if isinstance(el.get("source"), dict) else {}
        if isinstance(src.get("path"), list) and src["path"]:
            return str(src["path"][-1])
        if str(src.get("kind", "")).lower() == "sql":
            return "Custom SQL"
        return str(el.get("id") or "")

    def model_name(self, el: dict, taken: set[str]) -> str:
        name = _text(el.get("name"))
        base = snake(name) if name else snake(TABLE_PREFIX.sub("", snake(self.display_name(el))))
        base = base or snake(str(el.get("id", "element")))
        out, n = base, 2
        while out in taken:
            out, n = f"{base}_{n}", n + 1
        taken.add(out)
        return out

    def table_for(self, el: dict, src: dict, skind: str, parent: _Element | None, where: str) -> str | None:
        if isinstance(src.get("path"), list) and src["path"]:
            return ".".join(_ident(str(p)) for p in src["path"])
        if parent is not None and parent.model is not None:
            return parent.model.table
        if skind == "sql" or src.get("statement"):
            name = f"sigma_{snake(self.display_name(el) if _text(el.get('name')) else str(el.get('id') or 'sql'))}"
            self.b.queries.append(
                VerifiedQuery(
                    name=f"{self.display_name(el)} {el.get('id', '')} (Sigma custom SQL)".replace("  ", " "),
                    sql=str(src.get("statement") or src.get("sql") or ""),
                    description=f"Custom SQL behind Sigma data model {self.name} element {self.display_name(el)}.",
                    datasource=self.b.datasource,
                    source_ref=f"sigma:dataModel/{self.dm_id}#{el.get('id', '')}",
                    todo=f"materialize this as a view or dbt model named {name}; the semantic model points at it",
                )
            )
            self.b.warn(where, f"custom SQL source; semantic model points at {name}, which you need to create")
            return name
        if skind == "csv":
            self.b.warn(where, "CSV upload; load the file into the warehouse and add a semantic model for it by hand")
            return None
        self.b.warn(where, f"unsupported element source {skind or src!r}; skipped")
        return None

    def join_element(self, el: dict, where: str) -> None:
        src = el["source"]
        for j in src.get("joins", []) or []:
            if not isinstance(j, dict):
                continue
            left = str((j.get("left") or {}).get("elementId", ""))
            right = str((j.get("right") or {}).get("elementId", ""))
            cols = [c for c in j.get("columns", []) or [] if isinstance(c, dict)]
            if len(cols) != 1:
                self.b.warn(where, f"join {left} -> {right} on {len(cols)} column pairs; MetricFlow needs one entity column")
                continue
            self.extra_rels.append({"sourceElementId": left, "targetElementId": right, "name": f"{self.display_name(el)} join",
                                    "_leftRef": cols[0].get("left"), "_rightRef": cols[0].get("right"), "_joinType": j.get("joinType")})
        msg = "join element: its joins became entities between the joined elements; MetricFlow joins them at query time"
        if el.get("metrics") or any(not PURE_REF.match(str(c.get("formula") or "")) for c in el.get("columns", []) or [] if isinstance(c, dict)):
            msg += ". Its calculated columns / metrics are not migrated: recreate them on the base model"
        self.b.warn(where, msg, "info")

    def catalog(self, e: _Element, table: str, src: dict) -> None:
        parts = table.split(".")
        name, schema = parts[-1], parts[-2] if len(parts) >= 2 else ""
        if any(t.name == name and t.schema == schema for t in self.b.tables):
            return
        cols = []
        for c in e.cols:
            if not self.is_source_col(e, c):
                continue  # calculated column: lives in the semantic layer, not the catalog
            cols.append(Column(
                name=self.phys(e, c), type=str(c.get("type", "") or ""), description=_text(c.get("description")),
                label=_col_name(c), hidden=bool(c.get("hidden")),
            ))
        self.b.tables.append(Table(self.b.datasource, schema, name, _text(e.el.get("description")), cols,
                                   source_ref=f"sigma:dataModel/{self.dm_id}#{e.el.get('id', '')}"))

    def primary_entity(self, e: _Element) -> None:
        assert e.model is not None
        pk_ids = _col_ids(e.el.get("primaryKey"))
        col = e.by_id.get(pk_ids[0]) if pk_ids else None
        want = _singular(e.model.name)
        if col is None:
            for c in e.cols:
                if snake(_col_name(c)) in (f"{want}_id", "id") or self.phys(e, c) in (f"{want}_id",):
                    col = c
                    break
        if col is None:
            return
        try:
            expr = self.col_expr(e, col)
        except fx.FormulaError:
            return
        e.entity = want
        e.model.entities.append(Entity(want, "primary", expr))

    def is_time(self, e: _Element, col: dict) -> bool:
        if str(col.get("type", "")).lower() in TIME_TYPES or str((col.get("format") or {}).get("kind", "")).lower() == "datetime":
            return True
        try:
            node = fx.parse(str(col.get("formula") or ""))
        except fx.FormulaError:
            return False
        if fx.time_grain(node) is not None:
            return True
        if e.parent is not None and isinstance(node, fx.Ref):
            parent_col = e.parent.by_name.get(node.name.rpartition("/")[2].casefold())
            return parent_col is not None and self.is_time(e.parent, parent_col)
        name = snake(_col_name(col))
        return bool(re.search(r"(^|_)(date|at|time|timestamp)$", name)) and self.is_source_col(e, col)

    def columns(self, e: _Element) -> None:
        assert e.model is not None
        col_sql = self.ref_ctx(e)
        for c in e.cols:
            name = _col_name(c)
            formula = str(c.get("formula") or "").strip()
            try:
                node = fx.parse(formula) if formula else None
            except fx.FormulaError:
                node = None
            if node is not None and set(fx.calls(node)) & USER_FUNCS:
                continue  # row-level security helper column, handled in security()
            dim = Dimension(name=snake(name), label=name if snake(name) != name else "", description=_text(c.get("description")))
            try:
                grain = fx.time_grain(node) if node is not None else None
                if grain:
                    dim.type, dim.time_granularity = "time", grain[0]
                    dim.expr = f"date_trunc('{grain[0]}', {fx.to_sql(grain[1], col_sql)})"
                else:
                    expr = self.col_expr(e, c)
                    dim.expr = "" if expr == dim.name else expr
                    if self.is_time(e, c):
                        dim.type, dim.time_granularity = "time", "day"
            except fx.FormulaError as exc:
                calls = set(fx.calls(node)) if node is not None else set()
                if {"lookup", "rollup"} & calls:
                    hint = "Lookup/Rollup pull from another element: use the joined model's dimension via its entity"
                elif calls & PASSTHROUGH_FUNCS:
                    hint = "warehouse passthrough function: write the SQL expression in the warehouse dialect"
                elif calls & WINDOW_FUNCS:
                    hint = "window function: compute it in the tile SQL"
                else:
                    hint = str(exc)
                dim.todo = f"Sigma column formula not translated ({hint})"
                dim.source_expression, dim.source_language, dim.expr = formula, LANG, ""
            e.model.dimensions.append(dim)

    def relationships(self) -> None:
        rels = list(self.spec.get("relationships", []) or []) + self.extra_rels
        for e in self.elements.values():
            rels += [dict(r, sourceElementId=r.get("sourceElementId") or e.el.get("id")) for r in e.el.get("relationships", []) or [] if isinstance(r, dict)]
        seen = set()
        for r in rels:
            if not isinstance(r, dict):
                continue
            src, tgt = self.elements.get(str(r.get("sourceElementId"))), self.elements.get(str(r.get("targetElementId")))
            label = r.get("name") or r.get("id") or "relationship"
            where = f"data model {self.name} relationship {label}"
            if not src or not tgt:
                self.b.warn(where, "source or target element not migrated; skipped")
                continue
            try:
                if "_leftRef" in r:
                    sk = self.ref_ctx(src)(str(r["_leftRef"]).strip("[]"))
                    tk = self.ref_ctx(tgt)(str(r["_rightRef"]).strip("[]"))
                else:
                    keys = [k for k in r.get("keys", []) or [] if isinstance(k, dict)]
                    if len(keys) != 1:
                        self.b.warn(where, "composite or missing key; MetricFlow needs one entity column, add a surrogate key")
                        continue
                    card = str(r.get("cardinality", "many-to-one")).lower().replace("_", "-")
                    sk_id, tk_id = str(keys[0].get("sourceColumnId")), str(keys[0].get("targetColumnId"))
                    if card.startswith("one-to-many"):
                        src, tgt, sk_id, tk_id = tgt, src, tk_id, sk_id
                    sk = self.col_expr(src, src.by_id[sk_id])
                    tk = self.col_expr(tgt, tgt.by_id[tk_id])
            except (KeyError, fx.FormulaError) as exc:
                self.b.warn(where, f"key column not resolved: {exc}")
                continue
            assert src.model is not None and tgt.model is not None
            if not tgt.entity:
                tgt.entity = _singular(tgt.model.name)
                tgt.model.entities.append(Entity(tgt.entity, "primary", tk))
            key = (src.model.name, tgt.entity)
            if key in seen or any(x.name == tgt.entity for x in src.model.entities):
                continue
            seen.add(key)
            src.model.entities.append(Entity(tgt.entity, "foreign", sk))

    # ---------------------------------------------------------- metrics

    def metrics(self, e: _Element) -> None:
        model = e.model
        assert model is not None
        col_sql = self.ref_ctx(e)
        prefix = e.entity or model.name
        known = {snake(_text(m.get("name"))) for m in e.metrics}
        by_agg: dict[tuple[str, str], str] = {}  # (agg, expr) -> measure name
        simple_metric: dict[tuple[str, str], str] = {}  # (agg, expr) -> unfiltered simple metric name

        def metric_ref(name: str) -> str | None:
            key = name.split("/")[-1].casefold()
            return snake(_text(e.metric_by_name[key].get("name"))) if key in e.metric_by_name else None

        def dim_ref(name: str) -> str:
            col = e.by_name.get(name.split("/")[-1].casefold())
            return f"{prefix}__{snake(_col_name(col) if col else name.split('/')[-1])}"

        def measure_for(agg: fx.Agg, wanted: str) -> str:
            key = (agg.agg, agg.expr)
            if key in by_agg:
                return by_agg[key]
            name = wanted
            if any(m.name == name for m in model.measures):
                name = f"{wanted}_{agg.agg}"
            model.measures.append(Measure(name=name, agg=agg.agg, expr="" if agg.expr == name else agg.expr))
            by_agg[key] = name
            return name

        def side_metric(side: fx.Side, fallback: str) -> str:
            if isinstance(side, fx.MetricRef):
                return side.name
            if side.condition is None and (side.agg, side.expr) in simple_metric:
                return simple_metric[(side.agg, side.expr)]
            name = fallback if side.condition is not None else f"{side.agg}_{snake(side.expr)}"
            measure = measure_for(side, name)
            flt = fx.strip_outer(fx.filter_jinja(side.condition, dim_ref)) if side.condition is not None else ""
            self.b.metrics.append(Metric(name=name, measure=measure, filter=flt, semantic_model=model.name,
                                         description="Helper metric for a Sigma ratio.", source_ref=model.source_ref))
            if not flt:
                simple_metric[(side.agg, side.expr)] = name
            return name

        # simple unfiltered aggregates first, so ratios can reuse them
        ordered = sorted(e.metrics, key=lambda m: 0 if re.fullmatch(r"\s*\w+\(\s*\[[^\]]+\]\s*\)\s*", m.get("formula") or "") else 1)
        for m in ordered:
            name = _text(m.get("name")) or str(m.get("id") or "metric")
            formula = str(m.get("formula") or "").strip()
            metric = Metric(name=name, label=name, description=_text(m.get("description")), semantic_model=model.name,
                            source_ref=f"{model.source_ref}/metric/{m.get('id', snake(name))}")
            try:
                node = fx.parse(formula)
                bad = sorted(set(fx.calls(node)) & (WINDOW_FUNCS | PASSTHROUGH_FUNCS))
                if bad:
                    raise fx.FormulaError(f"{', '.join(bad)}() has no MetricFlow aggregate")
                helpers: set[str] = set()

                def agg_metric(a: fx.Agg) -> str:
                    h = side_metric(a, f"{a.agg}_{snake(a.expr)}_filtered")
                    helpers.add(h)
                    return h

                c = fx.classify(node, LANG, col_sql, metric_ref, agg_metric)
                if c.kind == "agg" and c.agg:
                    if c.agg.condition is None:
                        metric.measure = measure_for(c.agg, snake(name))
                        simple_metric.setdefault((c.agg.agg, c.agg.expr), snake(name))
                    else:
                        metric.measure = measure_for(c.agg, f"{c.agg.agg}_{snake(c.agg.expr)}")
                        metric.filter = fx.strip_outer(fx.filter_jinja(c.agg.condition, dim_ref))
                    for meas in model.measures:
                        if meas.name == metric.measure == snake(name) and not meas.description:
                            meas.description = metric.description
                elif c.kind == "ratio" and c.numerator and c.denominator:
                    metric.type = "ratio"
                    metric.numerator = side_metric(c.numerator, f"{snake(name)}_numerator")
                    metric.denominator = side_metric(c.denominator, f"{snake(name)}_denominator")
                else:
                    missing = [x for x in c.metrics if x not in known and x not in helpers]
                    if missing:
                        raise fx.FormulaError(f"unknown metric {missing[0]}")
                    metric.type, metric.expr, metric.metrics = "derived", fx.strip_outer(c.expr), c.metrics
            except fx.FormulaError as exc:
                calls: set[str] = set()
                try:
                    calls = set(fx.calls(fx.parse(formula)))
                except fx.FormulaError:
                    pass
                win = sorted(calls & WINDOW_FUNCS)
                why = (f"{', '.join(win)}() is a window/lookup function: use a cumulative metric (window/grain_to_date) or a derived metric with offset_window"
                       if win else str(exc))
                metric.todo = f"Sigma metric not translated ({why})"
                metric.source_expression, metric.source_language = formula, LANG
                metric.type, metric.measure, metric.expr, metric.metrics = "simple", "", "", []
                try:
                    node = fx.parse(formula)
                    if isinstance(node, fx.Call) and node.name == "cumulativesum" and len(node.args) == 1:
                        inner = fx.classify(node.args[0], LANG, col_sql, metric_ref)
                        if inner.kind == "agg" and inner.agg and inner.agg.condition is None:
                            metric.type, metric.measure = "cumulative", measure_for(inner.agg, f"{inner.agg.agg}_{snake(inner.agg.expr)}")
                            metric.todo = ("Sigma CumulativeSum() runs along the chart's sort axis; translated as an all-time "
                                           "MetricFlow cumulative metric over the time dimension. Confirm, or add window/grain_to_date")
                except fx.FormulaError:
                    pass
            self.b.metrics.append(metric)

    # ---------------------------------------------------------- security

    def security(self, e: _Element) -> None:
        assert e.model is not None
        table = e.model.table
        col_sql = self.ref_ctx(e)
        filtered = set(_col_ids([f.get("columnId") for f in e.el.get("filters", []) or [] if isinstance(f, dict)]))
        for c in e.cols:
            formula = str(c.get("formula") or "").strip()
            try:
                node = fx.parse(formula) if formula else None
            except fx.FormulaError:
                continue
            if node is None or not set(fx.calls(node)) & USER_FUNCS:
                continue
            cid = str(c.get("id") or c.get("columnId") or "")
            rf = RowFilter(name=snake(f"{e.model.name}_{_col_name(c) or 'rls'}"), table=table, groups=["*"],
                           source_expression=formula, source_ref=f"{e.model.source_ref}/column/{cid}")
            try:
                rf.predicate = fx.strip_outer(fx.to_sql(_user_subst(node), lambda n: _user_ref(n) or col_sql(n)))
                if cid not in filtered:
                    rf.todo = "Sigma column uses CurrentUser*() but no element filter on it was exported; confirm it is enforced"
            except fx.FormulaError as exc:
                rf.todo = f"row-level rule not translated ({exc}); CurrentUserInTeam() maps to `groups:` on the row filter"
            self.b.row_filters.append(rf)
        rules = list(e.el.get("columnSecurity", []) or e.el.get("columnLevelSecurity", []) or [])
        for c in e.cols:
            sec = c.get("security") or c.get("columnSecurity") or c.get("columnLevelSecurity")
            if isinstance(sec, dict):
                rules.append(dict(sec, columnId=c.get("id")))
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            col = e.by_id.get(str(rule.get("columnId")))
            if col is None:
                self.b.warn(f"{e.model.source_ref} column security", "rule for unknown column skipped")
                continue
            teams = [_text(t.get("name") if isinstance(t, dict) else t) for t in (rule.get("allowedTeams") or rule.get("teams") or rule.get("teamIds") or [])]
            users = [_text(u.get("email") if isinstance(u, dict) else u) for u in (rule.get("allowedUsers") or rule.get("users") or [])]
            attr = rule.get("userAttribute") or rule.get("attribute")
            try:
                phys = self.col_expr(e, col)
            except fx.FormulaError:
                phys = snake(_col_name(col))
            cond = " || ".join([f"principal in Group::{json.dumps(t)}" for t in teams] + [f"principal == User::{json.dumps(u)}" for u in users])
            todo = ""
            if attr:
                todo = f"Sigma column security by user attribute {attr!r}; map the allowed attribute values to IdP groups"
            elif not (teams or users):
                todo = "Sigma column security with nobody allowed (or no teams exported); check who may see this column"
            self.b.permissions.append(
                Permission(
                    principal="*",
                    action="query",
                    resource_type="Column",
                    resource_id=f"{table}.{phys}",
                    effect="forbid",
                    condition=f"!({cond})" if cond else "",
                    source_expression=f"column security: visible to {', '.join(teams + users) or '(none)'}"
                    + (f" / user attribute {attr}" if attr else ""),
                    source_ref=f"{e.model.source_ref}/column/{col.get('id', '')}",
                    todo=todo,
                )
            )

    def knowledge(self) -> None:
        lines = []
        desc = _text(self.spec.get("description"))
        if desc:
            lines += [desc, ""]
        for e in self.elements.values():
            described = [c for c in e.cols if c.get("description")]
            if not e.metrics and not e.el.get("description") and not described:
                continue
            lines.append(f"## {e.name}")
            if e.el.get("description"):
                lines.append(_text(e.el["description"]))
            for c in described:
                lines.append(f"- {_col_name(c)}: {_text(c['description'])}")
            for m in e.metrics:
                d = f": {_text(m['description'])}" if m.get("description") else ""
                lines.append(f"- **{_text(m.get('name'))}** (`{m.get('formula', '')}`){d}")
            lines.append("")
        if lines:
            self.b.knowledge.append(
                KnowledgeItem(
                    name=f"Sigma data model {self.name}",
                    body="\n".join(lines),
                    description=f"Definitions from the Sigma data model {self.name}, with original Sigma formulas.",
                    datasource=self.b.datasource,
                    source_ref=f"sigma:dataModel/{self.dm_id}",
                )
            )


def _user_subst(node: fx.Node) -> fx.Node:
    if isinstance(node, fx.Call):
        if node.name == "currentuseremail":
            return fx.Ref("{{user}}email")
        if node.name.startswith("currentuserattribute") and node.args and isinstance(node.args[0], fx.Str):
            return fx.Ref("{{user}}" + node.args[0].value)
        if node.name == "currentuserinteam":
            raise fx.FormulaError("CurrentUserInTeam() is a group check, not a row predicate")
        return fx.Call(node.name, [_user_subst(a) for a in node.args])
    if isinstance(node, fx.BinOp):
        return fx.BinOp(node.op, _user_subst(node.left), _user_subst(node.right))
    if isinstance(node, fx.Unary):
        return fx.Unary(node.op, _user_subst(node.operand))
    return node


def _user_ref(name: str) -> str:
    if name.startswith("{{user}}"):
        return "{{ user." + snake(name[len("{{user}}"):]) + " }}"
    return ""
