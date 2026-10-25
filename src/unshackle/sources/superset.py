"""Apache Superset source (tested against 6.1.0).

Raw layout under migration/raw/superset/:

  instance.json                       {"url", "version"}  version from GET /version
  export/                             Superset's own export bundle, unzipped without its top folder:
                                      GET /api/v1/assets/export/ (everything), or with --only the dashboard,
                                      chart and dataset exports (/api/v1/<kind>/export/?q=!(ids)). YAML keyed by uuid:
    metadata.yaml
    databases/<name>.yaml             connection settings; Superset masks the password, encrypted_extra is dropped
    datasets/<db>/<name>.yaml         table or virtual SQL, columns (calculated: `expression`), metrics, main_dttm_col
    charts/<name>.yaml                viz_type, params (form_data), query_context (JSON string, null on older charts)
    dashboards/<name>.yaml            position (layout tree), metadata.native_filter_configuration
    queries/<db>/<name>.yaml          SQL Lab saved queries (full export only)
  dashboards.json / charts.json /     GET /api/v1/<kind>/: id <-> uuid, names, dashboard roles (DASHBOARD_RBAC)
  datasets.json
  sql/<chart uuid>.sql                POST /api/v1/chart/data with the chart's query_context and result_type "query":
                                      the SQL Superset runs (one file per query; mixed charts add <uuid>-1.sql)
  sql/<dashboard uuid>/<chart uuid>.sql  the same, with the dashboard's select filters set to a sentinel
                                      ('unshackle__<name>') so parse() can bind them as :<name>
  security/roles.json                 GET /api/v1/security/roles/search/   (needs FAB_ADD_SECURITY_API)
  security/permissions.json           GET /api/v1/security/permissions-resources/ (data access permissions only)
  security/rls.json                   GET /api/v1/rowlevelsecurity/
  usage.json                          GET /api/v1/log/ (admin; last 90 days): dashboard page loads (action "dashboard")
                                      and chart data requests (ChartDataRestApi.data with a slice_id) per id
  _errors.json                        items that failed to extract; parse() turns them into warnings

`unshackle import superset <export.zip | folder>` writes only export/ (from the zip Superset's UI downloads) plus
_import.json; tiles then have no SQL and carry a TODO, and there are no ids, usage, roles or RLS.

Auth: SUPERSET_API_KEY (sent as a bearer token) or SUPERSET_USERNAME / SUPERSET_PASSWORD, exchanged at
POST /api/v1/security/login (SUPERSET_AUTH_PROVIDER: db, the default, or ldap). Extract only reads, plus
POST /api/v1/chart/data, which Superset exempts from CSRF.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from unshackle.http import Client, SourceError, env
from unshackle.ir import (
    Bundle,
    Dashboard,
    DashboardFilter,
    Encoding,
    Layer,
    Metric,
    Permission,
    RowFilter,
    Tile,
    VerifiedQuery,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources import _manual
from unshackle.sources._semyaml import AGG_FN, NField, NTable, build_semantic
from unshackle.sources.base import Inventory, Source

SENTINEL = "unshackle__"
JINJA = re.compile(r"\{\{|\{%")
TIMESERIES = {
    "echarts_timeseries_bar": "bar", "echarts_timeseries_line": "line", "echarts_timeseries_smooth": "line",
    "echarts_timeseries_step": "line", "echarts_timeseries_scatter": "point", "echarts_area": "area",
    "echarts_timeseries": "line",
}
SERIES_TYPES = {"line": "line", "bar": "bar", "scatter": "point", "smooth": "line", "start": "line", "middle": "line", "end": "line"}
KPI = {"big_number_total", "big_number"}
TABLES = {"table", "pivot_table_v2"}
RESHAPE = {"pivot", "flatten", "sort", "rename"}  # post-processing that only reshapes what the SQL returns
AGGS = {"sum": "sum", "count": "count", "avg": "average", "min": "min", "max": "max"}
AGG = re.compile(r"^(sum|count|avg|min|max)\s*\(", re.I)
INT_TYPES = re.compile(r"^(SMALLINT|INTEGER|INT|BIGINT|INT2|INT4|INT8|TINYINT)\b", re.I)
FLOAT_CAST = re.compile(r"\b1\.0*\s*\*|\bcast\s*\(|::\s*(float|double|numeric|decimal|real)", re.I)
DATA_PERMS = {"all_datasource_access", "all_database_access", "database_access", "schema_access", "catalog_access",
              "datasource_access"}
NO_SQL_MANUAL = ("the export zip has no compiled SQL for this chart: write it from source_config, or run "
                 "`unshackle extract superset` to get the SQL Superset runs")
EXPORT_DIRS = {"databases", "datasets", "charts", "dashboards", "queries"}
DROP_DB_KEYS = {"encrypted_extra", "masked_encrypted_extra", "ssh_tunnel", "server_cert"}


def _scrub_db(data: bytes) -> bytes:
    """A database export file without passwords, service-account JSON or SSH tunnel settings."""
    db = {k: v for k, v in (yaml.safe_load(data) or {}).items() if k not in DROP_DB_KEYS}
    return yaml.safe_dump(db, sort_keys=False, allow_unicode=True).encode()


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]


def _bind(name: str) -> str:
    b = snake(name)
    return b if b and not b[0].isdigit() else f"p_{b}"


# ------------------------------------------------------------ metric SQL


def _top_level(expr: str, ch: str) -> list[int]:
    """Positions of `ch` outside parentheses and string literals."""
    out, depth, quote = [], 0, ""
    for i, c in enumerate(expr):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ch and depth == 0:
            out.append(i)
    return out


def _unwrap(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")") and _closes(expr, 0) == len(expr) - 1:
        expr = expr[1:-1].strip()
    return expr


def _closes(expr: str, start: int) -> int:
    """Index of the parenthesis that closes the one at `start`, or -1."""
    depth, quote = 0, ""
    for i in range(start, len(expr)):
        c = expr[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def simple_aggregate(expr: str) -> tuple[str, str] | None:
    """`SUM(x)`, `COUNT(*)`, `COUNT(DISTINCT x)`, `AVG(CASE WHEN ... END)` -> (MetricFlow agg, inner SQL)."""
    e = _unwrap(expr)
    m = AGG.match(e)
    if not m or _closes(e, m.end() - 1) != len(e) - 1:
        return None
    inner = e[m.end():-1].strip()
    if not inner or AGG_FN.search(inner):
        return None
    fn = m.group(1).lower()
    if fn == "count" and inner == "*":
        return "count", "1"
    if fn == "count" and inner[:9].lower() == "distinct ":
        return "count_distinct", inner[9:].strip()
    return AGGS[fn], inner


def ratio_parts(expr: str) -> tuple[str, str] | None:
    """`A / B`, `A / NULLIF(B, 0)`, `1.0 * A / NULLIF(B, 0)` with A and B simple aggregates -> (A, B)."""
    e = _unwrap(expr)
    slashes = _top_level(e, "/")
    if len(slashes) != 1:
        return None
    num, den = e[:slashes[0]].strip(), _unwrap(e[slashes[0] + 1:])
    num = re.sub(r"^1(\.0*)?\s*\*\s*", "", _unwrap(num))
    cast = re.match(r"^cast\s*\((.*)\s+as\s+\w+(\s*\(\s*\d+(\s*,\s*\d+)?\s*\))?\s*\)$", num, re.I | re.S)
    num = cast.group(1) if cast else re.sub(r"::\s*\w+$", "", num)
    nullif = re.match(r"^nullif\s*\((.*),\s*0\s*\)$", den, re.I | re.S)
    den = nullif.group(1) if nullif and _closes(den, den.index("(")) == len(den) - 1 else den
    if simple_aggregate(num) and simple_aggregate(den):
        return num.strip(), den.strip()
    return None


# ------------------------------------------------------------ source


class SupersetSource(Source):
    name = "superset"
    label = "Apache Superset"
    env_vars = ("SUPERSET_URL", "SUPERSET_API_KEY", "SUPERSET_USERNAME", "SUPERSET_PASSWORD")
    docs = "https://superset.apache.org/docs/api"
    manual_files = ("the export .zip Superset downloads (Export on the Dashboards, Charts or Datasets list, or "
                    "`superset export-dashboards`), or that zip unzipped")
    manual_gaps = (
        "chart SQL: the export has form data, not the SQL Superset compiles, so tiles and charts carry a TODO to write it",
        "dashboard filters: without compiled SQL, select filters are not bound as :params; each tile lists them as a TODO",
        "permissions: roles and row-level security are not in the export",
        "usage: no view counts, and no instance URL or ids, so nothing links back to Superset",
    )

    def __init__(self, client: Client | None = None):
        self._client = client

    # ------------------------------------------------------------ network

    def client(self) -> Client:
        if self._client is None:
            c = Client(env("SUPERSET_URL"), per_minute=300)
            key = env("SUPERSET_API_KEY", required=False)
            if not key:
                res = c.post_json("/api/v1/security/login", json={
                    "username": env("SUPERSET_USERNAME"), "password": env("SUPERSET_PASSWORD"),
                    "provider": env("SUPERSET_AUTH_PROVIDER", required=False, default="db"), "refresh": True})
                key = res["access_token"]
            c.set_header("Authorization", f"Bearer {key}")
            self._client = c
        return self._client

    def _get(self, path: str, **kw: Any) -> Any:
        return self.client().get_json(path, **kw)

    def _list_all(self, path: str, columns: str = "") -> list[dict[str, Any]]:
        """Every row of a FAB list endpoint (page_size is capped at 100)."""
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            q = f"(page:{page},page_size:100{',columns:!(' + columns + ')' if columns else ''})"
            res = self._get(path, params={"q": q})
            rows = res.get("result") or []
            out += rows
            if len(rows) < 100 or len(out) >= int(res.get("count") or 0):
                return out
            page += 1

    def _lists(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "dashboards": self._list_all("/api/v1/dashboard/", "id,uuid,dashboard_title,slug,url,published,changed_on_utc,roles.name"),
            "charts": self._list_all("/api/v1/chart/", "id,uuid,slice_name,viz_type,url,datasource_id,changed_on_utc,dashboards.id"),
            "datasets": self._list_all("/api/v1/dataset/", "id,uuid,table_name,schema,sql,kind,database.database_name"),
        }

    def _usage(self) -> dict[str, dict[str, dict[str, Any]]]:
        since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S")
        out: dict[str, dict[str, dict[str, Any]]] = {"dashboards": {}, "charts": {}}
        page = 0
        while page < 100:  # 10,000 most recent events
            res = self._get("/api/v1/log/", params={"q": f"(filters:!((col:dttm,opr:gt,value:'{since}')),order_column:dttm,"
                                                         f"order_direction:desc,page:{page},page_size:100)"})
            rows = res.get("result") or []
            for r in rows:
                for kind, key, actions in (("dashboards", "dashboard_id", ("dashboard",)),
                                           ("charts", "slice_id", ("ChartDataRestApi.data", "explore_json"))):
                    if r.get(key) and r.get("action") in actions:
                        u = out[kind].setdefault(str(r[key]), {"events": 0, "last": ""})
                        u["events"] += 1
                        u["last"] = max(u["last"], str(r.get("dttm") or "")[:10])
            if len(rows) < 100:
                break
            page += 1
        return out

    def inventory(self) -> Inventory:
        inv = Inventory("superset")
        base = self.client().base_url
        lists = self._lists()
        try:
            usage = self._usage()
        except SourceError as exc:
            usage = {}
            inv.notes.append(f"No usage: the action log needs an admin ({str(exc)[:120]}).")
        for kind, rows, title in (("dashboard", lists["dashboards"], "dashboard_title"), ("chart", lists["charts"], "slice_name")):
            counts = usage.get(f"{kind}s", {})
            for r in rows:
                u = counts.get(str(r["id"]))
                inv.add(kind, f"{kind}:{r['id']}", r.get(title) or "", url=f"{base}{r.get('url') or ''}",
                        views=u["events"] if u else (0 if usage else None), last_viewed=u["last"] if u else "",
                        extra={"viz_type": r["viz_type"]} if kind == "chart" else {})
        for r in lists["datasets"]:
            inv.add("dataset", f"dataset:{r['id']}", r.get("table_name") or "", parent=(r.get("database") or {}).get("database_name", ""),
                    extra={"kind": r.get("kind") or ("virtual" if r.get("sql") else "physical"), "schema": r.get("schema") or ""})
        inv.notes.append("Ids are dashboard:<id>, chart:<id>, dataset:<id>; pass them to `extract --only`. "
                         "Views are action-log events in the last 90 days: dashboard page loads, and chart data requests (a dashboard view loads each of its charts).")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        c = self.client()
        try:
            version = c.get_json("/version").get("version_string", "")
        except (SourceError, ValueError):
            version = ""
        store.put_json("instance.json", {"url": c.base_url, "version": version})
        lists = self._lists()
        for kind, rows in lists.items():
            store.put_json(f"{kind}.json", rows)

        files: dict[str, bytes] = {}
        if only is None:
            files.update(self._export("/api/v1/assets/export/"))
        else:
            for kind in ("dashboard", "chart", "dataset"):
                ids = sorted(int(i.split(":", 1)[1]) for i in only if i.startswith(f"{kind}:") and i.split(":", 1)[1].isdigit())
                if ids:
                    files.update(self._export(f"/api/v1/{kind}/export/", params={"q": f"!({','.join(map(str, ids))})"}))
        for rel, data in sorted(files.items()):
            if rel.startswith("databases/"):
                data = _scrub_db(data)
            store.put_bytes(f"export/{rel}", data)

        charts = [yaml.safe_load(d) for r, d in files.items() if r.startswith("charts/")]
        dashboards = [yaml.safe_load(d) for r, d in files.items() if r.startswith("dashboards/")]
        datasets = {y["uuid"]: y for y in (yaml.safe_load(d) for r, d in files.items() if r.startswith("datasets/"))}
        chart_by_uuid = {ch["uuid"]: ch for ch in charts}
        for ch in charts:
            qc = _query_context(ch)
            if qc:
                self._compile(store, f"sql/{ch['uuid']}", qc, errors, f"chart {ch.get('slice_name')}")
        ids = {r["uuid"]: r["id"] for r in lists["charts"]}
        for d in dashboards:
            for ch_uuid, filters in _filters_by_chart(d, chart_by_uuid, datasets, ids).items():
                qc = _query_context(chart_by_uuid[ch_uuid])
                if not qc:
                    continue
                for q in qc.get("queries") or []:
                    q["filters"] = list(q.get("filters") or []) + [
                        {"col": col, "op": "IN", "val": [SENTINEL + _bind(f.get("name") or f["id"])]} for f, col in filters]
                self._compile(store, f"sql/{d['uuid']}/{ch_uuid}", qc, errors,
                              f"dashboard {d.get('dashboard_title')} chart {chart_by_uuid[ch_uuid].get('slice_name')}")

        for rel, call in (("security/roles.json", lambda: self._list_all("/api/v1/security/roles/search/")),
                          ("security/permissions.json", lambda: [p for p in self._list_all("/api/v1/security/permissions-resources/")
                                                                 if (p.get("permission") or {}).get("name") in DATA_PERMS]),
                          ("security/rls.json", lambda: self._list_all("/api/v1/rowlevelsecurity/",
                                                                       "id,name,filter_type,clause,group_key,description,roles.id,roles.name,"
                                                                       "tables.id,tables.schema,tables.table_name")),
                          ("usage.json", self._usage)):
            try:
                store.put_json(rel, call())
            except SourceError as exc:
                errors.append({"item": rel, "error": str(exc)[:300]})
        store.put_json("_errors.json", errors)

    def _export(self, path: str, **kw: Any) -> dict[str, bytes]:
        """An export ZIP as {path below its top folder: bytes}."""
        data = self.client().get(path, **kw).content
        out = {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                parts = PurePosixPath(name).parts[1:]
                if parts and not name.endswith("/") and ".." not in parts:
                    out["/".join(parts)] = z.read(name)
        return out

    def _compile(self, store: RawStore, rel: str, qc: dict[str, Any], errors: list[dict[str, str]], item: str) -> None:
        try:
            res = self.client().post_json("/api/v1/chart/data", json={**qc, "result_type": "query", "result_format": "json"})
        except SourceError as exc:
            errors.append({"item": item, "error": f"compile: {str(exc)[:300]}"})
            return
        for i, r in enumerate(res.get("result") or []):
            if r.get("query"):
                store.put_text(f"{rel}{'' if i == 0 else f'-{i}'}.sql", r["query"].strip() + "\n")

    # ------------------------------------------------------------ parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        """An export bundle (zip already unpacked) -> export/<path below the folder holding metadata.yaml>."""
        files = list(_manual.files_of(path))
        roots = sorted((rel.removesuffix("metadata.yaml") for rel, _ in files if rel.rsplit("/", 1)[-1] == "metadata.yaml"),
                       key=len, reverse=True)
        out: dict[str, bytes] = {}
        for rel, p in files:
            root = next((r for r in roots if rel.startswith(r)), None)
            if root is None:
                continue
            inner = rel[len(root):]
            if inner == "metadata.yaml":
                out["export/metadata.yaml"] = p.read_bytes()
                continue
            if inner.split("/", 1)[0] not in EXPORT_DIRS or not inner.endswith(".yaml"):
                continue
            data = p.read_bytes()
            try:
                doc = yaml.safe_load(data.decode("utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                raise SourceError(f"{rel}: not readable YAML: {exc}") from exc
            if not isinstance(doc, dict) or not doc.get("uuid"):
                raise SourceError(f"{rel}: not a Superset export file (no uuid)")
            if inner.startswith("databases/"):
                data = _scrub_db(data)
            out[f"export/{inner}"] = data
        return out

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool="superset")
        self.store = store
        self.instance = store.get_json("instance.json") if store.exists("instance.json") else {}
        self.manual = store.exists(_manual.IMPORT_META)  # an export zip downloaded by hand: no compiled SQL
        load = lambda pattern: [yaml.safe_load(store.get_text(r)) for r in store.glob(pattern)]  # noqa: E731
        self.datasets = {d["uuid"]: d for d in load("export/datasets/*/*.yaml")}
        self.charts = {c["uuid"]: c for c in load("export/charts/*.yaml")}
        self.dashboards = load("export/dashboards/*.yaml")
        self.databases = {d["uuid"]: d for d in load("export/databases/*.yaml")}
        self.lists = {k: store.get_json(f"{k}.json") if store.exists(f"{k}.json") else [] for k in ("dashboards", "charts", "datasets")}
        self.ids = {r["uuid"]: r["id"] for k in self.lists.values() for r in k}
        if len(self.databases) > 1:
            names = ", ".join(sorted(str(d.get("database_name")) for d in self.databases.values()))
            b.warn("databases", f"datasets use {len(self.databases)} Superset databases ({names}); all are written to datasource {b.datasource}")
        b.detect_warehouse(str(d.get("sqlalchemy_uri") or "").split(":")[0] for d in self.databases.values())
        self.models = self._semantic(b)
        on_dash = self._dashboards(b)
        self._queries(b, on_dash, load("export/queries/*/*.yaml"))
        self._permissions(b)
        for e in _list(store.get_json("_errors.json") if store.exists("_errors.json") else []):
            b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return b.settle_time_dimensions()

    def _ref(self, kind: str, uuid: str) -> str:
        return f"superset:{kind}/{self.ids.get(uuid, uuid)}"

    # ------------------------------------------------------------ semantic layer

    def _semantic(self, b: Bundle) -> dict[str, str]:
        """Datasets -> semantic models. Returns dataset uuid -> model name."""
        tables: list[NTable] = []
        names: dict[str, str] = {}
        used: set[str] = set()
        pending: list[tuple[str, dict[str, Any], str]] = []  # (model, metric, why)
        for uuid, ds in sorted(self.datasets.items(), key=lambda i: (i[1].get("table_name") or "", i[0])):
            name = _unique(snake(ds["table_name"]), used)
            names[uuid] = name
            virtual = bool((ds.get("sql") or "").strip())
            t = NTable(name=name, table=name if virtual else ds["table_name"], schema="" if virtual else ds.get("schema") or "",
                       description=ds.get("description") or "", source_ref=self._ref("dataset", uuid),
                       default_time=ds.get("main_dttm_col") or "")
            types: dict[str, str] = {}
            for col in ds.get("columns") or []:
                if col.get("is_active") is False:
                    continue
                types[col["column_name"]] = str(col.get("type") or "")
                t.dims[col["column_name"]] = NField(col["column_name"], (col.get("expression") or "").strip(), _dim_type(col),
                                                    col.get("verbose_name") or "", col.get("description") or "")
            simple: dict[str, str] = {}  # normalized aggregate SQL -> metric key, so ratios reuse existing metrics
            metrics = sorted(ds.get("metrics") or [], key=lambda m: m["metric_name"])
            for m in metrics:
                agg = simple_aggregate(m.get("expression") or "") if not JINJA.search(m.get("expression") or "") else None
                if agg:
                    t.metrics[m["metric_name"]] = NField(m["metric_name"], agg[1], agg[0], m.get("verbose_name") or "", m.get("description") or "")
                    simple[_norm(m["expression"])] = m["metric_name"]
            for m in metrics:
                expr = m.get("expression") or ""
                if m["metric_name"] in t.metrics:
                    continue
                if JINJA.search(expr):
                    pending.append((name, m, "uses Jinja templating, which runs per query in Superset"))
                    continue
                parts = ratio_parts(expr)
                if not parts:
                    pending.append((name, m, "not a single aggregate or a ratio of two"))
                    continue
                if not FLOAT_CAST.search(expr) and all(_integer(p, types) for p in parts):
                    pending.append((name, m, "both sides are integers, so the warehouse may divide as integers (Postgres, SQL "
                                             "Server and others truncate); the migrated ratio divides as decimals"))
                    continue
                refs = []
                for side, part in zip(("numerator", "denominator"), parts):
                    key = simple.get(_norm(part))
                    if not key:
                        key = f"{m['metric_name']}_{side}"
                        agg, inner = simple_aggregate(part)  # type: ignore[misc]
                        t.metrics[key] = NField(key, inner, agg, f"{m.get('verbose_name') or m['metric_name']} ({side})",
                                                f"{side.title()} of {m['metric_name']}: {part}")
                        simple[_norm(part)] = key
                    refs.append(key)
                t.metrics[m["metric_name"]] = NField(m["metric_name"], f"${{{refs[0]}}} / ${{{refs[1]}}}", "post",
                                                     m.get("verbose_name") or "", m.get("description") or "", source=expr)
            tables.append(t)
        build_semantic(b, tables, "sql")
        virtual = {t.source_ref for t, ds in zip(tables, (self.datasets[u] for u in names)) if (ds.get("sql") or "").strip()}
        b.tables = [t for t in b.tables if t.source_ref not in virtual]  # a virtual dataset has no table until it's materialized
        taken = {m.name for m in b.metrics}
        for model, m, why in pending:
            mname = snake(m["metric_name"]) if snake(m["metric_name"]) not in taken else snake(f"{model}_{m['metric_name']}")
            taken.add(mname)
            b.metrics.append(Metric(name=mname, label=m.get("verbose_name") or "", description=m.get("description") or "",
                                    semantic_model=model, source_expression=m.get("expression") or "", source_language="sql",
                                    source_ref=f"{self._ref('dataset', next(u for u, n in names.items() if n == model))}#{m['metric_name']}",
                                    todo=f"Superset metric {why}; rebuild it as measures plus a ratio/derived metric (skills/translate-metrics)"))
        for sm in b.semantic_models:
            ds = self.datasets[next(u for u, n in names.items() if n == sm.name)]
            for d in sm.dimensions:
                if JINJA.search(d.expr):
                    d.todo, d.source_expression, d.source_language = "calculated column uses Jinja templating", d.expr, "sql"
            if JINJA.search(ds.get("sql") or ""):
                sm.todo = "virtual dataset SQL uses Jinja templating; render it for the migrated model (see queries/)"
        return names

    # ------------------------------------------------------------ charts -> tiles

    def _sql(self, rel: str) -> str:
        return self.store.get_text(rel).strip() if self.store.exists(rel) else ""

    def _tile(self, ch: dict[str, Any], tid: str, sql: str, todos: list[str]) -> Tile:
        viz = str(ch.get("viz_type") or "")
        fd = ch.get("params") or {}
        if isinstance(fd, str):
            fd = json.loads(fd or "{}")
        ds = self.datasets.get(ch.get("dataset_uuid") or "", {})
        cols = {c["column_name"]: c for c in ds.get("columns") or []}
        mets = {m["metric_name"]: m for m in ds.get("metrics") or []}
        qc = _query_context(ch)
        tile = Tile(id=tid, title=ch.get("slice_name") or "", source_type=viz, source_config=fd)
        if not qc:
            todos.append("chart was saved without a query_context (older Superset): open and save it in Superset, then re-run "
                         "extract, or write the SQL from the form data in source_config")
        else:
            ops = sorted({p.get("operation") for q in qc.get("queries") or [] for p in q.get("post_processing") or []} - RESHAPE)
            if ops:
                todos.append(f"Superset post-processes this result in Python ({', '.join(str(o) for o in ops)}); the SQL returns the "
                             "rows before that, so add it in SQL or pandas")
            if JINJA.search(ds.get("sql") or ""):
                todos.append("the dataset's SQL uses Jinja; the compiled SQL has it rendered for the extracting user")
        outputs = [_label(x) for x in _list(fd.get("x_axis")) + _list(fd.get("groupby")) + _list(fd.get("columns"))
                   + _list(fd.get("all_columns")) + _list(fd.get("metrics")) + _list(fd.get("metric"))]
        sql, mapping = _rename(sql.rstrip().rstrip(";").rstrip(), [n for n in dict.fromkeys(outputs) if n])
        tile.sql = sql

        def enc(item: Any, typ: str = "") -> Encoding | None:
            name = _label(item)
            if not name:
                return None
            col = cols.get(name) if isinstance(item, str) else None
            if not typ:
                typ = "temporal" if col and _dim_type(col) in ("timestamp", "date") else "nominal"
            title = (col or mets.get(name) or {}).get("verbose_name") or (name if name in mapping else "")
            return Encoding(mapping.get(name, name), typ, title=title)  # type: ignore[arg-type]

        groupby = _list(fd.get("groupby"))
        if viz in TIMESERIES or viz == "mixed_timeseries":
            tile.mark = TIMESERIES.get(viz, SERIES_TYPES.get(str(fd.get("seriesType") or "line"), "line"))  # type: ignore[assignment]
            x = enc(fd.get("x_axis") or fd.get("granularity_sqla"), "temporal" if fd.get("time_grain_sqla") else "")
            if x and fd.get("time_grain_sqla"):
                x.type = "temporal"
            ys = [e for e in (enc(m, "quantitative") for m in _list(fd.get("metrics"))) if e]
            horizontal = fd.get("orientation") == "horizontal" and tile.mark == "bar"
            xch, ych = ("y", "x") if horizontal else ("x", "y")
            tile.encodings = {k: v for k, v in ((xch, x), (ych, ys[0] if ys else None)) if v}
            if groupby and enc(groupby[0]):
                tile.encodings["color"] = enc(groupby[0], "nominal")  # type: ignore[assignment]
                if tile.mark == "bar" and not fd.get("stack") and not horizontal:
                    tile.encodings["xOffset"] = enc(groupby[0], "nominal")  # type: ignore[assignment]
                if len(groupby) > 1:
                    todos.append(f"grouped by {len(groupby)} columns; Superset joins them into one series name, only the first is the colour")
            if fd.get("stack") == "Expand":
                todos.append("100% stacked in Superset; normalise in SQL or with stack='normalize'")
            if viz == "mixed_timeseries":
                yb = [e for e in (enc(m, "quantitative") for m in _list(fd.get("metrics_b"))) if e]
                tile.encodings.pop(ych, None)
                kind_a = SERIES_TYPES.get(str(fd.get("seriesType") or "line"), "line")
                kind_b = SERIES_TYPES.get(str(fd.get("seriesTypeB") or "line"), "line")
                tile.layers = [Layer(kind_a, {ych: e}) for e in ys] + [Layer(kind_b, {ych: e}) for e in yb]  # type: ignore[arg-type]
                tile.compose = "dual_axis" if fd.get("yAxisIndexB") == 1 else "layer"
                todos.append("mixed chart: Superset runs two queries (query A is the tile SQL, query B is in the -1.sql raw file); "
                             "join them on the x axis")
            elif len(ys) > 1:
                if tile.mark == "bar":
                    todos.append(f"{len(ys)} bar series; only the first is encoded: fold them in SQL and use color/xOffset")
                else:
                    tile.encodings.pop(ych, None)
                    tile.layers = [Layer(tile.mark, {ych: e}) for e in ys]
        elif viz == "pie":
            tile.mark = "arc"
            tile.encodings = {k: v for k, v in (("theta", enc(fd.get("metric"), "quantitative")),
                                                ("color", enc(groupby[0] if groupby else None, "nominal"))) if v}
        elif viz in KPI:
            tile.kind, tile.mark = "kpi", "text"
            e = enc(fd.get("metric"), "quantitative")
            if e:
                tile.encodings = {"text": e}
            if viz == "big_number":
                todos.append("big number with trendline: the SQL returns one row per time grain; show the latest value and add the trend")
        elif viz in TABLES:
            tile.kind, tile.mark = "table", "table"
            if viz == "pivot_table_v2":
                todos.append("pivot table: pivot the result with pandas (pivot_table) to match Superset")
        else:
            tile.kind = "unknown"
            todos.append(f"Superset {viz} chart has no Altair mapping; pick a mark or rebuild")
        if not sql and (qc or self.manual):
            todos.append(NO_SQL_MANUAL if self.manual else "no SQL for this chart; re-run extract or write it")
        tile.todo = "; ".join(dict.fromkeys(todos))
        return tile

    # ------------------------------------------------------------ dashboards

    def _dashboards(self, b: Bundle) -> set[str]:
        on_dash: set[str] = set()
        base = self.instance.get("url", "")
        usage = self.store.get_json("usage.json") if self.store.exists("usage.json") else {}
        rows = {r["uuid"]: r for r in self.lists["dashboards"]}
        for d in self.dashboards:
            row = rows.get(d["uuid"], {})
            title = d.get("dashboard_title") or "Dashboard"
            did = row.get("id")
            u = (usage.get("dashboards") or {}).get(str(did)) if did is not None else None
            dash = Dashboard(name=title, slug=slugify(d.get("slug") or title), description=d.get("description") or "",
                             datasource=b.datasource, source_ref=self._ref("dashboard", d["uuid"]),
                             url=f"{base}{row['url']}" if base and row.get("url") else "",
                             views=u["events"] if u else (0 if usage and did is not None else None))
            by_chart = _filters_by_chart(d, self.charts, self.datasets, self.ids)
            native = (d.get("metadata") or {}).get("native_filter_configuration") or []
            bound: dict[str, str] = {}
            for f in native:
                ftype = f.get("filterType")
                bind = _bind(f.get("name") or f.get("id"))
                fields = []
                for t in f.get("targets") or []:
                    ds = self.datasets.get(t.get("datasetUuid") or "", {})
                    col = (t.get("column") or {}).get("name")
                    if col:
                        fields.append(f"{snake(ds.get('table_name') or '?')}.{col}")
                value = ((f.get("defaultDataMask") or {}).get("filterState") or {}).get("value")
                if isinstance(value, list):
                    value = value[0] if len(value) == 1 else value or None
                kind = {"filter_select": "value", "filter_time": "date_range", "filter_range": "number"}.get(ftype, "unknown")
                dash.filters.append(DashboardFilter(bind, ", ".join(fields), value, kind))  # type: ignore[arg-type]
                bound[f.get("id")] = bind
            pos = d.get("position") or {}
            used: set[str] = set()
            for row_items in _layout(pos):
                ids_row = []
                for node in row_items:
                    meta = node.get("meta") or {}
                    if node["type"] in ("MARKDOWN", "HEADER", "TAB"):
                        text = meta.get("code") or meta.get("text") or ""
                        if node["type"] != "MARKDOWN":
                            text = f"## {text}"
                        tid = _unique(f"{node['type'].lower()}_{snake(node['id'].split('-', 1)[-1]) or 'text'}", used)
                        dash.tiles.append(Tile(tid, kind="text", text=text, source_type=node["type"].lower()))
                        ids_row.append(tid)
                        continue
                    ch_uuid = meta.get("uuid") or next((u for u, i in self.ids.items() if i == meta.get("chartId") and u in self.charts), "")
                    ch = self.charts.get(ch_uuid)
                    if not ch:
                        tid = _unique(f"chart_{meta.get('chartId')}", used)
                        dash.tiles.append(Tile(tid, title=meta.get("sliceName") or "", kind="unknown",
                                               todo="chart is not in the export; re-run extract with this dashboard"))
                        ids_row.append(tid)
                        continue
                    on_dash.add(ch_uuid)
                    tid = _unique(snake(ch.get("slice_name") or f"chart_{meta.get('chartId')}"), used)
                    todos: list[str] = []
                    sql = self._dashboard_sql(d, ch_uuid, by_chart.get(ch_uuid, []), native, bound, todos)
                    tile = self._tile(ch, tid, sql, todos)
                    if meta.get("sliceNameOverride"):
                        tile.title = meta["sliceNameOverride"]
                    dash.tiles.append(tile)
                    ids_row.append(tid)
                if ids_row:
                    dash.layout.append(ids_row)
            b.dashboards.append(dash)
        return on_dash

    def _dashboard_sql(self, d: dict[str, Any], ch_uuid: str, select: list[tuple[dict[str, Any], str]],
                       native: list[dict[str, Any]], bound: dict[str, str], todos: list[str]) -> str:
        """The chart's SQL with the dashboard's select filters as :binds (an empty filter means no filter, as in Superset)."""
        sql = self._sql(f"sql/{d['uuid']}/{ch_uuid}.sql") if select else ""
        if not sql:
            sql = self._sql(f"sql/{ch_uuid}.sql")
            for f, _ in select:
                todos.append(f"dashboard filter {bound[f['id']]} applies to this chart in Superset; add it to the SQL as :{bound[f['id']]}")
        for f, _ in select:
            bind = bound[f["id"]]
            lit = f"'{SENTINEL}{bind}'"
            pat = re.compile(rf"((?:\"[^\"]+\"\.)*(?:\"[^\"]+\"|[\w.]+))\s+IN\s+\(\s*{re.escape(lit)}\s*\)", re.I)
            if pat.search(sql):
                sql = pat.sub(lambda m: f"({m.group(1)} = :{bind} OR :{bind} IS NULL)", sql)
            elif lit in sql:
                todos.append(f"dashboard filter {bind} compiled to an expression unshackle can't bind; replace {lit} in the SQL with :{bind}")
        ch = self.charts[ch_uuid]
        ds = self.datasets.get(ch.get("dataset_uuid") or "", {})
        cid = self.ids.get(ch_uuid)
        for f in native:
            if f.get("filterType") == "filter_select" or not _in_scope(f, cid, d):
                continue
            if f.get("filterType") == "filter_time" and not ds.get("main_dttm_col") and not any(c.get("is_dttm") for c in ds.get("columns") or []):
                continue
            todos.append(f"dashboard {str(f.get('filterType')).replace('filter_', '')} filter {bound[f['id']]} applies to this chart "
                         f"in Superset; add it to the SQL as :{bound[f['id']]}")
        return sql

    # ------------------------------------------------------------ queries

    def _queries(self, b: Bundle, on_dash: set[str], saved: list[dict[str, Any]]) -> None:
        for uuid, ch in sorted(self.charts.items(), key=lambda i: i[1].get("slice_name") or ""):
            if uuid in on_dash:
                continue
            sql = self._sql(f"sql/{uuid}.sql")
            if not sql:
                b.warn(f"chart {ch.get('slice_name')}", f"{NO_SQL_MANUAL}; not migrated" if self.manual
                       else "chart without compiled SQL (no query_context); not migrated")
                continue
            b.queries.append(VerifiedQuery(ch.get("slice_name") or uuid, sql, ch.get("description") or "", datasource=b.datasource,
                                           source_ref=self._ref("chart", uuid)))
        for uuid, ds in sorted(self.datasets.items(), key=lambda i: i[1].get("table_name") or ""):
            sql = (ds.get("sql") or "").strip()
            if not sql:
                continue
            name = self.models[uuid]
            todo = f"Superset virtual dataset: materialize as a SQL model (queries/models/{name}.sql; dbt: models/unshackle/{name}.sql) " \
                   f"so semantic model {name} has a table"
            if JINJA.search(sql):
                todo += "; the SQL uses Jinja templating, render or replace it"
            b.queries.append(VerifiedQuery(ds["table_name"], sql, ds.get("description") or "", datasource=b.datasource,
                                           source_ref=self._ref("dataset", uuid), todo=todo))
        for q in saved:
            sql = (q.get("sql") or "").strip()
            if sql:
                b.queries.append(VerifiedQuery(q.get("label") or q.get("uuid"), sql, q.get("description") or "", datasource=b.datasource,
                                               source_ref=f"superset:saved_query/{q.get('uuid')}",
                                               todo="uses Jinja templating; render or replace it" if JINJA.search(sql) else ""))

    # ------------------------------------------------------------ permissions + row-level security

    def _permissions(self, b: Bundle) -> None:
        s = self.store
        roles = _list(s.get_json("security/roles.json") if s.exists("security/roles.json") else [])
        perms = {p["id"]: p for p in _list(s.get_json("security/permissions.json") if s.exists("security/permissions.json") else [])}
        by_id = {r["id"]: r for r in self.lists["datasets"]}
        uuid_of = {r["id"]: r["uuid"] for r in self.lists["datasets"]}

        def table_of(dsid: int) -> str:
            row = by_id.get(dsid, {})
            ds = self.datasets.get(uuid_of.get(dsid, ""), {})
            if (row.get("sql") or ds.get("sql") or "").strip():
                return self.models.get(uuid_of.get(dsid, ""), snake(row.get("table_name") or str(dsid)))
            return ".".join(x for x in (row.get("schema"), row.get("table_name")) if x) or str(dsid)

        for r in roles:
            if r.get("name") == "Admin":
                continue  # admins can do everything; give them an IdP admin group instead
            principal = "*" if r.get("name") == "Public" else f"group:{r.get('name')}"
            ref = f"superset:role/{r.get('id')}"
            for pid in r.get("permission_ids") or []:
                p = perms.get(pid)
                if not p:
                    continue
                perm, view = p["permission"]["name"], p["view_menu"]["name"]
                src = f"{perm} on {view}"
                todo = "Public role: anonymous users had this access in Superset" if principal == "*" else ""
                if perm in ("all_datasource_access", "all_database_access", "database_access", "catalog_access"):
                    b.permissions.append(Permission(principal, "query", "Datasource", b.datasource, source_expression=src, source_ref=ref, todo=todo))
                elif perm == "schema_access":
                    schema = re.findall(r"\[([^\]]*)\]", view)[-1]
                    for row in self.lists["datasets"]:
                        if row.get("schema") == schema and not (row.get("sql") or "").strip():
                            b.permissions.append(Permission(principal, "query", "Table", table_of(row["id"]), source_expression=src,
                                                            source_ref=ref, todo=todo))
                elif perm == "datasource_access":
                    m = re.search(r"\(id:(\d+)\)$", view)
                    if m and int(m.group(1)) in by_id:
                        b.permissions.append(Permission(principal, "query", "Table", table_of(int(m.group(1))), source_expression=src,
                                                        source_ref=ref, todo=todo))
        unique: dict[tuple[str, ...], Permission] = {}
        for p in b.permissions:  # Alpha has both all_datasource_access and all_database_access
            unique.setdefault((p.principal, p.action, p.resource_type, p.resource_id, p.effect), p)
        b.permissions = list(unique.values())
        slugs = {d["uuid"]: slugify(d.get("slug") or d.get("dashboard_title") or "") for d in self.dashboards}
        for row in self.lists["dashboards"]:
            for role in row.get("roles") or []:
                if row["uuid"] in slugs:
                    b.permissions.append(Permission(f"group:{role.get('name')}", "view", "Notebook", slugs[row["uuid"]],
                                                    source_expression="dashboard role (DASHBOARD_RBAC)",
                                                    source_ref=f"superset:dashboard/{row['id']}"))
        rules = _list(s.get_json("security/rls.json") if s.exists("security/rls.json") else [])
        keys: dict[tuple[str, str], int] = {}
        for rule in rules:
            for t in rule.get("tables") or []:
                if rule.get("group_key"):
                    k = (rule["group_key"], str(t.get("id")))
                    keys[k] = keys.get(k, 0) + 1
        for rule in rules:
            clause = rule.get("clause") or ""
            groups = [r.get("name") for r in rule.get("roles") or [] if r.get("name")]
            for t in rule.get("tables") or []:
                todos = []
                if rule.get("filter_type") == "Base":
                    todos.append(f"Base filter: applies to everyone except roles {groups or '(none)'}; write the exemption")
                if JINJA.search(clause):
                    todos.append("the clause uses Jinja (e.g. current_username()); map it to a user attribute")
                if rule.get("group_key") and keys[(rule["group_key"], str(t.get("id")))] > 1:
                    todos.append(f"Superset ORs this with the other filters in group {rule['group_key']!r} that apply to the user; "
                                 "unshackle ANDs row filters, so combine them into one predicate")
                b.row_filters.append(RowFilter(
                    name=snake(rule.get("name") or f"rls_{rule.get('id')}"), table=table_of(t["id"]) if t.get("id") in by_id else
                    ".".join(x for x in (t.get("schema"), t.get("table_name")) if x),
                    predicate="" if todos else clause, groups=[] if rule.get("filter_type") == "Base" else groups,
                    source_expression=json.dumps({k: rule.get(k) for k in ("filter_type", "clause", "group_key")}),
                    source_ref=f"superset:rls/{rule.get('id')}", todo="; ".join(todos)))


# ------------------------------------------------------------ helpers


def _query_context(ch: dict[str, Any]) -> dict[str, Any] | None:
    qc = ch.get("query_context")
    if isinstance(qc, str):
        try:
            qc = json.loads(qc)
        except ValueError:
            return None
    return qc if isinstance(qc, dict) and qc.get("queries") else None


def _label(item: Any) -> str:
    """The column name Superset gives a metric or column in the result (adhoc ones: their label)."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    if item.get("label"):
        return str(item["label"])
    if item.get("expressionType") == "SIMPLE":
        return f"{item.get('aggregate')}({(item.get('column') or {}).get('column_name')})"
    return str(item.get("sqlExpression") or "")


