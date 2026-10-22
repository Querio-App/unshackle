connection: "snowflake_prod"
label: "E-commerce"

include: "/*.view.lkml"

datagroup: nightly {
  sql_trigger: SELECT CURRENT_DATE ;;
  max_cache_age: "24 hours"
}

access_grant: can_see_finance {
  user_attribute: department
  allowed_values: ["finance", "executive"]
}

explore: orders {
  label: "Orders"
  description: "Every order placed on the web store, with the customer who placed it. Use for revenue and order volume."
  required_access_grants: [can_see_finance]

  access_filter: {
    field: customers.region
    user_attribute: sales_region
  }

  join: customers {
    type: left_outer
    sql_on: ${orders.customer_id} = ${customers.id} ;;
    relationship: many_to_one
  }

  join: customer_lifetime {
    type: left_outer
    sql_on: ${customers.id} = ${customer_lifetime.customer_id} ;;
    relationship: one_to_one
  }
}

explore: customers {
  label: "Customers"
  description: "One row per customer account. Test accounts are always excluded."
  sql_always_where: ${customers.is_test} = false ;;
}
