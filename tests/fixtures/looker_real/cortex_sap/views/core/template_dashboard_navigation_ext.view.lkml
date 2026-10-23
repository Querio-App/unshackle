#########################################################{
# PURPOSE
# Provides a template for dynamically creating and formatting HTML links for
# navigation between dashboards, including transferring filter values.
#
# SOURCE
# none (This template can only be EXTENDED into another view)
#
# STYLES
# This template provides three styles for the html links. Pick which one to use
# with parameter_navigation_style
#   1. buttons
#          parameter label: "Buttons"
#          parameter value: "buttons"
#   2. tabbed
#          parameter label: "Tabs"
#          parameter value: "tabs"
#   3. plan hyperlink
#          parameter label: "Plain Hyperlinks"
#          parameter value:  "plain"
#
# To add or modify styles, edit or add related Constants in the Manifest file
#
# FILTERS
# This template allows a dashboard to pass up to 20 possible filters. The dimensions are
# named filter1 to filter20 and hidden by default.
# Modify this template to add more filter dimensions as needed.
#
#
# STEPS TO EXTEND THIS TEMPLATE {
# When extending this template, make the REQUIRED customizations by following these steps:
# 1. Create new view
# 2. Add "extend: template_dashboard_navigation_ext" property (use name of this view)
# 3. Edit dimension map_filter_numbers_to_dashboard_filter_names.
#     - Update the "sql" property with the mapping of filter numbers to dashboard filter names.
#     - For each filter that must be passed between dashboards, assign a generic filter number 1 to N.
#     - This can be a broad list of filters that do not need to appear on every dashboard but should appear on at least 2 dashboards.
#     - Filters unique to a single dashboard do not need to be included here (as values do not pass to other dashboards).
#     - Use | between number and dashboard filter name
#     - Use || between each filter
#     - Note if dashboard filter name contains a space use + to represent the space (e.g. Order Date should be entered as Order+Date)
#     - You will specify the specific set of filters used in a dashboard in the dimension dash_bindings
#     - An example for 4 dashboard filters:
#           sql: '1|date||2|business_unit||3|customer_type||4|customer_country' ;;
#
# 4. Edit dash_bindings dimension
#    - Update the "sql" property with ID, Link Text and Filter Set of each dashboard:
#       ID - For UDD dashboards use numeric id. For a LookML dashboard, use dashboard name with or without model name.
#           131
#           model_name::dashboard_name1
#           dashboard_name1
#
#       Link Text (e.g., Sales Performance)
#
#       Filter Set used on the given dashboard. Use numbers 1 to N as specified separated by comma (e.g., 1,2 or 1,3,4).
#         Use numbers as specified in dimension map_filter_numbers_to_dashboard_filter_names
#
#     - Use | between each dashboard property.
#     - Use || between each dashboard set.
#     - Examples:
#       sql: '131|Dashboard 1 Link Text|1,2||132|Dashboard 2 Link Text|1,2,3,4' ;;
#       sql: 'dashboard_name1|Dashboard 1|1,2||dashboard_name2|Dashboard 2|1,2,3,4';;
#       sql: 'model_name::dashboard_name1|Dashboard 1|1,2||model_name::dashboard_name2|Dashboard 2|1,2,3,4';;
#
# 5. Update parameter_tab_focus with any extra values needed to match the number of dashboards.
#    Note, when extending, allowed values are additive, so only add values beyond 2.
#
# 6. Edit filter1 to filterN (up to 20) dimensions to set the type, unhide and/or label.
#    Make sure the filter dimension's type matches the field it is mapped to. For example, if filter1 is linked to a date field,
#    change the type to date. The filter dimensions are string by default.
#    Note if only using in LookML dashboards, the filter dimensions can remain hidden.
#
#}
#
# USING IN A DASHBOARD {
# 1. Once the extending view has been created and modified, add to an Explore using a bare join:
#       explore: sales_orders {
#           join: otc_dashboard_navigation_xvw {
#           view_label: "🛠 Dashboard Navigation"
#           relationship: one_to_one
#           sql:  ;;
#       }
#       }
# 2. Open the Explore and add "Dashboard Links" dimension to a Single Value Visualization
# 3. Add the navigation parameters to visualization and set to desired values
#           Navigation Style = "Buttons"
#           Navigation Focus Page = 1 for Dashboard 1. Set to 2 for Dashboard 2 and so on
# 4. Add navigation filters to the visualization. These filters will "listen" to the dashboard filters.
# 5. Add Visualization to dashboard and edit dashboard to pass the dashboard filters
#    to Filters 1 to N accordingly.
#
#    Alternatively, you can edit the dashboard LookML and the "listen" property as shown in
#    the LookML example below:
#       - name: navigation
#         explore: sales_orders
#         type: single_value
#         fields: [otc_dashboard_navigation_xvw.navigation_links]
#         filters:
#           otc_dashboard_navigation_xvw.parameter_navigation_focus_page: '1'
#           otc_dashboard_navigation_xvw.parameter_navigation_style: 'buttons'
#         show_single_value_title: false
#         show_comparison: false
#         listen:
#           date: otc_dashboard_navigation_xvw.filter1
#           business_unit: otc_dashboard_navigation_xvw.filter2
#           customer_type: otc_dashboard_navigation_xvw.filter3
#           customer_customer_country: otc_dashboard_navigation_xvw.filter4
#           customer_name: otc_dashboard_navigation_xvw.filter5
#           target_currency: otc_dashboard_navigation_xvw.filter6
#}
#########################################################}

