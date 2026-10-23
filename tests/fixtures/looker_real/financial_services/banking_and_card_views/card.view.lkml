view: card {
  label: "Credit Card"
  sql_table_name: `looker-private-demo.retail_banking.card` ;;
  drill_fields: [card_id]

  dimension: card_id {
    hidden: yes
    primary_key: yes
    type: number
    sql: ${TABLE}.card_id ;;
    value_format_name: id
  }

  dimension_group: create {
    label: "Card Creation"
    type: time
    timeframes: [date, day_of_month, day_of_year, week, month, month_name, quarter, quarter_of_year, year, raw]
#     datatype: yyyymmdd
    sql: cast(${TABLE}.date as timestamp) ;;
  }

  dimension_group: end {
    label: "Card Cancellation"
    type: time
    timeframes: [date, day_of_month, day_of_year, week, month, month_name, quarter, quarter_of_year, year, raw]
#     datatype: yyyymmdd
    sql: cast(${TABLE}.end_date as timestamp) ;;
  }

  dimension: card_number {
    label: "Card Number"
    type: string
    sql: ${TABLE}.card_number ;;
#     label: "{% if _user_attributes['can_see_ssn'] == 'yes' %} Card Number
#       {% else %} Card Number (Redacted due to insufficient permissions)
#       {% endif %}"
#     sql:
#       CASE
#         WHEN '{{ _user_attributes['can_see_ssn'] }}' = 'yes'
#             ${TABLE}.card_number
#         ELSE 'XXX'
#       END ;;
  }

  dimension: is_cancelled {
    label: "Is Cancelled (y/n)"
    description: "Was the card cancelled or not?"
    type: yesno
    sql: ${end_date} is not null;;
  }

  dimension: months_as_customer {
    label: "Months Active"
    description: "Months that the card has been, or was (if canceled), active for"
    type: duration_month
    sql_start: ${create_raw} ;;
    sql_end: case when ${end_date} is not null then ${end_raw} else current_timestamp() end ;;
  }

  dimension: months_as_customer_tiered {
    label: "Months Active Tiered"
    type: tier
    style: integer
    tiers: [1,3,6,12,24]
    sql: ${months_as_customer} ;;
  }


  dimension: days_between_account_signup {
    label: "Days Between Account Signup"
    description: "The days since they created a brokerage account, until they signed up for the credit card"
    type: duration_day
    sql_start: ${account.create_raw} ;;
    sql_end: ${create_raw} ;;
  }

  dimension: disp_id {
    type: number
    hidden: yes
    sql: ${TABLE}.disp_id ;;
    value_format_name: id
  }


  dimension: type {
    label: "Type"
    drill_fields: [client.name, card_transactions.category]
    description: "The type of credit card this is"
    type: string
    case: {
      when: {
        label: "Looker Junior"
        sql: ${TABLE}.type = 'junior' ;;
      }
      when: {
        label: "Looker Emerald"
        sql: ${TABLE}.type = 'junior2' ;;
      }
      when: {
        label: "Looker Ruby"
        sql: ${TABLE}.type = 'classic' ;;
      }
      when: {
        label: "Looker Ruby Preferred"
        sql: ${TABLE}.type = 'classic2' ;;
      }
      when: {
        label: "Looker Business Reserve"
        sql: ${TABLE}.type = 'gold' ;;
      }
      else: "Null"
    }
    link: {
      label: "{% if value == 'No Card' %}{% else %} Card Type Lookup Dashboard for {{ value }} {% endif %}"
      url: "/dashboards/WIrk7cojLJoQg2n1hiXw4O?Card%20Type={{ value }}"
      icon_url: "http://www.looker.com/favicon.ico"
    }
  }

  measure: number_of_credit_cards {
    label: "Number of Credit Cards"
    type: count
    drill_fields: [create_date, card_id, client.client_id, client.name, type, card_transactions.average_amount]
  }

  measure: count_junior {
    hidden: yes
    type: count
    filters: {
      field: type
      value: "Looker Junior"
    }
  }

  measure: percent_junior {
    hidden: yes
    type: number
    value_format_name: percent_0
    sql: 1.0*${count_junior}/nullif(${number_of_credit_cards},0) ;;
  }

  measure: count_business {
    hidden: yes
    type: count
    filters: {
      field: type
      value: "Looker Business Reserve"
    }
  }

  measure: average_days_between_account_signup {
    label: "Average Days Between Account Signup"
    type: average
    sql: ${days_between_account_signup} ;;
    value_format_name: decimal_0
  }

  measure: percent_business {
    hidden: yes
    type: number
    value_format_name: percent_0
    sql: 1.0*${count_business}/nullif(${number_of_credit_cards},0) ;;
  }
}
