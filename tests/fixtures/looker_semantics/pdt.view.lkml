# An incremental persistent derived table: Looker queries it under its materialized name, and
# {% incrementcondition %} only applies when the table is rebuilt.

view: billing {
  derived_table: {
    sql:
      SELECT id, account, amount, export_time
      FROM example.billing_rows
      WHERE {% incrementcondition %} export_time {% endincrementcondition %} ;;
    datagroup_trigger: nightly
    increment_key: "export_date"
    increment_offset: 3
  }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: account { type: string sql: ${TABLE}.account ;; }
  dimension_group: export { type: time convert_tz: no timeframes: [raw, date] sql: ${TABLE}.export_time ;; }
  measure: count { type: count }
  measure: total_amount { type: sum sql: ${TABLE}.amount ;; }
}
