connection: "@{MYSQL_CONNECTION_NAME}"

label: "Cloud SQL Observability (MySQL)"

# Include all refined views (which include raw views)
include: "/views/refined/*.view.lkml"

# Include modular explores
include: "/explores/*.explore.lkml"

# Include dedicated MySQL dashboard only
include: "/dashboards/mysql_observability.dashboard.lookml"

# Include unit tests
include: "/tests/*.test.lkml"

# Synchronized hourly caching policy for MySQL telemetry
datagroup: hourly_telemetry_datagroup {
  sql_trigger: SELECT EXTRACT(HOUR FROM CURRENT_TIMESTAMP) ;;
  max_cache_age: "1 hour"
}

persist_with: hourly_telemetry_datagroup
