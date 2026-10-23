view: card_transactions {
  sql_table_name: `looker-private-demo.retail_banking.card_transactions` ;;

  dimension: amount {
    description: "Amount that the transaction was for"
    type: number
    sql: ${TABLE}.amount ;;
    value_format_name: usd
  }

  dimension: category {
    group_label: "Category"
    drill_fields: [subcategory,merchant,transaction_day_of_week, transaction_hour_of_day]
    label: "Spend Category"
    type: string
    sql: case when ${subcategory} like '%shopping%'  then 'Shopping' when ${subcategory} like '%grocery%' then 'Grocery'
            when ${subcategory} like '%entertainment%' then 'Entertainment'
            when ${subcategory} like '%health%'  or ${subcategory} like '%personal%' then 'Health & Personal Care'
            when ${subcategory} like '%food%' then 'Food & Dining' when ${subcategory} like '%gas%' then 'Gas & Transport'
            when ${subcategory} like '%travel%' then 'Travel' when ${subcategory} like '%home%'  then 'Home' else 'Miscellaneous' end
            ;;
  }

  dimension: subcategory {
    group_label: "Category"
    label: "Subcategory"
    type: string
    sql: ${TABLE}.category;;
  }

  dimension: category_average_amount {
    label: "Category Average Amount"
    value_format_name: usd
    description: "The average amount per transaction for this category"
    group_label: "Category"
    type: number
    sql:(select avg(cast(t.amount as FLOAT64)) from  `looker-private-demo.retail_banking.card_transactions` as t where t.category = ${TABLE}.category);;
  }

  dimension: percent_diff_from_cat {
    label: "Percent Diff From Cat"
    type: number
    hidden: yes
    value_format_name: percent_0
    group_label: "Fraudulent Indicators"
    description: "The percentage difference between the amount for this transaction and the average for the category"
    sql: abs(${amount} - ${category_average_amount})/nullif(${category_average_amount},0);;
  }

  dimension: percent_diff_from_cat_tier {
    label: "Difference from Average Amount"
    type: tier
    group_label: "Fraudulent Indicators"
    description: "The percentage difference between the amount for this transaction and the average for the category"
    sql: ${percent_diff_from_cat}*100;;
    tiers: [50,100,500,1000]
    style: integer
  }

  dimension: middle_of_night_transaction {
    label: "Middle of Night Transaction"
    group_label: "Fraudulent Indicators"
    type: yesno
    description: "The transaction occured between 12-4 am in the timezone of the merchant"
    sql: ${transaction_hour_of_day} in (0,1,2,3) ;;
  }

  dimension: cc_number {
    hidden: yes
    view_label: "Credit Card"
    label: "Credit Card Number"
    type: string
    sql: ${TABLE}.cc_number ;;
  }

  dimension: is_fraud {
    label: "Is Fraud (y.n)"
    group_label: "Fraudulent Indicators"
    description: "Tells whether or not the transaction was confirmed to be fraudulent or not"
    type: yesno
    sql: ${TABLE}.is_fraud;;
  }

  dimension: merchant {
    view_label: "Merchant"
    label: "Merchant Name"
    type: string
    sql: ${TABLE}.merchant ;;

    link: {
      icon_url: "https://toppng.com/uploads/preview/web-png-jpg-transparent-stock-website-icon-blue-11563644926reanjnmk6x.png"
      label: "Go to Merchant Website"
      url: "https://google.com/"
    }
#     link: {
#       icon_url: "https://f0.pngfuel.com/png/312/936/red-and-blue-stop-sign-nicaragua-google-maps-navigation-map-icon-png-clip-art.png"
#       label: "Find Merchant on Google Maps"
#       url: "https://maps.google.com?q={{ merchant_coordinates._value | encode_uri }}"
#     }
    action: {
      label: "Launch Merchant Investigation"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://cdn.iconscout.com/icon/premium/png-512-thumb/investigation-1611552-1364980.png"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        name: "Subject"
        required: yes
        default: "Fraud Investigation into {{ value }}"
      }
      form_param: {
        name: "Body"
        type: textarea
        required: yes
        default:
        "
        I would like to escalate a review of {{ value }} who has been having increased fraudulent activity across cards recently
        "
      }
    }
    action: {
      label: "Create Points Campaign with Merchant"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://sendgrid.com/favicon.ico"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        name: "Subject"
        required: yes
        default: "Possible Points Campaign with {{ value }}"
      }
      form_param: {
        name: "Body"
        type: textarea
        required: yes
        default:
        "
        I'm thinking that partnering with {{ value }} to offer 2x points on all purchases may be a good way to get more traction on the Emerald card.

        What do you think?"
      }
    }
    link: {
      label: "{{value}} Deep-Dive Dashboard"
      url: "/dashboards/CsYsv5BqjZJVZh2DdS6FFR?Merchant%20Name={{ value | encode_uri }}"
      icon_url: "https://t4.ftcdn.net/jpg/03/32/61/47/240_F_332614727_hbL8NEDgMnlBzAHWDxU0v3qRNEu7kxTH.jpg"
    }
  }

  dimension: merchant_lat {
    hidden: yes
    type: number
    sql: cast(${TABLE}.merchant_lat as FLOAT64);;
  }

  dimension: merchant_lon {
    hidden: yes
    type: number
    sql: cast(${TABLE}.merchant_lon as FLOAT64) ;;
  }


  ### geom for BQ

  dimension: merchant_geom {
    type: string
    sql:
      -- spectacles: ignore
    ST_GEOGPOINT(${merchant_lon},${merchant_lat}) ;;
    hidden: yes
  }

  dimension: merchant_coordinates {
    type: string
    sql:concat(${merchant_lat},", ",${merchant_lon}) ;;
    hidden: yes
  }

  dimension: merchant_location {
    group_label: "Location"
    view_label: "Merchant"
    type: location
    sql_latitude: ${merchant_lat} ;;
    sql_longitude: ${merchant_lon} ;;
  }

  dimension: merchant_address_street_view {
    label: "Merchant Address Street View"
    view_label: "Merchant"
    group_label: "Location"
    type: string
    #hard coding since its fake data
    sql: '1909 W 95th St, Chicago, IL 60643, United States' ;;
    html: <img src="https://maps.googleapis.com/maps/api/streetview?size=700x400&location={{value | encode_uri}}&fov=120&key=AIzaSyD7BvCbKqjStBl7r6AmDu1p8yGF-IxtFLs" ;;
  }

  dimension: transaction_distance {
    label: "Transaction Distance"
    group_label: "Fraudulent Indicators"
    description: "Distance from the customer's home address to where the transaction occured"
    # sql: ST_DISTANCE(${merchant_geom}, ${client.client_geom}) ;;
    type: distance
    start_location_field:  client.location
    end_location_field: merchant_location
    units: miles
  }

  dimension: transaction_distance_tier {
    label: "Transaction Distance Tier"
    group_label: "Fraudulent Indicators"
    type: tier
    tiers: [20,40,60,80,100]
    style: integer
    sql: ${transaction_distance} ;;
  }

  dimension: occurs_outside_75th_percentile {
    label: "Occurs Outside 75th Percentile"
    group_label: "Fraudulent Indicators"
    description: "Did the transaction occur outside the 75th percentile of the border"
    type: yesno
    sql: ${transaction_distance} > ${client_card_fact.transaction_distance_75th_percentile} ;;
  }

  dimension: difference_from_average_distance {
    label: "DIfference From Average Distance"
    group_label: "Fraudulent Indicators"
    description: "Difference in distance for this transaction compared to the average distance traveled for the customer"
    type: number
    sql: ${transaction_distance} - ${client_card_fact.average_transaction_distance} ;;
  }

  dimension: trans_date {
    label: "Trans Date"
    hidden: yes
    type: string
    sql: ${TABLE}.trans_date ;;
  }

  dimension: trans_id {
    label: "Transaction ID"
    primary_key: yes
    type: string
    sql: ${TABLE}.trans_id ;;
    link: {
      label: "Transaction Deep-Dive Dashboard"
      url: "/dashboards-next/425?Transaction%20ID={{ value | encode_uri }}"
      icon_url: "https://image.flaticon.com/icons/svg/2856/2856957.svg"
    }
    action: {
      label: "Send this to fraud investigator"
      url: "https://hooks.zapier.com/hooks/catch/1662138/tvc3zj/"
      form_param: {
        name: "Message"
        type: textarea
        default: "Hey,
        Could you check out transaction #{{value}}. The customer is reaching out to us about it to report fraud.
        "
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
          label: "Fraud Department"
        }
        option: {
          name: "general"
          label: "General"
        }
      }
    }

  }

  dimension: trans_time {
    hidden: yes
    type: string
    sql: ${TABLE}.trans_time ;;
  }

  dimension_group: transaction {
    type: time
    sql: TIMESTAMP(CONCAT(${trans_date},' ',${trans_time})) ;;
    convert_tz: no
  }

  dimension: days_before_cancellation {
    label: "Days Before Cancellation"
    group_label: "Relative Dates"
    description: "If the card was canceled, how many days before the cancellation did the transaction occur"
    type: duration_day
    sql_start: ${card.end_raw};;
    sql_end:${transaction_raw};;
  }

  dimension: months_since_signup {
    label: "Months Since Signup"
    group_label: "Relative Dates"
    description: "How many months between the transaction and the signup"
    type: duration_month
    sql_start: ${card.create_raw};;
    sql_end:${transaction_raw};;
  }

  dimension: days_since_signup {
    label: "Days Since Signup"
    group_label: "Relative Dates"
    description: "How many days between the transaction and the signup"
    type: duration_day
    sql_start: ${card.create_raw};;
    sql_end:${transaction_raw};;
  }

  measure: count {
    label: "Number of Transactions"
    type: count
    drill_fields:[transaction_detail*]
  }

  ##additional metrics

  measure: average_amount {
    label: "Average Amount per Transaction"
    value_format_name: usd
    type: average
    sql: ${amount} ;;
  }

  measure: total_amount {
    label: "Total Amount"
    value_format_name: usd
    type: sum
    sql: ${amount} ;;
    drill_fields: [transaction_detail*]
   }

  measure: fraud_amount {
    label: "Sum of Fraud Amount"
    value_format_name: usd
    type: sum
    group_label: "Fraudulent Activity"
    sql: ${amount} ;;
    filters: {
      field: is_fraud
      value: "yes"
    }
    drill_fields: [transaction_detail*, is_fraud]
  }

  measure: number_of_fraudulent_transactions {
    label: "Count of Fraudulent Transactions"
    group_label: "Fraudulent Activity"
    description: "Number of fraudulent transactions that have occurred"
    type: count
    filters: {
      field: is_fraud
      value: "yes"
    }
    drill_fields:[transaction_detail*, is_fraud]
  }

  measure: number_of_legitimate_transactions {
    hidden: yes
    description: "Tells how many non-fraudulent transactions have occurred"
    type: count
    filters: {
      field: is_fraud
      value: "no"
    }
  }

  measure: average_transaction_distance {
    label: "Average Transaction Distance"
    description: "The average miles from the customers home to where transaction took place"
    type: average
    value_format_name: decimal_2
    sql: ${transaction_distance} ;;
    drill_fields: [transaction_detail*,transaction_distance]
  }

  measure: transaction_distance_75th_percentile {
    label: "Transaction Distance 75h Percentile"
    description: "The 75th percentile for number of miles from the customers home to where transaction took place"
    type: percentile
    percentile: 75
    value_format_name: decimal_2
    sql: ${transaction_distance} ;;
    drill_fields: [transaction_detail*,transaction_distance]
  }

  measure: cards {
    hidden: yes
    type: count_distinct
    sql: ${cc_number} ;;
  }

  measure: first_transaction {
    hidden: yes
    type: date_time
    sql: min(${transaction_raw}) ;;
  }

  measure: last_transaction {
    hidden: yes
    type: date_time
    sql: max(${transaction_raw}) ;;
  }

  measure: average_months_since_signup {
    label: "Average Months Since Signup"
    type: average
    sql: ${months_since_signup} ;;
    drill_fields: [transaction_detail*, months_since_signup]
  }

  measure: number_merchants {
    label: "Number of Merchants"
    view_label: "Merchant"
    type: count_distinct
    sql: ${merchant} ;;
    drill_fields: [merchant, count, total_amount, percent_fraudulent_amount, percent_fraudulent_transactions]
  }

  measure: percent_fraudulent_transactions {
    label: "Percent Fraudulent Transactions"
    group_label: "Fraudulent Activity"
    type: number
    value_format_name: percent_2
    sql: 1.0*${number_of_fraudulent_transactions}/nullif(${count},0) ;;
    drill_fields: [transaction_detail*, is_fraud]
  }

  measure: percent_fraudulent_amount {
    label: "Percent Fraudulent Amount"
    group_label: "Fraudulent Activity"
    type: number
    value_format_name: percent_2
    sql: 1.0*${fraud_amount}/nullif(${total_amount},0) ;;
    drill_fields: [transaction_detail*, is_fraud]
  }

  measure:  hour_75 {
    label: "75th Percentile Hour of Day"
    group_label: "Hour of Day Stats"
    type: percentile
    percentile: 75
    sql: ${transaction_hour_of_day} ;;
  }

  measure:  hour_25 {
    label: "25th Percentile Hour of Day"
    group_label: "Hour of Day Stats"
    type: percentile
    percentile: 25
    sql: ${transaction_hour_of_day} ;;
  }

  measure:  median_hour {
    label: "Median Hour of Day"
    group_label: "Hour of Day Stats"
    type: median
    sql: ${transaction_hour_of_day} ;;
  }

  measure: average_hour {
    label: "Average Hour of Day"
    group_label: "Hour of Day Stats"
    type: average
    sql: ${transaction_hour_of_day} ;;
  }

  measure: amount_75 {
    label: "75th Percentile Amount"
    group_label: "Amount Stats"
    type: percentile
    percentile: 75
    sql: ${amount} ;;
  }

  measure:  amount_25 {
    label: "25th Percentile Amount"
    group_label: "Amount Stats"
    type: percentile
    percentile: 25
    sql: ${amount} ;;
  }

  measure:  median_amount {
    label: "Median Amount"
    group_label: "Amount Stats"
    type: median
    sql: ${amount} ;;
  }

  measure: median_diff {
    label: "Median Difference"
    group_label: "Difference from Average Amount Stats"
    type: median
    sql: ${percent_diff_from_cat} ;;
  }

  measure: average_diff {
    label: "Average Difference"
    group_label: "Difference from Average Amount Stats"
    type: average
    sql: ${percent_diff_from_cat} ;;
  }

  measure: diff_25 {
    label: "25th Percentile Difference"
    group_label: "Difference from Average Amount Stats"
    type: percentile
    percentile: 25
    sql: ${percent_diff_from_cat} ;;
  }

  measure: diff_75 {
    label: "75th Percentile Difference"
    group_label: "Difference from Average Amount Stats"
    type: percentile
    percentile: 75
    sql: ${percent_diff_from_cat} ;;
  }

  measure: median_dist {
    label: "Median Distance"
     group_label: "Distance from Customer Stats"
    type: median
    sql: ${transaction_distance} ;;
  }

  measure: dist_25 {
    label: "25th Percentile Distance"
    group_label: "Distance from Customer Stats"
    type: percentile
    percentile: 25
    sql: ${transaction_distance} ;;
  }

  measure: dist_75 {
    label: "75th Percentile Distance"
    group_label: "Distance from Customer Stats"
    type: percentile
    percentile: 75
    sql: ${transaction_distance} ;;
  }

  measure: avg_diff_dist {
    label: "Average Distance Difference"
    group_label: "Difference from Average Distance Stats"
    type: median
    sql: ${difference_from_average_distance} ;;
  }

  measure: median_diff_dist {
    label: "Median Distance Difference"
    group_label: "Difference from Average Distance Stats"
    type: median
    sql: ${difference_from_average_distance} ;;
  }

  measure: diff_dist_25 {
    label: "25th Percentile Distance Difference"
    group_label: "Difference from Average Distance Stats"
    type: percentile
    percentile: 25
    sql: ${difference_from_average_distance} ;;
  }

  measure: diff_dist_75 {
    label: "75th Percentile Distance Difference"
    group_label: "Difference from Average Distance Stats"
    type: percentile
    percentile: 75
    sql: ${difference_from_average_distance} ;;
  }


##additional metrics

  dimension: reporting_period {
    group_label: "Transaction Date"
    description: "This Month to Date vs Last Month to Date"
    sql: CASE
        WHEN EXTRACT(MONTH from ${transaction_raw}) = EXTRACT(MONTH from CURRENT_TIMESTAMP())
        AND ${transaction_raw} < CURRENT_TIMESTAMP()
        THEN 'This Month to Date'

        WHEN EXTRACT(MONTH from ${transaction_raw}) + 1 = EXTRACT(MONTH from CURRENT_TIMESTAMP())
        AND CAST(FORMAT_TIMESTAMP('%j', ${transaction_raw}) AS INT64) <= CAST(FORMAT_TIMESTAMP('%j', CURRENT_TIMESTAMP()) AS INT64)
        THEN 'Last Month to Date'

      END
       ;;
  }

  set: transaction_detail {
    fields: [transaction_date, transaction_day_of_week, transaction_time_of_day, client.name, cc_number, card.type, merchant, category, total_amount]
  }
}
