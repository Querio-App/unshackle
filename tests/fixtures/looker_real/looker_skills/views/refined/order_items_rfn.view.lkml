include: "/views/raw/order_items.view.lkml"

view: +order_items {
  # =========================================================================
  # 1. KEYS (Primary & Foreign Keys)
  # =========================================================================

  dimension: id {
    label: "Order Item ID"
    description: "Unique identifier for each order item."
    group_label: "Identifiers"
  }

  dimension: order_id {
    label: "Order ID"
    description: "Reference to the associated order."
    group_label: "Identifiers"
  }

  dimension: inventory_item_id {
    label: "Inventory Item ID"
    description: "Reference to the associated inventory item."
    group_label: "Identifiers"
  }

  dimension: product_id {
    type: number
    label: "Product ID"
    description: "Reference to the associated product."
    group_label: "Identifiers"
    sql: ${TABLE}.product_id ;;
  }

  # =========================================================================
  # 2. DIMENSIONS (Attributes)
  # =========================================================================

  dimension: sale_price {
    label: "Sale Price"
    description: "The price at which the item was sold."
    value_format_name: usd
  }

  dimension: gross_margin {
    type: number
    label: "Gross Margin"
    description: "Difference between sale price and product cost."
    value_format_name: usd
    sql: ${sale_price} - ${products.cost} ;;
  }

  dimension: phone {
    label: "Phone"
    description: "Phone number associated with the order item."
    required_access_grants: [can_view_pii]
  }

  dimension: phones {
    label: "Phones"
    description: "Additional phone numbers associated with the order item."
    required_access_grants: [can_view_pii]
  }

  # =========================================================================
  # 3. MEASURES (Aggregations)
  # =========================================================================

  measure: order_items_count {
    type: count
    label: "Order Items Count"
    description: "Total number of order items."
    group_label: "Counts"
    value_format_name: decimal_0
  }

  measure: total_gross_margin {
    type: sum
    label: "Total Gross Margin"
    description: "Total gross margin."
    group_label: "Financials"
    value_format_name: usd
    sql: ${gross_margin} ;;
  }

  set: allowed_fields {
    fields: [
      id,
      order_id,
      sale_price,
      inventory_item_id,
      returned_raw,
      returned_time,
      returned_date,
      phone,
      phones,
      product_id,
      gross_margin,
      order_items_count,
      total_gross_margin
    ]
  }
}
