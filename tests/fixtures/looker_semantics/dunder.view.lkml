# Hand-written views over the fictional Dunder Mifflin tables (tests/data/dunder_mifflin/).

view: dm_accounts {
  sql_table_name: dunder_mifflin.accounts ;;
  dimension: account_id { primary_key: yes type: string sql: ${TABLE}.account_id ;; }
  dimension: account_manager { type: string sql: ${TABLE}.account_manager ;; }
  dimension: business_size { type: number sql: ${TABLE}.business_size ;; }
  measure: count { type: count }
  measure: total_business_size { type: sum sql: ${business_size} ;; }
  measure: avg_business_size { type: average sql: ${business_size} ;; }
}

view: dm_orders {
  sql_table_name: dunder_mifflin.orders ;;
  dimension: order_id { primary_key: yes type: string sql: ${TABLE}.order_id ;; }
  dimension: account_id { type: string sql: ${TABLE}.account_id ;; }
  dimension: revenue { type: number sql: ${TABLE}.post_discount_revenue ;; }
  dimension_group: purchased {
    type: time
    datatype: date
    convert_tz: no
    timeframes: [date, week, month, quarter, year, day_of_week, day_of_week_index, month_num, month_name,
                 quarter_of_year, week_of_year, day_of_month, day_of_year]
    sql: ${TABLE}.purchased_at ;;
  }
  measure: count { type: count }
  measure: total_revenue { type: sum sql: ${revenue} ;; }
}
