#########################################################{
# PURPOSE
# Dynamically generates html links (including filters) to support navigation between
# related income statement dashboards.
#
# SOURCE
# Extends View template_dashboard_navigation_ext
#
# REFERENCED BY
# Explores:
#   balance_sheet
#
# CUSTOMIZATIONS FOR BALANCE SHEET {
# While the template_dashboard_navigation_ext provides much of the logic needed, customizations
# were required for balance sheet dashboards.
#
#   1. Updated dimension map_filter_numbers_to_dashboard_filter_names with:
#       - filter number-to-dashboard filter values as follows:
#           1   hierarchy
#           2   display_timeframe
#           3   fiscal_timeframe
#           4   combine_timeframes
#           5   target_currency
#           6   company_code
#           7   ledger_name
#           8   top_hierarchy_level
#       - example syntax:
#              sql: '1|hierarchy||2|display_timeframe||3|fiscal_timeframe' ;;
#      Note that comparison_type, custom_comparison_period are not included
#      as their values are not passed between dashboards
#
#   2. Updated dash_bindings dimension:
#       - Define for each dashboard, the name, link text and filter set used:
#       dashboard name                               link text               filters used
#       ---------------                              --------------------    ----------
#       balance_sheet_subtotal_table                 With Comparisons        1,2,3,4,5,6,7
#       balance_sheet_subtotal_table_no_comparison   Without Comparisons     1,2,3,4,5,6,7
#
#      - The required syntax for the dashboard name, link name and filter set are defined in these
#        constants found in the manifest file:
#           link_map_fin_dash_bindings_bal_subtotal
#           link_map_fin_dash_bindings_bal_subtotal_no
#
#   3. Updated hidden and label properties of filter1 to filter7.
#
#   4. Added view_label for how these navigation-related fields appear in the Explore.
#}
#
# HOW TO USE FOR NAVIGATION {
#   1. Add to an Explore using a bare join (required if including in dashboard with cross-filtering)
#         explore: balance_sheet {
#         join: balance_sheet_dashboard_navigation_ext {
#           relationship: one_to_one
#           sql:  ;;
#           }}
#
#   2. Open the Explore and add "Dashboard Links" dimension to a Single Value Visualization.
#
#   3. Add these navigation parameters to visualization and set to desired values:
#         Navigation Style = Plain Hyperlinks (or if using LookML, plain)
#         Navigation Focus Page = 1 when adding to first dashboard listed. Set to 2 if
#             adding viz to second dashboard and so on.
#
#   4. Add navigation filters to the visualization. These filters will "listen" to the dashboard filters.
#
#   5. Add Visualization to dashboard and edit dashboard to pass the dashboard filters
#      to Filters 1 to N accordingly.
#
#  Alternatively, you can edit the dashboard LookML and the "listen" property as shown in
#  the LookML example below:
#   - name: dashboard_navigation
#     explore: balance_sheet
#     type: single_value
#     fields: [balance_sheet_dashboard_navigation_ext.navigation_links]
#     filters:
#       balance_sheet_dashboard_navigation_ext.parameter_navigation_focus_page: '1'
#       balance_sheet_dashboard_navigation_ext.parameter_navigation_style: 'plain'
#     show_single_value_title: false
#     show_comparison: false
#     listen:
#       fiscal_period: balance_sheet_dashboard_navigation_ext.filter1
#       target_currency: balance_sheet_dashboard_navigation_ext.filter2
#       hierarchy: balance_sheet_dashboard_navigation_ext.filter3
#       chart_of_accounts: balance_sheet_dashboard_navigation_ext.filter4
#       company_code: balance_sheet_dashboard_navigation_ext.filter5
#       ledger_name: balance_sheet_dashboard_navigation_ext.filter6
#       top_hierarchy_level: balance_sheet_dashboard_navigation_ext.filter7
#}
#########################################################}

include: "/views/core/template_dashboard_navigation_ext.view"

view: balance_sheet_dashboard_navigation_ext {

  extends: [template_dashboard_navigation_ext]

#--> Added view label to consistent organization in Explore
  view_label: "@{label_view_for_dashboard_navigation}"


#--> Added mapping for filter numbers to names
# Comparison Type and Custom Comparison Period filters are not transferred between dashboards so are not included in the mapping
  dimension: map_filter_numbers_to_dashboard_filter_names {
    sql: '1|fiscal_period||2|target_currency||3|hierarchy||4|chart_of_accounts||5|company_code||6|ledger_name||7|top_hierarchy_level' ;;
    # sql: "1|date||2|business_unit||3|customer_type||4|customer_country" ;;
  }

#--> Added logic to define each dashboard set (name, link text, filters)
# Uses constants to define the dashboard id|link text|filter set for each dashboard
  dimension: dash_bindings {
    hidden: yes
    type: string
    sql: "@{link_map_fin_dash_bindings_bal_subtotal}||@{link_map_fin_dash_bindings_bal_subtotal_no}"
          ;;
  }

#--> Updated hidden and label properties for filter1 to filter8
  filter: filter1 { hidden: no label: "fiscal_period"}
  filter: filter2 { hidden: no label: "target_currency"}
  filter: filter3 { hidden: no label: "hierarchy"}
  filter: filter4 { hidden: no label: "chart_of_accounts"}
  filter: filter5 { hidden: no label: "company_code"}
  filter: filter6 { hidden: no label: "ledger_name"}
  filter: filter7 { hidden: no label: "top_hierarchy_level"}

}
