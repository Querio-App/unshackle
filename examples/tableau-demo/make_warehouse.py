"""Build a small DuckDB 'warehouse' that matches the Tableau fixture workbook.

The numbers are chosen so the migrated Sales Trend tile reproduces the Tableau
view export in tests/fixtures/tableau/views/*.csv exactly.
"""

import sys

import duckdb

path = sys.argv[1] if len(sys.argv) > 1 else "warehouse.duckdb"
con = duckdb.connect(path)
con.execute("create schema if not exists analytics")
con.execute("create schema if not exists finance")
con.execute(
    """
    create or replace table analytics.orders as
    select * from (values
      (1, 'O-1', date '2025-01-05', 'Ada Lovelace',  'New York',   'East',    'Chuck', 'Furniture',  700.50, 2, 0.0, 120.0),
      (2, 'O-2', date '2025-01-19', 'Alan Turing',   'California', 'West',    'Dana',  'Furniture',  500.00, 1, 0.1,  80.0),
      (3, 'O-3', date '2025-02-02', 'Grace Hopper',  'Oregon',     'West',    'Dana',  'Furniture',  980.00, 3, 0.0, 150.0),
      (4, 'O-4', date '2025-01-11', 'Ada Lovelace',  'New York',   'East',    'Chuck', 'Technology', 2200.00, 4, 0.0, 600.0),
      (5, 'O-5', date '2025-02-14', 'Katherine J.',  'Maine',      'East',    'Chuck', 'Technology', 1875.25, 2, 0.2, 310.0),
      (6, 'O-6', date '2025-01-22', 'Edsger D.',     'Texas',      'Central', 'Sam',   'Technology', 9999.00, 9, 0.0, 999.0)
    ) t(row_id, order_id, order_date, customer_name, state, region, regional_manager, category, sales, quantity, discount, profit)
    """
)
con.execute(
    """
    create or replace table analytics.returns as
    select * from (values ('O-3', 'Yes'), ('O-5', 'Yes')) t(order_id, returned)
    """
)
con.execute(
    """
    create or replace table finance.sales_targets as
    select * from (values
      ('East', date '2025-01-01', 3000.0), ('West', date '2025-01-01', 1500.0)
    ) t(region, target_month, target_usd)
    """
)
con.close()
print(f"warehouse ready: {path}")
