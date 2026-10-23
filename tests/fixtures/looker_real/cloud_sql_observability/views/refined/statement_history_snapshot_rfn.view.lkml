include: "/views/raw/statement_history_snapshot.view.lkml"

view: +statement_history_snapshot {

  # =========================================================================
  # 1. PERIOD OVER PERIOD LOGIC & FILTERS
  # =========================================================================

  filter: pop_date_filter {
    view_label: "_PoP"
    label: "Comparison Date Filter"
    description: "Select the date range to compare against the directly preceding period of the same length (PoP Method 7)."
    type: date
    default_value: "7 days"
  }

  dimension_group: pop_filter_start {
    hidden: yes
    type: time
    timeframes: [raw, date]
    sql: CASE WHEN {% date_start pop_date_filter %} IS NULL THEN '1970-01-01' ELSE CAST({% date_start pop_date_filter %} AS DATE) END ;;
  }

  dimension_group: pop_filter_end {
    hidden: yes
    type: time
    timeframes: [raw, date]
    sql: CASE WHEN {% date_end pop_date_filter %} IS NULL THEN CURRENT_DATE ELSE CAST({% date_end pop_date_filter %} AS DATE) END ;;
  }

  dimension: pop_interval_days {
    hidden: yes
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${pop_filter_end_date} - ${pop_filter_start_date}
      {% else %}
        DATEDIFF(${pop_filter_end_date}, ${pop_filter_start_date})
      {% endif %} ;;
  }

  dimension: pop_previous_start_date {
    hidden: yes
    type: date
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${pop_filter_start_date} - (${pop_interval_days} || ' day')::INTERVAL
      {% else %}
        DATE_SUB(${pop_filter_start_date}, INTERVAL ${pop_interval_days} DAY)
      {% endif %} ;;
  }

  dimension: is_current_period {
    hidden: yes
    type: yesno
    sql: ${snapshot_date} >= ${pop_filter_start_date} AND ${snapshot_date} < ${pop_filter_end_date} ;;
  }

  dimension: is_previous_period {
    hidden: yes
    type: yesno
    sql: ${snapshot_date} >= ${pop_previous_start_date} AND ${snapshot_date} < ${pop_filter_start_date} ;;
  }

  dimension: pop_period_group {
    view_label: "_PoP"
    label: "Comparison Period"
    description: "Categorizes records into Selected Period, Previous Period, or Not in time period based on snapshot_date."
    type: string
    case: {
      when: {
        sql: ${is_current_period} = true ;;
        label: "Selected Period"
      }
      when: {
        sql: ${is_previous_period} = true ;;
        label: "Previous Period"
      }
      else: "Not in time period"
    }
  }

  measure: pop_unique_queries_current {
    view_label: "_PoP"
    label: "Unique Statements Tracked (Selected Period)"
    description: "Total unique statements active during the selected comparison period."
    type: count_distinct
    sql: ${digest} ;;
    filters: [is_current_period: "yes"]
    value_format_name: decimal_0
  }

  measure: pop_executions_current {
    view_label: "_PoP"
    label: "Total Executions (Selected Period)"
    description: "Total statement executions captured during the selected period."
    type: sum
    sql: ${total_executions_raw} ;;
    filters: [is_current_period: "yes"]
    value_format_name: decimal_0
  }

  measure: pop_executions_previous {
    view_label: "_PoP"
    label: "Total Executions (Previous Period)"
    description: "Total statement executions captured during the previous period of identical length."
    type: sum
    sql: ${total_executions_raw} ;;
    filters: [is_previous_period: "yes"]
    value_format_name: decimal_0
  }

  measure: pop_executions_change {
    view_label: "_PoP"
    label: "Total Executions PoP % Change"
    description: "Percentage change in statement executions between selected and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${pop_executions_current} - ${pop_executions_previous}) / NULLIF(${pop_executions_previous}, 0) ;;
    html:
      {% if value >= 0 %}
        <span style="color: #137333; font-weight: 600;">+{{ rendered_value }}</span>
      {% else %}
        <span style="color: #EA4335; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  measure: pop_avg_latency_current {
    view_label: "_PoP"
    label: "Average Latency (Selected Period)"
    description: "Average statement execution latency during the selected period in seconds."
    type: average
    sql: ${avg_latency_seconds_raw} ;;
    filters: [is_current_period: "yes"]
    value_format: "#,##0.000 \"s\""
  }

  measure: pop_avg_latency_previous {
    view_label: "_PoP"
    label: "Average Latency (Previous Period)"
    description: "Average statement execution latency during the previous period in seconds."
    type: average
    sql: ${avg_latency_seconds_raw} ;;
    filters: [is_previous_period: "yes"]
    value_format: "#,##0.000 \"s\""
  }

  measure: pop_avg_latency_change {
    view_label: "_PoP"
    label: "Average Latency PoP % Change"
    description: "Percentage change in average statement latency between selected and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${pop_avg_latency_current} - ${pop_avg_latency_previous}) / NULLIF(${pop_avg_latency_previous}, 0) ;;
    html:
      {% if value > 0 %}
        <span style="color: #EA4335; font-weight: 600;">+{{ rendered_value }}</span>
      {% else %}
        <span style="color: #137333; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  measure: pop_lock_time_current {
    view_label: "_PoP"
    label: "Lock Wait Time (Selected Period)"
    description: "Cumulative statement lock contention during the selected period in seconds."
    type: sum
    sql: ${total_lock_time_seconds_raw} ;;
    filters: [is_current_period: "yes"]
    value_format: "#,##0.00 \"s\""
  }

  # =========================================================================
  # 2. PERIOD-BOUNDED DELTA MEASURES
  # =========================================================================

  measure: max_cumulative_executions {
    type: max
    sql: ${cumulative_executions} ;;
    hidden: yes
  }

  measure: min_cumulative_executions {
    type: min
    sql: ${cumulative_executions} ;;
    hidden: yes
  }

  measure: delta_executions {
    type: number
    label: "Delta Executions"
    description: "Total statement executions within the filtered timeframe, calculated as max(cumulative_executions) - min(cumulative_executions)."
    sql: COALESCE(${max_cumulative_executions} - ${min_cumulative_executions}, 0) ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Execution Throughput Trend (Line Chart)"
      url: "
        @{VIZ_LINE_CHART}
        {{ link }}&fields={{ _view._name }}.snapshot_hour,{{ _view._name }}.delta_executions
        &sorts={{ _view._name }}.snapshot_hour+asc
        &limit=500
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: max_cumulative_rows_examined {
    type: max
    sql: ${cumulative_rows_examined} ;;
    hidden: yes
  }

  measure: min_cumulative_rows_examined {
    type: min
    sql: ${cumulative_rows_examined} ;;
    hidden: yes
  }

  measure: delta_rows_examined {
    type: number
    label: "Delta Rows Examined"
    description: "Total storage engine rows examined within the filtered timeframe, calculated as max(cumulative_rows_examined) - min(cumulative_rows_examined)."
    sql: COALESCE(${max_cumulative_rows_examined} - ${min_cumulative_rows_examined}, 0) ;;
    value_format_name: decimal_0
  }

  measure: max_cumulative_latency {
    type: max
    sql: ${cumulative_latency} ;;
    hidden: yes
  }

  measure: min_cumulative_latency {
    type: min
    sql: ${cumulative_latency} ;;
    hidden: yes
  }

  measure: delta_latency {
    type: number
    label: "Delta Latency (Seconds)"
    description: "Total execution latency spent within the filtered timeframe in seconds."
    sql: COALESCE(${max_cumulative_latency} - ${min_cumulative_latency}, 0) ;;
    value_format: "#,##0.00 \"s\""
  }

  measure: max_cumulative_rows_sent {
    type: max
    sql: ${cumulative_rows_sent} ;;
    hidden: yes
  }

  measure: min_cumulative_rows_sent {
    type: min
    sql: ${cumulative_rows_sent} ;;
    hidden: yes
  }

  measure: delta_rows_sent {
    type: number
    label: "Delta Rows Sent"
    description: "Total result rows transmitted to client within the filtered timeframe."
    sql: COALESCE(${max_cumulative_rows_sent} - ${min_cumulative_rows_sent}, 0) ;;
    value_format_name: decimal_0
  }

  measure: max_cumulative_no_index_used {
    type: max
    sql: ${cumulative_no_index_used} ;;
    hidden: yes
  }

  measure: min_cumulative_no_index_used {
    type: min
    sql: ${cumulative_no_index_used} ;;
    hidden: yes
  }

  measure: delta_no_index_used {
    type: number
    label: "Delta Unindexed Scans"
    description: "Total unindexed full table scans executed within the filtered timeframe."
    sql: COALESCE(${max_cumulative_no_index_used} - ${min_cumulative_no_index_used}, 0) ;;
    value_format_name: decimal_0
  }

  measure: max_cumulative_tmp_disk_tables {
    type: max
    sql: ${cumulative_tmp_disk_tables} ;;
    hidden: yes
  }

  measure: min_cumulative_tmp_disk_tables {
    type: min
    sql: ${cumulative_tmp_disk_tables} ;;
    hidden: yes
  }

  measure: delta_tmp_disk_tables {
    type: number
    label: "Delta Temp Tables on Disk"
    description: "Total temporary tables forced to disk storage within the filtered timeframe."
    sql: COALESCE(${max_cumulative_tmp_disk_tables} - ${min_cumulative_tmp_disk_tables}, 0) ;;
    value_format_name: decimal_0
  }

  measure: delta_average_latency {
    type: number
    label: "Delta Average Latency"
    description: "Average latency per execution in seconds (delta latency / delta executions)."
    sql: 1.0 * ${delta_latency} / NULLIF(${delta_executions}, 0) ;;
    value_format: "#,##0.000 \"s\""
  }

  measure: delta_rows_examined_per_row_sent {
    type: number
    label: "Delta Scan-to-Sent Ratio"
    description: "Storage rows examined per row sent to client within the filtered timeframe."
    sql: 1.0 * ${delta_rows_examined} / NULLIF(${delta_rows_sent}, 0) ;;
    value_format_name: decimal_1
  }

  # =========================================================================
  # 3. PERIOD-OVER-PERIOD (PoP) COMPARISON MEASURES
  # =========================================================================

  measure: max_cumulative_executions_selected {
    type: max
    sql: ${cumulative_executions} ;;
    filters: [pop_period_group: "Selected Period"]
    hidden: yes
  }

  measure: min_cumulative_executions_selected {
    type: min
    sql: ${cumulative_executions} ;;
    filters: [pop_period_group: "Selected Period"]
    hidden: yes
  }

  measure: pop_delta_executions_current {
    view_label: "_PoP"
    label: "Delta Executions (Selected Period)"
    description: "Total executions within the selected period."
    type: number
    sql: COALESCE(${max_cumulative_executions_selected} - ${min_cumulative_executions_selected}, 0) ;;
    value_format_name: decimal_0
  }

  measure: max_cumulative_executions_previous {
    type: max
    sql: ${cumulative_executions} ;;
    filters: [pop_period_group: "Previous Period"]
    hidden: yes
  }

  measure: min_cumulative_executions_previous {
    type: min
    sql: ${cumulative_executions} ;;
    filters: [pop_period_group: "Previous Period"]
    hidden: yes
  }

  measure: pop_delta_executions_previous {
    view_label: "_PoP"
    label: "Delta Executions (Previous Period)"
    description: "Total executions within the previous period."
    type: number
    sql: COALESCE(${max_cumulative_executions_previous} - ${min_cumulative_executions_previous}, 0) ;;
    value_format_name: decimal_0
  }

  measure: pop_delta_executions_change {
    view_label: "_PoP"
    label: "Delta Executions PoP % Change"
    description: "Percentage change in executions between selected and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${pop_delta_executions_current} - ${pop_delta_executions_previous}) / NULLIF(${pop_delta_executions_previous}, 0) ;;
    html:
      {% if value >= 0 %}
        <span style="color: #137333; font-weight: 600;">+{{ rendered_value }}</span>
      {% else %}
        <span style="color: #EA4335; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  measure: pop_delta_executions_abs_change {
    view_label: "_PoP"
    label: "Delta Executions PoP Absolute Change"
    description: "Absolute difference in executions between selected and previous periods."
    type: number
    value_format_name: decimal_0
    sql: ${pop_delta_executions_current} - ${pop_delta_executions_previous} ;;
  }

  measure: max_cumulative_rows_examined_selected {
    type: max
    sql: ${cumulative_rows_examined} ;;
    filters: [pop_period_group: "Selected Period"]
    hidden: yes
  }

  measure: min_cumulative_rows_examined_selected {
    type: min
    sql: ${cumulative_rows_examined} ;;
    filters: [pop_period_group: "Selected Period"]
    hidden: yes
  }

  measure: pop_delta_rows_examined_current {
    view_label: "_PoP"
    label: "Delta Rows Examined (Selected Period)"
    description: "Rows examined within the selected period."
    type: number
    sql: COALESCE(${max_cumulative_rows_examined_selected} - ${min_cumulative_rows_examined_selected}, 0) ;;
    value_format_name: decimal_0
  }

  measure: max_cumulative_rows_examined_previous {
    type: max
    sql: ${cumulative_rows_examined} ;;
    filters: [pop_period_group: "Previous Period"]
    hidden: yes
  }

  measure: min_cumulative_rows_examined_previous {
    type: min
    sql: ${cumulative_rows_examined} ;;
    filters: [pop_period_group: "Previous Period"]
    hidden: yes
  }

  measure: pop_delta_rows_examined_previous {
    view_label: "_PoP"
    label: "Delta Rows Examined (Previous Period)"
    description: "Rows examined within the previous period."
    type: number
    sql: COALESCE(${max_cumulative_rows_examined_previous} - ${min_cumulative_rows_examined_previous}, 0) ;;
    value_format_name: decimal_0
  }

  measure: pop_delta_rows_examined_change {
    view_label: "_PoP"
    label: "Delta Rows Examined PoP % Change"
    description: "Percentage change in rows examined between selected and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${pop_delta_rows_examined_current} - ${pop_delta_rows_examined_previous}) / NULLIF(${pop_delta_rows_examined_previous}, 0) ;;
    html:
      {% if value > 0 %}
        <span style="color: #EA4335; font-weight: 600;">+{{ rendered_value }}</span>
      {% else %}
        <span style="color: #137333; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  measure: max_cumulative_latency_selected {
    type: max
    sql: ${cumulative_latency} ;;
    filters: [pop_period_group: "Selected Period"]
    hidden: yes
  }

  measure: min_cumulative_latency_selected {
    type: min
    sql: ${cumulative_latency} ;;
    filters: [pop_period_group: "Selected Period"]
    hidden: yes
  }

  measure: pop_delta_latency_current {
    view_label: "_PoP"
    label: "Delta Latency (Selected Period)"
    type: number
    sql: COALESCE(${max_cumulative_latency_selected} - ${min_cumulative_latency_selected}, 0) ;;
    value_format: "#,##0.00 \"s\""
  }

  measure: max_cumulative_latency_previous {
    type: max
    sql: ${cumulative_latency} ;;
    filters: [pop_period_group: "Previous Period"]
    hidden: yes
  }

  measure: min_cumulative_latency_previous {
    type: min
    sql: ${cumulative_latency} ;;
    filters: [pop_period_group: "Previous Period"]
    hidden: yes
  }

  measure: pop_delta_latency_previous {
    view_label: "_PoP"
    label: "Delta Latency (Previous Period)"
    type: number
    sql: COALESCE(${max_cumulative_latency_previous} - ${min_cumulative_latency_previous}, 0) ;;
    value_format: "#,##0.00 \"s\""
  }

  measure: pop_delta_latency_change {
    view_label: "_PoP"
    label: "Delta Latency PoP % Change"
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${pop_delta_latency_current} - ${pop_delta_latency_previous}) / NULLIF(${pop_delta_latency_previous}, 0) ;;
    html:
      {% if value > 0 %}
        <span style="color: #EA4335; font-weight: 600;">+{{ rendered_value }}</span>
      {% else %}
        <span style="color: #137333; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  measure: pop_delta_avg_latency_current {
    view_label: "_PoP"
    label: "Average Latency (Selected Period)"
    description: "Average execution latency per statement within the selected comparison period in seconds."
    type: number
    value_format: "#,##0.000 \"s\""
    sql: 1.0 * ${pop_delta_latency_current} / NULLIF(${pop_delta_executions_current}, 0) ;;
  }

  measure: pop_delta_avg_latency_previous {
    view_label: "_PoP"
    label: "Average Latency (Previous Period)"
    description: "Average execution latency per statement within the previous comparison period in seconds."
    type: number
    value_format: "#,##0.000 \"s\""
    sql: 1.0 * ${pop_delta_latency_previous} / NULLIF(${pop_delta_executions_previous}, 0) ;;
  }

  measure: pop_delta_avg_latency_change {
    view_label: "_PoP"
    label: "Average Latency PoP % Change"
    description: "Percentage change in average execution latency between selected and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${pop_delta_avg_latency_current} - ${pop_delta_avg_latency_previous}) / NULLIF(${pop_delta_avg_latency_previous}, 0) ;;
    html:
      {% if value > 0 %}
        <span style="color: #EA4335; font-weight: 600;">+{{ rendered_value }}</span>
      {% else %}
        <span style="color: #137333; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  # =========================================================================
  # 4. HOURLY & BASE MEASURES (FOR COMPATIBILITY AND TREND VISUALIZATION)
  # =========================================================================

  measure: count {
    type: count
    description: "Total count of snapshot telemetry rows."
  }

  measure: total_queries_tracked {
    type: count_distinct
    sql: ${digest} ;;
    description: "Total number of unique query statements tracked across instances."
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Query Digest Distribution (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.engine_type,{{ _view._name }}.total_queries_tracked
        &sorts={{ _view._name }}.total_queries_tracked+desc
        &limit=10
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Query Inventory Catalog (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.engine_type,{{ _view._name }}.digest,{{ _view._name }}.query_text,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_executions+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_executions {
    type: sum
    sql: ${total_executions_raw} ;;
    description: "Total statement executions captured across historical snapshots."
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Execution Throughput Trend (Line Chart)"
      url: "
        @{VIZ_LINE_CHART}
        {{ link }}&fields={{ _view._name }}.snapshot_hour,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.snapshot_hour+asc
        &limit=500
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Top Executed Statements (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_executions+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Statement Execution Catalog (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.snapshot_hour,{{ _view._name }}.query_text,{{ _view._name }}.total_executions,{{ _view._name }}.average_latency
        &sorts={{ _view._name }}.total_executions+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: average_latency {
    type: average
    sql: ${avg_latency_seconds_raw} ;;
    description: "Average query execution latency across statements in seconds."
    value_format: "#,##0.000 \"s\""
    drill_fields: []
    link: {
      label: "Latency Trend Over Time (Line Chart)"
      url: "
        @{VIZ_LINE_CHART}
        {{ link }}&fields={{ _view._name }}.snapshot_hour,{{ _view._name }}.average_latency
        &sorts={{ _view._name }}.snapshot_hour+asc
        &limit=500
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Slowest Statements Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.average_latency
        &sorts={{ _view._name }}.average_latency+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Latency & Throughput Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.average_latency,{{ _view._name }}.total_executions,{{ _view._name }}.total_latency
        &sorts={{ _view._name }}.average_latency+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_latency {
    type: sum
    sql: ${total_latency_seconds_raw} ;;
    description: "Total cumulative latency spent executing statements in seconds."
    value_format: "#,##0.00 \"s\""
    drill_fields: []
    link: {
      label: "Total Latency Impact by Statement (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_latency
        &sorts={{ _view._name }}.total_latency+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Detailed Latency Allocation (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_latency,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_latency+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_lock_time {
    type: sum
    sql: ${total_lock_time_seconds_raw} ;;
    description: "Total cumulative lock wait time in seconds."
    value_format: "#,##0.00 \"s\""
    drill_fields: []
    link: {
      label: "Lock Contention Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_lock_time
        &sorts={{ _view._name }}.total_lock_time+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_errors {
    type: sum
    sql: ${total_errors_raw} ;;
    description: "Total errors encountered during statement executions."
    value_format_name: decimal_0
    drill_fields: []
  }

  measure: total_rows_sent {
    type: sum
    sql: ${rows_sent_raw} ;;
    description: "Total result rows transmitted across snapshots."
    value_format_name: decimal_0
    drill_fields: []
  }
}
