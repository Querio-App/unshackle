include: "/explores/orders.explore.lkml"

view: +orders {
  dimension_group: test_created {
    type: time
    timeframes: [raw, time]
    sql: ${TABLE}.created_at ;;
  }
}

explore: +orders {
  fields: [
    orders.allowed_fields*,
    users.allowed_fields*,
    order_items.allowed_fields*,
    products.allowed_fields*,
    user_order_facts.allowed_fields*,
    order_status_summary.allowed_fields*,
    orders.test_created_time,
    -users.password_hash
  ]
}

test: orders_pk_is_unique {
  explore_source: orders {
    column: id { field: orders.id }
    column: orders_count { field: orders.orders_count }
    
    filters: {
      field: orders.test_created_time
      value: "7 days"
    }
  }

  assert: pk_uniqueness_verified {
    expression: ${orders.orders_count} = 1 ;;
  }
}

test: margin_less_than_revenue {
  explore_source: orders {
    column: total_margin {}
    column: total_revenue {}
  }

  assert: margin_is_valid {
    expression: ${orders.total_margin} <= ${orders.total_revenue} ;;
  }
}

test: user_order_facts_dimensions {
  explore_source: orders {
    column: user_id { field: user_order_facts.user_id }
    column: lifetime_order_count { field: user_order_facts.lifetime_order_count }
    column: total_revenue { field: user_order_facts.total_revenue }
    limit: 5
  }

  assert: user_count_is_valid {
    expression: ${user_order_facts.lifetime_order_count} >= 0 ;;
  }
}

test: dynamic_timeframe_month_selector {
  explore_source: orders {
    column: created_month { field: orders.created_month }
    column: dynamic_timeframe { field: orders.dynamic_timeframe }
    column: order_item_id { field: order_items.id }
    column: product_id { field: products.id }
    filters: {
      field: orders.timeframe_selector
      value: "month"
    }
    limit: 5
  }

  assert: month_matches_dynamic {
    expression: ${orders.dynamic_timeframe} = concat(${orders.created_month}, "") ;;
  }
}

test: dynamic_timeframe_year_selector {
  explore_source: orders {
    column: created_year { field: orders.created_year }
    column: dynamic_timeframe { field: orders.dynamic_timeframe }
    column: order_item_id { field: order_items.id }
    column: product_id { field: products.id }
    filters: {
      field: orders.timeframe_selector
      value: "year"
    }
    limit: 5
  }

  assert: year_matches_dynamic {
    expression: ${orders.dynamic_timeframe} = concat(${orders.created_year}, "") ;;
  }
}

test: order_status_summary_filtering {
  explore_source: orders {
    column: status { field: order_status_summary.status }
    column: status_count { field: order_status_summary.status_count }
    filters: {
      field: orders.status_filter
      value: "Complete"
    }
    filters: {
      field: order_status_summary.status
      value: "-NULL"
    }
    limit: 1
  }

  assert: status_is_filtered_to_complete {
    expression: ${order_status_summary.status} = "Complete" ;;
  }
}



