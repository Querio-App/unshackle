include: "/views/raw/orders.view.lkml"

view: +orders {


  # =========================================================================
  # 1. KEYS (Primary & Foreign Keys)
  # =========================================================================

  dimension: id {
    primary_key: yes
    type: number
    label: "Order ID"
    description: "Unique internal order identifier."
    group_label: "Identifiers"
    sql: ${TABLE}.order_id ;;
  }

  dimension: user_id {
    type: number
    label: "User ID"
    description: "Foreign key referencing the user."
    group_label: "Identifiers"
    sql: ${TABLE}.user_id ;;
  }

  dimension: product_id {
    type: number
    label: "Product ID"
    description: "Foreign key referencing the product."
    group_label: "Identifiers"
    sql: ${order_items.product_id} ;;
  }

  # =========================================================================
  # 2. DIMENSIONS (Attributes)
  # =========================================================================

  dimension: status {
    type: string
    label: "Status"
    description: "Order processing state (e.g. Complete, Processing, Cancelled)."
    sql: ${TABLE}.status ;;
    html:
      {% if value == 'Complete' %}
        <span style="color: #0F9D58;">{{ value }}</span>
      {% elsif value == 'Processing' %}
        <span style="color: #4285F4;">{{ value }}</span>
      {% elsif value == 'Cancelled' %}
        <span style="color: #DB4437;">{{ value }}</span>
      {% else %}
        <span>{{ value }}</span>
      {% endif %} ;;
  }

  filter: status_filter {
    type: string
    suggest_dimension: status
  }

  dimension: sale_price {
    type: number
    label: "Sale Price"
    description: "Sale price of the order."
    value_format_name: usd
    sql: ${order_items.sale_price} ;;
  }

  dimension_group: created {
    timeframes: [raw, time, date, month, year]
  }

  dimension: dynamic_timeframe {
    type: string
    label: "Dynamic Timeframe"
    description: "Dynamic timeframe based on the timeframe_selector parameter."
    sql:
      {% if timeframe_selector._parameter_value == "'month'" %}
        ${created_month}
      {% elsif timeframe_selector._parameter_value == "'year'" %}
        ${created_year}
      {% else %}
        ${created_month}
      {% endif %} ;;
  }

  # --- Period over Period helper dimensions ---

  dimension: pop_data_date {
    hidden: yes
    type: date
    sql: ${created_date} ;;
  }

  dimension_group: pop_filter_start {
    hidden: yes
    type: time
    timeframes: [raw, date]
    sql: CASE WHEN {% date_start pop_date_filter %} IS NULL THEN '1970-01-01' ELSE CAST({% date_start pop_date_filter %} AS DATE) END ;;
  }

  dimension_group: pop_filter_end {
    hidden: yes
    type: time
    timeframes: [raw, date]
    sql: CASE WHEN {% date_end pop_date_filter %} IS NULL THEN CURRENT_DATE ELSE CAST({% date_end pop_date_filter %} AS DATE) END ;;
  }

  dimension: pop_interval_days {
    hidden: yes
    type: number
    sql: DATE_DIFF(${pop_filter_end_date}, ${pop_filter_start_date}, DAY) ;;
  }

  dimension: pop_previous_start_date {
    hidden: yes
    type: date
    sql: DATE_SUB(${pop_filter_start_date}, INTERVAL 
          {% if pop_compare_to._parameter_value == "'Yesterday'" %} 1 DAY
          {% elsif pop_compare_to._parameter_value == "'Week'" %} 1 WEEK
          {% elsif pop_compare_to._parameter_value == "'Month'" %} 1 MONTH
          {% elsif pop_compare_to._parameter_value == "'Year'" %} 1 YEAR
          {% else %} ${pop_interval_days} DAY
          {% endif %}
        ) ;;
  }

  dimension: pop_is_current_period {
    hidden: yes
    type: yesno
    sql: ${pop_data_date} > ${pop_filter_start_date} AND ${pop_data_date} <= ${pop_filter_end_date} ;;
  }

  dimension: pop_is_previous_period {
    hidden: yes
    type: yesno
    sql: ${pop_data_date} > ${pop_previous_start_date} AND ${pop_data_date} <= ${pop_filter_start_date} ;;
  }

  dimension: pop_period_group {
    view_label: "_PoP"
    label: "Comparison Period"
    description: "Pivots dates into Selected Period, Previous Period, or Excluded."
    type: string
    case: {
      when: {
        sql: ${pop_is_current_period} = true ;;
        label: "Selected Period"
      }
      when: {
        sql: ${pop_is_previous_period} = true ;;
        label: "Previous Period"
      }
      else: "Not in time period"
    }
  }

  # =========================================================================
  # 3. MEASURES (Aggregations - Use _count/_total/_amount Suffixes)
  # =========================================================================

  measure: orders_count {
    type: count
    label: "Orders Count"
    description: "Total number of orders."
    group_label: "Counts"
    value_format_name: decimal_0
    drill_fields: []

    link: {
      label: "📈 Orders Trend Over Time (Line)"
      url: "@{DRILL_LINE_VIZ}{{ link }}&fields=orders.created_date,orders.orders_count&fill_fields=orders.created_date&sorts=orders.created_date+asc&limit=500&toggle=dat,pik,vis"
    }
    
    link: {
      label: "🍩 Orders breakdown by Status (Pie)"
      url: "@{DRILL_PIE_VIZ}{{ link }}&fields=orders.status,orders.orders_count&sorts=orders.orders_count+desc&limit=10&toggle=dat,pik,vis"
    }
  }

  measure: count {
    type: count
    label: "Count"
    description: "Total count of orders."
    value_format_name: decimal_0
    drill_fields: [order_drill_detail*]
  }

  measure: total_revenue {
    type: sum
    sql_distinct_key: ${order_items.id} ;;
    label: "Total Revenue"
    description: "Total revenue from orders."
    value_format_name: usd
    sql: ${sale_price} ;;
  }

  measure: revenue_total {
    type: sum
    sql_distinct_key: ${order_items.id} ;;
    label: "Revenue Total"
    description: "Total revenue from orders."
    group_label: "Financials"
    value_format_name: usd
    sql: ${sale_price} ;;
  }

  measure: revenue_average {
    type: average
    sql_distinct_key: ${order_items.id} ;;
    label: "Revenue Average"
    description: "Average revenue per order."
    group_label: "Financials"
    value_format_name: usd
    sql: ${sale_price} ;;
  }

  measure: total_margin {
    type: sum
    sql_distinct_key: ${order_items.id} ;;
    label: "Total Margin"
    description: "Total margin."
    group_label: "Financials"
    value_format_name: usd
    sql: ${order_items.gross_margin} ;;
  }

  measure: orders_count_selected {
    view_label: "_PoP"
    label: "Orders Count (Selected Period)"
    description: "Total count during the selected period."
    type: count
    value_format_name: decimal_0
    filters: [pop_period_group: "Selected Period"]
  }

  measure: orders_count_previous {
    view_label: "_PoP"
    label: "Orders Count (Previous Period)"
    description: "Total count during the previous comparison period."
    type: count
    value_format_name: decimal_0
    filters: [pop_period_group: "Previous Period"]
  }

  measure: orders_count_pop_change {
    view_label: "_PoP"
    label: "Orders Count PoP % Change"
    description: "Percentage change in count between current and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${orders_count_selected} - ${orders_count_previous}) / NULLIF(${orders_count_previous}, 0) ;;
    html:
      {% if value >= 0 %}
        <span style="color: #0F9D58;">▲ {{ rendered_value }}</span>
      {% else %}
        <span style="color: #DB4437;">▼ {{ rendered_value }}</span>
      {% endif %} ;;
  }

  measure: revenue_total_selected {
    view_label: "_PoP"
    label: "Total Revenue (Selected Period)"
    description: "Total revenue during the selected period."
    type: sum
    sql_distinct_key: ${order_items.id} ;;
    value_format_name: usd
    sql: ${sale_price} ;;
    filters: [pop_period_group: "Selected Period"]
  }

  measure: revenue_total_previous {
    view_label: "_PoP"
    label: "Total Revenue (Previous Period)"
    description: "Total revenue during the previous comparison period."
    type: sum
    sql_distinct_key: ${order_items.id} ;;
    value_format_name: usd
    sql: ${sale_price} ;;
    filters: [pop_period_group: "Previous Period"]
  }

  measure: revenue_total_pop_change {
    view_label: "_PoP"
    label: "Total Revenue PoP % Change"
    description: "Percentage change in revenue between current and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${revenue_total_selected} - ${revenue_total_previous}) / NULLIF(${revenue_total_previous}, 0) ;;
    html:
      {% if value >= 0 %}
        <span style="color: #0F9D58;">▲ {{ rendered_value }}</span>
      {% else %}
        <span style="color: #DB4437;">▼ {{ rendered_value }}</span>
      {% endif %} ;;
  }

  # =========================================================================
  # 4. FILTERS & PARAMETERS (User Inputs)
  # =========================================================================

  filter: pop_date_filter {
    view_label: "_PoP"
    label: "Comparison Date Filter"
    description: "Select the current date range to compare against the previous period."
    type: date
    default_value: "7 days"
  }

  parameter: pop_compare_to {
    view_label: "_PoP"
    label: "Compare To"
    description: "Select the offset interval for the previous period comparison."
    type: string
    allowed_value: { value: "Yesterday" }
    allowed_value: { value: "Week" }
    allowed_value: { value: "Month" }
    allowed_value: { value: "Year" }
    default_value: "Year"
  }

  parameter: timeframe_selector {
    type: string
    label: "Timeframe Selector"
    description: "Choose between Month and Year timeframes for dynamic date analysis."
    allowed_value: {
      value: "month"
      label: "Month"
    }
    allowed_value: {
      value: "year"
      label: "Year"
    }
    default_value: "month"
  }


  # =========================================================================
  # 5. SETS (Drill Paths)
  # =========================================================================

  set: detail {
    fields: [
      id,
      status,
      sale_price
    ]
  }

  set: order_drill_detail {
    fields: [
      id,
      user_id,
      status,
      sale_price,
      created_date
    ]
  }

  set: allowed_fields {
    fields: [
      id,
      user_id,
      product_id,
      status,
      sale_price,
      created_raw,
      created_time,
      created_date,
      created_month,
      created_year,
      dynamic_timeframe,
      pop_period_group,
      orders_count,
      count,
      total_revenue,
      revenue_total,
      revenue_average,
      total_margin,
      orders_count_selected,
      orders_count_previous,
      orders_count_pop_change,
      revenue_total_selected,
      revenue_total_previous,
      revenue_total_pop_change,
      status_filter,
      pop_date_filter,
      pop_compare_to,
      timeframe_selector
    ]
  }
}
