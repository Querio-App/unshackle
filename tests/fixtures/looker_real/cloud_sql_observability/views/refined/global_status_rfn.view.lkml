include: "/views/raw/global_status.view.lkml"

view: +global_status {
  
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
  
  measure: active_connections {
    type: sum
    description: "Current active threads connected."
    sql: CASE WHEN ${variable_name} = 'Threads_connected' THEN ${variable_value_num} ELSE 0 END ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Connection Breakdown by Variable (Column Chart)"
      url: "
        @{VIZ_COLUMN_CHART}
        {{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.active_connections
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Connection Metrics Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.variable_value_num
        &sorts={{ _view._name }}.variable_value_num+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_active_connections_current {
    view_label: "_PoP"
    type: sum
    sql: CASE WHEN ${variable_name} = 'Threads_connected' THEN ${variable_value_num} ELSE 0 END ;;
    filters: [pop_period_group: "Selected Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Connection Breakdown by Variable (Column Chart)"
      url: "
        @{VIZ_COLUMN_CHART}
        {{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.pop_active_connections_current
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Connection Metrics Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.variable_value_num
        &sorts={{ _view._name }}.variable_value_num+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_active_connections_previous {
    view_label: "_PoP"
    type: sum
    sql: CASE WHEN ${variable_name} = 'Threads_connected' THEN ${variable_value_num} ELSE 0 END ;;
    filters: [pop_period_group: "Previous Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Connection Metrics Detail (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.pop_active_connections_previous&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: pop_active_connections_change {
    view_label: "_PoP"
    type: number
    sql: 1.0 * (${pop_active_connections_current} - ${pop_active_connections_previous}) / NULLIF(${pop_active_connections_previous}, 0) ;;
    value_format_name: percent_1
    drill_fields: []
    link: {
      label: "Connection Metrics Detail (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.pop_active_connections_change&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: total_aborted_connects {
    type: sum
    sql: CASE WHEN ${variable_name} = 'Aborted_connects' THEN ${variable_value_num} ELSE 0 END ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Aborted Connects Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.variable_name,{{ _view._name }}.total_aborted_connects
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  # --- Cache & Buffer Pool Metrics (Cloud SQL Expert KPIs) ---

  measure: buffer_pool_reads {
    hidden: yes
    type: sum
    sql: CASE WHEN ${variable_name} = 'Innodb_buffer_pool_reads' THEN ${variable_value_num} ELSE 0 END ;;
  }

  measure: buffer_pool_read_requests {
    hidden: yes
    type: sum
    sql: CASE WHEN ${variable_name} = 'Innodb_buffer_pool_read_requests' THEN ${variable_value_num} ELSE 0 END ;;
  }

  measure: buffer_cache_hit_ratio {
    type: number
    label: "Buffer Cache Hit Ratio"
    description: "Percentage of read requests served directly from memory buffer pool without disk I/O (Healthy > 95%)."
    sql: 100.0 * (1.0 - (1.0 * ${buffer_pool_reads} / NULLIF(${buffer_pool_read_requests}, 0))) ;;
    value_format: "0.00\"%\""
    html:
      {% if value < 90.0 %}
        <span style="color: #EA4335; font-weight: 600;">{{ rendered_value }}</span>
      {% elsif value < 95.0 %}
        <span style="color: #B06000; font-weight: 600;">{{ rendered_value }}</span>
      {% else %}
        <span style="color: #137333; font-weight: 600;">{{ rendered_value }}</span>
      {% endif %} ;;
  }

  # --- Database Health & Alerting Engine ---

  measure: fleet_health_status {
    type: string
    description: "Consolidated operational health status of the database instance evaluated against real-time connection saturation and memory cache efficiency."
    sql:
      CASE
        WHEN MAX(CASE WHEN ${variable_name} = 'Threads_connected' THEN ${variable_value_num} ELSE 0 END) > 80
          OR (${buffer_cache_hit_ratio} < 90.0 AND ${buffer_cache_hit_ratio} > 0) THEN 'CRITICAL'
        WHEN MAX(CASE WHEN ${variable_name} = 'Threads_connected' THEN ${variable_value_num} ELSE 0 END) > 40
          OR (${buffer_cache_hit_ratio} < 95.0 AND ${buffer_cache_hit_ratio} > 0) THEN 'WARNING'
        ELSE 'HEALTHY'
      END ;;
    html:
      {% if value == 'CRITICAL' %}
        <div style="font-weight: 700; color: #EA4335; font-size: 26px; padding: 6px 12px; border: 2px solid #EA4335; border-radius: 8px; text-align: center; background-color: #FCE8E6;">CRITICAL</div>
      {% elsif value == 'WARNING' %}
        <div style="font-weight: 700; color: #B06000; font-size: 26px; padding: 6px 12px; border: 2px solid #FBBC04; border-radius: 8px; text-align: center; background-color: #FEF7E0;">WARNING</div>
      {% else %}
        <div style="font-weight: 700; color: #137333; font-size: 26px; padding: 6px 12px; border: 2px solid #34A853; border-radius: 8px; text-align: center; background-color: #E6F4EA;">HEALTHY</div>
      {% endif %} ;;
  }

  measure: aborted_connections_alert {
    type: sum
    description: "Cumulative count of failed handshakes, authentication drops, or unauthorized connection attempts."
    sql: CASE WHEN ${variable_name} = 'Aborted_connects' THEN ${variable_value_num} ELSE 0 END ;;
    value_format_name: decimal_0
    html:
      <span style="color: #5F6368; font-weight: 500;">{{ rendered_value }}</span> ;;
  }
}
