"""Hex source: projects and cells via the public API, charts/layout from the Git export, semantic models from YAML.

Raw layout under migration/raw/hex/:

    _meta.json                    {"base_url": ...}
    projects.json                 GET /projects (all pages merged; includes analytics.appViews)
    cells/<projectId>.json        GET /cells?projectId=<id> (all pages merged; draft version)
    export/<name>.yaml            copy of HEX_EXPORT_DIR: Git-exported project YAML (chart configs, app layout)
    semantic/**/*.yaml            copy of HEX_SEMANTIC_DIR: `hex context semantic-project pull` output

The public API does not expose chart configuration or the app layout, so charts
are only rebuilt when the project's Git export is present. Its schema is not
documented; parse() reads this shape defensively:

    meta: {projectId, title, description}
    cells:
      - {cellId, cellType: SQL|CODE|MARKDOWN|TEXT|CHART|SINGLE_VALUE|TABLE|INPUT, label,
         config: {source, dataConnectionId, resultVariable,        # SQL / CODE / MARKDOWN
                  dataFrame, chartType, x: {column, type}, y: [{column, aggregate}], series: {column},  # CHART
                  column, aggregate,                                # SINGLE_VALUE
                  inputType, name, defaultValue}}                   # INPUT
    appLayout: {rows: [[cellId, ...] | {columns: [{cellId}]}]}
"""

from __future__ import annotations

import re
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
    Layer,
    Measure,
    Metric,
    SemanticModel,
    Table,
    Tile,
    VerifiedQuery,
    join_todos,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source

JINJA_SIMPLE = re.compile(r"""(['"]?)\{\{\s*([A-Za-z_]\w*)\s*\}\}\1""")
JINJA_ANY = re.compile(r"\{\{|\{%")
BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
IDENT = re.compile(r"^[A-Za-z_]\w*$")
CHART_MARK = {
    "bar": "bar",
    "column": "bar",
    "histogram": "bar",
    "line": "line",
    "area": "area",
    "scatter": "point",
    "point": "point",
    "pie": "arc",
    "donut": "arc",
}
HEX_AGG = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "avg": "average",
    "average": "average",
    "mean": "average",
    "min": "min",
    "max": "max",
    "median": "median",
}
TIME_TYPES = {"date", "timestamp", "timestamp_tz", "timestamp_naive", "datetime", "time"}
VIZ_CELLS = {"CHART", "TABLE", "SINGLE_VALUE", "METRIC", "BIG_NUMBER"}
INPUT_KIND = {"DATE": "date", "DATE_RANGE": "date_range", "NUMBER": "number", "SLIDER": "number"}


