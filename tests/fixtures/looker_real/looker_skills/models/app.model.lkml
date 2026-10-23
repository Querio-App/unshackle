connection: "@{CONNECTION_NAME}"

# Include only the explore file
include: "/explores/orders.explore.lkml"

# Datagroup referenced in orders.explore.lkml
datagroup: daily_etl_sync {
  sql_trigger: SELECT last_modified_time FROM `@{DATASET_NAME}.__TABLES__` WHERE table_id = 'orders' ;;
  max_cache_age: "24 hours"
}

persist_with: daily_etl_sync

# Access grant referenced in users_rfn.view.lkml (which is included in orders.explore.lkml)
access_grant: can_view_pii {
  user_attribute: department
  allowed_values: ["hr", "compliance"]
}
