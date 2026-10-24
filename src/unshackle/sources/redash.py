"""Redash source (tested against 26.9.0).

Raw layout under migration/raw/redash/:

  instance.json                 {"url", "version"}  version from GET /api/session (client_config.version)
  data_sources.json             GET /api/data_sources: id, name, type, syntax (connection options are not read)
  queries/<id>.json             GET /api/queries/{id}: SQL with {{ mustache }} parameters, options.parameters,
                                visualizations (type + options), tags, is_draft / is_archived
  results/<id>.json             column names and types of the query's latest cached result
                                (GET /api/query_results/{latest_query_data_id}); rows are not kept
  dashboards/<id>.json          GET /api/dashboards/{id}: widgets (visualization + query, or text) with
                                options.position {col, row, sizeX, sizeY} on a 6-column grid and options.parameterMappings
  snippets.json                 GET /api/query_snippets
  security/groups.json          GET /api/groups, each with members (GET /api/groups/{id}/members: id, name, email)
                                and data_sources (GET /api/groups/{id}/data_sources: id, name, view_only)
  security/acl.json             {"queries": {id: acl}, "dashboards": {id: acl}}: GET /api/<kind>/{id}/acl,
                                non-empty ones only ({"modify": [users]})
  usage.json                    GET /api/events (admin; last 90 days, read before anything else so extract's own
                                reads don't count): "view" events per dashboard and query
  _errors.json                  items that failed to extract; parse() turns them into warnings

Query and user API keys, owners and editors are dropped at extract.

Auth: REDASH_API_KEY, a user API key (profile page), sent as `Authorization: Key <key>`. Use an admin's:
other users only see queries on their groups' data sources, and groups, ACLs and events are admin-only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from unshackle.http import Client, SourceError, env
from unshackle.ir import (
    Bundle,
    Dashboard,
    DashboardFilter,
    Encoding,
    KnowledgeItem,
    Layer,
    Permission,
    Tile,
    VerifiedQuery,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources._semyaml import grid_rows
from unshackle.sources.base import Inventory, Source

DROP_KEYS = {"api_key", "user", "last_modified_by", "profile_image_url"}
MUSTACHE = re.compile(r"(')?\{\{\s*([^{}#^/!>&\s][^{}]*?)\s*\}\}(')?")
SECTION = re.compile(r"\{\{\s*[#^/]")
MULTI_IN = re.compile(r"\b(IN)\s*\(\s*\{\{\s*([^{}#^/!>&\s.][^{}.]*?)\s*\}\}\s*\)", re.I)
RANGES = {"date-range", "datetime-range", "datetime-range-with-seconds"}
DATES = {"date", "datetime-local", "datetime-with-seconds"}
MARKS = {"line": "line", "column": "bar", "area": "area", "scatter": "point", "bubble": "point", "pie": "arc"}
X_TYPES = {"datetime": "temporal", "category": "nominal", "linear": "quantitative", "logarithmic": "quantitative"}
RESULT_TYPES = {"datetime": "temporal", "date": "temporal", "integer": "quantitative", "float": "quantitative"}
FILTER_COLUMN = re.compile(r"(::|__)(multi-?)?filter\b", re.I)


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]


def _bind(name: str) -> str:
    b = snake(name)
    return b if b and not b[0].isdigit() else f"p_{b}"


def _multi(p: dict[str, Any]) -> dict[str, Any] | None:
    """multiValuesOptions of a parameter that allows several values."""
    o = p.get("multiValuesOptions")
    return o if isinstance(o, dict) else None


def _list_bind(p: dict[str, Any]) -> bool:
    """Redash joins several values as `prefix + v + suffix` with the separator; with single quotes that's a list of
    string literals, so `IN ({{ p }})` is `IN :p` with a list bound to :p. Without quotes the values are SQL (numbers,
    columns), with double quotes identifiers: those stay TODO."""
    o = _multi(p) or {}
    return o.get("prefix") == "'" and o.get("suffix") == "'" and str(o.get("separator", ",")).strip() == ","


def _dynamic(value: Any) -> bool:
    """Redash's relative dates ("d_last_7_days", "d_now") are computed when the query runs."""
    return isinstance(value, str) and value.startswith("d_")