class HexSource(Source):
    name = "hex"
    label = "Hex"
    env_vars = ("HEX_API_TOKEN", "HEX_BASE_URL", "HEX_EXPORT_DIR", "HEX_SEMANTIC_DIR")
    docs = "https://learn.hex.tech/docs/api/api-reference"
    manual_files = "project YAML exports (Git export) and `hex context semantic-project pull` YAML, as files or folders"
    manual_gaps = (
        "usage: no app view counts",
        "projects: only the exported projects; no project list, categories or links back to Hex",
        "permissions: project sharing is not in the export",
    )

    def __init__(self, client: Client | None = None, export_dir: str | None = None, semantic_dir: str | None = None):
        self._client = client
        self._export_dir = export_dir
        self._semantic_dir = semantic_dir

    def base_url(self) -> str:
        return env("HEX_BASE_URL", required=False, default="https://app.hex.tech/api/v1").rstrip("/")

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.base_url(), headers={"Authorization": f"Bearer {env('HEX_API_TOKEN')}"}, per_minute=30)
        return self._client

    def _pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        for _ in range(10_000):
            page = self.client().get_json(path, params={**params, **({"after": after} if after else {})})
            out += page.get("values") or []
            after = (page.get("pagination") or {}).get("after")
            if not after:
                break
        return out

    def projects(self) -> list[dict[str, Any]]:
        return self._pages("/projects", {"limit": 100})

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        inv.notes.append("The Hex API needs a Team or Enterprise workspace token and reads the draft version of each project.")
        inv.notes.append(
            "Chart configuration and app layout are not in the API: set HEX_EXPORT_DIR to the project's Git export to rebuild charts."
        )
        for p in self.projects():
            if (p.get("trashedAt") or p.get("archivedAt")) and not p.get("status"):
                continue
            views = ((p.get("analytics") or {}).get("appViews") or {}).get("allTime")
            inv.add(
                "project", p["id"], p.get("title") or p["id"],
                parent=", ".join(c.get("name", "") for c in p.get("categories") or []),
                views=views, last_viewed=str((p.get("analytics") or {}).get("lastViewedAt") or ""),
                url=p.get("url") or "", extra={"status": (p.get("status") or {}).get("name", "") if isinstance(p.get("status"), dict) else p.get("status") or ""},
            )
        sem = self._semantic_dir or env("HEX_SEMANTIC_DIR", required=False)
        if sem:
            for f in sorted(Path(sem).expanduser().rglob("*.y*ml")):
                inv.add("semantic_file", f.name, f.stem)
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        store.put_json("_meta.json", {"base_url": self.base_url()})
        projects = self.projects()
        if only:
            projects = [p for p in projects if p["id"] in only]
        store.put_json("projects.json", projects)
        for p in projects:
            store.put_json(f"cells/{p['id']}.json", self._pages("/cells", {"projectId": p["id"], "limit": 100}))
        for kind, root in (("export", self._export_dir or env("HEX_EXPORT_DIR", required=False)),
                           ("semantic", self._semantic_dir or env("HEX_SEMANTIC_DIR", required=False))):
            if not root:
                continue
            base = Path(root).expanduser()
            for f in sorted(base.rglob("*")):
                if f.is_file() and f.suffix in (".yaml", ".yml") and ".git" not in f.parts:
                    store.put_bytes(f"{kind}/{f.relative_to(base).as_posix()}", f.read_bytes())

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file() and path.suffix not in (".yaml", ".yml"):
            raise SourceError(f"{path.name}: Hex import accepts {self.manual_files}")
        out: dict[str, bytes] = {}
        for rel, p in _manual.files_of(path):
            if p.suffix not in (".yaml", ".yml"):
                continue
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            if "cells" in doc:
                out[f"export/{rel}"] = p.read_bytes()
            elif isinstance(doc.get("models"), list) or (doc.get("id") and ("dimensions" in doc or "measures" in doc)):
                out[f"semantic/{rel}"] = p.read_bytes()
        return out

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool="hex")
        projects = store.get_json("projects.json") if store.exists("projects.json") else []
        exports: dict[str, tuple[str, dict[str, Any]]] = {}
        for rel in store.glob("export/**/*.y*ml"):
            try:
                doc = yaml.safe_load(store.get_text(rel)) or {}
            except yaml.YAMLError as exc:
                b.warn(rel, f"export YAML did not parse: {exc}")
                continue
            meta = doc.get("meta") or {}
            pid = str(meta.get("projectId") or meta.get("id") or Path(rel).stem)
            exports[pid] = (rel, doc)
        seen = set()
        for p in projects:
            pid = p["id"]
            seen.add(pid)
            if pid in exports:
                rel, doc = exports[pid]
                cells = [_export_cell(c) for c in doc.get("cells") or []]
                layout = doc.get("appLayout")
                origin = rel
            elif store.exists(f"cells/{pid}.json"):
                cells = [_api_cell(c) for c in store.get_json(f"cells/{pid}.json")]
                layout = None
                origin = f"cells/{pid}.json"
                if any(c["type"] == "SQL" for c in cells):
                    b.warn(f"project {p.get('title')}", "no Git export; SQL cells become table tiles (set HEX_EXPORT_DIR for charts and layout)", "info")
            else:
                b.warn(f"project {p.get('title')}", "no cells or export in raw; skipped")
                continue
            try:
                _Project(b, p, cells, layout, origin).run()
            except Exception as exc:  # noqa: BLE001 - one bad project must not stop the rest
                b.warn(f"project {p.get('title') or pid}", f"skipped: {exc}")
        for pid, (rel, doc) in exports.items():
            if pid not in seen:
                meta = doc.get("meta") or {}
                p = {"id": pid, "title": meta.get("title") or Path(rel).stem, "description": meta.get("description") or ""}
                _Project(b, p, [_export_cell(c) for c in doc.get("cells") or []], doc.get("appLayout"), rel).run()
        for rel in store.glob("semantic/**/*.y*ml"):
            try:
                _semantic(b, rel, yaml.safe_load(store.get_text(rel)) or {})
            except Exception as exc:  # noqa: BLE001
                b.warn(rel, f"semantic YAML skipped: {exc}")
        return b.settle_time_dimensions()


