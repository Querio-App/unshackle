include: "/explores/events_statements_summary_by_digest.explore.lkml"
include: "/explores/global_status.explore.lkml"

test: assert_mysql_pk_is_unique {
  explore_source: events_statements_summary_by_digest {
    column: pk { field: events_statements_summary_by_digest.pk }
    column: count { field: events_statements_summary_by_digest.count }
  }
  assert: pk_is_unique {
    expression: ${events_statements_summary_by_digest.count} <= 1 ;;
  }
}

test: assert_mysql_global_status_not_null {
  explore_source: global_status {
    column: variable_name { field: global_status.variable_name }
  }
  assert: variable_name_is_not_null {
    expression: NOT is_null(${global_status.variable_name}) ;;
  }
}
