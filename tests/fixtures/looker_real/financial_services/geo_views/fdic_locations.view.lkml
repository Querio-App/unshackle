view: fdic_locations {
  sql_table_name: bigquery-public-data.fdic_banks.fdic_locations ;;

  dimension: branch_address {
    type: string
    sql: ${TABLE}.branch_address ;;
  }

  dimension: branch_city {
    type: string
    sql: ${TABLE}.branch_city ;;
  }

  dimension: branch_county {
    type: string
    sql: ${TABLE}.branch_county ;;
  }

  dimension: branch_fdic_uninum {
    type: string
    sql: ${TABLE}.branch_fdic_uninum ;;
  }

  dimension: branch_name {
    type: string
    sql: ${TABLE}.branch_name ;;
  }

  dimension: branch_number {
    type: string
    sql: ${TABLE}.branch_number ;;
  }

  dimension: cbsa_division_fips_code {
    type: number
    sql: ${TABLE}.cbsa_division_fips_code ;;
  }

  dimension: cbsa_division_flag {
    type: yesno
    sql: ${TABLE}.cbsa_division_flag ;;
  }

  dimension: cbsa_division_name {
    type: string
    sql: ${TABLE}.cbsa_division_name ;;
  }

  dimension: cbsa_fips_code {
    type: string
    sql: ${TABLE}.cbsa_fips_code ;;
  }

  dimension: cbsa_metro_fips_code {
    type: string
    sql: ${TABLE}.cbsa_metro_fips_code ;;
  }

  dimension: cbsa_metro_flag {
    type: yesno
    sql: ${TABLE}.cbsa_metro_flag ;;
  }

  dimension: cbsa_metro_name {
    type: string
    sql: ${TABLE}.cbsa_metro_name ;;
  }

  dimension: cbsa_micro_flag {
    type: yesno
    sql: ${TABLE}.cbsa_micro_flag ;;
  }

  dimension: cbsa_name {
    type: string
    sql: ${TABLE}.cbsa_name ;;
  }

  dimension: county_fips_code {
    type: string
    sql: ${TABLE}.county_fips_code ;;
  }

  dimension: csa_fips_code {
    type: string
    sql: ${TABLE}.csa_fips_code ;;
  }

  dimension: csa_flag {
    type: yesno
    sql: ${TABLE}.csa_flag ;;
  }

  dimension: csa_name {
    type: string
    sql: ${TABLE}.csa_name ;;
  }

  dimension_group: date_established {
    type: time
    timeframes: [
      raw,
      date,
      week,
      month,
      quarter,
      year
    ]
    convert_tz: no
    datatype: date
    sql: ${TABLE}.date_established ;;
  }

  dimension: fdic_certificate_number {
    type: string
    sql: ${TABLE}.fdic_certificate_number ;;
  }

  dimension: fdic_uninum {
    type: string
    sql: ${TABLE}.fdic_uninum ;;
  }

  dimension: institution_class {
    type: string
    sql: ${TABLE}.institution_class ;;
  }

  dimension: institution_name {
    type: string
    sql: ${TABLE}.institution_name ;;
  }

  dimension_group: last_updated {
    type: time
    timeframes: [
      raw,
      date,
      week,
      month,
      quarter,
      year
    ]
    convert_tz: no
    datatype: date
    sql: ${TABLE}.last_updated ;;
  }

  dimension: main_office {
    type: yesno
    sql: ${TABLE}.main_office ;;
  }

  dimension: service_type {
    type: string
    sql: ${TABLE}.service_type ;;
  }

  dimension: state {
    type: string
    sql: ${TABLE}.state ;;
  }

  dimension: state_name {
    type: string
    sql: ${TABLE}.state_name ;;
  }

  dimension: zip_code {
    type: zipcode
    sql: ${TABLE}.zip_code ;;
  }

  measure: count {
    type: count
    drill_fields: [detail*]
  }

  # ----- Sets of fields for drilling ------
  set: detail {
    fields: [
      cbsa_division_name,
      institution_name,
      state_name,
      cbsa_name,
      branch_name,
      csa_name,
      cbsa_metro_name
    ]
  }
}
