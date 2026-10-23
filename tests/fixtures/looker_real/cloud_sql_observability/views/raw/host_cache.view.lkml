view: host_cache {
  sql_table_name:
    {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' or '@{SQL_ENGINE_TYPE}' == 'postgres' %}
      (
        SELECT DISTINCT
          CAST(client_addr AS VARCHAR) AS IP,
          COALESCE(client_hostname, CAST(client_addr AS VARCHAR)) AS HOST
        FROM pg_stat_activity
        WHERE client_addr IS NOT NULL
      )
    {% else %}
      (
        SELECT DISTINCT
          SUBSTRING_INDEX(PROCESSLIST_HOST, ':', 1) AS IP,
          SUBSTRING_INDEX(PROCESSLIST_HOST, ':', 1) AS HOST
        FROM performance_schema.threads
        WHERE PROCESSLIST_HOST IS NOT NULL AND PROCESSLIST_HOST != ''
      )
    {% endif %} ;;

  dimension: ip {
    primary_key: yes
    type: string
    sql: ${TABLE}.IP ;;
  }

  dimension: host {
    type: string
    sql: ${TABLE}.HOST ;;
  }
}
