# Hand-written: one tile per behaviour the tests check.
- dashboard: semantics
  title: Semantics
  elements:
  # timeframes
  - name: event_timeframes
    model: semantics
    explore: events
    type: looker_grid
    fields: [events.id, events.created_date, events.created_week, events.created_month, events.created_quarter,
      events.created_year, events.created_day_of_week, events.created_day_of_week_index, events.created_month_num,
      events.created_month_name, events.created_quarter_of_year, events.created_week_of_year,
      events.created_day_of_month, events.created_day_of_year, events.created_hour_of_day, events.created_hour,
      events.created_minute, events.created_time_of_day, events.day_date, events.day_week, events.day_month,
      events.day_quarter, events.day_year, events.day_day_of_week, events.day_day_of_week_index,
      events.day_week_of_year]
    sorts: [events.id]
  - name: order_timeframes
    model: semantics
    explore: dm_orders
    type: looker_grid
    fields: [dm_orders.order_id, dm_orders.purchased_date, dm_orders.purchased_week, dm_orders.purchased_month,
      dm_orders.purchased_quarter, dm_orders.purchased_year, dm_orders.purchased_day_of_week,
      dm_orders.purchased_day_of_week_index, dm_orders.purchased_month_num, dm_orders.purchased_month_name,
      dm_orders.purchased_quarter_of_year, dm_orders.purchased_week_of_year, dm_orders.purchased_day_of_month,
      dm_orders.purchased_day_of_year]
    sorts: [dm_orders.order_id]
    limit: 5000
  # symmetric aggregates, sum_distinct, measures computed over the result
  - name: fanout_by_customer
    model: semantics
    explore: shop_orders
    type: looker_grid
    fields: [shop_orders.customer, shop_orders.count, shop_orders.total_shipping, shop_orders.avg_shipping,
      shop_items.count, shop_items.total_price, shop_items.shipping_sum_distinct, shop_items.shipping_avg_distinct]
    sorts: [shop_orders.customer]
  - name: fanout_total
    model: semantics
    explore: shop_orders
    type: single_value
    fields: [shop_orders.count, shop_orders.total_shipping, shop_orders.avg_shipping, shop_items.count,
      shop_items.total_price]
  - name: distinct_total
    model: semantics
    explore: shop_items
    type: looker_grid
    fields: [shop_items.count, shop_items.total_price, shop_items.shipping_sum_distinct,
      shop_items.shipping_avg_distinct, shop_items.shipping_naive_sum]
  - name: measure_types
    model: semantics
    explore: shop_orders
    type: looker_grid
    fields: [shop_orders.customer, shop_orders.total_shipping, shop_orders.shipping_pct, shop_orders.shipping_running]
    sorts: [shop_orders.customer]
  - name: fanout_by_manager
    model: semantics
    explore: dm_accounts
    type: looker_grid
    fields: [dm_accounts.account_manager, dm_accounts.count, dm_accounts.total_business_size,
      dm_accounts.avg_business_size, dm_orders.count, dm_orders.total_revenue]
    sorts: [dm_accounts.account_manager]
  - name: top_by_hidden_measure
    model: semantics
    explore: shop_orders
    type: looker_grid
    fields: [shop_orders.customer, shop_orders.avg_shipping]
    sorts: [shop_orders.total_shipping desc, shop_orders.ordered_month]
    limit: 1
  # Liquid
  - name: liquid_default
    model: semantics
    explore: pick
    type: looker_grid
    fields: [pick.customer, pick.count, pick.chosen_total, pick.chosen_label]
    sorts: [pick.customer]
  - name: liquid_set
    model: semantics
    explore: pick
    type: looker_grid
    fields: [pick.customer, pick.count, pick.chosen_total, pick.chosen_label]
    sorts: [pick.customer]
    filters:
      pick.customer_filter: "-nashua"
      pick.metric_choice: "shipping"
  - name: liquid_user_attribute
    model: semantics
    explore: pick
    type: looker_grid
    fields: [pick.customer, pick.user_locale, pick.count]
  # nested records
  - name: nested_by_id
    model: semantics
    explore: baskets
    type: looker_grid
    fields: [baskets.id, baskets.count, basket_lines.count, basket_lines.total_qty]
    sorts: [baskets.id]
  - name: nested_by_sku
    model: semantics
    explore: baskets
    type: looker_grid
    fields: [basket_lines.sku, baskets.count, basket_lines.total_qty]
    sorts: [basket_lines.sku]
  - name: nested_total
    model: semantics
    explore: baskets
    type: single_value
    fields: [baskets.count, basket_lines.count, basket_lines.total_qty]
  # dimension types, statistics, a timeframe filter
  - name: fact_types
    model: semantics
    explore: facts
    type: looker_grid
    fields: [facts.id, facts.y, facts.n_big, facts.n_tier, facts.n_tier_classic, facts.s_case]
    sorts: [facts.id]
  - name: fact_stats_by_big
    model: semantics
    explore: facts
    type: looker_grid
    fields: [facts.n_big, facts.count, facts.n_sum_big]
    sorts: [facts.n_big]
  - name: fact_month_filter
    model: semantics
    explore: facts
    type: single_value
    fields: [facts.count]
    filters:
      facts.at_month: "2027-07"
  # Liquid projects use, quoted filter values, hour timeframes
  - name: dialect
    model: semantics
    explore: quotes
    type: looker_grid
    fields: [quotes.id, quotes.dialect_name, quotes.dialect_branch]
    sorts: [quotes.id]
  - name: liquid_none
    model: semantics
    explore: quotes
    type: looker_grid
    fields: [quotes.id, quotes.range_start, quotes.range_end, quotes.flag_filtered, quotes.flag_in_query,
      quotes.flag_selected, quotes.p_str_param, quotes.p_str_branch, quotes.p_num_param]
    sorts: [quotes.id]
  - name: liquid_values
    model: semantics
    explore: quotes
    type: looker_grid
    fields: [quotes.id, quotes.s, quotes.range_start, quotes.range_end, quotes.flag_filtered, quotes.flag_in_query,
      quotes.flag_selected, quotes.p_str_param, quotes.p_str_branch, quotes.p_num_param]
    sorts: [quotes.id]
    filters:
      quotes.s: "-nobody"
      quotes.date_f: "2027-09-01 to 2027-10-01"
      quotes.p_str: "D'Angelo"
      quotes.p_num: "8"
  - name: date_start_month
    model: semantics
    explore: quotes
    type: looker_grid
    fields: [quotes.id, quotes.range_start, quotes.range_end]
    sorts: [quotes.id]
    filters:
      quotes.date_f: "2027-09"
  - name: date_start_relative
    model: semantics
    explore: quotes
    type: looker_grid
    fields: [quotes.id, quotes.range_start, quotes.range_end]
    sorts: [quotes.id]
    filters:
      quotes.date_f: "7 days"
  - name: filter_field_sql
    model: semantics
    explore: quotes
    type: single_value
    fields: [quotes.count]
    filters:
      quotes.f_sql: "-paper"
  - name: filter_quoted
    model: semantics
    explore: quotes
    type: single_value
    fields: [quotes.count]
    filters:
      quotes.s: "\"Michael's\""
  - name: filter_apostrophe
    model: semantics
    explore: quotes
    type: single_value
    fields: [quotes.count]
    filters:
      quotes.s: "Michael's,D'Angelo"
  - name: hour_timeframes
    model: semantics
    explore: quotes
    type: looker_grid
    fields: [quotes.id, quotes.at_time, quotes.at_hour, quotes.at_minute, quotes.at_time_of_day, quotes.at_hour_of_day,
      quotes.in_range]
    sorts: [quotes.id]
