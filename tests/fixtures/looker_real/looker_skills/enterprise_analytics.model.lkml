# Connection constant from manifest
connection: "@{CONNECTION_NAME}"

# Include all refined views (which automatically include their raw views)
include: "/views/refined/*.view.lkml"

# Include modular explores, tests, and dashboards
include: "/explores/*.explore.lkml"
include: "/tests/*.test.lkml"
include: "/dashboards/*.dashboard.lookml"

# --------------------------------------------------------------------------
# Centralized Caching Caching (Datagroups)
# --------------------------------------------------------------------------
datagroup: daily_etl_sync {
  sql_trigger: SELECT last_modified_time FROM `@{DATASET_NAME}.__TABLES__` WHERE table_id = 'orders' ;;
  max_cache_age: "24 hours"
}

persist_with: daily_etl_sync

access_grant: can_view_pii {
  user_attribute: department
  allowed_values: ["hr", "compliance"]
}
