view: customers {
  sql_table_name: analytics.dim_customers ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.customer_id ;;
  }

  dimension: name {
    type: string
    sql: ${TABLE}.full_name ;;
  }

  dimension: email {
    type: string
    sql: ${TABLE}.email ;;
    required_access_grants: [can_see_finance]
  }

  dimension: region {
    type: string
    description: "Sales region: EU, NA or APAC"
    sql: ${TABLE}.region ;;
  }

  dimension: is_test {
    type: yesno
    hidden: yes
    sql: ${TABLE}.is_test_account ;;
  }

  dimension_group: signed_up {
    type: time
    datatype: date
    timeframes: [date, week, month, year]
    sql: ${TABLE}.signup_date ;;
  }

  measure: count {
    type: count
  }
}
