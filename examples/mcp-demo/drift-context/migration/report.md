# Migration report

| Tool | Tables | Semantic models | Metrics | Dashboards | Queries | Knowledge | Permissions | Row filters |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dbt | 2 | 1 | 1 | 0 | 2 | 0 | 0 | 0 |
| sql | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| warehouse | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## This run

- 3 files written, 0 unchanged.

## Dashboards (3)

| Tool | Dashboard | Tiles | With SQL | Top blockers |
| --- | --- | --- | --- | --- |
| sql | python/monthly.py | 2 | 1 | other (1) |
| sql | sql/regions.sql | 1 | 1 |  |
| sql | sql/revenue.sql | 1 | 1 |  |

Top blockers: kinds of TODO on the dashboard's tiles, with how many tiles have each.

## Still to translate (2)

| Kind | TODOs | Files |
| --- | --- | --- |
| other | 1 | 1 |
| tile without SQL | 1 | 1 |

| File | Line | What's left |
| --- | --- | --- |
| `notebooks/python-monthly-py-cd98ddc19804.ipynb` | 111 | python/monthly.py:10: dynamic Python expression; SQL cannot be extracted statically |
| `notebooks/python-monthly-py-cd98ddc19804.ipynb` | 112 | no SQL recovered for this tile; write it here and run it with pd.read_sql |

## Warnings (2)

| Tool | Severity | Item | Message |
| --- | --- | --- | --- |
| dbt | warn | datasource warehouse | warehouse type unknown, so semantic expressions are labelled ANSI_SQL; set [datasources.warehouse] type in unshackle.toml and re-run build |
| warehouse | info | datasource warehouse | warehouse type 'postgres' taken from the warehouse export; set [datasources.warehouse] type in unshackle.toml to override |
