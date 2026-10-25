"""Tableau calculation semantics -> SQL, for the Tableau source only.

The formula parser is shared (`_aggexpr`), but its SQL renderer follows Sigma's and ThoughtSpot's rules. Tableau's
differ, so Tableau formulas render here (the Postgres form; `_dialect` rewrites it for the other warehouses):

  * `+` of two strings concatenates (`||`); NULL on either side gives NULL.
  * `IIF(test, then, else[, unknown])` returns `unknown` (NULL when omitted) when `test` is NULL, while `IF` and `CASE`
    take ELSE. `x = TRUE` is NULL when `x` is NULL. `NULL` is a literal.
  * `/` divides as reals, NULL for a zero divisor (`1.0 * a / nullif(b, 0)`; double precision on Postgres and Redshift,
    as Tableau sends it there).
  * `ROUND` depends on the connection. Tableau sends it to a live connection as the warehouse's `round()` (on Postgres:
    a double's halves to even, a numeric's away from zero; digits on a numeric), so the SQL is the same `round()`.
    An extract (Hyper) rounds halves away from zero: `round(x, 0)`. With digits, the extract rounds the stored double,
    so a decimal tie (0.25 to 1 digit) can go either way: that stays a TODO.
  * Strings compare with the connection's collation: on a live connection, the warehouse's, which the SQL keeps. An
    extract compares by code point, case- and accent-sensitively, and its UPPER/LOWER/TRIM differ from most
    warehouses' for a few characters: the semantic model's description says so (NOTES).
  * Functions follow what Tableau computes, and where a live connection and an extract differ, each its own way:
    STARTSWITH/ENDSWITH ignore leading/trailing spaces on a live connection (Tableau trims them in the SQL it sends);
    MID with a negative length is '' live and NULL on an extract; STR of a real and INT/FLOAT of a string are the
    warehouse's cast live and stay TODO on an extract (Hyper formats and parses its own way); regular expressions are
    the warehouse's live, and on an extract (ICU) only patterns every engine reads alike are translated (_regex_ok).
    Out-of-domain inputs give NULL (SQRT/LN/LOG of x <= 0, POWER of a negative to a fraction, LEFT/RIGHT/SPACE of a
    negative count); DATEDIFF counts boundaries crossed; DATEADD returns a datetime; MAKEDATE rolls over (Feb 29, 2023
    is Mar 1); IN is FALSE, never NULL, for a NULL value.
  * MEDIAN interpolates between the two middle values of an even count; AVG of integers is a real; COUNT and COUNTD
    skip NULLs. The aggregates' SQL is in tableau.AGG_SQL.

FUNCS is the function table: add a row to translate another Tableau function, and its cases to
tests/fixtures/tableau_calc/cases.yaml (run in DuckDB, and in Postgres when available).
"""

from __future__ import annotations

import re
from typing import Callable

from unshackle.sources import _aggexpr as ax

NUMERIC = {"integer", "real"}

# Tableau function -> (SQL template, arity or None for variadic, result type; "" = the first argument's type).
# {0}, {1}: the arguments' SQL; {args}: all of them, comma-separated.
FUNCS: dict[str, tuple[str, int | None, str]] = {
    "abs": ("abs({0})", 1, ""),
    "ceiling": ("ceil({0})", 1, "integer"),
    "floor": ("floor({0})", 1, "integer"),
    "zn": ("coalesce({0}, 0)", 1, ""),
    "ifnull": ("coalesce({0}, {1})", 2, ""),
    "isnull": ("({0} is null)", 1, "boolean"),
    "upper": ("upper({0})", 1, "string"),
    "lower": ("lower({0})", 1, "string"),
    "trim": ("trim({0})", 1, "string"),
    "len": ("length({0})", 1, "integer"),
    "date": ("cast({0} as date)", 1, "date"),
    "year": ("extract(year from {0})", 1, "integer"),
    "month": ("extract(month from {0})", 1, "integer"),
    "day": ("extract(day from {0})", 1, "integer"),
    "ltrim": ("ltrim({0})", 1, "string"),
    "rtrim": ("rtrim({0})", 1, "string"),
    "replace": ("replace({0}, {1}, {2})", 3, "string"),
    "contains": ("(position({1} in {0}) > 0)", 2, "boolean"),
    "sqrt": ("sqrt(case when {0} >= 0 then {0} end)", 1, "real"),
    "ln": ("ln(case when {0} > 0 then {0} end)", 1, "real"),
    "exp": ("exp({0})", 1, "real"),
    "sign": ("sign({0})", 1, "integer"),
    "today": ("current_date", 0, "date"),
}