# ------------------------------------------------------------ mustache parameters


def render(sql: str, params: dict[str, dict[str, Any]], binds: dict[str, str], inline: dict[str, Any]) -> tuple[str, list[str]]:
    """Redash SQL with {{ mustache }} parameters -> SQL with :binds. Returns (sql, todos).

    binds: parameter name -> bind name (a range `p` becomes `<bind>_start` / `<bind>_end`).
    inline: parameter name -> value, substituted as raw text the way Redash does (static widget values).
    Redash splices values in as text, so only `'{{ p }}'` (and a bare number) is a bind; anything else is a TODO.
    """
    todos: list[str] = []
    if SECTION.search(sql):
        todos.append("uses mustache sections ({{# }} / {{^ }}); Redash renders them with the parameter values before running "
                     "the query; rewrite them as SQL over the binds (e.g. a CASE or `:p IS NULL OR ...`)")

    def multi_in(m: re.Match[str]) -> str:
        p = params.get(m.group(2))
        if p is None or m.group(2) in inline or not _list_bind(p):
            return m.group(0)
        return f"{m.group(1)} :{binds[m.group(2)]}"

    def sub(m: re.Match[str]) -> str:
        key = m.group(2)
        name, _, part = key.partition(".")
        p = params.get(name)
        if p is None:
            todos.append(f"{{{{ {key} }}}} is not a declared parameter")
            return m.group(0)
        quoted = bool(m.group(1) and m.group(3))
        if name in inline:
            v = inline[name]
            v = v.get(part) if isinstance(v, dict) and part else v
            if isinstance(v, list):
                o = _multi(p) or {}
                v = str(o.get("separator", ",")).join(f"{o.get('prefix', '')}{x}{o.get('suffix', '')}" for x in v)
            return (m.group(1) or "") + str(v) + (m.group(3) or "")
        typ = str(p.get("type") or "text")
        if o := _multi(p):
            how = (f"quoted with {o.get('prefix')}" if o.get("prefix") else "unquoted") + f", separated by {o.get('separator', ',')!r}"
            todos.append(f"{{{{ {key} }}}} allows several values, which Redash splices into the SQL {how}; only "
                         f"IN ({{{{ {key} }}}}) with single quotes translates (to IN :{binds[name]}, a list); rewrite this use by hand")
            return m.group(0)
        if typ in RANGES:
            if part not in ("start", "end"):
                todos.append(f"{{{{ {key} }}}}: a {typ} parameter has .start and .end")
                return m.group(0)
            bind = f"{binds[name]}_{part}"
        elif part:
            todos.append(f"{{{{ {key} }}}}: parameter {name} is not a range")
            return m.group(0)
        else:
            bind = binds[name]
        if quoted:
            return f":{bind}"
        if typ == "number":
            return (m.group(1) or "") + f":{bind}" + (m.group(3) or "")
        if (m.string.count("'", 0, m.start()) + (1 if m.group(1) else 0)) % 2:
            todos.append(f"{{{{ {key} }}}} ({typ}) is spliced inside a string literal with other text; build the string "
                         f"around the bind instead, e.g. '%{{{{ {key} }}}}%' -> '%' || :{bind} || '%'")
        else:
            todos.append(f"{{{{ {key} }}}} ({typ}) is spliced into the SQL as raw text, without quotes, so its value is SQL "
                         f"(a column, ASC/DESC, a fragment) that a bind can't carry; inline the value the dashboard uses, "
                         f"or turn the choices into SQL over :{bind} (e.g. a CASE)")
        return m.group(0)

    return MUSTACHE.sub(sub, MULTI_IN.sub(multi_in, sql)), todos


def _describe(params: list[dict[str, Any]], binds: dict[str, str]) -> str:
    out = []
    for p in params:
        b = binds[p["name"]]
        typ, v = p.get("type"), p.get("value")
        if typ in RANGES:
            default = f"{v.get('start')} to {v.get('end')}" if isinstance(v, dict) else repr(v)
            out.append(f":{b}_start / :{b}_end ({typ}, default {default})")
        else:
            several = ", a list of values" if _multi(p) else ""
            out.append(f":{b} ({typ}{several}{', default ' + repr(v) if v not in (None, '', []) else ''})")
    return f"Parameters: {', '.join(out)}." if out else ""


