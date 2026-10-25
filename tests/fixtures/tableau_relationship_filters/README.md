# Filters on a data source with relationships

Hand-written, in the layout `unshackle extract tableau` writes, over the fictional Dunder
Mifflin data (`tests/data/dunder_mifflin/`). Used by `tests/test_tableau_relationship_filters.py`.

- `workbooks/*.twb`: workbook "Dunder Mifflin Relationship Filters" with three inline data
  sources, each the data source of `tests/fixtures/tableau_relationships/` (logical tables
  `orders`, `accounts`, `shippings`, custom SQL `targets`) plus a custom SQL table `tiers`
  related on `orders.status = tiers.tier_status`, whose keys repeat (COMPLETED has tiers A
  and B). The second data source has a data source filter on `payment_cycle` (ANNUAL, WEEK),
  the third on `tier` (B). The sheets filter on fields of every table: lists, exclusions
  (NULL kept), NULL only, ranges, a relative date, a wildcard, measure filters on a measure
  of another table (shown or not), top N and bottom N by another table's measure, conditions,
  a top N on a field that isn't on the sheet, and a context filter. One sheet (a top N in the
  context) stays TODO.
- `workbooks.json`: the REST list, with a fake LUID.
