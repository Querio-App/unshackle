view: client {
  sql_table_name: `looker-private-demo.retail_banking.client` ;;
  drill_fields: [client_id]

  dimension: client_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.client_id ;;
    value_format_name: id
    link: {
      label: "Customer Account Overview"
      url: "/dashboards/H6a20GawTA4JuU3drz1aCs?Client%20ID={{ value | encode_uri }}"
      icon_url: "https://img.icons8.com/?size=512&id=63841&format=png"
    }
  }

  dimension: first_name {
    hidden: yes
    type: string
    sql: ${TABLE}.first_name ;;
  }

  dimension:last_name {
    hidden: yes
    type: string
    sql: ${TABLE}.last_name ;;
  }

  dimension: name {
    group_label: "PII (Limited Accessibility)"
    type: string
    sql: concat(${first_name},' ',${last_name}) ;;
    action: {
        label: "Send Promotional Offering to Customer"
        url: "https://desolate-refuge-53336.herokuapp.com/posts"
        icon_url: "https://sendgrid.com/favicon.ico"
        param: {
          name: "some_auth_code"
          value: "abc123456"
        }
        form_param: {
          name: "Subject"
          required: yes
          default: "Thank you {{ client.name._value }}"
        }
        form_param: {
          name: "Body"
          type: textarea
          required: yes
          default:
          "Dear {{ client.first_name._value }},

          Thanks for your loyalty to the Look.  We'd like to offer you up to 3x points for the next 2 weeks on categories such as Entertainment and Travel.
          See attached for more details.

          Your friends at the Look"
        }
      }
    action: {
      label: "Message Customer About Potential Fraud"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://www.pngkey.com/png/full/51-512118_message-icon-message-icon-png-black.png"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        name: "Subject"
        required: yes
        default: "Possible Fraud on Your Credit Card"
      }
      form_param: {
        name: "Body"
        type: textarea
        required: yes
        default:
        "Dear {{ client.first_name._value }},

        We have noticed a pattern of potential fraudulent activity on your credit card over the past few days. Please review these charges and confirm if they are fraud.

        Thank you for your loyalty"
      }
    }
    required_fields: [name, first_name, client_id]
    drill_fields: [card_transactions.category]
  }

  dimension: ssn {
    label: "SSN"
    group_label: "PII (Limited Accessibility)"
    type: string
    sql: ${TABLE}.ssn ;;
  }

  dimension: street {
    label: "Street"
    group_label: "Address"
    type: string
    sql: ${TABLE}.street ;;
  }

  dimension: city {
    label: "City"
    group_label: "Address"
    type: string
    sql: SPLIT(${TABLE}.address,'|')[OFFSET(0)] ;;
  }

  dimension: state {
    label: "State"
    map_layer_name: us_states
    group_label: "Address"
    type: string
    sql: SPLIT(${TABLE}.address,'|')[OFFSET(1)] ;;
  }

  dimension: zip {
    label: "Zip Code"
    group_label: "Address"
    type: zipcode
    sql: SPLIT(${TABLE}.address,'|')[OFFSET(2)] ;;
    }

    dimension: client_full_address {
      type: string
      group_label: "Address"
      sql: concat(${street},", ",${city},", ", ${state}," ",${zip});;
    }


  dimension: lat {
    hidden: yes
    group_label: "Address"
    type: number
    sql: cast(SPLIT(${TABLE}.address,'|')[OFFSET(3)] as float64);;
  }

  dimension: lon {
    hidden: yes
    group_label: "Address"
    type: number
    sql: cast(SPLIT(${TABLE}.address,'|')[OFFSET(4)] as float64) ;;
  }

  dimension: client_geom {
    hidden: yes
    type: string
    sql:
    -- spectacles: ignore
    ST_GeogPoint(${lon},${lat}) ;;
  }

  dimension: location {
    label: "Location"
    group_label: "Address"
    type: location
    sql_latitude: ${lat} ;;
    sql_longitude: ${lon} ;;
  }

  dimension: job {
    label: "Job"
    type: string
    sql: ${TABLE}.job ;;
  }

  dimension: birth_date {
    label: "Birth Date"
    group_label: "PII (Limited Accessibility)"
    type: date
    sql: timestamp(${TABLE}.dob);;
  }

  dimension: age {
    label: "Age"
    group_label: "PII (Limited Accessibility)"
    type: number
    sql: ${TABLE}.age ;;
  }

  dimension: age_tier {
    label: "Age Tier"
    drill_fields: [card_transactions.merchant]
    type: tier
#     tiers: [18, 25, 35, 45, 55, 65]
    tiers: [20, 30, 40, 50, 60]
    style: integer
    sql: ${age} ;;
  }

  dimension: district_id {
    hidden: yes
    type: number
    sql: ${TABLE}.district_id ;;
    value_format_name: id
  }

  dimension: gender {
    label: "Gender"
    type: string
    sql: ${TABLE}.gender ;;
  }

  dimension: has_card {
    hidden: yes
    type: yesno
    sql: ${card.type} is not null ;;
  }

  dimension: has_loan {
    hidden: yes
    type: yesno
    sql: ${loan.loan_id} is not null ;;
  }

  dimension: profile {
    type: string
    hidden: yes
    sql: ${TABLE}.profile ;;
  }

  dimension: customer_segment {
    label: "Customer Segment"
    type: string
    description: "Named clusters produced by machine learning algorithm"
    sql: case when ${is_urban} and ${profile} like '%young%' then 'Young Adults in Urban Areas'
              when ${is_urban} and ${profile} like '%2550%' then 'Middle Age in Urban Areas'
              when ${is_urban} then 'Retirees in Urban Areas'
              when ${profile} like '%young%' then 'Young Adults in Rural Areas'
              when ${profile} like '%2550%' then 'Middle Age in Rural Areas'
              else 'Retirees in Rural Areas'
              end ;;
  }

  dimension: is_urban {
    label: "Is Urban (y/n)"
    type: yesno
    group_label: "Address"
    sql: ${profile} like '%urban%' ;;
  }

  measure: number_of_clients {
    label: "Number of Clients"
    type: count
    drill_fields: [client_id, location, name, card.create_date, card.end_date, card_transactions.count, card_transactions.total_amount]
  }

  measure: number_of_clients_with_cards {
    label: "Number of Clients with Cards"
    type: count_distinct
    sql: ${client_id} ;;
    filters: {
      field: has_card
      value: "yes"
    }
  }

  measure: percent_clients_with_cards {
    label: "Percent Clients with Cards"
    type: number
    value_format_name: percent_2
    sql: 1.0*${number_of_clients_with_cards}/NULLIF(${number_of_clients},0) ;;
  }

  measure: number_of_clients_with_loans {
    label: "Number of Clients with Loans"
    type: count_distinct
    sql: ${client_id} ;;
    filters: {
      field: has_loan
      value: "yes"
    }
  }

  measure: percent_clients_with_loans {
    label: "Percent Clients with Loans"
    type: number
    value_format_name: percent_2
    sql: 1.0*${number_of_clients_with_loans}/NULLIF(${number_of_clients},0) ;;
  }

  dimension: days_since_account_creation {
    label: "Days Since Account Creation"
    description: "The days since they created a brokerage account, until they signed up for the credit card"
    type: duration_day
    sql_start: ${account.create_raw} ;;
    sql_end: CURRENT_TIMESTAMP() ;;
  }


}