def _rename(sql: str, outputs: list[str]) -> tuple[str, dict[str, str]]:
    """Wrap the SQL so outputs Altair can't address ("SUM(quantity)", "a.b") get snake_case names.

    `outputs` must be every column the SQL returns (they come from the chart's form data). Returns (sql, {name: alias}).
    """
    if not sql or not any(re.search(r"[^\w ]", n) for n in outputs):
        return sql, {}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    cols = []
    for n in outputs:
        ref = f'"{n}"' if f'"{n}"' in sql or re.search(r"[^\w]", n) else n
        alias = _unique(snake(n) or "col", used) if re.search(r"[^\w ]", n) else n
        if alias != n:
            mapping[n] = alias
        cols.append(ref if alias == n else f"{ref} AS {alias}")
    return "SELECT\n  " + ",\n  ".join(cols) + f"\nFROM (\n{sql}\n) AS q", mapping


def _dim_type(col: dict[str, Any]) -> str:
    t = str(col.get("type") or "").upper()
    if col.get("is_dttm") or "TIMESTAMP" in t or "DATETIME" in t:
        return "date" if t == "DATE" else "timestamp"
    if t.startswith("DATE"):
        return "date"
    if re.match(r"^(SMALLINT|INT|BIGINT|TINYINT|NUMERIC|DECIMAL|FLOAT|DOUBLE|REAL|NUMBER)", t):
        return "number"
    if t.startswith("BOOL"):
        return "boolean"
    return "string"


