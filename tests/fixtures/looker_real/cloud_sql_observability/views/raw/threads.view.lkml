view: threads {
  sql_table_name:
    {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
      (
        SELECT
          pid AS THREAD_ID,
          pid AS PROCESSLIST_ID,
          application_name AS NAME,
          backend_type AS TYPE,
          usename AS PROCESSLIST_USER,
          client_addr AS PROCESSLIST_HOST,
          datname AS PROCESSLIST_DB,
          state AS PROCESSLIST_COMMAND,
          CAST(EXTRACT(epoch FROM (clock_timestamp() - query_start)) AS INTEGER) AS PROCESSLIST_TIME,
          state AS PROCESSLIST_STATE
        FROM pg_stat_activity
      )
    {% else %}
      performance_schema.threads
    {% endif %} ;;

  dimension: thread_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.THREAD_ID ;;
  }

  dimension: processlist_id {
    type: number
    sql: ${TABLE}.PROCESSLIST_ID ;;
  }

  dimension: name {
    type: string
    sql: ${TABLE}.NAME ;;
  }

  dimension: type {
    type: string
    sql: ${TABLE}.TYPE ;;
  }

  dimension: processlist_user {
    type: string
    sql: ${TABLE}.PROCESSLIST_USER ;;
  }

  dimension: processlist_host {
    type: string
    sql: ${TABLE}.PROCESSLIST_HOST ;;
  }

  dimension: processlist_db {
    type: string
    sql: ${TABLE}.PROCESSLIST_DB ;;
  }

  dimension: processlist_command {
    type: string
    sql: ${TABLE}.PROCESSLIST_COMMAND ;;
  }

  dimension: processlist_time {
    type: number
    sql: ${TABLE}.PROCESSLIST_TIME ;;
  }

  dimension: processlist_state {
    type: string
    sql: ${TABLE}.PROCESSLIST_STATE ;;
  }
}
