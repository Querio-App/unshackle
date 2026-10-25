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
  * MEDIAN interpolates between the two middle values of an even count; AVG of integers is a real; COUNT and COUNTD
    skip NULLs. The aggregates' SQL is in tableau.AGG_SQL.

FUNCS is the function table: add a row to translate another Tableau function, and its cases to
tests/fixtures/tableau_calc/cases.yaml (run in DuckDB, and in Postgres when available).
"""

from __future__ import annotations

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
}

# Where a Tableau extract's result can differ from the warehouse's, for the semantic model's description.
NOTES = {
    "compare": "Tableau's extract compares strings by code point (case- and accent-sensitive, 'B' < 'a'); the migrated SQL "
               "compares with the warehouse's collation, which can differ for `=`, `<`, MIN/MAX and CASE on strings.",
    "case": "Tableau's extract UPPER/LOWER use full Unicode case mapping ('ß' -> 'SS', a final 'Σ' -> 'ς'); the warehouse's "
            "upper()/lower() may map those few characters differently.",
    "trim": "Tableau's extract TRIM also removes tabs and line breaks; the warehouse's trim() removes spaces only.",
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
        if self.hyper and name in ("upper", "lower", "trim"):
            self.notes.add(NOTES["trim" if name == "trim" else "case"])
        if arity is not None and len(args) != arity:
            raise ax.FormulaError(f"{name.upper()} takes {arity} argument(s)")
        parts = [self.sql(a) for a in args]
        return tmpl.format(*parts, args=", ".join(parts))

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