def _integer(part: str, types: dict[str, str]) -> bool:
    agg, inner = simple_aggregate(part)  # type: ignore[misc]
    if agg in ("count", "count_distinct"):
        return True
    return agg in ("sum", "min", "max") and bool(INT_TYPES.match(types.get(inner.strip('"'), "")))


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr).lower()


def _in_scope(f: dict[str, Any], chart_id: int | None, d: dict[str, Any]) -> bool:
    if chart_id is None:
        return False
    if "chartsInScope" in f:
        return chart_id in (f.get("chartsInScope") or [])
    scope = f.get("scope") or {}
    if chart_id in (scope.get("excluded") or []):
        return False
    pos = d.get("position") or {}
    roots = set(scope.get("rootPath") or ["ROOT_ID"])
    for node in pos.values():
        if isinstance(node, dict) and node.get("type") == "CHART" and (node.get("meta") or {}).get("chartId") == chart_id:
            return "ROOT_ID" in roots or bool(roots & set(node.get("parents") or []))
    return False


def _filters_by_chart(d: dict[str, Any], charts: dict[str, dict[str, Any]], datasets: dict[str, dict[str, Any]],
                      ids: dict[str, int]) -> dict[str, list[tuple[dict[str, Any], str]]]:
    """chart uuid -> [(select filter, column)] for the dashboard's select filters that reach it.

    Superset applies a native filter to the charts in its scope whose dataset has a column of that name.
    """
    out: dict[str, list[tuple[dict[str, Any], str]]] = {}
    native = (d.get("metadata") or {}).get("native_filter_configuration") or []
    for f in native:
        if f.get("filterType") != "filter_select":
            continue
        col = next(((t.get("column") or {}).get("name") for t in f.get("targets") or [] if (t.get("column") or {}).get("name")), "")
        if not col:
            continue
        for uuid, ch in charts.items():
            ds = datasets.get(ch.get("dataset_uuid") or "", {})
            if col in {c.get("column_name") for c in ds.get("columns") or []} and _in_scope(f, ids.get(uuid), d):
                out.setdefault(uuid, []).append((f, col))
    return out