# Functions rendered by Renderer.special (their arguments decide the SQL), with their result type ("" = the first
# argument's type). "in" is the `x IN (a, b)` operator; "min2"/"max2" are the two-argument MIN/MAX.
SPECIAL: dict[str, str] = {
    "datediff": "integer", "dateadd": "datetime", "datepart": "integer", "datename": "string", "makedate": "date",
    "makedatetime": "datetime", "in": "boolean", "startswith": "boolean", "endswith": "boolean", "find": "integer",
    "left": "string", "right": "string", "mid": "string", "split": "string", "space": "string", "str": "string",
    "int": "integer", "float": "real", "div": "integer", "power": "real", "log": "real", "min2": "", "max2": "",
    "datetime": "datetime", "regexp_match": "boolean", "regexp_extract": "string", "regexp_replace": "string",
}
# Tableau functions that stay TODO, with what to write instead.
UNTRANSLATED = {
    "now": "NOW() is Tableau's clock when the query runs, in Tableau's time zone (a live connection sends it as a timestamp "
           "literal): write `localtimestamp` (or `current_timestamp at time zone '<zone>'`) in the zone Tableau uses",
    "isdate": "ISDATE parses the string with the workbook's locale and date formats: write a `case` that tests the formats the "
              "data uses (or a warehouse `try_cast`)",
    "dateparse": "DATEPARSE takes an ICU date format: write the warehouse's `to_timestamp(x, '<format>')` (Postgres patterns "
                 "differ from ICU's) or a `try_` form so text that doesn't parse gives NULL, as in Tableau",
}
DATEDIFF_UNITS = ("year", "quarter", "month", "week", "day", "dayofyear", "weekday", "hour", "minute", "second")
DATEADD_UNITS = {"year": "1 year", "quarter": "3 month", "month": "1 month", "week": "7 day", "day": "1 day",
                 "dayofyear": "1 day", "weekday": "1 day", "hour": "1 hour", "minute": "1 minute", "second": "1 second"}
# DATEPART of these units (year, month, day are YEAR/MONTH/DAY; week, weekday, quarter follow the Date Properties)
CLOCK_PARTS = {"hour": "extract(hour from cast({0} as timestamp))", "minute": "extract(minute from cast({0} as timestamp))",
               "second": "floor(extract(second from cast({0} as timestamp)))", "dayofyear": "extract(doy from {0})",
               "day": "extract(day from {0})", "month": "extract(month from {0})", "year": "extract(year from {0})"}

# Where a Tableau extract's result can differ from the warehouse's, for the semantic model's description.
NOTES = {
    "compare": "Tableau's extract compares strings by code point (case- and accent-sensitive, 'B' < 'a'); the migrated SQL "
               "compares with the warehouse's collation, which can differ for `=`, `<`, MIN/MAX and CASE on strings.",
    "case": "Tableau's extract UPPER/LOWER use full Unicode case mapping ('ß' -> 'SS', a final 'Σ' -> 'ς'); the warehouse's "
            "upper()/lower() may map those few characters differently.",
    "trim": "Tableau's extract TRIM (and LTRIM, RTRIM) also removes tabs and line breaks; the warehouse's trim() removes "
            "spaces only.",
    "regex_dot": "Tableau's extract matches line breaks with `.` in REGEXP_ functions; DuckDB, Snowflake, BigQuery and "
                 "Databricks don't, so a string with line breaks can match differently there.",
    "today": "Tableau's extract evaluates TODAY() on Tableau's clock and time zone; the migrated SQL uses the warehouse's "
             "current_date, in its session time zone.",
}


