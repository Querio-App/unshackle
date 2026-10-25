"""Dashboard -> Jupyter notebook (.ipynb, nbformat 4): parameters, then SQL and an Altair chart per tile.

Cells, top to bottom: a title, a setup cell (imports and the warehouse engine from
UNSHACKLE_WAREHOUSE_URL), a `parameters` cell (papermill convention) with the dashboard
filters, the source dashboard's interactions and what was not migrated, then per tile in layout order a heading, a SQL cell (`<tile>_sql`, run with
pd.read_sql into `<tile>`) and a chart cell (`<tile>_chart`). Text tiles are markdown
cells. No outputs are stored, so the file diffs cleanly.
"""

from __future__ import annotations

import builtins
import json
import keyword
import re

from unshackle.ir import Bundle, Dashboard, Encoding, Reference, Tile, slugify, snake
from unshackle.target.files import one_line_comment, unique_rel
from unshackle.target.layout import Layout

TYPE_CODE = {"quantitative": "Q", "nominal": "N", "temporal": "T", "ordinal": "O"}
CHANNEL_CLASS = {
    "x": "alt.X",
    "y": "alt.Y",
    "color": "alt.Color",
    "xOffset": "alt.XOffset",
    "theta": "alt.Theta",
    "size": "alt.Size",
    "text": "alt.Text",
    "detail": "alt.Detail",
    "row": "alt.Row",
    "column": "alt.Column",
    "facet": "alt.Facet",
    "latitude": "alt.Latitude",
    "longitude": "alt.Longitude",
}
FACETS = ("row", "column", "facet")
SORTABLE = ("x", "y", "color", "xOffset", "theta", "row", "column", "facet")
# where a channel takes a number format: its axis, its legend, or the channel itself
FORMAT_ARG = {"x": "axis=alt.Axis(format={})", "y": "axis=alt.Axis(format={})", "color": "legend=alt.Legend(format={})",
              "size": "legend=alt.Legend(format={})", "text": "format={}"}
MARK_METHOD = {
    "bar": "mark_bar()",
    "line": "mark_line(point=True)",
    "area": "mark_area()",
    "point": "mark_point()",
    "arc": "mark_arc()",
    "rect": "mark_rect()",
    "geoshape": "mark_geoshape()",
    "text": "mark_text()",
}
# Map shapes (TopoJSON with a `name` property per feature): (URL, feature collection, projection)
GEO_SHAPES = {
    "us_states": ("https://cdn.jsdelivr.net/npm/us-atlas@3.0.1/states-10m.json", "states", "albersUsa"),
    "countries": ("https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json", "countries", "equalEarth"),
}
RESERVED = {"alt", "pd", "os", "text", "engine", "create_engine", "base"}
PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _ident(name: str, used: set[str]) -> str:
    base = snake(name) or "tile"
    if base[0].isdigit() or keyword.iskeyword(base) or hasattr(builtins, base) or base in RESERVED:
        base = f"tile_{base}"
    ident, n = base, 2
    while ident in used:
        ident, n = f"{base}_{n}", n + 1
    used.add(ident)
    return ident


def _py_str(value: str) -> str:
    return repr(value)


def _sql_literal(sql: str) -> str:
    if '"""' in sql or sql.endswith('"'):
        return repr(sql)
    return '"""\n' + sql.strip().replace("\\", "\\\\") + '\n"""'


def _shorthand(enc: Encoding) -> str:
    field = enc.field
    if enc.time_unit:
        field = f"{enc.time_unit}({field})"
    if enc.aggregate:
        field = f"{enc.aggregate}({field})"
    return f"{field}:{TYPE_CODE.get(enc.type, 'N')}"


def _encode_args(encodings: dict[str, Encoding]) -> tuple[list[str], list[str]]:
    args, notes = [], []
    tooltips = []
    for channel, enc in encodings.items():
        if channel == "tooltip":
            fmt = f", format={_py_str(enc.format)}" if enc.format else ""
            tooltips.append(f"alt.Tooltip({_py_str(_shorthand(enc))}{fmt})" if fmt else _py_str(_shorthand(enc)))
            continue
        cls = CHANNEL_CLASS.get(channel)
        if cls is None:
            notes.append(f"unknown channel {channel!r} dropped")
            continue
        extra = f", title={_py_str(enc.title)}" if enc.title else ""
        if enc.sort and channel in SORTABLE:
            extra += ", sort=None" if enc.sort == "none" else f", sort={_py_str(enc.sort)}"  # None: the rows' order
        if enc.format and channel in FORMAT_ARG:
            extra += ", " + FORMAT_ARG[channel].format(_py_str(enc.format))
        if channel == "facet":
            extra += ", columns=3"
        args.append(f"{channel}={cls}({_py_str(_shorthand(enc))}{extra})")
    if tooltips:
        args.append(f"tooltip=[{', '.join(tooltips)}]")
    return args, notes


