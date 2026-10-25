"""Neutral intermediate representation shared by every source and every writer.

Sources parse raw exports into a Bundle. Writers turn a Bundle into the unshackle
structure. Resource ids used by permissions and row filters:
  Datasource "<datasource>", Table "<datasource>.<schema>.<table>",
  Column "<datasource>.<schema>.<table>.<column>", Metric "<metric_name>",
  Notebook "<notebook slug>".

Anything a source could not translate deterministically keeps its
original expression in `source_expression` and sets `todo`, so the agent (or a
person) can finish it.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, get_args, get_origin, get_type_hints

Mark = Literal["bar", "line", "area", "point", "arc", "rect", "text", "table", "unknown"]
FieldType = Literal["quantitative", "nominal", "temporal", "ordinal"]


@dataclass
class Column:
    name: str
    type: str = ""
    description: str = ""
    label: str = ""
    hidden: bool = False


@dataclass
class Table:
    datasource: str
    schema: str
    name: str
    description: str = ""
    columns: list[Column] = field(default_factory=list)
    source_ref: str = ""


@dataclass
class Entity:
    name: str
    type: Literal["primary", "foreign", "unique", "natural"] = "primary"
    expr: str = ""
    join_type: str = ""  # foreign entities from a source join: how the source joins (inner, left, ...)
    extensions: dict[str, Any] = field(default_factory=dict)  # foreign entities: more about the source's relationship (custom_extensions)


@dataclass
class Dimension:
    name: str
    type: Literal["categorical", "time"] = "categorical"
    expr: str = ""
    description: str = ""
    label: str = ""
    time_granularity: str = ""
    source_expression: str = ""
    source_language: str = ""
    todo: str = ""


@dataclass
class Measure:
    name: str
    agg: str = "sum"
    expr: str = ""
    description: str = ""
    label: str = ""
    agg_params: dict[str, Any] = field(default_factory=dict)
    source_expression: str = ""
    source_language: str = ""
    todo: str = ""


@dataclass
class SemanticModel:
    name: str
    table: str
    description: str = ""
    entities: list[Entity] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    default_time_dimension: str = ""
    datasource: str = ""
    source_ref: str = ""
    todo: str = ""
    # A query the model reads instead of a table (Ossie writes it as the dataset source; in dbt
    # projects it is written as the SQL model `table` names).
    sql: str = ""


@dataclass
class Metric:
    name: str
    type: Literal["simple", "ratio", "cumulative", "derived", "conversion"] = "simple"
    label: str = ""
    description: str = ""
    measure: str = ""
    numerator: str = ""
    denominator: str = ""
    expr: str = ""
    metrics: list[str] = field(default_factory=list)
    offsets: dict[str, str] = field(default_factory=dict)
    filter: str = ""
    window: str = ""
    grain_to_date: str = ""
    fill_nulls_with: int | None = None  # what the source shows for a group with nothing to aggregate
    semantic_model: str = ""
    source_expression: str = ""
    source_language: str = ""
    source_ref: str = ""
    todo: str = ""


@dataclass
class Encoding:
    field: str
    type: FieldType = "nominal"
    aggregate: str = ""
    title: str = ""
    time_unit: str = ""
    sort: str = ""


@dataclass
class Layer:
    """One series of a multi-series tile: its own mark plus encodings over the tile's shared ones."""

    mark: Mark = "unknown"
    encodings: dict[str, Encoding] = field(default_factory=dict)


@dataclass
class Reference:
    """A reference line from the source chart. Its value is a column of the tile's data, repeated on every row of the
    line's scope (the whole chart, a pane or a cell)."""

    field: str
    channel: Literal["x", "y"] = "y"
    scope: Literal["table", "pane", "cell"] = "table"
    label: str = ""  # text drawn next to the line; "{value}" stands for its value; "" draws none
    band_to: str = ""  # a band: shaded from `field` to this column


@dataclass
class Tile:
    id: str
    title: str = ""
    kind: Literal["chart", "table", "kpi", "text", "unknown"] = "chart"
    mark: Mark = "unknown"
    sql: str = ""
    encodings: dict[str, Encoding] = field(default_factory=dict)
    text: str = ""
    source_type: str = ""
    source_config: dict[str, Any] = field(default_factory=dict)
    todo: str = ""
    # Several series in one tile. `encodings` holds what they share (x, color, row/column facets).
    # layer: overlaid on shared axes; dual_axis: overlaid with independent y; hconcat/vconcat: side by side.
    layers: list[Layer] = field(default_factory=list)
    compose: Literal["layer", "dual_axis", "hconcat", "vconcat"] = "layer"
    references: list[Reference] = field(default_factory=list)


@dataclass
class DashboardFilter:
    name: str
    field: str = ""
    default: Any = None
    kind: Literal["value", "date_range", "date", "number", "unknown"] = "value"
    allowed: str = ""  # the values it takes ("one of 'a', 'b'", "from 0 to 1, step 0.1"), shown in the parameters cell


