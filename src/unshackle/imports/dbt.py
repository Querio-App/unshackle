"""Offline plain dbt import. Never renders Jinja, loads profiles or runs dbt."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import yaml

from unshackle.ir import (
    Bundle, Column, Dimension, Entity, KnowledgeItem, Measure, Metric,
    SemanticModel, Table, VerifiedQuery, join_todos,
)
from unshackle.sources._dbt import load_yaml, nodes

REF = re.compile(r"^(?:\{\{\s*)?ref\(\s*['\"]([\w]+)['\"]\s*\)(?:\s*\}\})?$")
AGGS = {"sum", "min", "max", "average", "count", "count_distinct", "median"}
RELATION_CONFIG = {"alias", "schema", "database", "materialized", "enabled", "quoting"}


def _project_relation_config(project: dict, relative: str, model_paths: list[str]) -> list[dict]:
    """Collect applicable literal config scopes without evaluating dbt/Jinja precedence."""
    scopes = []
    tree = project.get("models") or {}
    path = Path(relative)
    roots = [Path(p) for p in model_paths if path.is_relative_to(Path(p))]
    local = path.relative_to(max(roots, key=lambda p: len(p.parts))) if roots else path
    parts = [str(project.get("name") or ""), *local.with_suffix("").parts]
    scope = "models"
    for part in [None, *parts]:
        if not isinstance(tree, dict):
            break
        if part is not None:
            tree = tree.get(part) or {}
            scope += f".{part}"
        if not isinstance(tree, dict):
            break
        config = {k: v for k, v in tree.items() if str(k).removeprefix("+") in RELATION_CONFIG}
        if config:
            scopes.append({"scope": scope, "config": config})
    if project.get("quoting"):
        scopes.append({"scope": "quoting", "config": project["quoting"]})
    return scopes


def _source(value: dict) -> str:
    return yaml.safe_dump(value, sort_keys=True).strip()


def _jinja(value: str) -> bool:
    return any(tag in value for tag in ("{{", "{%", "{#"))


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("dbt path escapes project root")
    return path


def _pending(bundle: Bundle, ref: str, node: dict, why: str) -> None:
    bundle.knowledge.append(KnowledgeItem(
        name=f"dbt source {ref}", source_ref=f"dbt:{ref}",
        body=f"TODO(unshackle): {why}\n\n```yaml\n{_source(node)}\n```",
    ))


def _semantics(bundle: Bundle, documents: list[tuple[str, dict]], pending_relations: dict[str, str]) -> None:
    counts = Counter(str(raw["name"]) for _, doc in documents for raw in nodes(doc, "semantic_models"))
    for rel, doc in documents:
        for raw in nodes(doc, "semantic_models"):
            name = str(raw["name"])
            ref = f"{rel}#semantic_models/{name}"
            match = REF.fullmatch(str(raw.get("model", "")).strip())
            sm = SemanticModel(name, match[1] if match else name,
                               description=raw.get("description") or "", datasource=bundle.datasource,
                               source_ref=f"dbt:{ref}",
                               default_time_dimension=(raw.get("defaults") or {}).get("agg_time_dimension", ""))
            problems = []
            if counts[name] > 1:
                sm.name = f"{name}_{hashlib.sha256(ref.encode()).hexdigest()[:8]}"
                problems.append("duplicate semantic model name; reconcile the preserved source declarations")
            if not match:
                problems.append("semantic model relation is not a single local ref")
            else:
                if match[1] in pending_relations:
                    problems.append(pending_relations[match[1]])
                tables = [t for t in bundle.tables if Path(t.source_ref.removeprefix("dbt:")).stem == match[1]]
                if len(tables) != 1:
                    problems.append("dbt ref does not resolve to one imported SQL model")
                elif tables[0].schema or tables[0].name != match[1]:
                    relation = ".".join(p for p in (tables[0].schema, tables[0].name) if p)
                    problems.append(
                        f"dbt ref {match[1]!r} resolves to {relation!r}; the shared semantic model cannot "
                        "represent a different physical relation and dbt ref independently; reconcile before enabling"
                    )
            if set(raw) - {"name", "model", "description", "defaults", "entities", "dimensions", "measures", "label", "config"}:
                problems.append("unsupported semantic model options")
            if set(raw.get("defaults") or {}) - {"agg_time_dimension"}:
                problems.append("unsupported semantic model defaults")
            for e in nodes(raw, "entities"):
                if e.get("type") not in {"primary", "foreign", "unique", "natural"} or set(e) - {"name", "type", "expr", "description", "label"} or _jinja(str(e.get("expr", ""))):
                    problems.append("unsupported entity definition")
                else:
                    sm.entities.append(Entity(str(e["name"]), e["type"], str(e.get("expr") or "")))
            for d in nodes(raw, "dimensions"):
                params = d.get("type_params") or {}
                todo = ""
                if d.get("type") not in {"categorical", "time"} or set(d) - {"name", "type", "expr", "description", "label", "type_params"} or set(params) - {"time_granularity"} or _jinja(str(d.get("expr", ""))):
                    todo = "unsupported dimension semantics or unresolved Jinja"
                sm.dimensions.append(Dimension(str(d["name"]), d.get("type", "categorical"),
                    expr=str(d.get("expr") or ""), description=d.get("description") or "",
                    label=d.get("label") or "", time_granularity=params.get("time_granularity", ""),
                    source_expression=_source(d), source_language="MetricFlow", todo=todo))
            for m in nodes(raw, "measures"):
                todo = ""
                if m.get("agg") not in AGGS or set(m) - {"name", "agg", "expr", "description", "label", "create_metric"} or _jinja(str(m.get("expr", ""))):
                    todo = "unsupported measure semantics or unresolved Jinja"
                sm.measures.append(Measure(str(m["name"]), agg=m.get("agg", "sum"),
                    expr=str(m.get("expr", m["name"])), description=m.get("description") or "",
                    label=m.get("label") or "", source_expression=_source(m), source_language="MetricFlow", todo=todo))
                if m.get("create_metric"):
                    bundle.metrics.append(Metric(str(m["name"]), measure=str(m["name"]), semantic_model=name,
                                                 source_expression=_source(m), source_language="MetricFlow", todo=todo))
            if problems:
                sm.todo = join_todos(problems)
                _pending(bundle, ref, raw, sm.todo)
            bundle.semantic_models.append(sm)
    for rel, doc in documents:
        for raw in nodes(doc, "metrics"):
            params = raw.get("type_params") or {}
            measure = params.get("measure", "")
            supported = (raw.get("type") == "simple" and isinstance(measure, str) and bool(measure)
                         and not set(params) - {"measure"}
                         and not set(raw) - {"name", "type", "label", "description", "type_params", "config"})
            owners = [sm.name for sm in bundle.semantic_models if any(m.name == measure for m in sm.measures)]
            todo = "" if supported else "unsupported metric semantics; translate from preserved MetricFlow source"
            if len(owners) != 1:
                todo = join_todos([todo, "measure does not resolve to one semantic model"])
            if todo:
                _pending(bundle, f"{rel}#metrics/{raw['name']}", raw, todo)
            bundle.metrics.append(Metric(str(raw["name"]), measure=measure if isinstance(measure, str) else "",
                label=raw.get("label") or "", description=raw.get("description") or "",
                semantic_model=owners[0] if len(owners) == 1 else "", source_expression=_source(raw),
                source_language="MetricFlow", source_ref=f"dbt:{rel}#metrics/{raw['name']}", todo=todo))


def parse(path: Path) -> Bundle:
    root = Path(path).resolve()
    project_file = root / "dbt_project.yml"
    if not project_file.is_file():
        raise ValueError("dbt import needs a project directory containing dbt_project.yml")
    project = load_yaml(project_file.read_text(encoding="utf-8"))
    bundle = Bundle(tool="dbt")
    model_paths = project.get("model-paths", ["models"])
    if not isinstance(model_paths, list) or not all(isinstance(p, str) for p in model_paths):
        raise ValueError("dbt model-paths must be a list of relative paths")
    paths = set()
    for folder in model_paths:
        directory = _inside(root, folder)
        paths.update(p for p in directory.rglob("*") if p.suffix in {".yml", ".yaml", ".sql"} and p.is_file())
    documents, sql_files = [], []
    for file in sorted(paths):
        rel = file.relative_to(root).as_posix()
        _inside(root, rel)
        text = file.read_text(encoding="utf-8")
        if text.startswith("# Generated by unshackle") or text.startswith("-- dbt:") or file.name.endswith(".ossie.yaml"):
            continue
        if file.suffix == ".sql":
            sql_files.append((rel, text))
        else:
            try:
                documents.append((rel, load_yaml(text)))
            except yaml.YAMLError:
                bundle.warn(rel, "invalid YAML; retained as reference with TODO")
                _pending(bundle, rel, {"source_expression": text}, "invalid YAML; repair source")
    target = _inside(root, str(project.get("target-path") or "target"))
    manifest_file = _inside(root, str(target.relative_to(root) / "manifest.json"))
    manifest = json.loads(manifest_file.read_text()) if manifest_file.is_file() else {}
    by_path: dict[str, list[dict]] = {}
    for node in (manifest.get("nodes") or {}).values():
        if node.get("resource_type") == "model" and node.get("package_name") == project.get("name"):
            by_path.setdefault(node.get("original_file_path", ""), []).append(node)
    docs: dict[str, list[tuple[str, dict]]] = {}
    for rel, doc in documents:
        for model in nodes(doc, "models"):
            docs.setdefault(str(model["name"]), []).append((rel, model))
            if model.get("semantic_model") or model.get("metrics") or any(c.get("entity") or c.get("dimension") for c in nodes(model, "columns")):
                _pending(bundle, f"{rel}#models/{model['name']}", model,
                         "embedded semantic annotations need translation; legacy semantic_models are supported")
    counts = Counter(Path(rel).stem for rel, _ in sql_files)
    consumed = set()
    pending_relations = {}
    for rel, raw_sql in sql_files:
        name = Path(rel).stem
        matches = by_path.get(rel, [])
        node = matches[0] if len(matches) == 1 else {}
        definitions = docs.get(name, [])
        model = definitions[0][1] if len(definitions) == 1 and counts[name] == 1 else {}
        if model:
            consumed.add(name)
        cfg = model.get("config") or {}
        schema = str(node.get("schema") or cfg.get("schema") or "")
        alias = str(node.get("alias") or cfg.get("alias") or name)
        identity = name if counts[name] == 1 else f"{name}_{hashlib.sha256(rel.encode()).hexdigest()[:8]}"
        todo = ""
        relation_source = {}
        project_config = _project_relation_config(project, rel, model_paths)
        inline_config = [tag for tag in re.findall(r"\{\{.*?\}\}|\{%.*?%\}", raw_sql, re.S)
                         if re.search(r"\bconfig\s*\(", tag)]
        properties_config = {k: v for k, v in cfg.items() if k in RELATION_CONFIG}
        if not node:
            if project_config:
                relation_source["dbt_project.yml"] = project_config
            if inline_config:
                relation_source[rel] = inline_config
            if properties_config:
                relation_source["model.config"] = properties_config
        if (node.get("config") or {}).get("materialized") == "ephemeral":
            relation_source["manifest.config"] = {k: v for k, v in node["config"].items() if k in RELATION_CONFIG}
        if relation_source:
            reason = "dbt relation/materialization configuration is unresolved; use compiled manifest metadata or reconcile before enabling"
            pending_relations[name] = reason
            todo = reason
            _pending(bundle, f"{rel}#relation-config", relation_source, reason)
        if counts[name] > 1 and not node:
            alias = identity
            todo = "duplicate model name; relation and documentation require disambiguation"
        sql = node.get("compiled_code") or node.get("compiled_sql") or ""
        if not sql:
            compiled = _inside(root, f"{target.relative_to(root).as_posix()}/compiled/{project.get('name', '')}/{rel}")
            sql = compiled.read_text() if compiled.is_file() else raw_sql
        if _jinja(sql):
            todo = join_todos([todo, "unresolved dbt Jinja; compile this model before using its SQL"])
        if cfg.get("schema") and not node:
            todo = join_todos([todo, "schema is configured but not resolved; import a manifest to confirm the warehouse relation"])
        columns = dict(node.get("columns") or {})
        for c in nodes(model, "columns"):
            columns[c["name"]] = {**columns.get(c["name"], {}), **c}
        bundle.tables.append(Table(bundle.datasource, schema, alias,
            description=join_todos([model.get("description") or node.get("description") or "",
                                   f"TODO(unshackle): {pending_relations[name]}" if name in pending_relations else ""]),
            columns=[Column(str(n), str(c.get("data_type") or ""), str(c.get("description") or "")) for n, c in sorted(columns.items())],
            source_ref=f"dbt:{rel}"))
        bundle.queries.append(VerifiedQuery(f"dbt {identity}", str(sql), datasource=bundle.datasource,
            source_ref=f"dbt:{rel}", description="Reference model SQL; importing does not execute or verify it.", todo=todo))
    for name, definitions in sorted(docs.items()):
        if name in consumed:
            continue
        for rel, model in definitions:
            _pending(bundle, f"{rel}#models/{name}", model, "model documentation could not be matched to one SQL model")
    _semantics(bundle, documents, pending_relations)
    return bundle.settle_time_dimensions()
