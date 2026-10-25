# Looker translation semantics

Hand-written LookML for `tests/test_looker_semantics.py`: views, a model and a dashboard whose tiles
each exercise one part of Looker's documented behaviour (timeframes and week start, filter
expressions, symmetric aggregates and `sum_distinct`, tiers and cases, medians and percentiles,
Liquid, nested records, quoting).

The views read small literal tables (the rows are defined in the test, under the `example` schema)
or the fictional Dunder Mifflin tables in `tests/data/dunder_mifflin/`. The tests run unshackle's SQL
in DuckDB and compute the expected values in Python from the same rows, so nothing here is output
copied from another tool.

| File | Covers |
| --- | --- |
| `shop.view.lkml` | timeframes, measure types, one-to-many joins, `sum_distinct`, Liquid in a derived table, `LEFT JOIN UNNEST` |
| `facts.view.lkml` | string, number and date filter expressions, `yesno`, `tier`, `case`, `median`, `percentile` |
| `quotes.view.lkml` | `_dialect._name`, `date_start` / `date_end`, `_is_filtered`, parameters, quoted filter values, hour timeframes |
| `dunder.view.lkml` | timeframes over three years of order dates, a fan-out on real-sized tables |
| `semantics.model.lkml` | the explores |
| `semantics.dashboard.lookml` | one tile per case |