# ==================================================================== cells


def _api_cell(c: dict[str, Any]) -> dict[str, Any]:
    contents = c.get("contents") or {}
    sql = contents.get("sqlCell") or {}
    code = contents.get("codeCell") or {}
    md = contents.get("markdownCell") or contents.get("textCell") or {}
    body = sql or code or md
    return {
        "id": str(c.get("id") or c.get("cellId")),
        "type": (c.get("cellType") or "").upper(),
        "label": c.get("label") or "",
        "source": body.get("source") or "",
        "connection": sql.get("dataConnectionId"),
        "df": sql.get("resultVariable") or sql.get("dataFrameName") or c.get("resultVariable") or "",
        "config": {},
    }


def _export_cell(c: dict[str, Any]) -> dict[str, Any]:
    cfg = c.get("config") or {}
    return {
        "id": str(c.get("cellId") or c.get("id")),
        "type": (c.get("cellType") or "").upper(),
        "label": c.get("label") or "",
        "source": cfg.get("source") or cfg.get("sql") or "",
        "connection": cfg.get("dataConnectionId"),
        "df": cfg.get("resultVariable") or cfg.get("dataFrameName") or cfg.get("outputDataframe") or "",
        "config": cfg,
    }


def jinja_to_binds(sql: str) -> tuple[str, str]:
    """`{{ region }}` / `'{{ region }}'` -> `:region`. Returns (sql, todo)."""
    out = JINJA_SIMPLE.sub(lambda m: f":{m.group(2)}", sql)
    if JINJA_ANY.search(out):
        return out, "Jinja control flow or filters (e.g. {% if %}, | array, | sqlsafe) left as-is; rewrite with :param binds"
    return out, ""


def _layout_rows(layout: Any) -> list[list[str]]:
    rows = layout.get("rows") if isinstance(layout, dict) else layout
    out: list[list[str]] = []
    for row in rows or []:
        if isinstance(row, dict):
            ids = [str(c.get("cellId") or c.get("id")) for c in row.get("columns") or row.get("cells") or [] if isinstance(c, dict)]
            ids += [str(c) for c in row.get("columns") or [] if isinstance(c, str)]
        elif isinstance(row, list):
            ids = [str(c.get("cellId") if isinstance(c, dict) else c) for c in row]
        else:
            ids = [str(row)]
        if ids:
            out.append(ids)
    return out


