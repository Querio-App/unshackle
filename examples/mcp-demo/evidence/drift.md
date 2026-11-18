# Actual local Postgres rename evidence

Executed 2026-11-18 using F implementation f2da82413f9647170a05fdfaa47fb57f5e8d5c2f.
Baseline: 1d2069f, derived from integration bf97712. Rename commit: 63fa4fc.
Draft PR: https://github.com/Querio-App/unshackle/pull/207
Base branch: epic-193-drift-acceptance-base. Head: epic-193-drift-acceptance-rename.
Closes nothing. Evidence only, not intended for merge to main.

A separate local Postgres 16 fixture executed `ALTER TABLE orders RENAME COLUMN amount TO net_amount`.
The real drift CLI reran warehouse/dbt/SQL imports. Only its three generated artifact paths were staged.
Baseline refresh and second post-rename refresh both returned changed=false.
The database URL and credentials remained outside Git. This was manual workflow-equivalent execution;
no scheduled GitHub Actions run or hosted deployment was performed.

## Generated report

# Warehouse drift

Names are compared literally. Removed + added columns may represent a rename; no rename is inferred.

- column_removed: `catalog/warehouse/public/orders.yml · amount`
- column_added: `catalog/warehouse/public/orders.yml · net_amount`

## Query references

- `notebooks/python-monthly-py-cd98ddc19804.ipynb`: uncertain: source has TODO(unshackle); unresolved expressions may reference additional columns
- `notebooks/python-monthly-py-cd98ddc19804.ipynb#python_monthly_py_5`: missing column amount in catalog/warehouse/public/orders.yml
- `notebooks/python-monthly-py-cd98ddc19804.ipynb#python_monthly_py_10`: uncertain: not one static SELECT/WITH query
- `notebooks/sql-regions-sql-8613b8029aff.ipynb#sql_regions_sql_1`: missing column amount in catalog/warehouse/public/orders.yml
- `notebooks/sql-revenue-sql-3ea4e59b08ab.ipynb#sql_revenue_sql_1`: missing column amount in catalog/warehouse/public/orders.yml
- `queries/dbt-orders.sql`: missing column amount in catalog/warehouse/public/orders.yml

## Changed artifacts

- `catalog/warehouse/public/orders.yml`
- `migration/unshackle.lock`
- `migration/warehouse-schema.json`
