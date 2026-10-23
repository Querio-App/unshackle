view: balances_fact {
  derived_table: {
    sql: WITH balances as (
      SELECT date, account_id, eod_balance, lead(date) over (partition by account_id order by date asc) as next_day from
        (SELECT distinct date, account_id,
                  FIRST_VALUE(balance) OVER (PARTITION BY account_id, date ORDER BY trans_id DESC) eod_balance,
                  FROM `looker-private-demo.retail_banking.trans`
              order by account_id, date desc) as eb
    )

        select date_field
        , eod_balance
        , account_id
        FROM UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(CURRENT_DATE, INTERVAL 6 YEAR), CURRENT_DATE)) date_field
              INNER JOIN balances
              on (case when balances.next_day is not null then date_field < balances.next_day and date_field >= balances.date
                      else date_field < CURRENT_DATE and date_field >= balances.date end)
        order by account_id, date_field;;
    sql_trigger_value: select max(date) from looker-private-demo.retail_banking.trans;;
  }


  dimension: is_beginning_of_period {
    hidden: yes
    description: "Must use with a filter on the date"
    type: yesno
    sql: ${balance_date} = cast({% date_start balance_date %} as date);;
  }


  dimension: is_yesterday {
    hidden: yes
    type: yesno
    sql: ${balance_date} = date_add(current_date(), interval -1 day) ;;
  }

  dimension: primary_key {
    type: string
    primary_key: yes
    hidden: no
    sql: CONCAT(${account_id},cast(${balance_date} as string)) ;;
  }

  measure: count {
    type: count
  }

  dimension_group: balance {
    type: time
    timeframes: [date,day_of_year, day_of_month, day_of_week, week,quarter,month,month_name,year, raw]
    convert_tz: no
    sql: cast(${TABLE}.date_field as timestamp);;
  }

  dimension: balance {
    label: "Balance"
    type: number
    sql: ${TABLE}.eod_balance ;;
    value_format_name: usd
  }

  dimension: account_id {
    hidden: yes
    type: number
    sql: ${TABLE}.account_id ;;
  }

  dimension: balance_tier {
    label: "Balance Tier"
    type: tier
    sql: ${balance} ;;
    style: integer
    tiers: [1000,2000,5000,10000]
  }

  measure: total_balance {
    label: "Total Balance"
    description: "The sum of the daily balances for each account"
    type: sum
    sql: ${balance} ;;
    value_format_name: usd
    drill_fields: [balance_detail*]
  }

  measure: balance_yesterday {
    label: "Balance Yesterday"
    type: sum
    description: "This is the total balance in all accounts yesterday"
    sql: ${balance} ;;
    filters: {
      field: is_yesterday
      value: "yes"
    }
    value_format_name: usd
    drill_fields: [balance_detail*]
  }

  measure: average_daily_balance {
    label: "Average Daily Balance"
    type: average
    sql: ${balance} ;;
    value_format_name: usd
    drill_fields: [balance_detail*]
  }

  measure: balance_at_beginning {
    label: "Balance at Beginning"
    type: sum
    description: "This is the total balance at the beginning of the time period selected in the date filter"
    sql: ${balance} ;;
    filters: {
      field: is_beginning_of_period
      value: "yes"
    }
    value_format_name: usd
    drill_fields: [balance_detail*]
  }

  measure: balance_change {
    label: "Balance Change"
    type: number
    value_format_name: percent_2
    sql: 1.0*(${balance_yesterday} - ${balance_at_beginning}) / nullif(${balance_at_beginning},0);;
  }

  measure: attrition_rate {
    label: "Attrition Rate"
    description: "The attrition rate from the beginning of the period"
    type: number
    value_format_name: percent_2
    sql: 1.0*${trans.total_outflow}/nullif(${balance_at_beginning},0);;
    drill_fields: [account.account_id, account.account_manager_id, account_fact.number_of_transactions, attrition_rate, balance_at_beginning, balance_yesterday]
  }

  measure: inflow_rate {
    label: "Inflow Rate"
    description: "The inflow rate from the beginning of the period"
    type: number
    value_format_name: percent_2
    sql: 1.0*${trans.total_inflow}/nullif(${balance_at_beginning},0);;
    drill_fields: [account.account_id, account.account_manager_id, account_fact.number_of_transactions, inflow_rate, balance_at_beginning, balance_yesterday]
  }


  set: balance_detail {
    fields: [balance_date, account_id, balance, client.client_id, client.age, account.create_date]
  }
}