class _Project:
    def __init__(self, b: Bundle, p: dict[str, Any], cells: list[dict[str, Any]], layout: Any, origin: str):
        self.b, self.p, self.cells, self.layout, self.origin = b, p, cells, layout, origin
        self.title = p.get("title") or p["id"]
        self.df_sql: dict[str, dict[str, Any]] = {}  # df name -> {sql, todo, cell}
        self.py_dfs: set[str] = set()

    def ref(self, cell: dict[str, Any]) -> str:
        return f"hex project {self.p['id']} cell {cell['id']}"

    def run(self) -> None:
        filters = []
        for c in self.cells:
            if c["type"] == "INPUT":
                cfg = c["config"]
                name = cfg.get("name") or c["label"]
                if name:
                    filters.append(DashboardFilter(snake(name), snake(name), cfg.get("defaultValue"),
                                                   INPUT_KIND.get(str(cfg.get("inputType", "")).upper(), "value")))  # type: ignore[arg-type]
        for c in self.cells:
            if c["type"] == "SQL":
                self._sql(c)
            elif c["type"] == "CODE":
                self._python(c)
        tiles = [t for c in self.cells if (t := self._tile(c))]
        by_id = {t.id: t for t in tiles}
        layout: list[list[str]] = []
        if self.layout:
            rows = _layout_rows(self.layout)
            layout = [[i for i in r if i in by_id] for r in rows]
            layout = [r for r in layout if r]
            shown = {i for r in layout for i in r}
            tiles = [by_id[i] for r in layout for i in r] if shown else tiles
        known = {f.name for f in filters}
        for t in tiles:
            missing = [p for p in dict.fromkeys(BIND.findall(t.sql)) if p not in known]
            for p in missing:
                filters.append(DashboardFilter(p, p, None))
                known.add(p)
            if missing:
                note = f"Hex input(s) {', '.join(missing)} are not in the export; set defaults in the parameters cell"
                t.todo = f"{t.todo}; {note}" if t.todo else note
        text =[self.p.get("description") or ""] + [c["source"] for c in self.cells if c["type"] in ("MARKDOWN", "TEXT")]
        body = "\n\n".join(t.strip() for t in text if t and t.strip())
        if body:
            self.b.knowledge.append(
                KnowledgeItem(f"Hex: {self.title}", body, description=f"Notes from the Hex project {self.title}",
                              datasource=self.b.datasource, source_ref=f"hex project {self.p['id']}")
            )
        if tiles:
            views = ((self.p.get("analytics") or {}).get("appViews") or {}).get("allTime")
            self.b.dashboards.append(
                Dashboard(self.title, slugify(self.title), self.p.get("description") or "", tiles, layout, filters,
                          self.b.datasource, f"hex project {self.p['id']}", self.p.get("url") or "", views)
            )

    def _deps(self, sql: str, own: str) -> list[str]:
        names = [n for n in list(self.df_sql) + sorted(self.py_dfs) if n != own]
        return [n for n in names if re.search(rf"(?i)\b(from|join)\s+{re.escape(n)}\b", sql)]

    def _inline(self, sql: str, own: str) -> tuple[str, list[str]]:
        """Chained dataframe SQL -> CTEs of the upstream SQL cells, dependencies first."""
        todos: list[str] = []
        order: list[str] = []

        def visit(name: str, stack: tuple[str, ...]) -> None:
            if name in order or name in stack:
                return
            if name in self.py_dfs:
                todos.append(f"reads dataframe {name} built by a Python cell; port that cell to SQL first")
                return
            for dep in self._deps(self.df_sql[name]["raw"], name):
                visit(dep, stack + (name,))
            order.append(name)

        for dep in self._deps(sql, own):
            visit(dep, ())
        if not order:
            return sql, todos
        ctes = ",\n".join(f"{n} AS (\n{self.df_sql[n]['raw'].strip().rstrip(';')}\n)" for n in order)
        body = sql.strip().rstrip(";")
        m = re.match(r"(?is)^\s*with\s+(?!recursive\b)", body)
        if m:
            return f"WITH {ctes},\n{body[m.end():]}", todos
        return f"WITH {ctes}\n{body}", todos

    def _sql(self, c: dict[str, Any]) -> None:
        raw, todo = jinja_to_binds(c["source"])
        df = c["df"] or (c["label"] if IDENT.match(c["label"] or "") else "") or f"cell_{snake(c['id'])}"
        sql, todos = self._inline(raw, df)
        todos = [todo] + todos if todo else todos
        if self._deps(raw, df) and not c.get("connection"):
            self.b.warn(self.ref(c), "was Hex dataframe SQL (DuckDB dialect), now inlined as CTEs; check functions against your warehouse", "info")
        self.df_sql[df] = {"raw": raw, "sql": sql, "todo": join_todos(todos), "cell": c}
        self.b.queries.append(
            VerifiedQuery(f"{self.title} - {c['label'] or df}", sql, f"SQL cell from Hex project {self.title}",
                          datasource=self.b.datasource, source_ref=self.ref(c), todo=join_todos(todos))
        )

    def _python(self, c: dict[str, Any]) -> None:
        for m in re.finditer(r"(?m)^([A-Za-z_]\w*)\s*=", c["source"]):
            self.py_dfs.add(m.group(1))
        if not c["source"].strip():
            return
        self.b.queries.append(
            VerifiedQuery(f"{self.title} - {c['label'] or 'python ' + c['id']}", c["source"], f"Python cell from Hex project {self.title}",
                          language="python", datasource=self.b.datasource, source_ref=self.ref(c),
                          todo="Hex Python cell: port to warehouse SQL if it only reshapes data, otherwise keep as a Python step")
        )

    def _source_sql(self, df: str) -> tuple[str, str]:
        if df in self.df_sql:
            return self.df_sql[df]["sql"], self.df_sql[df]["todo"]
        if df in self.py_dfs:
            return "", f"charts dataframe {df} built by a Python cell; port it to SQL"
        return "", f"source dataframe {df!r} not found"

    def _tile(self, c: dict[str, Any]) -> Tile | None:
        t, cfg = c["type"], c["config"]
        if t in ("MARKDOWN", "TEXT"):
            return Tile(c["id"], c["label"], "text", "text", text=c["source"], source_type=t) if c["source"].strip() else None
        if t == "SQL":
            # API-only projects have no chart config: show each SQL result as a table.
            # With a Git export, a SQL cell is a tile only when the app layout shows it.
            has_viz = any(x["type"] in VIZ_CELLS for x in self.cells)
            laid_out = bool(self.layout) and c["id"] in {i for r in _layout_rows(self.layout) for i in r}
            if not (laid_out or (not self.layout and not has_viz)):
                return None
            df = next(k for k, v in self.df_sql.items() if v["cell"] is c)
            sql, todo = self._source_sql(df)
            return Tile(c["id"], c["label"] or df, "table", "table", sql, source_type="SQL", todo=todo)
        if t == "TABLE":
            sql, todo = self._source_sql(cfg.get("dataFrame") or "")
            return Tile(c["id"], c["label"] or cfg.get("title") or "", "table", "table", sql, source_type=t, todo=todo)
        if t in VIZ_CELLS - {"CHART", "TABLE"}:
            sql, todo = self._source_sql(cfg.get("dataFrame") or "")
            col = cfg.get("column") or cfg.get("valueColumn") or ""
            enc = {"text": Encoding(col, "quantitative", _agg(cfg.get("aggregate")))} if col else {}
            return Tile(c["id"], c["label"] or cfg.get("title") or "", "kpi", "text", sql, enc, source_type=t,
                        source_config=cfg, todo=todo)
        if t != "CHART":
            if t not in ("SQL", "CODE", "INPUT"):
                self.b.warn(self.ref(c), f"cell type {t or '?'} is not translated")
            return None
        sql, todo = self._source_sql(cfg.get("dataFrame") or "")
        todos = [todo] if todo else []
        ctype = str(cfg.get("chartType") or cfg.get("type") or "").lower()
        mark = CHART_MARK.get(ctype, "unknown")
        if mark == "unknown":
            todos.append(f"Hex chart type {ctype!r} has no Altair mark mapping here; pick one")
        if ctype == "histogram":
            todos.append("histogram: add binning (alt.X(..., bin=True)) or bin in SQL")
        enc: dict[str, Encoding] = {}
        x = _col(cfg.get("x"))
        ys = cfg.get("y") or []
        ys = ys if isinstance(ys, list) else [ys]
        series = _col(cfg.get("series") or cfg.get("color"))
        if mark == "arc":
            if ys:
                enc["theta"] = Encoding(_col(ys[0]), "quantitative", _agg(ys[0]))
            if x or series:
                enc["color"] = Encoding(x or series, "nominal")
        else:
            if x:
                xt = (cfg.get("x") or {}).get("type") if isinstance(cfg.get("x"), dict) else None
                enc["x"] = Encoding(x, xt if xt in ("temporal", "quantitative", "ordinal", "nominal") else "nominal")
            if ys:
                enc["y"] = Encoding(_col(ys[0]), "quantitative", _agg(ys[0]))
            if series:
                enc["color"] = Encoding(series, "nominal")
            if cfg.get("orientation") == "horizontal" and "x" in enc and "y" in enc:
                enc["x"], enc["y"] = enc["y"], enc["x"]
        layers = []
        if len(ys) > 1 and mark in ("line", "area", "point"):
            # One layer per y series on a shared axis.
            ych = "x" if cfg.get("orientation") == "horizontal" else "y"
            enc.pop(ych, None)
            layers = [Layer(mark, {ych: Encoding(_col(y), "quantitative", _agg(y))}) for y in ys]  # type: ignore[arg-type]
        elif len(ys) > 1:
            todos.append(f"Hex plotted {len(ys)} y series ({', '.join(_col(y) for y in ys)}); only the first is encoded: fold them in SQL (one row per series) and use color/xOffset")
        return Tile(c["id"], c["label"] or cfg.get("title") or "", "chart", mark, sql, enc, source_type=f"hex:{ctype}",  # type: ignore[arg-type]
                    source_config=cfg, todo=join_todos(todos), layers=layers)


