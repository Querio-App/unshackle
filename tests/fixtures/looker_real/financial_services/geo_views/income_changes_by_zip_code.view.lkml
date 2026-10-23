view: income_changes_by_zip_code {
  derived_table: {
    sql: WITH acs_changes_2011_2017 AS (WITH
        _fips AS (
        SELECT
          state_fips_code
        FROM
          `bigquery-public-data.census_utility.fips_codes_states`
        WHERE
          state_name = "Colorado"),

        _zip AS (
        SELECT
          z.zip_code,
          z.zip_code_geom,
        FROM
          `bigquery-public-data.geo_us_boundaries.zip_codes` z,
          _fips f
        WHERE
          z.state_fips_code = f.state_fips_code)

      ,acs_2011 AS (
        SELECT
          geo_id,
          total_pop,
          median_age,
          households,
          income_per_capita,
          housing_units,
          vacant_housing_units_for_sale,
          owner_occupied_housing_units_lower_value_quartile,
          owner_occupied_housing_units_median_value,
          owner_occupied_housing_units_upper_value_quartile,
          income_200000_or_more,
          income_150000_199999,
          income_125000_149999,
          income_100000_124999,
          income_75000_99999,
          income_60000_74999,
          income_50000_59999,
          income_45000_49999,
          income_40000_44999,
          income_35000_39999,
          income_30000_34999,
          income_25000_29999,
          income_20000_24999,
          income_15000_19999,
          income_10000_14999,
          income_less_10000,
          employed_pop,
          ROUND(SAFE_DIVIDE(bachelors_degree_or_higher_25_64, pop_25_64),4)*100 AS rate_bachelors_degree_or_higher_25_64
        FROM
          `bigquery-public-data.census_bureau_acs.zip_codes_2011_5yr`
        JOIN
          _zip ON zip_code = geo_id)
      ,acs_2017 AS (
        SELECT
          geo_id,
          total_pop,
          median_age,
          households,
          income_per_capita,
          housing_units,
          vacant_housing_units_for_sale,
          owner_occupied_housing_units_lower_value_quartile,
          owner_occupied_housing_units_median_value,
          owner_occupied_housing_units_upper_value_quartile,
          income_200000_or_more,
          income_150000_199999,
          income_125000_149999,
          income_100000_124999,
          income_75000_99999,
          income_60000_74999,
          income_50000_59999,
          income_45000_49999,
          income_40000_44999,
          income_35000_39999,
          income_30000_34999,
          income_25000_29999,
          income_20000_24999,
          income_15000_19999,
          income_10000_14999,
          income_less_10000,
          employed_pop,
          ROUND(SAFE_DIVIDE(bachelors_degree_or_higher_25_64, pop_25_64),4)*100 AS rate_bachelors_degree_or_higher_25_64
        FROM
          `bigquery-public-data.census_bureau_acs.zip_codes_2017_5yr`
        JOIN
          _zip ON zip_code = geo_id
        )

      ,acs_diff AS(
        SELECT
          CAST(acs17.geo_id as STRING) AS zip_code,
          ROUND(SAFE_SUBTRACT(acs17.total_pop, acs11.total_pop), 2) AS diff_total_pop,
          ROUND(SAFE_SUBTRACT(acs17.median_age, acs11.median_age), 2) AS diff_median_age,
          ROUND(SAFE_SUBTRACT(acs17.households, acs11.households), 2) AS diff_households,
          ROUND(SAFE_SUBTRACT(acs17.income_per_capita, acs11.income_per_capita), 2) AS diff_income_per_capita,
          ROUND(SAFE_SUBTRACT(acs17.housing_units, acs11.housing_units), 2) AS diff_housing_units,
          ROUND(SAFE_SUBTRACT(acs17.vacant_housing_units_for_sale, acs11.vacant_housing_units_for_sale), 2) AS diff_vacant_housing_units_for_sale,
          ROUND(SAFE_SUBTRACT(acs17. owner_occupied_housing_units_lower_value_quartile , acs11.owner_occupied_housing_units_lower_value_quartile), 2) AS diff_owner_occupied_housing_units_lower_value_quartile,
          ROUND(SAFE_SUBTRACT(acs17. owner_occupied_housing_units_median_value , acs11.owner_occupied_housing_units_median_value), 2) AS diff_owner_occupied_housing_units_median_value,
          ROUND(SAFE_SUBTRACT(acs17. owner_occupied_housing_units_upper_value_quartile , acs11.owner_occupied_housing_units_upper_value_quartile), 2) AS owner_occupied_housing_units_upper_value_quartile,
          ROUND(SAFE_SUBTRACT(acs17.income_200000_or_more, acs11.income_200000_or_more), 2) AS diff_income_200000_or_more,
          ROUND(SAFE_SUBTRACT(acs17.income_150000_199999, acs11.income_150000_199999), 2) AS diff_income_150000_199999,
          ROUND(SAFE_SUBTRACT(acs17.income_125000_149999, acs11.income_125000_149999), 2) AS diff_income_125000_149999,
          ROUND(SAFE_SUBTRACT(acs17.income_100000_124999, acs11.income_100000_124999), 2) AS diff_income_100000_124999,
          ROUND(SAFE_SUBTRACT(acs17.income_75000_99999, acs11.income_75000_99999), 2) AS diff_income_75000_99999,
          ROUND(SAFE_SUBTRACT(acs17.income_60000_74999, acs11.income_60000_74999), 2) AS diff_income_60000_74999,
          ROUND(SAFE_SUBTRACT(acs17.income_50000_59999, acs11.income_50000_59999), 2) AS diff_income_50000_59999,
          ROUND(SAFE_SUBTRACT(acs17.income_45000_49999, acs11.income_45000_49999), 2) AS diff_income_45000_49999,
          ROUND(SAFE_SUBTRACT(acs17.income_40000_44999, acs11.income_40000_44999), 2) AS diff_income_40000_44999,
          ROUND(SAFE_SUBTRACT(acs17.income_35000_39999, acs11.income_35000_39999), 2) AS diff_income_35000_39999,
          ROUND(SAFE_SUBTRACT(acs17.income_30000_34999, acs11.income_30000_34999), 2) AS diff_income_30000_34999,
          ROUND(SAFE_SUBTRACT(acs17.income_25000_29999, acs11.income_25000_29999), 2) AS diff_income_25000_29999,
          ROUND(SAFE_SUBTRACT(acs17.income_20000_24999, acs11.income_20000_24999), 2) AS diff_income_20000_24999,
          ROUND(SAFE_SUBTRACT(acs17.income_15000_19999, acs11.income_15000_19999), 2) AS diff_income_15000_19999,
          ROUND(SAFE_SUBTRACT(acs17.income_10000_14999, acs11.income_10000_14999), 2) AS diff_income_10000_14999,
          ROUND(SAFE_SUBTRACT(acs17.income_less_10000, acs11.income_less_10000), 2) AS diff_income_less_10000,
          ROUND(SAFE_SUBTRACT(acs17.employed_pop, acs11.employed_pop), 2) AS diff_employed_pop,
          ROUND(SAFE_SUBTRACT(acs17.rate_bachelors_degree_or_higher_25_64, acs11.rate_bachelors_degree_or_higher_25_64), 4) AS diff_rate_bachelors_degree_or_higher_25_64
        FROM
          acs_2017 acs17
        JOIN
          _zip ON zip_code = acs17.geo_id
        LEFT JOIN
          acs_2011 acs11 USING (geo_id)
        )

      SELECT
        *
      FROM
        acs_diff


           )
      SELECT
        acs_changes_2011_2017.zip_code,
        acs_changes_2011_2017.diff_income_per_capita,
        acs_changes_2011_2017.diff_rate_bachelors_degree_or_higher_25_64
      FROM acs_changes_2011_2017

      GROUP BY 1,2,3
      ORDER BY 1
      LIMIT 500
       ;;
  }

  measure: count {
    type: count
    drill_fields: [detail*]
  }

  dimension: zip_code {
    type: string
    sql: ${TABLE}.zip_code ;;
  }

  dimension: diff_income_per_capita {
    type: number
    sql: ${TABLE}.diff_income_per_capita ;;
  }

  dimension: diff_rate_bachelors_degree_or_higher_25_64 {
    type: number
    sql: ${TABLE}.diff_rate_bachelors_degree_or_higher_25_64 ;;
  }

  set: detail {
    fields: [zip_code, diff_income_per_capita, diff_rate_bachelors_degree_or_higher_25_64]
  }
}
