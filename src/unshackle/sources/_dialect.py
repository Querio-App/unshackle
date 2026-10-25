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

- Calculation functions (the Postgres form on the left): day differences `(cast(b as date) - cast(a as date))` (BigQuery
  `date_diff`, Databricks `datediff`); date arithmetic `(x + (n) * interval '1 month')` (Snowflake and Redshift
  `dateadd`, BigQuery `datetime_add`, Databricks `timestampadd`); `make_date`; `regexp_like(x, p)`, `(regexp_match(x, p))[1]` and
  `regexp_replace(x, p, r, 'g')` (each warehouse's regex functions; DuckDB's `regexp_extract`); `position(a in b)`
  (BigQuery `strpos`); `log(x)` base 10 and `log(b, x)`; `split_part` (BigQuery `split`); `double precision`; Snowflake
  and Redshift string literals escape backslashes; BigQuery casts to `datetime` and Databricks to `timestamp_ntz`
  (a wall-clock time, as Tableau's), not `timestamp`.

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
    if warehouse not in DIALECTS:
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
                g = self.group()
                out.append(g if g is not None else "(" + self.until({")"}) + self.take(")"))
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
        if self.w in ("snowflake", "redshift"):  # a backslash escapes in their literals
            return t.replace("\\", "\\\\")
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
        if name == "timestamp" and self.w in ("bigquery", "databricks") and self.peek() == ")":  # cast(x as timestamp): theirs is an instant
            return "datetime" if self.w == "bigquery" else "timestamp_ntz"
        if name == "double" and self.peek().lower() == "precision" and self.w in ("bigquery", "databricks"):
            while self.toks[self.i].isspace():
                self.i += 1
            self.i += 1
            return "float64" if self.w == "bigquery" else "double"
        if self.peek() != "(":
            return t
        if name in FUNCS and self.w in FUNCS[name]:
            save = self.i
            form = FUNCS[name][self.w](self.args())
            if form is None:
                self.i = save
                return t
            return form
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

    def group(self) -> str | None:
        """A parenthesized calculation form (see GROUPS) rewritten for the warehouse, or None to rewrite it token by token.
        Called with the '(' taken."""
        depth, j = 0, self.i
        while j < len(self.toks) and (self.toks[j] != ")" or depth):
            depth += {"(": 1, ")": -1}.get(self.toks[j], 0)
            j += 1
        if j == len(self.toks):
            return None
        inner, after = self.toks[self.i:j], j + 1
        subscript = self.toks[after:after + 3] == ["[", "1", "]"]
        for rule in GROUPS:
            form = rule(self, inner, subscript)
            if form is not None:
                self.i = after + (3 if rule is _extract_group else 0)
                return form
        return None

    def sub(self, toks: list[str]) -> str:
        return _Rewriter("".join(toks).strip(), self.w).run()


def _split(toks: list[str], op: str) -> list[list[str]]:
    """Tokens split at top-level `op`."""
    parts: list[list[str]] = [[]]
    depth = 0
    for t in toks:
        depth += {"(": 1, ")": -1}.get(t, 0)
        if t == op and depth == 0:
            parts.append([])
        else:
            parts[-1].append(t)
    return parts


def _strip(toks: list[str]) -> list[str]:
    a, b = 0, len(toks)
    while a < b and toks[a].isspace():
        a += 1
    while b > a and toks[b - 1].isspace():
        b -= 1
    return toks[a:b]


def _call(toks: list[str], name: str) -> list[str] | None:
    """The tokens inside `name(...)` when the tokens are exactly that call."""
    toks = _strip(toks)
    if len(toks) < 3 or toks[0].lower() != name or toks[1] != "(" or toks[-1] != ")":
        return None
    depth = 0
    for k, t in enumerate(toks[1:], 1):
        depth += {"(": 1, ")": -1}.get(t, 0)
        if depth == 0 and k != len(toks) - 1:
            return None
    return toks[2:-1]


def _cast_to(toks: list[str], typ: str) -> bool:
    inner = _call(toks, "cast")
    words = [t.lower() for t in _strip(inner or []) if not t.isspace()]
    return inner is not None and words[-2:] == ["as", typ]


def _days_group(r: _Rewriter, inner: list[str], _sub: bool) -> str | None:
    """(cast(b as date) - cast(a as date)): days between two dates."""
    parts = _split(inner, "-")
    if r.w not in ("bigquery", "databricks") or len(parts) != 2 or not all(_cast_to(p, "date") for p in parts):
        return None
    b, a = r.sub(parts[0]), r.sub(parts[1])
    return f"date_diff({b}, {a}, day)" if r.w == "bigquery" else f"datediff({b}, {a})"


_INTERVAL = re.compile(r"'(\d+) (year|month|day|hour|minute|second)'")


def _dateadd_group(r: _Rewriter, inner: list[str], _sub: bool) -> str | None:
    """(x + (n) * interval 'k unit'): x plus n times k units (months clamp to the month's last day)."""
    parts = _split(inner, "+")
    last = [t for t in parts[-1] if not t.isspace()]
    if r.w not in ("snowflake", "redshift", "bigquery", "databricks") or len(parts) < 2 or len(last) < 6:
        return None
    m = _INTERVAL.fullmatch(last[-1])
    if last[-3:-1] != ["*", "interval"] or m is None or _call(last[:-3], "") is not None or last[0] != "(" or last[-4] != ")":
        return None
    n = r.sub(_strip(parts[-1])[:_strip(parts[-1]).index("*")])
    x = r.sub([t for p in parts[:-1] for t in p + ["+"]][:-1])
    k, unit = int(m.group(1)), m.group(2)
    n = n if k == 1 else f"{k} * {n}"
    if r.w == "bigquery":
        return f"datetime_add(cast({x} as datetime), interval {n} {unit})"
    return f"{'timestampadd' if r.w == 'databricks' else 'dateadd'}({unit}, {n}, {x})"


def _extract_group(r: _Rewriter, inner: list[str], subscript: bool) -> str | None:
    """(regexp_match(x, p))[1]: the first capturing group of the first match, NULL when nothing matches."""
    args = _call(inner, "regexp_match")
    if not subscript or args is None or r.w == "postgres":
        return None
    parts = _split(args, ",")
    if len(parts) != 2:
        return None
    x, p = r.sub(parts[0]), r.sub(parts[1])
    return {"duckdb": f"case when regexp_matches({x}, {p}) then regexp_extract({x}, {p}, 1) end",
            "bigquery": f"regexp_extract({x}, {p})", "snowflake": f"regexp_substr({x}, {p}, 1, 1, 'e', 1)",
            "redshift": f"case when {x} ~ {p} then regexp_substr({x}, {p}, 1, 1, 'e') end",  # '' when nothing matches
            "databricks": f"case when {x} rlike {p} then regexp_extract({x}, {p}, 1) end"}[r.w]


def _datetime_group(r: _Rewriter, inner: list[str], _sub: bool) -> str | None:
    """(cast(d as date) + cast(t as time)): the date at the time of day."""
    parts = _split(inner, "+")
    if r.w not in ("snowflake", "bigquery", "databricks") or len(parts) != 2 or not (_cast_to(parts[0], "date") and _cast_to(parts[1], "time")):
        return None
    if r.w == "databricks":
        raise Unsupported("MAKEDATETIME: Databricks has no time type")
    d, t = r.sub(parts[0]), r.sub(parts[1])
    return f"{'timestamp_from_parts' if r.w == 'snowflake' else 'datetime'}({d}, {t})"


GROUPS = (_days_group, _dateadd_group, _extract_group, _datetime_group)


def _log(w: str):
    def form(a: list[str]) -> str | None:
        if len(a) == 1:
            return {"snowflake": f"log(10, {a[0]})", "bigquery": f"log10({a[0]})", "databricks": f"log10({a[0]})"}.get(w)
        if w == "redshift":
            raise Unsupported("LOG with a base: Redshift's LOG is base 10 only")
        return {"postgres": f"log(cast({a[0]} as numeric), cast({a[1]} as numeric))", "bigquery": f"log({a[1]}, {a[0]})"}.get(w)
    return form


def _regexp_replace(w: str):
    def form(a: list[str]) -> str | None:
        if len(a) != 4 or a[3] != "'g'":
            return None
        if w in ("databricks", "redshift") and "\\" in a[2]:
            raise Unsupported(f"REGEXP_REPLACE with backreferences: write them as {w.capitalize()} does")
        return f"regexp_replace({a[0]}, {a[1]}, {a[2]})"  # every match is replaced
    return form


# function -> {warehouse: rewrite of its rewritten arguments (None: keep the call)}
FUNCS = {
    "position": {"bigquery": lambda a: f"strpos({m.group(2)}, {m.group(1)})" if len(a) == 1 and (m := re.fullmatch(r"(.*?)\s+in\s+(.*)", a[0], re.S | re.I)) else None},
    "log": {w: _log(w) for w in ("snowflake", "bigquery", "databricks", "redshift", "postgres")},
    "regexp_replace": {w: _regexp_replace(w) for w in ("snowflake", "redshift", "bigquery", "databricks")},
    "regexp_like": {  # p matches somewhere in x (Postgres 15 has regexp_like; ~ is older)
        "postgres": lambda a: f"({a[0]} ~ {a[1]})", "redshift": lambda a: f"({a[0]} ~ {a[1]})",
        "duckdb": lambda a: f"regexp_matches({a[0]}, {a[1]})", "snowflake": lambda a: f"(regexp_instr({a[0]}, {a[1]}) > 0)",
        "bigquery": lambda a: f"regexp_contains({a[0]}, {a[1]})", "databricks": lambda a: f"({a[0]} rlike {a[1]})"},
    "split_part": {"bigquery": lambda a: f"case when {a[0]} is null or {a[1]} is null then null else coalesce(split({a[0]}, {a[1]})[safe_ordinal({a[2]})], '') end"},
    "make_date": {"snowflake": lambda a: f"date_from_parts({', '.join(a)})", "bigquery": lambda a: f"date({', '.join(a)})",
                  "redshift": lambda a: "to_date(" + " || '-' || ".join(f"cast({x} as varchar)" for x in a) + ", 'YYYY-MM-DD')"},
    "trunc": {"databricks": lambda a: f"case when {a[0]} < 0 then ceil({a[0]}) else floor({a[0]}) end" if len(a) == 1 else None},  # numbers
}