def type_of(node: ax.Node, dtype: Callable[[str], str]) -> str:
    """Tableau's result type of an expression: string, integer, real, date, datetime, boolean, or "" (unknown)."""
    if isinstance(node, ax.Ref):
        return dtype(node.name)
    if isinstance(node, ax.Str):
        return "string"
    if isinstance(node, ax.Num):
        return "" if node.value == "null" else "real" if "." in node.value else "integer"
    if isinstance(node, ax.Bool):
        return "boolean"
    if isinstance(node, ax.Unary):
        return "boolean" if node.op == "not" else type_of(node.operand, dtype)
    if isinstance(node, ax.BinOp):
        if node.op in ("and", "or", "=", "<>", "<", ">", "<=", ">="):
            return "boolean"
        left, right = type_of(node.left, dtype), type_of(node.right, dtype)
        if node.op == "+" and "string" in (left, right):
            return "string"
        if node.op == "||":
            return "string"
        if node.op == "/":
            return "real"
        if left in NUMERIC and right in NUMERIC:
            return "integer" if left == right == "integer" else "real"
        if node.op in ("+", "-") and left in ("date", "datetime") and right in NUMERIC:
            return left
        return ""
    if isinstance(node, ax.Call):
        if node.name == "if":
            for i, a in enumerate(node.args):
                if i % 2 == 1 or i == len(node.args) - 1:
                    t = type_of(a, dtype)
                    if t:
                        return t
            return ""
        spec = FUNCS.get(node.name)
        if spec is not None:
            return spec[2] or (type_of(node.args[0], dtype) if node.args else "")
        if node.name in SPECIAL:
            return SPECIAL[node.name] or (type_of(node.args[0], dtype) if node.args else "")
        if node.name == "round":
            return type_of(node.args[0], dtype) if node.args else ""
        if node.name == "iif" and len(node.args) >= 3:
            return type_of(node.args[1], dtype) or type_of(node.args[2], dtype)
    return ""


