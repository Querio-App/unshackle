view: card_transactions_with_zip {
  derived_table: {
    sql_trigger_value: select max(trans_date) from `looker-private-demo.retail_banking.card_transactions`;;
    sql: SELECT
          card_transactions.trans_id,
          zip_code AS merchant_zip,
          city as merchant_city,
          county as merchant_county,
          state_name as merchant_state
      FROM
          `looker-private-demo.retail_banking.card_transactions`  AS card_transactions,
          `bigquery-public-data.geo_us_boundaries.zip_codes` AS zip_codes
      WHERE ST_Within(ST_GEOGPOINT((cast(card_transactions.merchant_lon as FLOAT64)),(cast(card_transactions.merchant_lat as FLOAT64))),zip_codes.zip_code_geom)
       ;;
  }

  dimension: trans_id {
    primary_key: yes
    hidden: yes
    view_label: "Merchant"
    type: string
    sql: ${TABLE}.trans_id ;;
  }


  dimension: zip {
    label: "Zip Code"
    view_label: "Merchant"
    group_label: "Location"
    type: string
    sql: ${TABLE}.merchant_zip ;;
    map_layer_name: us_zipcode_tabulation_areas
  }

  dimension: city {
    label: "City"
    view_label: "Merchant"
    group_label: "Location"
    type: string
    sql: ${TABLE}.merchant_city ;;
  }

  dimension: county {
    label: "County"
    view_label: "Merchant"
    group_label: "Location"
    type: string
    sql: ${TABLE}.merchant_county ;;
  }

  dimension: state {
    label: "State"
    type: string
    view_label: "Merchant"
    group_label: "Location"
    sql: ${TABLE}.merchant_state ;;
    map_layer_name: us_states
    drill_fields: [city,county,zip]
  }

}
