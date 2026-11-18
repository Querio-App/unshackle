"""Run generated notebooks against a warehouse, and read their parameters and tile SQL.

Needs UNSHACKLE_WAREHOUSE_URL, e.g. snowflake://..., bigquery://..., postgresql://..., duckdb:///local.db
(not needed for notebooks that read the DuckDB file `build` loads with data copied out of the tool).
"""

from __future__ import annotations

import ast
import html
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
TILE_SQL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)_sql = ", re.M)
PROBE = "unshackle-rows:"
SQL_ECHO = re.compile(r"^Execution failed on sql '.*?': ", re.S)  # pandas repeats the query before the error


def query(sql: str, params: dict[str, Any], url: str | None = None, *,
          limit: int | None = None, timeout: float | None = None):
    """Execute SQL; optional bounded mode requires a natively cancellable driver.

    Existing notebook/verify callers retain their unbounded behavior. MCP uses both
    options. The deadline covers execution and fetching, starting after connection.
    """
    import math
    import threading
    import time
    import pandas as pd
    from sqlalchemy import create_engine, text

    url = url or os.environ.get("UNSHACKLE_WAREHOUSE_URL")
    if not url:
        raise SystemExit("set UNSHACKLE_WAREHOUSE_URL to run notebooks")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be positive and finite")
    used = {k: v for k, v in params.items() if k in set(PARAM.findall(sql))}
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            if limit is None and timeout is None:
                return pd.read_sql(text(sql), conn, params=used)
            dialect = engine.dialect.name
            raw = conn.connection.driver_connection
            cancel = None
            if timeout is not None:
                if dialect == "sqlite":
                    cancel = raw.interrupt
                elif dialect == "duckdb":
                    cancel = raw.interrupt
                elif dialect == "postgresql" and callable(getattr(raw, "cancel", None)):
                    cancel = raw.cancel
                    conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                                 {"ms": str(max(1, int(timeout * 1000)))})
                else:
                    raise ValueError(f"Cancellable query deadlines unsupported for {dialect}; use PostgreSQL, DuckDB or SQLite")
            expired = threading.Event()
            deadline = time.monotonic() + timeout if timeout is not None else float('inf')
            def interrupt():
                expired.set()
                cancel()
            timer = threading.Timer(timeout, interrupt) if timeout is not None else None
            if dialect == "sqlite" and timeout is not None:
                raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if timer:
                timer.daemon = True
                timer.start()
            try:
                # Limit at the database as well as the cursor. Fetch one extra row to
                # distinguish exact-limit results from truncation without a COUNT query.
                bounded_sql = f"SELECT * FROM ({sql.strip().rstrip(';')}) AS unshackle_result LIMIT {limit + 1}" if limit is not None else sql
                bounded = conn.execution_options(stream_results=dialect == "postgresql", max_row_buffer=(limit or 1000) + 1,
                                                 no_parameters=not used)
                # Literal SQL from MCP has no binds. SQLAlchemy text() otherwise
                # mistakes colons inside JSON/string literals for bind parameters.
                cursor = bounded.execute(text(bounded_sql), used) if used else bounded.exec_driver_sql(bounded_sql)
                with cursor as result:
                    rows = result.fetchmany(limit + 1) if limit is not None else result.fetchall()
                    frame = pd.DataFrame(rows[:limit] if limit is not None else rows, columns=result.keys())
                    frame.attrs['truncated'] = limit is not None and len(rows) > limit
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("Query execution deadline exceeded")
                return frame
            except Exception:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("Query execution deadline exceeded") from None
                raise
            finally:
                if timer:
                    timer.cancel()
                    timer.join()
                if dialect == "sqlite" and timeout is not None:
                    raw.set_progress_handler(None, 0)
    finally:
        engine.dispose()


def _source(cell: dict) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else src


def notebook_queries(path: str | Path) -> tuple[dict[str, Any], dict[str, str]]:
    """({parameter: default}, {tile: sql}) read from a notebook without running it.

    Parameters are the literal assignments in the cell tagged `parameters`; tile SQL is
    every `<tile>_sql = "..."` string assignment in a code cell.
    """
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    params: dict[str, Any] = {}
    sqls: dict[str, str] = {}
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        try:
            tree = ast.parse(_source(cell))
        except SyntaxError:
            continue
        is_params = "parameters" in cell.get("metadata", {}).get("tags", [])
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
                continue
            name = node.targets[0].id
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if is_params:
                params[name] = value
            elif name.endswith("_sql") and isinstance(value, str):
                sqls[name[: -len("_sql")]] = value
    return params, sqls


