include: "/views/raw/threads.view.lkml"

view: +threads {

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
  
  measure: active_threads_count {
    type: count
    description: "Total number of threads."
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Threads by Database User & Type (Stacked Column)"
      url: "
        @{VIZ_STACKED_COLUMN}
        {{ link }}&fields={{ _view._name }}.processlist_user,{{ _view._name }}.type,{{ _view._name }}.active_threads_count
        &f[{{ _view._name }}.processlist_user]=-EMPTY,-NULL
        &sorts={{ _view._name }}.active_threads_count+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Thread Distribution by State (Donut Chart)"
      url: "
        @{VIZ_DONUT_CHART}
        {{ link }}&fields={{ _view._name }}.processlist_state,{{ _view._name }}.active_threads_count
        &sorts={{ _view._name }}.active_threads_count+desc
        &limit=10
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Live Thread Processlist (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.thread_id,{{ _view._name }}.processlist_user,{{ _view._name }}.processlist_host,{{ _view._name }}.type,{{ _view._name }}.processlist_state
        &sorts={{ _view._name }}.thread_id+asc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_active_threads_current {
    view_label: "_PoP"
    type: count
    filters: [pop_period_group: "Selected Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Threads by Database User & Type (Stacked Column)"
      url: "
        @{VIZ_STACKED_COLUMN}
        {{ link }}&fields={{ _view._name }}.processlist_user,{{ _view._name }}.type,{{ _view._name }}.pop_active_threads_current
        &f[{{ _view._name }}.processlist_user]=-EMPTY,-NULL
        &sorts={{ _view._name }}.pop_active_threads_current+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Thread Distribution by State (Donut Chart)"
      url: "
        @{VIZ_DONUT_CHART}
        {{ link }}&fields={{ _view._name }}.processlist_state,{{ _view._name }}.pop_active_threads_current
        &sorts={{ _view._name }}.pop_active_threads_current+desc
        &limit=10
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Live Thread Processlist (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.thread_id,{{ _view._name }}.processlist_user,{{ _view._name }}.processlist_host,{{ _view._name }}.type,{{ _view._name }}.processlist_state
        &sorts={{ _view._name }}.thread_id+asc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_active_threads_previous {
    view_label: "_PoP"
    type: count
    filters: [pop_period_group: "Previous Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Live Thread Processlist (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.thread_id,{{ _view._name }}.processlist_user,{{ _view._name }}.processlist_host,{{ _view._name }}.type,{{ _view._name }}.processlist_state&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: pop_active_threads_change {
    view_label: "_PoP"
    type: number
    sql: 1.0 * (${pop_active_threads_current} - ${pop_active_threads_previous}) / NULLIF(${pop_active_threads_previous}, 0) ;;
    value_format_name: percent_1
    drill_fields: []
    link: {
      label: "Live Thread Processlist (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.thread_id,{{ _view._name }}.processlist_user,{{ _view._name }}.processlist_host,{{ _view._name }}.type,{{ _view._name }}.processlist_state&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: running_threads_count {
    type: count
    filters: [processlist_state: "-NULL"]
    description: "Number of threads actively executing a task."
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Running Threads by User (Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.processlist_user,{{ _view._name }}.running_threads_count
        &f[{{ _view._name }}.processlist_user]=-EMPTY,-NULL
        &sorts={{ _view._name }}.running_threads_count+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Active Worker Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.thread_id,{{ _view._name }}.processlist_user,{{ _view._name }}.processlist_host,{{ _view._name }}.processlist_state
        &sorts={{ _view._name }}.thread_id+asc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: background_threads_count {
    type: count
    filters: [type: "BACKGROUND"]
    description: "Number of background threads."
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Background Threads Breakdown (Donut Chart)"
      url: "
        @{VIZ_DONUT_CHART}
        {{ link }}&fields={{ _view._name }}.name,{{ _view._name }}.background_threads_count
        &sorts={{ _view._name }}.background_threads_count+desc
        &limit=10
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Background Engine Tasks (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.thread_id,{{ _view._name }}.name,{{ _view._name }}.type,{{ _view._name }}.processlist_state
        &sorts={{ _view._name }}.thread_id+asc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }
}
