"""Helpers shared by the YAML semantic-layer sources (Lightdash, Omni).

Both tools describe views/tables as dimensions + measures in YAML, with SQL
that references other fields as ``${field}``, ``${view.field}`` or
``${TABLE}.column``. Each source normalizes its raw files into NTable objects;
build_semantic() then turns those into catalog Tables, MetricFlow semantic
models and metrics, the same way for both tools.

Naming rules (so filters and joins line up across models):
  - each semantic model's primary entity is named after the model (``orders``)
  - a many-to-one join adds a foreign entity named after the target model
  - metric/measure names are the source names, prefixed with the table name
    only when two tables use the same name (MetricFlow needs global uniqueness)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterator

import json

from unshackle.ir import Bundle, Column, Dimension, Entity, Measure, Metric, Permission, SemanticModel, Table, snake

REF = re.compile(r"\$\{\s*([^}]+?)\s*\}")
IDENT = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?$')
AGG_FN = re.compile(
    r"\b(sum|count|avg|average|min|max|median|percentile_cont|percentile_disc|approx_count_distinct|"
    r"stddev|variance|array_agg|listagg|string_agg)\s*\(",
    re.I,
)
MF_AGGS = {"sum", "count", "count_distinct", "average", "min", "max", "median", "percentile", "sum_boolean"}
TIME_TYPES = {"timestamp", "date", "datetime", "time"}
GRAINS = {"day", "week", "month", "quarter", "year", "hour", "minute", "second"}
JOIN_ON = re.compile(r"^\s*\(?\s*\$\{(\w+)\.(\w+)\}\s*\)?\s*=\s*\(?\s*\$\{(\w+)\.(\w+)\}\s*\)?\s*$")
RATIO = re.compile(
    r"^\s*\(?\s*(?:1(?:\.0*)?\s*\*\s*)?(\w+)\s*\)?\s*/\s*"
    r"(?:nullif\s*\(\s*(\w+)\s*,\s*0\s*\)|\(?\s*(\w+)\s*\)?)\s*$",
    re.I,
)


@dataclass
class NFilter:
    field: str  # "status", "customers.country", "orders.created_at[month]"
    op: str  # equals, notEquals, isNull, notNull, greaterThan, ... (Lightdash operator names)
    values: list[Any] = field(default_factory=list)


@dataclass
class NField:
    name: str
    sql: str = ""  # source SQL with ${} refs; "" means "the column with this name"
    type: str = ""  # dims: string|number|timestamp|date|boolean. measures: MetricFlow agg, "post", or raw type
    label: str = ""
    description: str = ""
    hidden: bool = False
    granularity: str = ""
    filters: list[NFilter] = field(default_factory=list)
    agg_params: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # original definition, kept on TODOs
    todo: str = ""


@dataclass
class NJoin:
    from_col: str
    to_table: str
    to_col: str


@dataclass
class NTable:
    name: str
    table: str
    schema: str = ""
    database: str = ""
    description: str = ""
    label: str = ""
    dims: dict[str, NField] = field(default_factory=dict)
    metrics: dict[str, NField] = field(default_factory=dict)
    primary_key: str = ""
    joins: list[NJoin] = field(default_factory=list)
    default_time: str = ""
    source_ref: str = ""
    todo: str = ""

    @property
    def relation(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.table) if p)

    def is_time(self, dim: str) -> bool:
        d = self.dims.get(dim)
        return bool(d and d.type in TIME_TYPES)


# ---------------------------------------------------------------- generic utils


def walk(obj: Any) -> Iterator[dict[str, Any]]:
    """Every dict inside obj, depth first."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def find_key(obj: Any, keys: tuple[str, ...]) -> Any:
    for d in walk(obj):
        for k in keys:
            if k in d and d[k] not in (None, "", [], {}):
                return d[k]
    return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def grid_rows(items: list[tuple[str, float, float]]) -> list[list[str]]:
    """[(id, x, y)] from a grid layout -> rows of ids, top to bottom, left to right."""
    rows: dict[float, list[str]] = {}
    for tid, x, y in sorted(items, key=lambda i: (i[2], i[1])):
        rows.setdefault(y, []).append(tid)
    return list(rows.values())


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def op_sql(ref: str, op: str, values: list[Any]) -> str | None:
    """SQL predicate for a Lightdash-style operator, or None when there is no clean translation."""
    q = [sql_literal(v) for v in values]
    like = [str(v).replace("'", "''") for v in values]
    if op == "equals" and q:
        return f"{ref} = {q[0]}" if len(q) == 1 else f"{ref} IN ({', '.join(q)})"
    if op == "notEquals" and q:
        return f"{ref} <> {q[0]}" if len(q) == 1 else f"{ref} NOT IN ({', '.join(q)})"
    if op == "isNull":
        return f"{ref} IS NULL"
    if op == "notNull":
        return f"{ref} IS NOT NULL"
    simple = {"greaterThan": ">", "greaterThanOrEqual": ">=", "lessThan": "<", "lessThanOrEqual": "<="}
    if op in simple and len(q) == 1:
        return f"{ref} {simple[op]} {q[0]}"
    if op in ("include", "startsWith", "endsWith", "doesNotInclude") and like:
        pat = {"include": "%{}%", "doesNotInclude": "%{}%", "startsWith": "{}%", "endsWith": "%{}"}[op]
        neg = "NOT " if op == "doesNotInclude" else ""
        return " OR ".join(f"{ref} {neg}LIKE '{pat.format(v)}'" for v in like)
    if op in ("inBetween", "between") and len(q) == 2:
        return f"{ref} BETWEEN {q[0]} AND {q[1]}"
    return None


