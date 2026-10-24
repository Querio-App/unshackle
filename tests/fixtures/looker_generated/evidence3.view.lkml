# Evidence views, round 3: the Liquid that real projects use (_dialect._name, date_start / date_end,
# _is_filtered / _in_query / _is_selected), string and number parameters, filter fields with sql,
# quoted filter values and the hour / minute timeframes. Same use as evidence.view.lkml.

view: ev_l {
  derived_table: {
    sql:
      SELECT 1 AS id, 'apple' AS s, 1 AS n, TIMESTAMP '2026-12-31 23:30:45' AS ts UNION ALL
      SELECT 2, "Levi's", 5, TIMESTAMP '2026-12-01 08:05:00' UNION ALL
      SELECT 3, "O'Brien", 7, TIMESTAMP '2027-01-03 10:00:00' UNION ALL
      SELECT 4, CAST(NULL AS STRING), CAST(NULL AS INT64), CAST(NULL AS TIMESTAMP) ;;
  }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: s { type: string sql: ${TABLE}.s ;; }
  dimension: n { type: number sql: ${TABLE}.n ;; }
  dimension_group: at { type: time timeframes: [raw, time, hour, minute, time_of_day, hour_of_day] sql: ${TABLE}.ts ;; }

  filter: date_f { type: date }
  filter: f_sql { type: string sql: {% condition f_sql %} ${s} {% endcondition %} ;; }
  parameter: p_str { type: string default_value: "plain" }
  parameter: p_num { type: number default_value: "3" }

  dimension: dialect_name { type: string sql: '{{ _dialect._name }}' ;; }
  dimension: dialect_branch {
    type: string
    sql: {% if _dialect._name == 'postgres' or _dialect._name == 'google_cloud_postgres' %} 'postgres' {% else %} 'other' {% endif %} ;;
  }
  dimension: range_start { type: string sql: CAST({% date_start date_f %} AS STRING) ;; }
  dimension: range_end { type: string sql: CAST({% date_end date_f %} AS STRING) ;; }
  dimension: flag_filtered { type: string sql: {% if ev_l.s._is_filtered %} 'yes' {% else %} 'no' {% endif %} ;; }
  dimension: flag_in_query { type: string sql: {% if ev_l.s._in_query %} 'yes' {% else %} 'no' {% endif %} ;; }
  dimension: flag_selected { type: string sql: {% if ev_l.s._is_selected %} 'yes' {% else %} 'no' {% endif %} ;; }
  dimension: p_str_param { type: string sql: {% parameter p_str %} ;; }
  dimension: p_str_value { type: string sql: CAST('{{ p_str._parameter_value }}' AS STRING) ;; }
  dimension: p_str_branch { type: string sql: {% if p_str._parameter_value == "'plain'" %} 'quoted' {% elsif p_str._parameter_value == 'plain' %} 'bare' {% else %} 'neither' {% endif %} ;; }
  dimension: p_num_param { type: number sql: {% parameter p_num %} ;; }

  measure: count { type: count }
  measure: in_range { type: count filters: [at_time: "2026-12-01 to 2027-01-01"] }
}
