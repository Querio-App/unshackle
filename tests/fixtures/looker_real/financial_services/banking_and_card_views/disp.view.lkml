view: disp {
  label: "Disposition"
  sql_table_name: `looker-private-demo.retail_banking.disp` ;;
  drill_fields: [disp_id]

  dimension: disp_id {
    hidden: yes
    primary_key: yes
    type: number
    sql: ${TABLE}.disp_id ;;
    value_format_name: id
  }

  dimension: account_id {
    type: number
    hidden: yes
    sql: ${TABLE}.account_id ;;
    value_format_name: id
  }

  dimension: client_id {
    type: number
    hidden: yes
    sql: ${TABLE}.client_id ;;
    value_format_name: id
  }

  dimension: type {
    hidden: yes
    description: "Type of disposition: owner or user"
    type: string
    sql: ${TABLE}.type ;;
  }

  measure: count {
    hidden: yes
    type: count
    drill_fields: [disp_id, account.account_id, client.client_id, card.count]
  }
}
