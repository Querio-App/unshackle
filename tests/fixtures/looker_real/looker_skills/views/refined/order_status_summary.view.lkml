view: order_status_summary {
  derived_table: {
    sql:
      SELECT
        status,
        COUNT(*) as status_count
      FROM @{DATASET_NAME}.orders
      WHERE
        {% if orders.status_filter._is_filtered %}
          status = {{ _filters['orders.status_filter'] | sql_quote }}
        {% else %}
          1=1
        {% endif %}
      GROUP BY 1
    ;;
  }

  dimension: status {
    primary_key: yes
    type: string
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

  dimension: status_count {
    type: number
    value_format_name: decimal_0
    sql: ${TABLE}.status_count ;;
  }

  set: allowed_fields {
    fields: [
      status,
      status_count
    ]
  }
}
