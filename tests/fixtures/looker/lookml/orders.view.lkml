view: orders {
  sql_table_name: analytics.fct_orders ;;

  parameter: currency {
    type: unquoted
    allowed_value: { label: "USD" value: "usd" }
    allowed_value: { label: "EUR" value: "eur" }
  }

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.order_id ;;
  }

  dimension: customer_id {
    type: number
    hidden: yes
    sql: ${TABLE}.customer_id ;;
  }

  dimension: status {
    type: string
    description: "placed, shipped, complete or returned"
    sql: ${TABLE}.status ;;
  }

  dimension: channel {
    type: string
    sql: COALESCE(${TABLE}.channel, 'web') ;;
  }

  dimension: amount {
    type: number
    label: "Order amount (USD)"
    sql: ${TABLE}.amount_usd ;;
    value_format_name: usd
  }

  dimension: is_large_order {
    type: yesno
    sql: ${amount} >= 500 ;;
  }

  dimension: size_bucket {
    case: {
      when: {
        sql: ${amount} < 50 ;;
        label: "small"
      }
      when: {
        sql: ${amount} < 500 ;;
        label: "medium"
      }
      else: "large"
    }
  }

  dimension: amount_in_currency {
    type: number
    sql: {% if currency._parameter_value == 'eur' %} ${amount} * 0.92 {% else %} ${amount} {% endif %} ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month, quarter, year]
    sql: ${TABLE}.created_at ;;
  }

  measure: order_count {
    type: count
    drill_fields: [id, status, created_date]
  }

  measure: total_revenue {
    type: sum
    sql: ${amount} ;;
    value_format_name: usd
  }

  measure: completed_revenue {
    type: sum
    sql: ${amount} ;;
    filters: [status: "complete"]
  }

  measure: refunded_revenue {
    type: sum
    sql: ${amount} ;;
    filters: [status: "returned"]
  }

  measure: large_orders_outside_eu {
    type: count
    filters: [is_large_order: "yes", customers.region: "-EU"]
  }

  measure: customer_count {
    type: count_distinct
    sql: ${customer_id} ;;
  }

  measure: average_order_value {
    type: number
    sql: ${total_revenue} / NULLIF(${order_count}, 0) ;;
    value_format_name: usd
  }

  measure: revenue_per_customer {
    type: number
    sql: 1.0 * ${total_revenue} / NULLIF(${customer_count}, 0) ;;
  }

  measure: net_revenue {
    type: number
    sql: ${total_revenue} - ${refunded_revenue} ;;
  }

  measure: median_order_value {
    type: median
    sql: ${amount} ;;
  }

  measure: p90_order_value {
    type: percentile
    percentile: 90
    sql: ${amount} ;;
  }

  measure: revenue_in_currency {
    type: sum
    sql: {% if currency._parameter_value == 'eur' %} ${amount} * 0.92 {% else %} ${amount} {% endif %} ;;
  }

  measure: recent_revenue {
    type: sum
    sql: ${amount} ;;
    filters: [created_date: "30 days"]
  }
}
