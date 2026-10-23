connection: "looker-private-demo"

include: "/banking_and_card_views/*.view.lkml"
include: "/geo_views/*.view.lkml"
include: "/dashboards/*.dashboard"

include:"retail_banking_explores.lkml"

map_layer: us_census_places {
  # file: "/geo_views/city_limits.json"
  url: "https://rawcdn.githack.com/llooker/retail_banking_city_limits/2812bbf1778c54a204a6be9ce82d45cb37f5d11a/city_limits.json"
  property_key: "places"
}

persist_for: "24 hours"

datagroup: retail_banking_etl {
  sql_trigger: SELECT max(date) FROM retail_banking.loan ;;
  max_cache_age: "24 hours"
}


explore: account {
  label: "(1) Accounts, Loans and Cards"
  join: district {
    relationship: many_to_one
    sql_on: ${account.district_id} = ${district.district_id} ;;
  }
  join: loan {
    relationship: one_to_many
    sql_on: ${account.account_id} = ${loan.account_id} ;;
  }
  join: disp {
    relationship: one_to_many
    sql_on: ${account.account_id} = ${disp.account_id} ;;
  }
  join: card {
    type: full_outer
    relationship: one_to_one
    sql_on: ${disp.disp_id} = ${card.disp_id} ;;
  }
  join: client {
    relationship: one_to_one
    sql_on: ${disp.client_id} = ${client.client_id} ;;
  }
  join: client_fact {
    view_label: "Client"
    fields: [client_fact.number_of_credit_cards]
    relationship: one_to_one
    sql_on: ${client.client_id} = ${client_fact.client_id} ;;
  }
  aggregate_table: simple_rollup {
    query: {
      dimensions: [loan.grant_date,client.customer_segment, loan.duration, loan.status]
      measures: [loan.average_loan_amount, loan.number_of_loans, loan.percent_paid, loan.total_loan_value, loan.total_number_of_payments]
      filters: [loan.grant_date: "12 months"]
      timezone: "America/Los_Angeles"
    }
    materialization: {
      datagroup_trigger: retail_banking_etl
    }
  }
}





explore: trans {
  fields: [ALL_FIELDS*, -client.has_loan, -client.number_of_clients_with_loans, -client.percent_clients_with_loans]
  label: "(3) Account Transactions"
  join: account {
    relationship: many_to_one
    sql_on: ${trans.account_id} = ${account.account_id} ;;
  }
  join: disp {
    relationship: one_to_many
    sql_on: ${account.account_id} = ${disp.account_id} ;;
  }
  join: card {
    relationship: one_to_one
    sql_on: ${disp.disp_id} = ${card.disp_id} ;;
  }
  join: district {
    relationship: many_to_one
    sql_on: ${account.district_id} = ${district.district_id} ;;
  }
  join: client {
    relationship: one_to_one
    sql_on: ${disp.client_id} = ${client.client_id} ;;
  }
}

explore: card_payments {
  label: "(5) Card Payments"
  fields: [ALL_FIELDS*, -card.days_between_account_signup, -client.has_loan, -client.number_of_clients_with_loans, -client.percent_clients_with_loans]
  join: card_payment_dates {
    relationship: many_to_one
    sql_on: ${card_payments.card_id} = ${card_payment_dates.card_id} and ${card_payments.period_start_raw} = ${card_payment_dates.period_start_raw} ;;
  }
  join: card {
    relationship: many_to_one
    sql_on: ${card.card_id} = ${card_payments.card_id} ;;
  }
  join: card_type {
    relationship: many_to_one
    sql_on: ${card.type} = ${card_type.type} ;;
  }
  join: disp {
    relationship: many_to_one
    sql_on: ${card.disp_id} = ${disp.disp_id} ;;
  }
  join: account {
    relationship: many_to_one
    sql_on: ${disp.account_id} = ${account.account_id} ;;
  }
  join: client {
    relationship: one_to_one
    sql_on: ${client.client_id} = ${disp.client_id} ;;
  }
}

explore: card_order_sequence {
  hidden: yes
  fields: [ALL_FIELDS*, -card_order_sequence.days_between_account_signup, -repeat_card_purchase.days_between_account_signup,
            -second_repeat_card_purchase.days_between_account_signup,-card_order_sequence.months_between_signup, -second_repeat_card_purchase.months_between_signup,
            -client.has_card,-client.has_loan,-client.number_of_clients_with_loans, -client.percent_clients_with_loans,-client.number_of_clients_with_cards,
    -client.percent_clients_with_cards]
  join: repeat_card_purchase {
    view_label: "Repeat Card Purchase"
    from: card_order_sequence
    relationship: one_to_many
    type: left_outer
    sql_on: ${card_order_sequence.client_id} = ${repeat_card_purchase.client_id}
    and ${card_order_sequence.customer_card_sequence} < ${repeat_card_purchase.customer_card_sequence};;
  }
  join: second_repeat_card_purchase {
    view_label: "Second Repeat Card Purchase"
    from: card_order_sequence
    relationship: one_to_many
    type: left_outer
    sql_on: ${second_repeat_card_purchase.client_id} = ${repeat_card_purchase.client_id}
      and ${repeat_card_purchase.customer_card_sequence} < ${second_repeat_card_purchase.customer_card_sequence};;
  }
  join: disp {
    relationship: one_to_one
    sql_on: ${card_order_sequence.disp_id} = ${disp.disp_id}  ;;
  }
  join: account {
    relationship: many_to_one
    sql_on: ${disp.account_id} = ${account.account_id} ;;
  }
  join: client {
    relationship: one_to_one
    sql_on: ${disp.client_id} = ${client.client_id} ;;
  }
}


### location explores
explore: banks_by_zip {
  hidden: yes
}

explore: acs_changes_2011_2017  {
  hidden: yes
}

explore: acs_2017_zipcodes {
  hidden: yes
}

explore: acs_2011_zipcodes {
  hidden: yes
}

explore: acs_2017_zipcodes_and_banks {
  hidden: yes
}


explore: branches {
  hidden: yes
  from: district
  join: acs_2017_zipcodes_and_banks {
    relationship: many_to_one
    view_label: "ACS + FDCI Public Data"
    sql_on: ${branches.zipcode} = ${acs_2017_zipcodes_and_banks.Zip_Code} ;;
  }
}
