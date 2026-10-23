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
#   profit_and_loss (labeled as Income Statement)
#
# CUSTOMIZATIONS FOR INCOME STATEMENT {
# While the template_dashboard_navigation_ext provides much of the logic needed, customizations
# were required for income statement dashboards.
#
#   1. Added a new parameter called parameter_navigation_dashboard_style to indicate
#      which style of dashboard to display:
#         Subtotal Table or Marketplace Report Table
#
#   2. Updated dimension map_filter_numbers_to_dashboard_filter_names with:
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
#      Note that comparison_type is not include as its value is not passed between dashboards
#
#   3. Updated dash_bindings dimension to:
#       - use parameter_navigation_subject value to specify 2 sets of dashboards:
#       subject                     dashboard set
#       ---------------             ---------------
#       Subtotal Table              income_statement_subtotal_table
#                                   income_statement_subtotal_table_no_comparison
#       Marketplace Report Table    income_statement_mktplace_report_table
#                                   income_statement_mktplace_report_table_no_comparison
#
#       - and for each dashboard list the filters used on the dashboard that
#         should be passed between dashboards in the set:
#       dashboard name                                        link text               filters used
#       ---------------                                       --------------------    ----------
#       income_statement_subtotal_table                       With Comparisons        1,2,3,4,5,6,7,8
#       income_statement_subtotal_table_no_comparison         Without Comparisons     1,2,3,4,5,6,7,8
#       income_statement_mktplace_report_table                With Comparisons        1,2,3,4,5,6,7,8
#       income_statement_mktplace_report_table_no_comparison. Without Comparisons     1,2,3,4,5,6,7,8
#
#      - The required syntax for the dashboard name, link name and filter set are defined in these
#        constants found in the manifest file:
#           link_map_fin_dash_bindings_inc_subtotal
#           link_map_fin_dash_bindings_inc_subtotal_no
#           link_map_fin_dash_bindings_inc_mktplace
#           link_map_fin_dash_bindings_inc_mktplace_no
#
#   4. Updated hidden and label properties of filter1 to filter8.
#
#   5. Added view_label for how these navigation-related fields appear in the Explore.
#}
#
# HOW TO USE FOR NAVIGATION {
#   1. Add to an Explore using a bare join (required if including in dashboard with cross-filtering)
#         explore: profit_and_loss {
#         join: profit_and_loss_dashboard_navigation_ext {
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
#         Navigation Dashboard Style = Subtotal Table or Marketplace Report Table
#               (or if using LookML, subtotal or mktplace)
#
#   4. Add navigation filters to the visualization. These filters will "listen" to the dashboard filters.
#
#   5. Add Visualization to dashboard and edit dashboard to pass the dashboard filters
#    to Filters 1 to N accordingly.
#
#  Alternatively, you can edit the dashboard LookML and the "listen" property as shown in
#  the LookML example below:
#       - name: dashboard_navigation
#         explore: profit_and_loss
#         type: single_value
#         fields: [profit_and_loss_dashboard_navigation_ext.navigation_links]
#         filters:
#           profit_and_loss_dashboard_navigation_ext.parameter_navigation_focus_page: '1'
#           profit_and_loss_dashboard_navigation_ext.parameter_navigation_style: 'plain'
#           profit_and_loss_dashboard_navigation_ext.parameter_navigation_dashboard_style: 'subtotal'
#         show_single_value_title: false
#         show_comparison: false
#         listen:
#           hierarchy: profit_and_loss_dashboard_navigation_ext.filter1
#           display_timeframe: profit_and_loss_dashboard_navigation_ext.filter2
#           fiscal_timeframe: profit_and_loss_dashboard_navigation_ext.filter3
#           combine_timeframes: profit_and_loss_dashboard_navigation_ext.filter4
#           target_currency: profit_and_loss_dashboard_navigation_ext.filter5
#           company_code: profit_and_loss_dashboard_navigation_ext.filter6
#           ledger_name: profit_and_loss_dashboard_navigation_ext.filter7
#           top_hierarchy_level: profit_and_loss_dashboard_navigation_ext.filter8
#}
#########################################################}

include: "/views/core/template_dashboard_navigation_ext.view"

view: profit_and_loss_dashboard_navigation_ext {

  extends: [template_dashboard_navigation_ext]

#--> Added view label to consistent organization in Explore
  view_label: "@{label_view_for_dashboard_navigation}"

#--> Added new paramter for subject area that allows multiple dashboard sets to be defined.
    parameter: parameter_navigation_dashboard_style {
      type: unquoted
      label: "Which Dashboard Style to Use?"
      description: "Pick one of the two possible dashboards styles defined for Income Statement: Table with Subtotals or Report Table from Marketplace"
      allowed_value: {label:"Marketplace Report Table" value:"mktplace"}
      allowed_value: {label: "Subtotal Table" value:"subtotal" }
      default_value: "subtotal"
    }

#--> Added mapping for filter numbers to names
# Comparison Type filter value is not transferred between dashboards so not included in the mapping
    dimension: map_filter_numbers_to_dashboard_filter_names {
      sql: '1|hierarchy||2|display_timeframe||3|fiscal_timeframe||4|combine_timeframes||5|target_currency||6|company_code||7|ledger_name||8|top_hierarchy_level' ;;
      # sql: "1|date||2|business_unit||3|customer_type||4|customer_country" ;;
    }

#--> Added logic to define dashboard set based on the dashboard style selected with parameter_navigation_dashboard_style
# Uses constants to define the dashboard id|link text|filter set for each dashboard
    dimension: dash_bindings {
      hidden: yes
      type: string
      sql: {% assign dash_style = parameter_navigation_dashboard_style._parameter_value %}
          {% case dash_style %}
            {% when "mktplace" %}
            "@{link_map_fin_dash_bindings_inc_mktplace}||@{link_map_fin_dash_bindings_inc_mktplace_no}"
           {% when "subtotal" %}
            "@{link_map_fin_dash_bindings_inc_subtotal}||@{link_map_fin_dash_bindings_inc_subtotal_no}"
          {% endcase %}
          ;;
    }

#--> Updated hidden and label properties for filter1 to filter8
    filter: filter1 { hidden: no label: "hierarchy"}
    filter: filter2 { hidden: no label: "display_timeframe"}
    filter: filter3 { hidden: no label: "fiscal_timeframe"}
    filter: filter4 { hidden: no label: "combine_timeframes"}
    filter: filter5 { hidden: no label: "target_currency"}
    filter: filter6 { hidden: no label: "company_code"}
    filter: filter7 { hidden: no label: "ledger_name"}
    filter: filter8 { hidden: no label: "top_hierarchy_level"}

  }
