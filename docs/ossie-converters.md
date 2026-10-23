# Apache Ossie converters for Hex, Omni, Sigma and ThoughtSpot

unshackle writes one Apache Ossie document per semantic model
(`src/unshackle/target/ossie.py`). The
[apache/ossie-converters](https://github.com/apache/ossie-converters) repository has
converters for four of the tools unshackle reads: Hex, Omni, Sigma and ThoughtSpot.
This note compares each converter's Ossie output with unshackle's on the same input and
records whether unshackle should call the converter for the semantic model.

**Result: unshackle keeps its own parsers for all four tools for now.** Nothing in the
code changes. The reasons for each tool are below, along with what would change the
answer.

## What was compared

- apache/ossie-converters `82b9eff` (2026-10-22). Its `ossie` submodule pins apache/ossie
  `f098821`. apache/ossie HEAD `7205e15` was used for the JSON Schema and
  `validation/validate.py`.
- unshackle `main` at `8aa04f5`, Python 3.11.
- Inputs:
  - unshackle's raw fixtures in `tests/fixtures/<tool>/`. These are the Omni model
    directory, the Sigma data model `spec.json`, and the ThoughtSpot Model TML plus
    its Table TML.
  - Each converter's own fixtures, converted into unshackle's raw layout. For Omni
    that was `fixtureA`, `fixtureB` and `tpcds`. For Sigma it was `fixtureA`, `fixtureB`
    and `fixtureC`. For ThoughtSpot it was `minimal` and `tpcds`.
- Checks run on each output:
  - the Ossie JSON Schema;
  - `validation/validate.py`;
  - the field rules in `core-spec/spec.md` that the validator doesn't enforce yet:
    keys and relationship columns name declared fields, field expressions read source
    columns, and metrics reference fields rather than other metrics;
  - whether each metric has an expression in a SQL dialect.

Every output from both sides passes the JSON Schema and `validate.py`. The differences
are in fidelity.

## Converter facts

| | Hex | Omni | Sigma | ThoughtSpot |
| --- | --- | --- | --- | --- |
| Package | `apache-ossie-hex` 0.2.0.dev0 | `ossie-omni` 0.2.0.dev0 | `apache-ossie-sigma` 0.2.0.dev0 | `apache-ossie-thoughtspot` 0.1.0 |
| On PyPI | No | No | No | No |
| Direction | Ossie -> Hex only (`export`) | Both | Both | Both |
| License | Apache-2.0 | Apache-2.0 | Apache-2.0 | Apache-2.0 |
| Python | >= 3.11 | >= 3.10 (README says 3.9+) | >= 3.11 | >= 3.10 |
| Runtime deps | `apache-ossie` (not on PyPI), `hex-sl-utils`, pydantic, PyYAML | PyYAML | `apache-ossie` (not on PyPI), PyYAML, sqlglot | PyYAML |
| Own tests at `82b9eff` | 223 pass, `test_invalid_ossie_document` fails against the pinned Ossie; the TPC-DS snapshot test also fails against Ossie HEAD | Pass against the pinned Ossie; 2 TPC-DS tests fail against Ossie HEAD | 109 pass | 1015 pass |
| Vendor-only data | n/a | `custom_extensions[OMNI]`, lossless round trip | `custom_extensions[SIGMA]` plus `SIGMA` dialect text, lossless round trip | `custom_extensions[THOUGHTSPOT]` plus `THOUGHTSPOT` dialect text, lossless round trip |

None of the four converters is published on PyPI, and neither is `apache-ossie`, which
the Hex and Sigma converters depend on. PyPI rejects packages whose metadata contains
direct URL dependencies. That means unshackle can't declare an installable extra such as
`unshackle[ossie-sigma]` until these packages are published.

## Per-tool comparison

Counts are datasets / fields / relationships / metrics. "SQL" means the metric has an
expression in a SQL dialect (`ANSI_SQL` or a warehouse dialect) that a SQL engine or
another Ossie consumer can run.

### Hex: keep ours

The converter only goes from Ossie to Hex. It can't read a Hex semantic project, which is
what unshackle extracts, so there's nothing to compare on input. unshackle's Hex parser
stays.

### Omni: keep ours

| Input | Converter | unshackle |
| --- | --- | --- |
| unshackle fixture | 2 / 13 / 1 / 10 (10 SQL) | 2 / 14 / 1 / 9 (9 SQL, 1 TODO) |
| `fixtureB` | 3 / 11 / 2 / 5 | 3 / 11 / 1 / 4 (1 TODO) |
| `tpcds` | 5 / 32 / 4 / 8 | 5 / 32 / 4 / 3 (5 TODO) |

Where the converter does better:

- Composite keys: `custom_compound_primary_key_sql` and multi-column joins.
- Raw-SQL measures that aggregate fields across views. TPC-DS has five of these, and the
  converter writes all five as model-scoped metrics. unshackle leaves them as
  `TODO(unshackle)`.
- One document per model, where unshackle copies joined datasets into each document.
- Lossless round trip.

Where unshackle does better:

- **Filtered measures.** The converter writes them without their filter. It warns and
  keeps the original in `custom_extensions`, but the Ossie expression
  (`SUM(order_items.sale_price)` for `completed_revenue`) returns a different number from
  Omni. unshackle writes `SUM(CASE WHEN status = 'Complete' THEN sale_price END)`.
- **Measures built on other measures.** The converter keeps the references:
  `total_margin / NULLIF(total_revenue, 0)`, and
  `order_items.total_revenue / NULLIF(users.count, 0)`. The spec says metrics can't
  reference other metrics. `users.count` also matches nothing in the document, because
  the measure is written as `users__count`. unshackle inlines both, and divides as
  `1.0 * a / nullif(b, 0)`.
- **Field expressions.** The converter writes `${sale_price} - ${cost}` as
  `sale_price - cost`, which references fields. Field expressions should read source
  columns. unshackle writes `"SALE_PRICE" - "COST"`.

Verdict: fidelity comes first, and wrong numbers on filtered measures (common in Omni)
outweigh the coverage gains. Keep ours. Composite keys and cross-view raw-SQL measures
are gaps in unshackle's Omni parser to close.

### Sigma: keep ours

| Input | Converter | unshackle |
| --- | --- | --- |
| unshackle fixture | 4 / 23 / 1 / 10 (9 SQL) | 4 / 20 / 1 / 10 (9 SQL, 1 TODO) |
| `fixtureA` | 2 / 12 / 1 / 3 | 2 / 11 / 1 / 2 (1 TODO) |
| `fixtureB` | 7 / 20 / 1 / 0 | 4 / 12 / 0 / 0 |

Where the converter does better:

- Composite keys and composite relationships.
- Every Sigma element becomes a dataset, including `data-model`, `join` and `union`
  sources. Their `source` is a marker such as `join:elemJoined`, with the native block
  in `custom_extensions`.
- Coarse datatypes from column formats.
- Sigma formula text kept verbatim.

Where unshackle does better:

- **Physical columns.** The converter's `ANSI_SQL` uses Sigma's display name instead of
  the warehouse column. `[FCT_ORDERS/Amount Usd]` becomes `"FCT_ORDERS"."Amount Usd"`,
  but the column id is `inode-…/AMOUNT_USD`. The qualifier `FCT_ORDERS` also isn't a
  dataset name in the document. unshackle writes `amount_usd`.
- **Calculated columns.** The converter writes them against other fields' display
  names, for example `"Amount" - "Discount"` and `CASE WHEN "Status" = 'closed' …`. The
  spec says field expressions read source columns.
- **Metrics built on other metrics.** The converter writes `[Revenue] / [Orders]` as
  `"Orders"."Revenue" / "Orders"."Orders"`, which references metrics. unshackle inlines
  it as `1.0 * SUM(amount) / nullif(COUNT(DISTINCT order_id), 0)`.
- **Custom SQL elements.** The converter keeps only `sql:<connectionId>` as the source.
  unshackle saves the statement under `queries/`, points the dataset at a named model, and adds a TODO to materialize it.
- **Aggregates inside calculated columns.** In `fixtureA`, `Net Amount` contains
  `SumIf`. The converter writes it as a row-level field with an aggregate in it, and
  then writes `AVG("Orders"."Net Amount")`, which nests aggregates. unshackle marks that
  metric TODO.

Verdict: most converter expressions aren't runnable against the warehouse. Keep ours.
Composite keys and the `data-model`/`union` element sources are gaps in unshackle's Sigma
parser to close.

### ThoughtSpot: keep ours

| Input | Converter | unshackle |
| --- | --- | --- |
| unshackle fixture | 2 / 9 / 1 / 9 (0 SQL) | 2 / 15 / 1 / 7 (7 SQL, 2 TODO) |
| `minimal` | 2 / 2 / 1 / 1 (0 SQL) | 2 / 5 / 1 / 1 (1 SQL) |
| `tpcds` | 6 / 36 / 7 / 9 (0 SQL) | 6 / 39 / 5 / 5 (5 SQL, 4 TODO) |

By design, the converter writes ThoughtSpot formulas in the `THOUGHTSPOT` dialect and
adds `ANSI_SQL` only for a bare column reference (see its README). So none of its
metrics have SQL. unshackle translates `sum`, `unique_count`, `sum_if`, `safe_divide`,
arithmetic and `if/then/else` to SQL, and leaves LOD and window functions as TODO.

Where the converter does better:

- Composite join keys.
- `sql_view` as a `SELECT` source.
- Datatypes, synonyms and `ai_context` on fields and metrics. unshackle puts synonyms
  and `ai_context` in knowledge files instead.
- Residual join predicates, which it keeps and declares.

Other findings on the converter's output:

- **Keys and relationship columns that aren't declared fields.** A join column that the
  Model doesn't surface (`CUSTOMER_ID`) is used in `primary_key` and in the
  relationship's `from_columns`/`to_columns`, but no field declares it. The spec says
  these name dataset fields. This also happens on the converter's own `minimal` and
  `tpcds` fixtures.
- **Double aggregation.** Measure formulas with no `aggregation` property get a `sum ( … )`
  wrapper even when they already aggregate. Examples: `sum ( unique_count ( … ) )`,
  `sum ( sum_if ( … ) )`, `sum ( cumulative_sum ( … ) )`, and
  `sum ( safe_divide ( [Revenue] , [Order Count] ) )`. `[Revenue]` and `[Order Count]`
  are measure columns.
- **Dropped attribute.** The formula `Order Size` only reads a column that the Model
  surfaces as a measure. It goes to model-level `custom_extensions` and isn't written as
  a field.
- **Exit status.** Row-level security rules are reported as an ERROR issue, so the CLI
  exits 1 on any Model with RLS. unshackle turns these rules into row filters.

Verdict: none of the converter's metrics run as SQL, and four of the nine metrics on the
unshackle fixture are double-aggregated. Keep ours. ThoughtSpot's two-word
`unique count ( … )`, `nullif`, composite join keys and `sql_view` sources are gaps in
unshackle's ThoughtSpot parser to close.

## Spec and validator notes

`validation/validate.py` at apache/ossie HEAD passes documents that break these spec
rules:

- `primary_key`, `unique_keys`, `from_columns` and `to_columns` naming something that
  isn't a declared field of the dataset.
- Model-scoped metrics that reference other metrics or unknown names. Only
  dataset-scoped metrics are checked (`validate_metric_scoping`).

unshackle's own tests (`tests/test_ossie.py`) check the first rule on top of the schema.

## When to revisit

Reopen this when both of these are true:

- The converters (and `apache-ossie`) are on PyPI.
- For the tool in question, the issues above are fixed. That means filtered measures and
  metric references for Omni; physical column names and field references for Sigma; and
  for ThoughtSpot, SQL output for metrics, declared key fields and the default
  aggregation.

At that point the integration is small. It calls the converter at build time on the raw
files that `extract()` already saves, behind an optional extra, and falls back to
unshackle's parser with a report warning. Dashboards, queries and permissions stay with
unshackle either way: the converters cover the semantic model only.
