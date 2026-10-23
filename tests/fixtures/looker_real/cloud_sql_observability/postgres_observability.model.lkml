connection: "@{POSTGRES_CONNECTION_NAME}"

label: "Cloud SQL Observability (PostgreSQL)"

# Include all refined views (which include raw views)
include: "/views/refined/*.view.lkml"

# Include modular explores
include: "/explores/*.explore.lkml"

# Include dedicated PostgreSQL dashboard only
include: "/dashboards/postgres_observability.dashboard.lookml"

# Include unit tests
include: "/tests/*.test.lkml"

# Synchronized hourly caching policy for PostgreSQL telemetry
datagroup: hourly_telemetry_datagroup {
  sql_trigger: SELECT EXTRACT(HOUR FROM CURRENT_TIMESTAMP) ;;
  max_cache_age: "1 hour"
}

persist_with: hourly_telemetry_datagroup
