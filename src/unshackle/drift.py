"""Deterministic import refresh and conservative schema/reference drift reports.

Run with ``python -m unshackle.drift``. GitHub PR creation belongs to the example
workflow, not this module. Imported Python and notebook cells are never executed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from unshackle import build
from unshackle.imports import parse
from unshackle.imports.catalog_refresh import SNAPSHOT
from unshackle.mcp.repository import Repository


def schema_diff(before: dict, after: dict) -> list[dict]:
    """Names changing are removal + addition; never infer a rename."""
    changes = []
    old, new = before.get("tables", {}), after.get("tables", {})
    for path in sorted(old.keys() | new.keys()):
        if path not in old or path not in new:
            changes.append({"path": path, "kind": "table_added" if path in new else "table_removed"})
            continue
        previous = {c["name"]: c for c in old[path].get("columns", [])}
        current = {c["name"]: c for c in new[path].get("columns", [])}
        for name in sorted(previous.keys() | current.keys()):
            if previous.get(name) != current.get(name):
                kind = "column_added" if name not in previous else "column_removed" if name not in current else "column_changed"
                changes.append({"path": path, "kind": kind, "column": name,
                                "before": previous.get(name), "after": current.get(name)})
        if {k: v for k, v in old[path].items() if k != "columns"} != {k: v for k, v in new[path].items() if k != "columns"}:
            changes.append({"path": path, "kind": "table_metadata_changed"})
    return changes


def _query_references(sql: str, snapshot: dict, *, dialect: str | None = None) -> list[dict]:
    """Report provably missing columns in concrete tables; mark unresolved scopes uncertain."""
    if any(token in sql for token in ("{{", "{%", "{#")):
        return [{"kind": "uncertain", "reason": "unresolved template SQL"}]
    try:
        expressions = sqlglot.parse(sql, read=dialect)
        if len(expressions) != 1 or not isinstance(expressions[0], exp.Query):
            return [{"kind": "uncertain", "reason": "not one static SELECT/WITH query"}]
        normalize_identifiers(expressions[0], dialect=dialect)
        scopes = traverse_scope(expressions[0])
    except (sqlglot.errors.SqlglotError, ValueError):
        return [{"kind": "uncertain", "reason": "SQL could not be parsed for this dialect"}]
    tables = snapshot.get("tables", {})
    findings = []
    if any(join.args.get("using") for join in expressions[0].find_all(exp.Join)):
        findings.append({"kind": "uncertain", "reason": "JOIN USING keys require join-side lineage; validate each key against both inputs"})
    for scope in scopes:
        sources = {}
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, Scope):
                sources[alias] = None  # derived projection requires lineage, not a physical column guess
                continue
            if not isinstance(source, exp.Table) or source.alias_column_names:
                sources[alias] = None
                continue
            matches = [(p, doc) for p, doc in tables.items()
                       if doc["name"] == source.name and (not source.db or Path(p).parent.name == source.db)]
            if source.catalog or len(matches) != 1:
                sources[alias] = None
                findings.append({"kind": "uncertain", "reason": f"table {source.sql()} is absent or ambiguous in snapshot"})
            else:
                path, doc = matches[0]
                sources[alias] = (path, {c["name"] for c in doc.get("columns", [])})
        for column in scope.columns:
            if column.is_star:
                continue
            candidates = [sources.get(column.table)] if column.table else list(sources.values())
            if not candidates or any(c is None for c in candidates):
                findings.append({"kind": "uncertain", "reason": f"column {column.sql()} uses an unresolved or derived scope"})
                continue
            if not any(column.name in columns for _, columns in candidates):
                if not column.table and column.name in sources:
                    findings.append({"kind": "uncertain", "reason": f"{column.name} may be a whole-row relation reference"})
                    continue
                if dialect in (None, "postgres", "postgresql") and column.name in {"ctid", "tableoid", "xmin", "cmin", "xmax", "cmax"}:
                    findings.append({"kind": "uncertain", "reason": f"{column.name} may be a PostgreSQL system column absent from catalog metadata"})
                    continue
                findings.append({"kind": "missing_column", "column": column.name,
                                 "tables": sorted(path for path, _ in candidates)})
    return sorted({json.dumps(f, sort_keys=True): f for f in findings}.values(), key=lambda f: json.dumps(f, sort_keys=True))


def query_references(sql: str, snapshot: dict, *, dialect: str | None = None) -> list[dict]:
    try:
        return _query_references(sql, snapshot, dialect=dialect)
    except (sqlglot.errors.SqlglotError, ValueError, KeyError, TypeError):
        return [{"kind": "uncertain", "reason": "unsupported or ambiguous SQL scope/alias structure"}]


def _sql_datasource(content: str) -> str:
    """Only read render_query's leading comment header, never string literals in SQL."""
    for line in content.splitlines():
        if not line.strip():
            continue
        if not line.startswith("--"):
            break
        match = re.fullmatch(r"-- datasource:\s*(.+)", line)
        if match:
            return match[1].strip()
    return ""


