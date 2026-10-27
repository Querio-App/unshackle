"""Qlik Sense source (Qlik Cloud; tested against engine 12.2969).

Raw layout under migration/raw/qlik/:

  tenant.json                         {"url", "engine"}  engine version from the QIX EngineVersion call
  spaces.json                         GET /api/v1/spaces (id, name, type)
  apps.json                           GET /api/v1/items?resourceType=app: id, name, spaceId, itemViews, lastReloadTime
  apps/<app id>/                      one folder per app, in the layout `qlik app unbuild` writes, plus three extras:
    app-properties.json               GetAppProperties (qTitle, qHasSectionAccess, ...)
    script.qvs                        GetScript: the load script, Section Access included
    connections.yml                   GetConnections (name, type, connection string; never a password)
    dimensions.json / measures.json   master items: GetDimension / GetMeasure -> GetProperties
    variables.json                    GetVariableById -> GetProperties; script-created ones carry qIsScriptCreated and
                                      their value after the last reload
    objects/<type>-<slug>-<id>.json   GetFullPropertyTree of every sheet and master visualization
    bookmarks.json                    (extra) GetBookmark -> GetLayout: title and the selections it stores
    tables-and-keys.json              (extra) GetTablesAndKeys, from a second session that opens the app with data:
                                      tables, their fields, type tags and per-table distinct counts (key directions)
    data-metadata.json                (extra, only when the app can't be opened with data) GET /api/v1/apps/{id}/
                                      data/metadata: one distinct count per field, so fewer keys are recognised.
                                      With neither (an unbuild import) `LOAD *` columns and joins are unknown
  _errors.json                        apps that failed to extract; parse() turns them into warnings

The engine (QIX) is JSON-RPC over a WebSocket at wss://<tenant>/app/<app id>; the app is opened
without data (OpenDoc noData), so extract never loads an app into memory. `qlik app unbuild <folder>`
output imports as is (`unshackle import qlik <folder>`).

How parse() reads an app:
  - The load script is an ETL language. A table becomes a semantic model on a warehouse table when
    its LOAD is a plain field list (field, field AS name, row expressions unshackle translates) over
    one file, or over `SQL SELECT <columns> FROM <table>` with no WHERE/JOIN. Everything else (RESIDENT,
    JOIN/CONCATENATE, GROUP BY, WHERE, INLINE, mapping, loops) keeps the statement and gets a TODO.
    Files (Excel, CSV, Parquet) need landing in the warehouse first, so the model reads a table named
    after the file and the build warns; QVDs, usually written by another Qlik app, get a TODO.
  - Qlik associates tables on shared field names. A field that is unique in its table (distinct
    values = rows) is that table's primary key; the other tables holding it join many-to-one to it.
  - Master measures become metrics: Sum/Count/Avg/Min/Max (DISTINCT) of a row expression on one table,
    set modifiers with listed values as metric filters, ratios and arithmetic of those, static
    $(variable) expansion. Aggr, TOTAL, inter-record functions, P()/E(), searches, alternate states and
    $(=...) keep the expression and a TODO.
  - Sheets become notebooks; each chart's hypercube becomes SQL from its measure's table with LEFT JOINs
    to the dimension tables, and filter panes become notebook parameters.
  - Section Access loaded INLINE becomes row filters per USERID/GROUP: the reduction field's
    predicate on its table, carried to every associated table with key subqueries (Qlik reduces the
    whole associated model, ADMIN included), Cedar forbids for OMIT and for users not listed.

Auth: QLIK_TENANT_URL plus QLIK_CLIENT_ID / QLIK_CLIENT_SECRET (an OAuth machine-to-machine client
with the Trusted consent method) or QLIK_API_KEY. Extract only reads.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union

import yaml

from unshackle.http import Client, SourceError, env
from unshackle.ir import (
    Bundle,
    Dashboard,
    DashboardFilter,
    Encoding,
    KnowledgeItem,
    Layer,
    Permission,
    RowFilter,
    Tile,
    VerifiedQuery,
    join_todos,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources._manual import files_of, safe_name
from unshackle.sources._semyaml import NField, NJoin, NTable, build_semantic, grid_rows, sql_literal
from unshackle.sources.base import Inventory, Source

APP_FILES = ("app-properties.json", "script.qvs", "connections.yml", "dimensions.json", "measures.json", "variables.json",
             "bookmarks.json", "tables-and-keys.json", "data-metadata.json")
DECOR = {"action-button", "sn-shape", "sn-nav-menu", "qlik-button-for-navigation"}
CONTAINERS = {"container", "sn-layout-container", "sn-tabbed-container"}
MARKS = {"barchart": "bar", "linechart": "line", "piechart": "arc", "scatterplot": "point", "combochart": "bar"}
AGGS = {"sum": "SUM", "count": "COUNT", "avg": "AVG", "min": "MIN", "max": "MAX"}
MF_AGG = {"sum": "sum", "count": "count", "avg": "average", "min": "min", "max": "max"}
TRUNC = {"monthstart": "month", "yearstart": "year", "quarterstart": "quarter", "daystart": "day"}
ROW_FN = {"upper": "UPPER", "lower": "LOWER", "trim": "TRIM", "ltrim": "LTRIM", "rtrim": "RTRIM", "len": "LENGTH",
          "abs": "ABS", "floor": "FLOOR", "ceil": "CEIL", "coalesce": "COALESCE"}
NOT_TRANSLATED = {
    "aggr": "Aggr() (a nested aggregation per dimension values)",
    "above": "inter-record function Above()", "below": "inter-record function Below()", "before": "inter-record function Before()",
    "after": "inter-record function After()", "top": "inter-record function Top()", "bottom": "inter-record function Bottom()",
    "rangesum": "RangeSum() over chart rows", "rangeavg": "RangeAvg() over chart rows", "rowno": "RowNo()",
    "columnno": "ColumnNo()", "dimensionality": "Dimensionality()", "getselectedcount": "selection state (GetSelectedCount)",
    "getfieldselections": "selection state (GetFieldSelections)", "getcurrentselections": "selection state",
}
SIMPLE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


# ------------------------------------------------------------ engine + REST


class Engine:
    """A minimal JSON-RPC client for the Qlik associative engine (QIX) over a WebSocket.

    Qlik ships no Python client, and the protocol is small: send {id, handle, method, params},
    wait for the reply with that id, skip notifications.
    """

    def __init__(self, url: str, token: str, timeout: float = 300):
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect

        self.timeout, self.errors = timeout, (WebSocketException, OSError, TimeoutError)
        try:
            self.ws = connect(url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=None, open_timeout=60).__enter__()
        except self.errors as exc:
            raise SourceError(f"Qlik engine connection failed: {exc}") from exc
        self.ids = itertools.count(1)

    def call(self, handle: int, method: str, *params: Any) -> dict[str, Any]:
        rid = next(self.ids)
        try:
            self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "handle": handle, "method": method, "params": list(params)}))
            while True:
                msg = json.loads(self.ws.recv(timeout=self.timeout))
                if msg.get("id") == rid:
                    break
        except self.errors as exc:
            raise SourceError(f"Qlik engine {method}: {exc}") from exc
        if "error" in msg:
            err = msg["error"]
            raise SourceError(f"Qlik engine {method}: {err.get('message', '')} {err.get('parameter', '')} (code {err.get('code')})")
        return msg.get("result") or {}

    @staticmethod
    def handle(result: dict[str, Any]) -> int:
        return result["qReturn"]["qHandle"]

    def close(self) -> None:
        self.ws.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def engine_url(tenant: str, app: str) -> str:
    """Engine endpoint for an app, in a session of its own (so it never meets one the user has open with data)."""
    return f"wss://{tenant.split('://', 1)[-1].rstrip('/')}/app/{app}/identity/unshackle-{secrets.token_hex(4)}"


# ------------------------------------------------------------ expressions


class Untranslatable(ValueError):
    pass


@dataclass
class Num:
    value: str


@dataclass
class Str:
    value: str


@dataclass
class Fld:
    name: str


@dataclass
class Call:
    name: str  # lower-cased
    args: list["Node"] = field(default_factory=list)
    set: str | None = None  # raw set expression between the braces
    distinct: bool = False
    total: bool = False


@dataclass
class Bin:
    op: str
    left: "Node"
    right: "Node"


@dataclass
class Un:
    op: str
    operand: "Node"


Node = Union[Num, Str, Fld, Call, Bin, Un]

_TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<str>'(?:[^']|'')*')
  | (?P<field>\[[^\]]*\]|"[^"]*"|`[^`]*`)
  | (?P<ident>[A-Za-z_À-￿][\w.À-￿]*)
  | (?P<op><=|>=|<>|[-+*/&=<>(),{}^])
""", re.X | re.S)
PRECEDENCE = [("or", "xor"), ("and",), ("=", "<>", "<", ">", "<=", ">=", "like"), ("&",), ("+", "-"), ("*", "/")]


