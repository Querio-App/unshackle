connection: "warehouse"
include: "/*.view.lkml"

explore: events {}

explore: shop_orders {
  join: shop_items {
    type: left_outer
    sql_on: ${shop_orders.id} = ${shop_items.order_id} ;;
    relationship: one_to_many
  }
}

explore: shop_items {}

explore: pick {}

explore: baskets {
  join: basket_lines {
    sql: LEFT JOIN UNNEST(${baskets.lines}) AS basket_lines ;;
    relationship: one_to_many
  }
}

explore: facts {}

explore: quotes {}

explore: dm_accounts {
  join: dm_orders {
    type: left_outer
    sql_on: ${dm_accounts.account_id} = ${dm_orders.account_id} ;;
    relationship: one_to_many
  }
}

explore: dm_orders {}
