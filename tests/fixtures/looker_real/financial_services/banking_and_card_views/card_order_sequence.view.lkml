include: "*card.view"

view: card_order_sequence {
  extends: [card]
  derived_table: {
    sql: select card.*, disp.client_id, rank() over (partition by disp.client_id order by card.date) as rank
  from `looker-private-demo.retail_banking.disp` as disp
    inner join `looker-private-demo.retail_banking.card` as card
      on disp.disp_id = card.disp_id;;
    sql_trigger_value: select max(date) from `looker-private-demo.retail_banking.trans`;;

  }

  dimension: type {
    label: "Type"
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
      else: "No Card"
    }
  }

  dimension: client_id {
    hidden: yes
    sql: ${TABLE}.client_id ;;
  }

  dimension: customer_card_sequence {
    label: "Customer Card Sequence"
    type: number
    description: "The order in which the card was purchased by the disposition"
    sql: ${TABLE}.rank ;;
  }

  dimension: is_first_purchase {
    label: "Is First Purchase"
    type: yesno
    description: "Is this card the client's first from us?"
    sql: ${customer_card_sequence} = 1 ;;
  }

  dimension: months_between_signup {
    label: "Months Between Signup"
    description: "The days between signing up for the first card and signing up for the next card"
    type: duration_month
    sql_start:  ${card_order_sequence.create_raw};;
    sql_end:  ${repeat_card_purchase.create_raw};;
  }


}