def _col(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("column") or v.get("field") or "")
    return str(v or "")


def _agg(v: Any) -> str:
    a = v.get("aggregate") if isinstance(v, dict) else v
    a = str(a or "").lower()
    return {"avg": "mean", "average": "mean", "count_distinct": "distinct", "none": ""}.get(a, a)


# ==================================================================== semantic models


def _semantic(b: Bundle, rel: str, doc: dict[str, Any]) -> None:
    models = doc.get("models") if isinstance(doc.get("models"), list) else [doc] if doc.get("id") else []
    if not models:
        b.warn(rel, "no models in semantic YAML")
        return
    by_id = {m["id"]: m for m in models}
    counts: dict[str, int] = {}
    for m in models:
        for me in m.get("measures") or []:
            counts[snake(me["id"])] = counts.get(snake(me["id"]), 0) + 1
    names = {(m["id"], me["id"]): (f"{snake(m['id'])}_{snake(me['id'])}" if counts[snake(me["id"])] > 1 else snake(me["id"]))
             for m in models for me in m.get("measures") or []}
    sms: dict[str, SemanticModel] = {}
    for m in models:
        sms[m["id"]] = _model(b, rel, m, names)
    for m in models:
        for r in m.get("relations") or []:
            _relation(b, rel, m, r, by_id, sms)


