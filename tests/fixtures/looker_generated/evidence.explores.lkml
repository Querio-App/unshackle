# Paste at the bottom of the model file, next to an include of the evidence view file.

explore: ev_time {}

explore: ev_orders {
  join: ev_items {
    type: left_outer
    sql_on: ${ev_orders.id} = ${ev_items.order_id} ;;
    relationship: one_to_many
  }
}

explore: ev_items {}

explore: ev_liquid {}

explore: ev_nested {
  join: ev_nested_lines {
    sql: LEFT JOIN UNNEST(${ev_nested.lines}) AS ev_nested_lines ;;
    relationship: one_to_many
  }
}
