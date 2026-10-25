# Hand-written: filter expressions, dimension types and statistical measures over example.facts.

view: facts {
  sql_table_name: example.facts ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: s { type: string sql: ${TABLE}.s ;; }
  dimension: n { type: number sql: ${TABLE}.n ;; }
  dimension: y { type: yesno sql: ${TABLE}.y ;; }
  dimension: n_big { type: yesno sql: ${n} > 3 ;; }
  dimension: n_tier { type: tier tiers: [0, 5, 10] style: integer sql: ${n} ;; }
  dimension: n_tier_classic { type: tier tiers: [0, 5, 10] style: classic sql: ${n} ;; }
  dimension: s_case {
    case: {
      when: { sql: ${s} = 'paper' ;; label: "Paper" }
      when: { sql: ${n} > 5 ;; label: "Big" }
      else: "Other"
    }
  }
  dimension_group: at { type: time timeframes: [raw, time, date, month, year] sql: ${TABLE}.ts ;; }
  dimension_group: on { type: time datatype: date convert_tz: no timeframes: [raw, date, month, year] sql: ${TABLE}.d ;; }

  measure: count { type: count }
  # strings
  measure: s_starts { type: count filters: [s: "pap%"] }
  measure: s_ends { type: count filters: [s: "%per"] }
  measure: s_contains { type: count filters: [s: "%ap%"] }
  measure: s_not_starts { type: count filters: [s: "-pap%"] }
  measure: s_eq_lower { type: count filters: [s: "paper"] }
  measure: s_eq_upper { type: count filters: [s: "PAPER"] }
  measure: s_list { type: count filters: [s: "paper,stapler"] }
  measure: s_not_list { type: count filters: [s: "-paper,-stapler"] }
  measure: s_escaped_comma { type: count filters: [s: "ink^,toner"] }
  measure: s_empty { type: count filters: [s: "EMPTY"] }
  measure: s_null { type: count filters: [s: "NULL"] }
  # numbers
  measure: n_closed { type: count filters: [n: "[2,6]"] }
  measure: n_open { type: count filters: [n: "(2,6)"] }
  measure: n_half_open { type: count filters: [n: "[2,6)"] }
  measure: n_to { type: count filters: [n: "2 to 6"] }
  measure: n_and { type: count filters: [n: ">=2 AND <=6"] }
  measure: n_or { type: count filters: [n: ">6 OR <3"] }
  measure: n_not { type: count filters: [n: "NOT 6"] }
  measure: n_minus { type: count filters: [n: "-2"] }
  measure: n_list { type: count filters: [n: "4,6"] }
  measure: n_not_list { type: count filters: [n: "NOT 4,6"] }
  measure: n_null { type: count filters: [n: "NULL"] }
  measure: n_not_null { type: count filters: [n: "NOT NULL"] }
  measure: n_gt { type: count filters: [n: ">4"] }
  # absolute dates
  measure: d_day { type: count filters: [on_date: "2027-06-30"] }
  measure: d_month { type: count filters: [on_date: "2027-07"] }
  measure: d_year { type: count filters: [on_date: "2027"] }
  measure: d_before { type: count filters: [on_date: "before 2027-07-01"] }
  measure: d_after { type: count filters: [on_date: "after 2027-07-01"] }
  measure: d_range { type: count filters: [on_date: "2027-06-01 to 2027-07-01"] }
  measure: d_null { type: count filters: [on_date: "NULL"] }
  measure: d_not_null { type: count filters: [on_date: "NOT NULL"] }
  # relative dates: their counts depend on the day
  measure: d_last_7 { type: count filters: [on_date: "7 days"] }
  measure: d_this_month { type: count filters: [on_date: "this month"] }
  measure: d_last_month { type: count filters: [on_date: "last month"] }
  measure: d_ago { type: count filters: [on_date: "before 30 days ago"] }
  # timestamps
  measure: t_day { type: count filters: [at_date: "2027-07-01"] }
  measure: t_before { type: count filters: [at_time: "before 2027-07-01 06:00:00"] }
  measure: t_month { type: count filters: [at_month: "2027-07"] }
  # tiers
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
