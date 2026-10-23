view: card_payments {
  sql_table_name: `looker-private-demo.retail_banking.card_payment_amounts` ;;

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

  dimension_group: payment_completed{
    type: time
    timeframes: [date, raw]
    sql: timestamp(${TABLE}.payment_completed_date) ;;
  }

#   dimension: payment_due_date {
#     type: string
#     sql: ${TABLE}.payment_due_date ;;
#   }
#
#   dimension: payment_type {
#     type: number
#     sql: ${TABLE}.payment_type ;;
#   }

#   dimension: period_end {
#     type: string
#     sql: ${TABLE}.period_end ;;
#   }

  dimension_group: period_start {
    type: time
    timeframes: [date, raw]
    sql: timestamp(${TABLE}.period_start) ;;
  }

  dimension: transactions_total {
    label: "Transactions Total"
    type: number
    sql: ${TABLE}.period_transactions_total ;;
  }

  dimension: balance_remaining_last_payment {
    label: "Balance Remaining Last Payment"
    type: number
    sql: ${TABLE}.remaining_balance_last_period ;;
    value_format_name: usd
  }

  dimension: balance_remaining_this_payment {
    label: "Balance Remaining This Payment"
    type: number
    sql: ${TABLE}.remaining_balance_this_period ;;
    value_format_name: usd
  }

  dimension: amount_owed {
    label: "Amount Owed"
    type: number
    sql: ${TABLE}.total_amount_owed ;;
    value_format_name: usd
  }

  dimension: amount_paid {
    label: "Amount Paid"
    type: number
    value_format_name: usd
    sql: ${TABLE}.total_amount_paid ;;
  }

  dimension: balance_percent {
    label: "Percent of Balance Paid"
    type: number
    sql: ${amount_paid} / nullif(${amount_owed},0);;
    value_format_name: percent_2
  }

  dimension: interest_applied {
    label: "Interest Applied"
    description: "Interest rates are applied on a daily basis, based on how late the payment is "
    type: number
    sql: ${TABLE}.total_interest_applied ;;
  }

  dimension: full_balance_paid {
    label: "Full Balance Paid"
    type: yesno
    sql: ${balance_percent} = 100 ;;
  }

  measure: count {
    label: "Number of Payments"
    type: count
    drill_fields: []
  }

  measure: total_interest_charged {
    label: "Total Interest Charged"
    type: sum
    sql: ${interest_applied} ;;
    value_format_name: usd
  }

  measure: average_percent_balance_paid {
    label: "Average Percent Balance Paid"
    type: average
    sql: ${balance_percent} ;;
    value_format_name: percent_2
  }

  measure: average_amount_owed {
    label: "Average Amount Owed"
    type: average
    sql: ${amount_owed} ;;
    value_format_name: usd
  }

#   measure: current_outstanding_balance {
#
#   }
}