def _todos(notes: list[str]) -> list[str]:
    return [f"# TODO(unshackle): {one_line_comment(t)}" for t in notes]


def _mark(tile: Tile, mark: str) -> str | None:
    method = MARK_METHOD.get(mark)
    if tile.kind == "kpi" and not method:
        method = MARK_METHOD["text"]
    return method


def _map_chart(ident: str, tile: Tile) -> list[str]:
    """Shapes looked up by name: each feature takes the row whose `key` field equals its name."""
    url, feature, projection = GEO_SHAPES[tile.geo]
    encodings = dict(tile.encodings)
    key = encodings.pop("key")
    args, notes = _encode_args(encodings)
    lines = _todos(notes) + [
        f"{ident}_chart = (",
        f"    alt.Chart(alt.topo_feature({_py_str(url)}, {_py_str(feature)}))",
        "    .mark_geoshape(stroke='white')",
        f"    .transform_lookup(lookup='properties.name', from_=alt.LookupData({ident}, {_py_str(key.field)}, list({ident}.columns)))",
    ]
    if args:
        lines += ["    .encode(", *[f"        {a}," for a in args], "    )"]
    lines.append(f"    .project(type={_py_str(projection)})")
    if tile.title:
        lines.append(f"    .properties(title={_py_str(tile.title)})")
    return lines + [")", f"{ident}_chart"]


def _single_chart(ident: str, tile: Tile) -> list[str]:
    if tile.mark == "geoshape" and tile.geo in GEO_SHAPES and "key" in tile.encodings:
        return _map_chart(ident, tile)
    mark = _mark(tile, tile.mark)
    args, notes = _encode_args(tile.encodings)
    if not mark:
        notes.append(f"no Altair mark for source chart type {tile.source_type or tile.mark!r}; showing the data instead")
        return _todos(notes) + [ident]
    lines = _todos(notes) + [f"{ident}_chart = (", f"    alt.Chart({ident})", f"    .{mark}"]
    if args:
        lines += ["    .encode(", *[f"        {a}," for a in args], "    )"]
    if "latitude" in tile.encodings and "longitude" in tile.encodings:
        lines.append("    .project(type='mercator')")
    if tile.title:
        lines.append(f"    .properties(title={_py_str(tile.title)})")
    return lines + [")", f"{ident}_chart"]


def _composed_chart(ident: str, tile: Tile) -> list[str]:
    """Layers over shared encodings: alt.layer (optionally dual axis, faceted) or alt.hconcat / alt.vconcat.

    Layers without a colour field get `color=alt.datum(<series>)` so each series has its own colour and legend entry.
    """
    layered = tile.compose in ("layer", "dual_axis")
    shared = dict(tile.encodings)
    # A layered chart is faceted as a whole; concatenated panels keep facet channels in each panel.
    facets = {ch: shared.pop(ch) for ch in FACETS if ch in shared} if layered else {}
    args, notes = _encode_args(shared)
    if args:
        lines = ["base = (", f"    alt.Chart({ident})", "    .encode(", *[f"        {a}," for a in args], "    )", ")"]
    else:
        lines = [f"base = alt.Chart({ident})"]
    func = "alt.layer" if layered else f"alt.{tile.compose}"
    lines += [f"{ident}_chart = (", f"    {func}("]
    for layer in tile.layers:
        mark = _mark(tile, layer.mark)
        if not mark:
            notes.append(f"no Altair mark for series type {layer.mark!r}; drawn as points")
            mark = MARK_METHOD["point"]
        largs, more = _encode_args(layer.encodings)
        notes += more
        series = next((e for ch, e in layer.encodings.items() if ch in ("x", "y")), None)
        if layered and series and "color" not in shared and "color" not in layer.encodings:
            largs.append(f"color=alt.datum({_py_str(series.title or series.field)})")  # one legend entry per series
        if largs:
            lines += [f"        base.{mark}.encode(", *[f"            {a}," for a in largs], "        ),"]
        else:
            lines.append(f"        base.{mark},")
    lines.append("    )")
    if tile.compose == "dual_axis":  # on the layers' measure axis (columns: x)
        axis = "x" if all("x" in layer.encodings and "y" not in layer.encodings for layer in tile.layers) else "y"
        lines.append(f"    .resolve_scale({axis}='independent')")
    if facets:
        fargs, more = _encode_args(facets)
        notes += more
        lines += ["    .facet(", *[f"        {a}," for a in fargs], "    )"]
    if tile.title:
        lines.append(f"    .properties(title={_py_str(tile.title)})")
    return _todos(notes) + lines + [")", f"{ident}_chart"]


