"""Omni (omni.co) source.

Raw layout under migration/raw/omni/:

  _source.json                          {"url": OMNI_URL}
  models/_index.json                    GET /api/v1/models (all pages)
  models/<modelId>/_meta.json           {id, name, modelKind, viewNames, checksums}
  models/<modelId>/<file>.yaml          every entry of GET /api/v1/models/{id}/yaml?fullyResolved=true
                                        (model, relationships, <view>.view, <topic>.topic; folders kept)
  models/<modelId>/sample_queries/<topic>__<slug>.sql
                                        SQL for topic sample_queries (POST /api/v1/query/run, planOnly)
  documents/_index.json                 GET /api/v1/documents (all pages)
  documents/<id>/export.json            dashboard export: GET /api/unstable/documents/{id}/export,
                                        falling back to POST /api/v1/content/export-dashboard
  documents/<id>/queries.json           GET /api/v1/documents/{id}/queries
  documents/<id>/sql/<queryId>.sql      generated SQL per query (POST /api/v1/query/run, planOnly)
  _errors.json                          items that failed to extract; parse() turns them into warnings

Omni names result columns after field refs ("orders.created_at[month]"). When the saved SQL
aliases every field that way, parse() wraps it in an outer SELECT that renames them to
snake_case (orders_created_at_month) so Altair field names stay plain.
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
    RowFilter,
    Tile,
    VerifiedQuery,
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
    find_key,
    grid_rows,
    resolve_sql,
    walk,
)
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source

OMNI_OPS = {
    "is": "equals", "not": "notEquals", "greater_than": "greaterThan", "greater_than_or_equal_to": "greaterThanOrEqual",
    "less_than": "lessThan", "less_than_or_equal_to": "lessThanOrEqual", "contains": "include",
    "does_not_contain": "doesNotInclude", "not_contains": "doesNotInclude", "starts_with": "startsWith",
    "ends_with": "endsWith", "between": "between",
}
TIME_FRAMES = {"raw", "time", "date", "week", "month", "quarter", "year", "hour", "minute", "second", "fiscal_year", "fiscal_quarter"}
MARKS = {
    "bar": "bar", "column": "bar", "stacked_bar": "bar", "grouped_bar": "bar", "barhorizontal": "bar",
    "line": "line", "area": "area", "scatter": "point", "point": "point", "pie": "arc", "donut": "arc",
}
KPI = {"kpi", "summaryvalue", "summary_value", "single_value", "singlevalue", "number", "big_number"}
TABLES = {"table", "spreadsheet", "pivot_table", "pivot", "data_table", "result"}
CUSTOM = {"vegalite", "vega-lite", "vega_lite", "custom", "code"}


def _file_kind(key: str) -> tuple[str, str]:
    """('view'|'topic'|'relationships'|'model'|'other', name) for a model file key."""
    base = re.sub(r"\.ya?ml$", "", key)
    stem = base.rsplit("/", 1)[-1]
    for kind in ("view", "topic"):
        if stem.endswith(f".{kind}"):
            return kind, stem[: -len(kind) - 1]
    if stem in ("relationships", "model"):
        return stem, stem
    return "other", stem


def _find_sql(text: str) -> str:
    """Find generated SQL in a query/run response (JSON or JSON lines)."""
    docs: list[Any] = []
    try:
        docs = [json.loads(text)]
    except ValueError:
        for line in text.splitlines():
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue
    for doc in docs:
        sql = find_key(doc, ("sql", "generatedSql", "generated_sql", "querySql"))
        if isinstance(sql, str):
            return sql
    return ""


def _dim_type(name: str, d: dict[str, Any]) -> str:
    typ = str(d.get("type") or "").lower()
    if typ in ("timestamp", "date", "datetime", "time", "number", "boolean", "string"):
        return typ
    if d.get("timeframes") or "convert_tz" in d:
        return "timestamp"
    if name.endswith("_date") or name == "date":
        return "date"
    if re.search(r"(_at|_time|_timestamp)$", name):
        return "timestamp"
    if str(d.get("format") or "").lower() in ("number", "usdcurrency", "currency", "percent", "big", "id"):
        return "number"
    return "string"


def _filters(raw: Any) -> list[NFilter]:
    out = []
    for field, cond in (raw or {}).items() if isinstance(raw, dict) else []:
        cond = cond if isinstance(cond, dict) else {"is": cond}
        for key, value in cond.items():
            op = OMNI_OPS.get(key, key)
            if op == "equals" and value is None:
                out.append(NFilter(field, "isNull"))
            elif op == "notEquals" and value is None:
                out.append(NFilter(field, "notNull"))
            else:
                out.append(NFilter(field, op, as_list(value)))
    return out


def _alias(ref: str) -> str:
    return snake(ref) or "col"


class OmniSource(Source):
    name = "omni"
    label = "Omni"
    env_vars = ("OMNI_URL", "OMNI_API_KEY")
    docs = "https://docs.omni.co/docs/API"
    manual_files = "an Omni model folder of YAML files (the model's Git repo, or a .zip of it), one folder per model"
    manual_gaps = (
        "dashboards and workbooks: Omni documents only come from the API, so no notebooks are built",
        "sample query SQL: topic sample queries have no compiled SQL",
        "usage: no view counts and no links back to Omni",
        "permissions: access grants in the model YAML are kept; who holds the user attributes is not",
    )

    def __init__(self, client: Client | None = None, model_id: str | None = None):
        self._client = client
        self._model_id = model_id

    # ------------------------------------------------------------ network

    def client(self) -> Client:
        if self._client is None:
            key = env("OMNI_API_KEY")
            self._client = Client(env("OMNI_URL"), headers={"Authorization": f"Bearer {key}"}, per_minute=60)
        return self._client

    def _paged(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"pageSize": 100}
        while True:
            data = self.client().get_json(path, params=params)
            if isinstance(data, list):
                return out + [r for r in data if isinstance(r, dict)]
            out += [r for r in as_list(data.get("records")) if isinstance(r, dict)]
            info = data.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("nextCursor"):
                return out
            params["cursor"] = info["nextCursor"]

    def _plan_sql(self, query: dict[str, Any], model_id: str) -> str:
        body = {"query": {**query, "modelId": query.get("modelId") or model_id}, "planOnly": True}
        return _find_sql(self.client().post("/api/v1/query/run", json=body).text)

    def _selected_models(self, models: list[dict[str, Any]], only: set[str] | None) -> list[dict[str, Any]]:
        wanted = self._model_id or os.environ.get("OMNI_MODEL_ID", "")
        if wanted:
            return [m for m in models if m.get("id") == wanted] or [{"id": wanted}]
        if only and any(m.get("id") in only for m in models):
            return [m for m in models if m.get("id") in only]
        shared = [m for m in models if str(m.get("modelKind", "")).upper() == "SHARED"]
        return shared or [m for m in models if str(m.get("modelKind", "")).upper() not in ("WORKBOOK", "BRANCH")]

    def inventory(self) -> Inventory:
        inv = Inventory("omni")
        base = self.client().base_url
        for m in self._paged("/api/v1/models"):
            inv.add("model", m.get("id"), m.get("name") or m.get("id", ""), extra={"kind": m.get("modelKind", "")})
        for d in self._paged("/api/v1/documents"):
            ident = d.get("identifier") or d.get("id")
            dash = bool(d.get("hasDashboard"))
            inv.add(
                "dashboard" if dash else "workbook", ident, d.get("name", ""),
                parent=((d.get("folder") or {}).get("path") or "") if isinstance(d.get("folder"), dict) else "",
                views=find_key(d, ("views", "viewCount", "view_count")),
                last_viewed=str(d.get("lastViewedAt") or d.get("updatedAt") or ""),
                url=f"{base}/{'dashboards' if dash else 'w'}/{ident}",
            )
        inv.notes.append("Last viewed falls back to updatedAt where Omni does not return view data. Export needs an Org API key.")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        store.put_json("_source.json", {"url": self.client().base_url})
        models = self._paged("/api/v1/models")
        store.put_json("models/_index.json", models)
        default_model = ""
        for m in self._selected_models(models, only):
            mid = str(m.get("id"))
            default_model = default_model or mid
            try:
                data = self.client().get_json(f"/api/v1/models/{mid}/yaml", params={"fullyResolved": "true"})
            except SourceError as exc:
                errors.append({"item": f"model {mid}", "error": str(exc)})
                continue
            files = data.get("files") or {}
            store.put_json(f"models/{mid}/_meta.json", {**m, "viewNames": data.get("viewNames"), "checksums": data.get("checksums")})
            for key, text in files.items():
                safe = "/".join(p for p in str(key).split("/") if p not in ("", ".", ".."))
                rel = safe if re.search(r"\.ya?ml$", safe) else f"{safe}.yaml"
                store.put_text(f"models/{mid}/{rel}", text if isinstance(text, str) else yaml.safe_dump(text, sort_keys=False))
                kind, name = _file_kind(safe)
                if kind != "topic" or not isinstance(text, str):
                    continue
                try:
                    topic = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    continue
                for qname, sq in self._sample_queries(topic):
                    q = sq.get("query") if isinstance(sq.get("query"), dict) else None
                    if not q:
                        continue
                    try:
                        sql = self._plan_sql({"topic": name, **q}, mid)
                        if sql:
                            store.put_text(f"models/{mid}/sample_queries/{name}__{slugify(qname)}.sql", sql.strip() + "\n")
                    except SourceError as exc:
                        errors.append({"item": f"sample query {name}/{qname}", "error": str(exc)})

        docs = self._paged("/api/v1/documents")
        store.put_json("documents/_index.json", docs)
        for d in docs:
            ident = str(d.get("identifier") or d.get("id") or "")
            if not ident or (only and not ({ident, d.get("name")} & only)):
                continue
            try:
                raw = self.client().get_json(f"/api/v1/documents/{ident}/queries")
                queries = raw if isinstance(raw, list) else as_list(raw.get("queries") or raw.get("records"))
                store.put_json(f"documents/{ident}/queries.json", queries)
            except SourceError as exc:
                errors.append({"item": f"document {ident}", "error": f"queries: {exc}"})
                queries = []
            for i, q in enumerate(queries, 1):
                if not isinstance(q, dict):
                    continue
                qid = slugify(str(q.get("id") or i))
                query = q.get("query") or {}
                if isinstance(query.get("userEditedSQL"), str):
                    store.put_text(f"documents/{ident}/sql/{qid}.sql", query["userEditedSQL"].strip() + "\n")
                    continue
                try:
                    sql = self._plan_sql(query, str(query.get("modelId") or d.get("modelId") or default_model))
                    if sql:
                        store.put_text(f"documents/{ident}/sql/{qid}.sql", sql.strip() + "\n")
                except SourceError as exc:
                    errors.append({"item": f"query {ident}/{qid}", "error": str(exc)})
            if d.get("hasDashboard"):
                try:
                    export = self.client().get_json(f"/api/unstable/documents/{ident}/export")
                except SourceError:
                    try:
                        export = self.client().post_json("/api/v1/content/export-dashboard", json={"dashboardId": ident})
                    except SourceError as exc:
                        errors.append({"item": f"dashboard {ident}", "error": f"export: {exc}"})
                        continue
                store.put_json(f"documents/{ident}/export.json", export)
        store.put_json("_errors.json", errors)

    @staticmethod
    def _sample_queries(topic: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        raw = topic.get("sample_queries") or {}
        if isinstance(raw, dict):
            return [(k, v) for k, v in raw.items() if isinstance(v, dict)]
        return [(str(v.get("name") or v.get("prompt") or i), v) for i, v in enumerate(as_list(raw), 1) if isinstance(v, dict)]

    # ------------------------------------------------------------ parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        if not path.is_dir():
            raise SourceError(f"{path.name}: Omni import accepts {self.manual_files}")
        files = {rel: p for rel, p in _manual.walk(path) if p.suffix in (".yaml", ".yml")}
        if not any(_file_kind(rel)[0] != "other" for rel in files):
            return {}
        mid = _manual.safe_name(path.name)
        return {f"models/{mid}/{rel}": p.read_bytes() for rel, p in files.items()}

    def parse(self, store: RawStore) -> Bundle:
        bundle = Bundle(tool="omni")
        tables: dict[str, NTable] = {}
        self._notes: dict[str, list[str]] = {}
        self._field_grants: list[tuple[str, str, list[str]]] = []
        grants: dict[str, dict[str, Any]] = {}
        topics: list[tuple[str, str, str, dict[str, Any]]] = []
        relationships: list[dict[str, Any]] = []
        for rel in store.glob("models/**/*.yaml") + store.glob("models/**/*.yml"):
            parts = rel.split("/")
            if len(parts) < 3:
                continue
            mid, key = parts[1], "/".join(parts[2:])
            kind, name = _file_kind(key)
            try:
                doc = yaml.safe_load(store.get_text(rel))
            except yaml.YAMLError as exc:
                bundle.warn(rel, f"invalid YAML: {exc}")
                continue
            if kind == "view" and isinstance(doc, dict):
                folder = key.rsplit("/", 1)[0] if "/" in key else ""
                t = self._view(mid, key, name, folder, doc, bundle)
                if t.name in tables:
                    bundle.warn(t.name, f"view defined twice ({rel}); keeping the first")
                    continue
                tables[t.name] = t
            elif kind == "relationships":
                relationships += [r for r in as_list(doc) if isinstance(r, dict)]
            elif kind == "model" and isinstance(doc, dict):
                grants.update(doc.get("access_grants") or {})
                if doc.get("ai_context"):
                    meta = store.get_json(f"models/{mid}/_meta.json") if store.exists(f"models/{mid}/_meta.json") else {}
                    bundle.knowledge.append(
                        KnowledgeItem(f"{meta.get('name') or mid} model context", str(doc["ai_context"]), description="Model-wide AI context from Omni",
                                      context="always", datasource=bundle.datasource, source_ref=f"omni:{mid}/{key}")
                    )
            elif kind == "topic" and isinstance(doc, dict):
                topics.append((mid, key, name, doc))
        for r in relationships:
            to = r.get("join_to_view_as") or r.get("join_to_view", "")
            if r.get("join_to_view_as"):
                bundle.warn(to, "aliased join (join_to_view_as) is modelled as a join to the base view")
                to = r.get("join_to_view", "")
            add_join(tables, to, str(r.get("on_sql", "")), str(r.get("relationship_type", "")), bundle)
        build_semantic(bundle, list(tables.values()), "omni")
        for view, notes in self._notes.items():
            t = tables[view]
            bundle.knowledge.append(
                KnowledgeItem(f"{t.label or view} field notes", f"# {t.label or view}\n\n" + "\n".join(notes),
                              description=f"Synonyms and AI context for {view} fields (Omni)",
                              datasource=bundle.datasource, source_ref=t.source_ref)
            )
        for view, field, required in self._field_grants:
            t = tables[view]
            self._grant_permissions(bundle, grants, required, "Column", f"{t.relation}.{field}", f"{t.source_ref}.{field}")
        for mid, key, name, doc in topics:
            self._topic(store, bundle, tables, grants, mid, key, name, doc)
        self._documents(store, bundle, tables)
        if store.exists("_errors.json"):
            for e in as_list(store.get_json("_errors.json")):
                bundle.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return bundle.settle_time_dimensions()

    def _view(self, mid: str, key: str, name: str, folder: str, doc: dict[str, Any], bundle: Bundle) -> NTable:
        t = NTable(
            name=doc.get("name") or name,
            table=doc.get("table_name") or name,
            schema=doc.get("schema") or folder,
            database=doc.get("catalog") or doc.get("database") or "",
            description=doc.get("description") or "",
            label=doc.get("label") or "",
            source_ref=f"omni:{mid}/{key}",
        )
        if doc.get("sql") and not doc.get("table_name"):
            bundle.warn(t.name, "SQL-defined view; materialize it as a warehouse view or dbt model and point node_relation at it")
        notes = []
        for dname, d in (doc.get("dimensions") or {}).items():
            d = d or {}
            t.dims[dname] = NField(dname, str(d.get("sql") or ""), _dim_type(dname, d), d.get("label") or "",
                                   d.get("description") or "", bool(d.get("hidden")))
            if d.get("primary_key"):
                t.primary_key = dname
            notes += self._field_notes(dname, d)
            if d.get("required_access_grants"):
                self._field_grants.append((t.name, dname, as_list(d["required_access_grants"])))
        for mname, m in (doc.get("measures") or {}).items():
            m = m or {}
            agg = str(m.get("aggregate_type") or "").lower()
            sql = str(m.get("sql") or "")
            typ = agg if agg in MF_AGGS else ("post" if not agg else agg)
            params = {}
            if agg == "percentile":
                pct = float(m.get("percentile", 50))
                params = {"percentile": pct / 100 if pct > 1 else pct, "use_discrete_percentile": False}
            src = f"aggregate_type={agg or '(none)'} sql={sql}"
            if m.get("filters"):
                src += f" filters={json.dumps(m['filters'], default=str)}"
            t.metrics[mname] = NField(mname, sql, typ, m.get("label") or "", m.get("description") or "",
                                      bool(m.get("hidden")), filters=_filters(m.get("filters")), agg_params=params, source=src)
            notes += self._field_notes(mname, m)
        if notes:
            self._notes[t.name] = notes
        return t

    @staticmethod
    def _field_notes(name: str, f: dict[str, Any]) -> list[str]:
        bits = []
        if f.get("synonyms"):
            bits.append("synonyms: " + ", ".join(str(s) for s in as_list(f["synonyms"])))
        if f.get("ai_context"):
            bits.append(str(f["ai_context"]).strip())
        return [f"- `{name}`: " + "; ".join(bits)] if bits else []

    def _grant_permissions(self, bundle: Bundle, grants: dict[str, Any], required: list[str], rtype: str, rid: str, ref: str) -> None:
        req: dict[str, list[Any]] = {}
        missing = []
        for g in required:
            spec = grants.get(g)
            if isinstance(spec, dict) and spec.get("user_attribute"):
                req.setdefault(str(spec["user_attribute"]), []).extend(as_list(spec.get("allowed_values")))
            else:
                missing.append(g)
        perm = attribute_permission(req, rtype, rid, ref, f"required_access_grants {required}", "Omni")
        if missing:
            perm.todo += f"; access grant(s) {missing} not defined in the model file"
        bundle.permissions.append(perm)

    def _topic(self, store: RawStore, bundle: Bundle, tables: dict[str, NTable], grants: dict[str, Any],
               mid: str, key: str, name: str, doc: dict[str, Any]) -> None:
        ref = f"omni:{mid}/{key}"
        base = doc.get("base_view") or name
        joins = [k for d in walk(doc.get("joins")) for k in d] if isinstance(doc.get("joins"), dict) else []
        body = [f"# {doc.get('label') or name}", ""]
        if doc.get("description"):
            body += [str(doc["description"]).strip(), ""]
        body.append(f"Base view: `{base}`" + (f"; joins: {', '.join(f'`{j}`' for j in joins)}" if joins else ""))
        if doc.get("ai_context"):
            body += ["", "## AI context", "", str(doc["ai_context"]).strip()]
        bundle.knowledge.append(
            KnowledgeItem(f"{doc.get('label') or name} topic", "\n".join(body) + "\n", description=f"Omni topic {name}",
                          datasource=bundle.datasource, source_ref=ref)
        )
        for i, af in enumerate(as_list(doc.get("access_filters"))):
            if not isinstance(af, dict) or not af.get("field"):
                continue
            view, _, field = str(af["field"]).rpartition(".")
            t = tables.get(view or base)
            src = json.dumps(af, default=str)
            if t is None or field not in t.dims:
                bundle.row_filters.append(RowFilter(f"{name}_access_filter_{i + 1}", view or base, source_expression=src,
                                                    source_ref=ref, todo=f"field {af['field']} not found in the model"))
                continue
            expr, _ = resolve_sql(t, t.dims[field].sql) if t.dims[field].sql else (field, "")
            attr = af.get("user_attribute") or field
            bundle.row_filters.append(
                RowFilter(
                    f"{name}_{field}", t.relation, f"{expr} = {{{{ user.{attr} }}}}", source_expression=src, source_ref=ref,
                    todo=f"users with {af['values_for_unfiltered']} see all rows in Omni; add them as an exempt group"
                    if af.get("values_for_unfiltered") else "",
                )
            )
        if doc.get("required_access_grants") and base in tables:
            self._grant_permissions(bundle, grants, as_list(doc["required_access_grants"]), "Table", tables[base].relation, ref)
        for qname, sq in self._sample_queries(doc):
            rel = f"models/{mid}/sample_queries/{name}__{slugify(qname)}.sql"
            sql = store.get_text(rel).strip() if store.exists(rel) else ""
            prompt = str(sq.get("prompt") or "")
            vq = VerifiedQuery(prompt or qname, sql, description=str(sq.get("description") or ""), datasource=bundle.datasource,
                               aliases=[qname] if prompt and prompt != qname else [], source_ref=f"{ref}#{qname}")
            if not sql:
                vq.sql = "-- Omni query: " + json.dumps(sq.get("query"), default=str)
                vq.todo = "no SQL generated for this Omni sample query; write it or re-run extract"
            bundle.queries.append(vq)

    # ------------------------------------------------------------ documents

    def _measures(self, tables: dict[str, NTable]) -> tuple[set[str], set[str]]:
        measures = {f"{t.name}.{m}" for t in tables.values() for m in t.metrics}
        times = {f"{t.name}.{d}" for t in tables.values() for d, f in t.dims.items() if f.type in TIME_TYPES}
        return measures, times

    def _ftype(self, ref: str, measures: set[str], times: set[str]) -> str:
        base, _, frame = ref.partition("[")
        frame = frame.rstrip("]").lower()
        if base in measures:
            return "quantitative"
        if frame:
            return "temporal" if frame in TIME_FRAMES else "ordinal"
        return "temporal" if base in times else "nominal"

    def _tile(self, q: dict[str, Any], vis: dict[str, Any], tid: str, sql: str, measures: set[str], times: set[str]) -> Tile:
        query = q.get("query") or {}
        fields = [f for f in as_list(query.get("fields")) if isinstance(f, str)]
        pivots = [f for f in as_list(query.get("pivots")) if isinstance(f, str)]
        chart = str(vis.get("chartType") or vis.get("visType") or vis.get("type") or "table").lower()
        tile = Tile(tid, q.get("name") or tid, sql="", source_type=chart, source_config=vis)
        todos: list[str] = []
        names = {f: f for f in fields}
        if sql and fields:
            body = sql.strip().rstrip(";").strip()
            if all(f'"{f}"' in body for f in fields):
                names = {f: _alias(f) for f in fields}
                cols = ",\n  ".join(f'"{f}" AS {names[f]}' for f in fields)
                sql = f"SELECT\n  {cols}\nFROM (\n{body}\n) AS omni_query"
            else:
                names = {f: _alias(f) for f in fields}
                todos.append("column aliases in the Omni SQL do not match field names; alias them as the snake_case names used below")
        tile.sql = sql
        dims = [f for f in fields if f.split("[")[0] not in measures]
        meas = [f for f in fields if f.split("[")[0] in measures]

        def enc(f: str, force: str = "") -> Encoding:
            return Encoding(names.get(f, _alias(f)), force or self._ftype(f, measures, times), title="")  # type: ignore[arg-type]

        if chart in CUSTOM or (vis.get("spec") and chart not in MARKS and chart not in KPI and chart not in TABLES):
            todos.append("custom Vega-Lite visual; port source_config spec with alt.Chart.from_dict(spec) on this tile's data")
        elif chart in MARKS:
            tile.mark = MARKS[chart]  # type: ignore[assignment]
            color = pivots[0] if pivots else (dims[1] if len(dims) > 1 else "")
            if tile.mark == "arc":
                tile.encodings = {k: v for k, v in (("theta", enc(meas[0], "quantitative") if meas else None), ("color", enc(dims[0], "nominal") if dims else None)) if v}
            else:
                x = enc(dims[0]) if dims else None
                y = enc(meas[0], "quantitative") if meas else None
                if chart == "barhorizontal" or str(vis.get("orientation", "")).lower() == "horizontal":
                    x, y = y, x
                tile.encodings = {k: v for k, v in (("x", x), ("y", y)) if v}
                if color:
                    tile.encodings["color"] = enc(color, "nominal")
            if len(meas) > 1:
                todos.append(f"plots {len(meas)} measures ({', '.join(meas)}); only the first is encoded, fold or split the rest")
        elif chart in KPI:
            tile.kind, tile.mark = "kpi", "text"
            if meas or fields:
                tile.encodings = {"text": enc((meas or fields)[0], "quantitative")}
        elif chart in TABLES:
            tile.kind, tile.mark = "table", "table"
        else:
            tile.kind = "unknown"
            todos.append(f"Omni {chart} visual has no Altair mapping; choose a mark or rebuild")
        calcs = [c for c in as_list(query.get("calculations")) if isinstance(c, dict)]
        if calcs:
            labels = ", ".join(str(c.get("label") or c.get("calc_name") or c.get("name")) for c in calcs)
            todos.append(f"Omni table calculation(s) {labels} (Excel-style); rewrite as SQL window functions and check they are in the SQL")
        if not sql:
            todos.append("no generated SQL saved; re-run extract or write SQL from the semantic layer")
        tile.todo = "; ".join(todos)
        return tile

    def _documents(self, store: RawStore, bundle: Bundle, tables: dict[str, NTable]) -> None:
        base = (store.get_json("_source.json") if store.exists("_source.json") else {}).get("url", "")
        index = {str(d.get("identifier") or d.get("id")): d for d in as_list(store.get_json("documents/_index.json"))
                 if isinstance(d, dict)} if store.exists("documents/_index.json") else {}
        measures, times = self._measures(tables)
        ids = sorted({rel.split("/")[1] for rel in store.glob("documents/*/*.json")})
        for ident in ids:
            rec = index.get(ident, {})
            queries = [q for q in as_list(store.get_json(f"documents/{ident}/queries.json")) if isinstance(q, dict)] \
                if store.exists(f"documents/{ident}/queries.json") else []
            for i, q in enumerate(queries, 1):
                q["_qid"] = slugify(str(q.get("id") or i))

            def sql_for(q: dict[str, Any]) -> str:
                rel = f"documents/{ident}/sql/{q['_qid']}.sql"
                return store.get_text(rel).strip() if store.exists(rel) else ""

            export = store.get_json(f"documents/{ident}/export.json") if store.exists(f"documents/{ident}/export.json") else None
            if export is None:
                for q in queries:
                    sql = sql_for(q)
                    if sql:
                        bundle.queries.append(VerifiedQuery(q.get("name") or q["_qid"], sql, datasource=bundle.datasource,
                                                            source_ref=f"omni:document/{ident}#{q['_qid']}"))
                    else:
                        bundle.warn(f"query {ident}/{q['_qid']}", "workbook query without SQL; not migrated")
                continue
            dash_obj = export.get("dashboard") or {}
            doc = export.get("document") or {}
            name = doc.get("name") or dash_obj.get("name") or rec.get("name") or ident
            dash = Dashboard(name=name, slug=slugify(name), description=str(doc.get("description") or dash_obj.get("description") or ""),
                             datasource=bundle.datasource, source_ref=f"omni:dashboard/{ident}",
                             url=f"{base}/dashboards/{ident}" if base else "", views=find_key(rec, ("views", "viewCount", "view_count")))
            presentations = [m.get("queryPresentation") or m for m in as_list(find_key(dash_obj, ("queryPresentationCollectionMemberships",)))
                             if isinstance(m, dict)]
            if not presentations:
                presentations = [d for d in walk(dash_obj) if "visConfig" in d]
            by_id = {str(q.get("id")): q for q in queries}
            by_name = {q.get("name"): q for q in queries}
            key_to_tile: dict[str, str] = {}
            used_q: set[str] = set()
            used_ids: set[str] = set()
            for n, p in enumerate(presentations):
                q = by_id.get(str(p.get("queryId"))) or by_id.get(str(p.get("id"))) or by_name.get(p.get("name"))
                if q is None:
                    qj = (p.get("query") or {}).get("queryJson") or p.get("query")
                    q = {"id": p.get("id"), "name": p.get("name"), "query": qj if isinstance(qj, dict) else {}, "_qid": slugify(str(p.get("id") or n))}
                tid = self._unique(snake(p.get("name") or q.get("name") or f"tile_{n + 1}"), used_ids)
                used_q.add(q["_qid"])
                tile = self._tile({**q, "name": p.get("name") or q.get("name")}, p.get("visConfig") or q.get("visConfig") or {}, tid,
                                  sql_for(q), measures, times)
                dash.tiles.append(tile)
                for k in (p.get("id"), p.get("queryId"), n, str(n)):
                    if k is not None:
                        key_to_tile[str(k)] = tid
            for n, d in enumerate(walk(dash_obj)):
                text = d.get("markdown") if isinstance(d.get("markdown"), str) else (
                    d.get("text") if str(d.get("type", "")).lower() in ("markdown", "text") and isinstance(d.get("text"), str) else None)
                if text is None:
                    continue
                tid = self._unique(snake(d.get("title") or d.get("name") or f"text_{n}"), used_ids)
                dash.tiles.append(Tile(tid, d.get("title") or "", kind="text", text=text, source_type="markdown"))
                for k in (d.get("id"), d.get("i")):
                    if k is not None:
                        key_to_tile[str(k)] = tid
            layouts = find_key(dash_obj, ("layouts",))
            grid_items = []
            if isinstance(layouts, dict) and layouts:
                items = layouts.get("lg") or next(iter(layouts.values()))
                for it in as_list(items):
                    if isinstance(it, dict) and str(it.get("i")) in key_to_tile:
                        grid_items.append((key_to_tile[str(it.get("i"))], float(it.get("x") or 0), float(it.get("y") or 0)))
            placed = {g[0] for g in grid_items}
            dash.layout = grid_rows(grid_items) + [[t.id] for t in dash.tiles if t.id not in placed]
            for fid, f in (find_key(dash_obj, ("filterConfig",)) or {}).items():
                if not isinstance(f, dict):
                    continue
                field = str(f.get("fieldName") or f.get("field") or fid)
                typ = str(f.get("type") or "").lower()
                kind = "date_range" if typ in ("date", "timestamp", "time") or "TIME" in str(f.get("kind", "")).upper() else \
                    "number" if typ == "number" else "value"
                values = as_list(f.get("values"))
                default: Any = values[0] if len(values) == 1 else (values or None)
                if kind == "date_range" and f.get("left_side"):
                    default = f"{f.get('left_side')} {f.get('right_side', '')}".strip()
                dash.filters.append(DashboardFilter(snake(f.get("label") or field), field, default, kind))  # type: ignore[arg-type]
            bundle.dashboards.append(dash)
            for q in queries:
                if q["_qid"] in used_q:
                    continue
                sql = sql_for(q)
                if sql:
                    bundle.queries.append(VerifiedQuery(q.get("name") or q["_qid"], sql, description=f"Workbook tab in {name}",
                                                        datasource=bundle.datasource, source_ref=f"omni:document/{ident}#{q['_qid']}"))

    @staticmethod
    def _unique(base: str, used: set[str]) -> str:
        tid, n = base or "tile", 2
        while tid in used:
            tid, n = f"{base}_{n}", n + 1
        used.add(tid)
        return tid

