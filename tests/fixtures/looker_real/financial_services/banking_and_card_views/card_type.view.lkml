view: card_type {
  view_label: "Credit Card"
  sql_table_name: `looker-private-demo.retail_banking`.card_type_facts ;;

  dimension: annual_fee {
    label: "Annual Fee"
    type: number
    sql: ${TABLE}.annual_fee ;;
    value_format_name: usd
  }

  dimension: daily_interest_rate {
    hidden: yes
    type: number
    sql: ${TABLE}.daily_interest_rate/10 ;;
    value_format_name: percent_2
  }

  dimension: APR {
    label: "APR"
    type: number
    sql: (${daily_interest_rate}*365);;
    value_format_name: percent_2
  }

  dimension: primary_key {
    primary_key: yes
    hidden: yes
    sql: ${TABLE}.type ;;
  }

  dimension: type {
    hidden: yes
    description: "Name of the Credit Card"
    type: string
    case: {
      when: {
        label: "Looker Junior"
        sql: ${TABLE}.type = 'junior' ;;
      }
      when: {
        label: "Looker Emerald"
        sql: ${TABLE}.type = 'junior2' ;;
      }
      when: {
        label: "Looker Ruby"
        sql: ${TABLE}.type = 'classic' ;;
      }
      when: {
        label: "Looker Ruby Preferred"
        sql: ${TABLE}.type = 'classic2' ;;
      }
      when: {
        label: "Looker Business Reserve"
        sql: ${TABLE}.type = 'gold' ;;
      }
      else: "Null"
    }
    drill_fields: [card_transactions.merchant]
  }

  measure: total_annual_fee {
    type: sum_distinct
    sql_distinct_key: ${card.card_id} ;;
    sql: ${annual_fee} ;;
    value_format_name: usd
  }


#
#   measure: count {
#     type: count
#     drill_fields: []
#   }
}
