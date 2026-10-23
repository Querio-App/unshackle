# Real-world LookML fixtures

Open-source LookML projects used by `tests/test_looker_real.py`. Each folder holds
unmodified copies of the project's `.lkml` files (including `manifest.lkml`), at
most one `.dashboard.lookml`, and the project's `LICENSE`. Nothing else is kept
(no README, images, JavaScript, localization JSON or `.git`). Where a folder is a
subset of its project, the note says which files were kept; kept files are not
edited.

| Folder | Source | Commit | License | What it exercises |
| --- | --- | --- | --- | --- |
| `thelook_event` | [looker-open-source/thelookevent](https://github.com/looker-open-source/thelookevent) | `611d788ef7f96b7659ab78c72898c8ac81e5ab5c` | MIT | view `extends` (`kitten_order_items`, `order_items_cohorts`), explore `extends`, explore refinements (`explore: +order_items` in `queries/`), SQL and native (`explore_source`) derived tables, PDTs with `datagroup_trigger` and `materialized_view`, `sql_create` (BigQuery ML), `aggregate_table`, `percent_of_total`, `list`, `tier`, `location`, Liquid parameters and `{% condition %}`, measure filters, `from:` join aliases, `localization_settings`. All `.lkml` files. |
| `cortex_sap` | [looker-open-source/block-cortex-sap-v2](https://github.com/looker-open-source/block-cortex-sap-v2) | `1eeff9ee5fbd3c5a2128049c19a171c560e72427` | MIT | manifest constants (`@{GCP_PROJECT_ID}` in `sql_table_name`, constants holding Liquid), refinements that add `extends`, `extension: required`, explores in `explores/*.explore.lkml` included by the model, `sum_distinct` with `sql_distinct_key`, `list`, `parameter` / `_parameter_value`, `_user_attributes`, `always_join`, multi-condition `sql_on`. Subset: the model, manifest, `components/`, the Balance Sheet and Income Statement explores and the views they use, one dashboard. |
| `looker_skills` | [looker-open-source/looker-skills](https://github.com/looker-open-source/looker-skills) | `ce5ad43e76d57affddb8b54e472b6408dad352ca` | Apache-2.0 | `access_grant` + `required_access_grants` on fields, raw/refined view layering, explore refinements in `.test.lkml` files, two models including the same explore file, constants, period-over-period Liquid. Subset: the project root (the example project under `skills/` is left out). |
| `financial_services` | [looker-open-source/financial_services_demo](https://github.com/looker-open-source/financial_services_demo) | `9a161d6c6be11528d492c47865acca6d2f8c8a32` | MIT | `percentile`, `sum_distinct`, `dimension_group: duration`, `sql_trigger_value` and `persist_for` PDTs, a primary key built from a timeframe, `tier`, `distance`, `zipcode`, many models. All `.lkml` files. |
| `retail` | [looker-open-source/retail_demo](https://github.com/looker-open-source/retail_demo) | `6280454cc165a6c2afc453402c9ae00fa488b08b` | MIT | `access_filter` through a join to a derived-table view, `sql_always_where` with Liquid, `{% condition %}`, `_filters`, `partition_keys` / `cluster_keys`, nested (UNNEST) joins via `sql:`, cross joins, `NOT NULL` filters. All `.lkml` files. |
| `cloud_sql_observability` | [looker-open-source/block-intelligence-cloud-sql](https://github.com/looker-open-source/block-intelligence-cloud-sql) | `55720e30554c61ba87c6a0990150b499cd4489a4` | Apache-2.0 | `sql_table_name` that is a Liquid `{% if _dialect._name %}` switch between subqueries, a PDT built with `create_process`, two files defining the same view, one explore file included by three models, LookML tests. All `.lkml` files. |

The licenses are in each folder's `LICENSE`. The Apache-2.0 projects ship no
`NOTICE` file.
