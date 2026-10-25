"""Apache Ossie semantic models: one `<model>.ossie.yaml` document per semantic model.

Spec: https://github.com/apache/ossie, core-spec/spec.md, version 0.2.0.dev0.

Each document is named after its semantic model and that model is its first
dataset. Ossie has no references between documents, so the datasets the model
joins to (through foreign entities, transitively) are copied in: relationships
and cross-dataset metrics then resolve inside the document. A copy carries no
metrics and names the document that defines it.

  dimensions, entity keys, measure expressions -> fields (time dimensions: is_time)
  primary / unique entities -> primary_key / unique_keys
  foreign entity -> relationship to the dataset whose primary entity has that name
  metrics -> aggregate expressions over fields. Dataset-scoped when they read one
    dataset, model-scoped (`dataset.field`) otherwise. Ratio and derived metrics
    are inlined because Ossie metrics can't reference metrics; ratios divide as
    1.0 * a / nullif(b, 0); filters become CASE WHEN inside the aggregate.

Expressions are warehouse SQL, so they're labelled with the warehouse's dialect
from unshackle.toml or the export (SNOWFLAKE, BIGQUERY, DATABRICKS), else ANSI_SQL.

Anything Ossie can't express (cumulative windows, period offsets, conversion
metrics) or that the source left untranslated is never written as a field or
metric. It's kept under `custom_extensions` (vendor UNSHACKLE, key `pending`)
with a `# TODO(unshackle): ...` line; Ossie-specific losses also warn on the bundle.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from unshackle.ir import Bundle, Measure, Metric, SemanticModel, snake
from unshackle.target.layout import Layout
from unshackle.target.semantic import HEADER, _clean, _col, _has_jinja, _mf, _one_line, _reason, _unique, dump_yaml

VERSION = "0.2.0.dev0"
VENDOR = "UNSHACKLE"
DIALECTS = {"snowflake": "SNOWFLAKE", "bigquery": "BIGQUERY", "databricks": "DATABRICKS"}
AGGS = {
    "sum": "SUM({})",
    "count": "COUNT({})",
    "count_distinct": "COUNT(DISTINCT {})",
    "average": "AVG({})",
    "min": "MIN({})",
    "max": "MAX({})",
    "median": "MEDIAN({})",
    "sum_boolean": "SUM(CASE WHEN {} THEN 1 ELSE 0 END)",
}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CONSTANT = re.compile(r"\d+(\.\d+)?")
FILTER_REF = re.compile(r"\{\{\s*(?:Dimension|TimeDimension)\(\s*'(\w+?)__(\w+)'\s*(?:,\s*'(\w+)'\s*)?\)\s*\}\}")


def _norm(expr: str) -> str:
    """Compare column expressions the way Ossie resolves identifiers: unquoted ones ignore case."""
    expr = " ".join(expr.split())
    if IDENT.fullmatch(expr):
        return expr.upper()
    if re.fullmatch(r'"(?:[^"]|"")+"', expr):
        return expr[1:-1].replace('""', '"')
    return expr


def _ext(data: dict[str, Any]) -> list[dict[str, str]]:
    data = _clean(data)
    return [{"vendor_name": VENDOR, "data": json.dumps(data, indent=2, default=str)}] if data else []


def _expression(dialect: str, expr: str) -> dict[str, Any]:
    return {"dialects": [{"dialect": dialect, "expression": expr}]}


def _pending(kind: str, item: Any, reason: str) -> dict[str, Any]:
    entry = {"kind": kind, **_clean(asdict(item))}
    entry["todo"] = item.todo or reason
    return entry


class _Skip(Exception):
    """An item that can't be written as Ossie. `from_source`: the source already marked it TODO."""

    def __init__(self, reason: str, from_source: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.from_source = from_source


@dataclass(eq=False)
class _Dataset:
    model: SemanticModel
    name: str
    dialect: str
    used: set[str]
    fields: list[dict[str, Any]] = field(default_factory=list)
    by_expr: dict[str, str] = field(default_factory=dict)
    dims: dict[str, str] = field(default_factory=dict)
    entities: dict[str, tuple[str, str]] = field(default_factory=dict)
    joins: dict[str, str] = field(default_factory=dict)  # foreign entity -> the source's join type
    measures: dict[str, tuple[Measure, str]] = field(default_factory=dict)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    time_dims: list[str] = field(default_factory=list)
    default_time: str = ""

    def add(self, base: str, expr: str, **extra: Any) -> str:
        name = _unique(base if base not in self.used else f"{base}_value", self.used)
        self.fields.append(_clean({"name": name, "expression": _expression(self.dialect, expr), **extra}))
        self.by_expr.setdefault(_norm(expr), name)
        return name

    def key(self, entity: str) -> str:
        return self.entities[entity][1]

    def skip(self, kind: str, item: Any, reason: str) -> None:
        self.pending.append(_pending(kind, item, reason))
        self.todos.append(f"{kind} {self.name}.{_mf(item.name)}: {_one_line(reason)}")


def _dataset(model: SemanticModel, name: str, dialect: str, reserved: set[str]) -> _Dataset:
    """Fields for one semantic model. `reserved` holds its metric names (fields share their namespace)."""
    ds = _Dataset(model, name, dialect, used=set(reserved))
    for d in model.dimensions:
        if d.todo or _has_jinja(d.expr):
            ds.skip("dimension", d, _reason(d.todo or "expression still has template code", d.source_language, d.source_expression or d.expr))
            continue
        ext = _ext({"time_granularity": d.time_granularity}) if d.type == "time" else []
        ds.dims[snake(d.name)] = ds.add(
            _mf(d.name),
            d.expr or _col(d.name),
            dimension={"is_time": d.type == "time"},
            label=d.label,
            description=d.description,
            custom_extensions=ext,
        )
        if d.type == "time":
            ds.time_dims.append(ds.dims[snake(d.name)])
    # the source's declared default, or the only time column; never a guess among several
    declared = ds.dims.get(snake(model.default_time_dimension), "") if model.default_time_dimension else ""
    ds.default_time = declared if declared in ds.time_dims else (ds.time_dims[0] if len(ds.time_dims) == 1 else "")
    if not ds.default_time and len(ds.time_dims) > 1 and model.measures:
        ds.todos.append(
            f"dataset {name}: the source declares no default time dimension; "
            f"set custom_extensions default_time_dimension to one of {', '.join(ds.time_dims)}"
        )
    for e in model.entities:
        expr = e.expr or _col(e.name)
        key = ds.by_expr.get(_norm(expr)) or ds.add(_mf(expr if IDENT.fullmatch(expr) else e.name), expr)
        ds.entities.setdefault(e.name, (e.type, key))
        if e.join_type:
            ds.joins.setdefault(e.name, e.join_type)
    for m in model.measures:
        if m.todo or _has_jinja(m.expr):
            ds.skip("measure", m, _reason(m.todo or "expression still has template code", m.source_language, m.source_expression or m.expr))
            arg = ""
        else:
            expr = m.expr or _col(m.name)
            arg = expr if CONSTANT.fullmatch(expr) else ds.by_expr.get(_norm(expr)) or ds.add(_mf(m.name), expr, label=m.label, description=m.description)
        ds.measures.setdefault(m.name, (m, arg))
        ds.measures.setdefault(snake(m.name), (m, arg))
    return ds


def _render_dataset(ds: _Dataset, copy: bool = False) -> dict[str, Any]:
    keys = [(t, f) for t, f in ds.entities.values() if t in ("primary", "unique")]
    primary = next((f for t, f in keys if t == "primary"), "")
    unique = [[f] for f in dict.fromkeys(f for _, f in keys) if f != primary]
    if copy:
        ext = _ext({"defined_in": f"{ds.name}.ossie.yaml"})
    else:
        ext = _ext({"source_ref": ds.model.source_ref, "default_time_dimension": ds.default_time, "pending": ds.pending})
    return _clean(
        {
            "name": ds.name,
            "source": ds.model.sql or ds.model.table,
            "primary_key": [primary] if primary else [],
            "unique_keys": unique,
            "description": ds.model.description,
            "fields": ds.fields,
            "metrics": [] if copy else ds.metrics,
            "custom_extensions": ext,
        }
    )


@dataclass
class _Resolver:
    """Turns one metric into an Ossie expression over the fields of the datasets in a document."""

    datasets: dict[str, _Dataset]
    metrics: dict[str, Metric]
    doc: set[str]
    qualify: bool
    used: set[str] = field(default_factory=set)
    stack: list[str] = field(default_factory=list)

    def ref(self, ds: _Dataset, name: str) -> str:
        self.used.add(ds.name)
        return f"{ds.name}.{name}" if self.qualify else name

    def metric(self, m: Metric, filters: tuple[str, ...] = ()) -> str:
        if m.todo:
            raise _Skip(_reason(m.todo, m.source_language, m.source_expression), from_source=True)
        if m.name in self.stack:
            raise _Skip(f"metric {m.name} refers to itself")
        if m.type == "cumulative":
            raise _Skip("cumulative window / grain-to-date has no Ossie equivalent")
        if m.type == "conversion":
            raise _Skip("conversion metrics have no Ossie equivalent")
        if m.offsets:
            raise _Skip("period offsets (offset_window) have no Ossie equivalent")
        if m.filter:
            filters = filters + (self.filter(m.filter),)
        self.stack.append(m.name)
        try:
            if m.type == "simple":
                return self.measure(m.measure, m.semantic_model, filters)
            if m.type == "ratio":
                if not m.numerator or not m.denominator:
                    raise _Skip("ratio needs a numerator and a denominator")
                num = self.input(m.numerator, m.semantic_model, filters)
                den = self.input(m.denominator, m.semantic_model, filters)
                return f"1.0 * {num} / nullif({den}, 0)"
            if not m.expr or not m.metrics:
                raise _Skip("derived metric needs an expr and input metrics")
            subs: dict[str, str] = {}
            for ref in m.metrics:
                subs[ref] = subs[snake(ref)] = self.input(ref, m.semantic_model, filters)
            pattern = re.compile(r"(?<![\w.])(" + "|".join(re.escape(s) for s in sorted(subs, key=len, reverse=True)) + r")(?!\w)")
            return pattern.sub(lambda x: subs[x.group(1)], m.expr)
        finally:
            self.stack.pop()

    def input(self, ref: str, model: str, filters: tuple[str, ...]) -> str:
        """A metric (or bare measure) used inside a ratio or derived metric, parenthesized if compound."""
        m = self.metrics.get(ref) or self.metrics.get(snake(ref))
        if m is None:
            return self.measure(ref, model, filters)
        try:
            expr = self.metric(m, filters)
        except _Skip as skip:
            raise _Skip(f"depends on metric {_mf(m.name)}: {skip.reason}", skip.from_source) from None
        return expr if m.type == "simple" else f"({expr})"

    def measure(self, name: str, model: str, filters: tuple[str, ...]) -> str:
        if not name:
            raise _Skip("no measure")
        home = self.datasets.get(_mf(model)) if model else None
        owners = [home] if home and name in home.measures else [d for d in self.datasets.values() if name in d.measures]
        if len(owners) != 1:
            raise _Skip(f"measure {name} is {'ambiguous' if owners else 'not in any semantic model'}")
        ds = owners[0]
        meas, arg = ds.measures[name]
        if not arg:
            raise _Skip(f"measure {ds.name}.{_mf(name)} is not migrated yet", from_source=True)
        if ds.name not in self.doc:
            raise _Skip(f"needs dataset {ds.name}, which this model has no relationship to")
        x = arg if CONSTANT.fullmatch(arg) else self.ref(ds, arg)
        self.used.add(ds.name)
        if filters:
            x = f"CASE WHEN {filters[0] if len(filters) == 1 else ' AND '.join(f'({f})' for f in filters)} THEN {x} END"
        if meas.agg == "percentile":
            fn = "PERCENTILE_DISC" if meas.agg_params.get("use_discrete_percentile") else "PERCENTILE_CONT"
            return f"{fn}({meas.agg_params.get('percentile', 0.5)}) WITHIN GROUP (ORDER BY {x})"
        if meas.agg not in AGGS:
            raise _Skip(f"aggregation {meas.agg} has no Ossie equivalent")
        return AGGS[meas.agg].format(x)

    def filter(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            entity, dim, grain = m.groups()
            ds = next((d for d in self.datasets.values() if d.entities.get(entity, ("",))[0] == "primary"), None) or self.datasets.get(entity)
            if ds is None or snake(dim) not in ds.dims:
                raise _Skip(f"filter refers to {entity}__{dim}, which isn't a migrated dimension")
            if ds.name not in self.doc:
                raise _Skip(f"filter needs dataset {ds.name}, which this model has no relationship to")
            col = self.ref(ds, ds.dims[snake(dim)])
            return f"DATE_TRUNC('{grain}', {col})" if grain else col

        out = FILTER_REF.sub(sub, text)
        if "{{" in out or "{%" in out:
            raise _Skip(f"filter not translated: {_one_line(text)}")
        return out


def _relationships(datasets: dict[str, _Dataset]) -> list[tuple[_Dataset, _Dataset, str, str, str]]:
    """(from, to, from field, to field, source join type): foreign/unique entity -> the dataset with that primary (or unique) key."""
    out = []
    for ds in datasets.values():
        for ent, (etype, col) in ds.entities.items():
            allowed = {"foreign": ("primary", "unique"), "unique": ("primary",)}.get(etype, ())
            to = next((d for d in datasets.values() if d is not ds and d.entities.get(ent, ("",))[0] in allowed), None)
            if to is not None:
                out.append((ds, to, col, to.key(ent), ds.joins.get(ent, "")))
    return out


def _reachable(home: _Dataset, rels: list[tuple[_Dataset, _Dataset, str, str, str]]) -> list[_Dataset]:
    order = [home]
    for ds in order:
        for frm, to, *_ in rels:
            if frm is ds and to not in order:
                order.append(to)
    return order


def _with_bare_measures(bundle: Bundle) -> list[Metric]:
    """Every measure becomes a metric, so no aggregation is lost: add simple metrics for measures no metric names."""
    names = {m.name for m in bundle.metrics} | {snake(m.name) for m in bundle.metrics}
    measures = {m.measure for m in bundle.metrics if m.measure}
    extra = []
    for sm in bundle.semantic_models:
        for ms in sm.measures:
            if ms.name not in names and snake(ms.name) not in names and ms.name not in measures:
                extra.append(Metric(name=ms.name, measure=ms.name, semantic_model=sm.name, label=ms.label, description=ms.description))
                names |= {ms.name, snake(ms.name)}
    return list(bundle.metrics) + extra


def _home(m: Metric, datasets: dict[str, _Dataset]) -> str:
    if m.semantic_model and _mf(m.semantic_model) in datasets:
        return _mf(m.semantic_model)
    owners = [d.name for d in datasets.values() if m.measure and m.measure in d.measures]
    return owners[0] if len(owners) == 1 else ""


def render(bundle: Bundle, layout: Layout) -> dict[str, str]:
    """Return {relative path: file content}, one Ossie document per semantic model."""
    metrics = _with_bare_measures(bundle)
    by_name: dict[str, Metric] = {}
    for m in metrics:
        by_name.setdefault(m.name, m)
        by_name.setdefault(snake(m.name), m)

    names: dict[str, str] = {}
    for sm in bundle.semantic_models:
        names[sm.name] = _unique(_mf(sm.name), set(names.values()))
    model_names = {_mf(sm.name) for sm in bundle.semantic_models}
    used_metrics: set[str] = set()
    metric_names = [_unique(_mf(m.name), used_metrics) for m in metrics]
    reserved: dict[str, set[str]] = {}
    for m, mname in zip(metrics, metric_names):
        if m.semantic_model and _mf(m.semantic_model) in model_names:
            reserved.setdefault(_mf(m.semantic_model), set()).add(mname)

    datasets: dict[str, _Dataset] = {}
    for sm in bundle.semantic_models:
        dialect = DIALECTS.get(layout.warehouses.get(sm.datasource or bundle.datasource, ""), "ANSI_SQL")
        datasets[names[sm.name]] = _dataset(sm, names[sm.name], dialect, reserved.get(_mf(sm.name), set()))
    rels = _relationships(datasets)

    model_metrics: dict[str, list[dict[str, Any]]] = {}
    model_pending: dict[str, list[dict[str, Any]]] = {}
    model_todos: dict[str, list[str]] = {}
    for m, mname in zip(metrics, metric_names):
        home = _home(m, datasets)
        if not home:
            bundle.warn(f"metric {m.name}", f"not written to Ossie: no semantic model to attach it to{': ' + m.todo if m.todo else ''}")
            continue
        members = {d.name for d in _reachable(datasets[home], rels)}
        try:
            r = _Resolver(datasets, by_name, members, qualify=True)
            expr = r.metric(m)
            scoped = r.used == {home}
            if scoped:
                expr = _Resolver(datasets, by_name, members, qualify=False).metric(m)
        except _Skip as skip:
            model_pending.setdefault(home, []).append(_pending("metric", m, skip.reason))
            model_todos.setdefault(home, []).append(f"metric {mname}: {_one_line(skip.reason)}")
            if not skip.from_source:
                bundle.warn(f"metric {m.name}", f"kept in custom_extensions of {home}.ossie.yaml, not as an Ossie metric: {skip.reason}")
            continue
        body = _clean(
            {
                "name": mname,
                "expression": _expression(datasets[home].dialect, expr),
                "description": m.description,
                "custom_extensions": _ext({"label": (m.label or m.name) if (m.label or m.name) != mname else "", "source_ref": m.source_ref}),
            }
        )
        (datasets[home].metrics if scoped else model_metrics.setdefault(home, [])).append(body)

    header = HEADER.format(tool=bundle.tool)
    out: dict[str, str] = {}
    for name, ds in datasets.items():
        members = _reachable(ds, rels)
        rel_names: set[str] = set()
        warehouse = layout.warehouses.get(ds.model.datasource or bundle.datasource, "")
        doc = _clean(
            {
                "version": VERSION,
                "name": name,
                "description": ds.model.description,
                "datasets": [_render_dataset(d, copy=d is not ds) for d in members],
                "relationships": [
                    _clean(
                        {
                            "name": _unique(f"{frm.name}_to_{to.name}", rel_names),
                            "from": frm.name,
                            "to": to.name,
                            "from_columns": [fc],
                            "to_columns": [tc],
                            "custom_extensions": _ext({"join_type": join}),
                        }
                    )
                    for frm, to, fc, tc, join in rels
                    if frm in members and to in members
                ],
                "metrics": model_metrics.get(name, []),
                "custom_extensions": _ext({"tool": bundle.tool, "warehouse": warehouse, "todo": ds.model.todo, "pending": model_pending.get(name, [])}),
            }
        )
        todos = ([f"semantic_model {name}: {_one_line(ds.model.todo)}"] if ds.model.todo else []) + ds.todos + model_todos.get(name, [])
        text = header + dump_yaml(doc) + "".join(f"# TODO(unshackle): {t}\n" for t in todos)
        out[layout.rel("semantic", f"{name}.ossie.yaml")] = text
    return out
