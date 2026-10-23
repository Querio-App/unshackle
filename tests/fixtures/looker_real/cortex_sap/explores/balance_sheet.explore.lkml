#########################################################{
# PURPOSE
# The Balance Sheet Explore supports the Balance Sheet dashboards
# and any adhoc querying of balance_sheet
#
# SOURCES
# See the "include: " statements below for specifc sources
#
# REFERENCED BY
#   LookML dashboards:
#     balance_sheet_template
#     balance_sheet_mktplace_report_table
#     balance_sheet_subtotal_table
#     balance_sheet_subtotal_table_no_comparison
#
# NOTES
#   - always_filter on hierarchy_name, chart_of_accounts, company_text and target_currency_tcurr
#   - sql_always_where to limit to single Client MANDT based on CLIENT_ID constant set in manifest file
#   - sql_always_where to limit to single Language based on user_language constant set in manifest file
#   - For small views that only add a few fields to the Explore, set the view label to "Balance Sheet" in the "join:" property.
#########################################################}

include: "/views/core/balance_sheet_rfn.view"
include: "/views/core/universal_ledgers_md_rfn.view"
include: "/views/core/balance_sheet_fiscal_periods_selected_sdt.view"
include: "/views/core/balance_sheet_hierarchy_selection_sdt.view"
include: "/views/core/balance_sheet_dashboard_navigation_ext.view"

explore: balance_sheet {
  description: "Balance sheet financial reporting"
  always_filter: {filters:[balance_sheet.hierarchy_name: "",balance_sheet.chart_of_accounts: "",balance_sheet.company_text: "",balance_sheet.target_currency_tcurr: ""]}

  # sql always filter on:
  #   - Client and Langugage as defined with constant
  sql_always_where: ${balance_sheet.client_mandt}='@{CLIENT_ID}' AND ${balance_sheet.language_key_spras} = @{user_language};;


  join: universal_ledgers_md {
    view_label: "Balance Sheet"
    type: left_outer
    relationship: many_to_one
    sql_on: ${balance_sheet.client_mandt} = ${universal_ledgers_md.client_mandt} AND
            ${balance_sheet.ledger_in_general_ledger_accounting} = ${universal_ledgers_md.ledger_rldnr} AND
            ${balance_sheet.language_key_spras} = ${universal_ledgers_md.language_langu};;
  }

  join: balance_sheet_fiscal_periods_selected_sdt {
    type: inner
    relationship: many_to_one
    sql_on: ${balance_sheet.hierarchy_name} = ${balance_sheet_fiscal_periods_selected_sdt.hierarchy_name} AND
            ${balance_sheet.company_code}= ${balance_sheet_fiscal_periods_selected_sdt.company_code} AND
            ${balance_sheet.fiscal_year} = ${balance_sheet_fiscal_periods_selected_sdt.fiscal_year} AND
            ${balance_sheet.fiscal_period} =  ${balance_sheet_fiscal_periods_selected_sdt.fiscal_period};;
  }

  join: balance_sheet_hierarchy_selection_sdt {
    view_label: "Balance Sheet"
    type: inner
    relationship: many_to_one
    sql_on: ${balance_sheet.client_mandt} = ${balance_sheet_hierarchy_selection_sdt.client_mandt} AND
            ${balance_sheet.hierarchy_name} = ${balance_sheet_hierarchy_selection_sdt.hierarchy_name} AND
            ${balance_sheet.chart_of_accounts} = ${balance_sheet_hierarchy_selection_sdt.chart_of_accounts} AND
            ${balance_sheet.language_key_spras} = ${balance_sheet_hierarchy_selection_sdt.language_key_spras} AND
            ${balance_sheet.node} = ${balance_sheet_hierarchy_selection_sdt.node};;
  }

  join: balance_sheet_dashboard_navigation_ext {
    relationship: one_to_one
    sql:  ;;
}
}
