"""Lightdash source.

Raw layout under migration/raw/lightdash/:

  project.json              {"url", "uuid", ...}  GET /api/v1/projects/{uuid}
  explores/_index.json      GET /api/v1/projects/{uuid}/explores
  explores/<explore>.json   GET .../explores/{exploreId}   compiled explore: tables -> dimensions/metrics
  charts/<slug>.yaml        GET .../code/charts             chart as code (same shape as `lightdash download`)
  charts/<slug>.sql         POST .../explores/{explore}/compileQuery with the chart's metricQuery
  dashboards/<slug>.yaml    GET .../code/dashboards
  dashboards/_usage.json    GET .../dashboards              uuid, name, views, firstViewedAt
  dbt/<path>.yml            copy of the dbt schema YAML under LIGHTDASH_DBT_PROJECT_DIR/models (optional)
  _errors.json              items that failed to extract; parse() turns them into warnings

parse() prefers compiled explores. With only dbt/ files (no API access) it reads the
Lightdash `meta` blocks (meta.dimension, meta.metrics, joins, primary_key, sql_filter,
required_attributes; dbt 1.10+ `config.meta` too) straight from the dbt YAML.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from unshackle.http import Client, SourceError, env
from unshackle.ir import (
    Bundle,
    Dashboard,
    DashboardFilter,
    Encoding,
    KnowledgeItem,
    Layer,
    RowFilter,
    Tile,
    VerifiedQuery,
    join_todos,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources._semyaml import (
    MF_AGGS,
    TIME_TYPES,
    NField,
    NFilter,
    NTable,
    add_join,
    as_list,
    attribute_permission,
    build_semantic,
    grid_rows,
    resolve_sql,
)
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source

POST_TYPES = {"number", "string", "date", "timestamp", "boolean"}
DATA_TYPES = {
    "timestamp": "timestamp", "timestamp_ntz": "timestamp", "timestamp_tz": "timestamp", "timestamptz": "timestamp",
    "datetime": "timestamp", "date": "date", "boolean": "boolean", "bool": "boolean",
    "int": "number", "integer": "number", "bigint": "number", "number": "number", "numeric": "number",
    "float": "number", "float64": "number", "int64": "number", "double": "number", "decimal": "number",
}
INTERVALS = ("raw", "day", "week", "month", "quarter", "year", "hour", "minute")
ATTR = re.compile(r"\$\{\s*(?:lightdash|ld)\.(?:attributes?|attr)\.(\w+)\s*\}")
DATE_RANGE_OPS = {"inThePast", "notInThePast", "inTheNext", "inTheCurrent", "notInTheCurrent", "inBetween"}


def _results(data: Any) -> Any:
    return data["results"] if isinstance(data, dict) and "results" in data else data


def _merged_meta(node: dict[str, Any]) -> dict[str, Any]:
    return {**(node.get("meta") or {}), **((node.get("config") or {}).get("meta") or {})}


def _text(value: Any) -> str:
    return "\n".join(str(v) for v in value) if isinstance(value, list) else str(value or "")


def _compiled_filters(raw: Any) -> list[NFilter]:
    out = []
    for f in as_list(raw):
        if not isinstance(f, dict):
            continue
        target = f.get("target") or {}
        ref = target.get("fieldRef") or target.get("fieldId") or f.get("fieldRef") or ""
        out.append(NFilter(ref, f.get("operator", "equals"), as_list(f.get("values"))))
    return out


def _dbt_value_filter(field: str, value: Any) -> NFilter:
    """Lightdash dbt YAML filter shorthand: 'x', '!x', 'null', '!null', '%x%', '> 5', [a, b], true."""
    if isinstance(value, list):
        if value and all(isinstance(v, str) and v.startswith("!") for v in value):
            return NFilter(field, "notEquals", [v[1:] for v in value])
        return NFilter(field, "equals", value)
    if value is None or value == "null":
        return NFilter(field, "isNull")
    if not isinstance(value, str):
        return NFilter(field, "equals", [value])
    if value == "!null":
        return NFilter(field, "notNull")
    m = re.match(r"^(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", value.strip())
    if m:
        op = {">": "greaterThan", ">=": "greaterThanOrEqual", "<": "lessThan", "<=": "lessThanOrEqual"}[m.group(1)]
        num = float(m.group(2))
        return NFilter(field, op, [int(num) if num.is_integer() else num])
    if value.startswith("!"):
        return NFilter(field, "notEquals", [value[1:]])
    if value.startswith("%") and value.endswith("%") and len(value) > 1:
        return NFilter(field, "include", [value[1:-1]])
    if value.endswith("%"):
        return NFilter(field, "startsWith", [value[:-1]])
    if value.startswith("%"):
        return NFilter(field, "endsWith", [value[1:]])
    return NFilter(field, "equals", [value])


def _dbt_filters(raw: Any) -> list[NFilter]:
    out = []
    for f in as_list(raw):
        if isinstance(f, dict) and ("property" in f or "field" in f) and "operator" in f:
            out.append(NFilter(f.get("property") or f.get("field"), f["operator"], as_list(f.get("values", f.get("value")))))
        elif isinstance(f, dict):
            out += [_dbt_value_filter(k, v) for k, v in f.items()]
    return out


def _metric_field(name: str, m: dict[str, Any], filters: list[NFilter], default_sql: str = "") -> NField:
    typ = str(m.get("type") or "count").lower()
    params = {}
    if typ == "percentile":
        pct = float(m.get("percentile", 50))
        params = {"percentile": pct / 100 if pct > 1 else pct, "use_discrete_percentile": False}
    sql = m.get("sql") or default_sql
    source = sql if not m.get("filters") else f"{sql} filters={json.dumps(m.get('filters'), default=str)}"
    return NField(
        name=name,
        sql=sql,
        type=typ if typ in MF_AGGS else ("post" if typ in POST_TYPES else typ),
        label=m.get("label") or "",
        description=m.get("description") or "",
        hidden=bool(m.get("hidden")),
        filters=filters,
        agg_params=params,
        source=f"type={typ} sql={source}",
    )


def _guess_pk(t: NTable) -> str:
    singular = t.name[:-1] if t.name.endswith("s") else t.name
    last = t.name.split("_")[-1]
    last = last[:-1] if last.endswith("s") else last
    for cand in ("id", f"{singular}_id", f"{last}_id"):
        if cand in t.dims:
            return cand
    return ""


class LightdashSource(Source):
    name = "lightdash"
    label = "Lightdash"
    env_vars = ("LIGHTDASH_URL", "LIGHTDASH_API_KEY", "LIGHTDASH_PROJECT")
    docs = "https://docs.lightdash.com/api-reference/v1/introduction"
    manual_files = "the dbt project folder (Lightdash metrics live in its schema YAML) and/or `lightdash download` output (charts and dashboards as YAML)"
    manual_gaps = (
        "compiled explores: fields come from the dbt YAML `meta` blocks, not Lightdash's compiled explores",
        "chart SQL: charts have no compiled SQL, so their tiles carry a TODO to write it from the semantic layer",
        "usage: no dashboard view counts and no links back to Lightdash",
        "permissions: space and project access is not in the files; only `required_attributes` / `sql_filter` from the dbt YAML",
    )

    def __init__(self, client: Client | None = None, project: str | None = None, dbt_project_dir: str | None = None):
        self._client = client
        self._project = project
        self._dbt_dir = dbt_project_dir

    # ------------------------------------------------------------ network

    @property
    def project(self) -> str:
        return self._project or env("LIGHTDASH_PROJECT")

    def client(self) -> Client:
        if self._client is None:
            key = env("LIGHTDASH_API_KEY")
            self._client = Client(env("LIGHTDASH_URL"), headers={"Authorization": f"ApiKey {key}"}, per_minute=120)
        return self._client

    def _api_configured(self) -> bool:
        return self._client is not None or bool(os.environ.get("LIGHTDASH_URL"))

    def _get(self, path: str, **kw: Any) -> Any:
        return _results(self.client().get_json(f"/api/v1/projects/{self.project}{path}", **kw))

    def _code(self, kind: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            res = self._get(f"/code/{kind}", params={"offset": offset})
            items = res.get(kind, []) if isinstance(res, dict) else as_list(res)
            out += [i for i in items if isinstance(i, dict)]
            offset += len(items)
            total = res.get("total") if isinstance(res, dict) else None
            if not items or total is None or offset >= int(total):
                return out

    def inventory(self) -> Inventory:
        inv = Inventory("lightdash")
        base = self.client().base_url
        for e in as_list(self._get("/explores")):
            if isinstance(e, dict):
                inv.add("explore", e.get("name"), e.get("label") or e.get("name", ""), extra={"errors": bool(e.get("errors"))})
        for d in as_list(self._get("/dashboards")):
            if isinstance(d, dict):
                inv.add(
                    "dashboard", d.get("uuid"), d.get("name", ""), parent=d.get("spaceName", ""), views=d.get("views"),
                    url=f"{base}/projects/{self.project}/dashboards/{d.get('uuid')}",
                    extra={"first_viewed_at": d.get("firstViewedAt", "")},
                )
        try:
            charts = as_list(self._get("/charts"))
        except SourceError as exc:
            charts = []
            inv.notes.append(f"could not list charts: {exc}")
        for c in charts:
            if isinstance(c, dict):
                inv.add(
                    "chart", c.get("uuid"), c.get("name", ""), parent=c.get("spaceName", ""), views=c.get("views"),
                    url=f"{base}/projects/{self.project}/saved/{c.get('uuid')}",
                )
        inv.notes.append("Views are Lightdash's lifetime view counts. Explores with errors are dbt models Lightdash could not compile.")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        dbt_dir = self._dbt_dir or os.environ.get("LIGHTDASH_DBT_PROJECT_DIR", "")
        if dbt_dir:
            root = Path(dbt_dir)
            for p in sorted([*root.glob("models/**/*.yml"), *root.glob("models/**/*.yaml")]):
                store.put_text(f"dbt/{p.relative_to(root)}", p.read_text(encoding="utf-8"))
        if not self._api_configured():
            if not dbt_dir:
                raise SourceError("set LIGHTDASH_URL, LIGHTDASH_API_KEY, LIGHTDASH_PROJECT (or LIGHTDASH_DBT_PROJECT_DIR)")
            store.put_json("_errors.json", errors)
            return

        def picked(*keys: Any) -> bool:
            return only is None or any(str(k) in only for k in keys if k)

        try:
            project = self._get("")
        except SourceError as exc:
            project, _ = {}, errors.append({"item": "project", "error": str(exc)})
        store.put_json("project.json", {**(project if isinstance(project, dict) else {}), "url": self.client().base_url, "uuid": self.project})

        dashboards = [d for d in self._code("dashboards") if picked(d.get("slug"), d.get("uuid"), d.get("name"))]
        wanted = {t.get("properties", {}).get("chartSlug") for d in dashboards for t in d.get("tiles", []) if isinstance(t, dict)}
        charts = [c for c in self._code("charts") if picked(c.get("slug"), c.get("uuid")) or (only and c.get("slug") in wanted)]
        index = as_list(self._get("/explores"))
        store.put_json("explores/_index.json", index)
        used = {c.get("tableName") or (c.get("metricQuery") or {}).get("exploreName") for c in charts}
        for e in index:
            name = e.get("name") if isinstance(e, dict) else None
            if not name or not (picked(name) or name in used):
                continue
            if e.get("errors"):
                errors.append({"item": f"explore {name}", "error": json.dumps(e["errors"], default=str)[:300]})
                continue
            try:
                store.put_json(f"explores/{name}.json", self._get(f"/explores/{name}"))
            except SourceError as exc:
                errors.append({"item": f"explore {name}", "error": str(exc)})

        for c in charts:
            slug = slugify(c.get("slug") or c.get("name") or c.get("uuid") or "chart")
            store.put_text(f"charts/{slug}.yaml", yaml.safe_dump(c, sort_keys=False, allow_unicode=True))
            mq = dict(c.get("metricQuery") or {})
            explore = mq.get("exploreName") or c.get("tableName")
            if not explore:
                continue
            mq.setdefault("exploreName", explore)
            try:
                res = _results(self.client().post_json(f"/api/v1/projects/{self.project}/explores/{explore}/compileQuery", json=mq))
                sql = res if isinstance(res, str) else (res or {}).get("query") or (res or {}).get("sql")
                if sql:
                    store.put_text(f"charts/{slug}.sql", sql.strip() + "\n")
            except SourceError as exc:
                errors.append({"item": f"chart {slug}", "error": f"compileQuery: {exc}"})
        for d in dashboards:
            slug = slugify(d.get("slug") or d.get("name") or "dashboard")
            store.put_text(f"dashboards/{slug}.yaml", yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
        try:
            store.put_json("dashboards/_usage.json", as_list(self._get("/dashboards")))
        except SourceError as exc:
            errors.append({"item": "dashboard usage", "error": str(exc)})
        store.put_json("_errors.json", errors)

    # ------------------------------------------------------------ parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file() and path.suffix not in (".yml", ".yaml"):
            raise SourceError(f"{path.name}: Lightdash import accepts {self.manual_files}")
        out: dict[str, bytes] = {}
        files = list(_manual.files_of(path))
        dbt_roots = [rel.removesuffix("dbt_project.yml") for rel, _ in files if rel.rsplit("/", 1)[-1] == "dbt_project.yml"]
        for rel, p in files:
            if p.suffix not in (".yml", ".yaml"):
                continue
            root = next((r for r in dbt_roots if rel.startswith(f"{r}models/")), None)
            if root is not None:
                out[f"dbt/{rel.removeprefix(root)}"] = p.read_bytes()
                continue
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            kind = "charts" if "metricQuery" in doc else "dashboards" if "tiles" in doc else ""
            if kind:
                out[f"{kind}/{_manual.safe_name(str(doc.get('slug') or p.stem))}.yaml"] = p.read_bytes()
        return out

    def parse(self, store: RawStore) -> Bundle:
        bundle = Bundle(tool="lightdash")
        self._extra: dict[str, dict[str, Any]] = {}
        self._ids: dict[str, tuple[str, str]] = {}
        self._targets: list[str] = []  # each explore's targetDatabase (warehouse type)
        tables = self._from_explores(store, bundle) or self._from_dbt(store, bundle)
        project = store.get_json("project.json") if store.exists("project.json") else {}
        conn = project.get("warehouseConnection") if isinstance(project, dict) else None
        bundle.detect_warehouse([conn["type"]] if isinstance(conn, dict) and conn.get("type") else self._targets)
        build_semantic(bundle, list(tables.values()), "lightdash")
        self._access_and_knowledge(bundle, tables)
        self._content(store, bundle)
        if store.exists("_errors.json"):
            for e in as_list(store.get_json("_errors.json")):
                bundle.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return bundle.settle_time_dimensions()

    def _from_explores(self, store: RawStore, bundle: Bundle) -> dict[str, NTable]:
        tables: dict[str, NTable] = {}
        joins: list[tuple[str, dict[str, Any]]] = []
        for rel in store.glob("explores/*.json"):
            if rel.endswith("_index.json"):
                continue
            try:
                exp = _results(store.get_json(rel))
            except (ValueError, OSError) as exc:
                bundle.warn(rel, f"unreadable explore: {exc}")
                continue
            if not isinstance(exp, dict) or not isinstance(exp.get("tables"), dict):
                bundle.warn(rel, "explore has no tables; skipped")
                continue
            ename = exp.get("name") or Path(rel).stem
            self._targets.append(exp.get("targetDatabase") or "")
            for alias, tb in exp["tables"].items():
                if not isinstance(tb, dict):
                    continue
                for kind in ("dimensions", "metrics"):
                    for fname, f in (tb.get(kind) or {}).items():
                        self._ids[f"{alias}_{fname.replace('.', '__')}"] = (kind, str((f or {}).get("type", "")))
                if alias not in tables:
                    tables[alias] = self._table_from_explore(ename, alias, tb, bundle)
            joins += [(exp.get("baseTable") or ename, j) for j in as_list(exp.get("joinedTables")) if isinstance(j, dict)]
            if exp.get("aiHint"):
                self._extra.setdefault(exp.get("baseTable") or ename, {}).setdefault("hints", []).append(_text(exp["aiHint"]))
        for base, j in joins:
            add_join(tables, j.get("table", ""), j.get("sqlOn", ""), j.get("relationship", ""), bundle)
        for t in tables.values():
            self._finish_pk(t, bundle)
        return tables

    def _table_from_explore(self, ename: str, alias: str, tb: dict[str, Any], bundle: Bundle) -> NTable:
        sql_table = str(tb.get("sqlTable") or "")
        physical = re.split(r"\.", sql_table)[-1].strip('"`[] ') if sql_table else ""
        t = NTable(
            name=alias,
            table=physical or tb.get("originalName") or alias,
            schema=tb.get("schema") or "",
            database=tb.get("database") or "",
            description=tb.get("description") or "",
            label=tb.get("label") or "",
            source_ref=f"lightdash:explore/{ename}/{alias}",
        )
        extra = self._extra.setdefault(alias, {})
        for name, d in (tb.get("dimensions") or {}).items():
            if not isinstance(d, dict) or d.get("timeIntervalBaseDimensionName"):
                continue  # generated interval dims (created_at_month...) come back via MetricFlow grains
            t.dims[name] = NField(
                name, d.get("sql") or "", str(d.get("type") or "string"), d.get("label") or "", d.get("description") or "",
                bool(d.get("hidden")),
            )
            if d.get("requiredAttributes"):
                extra.setdefault("field_required", {})[name] = d["requiredAttributes"]
            if d.get("aiHint"):
                extra.setdefault("hints", []).append(f"- `{name}`: {_text(d['aiHint'])}")
        for name, m in (tb.get("metrics") or {}).items():
            if isinstance(m, dict):
                t.metrics[name] = _metric_field(name, m, _compiled_filters(m.get("filters")))
                if m.get("aiHint"):
                    extra.setdefault("hints", []).append(f"- `{name}`: {_text(m['aiHint'])}")
        pk = tb.get("primaryKey")
        if isinstance(pk, list):
            t.primary_key = pk[0] if len(pk) == 1 else ""
        else:
            t.primary_key = str(pk or "")
        if isinstance(pk, list) and len(pk) > 1:
            bundle.warn(alias, f"composite primary key {pk}; add a surrogate key column for the MetricFlow entity")
        t.default_time = ((tb.get("defaultTimeDimension") or {}).get("field")) or ""
        extra["sql_where"] = tb.get("uncompiledSqlWhere") or tb.get("sqlWhere") or ""
        extra["required"] = tb.get("requiredAttributes") or {}
        if tb.get("aiHint"):
            extra.setdefault("hints", []).insert(0, _text(tb["aiHint"]))
        return t

    def _from_dbt(self, store: RawStore, bundle: Bundle) -> dict[str, NTable]:
        tables: dict[str, NTable] = {}
        joins: list[tuple[str, dict[str, Any]]] = []
        for rel in [*store.glob("dbt/**/*.yml"), *store.glob("dbt/**/*.yaml")]:
            try:
                doc = yaml.safe_load(store.get_text(rel)) or {}
            except yaml.YAMLError as exc:
                bundle.warn(rel, f"invalid YAML: {exc}")
                continue
            for model in as_list(doc.get("models") if isinstance(doc, dict) else None):
                if not isinstance(model, dict) or not model.get("name"):
                    continue
                t = self._table_from_dbt(rel, model)
                tables[t.name] = t
                joins += [(t.name, j) for j in as_list(_merged_meta(model).get("joins")) if isinstance(j, dict)]
        for base, j in joins:
            alias = j.get("alias") or j.get("join", "")
            if j.get("alias") and j.get("join") in tables:
                bundle.warn(base, f"join alias {alias} of {j['join']}: aliased joins are not modelled; join uses {j['join']}")
            on = re.sub(rf"\$\{{{re.escape(alias)}\.", "${" + str(j.get("join")) + ".", str(j.get("sql_on", "")))
            add_join(tables, j.get("join", ""), on, j.get("relationship", ""), bundle)
        for t in tables.values():
            self._finish_pk(t, bundle)
        if tables and not any(t.schema for t in tables.values()):
            bundle.warn("dbt", "schema not in dbt YAML; semantic models use bare table names (set config.schema or fix node_relation)", "info")
        return tables

    def _table_from_dbt(self, rel: str, model: dict[str, Any]) -> NTable:
        meta = _merged_meta(model)
        cfg = model.get("config") or {}
        name = model["name"]
        t = NTable(
            name=name,
            table=cfg.get("alias") or name,
            schema=cfg.get("schema") or meta.get("schema") or "",
            database=cfg.get("database") or "",
            description=model.get("description") or "",
            label=meta.get("label") or "",
            source_ref=f"lightdash:dbt/{rel.removeprefix('dbt/')}#{name}",
        )
        extra = self._extra.setdefault(name, {})
        for col in as_list(model.get("columns")):
            if not isinstance(col, dict) or not col.get("name"):
                continue
            cname = col["name"]
            cmeta = _merged_meta(col)
            dim = cmeta.get("dimension") or {}
            dtype = str(col.get("data_type") or "").lower().split("(")[0].strip()
            typ = str(dim.get("type") or DATA_TYPES.get(dtype, "string")).lower()
            t.dims[cname] = NField(
                cname, dim.get("sql") or "", typ, dim.get("label") or "", dim.get("description") or col.get("description") or "",
                bool(dim.get("hidden")),
            )
            if typ in TIME_TYPES:
                for iv in INTERVALS:
                    self._ids[f"{name}_{cname}_{iv}"] = ("dimensions", typ)
            self._ids[f"{name}_{cname}"] = ("dimensions", typ)
            for extra_name, ad in (cmeta.get("additional_dimensions") or {}).items():
                t.dims[extra_name] = NField(extra_name, ad.get("sql") or "", str(ad.get("type") or "string"), ad.get("label") or "", ad.get("description") or "")
                self._ids[f"{name}_{extra_name}"] = ("dimensions", str(ad.get("type") or "string"))
            for mname, m in (cmeta.get("metrics") or {}).items():
                if isinstance(m, dict):
                    t.metrics[mname] = _metric_field(mname, m, _dbt_filters(m.get("filters")), "${" + cname + "}")
                    self._ids[f"{name}_{mname}"] = ("metrics", "number")
            if dim.get("required_attributes") or cmeta.get("required_attributes"):
                extra.setdefault("field_required", {})[cname] = dim.get("required_attributes") or cmeta.get("required_attributes")
            hint = cmeta.get("ai_hint") or dim.get("ai_hint")
            if hint:
                extra.setdefault("hints", []).append(f"- `{cname}`: {_text(hint)}")
        for mname, m in (meta.get("metrics") or {}).items():
            if isinstance(m, dict):
                t.metrics[mname] = _metric_field(mname, m, _dbt_filters(m.get("filters")))
                self._ids[f"{name}_{mname}"] = ("metrics", "number")
        pk = meta.get("primary_key")
        t.primary_key = pk[0] if isinstance(pk, list) and len(pk) == 1 else (pk if isinstance(pk, str) else "")
        t.default_time = (meta.get("default_time_dimension") or {}).get("field", "") if isinstance(meta.get("default_time_dimension"), dict) else ""
        extra["sql_where"] = meta.get("sql_filter") or meta.get("sql_where") or ""
        extra["required"] = meta.get("required_attributes") or {}
        if meta.get("ai_hint"):
            extra.setdefault("hints", []).insert(0, _text(meta["ai_hint"]))
        return t

    def _finish_pk(self, t: NTable, bundle: Bundle) -> None:
        if t.primary_key and t.primary_key not in t.dims:
            t.dims[t.primary_key] = NField(t.primary_key, hidden=True)
        if not t.primary_key:
            t.primary_key = _guess_pk(t)
            if t.primary_key:
                bundle.warn(t.name, f"no primary_key in Lightdash meta; guessed {t.primary_key!r}", "info")

    # ------------------------------------------------------------ access + knowledge

    def _access_and_knowledge(self, bundle: Bundle, tables: dict[str, NTable]) -> None:
        for name, extra in self._extra.items():
            t = tables.get(name)
            if t is None:
                continue
            where = str(extra.get("sql_where") or "")
            if where:
                pred = ATTR.sub(lambda m: "{{ user." + m.group(1) + " }}", where)
                pred, todo = resolve_sql(t, pred)
                bundle.row_filters.append(
                    RowFilter(
                        name=f"{t.name}_sql_filter", table=t.relation or t.name, predicate=pred, source_expression=where,
                        source_ref=t.source_ref, todo=todo or ("" if "{{ user." in pred else "static sql_filter; consider a view instead of a row filter"),
                    )
                )
            if extra.get("required"):
                bundle.permissions.append(
                    attribute_permission(extra["required"], "Table", t.relation or t.name, t.source_ref, "required_attributes", "Lightdash")
                )
            for col, req in (extra.get("field_required") or {}).items():
                bundle.permissions.append(
                    attribute_permission(req, "Column", f"{t.relation or t.name}.{col}", f"{t.source_ref}.{col}", "required_attributes", "Lightdash")
                )
            if extra.get("hints"):
                bundle.knowledge.append(
                    KnowledgeItem(
                        name=f"{t.label or t.name} AI hints",
                        body=f"# {t.label or t.name}\n\n" + "\n".join(extra["hints"]),
                        description=f"Lightdash AI hints for {t.name}",
                        datasource=bundle.datasource,
                        source_ref=t.source_ref,
                    )
                )

    # ------------------------------------------------------------ charts + dashboards

    def _ftype(self, fid: str, quantitative: set[str]) -> str:
        if fid in quantitative:
            return "quantitative"
        kind, typ = self._ids.get(fid, ("", ""))
        if kind == "metrics":
            return "quantitative"
        if typ in TIME_TYPES:
            return "temporal"
        if typ == "number":
            return "quantitative"
        if kind:
            return "nominal"
        if re.search(r"_(raw|day|week|month|quarter|year|hour|minute)$", fid):
            return "temporal"
        return "nominal"

    def _tile(self, chart: dict[str, Any], tid: str, title: str, sql: str) -> Tile:
        cc = chart.get("chartConfig") or {}
        typ = str(cc.get("type") or "table")
        cfg = cc.get("config") or {}
        mq = chart.get("metricQuery") or {}
        dims, mets = as_list(mq.get("dimensions")), as_list(mq.get("metrics"))
        calcs = [c for c in as_list(mq.get("tableCalculations")) if isinstance(c, dict)]
        quant = {c.get("name", "") for c in calcs}
        quant |= {f"{m.get('table')}_{m.get('name')}" for m in as_list(mq.get("additionalMetrics")) if isinstance(m, dict)}
        tile = Tile(id=tid, title=title or chart.get("name", ""), sql=sql, source_type=typ, source_config=cc)
        todos: list[str] = []

        def enc(fid: str, force: str = "") -> Encoding:
            return Encoding(fid, force or self._ftype(fid, quant))  # type: ignore[arg-type]

        if typ == "cartesian":
            layout = cfg.get("layout") or {}
            series = [s for s in as_list((cfg.get("eChartsConfig") or {}).get("series")) if isinstance(s, dict)]
            marks = {"bar": "bar", "line": "line", "area": "area", "scatter": "point"}
            per_field: dict[str, tuple[str, Any]] = {}  # y field -> (series type, y axis index); pivoted series repeat a field
            kinds = []
            for s in series:
                k = str(s.get("type") or "bar")
                k = "area" if k == "line" and s.get("areaStyle") is not None else k
                kinds.append(k)
                fid = ((s.get("encode") or {}).get("yRef") or {}).get("field")
                if fid:
                    per_field.setdefault(fid, (k, s.get("yAxisIndex") or 0))
            kinds = list(dict.fromkeys(kinds)) or ["bar"]
            tile.mark = marks.get(kinds[0], "unknown")  # type: ignore[assignment]
            x = layout.get("xField") or (dims[0] if dims else "")
            ys = list(per_field) or as_list(layout.get("yField")) or mets[:1]
            bars = [y for y in ys if per_field.get(y, ("bar",))[0] == "bar"]
            layered = len(ys) > 1 and len(bars) <= 1
            if len(ys) > 1 and not layered:
                todos.append(f"plots {len(bars)} bar series ({', '.join(bars)}); only the first is encoded: fold them in SQL (one row per series) and use color/xOffset")
            elif len(kinds) > 1 and not layered:
                todos.append(f"mixed series types {kinds} on one metric; choose the mark")
            pivot = as_list((chart.get("pivotConfig") or {}).get("columns"))
            xe = enc(x) if x else None
            ye = enc(ys[0], "quantitative") if ys else None
            if layout.get("flipAxes"):
                xe, ye = ye, xe
            tile.encodings = {k: v for k, v in (("x", xe), ("y", ye)) if v}
            if layered:
                # One layer per metric, each with its own mark; a second y axis makes it a dual-axis chart.
                ych = "x" if layout.get("flipAxes") else "y"
                tile.encodings.pop(ych, None)
                tile.layers = [Layer(marks.get(per_field.get(y, ("bar",))[0], "unknown"), {ych: enc(y, "quantitative")}) for y in ys]  # type: ignore[arg-type]
                axes = {per_field.get(y, ("", 0))[1] for y in ys}
                tile.compose = "dual_axis" if len(axes) > 1 and ych == "y" else "layer"
            if pivot:
                tile.encodings["color"] = enc(pivot[0], "nominal")
                if tile.mark == "bar" and not any(s.get("stack") for s in series) and not layout.get("flipAxes"):
                    tile.encodings["xOffset"] = enc(pivot[0], "nominal")
        elif typ == "big_number":
            fid = cfg.get("selectedField") or next(iter(mets + list(quant) + dims), "")
            tile.kind, tile.mark = "kpi", "text"
            if fid:
                tile.encodings = {"text": Encoding(fid, "quantitative", title=cfg.get("label") or "")}
        elif typ == "table":
            tile.kind, tile.mark = "table", "table"
        elif typ == "pie":
            group = as_list(cfg.get("groupFieldIds")) or dims
            metric = cfg.get("metricId") or (mets[0] if mets else "")
            tile.mark = "arc"
            tile.encodings = {k: v for k, v in (("theta", enc(metric, "quantitative") if metric else None), ("color", enc(group[0], "nominal") if group else None)) if v}
        elif typ == "custom":
            todos.append("custom Vega-Lite chart; port config.spec with alt.Chart.from_dict(spec) using this tile's SQL as data")
        else:
            tile.kind = "unknown"
            todos.append(f"Lightdash {typ} chart has no Altair mapping; choose a mark or rebuild")
        for c in calcs:
            if not c.get("sql"):
                todos.append(f"table calculation {c.get('name')} is formula/template based; confirm it is in the compiled SQL")
        if not sql:
            todos.append("no compiled SQL saved; re-run extract with API access or write SQL from the semantic layer")
        tile.todo = join_todos(todos)
        return tile

    def _content(self, store: RawStore, bundle: Bundle) -> None:
        charts: dict[str, dict[str, Any]] = {}
        for rel in store.glob("charts/*.yaml"):
            try:
                c = yaml.safe_load(store.get_text(rel))
            except yaml.YAMLError as exc:
                bundle.warn(rel, f"invalid chart YAML: {exc}")
                continue
            if isinstance(c, dict):
                c["_file"] = Path(rel).stem
                charts[c.get("slug") or c["_file"]] = c

        def chart_sql(c: dict[str, Any]) -> str:
            rel = f"charts/{c['_file']}.sql"
            return store.get_text(rel).strip() if store.exists(rel) else ""

        project = store.get_json("project.json") if store.exists("project.json") else {}
        usage = store.get_json("dashboards/_usage.json") if store.exists("dashboards/_usage.json") else []
        on_dash: set[str] = set()
        for rel in store.glob("dashboards/*.yaml"):
            if rel.endswith("_usage.json"):
                continue
            try:
                d = yaml.safe_load(store.get_text(rel)) or {}
            except yaml.YAMLError as exc:
                bundle.warn(rel, f"invalid dashboard YAML: {exc}")
                continue
            if not isinstance(d, dict):
                continue
            slug = d.get("slug") or Path(rel).stem
            dash = Dashboard(
                name=d.get("name") or slug, slug=slug, description=d.get("description") or "",
                datasource=bundle.datasource, source_ref=f"lightdash:dashboard/{slug}",
            )
            used: set[str] = set()
            grid = []
            for i, raw in enumerate(as_list(d.get("tiles"))):
                if not isinstance(raw, dict):
                    continue
                ttype = raw.get("type", "")
                props = raw.get("properties") or {}
                base = props.get("chartSlug") if ttype == "saved_chart" else f"{ttype or 'tile'}_{i + 1}"
                tid, n = snake(base or f"tile_{i + 1}"), 2
                while tid in used:
                    tid, n = f"{snake(base)}_{n}", n + 1
                used.add(tid)
                title = props.get("title") or ""
                if ttype == "saved_chart":
                    chart = charts.get(props.get("chartSlug", ""))
                    if chart is None:
                        tile = Tile(tid, title or props.get("chartSlug", ""), kind="unknown", source_type=ttype,
                                    todo=f"chart {props.get('chartSlug')!r} was not extracted")
                    else:
                        on_dash.add(props["chartSlug"])
                        tile = self._tile(chart, tid, title or props.get("chartName") or chart.get("name", ""), chart_sql(chart))
                elif ttype == "markdown":
                    body = str(props.get("content") or "")
                    tile = Tile(tid, title, kind="text", text=(f"## {title}\n\n" if title else "") + body, source_type=ttype)
                elif ttype == "loom":
                    tile = Tile(tid, title, kind="text", text=f"{title}\n\nLoom video: {props.get('url', '')}".strip(), source_type=ttype)
                else:
                    tile = Tile(tid, title, kind="unknown", source_type=ttype, source_config=props,
                                todo=f"Lightdash {ttype!r} tile is not migrated automatically; rebuild from its SQL")
                dash.tiles.append(tile)
                grid.append((tid, float(raw.get("x") or 0), float(raw.get("y") or 0)))
            dash.layout = grid_rows(grid)
            filt = d.get("filters") or {}
            for f in [*as_list(filt.get("dimensions")), *as_list(filt.get("metrics"))]:
                if not isinstance(f, dict) or f.get("disabled"):
                    continue
                fid = (f.get("target") or {}).get("fieldId", "")
                op, values = f.get("operator", "equals"), as_list(f.get("values"))
                ft = self._ftype(fid, set())
                kind = "date_range" if op in DATE_RANGE_OPS else "date" if ft == "temporal" else "number" if ft == "quantitative" else "value"
                default: Any = values[0] if len(values) == 1 else (values or None)
                if op in DATE_RANGE_OPS and values and (f.get("settings") or {}).get("unitOfTime"):
                    default = f"{op} {values[0]} {f['settings']['unitOfTime']}"
                dash.filters.append(DashboardFilter(snake(f.get("label") or fid), fid, default, kind))  # type: ignore[arg-type]
            for u in as_list(usage):
                if isinstance(u, dict) and (u.get("uuid") == d.get("uuid") or u.get("name") == dash.name):
                    dash.views = u.get("views")
                    if project.get("url") and u.get("uuid"):
                        dash.url = f"{project['url']}/projects/{project.get('uuid')}/dashboards/{u['uuid']}"
                    break
            bundle.dashboards.append(dash)
        for slug, c in charts.items():
            if slug in on_dash:
                continue
            sql = chart_sql(c)
            if sql:
                bundle.queries.append(
                    VerifiedQuery(c.get("name") or slug, sql, description=c.get("description") or "",
                                  datasource=bundle.datasource, source_ref=f"lightdash:chart/{slug}")
                )
            else:
                bundle.warn(f"chart {slug}", "not on a dashboard and no compiled SQL; not migrated")