def _dim_expr(m: dict[str, Any], dim_id: str) -> str:
    for d in m.get("dimensions") or []:
        if d["id"] == dim_id:
            return (d.get("expr_sql") or d["id"]).strip()
    return dim_id


def _pk(m: dict[str, Any]) -> dict[str, Any] | None:
    return next((d for d in m.get("dimensions") or [] if d.get("unique") or d.get("primary_key")), None)


def _model(b: Bundle, rel: str, m: dict[str, Any], names: dict[tuple[str, str], str]) -> SemanticModel:
    mid = m["id"]
    src = f"{rel} model: {mid}"
    table = (m.get("base_sql_table") or "").strip()
    if not table:
        table = snake(mid)
        b.queries.append(
            VerifiedQuery(f"{mid} base query", (m.get("base_sql_query") or "-- no base SQL").strip(),
                          f"Base SQL of the Hex semantic model {mid}", datasource=b.datasource, source_ref=src,
                          todo=f"materialize this SQL as a table or dbt model named `{table}` (the semantic model `{snake(mid)}` points at it)")
        )
    sm = SemanticModel(snake(mid), table, m.get("description") or "", datasource=b.datasource, source_ref=src)
    pk = _pk(m)
    if pk:
        expr = _dim_expr(m, pk["id"])
        sm.entities.append(Entity(snake(mid), "primary", "" if expr == snake(mid) else expr))
    else:
        b.warn(src, "no unique dimension; add a primary entity so MetricFlow can join and filter this model")
    tbl = Table(b.datasource, *_split(table), description=m.get("description") or "", source_ref=src) if m.get("base_sql_table") else None
    for d in m.get("dimensions") or []:
        expr = (d.get("expr_sql") or "").strip()
        dtype = str(d.get("type") or "string").lower()
        if tbl is not None and IDENT.match(expr or d["id"]):
            tbl.columns.append(Column(expr or d["id"], dtype, d.get("description") or "", d.get("name") or ""))
        if d is pk:
            continue
        dim = Dimension(snake(d["id"]), "time" if dtype in TIME_TYPES else "categorical",
                        "" if expr in ("", d["id"]) else expr, d.get("description") or "", d.get("name") or "",
                        "day" if dtype in TIME_TYPES else "")
        if dim.name == sm.name:
            b.warn(src, f"dimension {d['id']} clashes with the entity name; renamed {d['id']}_dim")
            dim.name += "_dim"
        sm.dimensions.append(dim)
    for me in m.get("measures") or []:
        _measure(b, src, m, me, sm, names, pk)
    if sm.measures and not any(d.type == "time" for d in sm.dimensions):
        b.warn(src, "has measures but no time dimension; MetricFlow needs defaults.agg_time_dimension", "info")
    b.semantic_models.append(sm)
    if tbl is not None:
        b.tables.append(tbl)
    return sm


