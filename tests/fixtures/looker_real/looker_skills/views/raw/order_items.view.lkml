view: order_items {
  sql_table_name: @{DATASET_NAME}.order_items ;;

  # --------------------------------------------------------------------------
  # Raw Dimensions (1:1 Table Column Mapping)
  # --------------------------------------------------------------------------

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: order_id {
    type: number
    sql: ${TABLE}.order_id ;;
  }

  dimension: sale_price {
    type: number
    sql: ${TABLE}.sale_price ;;
  }

  dimension: inventory_item_id {
    type: number
    sql: ${TABLE}.inventory_item_id ;;
  }

  dimension_group: returned {
    type: time
    timeframes: [raw, time, date]
    sql: ${TABLE}.returned_at ;;
  }

  dimension: phone {
    type: string
    sql: ${TABLE}.phone ;;
  }

  dimension: phones {
    type: string
    sql: ${TABLE}.phones ;;
  }
}