@dataclass
class Dashboard:
    name: str
    slug: str
    description: str = ""
    tiles: list[Tile] = field(default_factory=list)
    layout: list[list[str]] = field(default_factory=list)
    filters: list[DashboardFilter] = field(default_factory=list)
    datasource: str = ""
    source_ref: str = ""
    url: str = ""
    views: int | None = None


@dataclass
class VerifiedQuery:
    name: str
    sql: str
    description: str = ""
    language: Literal["sql", "python"] = "sql"
    datasource: str = ""
    aliases: list[str] = field(default_factory=list)
    owner: str = ""
    metric: bool = False
    source_ref: str = ""
    todo: str = ""


@dataclass
class KnowledgeItem:
    name: str
    body: str
    description: str = ""
    context: Literal["always", "reference"] = "reference"
    datasource: str = ""
    source_ref: str = ""


@dataclass
class Permission:
    principal: str
    action: Literal["view", "query", "edit"] = "view"
    resource_type: Literal["Datasource", "Table", "Column", "Metric", "Notebook"] = "Notebook"
    resource_id: str = ""
    effect: Literal["permit", "forbid"] = "permit"
    condition: str = ""
    source_expression: str = ""
    source_ref: str = ""
    todo: str = ""


@dataclass
class RowFilter:
    name: str
    table: str
    predicate: str = ""
    groups: list[str] = field(default_factory=list)
    source_expression: str = ""
    source_ref: str = ""
    todo: str = ""


@dataclass
class Warning:
    item: str
    message: str
    severity: Literal["info", "warn", "block"] = "warn"


# Engine names as sources report them (Lightdash warehouse types, Tableau connection
# classes, Power BI M connectors), lower-cased with punctuation dropped -> unshackle.toml type.
WAREHOUSE_ENGINES = {
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "bigqueryjdbc": "bigquery",
    "googlebigquery": "bigquery",
    "databricks": "databricks",
    "databricksmulticloud": "databricks",
    "redshift": "redshift",
    "amazonredshift": "redshift",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",  # Redash data source type
    "duckdb": "duckdb",
}


def warehouse_type(engine: str) -> str:
    """unshackle.toml warehouse type for an engine name, or "" when it isn't one we know."""
    return WAREHOUSE_ENGINES.get(re.sub(r"[^a-z]", "", re.split(r"[.+]", str(engine or ""))[0].lower()), "")


@dataclass
class Bundle:
    tool: str
    datasource: str = "warehouse"
    warehouse: str = ""  # warehouse type the export reports (see detect_warehouse); "" when unknown
    tables: list[Table] = field(default_factory=list)
    semantic_models: list[SemanticModel] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    dashboards: list[Dashboard] = field(default_factory=list)
    queries: list[VerifiedQuery] = field(default_factory=list)
    knowledge: list[KnowledgeItem] = field(default_factory=list)
    permissions: list[Permission] = field(default_factory=list)
    row_filters: list[RowFilter] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)

    def warn(self, item: str, message: str, severity: str = "warn") -> None:
        self.warnings.append(Warning(item=item, message=message, severity=severity))  # type: ignore[arg-type]

    def settle_time_dimensions(self) -> Bundle:
        """A model with exactly one time dimension uses it as the default; with several and none
        declared by the source, the default stays unset (the writers leave a TODO naming them)."""
        for sm in self.semantic_models:
            times = [d.name for d in sm.dimensions if d.type == "time" and not d.todo]
            if not sm.default_time_dimension and len(times) == 1:
                sm.default_time_dimension = times[0]
        return self

    def detect_warehouse(self, engines: Iterable[str]) -> None:
        """Record the warehouse type from engine names in the export, only when they all agree."""
        found = {e: warehouse_type(e) for e in engines if e}
        types = set(found.values())
        if len(types) == 1 and "" not in types:
            self.warehouse = types.pop()
        elif found:
            self.warn(
                "warehouse type",
                f"export reports {', '.join(sorted(found))}; not one known warehouse type, so none was detected",
                "info",
            )

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, sort_keys=False, default=str) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Bundle:
        return _from_dict(cls, json.loads(text))


def _from_dict(tp: Any, data: Any) -> Any:
    if dataclasses.is_dataclass(tp):
        hints = get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name in data:
                kwargs[f.name] = _from_dict(hints[f.name], data[f.name])
        return tp(**kwargs)
    origin = get_origin(tp)
    if origin is list:
        (inner,) = get_args(tp)
        return [_from_dict(inner, x) for x in data]
    if origin is dict:
        _, inner = get_args(tp)
        return {k: _from_dict(inner, v) for k, v in data.items()}
    return data


def slugify(value: str) -> str:
    out = []
    prev_dash = False
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return slug or "untitled"


def snake(value: str) -> str:
    return slugify(value).replace("-", "_")