view: template_dashboard_navigation_ext {
  extension: required

  view_label: "@{label_view_for_dashboard_navigation}"

#########################################################
# Fields *requiring override* in extension
#{

  dimension: map_filter_numbers_to_dashboard_filter_names {
    hidden: yes
    type: string
    sql: '1|date||2|business_unit||3|customer_type||4|customer_country' ;;
  }

  dimension: dash_bindings {
    hidden: yes
    type: string
    sql: '131|Dashboard 1||132|Dashboard 2' ;;
  }

#} end required overrides

#########################################################
# Fields *optional override* in extension
#{

  # use parameter to choose navigation style
  parameter: parameter_navigation_style {
    hidden: no
    type: unquoted
    label: "Navigation Style"
    description: "Select dashboard navigation style (e.g., Buttons, Tabs, Plain Hyperlinks)"
    allowed_value: {label: "Buttons" value: "buttons"}
    allowed_value: {label: "Tabs" value: "tabs"}
    allowed_value: {label: "Plain Hyperlinks" value: "plain"}
    default_value: "tabs"
  }

  # use parameter to set focus page until bug with _explore._dashboard_url is fixed
  # update allowed values to match number of dashboards defined in extension
  parameter: parameter_navigation_focus_page {
    hidden: no
    label: "Navigation Focus Page"
    description: "Used in dashboard navigation to set focus on selected dashboard page"
    type: unquoted
    allowed_value: {value:"1"}
    allowed_value: {value:"2"}
    default_value: "1"
  }

  # ** if using to build UDD dashboards, override hidden and label accordingly **
  # ** Add more as required, currently supports 20 **
  filter: filter1 { hidden: yes group_label: "Navigation Filters" label: "filter1" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter2 { hidden: yes group_label: "Navigation Filters" label: "filter2" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter3 { hidden: yes group_label: "Navigation Filters" label: "filter3" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter4 { hidden: yes group_label: "Navigation Filters" label: "filter4" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter5 { hidden: yes group_label: "Navigation Filters" label: "filter5" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter6 { hidden: yes group_label: "Navigation Filters" label: "filter6" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter7 { hidden: yes group_label: "Navigation Filters" label: "filter7" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter8 { hidden: yes group_label: "Navigation Filters" label: "filter8" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter9 { hidden: yes group_label: "Navigation Filters" label: "filter9" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter10 { hidden: yes group_label: "Navigation Filters" label: "filter10" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter11 { hidden: yes group_label: "Navigation Filters" label: "filter11" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter12 { hidden: yes group_label: "Navigation Filters" label: "filter12" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter13 { hidden: yes group_label: "Navigation Filters" label: "filter13" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter14 { hidden: yes group_label: "Navigation Filters" label: "filter14" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter15 { hidden: yes group_label: "Navigation Filters" label: "filter15" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter16 { hidden: yes group_label: "Navigation Filters" label: "filter16" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter17 { hidden: yes group_label: "Navigation Filters" label: "filter17" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter18 { hidden: yes group_label: "Navigation Filters" label: "filter18" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter19 { hidden: yes group_label: "Navigation Filters" label: "filter19" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}
  filter: filter20 { hidden: yes group_label: "Navigation Filters" label: "filter20" description: "Add to visualization with navigation links to 'listen' to this filter and pass between dashboards"}

#} end optional overrides

#########################################################
# Derive navigation links using liquid
#{

  dimension: navigation_links {
    type: string
    hidden: no
    label: "Dashboard Links"
    description: "Add to Single Value Visualization and the defined HTML styling will be shown. Returns a URL link for each dashboard."
    sql:  '' ;;
    html:
      <!-- generate non_click_style and click_style liquid variables used below -->
      @{link_style_dashboard_navigation}
      <div style="{{ div_style }}">
      <span style = "{{ span_style }}">

      <!-- initialize variables used in following steps-->
      @{link_build_variable_defaults}

      <!-- capture the full url of the dashboard including filters -->
      {% assign link = link_generator._link %}
      {% assign counter = 1 %}
      {% assign use_qualified_filter_names = false %}

      <!-- generate source_to_destination_filters_mapping liquid variable passed into next step -->
      @{link_map_filters_from_navigation_dash_bindings}
      <!-- generate dashboard_url liquid variable dashboard_url used in href below-->
      {% assign use_url_variable = true %}
      @{link_build_dashboard_url}

      {% assign focus_page = parameter_navigation_focus_page._parameter_value | times: 1 %}

      {% if parameter_navigation_focus_page._in_query and counter == focus_page %}
      <span style="{{ click_style }}">{{ dash_label }}</span>
      {% elsif _explore._dashboard_url == target_dashboard %}
      <span style="{{ click_style }}">{{ dash_label }}</span>
      {% else %}
      <a style="{{ non_click_style }}" href="{{ dashboard_url }}">{{ dash_label }}</a>
      {% endif %}
      <!-- increment counter by 1 -->
      {% assign counter = counter | plus: 1 %}
      <!-- end for loop which began in link_map_filters_from_navigation_dash_bindings -->
      {% endfor %}
      </span>
      </div>

      ;;
  }

  measure: link_generator {
    hidden: yes
    type: number
    sql: 1 ;;
    drill_fields: [link_generator]
  }

#} derive navigation_links


}