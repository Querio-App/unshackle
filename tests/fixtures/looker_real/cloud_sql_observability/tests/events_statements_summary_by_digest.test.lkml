include: "/explores/events_statements_summary_by_digest.explore.lkml"

test: assert_digest_pk_is_unique {
  explore_source: events_statements_summary_by_digest {
    column: pk { field: events_statements_summary_by_digest.pk }
    column: count { field: events_statements_summary_by_digest.count }
  }
  assert: pk_is_unique {
    expression: ${events_statements_summary_by_digest.count} <= 1 ;;
  }
}

test: assert_query_optimization_metrics_compile {
  explore_source: events_statements_summary_by_digest {
    column: query_health_tier { field: events_statements_summary_by_digest.query_health_tier }
    column: total_executions { field: events_statements_summary_by_digest.total_executions }
    column: rows_examined_per_row_sent { field: events_statements_summary_by_digest.rows_examined_per_row_sent }
    column: total_temp_disk_tables { field: events_statements_summary_by_digest.total_temp_disk_tables }
    limit: 10
  }
  assert: optimization_metrics_not_null {
    expression: NOT is_null(${events_statements_summary_by_digest.query_health_tier}) ;;
  }
}
