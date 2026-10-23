"""Looker source: LookML (views, explores, access rules) + dashboards and Looks via API 4.0.

Raw layout under migration/raw/looker/:

    _meta.json                          {"base_url": ...} (for dashboard links)
    lookml/**/*.lkml                    copy of LOOKER_LOOKML_DIR (the LookML Git repo), untouched
    api/lookml_models.json              GET /lookml_models
    api/explores/<model>/<explore>.json GET /lookml_models/{m}/explores/{e}  (only when no LookML dir)
    dashboards/<id>.json                GET /dashboards/{id}  (elements with `query`, layouts, filters)
    looks/<id>.json                     GET /looks/{id}
    queries/<query_id>.sql              GET /queries/{id}/run/sql  (generated SQL, not executed)
    content_usage.json                  GET /content_view/search  (view_count per dashboard / look)
    _skipped.json                       content the API user could not read (404/403)

parse() prefers raw LookML. Without it, it rebuilds views from the explore JSON
(field `sql` is only present when the API user has `see_lookml`).
"""

from __future__ import annotations

import json
import posixpath
import re
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

REF = re.compile(r"\$\{([^}]+)\}")
LIQUID = re.compile(r"\{%|\{\{")
CONSTANT = re.compile(r"@\{(\w+)\}")
_IDENT = r"""(?:[\w$-]+|`[^`(){};']+`|"[^"(){};']+"|\[[^\[\](){};']+\])"""
TABLE_NAME = re.compile(rf"\s*{_IDENT}(?:\s*\.\s*{_IDENT})*\s*")  # a.b.c, `p-1.d.t`, "My Schema"."T"
USER_ATTR = re.compile(r"\{\{\s*_user_attributes\[['\"](\w+)['\"]\]\s*\}\}")
SIMPLE_IDENT = re.compile(r"^[A-Za-z_][\w]*$")
AGG_FN = re.compile(r"\b(sum|count|avg|min|max|median|percentile_cont|approx_\w+)\s*\(", re.I)

MEASURE_AGG = {
    "sum": "sum",
    "average": "average",
    "count": "count",
    "count_distinct": "count_distinct",
    "min": "min",
    "max": "max",
    "median": "median",
    "percentile": "percentile",
}
# smallest first -> MetricFlow granularity
TIMEFRAME_GRAIN = [
    ("time", "second"),
    ("second", "second"),
    ("minute", "minute"),
    ("hour", "hour"),
    ("date", "day"),
    ("week", "week"),
    ("month", "month"),
    ("quarter", "quarter"),
    ("year", "year"),
]
TF_GRAIN = dict(TIMEFRAME_GRAIN)
VIS_MARK = {
    "looker_column": ("chart", "bar"),
    "looker_bar": ("chart", "bar"),
    "looker_line": ("chart", "line"),
    "looker_area": ("chart", "area"),
    "looker_scatter": ("chart", "point"),
    "looker_pie": ("chart", "arc"),
    "looker_donut_multiples": ("chart", "arc"),
    "single_value": ("kpi", "text"),
    "looker_single_record": ("kpi", "text"),
    "looker_grid": ("table", "table"),
    "table": ("table", "table"),
    "looker_table": ("table", "table"),
}
COLUMN_TYPE = {"number": "number", "string": "string", "yesno": "boolean", "zipcode": "string", "tier": "string"}


