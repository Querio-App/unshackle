"""Tableau's analytics pane on a worksheet: reference lines and bands, totals, trend lines and forecasts.

Tableau computes a reference line from the marks of the sheet, after every filter: `average`, `median`, `min`, `max`
and `sum` of the mark values within the line's scope, `total` the sheet's aggregate recomputed over the scope's rows.
The scope is the whole table, a pane or a cell. A cell is one combination of the discrete dimensions on Rows and
Columns; a pane is a cell without the innermost dimension of a shelf whose last field is that dimension (the one that
lays the marks out inside a pane); a continuous field (an axis) splits nothing. The tile SQL keeps its rows and adds
one window column per line over them: `avg(tile.sales) over (partition by tile.region)`.

Totals (`<rows total='true'>`: a Grand Total row under the table; `<cols total='true'>`: a Grand Total column;
`<subtotals>`: a Total per value of each listed dimension) recompute the sheet's aggregate at the total's level
(Tableau's "Automatic"), so they are extra grouping sets of the same query.

Trend lines and forecasts are models Tableau fits on the marks: no SQL; their settings go in a TODO.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# reference line formula -> SQL window function over the marks
WINDOW = {"average": "avg", "median": "median", "min": "min", "max": "max", "sum": "sum"}
COMPUTATION = {"average": "Average", "median": "Median", "min": "Minimum", "max": "Maximum", "sum": "Sum", "total": "Total",
               "constant": "Constant"}
# Tableau's `total` recomputes the aggregate over the scope's rows; from the marks only when they add up to it
TOTAL_OF_MARKS = {"sum": "sum", "cnt": "sum", "min": "min", "max": "max"}
SCOPE = {"per-table": "table", "per-pane": "pane", "per-cell": "cell"}
DISTRIBUTIONS = {"quantiles": "quantiles", "percentiles": "percentiles", "stdev": "standard deviation", "confidence": "confidence interval"}

TREND_FIT = {"linear": ("linear", "linear"), "log": ("logarithmic", "log"), "exp": ("exponential", "exp"),
             "polynomial": ("polynomial", "poly"), "power": ("power", "pow")}
_TREND_SKIP = {"enabled", "enable-instant-analytics", "fit"}
_FORECAST = {
    "trend-type": "trend",
    "season-type": "season",
    "model-type": "model",
    "range-type": "length",
    "forecast-length": "periods",
    "range-units": "length unit",
    "ignore-last": "ignore last periods",
    "fill-type": "missing values",
    "band-confidence-level": "prediction interval %",
    "show-prediction-bands": "prediction bands",
    "auto-forecast-agg": "automatic aggregation",
}


def reference_lines(panes: list[ET.Element]) -> list[ET.Element]:
    """The sheet's `<reference-line>`s, once each (a line on a shared axis can repeat in several panes)."""
    seen: dict[tuple, ET.Element] = {}
    for p in panes:
        for rl in p.findall("reference-line"):
            key = tuple(sorted(rl.attrib.items())) + tuple(ET.tostring(c) for c in rl)
            seen.setdefault(key, rl)
    return list(seen.values())


def label(rl: ET.Element, computation: str, percentage: str = "") -> str:
    """The line's label with "{value}" for its value ("" when it shows none)."""
    kind = rl.get("label-type") or "automatic"
    if kind == "none":
        return ""
    if kind == "custom":
        text = (rl.get("label") or "").replace("{", "{{").replace("}", "}}")
        return text.replace("<Value>", "{value}").replace("<Computation>", computation)
    if kind == "computation":
        return computation
    if percentage:
        return f"{percentage}% of {computation}"
    return computation or "{value}"  # Tableau's automatic label names the computation


