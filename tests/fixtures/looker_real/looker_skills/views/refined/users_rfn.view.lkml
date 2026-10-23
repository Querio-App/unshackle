include: "/views/raw/users.view.lkml"

view: +users {

  # =========================================================================
  # 1. KEYS (Primary & Foreign Keys)
  # =========================================================================

  dimension: id {
    primary_key: yes
    type: number
    label: "User ID"
    description: "Unique internal user identifier."
    group_label: "Identifiers"
    sql: ${TABLE}.id ;;
  }

  # =========================================================================
  # 2. DIMENSIONS (Attributes)
  # =========================================================================

  dimension: first_name {
    type: string
    label: "First Name"
    description: "The first name of the user."
    sql: ${TABLE}.first_name ;;
    required_access_grants: [can_view_pii]
  }

  dimension: last_name {
    type: string
    label: "Last Name"
    description: "The last name of the user."
    sql: ${TABLE}.last_name ;;
    required_access_grants: [can_view_pii]
  }

  dimension: full_name {
    type: string
    label: "Full Name"
    description: "Legal full name of the user."
    sql: CONCAT(${TABLE}.first_name, ' ', ${TABLE}.last_name) ;;
    required_access_grants: [can_view_pii]
  }

  dimension: email {
    type: string
    label: "Email Address"
    description: "The primary contact email address of the user."
    sql: ${TABLE}.email ;;
    required_access_grants: [can_view_pii]
  }

  dimension: country {
    type: string
    label: "Country"
    description: "The country where the user resides."
    group_label: "Geography"
    sql: ${TABLE}.country ;;

    link: {
      label: "🍩 Country User Distribution (Pie)"
      url: "@{DRILL_PIE_VIZ}{{ link }}&fields=users.full_name,users.users_count&f[users.country]={{ value | url_encode }}&sorts=users.users_count+desc&limit=10&toggle=vis"
    }
  }

  dimension: company_domain {
    type: string
    label: "Company Domain"
    description: "The email domain of the user's company."
    group_label: "Company Details"
    sql: REGEXP_EXTRACT(${TABLE}.email, r'@([^@]+)') ;;
  }

  dimension: company_name {
    type: string
    label: "Company Name"
    description: "The name of the user's company."
    group_label: "Company Details"
    sql: INITCAP(REGEXP_EXTRACT(${TABLE}.email, r'@([^.]+)\.')) ;;
  }

  dimension: password_hash {
    type: string
    sql: ${TABLE}.password_hash ;;
  }

  # --- Period over Period helper dimensions ---

  dimension: pop_data_date {
    hidden: yes
    type: date
    sql: ${created_date} ;;
  }

  dimension_group: pop_filter_start {
    hidden: yes
    type: time
    timeframes: [raw, date]
    sql: CASE WHEN {% date_start pop_date_filter %} IS NULL THEN '1970-01-01' ELSE CAST({% date_start pop_date_filter %} AS DATE) END ;;
  }

  dimension_group: pop_filter_end {
    hidden: yes
    type: time
    timeframes: [raw, date]
    sql: CASE WHEN {% date_end pop_date_filter %} IS NULL THEN CURRENT_DATE ELSE CAST({% date_end pop_date_filter %} AS DATE) END ;;
  }

  dimension: pop_interval_days {
    hidden: yes
    type: number
    sql: DATE_DIFF(${pop_filter_end_date}, ${pop_filter_start_date}, DAY) ;;
  }

  dimension: pop_previous_start_date {
    hidden: yes
    type: date
    sql: DATE_SUB(${pop_filter_start_date}, INTERVAL 
          {% if pop_compare_to._parameter_value == "'Yesterday'" %} 1 DAY
          {% elsif pop_compare_to._parameter_value == "'Week'" %} 1 WEEK
          {% elsif pop_compare_to._parameter_value == "'Month'" %} 1 MONTH
          {% elsif pop_compare_to._parameter_value == "'Year'" %} 1 YEAR
          {% else %} ${pop_interval_days} DAY
          {% endif %}
        ) ;;
  }

  dimension: pop_is_current_period {
    hidden: yes
    type: yesno
    sql: ${pop_data_date} > ${pop_filter_start_date} AND ${pop_data_date} <= ${pop_filter_end_date} ;;
  }

  dimension: pop_is_previous_period {
    hidden: yes
    type: yesno
    sql: ${pop_data_date} > ${pop_previous_start_date} AND ${pop_data_date} <= ${pop_filter_start_date} ;;
  }

  dimension: pop_period_group {
    view_label: "_PoP"
    label: "Comparison Period"
    description: "Pivots dates into Selected Period, Previous Period, or Excluded."
    type: string
    case: {
      when: {
        sql: ${pop_is_current_period} = true ;;
        label: "Selected Period"
      }
      when: {
        sql: ${pop_is_previous_period} = true ;;
        label: "Previous Period"
      }
      else: "Not in time period"
    }
  }

  # =========================================================================
  # 3. MEASURES (Aggregations - Use _count/_total/_amount Suffixes)
  # =========================================================================

  measure: users_count {
    type: count
    label: "Users Count"
    description: "Total number of users."
    group_label: "Counts"
    value_format_name: decimal_0
    drill_fields: []

    link: {
      label: "📈 Users Trend Over Time (Line)"
      url: "@{DRILL_LINE_VIZ}{{ link }}&fields=users.created_date,users.users_count&fill_fields=users.created_date&sorts=users.created_date+asc&limit=500&toggle=dat,pik,vis"
    }
    
    link: {
      label: "🍩 Users breakdown by Country (Pie)"
      url: "@{DRILL_PIE_VIZ}{{ link }}&fields=users.country,users.users_count&sorts=users.users_count+desc&limit=10&toggle=dat,pik,vis"
    }
  }

  measure: users_count_selected {
    view_label: "_PoP"
    label: "Users Count (Selected Period)"
    description: "Total count during the selected period."
    type: count
    value_format_name: decimal_0
    filters: [pop_period_group: "Selected Period"]
  }

  measure: users_count_previous {
    view_label: "_PoP"
    label: "Users Count (Previous Period)"
    description: "Total count during the previous comparison period."
    type: count
    value_format_name: decimal_0
    filters: [pop_period_group: "Previous Period"]
  }

  measure: users_count_pop_change {
    view_label: "_PoP"
    label: "Users Count PoP % Change"
    description: "Percentage change in count between current and previous periods."
    type: number
    value_format_name: percent_1
    sql: 1.0 * (${users_count_selected} - ${users_count_previous}) / NULLIF(${users_count_previous}, 0) ;;
    html:
      {% if value >= 0 %}
        <span style="color: #0F9D58;">▲ {{ rendered_value }}</span>
      {% else %}
        <span style="color: #DB4437;">▼ {{ rendered_value }}</span>
      {% endif %} ;;
  }

  # =========================================================================
  # 4. FILTERS & PARAMETERS (User Inputs)
  # =========================================================================

  filter: pop_date_filter {
    view_label: "_PoP"
    label: "Comparison Date Filter"
    description: "Select the current date range to compare against the previous period."
    type: date
    default_value: "7 days"
  }

  parameter: pop_compare_to {
    view_label: "_PoP"
    label: "Compare To"
    description: "Select the offset interval for the previous period comparison."
    type: string
    allowed_value: { value: "Yesterday" }
    allowed_value: { value: "Week" }
    allowed_value: { value: "Month" }
    allowed_value: { value: "Year" }
    default_value: "Year"
  }

  # =========================================================================
  # 5. SETS (Drill Paths)
  # =========================================================================

  set: detail {
    fields: [
      id,
      full_name,
      email,
      country
    ]
  }

  set: allowed_fields {
    fields: [
      id,
      first_name,
      last_name,
      full_name,
      email,
      country,
      company_domain,
      company_name,
      password_hash,
      created_date,
      created_raw,
      created_time,
      pop_period_group,
      users_count,
      users_count_selected,
      users_count_previous,
      users_count_pop_change,
      pop_date_filter,
      pop_compare_to
    ]
  }
}
