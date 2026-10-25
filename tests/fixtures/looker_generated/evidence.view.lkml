# Evidence views for #41: paste into one view file in a Looker dev-mode project on BigQuery,
# add the explores from evidence.explores.lkml to the model, then print the capture links with tests/live/looker/evidence_links.py.
# Every view reads literal rows, so the results are the same on any BigQuery connection.

# ---------------------------------------------------------------- timeframes (#18)

view: ev_time {
  derived_table: {
    sql:
      SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
      SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
      SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
      SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
      SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
      SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE) ;;
  }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }

  # convert_tz: no shows the formats alone; the second group shows what the connection's
  # time zone settings add
  dimension_group: created {
    type: time
    convert_tz: no
    timeframes: [raw, time, date, week, month, quarter, year, day_of_week, day_of_week_index,
                 month_num, month_name, quarter_of_year, week_of_year, day_of_month, day_of_year,
                 hour_of_day]
    sql: ${TABLE}.ts ;;
  }
  dimension_group: created_tz {
    type: time
    timeframes: [time, date, week, month]
    sql: ${TABLE}.ts ;;
  }
  dimension_group: day {
    type: time
    datatype: date
    convert_tz: no
    timeframes: [date, week, month, quarter, year, day_of_week]
    sql: ${TABLE}.d ;;
  }
  measure: count { type: count }
}

# ---------------------------------------------------------------- measure types (#31) and symmetric aggregates

view: ev_orders {
  derived_table: {
    sql:
      SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01' ;;
  }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: customer { type: string sql: ${TABLE}.customer ;; }
  dimension: shipping { type: number sql: ${TABLE}.shipping ;; }
  dimension_group: ordered { type: time datatype: date convert_tz: no timeframes: [date, month] sql: ${TABLE}.ordered ;; }

  measure: count { type: count }
  measure: total_shipping { type: sum sql: ${shipping} ;; }
  measure: avg_shipping { type: average sql: ${shipping} ;; }
  measure: shipping_pct { type: percent_of_total sql: ${total_shipping} ;; }
  measure: shipping_running { type: running_total sql: ${total_shipping} ;; }
  measure: order_ids { type: list list_field: id }
  measure: first_order { type: date sql: MIN(${TABLE}.ordered) ;; }
  measure: customers_string { type: string sql: STRING_AGG(DISTINCT ${customer}, ',' ORDER BY ${customer}) ;; }
}

view: ev_items {
  # one row per item; order_shipping repeats the order's shipping on every item
  derived_table: {
    sql:
      SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
      SELECT 12, 1, 6.0, 10.0 UNION ALL
      SELECT 13, 1, 1.0, 10.0 UNION ALL
      SELECT 21, 2, 9.0, 20.0 UNION ALL
      SELECT 31, 3, 2.5, 5.0 UNION ALL
      SELECT 32, 3, 2.5, 5.0 UNION ALL
      SELECT 51, 5, 8.0, 3.0 ;;
  }
  dimension: item_id { primary_key: yes type: number sql: ${TABLE}.item_id ;; }
  dimension: order_id { type: number sql: ${TABLE}.order_id ;; }
  dimension: price { type: number sql: ${TABLE}.price ;; }
  dimension: order_shipping { type: number sql: ${TABLE}.order_shipping ;; }

  measure: count { type: count }
  measure: total_price { type: sum sql: ${price} ;; }
  measure: shipping_sum_distinct { type: sum_distinct sql: ${order_shipping} ;; sql_distinct_key: ${order_id} ;; }
  measure: shipping_avg_distinct { type: average_distinct sql: ${order_shipping} ;; sql_distinct_key: ${order_id} ;; }
  measure: shipping_naive_sum { type: sum sql: ${order_shipping} ;; }
}

# ---------------------------------------------------------------- Liquid (#29)

view: ev_liquid {
  derived_table: {
    sql:
      SELECT customer, shipping, amount FROM (
        SELECT 'acme' AS customer, 10.0 AS shipping, 100.0 AS amount UNION ALL
        SELECT 'acme', 20.0, 50.0 UNION ALL
        SELECT 'bolt', 5.0, 70.0 UNION ALL
        SELECT CAST(NULL AS STRING), 3.0, 30.0
      )
      WHERE {% condition customer_filter %} customer {% endcondition %} ;;
  }
  filter: customer_filter { type: string }
  parameter: metric_choice {
    type: unquoted
    allowed_value: { value: "shipping" }
    allowed_value: { value: "amount" }
    default_value: "amount"
  }
  dimension: customer { type: string sql: ${TABLE}.customer ;; }
  dimension: user_locale { type: string sql: '{{ _user_attributes['locale'] }}' ;; }
  measure: count { type: count }
  measure: chosen_total { type: sum sql: ${TABLE}.{% parameter metric_choice %} ;; }
  measure: chosen_label {
    type: string
    sql: {% if metric_choice._parameter_value == 'shipping' %} 'shipping' {% else %} 'amount' {% endif %} ;;
  }
}

# ---------------------------------------------------------------- nested records (#30)

view: ev_nested {
  derived_table: {
    sql:
      SELECT 1 AS id, [STRUCT('x' AS sku, 2 AS qty), STRUCT('y' AS sku, 3 AS qty)] AS lines UNION ALL
      SELECT 2, ARRAY<STRUCT<sku STRING, qty INT64>>[] UNION ALL
      SELECT 3, [STRUCT('x' AS sku, 1 AS qty)] ;;
  }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: lines { hidden: yes sql: ${TABLE}.lines ;; }
  measure: count { type: count }
}

view: ev_nested_lines {
  dimension: sku { type: string sql: ${TABLE}.sku ;; }
  dimension: qty { type: number sql: ${TABLE}.qty ;; }
  measure: total_qty { type: sum sql: ${qty} ;; }
  measure: count { type: count }
}
