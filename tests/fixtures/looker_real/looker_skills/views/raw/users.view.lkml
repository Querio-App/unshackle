view: users {
  sql_table_name: @{DATASET_NAME}.users ;;

  # --------------------------------------------------------------------------
  # Raw Dimensions (1:1 Table Column Mapping)
  # --------------------------------------------------------------------------

  dimension: id {
    primary_key: yes
    type: number
    description: "Unique identifier for each user."
    sql: ${TABLE}.id ;;
  }

  dimension: first_name {
    type: string
    description: "The user's first name."
    sql: ${TABLE}.first_name ;;
  }

  dimension: last_name {
    type: string
    description: "The user's last name."
    sql: ${TABLE}.last_name ;;
  }

  dimension: email {
    type: string
    description: "The user's email address."
    sql: ${TABLE}.email ;;
  }

  dimension: age {
    type: number
    description: "The user's age in years."
    sql: ${TABLE}.age ;;
  }

  dimension: gender {
    type: string
    description: "The gender of the user."
    sql: ${TABLE}.gender ;;
  }

  dimension: state {
    type: string
    description: "The state or region where the user resides."
    sql: ${TABLE}.state ;;
  }

  dimension: street_address {
    type: string
    description: "The street address of the user."
    sql: ${TABLE}.street_address ;;
  }

  dimension: postal_code {
    type: string
    description: "The postal code or ZIP code of the user."
    sql: ${TABLE}.postal_code ;;
  }

  dimension: city {
    type: string
    description: "The city where the user resides."
    sql: ${TABLE}.city ;;
  }

  dimension: country {
    type: string
    description: "The country where the user resides."
    sql: ${TABLE}.country ;;
  }

  dimension: latitude {
    type: number
    description: "The latitude coordinates of the user's location."
    sql: ${TABLE}.latitude ;;
  }

  dimension: longitude {
    type: number
    description: "The longitude coordinates of the user's location."
    sql: ${TABLE}.longitude ;;
  }

  dimension: traffic_source {
    type: string
    description: "The source from which the user arrived (e.g., Search, Organic, Email)."
    sql: ${TABLE}.traffic_source ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, time, date]
    description: "The timestamp when the user account was created."
    sql: ${TABLE}.created_at ;;
  }

  dimension: user_geom {
    type: string
    description: "The geographic boundary or coordinates representing the user's location."
    sql: ${TABLE}.user_geom ;;
  }
}