class LookerSource(Source):
    name = "looker"
    label = "Looker"
    env_vars = ("LOOKER_BASE_URL", "LOOKER_CLIENT_ID", "LOOKER_CLIENT_SECRET", "LOOKER_LOOKML_DIR")
    docs = "https://cloud.google.com/looker/docs/reference/looker-api/latest"
    manual_files = "the LookML project folder (a Git checkout, or its .zip download) or .lkml files"
    manual_gaps = (
        "dashboards and Looks: user-defined dashboards and Looks only come from the API; LookML dashboards (.lookml) are not parsed",
        "usage: no view counts",
        "generated SQL: no Looker-compiled SQL for saved queries",
        "permissions: access grants in the LookML are kept, but which users and groups hold the user attributes is not",
    )

    def __init__(self, client: Client | None = None, lookml_dir: str | None = None, base_url: str | None = None):
        self._client = client
        self._logged_in = False
        self._lookml_dir = lookml_dir
        self._base_url = base_url

    # ---------------------------------------------------------------- network

    def base_url(self) -> str:
        return (self._base_url or env("LOOKER_BASE_URL")).rstrip("/")

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(f"{self.base_url()}/api/4.0", per_minute=120)
        if not self._logged_in:
            token = self._client.post(
                "/login", data={"client_id": env("LOOKER_CLIENT_ID"), "client_secret": env("LOOKER_CLIENT_SECRET")}
            ).json()["access_token"]
            self._client.set_header("Authorization", f"token {token}")
            self._logged_in = True
        return self._client

    def _try_json(self, path: str, skipped: list[str], **kw: Any) -> Any:
        try:
            return self.client().get_json(path, **kw)
        except SourceError as exc:
            if " -> 404" in str(exc) or " -> 403" in str(exc):
                skipped.append(path)
                return None
            raise

    def _usage(self, skipped: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset, limit = 0, 1000
        while True:
            page = self._try_json(
                "/content_view/search",
                skipped,
                params={"fields": "dashboard_id,look_id,view_count,last_viewed_at", "limit": limit, "offset": offset},
            )
            if not page:
                break
            rows += page
            if len(page) < limit:
                break
            offset += limit
        return rows

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        inv.notes.append(
            "Looker returns 404 for content the API user cannot see, so a non-admin export is silently partial. "
            "Use an admin API key (or one with see_lookml + access to every folder) for a complete migration."
        )
        skipped: list[str] = []
        usage = _usage_by_content(self._usage(skipped))
        if skipped:
            inv.notes.append("Usage (content_view) not readable: needs admin or see_system_activity.")
        base = self.base_url()
        for model in self.client().get_json("/lookml_models", params={"fields": "name,label,explores"}) or []:
            for ex in model.get("explores") or []:
                inv.add("explore", f"explore:{model['name']}/{ex['name']}", ex.get("label") or ex["name"], parent=model["name"])
        for d in self.client().get_json("/dashboards", params={"fields": "id,title,folder"}) or []:
            u = usage.get(("dashboard", str(d["id"])), {})
            inv.add(
                "dashboard", f"dashboard:{d['id']}", d.get("title") or str(d["id"]),
                parent=((d.get("folder") or {}).get("name") or ""),
                views=u.get("views"), last_viewed=u.get("last_viewed", ""), url=f"{base}/dashboards/{d['id']}",
            )
        for lk in self.client().get_json("/looks", params={"fields": "id,title,folder"}) or []:
            u = usage.get(("look", str(lk["id"])), {})
            inv.add(
                "look", f"look:{lk['id']}", lk.get("title") or str(lk["id"]),
                parent=((lk.get("folder") or {}).get("name") or ""),
                views=u.get("views"), last_viewed=u.get("last_viewed", ""), url=f"{base}/looks/{lk['id']}",
            )
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        want = _only(only)
        skipped: list[str] = []
        store.put_json("_meta.json", {"base_url": self.base_url()})
        lookml_dir = self._lookml_dir or env("LOOKER_LOOKML_DIR", required=False)
        models = self.client().get_json("/lookml_models") or []
        store.put_json("api/lookml_models.json", models)
        if lookml_dir:
            root = Path(lookml_dir).expanduser()
            for p in sorted(root.rglob("*")):
                if p.is_file() and (p.suffix == ".lkml" or p.name.endswith(".lookml")) and ".git" not in p.parts:
                    store.put_bytes(f"lookml/{p.relative_to(root).as_posix()}", p.read_bytes())
        else:
            for m in models:
                for ex in m.get("explores") or []:
                    if want and f"explore:{m['name']}/{ex['name']}" not in want and "explore" in want.get("_kinds", set()):
                        continue
                    data = self._try_json(f"/lookml_models/{m['name']}/explores/{ex['name']}", skipped)
                    if data is not None:
                        store.put_json(f"api/explores/{m['name']}/{ex['name']}.json", data)
        query_ids: set[str] = set()
        for d in self.client().get_json("/dashboards", params={"fields": "id,title"}) or []:
            if not _wanted(want, "dashboard", d["id"]):
                continue
            full = self._try_json(f"/dashboards/{d['id']}", skipped)
            if full is None:
                continue
            store.put_json(f"dashboards/{d['id']}.json", full)
            for el in full.get("dashboard_elements") or []:
                qid = _element_query_id(el)
                if qid:
                    query_ids.add(qid)
        for lk in self.client().get_json("/looks", params={"fields": "id,title"}) or []:
            if not _wanted(want, "look", lk["id"]):
                continue
            full = self._try_json(f"/looks/{lk['id']}", skipped)
            if full is None:
                continue
            store.put_json(f"looks/{lk['id']}.json", full)
            qid = str(full.get("query_id") or (full.get("query") or {}).get("id") or "")
            if qid:
                query_ids.add(qid)
        for qid in sorted(query_ids):
            try:
                store.put_text(f"queries/{qid}.sql", self.client().get(f"/queries/{qid}/run/sql").text)
            except SourceError as exc:
                skipped.append(f"/queries/{qid}/run/sql ({str(exc)[:80]})")
        usage = self._usage(skipped)
        if usage:
            store.put_json("content_usage.json", usage)
        store.put_json("_skipped.json", sorted(set(skipped)))

    # ---------------------------------------------------------------- parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file() and not path.name.endswith((".lkml", ".lookml")):
            raise SourceError(f"{path.name}: Looker import accepts {self.manual_files}")
        return {f"lookml/{rel}": p.read_bytes() for rel, p in _manual.files_of(path) if p.name.endswith((".lkml", ".lookml"))}

    def parse(self, store: RawStore) -> Bundle:
        return _Parser(store).run()


# ==================================================================== helpers


def _only(only: set[str] | None) -> dict[str, Any] | None:
    if not only:
        return None
    out: dict[str, Any] = {"_ids": set(), "_kinds": set()}
    for item in only:
        kind, sep, rest = item.partition(":")
        if sep:
            out["_ids"].add(f"{kind}:{rest}")
            out["_kinds"].add(kind)
        else:
            out["_ids"].add(f"dashboard:{item}")
            out["_ids"].add(f"look:{item}")
            out["_kinds"].update({"dashboard", "look"})
    return out


def _wanted(want: dict[str, Any] | None, kind: str, id: Any) -> bool:
    return want is None or f"{kind}:{id}" in want["_ids"]


def _element_query_id(el: dict[str, Any]) -> str:
    q = el.get("query_id") or (el.get("query") or {}).get("id")
    if not q and el.get("look"):
        q = el["look"].get("query_id") or (el["look"].get("query") or {}).get("id")
    return str(q) if q else ""


def _element_query(el: dict[str, Any]) -> dict[str, Any]:
    if el.get("query"):
        return el["query"]
    if el.get("look") and el["look"].get("query"):
        return el["look"]["query"]
    return {}


def _usage_by_content(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows or []:
        for kind in ("dashboard", "look"):
            cid = r.get(f"{kind}_id")
            if cid in (None, ""):
                continue
            cur = out.setdefault((kind, str(cid)), {"views": 0, "last_viewed": ""})
            cur["views"] += int(r.get("view_count") or 0)
            cur["last_viewed"] = max(cur["last_viewed"], str(r.get("last_viewed_at") or ""))
    return out


def _constants(docs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """`constant:` values from manifest.lkml, with constants that use other constants expanded."""
    out: dict[str, str] = {}
    for path in sorted(docs, key=lambda p: (p.count("/"), p)):
        if Path(path).name == "manifest.lkml":
            for c in docs[path].get("constants", []):
                out.setdefault(c["name"], str(c.get("value", "")))
    for _ in range(5):
        out = {k: CONSTANT.sub(lambda m: out.get(m.group(1), m.group(0)), v) for k, v in out.items()}
    return out


def _substitute(x: Any, constants: dict[str, str]) -> Any:
    """Replace `@{name}` in every string of a parsed LookML file, as Looker does before evaluating."""
    if isinstance(x, str):
        return CONSTANT.sub(lambda m: constants.get(m.group(1), m.group(0)), x) if "@{" in x else x
    if isinstance(x, list):
        return [_substitute(y, constants) for y in x]
    if isinstance(x, dict):
        return {k: _substitute(v, constants) for k, v in x.items()}
    return x


def _include_regex(pattern: str, from_path: str) -> re.Pattern | None:
    """An `include:` pattern as a regex over project paths. `*` stays in one folder, `**` crosses
    folders, a missing `.lkml` suffix is implied, and paths without a leading `/` are relative."""
    pattern = pattern.strip()
    if not pattern or pattern.startswith("//"):  # another project's files
        return None
    path = pattern[1:] if pattern.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(from_path), pattern))
    out = ""
    for part in re.split(r"(\*\*/|\*\*|\*|\?)", path):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + ("" if path.endswith((".lkml", ".lookml")) else r"(?:\.lkml|\.lookml)?"))


def _include_closure(model_path: str, docs: dict[str, dict[str, Any]]) -> list[str]:
    """Every file a model includes, directly or through included files, in include order."""
    out: list[str] = []
    todo = [model_path]
    while todo:
        path = todo.pop(0)
        for pattern in docs[path].get("includes", []):
            rx = _include_regex(pattern, path)
            for other in sorted(docs) if rx else []:
                if rx.fullmatch(other) and other != model_path and other not in out:
                    out.append(other)
                    todo.append(other)
    return out


def _plain_table(sql_table_name: str) -> str:
    """The table name, or "" when sql_table_name is SQL (a subquery, Liquid, an undefined constant)."""
    return _strip_ident(sql_table_name) if TABLE_NAME.fullmatch(sql_table_name or "") else ""


def _source(field: dict[str, Any]) -> str:
    """What a person needs to translate a field by hand: its SQL, else its definition (case:, sql_latitude, ...)."""
    if field.get("sql"):
        return field["sql"]
    skip = ("name", "label", "description", "hidden", "view_label", "group_label", "drill_fields", "link", "html", "tags")
    return json.dumps({k: v for k, v in field.items() if k not in skip}, default=str)


def _label(field: dict[str, Any]) -> str:
    """A field's label; "" for a Liquid label (it depends on the user or the query, so the name is used)."""
    label = str(field.get("label") or "")
    return "" if LIQUID.search(label) or CONSTANT.search(label) else label


def _yes(v: Any) -> bool:
    return str(v).lower() in ("yes", "true")


def _strip_ident(name: str) -> str:
    return ".".join(p.strip().strip('"`[]') for p in name.strip().split("."))


def _split_table(fq: str) -> tuple[str, str]:
    parts = _strip_ident(fq).split(".")
    return (parts[-2] if len(parts) >= 2 else ""), parts[-1]


def _paren(expr: str) -> str:
    return expr if SIMPLE_IDENT.match(expr) or re.match(r"^[\w.]+$", expr) else f"({expr})"