def _label_expr(ref: Reference) -> str:
    """Vega expression for a reference line's label: its text with the value formatted in place of {value}."""
    value = f"format(datum[{ref.field!r}], ',.2~f')"
    pieces = []
    for i, part in enumerate(ref.label.split("{value}")):
        if i:
            pieces.append(value)
        if part:
            pieces.append(repr(part.replace("{{", "{").replace("}}", "}")))
    return " + ".join(pieces) or "''"


def _reference_layers(ident: str, tile: Tile) -> list[str]:
    """Rule (a tick per cell for cell scope), band and label layers for the tile's reference lines."""
    out = []
    for ref in tile.references:
        other = "x" if ref.channel == "y" else "y"
        enc_cls = CHANNEL_CLASS[ref.channel]
        cell = tile.encodings.get(other) if ref.scope == "cell" else None
        cell_arg = f"{other}={CHANNEL_CLASS[other]}({_py_str(_shorthand(cell))}), " if cell and cell.type in ("nominal", "ordinal") else ""
        value = f"{ref.channel}={enc_cls}({_py_str(ref.field + ':Q')})"
        if ref.band_to:
            out.append(f"alt.Chart({ident}).mark_rect(opacity=0.15).encode({cell_arg}{value}, {ref.channel}2={_py_str(ref.band_to)}),")
            continue
        mark = "mark_tick(color='black', thickness=2)" if cell_arg else "mark_rule(color='black')"
        out.append(f"alt.Chart({ident}).{mark}.encode({cell_arg}{value}),")
        if ref.label:
            out.append(f"alt.Chart({ident}).mark_text(align='left', dx=4, dy=-6).encode({cell_arg}{value}, text='label:N')"
                       f".transform_calculate(label={_py_str(_label_expr(ref))}),")
    return out


def _with_references(ident: str, tile: Tile, lines: list[str]) -> list[str]:
    """The chart with the source's reference lines layered on it; a faceted chart is layered first, then faceted."""
    if not tile.references:
        return lines
    if tile.layers or lines[-1] != f"{ident}_chart":
        cols = ", ".join(dict.fromkeys(r.band_to or r.field for r in tile.references))
        return _todos([f"reference lines are columns of {ident} ({cols}): draw them as rule layers"]) + lines
    layers = [f"    {x}" for x in _reference_layers(ident, tile)]
    facets = {ch: e for ch, e in tile.encodings.items() if ch in FACETS}
    if not facets:
        return lines[:-1] + [f"{ident}_chart = alt.layer(", f"    {ident}_chart,", *layers, ")", f"{ident}_chart"]
    flat = Tile(tile.id, kind=tile.kind, mark=tile.mark, encodings={ch: e for ch, e in tile.encodings.items() if ch not in FACETS})
    body = [x for x in _single_chart(ident, flat) if not x.startswith("# TODO")][1:-2]  # the chart expression
    fargs, notes = _encode_args(facets)
    out = [x for x in lines if x.startswith("# TODO")] + _todos(notes)
    out += [f"{ident}_chart = (", "    alt.layer(", *[f"    {x}" for x in body[:-1]], f"    {body[-1]},", *[f"    {x}" for x in layers]]
    out += ["    )", "    .facet(", *[f"        {a}," for a in fargs], "    )"]
    if tile.title:
        out.append(f"    .properties(title={_py_str(tile.title)})")
    return out + [")", f"{ident}_chart"]


def _cell_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)[:64] or "cell"


def _markdown(cell_id: str, text: str) -> dict:
    return {"cell_type": "markdown", "id": _cell_id(cell_id), "metadata": {}, "source": text}


def _code(cell_id: str, lines: list[str], **metadata) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "id": _cell_id(cell_id),
        "metadata": metadata,
        "outputs": [],
        "source": "\n".join(lines),
    }


def _title(d: Dashboard, tool: str) -> str:
    lines = [f"# {d.name}", ""]
    if d.description.strip():
        lines += [d.description.strip(), ""]
    about = [f"Migrated by unshackle from {tool}"]
    if d.source_ref:
        about.append(f"source `{d.source_ref}`")
    if d.datasource:
        about.append(f"datasource `{d.datasource}`")
    if d.url:
        about.append(f"[original dashboard]({d.url})")
    lines += [", ".join(about) + ".", "", "Set `UNSHACKLE_WAREHOUSE_URL` to a SQLAlchemy URL for the warehouse, then run all cells."]
    return "\n".join(lines)


SETUP = [
    "import os",
    "",
    "import altair as alt",
    "import pandas as pd",
    "from sqlalchemy import create_engine, text",
    "",
    'engine = create_engine(os.environ["UNSHACKLE_WAREHOUSE_URL"])',
]


