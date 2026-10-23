- dashboard: merchant_deepdive
  title: Merchant Deep-Dive
  layout: newspaper
  preferred_viewer: dashboards-next
  elements:
  - name: Merchant Name
    title: Merchant Name
    model: retail_banking
    explore: card_transactions
    type: single_value
    fields: [card_transactions.merchant, card_transactions.category]
    sorts: [card_transactions.category desc]
    limit: 1
    column_limit: 50
    custom_color_enabled: true
    show_single_value_title: false
    show_comparison: true
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: false
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    x_axis_gridlines: false
    y_axis_gridlines: true
    show_view_names: false
    show_y_axis_labels: true
    show_y_axis_ticks: true
    y_axis_tick_density: default
    y_axis_tick_density_custom: 5
    show_x_axis_label: true
    show_x_axis_ticks: true
    y_axis_scale_mode: linear
    x_axis_reversed: false
    y_axis_reversed: false
    plot_size_by_field: false
    trellis: ''
    stacking: ''
    limit_displayed_rows: false
    legend_position: center
    point_style: none
    show_value_labels: false
    label_density: 25
    x_axis_scale: auto
    y_axis_combined: true
    ordering: none
    show_null_labels: false
    show_totals_labels: false
    show_silhouette: false
    totals_color: "#808080"
    defaults_version: 1
    series_types: {}
    listen:
      Merchant Name: card_transactions.merchant
    row: 3
    col: 0
    width: 5
    height: 4
  - name: Fraudulent vs Legitimate Transactions
    title: Fraudulent vs Legitimate Transactions
    model: retail_banking
    explore: card_transactions
    type: looker_line
    fields: [card_transactions.number_of_fraudulent_transactions, card_transactions.number_of_legitimate_transactions,
      card_transactions.transaction_date]
    filters: {}
    sorts: [card_transactions.transaction_date desc]
    limit: 500
    column_limit: 50
    x_axis_gridlines: false
    y_axis_gridlines: false
    show_view_names: false
    show_y_axis_labels: true
    show_y_axis_ticks: true
    y_axis_tick_density: default
    y_axis_tick_density_custom: 5
    show_x_axis_label: false
    show_x_axis_ticks: true
    y_axis_scale_mode: linear
    x_axis_reversed: false
    y_axis_reversed: false
    plot_size_by_field: false
    trellis: ''
    stacking: ''
    limit_displayed_rows: false
    legend_position: center
    point_style: none
    show_value_labels: false
    label_density: 25
    x_axis_scale: auto
    y_axis_combined: true
    show_null_points: true
    interpolation: monotone
    y_axes: [{label: '', orientation: left, series: [{axisId: card_transactions.number_of_legitimate_transactions,
            id: card_transactions.number_of_legitimate_transactions, name: Number
              of Legitimate Transactions}], showLabels: true, showValues: true, unpinAxis: false,
        tickDensity: default, tickDensityCustom: 5, type: linear}, {label: !!null '',
        orientation: right, series: [{axisId: card_transactions.number_of_fraudulent_transactions,
            id: card_transactions.number_of_fraudulent_transactions, name: Number
              of Fraudulent Transactions}], showLabels: true, showValues: true, unpinAxis: false,
        tickDensity: default, tickDensityCustom: 5, type: linear}]
    series_types: {}
    series_colors:
      card_transactions.number_of_legitimate_transactions: "#72D16D"
      card_transactions.number_of_fraudulent_transactions: "#B32F37"
    ordering: none
    show_null_labels: false
    show_totals_labels: false
    show_silhouette: false
    totals_color: "#808080"
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 18
    col: 0
    width: 24
    height: 6
  - title: Merchant Location
    name: Merchant Location
    model: retail_banking
    explore: card_transactions
    type: looker_map
    fields: [card_transactions.merchant, card_transactions.merchant_location]
    sorts: [card_transactions.merchant]
    limit: 1
    column_limit: 50
    map_plot_mode: points
    heatmap_gridlines: false
    heatmap_gridlines_empty: false
    heatmap_opacity: 0.5
    show_region_field: true
    draw_map_labels_above_data: true
    map_tile_provider: traffic_day
    map_position: fit_data
    map_scale_indicator: 'off'
    map_pannable: true
    map_zoomable: true
    map_marker_type: icon
    map_marker_icon_name: default
    map_marker_radius_mode: proportional_value
    map_marker_units: meters
    map_marker_proportional_scale_type: linear
    map_marker_color_mode: fixed
    show_view_names: false
    show_legend: true
    quantize_map_value_colors: false
    reverse_map_value_colors: false
    map_latitude: 38.99237128544011
    map_longitude: -104.04838085174562
    map_zoom: 8
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
    row: 7
    col: 0
    width: 5
    height: 4
  - title: Merchant Street View
    name: Merchant Street View
    model: retail_banking
    explore: card_transactions
    type: single_value
    fields: [card_transactions.merchant, card_transactions.merchant_address_street_view]
    sorts: [card_transactions.merchant]
    limit: 1
    column_limit: 50
    custom_color_enabled: true
    show_single_value_title: true
    show_comparison: false
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: true
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    map_plot_mode: points
    heatmap_gridlines: false
    heatmap_gridlines_empty: false
    heatmap_opacity: 0.5
    show_region_field: true
    draw_map_labels_above_data: true
    map_tile_provider: light
    map_position: custom
    map_latitude: 38.99237128544011
    map_longitude: -104.04838085174562
    map_zoom: 8
    map_scale_indicator: 'off'
    map_pannable: true
    map_zoomable: true
    map_marker_type: icon
    map_marker_icon_name: default
    map_marker_radius_mode: proportional_value
    map_marker_units: meters
    map_marker_proportional_scale_type: linear
    map_marker_color_mode: fixed
    show_view_names: false
    show_legend: true
    quantize_map_value_colors: false
    reverse_map_value_colors: false
    defaults_version: 1
    series_types: {}
    hidden_fields: [card_transactions.merchant]
    listen:
      Merchant Name: card_transactions.merchant
    row: 3
    col: 5
    width: 9
    height: 8
  - title: Transactions
    name: Transactions
    model: retail_banking
    explore: card_transactions
    type: single_value
    fields: [card_transactions.count]
    limit: 500
    column_limit: 50
    custom_color_enabled: true
    show_single_value_title: true
    show_comparison: false
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: true
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 7
    col: 19
    width: 5
    height: 4
  - title: Customers
    name: Customers
    model: retail_banking
    explore: card_transactions
    type: single_value
    fields: [client.number_of_clients]
    limit: 500
    column_limit: 50
    custom_color_enabled: true
    show_single_value_title: true
    show_comparison: false
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: true
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 7
    col: 14
    width: 5
    height: 4
  - title: Amount
    name: Amount
    model: retail_banking
    explore: card_transactions
    type: single_value
    fields: [card_transactions.total_amount]
    limit: 500
    column_limit: 50
    custom_color_enabled: true
    show_single_value_title: true
    show_comparison: false
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: true
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    show_view_names: false
    show_row_numbers: true
    truncate_column_names: false
    hide_totals: false
    hide_row_totals: false
    table_theme: editable
    limit_displayed_rows: false
    defaults_version: 1
    series_types: {}
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 3
    col: 14
    width: 5
    height: 4
  - title: Amount by Customer Age Tier
    name: Amount by Customer Age Tier
    model: retail_banking
    explore: card_transactions
    type: looker_column
    fields: [card_transactions.total_amount, client.age_tier]
    fill_fields: [client.age_tier]
    sorts: [client.age_tier]
    limit: 500
    column_limit: 50
    x_axis_gridlines: false
    y_axis_gridlines: false
    show_view_names: false
    show_y_axis_labels: true
    show_y_axis_ticks: true
    y_axis_tick_density: default
    y_axis_tick_density_custom: 5
    show_x_axis_label: false
    show_x_axis_ticks: true
    y_axis_scale_mode: linear
    x_axis_reversed: false
    y_axis_reversed: false
    plot_size_by_field: false
    trellis: ''
    stacking: ''
    limit_displayed_rows: false
    legend_position: center
    point_style: none
    show_value_labels: true
    label_density: 25
    x_axis_scale: auto
    y_axis_combined: true
    ordering: none
    show_null_labels: false
    show_totals_labels: false
    show_silhouette: false
    totals_color: "#808080"
    y_axes: [{label: '', orientation: left, series: [{axisId: card_transactions.total_amount,
            id: card_transactions.total_amount, name: Total Amount for All Transactions}],
        showLabels: false, showValues: false, unpinAxis: false, tickDensity: default,
        tickDensityCustom: 5, type: linear}]
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 11
    col: 0
    width: 7
    height: 7
  - title: Amount by Card Type
    name: Amount by Card Type
    model: retail_banking
    explore: card_transactions
    type: looker_pie
    fields: [card_transactions.total_amount, card.type]
    fill_fields: [card.type]
    sorts: [card_transactions.total_amount desc]
    limit: 500
    column_limit: 50
    value_labels: legend
    label_type: labPer
    inner_radius: 50
    x_axis_gridlines: false
    y_axis_gridlines: false
    show_view_names: false
    y_axes: [{label: '', orientation: left, series: [{axisId: card_transactions.total_amount,
            id: card_transactions.total_amount, name: Total Amount for All Transactions}],
        showLabels: false, showValues: true, unpinAxis: false, tickDensity: default,
        tickDensityCustom: 5, type: linear}]
    show_y_axis_labels: true
    show_y_axis_ticks: true
    y_axis_tick_density: default
    y_axis_tick_density_custom: 5
    show_x_axis_label: true
    show_x_axis_ticks: true
    y_axis_scale_mode: linear
    x_axis_reversed: false
    y_axis_reversed: false
    plot_size_by_field: false
    trellis: ''
    stacking: ''
    limit_displayed_rows: false
    legend_position: center
    series_types: {}
    point_style: none
    show_value_labels: false
    label_density: 25
    x_axis_scale: auto
    y_axis_combined: true
    ordering: none
    show_null_labels: false
    show_totals_labels: false
    show_silhouette: false
    totals_color: "#808080"
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 11
    col: 7
    width: 7
    height: 7
  - title: Number of Transactions by Time of Day
    name: Number of Transactions by Time of Day
    model: retail_banking
    explore: card_transactions
    type: looker_column
    fields: [card_transactions.transaction_day_of_week, card_transactions.transaction_hour_of_day,
      card_transactions.count]
    pivots: [card_transactions.transaction_hour_of_day]
    fill_fields: [card_transactions.transaction_day_of_week, card_transactions.transaction_hour_of_day]
    sorts: [card_transactions.transaction_hour_of_day 0, card_transactions.transaction_day_of_week]
    limit: 500
    column_limit: 50
    x_axis_gridlines: false
    y_axis_gridlines: false
    show_view_names: false
    show_y_axis_labels: true
    show_y_axis_ticks: true
    y_axis_tick_density: default
    y_axis_tick_density_custom: 5
    show_x_axis_label: false
    show_x_axis_ticks: true
    y_axis_scale_mode: linear
    x_axis_reversed: false
    y_axis_reversed: false
    plot_size_by_field: false
    trellis: ''
    stacking: ''
    limit_displayed_rows: false
    legend_position: center
    point_style: none
    show_value_labels: false
    label_density: 25
    x_axis_scale: auto
    y_axis_combined: true
    ordering: none
    show_null_labels: false
    show_totals_labels: false
    show_silhouette: false
    totals_color: "#808080"
    y_axes: [{label: '', orientation: left, series: [{axisId: card_transactions.total_amount,
            id: card_transactions.total_amount, name: Total Amount for All Transactions}],
        showLabels: false, showValues: true, unpinAxis: false, tickDensity: default,
        tickDensityCustom: 5, type: linear}]
    hide_legend: true
    series_types: {}
    series_colors:
      0 - card_transactions.count: "#34A853"
      1 - card_transactions.count: "#34A853"
      2 - card_transactions.count: "#34A853"
      4 - card_transactions.count: "#34A853"
      5 - card_transactions.count: "#34A853"
      6 - card_transactions.count: "#34A853"
      7 - card_transactions.count: "#34A853"
      8 - card_transactions.count: "#34A853"
      9 - card_transactions.count: "#34A853"
      10 - card_transactions.count: "#34A853"
      11 - card_transactions.count: "#34A853"
      12 - card_transactions.count: "#34A853"
      13 - card_transactions.count: "#34A853"
      14 - card_transactions.count: "#34A853"
      15 - card_transactions.count: "#34A853"
      16 - card_transactions.count: "#34A853"
      17 - card_transactions.count: "#34A853"
      18 - card_transactions.count: "#34A853"
      19 - card_transactions.count: "#34A853"
      20 - card_transactions.count: "#34A853"
      21 - card_transactions.count: "#34A853"
      22 - card_transactions.count: "#34A853"
      23 - card_transactions.count: "#34A853"
    value_labels: legend
    label_type: labPer
    inner_radius: 50
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 11
    col: 14
    width: 10
    height: 7
  - title: Percent Fraudulent
    name: Percent Fraudulent
    model: retail_banking
    explore: card_transactions
    type: single_value
    fields: [card_transactions.percent_fraudulent_transactions]
    limit: 500
    column_limit: 50
    custom_color_enabled: true
    show_single_value_title: true
    show_comparison: false
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: true
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    custom_color: "#EA4335"
    series_types: {}
    defaults_version: 1
    listen:
      Merchant Name: card_transactions.merchant
      Transaction Date: card_transactions.transaction_date
    row: 3
    col: 19
    width: 5
    height: 4
  - name: "<span class='fa fa-building'> Merchant Deep Dive</span>"
    type: text
    title_text: "<span class='fa fa-building'> Merchant Deep Dive</span>"
    subtitle_text: ''
    row: 0
    col: 0
    width: 24
    height: 3
  filters:
  - name: Merchant Name
    title: Merchant Name
    type: field_filter
    default_value: ''
    allow_multiple_values: true
    required: false
    ui_config:
      type: tag_list
      display: popover
    model: retail_banking
    explore: card_transactions
    listens_to_filters: []
    field: card_transactions.merchant
  - name: Transaction Date
    title: Transaction Date
    type: field_filter
    default_value: 6 months
    allow_multiple_values: true
    required: false
    model: retail_banking
    explore: card_transactions
    listens_to_filters: []
    field: card_transactions.transaction_date
