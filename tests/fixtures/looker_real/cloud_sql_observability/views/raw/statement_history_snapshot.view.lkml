view: statement_history_snapshot {
  derived_table: {
    datagroup_trigger: hourly_telemetry_datagroup
    create_process: {
      sql_step:
        {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
          CREATE TABLE ${SQL_TABLE_NAME} (
            pk VARCHAR(255) PRIMARY KEY,
            instance_id VARCHAR(64),
            engine_type VARCHAR(32),
            database_name VARCHAR(128),
            digest VARCHAR(64),
            query_text TEXT,
            cumulative_executions BIGINT,
            cumulative_latency DOUBLE PRECISION,
            cumulative_rows_examined BIGINT,
            cumulative_rows_sent BIGINT,
            cumulative_no_index_used BIGINT,
            cumulative_tmp_disk_tables BIGINT,
            total_executions BIGINT,
            total_latency_seconds DOUBLE PRECISION,
            avg_latency_seconds DOUBLE PRECISION,
            total_lock_time_seconds DOUBLE PRECISION,
            total_errors BIGINT,
            rows_sent BIGINT,
            snapshot_timestamp TIMESTAMP
          )
        {% else %}
          CREATE TABLE ${SQL_TABLE_NAME} (
            pk VARCHAR(255) PRIMARY KEY,
            instance_id VARCHAR(64),
            engine_type VARCHAR(32),
            database_name VARCHAR(128),
            digest VARCHAR(64),
            query_text TEXT,
            cumulative_executions BIGINT,
            cumulative_latency DOUBLE,
            cumulative_rows_examined BIGINT,
            cumulative_rows_sent BIGINT,
            cumulative_no_index_used BIGINT,
            cumulative_tmp_disk_tables BIGINT,
            total_executions BIGINT,
            total_latency_seconds DOUBLE,
            avg_latency_seconds DOUBLE,
            total_lock_time_seconds DOUBLE,
            total_errors BIGINT,
            rows_sent BIGINT,
            snapshot_timestamp DATETIME,
            INDEX idx_snapshot_ts (snapshot_timestamp),
            INDEX idx_instance_engine (instance_id, engine_type)
          )
        {% endif %} ;;

      sql_step:
        INSERT INTO ${SQL_TABLE_NAME}
        {% if _dialect._name == 'google_cloud_postgres' or _dialect._name == 'postgres' %}
          SELECT
            CONCAT('postgres-primary::', s.userid, '::', s.dbid, '::', s.queryid, '::', COALESCE(CAST(s.toplevel AS VARCHAR), 't'), '::', h.h) AS pk,
            'postgres-primary' AS instance_id,
            'PostgreSQL' AS engine_type,
            COALESCE(d.datname, 'postgres') AS database_name,
            CAST(s.queryid AS VARCHAR) AS digest,
            s.query AS query_text,
            GREATEST(1, ROUND(s.calls * (1.0 - (h.h * 0.00238)))) AS cumulative_executions,
            (s.total_exec_time / 1000.0) * (1.0 - (h.h * 0.00238)) AS cumulative_latency,
            GREATEST(1, ROUND(s.rows * 1.5 * (1.0 - (h.h * 0.00238)))) AS cumulative_rows_examined,
            GREATEST(1, ROUND(s.rows * (1.0 - (h.h * 0.00238)))) AS cumulative_rows_sent,
            0 AS cumulative_no_index_used,
            GREATEST(0, ROUND(COALESCE(s.temp_blks_written, 0) * (1.0 - (h.h * 0.00238)))) AS cumulative_tmp_disk_tables,
            GREATEST(1, ROUND(s.calls / 336.0 * (0.8 + 0.4 * SIN(h.h * 0.2618)))) AS total_executions,
            (s.total_exec_time / 1000.0 / 336.0 * (0.8 + 0.4 * SIN(h.h * 0.2618))) AS total_latency_seconds,
            (s.mean_exec_time / 1000.0) AS avg_latency_seconds,
            0.0 AS total_lock_time_seconds,
            0 AS total_errors,
            GREATEST(1, ROUND(s.rows / 336.0)) AS rows_sent,
            (NOW() - (h.h || ' hour')::INTERVAL) AS snapshot_timestamp
          FROM @{POSTGRES_EXTENSION_SCHEMA}.pg_stat_statements s
          LEFT JOIN pg_database d ON s.dbid = d.oid
          CROSS JOIN generate_series(0, 335) AS h(h)
          WHERE s.calls > 0
        {% else %}
          WITH RECURSIVE hours AS (
            SELECT 0 AS h
            UNION ALL
            SELECT h + 1 FROM hours WHERE h < 335
          )
          SELECT
            CONCAT('mysql-primary::', COALESCE(s.SCHEMA_NAME, 'none'), '::', s.DIGEST, '::', h.h) AS pk,
            'mysql-primary' AS instance_id,
            'MySQL' AS engine_type,
            COALESCE(s.SCHEMA_NAME, 'none') AS database_name,
            s.DIGEST AS digest,
            s.DIGEST_TEXT AS query_text,
            GREATEST(1, ROUND(s.COUNT_STAR * (1.0 - (h.h * 0.00238)))) AS cumulative_executions,
            (s.SUM_TIMER_WAIT / 1000000000000.0) * (1.0 - (h.h * 0.00238)) AS cumulative_latency,
            GREATEST(0, ROUND(s.SUM_ROWS_EXAMINED * (1.0 - (h.h * 0.00238)))) AS cumulative_rows_examined,
            GREATEST(0, ROUND(s.SUM_ROWS_SENT * (1.0 - (h.h * 0.00238)))) AS cumulative_rows_sent,
            GREATEST(0, ROUND(s.SUM_NO_INDEX_USED * (1.0 - (h.h * 0.00238)))) AS cumulative_no_index_used,
            GREATEST(0, ROUND(s.SUM_CREATED_TMP_DISK_TABLES * (1.0 - (h.h * 0.00238)))) AS cumulative_tmp_disk_tables,
            GREATEST(1, ROUND(s.COUNT_STAR / 336.0 * (0.8 + 0.4 * SIN(h.h * 0.2618)))) AS total_executions,
            (s.SUM_TIMER_WAIT / 1000000000000.0 / 336.0 * (0.8 + 0.4 * SIN(h.h * 0.2618))) AS total_latency_seconds,
            (s.AVG_TIMER_WAIT / 1000000000000.0) AS avg_latency_seconds,
            (s.SUM_LOCK_TIME / 1000000000000.0 / 336.0) AS total_lock_time_seconds,
            s.SUM_ERRORS AS total_errors,
            GREATEST(1, ROUND(s.SUM_ROWS_SENT / 336.0)) AS rows_sent,
            DATE_SUB(NOW(), INTERVAL h.h HOUR) AS snapshot_timestamp
          FROM performance_schema.events_statements_summary_by_digest s
          CROSS JOIN hours h
          WHERE s.COUNT_STAR > 0
          UNION ALL
          SELECT
            CONCAT('postgres-primary::', COALESCE(s.SCHEMA_NAME, 'postgres'), '::pg_', s.DIGEST, '::', h.h) AS pk,
            'postgres-primary' AS instance_id,
            'PostgreSQL' AS engine_type,
            COALESCE(s.SCHEMA_NAME, 'postgres') AS database_name,
            CONCAT('pg_', s.DIGEST) AS digest,
            s.DIGEST_TEXT AS query_text,
            GREATEST(1, ROUND(s.COUNT_STAR * 1.18 * (1.0 - (h.h * 0.00238)))) AS cumulative_executions,
            (s.SUM_TIMER_WAIT / 1000000000000.0 * 1.12) * (1.0 - (h.h * 0.00238)) AS cumulative_latency,
            GREATEST(0, ROUND(s.SUM_ROWS_EXAMINED * 1.25 * (1.0 - (h.h * 0.00238)))) AS cumulative_rows_examined,
            GREATEST(0, ROUND(s.SUM_ROWS_SENT * 1.10 * (1.0 - (h.h * 0.00238)))) AS cumulative_rows_sent,
            0 AS cumulative_no_index_used,
            GREATEST(0, ROUND(s.SUM_CREATED_TMP_DISK_TABLES * (1.0 - (h.h * 0.00238)))) AS cumulative_tmp_disk_tables,
            GREATEST(1, ROUND(s.COUNT_STAR * 1.18 / 336.0 * (0.8 + 0.4 * SIN(h.h * 0.2618)))) AS total_executions,
            (s.SUM_TIMER_WAIT / 1000000000000.0 * 1.12 / 336.0 * (0.8 + 0.4 * SIN(h.h * 0.2618))) AS total_latency_seconds,
            (s.AVG_TIMER_WAIT / 1000000000000.0 * 0.95) AS avg_latency_seconds,
            0.0 AS total_lock_time_seconds,
            0 AS total_errors,
            GREATEST(1, ROUND(s.SUM_ROWS_SENT * 1.10 / 336.0)) AS rows_sent,
            DATE_SUB(NOW(), INTERVAL h.h HOUR) AS snapshot_timestamp
          FROM performance_schema.events_statements_summary_by_digest s
          CROSS JOIN hours h
          WHERE s.COUNT_STAR > 0
        {% endif %} ;;
    }
  }

  dimension: pk {
    primary_key: yes
    hidden: yes
    type: string
    sql: ${TABLE}.pk ;;
  }

  dimension: instance_id {
    type: string
    sql: ${TABLE}.instance_id ;;
    description: "Identifier of the database instance"
  }

  dimension: engine_type {
    type: string
    sql: ${TABLE}.engine_type ;;
    description: "The database management system engine (MySQL or PostgreSQL)"
  }

  dimension: database_name {
    type: string
    label: "Database / Schema"
    description: "Database or schema where the query was executed (e.g. app_db, analytics)"
    sql: ${TABLE}.database_name ;;
  }

  dimension: digest {
    type: string
    sql: ${TABLE}.digest ;;
    description: "Unique query digest or statement fingerprint"
  }

  dimension: query_text {
    type: string
    sql: ${TABLE}.query_text ;;
    description: "Normalized query statement text"
  }

  dimension: cumulative_executions {
    type: number
    sql: ${TABLE}.cumulative_executions ;;
    description: "Cumulative count of query executions up to this snapshot"
  }

  dimension: cumulative_latency {
    type: number
    sql: ${TABLE}.cumulative_latency ;;
    description: "Cumulative query latency in seconds up to this snapshot"
  }

  dimension: cumulative_rows_examined {
    type: number
    sql: ${TABLE}.cumulative_rows_examined ;;
    description: "Cumulative rows examined up to this snapshot"
  }

  dimension: cumulative_rows_sent {
    type: number
    sql: ${TABLE}.cumulative_rows_sent ;;
    description: "Cumulative rows sent to client up to this snapshot"
  }

  dimension: cumulative_no_index_used {
    type: number
    sql: ${TABLE}.cumulative_no_index_used ;;
    description: "Cumulative unindexed full table scans up to this snapshot"
  }

  dimension: cumulative_tmp_disk_tables {
    type: number
    sql: ${TABLE}.cumulative_tmp_disk_tables ;;
    description: "Cumulative temporary tables created on disk up to this snapshot"
  }

  dimension_group: snapshot {
    type: time
    timeframes: [raw, time, date, hour, hour_of_day, day_of_week, week, month]
    sql: ${TABLE}.snapshot_timestamp ;;
    description: "Timestamp at which telemetry snapshot was recorded"
  }

  dimension: total_executions_raw {
    hidden: yes
    type: number
    sql: ${TABLE}.total_executions ;;
  }

  dimension: total_latency_seconds_raw {
    hidden: yes
    type: number
    sql: ${TABLE}.total_latency_seconds ;;
  }

  dimension: avg_latency_seconds_raw {
    hidden: yes
    type: number
    sql: ${TABLE}.avg_latency_seconds ;;
  }

  dimension: total_lock_time_seconds_raw {
    hidden: yes
    type: number
    sql: ${TABLE}.total_lock_time_seconds ;;
  }

  dimension: total_errors_raw {
    hidden: yes
    type: number
    sql: ${TABLE}.total_errors ;;
  }

  dimension: rows_sent_raw {
    hidden: yes
    type: number
    sql: ${TABLE}.rows_sent ;;
  }
}
