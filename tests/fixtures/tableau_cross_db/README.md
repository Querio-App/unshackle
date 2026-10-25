# Cross-database joins

Hand-written, in the layout `unshackle extract tableau` writes, over the fictional Dunder
Mifflin data (`tests/data/dunder_mifflin/`). Used by `tests/test_tableau_cross_db.py`.

- `workbooks/*.twb`: workbook "Dunder Mifflin Across Databases" (Tableau 2023.1 format) with
  four inline data sources:
  - "Orders and CRM Accounts": `orders` from the `sales` database on `warehouse.example.com`
    left joined to `accounts` from the `crm` database on `crm.example.com` (two named
    connections, a cross-database join), plus a FIXED LOD over the joined rows;
  - "Orders and Accounts": the same join with both tables in one connection;
  - "Related across Databases": `orders` and `accounts` from the two databases related by a
    relationship (`<object-graph>`) instead of a join.

  Sheets show revenue by order status (one table) and by account business size (both tables)
  for each data source, and "Related in one Database" (both tables in one connection) counts
  the rows of each logical table ("<table> (Count)") by order status.
- `workbooks.json`: the REST list, with a fake LUID.