# ---------------------------------------------------------------- expressions


def _split_ref(ref: str, t: NTable) -> tuple[str, str]:
    parts = ref.split(".")
    if len(parts) == 1:
        return t.name, parts[0]
    if parts[0] in ("TABLE", t.name):
        return t.name, ".".join(parts[1:])
    return parts[0], ".".join(parts[1:])


def resolve_sql(t: NTable, sql: str, depth: int = 0) -> tuple[str, str]:
    """Rewrite source SQL into an expression on the model's own table. Returns (expr, todo)."""
    if not sql:
        return "", ""
    todos: list[str] = []

    def sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if ref == "TABLE":
            return "\x00TABLE"
        table, name = _split_ref(ref, t)
        if table == t.name and name in t.dims and depth < 6:
            d = t.dims[name]
            expr, todo = resolve_sql(t, d.sql, depth + 1) if d.sql else (d.name, "")
            if todo:
                todos.append(todo)
            return expr if IDENT.match(expr) else f"({expr})"
        todos.append(f"references ${{{ref}}}, which is not a dimension of {t.name}")
        return m.group(0)

    out = REF.sub(sub, sql)
    out = re.sub(r"\x00TABLE\s*\.\s*", "", out)
    if "\x00TABLE" in out:
        todos.append("uses ${TABLE} as a relation")
        out = out.replace("\x00TABLE", t.relation or t.name)
    return out.strip(), "; ".join(dict.fromkeys(todos))


def dimension_ref(tables: dict[str, NTable], t: NTable, ref: str) -> tuple[str, str]:
    """MetricFlow Jinja for a filter field. Returns (jinja, todo)."""
    grain = ""
    m = re.match(r"^(.*?)\[(\w+)\]$", ref)
    if m:
        ref, grain = m.group(1), m.group(2).lower()
    table, dim = _split_ref(ref, t)
    target = tables.get(table)
    if target is None or dim not in target.dims:
        return "", f"filter field {ref!r} not found in the model"
    name = f"{snake(table)}__{snake(dim)}"
    if grain or target.is_time(dim):
        g = grain if grain in GRAINS else "day"
        return f"{{{{ TimeDimension('{name}', '{g}') }}}}", ""
    return f"{{{{ Dimension('{name}') }}}}", ""


def filter_expr(tables: dict[str, NTable], t: NTable, filters: list[NFilter]) -> tuple[str, str]:
    parts, todos = [], []
    for f in filters:
        ref, todo = dimension_ref(tables, t, f.field)
        pred = op_sql(ref, f.op, f.values) if ref else None
        if todo or pred is None:
            todos.append(todo or f"filter operator {f.op!r} on {f.field} has no direct SQL translation")
            continue
        parts.append(pred if len(filters) == 1 else f"({pred})")
    return " AND ".join(parts), "; ".join(todos)