# ------------------------------------------------------------ source


class RedashSource(Source):
    name = "redash"
    label = "Redash"
    env_vars = ("REDASH_URL", "REDASH_API_KEY")
    docs = "https://redash.io/help/user-guide/integrations-and-api/api"

    def __init__(self, client: Client | None = None):
        self._client = client

    # ------------------------------------------------------------ network

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(env("REDASH_URL"), headers={"Authorization": f"Key {env('REDASH_API_KEY')}"}, per_minute=300)
        return self._client

    def _get(self, path: str, **kw: Any) -> Any:
        return self.client().get_json(path, **kw)

    def _pages(self, path: str) -> list[dict[str, Any]]:
        """Every row of a paginated list (page_size is capped at 250)."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            res = self._get(path, params={"page": page, "page_size": 250})
            rows = res.get("results") or []
            out += rows
            if len(rows) < 250 or len(out) >= int(res.get("count") or 0):
                return out
            page += 1

    def _usage(self) -> dict[str, dict[str, dict[str, Any]]]:
        since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        out: dict[str, dict[str, dict[str, Any]]] = {"dashboards": {}, "queries": {}}
        for page in range(1, 41):  # 10,000 most recent events, newest first
            res = self._get("/api/events", params={"page": page, "page_size": 250})
            rows = res.get("results") or []
            for r in rows:
                day = str(r.get("created_at") or "")[:10]
                if day < since:
                    return out
                kind = {"dashboard": "dashboards", "query": "queries"}.get(r.get("object_type"))
                if kind and r.get("action") == "view" and r.get("object_id"):
                    u = out[kind].setdefault(str(r["object_id"]), {"events": 0, "last": ""})
                    u["events"] += 1
                    u["last"] = max(u["last"], day)
            if len(rows) < 250 or page * 250 >= int(res.get("count") or 0):
                break
        return out

    def inventory(self) -> Inventory:
        inv = Inventory("redash")
        base = self.client().base_url
        try:
            usage = self._usage()
        except SourceError as exc:
            usage = {}
            inv.notes.append(f"No usage: events need an admin API key ({str(exc)[:120]}).")
        sources = {d["id"]: d for d in self._get("/api/data_sources")}
        for kind, rows in (("dashboard", self._pages("/api/dashboards")), ("query", self._pages("/api/queries"))):
            counts = usage.get("queries" if kind == "query" else "dashboards", {})
            for r in rows:
                u = counts.get(str(r["id"]))
                ds = sources.get(r.get("data_source_id"), {})
                inv.add(kind, f"{kind}:{r['id']}", r.get("name") or "", url=f"{base}/{'queries' if kind == 'query' else 'dashboards'}/{r['id']}",
                        parent=ds.get("name", ""), views=u["events"] if u else (0 if usage else None), last_viewed=u["last"] if u else "",
                        extra={"tags": r.get("tags") or []})
        for d in sources.values():
            inv.add("data_source", f"data_source:{d['id']}", d.get("name") or "", extra={"type": d.get("type"), "syntax": d.get("syntax")})
        inv.notes.append("Ids are dashboard:<id> and query:<id>; pass them to `extract --only` (a dashboard brings its queries). "
                         "Views are Redash 'view' events in the last 90 days (page loads; API reads count too).")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        c = self.client()
        try:
            usage = self._usage()
        except SourceError as exc:
            usage = None
            errors.append({"item": "usage.json", "error": str(exc)[:300]})
        session = self._get("/api/session")
        store.put_json("instance.json", {"url": c.base_url, "version": (session.get("client_config") or {}).get("version", "")})
        store.put_json("data_sources.json", self._get("/api/data_sources"))

        def picked(kind: str, i: Any) -> bool:
            return only is None or f"{kind}:{i}" in only

        dash_ids = [r["id"] for r in self._pages("/api/dashboards") if picked("dashboard", r["id"])]
        query_ids = [r["id"] for r in self._pages("/api/queries") if picked("query", r["id"])]
        if only is not None:  # archived and draft queries aren't listed, but can be asked for by id
            query_ids += sorted({int(i.split(":", 1)[1]) for i in only if i.startswith("query:") and i.split(":", 1)[1].isdigit()} - set(query_ids))
        for did in dash_ids:
            try:
                d = _clean(self._get(f"/api/dashboards/{did}"))
            except SourceError as exc:
                errors.append({"item": f"dashboard {did}", "error": str(exc)[:300]})
                continue
            store.put_json(f"dashboards/{did}.json", d)
            for w in d.get("widgets") or []:
                qid = ((w.get("visualization") or {}).get("query") or {}).get("id")
                if qid and qid not in query_ids:
                    query_ids.append(qid)
        for qid in query_ids:
            try:
                q = _clean(self._get(f"/api/queries/{qid}"))
            except SourceError as exc:
                errors.append({"item": f"query {qid}", "error": str(exc)[:300]})
                continue
            store.put_json(f"queries/{qid}.json", q)
            if q.get("latest_query_data_id"):
                try:
                    res = self._get(f"/api/query_results/{q['latest_query_data_id']}").get("query_result") or {}
                    store.put_json(f"results/{qid}.json", {"columns": (res.get("data") or {}).get("columns") or [],
                                                           "retrieved_at": res.get("retrieved_at")})
                except SourceError as exc:
                    errors.append({"item": f"query {qid} result columns", "error": str(exc)[:300]})

        for rel, call in (("snippets.json", lambda: _clean(self._get("/api/query_snippets"))),
                          ("security/groups.json", self._groups),
                          ("security/acl.json", lambda: self._acl(dash_ids, query_ids))):
            try:
                store.put_json(rel, call())
            except SourceError as exc:
                errors.append({"item": rel, "error": str(exc)[:300]})
        if usage is not None:
            store.put_json("usage.json", usage)
        store.put_json("_errors.json", errors)

    def _groups(self) -> list[dict[str, Any]]:
        out = []
        for g in self._get("/api/groups"):
            members = self._get(f"/api/groups/{g['id']}/members")
            sources = self._get(f"/api/groups/{g['id']}/data_sources")
            out.append({**g, "members": [{k: m.get(k) for k in ("id", "name", "email")} for m in members],
                        "data_sources": [{k: d.get(k) for k in ("id", "name", "view_only")} for d in sources]})
        return out

    def _acl(self, dash_ids: list[int], query_ids: list[int]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {"dashboards": {}, "queries": {}}
        for kind, ids in (("dashboards", dash_ids), ("queries", query_ids)):
            for i in ids:
                acl = self._get(f"/api/{kind}/{i}/acl")
                if acl:
                    out[kind][str(i)] = {access: [{k: u.get(k) for k in ("id", "name", "email")} for u in users]
                                         for access, users in acl.items()}
        return out

    # ------------------------------------------------------------ parse

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool="redash")
        self.store = store
        get = lambda rel, default: store.get_json(rel) if store.exists(rel) else default  # noqa: E731
        self.instance = get("instance.json", {})
        self.sources = {d["id"]: d for d in _list(get("data_sources.json", []))}
        self.queries = {int(Path(r).stem): store.get_json(r) for r in store.glob("queries/*.json")}
        self.results = {int(Path(r).stem): store.get_json(r) for r in store.glob("results/*.json")}
        self.usage = get("usage.json", {})
        used = {q.get("data_source_id") for q in self.queries.values() if not q.get("is_archived")}
        sql_sources = sorted(str(self.sources[i].get("name")) for i in used if i in self.sources and self.sources[i].get("syntax") == "sql")
        if len(sql_sources) > 1:
            b.warn("data sources", f"queries use {len(sql_sources)} Redash data sources ({', '.join(sql_sources)}); "
                                   f"all are written to datasource {b.datasource}")
        b.detect_warehouse(self.sources[i].get("type") for i in used if i in self.sources and self.sources[i].get("syntax") == "sql")
        dashboards = [store.get_json(r) for r in store.glob("dashboards/*.json")]
        self._dashboards(b, dashboards)
        self._queries(b)
        self._permissions(b, dashboards, get("security/groups.json", []), get("security/acl.json", {}))
        snippets = [s for s in _list(get("snippets.json", [])) if isinstance(s, dict)]
        if snippets:
            lines = ["# Redash query snippets", "", "Editor snippets from Redash: typing the trigger inserted this SQL.", ""]
            for s in sorted(snippets, key=lambda s: str(s.get("trigger"))):
                lines += [f"## {s.get('trigger')}", ""] + ([s["description"], ""] if s.get("description") else []) + \
                         ["```sql", str(s.get("snippet") or "").strip(), "```", ""]
            b.knowledge.append(KnowledgeItem("Redash query snippets", "\n".join(lines), "Reusable SQL fragments from the Redash editor",
                                             datasource=b.datasource, source_ref="redash:query_snippets"))
        for e in _list(get("_errors.json", [])):
            b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return b.settle_time_dimensions()

    def _source_todo(self, q: dict[str, Any]) -> str:
        ds = self.sources.get(q.get("data_source_id"))
        if not ds:
            return ""
        if ds.get("type") == "results":
            return (f"runs on Redash's Query Results data source ({ds.get('name')}): SQLite over other queries' cached results "
                    "(query_<id> tables); rewrite it as SQL over the warehouse")
        if ds.get("syntax") != "sql":
            return f"runs on a {ds.get('type')} data source ({ds.get('name')}), not SQL; rewrite it as SQL over the warehouse"
        return ""

    # ------------------------------------------------------------ queries

    def _queries(self, b: Bundle) -> None:
        for qid, q in sorted(self.queries.items()):
            if q.get("is_archived"):
                continue
            params = [p for p in (q.get("options") or {}).get("parameters") or [] if p.get("name")]
            binds = {p["name"]: _bind(p["name"]) for p in params}
            sql, todos = render(str(q.get("query") or ""), {p["name"]: p for p in params}, binds, {})
            src = self._source_todo(q)
            charts = [f"{v.get('name')} ({_viz_kind(v)})" for v in q.get("visualizations") or [] if v.get("type") != "TABLE" or v.get("options")]
            desc = " ".join(x for x in (q.get("description") or "", _describe(params, binds),
                                        f"Redash visualizations: {', '.join(charts)}." if charts else "") if x)
            b.queries.append(VerifiedQuery(q.get("name") or f"query {qid}", sql.strip().rstrip(";").rstrip(), desc, datasource=b.datasource,
                                           source_ref=f"redash:query/{qid}", todo="; ".join(dict.fromkeys(([src] if src else []) + todos))))

    # ------------------------------------------------------------ visualizations -> tiles

    def _tile(self, q: dict[str, Any], viz: dict[str, Any], tid: str, title: str, sql: str, todos: list[str]) -> Tile:
        vtype = str(viz.get("type") or "")
        o = viz.get("options") or {}
        types = {c.get("name"): RESULT_TYPES.get(str(c.get("type")), "nominal") for c in (self.results.get(q.get("id")) or {}).get("columns") or []}
        tile = Tile(id=tid, title=title, sql=sql, source_type=f"{vtype}:{o.get('globalSeriesType')}" if vtype == "CHART" else vtype,
                    source_config=o)
        filters = [c for c in types if FILTER_COLUMN.search(str(c))] or re.findall(r"[\"`]?(\w+(?:::|__)(?:multi-?)?filter)\b", sql, re.I)
        if filters:
            todos.append(f"result columns {sorted(set(filters))} are Redash dashboard filters (filtered in the browser); "
                         "filter the dataframe, or turn them into dashboard parameters")
        if vtype == "CHART":
            self._chart(tile, o, types, todos)
        elif vtype == "COUNTER":
            tile.kind, tile.mark = "kpi", "text"
            if o.get("countRow"):
                todos.append("counter counts the result's rows; wrap the SQL in SELECT COUNT(*)")
            else:
                tile.encodings = {"text": Encoding(str(o.get("counterColName") or "counter"), "quantitative", title=o.get("counterLabel") or "")}
                if int(o.get("rowNumber") or 1) != 1:
                    todos.append(f"counter shows row {o.get('rowNumber')} of the result, not the first")
            if o.get("targetColName"):
                todos.append(f"counter compares with target column {o['targetColName']} (row {o.get('targetRowNumber') or 1}); add the comparison")
        elif vtype == "TABLE":
            tile.kind, tile.mark = "table", "table"
            hidden = [c.get("name") for c in o.get("columns") or [] if c.get("visible") is False]
            if hidden:
                todos.append(f"Redash hides columns {hidden} in this table; drop them from the SELECT")
        elif vtype == "PIVOT":
            tile.kind, tile.mark = "table", "table"
            todos.append("pivot table: pivot the result with pandas (pivot_table) to match Redash")
        else:
            tile.kind = "unknown"
            todos.append(f"Redash {vtype} visualization has no Altair mapping; pick a mark or rebuild")
        tile.todo = "; ".join(dict.fromkeys(todos))
        return tile

    def _chart(self, tile: Tile, o: dict[str, Any], types: dict[str, str], todos: list[str]) -> None:
        gst = str(o.get("globalSeriesType") or "column")
        mapping = o.get("columnMapping") or {}
        role = lambda r: [c for c, v in mapping.items() if v == r]  # noqa: E731
        xs, ys, series = role("x"), role("y"), role("series")
        sopts = o.get("seriesOptions") or {}
        if gst not in MARKS:
            tile.kind = "unknown"
            todos.append(f"Redash {gst} chart has no Altair mapping; pick a mark or rebuild")
            return
        if not xs or not ys:
            tile.kind = "unknown"
            todos.append("chart has no x or y column in columnMapping; set the encodings by hand")
            return
        tile.mark = MARKS[gst]  # type: ignore[assignment]
        xtype = X_TYPES.get(str((o.get("xAxis") or {}).get("type")), "")
        if not xtype:  # "-": Plotly decides from the data
            xtype = types.get(xs[0], "nominal")
            xtype = "nominal" if xtype == "quantitative" and tile.mark == "bar" else xtype
        x = Encoding(xs[0], xtype, title=((o.get("xAxis") or {}).get("title") or {}).get("text") or "")  # type: ignore[arg-type]
        y_title = (((o.get("yAxis") or [{}])[0] or {}).get("title") or {}).get("text") or ""
        enc_y = [Encoding(y, "quantitative", title=(sopts.get(y) or {}).get("name") or y_title) for y in ys]
        if gst == "pie":
            tile.encodings = {"theta": enc_y[0], "color": Encoding(xs[0], "nominal")}
            if len(ys) > 1:
                todos.append(f"Redash draws one pie per y column ({', '.join(ys)}); only the first is encoded")
            return
        xch, ych = ("y", "x") if o.get("swappedAxes") else ("x", "y")
        tile.encodings = {xch: x, ych: enc_y[0]}
        if gst == "bubble" and role("size"):
            tile.encodings["size"] = Encoding(role("size")[0], "quantitative")
        stacking = (o.get("series") or {}).get("stacking")
        if series:
            tile.encodings["color"] = Encoding(series[0], "nominal")
            if tile.mark == "bar" and not stacking and not o.get("swappedAxes"):
                tile.encodings["xOffset"] = Encoding(series[0], "nominal")
            if tile.mark == "area" and not stacking:
                todos.append("Redash overlaps these areas (no stacking); Altair stacks them: add stack=None to the y encoding")
            if len(ys) > 1:
                todos.append(f"{len(ys)} y columns split by series column {series[0]}; Redash draws every combination, only the first y is encoded")
        if (o.get("series") or {}).get("percentValues"):
            todos.append("100% stacked in Redash; normalise in SQL or with stack='normalize'")
        if role("yError"):
            todos.append(f"error bars from {role('yError')} are not drawn")
        if len(ys) > 1 and not series:
            kinds = [MARKS.get(str((sopts.get(y) or {}).get("type") or gst), "unknown") for y in ys]
            if all(k == "bar" for k in kinds):
                todos.append(f"{len(ys)} bar series ({', '.join(ys)}); only the first is encoded: fold them in SQL and use color/xOffset")
            else:
                tile.encodings.pop(ych)
                tile.layers = [Layer(k, {ych: e}) for k, e in zip(kinds, enc_y)]  # type: ignore[arg-type]
                tile.compose = "dual_axis" if any((sopts.get(y) or {}).get("yAxis") == 1 for y in ys) else "layer"

    # ------------------------------------------------------------ dashboards

    def _dashboards(self, b: Bundle, dashboards: list[dict[str, Any]]) -> None:
        base = self.instance.get("url", "")
        for d in sorted(dashboards, key=lambda d: d.get("id") or 0):
            if d.get("is_archived"):
                continue
            did = d.get("id")
            u = (self.usage.get("dashboards") or {}).get(str(did))
            dash = Dashboard(name=d.get("name") or f"Dashboard {did}", slug=slugify(d.get("slug") or d.get("name") or f"dashboard-{did}"),
                             datasource=b.datasource, source_ref=f"redash:dashboard/{did}", url=f"{base}/dashboards/{did}" if base else "",
                             views=u["events"] if u else (0 if self.usage else None))
            saved = {p.get("name"): p.get("value") for p in (d.get("options") or {}).get("parameters") or []}
            filters: dict[str, tuple[str, DashboardFilter]] = {}  # bind -> (Redash parameter name, filter)
            used: set[str] = set()
            grid: list[tuple[str, float, float]] = []
            widgets = [w for w in d.get("widgets") or [] if not (w.get("options") or {}).get("isHidden")]
            per_query: dict[int, int] = {}
            for w in widgets:
                qid = ((w.get("visualization") or {}).get("query") or {}).get("id")
                if qid:
                    per_query[qid] = per_query.get(qid, 0) + 1
            for w in widgets:
                pos = (w.get("options") or {}).get("position") or {}
                viz = w.get("visualization")
                if not viz:
                    tid = _unique(f"text_{w.get('id')}", used)
                    dash.tiles.append(Tile(tid, kind="text", text=str(w.get("text") or ""), source_type="text"))
                    grid.append((tid, float(pos.get("col") or 0), float(pos.get("row") or 0)))
                    continue
                q = self.queries.get((viz.get("query") or {}).get("id")) or viz.get("query") or {}
                title = q.get("name") or ""
                if per_query.get(q.get("id"), 0) > 1:
                    title = f"{title}: {viz.get('name')}"
                tid = _unique(snake(title or f"widget_{w.get('id')}"), used)
                todos = [t for t in [self._source_todo(q)] if t]
                sql = self._widget_sql(q, w, tid, filters, saved, todos)
                dash.tiles.append(self._tile(q, viz, tid, title, sql, todos))
                grid.append((tid, float(pos.get("col") or 0), float(pos.get("row") or 0)))
            order = (d.get("options") or {}).get("globalParamOrder") or []
            dash.filters = [f for name, f in sorted(filters.values(), key=lambda i: order.index(i[0]) if i[0] in order else len(order))]
            dash.layout = grid_rows(grid)
            b.dashboards.append(dash)

    def _widget_sql(self, q: dict[str, Any], w: dict[str, Any], tid: str, filters: dict[str, tuple[str, DashboardFilter]],
                    saved: dict[str, Any], todos: list[str]) -> str:
        """The widget's SQL: dashboard-level parameters bind as the dashboard's :name, widget-level ones as :<tile>_<name>,
        static values are inlined (as Redash does)."""
        params = [p for p in (q.get("options") or {}).get("parameters") or [] if p.get("name")]
        mappings = (w.get("options") or {}).get("parameterMappings") or {}
        binds: dict[str, str] = {}
        inline: dict[str, Any] = {}
        for p in params:
            m = mappings.get(p["name"]) or {"type": "dashboard-level" if p.get("global") else "widget-level", "mapTo": p["name"]}
            if m.get("type") == "static-value":
                inline[p["name"]] = m.get("value")
                continue
            dash_level = m.get("type") == "dashboard-level"
            name = (m.get("mapTo") or p["name"]) if dash_level else f"{tid}_{p['name']}"
            binds[p["name"]] = _bind(name)
            value = saved.get(name, p.get("value")) if dash_level else p.get("value")
            typ = str(p.get("type") or "text")
            if _dynamic(value):
                todos.append(f"parameter {p['name']} defaults to Redash's relative date {value!r}, computed when the query runs; "
                             f"set the default in the parameters cell")
                value = None
            if typ in RANGES:
                for part in ("start", "end"):
                    key = f"{binds[p['name']]}_{part}"
                    filters.setdefault(key, (name, DashboardFilter(key, "", value.get(part) if isinstance(value, dict) else None, "date")))
            else:
                kind = "number" if typ == "number" else "date" if typ in DATES else "value"
                if _list_bind(p) and value not in (None, "") and not isinstance(value, list):
                    value = [value]
                filters.setdefault(binds[p["name"]], (name, DashboardFilter(binds[p["name"]], "", value, kind)))  # type: ignore[arg-type]
        sql, more = render(str(q.get("query") or ""), {p["name"]: p for p in params}, binds, inline)
        todos += more
        for p in params:
            b = binds.get(p["name"])
            if b and _list_bind(p) and re.search(rf"(?<!:):{b}\b", sql):
                todos.append(f":{b} is a list (Redash parameter {p['name']} allows several values); bind it as an expanding "
                             f"parameter: text(sql).bindparams(bindparam({b!r}, expanding=True)), from sqlalchemy")
        return sql.strip().rstrip(";").rstrip()

    # ------------------------------------------------------------ permissions

    def _permissions(self, b: Bundle, dashboards: list[dict[str, Any]], groups: list[dict[str, Any]], acl: dict[str, Any]) -> None:
        access: dict[str, dict[int, bool]] = {}  # principal -> data source id -> view_only
        for g in _list(groups):
            if g.get("type") == "builtin" and g.get("name") == "admin":
                continue  # admins can do everything; give them an IdP admin group instead
            principal = "*" if g.get("type") == "builtin" and g.get("name") == "default" else f"group:{g.get('name')}"
            for ds in g.get("data_sources") or []:
                access.setdefault(principal, {})[ds["id"]] = bool(ds.get("view_only"))
                b.permissions.append(Permission(principal, "view" if ds.get("view_only") else "query", "Datasource", b.datasource,
                                                source_expression=f"data source {ds.get('name')}: {'view only' if ds.get('view_only') else 'full access'}",
                                                source_ref=f"redash:group/{g.get('id')}"))
        unique: dict[tuple[str, ...], Permission] = {}
        for p in b.permissions:  # several Redash data sources are one datasource here
            unique.setdefault((p.principal, p.action, p.resource_type, p.resource_id), p)
        b.permissions = list(unique.values())
        slugs = {d.slug: d for d in b.dashboards}
        for d in dashboards:
            slug = slugify(d.get("slug") or d.get("name") or f"dashboard-{d.get('id')}")
            if slug not in slugs:
                continue
            used = {((w.get("visualization") or {}).get("query") or {}).get("data_source_id") for w in d.get("widgets") or [] if w.get("visualization")}
            used.discard(None)
            for principal, sources in sorted(access.items()):
                seen = used & set(sources)
                if used and not seen:
                    continue
                names = sorted(str(self.sources.get(i, {}).get("name", i)) for i in used - seen)
                b.permissions.append(Permission(
                    principal, "view", "Notebook", slug, source_expression="Redash shows every dashboard; widgets need their data source",
                    source_ref=f"redash:dashboard/{d.get('id')}",
                    todo=f"Redash hides this group the widgets on data sources {names}" if names else ""))
            for access_type, users in ((acl.get("dashboards") or {}).get(str(d.get("id"))) or {}).items():
                for u in users:
                    b.permissions.append(Permission(f"user:{u.get('email') or u.get('id')}", "edit", "Notebook", slug,
                                                    source_expression=f"dashboard ACL: {access_type}", source_ref=f"redash:dashboard/{d.get('id')}/acl"))
        for qid, entries in sorted((acl.get("queries") or {}).items()):
            users = sorted(str(u.get("email") or u.get("id")) for us in entries.values() for u in us)
            name = (self.queries.get(int(qid)) or {}).get("name") or qid
            b.warn(f"query {name}", f"Redash lets {', '.join(users)} edit this query; Cedar has no query resource, so it's not translated", "info")


def _viz_kind(v: dict[str, Any]) -> str:
    t = str(v.get("type") or "")
    return f"{(v.get('options') or {}).get('globalSeriesType') or 'column'} chart" if t == "CHART" else t.lower()


def _unique(base: str, used: set[str]) -> str:
    tid, n = base or "tile", 2
    while tid in used:
        tid, n = f"{base}_{n}", n + 1
    used.add(tid)
    return tid
