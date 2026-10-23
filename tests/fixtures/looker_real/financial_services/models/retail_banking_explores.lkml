include: "/banking_and_card_views/*.view.lkml"

explore: card_transactions {
  label: "(4) Card Transactions"
  fields: [ALL_FIELDS*, -client.has_loan, -client.number_of_clients_with_loans, -client.percent_clients_with_loans, -client.number_of_clients_with_cards,
    -client.percent_clients_with_cards]
  join: card {
    relationship: many_to_one
    sql_on: ${card.card_number} = ${card_transactions.cc_number} ;;
  }
  join: disp {
    relationship: many_to_one
    sql_on: ${card.disp_id} = ${disp.disp_id} ;;
  }
  join: client {
    relationship: one_to_many
    sql_on: ${disp.client_id} = ${client.client_id} ;;
  }
  join: merchant_fact {
    relationship: many_to_one
    sql_on: ${merchant_fact.merchant} = ${card_transactions.merchant} ;;
  }
  join: card_transactions_with_zip {
    relationship: one_to_one
    sql_on: ${card_transactions.trans_id} =  ${card_transactions_with_zip.trans_id};;
  }
  join: account {
    relationship: many_to_one
    sql_on: ${account.account_id} = ${disp.account_id} ;;
  }
  join: client_card_fact {
    relationship: one_to_one
    sql_on: ${client.client_id}=${client_card_fact.client_id} ;;
  }
  join: current_fraud_prediction{
    view_label: "Card Transactions"
    relationship: one_to_one
    sql_on: ${card_transactions.trans_id} = ${current_fraud_prediction.trans_id} ;;
  }
}

explore: balances_fact {
  fields: [ALL_FIELDS*, -client.has_loan, -client.number_of_clients_with_loans, -client.percent_clients_with_loans]
  # conditionally_filter: {
  #   filters: {
  #     field: balance_date
  #     value: "7 days"
  #   }
  # }
  label: "(2) Point in Time Balance"
  view_label: "Balance"
  join:  account {
    relationship: many_to_one
    sql_on: ${balances_fact.account_id} = ${account.account_id} ;;
  }
  join: trans {
    view_label: "Transaction"
    relationship: one_to_many
    sql_on: ${trans.account_id} = ${account.account_id} and ${trans.transaction_date} = ${balances_fact.balance_date} ;;
  }
  join: disp {
    relationship: one_to_one
    sql_on: ${account.account_id} = ${disp.account_id} ;;
  }
  join: account_fact {
    relationship: one_to_one
    sql_on: ${account.account_id} = ${account_fact.account_id} ;;
  }
  join: district {
    relationship: one_to_one
    sql_on: ${account.district_id} = ${district.district_id} ;;
  }
  join: client {
    relationship: one_to_one
    sql_on: ${disp.client_id} = ${client.client_id} ;;
  }
  join: card {
    relationship: one_to_many
    sql_on: ${card.disp_id} = ${disp.disp_id} ;;
  }
}
