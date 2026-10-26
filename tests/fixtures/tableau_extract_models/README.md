# Two published data sources that are extracts

Hand-written, in the layout `unshackle extract tableau` writes. Used by
`tests/test_tableau_extract_data.py`.

Every Tableau extract stores its rows in a table `[Extract].[Extract]`, so both data
sources name the same table:

- `datasources/…241.tds`: "Order Lines" (`class='hyper'`): order_id, purchased_at,
  quantity, post_discount_revenue, and a calculation "Profit Ratio".
- `datasources/…242.tds`: "Product Catalog" (`class='hyper'`): product_id, paper_type,
  price, and a calculation also named "Profit Ratio" with another formula.
- `workbooks/…243.twb`: workbook "Order Review" on "Order Lines" (through `sqlproxy`).
- The REST lists (`workbooks.json`, `datasources.json`), with fake LUIDs and `example.*` names.
