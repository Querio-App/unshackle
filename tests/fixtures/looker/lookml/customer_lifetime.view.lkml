view: customer_lifetime {
  derived_table: {
    sql:
      SELECT
        customer_id,
        COUNT(*) AS lifetime_orders,
        SUM(amount_usd) AS lifetime_revenue,
        MIN(created_at) AS first_order_at
      FROM ${orders.SQL_TABLE_NAME}
      WHERE status <> 'returned'
      GROUP BY 1 ;;
    datagroup_trigger: nightly
  }

  dimension: customer_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.customer_id ;;
  }

  dimension: lifetime_orders {
    type: number
  }

  dimension: lifetime_revenue {
    type: number
  }

  dimension_group: first_order {
    type: time
    timeframes: [date, month]
    sql: ${TABLE}.first_order_at ;;
  }

  measure: average_lifetime_revenue {
    type: average
    sql: ${lifetime_revenue} ;;
  }
}
