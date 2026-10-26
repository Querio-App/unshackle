# Data sources that read Excel and CSV files

Hand-written, in the layout `unshackle extract tableau` writes. Used by
`tests/test_tableau_extract_data.py`.

A file names its table after a sheet or the file (`Orders`, `Sheet1`, `targets.csv`),
so data sources reading different files can name the same table:

- `datasources/…251.tds`: published "Regional Orders" (`class='excel-direct'`, sheet
  `Orders`): order_id, region, sales, profit, and a calculation "Profit Ratio"
  `SUM([profit])/SUM([sales])`.
- `workbooks/…252.twb`: workbook "Order Health" with
  - "Regional Orders" (through `sqlproxy`), redefining "Profit Ratio" as
    `sum([profit]) / sum([sales])`: the same formula in other case and spacing;
  - embedded "Store Orders", a sheet of another Excel file also named `Orders`, with its own
    "Profit Ratio";
  - embedded "Sales Targets" (`class='textscan'`, `targets.csv`) and "Budget"
    (`Sheet1` of an Excel file).
- The REST lists (`workbooks.json`, `datasources.json`), with fake LUIDs and `example.*` names.
