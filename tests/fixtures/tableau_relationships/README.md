# A data source with relationships

Hand-written, in the layout `unshackle extract tableau` writes, over the fictional Dunder
Mifflin data (`tests/data/dunder_mifflin/`). Used by `tests/test_tableau_relationships.py`.

- `workbooks/*.twb`: workbook "Dunder Mifflin Relationships" with an inline data source that
  uses relationships (the logical layer, `<object-graph>`), as Tableau writes them since
  2020.2: logical tables `orders`, `accounts`, `shippings` and a custom SQL table `targets`
  (status targets: one status that no order has, and one order status with no target),
  related on `orders.account_id = accounts.account_id`, `orders.shipping_id =
  shippings.shipping_id` and `orders.status = targets.target_status`. Some orders have no
  shipping row. The sheets mix fields of one, two and three tables: a measure of the "one"
  side by a dimension of the "many" side (a join would repeat it), unmatched rows on both
  sides, two measures from different tables, dimension-only sheets, and an aggregate
  calculation over two tables.
- `workbooks.json`: the REST list, with a fake LUID.
