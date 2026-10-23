include: "/views/refined/orders_rfn.view.lkml"
include: "/views/refined/users_rfn.view.lkml"
include: "/views/refined/products_rfn.view.lkml"
include: "/views/refined/order_status_summary.view.lkml"
include: "/views/refined/user_order_facts.view.lkml"
include: "/views/refined/order_items_rfn.view.lkml"

explore: orders {
  label: "Orders"
  description: "Use this Explore to analyze orders details, customer information, and products."
  view_name: orders

  fields: [
    orders.allowed_fields*,
    users.allowed_fields*,
    order_items.allowed_fields*,
    products.allowed_fields*,
    user_order_facts.allowed_fields*,
    order_status_summary.allowed_fields*,
    -users.password_hash
  ]

  join: order_items {
    type: left_outer
    relationship: one_to_many
    sql_on: ${orders.id} = ${order_items.order_id} ;;
  }

  join: users {
    type: left_outer
    relationship: many_to_one
    sql_on: ${orders.user_id} = ${users.id} ;;
  }

  join: user_order_facts {
    type: left_outer
    relationship: one_to_one
    sql_on: ${orders.user_id} = ${user_order_facts.user_id} ;;
  }

  join: products {
    type: left_outer
    relationship: many_to_one
    sql_on: ${orders.product_id} = ${products.id} ;;
  }

  join: order_status_summary {
    type: left_outer
    relationship: one_to_one
    sql_on: ${orders.status} = ${order_status_summary.status} ;;
  }

  aggregate_table: monthly_sales_rollup {
    query: {
      dimensions: [created_month, status]
      measures: [total_revenue, count]
    }
    materialization: {
      datagroup_trigger: daily_etl_sync
    }
  }
}
