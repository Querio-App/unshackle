"""Apply a parsed Bundle to the target repo and write migration/report.md."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from unshackle.http import missing_extra
from unshackle.ir import Bundle, Dashboard, todo_reasons
from unshackle.target import files as tf
from unshackle.target import notebooks, ossie, policies, semantic
from unshackle.target.writer import Target, UnsafePath, WriteResult, load_last_result

TODO_RE = re.compile(r"TODO\(unshackle\):?\s*(.*)")
SCAN_SUFFIXES = {".yml", ".yaml", ".py", ".sql", ".md", ".cedar", ".ipynb"}
DBTIGNORE = "*.ossie.yaml"
# what a TODO is about, from its text: the first kind whose pattern matches (migration/report.md counts them)
TODO_KINDS = [
    ("tile without SQL", r"no SQL recovered|worksheet not parsed|no fields on the sheet"),
    ("unknown field", r"unknown field"),
    ("table calculation", r"table calc|\b(INDEX|RANK\w*|WINDOW_\w+|RUNNING_\w+|LOOKUP|FIRST|LAST|TOTAL)\(\)"),
    ("level of detail", r"\bLOD\b|\b(FIXED|INCLUDE|EXCLUDE)\b"),
    ("dates (week start, fiscal year)", r"week start|fiscal year|ISO-8601"),
    ("default time dimension", r"default time dimension|agg_time_dimension|default_time_dimension"),
    ("parameter", r"parameter"),
    ("analytics (reference, trend, forecast)", r"reference line|reference band|trend line|forecast"),
    ("several data sources", r"blend|another datasource|cross-database"),
    ("filter", r"filter"),
    ("sort", r"\bsort"),
    ("tooltip", r"tooltip"),
    ("chart", r"Altair|facet|chart|\bmark|axis"),
    ("permissions", r"row filter|permission|polic(y|ies)|user attribute|USERNAME|ISMEMBEROF"),
    ("data copied from the source", r"copied from"),
    ("calculation", r"formula|function|calculation|translated|derivation|ATTR\(\)"),
]


@dataclass
class BuildResult:
    bundle: Bundle
    writes: WriteResult
    report_path: Path
    local_db: str = ""  # the DuckDB file this build loaded (see load_local_db)


def render_all(bundle: Bundle, target: Target) -> dict[str, tuple[str, bool]]:
    """{path: (content, merged)}"""
    layout = target.layout
    out: dict[str, tuple[str, bool]] = {}
    for rel, content in ossie.render(bundle, layout).items():
        out[rel] = (content, False)
    if layout.is_dbt:
        for rel, content in semantic.render(bundle, layout, getattr(target, "taken_types", {})).items():
            out[rel] = (content, False)
        for sm in bundle.semantic_models:
            if sm.sql:  # a model defined by a query: MetricFlow refs it as a dbt model
                out[layout.rel("sql_models", f"{sm.table.split('.')[-1]}.sql")] = (f"-- {sm.source_ref or bundle.tool}\n{sm.sql.strip()}\n", False)
        # dbt reads every YAML file under models/ as properties and rejects Ossie's `metrics`.
        # Rendered on every build, even when unchanged, so it never looks like a file this build dropped.
        ignore = target.read(".dbtignore") or ""
        if DBTIGNORE not in ignore.splitlines():
            ignore += ("\n" if ignore and not ignore.endswith("\n") else "") + DBTIGNORE + "\n"
        out[".dbtignore"] = (ignore, True)
    for rel, content in tf.catalog_files(bundle, layout, target.read).items():
        out[rel] = (content, True)
    for group in (
        tf.knowledge_files(bundle, layout),
        tf.query_files(bundle, layout),
        notebooks.notebook_files(bundle, layout),
        policies.policy_files(bundle, layout),
    ):
        for rel, content in group.items():
            out[rel] = (content, False)
    mem_rel = layout.rel("memory", "MEMORY.md")
    out[mem_rel] = (tf.render_memory(target.read(mem_rel), bundle.tool, tf.migration_summary(bundle)), True)
    return out


def _warehouse_types(bundle: Bundle, warehouses: dict[str, str]) -> None:
    """Warehouse type per datasource: unshackle.toml, else what the export reports, else unknown (ANSI_SQL)."""
    for ds in sorted({bundle.datasource} | {sm.datasource for sm in bundle.semantic_models if sm.datasource}):
        if warehouses.get(ds):
            continue
        if ds == bundle.datasource and bundle.warehouse:
            warehouses[ds] = bundle.warehouse
            bundle.warn(f"datasource {ds}", f"warehouse type {bundle.warehouse!r} taken from the {bundle.tool} export; "
                        f"set [datasources.{ds}] type in unshackle.toml to override", "info")
        elif bundle.semantic_models:
            bundle.warn(f"datasource {ds}", f"warehouse type unknown, so semantic expressions are labelled ANSI_SQL; "
                        f"set [datasources.{ds}] type in unshackle.toml and re-run build")


def _avoid_collisions(bundle: Bundle, target: Target) -> None:
    """A second tool in the same repo must not reuse names another tool's files define.

    MetricFlow needs semantic model, measure and metric names to be unique across
    the project, and Cedar notebook ids must point at the right notebook. Colliding
    names get the tool as a prefix (models, measures, metrics) or suffix (notebooks).
    """
    import yaml

    from unshackle.ir import slugify, snake

    target.taken_types = {}
    others = [rel for rel, owner in target.lock.owners.items() if owner and owner != bundle.tool]
    if not others:
        return
    sem_dir = target.layout.paths["semantic"].rstrip("/") + "/"
    nb_dir = target.layout.paths["notebooks"].rstrip("/") + "/"
    models, measures, metrics, notebooks = set(), set(), set(), set()
    entities: dict[str, str] = {}
    labels: set[str] = set()
    for rel in others:
        if rel.startswith(sem_dir) and rel.endswith(".yml"):
            try:
                doc = yaml.safe_load(target.read(rel) or "") or {}
            except yaml.YAMLError:
                continue
            for sm in doc.get("semantic_models", []) or []:
                models.add(sm.get("name"))
                for kind, key in (("entity", "entities"), ("dimension", "dimensions"), ("measure", "measures")):
                    for el in sm.get(key, []) or []:
                        target.taken_types.setdefault(el.get("name"), kind)
                if sm.get("primary_entity"):
                    target.taken_types.setdefault(sm["primary_entity"], "entity")
                measures |= {m.get("name") for m in sm.get("measures", []) or []}
                for e in sm.get("entities", []) or []:
                    if e.get("type") == "primary":
                        entities[e.get("name")] = sm.get("name")
                if sm.get("primary_entity"):
                    entities[sm["primary_entity"]] = sm.get("name")
            for m in doc.get("metrics", []) or []:
                metrics.add(m.get("name"))
                labels.add(m.get("label") or m.get("name"))
        elif rel.startswith(nb_dir) and rel.endswith((".ipynb", ".py")):
            notebooks.add(Path(rel).stem)
    tool = snake(bundle.tool)
    for sm in bundle.semantic_models:
        primary = [snake(e.name) for e in sm.entities if e.type == "primary"] or [snake(sm.name)]
        clash = next((entities[p] for p in primary if p in entities), "") or (snake(sm.name) if snake(sm.name) in models else "")
        if clash:
            sm.todo = (
                f"another tool's migration already models this table as semantic model `{clash}`; "
                f"merge this {bundle.tool} model into it (or rename its primary entity) before enabling it"
            )
            sm.name = f"{tool}_{snake(sm.name)}" if snake(sm.name) in models else sm.name
            continue
        if snake(sm.name) in models:
            old, sm.name = sm.name, f"{tool}_{snake(sm.name)}"
            for m in bundle.metrics:
                if m.semantic_model and snake(m.semantic_model) == snake(old):
                    m.semantic_model = sm.name
            bundle.warn(f"semantic model {old}", f"renamed to {sm.name}: another tool already defines {snake(old)}", "info")
        for ms in sm.measures:
            if snake(ms.name) in measures:
                old, ms.name = ms.name, f"{tool}_{snake(ms.name)}"
                ms.expr = ms.expr or old
                for m in bundle.metrics:
                    if m.measure and snake(m.measure) == snake(old) and snake(m.semantic_model or "") == snake(sm.name):
                        m.measure = ms.name
    for m in bundle.metrics:
        label = m.label or m.name
        if label in labels:
            m.label = f"{label} ({bundle.tool})"
    renamed: dict[str, str] = {}
    for m in bundle.metrics:
        if snake(m.name) in metrics:
            renamed[snake(m.name)] = f"{tool}_{snake(m.name)}"
            m.label = m.label or m.name
            m.name = renamed[snake(m.name)]
    if renamed:
        for m in bundle.metrics:
            m.numerator = renamed.get(snake(m.numerator), m.numerator) if m.numerator else m.numerator
            m.denominator = renamed.get(snake(m.denominator), m.denominator) if m.denominator else m.denominator
            m.metrics = [renamed.get(snake(x), x) for x in m.metrics]
            m.offsets = {renamed.get(snake(k), k): v for k, v in m.offsets.items()}
            if m.expr:
                for old, new in renamed.items():
                    m.expr = re.sub(rf"\b{re.escape(old)}\b", new, m.expr)
    for d in bundle.dashboards:
        slug = slugify(d.slug or d.name)
        if slug in notebooks:
            new = f"{slug}-{slugify(bundle.tool)}"
            for p in bundle.permissions:
                if p.resource_type == "Notebook" and slugify(p.resource_id) == slug:
                    p.resource_id = new
            d.slug = new


def apply(bundle: Bundle, repo: Path, dry_run: bool = False) -> BuildResult:
    target = Target(repo, dry_run=dry_run, tool=bundle.tool)
    _avoid_collisions(bundle, target)
    _warehouse_types(bundle, target.layout.warehouses)
    for rel, (content, merged) in render_all(bundle, target).items():
        try:
            target.write(rel, content, merged=merged)
        except UnsafePath as exc:
            bundle.warn(rel, str(exc), "block")
    target.prune()
    if not dry_run:
        ir_path = repo / "migration" / "ir" / f"{bundle.tool}.json"
        ir_path.parent.mkdir(parents=True, exist_ok=True)
        ir_path.write_text(bundle.to_json())
    target.save()
    loaded = "" if dry_run else load_local_db(bundle, repo)
    report = write_report(repo, [bundle], target.result, dry_run=dry_run)
    return BuildResult(bundle=bundle, writes=target.result, report_path=report, local_db=loaded)


def load_local_db(bundle: Bundle, repo: Path) -> str:
    """Load the data copied out of the tool into bundle.local_db, the DuckDB file the notebooks fall back to.

    Returns its path, or "" (with a warning in the report) when there is nothing to load or it could not be loaded.
    """
    if not bundle.local_db:
        return ""
    try:
        import duckdb
    except ImportError:
        bundle.warn(bundle.local_db, missing_extra(bundle.tool, "loading the copied data into DuckDB"))
        return ""
    path = repo / bundle.local_db
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with duckdb.connect(str(path)) as con:
            con.execute(f"set file_search_path = '{str(repo.resolve()).replace(chr(39), chr(39) * 2)}'")  # the SQL's paths are repo-relative
            con.execute(bundle.local_db_sql)
    except duckdb.Error as exc:  # e.g. a notebook still has the file open
        bundle.warn(bundle.local_db, f"not loaded: {exc}; close what has it open and run `unshackle build {bundle.tool}` again")
        return ""
    return bundle.local_db


def _clean_todo(text: str) -> str:
    text = text.strip().replace("''", "'").replace('\\"', '"')
    for tail in ("*/", "\\", "'", '"'):
        if text.endswith(tail):
            text = text[: -len(tail)].rstrip()
    return text


def _json_line(line: str) -> str:
    """A notebook cell source line as saved in the .ipynb JSON: `  "text\\n",` -> `text`."""
    try:
        value = json.loads(line.strip().rstrip(","))
    except json.JSONDecodeError:
        return line
    return value if isinstance(value, str) else line


def todo_kind(msg: str) -> str:
    return next((kind for kind, pat in TODO_KINDS if re.search(pat, msg, re.I)), "other")


def tile_coverage(d: Dashboard) -> tuple[int, int, list[tuple[str, int]]]:
    """(tiles, tiles with SQL, [(kind of TODO, tiles it holds back)] most frequent first) for one dashboard; text tiles don't count."""
    tiles = [t for t in d.tiles if t.kind != "text"]
    kinds: dict[str, int] = {}
    for t in tiles:
        for kind in dict.fromkeys(todo_kind(r) for r in todo_reasons(t.todo) or (["no SQL recovered"] if not t.sql.strip() else [])):
            kinds[kind] = kinds.get(kind, 0) + 1
    return len(tiles), sum(1 for t in tiles if t.sql.strip()), sorted(kinds.items(), key=lambda x: (-x[1], x[0]))


