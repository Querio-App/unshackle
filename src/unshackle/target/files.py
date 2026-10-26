"""Catalog, knowledge, verified queries and evals writers."""

from __future__ import annotations

import re
from typing import Any

import yaml

from unshackle.ir import Bundle, KnowledgeItem, Table, VerifiedQuery, slugify, todo_reasons
from unshackle.target.layout import Layout
from unshackle.target.semantic import dump_yaml

def one_line_comment(value: str) -> str:
    return " ".join(str(value).splitlines())


def render_table(table: Table, existing: str | None) -> str:
    """One YAML file per table. Anything a person added to the file wins."""
    old: dict[str, Any] = (yaml.safe_load(existing) if existing else {}) or {}
    old_cols = {c.get("name"): c for c in old.get("columns", []) if isinstance(c, dict)}
    cols = []
    for c in table.columns:
        fresh: dict[str, Any] = {"name": c.name}
        if c.type:
            fresh["type"] = c.type
        if c.description:
            fresh["description"] = c.description
        if c.label and c.label != c.name:
            fresh["label"] = c.label
        if c.hidden:
            fresh["visible"] = False
        prev = old_cols.get(c.name, {})
        merged = {**fresh, **{k: v for k, v in prev.items() if v not in (None, "")}}
        if c.type:
            merged["type"] = c.type
        cols.append(merged)
    known = {c.name for c in table.columns}
    cols += [prev for name, prev in old_cols.items() if name not in known]
    fresh_doc: dict[str, Any] = {"name": table.name}
    if table.description:
        fresh_doc["description"] = table.description
    if table.source_ref:
        fresh_doc["source"] = table.source_ref
    doc = {**fresh_doc, **{k: v for k, v in old.items() if k != "columns" and v not in (None, "")}}
    doc["columns"] = cols
    return dump_yaml(doc)


def safe_segment(value: str) -> str:
    """A single path segment: no separators, no leading dots."""
    seg = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._") or "unnamed"
    return seg


def unique_rel(files: dict, rel: str) -> str:
    if rel not in files:
        return rel
    stem, dot, ext = rel.rpartition(".")
    n = 2
    while f"{stem}-{n}.{ext}" in files:
        n += 1
    return f"{stem}-{n}.{ext}"


def catalog_files(bundle: Bundle, layout: Layout, read) -> dict[str, str]:
    files = {}
    for t in bundle.tables:
        rel = layout.rel(
            "catalog",
            safe_segment(t.datasource or bundle.datasource),
            safe_segment(t.schema or "main"),
            f"{safe_segment(t.name)}.yml",
        )
        files[rel] = render_table(t, read(rel))
    return files


def _frontmatter_md(fields: dict[str, Any]) -> str:
    clean = {k: v for k, v in fields.items() if v not in ("", None, [])}
    return "---\n" + yaml.safe_dump(clean, sort_keys=False, allow_unicode=True) + "---\n\n"


def render_knowledge(item: KnowledgeItem, tool: str) -> str:
    fm = _frontmatter_md(
        {
            "name": item.name,
            "description": item.description,
            "context": item.context,
            "datasource": item.datasource,
            "source": item.source_ref or tool,
        }
    )
    return fm + item.body.strip() + "\n"


def knowledge_files(bundle: Bundle, layout: Layout) -> dict[str, str]:
    files = {}
    for k in bundle.knowledge:
        files[unique_rel(files, layout.rel("knowledge", f"{slugify(k.name)}.md"))] = render_knowledge(k, bundle.tool)
    return files


def render_query(q: VerifiedQuery, tool: str) -> str:
    prefix = "--" if q.language == "sql" else "#"
    fields = {
        "name": q.name,
        "description": " ".join(q.description.split()),
        "datasource": q.datasource,
        "metric": "true" if q.metric else "",
        "aliases": ", ".join(q.aliases),
        "owner": q.owner,
        "source": q.source_ref or tool,
    }
    lines = [f"{prefix} {k}: {one_line_comment(v)}" for k, v in fields.items() if v]
    body = q.sql.strip()
    if q.todo:
        todos = [f"{prefix} TODO(unshackle): {r}" if q.language == "python" else f"/* TODO(unshackle): {r.replace('*/', '* /')} */"
                 for r in todo_reasons(q.todo)]
        body = "\n".join(todos + [body])
    return "\n".join(lines) + "\n" + body + "\n"


def query_files(bundle: Bundle, layout: Layout) -> dict[str, str]:
    files = {}
    for q in bundle.queries:
        ext = "sql" if q.language == "sql" else "py"
        files[unique_rel(files, layout.rel("queries", f"{slugify(q.name)}.{ext}"))] = render_query(q, bundle.tool)
    return files


def render_eval(case: dict[str, Any]) -> str:
    return dump_yaml(case)
