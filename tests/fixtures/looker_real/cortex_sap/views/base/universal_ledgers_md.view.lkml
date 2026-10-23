view: universal_ledgers_md {
  sql_table_name: `@{GCP_PROJECT_ID}.@{REPORTING_DATASET}.UniversalLedgersMD` ;;

  dimension: addl_curr_role1_curt1 {
    type: string
    sql: ${TABLE}.AddlCurrRole1_CURT1 ;;
  }
  dimension: addl_curr_role2_curt2 {
    type: string
    sql: ${TABLE}.AddlCurrRole2_CURT2 ;;
  }
  dimension: addl_curr_role3_curt3 {
    type: string
    sql: ${TABLE}.AddlCurrRole3_CURT3 ;;
  }
  dimension: cash_ledger_indicator_xcash_ledger {
    type: string
    sql: ${TABLE}.CashLedgerIndicator_XCASH_LEDGER ;;
  }
  dimension: client_mandt {
    type: string
    sql: ${TABLE}.Client_MANDT ;;
  }
  dimension: company_currency_role_class {
    type: string
    sql: ${TABLE}.CompanyCurrencyRole_CLASS ;;
  }
  dimension: currency_gcurr {
    type: string
    sql: ${TABLE}.Currency_GCURR ;;
  }
  dimension: language_langu {
    type: string
    sql: ${TABLE}.Language_LANGU ;;
  }
  dimension: leading_ledger_indicator_xleading {
    type: string
    sql: ${TABLE}.LeadingLedgerIndicator_XLEADING ;;
  }
  dimension: ledger_rldnr {
    type: string
    sql: ${TABLE}.Ledger_RLDNR ;;
  }
  dimension: ledger_type {
    type: string
    sql: ${TABLE}.LedgerType ;;
  }
  dimension: logical_system_logsys {
    type: string
    sql: ${TABLE}.LogicalSystem_LOGSYS ;;
  }
  dimension: name_name {
    type: string
    sql: ${TABLE}.Name_NAME ;;
  }
  dimension: owner_application_appl {
    type: string
    sql: ${TABLE}.OwnerApplication_APPL ;;
  }
  dimension: sub_application_subappl {
    type: string
    sql: ${TABLE}.SubApplication_SUBAPPL ;;
  }
  dimension: valuation_view_valutyp {
    type: string
    sql: ${TABLE}.ValuationView_VALUTYP ;;
  }
  measure: count {
    type: count
    drill_fields: [name_name]
  }
}