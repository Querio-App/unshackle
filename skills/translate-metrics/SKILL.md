---
name: translate-metrics
description: Use when turning BI calculations (LookML measures, DAX, Tableau calcs and LODs, Sigma/ThoughtSpot/Omni formulas, Lightdash metrics, Hex SQL) into Apache Ossie semantic models (plus MetricFlow in dbt projects), or when resolving TODO(unshackle) items in semantic/*.ossie.yaml, semantic/*.yml or queries/.
---

# Translate BI calculations into Ossie (and MetricFlow in dbt projects)

The build wrote one [Apache Ossie](https://github.com/apache/ossie) document per
semantic model, `semantic/<model>.ossie.yaml`. In a dbt project (`dbt_project.yml`
at the root) it also wrote MetricFlow YAML next to it, `models/semantic/<model>.yml`
(and `_<tool>_metrics.yml` for metrics it couldn't attach to a model). Fix both
when both exist: they describe the same metrics.

Unfinished items are never written as live definitions. Each has a
`# TODO(unshackle): <kind> <name>: <why> [<language>] <original expression>` line
at the end of the file. In Ossie the item itself is kept under `custom_extensions`
(vendor `UNSHACKLE`, key `pending`): on the dataset for dimensions and measures, on
the model for metrics. In MetricFlow it is a commented-out block under the TODO line.
Write a faithful definition, then delete the pending entry and the TODO line.

## Decide what it becomes

| Source calc | Ossie | MetricFlow (dbt projects) |
| --- | --- | --- |
| Plain aggregation (`SUM`, `COUNT`, `COUNTD`, `AVG`, `MIN`, `MAX`, `MEDIAN`) | dataset metric `SUM(amount)` | measure (`agg`) + `simple` metric |
| One aggregate divided by another | metric `1.0 * SUM(a) / nullif(COUNT(b), 0)` | `ratio` metric (numerator/denominator are **metrics**) |
| Aggregation with a condition (`CALCULATE(..., filter)`, LookML `filters:`, `SUM_IF`) | `SUM(CASE WHEN status = 'complete' THEN amount END)` | `simple` metric with `filter:` |
| Running total, YTD / MTD / QTD | no equivalent: verified query | `cumulative` with `grain_to_date` (to-date) or `window` (rolling) |
| Period over period (prior year, % growth) | no equivalent: verified query | `derived` metric with `offset_window` |
| Arithmetic on other metrics | one metric with the inputs' expressions inlined | `derived` metric with `expr` |
| Row-level expression (`IF`, string/date maths, bucketing) | field `expression` | dimension `expr`, or measure `expr` when it feeds an aggregate |
| Semi-additive (balances, snapshots, `LASTNONBLANK`) | no equivalent: verified query | measure with `non_additive_dimension` |
| Pre-aggregated at a fixed grain (Tableau `FIXED`, Looker derived tables / PDTs, ThoughtSpot `group_aggregate`, DAX `SUMMARIZE` tables) | SQL model in `queries/models/` (dbt: `models/unshackle/`), then a semantic model on it | same |
| View-dependent (Tableau table calcs, `INCLUDE`/`EXCLUDE`, Omni cell formulas, Sigma window functions, Lightdash table calculations, DAX `ALLSELECTED`) | window SQL in the notebook tile (see `translate-dashboards`), not a metric | same |
| Nothing above fits | verified query with `-- metric: true` (bottom of this file) | same |

## Ossie rules

- Spec: `core-spec/spec.md` in apache/ossie, version `0.2.0.dev0`. One semantic
  model per document: `version`, `name`, `datasets`, then `relationships` and `metrics`.
- A dataset is a table (`source`), with `primary_key`, `unique_keys` and `fields`.
  A field is a row-level `expression`; time dimensions have `dimension: {is_time: true}`.
- Relationships go from the many side to the one side: `from`, `to`,
  `from_columns`, `to_columns` (field names; `to_columns` is a key of `to`).
- A metric that reads one dataset goes in that dataset's `metrics` and names its
  fields without a prefix: `SUM(amount)`. One that reads several goes in the
  model's `metrics` and uses `dataset.field`: `SUM(CASE WHEN customers.region = 'EU' THEN orders.amount END)`.
  Field names and dataset metric names share one namespace, which is why some
  fact fields are called `<name>_value`.
- Metrics can't reference other metrics: inline the inputs' expressions. Divide
  with `1.0 * a / nullif(b, 0)`.
- The document copies in the datasets its model joins to so relationships resolve.
  A copy's `custom_extensions` names the file that defines it (`defined_in`): edit
  that file, then make the same change to the copies.
- Expressions are in the warehouse's dialect (`SNOWFLAKE`, `BIGQUERY`,
  `DATABRICKS`, else `ANSI_SQL`, from `[datasources.*] type` in `unshackle.toml`).
- unshackle's own data (labels, source references, time granularity, pending
  items) is JSON in `custom_extensions` with `vendor_name: UNSHACKLE`.

## MetricFlow rules (dbt projects only)

- Every semantic model needs `entities`, including one `type: primary` when it has
  dimensions (types: `primary`, `foreign`, `unique`, `natural`). Joins happen
  through a `foreign` entity whose name matches another model's primary entity.
- Models with measures need `defaults.agg_time_dimension` pointing at a
  `type: time` dimension with `type_params.time_granularity`.
- `agg` is one of `sum count count_distinct average min max median percentile
  sum_boolean`. Percentile: `agg_params: {percentile: 0.9, use_discrete_percentile: false}`.
- Names are `snake_case`, unique across the project (measures too). Keep the
  original BI name as `label`.
- Ratio numerator/denominator must be metrics. If only a measure exists, add a
  `simple` metric for it (or set `create_metric: true` on the measure).
- Filters use Jinja with the **entity** prefix, not the model name:
  `"{{ Dimension('order__status') }} = 'complete'"`,
  `"{{ TimeDimension('order__order_date', 'month') }} >= '2024-01-01'"`.
- Cumulative params: unshackle writes `type_params.window` / `grain_to_date`. dbt >= 1.9
  prefers `type_params.cumulative_type_params: {window: ..., grain_to_date: ...}`;
  use whichever the repo's dbt version accepts.
- Write `expr` SQL in the customer's warehouse dialect. To convert:
  `sqlglot.transpile(sql, read="snowflake", write="bigquery")[0]`.

## Before / after

**LookML** `measure: completed_revenue { type: sum sql: ${amount} ;; filters: [status: "complete"] }`

```yaml
measures:
- {name: revenue, agg: sum, expr: amount}
metrics:
- name: completed_revenue
  label: Completed Revenue
  type: simple
  type_params: {measure: revenue}
  filter: "{{ Dimension('order__status') }} = 'complete'"
```

The same in Ossie, under the `orders` dataset's `metrics` (`amount` and `status` are fields):

```yaml
- name: completed_revenue
  expression:
    dialects:
    - dialect: SNOWFLAKE
      expression: SUM(CASE WHEN status = 'complete' THEN amount END)
```

**DAX** `Margin % = DIVIDE([Total Profit], [Total Sales])` -> ratio:

```yaml
- name: margin_pct
  label: Margin %
  type: ratio
  type_params: {numerator: total_profit, denominator: total_sales}
```

In Ossie: `expression: 1.0 * SUM(profit) / nullif(SUM(sales), 0)`.

**DAX** `Sales YTD = TOTALYTD([Total Sales], 'Date'[Date])`:

```yaml
- name: sales_ytd
  label: Sales YTD
  type: cumulative
  type_params: {measure: total_sales, grain_to_date: year}
```

Check the fiscal year: `TOTALYTD(..., "6/30")` needs a fiscal-year dimension; if
MetricFlow can't express it, fall back to a verified query.

**DAX** `Sales PY = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))` and growth:

```yaml
- name: sales_yoy_growth
  label: Sales YoY %
  type: derived
  type_params:
    expr: (total_sales - total_sales_py) / nullif(total_sales_py, 0)
    metrics:
    - name: total_sales
    - name: total_sales
      offset_window: 1 year
      alias: total_sales_py
```

**Tableau** `IF [Region] = "EU" THEN [Sales] END` aggregated with SUM -> measure
`{name: eu_sales, agg: sum, expr: "case when region = 'EU' then sales end"}`.

**Tableau** `{FIXED [Customer ID] : MIN([Order Date])}` (first order date) -> SQL model
`queries/models/customer_first_order.sql`:

```sql
select customer_id, min(order_date) as first_order_date
from analytics.fct_orders group by 1
```

then a semantic model on it with entity `customer` (primary) and a time dimension
`first_order_date`. In dbt repos write it as `models/unshackle/customer_first_order.sql`
and reference it with `model: ref('customer_first_order')`.

**Tableau** `RUNNING_SUM(SUM([Sales]))` or `WINDOW_AVG` -> table calc; do it in the
notebook tile SQL with `sum(sales) over (order by month)`.

**Sigma** `CountDistinct([Customer Id])` -> `agg: count_distinct, expr: customer_id`.
`CumulativeSum([Revenue])` -> window SQL in the tile, or a `cumulative` metric if it
runs over time with no other partition.

**ThoughtSpot** `sum_if ( status = 'complete' , revenue )` -> simple metric with filter
(as LookML above). `group_aggregate ( sum ( revenue ) , { customer_id } , {} )` -> SQL
model. `safe_divide ( sum ( profit ) , sum ( revenue ) )` -> ratio.

**Omni** view YAML `measures: { total_revenue: { sql: '${amount}', aggregate_type: sum } }`
-> measure + simple metric; Omni `filters:` on a measure -> metric `filter`. Workbook
cell formulas (`=B2/B1`) are view-dependent: tile SQL.

**Lightdash** (dbt `meta.metrics`) `type: sum` -> measure; `type: number` with
`sql: ${revenue} / ${orders}` -> ratio or derived; metric `filters:` -> metric
`filter`. Table calculations -> tile SQL. In dbt repos keep the semantic YAML under
`models/semantic/` next to the models.

**Hex**: SQL cells that compute a KPI become verified queries; promote them to a
metric only when the SQL is a plain aggregation over one table (plus joins you can
model as entities).

## Validate

- Ossie: `python validation/validate.py semantic/*.ossie.yaml` from a clone of
  apache/ossie (needs `pyyaml` and `jsonschema`; `sqlglot` adds SQL syntax checks).
  It checks the JSON Schema, unique names and relationship references.
- dbt repo: `dbt parse` (catches YAML and reference errors), then
  `mf validate-configs` if MetricFlow is installed (`dbt sl validate` on dbt Cloud).
  unshackle adds `*.ossie.yaml` to `.dbtignore` so dbt doesn't read Ossie files as
  properties; keep that line.
- Spot-check with the warehouse: run the measure's SQL for a fixed month and compare
  with the BI tool (the `verify` skill turns this into a parity case).

## Fallback: verified query with `-- metric: true`

Use when Ossie (or MetricFlow) can't express the calc faithfully (fiscal calendars it can't
model, complex multi-step logic, dialect-specific functions, non-equi joins). Write
`queries/<slug>.sql`:

```sql
-- name: Net revenue retention
-- description: NRR for customers active 12 months ago, trailing 12 months
-- datasource: warehouse
-- metric: true
-- aliases: NRR, net dollar retention
-- source: looker://model/explore/finance.nrr
select ...
```

Remove the pending entry from `semantic/` (or leave it with the TODO pointing at the
query) and mention it in the PR. If you can't write a faithful query either, keep
the TODO and add your reasoning.