def _split(table: str) -> tuple[str, str]:
    parts = [p.strip('"`') for p in table.split(".")]
    return (parts[-2] if len(parts) >= 2 else ""), parts[-1]


def _measure(b, src, m, me, sm: SemanticModel, names, pk) -> None:
    name = names[(m["id"], me["id"])]
    label = me.get("name") or me["id"].replace("_", " ").title()
    metric = Metric(name, label=label, description=me.get("description") or "", semantic_model=sm.name, source_ref=f"{src} measure: {me['id']}")
    own = {x["id"]: names[(m["id"], x["id"])] for x in m.get("measures") or []}

    def todo(msg: str, expr: str) -> None:
        metric.todo, metric.source_expression, metric.source_language = msg, expr, "hex"
        b.metrics.append(metric)

    if me.get("func_calc"):
        calc = me["func_calc"].strip()
        refs = [r for r in re.findall(r"[A-Za-z_]\w*", calc) if r in own]
        unknown = [r for r in re.findall(r"[A-Za-z_]\w*", calc) if r not in own and r.upper() not in ("NULLIF", "COALESCE", "CASE", "WHEN", "THEN", "ELSE", "END")]
        if unknown or not refs:
            return todo(f"func_calc references {', '.join(unknown) or 'no measures'}", calc)
        expr = re.sub(r"[A-Za-z_]\w*", lambda x: own.get(x.group(0), x.group(0)), calc)
        ratio = re.fullmatch(r"(\w+)\s*/\s*(?:NULLIF\s*\(\s*(\w+)\s*,\s*0\s*\)|(\w+))", expr, re.I)
        if ratio:
            metric.type, metric.numerator, metric.denominator = "ratio", ratio.group(1), ratio.group(2) or ratio.group(3)
        else:
            metric.type, metric.expr, metric.metrics = "derived", expr, list(dict.fromkeys(own[r] for r in refs))
        b.metrics.append(metric)
        return
    if me.get("func_sql"):
        fs = me["func_sql"].strip()
        mm = re.fullmatch(r"(?is)(sum|count|avg|min|max|median)\s*\(\s*(distinct\s+)?(.+)\)", fs)
        if not mm or re.search(r"(?i)\b(sum|count|avg|min|max|median)\s*\(", mm.group(3)):
            return todo("func_sql is not a single aggregate; split into simple measures and a derived metric", fs)
        agg = HEX_AGG[mm.group(1).lower()]
        agg = "count_distinct" if mm.group(2) and agg == "count" else agg
        if mm.group(2) and agg != "count_distinct":
            return todo("DISTINCT aggregate other than COUNT", fs)
        sm.measures.append(Measure(name, agg, mm.group(3).strip(), me.get("description") or "", label))
        metric.measure = name
        b.metrics.append(metric)
        return
    func = str(me.get("func") or "count").lower()
    if func not in HEX_AGG and func not in ("percentile",):
        return todo(f"func {func} has no MetricFlow agg", str(me))
    expr = _dim_expr(m, me["of"]) if me.get("of") else (_dim_expr(m, pk["id"]) if pk and func == "count" else "1")
    agg = HEX_AGG.get(func, func)
    if not me.get("of") and func == "count" and not pk:
        agg = "sum"
    measure = Measure(name, agg, expr, me.get("description") or "", label)
    if func == "percentile":
        measure.agg_params = {"percentile": float(me.get("percentile", 0.5)), "use_discrete_percentile": False}
    wheres, dim_filters, bad = [], [], []
    for f in me.get("filters") or []:
        if isinstance(f, str) or (isinstance(f, dict) and f.get("expr_sql")):
            wheres.append(f if isinstance(f, str) else f["expr_sql"])
        elif isinstance(f, dict) and f.get("dimension") and (sql := _dim_filter(sm.name, f)):
            dim_filters.append(sql)
        else:
            bad.append(str(f))
    if wheres:
        pred = " AND ".join(f"({w})" for w in wheres)
        measure.expr = f"CASE WHEN {pred} THEN {expr} END"
        if agg == "sum" and expr == "1":
            measure.expr = f"CASE WHEN {pred} THEN 1 ELSE 0 END"
    metric.filter = " AND ".join(dim_filters)
    sm.measures.append(measure)
    metric.measure = name
    if bad:
        return todo(f"measure filter(s) not translated: {'; '.join(bad)}", str(me.get("filters")))
    b.metrics.append(metric)


