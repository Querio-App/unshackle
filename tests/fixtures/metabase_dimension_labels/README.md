# Duplicate dimension labels

Minimal synthetic reproduction of issue #170, reported with Metabase v0.65.3's
Sample Database Orders model joining People and Products. The result metadata
labels both `user_id` and `name` as `User ID`, and both `product_id` and `title`
as `Product ID`. Only fields needed to reproduce the shared writer bug are kept;
there are no customer records or instance identities.

Before the fix, dbt-core 1.12.6 with dbt-duckdb 1.11.1 rejects the generated
MetricFlow YAML: dimension labels must be unique within a semantic model.
