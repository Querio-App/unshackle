"""Amazon QuickSight source (Amazon Quick Sight; the API, CLI and IAM namespace are `quicksight`).

Raw layout under migration/raw/quicksight/:

  account.json                  {"account", "region", "namespace"}
  lists.json                    ListDashboards / ListAnalyses / ListDataSets / ListDataSources summaries
  bundle/                       the asset bundle: StartAssetBundleExportJob (ExportFormat QUICKSIGHT_JSON,
                                IncludeAllDependencies, IncludePermissions, IncludeTags), the .qs zip unpacked as is:
                                one folder per asset type, one JSON file per asset (camelCase keys)
  api/<kind>/<id>.json          the fallback when the bundle export is refused or fails: DescribeDashboardDefinition,
                                DescribeAnalysisDefinition, DescribeDataSet (DataSet), DescribeDataSource (DataSource),
                                each with Permissions from Describe*Permissions added (PascalCase keys)
  export_job.json               DescribeAssetBundleExportJob's final status, errors and warnings
  identity/users.json           ListUsers
  identity/groups.json          ListGroups, each with Members from ListGroupMemberships
  _errors.json                  calls that failed; parse() turns them into warnings

`unshackle import quicksight <file.qs>` writes the same bundle/ folder from an export someone ran by hand.

Bundle and API files describe the same shapes in two casings. parse() normalizes keys to the service model's member
names (SHAPES lists every member it reads; tests check them against botocore's QuickSight model), so one parser
reads both.

parse() translates deterministically:
  * data sources -> warehouse type (bundle.detect_warehouse) and the SQL dialect for dateDiff and median
  * datasets -> one semantic model per physical table: RelationalTable -> catalog table, CustomSql -> a query to
    materialize; LogicalTableMap joins -> entities, renames, casts, filters, projections and calculated columns
  * calculated fields (dataset and analysis level) -> dimensions (row-level), measures + metrics (sum, avg, count,
    distinct_count, min, max, median, sumIf/countIf/...), ratio metrics (aggregate / aggregate)
  * dashboards and analyses -> notebooks: one tile per visual with SQL generated from the dataset (joins, calculated
    fields inlined, filter groups as WHERE, parameters and filter controls as :binds), Altair encodings from the field wells
  * permissions -> Cedar permits, column-level security -> Cedar forbids, row-level security -> row filters
Everything else (table calculations, PRE_FILTER / POST_AGG level-aware calculations, $visualDimensions, relative
date filters, geospatial and custom visuals) keeps its QuickSight definition with a TODO(unshackle).
"""

from __future__ import annotations

import io
import json
import re
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Union

from unshackle.http import SourceError, env
from unshackle.ir import (
    Bundle,
    Dashboard,
    DashboardFilter,
    Encoding,
    Layer,
    Metric,
    Permission,
    RowFilter,
    Tile,
    VerifiedQuery,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources import _manual
from unshackle.sources._aggexpr import strip_outer
from unshackle.sources._semyaml import NField, NJoin, NTable, build_semantic, grid_rows
from unshackle.sources.base import Inventory, Source

# ------------------------------------------------------------------ service model members parse() reads

SHAPES: dict[str, tuple[str, ...]] = {
    "DataSource": ("Arn", "DataSourceId", "Name", "Type", "DataSourceParameters"),
    "AthenaParameters": ("WorkGroup",),
    "DataSet": ("Arn", "DataSetId", "Name", "PhysicalTableMap", "LogicalTableMap", "ImportMode", "RowLevelPermissionDataSet",
                "RowLevelPermissionTagConfiguration", "ColumnLevelPermissionRules", "DatasetParameters", "UseAs",
                "DataPrepConfiguration", "SemanticModelConfiguration"),
    "PhysicalTable": ("RelationalTable", "CustomSql", "S3Source", "SaaSTable", "FileSource"),
    "RelationalTable": ("DataSourceArn", "Catalog", "Schema", "Name", "InputColumns"),
    "CustomSql": ("DataSourceArn", "Name", "SqlQuery", "Columns"),
    "InputColumn": ("Name", "Type"),
    "LogicalTable": ("Alias", "DataTransforms", "Source"),
    "LogicalTableSource": ("JoinInstruction", "PhysicalTableId", "DataSetArn"),
    "JoinInstruction": ("LeftOperand", "RightOperand", "LeftJoinKeyProperties", "RightJoinKeyProperties", "Type", "OnClause"),
    "JoinKeyProperties": ("UniqueKey",),
    "TransformOperation": ("ProjectOperation", "FilterOperation", "CreateColumnsOperation", "RenameColumnOperation",
                           "CastColumnTypeOperation", "TagColumnOperation", "UntagColumnOperation", "OverrideDatasetParameterOperation"),
    "ProjectOperation": ("ProjectedColumns",),
    "FilterOperation": ("ConditionExpression",),
    "CreateColumnsOperation": ("Columns",),
    "CalculatedColumn": ("ColumnName", "Expression"),
    "RenameColumnOperation": ("ColumnName", "NewColumnName"),
    "CastColumnTypeOperation": ("ColumnName", "NewColumnType", "Format"),
    "RowLevelPermissionDataSet": ("Arn", "PermissionPolicy", "FormatVersion", "Status"),
    "RowLevelPermissionTagConfiguration": ("Status", "TagRules", "TagRuleConfigurations"),
    "RowLevelPermissionTagRule": ("TagKey", "ColumnName", "TagMultiValueDelimiter", "MatchAllValue"),
    "ColumnLevelPermissionRule": ("Principals", "ColumnNames"),
    "SemanticModelConfiguration": ("TableMap",),
    "SemanticTable": ("RowLevelPermissionConfiguration",),
    "RowLevelPermissionConfiguration": ("TagConfiguration", "RowLevelPermissionDataSet"),
    "ResourcePermission": ("Principal", "Actions"),
    "CreateDashboardRequest": ("DashboardId", "Name", "Definition", "Permissions"),
    "CreateAnalysisRequest": ("AnalysisId", "Name", "Definition", "Permissions"),
    "CreateDataSetRequest": ("Permissions",),
    "AnalysisDefinition": ("DataSetIdentifierDeclarations", "Sheets", "CalculatedFields", "ParameterDeclarations", "FilterGroups"),
    "DashboardVersionDefinition": ("DataSetIdentifierDeclarations", "Sheets", "CalculatedFields", "ParameterDeclarations", "FilterGroups"),
    "DataSetIdentifierDeclaration": ("Identifier", "DataSetArn"),
    "CalculatedField": ("DataSetIdentifier", "Name", "Expression"),
    "SheetDefinition": ("SheetId", "Name", "Title", "Visuals", "TextBoxes", "Layouts", "FilterControls", "ParameterControls"),
    "SheetTextBox": ("SheetTextBoxId", "Content"),
    "Layout": ("Configuration",),
    "LayoutConfiguration": ("GridLayout", "FreeFormLayout", "SectionBasedLayout"),
    "GridLayoutConfiguration": ("Elements",),
    "GridLayoutElement": ("ElementId", "ElementType", "ColumnIndex", "RowIndex"),
    "FreeFormLayoutElement": ("ElementId", "ElementType", "XAxisLocation", "YAxisLocation"),
    "Visual": ("TableVisual", "PivotTableVisual", "BarChartVisual", "KPIVisual", "PieChartVisual", "GaugeChartVisual",
               "LineChartVisual", "HeatMapVisual", "TreeMapVisual", "GeospatialMapVisual", "FilledMapVisual", "LayerMapVisual",
               "FunnelChartVisual", "ScatterPlotVisual", "ComboChartVisual", "BoxPlotVisual", "WaterfallVisual",
               "HistogramVisual", "WordCloudVisual", "InsightVisual", "SankeyDiagramVisual", "CustomContentVisual",
               "EmptyVisual", "RadarChartVisual", "PluginVisual"),
    "BarChartVisual": ("VisualId", "Title", "ChartConfiguration"),
    "VisualTitleLabelOptions": ("FormatText",),
    "ShortFormatText": ("PlainText", "RichText"),
    "BarChartConfiguration": ("FieldWells", "Orientation", "BarsArrangement"),
    "BarChartFieldWells": ("BarChartAggregatedFieldWells",),
    "BarChartAggregatedFieldWells": ("Category", "Values", "Colors", "SmallMultiples"),
    "LineChartConfiguration": ("FieldWells", "Type", "ForecastConfigurations"),
    "LineChartFieldWells": ("LineChartAggregatedFieldWells",),
    "LineChartAggregatedFieldWells": ("Category", "Values", "Colors", "SmallMultiples"),
    "PieChartConfiguration": ("FieldWells",),
    "PieChartFieldWells": ("PieChartAggregatedFieldWells",),
    "PieChartAggregatedFieldWells": ("Category", "Values", "SmallMultiples"),
    "KPIConfiguration": ("FieldWells",),
    "KPIFieldWells": ("Values", "TargetValues", "TrendGroups"),
    "GaugeChartConfiguration": ("FieldWells",),
    "GaugeChartFieldWells": ("Values", "TargetValues"),
    "TableConfiguration": ("FieldWells",),
    "TableFieldWells": ("TableAggregatedFieldWells", "TableUnaggregatedFieldWells"),
    "TableAggregatedFieldWells": ("GroupBy", "Values"),
    "TableUnaggregatedFieldWells": ("Values",),
    "UnaggregatedField": ("FieldId", "Column"),
    "PivotTableConfiguration": ("FieldWells",),
    "PivotTableFieldWells": ("PivotTableAggregatedFieldWells",),
    "PivotTableAggregatedFieldWells": ("Rows", "Columns", "Values"),
    "ComboChartConfiguration": ("FieldWells", "SingleAxisOptions"),
    "ComboChartFieldWells": ("ComboChartAggregatedFieldWells",),
    "ComboChartAggregatedFieldWells": ("Category", "BarValues", "Colors", "LineValues"),
    "SingleAxisOptions": ("YAxisOptions",),
    "YAxisOptions": ("YAxis",),
    "ScatterPlotConfiguration": ("FieldWells",),
    "ScatterPlotFieldWells": ("ScatterPlotCategoricallyAggregatedFieldWells", "ScatterPlotUnaggregatedFieldWells"),
    "ScatterPlotCategoricallyAggregatedFieldWells": ("XAxis", "YAxis", "Category", "Size"),
    "ScatterPlotUnaggregatedFieldWells": ("XAxis", "YAxis", "Category", "Size"),
    "HeatMapConfiguration": ("FieldWells",),
    "HeatMapFieldWells": ("HeatMapAggregatedFieldWells",),
    "HeatMapAggregatedFieldWells": ("Rows", "Columns", "Values"),
    "DimensionField": ("NumericalDimensionField", "CategoricalDimensionField", "DateDimensionField"),
    "MeasureField": ("NumericalMeasureField", "CategoricalMeasureField", "DateMeasureField", "CalculatedMeasureField"),
    "DateDimensionField": ("FieldId", "Column", "DateGranularity"),
    "NumericalMeasureField": ("FieldId", "Column", "AggregationFunction"),
    "CategoricalMeasureField": ("FieldId", "Column", "AggregationFunction"),
    "CalculatedMeasureField": ("FieldId", "Expression"),
    "NumericalAggregationFunction": ("SimpleNumericalAggregation", "PercentileAggregation"),
    "ColumnIdentifier": ("DataSetIdentifier", "ColumnName"),
    "ParameterDeclaration": ("StringParameterDeclaration", "DecimalParameterDeclaration", "IntegerParameterDeclaration",
                             "DateTimeParameterDeclaration"),
    "StringParameterDeclaration": ("ParameterValueType", "Name", "DefaultValues"),
    "DateTimeParameterDeclaration": ("Name", "DefaultValues"),
    "StringDefaultValues": ("StaticValues",),
    "FilterGroup": ("FilterGroupId", "Filters", "ScopeConfiguration", "Status", "CrossDataset"),
    "FilterScopeConfiguration": ("SelectedSheets", "AllSheets"),
    "SelectedSheetsFilterScopeConfiguration": ("SheetVisualScopingConfigurations",),
    "SheetVisualScopingConfiguration": ("SheetId", "Scope", "VisualIds"),
    "Filter": ("CategoryFilter", "NumericRangeFilter", "NumericEqualityFilter", "TimeEqualityFilter", "TimeRangeFilter",
               "RelativeDatesFilter", "TopBottomFilter", "NestedFilter", "HierarchyFilter"),
    "CategoryFilter": ("FilterId", "Column", "Configuration"),
    "CategoryFilterConfiguration": ("FilterListConfiguration", "CustomFilterListConfiguration", "CustomFilterConfiguration"),
    "FilterListConfiguration": ("MatchOperator", "CategoryValues", "SelectAllOptions", "NullOption"),
    "CustomFilterConfiguration": ("MatchOperator", "CategoryValue", "ParameterName", "NullOption"),
    "NumericRangeFilter": ("FilterId", "Column", "IncludeMinimum", "IncludeMaximum", "RangeMinimum", "RangeMaximum",
                           "AggregationFunction", "NullOption"),
    "NumericRangeFilterValue": ("StaticValue", "Parameter"),
    "NumericEqualityFilter": ("Column", "Value", "MatchOperator", "AggregationFunction", "ParameterName", "NullOption"),
    "TimeRangeFilter": ("Column", "IncludeMinimum", "IncludeMaximum", "RangeMinimumValue", "RangeMaximumValue", "NullOption",
                        "TimeGranularity"),
    "TimeRangeFilterValue": ("StaticValue", "RollingDate", "Parameter"),
    "RelativeDatesFilter": ("Column", "TimeGranularity", "RelativeDateType", "RelativeDateValue"),
    "FilterControl": ("DateTimePicker", "List", "Dropdown", "TextField", "TextArea", "Slider", "RelativeDateTime"),
    "FilterDropDownControl": ("FilterControlId", "Title", "SourceFilterId"),
    "DashboardSummary": ("Arn", "DashboardId", "Name", "LastPublishedTime"),
    "AnalysisSummary": ("Arn", "AnalysisId", "Name", "Status"),
    "DataSetSummary": ("Arn", "DataSetId", "Name", "ImportMode"),
    "User": ("UserName", "Email", "Role"),
    "Group": ("GroupName", "Arn"),
    "GroupMember": ("MemberName",),
}
# Fields whose values are maps keyed by ids: their keys stay as they are.
MAP_FIELDS = {"PhysicalTableMap", "LogicalTableMap", "FieldFolders", "SourceTableMap", "TransformStepMap",
              "DestinationTableMap", "TableMap"}
KNOWN = {m.lower(): m for members in SHAPES.values() for m in members}
# asset kind -> (lists.json key and URL segment, id member)
KINDS = {"dashboard": ("dashboards", "DashboardId"), "analysis": ("analyses", "AnalysisId"),
         "dataset": ("datasets", "DataSetId"), "datasource": ("datasources", "DataSourceId")}


def normalize(obj: Any, parent: str = "") -> Any:
    """Bundle JSON (camelCase) or API JSON (PascalCase) -> the service model's member names."""
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    if parent == "FieldFolders":
        return obj  # FieldFolder members are lower-case in the service model
    if parent in MAP_FIELDS:
        return {k: normalize(v) for k, v in obj.items()}
    out = {}
    for k, v in obj.items():
        name = KNOWN.get(k.lower()) or (k[:1].upper() + k[1:])
        out[name] = normalize(v, name)
    return out


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]


