view: district {
  view_label: "Branch"
  sql_table_name: `looker-private-demo.retail_banking.district` ;;
  drill_fields: [district_id]

  measure: count {
    label: "Number of Branches"
    type: count_distinct
    sql: ${district_id} ;;
  }

  dimension: district_id {
    label: "Branch ID"
    primary_key: yes
    type: number
    sql: ${TABLE}.district_id ;;
    value_format_name: id
  }

  dimension: avgsal {
    hidden: yes
    label: "Average Salary in Zipcode"
    type: number
    sql: ${TABLE}.avgsal ;;
  }

  dimension: zipcode {
    label: "Zip Code"
    type: zipcode
    sql: ${TABLE}.zipcode ;;
  }

  dimension: ent_ppt {
    hidden: yes
    label: "Entrepreneurs per 1,000 in Zipcode"
    type: number
    sql: ${TABLE}.ent_ppt ;;
  }

#   dimension: ncri95 {
#     hidden: yes
#     label: "Crimes 1995"
#     type: string
#     sql: ${TABLE}.ncri95 ;;
#   }

  dimension: ncri {
    hidden: yes
    label: "Number of Crimes in Zipcode"
    type: number
    sql: ${TABLE}.ncri96 ;;
  }

#   dimension: nmu10k {
#     hidden: yes
#     label: "Municipalities Population 2000-9999"
#     type: number
#     sql: ${TABLE}.nmu10k ;;
#   }
#
#   dimension: nmu2k {
#     hidden: yes
#     label: "Municipalities Population 500-1999"
#     type: number
#     sql: ${TABLE}.nmu2k ;;
#   }

#   dimension: nmu500 {
#     hidden: yes
#     label: "Municipalities Population < 500"
#     type: number
#     sql: ${TABLE}.nmu500 ;;
#   }
#
#   dimension: nmuinf {
#     hidden: yes
#     label: "Municipalities Population > 10000"
#     type: number
#     sql: ${TABLE}.nmuinf ;;
#   }

  dimension: pop {
    hidden: yes
    label: "Zipcode Population"
    type: number
    sql: ${TABLE}.pop ;;
  }

  dimension: state {
    type: string
    map_layer_name: us_states
    sql: ${TABLE}.state ;;
  }

  dimension: city {
    type: string
    sql: ${TABLE}.city ;;
  }

#   dimension: rurba {
#     hidden: yes
#     label: "Ratio Urban Inhabitants 1995"
#     type: number
#     sql: ${TABLE}.rurba ;;
#   }

#   dimension: urat95 {
#     hidden: yes
#     label: "Unemployment Rate 1995"
#     type: string
#     sql: ${TABLE}.urat95 ;;
#   }

  dimension: urat {
    hidden: yes
    label: "Unemployment Rate in Zipcode"
    type: number
    sql: ${TABLE}.urat96 ;;
  }
#
#   measure: number_of_districts {
#
#     type: count
#     drill_fields: [district_id, client.count, account.count]
#   }

  measure: total_population {
    hidden: yes
    type: sum
    sql: ${pop} ;;
    value_format_name: decimal_0
  }

  measure: average_salary {
    hidden: yes
    type: average
    sql: ${avgsal} ;;
    value_format_name: usd
  }

  measure: total_entreupreneurs {
    hidden: yes
    type: number
    value_format_name: decimal_0
    sql:sum(1.0*${ent_ppt}*${pop}/1000);;
  }

  measure: total_number_crimes {
    hidden: yes
    type: sum
    sql: ${ncri} ;;
    value_format_name: decimal_0
  }

  measure: average_unemployment_rate {
    hidden: yes
    type: average
    sql: ${urat} ;;
    value_format_name: decimal_0
  }

}
