view: client_fact {
  derived_table: {
    explore_source: account {
      column: client_id {field: client.client_id}
      column: number_of_credit_cards {field: card.number_of_credit_cards}
      bind_all_filters: yes
    }
  }

  dimension: client_id {}
  dimension: number_of_credit_cards {}
}

view: client_card_fact {
  derived_table: {
    sql_trigger_value: select max(trans_date) from `looker-private-demo.retail_banking.card_transactions`;;
    explore_source: card_transactions {
      column: client_id { field: client.client_id }
      column: count {}
      column: percent_fraudulent_amount {}
      column: percent_fraudulent_transactions {}
      column: average_transaction_distance {}
      column: transaction_distance_75th_percentile {}
    }
  }
  dimension: client_id {
    primary_key: yes
    hidden: yes
    value_format: "0"
    type: number
  }

  dimension: count {
    view_label: "Client"
    label: "Number of Transactions"
    type: number
  }

  dimension: percent_fraudulent_amount {
    label: "Fraudulent Amount Percentage"
    view_label: "Client"
    value_format_name: percent_2
    type: number
  }

  dimension: percent_fraudulent_transactions {
    label: "Fraudulent Transaction Percentage"
    view_label: "Client"
    value_format_name: percent_2
    type: number
  }

  dimension: average_transaction_distance {
    label: "Average Transaction Distance"
    view_label: "Client"
    description: "The average miles from the customers home to where transaction took place"
    value_format_name: decimal_2
    type: number
  }

  dimension: transaction_distance_75th_percentile {
    label: "Transaction Distance 75th Percentile"
    view_label: "Client"
    description: "The 75th percentile for number of miles from the customers home to where transaction took place"
    value_format_name: decimal_2
    type: number
  }
}
