include: "/models/**/thelook.model.lkml"
view: order_facts {
  view_label: "Order Facts"
  derived_table: {
    explore_source: order_items {
      column: order_id {field: order_items.order_id_no_actions }
      column: items_in_order { field: order_items.count }
      column: order_amount { field: order_items.total_sale_price }
      column: order_cost { field: inventory_items.total_cost }
      column: user_id {field: order_items.user_id }
      column: created_at {field: order_items.created_raw}
      column: order_gross_margin {field: order_items.total_gross_margin}
      derived_column: order_sequence_number {
        sql: RANK() OVER (PARTITION BY user_id ORDER BY created_at) ;;
      }
    }
    datagroup_trigger: ecommerce_etl_modified
  }

  dimension: order_id {
    label: "Order ID"
    type: number
    hidden: yes
    primary_key: yes
    sql: ${TABLE}.order_id ;;
  }

  dimension: items_in_order {
    label: "Items in Order"
    type: number
    sql: ${TABLE}.items_in_order ;;
  }

  dimension: order_amount {
    label: "Order Amount"
    type: number
    value_format_name: usd
    sql: ${TABLE}.order_amount ;;
  }

  dimension: order_cost {
    label: "Order Cost"
    type: number
    value_format_name: usd
    sql: ${TABLE}.order_cost ;;
  }

  dimension: order_gross_margin {
    label: "Order Gross Margin"
    type: number
    value_format_name: usd
  }

  dimension: order_sequence_number {
    label: "Order Sequence Number"
    type: number
    sql: ${TABLE}.order_sequence_number ;;
  }

  dimension: is_first_purchase {
    label: "Is First Purchase"
    type: yesno
    sql: ${order_sequence_number} = 1 ;;
  }
}