class Renderer:
    """Row-level SQL for a Tableau expression. `ref` maps a field to SQL, `dtype` to its Tableau datatype; `hyper`: the
    data source is an extract. `notes` collects NOTES keys that apply."""

    def __init__(self, ref: Callable[[str], str], dtype: Callable[[str], str] = lambda _: "", hyper: bool = False):
        self.ref, self.dtype, self.hyper = ref, dtype, hyper
        self.notes: set[str] = set()

    def __call__(self, node: ax.Node) -> str:
        return self.sql(node)

    def sql(self, node: ax.Node) -> str:
        if isinstance(node, ax.Ref):
            return self.ref(node.name)
        if isinstance(node, ax.Num):
            return node.value
        if isinstance(node, ax.Str):
            return ax._sql_str(node.value)
        if isinstance(node, ax.Bool):
            return "true" if node.value else "false"
        if isinstance(node, ax.Unary):
            inner = self.sql(node.operand)
            return f"not ({inner})" if node.op == "not" else f"-{inner}"
        if isinstance(node, ax.BinOp):
            return self.binop(node)
        if isinstance(node, ax.Call):
            return self.call(node)
        raise ax.FormulaError(f"cannot translate {node!r}")

    def operand(self, node: ax.Node) -> str:
        """An operand's SQL, in parentheses when it is a comparison or a product (`(x > 1) = true`, `1.0 * (a % 2)`)."""
        sql = self.sql(node)
        return f"({sql})" if isinstance(node, ax.BinOp) and node.op in ("*", "/", "%", "=", "<>", "<", ">", "<=", ">=") else sql

    def binop(self, node: ax.BinOp) -> str:
        if node.op in ("and", "or"):
            left, right = self.sql(node.left), self.sql(node.right)
        else:
            left, right = self.operand(node.left), self.operand(node.right)
        if node.op in ("and", "or"):
            return f"({left} {node.op} {right})"
        if node.op == "/":
            return f"1.0 * {left} / nullif({right}, 0)"
        if node.op == "+" and "string" in (type_of(node.left, self.dtype), type_of(node.right, self.dtype)):
            return f"({left} || {right})"
        if node.op in ("+", "-", "||"):
            return f"({left} {node.op} {right})"
        if self.hyper and node.op in ("=", "<>", "<", ">", "<=", ">=") and "string" in (type_of(node.left, self.dtype), type_of(node.right, self.dtype)):
            self.notes.add(NOTES["compare"])
        return f"{left} {node.op} {right}"

    def call(self, node: ax.Call) -> str:
        name, args = node.name, node.args
        if name == "if":
            return self.case(args)
        if name == "iif":
            if len(args) not in (3, 4):
                raise ax.FormulaError("IIF takes three or four arguments")
            test = self.sql(args[0])
            unknown = f" else {self.sql(args[3])}" if len(args) == 4 else ""
            return f"case when {test} then {self.sql(args[1])} when not ({test}) then {self.sql(args[2])}{unknown} end"
        if name == "round":
            return self.round(args)
        if name in SPECIAL:
            return self.special(name, args)
        if name in UNTRANSLATED:
            raise ax.FormulaError(UNTRANSLATED[name])
        spec = FUNCS.get(name)
        if spec is None:
            # the shared renderer's functions (date functions among them), with the arguments rendered here
            rendered: dict[str, str] = {}

            def hole(a: ax.Node) -> ax.Node:
                if isinstance(a, (ax.Str, ax.Num, ax.Bool)):
                    return a
                rendered[f"\x00{len(rendered)}"] = self.sql(a)
                return ax.Ref(f"\x00{len(rendered) - 1}")

            return ax.to_sql(ax.Call(name, [hole(a) for a in args]), lambda r: rendered[r])
        tmpl, arity, _ = spec
        if self.hyper and name in ("upper", "lower", "trim", "ltrim", "rtrim", "today"):
            self.notes.add(NOTES["case" if name in ("upper", "lower") else "today" if name == "today" else "trim"])
        if arity is not None and len(args) != arity:
            raise ax.FormulaError(f"{name.upper()} takes {arity} argument(s)")
        parts = [self.sql(a) for a in args]
        return tmpl.format(*parts, args=", ".join(parts))

    def special(self, name: str, args: list[ax.Node]) -> str:
        """SQL for a SPECIAL function: Tableau's rule for its arguments' types, literals and edge values."""
        n = len(args)

        def want(*counts: int) -> None:
            if n not in counts:
                raise ax.FormulaError(f"{name.upper()} takes {' or '.join(map(str, counts))} arguments")

        def typ(i: int) -> str:
            return type_of(args[i], self.dtype)

        sql = [self.sql(a) for a in args]
        if name == "in":
            return f"coalesce({sql[0]} in ({', '.join(sql[1:])}), false)"
        if name in ("min2", "max2"):
            a, b = sql
            return f"case when {a} is null or {b} is null then null when {a} {'<' if name == 'min2' else '>'} {b} then {a} else {b} end"
        if name == "datediff":
            want(3, 4)
            unit = _unit(args[0], DATEDIFF_UNITS)
            a, b = sql[1], sql[2]
            if unit in ("year", "quarter", "month"):
                k = {"year": "", "quarter": " * 4", "month": " * 12"}[unit]
                diff = f"(extract(year from {b}) - extract(year from {a})){k}"
                if unit != "year":
                    diff += f" + extract({unit} from {b}) - extract({unit} from {a})"
                return f"cast({diff} as integer)"
            if unit == "week":  # parse_formula turns it into a `tableau:` function that follows the Week start
                raise ax.FormulaError("DATEDIFF('week') needs the datasource's Week start")
            days = f"cast((cast({b} as date) - cast({a} as date)) as integer)"
            if unit in ("day", "dayofyear", "weekday"):
                return days
            diff = days
            for part, k in (("hour", 24), ("minute", 60), ("second", 60)):
                diff = f"{diff if diff is days else f'({diff})'} * {k} + {CLOCK_PARTS[part].format(b)} - {CLOCK_PARTS[part].format(a)}"
                if part == unit:
                    return f"cast({diff} as integer)"
        if name == "dateadd":
            want(3)
            unit = _unit(args[0], tuple(DATEADD_UNITS))
            if typ(1) == "real":
                raise ax.FormulaError("DATEADD with a fractional increment: Tableau accepts only whole numbers")
            return f"cast(({sql[2]} + ({sql[1]}) * interval '{DATEADD_UNITS[unit]}') as timestamp)"
        if name in ("datepart", "datename"):
            want(2, 3)
            unit = _unit(args[0], tuple(CLOCK_PARTS))  # week, weekday, quarter (Date Properties) and month names: parse_formula
            if name == "datename" and unit in ("month", "year"):
                raise ax.FormulaError(f"DATENAME('{unit}') follows the locale and the Date Properties")
            part = f"cast({CLOCK_PARTS[unit].format(sql[1])} as integer)"
            return part if name == "datepart" else f"cast({part} as varchar)"
        if name == "datetime":
            want(1)
            ok = typ(0) in ("date", "datetime") or (isinstance(args[0], ax.Str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?", args[0].value))
            if not ok:
                raise ax.FormulaError("DATETIME of a string parses it with the workbook's locale: write the warehouse's `to_timestamp(x, '<format>')`")
            return f"cast({sql[0]} as timestamp)"
        if name == "makedate":
            want(3)
            y, m, d = sql
            # Tableau rolls an out-of-range month or day over (MAKEDATE(2023, 2, 29) is March 1)
            return f"cast((((make_date(cast({y} as integer), 1, 1) + ({m} - 1) * interval '1 month') + ({d} - 1) * interval '1 day')) as date)"
        if name == "makedatetime":
            want(2)
            return f"cast((cast({sql[0]} as date) + cast({sql[1]} as time)) as timestamp)"
        if name in ("startswith", "endswith"):
            want(2)
            s, sub = sql
            if name == "startswith":  # a live connection ignores the string's leading spaces, an extract doesn't
                return f"(left({s if self.hyper else f'ltrim({s})'}, length({sub})) = {sub})"
            return f"(right({s if self.hyper else f'rtrim({s})'}, length({sub})) = {sub})"
        if name == "find":
            want(2, 3)
            if n == 2:
                return f"position({sql[1]} in {sql[0]})"
            start = _count(args[2], sql[2], 1)
            at = f"position({sql[1]} in substr({sql[0]}, {start}))"
            return f"case when {sql[2]} is null then null when {at} = 0 then 0 else {at} + {start} - 1 end"
        if name in ("left", "right"):
            want(2)
            lit = _int_lit(args[1])
            if lit is not None:
                return f"{name}({sql[0]}, {lit})" if lit >= 0 else "cast(null as varchar)"
            return f"case when {sql[1]} >= 0 then {name}({sql[0]}, {_count(args[1], sql[1], 0)}) end"
        if name == "mid":
            want(2, 3)
            start = _count(args[1], sql[1], 1)
            if n == 2:
                return f"substr({sql[0]}, {start})"
            lit = _int_lit(args[2])
            if lit is not None and lit >= 0:
                return f"substr({sql[0]}, {start}, {lit})"
            if not self.hyper:  # a negative length gives '' on a live connection
                return f"substr({sql[0]}, {start}, {_count(args[2], sql[2], 0)})"
            return f"case when {sql[2]} >= 0 then substr({sql[0]}, {start}, cast(floor({sql[2]}) as integer)) end"
        if name == "split":
            want(3)
            token = _int_lit(args[2])
            if token is None or token == 0:
                raise ax.FormulaError("SPLIT's token number must be a whole number other than 0")
            if isinstance(args[1], ax.Str) and args[1].value == "":
                raise ax.FormulaError("SPLIT with an empty delimiter")
            if token > 0:
                return f"split_part({sql[0]}, {sql[1]}, {token})"
            if self.hyper and not (isinstance(args[1], ax.Str) and len(args[1].value) == 1):
                raise ax.FormulaError("SPLIT with a negative token and a delimiter longer than one character: an extract splits from "
                                      "the start, so `reverse(split_part(reverse(s), reverse(d), n))` can split overlapping "
                                      "delimiters differently ('xaaay' on 'aa')")
            return f"reverse(split_part(reverse({sql[0]}), reverse({sql[1]}), {-token}))"
        if name == "space":
            want(1)
            lit = _int_lit(args[0])
            if lit is not None:
                return f"repeat(' ', {lit})" if lit >= 0 else "cast(null as varchar)"
            return f"case when {sql[0]} >= 0 then repeat(' ', {_count(args[0], sql[0], 0)}) end"
        if name == "str":
            want(1)
            t = typ(0)
            if t == "string":
                return sql[0]
            if t in ("integer", "date") or (t == "real" and not self.hyper):
                return f"cast({sql[0]} as varchar)"  # a real live: the warehouse's text for it, as Tableau sends it
            raise ax.FormulaError(f"STR of a {t or 'value of unknown type'}: " + {
                "real": "an extract writes a real its own way (3 for 3.0, 1e-05 for 0.00001): write `cast(x as varchar)` if "
                        "the warehouse's form is acceptable",
                "datetime": "Tableau writes 'YYYY-MM-DD HH:MM:SS': write the warehouse's `to_char(x, ...)` for that form",
                "boolean": "a live connection writes 1/0 and an extract true/false: write a `case`",
            }.get(t, "write the cast for its type"))
        if name in ("int", "float"):
            want(1)
            t, x = typ(0), sql[0]
            if t == "boolean":
                return f"case when {x} then 1{'.0' if name == 'float' else ''} when not ({x}) then 0{'.0' if name == 'float' else ''} end"
            if name == "int":
                if t == "integer":
                    return x
                if t == "real":
                    return f"cast(trunc({x}) as bigint)"  # toward zero
                if t == "date":
                    return f"cast((cast({x} as date) - cast(date '1900-01-01' as date)) as integer)"  # days since 1900-01-01
                if t == "string" and not self.hyper:
                    return f"cast(trunc(cast({x} as double precision)) as bigint)"
            else:
                if t in NUMERIC or (t == "string" and not self.hyper):
                    return f"cast({x} as double precision)"
            if t == "string":
                raise ax.FormulaError(f"{name.upper()} of a string on an extract: Tableau gives NULL for text that isn't a number, "
                                      f"which a cast doesn't: write the warehouse's `try_cast` (Postgres: a `case` on `x ~ '^-?[0-9.]+$'`)")
            raise ax.FormulaError(f"{name.upper()} of a {t or 'value of unknown type'} is not translated")
        if name == "div":
            want(2)
            if "real" in (typ(0), typ(1)):
                raise ax.FormulaError("DIV takes integers")
            return f"cast(trunc(1.0 * {sql[0]} / nullif({sql[1]}, 0)) as bigint)"  # toward zero; NULL for 0
        if name == "power":
            want(2)
            x, y = sql
            return f"case when {x} < 0 and floor({y}) <> {y} then null when {x} = 0 and {y} < 0 then null else power({x}, {y}) end"
        if name == "log":
            want(1, 2)
            if n == 1:
                return f"log(case when {sql[0]} > 0 then {sql[0]} end)"  # base 10
            x, b = sql
            return f"case when {x} > 0 and {b} > 0 and {b} <> 1 then log({b}, {x}) end"
        # regular expressions
        want(3 if name == "regexp_replace" else 2)
        if self.hyper:
            if not isinstance(args[1], ax.Str) or not _regex_ok(args[1].value, name != "regexp_match"):
                raise ax.FormulaError(f"{name.upper()} on an extract: Tableau uses ICU regular expressions, and this pattern may "
                                      "match differently in the warehouse's engine (\\d, \\w, \\b, classes, look-arounds, "
                                      "alternation and several quantifiers differ): rewrite it for the warehouse")
            if "." in re.sub(r"\\.|\[(?:\\.|[^\]])*\]", "", args[1].value):
                self.notes.add(NOTES["regex_dot"])
        if name == "regexp_match":
            return f"regexp_like({sql[0]}, {sql[1]})"
        if name == "regexp_extract":
            if not isinstance(args[1], ax.Str) or _groups(args[1].value) != 1:
                raise ax.FormulaError("REGEXP_EXTRACT with a pattern that doesn't have exactly one capturing group")
            return f"(regexp_match({sql[0]}, {sql[1]}))[1]"
        repl = sql[2]  # a live connection: the warehouse's replacement syntax, as Tableau sends it
        if self.hyper:
            if not isinstance(args[2], ax.Str) or "\\" in args[2].value:
                raise ax.FormulaError("REGEXP_REPLACE with this replacement: write the warehouse's backreferences (ICU writes $1)")
            repl = ax._sql_str(re.sub(r"\$(\d)", r"\\\1", args[2].value))  # ICU $1 is \1 in the warehouse
        return f"regexp_replace({sql[0]}, {sql[1]}, {repl}, 'g')"

    def round(self, args: list[ax.Node]) -> str:
        if len(args) not in (1, 2):
            raise ax.FormulaError("ROUND takes one or two arguments")
        x = self.sql(args[0])
        if not self.hyper:  # a live connection: the warehouse's round(), as Tableau sends it
            return f"round({x}, {self.sql(args[1])})" if len(args) == 2 else f"round({x})"
        if len(args) == 1 or (isinstance(args[1], ax.Num) and args[1].value == "0"):
            return f"round({x}, 0)"  # halves away from zero (Postgres: on a numeric, see _dialect)
        if type_of(args[0], self.dtype) == "integer":
            return f"round({x}, {self.sql(args[1])})"
        raise ax.FormulaError("ROUND with digits on an extract: Tableau rounds the stored double, so a value halfway in decimal "
                              f"(0.25 to 1 digit) can round down; `round({x}, {self.sql(args[1])})` rounds it away from zero. Use it if "
                              "those ties don't matter, or round the source's decimal value")

    def case(self, args: list[ax.Node]) -> str:
        if len(args) < 2:
            raise ax.FormulaError("IF needs a condition and a value")
        parts = ["case"]
        rest = list(args)
        while len(rest) >= 2:
            cond, val = rest.pop(0), rest.pop(0)
            parts.append(f"when {self.sql(cond)} then {self.sql(val)}")
        if rest:
            parts.append(f"else {self.sql(rest[0])}")
        parts.append("end")
        return " ".join(parts)


def _unit(node: ax.Node, units: tuple[str, ...]) -> str:
    if not isinstance(node, ax.Str) or node.value.lower() not in units:
        raise ax.FormulaError(f"date part {getattr(node, 'value', '?')!r} is not translated (a literal one of {', '.join(units)})")
    return node.value.lower()


def _int_lit(node: ax.Node) -> int | None:
    """A whole-number literal (negative ones too)."""
    if isinstance(node, ax.Unary) and node.op == "-":
        v = _int_lit(node.operand)
        return None if v is None else -v
    return int(node.value) if isinstance(node, ax.Num) and node.value.isdigit() else None


def _count(node: ax.Node, sql: str, low: int) -> str:
    """A count or position as Tableau takes it: whole (floor) and at least `low`; NULL stays NULL."""
    lit = _int_lit(node)
    if lit is not None:
        return str(max(lit, low))
    return f"case when {sql} < {low} then {low} else cast(floor({sql}) as integer) end"


# A pattern ICU and the warehouses' engines read alike: literal characters, `.`, bracket lists, anchors, groups and
# escaped punctuation; no \d \w \s \b (Unicode-aware in ICU, not everywhere), POSIX classes or (?...) constructs.
_UNSAFE = re.compile(r"\\[A-Za-z0-9]|\[\[?:|\(\?(?!:)|\[\^?\]")


def _regex_ok(pattern: str, matched_text: bool) -> bool:
    """Whether `pattern` matches alike in ICU and the warehouse. When the matched text matters (EXTRACT, REPLACE), also no
    alternation and at most one quantifier: backtracking engines take the first match, POSIX ones the longest."""
    if _UNSAFE.search(pattern):
        return False
    if matched_text:
        bare = re.sub(r"\\.|\[(?:\\.|[^\]])*\]", "x", pattern)
        if "|" in bare or len(re.findall(r"[*+?]|\{\d", bare)) > 1:
            return False
    return True


def _groups(pattern: str) -> int:
    bare = re.sub(r"\\.|\[(?:\\.|[^\]])*\]", "x", pattern)
    return len(re.findall(r"\((?!\?)", bare))
