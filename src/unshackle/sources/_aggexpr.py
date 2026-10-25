"""Tiny parser + translator for spreadsheet-style BI formulas (Sigma, ThoughtSpot).

Shared by unshackle.sources.sigma and unshackle.sources.thoughtspot. It only handles the
deterministic subset:

  * column references        [Amount]  [Orders/Amount]  [FCT_ORDERS::AMOUNT]
  * literals, arithmetic, comparisons, and/or/not, If(...) / if .. then .. else
  * a handful of row functions (upper, lower, abs, round, coalesce, date_trunc...)
  * one aggregate of a row expression           Sum([Amount])        sum ( [Amount] )
  * conditional aggregates                      SumIf([Amount], [Status] = "paid")
  * a ratio of two of those (or two metrics)    Sum([A]) / CountDistinct([B])  safe_divide(a, b)
  * arithmetic over metric references           [Revenue] - [Refunds]

Anything else (window functions, LODs, lookups, unknown functions) raises
FormulaError, and the caller keeps the source expression with a TODO(unshackle).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Union


class FormulaError(ValueError):
    pass


@dataclass
class Ref:
    name: str


@dataclass
class Num:
    value: str


@dataclass
class Str:
    value: str


@dataclass
class Bool:
    value: bool


@dataclass
class Call:
    name: str  # lower-cased
    args: list["Node"] = field(default_factory=list)


@dataclass
class BinOp:
    op: str
    left: "Node"
    right: "Node"


@dataclass
class Unary:
    op: str
    operand: "Node"


Node = Union[Ref, Num, Str, Bool, Call, BinOp, Unary]

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<ref>\[[^\]]*\])
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<ident>[A-Za-z_][A-Za-z0-9_\.]*)
  | (?P<op><=|>=|!=|<>|==|&&|\|\||[=<>+\-*/(),&%{}])
    """,
    re.VERBOSE,
)
KEYWORDS = {"and", "or", "not", "if", "then", "else", "true", "false", "in"}


