include: "/explores/statement_history_snapshot.explore.lkml"

test: assert_historical_snapshot_pk_is_unique {
  explore_source: statement_history_snapshot {
    column: pk { field: statement_history_snapshot.pk }
    column: count { field: statement_history_snapshot.count }
  }
  assert: pk_is_unique {
    expression: ${statement_history_snapshot.count} <= 1 ;;
  }
  assert: pk_is_not_null {
    expression: NOT is_null(${statement_history_snapshot.pk}) ;;
  }
}

test: assert_historical_snapshot_timestamps_not_null {
  explore_source: statement_history_snapshot {
    column: snapshot_time { field: statement_history_snapshot.snapshot_time }
    column: snapshot_date { field: statement_history_snapshot.snapshot_date }
    limit: 10
  }
  assert: snapshot_time_is_not_null {
    expression: NOT is_null(${statement_history_snapshot.snapshot_time}) ;;
  }
  assert: snapshot_date_is_not_null {
    expression: NOT is_null(${statement_history_snapshot.snapshot_date}) ;;
  }
}

test: assert_historical_snapshot_delta_metrics_compile {
  explore_source: statement_history_snapshot {
    column: total_executions { field: statement_history_snapshot.total_executions }
    column: delta_executions { field: statement_history_snapshot.delta_executions }
    column: delta_rows_examined { field: statement_history_snapshot.delta_rows_examined }
    column: delta_latency { field: statement_history_snapshot.delta_latency }
    column: delta_rows_sent { field: statement_history_snapshot.delta_rows_sent }
    limit: 10
  }
  assert: total_executions_greater_than_zero {
    expression: ${statement_history_snapshot.total_executions} > 0 ;;
  }
  assert: delta_executions_not_null {
    expression: NOT is_null(${statement_history_snapshot.delta_executions}) ;;
  }
}