def add_join(tables: dict[str, NTable], to_table: str, sql_on: str, relationship: str, bundle: Bundle) -> None:
    """Record a join `... = ${to_table.col}` as a foreign entity on the many side."""
    m = JOIN_ON.match(sql_on or "")
    if not m:
        bundle.warn(to_table or "join", f"join condition is not a simple equality, entity not created: {sql_on}")
        return
    a, ac, b, bc = m.groups()
    if a == to_table:
        a, ac, b, bc = b, bc, a, ac
    rel = (relationship or "many_to_one").replace("-", "_").lower()
    if rel == "one_to_one" and a in tables and b in tables and tables[a].primary_key == ac != tables[b].primary_key:
        tables[b].joins.append(NJoin(bc, a, ac))  # the key is on the joined table: orders.order_id = shippings.order_id
    elif rel in ("many_to_one", "one_to_one") and a in tables:
        tables[a].joins.append(NJoin(ac, b, bc))
    elif rel == "one_to_many" and b in tables:
        tables[b].joins.append(NJoin(bc, a, ac))
    else:
        bundle.warn(to_table, f"{rel} join {a} -> {b} is not modelled as a MetricFlow entity")


def attribute_permission(req: dict[str, Any], rtype: str, rid: str, ref: str, source: str, tool: str) -> Permission:
    """User-attribute gate (all attributes required, any listed value) as a Cedar forbid-unless.

    Attribute values become groups named "attr=value"; the TODO asks for the real mapping.
    """
    clauses = [" || ".join(f'principal in Group::"{attr}={v}"' for v in as_list(vals)) for attr, vals in req.items()]
    cond = " && ".join(f"({c})" for c in clauses if c)
    return Permission(
        principal="*",
        action="query",
        resource_type=rtype,  # type: ignore[arg-type]
        resource_id=rid,
        effect="forbid",
        condition=f"!({cond})" if cond else "",
        source_expression=f"{source}: {json.dumps(req, default=str)}",
        source_ref=ref,
        todo=f"{tool} user-attribute access; map each attribute value to a group (named attr=value here)",
    )


# ---------------------------------------------------------------- bundle building


def _metric_names(tables: list[NTable]) -> dict[tuple[str, str], str]:
    counts = Counter(snake(m) for t in tables for m in t.metrics)
    return {
        (t.name, m): snake(m) if counts[snake(m)] == 1 else snake(f"{t.name}_{m}")
        for t in tables
        for m in t.metrics
    }


def _post_metric(
    t: NTable, key: str, m: NField, names: dict[tuple[str, str], str], by_name: dict[str, NTable], language: str
) -> Metric:
    gname = names[(t.name, key)]
    metric = Metric(
        name=gname,
        label=m.label,
        description=m.description,
        semantic_model=t.name,
        source_ref=f"{t.source_ref}#{key}",
    )
    refs: dict[str, str] = {}
    problems = []
    for ref in REF.findall(m.sql):
        table, name = _split_ref(ref, t)
        if (table, name) in names:
            refs[ref] = names[(table, name)]
        else:
            problems.append(f"${{{ref}}} is not a metric")
    body = REF.sub(lambda mm: refs.get(mm.group(1), mm.group(0)), m.sql).strip()
    if problems or not refs or AGG_FN.search(body) or m.filters:
        if AGG_FN.search(body):
            why = "aggregates raw columns; split into measures and a ratio/derived metric"
        else:
            why = "; ".join(problems)
        if m.filters:
            why = (why + "; " if why else "") + "post-aggregate metric with filters"
        metric.type = "derived"
        metric.expr = body
        metric.metrics = sorted(set(refs.values()))
        metric.todo = f"custom {language} metric: {why or 'rewrite as MetricFlow measures'}"
        metric.source_expression = m.source or m.sql
        metric.source_language = language
        return metric
    ratio = RATIO.match(body)
    num, den = (ratio.group(1), ratio.group(2) or ratio.group(3)) if ratio else ("", "")
    if ratio and {num, den} <= set(refs.values()):
        metric.type, metric.numerator, metric.denominator = "ratio", num, den
    else:
        metric.type, metric.expr, metric.metrics = "derived", body, sorted(set(refs.values()))
    return metric


