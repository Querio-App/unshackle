#########################################################{
# PURPOSE
# The profit_and_loss Explore supports the Income Statement dashboards
# and any adhoc querying of profit_and_loss view
#
# SOURCES
# See the "include: " statements below for specifc sources
#
# REFERENCED BY
#   LookML dashboards:
#     income_statement_template
#     income_statement_mktplace_report_table
#     income_statement_mktplace_report_table_no_comparison
#     income_statement_subtotal_table
#     income_statement_subtotal_table_no_comparison
#
# NOTES
#   - Explore and base view labeled Income Statement (instead of Profit and Loss)
#   - always_filter on glhierarchy, company_text and target_currency_tcurr
#   - sql_always_where to limit to single Client MANDT based on CLIENT_ID constant set in manifest file
#   - sql_always_where to limit to single Language based on user_language constant set in manifest file
#   - For small views that only add a few fields to the Explore, set the view label to "Balance Sheet" in the "join:" property.
#########################################################}

include: "/views/core/profit_and_loss_rfn.view"
include: "/views/core/universal_ledgers_md_rfn.view"
include: "/views/core/profit_and_loss_03_selected_fiscal_periods_sdt.view"
include: "/views/core/profit_and_loss_hierarchy_selection_sdt.view"
include: "/views/core/profit_and_loss_dashboard_navigation_ext.view"

explore: profit_and_loss {
  label: "Income Statement"
  description: "Profit and Loss financial reporting"

  always_filter: {filters:[profit_and_loss.glhierarchy: "",profit_and_loss.company_text: "",profit_and_loss.target_currency_tcurr: ""]}

  sql_always_where: ${profit_and_loss.client_mandt}='@{CLIENT_ID}' AND ${profit_and_loss.language_key_spras} = @{user_language} ;;

  join: universal_ledgers_md {
    view_label: "Income Statement"
    type: left_outer
    relationship: many_to_one
    sql_on: ${profit_and_loss.client_mandt} = ${universal_ledgers_md.client_mandt} AND
            ${profit_and_loss.ledger_in_general_ledger_accounting} = ${universal_ledgers_md.ledger_rldnr} AND
            ${profit_and_loss.language_key_spras} = ${universal_ledgers_md.language_langu};;
  }

  join: profit_and_loss_03_selected_fiscal_periods_sdt  {
    type: inner
    relationship: many_to_many
    sql_on:
      ${profit_and_loss.glhierarchy} = ${profit_and_loss_03_selected_fiscal_periods_sdt.glhierarchy} AND
      ${profit_and_loss.company_code} = ${profit_and_loss_03_selected_fiscal_periods_sdt.company_code} AND
      ${profit_and_loss.fiscal_year} = ${profit_and_loss_03_selected_fiscal_periods_sdt.fiscal_year} AND
      ${profit_and_loss.fiscal_period} = ${profit_and_loss_03_selected_fiscal_periods_sdt.fiscal_period};;
  }

  join: profit_and_loss_hierarchy_selection_sdt {
    type: inner
    relationship: many_to_one
    sql_on: ${profit_and_loss.client_mandt} = ${profit_and_loss_hierarchy_selection_sdt.client_mandt} AND
            ${profit_and_loss.glhierarchy} = ${profit_and_loss_hierarchy_selection_sdt.glhierarchy} AND
            ${profit_and_loss.chart_of_accounts} = ${profit_and_loss_hierarchy_selection_sdt.chart_of_accounts} AND
            ${profit_and_loss.language_key_spras} = ${profit_and_loss_hierarchy_selection_sdt.language_key_spras} AND
            ${profit_and_loss.glnode} = ${profit_and_loss_hierarchy_selection_sdt.glnode} ;;
  }

  join: profit_and_loss_dashboard_navigation_ext {
    relationship: one_to_one
    sql:  ;;
}

}
