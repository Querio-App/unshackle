"""Metabase source (open source and paid editions, tested against v0.64.1).

Raw layout under migration/raw/metabase/:

  instance.json                  {"url", "version"}  version from GET /api/session/properties
  collections.json               GET /api/collection/tree
  cards/<id>.json                GET /api/card/{id}   questions, models and metrics (dataset_query as stored:
                                 MBQL 5 `stages` since v0.57, MBQL 4 `query`/`native` before; both are read)
  cards/<id>.sql                 POST /api/dataset/native (pretty) for query-builder cards: the SQL Metabase runs
  dashboards/<id>.json           GET /api/dashboard/{id}   dashcards (24-column grid), parameters, tabs, view_count
  dashboards/<id>/<dashcard>.sql the card's query compiled with the dashboard's filters set to sentinels
                                 ('unshackle__<slug>', -901234.5, 1901-02-03~1901-04-05; see _probe), so parse()
                                 can bind them as :<slug> (:<slug>_from / _to, _min / _max)
  databases/<id>.json            GET /api/database/{id}/metadata (connection details removed): tables, fields,
                                 PK/FK semantic types, display names, descriptions
  snippets.json                  GET /api/native-query-snippet
  permissions/groups.json        GET /api/permissions/group
  permissions/graph.json         GET /api/permissions/graph   (view-data, create-queries per group and database)
  permissions/collections.json   GET /api/collection/graph    (read / write per group and collection)
  permissions/gtap.json          GET /api/mt/gtap   row and column security; paid plans only
  paid_features.json             paid-only endpoints that were not available, with the reason
  _errors.json                   items that failed to extract; parse() turns them into warnings

Fingerprints, creator/editor details and database connection details are dropped at extract.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
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
    Metric,
    Permission,
    RowFilter,
    SemanticModel,
    Table,
    Tile,
    VerifiedQuery,
    join_todos,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources._semyaml import NField, NJoin, NTable, build_semantic, dimension_ref, grid_rows, sql_literal
from unshackle.sources.base import Inventory, Source

DROP_KEYS = {"fingerprint", "creator", "last-edit-info", "made_public_by_id", "details", "admin_details", "write_data_details"}
TAG = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
OPTIONAL = re.compile(r"\[\[(.*?)\]\]", re.S)
SENTINEL = "unshackle__"
TEXT_PARAMS = ("string/=", "category", "id")
STRING_OPS = ("string/!=", "string/contains", "string/does-not-contain", "string/starts-with", "string/ends-with")
COL = r'(?:"[^"]+"\.)*"[^"]+"|[\w.]+'
DAY = r"\d{4}-\d{2}-\d{2}"
PROBE_NUMBER, PROBE_YEAR = 901234.5, 1901  # + filter index; Metabase writes numbers from 1e7 up in scientific notation
NUMERIC = ("type/Integer", "type/BigInteger", "type/Decimal", "type/Float", "type/Number")
TEMPORAL = ("type/Date", "type/DateTime", "type/DateTimeWithTZ", "type/DateTimeWithLocalTZ", "type/DateTimeWithZoneOffset", "type/Time")
METRIC_AGGS = {"count": "count", "sum": "sum", "avg": "average", "distinct": "count_distinct", "min": "min", "max": "max", "median": "median"}
COMPARE = {"=": "=", "!=": "<>", ">": ">", "<": "<", ">=": ">=", "<=": "<="}
MARKS = {"bar": "bar", "row": "bar", "line": "line", "area": "area", "scatter": "point", "point": "point", "combo": "bar"}
KPI = {"scalar", "smartscalar", "progress", "gauge"}
TABLES = {"table", "pivot", "object"}


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]


# ------------------------------------------------------------ MBQL 4 / 5


def _stages(dq: dict[str, Any]) -> list[dict[str, Any]]:
    """Stages of a dataset_query, MBQL 5 (`stages`) or MBQL 4 (`query` with nested `source-query`)."""
    if isinstance(dq.get("stages"), list):
        return dq["stages"]
    if dq.get("type") == "native":
        nat = dq.get("native") or {}
        return [{"lib/type": "mbql.stage/native", "native": nat.get("query", ""), "template-tags": nat.get("template-tags") or {}}]
    out, q = [], dq.get("query") or {}
    while isinstance(q, dict) and q:
        out.insert(0, {**q, "filters": [q["filter"]] if q.get("filter") else []})
        q = q.get("source-query") or {}
    return out


def _is_native(dq: dict[str, Any]) -> bool:
    st = _stages(dq)
    return bool(st) and st[0].get("lib/type") == "mbql.stage/native"


def _native(dq: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    st = _stages(dq)[0]
    tags = st.get("template-tags") or {}
    if isinstance(tags, list):  # MBQL 5 stores a list
        tags = {t.get("name"): t for t in tags if isinstance(t, dict)}
    return str(st.get("native") or ""), tags


def _clause(c: Any) -> tuple[str, list[Any], dict[str, Any]]:
    """(operator, args, options) of an MBQL 4 or 5 clause.

    MBQL 5: [op, {options with lib/uuid}, *args]. MBQL 4: [op, *args], fields as ["field", id, options].
    """
    if not isinstance(c, list) or not c or not isinstance(c[0], str):
        return "", [], {}
    op = c[0]
    if len(c) > 1 and isinstance(c[1], dict) and "lib/uuid" in c[1]:
        return op, list(c[2:]), c[1]
    if op in ("field", "expression", "aggregation") and len(c) == 3:
        return op, [c[1]], c[2] if isinstance(c[2], dict) else {}
    if op == "aggregation-options" and len(c) == 3:
        inner, args, opts = _clause(c[1])
        return inner, args, {**opts, **(c[2] if isinstance(c[2], dict) else {})}
    return op, list(c[1:]), {}


# ------------------------------------------------------------ native SQL


def _bind(name: str) -> str:
    b = snake(name)
    return b if b and not b[0].isdigit() else f"p_{b}"


def _probe(p: dict[str, Any], i: int, bind: str) -> Any:
    """The value extract compiles dashboard filter number `i` with, so parse() can find it in the SQL (None: not bound).

    Text gets 'unshackle__<slug>', numbers -(901234.5 + i) (and +(901234.5 + i) as the upper bound of `between`),
    dates the range {1901 + i}-02-03~{1901 + i}-04-05. The time-grouping filter compiles with its default unit.
    """
    t = str(p.get("type") or "")
    if t in TEXT_PARAMS or t in STRING_OPS:
        return [SENTINEL + bind]
    if t.startswith("number/"):
        n = PROBE_NUMBER + i
        return [-n, n] if t == "number/between" else [-n]
    if t.startswith("date/"):
        y = PROBE_YEAR + i
        return f"{y}-02-03~{y}-04-05"
    if t == "temporal-unit":
        return p.get("default")
    return None


def _date_bounds(value: Any) -> tuple[str | None, str | None] | None:
    """Inclusive (from, to) days of an absolute Metabase date filter value; None for relative dates and exclusions.

    `~d` (before) and `d~` (after) exclude d, as Metabase compiles them (`< d`, `> d`).
    """
    v = value[0] if isinstance(value, list) and len(value) == 1 else value
    if not isinstance(v, str):
        return None
    if re.fullmatch(DAY, v):
        return v, v
    if (m := re.fullmatch(rf"({DAY})?~({DAY})?", v)) and (m[1] or m[2]):
        if m[1] and m[2]:
            return m[1], m[2]
        if m[2]:
            return None, str(date.fromisoformat(m[2]) - timedelta(days=1))
        return str(date.fromisoformat(m[1]) + timedelta(days=1)), None
    if m := re.fullmatch(r"(\d{4})-(\d{2})", v):
        y, mo = int(m[1]), int(m[2])
        return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    if m := re.fullmatch(r"Q([1-4])-(\d{4})", v):
        y, q = int(m[2]), int(m[1])
        last = 3 * q
        return f"{y:04d}-{last - 2:02d}-01", f"{y:04d}-{last:02d}-{calendar.monthrange(y, last)[1]:02d}"
    return None


def _filter_binds(p: dict[str, Any], bind: str) -> list[tuple[str, Any, str]]:
    """(name, default, kind) of the notebook parameters for one dashboard filter.

    Date filters become <slug>_from / <slug>_to (inclusive days) and `number/between` <slug>_min / <slug>_max;
    the rest keep one parameter named after the slug.
    """
    t = str(p.get("type") or "")
    default = p.get("default")
    if t.startswith("date/"):
        lo, hi = _date_bounds(default) or (None, None)
        return [(f"{bind}_from", lo, "date"), (f"{bind}_to", hi, "date")]
    if t == "number/between":
        lo, hi = (list(default) + [None, None])[:2] if isinstance(default, list) else (None, None)
        return [(f"{bind}_min", lo, "number"), (f"{bind}_max", hi, "number")]
    if isinstance(default, list):
        default = default[0] if len(default) == 1 else default
    kind = "number" if t.startswith("number") else "unknown" if t == "temporal-unit" else "value"
    return [(bind, default, kind)]


def _range(col: str, lo: str, hi: str, cast: bool = False) -> str:
    """`col` between :lo and :hi (inclusive), either bound empty meaning open; `cast` compares the upper bound by day."""
    upper = f"CAST({col} AS date)" if cast else col
    return f"(({col} >= {lo} OR {lo} IS NULL) AND ({upper} <= {hi} OR {hi} IS NULL))"


def _bind_filter(sql: str, t: str, bind: str, i: int) -> tuple[str, str]:
    """Replace the predicate Metabase compiled for dashboard filter number `i` (see _probe) with :binds.

    Returns (sql, status): "bound", "unbound" (the sentinel is there in a shape unshackle doesn't know) or
    "absent". An empty parameter means no filter, as in Metabase. Each :bind is used in a comparison before
    `:bind IS NULL`, so Postgres can tell its type.
    """
    b = f":{bind}"
    if t.startswith("date/"):
        y = PROBE_YEAR + i
        f, to = f"{y}-02-03", f"{y}-04-05"
        lo, hi = f":{bind}_from", f":{bind}_to"
        on_day = re.compile(rf"({COL})\s+BETWEEN\s+date\s+'{f}'\s+AND\s+date\s+'{to}'")  # date columns
        ts = r"timestamp\s+'{}\s+00:00:00(?:\.0+)?'"  # timestamp without time zone: [from, to + 1 day)
        on_ts = re.compile(rf"(\(\s*)?({COL})\s*>=\s*{ts.format(f)}\s*(?(1)\))\s*AND\s*"
                           rf"(\(\s*)?\2\s*<\s*{ts.format(f'{y}-04-06')}\s*(?(3)\))")
        out = on_day.sub(lambda m: _range(m[1], lo, hi), sql)
        out = on_ts.sub(lambda m: _range(m[2], lo, hi, cast=True), out)
        marker = f"'{f}"
    elif t.startswith("number/"):
        n = PROBE_NUMBER + i
        marker = repr(-n)
        lit = re.escape(marker)
        if t == "number/between":
            pat = re.compile(rf"({COL})\s+BETWEEN\s+{lit}\s+AND\s+{re.escape(repr(n))}(?![\d.])")
            out = pat.sub(lambda m: _range(m[1], f":{bind}_min", f":{bind}_max"), sql)
        else:
            out = _bind_compare(sql, t.split("/", 1)[1], lit, b, r"(?![\d.])")
    elif t in TEXT_PARAMS or t == "string/!=":
        marker = f"'{SENTINEL}{bind}'"
        out = _bind_compare(sql, "=" if t != "string/!=" else "!=", re.escape(marker), b, "")
    elif t in STRING_OPS:  # Metabase writes LIKE with _ and % escaped; POSITION / RIGHT match the text exactly
        marker = f"{SENTINEL}{bind}".replace("_", "\\_")
        like = re.escape(marker)
        pattern = {"string/contains": f"%{like}%", "string/does-not-contain": f"%{like}%",
                   "string/starts-with": f"{like}%", "string/ends-with": f"%{like}"}[t]
        pat = re.compile(rf"(?:LOWER\(({COL})\)|({COL}))\s+LIKE\s+'{pattern}'")

        def test(m: re.Match[str]) -> str:
            col, x = (f"LOWER({m[1]})", f"LOWER({b})") if m[1] else (m[2], b)
            return {"string/starts-with": f"POSITION({x} IN {col}) = 1",
                    "string/ends-with": f"RIGHT({col}, LENGTH({x})) = {x}"}.get(t, f"POSITION({x} IN {col}) > 0")

        if t == "string/does-not-contain":
            neg = re.compile(rf"NOT\s*\(\s*{pat.pattern}\s*\)\s*OR\s*\(\s*({COL})\s+IS\s+NULL\s*\)")
            out = neg.sub(lambda m: f"(NOT ({test(m)}) OR {m[3]} IS NULL OR {b} IS NULL)", sql)
        else:
            out = pat.sub(lambda m: f"({test(m)} OR {b} IS NULL)", sql)
    else:
        return sql, "absent"
    return (out, "bound") if out != sql else (sql, "unbound" if marker in sql else "absent")


def _bind_compare(sql: str, op: str, lit: str, b: str, end: str) -> str:
    """`col = lit` -> `(col = :b OR :b IS NULL)`; `(col <> lit) OR (col IS NULL)` keeps NULLs like Metabase."""
    if op == "!=":
        pat = re.compile(rf"\(\s*({COL})\s*<>\s*{lit}{end}\s*\)\s*OR\s*\(\s*\1\s+IS\s+NULL\s*\)")
        return pat.sub(lambda m: f"({m[1]} <> {b} OR {m[1]} IS NULL OR {b} IS NULL)", sql)
    pat = re.compile(rf"({COL})\s*{re.escape(COMPARE[op])}\s*{lit}{end}")
    return pat.sub(lambda m: f"({m[1]} {COMPARE[op]} {b} OR {b} IS NULL)", sql)


class SqlRenderer:
    """Native SQL with Metabase template tags -> plain SQL with :binds (deterministic, offline)."""

    def __init__(self, fields: dict[int, tuple[str, str, str]], snippets: dict[str, str], card_sql):
        self.fields = fields
        self.snippets = snippets
        self.card_sql = card_sql

    def render(self, sql: str, tags: dict[str, dict[str, Any]], binds: dict[str, str] | None = None,
               values: dict[str, Any] | None = None) -> tuple[str, list[str], list[str]]:
        """Returns (sql, todos, notes).

        binds: tag -> :name to use (dashboard filters); tags not in binds become :<tag> unless `values`
        is given, in which case they're inlined from values / the tag default (a dashboard tile).
        """
        todos: list[str] = []
        notes: list[str] = []
        binds = binds or {}

        def known(name: str) -> bool:
            return name in binds or (values is None and tags.get(name, {}).get("default") is not None) or (
                values is not None and (values.get(name) if name in values else tags.get(name, {}).get("default")) is not None
            )

        def opt(m: re.Match[str]) -> str:
            names = [t for t in TAG.findall(m.group(1)) if not t.startswith(("snippet:", "#"))]
            if all(known(n) for n in names):
                return m.group(1)
            notes.append(f"optional clause dropped (no default): {' '.join(m.group(1).split())}")
            return ""

        def tag(m: re.Match[str]) -> str:
            name = m.group(1)
            if name.startswith("snippet:"):
                key = name.split(":", 1)[1].strip()
                if key in self.snippets:
                    return self.snippets[key]
                todos.append(f"snippet {key!r} was not extracted")
                return m.group(0)
            t = tags.get(name, {})
            if name.startswith("#") or t.get("type") == "card":
                cid = t.get("card-id") or (re.match(r"#(\d+)", name) or [None, None])[1]
                inner = self.card_sql(int(cid)) if cid else ""
                if not inner:
                    todos.append(f"references card {cid}, which has no SQL here")
                    return m.group(0)
                return f"(\n{inner.strip().rstrip(';')}\n)"
            if t.get("type") == "dimension":
                return self._field_filter(name, t, binds, values, todos)
            if name in binds:
                return f":{binds[name]}"
            if values is not None:
                v = values.get(name, t.get("default"))
                if isinstance(v, list):
                    v = v[0] if len(v) == 1 else v
                if isinstance(v, list):
                    todos.append(f"parameter {name} has several values {v!r}")
                    return m.group(0)
                return sql_literal(v)
            return f":{_bind(name)}"

        out = OPTIONAL.sub(opt, sql)
        out = TAG.sub(tag, out)
        return out, todos, notes

    def _field_filter(self, name: str, t: dict[str, Any], binds: dict[str, str], values: dict[str, Any] | None,
                      todos: list[str]) -> str:
        """Field filter: Metabase writes `<column> <op> <value>`, or `1 = 1` with no value."""
        op, args, _ = _clause(t.get("dimension"))
        col = self.fields.get(args[0]) if op == "field" and args and isinstance(args[0], int) else None
        widget = str(t.get("widget-type") or "")
        if col is None or widget not in ("category", "string/=", "number/=", "id", "location/city", "location/state"):
            todos.append(f"field filter {{{{{name}}}}} ({widget or 'no widget type'}) needs a hand-written predicate")
            return "1 = 1"
        ref = ".".join(f'"{p}"' for p in col if p)
        if name in binds:
            return f"({ref} = :{binds[name]} OR :{binds[name]} IS NULL)"  # an empty filter is `1 = 1` in Metabase
        default = (values or {}).get(name, t.get("default")) if values is not None else t.get("default")
        if values is not None:
            if default in (None, []):
                return "1 = 1"
            vals = _list(default)
            return f"{ref} = {sql_literal(vals[0])}" if len(vals) == 1 else f"{ref} IN ({', '.join(sql_literal(v) for v in vals)})"
        b = _bind(name)
        return f"({ref} = :{b} OR :{b} IS NULL)"


# ------------------------------------------------------------ source


class MetabaseSource(Source):
    name = "metabase"
    label = "Metabase"
    env_vars = ("METABASE_URL", "METABASE_API_KEY")
    docs = "https://www.metabase.com/docs/latest/api"

    def __init__(self, client: Client | None = None):
        self._client = client

    # ------------------------------------------------------------ network

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(env("METABASE_URL"), headers={"X-API-Key": env("METABASE_API_KEY")}, per_minute=300)
        return self._client

    def _get(self, path: str, **kw: Any) -> Any:
        return self.client().get_json(path, **kw)

    def _search(self) -> list[dict[str, Any]]:
        res = self._get("/api/search", params={"models": ["card", "dataset", "metric", "dashboard"]})
        return [i for i in (res.get("data", []) if isinstance(res, dict) else res) if isinstance(i, dict) and not i.get("archived")]

    def check(self) -> str:
        me = self._get("/api/user/current")
        return f"{self.client().base_url} as {me.get('common_name') or me.get('email') or me.get('id')}"

    def inventory(self) -> Inventory:
        inv = Inventory("metabase")
        base = self.client().base_url
        kinds = {"card": ("question", "question"), "dataset": ("model", "model"), "metric": ("metric", "metric"), "dashboard": ("dashboard", "dashboard")}
        for i in self._search():
            kind, path = kinds.get(i.get("model", ""), ("", ""))
            if not kind:
                continue
            coll = (i.get("collection") or {}).get("name") or ""
            prefix = "dashboard" if kind == "dashboard" else "card"
            inv.add(kind, f"{prefix}:{i.get('id')}", i.get("name", ""), parent=coll, url=f"{base}/{path}/{i.get('id')}",
                    last_viewed=str(i.get("last_viewed_at") or "")[:10])
        for c in _walk_collections(self._get("/api/collection/tree")):
            if not c.get("personal_owner_id"):
                inv.add("collection", f"collection:{c['id']}", c.get("name", ""), parent=c.get("location", ""))
        inv.notes.append("Ids are card:<id>, dashboard:<id>, collection:<id>; pass them to `extract --only`. "
                         "Search has no view counts: dashboards/<id>.json and cards/<id>.json carry view_count after extract.")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        c = self.client()
        props = self._get("/api/session/properties")
        store.put_json("instance.json", {"url": c.base_url, "version": (props.get("version") or {}).get("tag", "")})
        tree = self._get("/api/collection/tree")
        store.put_json("collections.json", _clean(tree))
        items = self._search()
        colls = {str(x["id"]): x for x in _walk_collections(tree)}

        def in_coll(item: dict[str, Any]) -> bool:
            cid = str((item.get("collection") or {}).get("id") or item.get("collection_id") or "")
            path = (colls.get(cid, {}).get("location") or "/").strip("/").split("/") + [cid]
            return any(f"collection:{p}" in (only or set()) for p in path if p)

        def picked(item: dict[str, Any]) -> bool:
            prefix = "dashboard" if item.get("model") == "dashboard" else "card"
            return only is None or f"{prefix}:{item.get('id')}" in only or in_coll(item)

        dash_ids = [i["id"] for i in items if i.get("model") == "dashboard" and picked(i)]
        card_ids = [i["id"] for i in items if i.get("model") != "dashboard" and picked(i)]
        dashboards: dict[int, dict[str, Any]] = {}
        for did in dash_ids:
            try:
                dashboards[did] = _clean(self._get(f"/api/dashboard/{did}"))
            except SourceError as exc:
                errors.append({"item": f"dashboard {did}", "error": str(exc)})
                continue
            store.put_json(f"dashboards/{did}.json", dashboards[did])
            for dc in dashboards[did].get("dashcards") or []:
                for cid in [dc.get("card_id"), *[s.get("id") for s in dc.get("series") or []]]:
                    if cid and cid not in card_ids:
                        card_ids.append(cid)

        cards: dict[int, dict[str, Any]] = {}
        queue = list(card_ids)
        while queue:
            cid = queue.pop(0)
            if cid in cards:
                continue
            try:
                card = cards[cid] = _clean(self._get(f"/api/card/{cid}"))
            except SourceError as exc:
                errors.append({"item": f"card {cid}", "error": str(exc)})
                cards[cid] = {}
                continue
            store.put_json(f"cards/{cid}.json", card)
            dq = card.get("dataset_query") or {}
            if _is_native(dq):
                for t in _native(dq)[1].values():
                    if t.get("type") == "card" and t.get("card-id"):
                        queue.append(int(t["card-id"]))
                continue
            sql = self._compile(dq, errors, f"card {cid}")
            if sql:
                store.put_text(f"cards/{cid}.sql", sql)

        for did, d in dashboards.items():
            params = {p.get("id"): p for p in d.get("parameters") or []}
            for dc in d.get("dashcards") or []:
                card = cards.get(dc.get("card_id")) or {}
                dq = card.get("dataset_query") or {}
                if not dq or _is_native(dq):
                    continue
                bound = []
                for m in dc.get("parameter_mappings") or []:
                    pid = m.get("parameter_id")
                    p = params.get(pid, {})
                    value = _probe(p, list(params).index(pid), _bind(p.get("slug") or p.get("name", ""))) if pid in params else None
                    if value is not None and _clause(m.get("target"))[0] == "dimension":
                        bound.append({"type": p["type"], "target": m["target"], "value": value,
                                      **({"options": p["options"]} if p.get("options") else {})})
                if bound:
                    sql = self._compile({**dq, "parameters": bound}, errors, f"dashboard {did} dashcard {dc.get('id')}")
                    if sql:
                        store.put_text(f"dashboards/{did}/{dc.get('id')}.sql", sql)

        for db in sorted({card.get("database_id") for card in cards.values() if card.get("database_id")}):
            try:
                store.put_json(f"databases/{db}.json", _clean(self._get(f"/api/database/{db}/metadata")))
            except SourceError as exc:
                errors.append({"item": f"database {db}", "error": str(exc)})
        for rel, path in (("snippets.json", "/api/native-query-snippet"), ("permissions/groups.json", "/api/permissions/group"),
                          ("permissions/graph.json", "/api/permissions/graph"), ("permissions/collections.json", "/api/collection/graph")):
            try:
                store.put_json(rel, _clean(self._get(path)))
            except SourceError as exc:
                errors.append({"item": rel, "error": str(exc)})
        paid: dict[str, str] = {}
        try:
            store.put_json("permissions/gtap.json", self._get("/api/mt/gtap"))
        except SourceError as exc:
            paid["row_and_column_security"] = str(exc)[:200]
        store.put_json("paid_features.json", paid)
        store.put_json("_errors.json", errors)

    def _compile(self, dq: dict[str, Any], errors: list[dict[str, str]], item: str) -> str:
        try:
            res = self.client().post_json("/api/dataset/native", json={**dq, "pretty": True})
        except SourceError as exc:
            errors.append({"item": item, "error": f"compile: {exc}"})
            return ""
        sql = (res or {}).get("query") if isinstance(res, dict) else None
        if (res or {}).get("params"):
            errors.append({"item": item, "error": f"compiled SQL has positional params {res['params']!r}; not saved"})
            return ""
        return sql.strip() + "\n" if sql else ""

    # ------------------------------------------------------------ parse

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool="metabase")
        self.store = store
        self.instance = store.get_json("instance.json") if store.exists("instance.json") else {}
        self.cards = {int(Path(r).stem): store.get_json(r) for r in store.glob("cards/*.json")}
        self.dbs = {int(Path(r).stem): store.get_json(r) for r in store.glob("databases/*.json")}
        self.fields: dict[int, tuple[str, str, str]] = {}  # field id -> (schema, table, column)
        self.tables: dict[int, dict[str, Any]] = {}
        for db in self.dbs.values():
            for t in db.get("tables") or []:
                self.tables[t["id"]] = t
                for f in t.get("fields") or []:
                    self.fields[f["id"]] = (t.get("schema") or "", t["name"], f["name"])
        snippets = {s.get("name"): s.get("content", "") for s in _list(store.get_json("snippets.json") if store.exists("snippets.json") else [])
                    if isinstance(s, dict) and not s.get("archived")}
        self.sql = SqlRenderer(self.fields, snippets, self._card_sql)
        if len(self.dbs) > 1:
            names = ", ".join(sorted(str(d.get("name")) for d in self.dbs.values()))
            b.warn("databases", f"cards use {len(self.dbs)} Metabase databases ({names}); all are written to datasource {b.datasource}")
        b.detect_warehouse(d.get("engine") for d in self.dbs.values())
        self._semantic(b)
        on_dash = self._dashboards(b)
        self._queries(b, on_dash)
        self._permissions(b)
        self._collections(b)
        paid = store.get_json("paid_features.json") if store.exists("paid_features.json") else {}
        for feature, why in paid.items():
            b.warn(feature.replace("_", " "), f"paid Metabase feature not available on this instance, skipped ({why})", "info")
        for e in _list(store.get_json("_errors.json") if store.exists("_errors.json") else []):
            b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return b.settle_time_dimensions()

    def _ref(self, kind: str, id: Any) -> str:
        return f"metabase:{kind}/{id}"

    def _card_sql(self, cid: int) -> str:
        card = self.cards.get(cid) or {}
        dq = card.get("dataset_query") or {}
        if dq and _is_native(dq):
            sql, tags = _native(dq)
            return self.sql.render(sql, tags, values={})[0]
        rel = f"cards/{cid}.sql"
        return self.store.get_text(rel) if self.store.exists(rel) else ""

    # ------------------------------------------------------------ semantic layer

    def _semantic(self, b: Bundle) -> None:
        """Catalog tables from the database metadata; semantic models for tables that metrics read."""
        metrics: list[tuple[int, dict[str, Any], dict[str, Any] | None, str]] = []
        for cid, card in sorted(self.cards.items()):
            if card.get("type") != "metric" or card.get("archived"):
                continue
            spec, why = self._simple_metric(card)
            metrics.append((cid, card, spec, why))
        homes = {cid: self._source_table(card) for cid, card, _, _ in metrics}
        used = {t for t in homes.values() if t in self.tables}
        used |= {self._table_of(f["fk_target_field_id"]) for t in used for f in self.tables[t].get("fields") or []
                 if f.get("fk_target_field_id") in self.fields}
        used.discard(None)
        ntables: dict[int, NTable] = {tid: self._ntable(self.tables[tid]) for tid in sorted(used)}
        by_name = {t.name: t for t in ntables.values()}
        keys: dict[int, tuple[NTable, str]] = {}
        for cid, card, spec, _ in metrics:
            if not spec:
                continue
            t = ntables[spec["table_id"]]
            key = card["name"] if card["name"] not in t.metrics else f"{card['name']} {cid}"
            t.metrics[key] = NField(key, spec["column"], spec["agg"], card["name"], card.get("description") or "")
            keys[cid] = (t, key)
        build_semantic(b, list(ntables.values()), "metabase")
        for t in self.tables.values():  # every other table still goes to the catalog
            if t["id"] not in ntables and t.get("visibility_type") not in ("hidden", "retired", "technical", "cruft"):
                nt = self._ntable(t)
                b.tables.append(Table(b.datasource, nt.schema, nt.table, nt.description,
                                      [Column(d.name, d.type, d.description, d.label, d.hidden) for d in nt.dims.values()],
                                      source_ref=nt.source_ref))
        for cid, card, spec, why in metrics:
            if spec:
                t, key = keys[cid]
                m = next(x for x in b.metrics if x.source_ref == f"{t.source_ref}#{key}")
                m.source_ref = self._ref("metric", cid)
                if spec["filter"]:
                    m.filter, ftodo = self._metric_filter(spec["filter"], t, by_name)
                    if ftodo:
                        m.todo, m.filter = ftodo, ""
                        m.source_expression, m.source_language = self._card_sql(cid) or json.dumps(spec["filter"]), "sql"
                continue
            sql = self._card_sql(cid)
            home = ntables.get(homes[cid])
            b.metrics.append(Metric(
                name=snake(card["name"]), label=card["name"], description=card.get("description") or "",
                semantic_model=home.name if home else "",
                source_expression=sql or json.dumps(card.get("dataset_query"), default=str), source_language="sql" if sql else "mbql",
                source_ref=self._ref("metric", cid), todo=f"Metabase metric is not a simple single-table aggregation ({why}); "
                "rebuild it as measures plus a ratio/derived metric (skills/translate-metrics). Compiled SQL is in queries/",
            ))
            if sql:
                b.queries.append(VerifiedQuery(card["name"], sql.strip(), card.get("description") or "", datasource=b.datasource,
                                               metric=True, source_ref=self._ref("metric", cid)))

    def _source_table(self, card: dict[str, Any]) -> int | None:
        st = _stages(card.get("dataset_query") or {})
        src = st[0].get("source-table") if st else None
        return src if isinstance(src, int) else None

    def _table_of(self, field_id: int) -> int | None:
        schema, table, _ = self.fields[field_id]
        return next((tid for tid, t in self.tables.items() if t["name"] == table and (t.get("schema") or "") == schema), None)

    def _ntable(self, t: dict[str, Any]) -> NTable:
        nt = NTable(name=t["name"], table=t["name"], schema=t.get("schema") or "", description=t.get("description") or "",
                    label=t.get("display_name") or "", source_ref=self._ref("table", t["id"]))
        for f in t.get("fields") or []:
            base = str(f.get("effective_type") or f.get("base_type") or "")
            typ = "timestamp" if base in TEMPORAL[1:] else "date" if base == "type/Date" else "number" if base in NUMERIC else (
                "boolean" if base == "type/Boolean" else "string")
            nt.dims[f["name"]] = NField(f["name"], "", typ, f.get("display_name") or "", f.get("description") or "",
                                        f.get("visibility_type") in ("sensitive", "retired"))
            if f.get("semantic_type") == "type/PK" and not nt.primary_key:
                nt.primary_key = f["name"]
            target = f.get("fk_target_field_id")
            if target in self.fields:
                _, ttable, tcol = self.fields[target]
                nt.joins.append(NJoin(f["name"], ttable, tcol))
        return nt

    def _simple_metric(self, card: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """{table_id, agg, column, filter} for one aggregation over one table's columns, else (None, why)."""
        dq = card.get("dataset_query") or {}
        st = _stages(dq)
        if not st or _is_native(dq):
            return None, "native SQL"
        if len(st) > 1:
            return None, "multi-stage query"
        s = st[0]
        if not s.get("source-table") or not isinstance(s["source-table"], int):
            return None, "built on a saved question or model"
        if s.get("joins") or s.get("expressions") or s.get("breakout"):
            return None, "uses joins, custom columns or a breakout"
        aggs = _list(s.get("aggregation"))
        if len(aggs) != 1:
            return None, f"{len(aggs)} aggregations"
        op, args, _ = _clause(aggs[0])
        if op not in METRIC_AGGS:
            return None, f"aggregation {op!r} is a custom expression"
        column = ""
        if op != "count" or args:
            col = self._plain_field(args[0] if args else None, s["source-table"])
            if col is None:
                return None, f"{op} over an expression or a joined column"
            column = col
        filters = _list(s.get("filters"))
        for f in filters:
            if not self._filter_ok(f, s["source-table"]):
                return None, "filter is not a simple comparison on the table's own columns"
        return {"table_id": s["source-table"], "agg": METRIC_AGGS[op], "column": column, "filter": filters}, ""

    def _plain_field(self, ref: Any, table_id: int) -> str | None:
        op, args, opts = _clause(ref)
        if op != "field" or not args or not isinstance(args[0], int) or args[0] not in self.fields:
            return None
        if opts.get("source-field") or opts.get("join-alias") or opts.get("temporal-unit") or opts.get("binning"):
            return None
        if self._table_of(args[0]) != table_id:
            return None
        return self.fields[args[0]][2]

    def _filter_ok(self, f: Any, table_id: int) -> bool:
        op, args, _ = _clause(f)
        if op in ("and", "or"):
            return all(self._filter_ok(a, table_id) for a in args)
        if op in COMPARE or op in ("between", "is-null", "not-null"):
            return bool(args) and self._plain_field(args[0], table_id) is not None and all(
                isinstance(v, (str, int, float, bool)) for v in args[1:])
        return False

    def _metric_filter(self, filters: list[Any], t: NTable, tables: dict[str, NTable]) -> tuple[str, str]:
        todos = []

        def one(f: Any) -> str:
            op, args, _ = _clause(f)
            if op in ("and", "or"):
                inner = [one(a) for a in args]
                return "(" + f" {op.upper()} ".join(inner) + ")"
            _, fargs, _ = _clause(args[0])
            ref, todo = dimension_ref(tables, t, self.fields[fargs[0]][2])
            if todo:
                todos.append(todo)
            vals = [sql_literal(v) for v in args[1:]]
            if op == "is-null":
                return f"{ref} IS NULL"
            if op == "not-null":
                return f"{ref} IS NOT NULL"
            if op == "between":
                return f"{ref} BETWEEN {vals[0]} AND {vals[1]}"
            if op == "=":
                return f"{ref} = {vals[0]}" if len(vals) == 1 else f"{ref} IN ({', '.join(vals)})"
            if op == "!=":  # Metabase's != keeps NULLs (compiled SQL: `x <> 'a' OR x IS NULL`)
                cmp = f"{ref} <> {vals[0]}" if len(vals) == 1 else f"{ref} NOT IN ({', '.join(vals)})"
                return f"({cmp} OR {ref} IS NULL)"
            return f"{ref} {COMPARE[op]} {vals[0]}"

        flat: list[Any] = []
        for f in filters:  # top-level `and` and the list of stage filters are the same conjunction
            op, args, _ = _clause(f)
            flat += args if op == "and" else [f]
        expr = " AND ".join(one(f) for f in flat)
        return expr, join_todos(todos)

    # ------------------------------------------------------------ cards -> SQL, tiles, queries

    def _columns(self, card: dict[str, Any], sql: str) -> list[dict[str, Any]]:
        """Result columns: name (as viz settings use it), alias (as the SQL returns it), role, type."""
        out = []
        for c in _list(card.get("result_metadata")):
            if not isinstance(c, dict) or not c.get("name"):
                continue
            alias = c.get("lib/desired-column-alias") or c["name"]
            if f'"{alias}"' not in sql and alias != c["name"]:
                alias = c["name"]
            base = str(c.get("effective_type") or c.get("base_type") or "")
            src = c.get("source")
            numeric = base in NUMERIC
            role = "metric" if src == "aggregation" or (src not in ("breakout",) and numeric and c.get("semantic_type") not in ("type/PK", "type/FK")) else "dim"
            typ = "temporal" if base in TEMPORAL else "quantitative" if role == "metric" else "ordinal" if numeric else "nominal"
            out.append({"name": c["name"], "alias": alias, "role": role, "type": typ, "title": c.get("display_name") or ""})
        return out

    def _tile(self, card: dict[str, Any], viz: dict[str, Any], tid: str, title: str, sql: str, todos: list[str]) -> Tile:
        display = str(card.get("display") or "table")
        cols = self._columns(card, sql)
        by_name = {c["name"]: c for c in cols}
        tile = Tile(id=tid, title=title, sql=sql, source_type=display, source_config=viz)

        def enc(name: str, force: str = "") -> Encoding | None:
            c = by_name.get(name)
            return Encoding(c["alias"], force or c["type"], title=c["title"]) if c else None  # type: ignore[arg-type]

        dims = [d for d in _list(viz.get("graph.dimensions")) if d] or [c["name"] for c in cols if c["role"] == "dim"][:2]
        mets = [m for m in _list(viz.get("graph.metrics")) if m] or [c["name"] for c in cols if c["role"] == "metric"]
        missing = [n for n in dims + mets if n not in by_name]
        if missing and display not in TABLES:
            todos.append(f"chart columns {missing} are not in the card's result metadata; check the encodings")
        if display in MARKS:
            series = viz.get("series_settings") or {}
            kinds = [str((series.get(m) or {}).get("display") or ("line" if display == "combo" and i else MARKS[display] if display != "combo" else "bar"))
                     for i, m in enumerate(mets)]
            tile.mark = MARKS[display] if display != "combo" else MARKS.get(kinds[0], "bar")  # type: ignore[assignment]
            x = enc(dims[0]) if dims else None
            if display == "scatter" and not dims and len(mets) > 1:
                x, mets = enc(mets[0], "quantitative"), mets[1:]
            ys = [e for e in (enc(m, "quantitative") for m in mets) if e]
            xch, ych = ("y", "x") if display == "row" else ("x", "y")
            tile.encodings = {k: v for k, v in ((xch, x), (ych, ys[0] if ys else None)) if v}
            if len(dims) > 1 and enc(dims[1]):
                tile.encodings["color"] = enc(dims[1], "nominal")  # type: ignore[assignment]
                if tile.mark == "bar" and not viz.get("stackable.stack_type") and display != "row":
                    tile.encodings["xOffset"] = enc(dims[1], "nominal")  # type: ignore[assignment]
            if viz.get("stackable.stack_type") == "normalized":
                todos.append("100% stacked in Metabase; normalise in SQL or with stack='normalize'")
            if len(ys) > 1:
                if tile.mark == "bar" and display != "combo" and all(k == "bar" for k in kinds):
                    todos.append(f"{len(ys)} bar series ({', '.join(mets)}); only the first is encoded: fold them in SQL and use color/xOffset")
                else:
                    tile.encodings.pop(ych, None)
                    tile.layers = [Layer(MARKS.get(k, "unknown"), {ych: e}) for k, e in zip(kinds, ys)]  # type: ignore[arg-type]
                    axes = {(series.get(m) or {}).get("axis") for m in mets}
                    tile.compose = "dual_axis" if "right" in axes and display != "row" else "layer"
                    if display == "combo" and not all((series.get(m) or {}).get("display") for m in mets):
                        todos.append("combo chart series types not saved in the card; assumed bar for the first series and lines for the rest")
        elif display == "pie":
            dim = viz.get("pie.dimension") or (dims[0] if dims else "")
            met = viz.get("pie.metric") or (mets[0] if mets else "")
            tile.mark = "arc"
            tile.encodings = {k: v for k, v in (("theta", enc(met, "quantitative")), ("color", enc(_list(dim)[0] if dim else "", "nominal"))) if v}
        elif display in KPI:
            field = viz.get("scalar.field") or (mets[0] if mets else (cols[0]["name"] if cols else ""))
            tile.kind, tile.mark = "kpi", "text"
            e = enc(field, "quantitative")
            if e:
                tile.encodings = {"text": e}
            if display == "smartscalar":
                todos.append("trend number: the comparison with the previous period is not migrated; add it to the SQL")
            elif display in ("progress", "gauge"):
                todos.append(f"{display} chart: the goal/segments in visualization_settings are not drawn")
        elif display in TABLES:
            tile.kind, tile.mark = "table", "table"
            if display == "pivot":
                todos.append("pivot table: pivot the result with pandas (pivot_table) to match Metabase")
        else:
            tile.kind = "unknown"
            todos.append(f"Metabase {display} chart has no Altair mapping; pick a mark or rebuild")
        if not sql:
            todos.append("no SQL for this card; re-run extract with API access or write it")
        tile.todo = join_todos(dict.fromkeys(todos))
        return tile

    def _dashboards(self, b: Bundle) -> set[int]:
        on_dash: set[int] = set()
        base = self.instance.get("url", "")
        for rel in self.store.glob("dashboards/*.json"):
            d = self.store.get_json(rel)
            did = d.get("id") or Path(rel).stem
            dash = Dashboard(name=d.get("name") or f"Dashboard {did}", slug=slugify(d.get("name") or f"dashboard-{did}"),
                             description=d.get("description") or "", datasource=b.datasource, source_ref=self._ref("dashboard", did),
                             url=f"{base}/dashboard/{did}" if base else "", views=d.get("view_count"))
            params = {p.get("id"): p for p in d.get("parameters") or []}
            names = {pid: _bind(p.get("slug") or p.get("name") or pid) for pid, p in params.items()}
            base_of: dict[str, str] = {}  # notebook parameter -> the filter's slug
            for pid, p in params.items():
                for name, default, kind in _filter_binds(p, names[pid]):
                    base_of[name] = names[pid]
                    dash.filters.append(DashboardFilter(name, "", default, kind))  # type: ignore[arg-type]
            tabs = {t.get("id"): (i, t.get("name") or f"Tab {i + 1}") for i, t in enumerate(d.get("tabs") or [])}
            used: set[str] = set()
            grid: list[tuple[str, float, float]] = []
            fields_of: dict[str, set[str]] = {}
            if len(tabs) > 1:
                for tab_id, (i, name) in tabs.items():
                    tid = _unique(f"tab_{snake(name)}", used)
                    dash.tiles.append(Tile(tid, kind="text", text=f"## {name}", source_type="tab"))
                    grid.append((tid, 0, i * 10000 - 1))
            for dc in d.get("dashcards") or []:
                viz = dc.get("visualization_settings") or {}
                row = (tabs.get(dc.get("dashboard_tab_id"), (0, ""))[0] * 10000 if len(tabs) > 1 else 0) + float(dc.get("row") or 0)
                card = self.cards.get(dc.get("card_id")) or dc.get("card") or {}
                if not dc.get("card_id"):
                    vdisplay = ((viz.get("virtual_card") or {}).get("display")) or "text"
                    tid = _unique(f"{vdisplay}_{dc.get('id')}", used)
                    if vdisplay in ("text", "heading"):
                        text = str(viz.get("text") or "")
                        tile = Tile(tid, kind="text", text=f"## {text}" if vdisplay == "heading" else text, source_type=vdisplay)
                    elif vdisplay == "link":
                        url = ((viz.get("link") or {}).get("url")) or ""
                        tile = Tile(tid, kind="text", text=f"Link: {url}" if url else "Link to Metabase content (not migrated)", source_type=vdisplay)
                    else:
                        tile = Tile(tid, kind="unknown", source_type=vdisplay, source_config=viz,
                                    todo=f"Metabase {vdisplay} card is not migrated automatically")
                    dash.tiles.append(tile)
                    grid.append((tid, float(dc.get("col") or 0), row))
                    continue
                cid = dc["card_id"]
                on_dash.add(cid)
                tid = _unique(snake(card.get("name") or f"card_{cid}"), used)
                title = viz.get("card.title") or card.get("name") or ""
                todos: list[str] = []
                sql = self._dashcard_sql(d, dc, card, names, params, todos)
                merged = {**(card.get("visualization_settings") or {}), **viz}
                tile = self._tile(card, merged, tid, title, sql, todos)
                if dc.get("series"):
                    extra = ", ".join(str(s.get("name") or s.get("id")) for s in dc["series"])
                    tile.todo = join_todos(x for x in (tile.todo, f"overlays other cards ({extra}); add them as layers") if x)
                dash.tiles.append(tile)
                grid.append((tid, float(dc.get("col") or 0), row))
                for m in dc.get("parameter_mappings") or []:
                    op, args, _ = _clause(m.get("target"))
                    fop, fargs, _ = _clause(args[0]) if args else ("", [], {})
                    if op == "dimension" and fop == "field" and fargs and fargs[0] in self.fields:
                        fields_of.setdefault(names.get(m.get("parameter_id"), ""), set()).add(".".join(self.fields[fargs[0]][1:]))
            for f in dash.filters:
                f.field = ", ".join(sorted(fields_of.get(base_of.get(f.name, f.name), set())))
            dash.layout = grid_rows(grid)
            b.dashboards.append(dash)
        return on_dash

    def _dashcard_sql(self, d: dict[str, Any], dc: dict[str, Any], card: dict[str, Any], names: dict[str, str],
                      params: dict[str, dict[str, Any]], todos: list[str]) -> str:
        """The tile's SQL with dashboard filters as :binds (an empty filter means no filter, as in Metabase)."""
        dq = card.get("dataset_query") or {}
        mappings = dc.get("parameter_mappings") or []
        if dq and _is_native(dq):
            raw, tags = _native(dq)
            binds: dict[str, str] = {}
            for m in mappings:
                op, args, _ = _clause(m.get("target"))
                top, targs, _ = _clause(args[0]) if args else ("", [], {})
                if op in ("variable", "dimension") and top == "template-tag" and targs:
                    binds[str(targs[0])] = names.get(m.get("parameter_id"), "")
            sql, more, _ = self.sql.render(raw, tags, binds=binds, values={})
            todos += more
            for name, bind in binds.items():
                p = params.get(next((k for k, v in names.items() if v == bind), ""), {})
                if p.get("default") in (None, []) and tags.get(name, {}).get("type") != "dimension":
                    todos.append(f"dashboard filter {bind} has no default; Metabase leaves {{{{{name}}}}} out when it is empty")
            return sql.strip()  # notes (optional clauses left out) match what Metabase runs with these filters
        rel = f"dashboards/{d.get('id')}/{dc.get('id')}.sql"
        sql = self.store.get_text(rel) if self.store.exists(rel) else self._card_sql(dc["card_id"])
        for m in mappings:
            pid = m.get("parameter_id")
            p = params.get(pid, {})
            t, bind = str(p.get("type") or ""), names.get(pid, "")
            binds = ", ".join(f":{n}" for n, _, _ in _filter_binds(p, bind))
            if t == "temporal-unit":
                unit = p.get("default") or "the card's own unit"
                todos.append(f"dashboard filter {bind} (time grouping) is fixed at {unit} in the SQL; Metabase lets viewers change the unit")
                continue
            sql, status = _bind_filter(sql, t, bind, list(params).index(pid) if pid in params else -1)
            if status == "bound" and t.startswith("date/") and p.get("default") not in (None, []) and not _date_bounds(p["default"]):
                todos.append(f"dashboard filter {bind} defaults to {p['default']!r} in Metabase, which isn't a fixed range of days "
                             f"(relative dates are computed when the dashboard loads); set {binds}")
            elif status == "unbound":
                todos.append(f"dashboard filter {bind} ({t}) compiled to an expression unshackle can't bind; "
                             f"write it in the SQL with {binds}")
            elif status == "absent":
                todos.append(f"dashboard filter {bind} ({t}) applies to this card in Metabase; add it to the SQL as {binds}")
        return sql.strip()

    def _queries(self, b: Bundle, on_dash: set[int]) -> None:
        for cid, card in sorted(self.cards.items()):
            if not card or card.get("archived") or card.get("type") == "metric":
                continue
            kind = card.get("type") or "question"
            dq = card.get("dataset_query") or {}
            desc = card.get("description") or ""
            todo = ""
            if dq and _is_native(dq):
                raw, tags = _native(dq)
                sql, todos, notes = self.sql.render(raw, tags)
                plist = [f":{_bind(n)} ({t.get('type')}{', default ' + repr(t['default']) if t.get('default') is not None else ''})"
                         for n, t in tags.items() if t.get("type") not in ("card", "snippet") and f":{_bind(n)}" in sql]
                desc = " ".join(x for x in (desc, f"Parameters: {', '.join(plist)}." if plist else "", " ".join(notes)) if x)
                todo = join_todos(todos)
            else:
                sql = self._card_sql(cid)
                if not sql:
                    b.warn(f"card {cid}", "query-builder card without compiled SQL; not migrated")
                    continue
            if kind == "model":
                self._model(b, cid, card)
                todo = join_todos(x for x in (todo, f"Metabase model: materialize as a SQL model (queries/models/{snake(card['name'])}.sql; "
                                                 f"dbt: models/unshackle/{snake(card['name'])}.sql) so semantic model {snake(card['name'])} has a table") if x)
            elif cid in on_dash and not (dq and _is_native(dq)):
                continue  # query-builder questions live on as dashboard tiles; native SQL is kept as a query too
            b.queries.append(VerifiedQuery(card.get("name") or f"card {cid}", sql.strip(), desc, datasource=b.datasource,
                                           source_ref=self._ref("model" if kind == "model" else "question", cid), todo=todo))

    def _model(self, b: Bundle, cid: int, card: dict[str, Any]) -> None:
        name = snake(card["name"])
        sm = SemanticModel(name=name, table=name, description=card.get("description") or "", datasource=b.datasource,
                           source_ref=self._ref("model", cid))
        for c in _list(card.get("result_metadata")):
            if not isinstance(c, dict) or not c.get("name"):
                continue
            if c.get("semantic_type") == "type/PK" and not sm.entities:
                sm.entities.append(Entity(name, "primary", c["name"]))
            base = str(c.get("effective_type") or c.get("base_type") or "")
            is_time = base in TEMPORAL
            sm.dimensions.append(Dimension(c["name"], "time" if is_time else "categorical", description=c.get("description") or "",
                                           label=c.get("display_name") or "", time_granularity="day" if is_time else ""))
        if not sm.entities:
            b.warn(f"model {card['name']}", "no column is marked as Entity Key in Metabase; add a primary entity", "info")
        b.semantic_models.append(sm)

    # ------------------------------------------------------------ permissions + collections

    def _permissions(self, b: Bundle) -> None:
        s = self.store
        groups = {str(g["id"]): g for g in _list(s.get_json("permissions/groups.json") if s.exists("permissions/groups.json") else [])}

        def principal(gid: str) -> str:
            g = groups.get(gid, {})
            if g.get("magic_group_type") == "all-internal-users" or g.get("name") == "All Users":
                return "*"
            return f"group:{g.get('name') or gid}"

        graph = (s.get_json("permissions/graph.json") if s.exists("permissions/graph.json") else {}).get("groups") or {}
        for gid, dbs in sorted(graph.items()):
            if groups.get(gid, {}).get("magic_group_type") == "admin":
                continue  # admins can do everything; give them an IdP admin group instead
            for dbid, perms in sorted((dbs or {}).items()):
                if not str(dbid).isdigit() or int(dbid) not in self.dbs:
                    continue
                view, create = perms.get("view-data"), perms.get("create-queries")
                ref = f"metabase:permissions/group/{gid}/database/{dbid}"
                src = json.dumps({"view-data": view, "create-queries": create}, default=str)
                if view == "blocked":
                    b.permissions.append(Permission(principal(gid), "query", "Datasource", b.datasource, "forbid", source_expression=src, source_ref=ref))
                    continue
                if view not in ("unrestricted", None) and not isinstance(view, dict):
                    b.permissions.append(Permission(principal(gid), "query", "Datasource", b.datasource, "forbid", source_expression=src, source_ref=ref,
                                                    todo=f"view-data {view!r} (impersonation / sandboxing / legacy) is not translated; decide this group's access"))
                    continue
                if isinstance(create, str) and create.startswith("query-builder"):
                    note = "" if create == "query-builder-and-native" else "Metabase allowed this group the query builder only (no native SQL); Cedar has no such distinction, decide whether query is right"
                    b.permissions.append(Permission(principal(gid), "query", "Datasource", b.datasource, source_expression=src, source_ref=ref,
                                                    todo=note))
                elif isinstance(create, dict) or isinstance(view, dict):
                    for tid, tname in self._granted_tables(int(dbid), create, view):
                        b.permissions.append(Permission(principal(gid), "query", "Table", tname, source_expression=src, source_ref=ref))
        cgraph = (s.get_json("permissions/collections.json") if s.exists("permissions/collections.json") else {}).get("groups") or {}
        dash_coll = {}
        for rel in s.glob("dashboards/*.json"):
            d = s.get_json(rel)
            dash_coll[slugify(d.get("name") or "")] = str(d.get("collection_id") or "root")
        for gid, colls in sorted(cgraph.items()):
            if groups.get(gid, {}).get("magic_group_type") == "admin":
                continue
            for slug, coll in sorted(dash_coll.items()):
                level = (colls or {}).get(coll)
                if level in ("read", "write"):
                    b.permissions.append(Permission(principal(gid), "edit" if level == "write" else "view", "Notebook", slug,
                                                    source_expression=f"collection {coll}: {level}", source_ref=f"metabase:permissions/collection/{coll}"))
        gtap = s.get_json("permissions/gtap.json") if s.exists("permissions/gtap.json") else []
        for g in _list(gtap):
            if not isinstance(g, dict):
                continue
            t = self.tables.get(g.get("table_id"), {})
            b.row_filters.append(RowFilter(
                name=f"sandbox_{g.get('id')}", table=".".join(x for x in (t.get("schema"), t.get("name")) if x) or str(g.get("table_id")),
                groups=[groups.get(str(g.get("group_id")), {}).get("name") or str(g.get("group_id"))],
                source_expression=json.dumps({k: g.get(k) for k in ("card_id", "attribute_remappings")}, default=str),
                source_ref=f"metabase:gtap/{g.get('id')}",
                todo="Metabase row and column security: write the predicate from attribute_remappings (user attribute -> column) "
                     "or, with a card_id, from that question's filter",
            ))

    def _granted_tables(self, dbid: int, create: Any, view: Any) -> list[tuple[int, str]]:
        out = []
        for t in self.dbs[dbid].get("tables") or []:
            schema = t.get("schema") or ""

            def level(p: Any) -> Any:
                if not isinstance(p, dict):
                    return p
                sp = p.get(schema, p.get("", None))
                return sp.get(str(t["id"]), sp.get(t["id"])) if isinstance(sp, dict) else sp

            c, v = level(create), level(view)
            if isinstance(c, str) and c.startswith("query-builder") and v != "blocked":
                out.append((t["id"], ".".join(x for x in (schema, t["name"]) if x)))
        return out

    def _collections(self, b: Bundle) -> None:
        if not self.store.exists("collections.json"):
            return
        where: dict[str, list[str]] = {}
        for cid, card in self.cards.items():
            if card:
                where.setdefault(str(card.get("collection_id") or "root"), []).append(f"{card.get('type') or 'question'}: {card.get('name')}")
        for rel in self.store.glob("dashboards/*.json"):
            d = self.store.get_json(rel)
            where.setdefault(str(d.get("collection_id") or "root"), []).append(f"dashboard: {d.get('name')} -> notebooks/{slugify(d.get('name') or '')}.ipynb")
        lines = ["# Metabase collections", "", "Where each migrated item lived in Metabase. Collections map to folders and",
                 "to the view/edit policies in policies/metabase.cedar.", ""]
        if where.get("root"):
            lines += ["## Our analytics (root)", ""] + [f"- {x}" for x in sorted(where["root"])] + [""]
        for c in _walk_collections(self.store.get_json("collections.json")):
            if c.get("personal_owner_id") or c.get("is_sample"):
                continue
            items = sorted(where.get(str(c["id"]), []))
            depth = len([p for p in str(c.get("location") or "/").split("/") if p])
            lines += [f"{'#' * min(depth + 2, 6)} {c.get('name')}", ""]
            if c.get("description"):
                lines += [c["description"], ""]
            lines += [f"- {x}" for x in items] + ([""] if items else [])
        b.knowledge.append(KnowledgeItem("Metabase collections", "\n".join(lines), "Folder structure of the Metabase content",
                                         datasource=b.datasource, source_ref="metabase:collections"))


def _walk_collections(tree: Any) -> list[dict[str, Any]]:
    out = []
    for c in _list(tree):
        if isinstance(c, dict) and isinstance(c.get("id"), int):
            out.append(c)
            out += _walk_collections(c.get("children"))
    return out


def _unique(base: str, used: set[str]) -> str:
    tid, n = base or "tile", 2
    while tid in used:
        tid, n = f"{base}_{n}", n + 1
    used.add(tid)
    return tid