def _filter_pairs(measure: dict[str, Any]) -> list[tuple[str, str]]:
    """lkml gives filters as [[{f: v}, ...]] (new syntax) or [{field, value}] (old)."""
    raw = measure.get("filters__all") or measure.get("filters") or []
    out: list[tuple[str, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if "field" in x and "value" in x:
                out.append((x["field"], str(x["value"])))
            else:
                out.extend((k, str(v)) for k, v in x.items())

    walk(raw)
    return out


def looker_filter_sql(ref: str, value: str, kind: str = "string") -> str | None:
    """Translate a Looker filter expression to SQL on `ref`. None when it needs a person."""
    v = value.strip()
    if kind == "yesno":
        if v.lower() in ("yes", "no"):
            return f"{ref} = {'true' if v.lower() == 'yes' else 'false'}"
        return None
    if v.upper() == "NULL":
        return f"{ref} IS NULL"
    if v.upper() in ("-NULL", "NOT NULL"):
        return f"{ref} IS NOT NULL"
    if v.upper() == "EMPTY":
        return f"({ref} IS NULL OR {ref} = '')"
    if v.upper() == "-EMPTY":
        return f"({ref} IS NOT NULL AND {ref} <> '')"
    m = re.fullmatch(r"(>=|<=|<>|!=|>|<|=)?\s*(-?\d+(?:\.\d+)?)", v)
    if m and (kind == "number" or m.group(1)):
        return f"{ref} {m.group(1) or '='} {m.group(2)}"
    # dates, number ranges and anything that reads like a relative date need a person; brackets
    # are literal in a string filter ("(5) Purchase")
    if kind in ("time", "number") or re.search(r"\b(ago|days?|weeks?|months?|years?|before|after|to|last|this|next)\b", v, re.I):
        return None
    neg = v.startswith("-")
    parts = [p.strip().replace("^,", ",") for p in re.split(r"(?<!\^),", v)]
    parts = [p[1:] if p.startswith("-") else p for p in parts] if neg else parts
    if any(p.startswith("-") for p in parts) or not all(parts):
        return None
    lit = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
    if any("%" in p for p in parts):
        if len(parts) > 1:
            return None
        return f"{ref} {'NOT LIKE' if neg else 'LIKE'} {lit(parts[0])}"
    if len(parts) == 1:
        return f"{ref} {'<>' if neg else '='} {lit(parts[0])}"
    return f"{ref} {'NOT IN' if neg else 'IN'} ({', '.join(lit(p) for p in parts)})"


# ==================================================================== parser


class _Parser:
    def __init__(self, store: RawStore):
        self.store = store
        self.b = Bundle(tool="looker")
        self.views: dict[str, dict[str, Any]] = {}
        self.view_file: dict[str, str] = {}
        self.models: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, tuple[str, str, str]] = {}  # "view.field" -> (kind, group/field, timeframe)
        self.metric_names: dict[tuple[str, str], str] = {}
        self.meta: dict[str, Any] = store.get_json("_meta.json") if store.exists("_meta.json") else {}

    def run(self) -> Bundle:
        if self.store.glob("lookml/**/*.lkml"):
            self._load_lookml()
        elif self.store.glob("api/explores/**/*.json"):
            self.b.warn("lookml", "no raw LookML; rebuilt views from explore API JSON (set LOOKER_LOOKML_DIR for full fidelity)", "info")
            self._load_explores_json()
        else:
            self.b.warn("lookml", "no LookML or explore JSON in raw; only dashboards and looks were parsed", "warn")
        self._index_fields()
        self._assign_metric_names()
        for vname, view in self.views.items():
            self._view(vname, view)
        for mname, model in self.models.items():
            self._model(mname, model)
        # an explore file included by several models yields the same policies once per model
        self.b.row_filters = list({(r.name, r.table, r.predicate, r.todo): r for r in self.b.row_filters}.values())
        self.b.permissions = list({(p.principal, p.action, p.resource_type, p.resource_id, p.todo): p for p in self.b.permissions}.values())
        usage = _usage_by_content(self.store.get_json("content_usage.json")) if self.store.exists("content_usage.json") else {}
        for rel in self.store.glob("dashboards/*.json"):
            try:
                self._dashboard(rel, self.store.get_json(rel), usage)
            except Exception as exc:  # noqa: BLE001 - one bad dashboard must not stop the rest
                self.b.warn(rel, f"skipped: {exc}")
        for rel in self.store.glob("looks/*.json"):
            try:
                self._look(rel, self.store.get_json(rel))
            except Exception as exc:  # noqa: BLE001
                self.b.warn(rel, f"skipped: {exc}")
        if self.store.exists("_skipped.json"):
            for path in self.store.get_json("_skipped.json"):
                self.b.warn(path, "not readable by the API user during extract (404/403); export is partial")
        return self.b.settle_time_dimensions()

    # ------------------------------------------------------------ loading

    def _load_lookml(self) -> None:
        try:
            import lkml
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise SourceError("parsing LookML needs the `looker` extra: uv pip install 'unshackle[looker]'") from exc
        docs: dict[str, dict[str, Any]] = {}  # path inside the project -> parsed file
        for rel in self.store.glob("lookml/**/*.lkml"):
            try:
                docs[rel.removeprefix("lookml/")] = lkml.load(self.store.get_text(rel))
            except Exception as exc:  # noqa: BLE001
                self.b.warn(rel, f"LookML did not parse: {exc}")
        constants = _constants(docs)
        if constants:
            docs = {path: _substitute(doc, constants) for path, doc in docs.items()}
        closures = {p: _include_closure(p, docs) for p, d in sorted(docs.items()) if p.endswith(".model.lkml") or d.get("connection")}
        used = set(closures).union(*closures.values())
        refinements: list[tuple[str, dict[str, Any]]] = []
        for path, doc in docs.items():
            for v in doc.get("views", []):
                if v["name"].startswith("+"):
                    refinements.append((path, v))
                    continue
                other = self.view_file.get(v["name"])
                if other:
                    keep = other if other in used and path not in used else path
                    self.b.warn(f"view {v['name']}", f"defined in both {other} and {path}; using {keep} (the one a model includes, else the last)")
                    if keep == other:
                        continue
                self.views[v["name"]] = v
                self.view_file[v["name"]] = path
            for g in doc.get("access_grants", []):
                self.grants[g["name"]] = g
        self._load_models(docs, closures)
        for rel in self.store.glob("lookml/**/*.lookml"):
            self.b.warn(rel, "LookML dashboards are not parsed; the same dashboard is exported through the API if it is in a user folder")
        # Looker applies refinements to the view first, then resolves extends, so a view that
        # extends a refined view gets the refinement, and a refinement may add `extends`.
        for path, ref in refinements:
            base = ref["name"][1:]
            if base not in self.views:
                self.b.warn(f"lookml/{path}", f"refinement +{base} has no base view")
                continue
            self.views[base] = _merge_view(self.views[base], ref, refine=True)
        for name, v in list(self.views.items()):
            if v.get("extends") or v.get("extends__all"):
                self.views[name] = self._extend(v, {name}, self.views, "view")

    def _load_models(self, docs: dict[str, dict[str, Any]], closures: dict[str, list[str]]) -> None:
        """Models and the explores they include (explores often live in their own files)."""
        included = set().union(*closures.values())
        explores: dict[str, list[dict[str, Any]]] = {}  # model file -> explores, in include order
        for mpath, paths in closures.items():
            explores[mpath] = list(docs[mpath].get("explores", []))
            for path in paths:
                if path not in closures:
                    explores[mpath] += docs[path].get("explores", [])
        for path, doc in docs.items():
            if path not in closures and doc.get("explores") and path not in included:
                explores[path] = list(doc["explores"])  # explores no model includes: keep them under their file
        for path, exs in explores.items():
            name = Path(path).name.removesuffix(".lkml").removesuffix(".model")
            model = self.models.setdefault(name, {"explores": [], "file": path})
            doc = docs[path]
            model["connection"] = doc.get("connection", model.get("connection", ""))
            model["label"] = doc.get("label", model.get("label", ""))
            model["explores"] += self._resolve_explores(path, exs)

    def _resolve_explores(self, path: str, explores: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply explore refinements (`explore: +x`) and `extends`; drop `extension: required` explores."""
        by_name: dict[str, dict[str, Any]] = {}
        refinements = []
        for ex in explores:
            if ex["name"].startswith("+"):
                refinements.append(ex)
            else:
                by_name[ex["name"]] = ex
        for ref in refinements:
            base = ref["name"][1:]
            if base not in by_name:
                self.b.warn(f"lookml/{path}", f"explore refinement +{base} has no base explore")
                continue
            by_name[base] = _merge_view(by_name[base], ref, refine=True)
        for name, ex in list(by_name.items()):
            if ex.get("extends") or ex.get("extends__all"):
                by_name[name] = self._extend(ex, {name}, by_name, "explore")
        return [ex for ex in by_name.values() if ex.get("extension") != "required"]

    def _extend(self, obj: dict[str, Any], seen: set[str], pool: dict[str, dict[str, Any]], kind: str) -> dict[str, Any]:
        parents = obj.get("extends") or [p for group in obj.get("extends__all", []) for p in group]
        out: dict[str, Any] = {}
        for p in parents:
            if p in seen or p not in pool:
                self.b.warn(f"{kind} {obj['name']}", f"extends {p} which was not found")
                continue
            out = _merge_view(out, self._extend(pool[p], seen | {p}, pool, kind))
        merged = _merge_view(out, obj)
        merged["name"] = obj["name"]
        if obj.get("extension"):
            merged["extension"] = obj["extension"]
        return merged

    def _load_explores_json(self) -> None:
        for rel in self.store.glob("api/explores/**/*.json"):
            ex = self.store.get_json(rel)
            model = self.models.setdefault(ex.get("model_name") or Path(rel).parent.name, {"explores": [], "file": rel})
            model["connection"] = ex.get("connection_name", "")
            base = ex.get("view_name") or ex["name"]
            tables = {base: ex.get("sql_table_name") or ""}
            joins = []
            for j in ex.get("joins") or []:
                tables[j.get("from") or j["name"]] = j.get("sql_table_name") or ""
                joins.append({k: j.get(k) for k in ("name", "from", "sql_on", "relationship", "foreign_key") if j.get(k)})
            explore = {
                "name": ex["name"],
                "label": ex.get("label", ""),
                "description": ex.get("description", ""),
                "joins": joins,
                "access_filters": ex.get("access_filters") or [],
                "sql_always_where": ex.get("sql_always_where") or "",
            }
            if base != ex["name"]:
                explore["from"] = base
            model["explores"].append(explore)
            fields = ex.get("fields") or {}
            for kind in ("dimensions", "measures"):
                for f in fields.get(kind) or []:
                    vname = f.get("view") or f["name"].split(".")[0]
                    view = self.views.setdefault(vname, {"name": vname, "dimensions": [], "dimension_groups": [], "measures": []})
                    if tables.get(vname) and not view.get("sql_table_name"):
                        view["sql_table_name"] = tables[vname]
                    self.view_file.setdefault(vname, rel)
                    short = f["name"].split(".", 1)[-1]
                    item: dict[str, Any] = {"name": short}
                    for k in ("sql", "description", "label_short", "hidden", "primary_key"):
                        if f.get(k) not in (None, "", False):
                            item["label" if k == "label_short" else k] = "yes" if f[k] is True else f[k]
                    if kind == "measures":
                        item["type"] = f.get("type") or "number"
                        if any(m["name"] == short for m in view["measures"]):
                            continue
                        view["measures"].append(item)
                    elif f.get("dimension_group"):
                        gname = f["dimension_group"].split(".", 1)[-1]
                        group = next((g for g in view["dimension_groups"] if g["name"] == gname), None)
                        if group is None:
                            group = {"name": gname, "type": "time", "timeframes": [], "sql": f.get("sql", "")}
                            view["dimension_groups"].append(group)
                        tf = short.removeprefix(gname + "_")
                        if tf not in group["timeframes"]:
                            group["timeframes"].append(tf)
                    else:
                        item["type"] = f.get("type") or "string"
                        if not any(d["name"] == short for d in view["dimensions"]):
                            view["dimensions"].append(item)

    # ------------------------------------------------------------ indexing

    def _index_fields(self) -> None:
        for vname, v in self.views.items():
            for d in v.get("dimensions", []):
                self.fields[f"{vname}.{d['name']}"] = ("dimension", d["name"], "")
            for g in v.get("dimension_groups", []):
                tfs = g.get("timeframes") or ["raw", "time", "date", "week", "month", "quarter", "year"]
                for tf in tfs:
                    self.fields[f"{vname}.{g['name']}_{tf}"] = ("time", g["name"], tf)
            for m in v.get("measures", []):
                self.fields[f"{vname}.{m['name']}"] = ("measure", m["name"], "")

    def _assign_metric_names(self) -> None:
        counts: dict[str, int] = {}
        for v in self.views.values():
            for m in v.get("measures", []):
                counts[snake(m["name"])] = counts.get(snake(m["name"]), 0) + 1
        for vname, v in self.views.items():
            for m in v.get("measures", []):
                n = snake(m["name"])
                if counts[n] > 1 or n == "count":
                    n = f"{snake(vname)}_{n}"
                self.metric_names[(vname, m["name"])] = n
        # a qualified name can meet a measure already called that (`count` next to `orders_count`)
        taken = {n for (_, m), n in self.metric_names.items() if n == snake(m)}
        for key, n in self.metric_names.items():
            if n != snake(key[1]) and n in taken:
                i = 2
                while f"{n}_{i}" in taken:
                    i += 1
                self.b.warn(f"view {key[0]} measure {key[1]}", f"named {n}_{i}: {n} is another measure's name", "info")
                n = self.metric_names[key] = f"{n}_{i}"
            taken.add(n)

    def _field(self, vname: str, name: str) -> dict[str, Any] | None:
        v = self.views.get(vname) or {}
        for kind in ("dimensions", "measures", "dimension_groups"):
            for f in v.get(kind, []):
                if f["name"] == name:
                    return f
        return None

    def _pk(self, vname: str) -> dict[str, Any] | None:
        for d in (self.views.get(vname) or {}).get("dimensions", []):
            if _yes(d.get("primary_key")):
                return d
        return None

    def table_name(self, vname: str) -> str:
        return _plain_table((self.views.get(vname) or {}).get("sql_table_name", "") or "")

    def model_table(self, vname: str) -> str:
        """The table the view's semantic model reads: its table, or the view name for SQL-backed views."""
        v = self.views.get(vname) or {}
        return self.table_name(vname) or (snake(vname) if v.get("derived_table") or v.get("sql_table_name") else "")

    # ------------------------------------------------------------ SQL resolution

    def resolve(self, vname: str, sql: str, seen: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
        """Turn LookML dimension SQL into a column expression on the view's table."""
        problems: list[str] = []
        if LIQUID.search(sql or ""):
            return sql, ["uses Liquid templating"]
        if CONSTANT.search(sql or ""):
            return sql, [f"uses constant {CONSTANT.search(sql).group(0)} that manifest.lkml does not define"]

        def sub(m: re.Match) -> str:
            ref = m.group(1).strip()
            if ref == "TABLE":
                return "\x00TABLE\x00"
            view, _, name = ref.rpartition(".")
            view = view or vname
            if name == "SQL_TABLE_NAME":
                t = self.table_name(view)
                if not t:
                    problems.append(f"references derived table {view}.SQL_TABLE_NAME")
                    return snake(view)
                return t
            if view != vname:
                problems.append(f"references another view ({ref})")
                return m.group(0)
            if ref in seen:
                problems.append(f"circular reference {ref}")
                return m.group(0)
            f = self._field(view, name)
            if f is None:
                kind = self.fields.get(f"{view}.{name}")
                if kind and kind[0] == "time":
                    g = self._field(view, kind[1]) or {}
                    if kind[2] in ("raw", "time"):
                        expr, p = self.resolve(view, g.get("sql") or f"${{TABLE}}.{kind[1]}", seen | {ref})
                        problems.extend(p)
                        return _paren(expr)
                    problems.append(f"references timeframe {ref} (truncated to {kind[2]} in Looker)")
                    return m.group(0)
                problems.append(f"unknown field {ref}")
                return m.group(0)
            if any(f is x for x in self.views[view].get("measures", [])):
                problems.append(f"references measure {ref}")
                return m.group(0)
            expr, p = self.resolve(view, f.get("sql") or f"${{TABLE}}.{name}", seen | {ref})
            problems.extend(p)
            return _paren(expr)

        out = REF.sub(sub, sql or "")
        out = out.replace("\x00TABLE\x00.", "")
        if "\x00TABLE\x00" in out:
            problems.append("uses ${TABLE} without a column")
            out = out.replace("\x00TABLE\x00", "${TABLE}")
        return " ".join(out.split()), problems

    def dim_expr(self, vname: str, field: dict[str, Any]) -> tuple[str, list[str]]:
        if field.get("case"):
            return self._case(vname, field["case"])
        return self.resolve(vname, field.get("sql") or f"${{TABLE}}.{field['name']}")

    def _case(self, vname: str, case: dict[str, Any]) -> tuple[str, list[str]]:
        parts, problems = ["CASE"], []
        for w in case.get("whens", []):
            expr, p = self.resolve(vname, w.get("sql", ""))
            problems += p
            label = str(w.get("label", "")).replace("'", "''")
            parts.append(f"WHEN {expr} THEN '{label}'")
        if case.get("else"):
            parts.append("ELSE '" + str(case["else"]).replace("'", "''") + "'")
        parts.append("END")
        return " ".join(parts), problems

    def field_ref(self, vname: str, ref: str) -> tuple[str, str] | None:
        """Filter field -> ("{{ Dimension('v__f') }}", kind)."""
        view, _, name = ref.rpartition(".")
        view = view or vname
        kind = self.fields.get(f"{view}.{name}")
        if not kind:
            return None
        if kind[0] == "time":
            return f"{{{{ TimeDimension('{snake(view)}__{snake(kind[1])}', '{TF_GRAIN.get(kind[2], 'day')}') }}}}", "time"
        if kind[0] != "dimension":
            return None
        f = self._field(view, name) or {}
        t = f.get("type", "string")
        kind = "yesno" if t == "yesno" else "number" if t == "number" else "time" if t.startswith("date") or t == "time" else "string"
        return f"{{{{ Dimension('{snake(view)}__{snake(name)}') }}}}", kind

    # ------------------------------------------------------------ views

    def _view(self, vname: str, v: dict[str, Any]) -> None:
        item = f"view {vname}"
        src = self.view_file.get(vname, "")
        if v.get("extension") == "required":
            self.b.warn(item, "extension: required view (only extended by other views); skipped", "info")
            return
        table = self.table_name(vname)
        dt = v.get("derived_table")
        what = "derived table"
        if not dt and not table and v.get("sql_table_name"):
            dt, what = {"sql": v["sql_table_name"]}, "sql_table_name that is SQL rather than a table name"
        if dt and not table:
            table = snake(vname)
            if dt.get("sql"):
                sql, problems = self.resolve(vname, dt["sql"])
            elif dt.get("explore_source"):
                sql, problems = "", ["native derived table (explore_source)"]
            else:
                sql, problems = "", ["no `sql` (sql_create or create_process runs custom DDL, e.g. a BigQuery ML model)"]
            pdt = [k for k in ("datagroup_trigger", "persist_for", "sql_trigger_value", "materialized_view", "increment_key") if dt.get(k)]
            todo = (
                f"Looker {'PDT (' + ', '.join(f'{k}: {dt[k]}' for k in pdt) + ')' if pdt else what}: "
                f"materialize this SQL as a table or dbt model named `{table}` (the semantic model `{snake(vname)}` points at it)"
            )
            if problems:
                todo += "; also " + "; ".join(problems)
            body = sql or json.dumps(dt.get("explore_source") or dt, indent=2, default=str)
            self.b.queries.append(
                VerifiedQuery(
                    name=f"{vname} derived table",
                    sql=body if sql else "-- " + body.replace("\n", "\n-- "),
                    description=f"Looker {what} {vname}",
                    datasource=self.b.datasource,
                    source_ref=f"{src} view: {vname}",
                    todo=todo,
                )
            )
        elif not table:
            self.b.warn(item, "view has no sql_table_name or derived_table; skipped (probably a field-only or extension view)", "info")
            return
        schema, tname = _split_table(table)
        tbl = Table(self.b.datasource, schema, tname, source_ref=f"{src} view: {vname}") if not dt else None
        sm = SemanticModel(name=vname, table=table, datasource=self.b.datasource, source_ref=f"{src} view: {vname}")
        pk = self._pk(vname)
        if pk:
            expr, problems = self.dim_expr(vname, pk)
            if problems:
                self.b.warn(item, f"primary key {pk['name']} {'; '.join(problems)}; no primary entity, add it by hand")
                pk = None
            else:
                sm.entities.append(Entity(snake(vname), "primary", "" if expr == snake(vname) else expr))
        for d in v.get("dimensions", []):
            if d is pk:
                if tbl is not None:
                    tbl.columns.append(Column(self.dim_expr(vname, d)[0], COLUMN_TYPE.get(d.get("type", "string"), ""), d.get("description", ""), _label(d)))
                continue
            expr, problems = self.dim_expr(vname, d)
            dtype = d.get("type", "string")
            dim = Dimension(
                name=d["name"],
                expr="" if expr == d["name"] else expr,
                description=d.get("description", ""),
                label=_label(d),
            )
            if dtype in ("tier", "location", "distance", "duration"):
                problems.append(f"type: {dtype} has no MetricFlow equivalent")
            if problems:
                dim.todo, dim.source_expression, dim.source_language = "; ".join(problems), _source(d), "lookml"
                dim.expr = ""
            sm.dimensions.append(dim)
            if tbl is not None and SIMPLE_IDENT.match(expr) and not problems:
                tbl.columns.append(Column(expr, COLUMN_TYPE.get(dtype, ""), d.get("description", ""), _label(d), _yes(d.get("hidden"))))
        for g in v.get("dimension_groups", []):
            if g.get("type", "time") != "time":
                sm.dimensions.append(
                    Dimension(g["name"], todo=f"dimension_group type: {g.get('type')} (interval between two times)",
                              source_expression=json.dumps({k: g[k] for k in g if k != "name"}), source_language="lookml")
                )
                continue
            expr, problems = self.resolve(vname, g.get("sql") or f"${{TABLE}}.{g['name']}")
            tfs = g.get("timeframes") or ["time"]
            grain = next((gr for tf, gr in TIMEFRAME_GRAIN if tf in tfs), "day")
            if g.get("datatype") in ("date", "yyyymmdd") and grain in ("second", "minute", "hour"):
                grain = "day"
            dim = Dimension(g["name"], "time", "" if expr == g["name"] else expr, g.get("description", ""), _label(g), grain)
            if g.get("datatype") in ("epoch", "yyyymmdd"):
                problems.append(f"datatype: {g['datatype']} (the column is a number, not a date); convert it to a date or timestamp in the expression")
            if problems:
                dim.todo, dim.source_expression, dim.source_language, dim.expr = "; ".join(problems), _source(g), "lookml", ""
            sm.dimensions.append(dim)
            if tbl is not None and SIMPLE_IDENT.match(expr) and not problems:
                ctype = "date" if g.get("datatype") == "date" else "timestamp"
                tbl.columns.append(Column(expr, ctype, g.get("description", ""), _label(g), _yes(g.get("hidden"))))
        taken = {e.name for e in sm.entities}
        for d in sm.dimensions:
            if snake(d.name) in taken:
                self.b.warn(item, f"dimension {d.name} clashes with entity name; renamed {d.name}_dim")
                d.name = f"{d.name}_dim"
        for m in v.get("measures", []):
            self._measure(vname, m, sm, pk)
        if sm.measures and not any(d.type == "time" for d in sm.dimensions):
            self.b.warn(item, "has measures but no time dimension; MetricFlow needs defaults.agg_time_dimension", "info")
        self.b.semantic_models.append(sm)
        if tbl is not None:
            self.b.tables.append(tbl)
        for fld in [*v.get("dimensions", []), *v.get("dimension_groups", []), *v.get("measures", [])]:
            if fld.get("required_access_grants"):
                is_dim = any(fld is d for d in v.get("dimensions", []))
                column = self.dim_expr(vname, fld)[0] if is_dim else fld["name"]
                extra = ""
                if is_dim and not SIMPLE_IDENT.match(column):
                    extra = f"; the field is an expression ({column}), so protect each column it reads"
                    column = fld["name"]
                rtype = "Column" if is_dim else "Metric"
                rid = f"{table}.{column}" if is_dim else self.metric_names.get((vname, fld["name"]), snake(fld["name"]))
                self._grant_permissions(fld["required_access_grants"], rtype, rid, f"{src} view: {vname} field: {fld['name']}", extra)
        if v.get("required_access_grants"):
            self._grant_permissions(v["required_access_grants"], "Table", table, f"{src} view: {vname}")

    def _measure(self, vname: str, m: dict[str, Any], sm: SemanticModel, pk: dict[str, Any] | None) -> None:
        mtype = m.get("type", "count")
        name = self.metric_names[(vname, m["name"])]
        src = f"{self.view_file.get(vname, '')} view: {vname} measure: {m['name']}"
        raw_sql = m.get("sql", "")
        metric = Metric(name=name, label=_label(m) or m["name"].replace("_", " ").title(),
                        description=m.get("description", ""), semantic_model=vname, source_ref=src)
        source_expr = f"type: {mtype}" + (f" sql: {raw_sql}" if raw_sql else "") + "".join(
            f" {k}: {m[k]}" for k in ("sql_distinct_key", "list_field", "direction", "percentile") if m.get(k)
        )

        def todo(msg: str) -> None:
            metric.todo, metric.source_expression, metric.source_language = msg, source_expr, "lookml"
            self.b.metrics.append(metric)

        if LIQUID.search(raw_sql):
            return todo("uses Liquid templating; rewrite as a plain SQL measure (or one metric per branch)")
        if CONSTANT.search(raw_sql):
            return todo(f"uses constant {CONSTANT.search(raw_sql).group(0)} that manifest.lkml does not define")
        if mtype in MEASURE_AGG:
            if mtype == "count":
                expr = self.dim_expr(vname, pk)[0] if pk else "1"
                problems: list[str] = []
                if not pk:
                    self.b.warn(src, "count without a primary key; counting rows with expr 1", "info")
            else:
                expr, problems = self.resolve(vname, raw_sql or f"${{TABLE}}.{m['name']}")
            if problems:
                return todo("; ".join(problems))
            measure = Measure(name, MEASURE_AGG[mtype], expr, m.get("description", ""), metric.label)
            if mtype == "percentile":
                measure.agg_params = {"percentile": float(m.get("percentile", 50)) / 100, "use_discrete_percentile": False}
            filters, bad = [], []
            for field, value in _filter_pairs(m):
                ref = self.field_ref(vname, field)
                sql = looker_filter_sql(ref[0], value, ref[1]) if ref else None
                if sql is None:
                    bad.append(f"{field}: {value!r}")
                else:
                    filters.append(sql)
            sm.measures.append(measure)
            metric.measure = name
            metric.filter = " AND ".join(filters)
            if bad:
                source_expr += f" filters: [{', '.join(bad)}]"
                return todo(f"Looker filter expression(s) not translated: {', '.join(bad)}; add them to the metric filter")
            self.b.metrics.append(metric)
            return
        if mtype == "number":
            refs = REF.findall(raw_sql)
            names, problems = {}, []
            for ref in refs:
                view, _, fname = ref.strip().rpartition(".")
                view = view or vname
                if (view, fname) in self.metric_names:
                    names[ref] = self.metric_names[(view, fname)]
                else:
                    problems.append(f"references non-measure {ref}")
            if problems or not refs:
                return todo("; ".join(problems) or "number measure with inline aggregate SQL; split into simple measures")
            expr = REF.sub(lambda mm: names[mm.group(1)], raw_sql).strip()
            if AGG_FN.search(expr):
                return todo("mixes aggregate SQL with measure references")
            ratio = re.fullmatch(
                r"(?:1(?:\.0*)?\s*\*\s*)?(\w+)\s*(?:\*\s*1(?:\.0*)?\s*)?/\s*(?:NULLIF\s*\(\s*(\w+)\s*,\s*0\s*\)|(\w+))(?:\s*\*\s*1(?:\.0*)?)?",
                expr, re.I,
            )
            if ratio:
                metric.type, metric.numerator, metric.denominator = "ratio", ratio.group(1), ratio.group(2) or ratio.group(3)
            else:
                metric.type, metric.expr = "derived", " ".join(expr.split())
                metric.metrics = list(dict.fromkeys(names.values()))
            self.b.metrics.append(metric)
            return
        if mtype == "running_total":
            return todo("running_total is computed over result rows in Looker; use a cumulative metric or a window in the tile SQL")
        return todo(f"measure type {mtype} has no direct MetricFlow agg")

    # ------------------------------------------------------------ models / explores

    def _model(self, mname: str, model: dict[str, Any]) -> None:
        src = model.get("file", f"{mname}.model.lkml")
        lines = [f"LookML model `{mname}`" + (f" (connection `{model['connection']}`)" if model.get("connection") else "") + ".", ""]
        table_explores: dict[str, list[str]] = {}
        for ex in model["explores"]:
            base = ex.get("from") or ex.get("view_name") or ex["name"]
            table_explores.setdefault(self.model_table(base), []).append(ex["name"])
            for j in ex.get("joins", []):
                table_explores.setdefault(self.model_table(j.get("from") or j["name"]), []).append(ex["name"])
        for ex in model["explores"]:
            base = ex.get("from") or ex.get("view_name") or ex["name"]
            alias = {base: base, ex["name"]: base}
            for j in ex.get("joins", []):
                alias[j["name"]] = j.get("from") or j["name"]
            lines.append(f"## Explore: {ex.get('label') or ex['name']}")
            if ex.get("description"):
                lines.append("")
                lines.append(ex["description"])
            lines += ["", f"- Base view: `{base}` (`{self.model_table(base)}`)"]
            for j in ex.get("joins", []):
                self._join(ex, base, j, alias)
                lines.append(f"- Joins `{j['name']}` ({j.get('relationship', 'many_to_one')}, {j.get('type', 'left_outer')}) on `{j.get('sql_on') or j.get('foreign_key', '')}`")
            for af in ex.get("access_filters", []):
                self._access_filter(src, ex, base, af, alias, table_explores)
            if ex.get("sql_always_where"):
                self._always_where(src, ex, base, alias)
                lines.append(f"- Always filtered: `{ex['sql_always_where']}`")
            if ex.get("always_filter"):
                lines.append(f"- Default filter users must fill: `{json.dumps(ex['always_filter'])}`")
            if ex.get("required_access_grants"):
                self._grant_permissions(ex["required_access_grants"], "Table", self.model_table(base), f"{src} explore: {ex['name']}")
            lines.append("")
        self.b.knowledge.append(
            KnowledgeItem(f"Looker model {mname}", "\n".join(lines), description=f"Explores and joins from the Looker model {mname}",
                          datasource=self.b.datasource, source_ref=src)
        )

    def _join_cols(self, sql_on: str, alias: dict[str, str]) -> list[tuple[str, str]] | None:
        """`${a.x} = ${b.y}` -> [(view_a, field_x), (view_b, field_y)]"""
        m = re.fullmatch(r"\s*\$\{(\w+)\.(\w+)\}\s*=\s*\$\{(\w+)\.(\w+)\}\s*", sql_on or "")
        if not m:
            return None
        return [(alias.get(m.group(1), m.group(1)), m.group(2)), (alias.get(m.group(3), m.group(3)), m.group(4))]

    def _join(self, ex: dict[str, Any], base: str, j: dict[str, Any], alias: dict[str, str]) -> None:
        target = j.get("from") or j["name"]
        sql_on = j.get("sql_on") or (f"${{{base}.{j['foreign_key']}}} = ${{{j['name']}.{(self._pk(target) or {}).get('name', 'id')}}}" if j.get("foreign_key") else "")
        cols = self._join_cols(sql_on, alias)
        if not sql_on and j.get("sql") is not None:
            self.b.warn(f"explore {ex['name']} join {j['name']}",
                        f"join uses `sql:` instead of sql_on ({j['sql'].strip()!r}), e.g. UNNEST of a nested field or a field-only view; "
                        "model it by hand (MetricFlow joins only through entities)")
            return
        if not sql_on:
            self.b.warn(f"explore {ex['name']} join {j['name']}",
                        f"join has no sql_on or foreign_key (type: {j.get('type', 'left_outer')}, so a cross join); add the entity by hand if it is needed")
            return
        if not cols:
            self.b.warn(f"explore {ex['name']} join {j['name']}", f"join is not a single equality ({sql_on!r}); add the entity by hand")
            return
        if j.get("from") and j["from"] != j["name"]:
            self.b.warn(f"explore {ex['name']} join {j['name']}", f"join aliases view {target}; MetricFlow uses one entity per model, so role-playing joins need a second entity by hand", "info")
        for (v1, f1), (v2, f2) in (cols, cols[::-1]):
            pk = self._pk(v2)
            if pk and pk["name"] == f2 and v1 != v2:
                sm = next((s for s in self.b.semantic_models if s.name == v1), None)
                f = self._field(v1, f1)
                if sm is None or f is None:
                    continue
                expr, problems = self.dim_expr(v1, f)
                if problems:
                    self.b.warn(f"explore {ex['name']} join {j['name']}", "; ".join(problems))
                    return
                if any(e.name == snake(v2) for e in sm.entities):
                    return
                etype = "unique" if j.get("relationship") == "one_to_one" else "foreign"
                sm.entities.append(Entity(snake(v2), etype, expr))
                return
        self.b.warn(f"explore {ex['name']} join {j['name']}", "neither side of the join is a primary key; add entities by hand")

    def _access_filter(self, src, ex, base, af, alias, table_explores) -> None:
        field = af.get("field", "")
        view, _, fname = field.rpartition(".")
        view = alias.get(view, view)
        f = self._field(view, fname)
        attr = af.get("user_attribute", "")
        source = f"explore: {ex['name']} access_filter: {{field: {field} user_attribute: {attr}}}"
        name = f"{ex['name']}_{snake(field)}"
        if f is None:
            self.b.row_filters.append(RowFilter(name, self.model_table(view), source_expression=source, source_ref=src,
                                                todo=f"field {field} not found in LookML; write the predicate by hand"))
            return
        expr, problems = self.dim_expr(view, f)
        table = self.model_table(view)
        others = list(dict.fromkeys(e for e in table_explores.get(table, []) if e != ex["name"]))
        todo = "; ".join(problems)
        if others:
            todo = (todo + "; " if todo else "") + (
                f"Looker applied this only inside explore {ex['name']}; explore(s) {', '.join(others)} expose {table} "
                "without it. unshackle row filters apply to every query on the table, so confirm that is intended"
            )
        rf = RowFilter(name, table, f"{expr} = {{{{ user.{attr} }}}}", source_expression=source, source_ref=src, todo=todo)
        self.b.row_filters.append(rf)
        if view == base:
            return
        path = next((j for j in ex.get("joins", []) if (j.get("from") or j["name"]) == view), None)
        cols = self._join_cols((path or {}).get("sql_on", ""), alias) if path else None
        if cols and base in (cols[0][0], cols[1][0]):
            (bv, bf), (jv, jf) = cols if cols[0][0] == base else cols[::-1]
            bexpr, p1 = self.dim_expr(bv, self._field(bv, bf) or {"name": bf})
            jexpr, p2 = self.dim_expr(jv, self._field(jv, jf) or {"name": jf})
            if not (p1 or p2):
                self.b.row_filters.append(
                    RowFilter(f"{name}_via_{snake(base)}", self.model_table(base),
                              f"{bexpr} IN (SELECT {jexpr} FROM {table} WHERE {expr} = {{{{ user.{attr} }}}})",
                              source_expression=source + f" (through join {path['name']})", source_ref=src)
                )
                return
        self.b.row_filters.append(
            RowFilter(f"{name}_via_{snake(base)}", self.model_table(base), source_expression=source, source_ref=src,
                      todo=f"Looker filtered explore {ex['name']} (base {self.model_table(base)}) through joins to {view}; write a predicate on the base table")
        )

    def _always_where(self, src, ex, base, alias) -> None:
        raw = ex["sql_always_where"]
        sql = USER_ATTR.sub(lambda m: f"\x01{m.group(1)}\x02", raw)

        def unalias(m: re.Match) -> str:
            view, dot, name = m.group(1).partition(".")
            return "${" + (alias.get(view, view) + dot + name if dot else view) + "}"

        # refs to the base view resolve; refs to joined views stay TODO (row filters apply to one table)
        expr, problems = self.resolve(base, REF.sub(unalias, sql))
        expr = expr.replace("\x01", "{{ user.").replace("\x02", " }}")
        self.b.row_filters.append(
            RowFilter(f"{ex['name']}_always_where", self.model_table(base), "" if problems else expr, ["*"],
                      source_expression=f"explore: {ex['name']} sql_always_where: {raw}", source_ref=src,
                      todo="; ".join(problems))
        )

    def _grant_permissions(self, grant_names: list[str], rtype: str, rid: str, src: str, extra: str = "") -> None:
        for gname in grant_names:
            g = self.grants.get(gname)
            if not g:
                self.b.warn(src, f"access_grant {gname} not found")
                continue
            attr, values = g.get("user_attribute", ""), g.get("allowed_values") or []
            expr = f"access_grant: {gname} {{user_attribute: {attr} allowed_values: {json.dumps(values)}}}"
            note = (
                f"Looker allowed users whose `{attr}` user attribute is one of {values}; one Cedar group per value was assumed. "
                f"Map each group to your IdP (or sync the attribute), and add a forbid for everyone else if a broader permit exists"
            )
            if len(grant_names) > 1:
                note += f". Looker required ALL of {grant_names}"
            note += extra
            for value in values:
                self.b.permissions.append(
                    Permission(f"group:{value}", "query" if rtype != "Notebook" else "view", rtype, rid,
                               source_expression=expr, source_ref=src, todo=note)  # type: ignore[arg-type]
                )

    # ------------------------------------------------------------ dashboards

    def _classify(self, field: str, explore_alias: dict[str, str]) -> tuple[str, str]:
        view, _, name = field.rpartition(".")
        kind = self.fields.get(f"{explore_alias.get(view, view)}.{name}")
        return (kind[0], kind[2]) if kind else ("unknown", "")

    def _tile(self, el: dict[str, Any], dash_filters: dict[str, DashboardFilter]) -> Tile:
        eid = str(el.get("id"))
        etype = el.get("type", "vis")
        if etype == "text" or (etype != "vis" and (el.get("body_text") or el.get("title_text"))):
            parts = [f"## {el['title_text']}" if el.get("title_text") else "", el.get("subtitle_text") or "", el.get("body_text") or ""]
            return Tile(eid, el.get("title_text") or "", "text", "text", text="\n\n".join(p for p in parts if p), source_type="text")
        q = _element_query(el)
        vis = q.get("vis_config") or el.get("result_maker", {}).get("vis_config") or {}
        vtype = vis.get("type") or q.get("vis_config", {}).get("type") or "unknown"
        kind, mark = VIS_MARK.get(vtype, ("unknown", "unknown"))
        todos: list[str] = []
        if (kind, mark) == ("unknown", "unknown"):
            todos.append(f"Looker visualization {vtype!r} (map or custom viz) has no Altair equivalent here; pick a chart")
        qid = _element_query_id(el)
        sql = self.store.get_text(f"queries/{qid}.sql") if qid and self.store.exists(f"queries/{qid}.sql") else ""
        fields = list(q.get("fields") or [])
        pivots = list(q.get("pivots") or [])
        hidden = set(vis.get("hidden_fields") or [])
        for f in fields + pivots:
            sql = sql.replace(f'"{f}"', snake(f)).replace(f"`{f}`", snake(f))
        if not sql:
            todos.append("no generated SQL saved for this element" + (" (merged results)" if el.get("merge_result_id") else ""))
        alias = self._explore_alias(q.get("model", ""), q.get("view", ""))
        col = snake
        dims = [f for f in fields if self._classify(f, alias)[0] in ("dimension", "time", "unknown") and f not in pivots and f not in hidden]
        measures = [f for f in fields if self._classify(f, alias)[0] == "measure" and f not in hidden]
        dyn = q.get("dynamic_fields")
        if isinstance(dyn, str) and dyn.strip():
            try:
                dyn = json.loads(dyn)
            except ValueError:
                dyn = []
        for d in dyn or []:
            if d.get("table_calculation"):
                todos.append(f"table calculation {d['table_calculation']} = {d.get('expression', '')} (Looker expression); add it to the SQL as a window/column")
            elif d.get("measure") or d.get("dimension"):
                todos.append(f"custom field {d.get('measure') or d.get('dimension')}: {d.get('expression') or d.get('based_on', '')} ({d.get('type', '')}); add it to the SQL")
        enc: dict[str, Encoding] = {}

        def e(f: str, q_type: str | None = None) -> Encoding:
            k, _ = self._classify(f, alias)
            t = q_type or ("temporal" if k == "time" else "quantitative" if k == "measure" else "nominal")
            return Encoding(col(f), t)  # type: ignore[arg-type]

        if kind == "kpi":
            if measures:
                enc["text"] = e(measures[0])
        elif mark == "arc":
            if measures:
                enc["theta"] = e(measures[0])
            if dims:
                enc["color"] = e(dims[0], "nominal")
        elif kind == "chart":
            if dims:
                enc["x"] = e(dims[0])
            if measures:
                enc["y"] = e(measures[0])
            if pivots:
                enc["color"] = e(pivots[0], "nominal")
            elif len(dims) > 1:
                enc["color"] = e(dims[1], "nominal")
            if len(measures) > 1:
                todos.append(f"Looker plotted {len(measures)} measures ({', '.join(measures)}); only the first is encoded, fold the rest or add tiles")
        listen = [
            (lf.get("dashboard_filter_name"), lf.get("field"))
            for fl in (el.get("result_maker") or {}).get("filterables") or []
            for lf in fl.get("listen") or []
        ]
        applied = [(n, f) for n, f in listen if n in dash_filters]
        if applied:
            todos.append(
                "dashboard filter(s) "
                + ", ".join(f"{n} on {f} (:{dash_filters[n].name})" for n, f in applied)
                + " are not in the generated SQL; add WHERE clauses with those parameters"
            )
        title = el.get("title") or vis.get("title") or ", ".join(fields[:3]) or eid
        return Tile(
            eid, title, kind, mark, sql.strip(), enc, source_type=vtype,
            source_config={"query": {k: q.get(k) for k in ("model", "view", "fields", "pivots", "filters", "sorts", "limit") if q.get(k)}, "vis_config": vis},
            todo="; ".join(todos),
        )

    def _explore_alias(self, model: str, explore: str) -> dict[str, str]:
        for ex in (self.models.get(model) or {}).get("explores", []):
            if ex["name"] == explore:
                base = ex.get("from") or ex["name"]
                out = {ex["name"]: base}
                out.update({j["name"]: j.get("from") or j["name"] for j in ex.get("joins", [])})
                return out
        return {}

    def _dashboard(self, rel: str, d: dict[str, Any], usage: dict) -> None:
        did = str(d.get("id") or Path(rel).stem)
        filters: dict[str, DashboardFilter] = {}
        for f in d.get("dashboard_filters") or []:
            dim = f.get("dimension") or ""
            k, _ = self._classify(dim, self._explore_alias(f.get("model", ""), f.get("explore", ""))) if dim else ("", "")
            ftype = f.get("type", "")
            kind = "date_range" if ftype == "date_filter" or k == "time" else "number" if ftype == "number_filter" else "value"
            filters[f["name"]] = DashboardFilter(snake(f.get("name") or f.get("title") or dim), snake(dim.replace(".", "_")), f.get("default_value") or None, kind)  # type: ignore[arg-type]
        tiles = [self._tile(el, filters) for el in d.get("dashboard_elements") or []]
        layout: list[list[str]] = []
        layouts = d.get("dashboard_layouts") or []
        active = next((lay for lay in layouts if lay.get("active")), layouts[0] if layouts else None)
        if active:
            rows: dict[int, list[tuple[int, str]]] = {}
            for c in active.get("dashboard_layout_components") or []:
                if c.get("deleted"):
                    continue
                rows.setdefault(int(c.get("row") or 0), []).append((int(c.get("column") or 0), str(c.get("dashboard_element_id"))))
            known = {t.id for t in tiles}
            layout = [[eid for _, eid in sorted(r) if eid in known] for _, r in sorted(rows.items())]
            layout = [r for r in layout if r]
            placed = {i for r in layout for i in r}
            layout += [[t.id] for t in tiles if t.id not in placed]
        base = self.meta.get("base_url", "")
        u = usage.get(("dashboard", did), {})
        self.b.dashboards.append(
            Dashboard(
                name=d.get("title") or did,
                slug=slugify(d.get("title") or did),
                description=d.get("description") or "",
                tiles=tiles,
                layout=layout,
                filters=list(filters.values()),
                datasource=self.b.datasource,
                source_ref=f"looker dashboard {did}",
                url=f"{base}/dashboards/{did}" if base else "",
                views=u.get("views"),
            )
        )

    def _look(self, rel: str, lk: dict[str, Any]) -> None:
        lid = str(lk.get("id") or Path(rel).stem)
        qid = str(lk.get("query_id") or (lk.get("query") or {}).get("id") or "")
        sql = self.store.get_text(f"queries/{qid}.sql").strip() if qid and self.store.exists(f"queries/{qid}.sql") else ""
        q = lk.get("query") or {}
        for f in list(q.get("fields") or []) + list(q.get("pivots") or []):
            sql = sql.replace(f'"{f}"', snake(f)).replace(f"`{f}`", snake(f))
        dyn = q.get("dynamic_fields")
        todo = "" if sql else "no generated SQL saved for this Look"
        if dyn and dyn not in ("[]", []):
            todo = (todo + "; " if todo else "") + "Look has table calculations or custom fields (see source dynamic_fields); add them to the SQL"
        self.b.queries.append(
            VerifiedQuery(
                name=lk.get("title") or f"look {lid}",
                sql=sql or "-- no SQL",
                description=lk.get("description") or "",
                datasource=self.b.datasource,
                owner=str((lk.get("user") or {}).get("display_name") or ""),
                source_ref=f"looker look {lid}",
                todo=todo,
            )
        )


def _merge_view(base: dict[str, Any], override: dict[str, Any], refine: bool = False) -> dict[str, Any]:
    """Merge a view or explore into another: named children (fields, joins) by name, other keys
    replaced. `extends` from a refinement adds to the base's; through `extends` it is not inherited."""
    out = {k: v for k, v in base.items()}
    for key, value in override.items():
        if key == "name":
            continue
        if key in ("dimensions", "dimension_groups", "measures", "filters", "parameters", "sets", "joins") and isinstance(value, list):
            by_name = {f["name"]: dict(f) for f in out.get(key, [])}
            for f in value:
                by_name[f["name"]] = {**by_name.get(f["name"], {}), **f}
            out[key] = list(by_name.values())
        elif key == "access_filters":
            out[key] = [*out.get(key, []), *(f for f in value if f not in out.get(key, []))]
        elif key in ("extends", "extends__all"):
            if refine:
                parents = [p for g in (base.get("extends__all") or [base.get("extends") or []]) for p in g]
                parents += [p for g in (override.get("extends__all") or [override.get("extends") or []]) for p in g]
                out.pop("extends", None)
                out["extends__all"] = [list(dict.fromkeys(parents))]
        elif key != "extension" or refine:
            out[key] = value
    out.setdefault("name", base.get("name") or override.get("name"))
    return out
