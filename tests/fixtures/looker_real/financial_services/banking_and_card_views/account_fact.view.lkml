view: account_fact {
  view_label: "Account"

  derived_table: {
    explore_source: balances_fact {
      column: account_id {field: trans.account_id}
      column: last_transaction_date {field: trans.last_transaction_date}
      column: first_transaction_date {field: trans.first_transaction_date}
      column: number_of_transactions {field: trans.number_of_transactions}
      column: average_transaction_value {field: trans.average_transaction_value}
      bind_all_filters: yes
    }
  }

    dimension: account_id {
      primary_key: yes
      hidden: yes
      type: number
      sql: ${TABLE}.account_id ;;
    }

    dimension: number_of_transactions {
      label: "Number of Transactions"
      type: number
      sql: ${TABLE}.number_of_transactions ;;
    }

    dimension_group: first_transaction {
      type: time
      timeframes: [date,raw,day_of_week,day_of_month,day_of_year, week, year, month, month_name]
      sql: ${TABLE}.first_transaction_date ;;
    }

    dimension_group: last_transaction {
      type: time
      timeframes: [date,raw,day_of_week,day_of_month,day_of_year, week, year, month, month_name]
      sql: ${TABLE}.last_transaction_date;;
    }

    dimension: days_of_activity {
      label: "Days of Activity"
      type: duration_day
      sql_start: ${first_transaction_raw} ;;
      sql_end: ${last_transaction_raw} ;;
    }

    measure: average_number_transactions {
      label: "Average Number of Transactions"
      type: average
      sql: ${number_of_transactions} ;;
    }

}