def expand(expr: str, variables: dict[str, str], depth: int = 0) -> str:
    """Static dollar-sign expansion: $(name) -> the variable's definition. $(=...) and unknown names raise."""
    def sub(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if inner.startswith("="):
            raise Untranslatable(f"dynamic dollar expansion $({inner}) is evaluated per selection")
        name = re.sub(r"\(.*\)$", "", inner).strip()
        if name not in variables:
            raise Untranslatable(f"variable {name} is not defined in the app")
        value = variables[name]
        if value.lstrip().startswith("="):
            raise Untranslatable(f"variable {name} is a calculated expression ({value.strip()})")
        if depth > 5:
            raise Untranslatable("variables expand into each other too deeply")
        return expand(value, variables, depth + 1)

    return re.sub(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)", sub, expr)


class _Parser:
    def __init__(self, text: str):
        self.text, self.pos = text, 0
        self.peeked: tuple[str, str] | None = None

    def _lex(self) -> tuple[str, str]:
        while self.pos < len(self.text):
            m = _TOKEN.match(self.text, self.pos)
            if not m:
                raise Untranslatable(f"cannot read the expression at {self.text[self.pos:self.pos + 20]!r}")
            self.pos = m.end()
            if m.lastgroup != "ws":
                return m.lastgroup or "", m.group()
        return "eof", ""

    def peek(self) -> tuple[str, str]:
        if self.peeked is None:
            self.peeked = self._lex()
        return self.peeked

    def next(self) -> tuple[str, str]:
        tok = self.peek()
        self.peeked = None
        return tok

    def expect(self, value: str) -> None:
        kind, v = self.next()
        if v != value:
            raise Untranslatable(f"expected {value!r}, found {v or 'the end'!r}")

    def raw_set(self) -> str:
        """The raw text of a set expression; the parser stands just before its `{`."""
        assert self.peeked is None
        start = self.text.index("{", self.pos)
        depth, quote, i = 0, "", start
        while i < len(self.text):
            c = self.text[i]
            if quote:
                quote = "" if c == quote else quote
            elif c in "'\"[":
                quote = "]" if c == "[" else c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    self.pos = i + 1
                    return self.text[start + 1:i].strip()
            i += 1
        raise Untranslatable("unbalanced set expression")

    def parse(self) -> Node:
        node = self.binary(0)
        if self.peek()[0] != "eof":
            raise Untranslatable(f"unexpected {self.peek()[1]!r}")
        return node

    def binary(self, level: int) -> Node:
        if level == len(PRECEDENCE):
            return self.unary()
        left = self.binary(level + 1)
        while True:
            kind, v = self.peek()
            op = v.lower()
            if (kind == "op" or (kind == "ident" and op in ("and", "or", "xor", "like"))) and op in PRECEDENCE[level]:
                self.next()
                left = Bin(op, left, self.binary(level + 1))
            else:
                return left

    def unary(self) -> Node:
        kind, v = self.peek()
        if v == "-" or v.lower() == "not":
            self.next()
            return Un(v.lower(), self.unary() if v == "-" else self.binary(3))
        if v == "+":
            self.next()
            return self.unary()
        return self.primary()

    def primary(self) -> Node:
        kind, v = self.next()
        if kind == "num":
            return Num(v)
        if kind == "str":
            return Str(v[1:-1].replace("''", "'"))
        if kind == "field":
            return Fld(v[1:-1])
        if v == "(":
            node = self.binary(0)
            self.expect(")")
            return node
        if kind == "ident":
            if self.peek()[1] == "(":
                self.next()
                return self.call(v.lower())
            return Fld(v)
        raise Untranslatable(f"unexpected {v or 'end of expression'!r}")

    def call(self, name: str) -> Call:
        node = Call(name)
        while True:
            # set expression and qualifiers, in any order, before the first argument
            if self.peeked is None and self.text[self.pos:].lstrip().startswith("{"):
                node.set = self.raw_set()
                continue
            kind, v = self.peek()
            if kind == "ident" and v.upper() in ("DISTINCT", "ALL", "NODISTINCT"):
                self.next()
                node.distinct = node.distinct or v.upper() == "DISTINCT"
                continue
            if kind == "ident" and v.upper() == "TOTAL":
                self.next()
                node.total = True
                if self.peek()[1] == "<":
                    while self.next()[1] not in (">", ""):
                        pass
                continue
            break
        if self.peek()[1] == ")":
            self.next()
            return node
        while True:
            node.args.append(self.binary(0))
            kind, v = self.next()
            if v == ")":
                return node
            if v != ",":
                raise Untranslatable(f"expected ',' or ')' in {name}(), found {v!r}")


def parse_expr(text: str, variables: dict[str, str] | None = None) -> Node:
    text = expand(text, variables or {}).strip()
    if text.startswith("="):
        text = text[1:]
    if not text.strip():
        raise Untranslatable("empty expression")
    return _Parser(text).parse()


def fields_of(node: Node) -> list[str]:
    if isinstance(node, Fld):
        return [node.name]
    if isinstance(node, Call):
        return [f for a in node.args for f in fields_of(a)]
    if isinstance(node, Bin):
        return fields_of(node.left) + fields_of(node.right)
    if isinstance(node, Un):
        return fields_of(node.operand)
    return []


def aggregates_of(node: Node) -> list[Call]:
    if isinstance(node, Call) and node.name in AGGS:
        return [node]
    if isinstance(node, Call):
        return [c for a in node.args for c in aggregates_of(a)]
    if isinstance(node, Bin):
        return aggregates_of(node.left) + aggregates_of(node.right)
    if isinstance(node, Un):
        return aggregates_of(node.operand)
    return []


def quote(name: str) -> str:
    return name if SIMPLE_IDENT.match(name) else '"' + name.replace('"', '""') + '"'


def row_sql(node: Node, col: Callable[[str], str], cond: bool = False) -> str:
    """SQL for a row-level Qlik expression. `cond`: the value is used as a condition (If, set analysis)."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return sql_literal(node.value)
    if isinstance(node, Fld):
        return col(node.name)
    if isinstance(node, Un):
        if node.op == "not":
            return f"NOT ({row_sql(node.operand, col, True)})"
        return f"-{_paren(node.operand, col)}"
    if isinstance(node, Bin):
        op = node.op
        if op in ("and", "or"):
            if not cond:
                raise Untranslatable(f"{op.upper()} used as a number (Qlik's true is -1)")
            return f"{_paren(node.left, col, True)} {op.upper()} {_paren(node.right, col, True)}"
        if op in ("=", "<>", "<", ">", "<=", ">="):
            if not cond:
                raise Untranslatable("a comparison used as a number (Qlik's true is -1)")
            if op not in ("=", "<>") and not (isinstance(node.left, Num) or isinstance(node.right, Num)):
                raise Untranslatable("ordering comparison that isn't against a number (Qlik compares text with its own collation)")
            return f"{_paren(node.left, col)} {op} {_paren(node.right, col)}"
        if op == "/":
            return f"1.0 * {_paren(node.left, col)} / nullif({row_sql(node.right, col)}, 0)"
        if op in ("+", "-", "*"):
            return f"{_paren(node.left, col)} {op} {_paren(node.right, col)}"
        raise Untranslatable(f"operator {op!r} (Qlik's & and like treat nulls and case differently from SQL)")
    if isinstance(node, Call):
        return _row_call(node, col, cond)
    raise Untranslatable(f"unknown node {node!r}")


def _paren(node: Node, col: Callable[[str], str], cond: bool = False) -> str:
    s = row_sql(node, col, cond)
    return f"({s})" if isinstance(node, Bin) else s


def _row_call(node: Call, col: Callable[[str], str], cond: bool) -> str:
    n, args = node.name, node.args
    if n in AGGS:
        raise Untranslatable(f"{n}() inside a row expression")
    if n in NOT_TRANSLATED:
        raise Untranslatable(NOT_TRANSLATED[n])
    if n == "if" and len(args) in (2, 3):
        other = row_sql(args[2], col) if len(args) == 3 else "NULL"
        return f"CASE WHEN {row_sql(args[0], col, True)} THEN {row_sql(args[1], col)} ELSE {other} END"
    if n in ROW_FN and (n == "coalesce" or len(args) == 1):
        return f"{ROW_FN[n]}({', '.join(row_sql(a, col) for a in args)})"
    if n in TRUNC and len(args) == 1:
        return f"DATE_TRUNC('{TRUNC[n]}', {row_sql(args[0], col)})"
    if n == "year" and len(args) == 1:
        return f"EXTRACT(YEAR FROM {row_sql(args[0], col)})"
    if n in ("num", "date", "timestamp", "money") and args and not isinstance(args[0], (Str,)):
        return row_sql(args[0], col)  # formatting only: the value is unchanged
    if n == "isnull" and len(args) == 1:
        if not cond:
            raise Untranslatable("IsNull() used as a number")
        return f"{_paren(args[0], col)} IS NULL"
    if n == "null" and not args:
        return "NULL"
    raise Untranslatable(f"Qlik function {n}() has no SQL translation in unshackle")


# ------------------------------------------------------------ set analysis


@dataclass
class SetFilter:
    field: str
    op: str  # "in", "not_in"
    values: list[Node]  # Num or Str literals
    insensitive: bool = False  # unquoted / double-quoted values: Qlik matches the whole value ignoring case


def parse_set(raw: str | None) -> tuple[list[SetFilter], bool, list[str]]:
    """A set expression -> (filters, ignores_selections, fields whose selections it clears).

    Handles the default set ($ or nothing) or {1} with modifiers listing values: F={'a','b'}, F={2025},
    F-={'x'} (exclude), F= (clear). Anything else raises Untranslatable."""
    if raw is None:
        return [], False, []
    raw = raw.strip()
    m = re.fullmatch(r"(\$|1)?\s*(?:<(.*)>)?", raw, re.S)
    if not m or raw == "":
        raise Untranslatable(f"set expression {{{raw}}} (alternate states, bookmarks or set operators)")
    ident, body = m.group(1), m.group(2)
    filters, cleared = [], []
    for part in _split_top(body or "", ","):
        part = part.strip()
        if not part:
            continue
        mm = re.fullmatch(r"(\[[^\]]+\]|\"[^\"]+\"|[\w.]+)\s*(-=|\+=|\*=|/=|=|-)?\s*(.*)", part, re.S)
        if not mm or mm.group(2) is None:
            raise Untranslatable(f"set modifier {part!r}")
        name, op, rhs = mm.group(1).strip("[]\""), mm.group(2), mm.group(3).strip()
        if op == "=" and rhs == "":
            cleared.append(name)
            continue
        if op not in ("=", "-="):
            raise Untranslatable(f"set modifier {part!r}: {op} combines with the current selection")
        if not (rhs.startswith("{") and rhs.endswith("}")):
            raise Untranslatable(f"set modifier {part!r}: implicit field value definitions (P(), E(), set operators)")
        values, insensitive = [], False
        for v in _split_top(rhs[1:-1], ","):
            v = v.strip()
            if re.fullmatch(r"'(?:[^']|'')*'", v):
                values.append(Str(v[1:-1].replace("''", "'")))
            elif re.fullmatch(r"-?\d+(\.\d+)?", v):
                values.append(Num(v))
            elif re.fullmatch(r"\"[^\"*?<>=]*\"|[^\"'*?<>=\s]+", v):
                values.append(Str(v.strip('"')))
                insensitive = True
            else:
                raise Untranslatable(f"set modifier {part!r}: search strings (wildcards, <, >, =) match by Qlik's search rules")
        if not values:
            raise Untranslatable(f"set modifier {part!r} selects no values")
        filters.append(SetFilter(name, "in" if op == "=" else "not_in", values, insensitive))
    return filters, ident == "1", cleared


def _split_top(text: str, sep: str) -> list[str]:
    out, depth, quote, start = [], 0, "", 0
    for i, c in enumerate(text):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"[":
            quote = "]" if c == "[" else c
        elif c in "({<":
            depth += 1
        elif c in ")}>":
            depth -= 1
        elif c == sep and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


def set_predicate(f: SetFilter, col: Callable[[str], str]) -> str:
    ref = col(f.field)
    lits = [v.value if isinstance(v, Num) else sql_literal(v.value.upper() if f.insensitive else v.value) for v in f.values]
    if f.insensitive:
        ref = f"UPPER({ref})"
    if len(lits) == 1:
        return f"{ref} {'=' if f.op == 'in' else '<>'} {lits[0]}"
    return f"{ref} {'IN' if f.op == 'in' else 'NOT IN'} ({', '.join(lits)})"


# ------------------------------------------------------------ load script


@dataclass
class Load:
    """One table-producing statement of the load script (after preceding LOADs are folded in)."""

    name: str
    text: str  # the statement(s) as written
    fields: list[tuple[str, str]] = field(default_factory=list)  # (expression, field name)
    star: bool = False
    source: str = ""  # "file", "sql", "resident", "inline", "autogenerate", "other"
    path: str = ""  # file path or SQL relation
    sql: str = ""
    problems: list[str] = field(default_factory=list)
    target: str = ""  # JOIN / CONCATENATE target, or the table it auto-concatenates into


@dataclass
class Script:
    loads: list[Load] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    section_access: list[str] = field(default_factory=list)  # statements between Section Access and Section Application
    notes: list[str] = field(default_factory=list)


def strip_comments(script: str) -> str:
    """Remove //, /* */ and REM comments; keeps strings, [brackets] and lib:// paths."""
    out, i, quote = [], 0, ""
    while i < len(script):
        c = script[i]
        if quote:
            out.append(c)
            quote = "" if c == quote else quote
            i += 1
            continue
        if c in "'\"[`":
            quote = {"[": "]"}.get(c, c)
            out.append(c)
            i += 1
        elif script.startswith("//", i) and not (i and script[i - 1] == ":"):
            while i < len(script) and script[i] != "\n":
                i += 1
        elif script.startswith("/*", i):
            end = script.find("*/", i + 2)
            i = len(script) if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r"(?im)^\s*REM\b[^;]*;", "", "".join(out))


def statements(script: str) -> list[str]:
    """Statements end with `;`, except control statements (FOR, NEXT, IF ... THEN, SUB, ...), which end with their line."""
    lines = [ln + ";" if CONTROL.match(ln.strip()) and not ln.rstrip().endswith(";") else ln for ln in strip_comments(script).split("\n")]
    return [s.strip() for s in _split_top_quoted("\n".join(lines), ";") if s.strip()]


def _split_top_quoted(text: str, sep: str) -> list[str]:
    out, depth, quote, start = [], 0, "", 0
    for i, c in enumerate(text):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"[`":
            quote = {"[": "]"}.get(c, c)
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == sep and depth <= 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


LABEL = re.compile(r"^(\[[^\]]+\]|\"[^\"]+\"|[\w.$#]+)\s*:(?!/)\s*", re.S)
PREFIX = re.compile(r"^(NoConcatenate|Concatenate|(?:Left|Right|Inner|Outer)?\s*(?:Join|Keep)|Mapping|Generic|CrossTable|"
                    r"Hierarchy\w*|IntervalMatch|Add|Replace|Merge|Buffer|First\s+\d+|Sample\s+[\d.]+|Semantic)\b\s*(\([^)]*\))?\s*",
                    re.I | re.S)
