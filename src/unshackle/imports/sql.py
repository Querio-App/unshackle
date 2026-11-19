"""Static SQL/Python import. Imported Python is parsed, never imported or executed."""
from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Sequence

import sqlglot
from sqlglot import exp

from unshackle.ir import Bundle, Dashboard, Tile, slugify

SQL_START = re.compile(r"^\s*(?:(?:--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)\s*)*(?:select|with|insert|update|delete|create|drop|alter|merge|truncate|copy|call|explain)\b", re.I)
SINKS = {"execute", "executemany", "exec_driver_sql", "read_sql", "read_sql_query", "query", "sql", "text"}
SKIP_DIRS = {"node_modules", "__pycache__", "migration"}


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _looks_sql(node: ast.AST) -> bool:
    return any(isinstance(part, ast.Constant) and isinstance(part.value, str)
               and SQL_START.match(part.value) for part in ast.walk(node))


def _query_todo(sql: str) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql) if s is not None]
    except sqlglot.errors.SqlglotError:
        return "SQL could not be parsed faithfully; review the original source"
    if len(statements) != 1:
        return "expected one read-only SELECT/WITH query; split or translate the original source"
    statement = statements[0]
    if not isinstance(statement, exp.Query) or any(
        isinstance(node, (exp.DDL, exp.DML, exp.Into, exp.Command, exp.Lock))
        for node in statement.walk()
    ):
        return "only read-only SELECT/WITH queries become executable notebook SQL"
    if any(isinstance(node, (exp.Placeholder, exp.Parameter)) for node in statement.walk()):
        return "query parameters need explicit notebook bindings"
    return ""


def _python_expressions(source: str):
    tree = ast.parse(source)

    def visit(node):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in SINKS:
                argument = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg in {"sql", "query", "statement"}), None)
                if argument is not None:
                    # A nested text(...) call still carries a literal rather than dynamic formatting.
                    if isinstance(argument, ast.Call) and (
                        isinstance(argument.func, ast.Name) and argument.func.id == "text"
                        or isinstance(argument.func, ast.Attribute) and argument.func.attr == "text"
                    ):
                        yield from visit(argument)
                    else:
                        yield argument
                    return
            if _looks_sql(node):
                yield node
                return
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Subscript, ast.IfExp)) and _looks_sql(node):
            yield node
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    return list(visit(tree))


def _inputs(paths: Sequence[Path]) -> tuple[Path, list[Path]]:
    roots, found = [], set()
    for item in paths:
        item = Path(item).absolute()
        if item.is_symlink():
            raise ValueError("SQL import does not follow source symlinks")
        if not item.exists():
            raise ValueError("SQL import path does not exist")
        roots.append(item if item.is_dir() else item.parent)
        if item.is_file():
            if item.suffix.lower() not in {".sql", ".py"}:
                raise ValueError("SQL import accepts .sql and .py files or directories")
            found.add(item)
            continue
        for folder, dirs, files in os.walk(item, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS
                             and not (Path(folder) / d).is_symlink())
            for name in sorted(files):
                path = Path(folder) / name
                if not name.startswith(".") and path.suffix.lower() in {".sql", ".py"} and not path.is_symlink():
                    found.add(path)
    if not roots:
        raise ValueError("SQL import needs at least one path")
    root = Path(os.path.commonpath(roots))
    return root, sorted(found)


def parse(paths: Sequence[Path]) -> Bundle:
    root, files = _inputs(paths)
    bundle = Bundle(tool="sql")
    for path in files:
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(relative.encode()).hexdigest()[:12]
        dashboard = Dashboard(name=relative, slug=f"{slugify(relative)[:60]}-{digest}", source_ref=relative)

        def add(line: int, original: str, sql: str | None, reason: str = ""):
            todo = reason or (_query_todo(sql) if sql is not None else "dynamic Python expression; SQL cannot be extracted statically")
            if todo:
                todo = f"{relative}:{line}: {todo}"
            tile = Tile(
                id=f"{digest}-{line}-{len(dashboard.tiles) + 1}", title=f"{relative}:{line}", kind="table", mark="table",
                sql=sql if sql is not None and not todo else "", todo=todo, source_type=path.suffix[1:],
                source_config={"source_path": relative, "line": line, "source_expression": original},
            )
            dashboard.tiles.append(tile)
            # The notebook writer doesn't render arbitrary source_config. Keep original source
            # in Markdown as well, with a fence longer than any backtick run in the source.
            fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", original)), default=3))
            dashboard.notes.append(f"Source `{relative}:{line}`\n\n{fence}{path.suffix[1:]}\n{original}\n{fence}")

        if path.suffix.lower() == ".sql":
            add(1, source, source)
        else:
            try:
                expressions = _python_expressions(source)
            except SyntaxError as error:
                # The parser cannot identify SQL boundaries in a malformed module.
                # Copying the whole module (or even the offending line) could expose
                # unrelated credentials. Retain location only for local repair.
                add(error.lineno or 1, "", None,
                    "Python syntax error; source not copied because query boundaries are unknown; inspect the original file locally")
            else:
                for expression in expressions:
                    add(expression.lineno, ast.get_source_segment(source, expression) or "", _literal(expression))
        if dashboard.tiles:
            bundle.dashboards.append(dashboard)
    return bundle
