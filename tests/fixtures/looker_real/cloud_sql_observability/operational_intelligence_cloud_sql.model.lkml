connection: "@{MYSQL_CONNECTION_NAME}"

label: "Cloud SQL Observability (Unified)"

include: "/views/refined/*.view.lkml"
include: "/explores/*.explore.lkml"
include: "/dashboards/database_observability.dashboard.lookml"
include: "/tests/*.test.lkml"

# Synchronized hourly caching policy for Unified telemetry
datagroup: hourly_telemetry_datagroup {
  sql_trigger: SELECT EXTRACT(HOUR FROM CURRENT_TIMESTAMP) ;;
  max_cache_age: "1 hour"
}

persist_with: hourly_telemetry_datagroup
