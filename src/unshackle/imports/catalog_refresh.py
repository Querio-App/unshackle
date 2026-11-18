"""Warehouse-only three-way catalog refresh against a deterministic generated snapshot."""
from __future__ import annotations

import json
from copy import deepcopy

import yaml

from unshackle.ir import Bundle
from unshackle.target.files import render_table, safe_segment
from unshackle.target.layout import Layout
from unshackle.target.semantic import dump_yaml

SNAPSHOT = "migration/warehouse-schema.json"


def _merge(old: dict, current: dict, fresh: dict) -> dict:
    """Replace only values still equal to the previous machine value."""
    out = deepcopy(current)
    for key in sorted(old.keys() | fresh.keys()):
        if key not in current or (key in old and current[key] == old[key]):
            if key in fresh:
                out[key] = deepcopy(fresh[key])
            else:
                out.pop(key, None)
    return out


def reconcile(old: dict, current: dict, fresh: dict) -> dict:
    old, current, fresh = deepcopy(old), deepcopy(current), deepcopy(fresh)
    previous = {c["name"]: c for c in old.pop("columns", [])}
    existing = {c["name"]: c for c in current.pop("columns", [])}
    incoming = {c["name"]: c for c in fresh.pop("columns", [])}
    out = _merge(old, current, fresh)
    columns = []
    retired = {c["name"]: c for c in out.pop("removed_columns", [])}
    for name in [*existing, *sorted(incoming.keys() - existing.keys())]:
        if name in incoming:
            columns.append(_merge(previous.get(name, {}), existing.get(name, {}), incoming[name]))
        elif name not in previous:
            columns.append(existing[name])  # a person or another import owns this column
        else:
            annotation = _merge(previous[name], existing[name], {})
            annotation.pop("name", None)
            if annotation:
                retired[name] = {"name": name, **annotation,
                                 "todo": "TODO(unshackle): column removed from warehouse; review retained annotations"}
    out["columns"] = columns
    if retired:
        out["removed_columns"] = [retired[n] for n in sorted(retired)]
    return out


def catalog_files(bundle: Bundle, layout: Layout, read) -> dict[str, str]:
    """Called by target.files only for warehouse imports; all writes still use Target."""
    saved = json.loads(read(SNAPSHOT) or '{"version": 1, "tables": {}}')
    if saved.get("version") != 1:
        raise ValueError("Unsupported warehouse schema snapshot version")
    previous = saved["tables"]
    fresh = {}
    for table in bundle.tables:
        rel = layout.rel("catalog", safe_segment(table.datasource or bundle.datasource),
                         safe_segment(table.schema or "main"), f"{safe_segment(table.name)}.yml")
        if rel in fresh:
            raise ValueError("Warehouse identifiers collide after filename normalization")
        fresh[rel] = yaml.safe_load(render_table(table, None))
    files = {}
    for rel, doc in sorted(fresh.items()):
        current = yaml.safe_load(read(rel) or "") or {}
        files[rel] = dump_yaml(reconcile(previous.get(rel, {}), current, doc))
    # Preserve annotations on removed tables without advertising their old generated schema.
    # Paths are read through Target's confinement check, even when the snapshot was edited.
    retained = []
    for rel in saved.get("retained_paths", []):
        if rel not in fresh and rel not in previous:
            content = read(rel)
            if content is not None:
                files[rel] = content
                retained.append(rel)
    for rel in sorted(previous.keys() - fresh.keys()):
        current = yaml.safe_load(read(rel) or "") or {}
        residual = reconcile(previous[rel], current, {})
        if any(v for k, v in residual.items() if k != "columns") or residual.get("columns"):
            residual["name"] = current.get("name", previous[rel].get("name"))
            residual["todo"] = "TODO(unshackle): table removed from warehouse; review retained annotations"
            files[rel] = dump_yaml(residual)
            retained.append(rel)
    files[SNAPSHOT] = json.dumps({"version": 1, "tables": fresh, "retained_paths": sorted(retained)}, indent=2, sort_keys=True) + "\n"
    return files
