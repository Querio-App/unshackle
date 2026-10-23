# If necessary, uncomment the line below to include explore_source.
# include: "thelookai.model.lkml"
view: customer_profile {
  derived_table: {
    datagroup_trigger: ecommerce_etl_modified
    explore_source: order_items {
      column: id { field: users.id }
      column: email { field: users.email }
      column: gender { field: users.gender}
      column: name { field: users.name}
      column: age { field : users.age}
      column: days_as_customer { field: user_order_facts.days_as_customer }
      column: lifetime_orders { field: user_order_facts.lifetime_orders }
      column: lifetime_revenue { field: user_order_facts.lifetime_revenue }
      column: latest_order_date { field: user_order_facts.latest_order_date }
      column: city { field: users.city }
      column: country { field: users.country }
      column: prefered_categories { field: products.prefered_categories }
      column: prefered_brands { field: products.prefered_brands }
      derived_column: p_brands {
        sql: replace(prefered_brands, "|RECORD|", ", ") ;;
      }
      derived_column: p_categories {
        sql: replace(prefered_categories, "|RECORD|", ", ") ;;
      }
      filters: {
        field: users.id
        value: "NOT NULL"
      }
    }
  }
  dimension: gender {}
  dimension: id {
    description: ""
    type: number
  }
  dimension: email {
    description: ""
  }
  dimension: days_as_customer {
    label: "Users Facts Days As Customer"
    description: "Days between first and latest order"
    type: number
  }
  dimension: lifetime_orders {
    label: "Users Facts Lifetime Orders"
    description: ""
    type: number
  }
  dimension: lifetime_revenue {
    label: "Users Facts Lifetime Revenue"
    description: ""
    value_format: "$#,##0.00"
    type: number
  }
  dimension: latest_order_date {
    label: "Users Facts Latest Orders"
    description: ""
    type: date
  }
  dimension: city {
    description: ""
  }
  dimension: country {
    description: ""
  }
  dimension: prefered_categories {
    description: ""
    #type: number
  }
  dimension: prefered_brands {
    description: ""
    #type: number
  }
  dimension: p_brands {}
  dimension: p_categories {}
}


view: promo_email {
  derived_table: {
    #datagroup_trigger: daily
    sql: SELECT
        ml_generate_text_result['predictions'][0]['content'] AS generated_text,
        ml_generate_text_result['predictions'][0]['safetyAttributes']
          AS safety_attributes,
        * EXCEPT (ml_generate_text_result)
      FROM
        ML.GENERATE_TEXT(
          MODEL  `looker-private-demo.ecomm.email_promotion`,
          (
            SELECT

      format(CONCAT('Generate Promo Email (150 words) including details about the following customer profile : \nName : %s\nGender : %s\nAge :%d\nDays as customer: %d\nLifetime order : %d\nLifetime revenue : %f\nExpiry Date : %s\nCity : %s\nCountry : %s'),name, gender, age, days_as_customer, lifetime_orders, lifetime_revenue, cast(DATE_ADD(CURRENT_DATE, interval 3 month) as string),city, country)  AS prompt,
      id
      FROM  ${customer_profile.SQL_TABLE_NAME}
      WHERE {% condition users.email %} email {% endcondition %}
      ),
      STRUCT(
      0.2 AS temperature,
      100 AS max_output_tokens)) ;;
  }

  dimension: prompt {
    hidden: yes
  }
  dimension: generated_text {
    label: "AI Generated Email"
    description: "Use with the user email in filter"
    view_label: "Users"
    type: string
    sql: JSON_VALUE(${TABLE}.generated_text) ;;
  }

  dimension: id {
    hidden: yes
    primary_key: yes
  }

  filter: gen_ai_email { #### Will need to see we integrate this to the UI in efficient way
    hidden: yes
    description: "If you want to override the email with some user input, use this field"
    type: string
  }

  dimension: gen_ai {
    hidden: yes
    type: string
    sql: {% parameter gen_ai_email %} ;;
  }
}