def _one(d: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """The single member set in a union structure (Visual, Filter, DimensionField...)."""
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            return k, v
    return "", {}


def _arn_id(arn: str) -> str:
    """The resource id at the end of an ARN (`...:dataset/<id>`)."""
    return str(arn or "").rsplit("/", 1)[-1]


def _q(name: str) -> str:
    """An identifier as SQL: bare when it's a plain lower-case name, else double-quoted."""
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else '"' + name.replace('"', '""') + '"'


def _str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _unique(base: str, used: set[str]) -> str:
    out, n = base or "field", 2
    while out in used:
        out, n = f"{base}_{n}", n + 1
    used.add(out)
    return out


# ------------------------------------------------------------------ calculated field expressions


class Untranslatable(ValueError):
    pass


@dataclass
class Ref:
    name: str


@dataclass
class Param:
    name: str


@dataclass
class Word:
    name: str  # PRE_AGG, MONTH, $visualDimensions, ...


@dataclass
class Num:
    value: str


@dataclass
class Str:
    value: str


@dataclass
class Null:
    pass


@dataclass
class Call:
    name: str  # lower-cased
    args: list["Node"] = field(default_factory=list)


@dataclass
class ListOf:
    items: list["Node"] = field(default_factory=list)


@dataclass
class Sorted:
    node: "Node"
    order: str


@dataclass
class BinOp:
    op: str
    left: "Node"
    right: "Node"


@dataclass
class Unary:
    op: str
    operand: "Node"


Node = Union[Ref, Param, Word, Num, Str, Null, Call, ListOf, Sorted, BinOp, Unary]

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<param>\$\{[^}]*\})
  | (?P<field>\{[^}]*\})
  | (?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
  | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<ident>\$?[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|<>|!=|[=<>+\-*/^%(),\[\]])
    """,
    re.VERBOSE,
)
WORDS = {"PRE_AGG", "POST_AGG_FILTER", "PRE_FILTER", "ASC", "DESC", "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR",
         "MINUTE", "SECOND", "MILLISECOND", "CASE_SENSITIVE", "CASE_INSENSITIVE"}


def tokenize(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise Untranslatable(f"cannot read the expression at {text[pos:pos + 20]!r}")
        pos = m.end()
        if m.lastgroup != "ws":
            out.append((m.lastgroup or "", m.group()))
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self) -> tuple[str, str]:
        return self.toks[self.i] if self.i < len(self.toks) else ("eof", "")

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, value: str) -> None:
        if self.take()[1] != value:
            raise Untranslatable(f"expected {value!r}")

    def word(self) -> str:
        kind, value = self.peek()
        return value.upper() if kind == "ident" else value

    def parse(self) -> Node:
        node = self.or_()
        if self.peek()[0] != "eof":
            raise Untranslatable(f"unexpected {self.peek()[1]!r}")
        return node

    def or_(self) -> Node:
        left = self.and_()
        while self.word() == "OR":
            self.take()
            left = BinOp("or", left, self.and_())
        return left

    def and_(self) -> Node:
        left = self.not_()
        while self.word() == "AND":
            self.take()
            left = BinOp("and", left, self.not_())
        return left

    def not_(self) -> Node:
        if self.word() == "NOT":
            self.take()
            return Unary("not", self.not_())
        return self.compare()

    def compare(self) -> Node:
        left = self.additive()
        while self.peek()[1] in ("=", "<>", "!=", "<", ">", "<=", ">="):
            op = self.take()[1]
            left = BinOp("<>" if op == "!=" else op, left, self.additive())
        return left

    def additive(self) -> Node:
        left = self.term()
        while self.peek()[1] in ("+", "-"):
            left = BinOp(self.take()[1], left, self.term())
        return left

    def term(self) -> Node:
        left = self.unary()
        while self.peek()[1] in ("*", "/", "%"):
            left = BinOp(self.take()[1], left, self.unary())
        return left

    def unary(self) -> Node:
        if self.peek()[1] == "-":
            self.take()
            return Unary("-", self.unary())
        return self.power()

    def power(self) -> Node:
        base = self.primary()
        if self.peek()[1] == "^":
            self.take()
            return BinOp("^", base, self.unary())
        return base

    def primary(self) -> Node:
        kind, value = self.take()
        if kind == "num":
            return Num(value)
        if kind == "str":
            return Str(re.sub(r"\\(.)", r"\1", value[1:-1]))
        if kind == "field":
            return Ref(value[1:-1].strip())
        if kind == "param":
            name = value[2:-1].strip()
            if name.lower() == "visualdimensions":  # the export writes $visualDimensions as ${visualDimensions}
                return Word("$visualDimensions")
            return Param(name)
        if kind == "ident":
            if self.peek()[1] == "(":
                return Call(value.lower(), self.args(")"))
            if value.upper() == "NULL":
                return Null()
            if value.upper() in WORDS or value.startswith("$"):
                return Word(value if value.startswith("$") else value.upper())
            return Ref(value)
        if value == "[":
            self.i -= 1
            return ListOf(self.args("]"))
        if value == "(":
            node = self.or_()
            self.expect(")")
            return node
        raise Untranslatable(f"unexpected {value or 'end of expression'!r}")

    def args(self, close: str) -> list[Node]:
        self.take()  # ( or [
        out: list[Node] = []
        while self.peek()[1] != close:
            node = self.or_()
            if self.word() in ("ASC", "DESC"):
                node = Sorted(node, self.take()[1].upper())
            out.append(node)
            if self.peek()[1] == ",":
                self.take()
            elif self.peek()[1] != close:
                raise Untranslatable(f"expected ',' or {close!r}")
        self.take()
        return out


def parse_expression(text: str) -> Node:
    if not str(text or "").strip():
        raise Untranslatable("empty expression")
    return _Parser(text).parse()


# function -> (MetricFlow agg, SQL aggregate)
AGGS = {"sum": ("sum", "sum"), "avg": ("average", "avg"), "average": ("average", "avg"), "min": ("min", "min"),
        "max": ("max", "max"), "count": ("count", "count"), "distinct_count": ("count_distinct", "count"),
        "median": ("median", "median")}
COND_AGGS = {f"{k}if": v for k, v in AGGS.items()}
WINDOW_AGGS = {"sumover": "sum", "avgover": "avg", "minover": "min", "maxover": "max", "countover": "count"}
TABLE_CALCS = {
    "runningsum", "runningavg", "runningcount", "runningmin", "runningmax", "runningmedian", "percentoftotal", "rank",
    "denserank", "percentilerank", "lag", "lead", "difference", "percentdifference", "periodoverperioddifference",
    "periodoverperiodlastvalue", "periodoverperiodpercentdifference", "periodtodatesum", "periodtodateavg",
    "periodtodatecount", "periodtodatemin", "periodtodatemax", "periodtodatesumovertime", "periodtodateavgovertime",
    "periodtodatecountovertime", "periodtodateminovertime", "periodtodatemaxovertime", "windowsum", "windowavg",
    "windowcount", "windowmin", "windowmax", "firstvalue", "lastvalue", "distinctcountover", "stdevover", "stdevpover",
    "varover", "varpover", "medianover", "percentileover", "percentilecontover", "percentilediscover",
}
TRUNC = {"YYYY": "year", "Q": "quarter", "MM": "month", "DD": "day", "HH": "hour", "MI": "minute", "SS": "second"}
EXTRACT = {"YYYY": "year", "Q": "quarter", "MM": "month", "DD": "day", "HH": "hour", "MI": "minute"}
ROW_FUNCS: dict[str, tuple[str, int | None]] = {
    "coalesce": ("coalesce({args})", None), "nullif": ("nullif({0}, {1})", 2), "concat": ("concat({args})", None),
    "toupper": ("upper({0})", 1), "tolower": ("lower({0})", 1), "trim": ("trim({0})", 1), "ltrim": ("ltrim({0})", 1),
    "rtrim": ("rtrim({0})", 1), "strlen": ("length({0})", 1), "substring": ("substr({0}, {1}, {2})", 3),
    "left": ("substr({0}, 1, {1})", 2), "replace": ("replace({0}, {1}, {2})", 3), "tostring": ("cast({0} as varchar)", 1),
    "inttodecimal": ("(1.0 * {0})", 1), "abs": ("abs({0})", 1), "ceil": ("ceil({0})", 1), "floor": ("floor({0})", 1),
    "round": ("round({args})", None), "sqrt": ("sqrt({0})", 1), "exp": ("exp({0})", 1), "ln": ("ln({0})", 1),
    "mod": ("mod({0}, {1})", 2),
}
DATE_DIFF_DAYS = {  # dateDiff(a, b) in days, per SQL dialect
    "trino": "date_diff('day', {0}, {1})",
    "postgres": "(cast({1} as date) - cast({0} as date))",
    "redshift": "datediff(day, {0}, {1})",
    "snowflake": "datediff(day, {0}, {1})",
    "databricks": "datediff({1}, {0})",
    "bigquery": "date_diff(cast({1} as date), cast({0} as date), day)",
}
MEDIAN = {"snowflake", "redshift", "databricks"}


@dataclass
class Sql:
    sql: str
    kind: str = "row"  # row | agg | window


class Translator:
    """QuickSight calculated-field expressions -> SQL.

    `ref(name)` returns the SQL for a field (raising Untranslatable when it can't), `param(name)` the SQL for a
    parameter (a :bind in tile SQL; untranslatable in the semantic layer). `dialect` picks the few functions that
    differ between warehouses (dateDiff, median).
    """

    def __init__(self, ref: Callable[[str], Sql], param: Callable[[str], str], dialect: str = ""):
        self.ref, self.param, self.dialect = ref, param, dialect

    def __call__(self, text: str) -> Sql:
        return self.node(parse_expression(text))

    def node(self, n: Node) -> Sql:
        if isinstance(n, Ref):
            return self.ref(n.name)
        if isinstance(n, Param):
            return Sql(self.param(n.name))
        if isinstance(n, Num):
            return Sql(n.value)
        if isinstance(n, Str):
            return Sql(_str(n.value))
        if isinstance(n, Null):
            return Sql("null")
        if isinstance(n, Word):
            raise Untranslatable(f"{n.name} has no SQL translation" if n.name.startswith("$") else f"unexpected {n.name}")
        if isinstance(n, (ListOf, Sorted)):
            raise Untranslatable("a field list outside a function")
        if isinstance(n, Unary):
            inner = self.node(n.operand)
            return Sql(f"not ({inner.sql})" if n.op == "not" else f"-{inner.sql}", inner.kind)
        if isinstance(n, BinOp):
            left, right = self.node(n.left), self.node(n.right)
            kind = _kind(left, right)
            if n.op in ("and", "or"):
                return Sql(f"({left.sql} {n.op} {right.sql})", kind)
            if n.op == "/":
                return Sql(f"1.0 * {left.sql} / nullif({right.sql}, 0)", kind)
            if n.op == "^":
                return Sql(f"power({left.sql}, {right.sql})", kind)
            if n.op == "%":
                return Sql(f"mod({left.sql}, {right.sql})", kind)
            if n.op in ("+", "-"):
                return Sql(f"({left.sql} {n.op} {right.sql})", kind)
            return Sql(f"{left.sql} {n.op} {right.sql}", kind)
        assert isinstance(n, Call)
        return self.call(n)

    def call(self, n: Call) -> Sql:
        name, args = n.name, n.args
        if name in TABLE_CALCS:
            raise Untranslatable(f"{name}() is a table calculation over the visual's aggregated rows")
        if name == "ifelse":
            if len(args) < 3 or len(args) % 2 == 0:
                raise Untranslatable("ifelse needs condition/value pairs and an else value")
            parts = [self.node(a) for a in args]
            whens = " ".join(f"when {parts[i].sql} then {parts[i + 1].sql}" for i in range(0, len(parts) - 1, 2))
            return Sql(f"case {whens} else {parts[-1].sql} end", _kind(*parts))
        if name in ("isnull", "isnotnull") and len(args) == 1:
            x = self.node(args[0])
            return Sql(f"({x.sql} is {'not ' if name == 'isnotnull' else ''}null)", x.kind)
        if name in ("in", "notin") and len(args) == 2 and isinstance(args[1], ListOf):
            x = self.node(args[0])
            items = [self.node(a) for a in args[1].items]
            return Sql(f"{x.sql} {'not ' if name == 'notin' else ''}in ({', '.join(i.sql for i in items)})", _kind(x, *items))
        if name in ("contains", "startswith", "endswith") and len(args) in (2, 3) and isinstance(args[1], Str):
            x = self.node(args[0])
            pat = {"contains": "%{}%", "startswith": "{}%", "endswith": "%{}"}[name]
            value = re.sub(r"([%_\\])", r"\\\1", args[1].value)
            if len(args) == 3 and isinstance(args[2], Word) and args[2].name == "CASE_INSENSITIVE":
                return Sql(f"lower({x.sql}) like lower({_str(pat.format(value))}) escape '\\'", x.kind)
            return Sql(f"{x.sql} like {_str(pat.format(value))} escape '\\'", x.kind)
        if name == "truncdate" and len(args) == 2 and isinstance(args[0], Str):
            period = args[0].value.upper()
            if period == "WK":
                raise Untranslatable("truncDate('WK') starts weeks on Sunday; date_trunc('week') starts on Monday")
            if period not in TRUNC:
                raise Untranslatable(f"truncDate period {period!r}")
            x = self.node(args[1])
            return Sql(f"date_trunc('{TRUNC[period]}', {x.sql})", x.kind)
        if name == "extract" and len(args) == 2 and isinstance(args[0], Str):
            period = args[0].value.upper()
            if period not in EXTRACT:
                raise Untranslatable(f"extract period {period!r} (WD counts weekdays from Sunday = 1)")
            x = self.node(args[1])
            return Sql(f"extract({EXTRACT[period]} from {x.sql})", x.kind)
        if name == "datediff" and len(args) in (2, 3):
            period = args[2].value.upper() if len(args) == 3 and isinstance(args[2], Str) else "DD" if len(args) == 2 else ""
            if period != "DD":
                raise Untranslatable("dateDiff in units other than days (DD)")
            if self.dialect not in DATE_DIFF_DAYS:
                raise Untranslatable("dateDiff depends on the warehouse dialect, which the data source doesn't name")
            a, b = self.node(args[0]), self.node(args[1])
            return Sql(DATE_DIFF_DAYS[self.dialect].format(a.sql, b.sql), _kind(a, b))
        if name in ROW_FUNCS:
            tmpl, arity = ROW_FUNCS[name]
            if arity is not None and len(args) != arity:
                raise Untranslatable(f"{name}() takes {arity} argument(s)")
            parts = [self.node(a) for a in args]
            return Sql(tmpl.format(*(p.sql for p in parts), args=", ".join(p.sql for p in parts)), _kind(*parts))
        if name in AGGS or name in COND_AGGS:
            return self.aggregate(name, args)
        if name in WINDOW_AGGS:
            if len(args) != 3 or not isinstance(args[2], Word) or args[2].name != "PRE_AGG":
                level = args[2].name if len(args) == 3 and isinstance(args[2], Word) else "POST_AGG_FILTER (the default)"
                raise Untranslatable(f"{name}() at {level} runs over the visual's filtered or aggregated rows")
            x = self.node(args[0])
            if x.kind != "row":
                raise Untranslatable(f"{name}() over an aggregate")
            return Sql(f"{WINDOW_AGGS[name]}({x.sql}) over ({self.partition(args[1])})", "window")
        raise Untranslatable(f"function {name}() has no deterministic SQL translation")

    def partition(self, node: Node) -> str:
        if not isinstance(node, ListOf):
            raise Untranslatable("partition is not a field list")
        if any(isinstance(i, Word) for i in node.items):
            raise Untranslatable("$visualDimensions partitions by whatever the visual groups by")
        cols = [self.node(i) for i in node.items]
        if any(c.kind != "row" for c in cols):
            raise Untranslatable("partition by an aggregate")
        return "partition by " + ", ".join(c.sql for c in cols) if cols else ""

    def aggregate(self, name: str, args: list[Node]) -> Sql:
        cond = name in COND_AGGS
        _, fn = (COND_AGGS if cond else AGGS)[name]
        if not args:
            raise Untranslatable(f"{name}() without a field")
        value = self.node(args[0])
        if value.kind != "row":
            raise Untranslatable(f"{name}() of an aggregate or window (a nested aggregation needs a subquery)")
        rest = args[1:]
        inner = value.sql
        if cond:
            if len(rest) != 1:
                raise Untranslatable(f"{name}() takes a field and a condition")
            c = self.node(rest[0])
            inner, rest = f"case when {c.sql} then {value.sql} end", []
        distinct = name.startswith("distinct_count")
        if name.startswith("median"):
            if self.dialect not in MEDIAN:
                raise Untranslatable("median has no portable SQL (percentile_cont on Postgres, approx_percentile on Athena)")
        sql = f"{fn}({'distinct ' if distinct else ''}{inner})"
        if not rest:
            return Sql(sql, "agg")
        if len(rest) == 1 and isinstance(rest[0], ListOf):  # level-aware aggregation: a FIXED-style partition
            if distinct:
                raise Untranslatable("distinct_count over a partition has no window form on most warehouses")
            if any(isinstance(i, Word) for i in rest[0].items):
                raise Untranslatable("$visualDimensions partitions by whatever the visual groups by")
            return Sql(f"{sql} over ({self.partition(rest[0])})", "window")
        raise Untranslatable(f"{name}() with {len(args)} arguments")


def _kind(*parts: Sql) -> str:
    kinds = {p.kind for p in parts}
    return "window" if "window" in kinds else "agg" if "agg" in kinds else "row"


def _walk(node: Node) -> Iterator[Node]:
    yield node
    kids = (node.args if isinstance(node, Call) else [node.left, node.right] if isinstance(node, BinOp)
            else [node.operand] if isinstance(node, Unary) else node.items if isinstance(node, ListOf)
            else [node.node] if isinstance(node, Sorted) else [])
    for k in kids:
        yield from _walk(k)


def calls(node: Node) -> list[str]:
    return [n.name for n in _walk(node) if isinstance(n, Call)]


def field_refs(node: Node) -> list[str]:
    return [n.name for n in _walk(node) if isinstance(n, Ref)]


# ------------------------------------------------------------------ datasets


DIM_TYPES = {"STRING": "string", "INTEGER": "number", "DECIMAL": "number", "DATETIME": "timestamp", "BIT": "boolean",
             "BOOLEAN": "boolean"}
JOINS = {"INNER": "JOIN", "LEFT": "LEFT JOIN", "RIGHT": "RIGHT JOIN", "OUTER": "FULL OUTER JOIN"}
DIALECTS = {"ATHENA": "trino", "PRESTO": "trino", "TRINO": "trino", "STARBURST": "trino", "POSTGRESQL": "postgres",
            "AURORA_POSTGRESQL": "postgres", "REDSHIFT": "redshift", "SNOWFLAKE": "snowflake", "BIGQUERY": "bigquery",
            "DATABRICKS": "databricks"}
ENGINES = {"AURORA_POSTGRESQL": "postgresql"}  # QuickSight type -> engine name ir.warehouse_type() knows


@dataclass
class Rel:
    """A physical table of a dataset: a warehouse table, custom SQL, or a source unshackle can't query."""

    pid: str
    kind: str  # table | sql | other
    name: str
    schema: str = ""
    database: str = ""
    sql: str = ""
    source_arn: str = ""
    alias: str = ""
    model: str = ""

    @property
    def relation(self) -> str:
        if self.kind == "sql":
            return f"(\n{self.sql.strip().rstrip(';')}\n)"
        return ".".join(_q(p) for p in (self.database, self.schema, self.name) if p)

    @property
    def table_id(self) -> str:
        """The catalog/policy id: schema.table, or the model name for custom SQL."""
        return ".".join(p for p in (self.schema, self.name) if p) if self.kind == "table" else self.model


@dataclass
class Col:
    name: str
    kind: str  # phys | calc
    rel: str = ""
    phys: str = ""
    type: str = ""
    expr: str = ""
    cast: str = ""
    todo: str = ""
    scope: dict[str, "Col"] = field(default_factory=dict)

    @property
    def dim_name(self) -> str:
        """A physical column's name in its table's semantic model (join-collision suffixes dropped)."""
        return self.phys if self.kind == "phys" and re.search(r"\[[^\]]*\]$", self.name) else self.name


@dataclass
class DS:
    id: str
    name: str
    arn: str
    doc: dict[str, Any]
    rels: dict[str, Rel] = field(default_factory=dict)
    cols: dict[str, Col] = field(default_factory=dict)
    from_sql: str = ""
    where: list[str] = field(default_factory=list)
    joins: list[tuple[str, str, str, str, bool, bool]] = field(default_factory=list)  # (rel a, col a, rel b, col b, a unique, b unique)
    todos: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # why tile SQL can't be generated for this dataset
    filters: list[str] = field(default_factory=list)  # QuickSight filter expressions, for the model TODO

    @property
    def multi(self) -> bool:
        return len(self.rels) > 1


class Scope:
    """Resolves field names of one dataset (and one definition's calculated fields) to SQL."""

    def __init__(self, ds: DS, mode: str, dialect: str, calcs: dict[str, str] | None = None,
                 param: Callable[[str], str] | None = None):
        self.ds, self.mode, self.dialect = ds, mode, dialect
        self.calcs = calcs or {}
        self.rels: set[str] = set()
        self.stack: list[str] = []
        self.param = param or self._no_param

    @staticmethod
    def _no_param(name: str) -> str:
        raise Untranslatable(f"uses parameter ${{{name}}}, which only exists on a dashboard")

    def translator(self, scope: dict[str, Col]) -> Translator:
        return Translator(lambda name: self.ref(name, scope), self.param, self.dialect)

    def col(self, c: Col) -> Sql:
        if c.todo:
            raise Untranslatable(c.todo)
        if c.kind == "phys":
            self.rels.add(c.rel)
            rel = self.ds.rels[c.rel]
            sql = f"{rel.alias}.{_q(c.phys)}" if self.mode == "tile" and self.ds.multi else _q(c.phys)
            return Sql(c.cast.format(sql) if c.cast else sql)
        return self.calc(c.name, c.expr, c.scope)

    def calc(self, name: str, expr: str, scope: dict[str, Col]) -> Sql:
        if name in self.stack or len(self.stack) > 20:
            raise Untranslatable(f"calculated field {name!r} refers to itself")
        self.stack.append(name)
        try:
            out = self.translator(scope)(expr)
        finally:
            self.stack.pop()
        return out if re.fullmatch(r"[\w.\"]+", out.sql) else Sql(f"({out.sql})", out.kind)

    def ref(self, name: str, scope: dict[str, Col]) -> Sql:
        if name in scope:
            return self.col(scope[name])
        if name in self.calcs:
            return self.calc(name, self.calcs[name], self.ds.cols)
        raise Untranslatable(f"field {name!r} is not in dataset {self.ds.name!r}")

    def __call__(self, expr: str) -> Sql:
        return self.translator(self.ds.cols)(expr)


def build_dataset(doc: dict[str, Any], dialect: str) -> DS:
    """A DataSet (normalized) -> its physical tables, columns after the logical table transforms, and FROM clause."""
    ds = DS(id=str(doc.get("DataSetId") or _arn_id(doc.get("Arn", ""))), name=str(doc.get("Name") or ""),
            arn=str(doc.get("Arn") or ""), doc=doc)
    physical = doc.get("PhysicalTableMap") or {}
    logical = doc.get("LogicalTableMap") or {}
    if not logical:
        if doc.get("DataPrepConfiguration"):
            ds.todos.append("dataset uses QuickSight's newer data preparation steps (DataPrepConfiguration), which unshackle "
                            "doesn't read yet; rebuild its joins and calculations from the raw JSON")
            ds.blockers.append("the dataset uses data preparation steps unshackle doesn't read")
            return ds
        if len(physical) == 1:
            logical = {"_root": {"Alias": ds.name, "Source": {"PhysicalTableId": next(iter(physical))}}}
    operands = {(lt.get("Source") or {}).get("JoinInstruction", {}).get(k) for lt in logical.values() for k in ("LeftOperand", "RightOperand")}
    roots = [lid for lid in logical if lid not in operands]
    if not roots:
        ds.todos.append("no logical table to start from")
        return ds
    if len(roots) > 1:
        ds.todos.append(f"{len(roots)} unconnected logical tables ({', '.join(roots)}); only {roots[0]} was read")

    def leaves(lid: str) -> list[str]:
        src = (logical.get(lid) or {}).get("Source") or {}
        j = src.get("JoinInstruction")
        if j:
            return leaves(j.get("LeftOperand")) + leaves(j.get("RightOperand"))
        return [src["PhysicalTableId"]] if src.get("PhysicalTableId") else []

    for i, pid in enumerate(dict.fromkeys(leaves(roots[0])), 1):
        kind, pt = _one(physical.get(pid))
        if kind == "RelationalTable":
            database = str(pt.get("Catalog") or "")
            rel = Rel(pid, "table", str(pt.get("Name") or pid), str(pt.get("Schema") or ""),
                      "" if database.lower() == "awsdatacatalog" else database, source_arn=str(pt.get("DataSourceArn") or ""))
        elif kind == "CustomSql":
            rel = Rel(pid, "sql", str(pt.get("Name") or pid), sql=str(pt.get("SqlQuery") or ""), source_arn=str(pt.get("DataSourceArn") or ""))
        else:
            rel = Rel(pid, "other", str(pt.get("Name") or pid))
            ds.todos.append(f"physical table {pid} ({kind or 'unknown source'}) is read by QuickSight itself (a file or SaaS app); "
                            "load it into the warehouse and point the model at that table")
            ds.blockers.append(f"physical table {pid} ({kind or 'unknown source'}) is not in the warehouse")
        rel.alias = f"t{i}"
        ds.rels[pid] = rel
    sc = Scope(ds, "tile", dialect)

    def build(lid: str) -> tuple[str, dict[str, Col], list[str]]:
        lt = logical.get(lid) or {}
        src = lt.get("Source") or {}
        filters: list[str] = []
        if src.get("JoinInstruction"):
            j = src["JoinInstruction"]
            lf, lcols, lfilt = build(j.get("LeftOperand"))
            rf, rcols, rfilt = build(j.get("RightOperand"))
            alias = str((logical.get(j.get("RightOperand")) or {}).get("Alias") or j.get("RightOperand"))
            cols = dict(lcols)
            for k, v in rcols.items():
                cols[k if k not in cols else f"{k}[{alias}]"] = v
            jtype = str(j.get("Type") or "INNER").upper()
            on = []
            try:
                node = parse_expression(str(j.get("OnClause") or ""))
                on.append(sc.translator(cols).node(node).sql)
                for a, b in _equalities(node):
                    ca, cb = cols.get(a), cols.get(b)
                    if ca and cb and ca.kind == cb.kind == "phys" and ca.rel != cb.rel:
                        ds.joins.append((ca.rel, ca.dim_name, cb.rel, cb.dim_name,
                                         bool((j.get("LeftJoinKeyProperties") or {}).get("UniqueKey")),
                                         bool((j.get("RightJoinKeyProperties") or {}).get("UniqueKey"))))
            except Untranslatable as exc:
                ds.todos.append(f"join {lid} ON {j.get('OnClause')!r} not translated ({exc})")
                ds.blockers.append(f"join {lid} ON {j.get('OnClause')!r} not translated")
            if jtype == "LEFT":
                on += rfilt
                filters = lfilt
            elif jtype == "RIGHT":
                on += lfilt
                filters = rfilt
            elif jtype == "INNER":
                filters = lfilt + rfilt
            elif lfilt or rfilt:
                ds.todos.append(f"full outer join {lid} has filters on its inputs; apply them before the join")
                ds.blockers.append(f"full outer join {lid} has filters on its inputs")
            if "\n" in rf:
                rf = "(\n" + rf + "\n)"
            frag = f"{lf}\n{JOINS.get(jtype, 'JOIN')} {rf} ON {' AND '.join(on)}"
        elif src.get("PhysicalTableId") in ds.rels:
            rel = ds.rels[src["PhysicalTableId"]]
            kind, pt = _one(physical.get(rel.pid))
            cols = {}
            for c in pt.get("InputColumns") or pt.get("Columns") or []:
                cols[c["Name"]] = Col(c["Name"], "phys", rel.pid, c["Name"], str(c.get("Type") or ""))
            frag = f"{rel.relation} AS {rel.alias}" if ds.multi or rel.kind == "sql" else rel.relation
        else:
            if src.get("DataSetArn"):
                ds.todos.append(f"logical table {lid} reads another dataset ({_arn_id(src['DataSetArn'])}); "
                                "build the model on that dataset's tables")
            ds.blockers.append(f"logical table {lid} has no warehouse source")
            return "", {}, []
        for op in lt.get("DataTransforms") or []:
            name, o = _one(op)
            if name == "CreateColumnsOperation":
                for c in o.get("Columns") or []:
                    cols[c["ColumnName"]] = Col(c["ColumnName"], "calc", expr=str(c.get("Expression") or ""), scope=dict(cols))
            elif name == "RenameColumnOperation" and o.get("ColumnName") in cols:
                c = cols.pop(o["ColumnName"])
                cols[o["NewColumnName"]] = Col(**{**c.__dict__, "name": o["NewColumnName"]})
            elif name == "CastColumnTypeOperation" and o.get("ColumnName") in cols:
                c = cols[o["ColumnName"]]
                new = str(o.get("NewColumnType") or "")
                if new == "STRING":
                    c.cast = "cast({} as varchar)"
                elif not (c.type == "INTEGER" and new == "DECIMAL") and new != c.type:
                    c.todo = f"QuickSight casts {c.name} from {c.type or '?'} to {new}" + (f" with format {o['Format']!r}" if o.get("Format") else "")
                c.type = new
            elif name == "ProjectOperation":
                keep = [k for k in o.get("ProjectedColumns") or [] if k in cols]
                cols = {k: cols[k] for k in keep}
            elif name == "FilterOperation":
                if o.get("ConditionExpression"):
                    ds.filters.append(o["ConditionExpression"])
                    try:
                        filters.append(sc.translator(cols)(o["ConditionExpression"]).sql)
                    except Untranslatable as exc:
                        ds.todos.append(f"dataset filter {o['ConditionExpression']!r} not translated ({exc})")
                else:
                    ds.todos.append(f"dataset filter {json.dumps(o)} uses a condition structure unshackle doesn't read; add it as SQL")
            elif name == "OverrideDatasetParameterOperation":
                ds.todos.append("dataset parameter override; replace it with a literal or a notebook parameter")
        return frag, cols, filters

    ds.from_sql, ds.cols, ds.where = build(roots[0])
    return ds


def _equalities(node: Node) -> list[tuple[str, str]]:
    if isinstance(node, BinOp) and node.op == "and":
        return _equalities(node.left) + _equalities(node.right)
    if isinstance(node, BinOp) and node.op == "=" and isinstance(node.left, Ref) and isinstance(node.right, Ref):
        return [(node.left.name, node.right.name)]
    return []


def _principal(arn: str) -> tuple[str, str]:
    """A QuickSight principal ARN -> (unshackle principal, todo)."""
    resource = str(arn).split(":", 5)[-1]
    parts = resource.split("/")
    if parts[0] == "group" and len(parts) >= 3:
        return f"group:{'/'.join(parts[2:])}", ""
    if parts[0] == "user" and len(parts) >= 3:
        return f"user:{'/'.join(parts[2:])}", ""
    if parts[0] == "namespace":
        return "*", f"shared with every user in namespace {'/'.join(parts[1:])}"
    return f"user:{arn}", "principal is not a QuickSight user or group ARN; map it to an IdP user or group"


def _cedar_principal(p: str) -> str:
    kind, _, name = p.partition(":")
    return f"principal == User::{json.dumps(name)}" if kind == "user" else f"principal in Group::{json.dumps(name)}"


# ------------------------------------------------------------------ visuals

AGG_SQL = {"SUM": "sum({})", "AVERAGE": "avg({})", "MIN": "min({})", "MAX": "max({})", "COUNT": "count({})",
           "DISTINCT_COUNT": "count(distinct {})", "VAR": "var_samp({})", "VARP": "var_pop({})", "STDEV": "stddev_samp({})",
           "STDEVP": "stddev_pop({})"}
GRAINS = {"YEAR": "year", "QUARTER": "quarter", "MONTH": "month", "DAY": "day", "HOUR": "hour", "MINUTE": "minute",
          "SECOND": "second"}
CHART_VISUALS = {"BarChartVisual", "LineChartVisual", "PieChartVisual", "KPIVisual", "GaugeChartVisual", "TableVisual",
                 "PivotTableVisual", "ComboChartVisual", "ScatterPlotVisual", "HeatMapVisual"}


@dataclass
class VField:
    role: str
    fid: str
    dataset: str
    column: str
    kind: str  # dim | measure
    sub: str  # CategoricalDimensionField, DateDimensionField, NumericalMeasureField, ...
    spec: dict[str, Any]


def _fields(role: str, items: list[Any], kind: str) -> list[VField]:
    out = []
    for item in items or []:
        sub, spec = _one(item)
        if not sub:
            continue
        col = spec.get("Column") or {}
        out.append(VField(role, str(spec.get("FieldId") or ""), str(col.get("DataSetIdentifier") or ""),
                          str(col.get("ColumnName") or ""), kind, sub, spec))
    return out


def _wells(vtype: str, conf: dict[str, Any]) -> tuple[list[VField], list[str]]:
    """Field wells of a visual's ChartConfiguration -> fields by role, plus TODO notes."""
    fw = conf.get("FieldWells") or {}
    todos: list[str] = []
    if vtype in ("KPIVisual", "GaugeChartVisual"):
        if fw.get("TargetValues"):
            todos.append("KPI target value not migrated; add it as a second measure")
        if fw.get("TrendGroups"):
            todos.append("KPI trend (sparkline over a date field) not migrated")
        return _fields("value", fw.get("Values"), "measure"), todos
    if vtype == "TableVisual":
        if fw.get("TableUnaggregatedFieldWells"):
            return [VField("column", f["FieldId"], *_col(f), "dim", "UnaggregatedField", f)
                    for f in fw["TableUnaggregatedFieldWells"].get("Values") or []], todos
        w = fw.get("TableAggregatedFieldWells") or {}
        return _fields("group", w.get("GroupBy"), "dim") + _fields("value", w.get("Values"), "measure"), todos
    if vtype == "PivotTableVisual":
        w = fw.get("PivotTableAggregatedFieldWells") or {}
        return _fields("rows", w.get("Rows"), "dim") + _fields("columns", w.get("Columns"), "dim") + _fields("value", w.get("Values"), "measure"), todos
    if vtype == "ScatterPlotVisual":
        if fw.get("ScatterPlotUnaggregatedFieldWells"):
            w = fw["ScatterPlotUnaggregatedFieldWells"]
            return (_fields("x", w.get("XAxis"), "dim") + _fields("y", w.get("YAxis"), "dim") + _fields("category", w.get("Category"), "dim")
                    + _fields("size", w.get("Size"), "measure")), todos
        w = fw.get("ScatterPlotCategoricallyAggregatedFieldWells") or {}
        return (_fields("category", w.get("Category"), "dim") + _fields("x", w.get("XAxis"), "measure") + _fields("y", w.get("YAxis"), "measure")
                + _fields("size", w.get("Size"), "measure")), todos
    if vtype == "HeatMapVisual":
        w = fw.get("HeatMapAggregatedFieldWells") or {}
        return _fields("rows", w.get("Rows"), "dim") + _fields("columns", w.get("Columns"), "dim") + _fields("value", w.get("Values"), "measure"), todos
    if vtype == "ComboChartVisual":
        w = fw.get("ComboChartAggregatedFieldWells") or {}
        return (_fields("category", w.get("Category"), "dim") + _fields("color", w.get("Colors"), "dim")
                + _fields("bar", w.get("BarValues"), "measure") + _fields("line", w.get("LineValues"), "measure")), todos
    key = {"BarChartVisual": "BarChartAggregatedFieldWells", "LineChartVisual": "LineChartAggregatedFieldWells",
           "PieChartVisual": "PieChartAggregatedFieldWells"}.get(vtype, "")
    w = fw.get(key) or {}
    if w.get("SmallMultiples"):
        todos.append("small multiples: only the first field is faceted")
    return (_fields("category", w.get("Category"), "dim") + _fields("color", w.get("Colors"), "dim")
            + _fields("facet", (w.get("SmallMultiples") or [])[:1], "dim") + _fields("value", w.get("Values"), "measure")), todos


def _col(spec: dict[str, Any]) -> tuple[str, str]:
    col = spec.get("Column") or {}
    return str(col.get("DataSetIdentifier") or ""), str(col.get("ColumnName") or "")


def _title(visual: dict[str, Any]) -> str:
    fmt = ((visual.get("Title") or {}).get("FormatText") or {})
    return (fmt.get("PlainText") or _plain(fmt.get("RichText") or "")).strip()


def _plain(markup: str) -> str:
    """Text box / rich text markup -> plain text. The export indents the markup (a line break and spaces around each
    tag) and keeps a typed space next to a tag as &nbsp;, so line breaks in the markup are layout, not text."""
    import html

    text = re.sub(r"\n\s*", "", str(markup or ""))
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").strip()


def _timestamp(value: Any) -> str:
    """A Timestamp member as 'YYYY-MM-DD HH:MM:SS': the asset bundle writes epoch milliseconds (1735689600000),
    the Describe* API an ISO string ('2025-01-01T00:00:00Z' or '2025-01-01 00:00:00+00:00')."""
    from datetime import datetime, timezone

    if isinstance(value, (int, float)) or re.fullmatch(r"-?\d{9,}", str(value)):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return str(value).replace("T", " ").rstrip("Z").split("+")[0][:19]


def _px(value: Any) -> float:
    m = re.match(r"^\s*(-?[\d.]+)", str(value or "0"))
    return float(m.group(1)) if m else 0.0


# ------------------------------------------------------------------ source


class QuickSightSource(Source):
    name = "quicksight"
    label = "Amazon QuickSight"
    env_vars = ("QUICKSIGHT_ACCOUNT_ID", "QUICKSIGHT_REGION", "QUICKSIGHT_NAMESPACE", "QUICKSIGHT_IDENTITY_REGION", "AWS_PROFILE",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    docs = "https://docs.aws.amazon.com/quicksight/latest/APIReference/"
    manual_files = ("a QuickSight asset bundle: the .qs file from StartAssetBundleExportJob with ExportFormat QUICKSIGHT_JSON "
                    "(or the folder it unzips to)")
    manual_gaps = (
        "usage: QuickSight has no view counts in its API, so nothing is sorted by views",
        "users and groups: no ListUsers / ListGroups output; Cedar policies use the names in the permission ARNs",
        "permissions: only present when the bundle was exported with IncludePermissions",
        "datasets: only those the exported dashboards and analyses depend on, and only with IncludeAllDependencies",
    )
    POLL_SECONDS = 3.0
    EXPORT_TIMEOUT = 900.0

    def __init__(self, client: Any = None, account: str = "", region: str = "", sleep: Callable[[float], None] = time.sleep,
                 download: Callable[[str], bytes] | None = None):
        self._client, self._account, self._region = client, account, region
        self._identity: Any = client
        self._sleep = sleep
        self._download = download

    # ------------------------------------------------------------------ network

    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise SourceError("QuickSight needs boto3: install unshackle[quicksight]") from exc
            session = boto3.Session()
            region = env("QUICKSIGHT_REGION", required=False) or session.region_name
            if not region:
                raise SourceError("missing environment variable QUICKSIGHT_REGION (the region QuickSight runs in)")
            self._region = region
            self._client = session.client("quicksight", region_name=region)
            if not self._account:
                self._account = env("QUICKSIGHT_ACCOUNT_ID", required=False) or session.client("sts", region_name=region).get_caller_identity()["Account"]
        if not self._account:
            self._account = env("QUICKSIGHT_ACCOUNT_ID")
        if not self._region:
            self._region = getattr(getattr(self._client, "meta", None), "region_name", "") or env("QUICKSIGHT_REGION")
        return self._client

    @property
    def namespace(self) -> str:
        return env("QUICKSIGHT_NAMESPACE", required=False, default="default")

    def identity_client(self) -> Any:
        """Users and groups live in the account's identity region, which can differ from QUICKSIGHT_REGION."""
        if self._identity is None:
            region = env("QUICKSIGHT_IDENTITY_REGION", required=False)
            if region and region != self._region:
                import boto3

                self._identity = boto3.Session().client("quicksight", region_name=region)
            else:
                self._identity = self.client()
        return self._identity

    def _call(self, op: str, identity: bool = False, **kw: Any) -> dict[str, Any]:
        client = self.client()
        if identity:
            client = self.identity_client()
        try:
            out = getattr(client, op)(AwsAccountId=self._account, **kw)
        except Exception as exc:  # botocore ClientError and friends; boto3 is optional, so no import here
            raise SourceError(f"{op}: {exc}") from exc
        out.pop("ResponseMetadata", None)
        return json.loads(json.dumps(out, default=str))

    def _pages(self, op: str, key: str, identity: bool = False, **kw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        while True:
            res = self._call(op, identity, **kw, **({"NextToken": token} if token else {}))
            out += res.get(key) or []
            token = res.get("NextToken")
            if not token:
                return out

    def _lists(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "dashboards": self._pages("list_dashboards", "DashboardSummaryList"),
            "analyses": [a for a in self._pages("list_analyses", "AnalysisSummaryList") if a.get("Status") != "DELETED"],
            "datasets": self._pages("list_data_sets", "DataSetSummaries"),
            "datasources": self._pages("list_data_sources", "DataSources"),
        }

    def _url(self, kind: str, rid: str, region: str = "") -> str:
        region = region or self._region
        return f"https://{region}.quicksight.aws.amazon.com/sn/{kind}/{rid}" if region else ""

    def inventory(self) -> Inventory:
        inv = Inventory("quicksight")
        lists = self._lists()
        for kind in ("dashboard", "analysis"):
            url, key = KINDS[kind]
            for r in lists[url]:
                inv.add(kind, f"{kind}:{r[key]}", r.get("Name") or r[key], url=self._url(url, r[key]),
                        last_viewed="", extra={"updated": str(r.get("LastUpdatedTime") or "")[:10]})
        for r in lists["datasets"]:
            inv.add("dataset", f"dataset:{r['DataSetId']}", r.get("Name") or r["DataSetId"], extra={"import_mode": r.get("ImportMode", "")})
        for r in lists["datasources"]:
            inv.add("datasource", f"datasource:{r['DataSourceId']}", r.get("Name") or r["DataSourceId"], extra={"type": r.get("Type", "")})
        inv.notes.append("Ids are dashboard:<id>, analysis:<id>, dataset:<id>, datasource:<id>; pass them to `extract --only` "
                         "(the bundle export adds the datasets and data sources they use). QuickSight's API has no view "
                         "counts, so Views is empty.")
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        errors: list[dict[str, str]] = []
        self.client()
        store.put_json("account.json", {"account": self._account, "region": self._region, "namespace": self.namespace})
        lists = self._lists()
        store.put_json("lists.json", lists)
        selected = {k: [r for r in lists[lk] if only is None or f"{k}:{r[idk]}" in only] for k, (lk, idk) in KINDS.items()}
        arns = [r["Arn"] for rows in selected.values() for r in rows if r.get("Arn")]
        exported = False
        if arns:
            try:
                exported = self._bundle(store, arns, errors)
            except SourceError as exc:
                errors.append({"item": "asset bundle export", "error": str(exc)[:500]})
        if not exported:
            self._describe(store, selected, errors)
        for rel, call in (("identity/users.json", lambda: self._pages("list_users", "UserList", True, Namespace=self.namespace)),
                          ("identity/groups.json", self._groups)):
            try:
                store.put_json(rel, call())
            except SourceError as exc:
                errors.append({"item": rel, "error": str(exc)[:500]})
        store.put_json("_errors.json", errors)

    def _groups(self) -> list[dict[str, Any]]:
        groups = self._pages("list_groups", "GroupList", True, Namespace=self.namespace)
        for g in groups:
            g["Members"] = self._pages("list_group_memberships", "GroupMemberList", True, GroupName=g["GroupName"], Namespace=self.namespace)
        return groups

    def _bundle(self, store: RawStore, arns: list[str], errors: list[dict[str, str]]) -> bool:
        """StartAssetBundleExportJob -> poll -> download the .qs zip into bundle/. False when the job failed."""
        job = f"unshackle-{int(time.time())}"
        self._call("start_asset_bundle_export_job", AssetBundleExportJobId=job, ResourceArns=arns, IncludeAllDependencies=True,
                   ExportFormat="QUICKSIGHT_JSON", IncludePermissions=True, IncludeTags=True)
        waited = 0.0
        while True:
            res = self._call("describe_asset_bundle_export_job", AssetBundleExportJobId=job)
            if res.get("JobStatus") in ("SUCCESSFUL", "FAILED"):
                break
            if waited >= self.EXPORT_TIMEOUT:
                errors.append({"item": "asset bundle export", "error": f"job {job} still {res.get('JobStatus')} after {waited:.0f}s"})
                return False
            self._sleep(self.POLL_SECONDS)
            waited += self.POLL_SECONDS
        store.put_json("export_job.json", {k: res.get(k) for k in ("AssetBundleExportJobId", "JobStatus", "Errors", "Warnings",
                                                                   "CreatedTime", "ResourceArns")})
        if res.get("JobStatus") != "SUCCESSFUL" or not res.get("DownloadUrl"):
            errors.append({"item": "asset bundle export", "error": json.dumps(res.get("Errors") or [])[:500]})
            return False
        data = self._download(res["DownloadUrl"]) if self._download else _get(res["DownloadUrl"])
        for rel, content in unpack_bundle(data, "export.qs").items():
            store.put_bytes(rel, content)
        return True

    def _describe(self, store: RawStore, selected: dict[str, list[dict[str, Any]]], errors: list[dict[str, str]]) -> None:
        """The per-asset fallback: definitions, datasets and data sources, with their permissions and dependencies."""
        datasets: list[str] = [r["DataSetId"] for r in selected["dataset"]]
        sources: list[str] = [r["DataSourceId"] for r in selected["datasource"]]

        def one(rel: str, describe: Callable[[], dict[str, Any]], perms: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
            try:
                doc = describe()
            except SourceError as exc:
                errors.append({"item": rel, "error": str(exc)[:500]})
                return None
            try:
                doc["Permissions"] = perms().get("Permissions") or []
            except SourceError as exc:
                errors.append({"item": f"{rel} permissions", "error": str(exc)[:500]})
            store.put_json(rel, doc)
            return doc

        for kind in ("dashboard", "analysis"):
            key, op, pop = KINDS[kind][1], f"describe_{kind}_definition", f"describe_{kind}_permissions"
            for r in selected[kind]:
                rid = r[key]
                doc = one(f"api/{kind}/{_manual.safe_name(rid)}.json", lambda: self._call(op, **{key: rid}),
                          lambda: self._call(pop, **{key: rid}))
                for decl in ((doc or {}).get("Definition") or {}).get("DataSetIdentifierDeclarations") or []:
                    datasets.append(_arn_id(decl.get("DataSetArn", "")))
        seen: set[str] = set()
        while datasets:
            did = datasets.pop(0)
            if not did or did in seen:
                continue
            seen.add(did)
            doc = one(f"api/dataset/{_manual.safe_name(did)}.json", lambda: self._call("describe_data_set", DataSetId=did)["DataSet"],
                      lambda: self._call("describe_data_set_permissions", DataSetId=did))
            if doc:
                for pt in (doc.get("PhysicalTableMap") or {}).values():
                    for v in pt.values():
                        if isinstance(v, dict) and v.get("DataSourceArn"):
                            sources.append(_arn_id(v["DataSourceArn"]))
                for rls in _rls_datasets(doc):
                    if rls.get("Arn"):
                        datasets.append(_arn_id(rls["Arn"]))
                for lt in (doc.get("LogicalTableMap") or {}).values():
                    if (lt.get("Source") or {}).get("DataSetArn"):
                        datasets.append(_arn_id(lt["Source"]["DataSetArn"]))
        for sid in dict.fromkeys(sources):
            one(f"api/datasource/{_manual.safe_name(sid)}.json", lambda: self._call("describe_data_source", DataSourceId=sid)["DataSource"],
                lambda: self._call("describe_data_source_permissions", DataSourceId=sid))

    # ------------------------------------------------------------------ import

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file():
            if path.suffix.lower() != ".qs":
                raise SourceError(f"{path.name}: QuickSight import accepts {self.manual_files}")
            return unpack_bundle(path.read_bytes(), path.name)
        out: dict[str, bytes] = {}
        loose: dict[str, bytes] = {}
        for rel, p in _manual.files_of(path):
            if p.suffix.lower() == ".qs":
                out.update(unpack_bundle(p.read_bytes(), p.name))
            elif p.suffix.lower() == ".json":
                loose[f"bundle/{rel}"] = p.read_bytes()
        if any(_asset_kind(rel, _load(data)) for rel, data in loose.items()):  # an unzipped bundle: keep all of it
            out.update(loose)
        return out

    # ------------------------------------------------------------------ parse

    def parse(self, store: RawStore) -> Bundle:
        b = Bundle(tool="quicksight")
        self.b, self.store = b, store
        account = store.get_json("account.json") if store.exists("account.json") else {}
        self.region = account.get("region", "")
        self.assets: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in KINDS}
        for rel in store.glob("bundle/**/*.json") + store.glob("api/**/*.json"):
            try:
                raw = json.loads(store.get_text(rel))
            except ValueError as exc:
                b.warn(rel, f"not JSON: {exc}")
                continue
            kind = _asset_kind(rel, raw)
            if not kind:
                continue
            doc = normalize(raw)
            rid = str(doc.get(KINDS[kind][1]) or _arn_id(doc.get("Arn", "")) or PurePosixPath(rel).stem)
            self.assets[kind].setdefault(rid, doc)
            m = None if self.region else re.search(r"arn:aws[\w-]*:quicksight:([\w-]+):", json.dumps(raw))
            if m:
                self.region = m.group(1)
        sources = self.assets["datasource"]
        types = {str(d.get("Type") or "") for d in sources.values()}
        b.detect_warehouse(ENGINES.get(t, t.lower()) for t in sorted(types) if t)
        dialects = {DIALECTS.get(t, "") for t in types}
        self.dialect = dialects.pop() if len(dialects) == 1 else ""
        if len(sources) > 1:
            b.warn("data sources", f"datasets use {len(sources)} QuickSight data sources "
                   f"({', '.join(sorted(str(d.get('Name')) for d in sources.values()))}); all are written to datasource {b.datasource}")
        self.datasets: dict[str, DS] = {}
        for did, doc in sorted(self.assets["dataset"].items()):
            ds = build_dataset(doc, self.dialect)
            ds.id = did
            self.datasets[did] = ds
        self.rules_ids = {_arn_id(r.get("Arn", "")) for d in self.datasets.values() for r in _rls_datasets(d.doc)}
        self.rules_ids |= {d.id for d in self.datasets.values() if str(d.doc.get("UseAs") or "") == "RLS_RULES"}
        self._semantic()
        self._definitions()
        self._permissions()
        self._security()
        for e in _list(store.get_json("_errors.json") if store.exists("_errors.json") else []):
            b.warn(e.get("item", "?"), f"extract failed: {e.get('error', '')}")
        return b.settle_time_dimensions()

    # ------------------------------------------------------------------ semantic layer

    def _definition_calcs(self) -> dict[str, dict[str, str]]:
        """Calculated fields of every dashboard and analysis, by dataset id: {name: expression}."""
        out: dict[str, dict[str, str]] = {}
        for kind in ("dashboard", "analysis"):
            for rid, doc in sorted(self.assets[kind].items()):
                d = doc.get("Definition") or {}
                decl = {x.get("Identifier"): _arn_id(x.get("DataSetArn", "")) for x in d.get("DataSetIdentifierDeclarations") or []}
                for c in d.get("CalculatedFields") or []:
                    did = decl.get(c.get("DataSetIdentifier"))
                    if not did or did not in self.datasets:
                        continue
                    calcs = out.setdefault(did, {})
                    name, expr = str(c.get("Name")), str(c.get("Expression") or "")
                    if name in calcs and calcs[name] != expr:
                        self.b.warn(f"{kind} {doc.get('Name')}", f"calculated field {name!r} differs from another definition's; "
                                    "the first one is in the semantic model")
                        continue
                    calcs.setdefault(name, expr)
        return out

    def _semantic(self) -> None:
        b = self.b
        self.calcs = self._definition_calcs()
        tables: dict[str, NTable] = {}  # relation key -> NTable
        used: set[str] = set()
        rel_model: dict[tuple[str, str], NTable] = {}
        pending_dims: dict[tuple[str, str], tuple[str, str]] = {}  # (model, dim) -> (todo, source)
        pending_metrics: list[tuple[str, str, str, str, str]] = []  # (model, name, label, expression, todo)
        sql_rels: list[tuple[DS, Rel]] = []
        for did, ds in sorted(self.datasets.items()):
            if did in self.rules_ids:
                continue
            for rel in ds.rels.values():
                if rel.kind == "other":
                    continue
                key = f"{rel.source_arn}|{rel.relation}"
                if key not in tables:
                    name = _unique(snake(rel.name), used)
                    t = NTable(name=name, table=name if rel.kind == "sql" else rel.name, schema=rel.schema, database=rel.database,
                               source_ref=f"quicksight:dataset/{did}/{rel.pid}")
                    tables[key] = t
                    if rel.kind == "sql":
                        sql_rels.append((ds, rel))
                t = tables[key]
                rel.model = t.name
                rel_model[(did, rel.pid)] = t
            visible = {(c.rel, c.phys): c for c in ds.cols.values() if c.kind == "phys"}
            for rel in ds.rels.values():
                if rel.kind == "other":
                    continue
                t = rel_model[(did, rel.pid)]
                _, pt = _one((ds.doc.get("PhysicalTableMap") or {}).get(rel.pid))
                for ic in pt.get("InputColumns") or pt.get("Columns") or []:
                    c = visible.get((rel.pid, ic["Name"])) or Col(ic["Name"], "phys", rel.pid, ic["Name"], str(ic.get("Type") or ""))
                    name = c.dim_name
                    if name in t.dims:
                        continue
                    t.dims[name] = NField(name, "" if name == c.phys else _q(c.phys), DIM_TYPES.get(c.type, "string"),
                                          hidden=(rel.pid, ic["Name"]) not in visible)
                    if c.todo or c.cast:
                        pending_dims[(t.name, name)] = (c.todo or f"QuickSight casts this column ({c.cast.format(c.phys)})", c.phys)
            for a_rel, a_col, b_rel, b_col, a_unique, b_unique in ds.joins:
                ta, tb = rel_model.get((did, a_rel)), rel_model.get((did, b_rel))
                if not ta or not tb:
                    continue
                if b_unique and not tb.primary_key:
                    tb.primary_key = b_col
                if a_unique and not ta.primary_key:
                    ta.primary_key = a_col
                if b_unique or tb.primary_key == b_col:
                    ta.joins.append(NJoin(a_col, tb.name, b_col))
                elif a_unique or ta.primary_key == a_col:
                    tb.joins.append(NJoin(b_col, ta.name, a_col))
                else:
                    b.warn(ta.name, f"join {ta.name}.{a_col} = {tb.name}.{b_col} has no unique key on either side; entity not created")
            for expr in ds.filters:
                fsc = Scope(ds, "bare", self.dialect)
                try:
                    fsc(expr)
                except Untranslatable:
                    pass
                for pid in fsc.rels or set(ds.rels):
                    t = rel_model.get((did, pid))
                    if t:
                        t.todo = "; ".join(x for x in (t.todo, f"dataset {ds.name!r} keeps only rows where {expr}; "
                                                       "add that filter to a SQL model or to each metric") if x)
            for msg in ds.todos:
                b.warn(f"dataset {ds.name}", msg)
            # calculated columns of the dataset, then calculated fields of dashboards and analyses on it
            calcs = [(c.name, c.name, c.expr, c.scope) for c in ds.cols.values() if c.kind == "calc"]
            calcs += [(snake(n), n, e, ds.cols) for n, e in sorted((self.calcs.get(did) or {}).items())]
            for key, label, expr, scope in calcs:
                self._place_calc(ds, key, label, expr, scope, rel_model, pending_dims, pending_metrics)
        for t in tables.values():
            if not t.primary_key:
                want = re.sub(r"s$", "", t.table.lower()) + "_id"
                t.primary_key = next((k for k in t.dims if k.lower() in (want, "id")), "")
        build_semantic(b, list(tables.values()), "quicksight")
        virtual = {r.model for _, r in sql_rels}  # custom SQL has no table until it's materialized
        b.tables = [t for t in b.tables if t.schema or t.name not in virtual]
        models = {m.name: m for m in b.semantic_models}
        for (model, dim), (todo, source) in pending_dims.items():
            for d in models[model].dimensions:
                if d.name == dim:
                    d.todo, d.source_expression, d.source_language = todo, source, "quicksight"
        for t in tables.values():
            if t.todo:
                models[t.name].todo = t.todo
        taken = {m.name for m in b.metrics}
        for model, name, label, expr, todo in pending_metrics:
            mname = _unique(snake(name) if snake(name) not in taken else snake(f"{model}_{name}"), taken)
            b.metrics.append(Metric(name=mname, label=label, semantic_model=model, source_expression=expr, source_language="quicksight",
                                    source_ref=f"{models[model].source_ref}#{name}", todo=todo))
        for ds, rel in sql_rels:
            b.queries.append(VerifiedQuery(rel.name, rel.sql.strip(), f"Custom SQL of QuickSight dataset {ds.name!r}",
                                           datasource=b.datasource, source_ref=f"quicksight:dataset/{ds.id}/{rel.pid}",
                                           todo=f"QuickSight custom SQL: materialize as a SQL model (queries/models/{rel.model}.sql; "
                                                f"dbt: models/unshackle/{rel.model}.sql) so semantic model {rel.model} has a table"))

    def _place_calc(self, ds: DS, key: str, label: str, expr: str, scope: dict[str, Col], rel_model: dict[tuple[str, str], NTable],
                    pending_dims: dict, pending_metrics: list) -> None:
        sc = Scope(ds, "bare", self.dialect, self.calcs.get(ds.id))
        root = next((r for r in ds.rels.values() if (ds.id, r.pid) in rel_model), None)
        if root is None:
            return
        try:
            node = parse_expression(expr)
        except Untranslatable as exc:
            pending_metrics.append((rel_model[(ds.id, root.pid)].name, key, label, expr, f"QuickSight expression not read ({exc})"))
            return
        try:
            out = sc.translator(scope).node(node)
            todo = ""
            is_agg = out.kind == "agg"
        except Untranslatable as exc:
            out, todo = Sql(""), f"QuickSight calculated field not translated: {exc} (skills/translate-metrics)"
            is_agg = any(c in AGGS or c in COND_AGGS or c in TABLE_CALCS or c in WINDOW_AGGS for c in calls(node)) or any(
                _is_agg_calc(self.calcs.get(ds.id) or {}, r) for r in field_refs(node))
        rels = sc.rels or {root.pid}
        home = rel_model.get((ds.id, next(iter(rels)))) if len(rels) == 1 else rel_model[(ds.id, root.pid)]
        if home is None:
            return
        if len(rels) > 1 and not todo:
            names = ", ".join(sorted(ds.rels[r].name for r in rels))
            todo = f"uses columns from several tables ({names}); put it in a SQL model that joins them: {out.sql}"
        if not is_agg and out.kind == "window" and not todo:
            todo = f"level-aware calculation, a window function per row: add it as a column of a SQL model: {out.sql}"
        if not is_agg:
            if key in home.dims:
                key = f"{key}_calc"
            home.dims[key] = NField(key, "" if todo else out.sql, _calc_type(node), label if label != key else "")
            if todo:
                pending_dims[(home.name, key)] = (todo, expr)
            return
        if todo:
            pending_metrics.append((home.name, key, label, expr, todo))
            return
        measure = _measure(node, sc, scope)
        if measure:
            home.metrics[key] = NField(key, measure[1], measure[0], label if label != key else "", source=expr)
            return
        ratio = node if isinstance(node, BinOp) and node.op == "/" else None
        sides = []
        for side, part in (("numerator", ratio.left if ratio else None), ("denominator", ratio.right if ratio else None)):
            if isinstance(part, Ref) and snake(part.name) in home.metrics and home.metrics[snake(part.name)].type != "post":
                sides.append(snake(part.name))
                continue
            m = _measure(part, sc, scope) if part is not None else None
            if not m:
                break
            helper = f"{key}_{side}"
            home.metrics[helper] = NField(helper, m[1], m[0], f"{label} ({side})", f"{side.title()} of {label}")
            sides.append(helper)
        if len(sides) == 2 and len(sc.rels or {root.pid}) <= 1:
            home.metrics[key] = NField(key, f"${{{sides[0]}}} / ${{{sides[1]}}}", "post", label if label != key else "", source=expr)
            return
        pending_metrics.append((home.name, key, label, expr, "QuickSight aggregate is not a single aggregation or a ratio of two; "
                                                             f"rebuild it as measures plus a derived metric. SQL: {out.sql}"))

    # ------------------------------------------------------------------ dashboards and analyses

    def _definitions(self) -> None:
        for kind in ("dashboard", "analysis"):
            for rid, doc in sorted(self.assets[kind].items(), key=lambda i: str(i[1].get("Name") or i[0])):
                self._notebook(kind, rid, doc, KINDS[kind][0])

    def _notebook(self, kind: str, rid: str, doc: dict[str, Any], url: str) -> None:
        b = self.b
        d = doc.get("Definition") or {}
        name = str(doc.get("Name") or rid)
        if not d.get("Sheets"):
            b.warn(f"{kind} {name}", "no sheets in the exported definition; nothing to migrate (re-export it, or check the "
                   "raw JSON for a shape unshackle doesn't read)")
            return
        dash = Dashboard(name=name, slug=slugify(name), datasource=b.datasource, source_ref=f"quicksight:{kind}/{rid}",
                         url=self._url(url, rid, self.region))
        if kind == "analysis":
            dash.description = "QuickSight analysis (the authoring view; dashboards are published from analyses)."
        decl = {x.get("Identifier"): _arn_id(x.get("DataSetArn", "")) for x in d.get("DataSetIdentifierDeclarations") or []}
        calcs: dict[str, dict[str, str]] = {}
        for c in d.get("CalculatedFields") or []:
            calcs.setdefault(decl.get(c.get("DataSetIdentifier"), ""), {})[str(c.get("Name"))] = str(c.get("Expression") or "")
        params: dict[str, str] = {}
        multi: set[str] = set()
        for p in d.get("ParameterDeclarations") or []:
            ptype, spec = _one(p)
            pname = str(spec.get("Name") or "")
            bind = _bind(pname)
            params[pname] = bind
            static = ((spec.get("DefaultValues") or {}).get("StaticValues")) or []
            if spec.get("ParameterValueType") == "MULTI_VALUED":
                multi.add(pname)
            fkind = {"DateTimeParameterDeclaration": "date", "IntegerParameterDeclaration": "number",
                     "DecimalParameterDeclaration": "number"}.get(ptype, "value")
            if fkind == "date":
                static = [_timestamp(v) for v in static]
            default = static[0] if len(static) == 1 else (static or None)
            dash.filters.append(DashboardFilter(bind, f"parameter {pname}", default, fkind))  # type: ignore[arg-type]
        controls: dict[str, str] = {}  # filter id -> control title
        for sheet in d.get("Sheets") or []:
            for ctl in sheet.get("FilterControls") or []:
                _, spec = _one(ctl)
                if spec.get("SourceFilterId"):
                    controls[spec["SourceFilterId"]] = str(spec.get("Title") or spec["SourceFilterId"])
        ctx = _Ctx(decl, calcs, params, multi, controls, d.get("FilterGroups") or [], dash)
        used: set[str] = set()
        sheets = d.get("Sheets") or []
        for sheet in sheets:
            if len(sheets) > 1:
                tid = _unique(f"sheet_{snake(str(sheet.get('Name') or sheet.get('SheetId')))}", used)
                dash.tiles.append(Tile(tid, kind="text", text=f"## {sheet.get('Title') or sheet.get('Name') or ''}".rstrip(), source_type="sheet"))
                dash.layout.append([tid])
            by_id: dict[str, Tile] = {}
            for v in sheet.get("Visuals") or []:
                vtype, spec = _one(v)
                if vtype == "EmptyVisual" or not spec:
                    continue
                tile = self._tile(vtype, spec, sheet, ctx, used)
                by_id[str(spec.get("VisualId"))] = tile
            for tb in sheet.get("TextBoxes") or []:
                text = _plain(tb.get("Content") or "")
                if text:
                    by_id[str(tb.get("SheetTextBoxId"))] = Tile(_unique(f"text_{snake(str(tb.get('SheetTextBoxId')))}", used),
                                                                kind="text", text=text, source_type="text_box")
            rows = _sheet_rows(sheet, list(by_id))
            for row in rows:
                ids = [by_id[i].id for i in row if i in by_id]
                if ids:
                    dash.layout.append(ids)
            dash.tiles += list(by_id.values())
        bound = {bind for t in dash.tiles for bind in re.findall(r"(?<!:):(\w+)", t.sql or "")}
        dash.filters = [f for f in dash.filters if f.name in bound or f.name not in ctx.control_binds]
        b.dashboards.append(dash)

    def _tile(self, vtype: str, spec: dict[str, Any], sheet: dict[str, Any], ctx: "_Ctx", used: set[str]) -> Tile:
        vid = str(spec.get("VisualId") or "")
        title = _title(spec)
        tile = Tile(_unique(snake(title) or snake(vid) or "visual", used), title=title, source_type=vtype,
                    source_config={"visual_id": vid, "sheet": sheet.get("SheetId")})
        if vtype not in CHART_VISUALS:
            tile.kind = "unknown"
            tile.todo = f"QuickSight {vtype} has no Altair mapping; rebuild it (visual {vid} in {ctx.dash.source_ref}'s raw JSON)"
            return tile
        conf = spec.get("ChartConfiguration") or {}
        fields, todos = _wells(vtype, conf)
        datasets = {ctx.decl.get(f.dataset, f.dataset) for f in fields if f.sub != "CalculatedMeasureField" or f.dataset}
        datasets.discard("")
        if not fields:
            tile.kind, tile.todo = "unknown", "visual has no fields"
            return tile
        if len(datasets) != 1 or next(iter(datasets)) not in self.datasets:
            tile.kind = "unknown"
            tile.todo = f"fields come from {len(datasets)} datasets ({', '.join(sorted(datasets))}) or a dataset not in the export"
            return tile
        ds = self.datasets[next(iter(datasets))]
        sc = Scope(ds, "tile", self.dialect, ctx.calcs.get(ds.id), lambda name: f":{ctx.param(name)}")
        select: list[tuple[str, str, VField, str]] = []  # (alias, sql, field, encoding type)
        aliases: set[str] = set()
        problems: list[str] = [f"no SQL: {x} (dataset {ds.name!r})" for x in ds.blockers]
        for f in fields:
            try:
                sql, etype = self._field_sql(f, sc, problems)
            except Untranslatable as exc:
                problems.append(f"{f.column or f.fid}: {exc}")
                continue
            select.append((_unique(snake(f.column or f.fid) or "value", aliases), strip_outer(sql), f, etype))
        preds = self._filters(ds, sc, sheet, vid, ctx, todos)
        dims = [s for s in select if s[2].kind == "dim"]
        measures = [s for s in select if s[2].kind == "measure"]
        if problems:
            todos += problems
            tile.sql = ""
        else:
            lines = ["SELECT", "  " + ",\n  ".join(f"{sql} AS {alias}" for alias, sql, _, _ in select), f"FROM {ds.from_sql}"]
            where = ds.where + preds
            if where:
                lines.append("WHERE " + "\n  AND ".join(where))
            aggregated = vtype != "TableVisual" or not (conf.get("FieldWells") or {}).get("TableUnaggregatedFieldWells")
            unaggregated_scatter = vtype == "ScatterPlotVisual" and (conf.get("FieldWells") or {}).get("ScatterPlotUnaggregatedFieldWells")
            if dims and measures:
                lines.append("GROUP BY " + ", ".join(str(i) for i in range(1, len(dims) + 1)))
            elif dims and aggregated and not unaggregated_scatter:
                lines[0] = "SELECT DISTINCT"
            if dims and aggregated:
                lines.append("ORDER BY " + ", ".join(str(i) for i in range(1, len(dims) + 1)))
            tile.sql = "\n".join(lines)
        enc = {alias: Encoding(alias, etype, title=f.column) for alias, _, f, etype in select}  # type: ignore[arg-type]
        by_role: dict[str, list[str]] = {}
        for alias, _, f, _ in select:
            by_role.setdefault(f.role, []).append(alias)
        todos += self._encode(tile, vtype, conf, enc, by_role)
        tile.todo = "; ".join(dict.fromkeys(t for t in todos if t))
        return tile

    def _field_sql(self, f: VField, sc: Scope, problems: list[str]) -> tuple[str, str]:
        if f.sub == "CalculatedMeasureField":
            out = sc(str(f.spec.get("Expression") or ""))
            if out.kind != "agg":
                raise Untranslatable("calculated measure is not an aggregation")
            return out.sql, "quantitative"
        col = sc.ds.cols.get(f.column)
        out = sc.ref(f.column, sc.ds.cols)
        if out.kind == "window":
            raise Untranslatable("window calculation in a visual; compute it in a subquery")
        if f.kind == "dim":
            if out.kind != "row":
                raise Untranslatable("aggregate used as a dimension")
            if f.sub == "DateDimensionField":
                grain = str(f.spec.get("DateGranularity") or "")
                if grain == "WEEK":
                    raise Untranslatable("week granularity: QuickSight weeks start on Sunday, date_trunc('week') on Monday")
                if grain and grain not in GRAINS:
                    raise Untranslatable(f"date granularity {grain}")
                return (f"date_trunc('{GRAINS[grain]}', {out.sql})" if grain else out.sql), "temporal"
            numeric = f.sub == "NumericalDimensionField"
            temporal = col is not None and col.type == "DATETIME"
            return out.sql, "temporal" if temporal else "ordinal" if numeric else "nominal"
        if out.kind == "agg":
            return out.sql, "quantitative"
        agg = f.spec.get("AggregationFunction")
        if isinstance(agg, dict):
            if agg.get("PercentileAggregation"):
                raise Untranslatable("percentile aggregation")
            agg = agg.get("SimpleNumericalAggregation") or "SUM"
        agg = str(agg or ("SUM" if f.sub == "NumericalMeasureField" else "COUNT"))
        if agg == "MEDIAN":
            if self.dialect not in MEDIAN:
                raise Untranslatable("median has no portable SQL (percentile_cont on Postgres, approx_percentile on Athena)")
            return f"median({out.sql})", "quantitative"
        if agg not in AGG_SQL:
            raise Untranslatable(f"aggregation {agg}")
        return AGG_SQL[agg].format(out.sql), "quantitative"

    def _filters(self, ds: DS, sc: Scope, sheet: dict[str, Any], vid: str, ctx: "_Ctx", problems: list[str]) -> list[str]:
        preds: list[str] = []
        for fg in ctx.filter_groups:
            if str(fg.get("Status") or "ENABLED") == "DISABLED" or not _in_scope(fg, sheet.get("SheetId"), vid):
                continue
            for flt in fg.get("Filters") or []:
                ftype, spec = _one(flt)
                did, cname = _col(spec)
                did = ctx.decl.get(did, did)
                if did != ds.id and not (fg.get("CrossDataset") == "ALL_DATASETS" and (cname in ds.cols or cname in (ctx.calcs.get(ds.id) or {}))):
                    continue
                label = f"filter {spec.get('FilterId')}"
                try:
                    col = sc.ref(cname, ds.cols)
                    if col.kind != "row":
                        raise Untranslatable("filters an aggregate (HAVING)")
                    pred = _filter_sql(ftype, spec, col.sql, ctx, f"{snake(ds.name)}.{cname}")
                except Untranslatable as exc:
                    problems.append(f"{label} on {cname} not translated ({exc}); add it to the SQL")
                    continue
                if pred:
                    preds.append(pred)
        return preds

    def _encode(self, tile: Tile, vtype: str, conf: dict[str, Any], enc: dict[str, Encoding], by_role: dict[str, list[str]]) -> list[str]:
        todos: list[str] = []
        first = lambda role: enc[by_role[role][0]] if by_role.get(role) else None  # noqa: E731
        values = [enc[a] for a in by_role.get("value", [])]
        put = lambda ch, e: tile.encodings.__setitem__(ch, e) if e else None  # noqa: E731
        if vtype in ("KPIVisual", "GaugeChartVisual"):
            tile.kind, tile.mark = "kpi", "text"
            put("text", values[0] if values else None)
            if vtype == "GaugeChartVisual":
                todos.append("gauge shown as a KPI value")
        elif vtype in ("TableVisual", "PivotTableVisual"):
            tile.kind, tile.mark = "table", "table"
            if vtype == "PivotTableVisual" and by_role.get("columns"):
                todos.append("pivot table: pivot the result with pandas (pivot_table) to match QuickSight")
        elif vtype == "PieChartVisual":
            tile.mark = "arc"
            put("theta", values[0] if values else None)
            put("color", first("category"))
        elif vtype == "HeatMapVisual":
            tile.mark = "rect"
            put("x", first("columns"))
            put("y", first("rows"))
            put("color", values[0] if values else None)
        elif vtype == "ScatterPlotVisual":
            tile.mark = "point"
            put("x", first("x"))
            put("y", first("y"))
            put("size", first("size"))
            put("color" if not (conf.get("FieldWells") or {}).get("ScatterPlotCategoricallyAggregatedFieldWells") else "detail", first("category"))
        elif vtype == "ComboChartVisual":
            tile.mark = "bar"
            put("x", first("category"))
            if by_role.get("color"):
                todos.append("combo chart bars are coloured by a field; only the series are drawn")
            tile.layers = [Layer("bar", {"y": enc[a]}) for a in by_role.get("bar", [])] + \
                          [Layer("line", {"y": enc[a]}) for a in by_role.get("line", [])]
            single = ((conf.get("SingleAxisOptions") or {}).get("YAxisOptions") or {}).get("YAxis") == "PRIMARY_Y_AXIS"
            tile.compose = "layer" if single or not by_role.get("line") or not by_role.get("bar") else "dual_axis"
        else:
            horizontal = vtype == "BarChartVisual" and conf.get("Orientation") == "HORIZONTAL"
            cat_ch, val_ch = ("y", "x") if horizontal else ("x", "y")
            if vtype == "LineChartVisual":
                tile.mark = "area" if str(conf.get("Type") or "LINE") in ("AREA", "STACKED_AREA") else "line"
                if conf.get("ForecastConfigurations"):
                    todos.append("forecast not migrated")
            else:
                tile.mark = "bar"
            put(cat_ch, first("category"))
            put("color", first("color"))
            put("column", first("facet"))
            arrangement = str(conf.get("BarsArrangement") or "CLUSTERED")
            if vtype == "BarChartVisual" and first("color") and arrangement == "CLUSTERED" and not horizontal:
                put("xOffset", first("color"))
            if arrangement == "STACKED_PERCENT":
                todos.append("100% stacked in QuickSight; normalise in SQL or with stack='normalize'")
            if len(values) > 1 and (vtype == "BarChartVisual" or first("color")):
                todos.append(f"{len(values)} value fields; only the first is encoded: unpivot in SQL and colour by measure")
                put(val_ch, values[0])
            elif len(values) > 1:
                tile.layers = [Layer(tile.mark, {val_ch: e}) for e in values]
            else:
                put(val_ch, values[0] if values else None)
        return todos

    # ------------------------------------------------------------------ permissions and security

    def _permissions(self) -> None:
        b = self.b
        slugs = {(d.source_ref.split(":", 1)[1]): d.slug for d in b.dashboards}
        for kind, edit in (("dashboard", "quicksight:UpdateDashboard"), ("analysis", "quicksight:UpdateAnalysis")):
            for rid, doc in self.assets[kind].items():
                slug = slugs.get(f"{kind}/{rid}")
                for perm in doc.get("Permissions") or []:
                    principal, todo = _principal(perm.get("Principal", ""))
                    actions = [str(a) for a in perm.get("Actions") or []]
                    src = f"{kind} permission: {', '.join(sorted(a.split(':')[-1] for a in actions))}"
                    ref = f"quicksight:{kind}/{rid}"
                    if slug:
                        b.permissions.append(Permission(principal, "view", "Notebook", slug, source_expression=src, source_ref=ref, todo=todo))
                        if edit in actions:
                            b.permissions.append(Permission(principal, "edit", "Notebook", slug, source_expression=src, source_ref=ref, todo=todo))
        for did, doc in self.assets["dataset"].items():
            ds = self.datasets.get(did)
            if not ds:
                continue
            ids = [r.table_id for r in ds.rels.values() if r.kind != "other" and r.table_id]
            for perm in doc.get("Permissions") or []:
                principal, todo = _principal(perm.get("Principal", ""))
                src = f"dataset {ds.name!r} permission: {', '.join(sorted(str(a).split(':')[-1] for a in perm.get('Actions') or []))}"
                for tid in ids:
                    b.permissions.append(Permission(principal, "query", "Table", tid, source_expression=src,
                                                    source_ref=f"quicksight:dataset/{did}", todo=todo))
        for sid, doc in self.assets["datasource"].items():
            for perm in doc.get("Permissions") or []:
                principal, todo = _principal(perm.get("Principal", ""))
                src = f"data source {doc.get('Name')!r} permission: {', '.join(sorted(str(a).split(':')[-1] for a in perm.get('Actions') or []))}"
                b.permissions.append(Permission(principal, "query", "Datasource", b.datasource, source_expression=src,
                                                source_ref=f"quicksight:datasource/{sid}",
                                                todo=todo or "QuickSight data source access lets authors build datasets on it; "
                                                              "narrow it to the tables they need"))
        unique: dict[tuple[str, ...], Permission] = {}
        for p in b.permissions:
            unique.setdefault((p.principal, p.action, p.resource_type, p.resource_id, p.effect), p)
        b.permissions = list(unique.values())

    def _security(self) -> None:
        b = self.b
        for did, ds in sorted(self.datasets.items()):
            if did in self.rules_ids:
                continue
            for rule in ds.doc.get("ColumnLevelPermissionRules") or []:
                principals = [_principal(p)[0] for p in rule.get("Principals") or []]
                cond = " || ".join(_cedar_principal(p) for p in principals if p != "*")
                for cname in rule.get("ColumnNames") or []:
                    c = ds.cols.get(cname)
                    if not c or c.kind != "phys" or ds.rels[c.rel].kind == "other":
                        b.warn(f"dataset {ds.name}", f"column-level security on {cname}, which is not a warehouse column; restrict it by hand")
                        continue
                    rel = ds.rels[c.rel]
                    b.permissions.append(Permission(
                        "*", "query", "Column", f"{rel.table_id}.{c.phys}", effect="forbid", condition=f"!({cond})" if cond else "",
                        source_expression=f"column-level security: visible to {', '.join(principals) or '(no one)'}",
                        source_ref=f"quicksight:dataset/{did}#cls/{cname}"))
            for rls in _rls_datasets(ds.doc):
                self._user_rls(ds, rls)
            for cfg in _tag_configs(ds.doc):
                self._tag_rls(ds, cfg)

    def _user_rls(self, ds: DS, rls: dict[str, Any]) -> None:
        b = self.b
        if str(rls.get("Status") or "ENABLED") == "DISABLED":
            return
        rid = _arn_id(rls.get("Arn", ""))
        rules = self.datasets.get(rid)
        policy = str(rls.get("PermissionPolicy") or "GRANT_ACCESS")
        src = json.dumps({k: rls.get(k) for k in ("PermissionPolicy", "FormatVersion")} | {"rules_dataset": rid})
        rf = RowFilter(name=snake(f"{ds.name}_{rules.name if rules else rid}"), table="", source_expression=src,
                       source_ref=f"quicksight:dataset/{ds.id}#rls")
        rel = next((r for r in ds.rels.values() if r.kind != "other"), None)
        rf.table = rel.table_id if rel else ds.name
        if not rules or len(rules.rels) != 1 or next(iter(rules.rels.values())).kind == "other":
            rf.todo = (f"user-based RLS from rules dataset {rid}, which is not in the export or not a single warehouse table or "
                       "custom SQL; rebuild it as a predicate joining your entitlements table")
            b.row_filters.append(rf)
            return
        rrel = next(iter(rules.rels.values()))
        names = {c.lower(): c for c in rules.cols}
        ucol, gcol = names.get("username"), names.get("groupname")
        fields = [c for c in rules.cols if c.lower() not in ("username", "groupname")]
        who = [f"r.{_q(ucol)} = {{{{ user.email }}}}" if ucol else "", f"r.{_q(gcol)} in ({{{{ user.groups }}}})" if gcol else ""]
        who = [w for w in who if w]
        targets = [ds.cols.get(f) for f in fields]
        if not who or not fields or any(t is None or t.kind != "phys" for t in targets):
            rf.todo = "user-based RLS rules dataset without UserName/GroupName or with fields that aren't warehouse columns of this dataset; write the predicate"
            b.row_filters.append(rf)
            return
        tables = {t.rel for t in targets if t}
        if len(tables) > 1 or len(fields) > 1:
            rf.todo = (f"user-based RLS on {len(fields)} fields ({', '.join(fields)}): QuickSight matches each rule row on all of them "
                       "(an empty cell matches every value); write it as a correlated EXISTS on the rules table")
            if len(tables) == 1:
                rf.table = ds.rels[next(iter(tables))].table_id
            b.row_filters.append(rf)
            return
        target = targets[0]
        assert target is not None
        rf.table = ds.rels[target.rel].table_id
        fcol, user = _q(fields[0]), " or ".join(who)
        user = f"({user})" if len(who) > 1 else user
        relation = rrel.relation
        grant = (f"({_q(target.phys)} in (select r.{fcol} from {relation} r where {user} and r.{fcol} is not null) "
                 f"or exists (select 1 from {relation} r where {user} and r.{fcol} is null))")
        rf.predicate = grant if policy == "GRANT_ACCESS" else f"not coalesce({grant}, false)"
        todo = ["QuickSight matches UserName to the QuickSight user name and GroupName to its groups: confirm {{ user.email }} "
                "and {{ user.groups }} carry those values, or map them"]
        if str(rls.get("FormatVersion") or "VERSION_1") == "VERSION_1":
            todo.append("rules dataset uses FormatVersion VERSION_1; check how it treats empty cells before relying on the predicate")
        if rrel.kind == "sql":
            todo.append("the rules dataset is custom SQL, inlined as a subquery")
        rf.todo = "; ".join(todo)
        b.row_filters.append(rf)

    def _tag_rls(self, ds: DS, cfg: dict[str, Any]) -> None:
        if str(cfg.get("Status") or "ENABLED") == "DISABLED":
            return
        for rule in cfg.get("TagRules") or []:
            key, cname = str(rule.get("TagKey") or ""), str(rule.get("ColumnName") or "")
            c = ds.cols.get(cname)
            rf = RowFilter(name=snake(f"{ds.name}_tag_{key}"), table="", source_ref=f"quicksight:dataset/{ds.id}#tag/{key}",
                           source_expression=json.dumps({k: rule.get(k) for k in ("TagKey", "ColumnName", "TagMultiValueDelimiter", "MatchAllValue")}))
            todos = [f"tag-based RLS applies to embedding sessions that pass session tag {key!r}; map it to a user attribute"]
            if not c or c.kind != "phys":
                rf.table = ds.name
                todos.append(f"{cname} is not a warehouse column of the dataset")
            else:
                rf.table = ds.rels[c.rel].table_id
                attr = f"{{{{ user.{snake(key)} }}}}"
                if rule.get("TagMultiValueDelimiter"):
                    todos.append(f"the tag holds several values separated by {rule['TagMultiValueDelimiter']!r}; split it in the predicate")
                else:
                    pred = f"{_q(c.phys)} = {attr}"
                    if rule.get("MatchAllValue"):
                        pred = f"({attr} = {_str(rule['MatchAllValue'])} or {pred})"
                    rf.predicate = pred
            if cfg.get("TagRuleConfigurations"):
                todos.append("TagRuleConfigurations combine tags per rule set; check the predicates against them")
            rf.todo = "; ".join(todos)
            self.b.row_filters.append(rf)


# ------------------------------------------------------------------ helpers


@dataclass
class _Ctx:
    decl: dict[str, str]
    calcs: dict[str, dict[str, str]]
    params: dict[str, str]
    multi: set[str]
    controls: dict[str, str]
    filter_groups: list[dict[str, Any]]
    dash: Dashboard
    control_binds: set[str] = field(default_factory=set)

    def param(self, name: str, multi_ok: bool = False) -> str:
        """The bind for a declared parameter, else Untranslatable."""
        if name not in self.params:
            raise Untranslatable(f"parameter {name} is not declared")
        if name in self.multi and not multi_ok:
            raise Untranslatable(f"multi-valued parameter {name}")
        return self.params[name]

    def control_bind(self, filter_id: str, field: str, default: Any, kind: str) -> str | None:
        title = self.controls.get(filter_id)
        if not title:
            return None
        bind = _bind(title)
        if not any(f.name == bind for f in self.dash.filters):
            self.dash.filters.append(DashboardFilter(bind, field, default, kind))  # type: ignore[arg-type]
            self.control_binds.add(bind)
        return bind


def _bind(name: str) -> str:
    b = snake(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name))
    return b if b and not b[0].isdigit() else f"p_{b}"


def _filter_sql(ftype: str, spec: dict[str, Any], col: str, ctx: _Ctx, field_name: str) -> str:
    null = str(spec.get("NullOption") or "")
    fid = str(spec.get("FilterId") or "")

    def with_nulls(pred: str) -> str:
        if null == "ALL_VALUES":
            return f"({pred} OR {col} IS NULL)"
        return pred

    if ftype == "CategoryFilter":
        kind, conf = _one(spec.get("Configuration"))
        null = str(conf.get("NullOption") or null)
        op = str(conf.get("MatchOperator") or "EQUALS")
        if kind == "FilterListConfiguration":  # a list of values: QuickSight only allows CONTAINS (include) / DOES_NOT_CONTAIN (exclude)
            op = {"CONTAINS": "EQUALS", "DOES_NOT_CONTAIN": "DOES_NOT_EQUAL"}.get(op, op)
        if conf.get("ParameterName"):
            if op != "EQUALS":
                raise Untranslatable(f"{op} against a parameter")
            bind = ctx.param(conf["ParameterName"])
            return f"({col} = :{bind} OR :{bind} IS NULL)"
        values = conf.get("CategoryValues") or ([conf["CategoryValue"]] if conf.get("CategoryValue") is not None else [])
        select_all = conf.get("SelectAllOptions") == "FILTER_ALL_VALUES" or not values
        if null == "NULLS_ONLY":
            return f"{col} IS NULL"
        bind = ctx.control_bind(fid, field_name, None if select_all else (values[0] if len(values) == 1 else values), "value") if op == "EQUALS" else None
        if bind:
            if not select_all and len(values) > 1:
                raise Untranslatable("the control's default selects several values; the notebook parameter holds one")
            pred = f"({col} = :{bind} OR :{bind} IS NULL)"
            return pred if null != "NON_NULLS_ONLY" else f"{pred} AND {col} IS NOT NULL"
        if select_all:
            return f"{col} IS NOT NULL" if null == "NON_NULLS_ONLY" else ""
        lits = [_str(v) for v in values]
        if op in ("EQUALS", "DOES_NOT_EQUAL"):
            neg = "NOT " if op == "DOES_NOT_EQUAL" else ""
            return with_nulls(f"{col} {neg}IN ({', '.join(lits)})")
        pats = {"CONTAINS": "%{}%", "DOES_NOT_CONTAIN": "%{}%", "STARTS_WITH": "{}%", "ENDS_WITH": "%{}"}
        if op in pats:
            neg = "NOT " if op == "DOES_NOT_CONTAIN" else ""
            esc = [re.sub(r"([%_\\])", r"\\\1", str(v)) for v in values]
            likes = [f"{col} {neg}LIKE {_str(pats[op].format(v))} ESCAPE " + "'\\'" for v in esc]
            return with_nulls(f"({(' AND ' if neg else ' OR ').join(likes)})")
        raise Untranslatable(f"match operator {op}")
    if ftype in ("NumericRangeFilter", "TimeRangeFilter"):
        if spec.get("AggregationFunction"):
            raise Untranslatable("filters on the aggregated value (HAVING)")
        timed = ftype == "TimeRangeFilter"
        grain = GRAINS.get(str(spec.get("TimeGranularity") or ""), "") if timed else ""
        target = f"date_trunc('{grain}', {col})" if grain and grain != "second" else col
        parts = []
        for key, op, inc in (("RangeMinimumValue" if timed else "RangeMinimum", ">", "IncludeMinimum"),
                             ("RangeMaximumValue" if timed else "RangeMaximum", "<", "IncludeMaximum")):
            v = spec.get(key) or {}
            if not v:
                continue
            o = op + ("=" if spec.get(inc) else "")
            if v.get("Parameter"):
                bind = ctx.param(v["Parameter"], multi_ok=True)
                parts.append(f"({target} {o} :{bind} OR :{bind} IS NULL)")
            elif v.get("RollingDate"):
                raise Untranslatable(f"rolling date {json.dumps(v['RollingDate'])}")
            elif v.get("StaticValue") is not None:
                value = v["StaticValue"]
                if timed:
                    parts.append(f"{target} {o} TIMESTAMP {_str(_timestamp(value))}")
                else:
                    parts.append(f"{target} {o} {value}")
        if not parts:
            return f"{col} IS NOT NULL" if null == "NON_NULLS_ONLY" else ""
        pred = " AND ".join(parts)
        return with_nulls(f"({pred})") if null == "ALL_VALUES" else pred
    if ftype == "NumericEqualityFilter":
        if spec.get("AggregationFunction"):
            raise Untranslatable("filters on the aggregated value (HAVING)")
        neg = str(spec.get("MatchOperator") or "EQUALS") == "DOES_NOT_EQUAL"
        if spec.get("ParameterName"):
            bind = ctx.param(spec["ParameterName"], multi_ok=True)
            return f"({col} {'<>' if neg else '='} :{bind} OR :{bind} IS NULL)"
        if spec.get("Value") is None:
            return ""
        return with_nulls(f"{col} {'<>' if neg else '='} {spec['Value']}")
    if ftype == "RelativeDatesFilter":
        raise Untranslatable(f"relative date filter ({spec.get('RelativeDateType')} {spec.get('RelativeDateValue') or ''} "
                             f"{spec.get('TimeGranularity')}); pin it to a date range or write it in the warehouse dialect")
    raise Untranslatable(f"{ftype} has no SQL translation here")


def _in_scope(fg: dict[str, Any], sheet_id: Any, vid: str) -> bool:
    scope = fg.get("ScopeConfiguration") or {}
    if "AllSheets" in scope:
        return True
    for s in (scope.get("SelectedSheets") or {}).get("SheetVisualScopingConfigurations") or []:
        if s.get("SheetId") == sheet_id and (s.get("Scope") == "ALL_VISUALS" or vid in (s.get("VisualIds") or [])):
            return True
    return False


def _sheet_rows(sheet: dict[str, Any], ids: list[str]) -> list[list[str]]:
    """Visual and text box ids of a sheet in reading order, as rows."""
    items: list[tuple[str, float, float]] = []
    for layout in sheet.get("Layouts") or []:
        conf = layout.get("Configuration") or {}
        if conf.get("GridLayout"):
            items += [(e["ElementId"], float(e.get("ColumnIndex") or 0), float(e.get("RowIndex") or 0))
                      for e in conf["GridLayout"].get("Elements") or [] if e.get("ElementId") in ids]
        elif conf.get("FreeFormLayout"):
            items += [(e["ElementId"], _px(e.get("XAxisLocation")), _px(e.get("YAxisLocation")))
                      for e in conf["FreeFormLayout"].get("Elements") or [] if e.get("ElementId") in ids]
        if items:
            break
    placed = {i for i, _, _ in items}
    return grid_rows(items) + [[i] for i in ids if i not in placed]


def _rls(doc: dict[str, Any], top: str, nested: str) -> list[dict[str, Any]]:
    """A dataset's RLS config: the top-level member plus the one in each semantic model table."""
    tables = ((doc.get("SemanticModelConfiguration") or {}).get("TableMap") or {}).values()
    found = [doc.get(top)] + [(t.get("RowLevelPermissionConfiguration") or {}).get(nested) for t in tables]
    return [r for r in found if r]


def _rls_datasets(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return _rls(doc, "RowLevelPermissionDataSet", "RowLevelPermissionDataSet")


def _tag_configs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return _rls(doc, "RowLevelPermissionTagConfiguration", "TagConfiguration")


def _measure(node: Node | None, sc: Scope, scope: dict[str, Col]) -> tuple[str, str] | None:
    """A single aggregation of a row expression -> (MetricFlow agg, expression), else None."""
    if not isinstance(node, Call) or (node.name not in AGGS and node.name not in COND_AGGS):
        return None
    cond = node.name in COND_AGGS
    if len(node.args) != (2 if cond else 1):
        return None
    tr = sc.translator(scope)
    try:
        value = tr.node(node.args[0])
        if value.kind != "row":
            return None
        expr = value.sql
        if cond:
            c = tr.node(node.args[1])
            if c.kind != "row":
                return None
            expr = f"case when {c.sql} then {value.sql} end"
    except Untranslatable:
        return None
    return (COND_AGGS if cond else AGGS)[node.name][0], expr


def _is_agg_calc(calcs: dict[str, str], name: str, depth: int = 0) -> bool:
    if name not in calcs or depth > 10:
        return False
    try:
        node = parse_expression(calcs[name])
    except Untranslatable:
        return False
    return any(c in AGGS or c in COND_AGGS or c in TABLE_CALCS for c in calls(node)) or any(
        _is_agg_calc(calcs, r, depth + 1) for r in field_refs(node))


def _calc_type(node: Node) -> str:
    if isinstance(node, Call) and node.name in ("truncdate", "adddatetime", "parsedate", "now"):
        return "timestamp"
    if isinstance(node, Call) and node.name in ("extract", "datediff", "strlen", "abs", "round", "floor", "ceil") or (
            isinstance(node, BinOp) and node.op in "+-*/^%") or isinstance(node, Num):
        return "number"
    return "string"


def _load(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _asset_kind(rel: str, doc: Any) -> str:
    """dashboard / analysis / dataset / datasource for an asset JSON file, else ''."""
    if not isinstance(doc, dict):
        return ""
    rtype = str(doc.get("resourceType") or doc.get("ResourceType") or "").lower().replace("_", "")
    if rtype in KINDS:
        return rtype
    keys = {k.lower() for k in doc}
    for key, kind in (("dashboardid", "dashboard"), ("analysisid", "analysis"), ("datasetid", "dataset"), ("datasourceid", "datasource")):
        if key in keys:
            return kind
    folder = PurePosixPath(rel).parent.name.lower().replace("_", "").replace("-", "")
    folder = {"analyses": "analysis"}.get(folder, folder.removesuffix("s"))
    return folder if folder in KINDS else ""


def unpack_bundle(data: bytes, name: str) -> dict[str, bytes]:
    """A .qs asset bundle (a zip) -> {bundle/<path>: bytes}, refusing unsafe paths, symlinks and zip bombs."""
    out: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if sum(i.file_size for i in infos) > _manual.MAX_UNZIPPED:
                raise SourceError(f"{name}: expands to more than {_manual.MAX_UNZIPPED // 1024**3} GiB")
            for info in infos:
                rel = _manual.member_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise SourceError(f"{name}: symlink in archive: {info.filename!r}")
                if not info.is_dir():
                    out[f"bundle/{rel}"] = zf.read(info)
    except zipfile.BadZipFile as exc:
        raise SourceError(f"{name}: not a QuickSight asset bundle (not a zip: {exc})") from exc
    if not any(_asset_kind(rel, _load(v)) for rel, v in out.items() if rel.endswith(".json")):
        raise SourceError(f"{name}: no QuickSight dashboards, analyses, datasets or data sources inside")
    return out


def _get(url: str) -> bytes:
    import httpx

    res = httpx.get(url, timeout=300, follow_redirects=True)
    res.raise_for_status()
    return res.content
