- dashboard: database_observability
  title: "Cloud SQL Fleet Observability"
  layout: newspaper
  preferred_viewer: dashboards-next
  
  filters:
    - name: pop_date_filter
      title: "Comparison Date Range"
      type: date_filter
      default_value: "7 days"
      allow_multiple_values: false
      required: false
      ui_config:
        type: relative_timeframes
        display: inline


    - name: engine_type
      title: "Database Engine"
      type: field_filter
      default_value: ""
      allow_multiple_values: true
      required: false
      ui_config:
        type: dropdown_menu
        display: inline
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      field: statement_history_snapshot.engine_type

    - name: database_name
      title: "Database / Schema"
      type: field_filter
      default_value: ""
      allow_multiple_values: true
      required: false
      ui_config:
        type: dropdown_menu
        display: inline
      model: operational_intelligence_cloud_sql
      explore: events_statements_summary_by_digest
      field: events_statements_summary_by_digest.database_name

    - name: query_search
      title: "Query Contains"
      type: string_filter
      default_value: ""
      allow_multiple_values: false
      required: false
      ui_config:
        type: input
        display: inline

    - name: process_user
      title: "Process User"
      type: string_filter
      default_value: ""
      allow_multiple_values: false
      required: false
      ui_config:
        type: input
        display: inline

  tabs:
    - name: mysql_observability_tab
      label: "MySQL Observability"
    - name: postgres_observability_tab
      label: "PostgreSQL Observability"
    - name: fleet_historical_trends_tab
      label: "Fleet Historical Trends"

  elements:
    # =========================================================================
    # TAB 1: MYSQL OBSERVABILITY
    # =========================================================================
    - name: mysql_banner
      type: text
      title_text: ""
      body_text: "<div style='display: flex; justify-content: space-between; align-items: center; padding: 4px 0 10px 0; border-bottom: 1px solid #E8EAED;'><div><div style='font-size: 20px; font-weight: 600; color: #1A73E8; margin-bottom: 2px;'>Cloud SQL Fleet Observability (MySQL)</div><div style='font-size: 13px; color: #5F6368;'>Real-time query performance diagnostics, connection saturation, and thread activity across Cloud SQL MySQL instances via <code>performance_schema</code>.</div></div><div style='display: flex; gap: 8px;'><a href='/dashboards/operational_intelligence_cloud_sql::database_observability' style='display: inline-block; background-color: #1A73E8; color: #FFFFFF; font-size: 12px; font-weight: 600; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #1A73E8;'>Fleet Observability</a><a href='/dashboards/mysql_observability::mysql_database_observability' style='display: inline-block; background-color: #FFFFFF; color: #1A73E8; font-size: 12px; font-weight: 500; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #DADCE0;'>MySQL</a><a href='/dashboards/postgres_observability::postgres_database_observability' style='display: inline-block; background-color: #FFFFFF; color: #1A73E8; font-size: 12px; font-weight: 500; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #DADCE0;'>PostgreSQL</a></div></div>"
      tab_name: mysql_observability_tab
      row: 0
      col: 0
      width: 24
      height: 2

    # --- ROW 1: HEALTH STATUS & DIAGNOSTIC SCORECARDS (6 TILES = 24 COLS) ---
    - name: mysql_fleet_health
      title: "Instance Health"
      subtitle: "Operational status assessment"
      model: mysql_observability
      explore: global_status
      type: single_value
      fields: [global_status.fleet_health_status]
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Evaluates operational health against real-time connection pool pressure (Healthy < 40, Warning 40-80, Critical > 80) and Buffer Pool Hit Ratio (Healthy > 95%, Warning 90-95%, Critical < 90%)."
      tab_name: mysql_observability_tab
      row: 2
      col: 0
      width: 4
      height: 4

    - name: mysql_buffer_cache_hit
      title: "Buffer Pool Hit Ratio"
      subtitle: "Memory read efficiency"
      model: mysql_observability
      explore: global_status
      type: single_value
      fields: [global_status.buffer_cache_hit_ratio]
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Percentage of logical read requests served directly from InnoDB buffer pool in RAM without physical disk I/O. Cloud SQL benchmark: Healthy > 95%."
      tab_name: mysql_observability_tab
      row: 2
      col: 4
      width: 4
      height: 4

    - name: mysql_active_connections
      title: "Active Connections"
      subtitle: "Connected client threads"
      model: mysql_observability
      explore: global_status
      type: single_value
      fields: [global_status.pop_active_connections_current, global_status.pop_active_connections_change]
      listen:
        pop_date_filter: global_status.pop_date_filter
        pop_compare_to: global_status.pop_compare_to
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: true
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Current count of active client connections to MySQL compared against the prior period benchmark. Thresholds: Healthy < 40, Warning 40-80, Critical > 80."
      tab_name: mysql_observability_tab
      row: 2
      col: 8
      width: 4
      height: 4

    - name: mysql_aborted_connections
      title: "Aborted Connections"
      subtitle: "Cumulative failed attempts"
      model: mysql_observability
      explore: global_status
      type: single_value
      fields: [global_status.aborted_connections_alert]
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Cumulative failed connection handshakes, unauthorized password attempts, or dropped TCP sockets since server start."
      tab_name: mysql_observability_tab
      row: 2
      col: 12
      width: 4
      height: 4

    - name: mysql_total_executions
      title: "Total Query Executions"
      subtitle: "Aggregated query throughput"
      model: mysql_observability
      explore: events_statements_summary_by_digest
      type: single_value
      fields: [events_statements_summary_by_digest.pop_total_executions_current, events_statements_summary_by_digest.pop_total_executions_change]
      listen:
        pop_date_filter: events_statements_summary_by_digest.pop_date_filter
        pop_compare_to: events_statements_summary_by_digest.pop_compare_to
        query_search: events_statements_summary_by_digest.query_text
        database_name: events_statements_summary_by_digest.database_name
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: false
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Total number of queries executed across MySQL instances within the selected timeframe with period-over-period delta."
      tab_name: mysql_observability_tab
      row: 2
      col: 16
      width: 4
      height: 4

    - name: mysql_total_instances
      title: "Connected Client Hosts"
      subtitle: "Unique remote client endpoints"
      model: mysql_observability
      explore: host_cache
      type: single_value
      fields: [host_cache.pop_total_instances_current, host_cache.pop_total_instances_change]
      listen:
        pop_date_filter: host_cache.pop_date_filter
        pop_compare_to: host_cache.pop_compare_to
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: false
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Total number of unique client IP addresses and hostnames connected to MySQL instances (Looker, workload daemons, Cloud SQL agents)."
      tab_name: mysql_observability_tab
      row: 2
      col: 20
      width: 4
      height: 4

    # --- ROW 2: CATEGORICAL BREAKDOWNS & THREAD PROFILES ---
    - name: mysql_connection_trends
      title: "Active Connections Breakdown"
      subtitle: "Current active thread saturation"
      model: mysql_observability
      explore: global_status
      type: looker_column
      fields: [global_status.variable_name, global_status.active_connections]
      filters:
        global_status.variable_name: "Threads_connected"
      note_state: collapsed
      note_display: hover
      note_text: "Tracks connection pool utilization to identify connection spikes and saturation risks."
      tab_name: mysql_observability_tab
      row: 6
      col: 0
      width: 12
      height: 6

    - name: mysql_threads_trends
      title: "Active vs. Running Worker Threads"
      subtitle: "Worker thread concurrency profile"
      model: mysql_observability
      explore: threads
      type: looker_column
      fields: [threads.type, threads.active_threads_count, threads.running_threads_count]
      listen:
        process_user: threads.processlist_user
      series_types:
        threads.running_threads_count: column
      y_axes:
        - label: "Total Threads"
          orientation: left
          series:
            - id: threads.active_threads_count
              name: "Active"
        - label: "Running"
          orientation: right
          series:
            - id: threads.running_threads_count
              name: "Running"
      note_state: collapsed
      note_display: hover
      note_text: "Compares total allocated threads against threads actively processing queries to gauge worker efficiency."
      tab_name: mysql_observability_tab
      row: 6
      col: 12
      width: 12
      height: 6

    # --- ROW 3: SLOW QUERIES & INVENTORY ---
    - name: mysql_slow_queries_grid
      title: "Top 10 Slowest Statements"
      subtitle: "Ranked by average latency"
      model: mysql_observability
      explore: events_statements_summary_by_digest
      type: looker_grid
      fields: [events_statements_summary_by_digest.query_text, events_statements_summary_by_digest.database_name, events_statements_summary_by_digest.total_executions, events_statements_summary_by_digest.average_execution_time, events_statements_summary_by_digest.total_lock_time]
      sorts: [events_statements_summary_by_digest.average_execution_time desc]
      filters:
        events_statements_summary_by_digest.query_text: "-NULL"
      listen:
        query_search: events_statements_summary_by_digest.query_text
        database_name: events_statements_summary_by_digest.database_name
      limit: 10
      note_state: collapsed
      note_display: hover
      note_text: "Identifies top bottleneck SQL statements ranked by average execution latency, execution count, and lock wait time."
      tab_name: mysql_observability_tab
      row: 12
      col: 0
      width: 12
      height: 8

    - name: mysql_client_hosts_grid
      title: "Connected Client Host Inventory"
      subtitle: "Active client endpoint listings"
      model: mysql_observability
      explore: host_cache
      type: looker_grid
      fields: [host_cache.host, host_cache.total_instances]
      limit: 10
      note_state: collapsed
      note_display: hover
      note_text: "Inventory of client hostnames and IP addresses actively communicating with MySQL."
      tab_name: mysql_observability_tab
      row: 12
      col: 12
      width: 12
      height: 8

    # --- ROW 4: USER PROCESSES & MISSING INDEXES ---
    - name: mysql_user_threads_distribution
      title: "Active Threads by Database User"
      subtitle: "Resource allocation by account"
      model: mysql_observability
      explore: threads
      type: looker_column
      fields: [threads.processlist_user, threads.active_threads_count]
      filters:
        threads.processlist_user: "-NULL"
      listen:
        process_user: threads.processlist_user
      note_state: collapsed
      note_display: hover
      note_text: "Shows the proportion of active threads consumed by each database user account."
      tab_name: mysql_observability_tab
      row: 20
      col: 0
      width: 12
      height: 7

    - name: mysql_query_no_index_scans
      title: "Statements Missing Indexes (Full Scans)"
      subtitle: "Queries executing full table scans"
      model: mysql_observability
      explore: events_statements_summary_by_digest
      type: looker_pie
      fields: [events_statements_summary_by_digest.query_text, events_statements_summary_by_digest.total_queries_no_index]
      sorts: [events_statements_summary_by_digest.total_queries_no_index desc]
      filters:
        events_statements_summary_by_digest.query_text: "-NULL"
        events_statements_summary_by_digest.total_queries_no_index: ">0"
      listen:
        query_search: events_statements_summary_by_digest.query_text
        database_name: events_statements_summary_by_digest.database_name
      limit: 10
      inner_radius: 50
      note_state: collapsed
      note_display: hover
      note_text: "Highlights queries executing without an index, indicating high-priority optimization opportunities."
      tab_name: mysql_observability_tab
      row: 20
      col: 12
      width: 12
      height: 7

    # =========================================================================
    # TAB 2: POSTGRESQL OBSERVABILITY
    # =========================================================================
    - name: postgres_banner
      type: text
      title_text: ""
      body_text: "<div style='display: flex; justify-content: space-between; align-items: center; padding: 4px 0 10px 0; border-bottom: 1px solid #E8EAED;'><div><div style='font-size: 20px; font-weight: 600; color: #1A73E8; margin-bottom: 2px;'>Cloud SQL Fleet Observability (PostgreSQL)</div><div style='font-size: 13px; color: #5F6368;'>Real-time query execution profiles, backend sessions, and worker distributions across Cloud SQL PostgreSQL instances via <code>pg_stat_statements</code>.</div></div><div style='display: flex; gap: 8px;'><a href='/dashboards/operational_intelligence_cloud_sql::database_observability' style='display: inline-block; background-color: #1A73E8; color: #FFFFFF; font-size: 12px; font-weight: 600; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #1A73E8;'>Fleet Observability</a><a href='/dashboards/mysql_observability::mysql_database_observability' style='display: inline-block; background-color: #FFFFFF; color: #1A73E8; font-size: 12px; font-weight: 500; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #DADCE0;'>MySQL</a><a href='/dashboards/postgres_observability::postgres_database_observability' style='display: inline-block; background-color: #FFFFFF; color: #1A73E8; font-size: 12px; font-weight: 500; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #DADCE0;'>PostgreSQL</a></div></div>"
      tab_name: postgres_observability_tab
      row: 0
      col: 0
      width: 24
      height: 2

    # --- ROW 1: HEALTH STATUS & DIAGNOSTIC SCORECARDS (6 TILES = 24 COLS) ---
    - name: postgres_fleet_health
      title: "Instance Health"
      subtitle: "Operational status assessment"
      model: postgres_observability
      explore: global_status
      type: single_value
      fields: [global_status.fleet_health_status]
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Evaluates operational health against real-time connection pool pressure (Healthy < 40, Warning 40-80, Critical > 80) and Buffer Cache Hit Ratio (Healthy > 95%, Warning 90-95%, Critical < 90%)."
      tab_name: postgres_observability_tab
      row: 2
      col: 0
      width: 4
      height: 4

    - name: postgres_buffer_cache_hit
      title: "Buffer Cache Hit Ratio"
      subtitle: "Shared buffers efficiency"
      model: postgres_observability
      explore: global_status
      type: single_value
      fields: [global_status.buffer_cache_hit_ratio]
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Percentage of block reads served directly from PostgreSQL shared buffers in RAM without disk I/O. Cloud SQL benchmark: Healthy > 95%."
      tab_name: postgres_observability_tab
      row: 2
      col: 4
      width: 4
      height: 4

    - name: postgres_active_connections
      title: "Active Connections"
      subtitle: "Connected client sessions"
      model: postgres_observability
      explore: global_status
      type: single_value
      fields: [global_status.pop_active_connections_current, global_status.pop_active_connections_change]
      listen:
        pop_date_filter: global_status.pop_date_filter
        pop_compare_to: global_status.pop_compare_to
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: true
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Total active client connections tracked in pg_stat_activity compared against the prior benchmark. Thresholds: Healthy < 40, Warning 40-80, Critical > 80."
      tab_name: postgres_observability_tab
      row: 2
      col: 8
      width: 4
      height: 4

    - name: postgres_aborted_connections
      title: "Aborted Connections"
      subtitle: "Failed / dropped attempts"
      model: postgres_observability
      explore: global_status
      type: single_value
      fields: [global_status.aborted_connections_alert]
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Failed connection attempts or dropped sessions. Currently 0 (Normal)."
      tab_name: postgres_observability_tab
      row: 2
      col: 12
      width: 4
      height: 4

    - name: postgres_total_executions
      title: "Total Statement Calls"
      subtitle: "Aggregated statement executions"
      model: postgres_observability
      explore: events_statements_summary_by_digest
      type: single_value
      fields: [events_statements_summary_by_digest.pop_total_executions_current, events_statements_summary_by_digest.pop_total_executions_change]
      listen:
        pop_date_filter: events_statements_summary_by_digest.pop_date_filter
        pop_compare_to: events_statements_summary_by_digest.pop_compare_to
        query_search: events_statements_summary_by_digest.query_text
        database_name: events_statements_summary_by_digest.database_name
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: false
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Total number of statement calls tracked by pg_stat_statements across PostgreSQL instances with period delta."
      tab_name: postgres_observability_tab
      row: 2
      col: 16
      width: 4
      height: 4

    - name: postgres_total_instances
      title: "Connected Client Hosts"
      subtitle: "Unique remote client endpoints"
      model: postgres_observability
      explore: host_cache
      type: single_value
      fields: [host_cache.pop_total_instances_current, host_cache.pop_total_instances_change]
      listen:
        pop_date_filter: host_cache.pop_date_filter
        pop_compare_to: host_cache.pop_compare_to
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: false
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Total distinct client IP addresses connecting to PostgreSQL instances."
      tab_name: postgres_observability_tab
      row: 2
      col: 20
      width: 4
      height: 4

    # --- ROW 2: CATEGORICAL BREAKDOWNS & BACKEND PROFILES ---
    - name: postgres_connection_trends
      title: "Active Connections Breakdown"
      subtitle: "Connected sessions profile"
      model: postgres_observability
      explore: global_status
      type: looker_column
      fields: [global_status.variable_name, global_status.active_connections]
      filters:
        global_status.variable_name: "Threads_connected"
      note_state: collapsed
      note_display: hover
      note_text: "Monitors PostgreSQL backend connection patterns to detect connection surges and pool limits."
      tab_name: postgres_observability_tab
      row: 6
      col: 0
      width: 12
      height: 6

    - name: postgres_threads_trends
      title: "Active vs. Running Worker Backends"
      subtitle: "Backend concurrency profile"
      model: postgres_observability
      explore: threads
      type: looker_column
      fields: [threads.type, threads.active_threads_count, threads.running_threads_count]
      listen:
        process_user: threads.processlist_user
      series_types:
        threads.running_threads_count: column
      y_axes:
        - label: "Total Backends"
          orientation: left
          series:
            - id: threads.active_threads_count
              name: "Active"
        - label: "Running"
          orientation: right
          series:
            - id: threads.running_threads_count
              name: "Running"
      note_state: collapsed
      note_display: hover
      note_text: "Compares total active sessions against sessions actively processing queries in pg_stat_activity."
      tab_name: postgres_observability_tab
      row: 6
      col: 12
      width: 12
      height: 6

    # --- ROW 3: SLOW QUERIES & INVENTORY ---
    - name: postgres_slow_queries_grid
      title: "Top 10 Slowest Statements"
      subtitle: "Ranked by average latency"
      model: postgres_observability
      explore: events_statements_summary_by_digest
      type: looker_grid
      fields: [events_statements_summary_by_digest.query_text, events_statements_summary_by_digest.database_name, events_statements_summary_by_digest.total_executions, events_statements_summary_by_digest.average_execution_time]
      sorts: [events_statements_summary_by_digest.average_execution_time desc]
      filters:
        events_statements_summary_by_digest.query_text: "-NULL"
      listen:
        query_search: events_statements_summary_by_digest.query_text
        database_name: events_statements_summary_by_digest.database_name
      limit: 10
      note_state: collapsed
      note_display: hover
      note_text: "Ranks top slowest PostgreSQL statements by mean execution time from pg_stat_statements."
      tab_name: postgres_observability_tab
      row: 12
      col: 0
      width: 12
      height: 8

    - name: postgres_client_hosts_grid
      title: "Connected Client Host Inventory"
      subtitle: "Active client endpoint listings"
      model: postgres_observability
      explore: host_cache
      type: looker_grid
      fields: [host_cache.host, host_cache.total_instances]
      limit: 10
      note_state: collapsed
      note_display: hover
      note_text: "Inventory of client IP addresses and hostnames actively connected to PostgreSQL."
      tab_name: postgres_observability_tab
      row: 12
      col: 12
      width: 12
      height: 8

    # --- ROW 4: USER SESSIONS & LATENCY DISTRIBUTION ---
    - name: postgres_user_threads_distribution
      title: "Active Backends by Database User"
      subtitle: "Resource allocation by role"
      model: postgres_observability
      explore: threads
      type: looker_column
      fields: [threads.processlist_user, threads.active_threads_count]
      filters:
        threads.processlist_user: "-NULL"
      listen:
        process_user: threads.processlist_user
      note_state: collapsed
      note_display: hover
      note_text: "Shows the proportion of backend processes associated with each PostgreSQL role."
      tab_name: postgres_observability_tab
      row: 20
      col: 0
      width: 12
      height: 7

    - name: postgres_execution_scatter
      title: "Execution Time vs Calls Distribution"
      subtitle: "Latency vs call volume correlation"
      model: postgres_observability
      explore: events_statements_summary_by_digest
      type: looker_scatter
      fields: [events_statements_summary_by_digest.total_executions, events_statements_summary_by_digest.average_execution_time]
      listen:
        query_search: events_statements_summary_by_digest.query_text
        database_name: events_statements_summary_by_digest.database_name
      note_state: collapsed
      note_display: hover
      note_text: "Plots statement execution volume against average latency to isolate high-frequency slow queries."
      tab_name: postgres_observability_tab
      row: 20
      col: 12
      width: 12
      height: 7

    # =========================================================================
    # TAB 3: FLEET HISTORICAL TRENDS
    # =========================================================================
    - name: historical_trends_banner
      type: text
      title_text: ""
      body_text: "<div style='display: flex; justify-content: space-between; align-items: center; padding: 4px 0 10px 0; border-bottom: 1px solid #E8EAED;'><div><div style='font-size: 20px; font-weight: 600; color: #1A73E8; margin-bottom: 2px;'>Cloud SQL Fleet Historical Telemetry Trends</div><div style='font-size: 13px; color: #5F6368;'>Hourly statement execution cadence, latency regressions, and long-term query throughput benchmarks across Cloud SQL instances.</div></div><div style='display: flex; gap: 8px;'><a href='/dashboards/operational_intelligence_cloud_sql::database_observability' style='display: inline-block; background-color: #1A73E8; color: #FFFFFF; font-size: 12px; font-weight: 600; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #1A73E8;'>Fleet Observability</a><a href='/dashboards/mysql_observability::mysql_database_observability' style='display: inline-block; background-color: #FFFFFF; color: #1A73E8; font-size: 12px; font-weight: 500; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #DADCE0;'>MySQL</a><a href='/dashboards/postgres_observability::postgres_database_observability' style='display: inline-block; background-color: #FFFFFF; color: #1A73E8; font-size: 12px; font-weight: 500; padding: 6px 16px; border-radius: 18px; text-decoration: none; border: 1.5px solid #DADCE0;'>PostgreSQL</a></div></div>"
      tab_name: fleet_historical_trends_tab
      row: 0
      col: 0
      width: 24
      height: 2

    # --- ROW 1: HISTORICAL SCORECARDS (4 TILES = 24 COLS) ---
    - name: hist_total_queries
      title: "Unique Statements Tracked"
      subtitle: "Distinct query fingerprints"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: single_value
      fields: [statement_history_snapshot.pop_unique_queries_current]
      listen:
        pop_date_filter: statement_history_snapshot.pop_date_filter
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Total number of unique normalized statement fingerprints tracked across historical snapshots."
      tab_name: fleet_historical_trends_tab
      row: 2
      col: 0
      width: 6
      height: 4

    - name: hist_total_executions
      title: "Total Historical Executions"
      subtitle: "Period-over-Period query volume"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: single_value
      fields: [statement_history_snapshot.pop_executions_current, statement_history_snapshot.pop_executions_change]
      listen:
        pop_date_filter: statement_history_snapshot.pop_date_filter
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: false
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Total statement executions within the selected comparison window with period-over-period delta comparison."
      tab_name: fleet_historical_trends_tab
      row: 2
      col: 6
      width: 6
      height: 4

    - name: hist_avg_latency
      title: "Fleet Average Latency"
      subtitle: "Period-over-Period latency"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: single_value
      fields: [statement_history_snapshot.pop_avg_latency_current, statement_history_snapshot.pop_avg_latency_change]
      listen:
        pop_date_filter: statement_history_snapshot.pop_date_filter
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      show_single_value_title: true
      show_comparison: true
      comparison_type: change
      comparison_reverse_colors: true
      show_comparison_label: true
      comparison_label: "vs Prior Period"
      note_state: collapsed
      note_display: hover
      note_text: "Execution latency across statements captured in historical snapshots with period-over-period delta comparison."
      tab_name: fleet_historical_trends_tab
      row: 2
      col: 12
      width: 6
      height: 4

    - name: hist_lock_time
      title: "Total Lock Wait Time"
      subtitle: "Cumulative lock contention"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: single_value
      fields: [statement_history_snapshot.pop_lock_time_current]
      listen:
        pop_date_filter: statement_history_snapshot.pop_date_filter
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      show_single_value_title: true
      note_state: collapsed
      note_display: hover
      note_text: "Total duration spent waiting on table and row locks across statements."
      tab_name: fleet_historical_trends_tab
      row: 2
      col: 18
      width: 6
      height: 4

    # --- ROW 2: HOURLY TREND LINES (2 TILES = 24 COLS) ---
    - name: hist_hourly_execution_trend
      title: "Hourly Statement Execution Throughput"
      subtitle: "Comparative execution volume by engine over selected timeframe"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: looker_line
      fields: [statement_history_snapshot.snapshot_hour, statement_history_snapshot.engine_type, statement_history_snapshot.total_executions]
      pivots: [statement_history_snapshot.engine_type]
      sorts: [statement_history_snapshot.snapshot_hour asc]
      series_colors:
        MySQL - statement_history_snapshot.total_executions: "#1A73E8"
        PostgreSQL - statement_history_snapshot.total_executions: "#EA4335"
      listen:
        pop_date_filter: statement_history_snapshot.snapshot_date
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      x_axis_gridlines: false
      y_axis_gridlines: true
      show_view_names: false
      show_y_axis_labels: true
      show_y_axis_ticks: true
      show_x_axis_label: true
      show_x_axis_ticks: true
      legend_position: center
      point_style: circle
      interpolation: monotone
      note_state: collapsed
      note_display: hover
      note_text: "Tracks query execution cadence per hour by database engine to highlight traffic peaks and cross-engine comparison."
      tab_name: fleet_historical_trends_tab
      row: 6
      col: 0
      width: 12
      height: 8

    - name: hist_hourly_latency_trend
      title: "Hourly Average Latency Evolution"
      subtitle: "Comparative query response time progression by engine over selected timeframe"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: looker_line
      fields: [statement_history_snapshot.snapshot_hour, statement_history_snapshot.engine_type, statement_history_snapshot.average_latency]
      pivots: [statement_history_snapshot.engine_type]
      sorts: [statement_history_snapshot.snapshot_hour asc]
      series_colors:
        MySQL - statement_history_snapshot.average_latency: "#1A73E8"
        PostgreSQL - statement_history_snapshot.average_latency: "#EA4335"
      listen:
        pop_date_filter: statement_history_snapshot.snapshot_date
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      x_axis_gridlines: false
      y_axis_gridlines: true
      show_view_names: false
      show_y_axis_labels: true
      show_y_axis_ticks: true
      show_x_axis_label: true
      show_x_axis_ticks: true
      legend_position: center
      point_style: circle
      interpolation: monotone
      note_state: collapsed
      note_display: hover
      note_text: "Monitors average query latency per hour by database engine to detect system regressions and compare engine performance."
      tab_name: fleet_historical_trends_tab
      row: 6
      col: 12
      width: 12
      height: 8

    # --- ROW 3: DETAILED HISTORICAL BOTTLENECKS (2 TILES = 24 COLS) ---
    - name: hist_top_latency_bottlenecks
      title: "Top Latency Bottleneck Statements"
      subtitle: "Statements with highest cumulative execution time"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: looker_grid
      fields: [statement_history_snapshot.engine_type, statement_history_snapshot.query_text, statement_history_snapshot.database_name, statement_history_snapshot.total_executions, statement_history_snapshot.average_latency, statement_history_snapshot.total_latency]
      sorts: [statement_history_snapshot.total_latency desc]
      filters:
        statement_history_snapshot.query_text: "-NULL"
      listen:
        pop_date_filter: statement_history_snapshot.snapshot_date
        query_search: statement_history_snapshot.query_text
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      limit: 10
      note_state: collapsed
      note_display: hover
      note_text: "Ranks top query bottlenecks by total cumulative execution time and average latency."
      tab_name: fleet_historical_trends_tab
      row: 14
      col: 0
      width: 14
      height: 8

    - name: hist_query_volume_by_engine
      title: "Execution Throughput by Engine"
      subtitle: "Workload distribution between database engines"
      model: operational_intelligence_cloud_sql
      explore: statement_history_snapshot
      type: looker_column
      fields: [statement_history_snapshot.engine_type, statement_history_snapshot.total_executions]
      sorts: [statement_history_snapshot.total_executions desc]
      listen:
        pop_date_filter: statement_history_snapshot.snapshot_date
        database_name: statement_history_snapshot.database_name
        engine_type: statement_history_snapshot.engine_type
      note_state: collapsed
      note_display: hover
      note_text: "Compares query volume processed by MySQL vs PostgreSQL."
      tab_name: fleet_historical_trends_tab
      row: 14
      col: 14
      width: 10
      height: 8
