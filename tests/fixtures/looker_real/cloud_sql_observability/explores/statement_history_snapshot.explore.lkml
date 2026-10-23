include: "/views/refined/statement_history_snapshot_rfn.view.lkml"
include: "/views/refined/instance_dimension_lookup_rfn.view.lkml"

explore: statement_history_snapshot {
  label: "Database Observability: Historical Trends"
  description: "Explore for analyzing multi-engine query execution history, hourly performance trends, period-over-period telemetry, and latency regressions."

  sql_always_where:
    {% if statement_history_snapshot.pop_date_filter._is_filtered %}
      (${statement_history_snapshot.is_current_period} = true OR ${statement_history_snapshot.is_previous_period} = true)
    {% else %}
      1=1
    {% endif %} ;;

  join: instance_dimension_lookup {
    type: left_outer
    relationship: many_to_one
    sql_on: ${statement_history_snapshot.instance_id} = ${instance_dimension_lookup.instance_id} ;;
  }
}