CONTROL = re.compile(r"^(FOR\s+(?:EACH\s+)?\w+\s*(?:=|IN\b)|NEXT\b|(?:ELSE)?IF\b.*\bTHEN\b|ELSE\s*$|END\s*(?:IF|SUB|SWITCH)\b|"
                     r"SUB\s+\w|CALL\s+\w|DO\b(?!\w)|LOOP\b|SWITCH\s|CASE\s|DEFAULT\s*$|EXIT\s+(?:DO|FOR|SUB|SCRIPT)\b)", re.I)


def _keyword_at(text: str, words: tuple[str, ...]) -> list[tuple[int, str]]:
    """(position, keyword) of top-level keywords (outside quotes, brackets and parentheses)."""
    hits, depth, quote = [], 0, ""
    pattern = re.compile(r"\b(" + "|".join(w.replace(" ", r"\s+") for w in words) + r")\b", re.I)
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"[`":
            quote = {"[": "]"}.get(c, c)
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            m = pattern.match(text, i)
            if m:
                hits.append((i, re.sub(r"\s+", " ", m.group(1).upper())))
                i = m.end()
                continue
        i += 1
    return hits


def _name(text: str) -> str:
    text = text.strip()
    return text[1:-1] if text[:1] in "[\"`" and text[-1:] in "]\"`" else text


def parse_fields(text: str) -> tuple[list[tuple[str, str]], bool]:
    """A LOAD field list -> ([(expression, field name)], has *)."""
    out, star = [], False
    for item in _split_top_quoted(text, ","):
        item = item.strip()
        if not item:
            continue
        if item == "*":
            star = True
            continue
        hits = _keyword_at(item, ("AS",))
        if hits:
            pos = hits[-1][0]
            out.append((item[:pos].strip(), _name(item[pos + 2:])))
        else:
            out.append((item, _name(item)))
    return out, star


def parse_script(script: str) -> Script:
    out = Script()
    pending: list[tuple[str, str, list[tuple[str, str]], bool]] = []  # preceding LOADs: (label, text, fields, star)
    qualify = False
    section = "application"
    for st in statements(script):
        head = st.split(None, 2)
        word = head[0].upper() if head else ""
        if word == "SECTION" and len(head) > 1:
            section = head[1].lower().rstrip(";")
            continue
        if section == "access":
            out.section_access.append(st)
            continue
        if word in ("SET", "LET"):
            m = re.match(r"^(SET|LET)\s+([\w.$#\[\]\"]+)\s*=\s*(.*)$", st, re.I | re.S)
            if m:
                value = m.group(3).strip()
                if m.group(1).upper() == "SET" and len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]  # SET strips one pair of quotes
                out.variables[_name(m.group(2))] = value if m.group(1).upper() == "SET" else f"={value}"
            continue
        if CONTROL.match(st):
            out.notes.append(f"control statement {st.split()[0].upper().split('(')[0]}")
            continue
        label, body = "", st
        m = LABEL.match(body)
        if m and not re.match(r"^(LOAD|SELECT|SQL)\b", body, re.I):
            label, body = _name(m.group(1)), body[m.end():]
        prefixes = []
        while True:
            m = PREFIX.match(body)
            if not m:
                break
            prefixes.append((re.sub(r"\s+", " ", m.group(1)).upper(), (m.group(2) or "").strip("() ")))
            body = body[m.end():]
        if re.match(r"^(SQL\s+)?SELECT\b", body, re.I):
            sql = re.sub(r"^SQL\s+", "", body, flags=re.I).strip()
            load = _from_sql(label, st, sql, pending)
            pending = []
            _prefixes(load, prefixes)
            if qualify:
                load.problems.append("QUALIFY prefixes field names with the table name")
            out.loads.append(load)
            continue
        if not re.match(r"^LOAD\b", body, re.I):
            if word in ("QUALIFY", "UNQUALIFY"):
                qualify = word == "QUALIFY"
            if word in ("DROP", "RENAME", "QUALIFY", "UNQUALIFY", "STORE", "BINARY", "DIRECTORY", "ALIAS", "TAG", "UNTAG",
                        "COMMENT", "MAP"):
                out.notes.append(f"{word} statement: {_one_line(st)}")
            continue
        body = body[4:].strip()
        distinct = bool(re.match(r"^DISTINCT\b", body, re.I))
        if distinct:
            body = body[8:].strip()
        hits = _keyword_at(body, ("FROM", "RESIDENT", "INLINE", "AUTOGENERATE", "EXTENSION", "WHERE", "WHILE", "GROUP BY",
                                  "ORDER BY"))
        field_text = body[:hits[0][0]] if hits else body
        fields, star = parse_fields(field_text)
        if not hits:  # a preceding LOAD: its source is the next statement
            pending.append((label, st, fields, star))
            continue
        load = Load(label, st, fields, star)
        if distinct:
            load.problems.append("LOAD DISTINCT")
        kw = {k: body[p + len(k):(hits[i + 1][0] if i + 1 < len(hits) else len(body))].strip() for i, (p, k) in enumerate(hits)}
        if "FROM" in kw:
            src = kw["FROM"]
            pm = re.match(r"^(\[[^\]]+\]|'[^']+'|\"[^\"]+\"|\S+)\s*(\((.*)\))?\s*$", src, re.S)
            load.source, load.path = "file", _name(pm.group(1)).strip("'") if pm else src
            fmt = (pm.group(3) or "").lower() if pm else ""
            if re.search(r"\bqvd\b", fmt) or load.path.lower().endswith(".qvd"):
                load.problems.append("reads a QVD file (Qlik's own format): land it in the warehouse first")
        elif "RESIDENT" in kw:
            load.source, load.path = "resident", _name(kw["RESIDENT"])
            load.problems.append(f"built from resident table {load.path} inside Qlik")
        elif "INLINE" in kw:
            load.source = "inline"
            load.problems.append("INLINE data typed into the script")
        elif "AUTOGENERATE" in kw:
            load.source = "autogenerate"
            load.problems.append("AUTOGENERATE rows generated by the script")
        else:
            load.source = "other"
            load.problems.append("loads through a connector extension")
        for k, why in (("WHERE", "filters rows with WHERE"), ("WHILE", "repeats rows with WHILE"), ("GROUP BY", "aggregates with GROUP BY"),
                       ("ORDER BY", "orders a resident table")):
            if k in kw:
                load.problems.append(why)
        for label_, text_, fields_, star_ in reversed(pending):
            load = _stack(load, label_, text_, fields_, star_)
        pending = []
        _prefixes(load, prefixes)
        if not load.name:
            load.name = Path(load.path.replace("\\", "/").split(":", 1)[-1]).stem if load.source == "file" else load.path or "table"
        if qualify:
            load.problems.append("QUALIFY prefixes field names with the table name")
        out.loads.append(load)
    return out


