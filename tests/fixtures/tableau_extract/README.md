# Data sources whose data lives only in Tableau

Hand-written, in the layout `unshackle extract tableau` writes, over the fictional Dunder
Mifflin data (`tests/data/dunder_mifflin/`). Used by `tests/test_tableau_extract_data.py`,
which builds the data files Tableau would package (a `.hyper` with Tableau's Hyper API, an
`.xlsx`, a `.csv`) from the Dunder Mifflin data in the test; none are committed.

- `datasources/…81.tds`: published data source "Order Lines" on a Hyper file
  (`class='hyper'`, table `[Extract].[Extract]`): orders with their product's paper type.
- `datasources/…82.tds`: published data source "Order History" on an application connector
  (`class='salesforce'`, table `Order__c`) that Tableau answers from its extract
  (`<extract enabled='true'>`, same rows, columns named as in Tableau).
- `workbooks/…83.twb`: workbook "Extract Sales" on both (through `sqlproxy` connections),
  with its own Excel data source "Accounts" (`excel-direct`, sheet `Accounts$`) and CSV data
  source "Products" (`textscan`, `products.csv`), and one sheet per data source.
- The REST lists (`workbooks.json`, `datasources.json`), with fake LUIDs and `example.*` names.
