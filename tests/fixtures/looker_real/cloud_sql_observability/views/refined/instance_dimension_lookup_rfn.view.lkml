include: "/views/raw/instance_dimension_lookup.view.lkml"

view: +instance_dimension_lookup {
  # --- Measures ---
  measure: total_monitored_instances {
    type: count_distinct
    sql: ${instance_id} ;;
    description: "Total number of distinct monitored Cloud SQL instances."
    value_format_name: decimal_0
  }
}
