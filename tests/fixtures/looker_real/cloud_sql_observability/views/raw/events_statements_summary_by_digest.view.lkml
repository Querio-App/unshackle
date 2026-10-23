view: events_statements_summary_by_digest {
  sql_table_name:
    {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
      @{POSTGRES_EXTENSION_SCHEMA}.pg_stat_statements
    {% else %}
      performance_schema.events_statements_summary_by_digest
    {% endif %} ;;

  dimension: pk {
    primary_key: yes
    hidden: yes
    type: string
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        CONCAT(${TABLE}.userid, '::', ${TABLE}.dbid, '::', ${TABLE}.queryid, '::', COALESCE(CAST(${TABLE}.toplevel AS VARCHAR), 't'))
      {% else %}
        CONCAT(COALESCE(${TABLE}.SCHEMA_NAME, 'none'), '::', ${TABLE}.DIGEST)
      {% endif %} ;;
  }

  dimension: database_name {
    type: string
    label: "Database / Schema"
    description: "Database or schema where the query was executed (e.g. app_db, analytics)"
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        COALESCE((SELECT datname FROM pg_database WHERE oid = ${TABLE}.dbid), 'postgres')
      {% else %}
        COALESCE(${TABLE}.SCHEMA_NAME, 'none')
      {% endif %} ;;
  }

  dimension: digest {
    type: string
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        CAST(${TABLE}.queryid AS VARCHAR)
      {% else %}
        ${TABLE}.DIGEST
      {% endif %} ;;
  }

  dimension: digest_text {
    type: string
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.query
      {% else %}
        ${TABLE}.DIGEST_TEXT
      {% endif %} ;;
  }

  dimension: count_star {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.calls
      {% else %}
        ${TABLE}.COUNT_STAR
      {% endif %} ;;
  }

  dimension: sum_timer_wait {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.total_exec_time * 1000000000
      {% else %}
        ${TABLE}.SUM_TIMER_WAIT
      {% endif %} ;;
  }

  dimension: avg_timer_wait {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.mean_exec_time * 1000000000
      {% else %}
        ${TABLE}.AVG_TIMER_WAIT
      {% endif %} ;;
  }

  dimension: max_timer_wait {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.max_exec_time * 1000000000
      {% else %}
        ${TABLE}.MAX_TIMER_WAIT
      {% endif %} ;;
  }

  dimension: sum_lock_time {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        0
      {% else %}
        ${TABLE}.SUM_LOCK_TIME
      {% endif %} ;;
  }

  dimension: sum_rows_sent {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.rows
      {% else %}
        ${TABLE}.SUM_ROWS_SENT
      {% endif %} ;;
  }

  dimension: sum_rows_examined {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        ${TABLE}.rows
      {% else %}
        ${TABLE}.SUM_ROWS_EXAMINED
      {% endif %} ;;
  }

  dimension: sum_no_index_used {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        0
      {% else %}
        ${TABLE}.SUM_NO_INDEX_USED
      {% endif %} ;;
  }

  dimension: sum_no_good_index_used {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        0
      {% else %}
        ${TABLE}.SUM_NO_GOOD_INDEX_USED
      {% endif %} ;;
  }

  dimension: sum_created_tmp_disk_tables {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        COALESCE(${TABLE}.temp_blks_written, 0)
      {% else %}
        ${TABLE}.SUM_CREATED_TMP_DISK_TABLES
      {% endif %} ;;
  }

  dimension: sum_created_tmp_tables {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        0
      {% else %}
        ${TABLE}.SUM_CREATED_TMP_TABLES
      {% endif %} ;;
  }

  dimension: sum_sort_rows {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        0
      {% else %}
        ${TABLE}.SUM_SORT_ROWS
      {% endif %} ;;
  }

  dimension: sum_sort_scan {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        0
      {% else %}
        ${TABLE}.SUM_SORT_SCAN
      {% endif %} ;;
  }
}
