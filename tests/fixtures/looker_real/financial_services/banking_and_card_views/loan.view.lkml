view: loan {
  sql_table_name: `looker-private-demo.retail_banking.loan` ;;
  drill_fields: [loan_id]

  dimension: loan_id {
    label: "Loan ID"
    primary_key: yes
    type: number
    sql: ${TABLE}.loan_id ;;
    value_format_name: id
    action: {
      label: "Send Financial Advice Letter"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://sendgrid.com/favicon.ico"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
    }
    action: {
      label: "Send Warning Letter"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://sendgrid.com/favicon.ico"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
    }
  }

  dimension: account_id {
    type: number
    hidden: yes
    sql: ${TABLE}.account_id ;;
    value_format_name: id

#     link: {
#       label: "Customer Account Dashboard"
#       url: "http://pebl.dev.looker.com/dashboards/1?Account%20ID={{ value | encode_uri }}"
#       icon_url: "http://www.looker.com/favicon.ico"
#     }
  }

  dimension: amount {
    label: "Amount"
    type: number
    sql: ${TABLE}.amount ;;
    value_format_name: usd_0
  }

  dimension_group: grant {
    description: "Date when loan was granted"
    type: time
#     datatype: yyyymmdd
    timeframes: [date, day_of_month, day_of_year, week, month, month_name, quarter, quarter_of_year, year, raw]
    sql: timestamp(${TABLE}.date);;
  }

  dimension: days_between_account_signup {
    label: "Days Between Account Signup"
    description: "The days since they created a brokerage account, until they signed up for the credit card"
    type: duration_day
    sql_start: ${account.create_raw} ;;
    sql_end: ${grant_raw} ;;
  }

  dimension: duration {
    label: "Duration"
    description: "Loan duration in months"
    type: number
    sql: ${TABLE}.duration ;;
  }

  dimension: payments {
    label: "Payments"
    description: "Monthly Payments"
    type: number
    sql: ${TABLE}.payments ;;
    value_format: "\€ ##0 \" per month\""
  }

  dimension: status {
    label: "Status"
    type: string
#     sql: ${TABLE}.status ;;
    case: {
      when: {
        label: "Finished, Paid"
        sql: ${TABLE}.status = 'A' ;;
      }
      when: {
        label: "Finished, Unpaid"
        sql: ${TABLE}.status = 'B' ;;
      }
      when: {
        label: "Running, OK"
        sql: ${TABLE}.status = 'C' ;;
      }
      when: {
        label: "Running, In Debt"
        sql: ${TABLE}.status = 'D' ;;
      }
      else: "Unknown"
    }
  }

  measure: number_of_loans {
    label: "Number of Loans"
    type: count
    drill_fields: [loan_id, account.account_id, grant_month, duration, payments, status, amount]
  }

  measure: total_loan_value {
    label: "Total Loan Value"
    type: sum
    sql: ${amount} ;;
    value_format_name: usd_0
  }

  measure: average_loan_amount {
    label: "Average Loan Amount"
    type: average
    sql: ${amount} ;;
  }

  measure: total_number_of_payments {
    label: "Total Number of Payments"
    type: sum
    sql: ${payments} ;;
  }

  measure: percent_paid {
    label: "Percent Paid"
    type: number
    sql: 1.0 * ${total_number_of_payments}/nullif(${total_loan_value},0) ;;
    value_format_name: percent_2
  }

  measure: average_days_between_signup {
    label: "Average Days Between Signup"
    type: average
    value_format_name:  decimal_2
    sql: ${days_between_account_signup} ;;
  }
}