def run_notebook(path: str | Path, url: str | None = None, timeout: int = 600) -> Path:
    """Execute the notebook in a fresh kernel and write a `<name>.html` preview next to it.

    The .ipynb itself is not changed. The preview shows the markdown, tables and charts
    (not the code); source text and chart data are escaped.
    """
    return _execute(Path(path), url, timeout)[1]


@dataclass
class NotebookRun:
    """What `unshackle run` reports for one notebook (one dashboard)."""

    path: Path
    tiles: int = 0  # tiles with a query cell (text tiles are markdown)
    with_sql: int = 0
    ran: int = 0  # tiles whose SQL ran without an error
    returned_rows: int = 0  # of those, tiles that returned at least one row
    errors: dict[str, str] = field(default_factory=dict)  # tile (or setup cell) -> its first error


def run_report(path: str | Path, url: str | None = None, timeout: int = 600) -> NotebookRun:
    """Execute the notebook as run_notebook does, but carry on past errors, and count what its tiles did."""
    path = Path(path)
    _, sqls = notebook_queries(path)
    probe = f"print({PROBE!r}, {{k: len(globals()[k]) for k in {sorted(sqls)!r} if hasattr(globals().get(k), 'shape')}})"
    nb, _ = _execute(path, url, timeout, allow_errors=True, probe=probe)
    *cells, last = nb.cells
    printed = "".join(_source({"source": o.get("text", "")}) for o in last.get("outputs", []))
    rows: dict[str, int] = ast.literal_eval(printed[len(PROBE):].strip()) if printed.startswith(PROBE) else {}
    errors: dict[str, str] = {}
    tile = ""
    for cell in cells:
        if cell.cell_type != "code":
            continue
        m = TILE_SQL.search(_source(cell))
        tile = m.group(1) if m and m.group(1) in sqls else tile  # a tile's chart cell follows its SQL cell
        err = next((o for o in cell.get("outputs", []) if o.get("output_type") == "error"), None)
        if err:
            first = SQL_ECHO.sub("", (err.get("evalue") or "").strip()).splitlines()
            errors.setdefault(tile or cell.get("id") or "setup", f"{err.get('ename')}: {first[0] if first else ''}")
    run = NotebookRun(path, tiles=len(sqls), with_sql=sum(1 for q in sqls.values() if q.strip()), errors=errors)
    ran = [t for t, q in sqls.items() if q.strip() and t in rows]
    run.ran, run.returned_rows = len(ran), sum(1 for t in ran if rows[t] > 0)
    return run


def _execute(path: Path, url: str | None, timeout: int, allow_errors: bool = False, probe: str = ""):
    """(the executed notebook, its preview). `probe`: code run in a last cell, left out of the preview."""
    import nbformat
    from nbclient import NotebookClient

    url = url or os.environ.get("UNSHACKLE_WAREHOUSE_URL")
    nb = nbformat.read(path, as_version=4)
    if not url and not nb.metadata.get("unshackle", {}).get("local_db"):
        raise SystemExit("set UNSHACKLE_WAREHOUSE_URL (a SQLAlchemy URL) to run this notebook")
    # Preview only: charts come back as Vega-Lite JSON instead of Altair's HTML, so they can be embedded safely.
    nb.cells.insert(0, nbformat.v4.new_code_cell('import altair as alt\nalt.renderers.enable("json")'))
    if probe:
        nb.cells.append(nbformat.v4.new_code_cell(probe))
    previous = os.environ.get("UNSHACKLE_WAREHOUSE_URL")
    if url:
        os.environ["UNSHACKLE_WAREHOUSE_URL"] = url  # the kernel inherits it
    try:
        NotebookClient(nb, timeout=timeout, kernel_name="python3", allow_errors=allow_errors,
                       resources={"metadata": {"path": str(path.parent)}}).execute()
    finally:
        if previous is None:
            os.environ.pop("UNSHACKLE_WAREHOUSE_URL", None)
        else:
            os.environ["UNSHACKLE_WAREHOUSE_URL"] = previous
    out = path.with_suffix(".html")
    out.write_text(_preview(nb.cells[1 : len(nb.cells) - (1 if probe else 0)], path.stem), encoding="utf-8")
    print(f"wrote {out}")
    return nb, out


