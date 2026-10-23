include: "/explores/users.explore.lkml"

test: users_pk_is_unique {
  explore_source: users {
    column: id { field: users.id }
    column: users_count { field: users.users_count }
    
    # Optional: filter to optimize performance
    # filters: {
    #   field: users.pop_data_date
    #   value: "last 7 days"
    # }
  }

  assert: pk_uniqueness_verified {
    expression: ${users.users_count} = 1 ;;
  }
}

test: company_dimensions_are_populated {
  explore_source: users {
    column: id { field: users.id }
    column: company_domain { field: users.company_domain }
    column: company_name { field: users.company_name }
    limit: 5
  }

  assert: company_domain_not_null {
    expression: is_null(${users.company_domain}) = no ;;
  }

  assert: company_name_not_null {
    expression: is_null(${users.company_name}) = no ;;
  }
}