def coverage_line(bundle: Bundle) -> str:
    """One line for the end of a build: how many dashboard tiles got SQL."""
    counts = [tile_coverage(d)[:2] for d in bundle.dashboards]
    tiles, with_sql = sum(c[0] for c in counts), sum(c[1] for c in counts)
    return f"{with_sql} of {tiles} tiles in {len(counts)} dashboards have SQL (per dashboard: migration/report.md)"


def scan_todos(repo: Path, rels: list[str]) -> list[tuple[str, int, str]]:
    found = []
    for rel in rels:
        path = repo / rel
        if path.suffix not in SCAN_SUFFIXES or not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if path.suffix == ".ipynb" and "TODO(unshackle)" in line:
                line = _json_line(line)
            m = TODO_RE.search(line)
            if m:
                found.append((rel, n, _clean_todo(m.group(1))))
    return found


def all_generated(repo: Path) -> list[str]:
    lock = repo / "migration" / "unshackle.lock"
    if not lock.exists():
        return []
    return sorted(json.loads(lock.read_text()).get("files", {}))


def load_bundles(repo: Path) -> list[Bundle]:
    ir_dir = repo / "migration" / "ir"
    if not ir_dir.exists():
        return []
    return [Bundle.from_json(p.read_text()) for p in sorted(ir_dir.glob("*.json"))]