def build_notebook(d: Dashboard, tool: str) -> dict:
    """The notebook as an nbformat 4 dict, cell sources as plain strings."""
    used = set(RESERVED)
    params: dict[str, str] = {}  # filter name (the :bind in SQL) -> Python variable in the parameters cell
    for f in d.filters:
        ok = f.name.isidentifier() and not keyword.iskeyword(f.name) and f.name not in used
        params[f.name] = f.name if ok else _ident(f.name, used)
        used.add(params[f.name])
    lines = ["# Dashboard filters, bound as :name in the tile SQL."]
    for f in d.filters:
        if f.default is not None and not any(re.search(rf"(?<!:):{re.escape(f.name)}\b", t.sql or "") for t in d.tiles):
            lines.append(
                f"# TODO(unshackle): dashboard filter {one_line_comment(f.name)} (default {f.default!r}) is not applied to any tile's SQL; "
                f"add :{one_line_comment(f.name)} where the source dashboard applies it (skills/translate-dashboards)"
            )
    for f in d.filters:
        lines += [f"# {one_line_comment(f.note)}"] if f.note else []
        lines.append(f"{params[f.name]} = {f.default!r}" + (f"  # {one_line_comment(f.allowed)}" if f.allowed else ""))
    cells = [_markdown("title", _title(d, tool)), _code("setup", SETUP), _code("parameters", lines, tags=["parameters"])]
    about = []
    if d.notes:
        about += ["## Interactions", "", *[f"- {n}" for n in d.notes], ""]
    if d.not_migrated:
        about += ["## Not migrated", "", *[f"- {n}" for n in d.not_migrated], ""]
    if about:
        cells.append(_markdown("interactions", "\n".join(about).strip()))

    by_id = {t.id: t for t in d.tiles}
    order = list(dict.fromkeys([i for row in d.layout for i in row if i in by_id] + list(by_id)))
    idents: dict[str, str] = {}
    for tid in order:
        tile = by_id[tid]
        ident = idents[tid] = _ident(tile.title or tile.id, used)
        heading = f"## {tile.title}" if tile.title else ""
        if tile.kind == "text":
            cells.append(_markdown(ident, "\n\n".join(x for x in (heading, tile.text.strip()) if x)))
            continue
        cells.append(_markdown(f"{ident}-title", heading or f"## {ident}"))
        lines = _todos([tile.todo]) if tile.todo else []
        sql = tile.sql.strip()
        if sql:
            binds = [p for p in dict.fromkeys(PARAM.findall(sql)) if p in params]
            bound = ", params={" + ", ".join(f"{_py_str(p)}: {params[p]}" for p in binds) + "}" if binds else ""
            lines += [f"{ident}_sql = {_sql_literal(sql)}", f"{ident} = pd.read_sql(text({ident}_sql), engine{bound})"]
        else:
            lines += [
                "# TODO(unshackle): no SQL recovered for this tile; write it here and run it with pd.read_sql",
                f'{ident}_sql = ""',
                f"{ident} = pd.DataFrame()",
            ]
        if tile.kind == "table":
            cells.append(_code(f"{ident}-sql", lines + [ident]))
            continue
        cells.append(_code(f"{ident}-sql", lines))
        chart = _composed_chart(ident, tile) if tile.layers else _single_chart(ident, tile)
        cells.append(_code(f"{ident}-chart", _with_references(ident, tile, chart)))
    layout = [[idents[i] for i in row if i in idents] for row in d.layout or [[t.id] for t in d.tiles]]
    seen: set[str] = set()
    for cell in cells:  # ids are cut at 64 characters, so long titles can collide
        cid, i = cell["id"], 2
        while cid in seen:
            cid, i = f"{cell['id'][:60]}-{i}", i + 1
        seen.add(cid)
        cell["id"] = cid
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "unshackle": {"tool": tool, "source": d.source_ref, "layout": [r for r in layout if r]},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def render_dashboard(d: Dashboard, tool: str) -> str:
    """Serialised the way Jupyter saves notebooks: one-space indent, sorted keys, sources as lists of lines."""
    nb = build_notebook(d, tool)
    for cell in nb["cells"]:
        cell["source"] = cell["source"].splitlines(keepends=True)
    return json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def notebook_files(bundle: Bundle, layout: Layout) -> dict[str, str]:
    files = {}
    for d in bundle.dashboards:
        name = re.sub(r"-+", "-", slugify(d.slug or d.name))
        rel = unique_rel(files, layout.rel("notebooks", f"{name}.ipynb"))
        files[rel] = render_dashboard(d, bundle.tool)
    return files