def _layout(pos: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Rows of layout nodes (CHART, MARKDOWN, HEADER, TAB headings), top to bottom, from the position tree."""
    rows: list[list[dict[str, Any]]] = []

    def leaves(nid: str) -> list[dict[str, Any]]:
        node = pos.get(nid) or {}
        if node.get("type") in ("CHART", "MARKDOWN"):
            return [node]
        return [x for c in node.get("children") or [] for x in leaves(c)]

    def walk(nid: str) -> None:
        node = pos.get(nid)
        if not isinstance(node, dict):
            return
        t = node.get("type")
        if t in ("ROW", "COLUMN"):
            items = leaves(nid)
            if items:
                rows.append(items)
        elif t in ("CHART", "MARKDOWN"):
            rows.append([node])
        elif t == "HEADER" and nid != "HEADER_ID":
            rows.append([node])
        else:
            if t == "TAB":
                rows.append([{**node, "meta": {"text": (node.get("meta") or {}).get("text") or ""}}])
            for c in node.get("children") or []:
                walk(c)

    walk("ROOT_ID")
    return rows


def _unique(base: str, used: set[str]) -> str:
    tid, n = base or "tile", 2
    while tid in used:
        tid, n = f"{base}_{n}", n + 1
    used.add(tid)
    return tid