def build_semantic(bundle: Bundle, tables: list[NTable], language: str) -> None:
    """Add catalog tables, semantic models and metrics for normalized tables."""
    by_name = {t.name: t for t in tables}
    names = _metric_names(tables)
    for t in tables:
        cols = []
        for d in t.dims.values():
            expr, _ = resolve_sql(t, d.sql) if d.sql else (d.name, "")
            ident = IDENT.match(expr)
            if ident:
                cols.append(Column(ident.group(1), d.type, d.description, d.label, d.hidden))
        bundle.tables.append(
            Table(bundle.datasource, t.schema, t.table, t.description, cols, source_ref=t.source_ref)
        )
        model = SemanticModel(
            name=t.name,
            table=t.relation or t.name,
            description=t.description,
            datasource=bundle.datasource,
            source_ref=t.source_ref,
        )
        if t.primary_key:
            pk_expr, _ = resolve_sql(t, t.dims[t.primary_key].sql) if t.primary_key in t.dims and t.dims[t.primary_key].sql else (t.primary_key, "")
            model.entities.append(Entity(t.name, "primary", pk_expr))
        else:
            bundle.warn(t.name, "no primary key found; add a primary entity so joins and Dimension() filters work")
        seen = {t.name}
        for j in t.joins:
            target = by_name.get(j.to_table)
            if target is None or j.to_table in seen:
                continue
            if target.primary_key != j.to_col:
                bundle.warn(t.name, f"join to {j.to_table} is on {j.to_col}, not its primary key; entity not created")
                continue
            expr, _ = resolve_sql(t, t.dims[j.from_col].sql) if j.from_col in t.dims and t.dims[j.from_col].sql else (j.from_col, "")
            model.entities.append(Entity(j.to_table, "foreign", expr))
            seen.add(j.to_table)
        for key, d in t.dims.items():
            expr, todo = resolve_sql(t, d.sql) if d.sql else (d.name, "")
            if expr == snake(key):
                expr = ""
            is_time = d.type in TIME_TYPES
            model.dimensions.append(
                Dimension(
                    name=key,
                    type="time" if is_time else "categorical",
                    expr=expr,
                    description=d.description,
                    label=d.label,
                    time_granularity=(d.granularity or "day") if is_time else "",
                    source_expression=d.sql if todo else "",
                    source_language=language if todo else "",
                    todo=todo,
                )
            )
        times = [k for k, d in t.dims.items() if d.type in TIME_TYPES]
        model.default_time_dimension = t.default_time if t.default_time in times else ""  # declared only, never guessed
        for key, m in t.metrics.items():
            gname = names[(t.name, key)]
            if m.type == "post":
                bundle.metrics.append(_post_metric(t, key, m, names, by_name, language))
                continue
            agg, expr, todo = m.type, "", m.todo
            if agg in MF_AGGS:
                if m.sql:
                    expr, t2 = resolve_sql(t, m.sql)
                    todo = "; ".join(x for x in (todo, t2) if x)
                elif agg == "count":
                    agg, expr = ("count", model.entities[0].expr) if model.entities else ("sum", "1")
                else:
                    expr = key
            else:
                todo = todo or f"{language} aggregate {agg!r} has no MetricFlow equivalent"
                expr, agg = resolve_sql(t, m.sql)[0] if m.sql else key, "sum"
            measure = Measure(gname, agg, expr, m.description, m.label, dict(m.agg_params))
            metric = Metric(
                name=gname,
                label=m.label,
                description=m.description,
                measure=gname,
                semantic_model=t.name,
                source_ref=f"{t.source_ref}#{key}",
            )
            if m.filters:
                metric.filter, ftodo = filter_expr(by_name, t, m.filters)
                todo = "; ".join(x for x in (todo, ftodo) if x)
            if todo:
                for item in (measure, metric):
                    item.todo, item.source_expression, item.source_language = todo, m.source or m.sql, language
            model.measures.append(measure)
            bundle.metrics.append(metric)
        if model.measures and not times:
            bundle.warn(t.name, "measures but no time dimension; set defaults.agg_time_dimension before MetricFlow validate", "info")
        bundle.semantic_models.append(model)
