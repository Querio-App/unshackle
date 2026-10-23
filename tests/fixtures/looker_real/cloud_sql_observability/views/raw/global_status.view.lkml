view: global_status {
  sql_table_name:
    {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
      (
        SELECT 'Threads_connected' AS VARIABLE_NAME, CAST(count(*) AS VARCHAR) AS VARIABLE_VALUE FROM pg_stat_activity
        UNION ALL
        SELECT 'Aborted_connects' AS VARIABLE_NAME, '0' AS VARIABLE_VALUE
        UNION ALL
        SELECT 'Innodb_buffer_pool_reads' AS VARIABLE_NAME, CAST(COALESCE(sum(blks_read), 0) AS VARCHAR) AS VARIABLE_VALUE FROM pg_stat_database
        UNION ALL
        SELECT 'Innodb_buffer_pool_read_requests' AS VARIABLE_NAME, CAST(COALESCE(sum(blks_hit) + sum(blks_read), 1) AS VARCHAR) AS VARIABLE_VALUE FROM pg_stat_database
      )
    {% else %}
      performance_schema.global_status
    {% endif %} ;;

  dimension: variable_name {
    primary_key: yes
    type: string
    sql: ${TABLE}.VARIABLE_NAME ;;
  }

  dimension: variable_value {
    type: string
    sql: ${TABLE}.VARIABLE_VALUE ;;
  }

  dimension: variable_value_num {
    type: number
    sql:
      {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
        CAST(${TABLE}.VARIABLE_VALUE AS BIGINT)
      {% else %}
        CAST(${TABLE}.VARIABLE_VALUE AS UNSIGNED)
      {% endif %} ;;
  }
}