def reference_report(repo: Path, snapshot: dict, *, dialect: str | None = None) -> list[dict]:
    view = Repository(repo)
    findings = []
    for path in view.files():
        suffix = Path(path).suffix
        if suffix not in {".sql", ".ipynb", ".py"}:
            continue
        result = view.read(path)
        if suffix == ".sql":
            queries = [("", result["content"])]
        elif suffix == ".ipynb":
            queries = [(tile["id"], tile["sql"]) for tile in result["tiles"]]
        else:
            queries = []
            findings.append({"path": path, "kind": "uncertain", "reason": "Python reference file is not executed; import SQL literals as notebooks"})
        if result["has_todo"]:
            findings.append({"path": path, "kind": "uncertain", "reason": "source has TODO(unshackle); unresolved expressions may reference additional columns"})
        datasource = (_sql_datasource(result["content"]) if suffix == ".sql" else "") or result.get("datasource") or "warehouse"
        if datasource != "warehouse":
            findings.append({"path": path, "kind": "uncertain", "reason": "query belongs to another datasource"})
            continue
        for tile, sql in queries:
            for finding in query_references(sql, snapshot, dialect=dialect):
                findings.append({"path": path, **({"tile": tile} if tile else {}), **finding})
    return findings


def _confined(repo: Path, relative: str) -> Path:
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or "\n" in relative or "\r" in relative:
        raise ValueError("drift paths must be relative and confined to the repository")
    result = (repo / p).resolve()
    if result != repo and repo not in result.parents:
        raise ValueError("drift path escapes repository")
    return result


def _snapshot(repo: Path) -> dict:
    file = _confined(repo, SNAPSHOT)
    value = json.loads(file.read_text()) if file.exists() else {"version": 1, "tables": {}}
    if value.get("version") != 1 or not isinstance(value.get("tables"), dict):
        raise ValueError("unsupported warehouse snapshot")
    return value


def _artifacts(repo: Path) -> dict[str, bytes]:
    view = Repository(repo)
    paths = set(view.files()) | {SNAPSHOT, "migration/unshackle.lock", ".dbtignore"}
    return {p: file.read_bytes() for p in sorted(paths) if (file := _confined(repo, p)).is_file()}


