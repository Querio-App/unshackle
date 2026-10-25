# Hand-written views over small literal tables (the rows are in tests/test_looker_semantics.py).

# ---------------------------------------------------------------- timeframes

view: events {
  sql_table_name: example.events ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension_group: created {
    type: time
    convert_tz: no
    timeframes: [raw, time, date, week, month, quarter, year, day_of_week, day_of_week_index,
                 month_num, month_name, quarter_of_year, week_of_year, day_of_month, day_of_year,
                 hour_of_day, hour, minute, time_of_day]
    sql: ${TABLE}.ts ;;
  }
  dimension_group: day {
    type: time
    datatype: date
    convert_tz: no
    timeframes: [date, week, month, quarter, year, day_of_week, day_of_week_index, week_of_year]
    sql: ${TABLE}.d ;;
  }
  measure: count { type: count }
}

# ---------------------------------------------------------------- measure types and symmetric aggregates

view: shop_orders {
  sql_table_name: example.shop_orders ;;
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
}

view: shop_items {
  # one row per item; order_shipping repeats the order's shipping on each of its items
  sql_table_name: example.shop_items ;;
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

# ---------------------------------------------------------------- Liquid in a derived table and in measures

view: pick {
  derived_table: {
    sql:
      SELECT customer, shipping, amount
      FROM example.pick_rows
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

# ---------------------------------------------------------------- nested records

view: baskets {
  sql_table_name: example.baskets ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: lines { hidden: yes sql: ${TABLE}.lines ;; }
  measure: count { type: count }
}

view: basket_lines {
  dimension: sku { type: string sql: ${TABLE}.sku ;; }
  dimension: qty { type: number sql: ${TABLE}.qty ;; }
  measure: total_qty { type: sum sql: ${qty} ;; }
  measure: count { type: count }
}
