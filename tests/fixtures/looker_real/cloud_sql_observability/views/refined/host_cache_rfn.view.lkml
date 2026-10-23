include: "/views/raw/host_cache.view.lkml"

view: +host_cache {

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
  
  measure: total_instances {
    type: count_distinct
    sql: ${host} ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Connected Hosts Distribution (Bar)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.host,{{ _view._name }}.total_instances
        &sorts={{ _view._name }}.total_instances+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Fleet Inventory (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.ip,{{ _view._name }}.host,{{ _view._name }}.total_instances
        &sorts={{ _view._name }}.total_instances+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_total_instances_current {
    view_label: "_PoP"
    type: count_distinct
    sql: ${host} ;;
    filters: [pop_period_group: "Selected Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Connected Hosts Distribution (Bar)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.host,{{ _view._name }}.pop_total_instances_current
        &sorts={{ _view._name }}.pop_total_instances_current+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Fleet Inventory (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.ip,{{ _view._name }}.host,{{ _view._name }}.pop_total_instances_current
        &sorts={{ _view._name }}.pop_total_instances_current+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: pop_total_instances_previous {
    view_label: "_PoP"
    type: count_distinct
    sql: ${host} ;;
    filters: [pop_period_group: "Previous Period"]
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Fleet Inventory (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.host,{{ _view._name }}.pop_total_instances_previous&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }

  measure: pop_total_instances_change {
    view_label: "_PoP"
    type: number
    sql: 1.0 * (${pop_total_instances_current} - ${pop_total_instances_previous}) / NULLIF(${pop_total_instances_previous}, 0) ;;
    value_format_name: percent_1
    drill_fields: []
    link: {
      label: "Fleet Inventory (Grid Table)"
      url: "@{VIZ_GRID_TABLE}{{ link }}&fields={{ _view._name }}.host,{{ _view._name }}.pop_total_instances_change&limit=50&vis_config={{ vis_config | encode_uri }}&toggle=dat,pik,vis"
    }
  }
}
