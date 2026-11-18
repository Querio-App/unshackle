"""Static import fixture. Do not execute this module."""
raise RuntimeError("SQL import must not execute Python")

monthly_revenue = (
    "SELECT substr(CAST(ordered_at AS VARCHAR), 1, 7) AS month, "
    "SUM(amount) AS revenue FROM orders WHERE status = 'paid' "
    "GROUP BY 1 ORDER BY 1"
)
# Deliberately uncertain: expected to remain a source-preserving TODO.
dynamic_query = f"SELECT * FROM {table_name}"
