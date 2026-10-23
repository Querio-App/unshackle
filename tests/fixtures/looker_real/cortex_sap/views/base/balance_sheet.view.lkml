view: balance_sheet {
  sql_table_name: `@{GCP_PROJECT_ID}.@{REPORTING_DATASET}.BalanceSheet` ;;

  dimension: amount_in_local_currency {
    type: number
    description: "Amount In Local Currency"
    sql: ${TABLE}.AmountInLocalCurrency ;;
  }
  dimension: amount_in_target_currency {
    type: number
    description: "Amount In Target Currency"
    sql: ${TABLE}.AmountInTargetCurrency ;;
  }
  dimension: avg_exchange_rate {
    type: number
    description: "Avg Exchange Rate"
    sql: ${TABLE}.AvgExchangeRate ;;
  }
  dimension: business_area {
    type: string
    description: "Business Area"
    sql: ${TABLE}.BusinessArea ;;
  }
  dimension: chart_of_accounts {
    type: string
    description: "Chart Of Accounts"
    sql: ${TABLE}.ChartOfAccounts ;;
  }
  dimension: client {
    type: string
    description: "Client (Mandant)"
    sql: ${TABLE}.Client ;;
  }
  dimension: company_code {
    type: string
    description: "Company Code"
    sql: ${TABLE}.CompanyCode ;;
  }
  dimension: company_text {
    type: string
    description: "Company Text"
    sql: ${TABLE}.CompanyText ;;
  }
  dimension: cumulative_amount_in_local_currency {
    type: number
    description: "Cumulative Amount In Local Currency"
    sql: ${TABLE}.CumulativeAmountInLocalCurrency ;;
  }
  dimension: cumulative_amount_in_target_currency {
    type: number
    description: "Cumulative Amount In Target Currency"
    sql: ${TABLE}.CumulativeAmountInTargetCurrency ;;
  }
  dimension: currency_key {
    type: string
    description: "Currency Key"
    sql: ${TABLE}.CurrencyKey ;;
  }
  dimension: exchange_rate {
    type: number
    description: "Exchange Rate"
    sql: ${TABLE}.ExchangeRate ;;
  }
  dimension: fiscal_period {
    type: string
    description: "Fiscal Period"
    sql: ${TABLE}.FiscalPeriod ;;
  }
  dimension: fiscal_quarter {
    type: number
    description: "Fiscal Quarter"
    sql: ${TABLE}.FiscalQuarter ;;
  }
  dimension: fiscal_year {
    type: string
    description: "Fiscal Year"
    sql: ${TABLE}.FiscalYear ;;
  }
  dimension: hierarchy_name {
    type: string
    description: "Hierarchy Name"
    sql: ${TABLE}.HierarchyName ;;
  }
  dimension: is_leaf_node {
    type: yesno
    description: "Is Leaf Node"
    sql: ${TABLE}.IsLeafNode ;;
  }
  dimension: language_key_spras {
    type: string
    description: "Language Key"
    sql: ${TABLE}.LanguageKey_SPRAS ;;
  }
  dimension: ledger_in_general_ledger_accounting {
    type: string
    description: "Ledger In General Ledger Accounting"
    sql: ${TABLE}.LedgerInGeneralLedgerAccounting ;;
  }
  dimension: level {
    type: string
    description: "Level"
    sql: ${TABLE}.Level ;;
  }
  dimension: max_exchange_rate {
    type: number
    description: "Max Exchange Rate"
    sql: ${TABLE}.MaxExchangeRate ;;
  }
  dimension: node {
    type: string
    description: "Node"
    sql: ${TABLE}.Node ;;
  }
  dimension: node_text {
    type: string
    description: "Node Text"
    sql: ${TABLE}.NodeText ;;
  }
  dimension: parent {
    type: string
    description: "ID"
    sql: ${TABLE}.Parent ;;
  }
  dimension: parent_text {
    type: string
    description: "Parent Text"
    sql: ${TABLE}.ParentText ;;
  }
  dimension: target_currency_tcurr {
    type: string
    description: "To-currency"
    sql: ${TABLE}.TargetCurrency_TCURR ;;
  }
  measure: count {
    type: count
    drill_fields: [hierarchy_name]
  }
}