# Evidence views, round 2: filter expressions, dimension types, statistical measures and
# explore-level filters. Same use as evidence.view.lkml (paste into a second view file).

view: ev_f {
  derived_table: {
    sql:
      SELECT 1 AS id, 'apple' AS s, 1.0 AS n, DATE '2026-12-31' AS d, TIMESTAMP '2026-12-31 23:30:00' AS ts, TRUE AS y UNION ALL
      SELECT 2, 'Apple', 5.0, DATE '2027-01-03', TIMESTAMP '2027-01-03 10:00:00', FALSE UNION ALL
      SELECT 3, 'banana', 7.0, DATE '2026-03-01', TIMESTAMP '2026-03-01 12:00:00', CAST(NULL AS BOOL) UNION ALL
      SELECT 4, '', -5.0, DATE '2026-04-01', TIMESTAMP '2026-04-01 00:00:00', TRUE UNION ALL
      SELECT 5, 'a,b', 10.0, DATE '2026-12-01', TIMESTAMP '2026-12-01 08:00:00', FALSE UNION ALL
      SELECT 6, 'pineapple', 2.5, DATE '2027-01-04', TIMESTAMP '2027-01-04 00:15:00', FALSE UNION ALL
      SELECT 7, CAST(NULL AS STRING), CAST(NULL AS FLOAT64), CAST(NULL AS DATE), CAST(NULL AS TIMESTAMP), CAST(NULL AS BOOL) ;;
  }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: s { type: string sql: ${TABLE}.s ;; }
  dimension: n { type: number sql: ${TABLE}.n ;; }
  dimension: y { type: yesno sql: ${TABLE}.y ;; }
  dimension: n_big { type: yesno sql: ${n} > 3 ;; }
  dimension: n_tier { type: tier tiers: [0, 5, 10] style: integer sql: ${n} ;; }
  dimension: n_tier_classic { type: tier tiers: [0, 5, 10] style: classic sql: ${n} ;; }
  dimension: s_case {
    case: {
      when: { sql: ${s} = 'apple' ;; label: "Fruit A" }
      when: { sql: ${n} > 5 ;; label: "Big" }
      else: "Other"
    }
  }
  dimension_group: at { type: time timeframes: [raw, time, date, month, year] sql: ${TABLE}.ts ;; }
  dimension_group: on { type: time datatype: date convert_tz: no timeframes: [raw, date, month, year] sql: ${TABLE}.d ;; }

  measure: count { type: count }
  # strings
  measure: s_starts { type: count filters: [s: "app%"] }
  measure: s_ends { type: count filters: [s: "%ple"] }
  measure: s_contains { type: count filters: [s: "%an%"] }
  measure: s_not_starts { type: count filters: [s: "-app%"] }
  measure: s_eq_lower { type: count filters: [s: "apple"] }
  measure: s_eq_upper { type: count filters: [s: "APPLE"] }
  measure: s_list { type: count filters: [s: "apple,banana"] }
  measure: s_escaped_comma { type: count filters: [s: "a^,b"] }
  measure: s_empty { type: count filters: [s: "EMPTY"] }
  measure: s_null { type: count filters: [s: "NULL"] }
  # numbers
  measure: n_closed { type: count filters: [n: "[1,5]"] }
  measure: n_open { type: count filters: [n: "(1,5)"] }
  measure: n_half_open { type: count filters: [n: "[1,5)"] }
  measure: n_to { type: count filters: [n: "1 to 5"] }
  measure: n_and { type: count filters: [n: ">=1 AND <=5"] }
  measure: n_or { type: count filters: [n: ">5 OR <2"] }
  measure: n_not { type: count filters: [n: "NOT 5"] }
  measure: n_minus { type: count filters: [n: "-5"] }
  measure: n_list { type: count filters: [n: "5,7"] }
  measure: n_not_list { type: count filters: [n: "NOT 5,7"] }
  measure: n_null { type: count filters: [n: "NULL"] }
  measure: n_not_null { type: count filters: [n: "NOT NULL"] }
  measure: n_gt { type: count filters: [n: ">5"] }
  # dates (absolute; relative ones depend on today, their SQL is what matters)
  measure: d_day { type: count filters: [on_date: "2026-12-31"] }
  measure: d_month { type: count filters: [on_date: "2026-12"] }
  measure: d_year { type: count filters: [on_date: "2026"] }
  measure: d_before { type: count filters: [on_date: "before 2027-01-01"] }
  measure: d_after { type: count filters: [on_date: "after 2027-01-01"] }
  measure: d_range { type: count filters: [on_date: "2026-12-01 to 2027-01-01"] }
  measure: d_null { type: count filters: [on_date: "NULL"] }
  measure: d_not_null { type: count filters: [on_date: "NOT NULL"] }
  measure: d_last_7 { type: count filters: [on_date: "7 days"] }
  measure: d_this_month { type: count filters: [on_date: "this month"] }
  measure: d_last_month { type: count filters: [on_date: "last month"] }
  measure: d_ago { type: count filters: [on_date: "before 30 days ago"] }
  measure: t_day { type: count filters: [at_date: "2026-12-31"] }
  measure: t_before { type: count filters: [at_time: "before 2027-01-03 10:00:00"] }
  measure: t_month { type: count filters: [at_month: "2026-12"] }
  # tiers and cases
  measure: tier_filtered { type: count filters: [n_tier: "5 to 9"] }
  # statistics
  measure: n_median { type: median sql: ${n} ;; }
  measure: n_p25 { type: percentile percentile: 25 sql: ${n} ;; }
  measure: n_p90 { type: percentile percentile: 90 sql: ${n} ;; }
  measure: s_distinct { type: count_distinct sql: ${s} ;; }
  measure: n_sum { type: sum sql: ${n} ;; }
  measure: n_avg { type: average sql: ${n} ;; }
  measure: n_min { type: min sql: ${n} ;; }
  measure: n_max_date { type: max sql: ${on_raw} ;; }
  measure: n_per_row { type: number sql: ${n_sum} / NULLIF(${count}, 0) ;; }
  measure: n_sum_big { type: sum sql: ${n} ;; filters: [n_big: "yes"] }
}
