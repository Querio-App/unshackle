
view: merchant_fact {
  derived_table: {
    explore_source: card_transactions {
      column: merchant {}
      column: percent_fraudulent_transactions {}
      column: percent_fraudulent_amount {}
      column: total_amount {}
      column: count {}
      bind_all_filters: yes
    }
  }

  dimension: merchant {
    primary_key: yes
    hidden: yes
  }

  dimension: percent_fraudulent_transactions {
    type: number
    value_format_name: percent_2
    view_label: "Merchant"
    label: "Percent Fraudulent Transactions"
    description: "The percent of all transactions for the merchant that were fraudulent"
  }

  dimension: percent_fraudulent_amount {
    type: number
    value_format_name: percent_2
    view_label: "Merchant"
    label: "Percent Fraudulent Amount"
    description: "The percent of all transactions for the merchant that were fraudulent"
  }

  dimension: total_amount {
    type: number
    value_format_name: usd
    view_label: "Merchant"
    label: "Total Amount"
    description: "The total amount from all transactions for the merchant"
  }

  dimension: count {
    type: number
    view_label: "Merchant"
    label: "Number of Transaction"
    description: "The total number of transactions for the merchant"
  }

  dimension: percent_fraudulent_amount_tier {
    label: "Percent Fraudulent Amount Tier"
    view_label: "Merchant"
    sql: case when ${percent_fraudulent_transactions}*100 <.5 then '< .5 %'
            when ${percent_fraudulent_transactions}*100 <1 then '< 1 %'
            when ${percent_fraudulent_transactions}*100 <2 then '< 2%'
            else '> 2%' end ;;
  }


#   dimension: total_amount_tier {
#
#   }


}
