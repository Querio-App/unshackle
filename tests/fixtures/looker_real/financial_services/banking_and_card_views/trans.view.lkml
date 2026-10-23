view: trans {
  label: "Transactions"
  sql_table_name: `looker-private-demo.retail_banking.trans` ;;
  drill_fields: [transaction_details*]

  dimension: trans_id {
    label: "Transaction ID"
    primary_key: yes
    type: number
    sql: ${TABLE}.trans_id ;;
    value_format_name: id

    link: {
      label: "Raise investigation ticket"
      url: "https://hooks.zapier.com/hooks/catch/3658444/ouq3cnd/"
      icon_url: "http://icons.iconarchive.com/icons/paomedia/small-n-flat/96/sign-warning-icon.png"
    }

    action: {
      label: "Send this to slack channel"
      url: "https://hooks.zapier.com/hooks/catch/1662138/tvc3zj/"

      form_param: {
        name: "Message"
        type: textarea
        default: "Hey,
        Could you check out transaction #{{value}}, please.
        ~{{ _user_attributes.first_name}}"
      }

      form_param: {
        name: "Recipient"
        type: select
        default: "zevl"
        option: {
          name: "zevl"
          label: "Zev"
        }
        option: {
          name: "slackdemo"
          label: "Slack Demo User"
        }

      }

      form_param: {
        name: "Channel"
        type: select
        default: "cs"
        option: {
          name: "cs"
          label: "Customer Support"
        }
        option: {
          name: "general"
          label: "General"
        }
      }
    }
  }

  dimension: account_id {
    type: number
    hidden: yes
    sql: ${TABLE}.account_id ;;
    value_format_name: id
  }

  dimension: amount {
    label: "Amount"
    type: number
    sql: ${TABLE}.amount ;;
    value_format_name: usd
  }

  dimension: balance {
    label: "Balance"
    description: "Balance after transaction"
    type: number
    sql: ${TABLE}.balance ;;
    value_format_name: usd
  }

  dimension: bank {
    label: "Bank"
    description: "External Bank"
    hidden: no
    type: string
#     sql: ${TABLE}.bank ;;
    sql: CASE WHEN ${TABLE}.bank IS NULL THEN 'Internal' ELSE ${TABLE}.bank END ;;
    link: {
      label: "Investigate Competitor Interest Rates"
      url: "https://google.com/search?q={{ value }}%20Interest%20Rates"
      icon_url: "https://banner2.cleanpng.com/20180324/sbe/kisspng-google-logo-g-suite-google-5ab6f1f0dbc9b7.1299911115219389289003.jpg"
    }
  }

#   dimension: destination_bank {
#     group_label: "External Banks"
#     type: string
#     sql: case when ${type} = 'Debit' then ${bank} else 'Internal' end ;;
#   }
#
#   dimension: transferring_bank {
#     group_label: "External Banks"
#     type: string
#     sql: case when ${type} = 'Credit' then ${bank} else 'Internal' end ;;
#   }

  dimension: category {
    label: "Category"
    tags: ["finance:sort:2"]
    order_by_field: category_sort
    type: string
    # sql: ${TABLE}.category ;;
    sql:
      CASE ${TABLE}.category
      WHEN 'household' THEN 'Household'
      WHEN 'ins_paymt' THEN 'Insurance Payment'
      WHEN 'int_credited' THEN 'Interest Credited'
      WHEN 'loan_paymt' THEN 'Loan Payment'
      WHEN 'paymt_for_statement' THEN 'Payment for Statement'
      WHEN 'pension' THEN 'Pension Contribution'
      WHEN 'sanc_int' THEN 'Interest Sanctioned'
      ELSE ${operation}
      END
    ;;}

  dimension: category_sort {
    type: string
    hidden: yes
    sql:
    CASE ${TABLE}.category
    WHEN 'household' THEN 'Own Brand'
    WHEN 'ins_paymt' THEN 'Own Brand'
    WHEN 'int_credited' THEN 'Branded'
    WHEN 'loan_paymt' THEN 'Branded'
    WHEN 'paymt_for_statement' THEN 'Own Brand'
    WHEN 'pension' THEN 'Own Brand'
    WHEN 'sanc_int' THEN 'Own Brand'
    ELSE 'Own Brand'
    END
  ;;
  }

  dimension_group: transaction {
    type: time
    timeframes: [date, day_of_week, day_of_month, day_of_year, week, month, month_name, quarter, quarter_of_year, year,raw]
#       datatype: yyyymmdd
    sql: timestamp(${TABLE}.date);;
  }

  dimension: operation {
    label: "Operation"
    tags: ["finance:sort:3"]
    description: "Mode of transaction."
    type: string
    # sql: ${TABLE}.operation ;;
    case: {
      when: {
        label: "Cash Withdrawal"
        sql: ${TABLE}.operation = 'cash_wd' ;;
      }
      when: {
        label: "Collect from Bank"
        sql: ${TABLE}.operation = 'coll_from_bank' ;;
      }
      when: {
        label: "Credit in Cash"
        sql: ${TABLE}.operation = 'credit_in_cash' ;;
      }
      when: {
        label: "Creditcard Withdrawal"
        sql: ${TABLE}.operation = 'creditcard_wd' ;;
      }
      when: {
        label: "Remittance to Bank"
        sql: ${TABLE}.operation = 'remi_to_bank' ;;
      }
      else: "Unknown"
    }
  }

  dimension: type {
    label: "Type"
    tags: ["finance:sort:1"]
    description: "Credit or Debit"
    type: string
    # sql: ${TABLE}.type ;;
    sql: CASE WHEN ${TABLE}.type = 'credit' THEN 'Credit' ELSE 'Debit' END ;;
    html:
     {% if value == 'Credit' %}
       <p style="color: black; font-size: 100%">{{ rendered_value }}</p>
     {% else %}
       <p style="color: red; font-size:100%">{{ rendered_value }}</p>
     {% endif %}
  ;;
  }

  measure: number_of_transactions {
    label: "Number of Transactions"
    type: count
    value_format_name: decimal_0
    drill_fields: [transaction_details*]
  }

  measure: total_transaction_value {
    label: "Total Transaction Value"
    tags: ["finance:sort:4"]
    type: sum
    sql: ${amount} ;;
    value_format_name: usd
    drill_fields: [transaction_details*]
  }

  measure: average_transaction_value {
    label: "Average Transaction Value"
    type: average
    sql: ${amount} ;;
    value_format_name: usd
    drill_fields: [transaction_details*]
  }

  measure: average_account_balance_during_transaction {
    label: "Average Account Balance During Transaction"
    type: average
    sql: ${balance} ;;
    value_format_name: usd
    drill_fields: [transaction_details*]
  }

  measure: last_transaction_date {
    hidden: yes
    type: date
    sql: max(${transaction_date}) ;;
  }

  measure: first_transaction_date {
    hidden: yes
    type: date
    sql: min(${transaction_date}) ;;
  }

  measure: total_inflow {
    label: "Total Inflow"
    type: sum
    sql: ${amount} ;;
    value_format_name: usd
    filters: {
      field: type
      value: "Credit"
    }
    drill_fields: [transaction_details*]
  }

  measure: total_outflow {
    label: "Total Outflow"
    type: sum
    sql: ${amount} ;;
    value_format_name: usd
    filters: {
      field: type
      value: "Debit"
    }
    drill_fields: [transaction_details*]
  }

  set: transaction_details {
    fields: [trans_id, transaction_date, account_id, category, type, operation, amount, balance]
  }
}