def _markdown_html(text: str) -> str:
    """Headings and paragraphs, escaped. Enough for a preview; open the notebook for the real thing."""
    parts = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = re.match(r"(#{1,6})\s+(.*)", block)
        if m and "\n" not in block:
            level = len(m.group(1))
            parts.append(f"<h{level}>{html.escape(m.group(2))}</h{level}>")
        elif block:
            parts.append("<p>" + html.escape(block).replace("\n", "<br>") + "</p>")
    return "".join(parts)


def _preview(cells: list, title: str) -> str:
    body = []
    for cell in cells:
        if cell.cell_type == "markdown":
            body.append(_markdown_html(_source(cell)))
            continue
        for output in cell.get("outputs", []):
            data = output.get("data", {})
            spec = data.get("application/json")
            if isinstance(spec, dict) and "vega-lite" in str(spec.get("$schema", "")):
                n = len(body)
                chart = json.dumps(spec, default=str).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
                body.append(f"<div id='c{n}'></div><script>vegaEmbed('#c{n}', {chart});</script>")
            elif "text/html" in data:  # DataFrames (pandas escapes the values)
                body.append(_source({"source": data["text/html"]}))
            elif output.get("output_type") == "stream" and output.get("name") == "stdout":
                body.append(f"<pre>{html.escape(_source({'source': output.get('text', '')}))}</pre>")
            elif "text/plain" in data:
                body.append(f"<pre>{html.escape(_source({'source': data['text/plain']}))}</pre>")
    return (
        "<!doctype html><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
        "<script src='https://cdn.jsdelivr.net/npm/vega'></script>"
        "<script src='https://cdn.jsdelivr.net/npm/vega-lite'></script>"
        "<script src='https://cdn.jsdelivr.net/npm/vega-embed'></script>"
        + "".join(body)
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m unshackle.run notebooks/<name>.ipynb")
    run_notebook(sys.argv[1])


def chart_spec_from_source(source: str, ident: str) -> dict:
    """Interpret a small declarative Altair subset, never execute notebook Python.

    Unknown expressions keep their complete source and a TODO. No imports, arbitrary
    attributes, Python calls, comprehensions or code execution are permitted.
    """
    import altair as alt
    constructors = {name: getattr(alt, name) for name in (
        'X', 'Y', 'Color', 'XOffset', 'Theta', 'Size', 'Text', 'Detail', 'Row', 'Column',
        'Facet', 'Latitude', 'Longitude', 'Tooltip', 'Axis', 'Legend', 'Scale',
        'layer', 'hconcat', 'vconcat', 'datum', 'value', 'topo_feature')}
    methods = {'encode', 'properties', 'facet', 'resolve_scale', 'project',
               'mark_bar', 'mark_line', 'mark_area', 'mark_point', 'mark_arc',
               'mark_rect', 'mark_geoshape', 'mark_text', 'mark_rule'}
    values = {ident: alt.NamedData(name=ident)}
    def value(node):
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.List):
            return [value(n) for n in node.elts]
        if isinstance(node, ast.Dict):
            return {value(k): value(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.Call):
            if any(kw.arg is None for kw in node.keywords):
                raise ValueError('expanded arguments')
            args = [value(n) for n in node.args]
            kwargs = {kw.arg: value(kw.value) for kw in node.keywords}
            fn = node.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == 'alt':
                if fn.attr == 'Chart':
                    return alt.Chart(*args, **kwargs)
                if fn.attr in constructors:
                    return constructors[fn.attr](*args, **kwargs)
            elif isinstance(fn, ast.Attribute) and fn.attr in methods:
                receiver = value(fn.value)
                if isinstance(receiver, alt.TopLevelMixin):
                    return getattr(receiver, fn.attr)(*args, **kwargs)
            raise ValueError('unsupported call')
        return ast.literal_eval(node)
    try:
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                values[node.targets[0].id] = value(node.value)
            elif isinstance(node, ast.Expr):
                value(node.value)
            else:
                raise ValueError('unsupported statement')
        chart = values.get(ident + '_chart')
        if not isinstance(chart, alt.TopLevelMixin):
            raise ValueError('no chart specification')
        return chart.to_dict()
    except Exception:
        return {'source_expression': source,
                'todo': 'TODO(unshackle): chart cannot be faithfully recovered statically; inspect source_expression'}
