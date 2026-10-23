include: "/views/raw/products.view.lkml"

view: +products {
  # =========================================================================
  # 1. KEYS (Primary & Foreign Keys)
  # =========================================================================

  dimension: id {
    label: "Product ID"
    description: "Unique internal product identifier."
    group_label: "Identifiers"
  }

  dimension: distribution_center_id {
    label: "Distribution Center ID"
    description: "Foreign key referencing the distribution center."
    group_label: "Identifiers"
  }

  dimension: sku {
    label: "SKU"
    description: "Stock Keeping Unit code."
    group_label: "Identifiers"
  }

  # =========================================================================
  # 2. DIMENSIONS (Attributes)
  # =========================================================================

  dimension: brand {
    label: "Brand"
    description: "Brand name of the product."
  }

  dimension: category {
    label: "Category"
    description: "Product category (e.g. Jeans, Sweaters)."
  }

  dimension: cost {
    label: "Cost"
    description: "Cost to acquire/manufacture the product."
    value_format_name: usd
  }

  dimension: department {
    label: "Department"
    description: "Target demographic department (e.g. Men, Women)."
  }

  dimension: name {
    label: "Name"
    description: "Product name."
  }

  dimension: retail_price {
    label: "Retail Price"
    description: "Retail sale price of the product."
    value_format_name: usd
  }

  # =========================================================================
  # 3. MEASURES (Aggregations)
  # =========================================================================

  measure: products_count {
    type: count
    label: "Products Count"
    description: "Total number of products."
    group_label: "Counts"
    value_format_name: decimal_0
  }

  set: allowed_fields {
    fields: [
      id,
      brand,
      category,
      cost,
      department,
      distribution_center_id,
      name,
      retail_price,
      sku,
      products_count
    ]
  }
}