def write_report(repo: Path, bundles: list[Bundle] | None = None, writes: WriteResult | None = None, dry_run: bool = False) -> Path:
    stored = {b.tool: b for b in load_bundles(repo)}
    for b in bundles or []:
        stored[b.tool] = b
    lines = ["# Migration report", ""]
    lines += ["| Tool | Tables | Semantic models | Metrics | Dashboards | Queries | Knowledge | Permissions | Row filters |"]
    lines += ["| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for b in stored.values():
        lines.append(
            f"| {b.tool} | {len(b.tables)} | {len(b.semantic_models)} | {len(b.metrics)} | {len(b.dashboards)} | "
            f"{len(b.queries)} | {len(b.knowledge)} | {len(b.permissions)} | {len(b.row_filters)} |"
        )
    lines.append("")
    for b in stored.values():
        meta_path = repo / "migration" / "raw" / b.tool / "_import.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        lines += [
            f"## {b.tool}: imported manually",
            "",
            f"Built from files downloaded by hand ({', '.join(meta.get('inputs') or [])}), "
            "not from the API. What those files can't carry:",
            "",
        ]
        lines += [f"- {g}" for g in meta.get("gaps") or []]
        lines.append("")
    writes = writes or load_last_result(repo)
    if writes:
        lines += [
            "## This run",
            "",
            f"- {len(writes.written)} files written, {len(writes.unchanged)} unchanged.",
        ]
        for old, new in writes.renamed.items():
            lines.append(f"- `{old}` belongs to another tool's migration, so this one was written to `{new}`. Merge them by hand.")
        if writes.kept_hand_edits:
            lines.append(f"- {len(writes.kept_hand_edits)} files kept because someone edited them by hand:")
            lines += [f"  - `{r}`" for r in writes.kept_hand_edits]
        if writes.removed:
            lines.append(f"- {len(writes.removed)} files removed because this build no longer generates them:")
            lines += [f"  - `{r}`" for r in writes.removed]
        if writes.kept_stale:
            lines.append(f"- {len(writes.kept_stale)} files no longer generated, kept because edited. Merge or delete them by hand:")
            lines += [f"  - `{r}`" for r in writes.kept_stale]
        lines.append("")
    dashboards = [(b.tool, d) for b in stored.values() for d in b.dashboards]
    if dashboards:
        lines += [f"## Dashboards ({len(dashboards)})", "", "| Tool | Dashboard | Tiles | With SQL | Top blockers |", "| --- | --- | --- | --- | --- |"]
        for tool, d in dashboards:
            tiles, with_sql, kinds = tile_coverage(d)
            blockers = ", ".join(f"{k} ({n})" for k, n in kinds[:3])
            lines.append(f"| {tool} | {d.name.replace('|', '/')} | {tiles} | {with_sql} | {blockers} |")
        lines += ["", "Top blockers: kinds of TODO on the dashboard's tiles, with how many tiles have each.", ""]
    gaps = [(b.tool, d) for b in stored.values() for d in b.dashboards if d.not_migrated]
    if gaps:
        lines += [f"## Not migrated from dashboards ({len(gaps)})", ""]
        for tool, d in gaps:
            lines += [f"### {d.name} ({tool})", "", *[f"- {x}" for x in d.not_migrated], ""]
    todos = scan_todos(repo, all_generated(repo)) if not dry_run else []
    lines += [f"## Still to translate ({len(todos)})", ""]
    if todos:
        kinds: dict[str, list[str]] = {}
        for rel, _, msg in todos:
            kinds.setdefault(todo_kind(msg), []).append(rel)
        lines += ["| Kind | TODOs | Files |", "| --- | --- | --- |"]
        lines += [f"| {k} | {len(rels)} | {len(set(rels))} |" for k, rels in sorted(kinds.items(), key=lambda x: (-len(x[1]), x[0]))]
        lines += ["", "| File | Line | What's left |", "| --- | --- | --- |"]
        lines += [f"| `{rel}` | {n} | {msg.replace('|', '/')[:200]} |" for rel, n, msg in todos]
    else:
        lines.append("Nothing marked TODO(unshackle).")
    lines.append("")
    warnings = [(b.tool, w) for b in stored.values() for w in b.warnings]
    if warnings:
        lines += [f"## Warnings ({len(warnings)})", "", "| Tool | Severity | Item | Message |", "| --- | --- | --- | --- |"]
        lines += [f"| {t} | {w.severity} | {w.item} | {w.message.replace('|', '/')} |" for t, w in warnings]
        lines.append("")
    skipped = [(b.tool, x) for b in stored.values() for x in b.skipped]
    if skipped:
        unread = sum(1 for _, x in skipped if x.why == "not read")
        lines += [
            f"## Not read from the source ({len(skipped)} kinds, {unread} not read)",
            "",
            "Elements and attributes of the exported files that unshackle doesn't read, counted by kind. Those marked "
            "\"not read\" may change what the source shows: check them. The others are left out on purpose.",
            "",
            "| Tool | What | Count | Why | First seen in |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [f"| {t} | `{x.kind}` | {x.count} | {x.why} | {x.where.replace('|', '/')} |" for t, x in skipped]
        lines.append("")
    if (repo / "dbt_project.yml").exists() and any(b.metrics for b in stored.values()):
        has_spine = any("time_spine" in p.read_text(errors="ignore") for p in (repo / "models").rglob("*.yml")) if (repo / "models").exists() else False
        if not has_spine:
            lines += [
                "## dbt time spine",
                "",
                "MetricFlow needs a time spine model before `dbt parse` accepts these metrics.",
                "See https://docs.getdbt.com/docs/build/metricflow-time-spine.",
                "",
            ]
    results_path = repo / "migration" / "parity" / "results.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
        passed = sum(1 for r in results if r["status"] == "pass")
        lines += [f"## Parity ({passed}/{len(results)} pass)", "", "| Check | Status | Rows (source / new) | Largest gap | Note |", "| --- | --- | --- | --- | --- |"]
        for r in results:
            lines.append(
                f"| {r['name']} | {r['status']} | {r.get('source_rows', '')} / {r.get('target_rows', '')} | "
                f"{r.get('max_gap', '')} | {r.get('note', '').replace('|', '/')} |"
            )
        lines.append("")
    path = repo / "migration" / "report.md"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
    return path
