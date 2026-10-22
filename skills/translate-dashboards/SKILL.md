---
name: translate-dashboards
description: Use when finishing migrated dashboards in notebooks/*.ipynb (Jupyter, SQL + Altair) - fixing tile SQL, wiring filters, mapping BI chart types to Altair charts (including facets, layers and dual axes), and resolving TODO(unshackle) items in notebooks.
---

# Finish migrated dashboards

Each dashboard is a Jupyter notebook, `notebooks/<slug>.ipynb`: a title cell (name,
source, link to the original), a `setup` cell (imports and `engine` from
`UNSHACKLE_WAREHOUSE_URL`), a `parameters` cell (tagged `parameters`, one
`name = default` per dashboard filter), then per tile in layout order a `## Title`
markdown cell, a SQL cell (`<tile>_sql = """..."""` read into the DataFrame `<tile>`)
and a chart cell that builds `<tile>_chart`. Table tiles show the DataFrame; text tiles
are markdown cells. TODOs are `# TODO(unshackle): ...` comments at the top of a tile's
cells (a tile with no SQL has `<tile>_sql = ""` and an empty DataFrame).

Edit it in Jupyter, VS Code or as JSON. Keep it saved without outputs (clear outputs
before committing): outputs contain warehouse data and make diffs unreadable.

## 1. Fix the tile SQL

- Recover the query from the raw export (`migration/raw/<tool>/`, path in
  `# source:`) or the tool's generated SQL (see the source skill).
- Compute numbers the same way the metric does: copy the measure's `expr`/`agg` and
  the metric's `filter` from `semantic/*.yml`. Don't re-invent a definition that
  already exists as a metric or a `-- metric: true` query.
- Alias output columns to the names the chart encodes (and to the BI tool's column
  names if you'll compare with its CSV in parity).
- Dashboard filters become `:name` binds; the value comes from the parameters cell
  and is passed to `pd.read_sql`:

```python
# parameters cell
region = None
date_from = '2025-01-01'
date_to = '2025-12-31'
```

```python
revenue_sql = """
select date_trunc('month', order_date)::date as month, sum(amount) as revenue
from analytics.fct_orders
where order_date >= :date_from and order_date < :date_to
  and (:region is null or region = :region)
group by 1
"""
revenue = pd.read_sql(text(revenue_sql), engine, params={'region': region, 'date_from': date_from, 'date_to': date_to})
```

  Split a date range into `<name>_from` / `<name>_to` parameters. Use
  `(:x is null or col = :x)` for optional filters (default `None` = all). `::type`
  casts are safe (only single-colon names are binds). Multi-select lists depend on
  the driver; test them, otherwise note the limitation in a TODO.
- Write in the customer's warehouse dialect (`sqlglot.transpile(sql, read=..., write=...)`).
- Table calcs (running totals, percent of total, rank, moving average) go here as
  window functions: `sum(revenue) over (order by month)`,
  `revenue / sum(revenue) over ()`.
- Top N: `order by revenue desc limit 10` in SQL, not in Altair.

## 2. Map the chart

| BI chart | Altair |
| --- | --- |
| Bar / column | `mark_bar()`, category on `x` (column) or `y` (horizontal bar) |
| Grouped bar | `mark_bar()` + `xOffset=alt.XOffset('series:N')` + `color` |
| Stacked bar | `mark_bar()` + `color=alt.Color('series:N')` |
| 100% stacked | `y=alt.Y('value:Q', stack='normalize')` |
| Line | `mark_line(point=True)`, time on `x` as `:T` |
| Area | `mark_area()` (+ `color` for stacked) |
| Scatter | `mark_point()` with two `:Q` channels |
| Pie / donut | `mark_arc()` (donut: `mark_arc(innerRadius=50)`) with `theta=alt.Theta('value:Q')`, `color=alt.Color('category:N')` |
| KPI / single value | `mark_text(fontSize=36)` with `text=alt.Text('value:Q', format=',.0f')`; SQL returns one row |
| Table / pivot | show the DataFrame (pivot in SQL or `df.pivot_table(...)`) |
| Heatmap | `mark_rect()` with `x`, `y`, `color` |
| Combo (bar + line on one axis) | `alt.layer(base.mark_bar().encode(y=...), base.mark_line().encode(y=...))` |
| Dual axis | the same layer + `.resolve_scale(y='independent')` |
| Small multiples / trellis | `row=alt.Row(...)`, `column=alt.Column(...)` or `facet=alt.Facet(..., columns=3)`; a layered chart takes `.facet(row=..., column=...)` |
| Several measures in separate panes | `alt.vconcat(...)` (stacked) or `alt.hconcat(...)` (side by side) |
| Reference line / band | layer a `mark_rule()` / `mark_rect()` over the chart |
| Map, custom visual, anything else | show the DataFrame with a TODO that references the source (e.g. `# TODO(unshackle): filled map, see <url> / screenshot`); a custom Vega-Lite spec ports with `alt.Chart.from_dict(spec)` |

Encoding types: `:Q` numbers, `:N` categories, `:T` dates/timestamps, `:O` ordered
categories (months as names, ranks, buckets).

Sorting: `x=alt.X('region:N', sort='-y')` sorts by value; `sort=['Low', 'Mid', 'High']`
for a fixed order. Keep the BI tool's sort.

Titles and formats: `.properties(title=...)`, axis titles with `title=`,
`axis=alt.Axis(format='%b %Y')` for dates, `format='$,.0f'` / `'.1%'` for numbers.
Add `tooltip=[...]` for the fields users hover on.

Colours: use the default scheme. Set a colour with `alt.value('#d62728')` or a
`scale=alt.Scale(domain=[...], range=[...])` only when it carries meaning in the
source (red = bad, brand colours for a fixed set of categories).

## Multi-view charts

Altair compiles to Vega-Lite, so facets, layers and concatenation work everywhere the
notebook runs. Match the source: a combo chart is a layer, a dual axis is a layer with
independent y scales, a trellis is a facet. For several series the build writes a
`base` chart with the shared encodings and one layer or panel per series; layers get
`color=alt.datum('<series>')` so each series has its own colour and legend entry. Two
bar series side by side are a fold (`transform_fold` or a long-format SQL result) with
`xOffset` and `color`, not a layer: layered bars draw on top of each other.

Example, combo chart with a dual axis:

```python
base = alt.Chart(revenue).encode(x=alt.X('month:T', title='Month'))
revenue_chart = (
    alt.layer(
        base.mark_bar().encode(y=alt.Y('revenue:Q', title='Revenue'), color=alt.datum('Revenue')),
        base.mark_line(point=True).encode(y=alt.Y('orders:Q', title='Orders'), color=alt.datum('Orders')),
    )
    .resolve_scale(y='independent')
    .properties(title='Revenue and orders')
)
revenue_chart
```

Example, grouped bar:

```python
revenue_by_region_chart = (
    alt.Chart(revenue_by_region)
    .mark_bar()
    .encode(
        x=alt.X('quarter:O', title='Quarter'),
        xOffset=alt.XOffset('region:N'),
        y=alt.Y('revenue:Q', title='Revenue', axis=alt.Axis(format='$,.0f')),
        color=alt.Color('region:N'),
        tooltip=['quarter:O', 'region:N', alt.Tooltip('revenue:Q', format='$,.0f')],
    )
    .properties(title='Revenue by region')
)
revenue_by_region_chart
```

## Text tiles

Text tiles are markdown cells. Convert the source's rich text or HTML to Markdown,
keep links, drop styling.

## Layout

Cells follow the source dashboard top to bottom, left to right. The notebook metadata
`unshackle.layout` keeps the source rows (tile names left to right) for tools that
rebuild the grid; update it if you add, split or rename tiles.

## Test

```
UNSHACKLE_WAREHOUSE_URL=snowflake://... python -m unshackle.run notebooks/<slug>.ipynb   # -> notebooks/<slug>.html
```

or open the notebook in Jupyter / VS Code with `UNSHACKLE_WAREHOUSE_URL` set and run
all cells. `papermill notebooks/<slug>.ipynb out.ipynb -p region EU` runs it with other
filter values (don't commit `out.ipynb`).

Open the HTML and compare with the source dashboard (same filters, same period). A
tile with an empty table still has no SQL; one showing its data instead of a chart
still has no mark. `*.html` is git-ignored; don't commit previews (they contain data).

Before you finish a notebook: no `TODO(unshackle)` left unless explained, every tile
renders, outputs are cleared, and the numbers agree with a parity case for at least the
headline tiles.
