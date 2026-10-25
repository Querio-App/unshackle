# A workbook on a published data source

Hand-written, in the layout `unshackle extract tableau` writes, over the fictional Dunder
Mifflin data (`tests/data/dunder_mifflin/`). Used by `tests/test_tableau_published.py`.

- `datasources/*.tds`: published data source "Dunder Mifflin Orders": a Postgres connection
  with an extract, orders inner join accounts and products, calculated fields (a ratio, an
  `IF`, `DATETRUNC`, a `FIXED` LOD, a user filter with `ISMEMBEROF` and `USERNAME` used as a
  data source filter, a plain `SUM`), a hierarchy and a default aggregation (`Avg`).
- `workbooks/*.twb`: workbook "Dunder Mifflin Sales" on that data source through a
  `sqlproxy` connection, as Tableau writes workbooks on published data sources: a bar with
  a filter, a line by month, a KPI, a table filtered through a parameter, and a dashboard.
- `views/*.csv`: each view's column names as Tableau's view data names them (no values:
  the tests compute the expected numbers).
- `permissions/`, `groups.json` and the REST lists (`workbooks.json`, `views.json`,
  `datasources.json`), with fake LUIDs and `example.*` names.