def trend_todo(tl: ET.Element, x: str, y: str) -> str:
    fit = tl.get("fit") or "linear"
    name, method = TREND_FIT.get(fit, (fit, ""))
    opts = [f"{k}={v}" for k, v in sorted(tl.attrib.items()) if k not in _TREND_SKIP]
    how = f"transform_regression({y!r}, {x!r}, method={method!r})" if method else "a regression"
    return (f"Tableau trend line, {name} model of {y} over {x} ({', '.join(opts)}): no SQL computes it; the tile keeps the "
            f"data, so fit it in the chart cell ({how}, per colour unless exclude-color is true) and compare the "
            "coefficients with Tableau's Describe Trend Model")


def forecast_todo(fs: ET.Element, measures: list[str], dates: list[str]) -> str:
    opts = [f"{_FORECAST.get(k, k)}: {v}" for k, v in sorted(fs.attrib.items()) if k != "enabled"]
    what = f" of {', '.join(measures)}" if measures else ""
    over = f" over {', '.join(dates)}" if dates else ""
    ignored = fs.get("ignore-last") or "0"
    hidden = (f"; Tableau also leaves the last {ignored} period(s) of actual values out of the view and the model "
              "(Ignore last), which the SQL still returns") if ignored not in ("0", "") else ""
    return (f"Tableau forecast{what}{over} (exponential smoothing; {'; '.join(opts)}): no SQL computes it; the tile keeps "
            f"the actual values only{hidden}; fit the model in a notebook cell (statsmodels ExponentialSmoothing with that "
            "trend and season) and append its rows and prediction interval")


def grouping_sets(rows: list[str], cols: list[str], rows_total: bool, cols_total: bool, subtotals: set[str]) -> list[list[str]]:
    """Grouping sets (dimension instances) for a sheet's totals: each level of Rows crossed with each level of Columns.
    A shelf's levels: all its dimensions, the outer ones up to each subtotalled dimension, and none for a grand total."""

    def levels(dims: list[str], total: bool) -> list[list[str]]:
        out = [dims]
        out += [dims[: i + 1] for i, d in enumerate(dims[:-1]) if d in subtotals]
        if total and dims:
            out.append([])
        return out

    sets = [r + c for r in levels(rows, rows_total) for c in levels(cols, cols_total)]
    return sets if len(sets) > 1 else []


def split_with(sql: str) -> tuple[str, str]:
    """(the CTEs of a `with ... select` without "with ", the final select); ("", sql) when it has none."""
    if not sql.startswith("with "):
        return "", sql
    depth, quote, i = 0, "", 5
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = "" if sql[i + 1 : i + 2] != quote else quote
                i += 1 if quote else 0
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and not sql[i + 1 :].lstrip().startswith(","):
                return sql[5 : i + 1], sql[i + 1 :].lstrip("\n")
        i += 1
    return "", sql


def wrap(sql: str, items: list[str], grouped: list[tuple[str, str, list[str]]], n_dims: int) -> str:
    """The tile's query as the CTE `tile`, with `items` after its columns (same row order). `grouped`: (name, value
    expression, partition columns) of values computed per partition in their own CTE and joined back."""
    m = re.search(r"\norder by ((?:\d+, )*\d+)$", sql)
    if m:
        sql, order = sql[: m.start()], m.group(1)
    else:
        order = ", ".join(str(i) for i in range(1, n_dims + 1))
    ctes, body = split_with(sql)
    parts = ([ctes] if ctes else []) + [f"tile as (\n{body}\n)"]
    joins = []
    for name, value, by in grouped:
        keys = ", ".join(by)
        parts.append(f"{name} as (\nselect {keys + ', ' if by else ''}{value} as value\nfrom tile{chr(10) + 'group by ' + keys if by else ''}\n)")
        on = " and ".join(f"tile.{k} is not distinct from {name}.{k}" for k in by)
        joins.append(f"left join {name} on {on}" if by else f"cross join {name}")
    out = ["with " + ",\n".join(parts), "select", ",\n".join(["  tile.*"] + [f"  {x}" for x in items]), "from tile", *joins]
    if order:
        out.append(f"order by {order}")
    return "\n".join(out)
