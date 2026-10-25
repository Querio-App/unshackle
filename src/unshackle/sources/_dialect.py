"""SQL the Tableau source writes, in the warehouse's dialect.

The Tableau source writes tile and LOD model SQL in one form (Postgres and DuckDB run it as written). For the other
warehouse types, `render(sql, warehouse)` rewrites only what those warehouses read differently, keeping the meaning
the Postgres form has:

- BigQuery, Databricks: identifiers are quoted with backticks (a double-quoted name is a string there) and string
  literals escape with a backslash; BigQuery writes `date_trunc(x, MONTH)`, and `extract` parts are mapped to the
  same numbers (ISO week, Sunday = 0 day of week);
- BigQuery, Databricks: `interval '1 day'` is written `interval 1 day`, and `cast(x as varchar)` `cast(x as string)`;
- Postgres has no `median()`: `percentile_cont(0.5) within group (order by x)`; its `round()` of a double takes no
  digits, so `round(x, n)` rounds a numeric (halves away from zero), as Tableau's Postgres connection does;
- Redshift keeps the scale of a DECIMAL input in `avg()` and in decimal division, truncating the rest, and averages
  integers as integers: `avg()` and the `1.0 * a / nullif(b, 0)` divisions are computed in double precision, as
  Tableau computes them.
- Postgres: the `1.0 * a / nullif(b, 0)` divisions are in double precision too, as Tableau's Postgres connection sends
  them (`CAST(a AS DOUBLE PRECISION) / b`): a quotient then rounds and compares as it does in Tableau.

Snowflake, Redshift, Databricks and DuckDB support `is not distinct from` as written. Anything without a faithful form
raises Unsupported, and the caller leaves the SQL as a TODO. Text inside `keep` (custom SQL, already in the
warehouse's dialect) is never touched.
"""

from __future__ import annotations

import re

DIALECTS = ("postgres", "redshift", "snowflake", "bigquery", "databricks", "duckdb")

_TOKEN = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|\s+|.""", re.S)

# extract(<part> from x) as Postgres numbers it -> the dialect's form ({0} is x)
_EXTRACT = {
    "bigquery": {"dow": "(extract(dayofweek from {0}) - 1)", "isodow": "(mod(extract(dayofweek from {0}) + 5, 7) + 1)",
                 "week": "extract(isoweek from {0})", "doy": "extract(dayofyear from {0})"},
    "databricks": {"dow": "(extract(dow from {0}) - 1)", "isodow": "extract(dow_iso from {0})"},
}
_BQ_UNITS = {"year", "quarter", "month", "week", "day", "hour", "minute", "second"}


class Unsupported(ValueError):
    pass


def render(sql: str, warehouse: str, keep: tuple[str, ...] = ()) -> str:
    """`sql` for `warehouse` (an unshackle.toml type); unchanged for Postgres-compatible SQL and unknown types."""
    if warehouse not in ("postgres", "redshift", "bigquery", "databricks"):
        return sql
    kept = sorted((k for k in dict.fromkeys(keep) if k and k in sql), key=len, reverse=True)
    for i, k in enumerate(kept):
        sql = sql.replace(k, f"__unshackle_keep_{i}__")
    sql = _Rewriter(sql, warehouse).run()
    for i, k in enumerate(kept):
        sql = sql.replace(f"__unshackle_keep_{i}__", k)
    return sql


class _Rewriter:
    def __init__(self, sql: str, warehouse: str):
        self.toks = _TOKEN.findall(sql)
        self.i = 0
        self.w = warehouse
        self.backticks = warehouse in ("bigquery", "databricks")

    def run(self) -> str:
        return self.until(None)

    def peek(self, skip_ws: bool = True) -> str:
        j = self.i
        while skip_ws and j < len(self.toks) and self.toks[j].isspace():
            j += 1
        return self.toks[j] if j < len(self.toks) else ""

    def until(self, stop: set[str] | None) -> str:
        """Rewrite tokens up to (not including) a top-level token in `stop`, or to the end."""
        out: list[str] = []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            if stop is not None and t.lower() in stop:
                break
            self.i += 1
            if t == "(":
                out.append("(" + self.until({")"}) + self.take(")"))
            elif t.startswith('"'):
                out.append(self.ident(t))
            elif t.startswith("'"):
                out.append(self.string(t))
            elif t[0].isalpha() or t[0] == "_":
                out.append(self.word(t))
            elif t == "1.0" and self.w in ("redshift", "postgres") and self.peek() == "*":
                out.append("cast(1.0 as double precision)")
            else:
                out.append(t)
        return "".join(out)

    def take(self, tok: str) -> str:
        if self.i < len(self.toks) and self.toks[self.i] == tok:
            self.i += 1
            return tok
        raise Unsupported(f"unbalanced SQL: expected {tok!r}")

    def args(self) -> list[str]:
        """The rewritten, comma-separated arguments of the call whose '(' is next."""
        while self.toks[self.i].isspace():
            self.i += 1
        self.take("(")
        out = [self.until({",", ")"}).strip()]
        while self.toks[self.i] == ",":
            self.i += 1
            out.append(self.until({",", ")"}).strip())
        self.take(")")
        return out

    def ident(self, t: str) -> str:
        if not self.backticks:
            return t
        return "`" + t[1:-1].replace('""', '"').replace("`", "``" if self.w == "databricks" else "\\`") + "`"

    def string(self, t: str) -> str:
        if not self.backticks:
            return t
        return "'" + t[1:-1].replace("''", "'").replace("\\", "\\\\").replace("'", "\\'") + "'"

    def word(self, t: str) -> str:
        name = t.lower()
        if self.w in ("bigquery", "databricks"):
            m = re.fullmatch(r"'(\d+) (day|month)'", self.peek()) if name == "interval" else None
            if m:  # interval '1 day' -> interval 1 day
                while self.toks[self.i].isspace():
                    self.i += 1
                self.i += 1
                return f"interval {m.group(1)} {m.group(2)}"
            if name == "varchar" and self.peek() != "(":
                return "string"
        if self.peek() != "(":
            return t
        if name == "avg" and self.w == "redshift":
            a = self.args()
            return f"avg(cast({a[0]} as double precision))" if len(a) == 1 and not a[0].lower().startswith("distinct ") else f"avg({', '.join(a)})"
        if name == "round" and self.w == "postgres":
            a = self.args()
            return f"round(cast({a[0]} as numeric), {a[1]})" if len(a) == 2 else f"round({a[0]})"
        if name == "median" and self.w in ("postgres", "bigquery"):
            a = self.args()
            if self.w == "bigquery":
                raise Unsupported("MEDIAN: BigQuery has no exact median aggregate (PERCENTILE_CONT is a window function there)")
            return f"percentile_cont(0.5) within group (order by {a[0]})"
        if name == "date_trunc" and self.w == "bigquery":
            a = self.args()
            unit = a[0].strip("'").lower() if len(a) == 2 and re.fullmatch(r"'[a-z]+'", a[0].lower()) else ""
            if unit not in _BQ_UNITS:
                raise Unsupported(f"date_trunc({a[0]}, ...) has no BigQuery form")
            return f"date_trunc({a[1]}, {'week(monday)' if unit == 'week' else unit})"
        if name == "extract" and self.w in _EXTRACT:
            save = self.i
            a = self.args()
            m = re.fullmatch(r"(\w+)\s+from\s+(.*)", a[0], re.S | re.I) if len(a) == 1 else None
            if m is None:
                self.i = save
                return t
            form = _EXTRACT[self.w].get(m.group(1).lower())
            return form.format(m.group(2)) if form else f"extract({m.group(1)} from {m.group(2)})"
        return t
