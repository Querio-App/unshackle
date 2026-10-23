include: "/views/raw/inventory_items.view.lkml"

view: +inventory_items {
  # =========================================================================
  # 1. KEYS (Primary & Foreign Keys)
  # =========================================================================

  dimension: id {
    label: "Inventory Item ID"
    description: "Unique internal inventory item identifier."
    group_label: "Identifiers"
  }

  dimension: product_id {
    label: "Product ID"
    description: "Foreign key referencing the product."
    group_label: "Identifiers"
  }

  dimension: product_distribution_center_id {
    label: "Product Distribution Center ID"
    description: "Foreign key referencing the distribution center."
    group_label: "Identifiers"
  }

  dimension: product_sku {
    label: "Product SKU"
    description: "Stock Keeping Unit code."
    group_label: "Identifiers"
  }

  # =========================================================================
  # 2. DIMENSIONS (Attributes)
  # =========================================================================

  dimension: cost {
    label: "Cost"
    description: "Cost to acquire the inventory item."
    value_format_name: usd
  }

  dimension: product_brand {
    label: "Product Brand"
    description: "Brand name of the product."
  }

  dimension: product_category {
    label: "Product Category"
    description: "Product category."
  }

  dimension: product_department {
    label: "Product Department"
    description: "Target department."
  }

  dimension: product_name {
    label: "Product Name"
    description: "Product name."
  }

  dimension: product_retail_price {
    label: "Product Retail Price"
    description: "Retail sale price of the product."
    value_format_name: usd
  }

  # =========================================================================
  # 3. MEASURES (Aggregations)
  # =========================================================================

  measure: inventory_items_count {
    type: count
    label: "Inventory Items Count"
    description: "Total number of inventory items."
    group_label: "Counts"
    value_format_name: decimal_0
  }

  measure: total_cost {
    type: sum
    sql: ${cost} ;;
    label: "Total Cost"
    description: "Total cost of inventory items."
    value_format_name: usd
  }

  measure: total_retail_value {
    type: sum
    sql: ${product_retail_price} ;;
    label: "Total Retail Value"
    description: "Total retail value of inventory items."
    value_format_name: usd
  }
}
