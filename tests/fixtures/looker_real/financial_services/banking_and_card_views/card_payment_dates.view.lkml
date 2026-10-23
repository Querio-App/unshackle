view: card_payment_dates {
  sql_table_name: `looker-private-demo.retail_banking.card_payment_dates` ;;

  dimension: card_id {
    label: "Card ID"
    type: number
    sql: ${TABLE}.card_id ;;
  }

  dimension: card_number {
    label: "Card Number"
    type: string
    sql: ${TABLE}.card_number ;;
  }

  dimension_group: payment_due_date {
    timeframes: [date, raw]
    type: time
    sql: timestamp(${TABLE}.payment_due_date);;
  }

  dimension_group: period_end {
    timeframes: [date, raw]
    type: time
    sql: timestamp(${TABLE}.period_end) ;;
  }

  dimension_group: period_start {
    timeframes: [date, raw]
    type: time
    sql: timestamp(${TABLE}.period_start) ;;
  }

#   measure: count {
#     type: count
#     drill_fields: []
#   }
}