def _dim_filter(model: str, f: dict[str, Any]) -> str:
    ref = f"{{{{ Dimension('{model}__{snake(f['dimension'])}') }}}}"
    op = str(f.get("operator") or f.get("op") or "equals").lower()
    vals = f.get("values") if f.get("values") is not None else f.get("value")
    lit = lambda v: str(v).lower() if isinstance(v, bool) else str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"  # noqa: E731
    if op in ("is_null", "is_not_null"):
        return f"{ref} IS {'NOT ' if op == 'is_not_null' else ''}NULL"
    if isinstance(vals, list):
        if op in ("in", "equals", "is_one_of"):
            return f"{ref} IN ({', '.join(lit(v) for v in vals)})"
        if op in ("not_in", "not_equals", "is_not_one_of"):
            return f"{ref} NOT IN ({', '.join(lit(v) for v in vals)})"
        return ""
    sym = {"equals": "=", "eq": "=", "not_equals": "<>", "neq": "<>", "gt": ">", "greater_than": ">", "gte": ">=",
           "lt": "<", "less_than": "<", "lte": "<="}.get(op)
    return f"{ref} {sym} {lit(vals)}" if sym and vals is not None else ""


def _relation(b, rel, m, r, by_id, sms) -> None:
    src = f"{rel} model: {m['id']} relation: {r.get('id')}"
    target = r.get("target") or r.get("target_model")
    if target not in by_id:
        b.warn(src, f"relation target {target!r} not found")
        return
    join = r.get("join_sql") or ""
    norm = re.sub(r"\$\{(\w+)\}", r"\1", join)
    mm = re.fullmatch(r"\s*(?:(\w+)\.)?(\w+)\s*=\s*(?:(\w+)\.)?(\w+)\s*", norm)
    if not mm:
        b.warn(src, f"join_sql {join!r} is not a single equality; add the entity by hand")
        return
    sides = [(mm.group(1) or m["id"], mm.group(2)), (mm.group(3) or target, mm.group(4))]
    rtype = str(r.get("type") or "many_to_one").lower()
    # the side holding the foreign key gets an entity named after the other model
    for (fk_model, fk_col), (pk_model, pk_col) in (sides, sides[::-1]):
        if fk_model not in by_id or pk_model not in by_id or fk_model == pk_model:
            continue
        pk = _pk(by_id[pk_model])
        if not pk or _dim_expr(by_id[pk_model], pk["id"]) != pk_col:
            continue
        sm = sms[fk_model]
        ename = snake(pk_model)
        if any(e.name == ename for e in sm.entities):
            return
        etype = "unique" if rtype == "one_to_one" else "foreign"
        sm.entities.append(Entity(ename, etype, fk_col))
        return
    b.warn(src, f"neither side of {join!r} is the other model's unique key; add entities by hand")