def _from_sql(label: str, text: str, sql: str, pending: list[tuple[str, str, list[tuple[str, str]], bool]]) -> Load:
    load = Load(label, text, source="sql", sql=sql)
    m = re.match(r"^SELECT\s+(.*?)\s+FROM\s+([\w.\"\[\]`$]+)\s*$", sql, re.I | re.S)
    if not m or re.search(r"\b(JOIN|WHERE|GROUP\s+BY|UNION|HAVING|DISTINCT|LIMIT|TOP|WITH)\b", sql, re.I):
        load.problems.append("SQL SELECT with more than a column list from one table")
        load.star = True  # its columns come from the data model
    else:
        cols, star = parse_fields(m.group(1))
        load.fields, load.star = [(_name(e), n) for e, n in cols], star
        if any(not re.fullmatch(r"[\w\"\[\]`]+", e) for e, _ in cols):
            load.problems.append("SQL SELECT computes columns")
        load.path = ".".join(_name(p) for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", m.group(2)))
    for label_, text_, fields_, star_ in reversed(pending):
        load = _stack(load, label_, text_, fields_, star_)
    if not load.name:
        load.name = load.path.split(".")[-1] if load.path else "table"
    return load


def _stack(inner: Load, label: str, text: str, fields: list[tuple[str, str]], star: bool) -> Load:
    """A preceding LOAD over `inner`: its expressions read inner's fields."""
    by_name = {n: e for e, n in inner.fields}
    out = Load(label or inner.name, f"{text};\n{inner.text}", source=inner.source, path=inner.path, sql=inner.sql,
               problems=list(inner.problems), target=inner.target)
    if star:
        out.fields, out.star = list(inner.fields), inner.star
    for expr, name in fields:
        plain = _is_name(expr)
        refs = [_name(expr)] if plain else _safe_fields(expr)
        if not inner.star and any(r not in by_name for r in refs):
            out.problems.append(f"preceding LOAD reads a field its source doesn't return ({_one_line(expr)})")
        if plain:
            out.fields.append((by_name.get(_name(expr), expr), name))
            continue
        if any(by_name.get(r, r) != r for r in refs):
            out.problems.append("preceding LOAD computes over renamed fields")
        out.fields.append((expr, name))
    return out


def _is_name(expr: str) -> bool:
    return bool(re.fullmatch(r"\[[^\]]+\]|\"[^\"]+\"|`[^`]+`|[A-Za-z_][\w.]*", expr.strip()))


def _safe_fields(expr: str) -> list[str]:
    try:
        return fields_of(parse_expr(expr))
    except Untranslatable:
        return []


def _prefixes(load: Load, prefixes: list[tuple[str, str]]) -> None:
    for word, arg in prefixes:
        if word in ("NOCONCATENATE", "SEMANTIC"):
            continue
        if word.endswith("JOIN") or word.endswith("KEEP") or word == "CONCATENATE":
            load.target = _name(arg) if arg else ""
            load.problems.append(f"{word.title()} into {load.target or 'the previous table'}")
        elif word == "MAPPING":
            load.source = "mapping"
        else:
            load.problems.append(f"{word.title()} prefix")


def inline_table(st: str) -> tuple[list[str], list[list[str]]] | None:
    """The header and rows of `LOAD * INLINE [ ... ]` (comma-separated, or the delimiter it names)."""
    m = re.search(r"\bINLINE\s*\[(.*)\]\s*(\((.*)\))?\s*$", st, re.I | re.S)
    if not m:
        return None
    delim = ","
    d = re.search(r"delimiter\s+is\s+'(.)'", m.group(3) or "", re.I)
    if d:
        delim = d.group(1)
    rows = [r for r in csv.reader(io.StringIO(m.group(1).strip()), delimiter=delim, skipinitialspace=True)]
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    return (rows[0], rows[1:]) if rows else None


def _one_line(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


# ------------------------------------------------------------ the data model of one app


@dataclass
class QTable:
    name: str  # Qlik table name
    model: str  # semantic model name
    relation: str  # warehouse relation the model reads
    fields: dict[str, Node | str | None] = field(default_factory=dict)  # field -> source column (str), row expression, or None (unknown)
    types: dict[str, str] = field(default_factory=dict)
    untranslated: dict[str, str] = field(default_factory=dict)  # field -> script expression unshackle can't translate
    rows: int | None = None
    todo: str = ""
    load: Load | None = None


class AppModel:
    """Qlik's associative model for one app: tables, the field -> tables index and the many-to-one key joins."""

    def __init__(self, script: Script, dm: dict[str, Any], variables: dict[str, str], used: set[str]):
        self.variables = variables
        self.tables: dict[str, QTable] = {}
        self.warnings: list[tuple[str, str]] = []
        meta_tables: dict[str, int] = dm.get("tables") or {}
        self.stats: dict[str, dict[str, dict[str, Any]]] = dm.get("fields") or {}
        loads: dict[str, Load] = {}
        for ld in script.loads:
            if ld.source == "mapping":
                continue
            if ld.target and ld.target in loads:
                loads[ld.target].problems.append(f"{ld.text.split()[0]} adds rows or columns from another statement")
                loads[ld.target].text += f";\n\n{ld.text}"
                continue
            if ld.name in loads:  # Qlik concatenates a second load with the same label
                loads[ld.name].problems.append("several LOAD statements build this table")
                loads[ld.name].text += f";\n\n{ld.text}"
                continue
            loads[ld.name] = ld
        if meta_tables:
            for ld in list(loads.values()):
                if ld.name in meta_tables:
                    continue
                same = next((o for o in loads.values() if o is not ld and o.name in meta_tables and not ld.star and not o.star
                             and {n for _, n in o.fields} == {n for _, n in ld.fields}), None)
                if same:  # identical field lists: Qlik concatenates the rows into the first table
                    same.problems.append(f"rows from another LOAD ({ld.name}) are concatenated into it")
                    same.text += f";\n\n{ld.text}"
                else:
                    self.warnings.append((ld.name, "a LOAD in the script whose table isn't in the data model (dropped, renamed "
                                                   "or concatenated into another table); check the tables built from it"))
        names = list(meta_tables) if meta_tables else list(loads)
        for name in names:
            ld = loads.get(name)
            model = _unique(snake(name), used)
            t = QTable(name, model, model, load=ld, rows=meta_tables.get(name))
            meta_cols = list(self.stats.get(name) or {})
            t.types = {f: _meta_type(st.get("tags") or []) for f, st in (self.stats.get(name) or {}).items()}
            todos: list[str] = []
            if ld is None:
                todos.append("this table isn't a LOAD statement unshackle could trace in the script")
                t.fields = {c: None for c in meta_cols}
            else:
                todos += ld.problems
                if ld.source == "sql" and ld.path:
                    t.relation = ld.path
                elif ld.source == "file":
                    t.relation = snake(Path(ld.path.replace("\\", "/").split(":", 1)[-1]).stem) or model
                listed = {fname: _field_source(expr, variables) for expr, fname in ld.fields}
                t.untranslated = {fname: expr for expr, fname in ld.fields if listed[fname] is None}
                defined: dict[str, Node | str | None] = {c: c for c in meta_cols if c not in listed} if ld.star else {}
                defined.update(listed)  # `LOAD *, expr AS name`: the file's columns, then the listed fields
                if ld.star and not meta_cols:
                    todos.append("LOAD * without the app's data model: its columns are unknown")
                if todos:  # rebuilt as a SQL model that returns the fields by name
                    defined, t.untranslated = {c: c for c in defined}, {}
                t.fields = {c: defined.get(c) for c in [c for c in defined if c in meta_cols or not meta_cols]
                            + [c for c in meta_cols if c not in defined]}
            t.todo = join_todos(dict.fromkeys(todos))
            self.tables[name] = t
        self.field_tables: dict[str, list[str]] = {}
        for t in self.tables.values():
            for f in t.fields:
                self.field_tables.setdefault(f, []).append(t.name)

    # --- keys

    def unique_in(self, f: str, table: str) -> bool:
        """Every row of the table has its own value of the field (distinct values = non-null values = rows)."""
        st, rows = (self.stats.get(table) or {}).get(f) or {}, self.tables[table].rows
        return rows is not None and st.get("distinct") == rows and st.get("nonnull", rows) == rows

    def complete_in(self, f: str, table: str) -> bool:
        """The table holds every value the field has anywhere in the app."""
        st = (self.stats.get(table) or {}).get(f) or {}
        return st.get("distinct") is not None and st.get("distinct") == st.get("total")

    def joins(self, table: str) -> list[tuple[str, str]]:
        """(key field, table) this table joins many-to-one to."""
        out = []
        for f in self.tables[table].fields:
            for other in self.field_tables.get(f, []):
                if other != table and self.unique_in(f, other):
                    out.append((f, other))
        return out

    def home(self, f: str) -> str:
        """The table an aggregation over field f reads (for a key: the table where it's unique)."""
        tables = self.field_tables.get(f, [])
        if not tables:
            raise Untranslatable(f"field {f} is not in the app's data model")
        if len(tables) == 1:
            return tables[0]
        homes = [t for t in tables if self.unique_in(f, t) and self.complete_in(f, t)]
        if homes:
            return homes[0]
        raise Untranslatable(f"key field {f} is in {', '.join(tables)} and no one table holds all its values; "
                             "Qlik aggregates it across the association")

    def path(self, base: str, target: str, step: Callable[[str], list[tuple[str, str]]] | None = None
             ) -> list[tuple[str, str, str]] | None:
        """Hops (from table, key, to table) from base to target, or None; many-to-one unless `step` says otherwise."""
        seen, queue = {base: []}, [base]
        for t in queue:
            for key, other in (step or self.joins)(t):
                if other not in seen:
                    seen[other] = seen[t] + [(t, key, other)]
                    queue.append(other)
        return seen.get(target)

    def neighbours(self, table: str) -> list[tuple[str, str]]:
        """(key field, table) associated in either direction."""
        return [(f, o) for f in self.tables[table].fields for o in self.field_tables.get(f, []) if o != table]

    # --- SQL for one field

    def field_sql(self, table: str, f: str, qualifier: str = "") -> str:
        t = self.tables[table]
        src = t.fields.get(f)
        if src is None and f not in t.fields:
            raise Untranslatable(f"field {f} is not in table {table}")
        q = (lambda c: f"{qualifier}.{quote(c)}") if qualifier else quote
        if src is None:
            raise Untranslatable(f"field {f} of table {table} isn't traced to a column in the load script")
        if isinstance(src, str):
            return q(src)
        return row_sql(src, q)


def data_model(tables_and_keys: dict[str, Any] | None, metadata: dict[str, Any] | None) -> dict[str, Any]:
    """{"tables": {table: rows}, "fields": {table: {field: {distinct, nonnull, total, tags}}}} from the engine's
    GetTablesAndKeys (per-table distinct counts), else from the REST data metadata (one distinct count per field,
    so a field counts as unique only in a table with as many rows)."""
    out: dict[str, Any] = {"tables": {}, "fields": {}}
    if tables_and_keys:
        for t in tables_and_keys.get("qtr") or []:
            out["tables"][t["qName"]] = t.get("qNoOfRows")
            out["fields"][t["qName"]] = {f["qName"]: {"distinct": f.get("qnPresentDistinctValues"), "nonnull": f.get("qnNonNulls"),
                                                     "total": f.get("qnTotalDistinctValues"), "tags": f.get("qTags") or []}
                                        for f in t.get("qFields") or []}
        return out
    for t in (metadata or {}).get("tables") or []:
        if not t.get("is_system") and not str(t.get("name", "")).startswith("$$"):
            out["tables"][t["name"]] = t.get("no_of_rows")
            out["fields"][t["name"]] = {}
    for f in (metadata or {}).get("fields") or []:
        for t in f.get("src_tables") or []:
            if t in out["fields"] and not f.get("is_system"):
                out["fields"][t][f["name"]] = {"distinct": f.get("cardinal"), "total": f.get("cardinal"), "tags": f.get("tags") or []}
    return out


def _field_source(expr: str, variables: dict[str, str]) -> Node | str | None:
    e = expr.strip()
    if _is_name(e):
        return _name(e)
    try:
        node = parse_expr(e, variables)
        row_sql(node, quote)  # translatable now, or never
        return node
    except Untranslatable:
        return None


def _meta_type(tags: list[str]) -> str:
    if "$timestamp" in tags:
        return "timestamp"
    if "$date" in tags:
        return "date"
    if "$numeric" in tags or "$integer" in tags:
        return "number"
    return "string"


def _unique(base: str, used: set[str]) -> str:
    name, n = base or "table", 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


# ------------------------------------------------------------ measures -> SQL


@dataclass
class Plan:
    """FROM + joins for one query: the base table and the tables joined to it."""

    model: AppModel
    base: str
    joined: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)

    def col(self, f: str, prefer: str = "") -> str:
        tables = self.model.field_tables.get(f, [])
        if not tables:
            raise Untranslatable(f"field {f} is not in the app's data model")
        for t in ([prefer] if prefer in tables else []) + [self.base] + [t for t in tables if t in self.joined]:
            if t in tables:
                return self.model.field_sql(t, f, self.alias(t))
        for t in tables:
            hops = self.model.path(self.base, t)
            if hops is not None:
                self.joined[t] = hops
                for _, _, mid in hops:
                    self.joined.setdefault(mid, self.model.path(self.base, mid) or [])
                return self.model.field_sql(t, f, self.alias(t))
        raise Untranslatable(f"field {f} is in {', '.join(tables)}, which {self.base} doesn't join to many-to-one")

    def alias(self, table: str) -> str:
        return self.model.tables[table].model

    def from_sql(self) -> str:
        base = self.model.tables[self.base]
        lines = [f"FROM {_relation(base)}"]
        done = {self.base}
        for t, hops in sorted(self.joined.items(), key=lambda i: len(i[1])):
            for a, key, b in hops:
                if b in done:
                    continue
                tb = self.model.tables[b]
                lines.append(f"LEFT JOIN {_relation(tb)} ON {self.model.field_sql(b, key, tb.model)} = "
                             f"{self.model.field_sql(a, key, self.model.tables[a].model)}")
                done.add(b)
        return "\n".join(lines)


def _relation(t: QTable) -> str:
    return t.relation if t.relation == t.model else f"{t.relation} AS {t.model}"


def arith(node: Node, leaf: Callable[[Call], str | None], post: bool = False) -> str:
    """Arithmetic over aggregations; `leaf` renders an aggregation (None: not one). `post`: a post-aggregate
    expression over ${metric} references, where a plain ratio is left to Ossie."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Call) and (s := leaf(node)) is not None:
        return s
    if isinstance(node, Bin) and node.op in ("+", "-", "*", "/"):
        left, right = arith(node.left, leaf, post), arith(node.right, leaf, post)
        if node.op == "/" and post and isinstance(node.left, Call) and isinstance(node.right, Call):
            return f"{left} / {right}"  # a ratio metric: Ossie divides it as 1.0 * a / nullif(b, 0)
        if isinstance(node.left, Bin):
            left = f"({left})"
        if node.op == "/":
            return f"1.0 * {left} / nullif({right}, 0)"
        if isinstance(node.right, Bin):
            right = f"({right})"
        return f"{left} {node.op} {right}"
    if isinstance(node, Un) and node.op == "-":
        return f"-({arith(node.operand, leaf, post)})"
    if isinstance(node, Call) and node.name in ("num", "money") and node.args:
        return arith(node.args[0], leaf, post)
    raise Untranslatable("not arithmetic over aggregations" if post else "not an aggregation, or arithmetic over aggregations")


def measure_sql(node: Node, plan: Plan) -> str:
    """SQL for a chart measure (after measure_base has checked it): aggregations, with set modifiers as CASE WHEN,
    and arithmetic between them."""
    return arith(node, lambda c: _agg_sql(c, plan) if c.name in AGGS else None)


def _agg_sql(node: Call, plan: Plan) -> str:
    if len(node.args) != 1:
        raise Untranslatable(f"{node.name}() with {len(node.args)} arguments")
    arg = node.args[0]
    filters, _, _ = parse_set(node.set)
    home = _home(arg, plan.model)
    inner = row_sql(arg, lambda f: plan.col(f, home))
    if filters:
        cond = " AND ".join(set_predicate(f, plan.col) for f in filters)
        inner = f"CASE WHEN {cond} THEN {inner} END"
    fn = AGGS[node.name]
    if node.distinct:
        return f"{fn}(DISTINCT {inner})"
    return f"{fn}({inner})"


def _calls(node: Node) -> list[Call]:
    if isinstance(node, Call):
        return [node] + [c for a in node.args for c in _calls(a)]
    if isinstance(node, Bin):
        return _calls(node.left) + _calls(node.right)
    if isinstance(node, Un):
        return _calls(node.operand)
    return []


def _home(arg: Node, model: AppModel) -> str:
    homes = {model.home(f) for f in fields_of(arg)}
    if len(homes) > 1:
        raise Untranslatable(f"aggregates over fields of several tables ({', '.join(sorted(homes))}): Qlik evaluates it over their association")
    return homes.pop() if homes else ""


def check_supported(node: Node) -> None:
    """Raise for the Qlik constructs unshackle never translates, before anything else looks at the expression."""
    for c in _calls(node):
        if c.name in NOT_TRANSLATED:
            raise Untranslatable(NOT_TRANSLATED[c.name])
    for c in _calls(node):
        if c.name in AGGS and c.total:
            raise Untranslatable("TOTAL (aggregates over all chart rows)")
        if c.name in AGGS and any(aggregates_of(a) for a in c.args):
            raise Untranslatable("nested aggregation")


def measure_base(node: Node, model: AppModel) -> str:
    """The one table every aggregation of a measure reads."""
    check_supported(node)
    homes = set()
    for agg in aggregates_of(node):
        if agg.args:
            h = _home(agg.args[0], model)
            if h:
                homes.add(h)
    if len(homes) > 1:
        raise Untranslatable(f"aggregates over several tables ({', '.join(sorted(homes))})")
    if not homes:
        raise Untranslatable("no aggregation over a field")
    return homes.pop()


# ------------------------------------------------------------ source


class QlikSource(Source):
    name = "qlik"
    label = "Qlik Sense"
    env_vars = ("QLIK_TENANT_URL", "QLIK_CLIENT_ID", "QLIK_CLIENT_SECRET", "QLIK_API_KEY")
    docs = "https://qlik.dev/apis/"
    manual_files = "a `qlik app unbuild` folder (script.qvs, measures.json, dimensions.json, variables.json, objects/), or a raw extract folder"
    manual_gaps = (
        "no data model: `LOAD *` columns, field types and joins are unknown unless you add the app's "
        "GET /api/v1/apps/{id}/data/metadata response as data-metadata.json next to script.qvs",
        "no bookmarks, no usage (item views)",
    )

    def __init__(self, client: Client | None = None, token: str = ""):
        self._client, self._token = client, token

    # ------------------------------------------------------------ network

    def client(self) -> Client:
        if self._client is None:
            tenant = env("QLIK_TENANT_URL").rstrip("/")
            if not tenant.startswith("http"):
                tenant = f"https://{tenant}"
            c = Client(tenant, per_minute=600)
            key = env("QLIK_API_KEY", required=False)
            if not key:
                res = c.post_json("/oauth/token", json={
                    "client_id": env("QLIK_CLIENT_ID"), "client_secret": env("QLIK_CLIENT_SECRET"),
                    "grant_type": "client_credentials", "scope": "user_default"})
                key = res["access_token"]
            c.set_header("Authorization", f"Bearer {key}")
            self._client, self._token = c, key
        return self._client

    def _pages(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out, url, base = [], path, self.client().base_url
        while url:
            res = self.client().get_json(url, params=params if url == path else None)
            out += res.get("data") or []
            nxt = ((res.get("links") or {}).get("next") or {})
            url = nxt.get("href", "") if isinstance(nxt, dict) else str(nxt or "")
            url = re.sub(r"^https?://[^/]+", "", url) if url.startswith(base.split("://")[0]) else url
        return out

    def _apps(self) -> list[dict[str, Any]]:
        return [{"id": r["resourceId"], "name": r.get("name") or "", "spaceId": r.get("spaceId") or "",
                 "itemViews": r.get("itemViews") or {}, "lastReloadTime": (r.get("resourceAttributes") or {}).get("lastReloadTime", ""),
                 "hasSectionAccess": (r.get("resourceAttributes") or {}).get("hasSectionAccess", False)}
                for r in self._pages("/api/v1/items", resourceType="app", limit=100)]

    def check(self) -> str:
        me = self.client().get_json("/api/v1/users/me")
        return f"{self.client().base_url} as {me.get('name') or me.get('id')}"

    def inventory(self) -> Inventory:
        inv = Inventory("qlik")
        base = self.client().base_url
        spaces = {s["id"]: s.get("name", "") for s in self._pages("/api/v1/spaces", limit=100)}
        for a in self._apps():
            views = (a.get("itemViews") or {}).get("total")
            inv.add("app", f"app:{a['id']}", a["name"], parent=spaces.get(a["spaceId"], "personal" if not a["spaceId"] else ""),
                    views=views, last_viewed="", url=f"{base}/sense/app/{a['id']}",
                    extra={"last_reload": a["lastReloadTime"], "section_access": a["hasSectionAccess"]})
        inv.notes.append("Ids are app:<id>; pass them to `extract --only`. Views are Qlik's item views of the app "
                         "(Qlik counts views per app, not per sheet).")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        c = self.client()
        apps = self._apps()
        store.put_json("spaces.json", [{k: s.get(k) for k in ("id", "name", "type")} for s in self._pages("/api/v1/spaces", limit=100)])
        wanted = {i.split(":", 1)[1] for i in only or set() if i.startswith("app:")}
        apps = [a for a in apps if not only or a["id"] in wanted]
        store.put_json("apps.json", apps)
        version = ""
        for a in apps:
            try:
                version = self._extract_app(store, a["id"]) or version
            except (SourceError, OSError, TimeoutError) as exc:
                errors.append({"item": f"app {a['name']} ({a['id']})", "error": str(exc)[:300]})
        store.put_json("tenant.json", {"url": c.base_url, "engine": version})
        store.put_json("_errors.json", errors)

    def _extract_app(self, store: RawStore, app: str) -> str:
        c = self.client()
        p = f"apps/{app}/"
        try:
            with Engine(engine_url(c.base_url, app), self._token) as e:  # with data: per-table key statistics
                doc = e.handle(e.call(-1, "OpenDoc", app))
                tk = e.call(doc, "GetTablesAndKeys", {"qcx": 1000, "qcy": 1000}, {"qcx": 0, "qcy": 0}, 30, True, False)
                store.put_json(p + "tables-and-keys.json", {"qtr": [{k: v for k, v in t.items() if k not in ("qPos", "qProfilingData")}
                                                                    for t in tk.get("qtr") or []], "qk": tk.get("qk") or []})
        except (SourceError, OSError, TimeoutError):  # no data access: the REST metadata has one distinct count per field
            meta = c.get_json(f"/api/v1/apps/{app}/data/metadata")
            meta["fields"] = [{k: v for k, v in f.items() if k not in ("hash", "byte_size")} for f in meta.get("fields") or []]
            store.put_json(p + "data-metadata.json", meta)
        with Engine(engine_url(c.base_url, app), self._token) as e:
            version = e.call(-1, "EngineVersion").get("qVersion", {}).get("qComponentVersion", "")
            doc = e.handle(e.call(-1, "OpenDoc", app, "", "", "", True))
            store.put_json(p + "app-properties.json", e.call(doc, "GetAppProperties").get("qProp") or {})
            store.put_text(p + "script.qvs", e.call(doc, "GetScript").get("qScript") or "")
            conns = e.call(doc, "GetConnections").get("qConnections") or []
            store.put_text(p + "connections.yml", yaml.safe_dump({"connections": {
                x.get("qName"): {"type": x.get("qType"), "connectionstring": x.get("qConnectionString")} for x in conns}},
                sort_keys=True, allow_unicode=True))
            infos = e.call(doc, "GetAllInfos").get("qInfos") or []
            for kind, method, rel in (("dimension", "GetDimension", "dimensions.json"), ("measure", "GetMeasure", "measures.json")):
                items = []
                for i in infos:
                    if i["qType"] == kind:
                        items.append(e.call(e.handle(e.call(doc, method, i["qId"])), "GetProperties").get("qProp"))
                store.put_json(p + rel, items)
            vl = e.handle(e.call(doc, "CreateSessionObject", {"qInfo": {"qType": "VariableList"}, "qVariableListDef": {
                "qType": "variable", "qShowReserved": False, "qShowConfig": False, "qData": {"tags": "/tags"}}}))
            variables = []
            for item in e.call(vl, "GetLayout")["qLayout"]["qVariableList"].get("qItems") or []:
                prop = e.call(e.handle(e.call(doc, "GetVariableById", item["qInfo"]["qId"])), "GetProperties").get("qProp") or {}
                if item.get("qIsScriptCreated"):
                    prop["qIsScriptCreated"] = True
                variables.append(prop)
            store.put_json(p + "variables.json", variables)
            for i in infos:
                if i["qType"] in ("sheet", "masterobject"):
                    tree = e.call(e.handle(e.call(doc, "GetObject", i["qId"])), "GetFullPropertyTree").get("qPropEntry") or {}
                    title = ((tree.get("qProperty") or {}).get("qMetaDef") or {}).get("title") or ""
                    store.put_json(p + f"objects/{i['qType']}-{slugify(title) if title else ''}-{safe_name(i['qId'])}.json", tree)
            marks = []
            for i in infos:
                if i["qType"] == "bookmark":
                    lay = e.call(e.handle(e.call(doc, "GetBookmark", i["qId"])), "GetLayout").get("qLayout") or {}
                    marks.append({"qInfo": lay.get("qInfo"), "qMetaDef": {k: (lay.get("qMeta") or {}).get(k) for k in ("title", "description")},
                                  "qBookmark": {"qStateData": (lay.get("qBookmark") or {}).get("qStateData") or []}})
            store.put_json(p + "bookmarks.json", marks)
        return version

    # ------------------------------------------------------------ manual import

    def import_path(self, path: Path) -> dict[str, bytes]:
        files = dict(files_of(path))
        if any(rel.startswith("apps/") for rel in files):  # a raw extract folder
            return {rel: p.read_bytes() for rel, p in files.items() if rel.startswith("apps/") or rel in ("apps.json", "spaces.json", "tenant.json")}
        out: dict[str, bytes] = {}
        roots = sorted({rel.rsplit("script.qvs", 1)[0] for rel in files if rel == "script.qvs" or rel.endswith("/script.qvs")})
        for root in roots:
            props = files.get(root + "app-properties.json")
            title = json.loads(props.read_bytes()).get("qTitle", "") if props else ""
            app = safe_name(slugify(title) if title else (root.strip("/").split("/")[-1] or path.stem))
            for rel, p in files.items():
                if not rel.startswith(root):
                    continue
                sub = rel[len(root):]
                if sub in APP_FILES or (sub.startswith("objects/") and sub.endswith(".json")):
                    out[f"apps/{app}/{sub}"] = p.read_bytes()
        return out

    # ------------------------------------------------------------ parse

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool="qlik")
        tenant = store.get_json("tenant.json") if store.exists("tenant.json") else {}
        apps = {a["id"]: a for a in (store.get_json("apps.json") if store.exists("apps.json") else [])}
        ids = sorted({rel.split("/")[1] for rel in store.glob("apps/*/script.qvs")})
        used_models: set[str] = set()
        used_slugs: set[str] = set()
        engines: list[str] = []
        tables_all: list[NTable] = []
        post: list[tuple[str, str]] = []  # (metric source_ref, filter) applied after build_semantic
        dim_todos: list[tuple[str, str, str, str]] = []  # (model, dimension, todo, source expression), likewise
        for app in ids:
            ctx = _App(store, app, apps.get(app, {}), tenant.get("url", ""))
            engines += ctx.engines
            ctx.semantic(b, used_models, tables_all, post, dim_todos)
            ctx.sheets(b, used_slugs)
            ctx.security(b)
            ctx.knowledge(b)
        build_semantic(b, tables_all, "qlik")
        metrics = {m.source_ref: m for m in b.metrics}
        for ref, flt in post:
            if ref in metrics:
                metrics[ref].filter = flt
        todos = {t.name: t.todo for t in tables_all if t.todo}
        dims = {(m, d): (todo, src) for m, d, todo, src in dim_todos}
        for sm in b.semantic_models:
            sm.todo = todos.get(sm.name, "")
            for d in sm.dimensions:
                if (sm.name, d.name) in dims:
                    d.todo, d.source_expression = dims[(sm.name, d.name)]
                    d.source_language = "qlik"
        b.detect_warehouse(engines)
        for e in store.get_json("_errors.json") if store.exists("_errors.json") else []:
            b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return b.settle_time_dimensions()


class _App:
    """Parse state for one app folder."""

    def __init__(self, store: RawStore, app: str, row: dict[str, Any], tenant: str):
        self.store, self.app, self.row, self.tenant = store, app, row, tenant
        p = f"apps/{app}/"
        load = lambda rel, default: store.get_json(p + rel) if store.exists(p + rel) else default  # noqa: E731
        self.props = load("app-properties.json", {})
        self.title = self.props.get("qTitle") or row.get("name") or app
        self.script_text = store.get_text(p + "script.qvs") if store.exists(p + "script.qvs") else ""
        self.script = parse_script(self.script_text)
        self.dims = {d["qInfo"]["qId"]: d for d in load("dimensions.json", [])}
        self.measures = {m["qInfo"]["qId"]: m for m in load("measures.json", [])}
        self.bookmarks = load("bookmarks.json", [])
        self.dm = data_model(load("tables-and-keys.json", None), load("data-metadata.json", None))
        self.variables = dict(self.script.variables)
        for v in load("variables.json", []):
            self.variables[v.get("qName", "")] = str(v.get("qDefinition", ""))
        self.objects: dict[str, dict[str, Any]] = {}
        for rel in store.glob(p + "objects/*.json"):
            tree = store.get_json(rel)
            prop = tree.get("qProperty", tree)
            self.objects[prop["qInfo"]["qId"]] = tree
        conns = yaml.safe_load(store.get_text(p + "connections.yml")) if store.exists(p + "connections.yml") else {}
        self.engines = []
        for c in ((conns or {}).get("connections") or {}).values():
            drv = re.search(r"\bdriver=([\w.-]+)", str((c or {}).get("connectionstring") or ""), re.I)
            name = drv.group(1) if drv else str((c or {}).get("type") or "")
            if name and not re.search(r"datafiles|folder|internet|web|\.exe$", name, re.I):
                self.engines.append(name)
        self.ref = f"qlik:app/{app}"

    # --- semantic models + metrics

    def semantic(self, b: Bundle, used: set[str], tables_all: list[NTable], post: list[tuple[str, str]],
                 dim_todos: list[tuple[str, str, str, str]]) -> None:
        self.model = AppModel(self.script, self.dm, self.variables, used)
        control = sorted({n.split()[2] for n in self.script.notes if n.startswith("control statement")})
        if control:
            b.warn(self.title, f"the load script loops or branches ({', '.join(control)}): a LOAD may run several times or not "
                               "at all, so check each table built from it")
        if not self.dm["tables"]:
            b.warn(self.title, "no data model (tables-and-keys.json or data-metadata.json): tables come from the script alone and "
                               "key directions are unknown, so there are no joins (skills/source-qlik)")
        self.ntables: dict[str, NTable] = {}
        self.dim_names: dict[tuple[str, str], str] = {}
        for name, why in self.model.warnings:
            b.warn(name, why)
        for t in self.model.tables.values():
            nt = NTable(name=t.model, table=t.relation, source_ref=f"{self.ref}#table/{t.name}",
                        todo=f"the load script builds this table in a way unshackle doesn't translate ({t.todo})" if t.todo else "")
            used_dims: set[str] = set()
            for f in sorted(t.fields, key=lambda f: snake(f) != f):  # exact names first: paper_type before PAPER_TYPE
                key = _unique(snake(f), used_dims)
                self.dim_names[(t.name, f)] = key
                src = t.fields[f]
                typ = t.types.get(f, "string")
                if isinstance(src, str):
                    sql = "" if src == key else quote(src)
                    nt.dims[key] = NField(key, sql, typ, label=f if f != key else "")
                elif src is not None:
                    nt.dims[key] = NField(key, row_sql(src, quote), _expr_type(src, typ), label=f if f != key else "",
                                          granularity=_grain(src))
                else:
                    nt.dims[key] = NField(key, "", typ, label=f if f != key else "")
                    why = f"the load script computes {f} with an expression unshackle doesn't translate" if f in t.untranslated else \
                        f"field {f} isn't a column unshackle could trace in the load script"
                    dim_todos.append((nt.name, key, why, t.untranslated.get(f) or (t.load.text if t.load else "")))
            pk = self._primary_key(t.name)
            if pk:
                nt.primary_key = self.dim_names[(t.name, pk)]
            if t.todo:
                why = t.todo
                if t.load and t.load.source == "sql" and t.load.sql:
                    b.queries.append(VerifiedQuery(f"{self.title}: {t.name}", t.load.sql, f"SQL the load script runs for table {t.name}",
                                                   datasource=b.datasource, source_ref=nt.source_ref,
                                                   todo=f"materialize as a SQL model so semantic model {t.model} has a table"))
                b.warn(t.model, f"load script ({why}); table {t.relation} needs a SQL model or a landing step "
                                f"(skills/source-qlik). Statement: {_one_line(t.load.text if t.load else t.name, 300)}")
            elif t.load and t.load.source == "file":
                b.warn(t.model, f"loaded from file {t.load.path}: land it in the warehouse as table {t.relation} (skills/source-qlik)", "info")
            self.ntables[t.name] = nt
        for t in self.model.tables.values():  # many-to-one joins onto the other table's primary key become foreign entities
            for key_field, other in self.model.joins(t.name):
                if self._primary_key(other) == key_field:
                    self.ntables[t.name].joins.append(NJoin(self.dim_names[(t.name, key_field)], self.model.tables[other].model,
                                                            self.dim_names[(other, key_field)]))
        self._master_measures(b, post)
        self._master_dimensions()
        tables_all.extend(self.ntables.values())

    def _primary_key(self, table: str) -> str:
        """The first field (in load order) with a distinct value on every row."""
        return next((f for f in self.model.tables[table].fields if self.model.unique_in(f, table)), "")

    def _master_dimensions(self) -> None:
        for d in self.dims.values():
            qd = d.get("qDim") or {}
            title = (d.get("qMetaDef") or {}).get("title") or qd.get("title") or ""
            defs = qd.get("qFieldDefs") or []
            if qd.get("qGrouping") in ("H", "C") or len(defs) != 1:
                continue
            text = defs[0].strip()
            try:
                node = parse_expr(text, self.variables)
                tables = {self.model.home(f) if len(self.model.field_tables.get(f, [])) > 1 else self.model.field_tables.get(f, [""])[0]
                          for f in fields_of(node)}
            except Untranslatable:
                continue
            if len(tables) != 1 or "" in tables:
                continue
            table = tables.pop()
            nt = self.ntables[table]
            if isinstance(node, Fld):
                key = self.dim_names.get((table, node.name))
                if key and not nt.dims[key].label:
                    nt.dims[key].label = title
                if key and not nt.dims[key].description:
                    nt.dims[key].description = (d.get("qMetaDef") or {}).get("description") or ""
                continue
            try:
                sql = row_sql(node, lambda f: self.model.field_sql(table, f))
            except Untranslatable:
                continue
            key = _unique(snake(title), set(nt.dims))
            nt.dims[key] = NField(key, sql, _expr_type(node, ""), label=title,
                                  description=(d.get("qMetaDef") or {}).get("description") or "", granularity=_grain(node),
                                  source=text)

    def _master_measures(self, b: Bundle, post: list[tuple[str, str]]) -> None:
        simple: dict[str, tuple[str, str]] = {}  # normalized expression -> (table, metric key)
        rows = sorted(self.measures.values(), key=lambda m: _measure_text(m))
        ordered = [m for m in rows if _is_single_agg(_measure_text(m))] + [m for m in rows if not _is_single_agg(_measure_text(m))]
        for m in ordered:
            title = (m.get("qMetaDef") or {}).get("title") or (m.get("qMeasure") or {}).get("qLabel") or m["qInfo"]["qId"]
            desc = (m.get("qMetaDef") or {}).get("description") or ""
            text = _measure_text(m)
            key = snake(title)
            try:
                node = parse_expr(text, self.variables)
                base = measure_base(node, self.model)
                nt = self.ntables[base]
                if isinstance(node, Call) and node.name in AGGS:
                    self._agg_field(nt, base, key, node, title, desc, text, post)
                    simple[_norm(text)] = (base, key)
                    continue
                refs: dict[int, str] = {}
                for i, agg in enumerate(aggregates_of(node)):
                    agg_text = _render_agg(agg)
                    hit = simple.get(_norm(agg_text))
                    if hit and hit[0] == base:
                        refs[id(agg)] = hit[1]
                        continue
                    part = f"{key}_part{i + 1}"
                    self._agg_field(nt, base, part, agg, f"{title} (part {i + 1})", f"Part of {title}: {agg_text}", agg_text, post)
                    simple[_norm(agg_text)] = (base, part)
                    refs[id(agg)] = part
                expr = arith(node, lambda c: f"${{{refs[id(c)]}}}" if id(c) in refs else None, post=True)
                nt.metrics[key] = NField(key, expr, "post", title, desc, source=text)
            except Untranslatable as exc:
                table = self._guess_table(text)
                nt = self.ntables.get(table) if table else None
                if nt is None and self.ntables:
                    nt = next(iter(self.ntables.values()))
                if nt is None:
                    b.warn(title, f"master measure not migrated: {exc}")
                    continue
                nt.metrics[key] = NField(key, "", "sum", title, desc, source=text,
                                         todo=f"Qlik expression: {exc}; rebuild it (skills/translate-metrics)")

    def _agg_field(self, nt: NTable, base: str, key: str, node: Call, title: str, desc: str, text: str,
                   post: list[tuple[str, str]]) -> None:
        if len(node.args) != 1:
            raise Untranslatable("nested aggregation" if node.args else f"{node.name}() without an argument")
        filters, ignores, _ = parse_set(node.set)
        sql = row_sql(node.args[0], lambda f: self.model.field_sql(base, f))
        agg = "count_distinct" if node.name == "count" and node.distinct else MF_AGG[node.name]
        if node.distinct and node.name != "count":
            raise Untranslatable(f"{node.name}(DISTINCT ...)")
        flt = []
        for f in filters:
            table = self._filter_table(base, f.field)
            dim = self.dim_names[(table, f.field)]
            ref = f"{{{{ Dimension('{self.model.tables[table].model}__{dim}') }}}}"
            flt.append(set_predicate(f, lambda _f, ref=ref: ref))
        nt.metrics[key] = NField(key, sql, agg, title, desc, source=text)
        if flt:
            post.append((f"{nt.source_ref}#{key}", " AND ".join(f"({x})" if len(flt) > 1 else x for x in flt)))

    def _filter_table(self, base: str, f: str) -> str:
        tables = self.model.field_tables.get(f, [])
        if base in tables:
            return base
        for t in tables:
            hops = self.model.path(base, t)
            if hops is not None:
                if len(hops) > 1:
                    raise Untranslatable(f"set modifier on {f}, more than one join away from {base}")
                return t
        raise Untranslatable(f"set modifier on {f}, which {base} doesn't join to many-to-one (Qlik filters through the association)")

    def _guess_table(self, text: str) -> str:
        try:
            names = re.findall(r"\[([^\]]+)\]|\b([A-Za-z_][\w.]*)\b", expand(text, self.variables))
        except Untranslatable:
            names = re.findall(r"\[([^\]]+)\]|\b([A-Za-z_][\w.]*)\b", text)
        for a, c in names:
            tables = self.model.field_tables.get(a or c)
            if tables:
                return tables[0]
        return ""

    # --- sheets -> notebooks

    def sheets(self, b: Bundle, used_slugs: set[str]) -> None:
        sheets = [t for t in self.objects.values() if t.get("qProperty", t)["qInfo"]["qType"] == "sheet"]
        sheets.sort(key=lambda t: (t["qProperty"].get("rank") or 0, (t["qProperty"].get("qMetaDef") or {}).get("title") or ""))
        masters = {k: v.get("qProperty", v) for k, v in self.objects.items() if v.get("qProperty", v)["qInfo"]["qType"] == "masterobject"}
        views = (self.row.get("itemViews") or {}).get("total")
        for tree in sheets:
            sp = tree["qProperty"]
            title = (sp.get("qMetaDef") or {}).get("title") or "Sheet"
            slug = slugify(title)
            if slug in used_slugs:
                slug = slugify(f"{self.title} {title}")
            used_slugs.add(slug)
            dash = Dashboard(name=title, slug=slug, description=(sp.get("qMetaDef") or {}).get("description") or "",
                             datasource=b.datasource, source_ref=f"{self.ref}/sheet/{sp['qInfo']['qId']}",
                             url=f"{self.tenant}/sense/app/{self.app}/sheet/{sp['qInfo']['qId']}" if self.tenant else "",
                             views=views)
            children = {c["qProperty"]["qInfo"]["qId"]: c for c in tree.get("qChildren") or []}
            filters: list[tuple[str, str]] = []  # (bind, field)
            for c in children.values():
                for lb in _listboxes(c):
                    fdefs = ((lb.get("qListObjectDef") or {}).get("qDef") or {}).get("qFieldDefs") or []
                    lib = (lb.get("qListObjectDef") or {}).get("qLibraryId")
                    if lib and lib in self.dims:
                        fdefs = (self.dims[lib].get("qDim") or {}).get("qFieldDefs") or []
                    if fdefs and not fdefs[0].startswith("="):
                        f = _name(fdefs[0])
                        bind = snake(f)
                        if bind and bind not in {x for x, _ in filters}:
                            filters.append((bind, f))
                            dash.filters.append(DashboardFilter(bind, f, None, "value"))
            used: set[str] = set()
            cells = []
            for cell in sp.get("cells") or []:
                c = children.get(cell.get("name"))
                if c is None:
                    continue
                for prop, note in _objects(c, masters):
                    tile = self._tile(prop, used, filters)
                    if note:
                        tile.todo = join_todos(x for x in (note, tile.todo) if x)
                    dash.tiles.append(tile)
                    cells.append((tile.id, float(cell.get("col") or 0), float(cell.get("row") or 0)))
            dash.layout = grid_rows(cells)
            b.dashboards.append(dash)

    def _tile(self, prop: dict[str, Any], used: set[str], filters: list[tuple[str, str]]) -> Tile:
        kind = _kind(prop)
        title = prop.get("title") if isinstance(prop.get("title"), str) else ""
        title = title or (prop.get("qMetaDef") or {}).get("title") or ""
        hc = prop.get("qHyperCubeDef") or {}
        dims, measures, todos = [], [], []
        for d in hc.get("qDimensions") or []:
            dims.append(self._dim_def(d, todos))
        for m in hc.get("qMeasures") or []:
            measures.append(self._measure_def(m))
        tid = _unique(snake(title or f"{kind} {prop['qInfo']['qId']}"), used)
        cfg = {k: prop.get(k) for k in ("visualization", "orientation", "barGrouping", "qHyperCubeDef") if k in prop}
        tile = Tile(id=tid, title=title or "", source_type=kind, source_config=cfg)
        if kind == "text-image":
            tile.kind, tile.text = "text", str(prop.get("markdown") or "")
            if measures:
                tile.todo = f"text with Qlik expressions ({'; '.join(t for _, t in measures)}); compute them in a SQL cell"
            return tile
        if kind not in MARKS and kind not in ("kpi", "table", "pivot-table", "sn-table", "sn-pivot-table"):
            tile.kind = "unknown"
            tile.todo = (f"Qlik {kind} object has no Altair mapping (extension, container or map); rebuild it"
                         + (f". Its hypercube: {', '.join(x[0] for x in dims + measures)}" if dims or measures else ""))
            return tile
        if any(_hc_option(d) for d in hc.get("qDimensions") or []):
            todos.append("dimension limit (top N with Others) in Qlik; add ORDER BY/LIMIT or an Others bucket")
        sql, names = self._tile_sql(dims, measures, filters, todos, hc)
        tile.sql = sql
        dcols = [Encoding(n, "temporal" if t else "nominal", title=l) for (l, _, t), n in zip(dims, names[:len(dims)])]
        mcols = [Encoding(n, "quantitative", title=l) for (l, _), n in zip(measures, names[len(dims):])]
        if kind == "kpi":
            tile.kind, tile.mark = "kpi", "text"
            if mcols:
                tile.encodings = {"text": mcols[0]}
            if len(mcols) > 1:
                todos.append("KPI with a comparison measure; only the first value is shown")
        elif kind in ("table", "sn-table", "pivot-table", "sn-pivot-table"):
            tile.kind, tile.mark = "table", "table"
            if "pivot" in kind:
                todos.append("pivot table: pivot the result with pandas (pivot_table) to match Qlik")
        elif kind == "piechart":
            tile.mark = "arc"
            tile.encodings = {k: v for k, v in (("theta", mcols[0] if mcols else None), ("color", dcols[0] if dcols else None)) if v}
        elif kind == "scatterplot":
            tile.mark = "point"
            if len(mcols) >= 2:
                tile.encodings = {"x": mcols[0], "y": mcols[1]}
            if dcols:
                tile.encodings["detail"] = dcols[0]
        else:
            tile.mark = MARKS[kind]  # type: ignore[assignment]
            horizontal = prop.get("orientation") == "horizontal" and kind == "barchart"
            xch, ych = ("y", "x") if horizontal else ("x", "y")
            if dcols:
                tile.encodings[xch] = dcols[0]
            if len(dcols) > 1:
                tile.encodings["color"] = dcols[1]
                if kind == "barchart" and (prop.get("barGrouping") or {}).get("grouping") != "stacked" and not horizontal:
                    tile.encodings["xOffset"] = dcols[1]
            if kind == "combochart" and len(mcols) > 1:
                types = [((m.get("qDef") or {}).get("series") or {}).get("type") or "bar" for m in hc.get("qMeasures") or []]
                axes = {((m.get("qDef") or {}).get("series") or {}).get("axis") for m in hc.get("qMeasures") or []}
                tile.layers = [Layer("line" if t == "line" else "point" if t == "marker" else "bar", {ych: e})  # type: ignore[arg-type]
                               for t, e in zip(types, mcols)]
                tile.compose = "dual_axis" if len(axes - {None}) > 1 else "layer"
            elif len(mcols) > 1 and kind == "linechart":
                tile.layers = [Layer("line", {ych: e}) for e in mcols]
            elif mcols:
                tile.encodings[ych] = mcols[0]
                if len(mcols) > 1:
                    todos.append(f"{len(mcols)} measures on a bar chart; only the first is encoded: fold them and use color/xOffset")
        tile.todo = join_todos(dict.fromkeys(todos))
        return tile

    def _dim_def(self, d: dict[str, Any], todos: list[str]) -> tuple[str, str, bool]:
        """(label, expression, is time) for a hypercube dimension."""
        qdef = d.get("qDef") or {}
        defs, labels = qdef.get("qFieldDefs") or [], qdef.get("qFieldLabels") or []
        title = ""
        if d.get("qLibraryId") and d["qLibraryId"] in self.dims:
            qd = self.dims[d["qLibraryId"]].get("qDim") or {}
            defs, labels = qd.get("qFieldDefs") or [], qd.get("qFieldLabels") or []
            title = (self.dims[d["qLibraryId"]].get("qMetaDef") or {}).get("title") or qd.get("title") or ""
            if qd.get("qGrouping") == "H":
                todos.append(f"drill-down dimension {title}: only its first level ({defs[0] if defs else '?'}) is migrated")
            elif qd.get("qGrouping") == "C":
                todos.append(f"cyclic dimension {title}: only its first field is migrated")
        elif d.get("qLibraryId"):
            todos.append(f"master dimension {d['qLibraryId']} is missing from dimensions.json")
            return (d["qLibraryId"], "", False)
        expr = defs[0] if defs else ""
        label = title or (labels[0] if labels and labels[0] else "") or _name(expr.lstrip("="))
        is_time = False
        try:
            node = parse_expr(expr, self.variables)
            if isinstance(node, Call) and node.name in TRUNC:
                is_time = True
            elif isinstance(node, Fld):
                tables = self.model.field_tables.get(node.name, [])
                is_time = bool(tables) and self.model.tables[tables[0]].types.get(node.name) in ("date", "timestamp")
        except Untranslatable:
            pass
        return (label, expr, is_time)

    def _measure_def(self, m: dict[str, Any]) -> tuple[str, str]:
        qdef = m.get("qDef") or {}
        if m.get("qLibraryId") and m["qLibraryId"] in self.measures:
            lib = self.measures[m["qLibraryId"]]
            title = (lib.get("qMetaDef") or {}).get("title") or (lib.get("qMeasure") or {}).get("qLabel") or ""
            return (title, _measure_text(lib))
        if m.get("qLibraryId"):
            return (m["qLibraryId"], "")
        return (qdef.get("qLabel") or qdef.get("qDef") or "", qdef.get("qDef") or "")

    def _tile_sql(self, dims: list[tuple[str, str, bool]], measures: list[tuple[str, str]], filters: list[tuple[str, str]],
                  todos: list[str], hc: dict[str, Any]) -> tuple[str, list[str]]:
        names_used: set[str] = set()
        names = [_unique(snake(l) or f"col_{i + 1}", names_used) for i, (l, *_rest) in enumerate(list(dims) + list(measures))]
        try:
            mnodes = []
            for label, text in measures:
                if not text:
                    raise Untranslatable(f"measure {label} is missing from measures.json")
                mnodes.append((label, text, parse_expr(text, self.variables)))
            dnodes = []
            for label, text, _ in dims:
                if not text:
                    raise Untranslatable(f"dimension {label} is missing from dimensions.json")
                dnodes.append(parse_expr(text, self.variables))
            bases = {measure_base(n, self.model) for _, _, n in mnodes}
            if len(bases) > 1:
                raise Untranslatable(f"measures read different tables ({', '.join(sorted(bases))})")
            if bases:
                base = bases.pop()
            elif dnodes and fields_of(dnodes[0]):
                base = self.model.field_tables.get(fields_of(dnodes[0])[0], [""])[0]
                if not base:
                    raise Untranslatable(f"field {fields_of(dnodes[0])[0]} is not in the app's data model")
            else:
                raise Untranslatable("no measure or dimension to query")
            plan = Plan(self.model, base)
            select, group, where = [], [], []
            for node, name, d in zip(dnodes, names, hc.get("qDimensions") or [{}] * len(dnodes)):
                expr = row_sql(node, plan.col)
                select.append(f"{expr} AS {name}")
                group.append(expr)
                if d.get("qNullSuppression"):
                    where.append(f"{expr} IS NOT NULL")
            overrides: set[str] = set()
            for (label, text, node), name in zip(mnodes, names[len(dims):]):
                select.append(f"{measure_sql(node, plan)} AS {name}")
                for agg in aggregates_of(node):
                    flt, ignores, cleared = parse_set(agg.set)
                    if ignores:
                        overrides.add("*")
                    overrides.update(f.field for f in flt)
                    overrides.update(cleared)
            for bind, f in filters:
                try:
                    col = plan.col(f)
                except Untranslatable as exc:
                    todos.append(f"filter pane field {f} applies to this chart in Qlik but not to its SQL ({exc})")
                    continue
                if "*" in overrides or f in overrides:
                    todos.append(f"a measure's set expression ignores selections on {f}, but :{bind} filters the whole query; "
                                 "move the filter into the other measures")
                where.append(f"({col} = :{bind} OR :{bind} IS NULL)")
        except Untranslatable as exc:
            exprs = "; ".join(f"{l} = {t}" for l, t in [(l, t) for l, t, _ in dims] + list(measures))
            todos.append(f"Qlik expression not translated: {exc}. Hypercube: {exprs}")
            return "", names
        for t in [plan.base, *plan.joined]:
            if self.model.tables[t].todo:
                todos.append(f"reads table {t}, which the load script builds in a way unshackle doesn't translate "
                             f"({self.model.tables[t].todo})")
        sql = "SELECT\n  " + ",\n  ".join(select) + "\n" + plan.from_sql()
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if group and mnodes:
            sql += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(group)))
        elif group:
            sql = sql.replace("SELECT\n", "SELECT DISTINCT\n", 1)
        if group:
            sql += "\nORDER BY " + ", ".join(str(i + 1) for i in range(len(group)))
        return sql, names

    # --- section access

    def security(self, b: Bundle) -> None:
        block = self.script.section_access
        if not block:
            return
        ref = f"{self.ref}/section-access"
        source = ";\n".join(block)
        tables = [inline_table(st) for st in block if re.search(r"\bLOAD\b", st, re.I)]
        slugs = [d.slug for d in b.dashboards if d.source_ref.startswith(self.ref + "/")]
        if not tables or any(t is None for t in tables) or len(tables) > 1:
            b.row_filters.append(RowFilter(snake(f"{self.title} section access"), "", source_expression=_one_line(source, 500),
                                           source_ref=ref, todo="Section Access loaded from a file, a database or several tables: "
                                                                "rebuild it as row filters per user (skills/source-qlik)"))
            return
        header, rows = tables[0]  # type: ignore[misc]
        cols = [h.upper() for h in header]
        ident = [c for c in ("USERID", "USER.EMAIL", "GROUP", "NTNAME") if c in cols]
        fields = [c for c in cols if c not in ("ACCESS", "USERID", "USER.EMAIL", "GROUP", "NTNAME", "OMIT", "PASSWORD", "SERIAL", "NTDOMAINSID", "NTSID")]
        listed: dict[str, list[str]] = {f: sorted({r[cols.index(f)].upper() for r in rows if len(r) > cols.index(f)} - {"*", ""}) for f in fields}
        principals: dict[str, list[dict[str, str]]] = {}
        for r in rows:
            r = r + [""] * (len(cols) - len(r))
            row = dict(zip(cols, r))
            for c in ident:
                if row.get(c) and row[c] != "*":
                    p = f"group:{row[c]}" if c == "GROUP" else f"user:{row[c]}"
                    principals.setdefault(p, []).append(row)
                elif row.get(c) == "*":
                    principals.setdefault("*", []).append(row)
        email = ident == ["USER.EMAIL"]
        idtodo = "" if email or ident == ["GROUP"] else \
            f"Qlik section access {'/'.join(ident)} values are Qlik user ids, not emails or IdP groups; map each to your IdP"
        omitted: set[tuple[str, str]] = set()
        for p, prow in principals.items():
            specs = [{f: listed[f] if r[f] == "*" else [r[f].upper()] if r[f] else [] for f in fields} for r in prow]
            if len(fields) == 1:  # one reduction field: the user's rows add up to one list of values
                specs = [{fields[0]: sorted({v for sp in specs for v in sp[fields[0]]})}]
            for t in self.model.tables.values():
                preds, why = [], ""
                for spec in specs:
                    parts = []
                    for f, values in spec.items():
                        pred, why = self._reduction(t.name, f, values) if values else ("1 = 0", "")
                        if pred is None:
                            break
                        if pred:
                            parts.append(pred)
                    if why:
                        break
                    if parts:
                        preds.append(" AND ".join(parts))
                if not preds and not why:
                    continue
                todos = [x for x in (why, idtodo if p != "*" else "") if x]
                pred = preds[0] if len(preds) == 1 else " OR ".join(f"({x})" for x in preds)
                who = p.split(":", 1)[-1]
                b.row_filters.append(RowFilter(snake(f"{t.model} {who if p != '*' else 'everyone'}"), t.relation,
                                               predicate="" if why else pred, groups=[who] if p != "*" else ["*"],
                                               source_expression=f"Section Access for {p}: "
                                                                 + ", ".join(f"{f} = {sorted({r[f] for r in prow})}" for f in fields),
                                               source_ref=ref, todo=join_todos(todos)))
            for row in prow:
                omit = row.get("OMIT", "")
                for t in self.model.tables.values():
                    for f in t.fields:
                        if omit and f.upper() == omit.upper() and (p, f"{t.name}.{f}") not in omitted:
                            omitted.add((p, f"{t.name}.{f}"))
                            b.permissions.append(Permission(p, "query", "Column", f"{t.relation}.{quote(f)}", "forbid",
                                                            source_expression=f"Section Access OMIT {omit}", source_ref=ref, todo=idtodo))
        allowed = [p for p in principals if p != "*"]
        if "*" not in principals and allowed:
            cond = " || ".join(f"principal == User::{json.dumps(p[5:])}" if p.startswith("user:") else f"principal in Group::{json.dumps(p[6:])}"
                               for p in allowed)
            for slug in slugs:
                b.permissions.append(Permission("*", "view", "Notebook", slug, "forbid", condition=f"!({cond})",
                                                source_expression="Section Access: users not listed can't open the app",
                                                source_ref=ref, todo=idtodo))
            for t in self.model.tables.values():
                b.permissions.append(Permission("*", "query", "Table", t.relation, "forbid", condition=f"!({cond})",
                                                source_expression="Section Access: users not listed see no data (strict exclusion)",
                                                source_ref=ref, todo=idtodo))

    def _reduction(self, table: str, f: str, values: list[str]) -> tuple[str | None, str]:
        """Predicate on `table` that keeps the rows associated with reduction field f in `values`.
        Qlik reduces the whole associated model: other tables keep rows whose keys reach an allowed row."""
        homes = self.model.field_tables.get(f, [])  # Qlik matches the upper-case field name exactly
        if not homes:
            return None, f"reduction field {f} is not in the data model (Qlik needs it upper case there too)"
        if len(homes) > 1:
            return None, f"reduction field {f} is a key in several tables"
        home, real = homes[0], f
        try:
            base = f"{self.model.field_sql(home, real)} IN ({', '.join(sql_literal(v) for v in values)})"
            if table == home:
                return base, ""
            path = self.model.path(table, home, self.model.neighbours)
            if path is None:
                return "", ""  # not associated: Qlik doesn't reduce it
            pred = base
            for (a, key, nxt) in reversed(path):
                tn = self.model.tables[nxt]
                pred = f"{self.model.field_sql(a, key)} IN (SELECT {self.model.field_sql(nxt, key)} FROM {tn.relation} WHERE {pred})"
            return pred, ""
        except Untranslatable as exc:
            return None, f"reduction on {f}: {exc}"

    # --- knowledge

    def knowledge(self, b: Bundle) -> None:
        if self.script_text.strip():
            body = "The Qlik load script of this app: the ETL that builds its tables. Rebuild what it does in the warehouse " \
                   "(dbt or SQL models) before trusting the semantic models built from it.\n\n```\n" + \
                   re.sub(r"(?is)(section\s+access\s*;).*?(section\s+application\s*;)", r"\1\n// (removed: see policies/)\n\2",
                          self.script_text).strip() + "\n```\n"
            b.knowledge.append(KnowledgeItem(f"{self.title} load script", body, f"Load script of Qlik app {self.title}",
                                             datasource=b.datasource, source_ref=f"{self.ref}/script"))
        if self.bookmarks:
            lines = []
            for bm in self.bookmarks:
                sel = []
                for state in (bm.get("qBookmark") or {}).get("qStateData") or []:
                    for fi in state.get("qFieldItems") or []:
                        name = (fi.get("qDef") or {}).get("qName") or ""
                        vals = [str(v.get("qText", v)) if isinstance(v, dict) else str(v) for v in fi.get("qValues") or []]
                        sel.append(f"{name}" + (f" = {', '.join(vals)}" if vals else " (selected values aren't stored in the layout)"))
                lines.append(f"- **{(bm.get('qMetaDef') or {}).get('title') or bm['qInfo']['qId']}**: {'; '.join(sel) or 'no selections'}")
            b.knowledge.append(KnowledgeItem(f"{self.title} bookmarks", "Saved selections in Qlik. Re-create the useful ones as "
                                             "notebook parameter presets.\n\n" + "\n".join(lines) + "\n",
                                             f"Bookmarks of Qlik app {self.title}", datasource=b.datasource, source_ref=f"{self.ref}/bookmarks"))


# ------------------------------------------------------------ helpers


def _kind(prop: dict[str, Any]) -> str:
    """The object's chart type: `visualization` (a childObject inside a container has only that), else its qType."""
    return str(prop.get("visualization") or prop["qInfo"]["qType"])


def _objects(child: dict[str, Any], masters: dict[str, dict[str, Any]], depth: int = 0) -> list[tuple[dict[str, Any], str]]:
    """The visualizations in one sheet cell, with a note when they sat in a tabbed container.

    Linked master visualizations (qExtendsId) are resolved; filter panes and decoration (buttons, shapes, menus)
    are dropped; containers are opened up, their children placed in the container's cell."""
    prop = child["qProperty"]
    if prop.get("qExtendsId") in masters:
        prop = {**masters[prop["qExtendsId"]], "qInfo": prop["qInfo"]}
    kind = _kind(prop)
    if kind in ("filterpane", "listbox") or kind in DECOR:
        return []
    if kind in CONTAINERS and child.get("qChildren") and depth < 4:
        note = f"one tab of a Qlik {kind} ({prop.get('title') or 'untitled'}); the notebook shows every tab" if kind != "sn-layout-container" else ""
        return [(p, n or note) for c in child["qChildren"] for p, n in _objects(c, masters, depth + 1)]
    return [(prop, "")]


def _listboxes(child: dict[str, Any]) -> list[dict[str, Any]]:
    prop = child.get("qProperty") or {}
    if prop.get("qInfo", {}).get("qType") == "listbox":
        return [prop]
    return [c["qProperty"] for c in child.get("qChildren") or [] if c.get("qProperty", {}).get("qInfo", {}).get("qType") == "listbox"]


def _hc_option(d: dict[str, Any]) -> bool:
    return ((d.get("qOtherTotalSpec") or {}).get("qOtherMode") or "OTHER_OFF") != "OTHER_OFF"


def _measure_text(m: dict[str, Any]) -> str:
    return str((m.get("qMeasure") or {}).get("qDef") or "").strip()


def _is_single_agg(text: str) -> bool:
    try:
        node = parse_expr(text)
    except Untranslatable:
        return False
    return isinstance(node, Call) and node.name in AGGS


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _render_agg(node: Call) -> str:
    """Qlik text of one aggregation node (for matching it against master measures)."""
    parts = []
    if node.set is not None:
        parts.append("{" + node.set + "}")
    if node.distinct:
        parts.append("DISTINCT")
    if node.total:
        parts.append("TOTAL")
    parts.append(", ".join(_render(a) for a in node.args))
    return f"{node.name.title()}({' '.join(parts)})"


def _render(node: Node) -> str:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return sql_literal(node.value)
    if isinstance(node, Fld):
        return node.name if re.fullmatch(r"[A-Za-z_][\w.]*", node.name) else f"[{node.name}]"
    if isinstance(node, Call):
        return _render_agg(node) if node.name in AGGS else f"{node.name}({', '.join(_render(a) for a in node.args)})"
    if isinstance(node, Bin):
        return f"({_render(node.left)} {node.op} {_render(node.right)})"
    return f"{node.op} {_render(node.operand)}"  # type: ignore[union-attr]


def _expr_type(node: Node, default: str) -> str:
    if isinstance(node, Call) and node.name in TRUNC:
        return "date"
    if isinstance(node, Call) and node.name in ("year", "len", "abs", "floor", "ceil"):
        return "number"
    return default or "string"


def _grain(node: Node) -> str:
    return TRUNC.get(node.name, "") if isinstance(node, Call) else ""
