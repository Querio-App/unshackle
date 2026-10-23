view: repeat_purchase_facts {
  view_label: "Repeat Purchase Facts"
  derived_table: {
    datagroup_trigger: ecommerce_etl_modified
    sql: SELECT
      order_items.order_id as order_id
      , order_items.created_at
      , COUNT(DISTINCT repeat_order_items.id) AS number_subsequent_orders
      , MIN(repeat_order_items.created_at) AS next_order_date
      , MIN(repeat_order_items.order_id) AS next_order_id
    FROM looker-private-demo.ecomm.order_items as order_items
    LEFT JOIN looker-private-demo.ecomm.order_items repeat_order_items
      ON order_items.user_id = repeat_order_items.user_id
      AND order_items.created_at < repeat_order_items.created_at
    GROUP BY 1, 2
     ;;
    }

   dimension: order_id {
    label: "Order ID"
    type: number
    hidden: yes
    primary_key: yes
    sql: ${TABLE}.order_id ;;
  }

  dimension: next_order_id {
    label: "Next Order ID"
    type: number
    hidden: yes
    sql: ${TABLE}.next_order_id ;;
  }

  dimension: has_subsequent_order {
    label: "Has Subsequent Order"
    type: yesno
    sql: ${next_order_id} > 0 ;;
  }

  dimension: number_subsequent_orders {
    label: "Number Subsequent Orders"
    type: number
    sql: ${TABLE}.number_subsequent_orders ;;
  }

  dimension_group: next_order {
    label: "Next Order"
    type: time
    timeframes: [raw, date]
    hidden: yes
    sql: CAST(${TABLE}.next_order_date AS TIMESTAMP) ;;
  }
}