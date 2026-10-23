view: statement_history_snapshot {
  derived_table: {
    sql:
      SELECT
        {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
          date_trunc('hour', CURRENT_TIMESTAMP) AS snapshot_time,
          'PostgreSQL' AS engine_type,
          CONCAT(s.userid, '::', s.dbid, '::', s.queryid) AS pk,
          CAST(s.queryid AS VARCHAR) AS digest,
          s.query AS query_text,
          s.calls AS cumulative_executions,
          s.total_exec_time * 1000000000 AS cumulative_timer_wait_ps,
          s.mean_exec_time * 1000000000 AS cumulative_avg_timer_wait_ps,
          s.rows AS cumulative_rows_sent
        FROM @{POSTGRES_EXTENSION_SCHEMA}.pg_stat_statements s
        WHERE s.queryid IS NOT NULL
        {% else %}
          DATE_FORMAT(CURRENT_TIMESTAMP, '%Y-%m-%d %H:00:00') AS snapshot_time,
          'MySQL' AS engine_type,
          CONCAT(COALESCE(s.SCHEMA_NAME, 'none'), '::', s.DIGEST) AS pk,
          s.DIGEST AS digest,
          s.DIGEST_TEXT AS query_text,
          s.COUNT_STAR AS cumulative_executions,
          s.SUM_TIMER_WAIT AS cumulative_timer_wait_ps,
          s.AVG_TIMER_WAIT AS cumulative_avg_timer_wait_ps,
          s.SUM_ROWS_SENT AS cumulative_rows_sent
        FROM performance_schema.events_statements_summary_by_digest s
        WHERE s.DIGEST IS NOT NULL
        {% endif %} ;;
  }

  dimension: pk {
    primary_key: yes
    hidden: yes
    type: string
    sql: ${TABLE}.pk ;;
  }

  dimension_group: snapshot {
    type: time
    timeframes: [raw, time, hour, date, day_of_week, week, month]
    sql: ${TABLE}.snapshot_time ;;
  }

  dimension: engine_type {
    type: string
    sql: ${TABLE}.engine_type ;;
  }

  dimension: digest {
    type: string
    sql: ${TABLE}.digest ;;
  }

  dimension: query_text {
    type: string
    sql: ${TABLE}.query_text ;;
  }

  dimension: cumulative_executions {
    type: number
    sql: ${TABLE}.cumulative_executions ;;
  }

  dimension: cumulative_timer_wait_ps {
    type: number
    sql: ${TABLE}.cumulative_timer_wait_ps ;;
  }

  dimension: cumulative_avg_timer_wait_ps {
    type: number
    sql: ${TABLE}.cumulative_avg_timer_wait_ps ;;
  }

  dimension: is_within_retention_window {
    type: yesno
    description: "Enforces 30-day retention policy on historical snapshots."
    sql:
      ${snapshot_date} >=
        {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
          CURRENT_DATE - INTERVAL '30 days'
        {% else %}
          DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY)
        {% endif %} ;;
  }

  # --- Measures with Visual Drilling Library ---

  measure: count {
    type: count
    drill_fields: [digest, snapshot_time, engine_type]
  }

  measure: total_queries_tracked {
    type: count_distinct
    description: "Total unique statements monitored across snapshots."
    sql: ${pk} ;;
    drill_fields: []
    link: {
      label: "Queries by Statement Text (Horizontal Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_executions+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Statement Inventory Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.engine_type,{{ _view._name }}.digest,{{ _view._name }}.query_text,{{ _view._name }}.total_executions,{{ _view._name }}.average_execution_time
        &sorts={{ _view._name }}.total_executions+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: total_executions {
    type: sum
    description: "Aggregated execution count across recorded snapshots."
    sql: ${cumulative_executions} ;;
    value_format_name: decimal_0
    drill_fields: []
    link: {
      label: "Execution Volume Over Time (Line Chart)"
      url: "
        @{VIZ_LINE_CHART}
        {{ link }}&fields={{ _view._name }}.snapshot_hour,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.snapshot_hour+asc
        &limit=100
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Top Executed Statements (Column Chart)"
      url: "
        @{VIZ_COLUMN_CHART}
        {{ link }}&fields={{ _view._name }}.digest,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.total_executions+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Execution Audit Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.snapshot_time,{{ _view._name }}.engine_type,{{ _view._name }}.query_text,{{ _view._name }}.total_executions,{{ _view._name }}.average_execution_time
        &sorts={{ _view._name }}.snapshot_time+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }

  measure: average_execution_time {
    type: average
    description: "Average latency across statement snapshots in milliseconds."
    sql: (${cumulative_avg_timer_wait_ps} / 1000000000.0) ;;
    value_format_name: decimal_2
    drill_fields: []
    link: {
      label: "Latency Distribution by Statement (Horizontal Bar Chart)"
      url: "
        @{VIZ_BAR_CHART}
        {{ link }}&fields={{ _view._name }}.query_text,{{ _view._name }}.average_execution_time
        &sorts={{ _view._name }}.average_execution_time+desc
        &limit=20
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
    link: {
      label: "Forensic Latency Detail (Grid Table)"
      url: "
        @{VIZ_GRID_TABLE}
        {{ link }}&fields={{ _view._name }}.engine_type,{{ _view._name }}.digest,{{ _view._name }}.query_text,{{ _view._name }}.average_execution_time,{{ _view._name }}.total_executions
        &sorts={{ _view._name }}.average_execution_time+desc
        &limit=50
        &vis_config={{ vis_config | encode_uri }}
        &toggle=dat,pik,vis"
    }
  }
}