def refresh(repo: Path, config: dict) -> dict:
    """Refresh configured imports once each; return report and exact changed artifact paths."""
    repo = repo.resolve()
    imports = config.get("imports", [])
    sources = [i.get("source") for i in imports]
    if sources.count("warehouse") != 1 or len(sources) != len(set(sources)):
        raise ValueError("configure warehouse exactly once and each other source at most once")
    if set(config) - {"imports", "dialect"}:
        raise ValueError("unknown drift config fields; credentials belong in the environment")
    specs = []
    for item in imports:
        if set(item) - {"source", "paths"} or item.get("source") not in {"warehouse", "dbt", "sql"}:
            raise ValueError("unsupported drift import configuration")
        paths = item.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError("import paths must be a list of strings")
        if (item["source"] == "warehouse" and paths or
                item["source"] == "dbt" and len(paths) != 1 or
                item["source"] == "sql" and not paths):
            raise ValueError("warehouse takes no paths, dbt one project, and sql one or more paths")
        specs.append((item["source"], [_confined(repo, p) for p in paths]))
    old = _snapshot(repo)
    before = _artifacts(repo)
    # Per-run build summaries are operational output, not schema drift. Keep their
    # previous bytes so scheduled refreshes do not open report-only pull requests.
    transient = {p: file.read_bytes() if file.exists() else None
                 for p in ("migration/report.md", "migration/last_build.json")
                 for file in [_confined(repo, p)]}
    try:
        for source, paths in sorted(specs, key=lambda item: item[0] != "warehouse"):
            bundle = parse(source, paths, url=os.environ.get("UNSHACKLE_WAREHOUSE_URL"))
            result = build.apply(bundle, repo)
            if result.writes.kept_hand_edits and SNAPSHOT in result.writes.kept_hand_edits:
                raise ValueError("warehouse snapshot is hand edited; reconcile it before refreshing drift")
    finally:
        for p, content in transient.items():
            file = _confined(repo, p)
            if content is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(content)
    new = _snapshot(repo)
    after = _artifacts(repo)
    changed = [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
    return {"changes": schema_diff(old, new), "references": reference_report(repo, new, dialect=config.get("dialect")),
            "paths": changed, "changed": bool(changed)}


def markdown(report: dict) -> str:
    lines = ["# Warehouse drift", "", "Names are compared literally. Removed + added columns may represent a rename; no rename is inferred.", ""]
    if not report["changes"]:
        lines += ["No warehouse schema changes.", ""]
    for change in report["changes"]:
        label = f"{change['path']}" + (f" · {change['column']}" if "column" in change else "")
        lines.append(f"- {change['kind']}: `{label}`")
        if change["kind"] == "column_changed":
            lines.append(f"  - Before: `{json.dumps(change['before'], sort_keys=True)}`; after: `{json.dumps(change['after'], sort_keys=True)}`")
    lines += ["", "## Query references", ""]
    for finding in report["references"]:
        where = finding["path"] + (f"#{finding['tile']}" if finding.get("tile") else "")
        detail = f"missing column {finding['column']} in {', '.join(finding['tables'])}" if finding["kind"] == "missing_column" else f"uncertain: {finding['reason']}"
        lines.append(f"- `{where}`: {detail}")
    if not report["references"]:
        lines.append("No missing columns found in statically resolvable references. This is not execution or parity verification.")
    lines += ["", "## Changed artifacts", "", *[f"- `{p}`" for p in report["paths"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="migration/imports.json")
    parser.add_argument("--report", type=Path, required=True, help="write PR body outside the repository")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.report.resolve().is_relative_to(repo):
        parser.error("report must be outside the repository to avoid report-only drift")
    try:
        config = json.loads(_confined(repo, args.config).read_text())
        report = refresh(repo, config)
    except Exception as exc:
        # Adapter errors can include credential-bearing URLs. Never echo them.
        parser.exit(1, f"Drift refresh failed ({type(exc).__name__}); inspect configuration and warehouse access locally.\n")
    args.report.write_text(markdown(report))
    if args.github_output:
        with args.github_output.open("a") as f:
            f.write(f"changed={str(report['changed']).lower()}\n")
            f.write("paths<<UNSHACKLE_DRIFT_PATHS\n" + "\n".join(report["paths"]) + "\nUNSHACKLE_DRIFT_PATHS\n")
    print(f"{len(report['changes'])} schema changes; {len(report['paths'])} changed artifacts")


if __name__ == "__main__":
    main()