def tokenize(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise FormulaError(f"cannot read formula at {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        value = m.group()
        if kind == "ws":
            continue
        if kind == "ident" and value.lower() in KEYWORDS:
            kind, value = "kw", value.lower()
        out.append((kind, value))
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self, offset: int = 0) -> tuple[str, str]:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else ("eof", "")

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, value: str) -> None:
        kind, v = self.take()
        if v != value:
            raise FormulaError(f"expected {value!r}, got {v!r}")

    def parse(self) -> Node:
        node = self.expr()
        if self.peek()[0] != "eof":
            raise FormulaError(f"unexpected {self.peek()[1]!r}")
        return node

    def expr(self) -> Node:
        return self.or_()

    def or_(self) -> Node:
        left = self.and_()
        while self.peek()[1] in ("or", "||"):
            self.take()
            left = BinOp("or", left, self.and_())
        return left

    def and_(self) -> Node:
        left = self.not_()
        while self.peek()[1] in ("and", "&&"):
            self.take()
            left = BinOp("and", left, self.not_())
        return left

    def not_(self) -> Node:
        if self.peek()[1] == "not":
            self.take()
            return Unary("not", self.not_())
        return self.compare()

    def compare(self) -> Node:
        left = self.concat()
        while self.peek()[1] in ("=", "==", "!=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            op = {"==": "=", "!=": "<>"}.get(op, op)
            left = BinOp(op, left, self.concat())
        return left

    def concat(self) -> Node:
        left = self.additive()
        while self.peek()[1] == "&":
            self.take()
            left = BinOp("||", left, self.additive())
        return left

    def additive(self) -> Node:
        left = self.term()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            left = BinOp(op, left, self.term())
        return left

    def term(self) -> Node:
        left = self.unary()
        while self.peek()[1] in ("*", "/", "%"):
            op = self.take()[1]
            left = BinOp(op, left, self.unary())
        return left

    def unary(self) -> Node:
        if self.peek()[1] == "-":
            self.take()
            return Unary("-", self.unary())
        return self.primary()

    def primary(self) -> Node:
        kind, value = self.take()
        if kind == "num":
            return Num(value)
        if kind == "str":
            q = value[0]
            return Str(value[1:-1].replace(q + q, q))
        if kind == "ref":
            return Ref(value[1:-1].strip())
        if kind == "kw" and value in ("true", "false"):
            return Bool(value == "true")
        if kind == "kw" and value == "if":
            if self.peek()[1] == "(":
                args = self.call_args()
                if self.peek()[1] != "then":
                    return Call("if", args)  # Sigma If(cond, a, b)
                if len(args) != 1:
                    raise FormulaError("if (...) then: expected one condition")
                return self.then_else(args[0])
            return self.if_then_else()
        if kind == "ident":
            if self.peek()[1] == "(":
                return Call(value.lower(), self.call_args())
            return Ref(value)
        if value == "{":
            items: list[Node] = []
            if self.peek()[1] != "}":
                items.append(self.expr())
                while self.peek()[1] == ",":
                    self.take()
                    items.append(self.expr())
            self.expect("}")
            return Call("{set}", items)
        if value == "(":
            node = self.expr()
            self.expect(")")
            return node
        raise FormulaError(f"unexpected {value or 'end of formula'!r}")

    def call_args(self) -> list[Node]:
        self.expect("(")
        args: list[Node] = []
        if self.peek()[1] != ")":
            args.append(self.expr())
            while self.peek()[1] == ",":
                self.take()
                args.append(self.expr())
        self.expect(")")
        return args

    def if_then_else(self) -> Node:
        return self.then_else(self.expr())

    def then_else(self, cond: Node) -> Node:
        self.expect("then")
        then = self.expr()
        if self.peek()[1] == "else":
            self.take()
            if self.peek()[1] == "if":
                self.take()
                return Call("if", [cond, then, self.if_then_else()])
            return Call("if", [cond, then, self.expr()])
        return Call("if", [cond, then])


def parse(text: str) -> Node:
    if not text or not text.strip():
        raise FormulaError("empty formula")
    return _Parser(text).parse()


def refs(node: Node) -> list[str]:
    if isinstance(node, Ref):
        return [node.name]
    if isinstance(node, Call):
        return [r for a in node.args for r in refs(a)]
    if isinstance(node, BinOp):
        return refs(node.left) + refs(node.right)
    if isinstance(node, Unary):
        return refs(node.operand)
    return []


def calls(node: Node) -> list[str]:
    if isinstance(node, Call):
        return [node.name] + [c for a in node.args for c in calls(a)]
    if isinstance(node, BinOp):
        return calls(node.left) + calls(node.right)
    if isinstance(node, Unary):
        return calls(node.operand)
    return []


# --------------------------------------------------------------------------- row-level SQL

GRAINS = {"day", "week", "month", "quarter", "year", "hour", "minute"}
# name -> (sql template, arity or None for variadic)
ROW_FUNCS: dict[str, tuple[str, int | None]] = {
    "upper": ("upper({0})", 1),
    "lower": ("lower({0})", 1),
    "trim": ("trim({0})", 1),
    "abs": ("abs({0})", 1),
    "round": ("round({args})", None),
    "floor": ("floor({0})", 1),
    "ceiling": ("ceil({0})", 1),
    "ceil": ("ceil({0})", 1),
    "coalesce": ("coalesce({args})", None),
    "nullif": ("nullif({0}, {1})", 2),
    "ifnull": ("coalesce({args})", None),
    "zn": ("coalesce({0}, 0)", 1),
    "isnull": ("({0} is null)", 1),
    "isnotnull": ("({0} is not null)", 1),
    "concat": ("concat({args})", None),
    "length": ("length({0})", 1),
    "len": ("length({0})", 1),
    "strlen": ("length({0})", 1),
    "to_date": ("cast({0} as date)", 1),
    "date": ("cast({0} as date)", 1),
    "start_of_day": ("date_trunc('day', {0})", 1),
    "start_of_week": ("date_trunc('week', {0})", 1),
    "start_of_month": ("date_trunc('month', {0})", 1),
    "start_of_quarter": ("date_trunc('quarter', {0})", 1),
    "start_of_year": ("date_trunc('year', {0})", 1),
    "year": ("extract(year from {0})", 1),
    "month": ("extract(month from {0})", 1),
    "day": ("extract(day from {0})", 1),
}
TRUNC_FUNCS = {"start_of_day": "day", "start_of_week": "week", "start_of_month": "month", "start_of_quarter": "quarter", "start_of_year": "year"}


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def to_sql(node: Node, ref: Callable[[str], str]) -> str:
    """Row-level SQL. `ref` maps a column reference to SQL (or raises FormulaError)."""
    if isinstance(node, Ref):
        return ref(node.name)
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return _sql_str(node.value)
    if isinstance(node, Bool):
        return "true" if node.value else "false"
    if isinstance(node, Unary):
        inner = to_sql(node.operand, ref)
        return f"not ({inner})" if node.op == "not" else f"-{inner}"
    if isinstance(node, BinOp):
        left, right = to_sql(node.left, ref), to_sql(node.right, ref)
        if node.op in ("and", "or"):
            return f"({left} {node.op} {right})"
        if node.op in ("=", "<>") and isinstance(node.right, Bool):
            return f"{left} is {'not ' if (node.op == '<>') == node.right.value else ''}true"
        if node.op == "/":
            return f"1.0 * {left} / nullif({right}, 0)"
        return f"{left} {node.op} {right}" if node.op not in ("+", "-") else f"({left} {node.op} {right})"
    if isinstance(node, Call):
        name = node.name
        if name == "if":
            return _case(node.args, ref)
        if name in ("datetrunc", "date_trunc"):
            if len(node.args) != 2 or not isinstance(node.args[0], Str) or node.args[0].value.lower() not in GRAINS:
                raise FormulaError("DateTrunc needs a literal grain")
            return f"date_trunc('{node.args[0].value.lower()}', {to_sql(node.args[1], ref)})"
        if name == "safe_divide":
            if len(node.args) != 2:
                raise FormulaError("safe_divide takes two arguments")
            return f"1.0 * ({to_sql(node.args[0], ref)}) / nullif({to_sql(node.args[1], ref)}, 0)"
        if name in ROW_FUNCS:
            tmpl, arity = ROW_FUNCS[name]
            if arity is not None and len(node.args) != arity:
                raise FormulaError(f"{name} takes {arity} argument(s)")
            parts = [to_sql(a, ref) for a in node.args]
            return tmpl.format(*parts, args=", ".join(parts))
        raise FormulaError(f"function {name}() has no deterministic SQL translation")
    raise FormulaError(f"cannot translate {node!r}")


def _case(args: list[Node], ref: Callable[[str], str]) -> str:
    if len(args) < 2:
        raise FormulaError("If needs a condition and a value")
    parts = ["case"]
    rest = list(args)
    while len(rest) >= 2:
        cond, val = rest.pop(0), rest.pop(0)
        parts.append(f"when {to_sql(cond, ref)} then {to_sql(val, ref)}")
    if rest:
        parts.append(f"else {to_sql(rest[0], ref)}")
    parts.append("end")
    return " ".join(parts)


def time_grain(node: Node) -> tuple[str, Node] | None:
    """DateTrunc("month", [x]) / start_of_month([x]) -> ("month", x)."""
    if isinstance(node, Call):
        if node.name in ("datetrunc", "date_trunc") and len(node.args) == 2 and isinstance(node.args[0], Str):
            grain = node.args[0].value.lower()
            if grain in GRAINS:
                return grain, node.args[1]
        if node.name in TRUNC_FUNCS and len(node.args) == 1:
            return TRUNC_FUNCS[node.name], node.args[0]
    return None


# --------------------------------------------------------------------------- aggregates

# function -> (MetricFlow agg, index of value arg or None, index of condition arg or None)
AGGS = {
    "sigma": {
        "sum": ("sum", 0, None),
        "count": ("count", 0, None),
        "countdistinct": ("count_distinct", 0, None),
        "avg": ("average", 0, None),
        "average": ("average", 0, None),
        "min": ("min", 0, None),
        "max": ("max", 0, None),
        "median": ("median", 0, None),
        "sumif": ("sum", 0, 1),
        "countif": ("count", None, 0),
        "countdistinctif": ("count_distinct", 0, 1),
        "avgif": ("average", 0, 1),
        "minif": ("min", 0, 1),
        "maxif": ("max", 0, 1),
    },
    "thoughtspot": {
        "sum": ("sum", 0, None),
        "count": ("count", 0, None),
        "unique_count": ("count_distinct", 0, None),
        "average": ("average", 0, None),
        "min": ("min", 0, None),
        "max": ("max", 0, None),
        "median": ("median", 0, None),
        "sum_if": ("sum", 1, 0),
        "count_if": ("count", 1, 0),
        "unique_count_if": ("count_distinct", 1, 0),
        "average_if": ("average", 1, 0),
        "min_if": ("min", 1, 0),
        "max_if": ("max", 1, 0),
    },
}


@dataclass
class Agg:
    agg: str
    expr: str
    condition: Node | None = None


@dataclass
class MetricRef:
    name: str


Side = Union[Agg, MetricRef]


@dataclass
class Classified:
    kind: str  # agg | ratio | derived
    agg: Agg | None = None
    numerator: Side | None = None
    denominator: Side | None = None
    expr: str = ""
    metrics: list[str] = field(default_factory=list)


def classify(
    node: Node,
    dialect: str,
    ref_sql: Callable[[str], str],
    metric_ref: Callable[[str], str | None],
    agg_metric: Callable[[Agg], str] | None = None,
    render: Callable[[Node, Callable[[str], str]], str] = to_sql,
) -> Classified:
    """Classify an aggregate formula. `metric_ref(name)` returns a metric name if the ref is a metric. `render` writes
    an aggregate's row expression (a source with its own semantics passes its renderer)."""
    side = _side(node, dialect, ref_sql, metric_ref, render)
    if isinstance(side, Agg):
        return Classified("agg", agg=side)
    if isinstance(side, MetricRef):
        return Classified("derived", expr=side.name, metrics=[side.name])
    ratio = _ratio(node)
    if ratio:
        num = _side(ratio[0], dialect, ref_sql, metric_ref, render)
        den = _side(ratio[1], dialect, ref_sql, metric_ref, render)
        if num is not None and den is not None:
            return Classified("ratio", numerator=num, denominator=den)
    # arithmetic over metric references only
    names: list[str] = []
    expr = _metric_arith(node, metric_ref, names)
    if expr is not None and names:
        return Classified("derived", expr=expr, metrics=list(dict.fromkeys(names)))
    if agg_metric is not None:
        # arithmetic over metrics *and* plain aggregates: each aggregate becomes a helper metric
        found: list[Agg] = []

        def collect(n: Node) -> str | None:
            side = _side(n, dialect, ref_sql, metric_ref, render)
            if isinstance(side, Agg):
                found.append(side)
                return f"\x00{len(found) - 1}\x00"
            return None

        names = []
        expr = _metric_arith(node, metric_ref, names, collect)
        if expr is not None and found:
            helpers = [agg_metric(a) for a in found]
            expr = re.sub("\x00(\\d+)\x00", lambda m: helpers[int(m.group(1))], expr)
            return Classified("derived", expr=expr, metrics=list(dict.fromkeys(names + helpers)))
    bad = [c for c in calls(node) if c not in AGGS[dialect] and c not in ROW_FUNCS and c not in ("if", "safe_divide", "datetrunc")]
    if bad:
        raise FormulaError(f"function {bad[0]}() has no deterministic MetricFlow translation")
    raise FormulaError("aggregate expression is not a single aggregate, a ratio or arithmetic over metrics")


def _ratio(node: Node) -> tuple[Node, Node] | None:
    if isinstance(node, BinOp) and node.op == "/":
        return node.left, node.right
    if isinstance(node, Call) and node.name == "safe_divide" and len(node.args) == 2:
        return node.args[0], node.args[1]
    return None


def _side(node: Node, dialect: str, ref_sql, metric_ref, render=to_sql) -> Side | None:
    if isinstance(node, Ref):
        name = metric_ref(node.name)
        return MetricRef(name) if name else None
    if isinstance(node, Call) and node.name in AGGS[dialect]:
        agg, vi, ci = AGGS[dialect][node.name]
        if agg == "count" and vi is not None and ci is None and not node.args:
            return Agg("count", "1")  # Count() with no argument counts rows
        arity = (vi is not None) + (ci is not None)
        if len(node.args) != arity:
            return None
        if vi is not None:
            value = node.args[vi]
            if any(c in AGGS[dialect] for c in calls(value)):
                return None
            expr = render(value, ref_sql)
        else:
            expr = "1"
        cond = node.args[ci] if ci is not None else None
        return Agg(agg, expr, cond)
    return None


def _metric_arith(node: Node, metric_ref, names: list[str], agg=None) -> str | None:
    if agg is not None and isinstance(node, Call):
        return agg(node)
    if isinstance(node, Ref):
        name = metric_ref(node.name)
        if not name:
            return None
        names.append(name)
        return name
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Unary) and node.op == "-":
        inner = _metric_arith(node.operand, metric_ref, names, agg)
        return None if inner is None else f"-{inner}"
    if isinstance(node, BinOp) and node.op in "+-*/":
        left = _metric_arith(node.left, metric_ref, names, agg)
        right = _metric_arith(node.right, metric_ref, names, agg)
        if left is None or right is None:
            return None
        return f"({left} {node.op} {right})" if node.op in "+-" else f"{left} {node.op} {right}"
    return None


