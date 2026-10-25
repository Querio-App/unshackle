"""Tableau table calculations as window SQL over a sheet's marks.

A table calculation runs on the sheet's aggregated result (its marks, after every filter, measure filters included).
Its addressing (Compute Using) splits the sheet's dimensions in two: the addressed ones, along which it moves in their
order on the sheet, and the rest, which partition it (it restarts for each of their combinations). In the XML a
`<table-calc>` on the column instance (quick table calcs and formulas alike; a formula's own `<calculation>` may hold its
default) sets it with `ordering-type`:

- `Rows`: Table (across), the dimensions on Columns; `Columns`: Table (down), the dimensions on Rows; `Table`: Table
  (across then down), Rows' then Columns' dimensions. Without a `<table-calc>`: Table (across).
- `RowInPane`: Pane (across), `ColumnInPane`: Pane (down), `Pane`: Pane (across then down): the innermost dimension
  of Columns, of Rows, or both; a pane is a cell without them. `CellInPane`: Cell, each mark alone.
- `Field`: Specific Dimensions, `ordering-field` or the `<order field>` list (outermost first); `level-break` is the
  first addressed field after Restart Every (the listed fields before it partition).

Dimensions on the Marks card (colour, detail, ...) partition in every mode but Specific Dimensions. A crosstab (discrete
dimensions on both Rows and Columns) is padded first: every combination of its row headers and column headers is a
mark, NULL where the data has none, and the calculation moves over those too.

Each instance name is one table calculation for the whole workbook; Tableau numbers further ones (`cum:sum:x:qk:2`).
Quick table calcs (`type`) are Tableau formulas over the measure: running total RUNNING_<AGG>(x), percent of total
x / TOTAL(x), difference ZN(x) - LOOKUP(ZN(x), n), percent difference (ZN(x) - LOOKUP(ZN(x), n)) / ABS(LOOKUP(ZN(x), n)),
percent from ZN(x) / LOOKUP(ZN(x), n), rank RANK*(x), percentile RANK_PERCENTILE(x) and moving calculation
WINDOW_<AGG>(x, from, to). TOTAL recomputes the aggregate over the partition's rows: from the marks for SUM, COUNT, MIN
and MAX, and for AVG as their sums over their counts.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

MODES = {"Rows": "across", "Columns": "down", "Table": "table", "RowInPane": "pane across", "ColumnInPane": "pane down",
         "Pane": "pane", "CellInPane": "cell", "Field": "field"}
_ATTRS = {"ordering-type", "ordering-field", "type", "aggregation", "rank-options", "diff-options", "from", "to", "window-options",
          "level-break"}
_UNSUPPORTED = {"level-address": "At the level", "tc-options": "its options", "field": "a nested table calculation's addressing"}
RUNNING = {"Sum": "sum", "Avg": "avg", "Min": "min", "Max": "max"}
RANKS = {"Competition": "rank", "Dense": "dense_rank", "Unique": "row_number"}
LABELS = {"CumTotal": "Running {agg} of {x}", "PctTotal": "% of Total {x}", "Difference": "Difference in {x}", "PctDiff": "% Difference in {x}",
          "PctValue": "% of {x}", "Rank": "Rank of {x}", "PctRank": "Percentile of {x}", "WindowTotal": "Moving {agg} of {x}"}
AGG_WORD = {"Sum": "Sum", "Avg": "Average", "Min": "Minimum", "Max": "Maximum"}
# a formula's table-calc functions with no SQL here
TODO_FUNCS = {
    "previous_value": "PREVIOUS_VALUE() reads the calculation's own result on the previous mark (a recursion): write it as a running "
                      "aggregate when it is one (e.g. PREVIOUS_VALUE(0) + x is RUNNING_SUM(x)), else compute it in Python",
    "script_bool": "SCRIPT_*() runs on an analytics extension (R, Python): port the script to a notebook cell",
    "script_int": "SCRIPT_*() runs on an analytics extension (R, Python): port the script to a notebook cell",
    "script_real": "SCRIPT_*() runs on an analytics extension (R, Python): port the script to a notebook cell",
    "script_str": "SCRIPT_*() runs on an analytics extension (R, Python): port the script to a notebook cell",
    "model_quantile": "MODEL_QUANTILE() fits a model on the marks: fit it in a notebook cell",
    "model_percentile": "MODEL_PERCENTILE() fits a model on the marks: fit it in a notebook cell",
}


class Unsupported(ValueError):
    pass


@dataclass
class Addressing:
    mode: str = "across"
    fields: list[str] = field(default_factory=list)  # Specific Dimensions: field references, outermost first
    level_break: str = ""


@dataclass
class Spec:
    """One `<table-calc>`: its addressing and, for a quick table calc, its type and options."""

    addressing: Addressing
    type: str = ""
    el: ET.Element | None = None

    def get(self, attr: str) -> str:
        return (self.el.get(attr) if self.el is not None else None) or ""


def read(el: ET.Element | None) -> Spec:
    """A `<table-calc>` element (None: Tableau's default, Table (across)). Raises Unsupported for what has no SQL here."""
    if el is None:
        return Spec(Addressing())
    for attr, what in _UNSUPPORTED.items():
        if el.get(attr):
            raise Unsupported(f"{what} ({attr}={el.get(attr)!r})")
    unknown = sorted(set(el.attrib) - _ATTRS - set(_UNSUPPORTED))
    if unknown:
        raise Unsupported(f"table-calc attributes {', '.join(unknown)}")
    children = {c.tag for c in el}
    if "sort" in children:
        raise Unsupported("a custom sort of the addressed fields (Sort order: Custom)")
    if children - {"order", "address"}:
        raise Unsupported(f"table-calc elements {', '.join(sorted(children - {'order', 'address'}))}")
    kind = el.get("ordering-type") or "Rows"
    if kind not in MODES:
        raise Unsupported(f"Compute Using {kind!r}")
    fields = [o.get("field") or "" for o in el.findall("order")]
    if el.get("ordering-field"):
        fields = [el.get("ordering-field") or ""]
    addressing = Addressing(MODES[kind], [f for f in fields if f], el.get("level-break") or "")
    if addressing.mode == "field" and not addressing.fields:
        raise Unsupported("Specific Dimensions with no fields")
    return Spec(addressing, el.get("type") or "", el)


@dataclass
class Window:
    """How one table calculation splits the marks: SQL of its partitioning dimensions and ORDER BY items along the
    addressed ones."""

    partition: list[str]
    order: list[str]

    def over(self, frame: str = "", ordered: bool = True, order: list[str] | None = None) -> str:
        items = order if order is not None else (self.order if ordered else [])
        parts = ([f"partition by {', '.join(self.partition)}"] if self.partition else []) + \
                ([f"order by {', '.join(items)}"] if items else []) + ([frame] if frame and items else [])
        return f"over ({' '.join(parts)})"


RUNNING_FRAME = "rows between unbounded preceding and current row"
WHOLE_FRAME = "rows between unbounded preceding and unbounded following"


def bound(n: int | None, end: bool) -> str:
    """A frame bound for a Tableau offset (None: FIRST() at the start, LAST() at the end)."""
    if n is None:
        return "unbounded following" if end else "unbounded preceding"
    return "current row" if n == 0 else f"{abs(n)} {'following' if n > 0 else 'preceding'}"


def lookup(v: str, n: int | None, w: Window, first: bool = True) -> str:
    """LOOKUP(v, n): v on the mark n away along the addressing (NULL past the partition's ends); n None: the first
    (`first`) or last mark."""
    if n is None:
        return f"{'first' if first else 'last'}_value({v}) {w.over(WHOLE_FRAME)}"
    if n == 0:
        return v
    return f"{'lead' if n > 0 else 'lag'}({v}, {abs(n)}) {w.over()}"


def rank(v: str, w: Window, kind: str, desc: bool) -> str:
    """RANK (competition), RANK_DENSE, RANK_UNIQUE (ties in the addressing order), RANK_MODIFIED; NULL is not ranked."""
    by = f"{v} {'desc' if desc else 'asc'} nulls last"
    if kind == "modified":
        value = f"count({v}) {w.over('range between unbounded preceding and current row', order=[by])}"
    else:
        fn = {"competition": "rank", "dense": "dense_rank", "unique": "row_number"}[kind]
        value = f"{fn}() {w.over(order=[by] + (w.order if kind == 'unique' else []))}"
    return f"case when {v} is null then null else {value} end"


def percentile(v: str, w: Window, desc: bool) -> str:
    """RANK_PERCENTILE: (modified rank - 1) / (marks ranked - 1), 1 for a partition of one."""
    by = f"{v} {'desc' if desc else 'asc'} nulls last"
    upto = f"count({v}) {w.over('range between unbounded preceding and current row', order=[by])}"
    n = f"count({v}) {w.over(ordered=False)}"
    return f"case when {v} is null then null when {n} = 1 then 1.0 else 1.0 * ({upto} - 1) / ({n} - 1) end"


def quick(spec: Spec, v: str, w: Window, total: str | None) -> str:
    """SQL of a quick table calc over the measure's aggregate `v`. `total`: TOTAL(v) over the partition, None when it
    can't be computed from the marks."""
    t = spec.type
    if t == "CumTotal":
        agg = RUNNING.get(spec.get("aggregation") or "Sum")
        if agg is None:
            raise Unsupported(f"running {spec.get('aggregation')}")
        return f"{agg}({v}) {w.over(RUNNING_FRAME)}"
    if t == "PctTotal":
        if total is None:
            raise Unsupported("percent of total of an aggregate TOTAL() can't recompute from the marks")
        return f"1.0 * {v} / nullif({total}, 0)"
    if t in ("Difference", "PctDiff", "PctValue"):
        if (spec.get("diff-options") or "Relative") != "Relative":
            raise Unsupported(f"relative to a fixed member ({spec.get('diff-options')})")
        values = [x.text or "" for x in spec.el.findall("address/value")] if spec.el is not None else []
        try:
            n = int(values[0]) if values else -1
        except ValueError:
            raise Unsupported(f"relative to {values[0]!r}") from None
        cur = f"coalesce({v}, 0)"
        prev = lookup(cur, n, w)
        if t == "Difference":
            return f"{cur} - {prev}"
        if t == "PctDiff":
            return f"1.0 * ({cur} - {prev}) / nullif(abs({prev}), 0)"
        return f"1.0 * {cur} / nullif({prev}, 0)"
    if t in ("Rank", "PctRank"):
        kind, _, direction = (spec.get("rank-options") or "Competition,Descending").partition(",")
        desc = (direction or "Descending") == "Descending"
        if t == "PctRank":
            if kind != "Competition":
                raise Unsupported(f"percentile with {kind} ranks")
            return percentile(v, w, desc)
        if kind not in RANKS:
            raise Unsupported(f"{kind} rank")
        return rank(v, w, kind.lower(), desc)
    if t == "WindowTotal":
        agg = RUNNING.get(spec.get("aggregation") or "Sum")
        opts = set(filter(None, (spec.get("window-options") or "").split(",")))
        if agg is None or opts - {"IncludeCurrent"}:
            raise Unsupported(f"moving {spec.get('aggregation')} with {spec.get('window-options') or 'no options'}")
        try:
            lo, hi = int(spec.get("from") or "-2"), int(spec.get("to") or "0")
        except ValueError:
            raise Unsupported("moving window bounds") from None
        if "IncludeCurrent" not in opts:  # the window without the current mark: only when it ends or starts there
            if hi == 0 and lo < 0:
                hi = -1
            elif lo == 0 and hi > 0:
                lo = 1
            elif lo > 0 or hi < 0:
                pass
            else:
                raise Unsupported("a moving window that leaves out the current mark in its middle")
        if lo > hi:
            raise Unsupported("moving window bounds")
        return f"{agg}({v}) {w.over(f'rows between {bound(lo, False)} and {bound(hi, True)}')}"
    raise Unsupported(f"quick table calculation {t or '(none)'}")


def label(spec: Spec, display: str) -> str:
    tmpl = LABELS.get(spec.type, "{x}")
    return tmpl.format(x=display, agg=AGG_WORD.get(spec.get("aggregation") or "Sum", spec.get("aggregation")))
