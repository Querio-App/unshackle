include: "/views/raw/events_statements_summary_by_digest.view.lkml"

view: +events_statements_summary_by_digest {
  
  # --- Dimensions ---
  
  dimension: query_text {
    type: string
    description: "The normalized statement text"
    sql: ${digest_text} ;;
  }

  dimension: query_health_tier {
    view_label: "Health & Governance"
    label: "Query Health Tier"
    description: "Evaluates query performance risk: Critical (high latency or heavy disk spill), Warning (unindexed scans or high rows examined), Healthy."
    type: string
    case: {
      when: {
        sql: (${avg_timer_wait} / 1000000000000.0) > 5.0 OR ${sum_created_tmp_disk_tables} > 50 ;;
        label: "Critical"
      }
      when: {
        sql: ${sum_no_index_used} > 0 OR ${sum_no_good_index_used} > 0 OR (${sum_rows_examined} > 5000 AND ${sum_rows_sent} < 50) ;;
        label: "Warning"
      }
      else: "Healthy"
    }
    html:
      {% if value == 'Critical' %}
        <span style="color: #EA4335; font-weight: 700; background-color: #FCE8E6; border: 1px solid #F28B82; padding: 2px 8px; border-radius: 4px; display: inline-block;">CRITICAL</span>
      {% elsif value == 'Warning' %}
        <span style="color: #B06000; font-weight: 700; background-color: #FEF7E0; border: 1px solid #FBBC04; padding: 2px 8px; border-radius: 4px; display: inline-block;">WARNING</span>
      {% else %}
        <span style="color: #137333; font-weight: 700; background-color: #E6F4EA; border: 1px solid #81C995; padding: 2px 8px; border-radius: 4px; display: inline-block;">HEALTHY</span>
      {% endif %} ;;
  }

  dimension: is_missing_index_risk {
    view_label: "Health & Governance"
    label: "Missing Index Flag"
    description: "Indicates whether the statement triggered unindexed table scans."
    type: yesno
    sql: ${sum_no_index_used} > 0 OR ${sum_no_good_index_used} > 0 ;;
  }

  dimension: is_disk_spill_risk {
    view_label: "Health & Governance"
    label: "Disk Temp Table Flag"
    description: "Indicates whether internal temporary tables spilled to disk storage."
    type: yesno
    sql: ${sum_created_tmp_disk_tables} > 0 ;;
  }
  
  # --- PERIOD OVER PERIOD LOGIC (Method 7: Arbitrary Period & Directly Previous Period) ---
  filter: pop_date_filter {
    view_label: "_PoP"
    label: "Comparison Date Filter"
    description: "Select the current date range to compare against the previous period."
    type: date
    default_value: "7 days"
  }

  parameter: pop_compare_to {
    view_label: "_PoP"
    label: "Compare To"
    description: "Select the offset interval for the previous period comparison. Defaults to Period (Method 7: directly preceding period of identical length)."
    type: string
    allowed_value: { value: "Period" }
    allowed_value: { value: "Yesterday" }
    allowed_value: { value: "Week" }
    allowed_value: { value: "Month" }
    allowed_value: { value: "Year" }
    default_value: "Period"
  }

  dimension: pop_data_date {
    hidden: yes
    type: date
    sql: CURRENT_DATE ;;
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
        ${pop_filter_start_date} -
          {% if pop_compare_to._parameter_value == "'Yesterday'" %} INTERVAL '1 day'
          {% elsif pop_compare_to._parameter_value == "'Week'" %} INTERVAL '1 week'
          {% elsif pop_compare_to._parameter_value == "'Month'" %} INTERVAL '1 month'
          {% elsif pop_compare_to._parameter_value == "'Year'" %} INTERVAL '1 year'
          {% else %} (${pop_interval_days} || ' day')::INTERVAL
          {% endif %}
      {% else %}
        DATE_SUB(${pop_filter_start_date}, INTERVAL 
          {% if pop_compare_to._parameter_value == "'Yesterday'" %} 1 DAY
          {% elsif pop_compare_to._parameter_value == "'Week'" %} 7 DAY
          {% elsif pop_compare_to._parameter_value == "'Month'" %} 1 MONTH
          {% elsif pop_compare_to._parameter_value == "'Year'" %} 1 YEAR
          {% else %} ${pop_interval_days} DAY
          {% endif %}
        )
      {% endif %} ;;
  }

  dimension: pop_period_group {
    view_label: "_PoP"
    label: "Comparison Period"
    description: "Categorizes records into Selected Period, Previous Period, or Not in time period."
    type: string
    case: {
      when: {
        sql: ${pop_data_date} > ${pop_filter_start_date} AND ${pop_data_date} <= ${pop_filter_end_date} ;;
        label: "Selected Period"
      }
      when: {
        sql: ${pop_data_date} > ${pop_previous_start_date} AND ${pop_data_date} <= ${pop_filter_start_date} ;;
        label: "Previous Period"
      }
      else: "Not in time period"
    }
  }
  
  # --- Measures ---
  
  measure: count {
    type: count
    hidden: yes
  }

  measure: total_executions {
    type: sum
    description: "Total number of times the query was executed."
    sql: ${count_star} ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Top Executed Statements (Donut Chart)"
      url: "
        @{VIZ_DONUT_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_executions+desc
        &limit=10
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Latency vs Volume Profile (Column Chart)"
      url: "
        @{VIZ_COLUMN_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_executions+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Query Execution Catalog (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_executions,{{ _view._name }}.average_execution_time,{{ _view._name }}.total_lock_time
        &sorts={{ _view._name }}.total_executions+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_total_executions_current {
    view_label: "_PoP"
    type: sum
    description: "Total executions for the current period."
    sql: ${count_star} ;;
    filters: [pop_period_group: "Selected Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Top Executed Statements (Donut Chart)"
      url: "
        @{VIZ_DONUT_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.pop_total_executions_current
        &sorts={{ _view._name }}.pop_total_executions_current+desc
        &limit=10
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Latency vs Volume Profile (Column Chart)"
      url: "
        @{VIZ_COLUMN_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.pop_total_executions_current
        &sorts={{ _view._name }}.pop_total_executions_current+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Query Execution Catalog (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.pop_total_executions_current,{{ _view._name }}.average_execution_time,{{ _view._name }}.total_lock_time
        &sorts={{ _view._name }}.pop_total_executions_current+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_total_executions_previous {
    view_label: "_PoP"
    type: sum
    description: "Total executions for the previous period."
    sql: ${count_star} ;;
    filters: [pop_period_group: "Previous Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Query Execution Catalog (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.pop_total_executions_previous&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: pop_total_executions_change {
    view_label: "_PoP"
    type: number
    description: "Percentage change vs previous period."
    sql: 1.0 * (${pop_total_executions_current} - ${pop_total_executions_previous}) / NULLIF(${pop_total_executions_previous}, 0) ;;
    value_format_name: percent_1
    drill_fields: []
    link: {
      label: "Query Execution Catalog (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.pop_total_executions_change&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: average_execution_time {
    type: average
    description: "Average execution time in seconds."
    sql: ${avg_timer_wait} / 1000000000000.0 ;;
    value_format: "#,##0.00 \"s\""
    drill_fields: []
    link: {
      label: "Slowest Statements Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.average_execution_time
        &sorts={{ _view._name }}.average_execution_time+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Latency & Throughput Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.average_execution_time,{{ _view._name }}.total_executions,{{ _view._name }}.total_lock_time
        &sorts={{ _view._name }}.average_execution_time+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: max_execution_time {
    type: max
    label: "Peak Execution Time"
    description: "Maximum single execution latency in seconds."
    sql: ${max_timer_wait} / 1000000000000.0 ;;
    value_format: "#,##0.00 \"s\""
    drill_fields: []
    link: {
      label: "Peak Latency Outliers (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.max_execution_time
        &sorts={{ _view._name }}.max_execution_time+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_lock_time {
    type: sum
    description: "Total lock wait time in seconds."
    sql: ${sum_lock_time} / 1000000000000.0 ;;
    value_format: "#,##0.00 \"s\""
    drill_fields: []
    link: {
      label: "Lock Wait Time Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_lock_time
        &sorts={{ _view._name }}.total_lock_time+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Lock Time Impact Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_lock_time,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_lock_time+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_rows_sent {
    hidden: yes
    type: sum
    sql: ${sum_rows_sent} ;;
  }

  measure: total_rows_examined {
    type: sum
    label: "Total Rows Examined"
    description: "Total number of rows scanned in storage engine across all executions."
    sql: ${sum_rows_examined} ;;
    value_format_name: decimal_0
  }

  measure: rows_sent_per_execution {
    type: number
    description: "Average rows sent per query execution."
    sql: 1.0 * ${total_rows_sent} / NULLIF(${total_executions}, 0) ;;
    value_format_name: decimal_1
    drill_fields: []
    link: {
      label: "Rows Sent Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.rows_sent_per_execution
        &sorts={{ _view._name }}.rows_sent_per_execution+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Data Volume Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.rows_sent_per_execution,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.rows_sent_per_execution+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: rows_examined_per_row_sent {
    type: number
    label: "Rows Examined per Row Sent"
    description: "Ratio of storage rows scanned vs rows returned to client. Ratios > 100x indicate severe index starvation."
    sql: 1.0 * ${total_rows_examined} / NULLIF(${total_rows_sent}, 0) ;;
    value_format: "#,##0.0\"x\""
    html:
      {% if value > 500 %}
        <span style="color: #D93025; font-weight: bold;">{{ rendered_value }}</span>
      {% elsif value > 100 %}
        <span style="color: #F29900; font-weight: bold;">{{ rendered_value }}</span>
      {% else %}
        <span style="color: #1E8E3E;">{{ rendered_value }}</span>
      {% endif %} ;;
    drill_fields: []
    link: {
      label: "Index Starvation Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.rows_examined_per_row_sent
        &sorts={{ _view._name }}.rows_examined_per_row_sent+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Index Efficiency Diagnostics (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.rows_examined_per_row_sent,{{ _view._name }}.total_rows_examined,{{ _view._name }}.total_rows_sent,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.rows_examined_per_row_sent+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_queries_no_index {
    type: sum
    description: "Total queries executed without an index."
    sql: ${sum_no_index_used} ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Full Table Scan Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_queries_no_index
        &sorts={{ _view._name }}.total_queries_no_index+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Missing Index Detailed Diagnostics (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_queries_no_index,{{ _view._name }}.total_executions,{{ _view._name }}.average_execution_time
        &sorts={{ _view._name }}.total_queries_no_index+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_temp_disk_tables {
    type: sum
    label: "Temp Tables Created on Disk"
    description: "Total internal temporary tables that spilled to physical disk storage, causing I/O degradation."
    sql: ${sum_created_tmp_disk_tables} ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Disk Spill Ranking (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_temp_disk_tables
        &sorts={{ _view._name }}.total_temp_disk_tables+desc
        &limit=15
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Disk Spill Impact Details (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_temp_disk_tables,{{ _view._name }}.total_executions,{{ _view._name }}.average_execution_time
        &sorts={{ _view._name }}.total_temp_disk_tables+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_sort_scans {
    type: sum
    label: "Sort Operations via Scans"
    description: "Total sorts performed by full table scans rather than index lookups."
    sql: ${sum_sort_scan} ;;
    value_format_name: decimal_0
  }
}