def filter_jinja(node: Node, dim_ref: Callable[[str], str]) -> str:
    """Condition -> MetricFlow filter, e.g. {{ Dimension('order__status') }} = 'paid'."""
    return to_sql(node, lambda name: "{{ Dimension('" + dim_ref(name) + "') }}")


def strip_outer(sql: str) -> str:
    while sql.startswith("(") and sql.endswith(")"):
        depth = 0
        for i, ch in enumerate(sql):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(sql) - 1:
                return sql
        sql = sql[1:-1]
    return sql


# --------------------------------------------------------------------------- output aliases


def alias_wrap(sql: str, names: list[str], wrapper: str = "q") -> tuple[str, dict[str, str], list[str]]:
    """Wrap generated SQL so display-named outputs ("Month of Created At") become snake_case columns.

    Returns (sql, {display name: alias}, [names not found as quoted outputs]). Altair field
    shorthand breaks on names with brackets, parentheses or colons, so tiles use the aliases
    and keep the display name as the axis title.
    """
    body = sql.strip().rstrip(";").strip()
    if not body or not names:
        return sql, {}, list(names)
    present = [n for n in dict.fromkeys(names) if f'"{n}"' in body]
    missing = [n for n in dict.fromkeys(names) if n not in present]
    if not present:
        return sql, {}, missing
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for n in present:
        base = re.sub(r"[^0-9a-z]+", "_", n.lower()).strip("_") or "col"
        if base[0].isdigit():
            base = f"c_{base}"
        alias, i = base, 2
        while alias in used:
            alias, i = f"{base}_{i}", i + 1
        used.add(alias)
        mapping[n] = alias
    cols = ",\n  ".join(f'"{n}" as {a}' for n, a in mapping.items())
    return f"select\n  {cols}\nfrom (\n{body}\n) {wrapper}", mapping, missing
