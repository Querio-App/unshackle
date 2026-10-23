project_name: "operational-intelligence-cloud-sql"

# --------------------------------------------------------------------------
# Database Connection Constants (Marketplace Export Parameterization)
# --------------------------------------------------------------------------

constant: MYSQL_CONNECTION_NAME {
  value: "mysql"
  export: override_optional
}

constant: POSTGRES_CONNECTION_NAME {
  value: "looker-block-test"
  export: override_optional
}

constant: POSTGRES_EXTENSION_SCHEMA {
  value: "public"
  export: override_optional
}

constant: SQL_ENGINE_TYPE {
  value: "mysql"
  export: override_optional
}

# --------------------------------------------------------------------------
# Advanced Visualization Configurations for Drills (Looker Architect Library)
# --------------------------------------------------------------------------

constant: VIZ_LINE_CHART {
  value: "{% assign vis_config = '{
    \"type\": \"looker_line\",
    \"x_axis_gridlines\": false,
    \"y_axis_gridlines\": true,
    \"show_view_names\": false,
    \"show_y_axis_labels\": true,
    \"show_y_axis_ticks\": true,
    \"show_x_axis_label\": true,
    \"show_x_axis_ticks\": true,
    \"legend_position\": \"center\",
    \"point_style\": \"circle\",
    \"interpolation\": \"monotone\",
    \"series_colors\": { \"active_connections\": \"#1A73E8\" }
  }' %}"
}

constant: VIZ_BAR_CHART {
  value: "{% assign vis_config = '{
    \"type\": \"looker_bar\",
    \"x_axis_gridlines\": false,
    \"y_axis_gridlines\": true,
    \"show_view_names\": false,
    \"show_y_axis_labels\": true,
    \"show_y_axis_ticks\": true,
    \"show_x_axis_label\": true,
    \"show_x_axis_ticks\": true,
    \"show_value_labels\": true,
    \"legend_position\": \"center\",
    \"series_colors\": { \"total_executions\": \"#1A73E8\", \"total_queries_no_index\": \"#EA4335\" }
  }' %}"
}

constant: VIZ_COLUMN_CHART {
  value: "{% assign vis_config = '{
    \"type\": \"looker_column\",
    \"x_axis_gridlines\": false,
    \"y_axis_gridlines\": true,
    \"show_view_names\": false,
    \"show_y_axis_labels\": true,
    \"show_y_axis_ticks\": true,
    \"show_x_axis_label\": true,
    \"show_x_axis_ticks\": true,
    \"show_value_labels\": true,
    \"legend_position\": \"center\",
    \"series_colors\": { \"total_instances\": \"#1A73E8\" }
  }' %}"
}

constant: VIZ_STACKED_COLUMN {
  value: "{% assign vis_config = '{
    \"type\": \"looker_column\",
    \"stacking\": \"normal\",
    \"x_axis_gridlines\": false,
    \"y_axis_gridlines\": true,
    \"show_view_names\": false,
    \"show_y_axis_labels\": true,
    \"show_y_axis_ticks\": true,
    \"show_x_axis_label\": true,
    \"show_x_axis_ticks\": true,
    \"show_value_labels\": true,
    \"legend_position\": \"center\"
  }' %}"
}

constant: VIZ_DONUT_CHART {
  value: "{% assign vis_config = '{
    \"type\": \"looker_pie\",
    \"value_labels\": \"legend\",
    \"label_type\": \"labPer\",
    \"inner_radius\": 50,
    \"show_view_names\": false,
    \"legend_position\": \"right\"
  }' %}"
}

constant: VIZ_GRID_TABLE {
  value: "{% assign vis_config = '{
    \"type\": \"looker_grid\",
    \"show_view_names\": false,
    \"show_row_numbers\": true,
    \"truncate_text\": false,
    \"table_theme\": \"white\",
    \"limit_displayed_rows\": false,
    \"enable_conditional_formatting\": true
  }' %}"
}
