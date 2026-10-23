view: acs_2017_zipcodes {

   derived_table: {
     sql: WITH
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
     _fips u
  WHERE
    z.state_fips_code = u.state_fips_code)

,acs_2017 AS (
  SELECT
    CAST(geo_id as STRING) AS zip_code,
    total_pop,
    median_age,
    households,
    income_per_capita,
    housing_units,
    vacant_housing_units_for_sale,
    owner_occupied_housing_units_lower_value_quartile,
    owner_occupied_housing_units_median_value,
    owner_occupied_housing_units_upper_value_quartile,
    ROUND(SAFE_DIVIDE(income_200000_or_more, households)*100, 2) AS percent_income_200000_or_more,
    ROUND(SAFE_DIVIDE(income_150000_199999, households)*100, 2) AS percent_income_150000_199999,
    ROUND(SAFE_DIVIDE(income_125000_149999, households)*100, 2) AS percent_income_125000_149999,
    ROUND(SAFE_DIVIDE(income_100000_124999, households)*100, 2) AS percent_income_100000_124999,
    ROUND(SAFE_DIVIDE(income_75000_99999, households)*100, 2) AS percent_income_75000_99999,
    ROUND(SAFE_DIVIDE(income_60000_74999, households)*100, 2) AS percent_income_60000_74999,
    ROUND(SAFE_DIVIDE(income_50000_59999, households)*100, 2) AS percent_income_50000_59999,
    ROUND(SAFE_DIVIDE(income_45000_49999, households)*100, 2) AS percent_income_45000_49999,
    ROUND(SAFE_DIVIDE(income_40000_44999, households)*100, 2) AS percent_income_40000_44999,
    ROUND(SAFE_DIVIDE(income_35000_39999, households)*100, 2) AS percent_income_35000_39999,
    ROUND(SAFE_DIVIDE(income_30000_34999, households)*100, 2) AS percent_income_30000_34999,
    ROUND(SAFE_DIVIDE(income_25000_29999, households)*100, 2) AS percent_income_25000_29999,
    ROUND(SAFE_DIVIDE(income_20000_24999, households)*100, 2) AS percent_income_20000_24999,
    ROUND(SAFE_DIVIDE(income_15000_19999, households)*100, 2) AS percent_income_15000_19999,
    ROUND(SAFE_DIVIDE(income_10000_14999, households)*100, 2) AS percent_income_10000_14999,
    ROUND(SAFE_DIVIDE(income_less_10000, households)*100, 2) AS percent_income_less_10000,
    ROUND(SAFE_DIVIDE(employed_pop, pop_16_over),4) AS rate_employment,
    ROUND(SAFE_DIVIDE(bachelors_degree_or_higher_25_64, pop_25_64),4)*100 AS rate_bachelors_degree_or_higher_25_64
  FROM
    `bigquery-public-data.census_bureau_acs.zip_codes_2017_5yr`
  JOIN
     _zip ON geo_id = zip_code)

SELECT
  *
FROM
  acs_2017
       ;;
  }

# Define your dimensions and measures here, like this:
  dimension: Zip_Code{
    description: "The 5 digit zip code associated with the ZCTA."
    type: zipcode
    sql: ${TABLE}.zip_code;;
  }

  measure: Banks_per_10000_Residents {
    description: "Number of banks for every 10,000 residents within a given ZCTA"
    type: average
    sql: ${TABLE}.banks_per_10000_residents ;;
  }

  measure: Population {
    description: "Total Population. The total number of all people living in a given geographic area. This is a very useful catch-all denominator when calculating rates."
    type: average
    sql: ${TABLE}.total_pop ;;
  }

  measure: Median_Age {
    description: "Median Age. The median age of all people in a given geographic area."
    type: average
    sql: ${TABLE}.median_age ;;
  }

  measure: Income_Per_Capita {
    description: "Per Capita Income in the past 12 Months. Per capita income is the mean income computed for every man, woman, and child in a particular group. It is derived by dividing the total income of a particular group by the total population."
    type: average
    sql: ${TABLE}.income_per_capita ;;
  }

  measure: Count_Households {
    description: "Households. A count of the number of households in each geography. A household consists of one or more people who live in the same dwelling and also share at meals or living accommodation, and may consist of a single family or some other grouping of people."
    type: average
    sql: ${TABLE}.households ;;
  }

  measure: Housing_Units {
    description: "Housing Units. A count of housing units in each geography. A housing unit is a house, an apartment, a mobile home or trailer, a group of rooms, or a single room occupied as separate living quarters, or if vacant, intended for occupancy as separate living quarters."
    type: average
    sql: ${TABLE}.housing_units ;;
  }

  measure: vacant_housing_units_for_sale {
    description: "Vacant Housing Units for Sale. The count of vacant housing units in a geographic area that are for sale. A housing unit is vacant if no one is living in it at the time of enumeration, unless its occupants are only temporarily absent. Units temporarily occupied at the time of enumeration entirely by people who have a usual residence elsewhere are also classified as vacant."
    type: average
    sql: ${TABLE}.vacant_housing_units_for_sale ;;
  }

  measure: owner_occupied_housing_units_lower_value_quartile {
    description: "Owner-Occupied Housing Units Lower Value Quartile"
    type: average
    sql: ${TABLE}.owner_occupied_housing_units_lower_value_quartile ;;
  }

  measure: owner_occupied_housing_units_median_value {
    description: "Owner-Occupied Housing Units Median Value. The middle value (median) in a geographic area owner occupied housing units."
    type: average
    sql: ${TABLE}.owner_occupied_housing_units_median_value ;;
  }

  measure: owner_occupied_housing_units_upper_value_quartile {
    description: "Owner-Occupied Housing Units Upper Value Quartile."
    type: average
    sql: ${TABLE}.owner_occupied_housing_units_upper_value_quartile ;;
  }

  measure: percent_income_200000_or_More {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_200000_or_more ;;
  }

  measure: percent_income_150000_199999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_150000_199999 ;;
  }

  measure: percent_income_125000_149999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_125000_149999 ;;
  }

  measure: percent_income_100000_124999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_100000_124999 ;;
  }

  measure: percent_income_75000_99999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_75000_99999 ;;
  }

  measure: percent_income_60000_74999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_60000_74999 ;;
  }

  measure: percent_income_50000_59999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_50000_59999 ;;
  }

  measure: percent_income_45000_49999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_45000_49999 ;;
  }

  measure: percent_income_40000_44999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_40000_44999 ;;
  }

  measure: percent_income_35000_39999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_35000_39999 ;;
  }

  measure: percent_income_30000_34999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_30000_34999 ;;
  }

  measure: percent_income_25000_29999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_25000_29999 ;;
  }

  measure: percent_income_20000_24999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_20000_24999 ;;
  }

  measure: percent_income_15000_19999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_15000_19999 ;;
  }


  measure: percent_income_10000_14999 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_10000_14999 ;;
  }

  measure: percent_income_less_10000 {
    description: "Households with income between the given range as USD. The number of households in a geographic area whose annual income was between the given range as USD."
    type: average
    sql: ${TABLE}.percent_income_10000_14999 ;;
  }

  measure: employment_rate {
    description: "Employed Population. The percentage of civilians 16 years old and over in each geography who either (1) were 'at work,' that is, those who did any work at all during the reference week as paid employees, worked in their own business or profession, worked on their own farm, or worked 15 hours or more as unpaid workers on a family farm or in a family business; or (2) were 'with a job but not at work,' that is, those who did not work during the reference week but had jobs or businesses from which they were temporarily absent due to illness, bad weather, industrial dispute, vacation, or other personal reasons. Excluded from the employed are people whose only activity consisted of work around the house or unpaid volunteer work for religious, charitable, and similar organizations; also excluded are all institutionalized people and people on active duty in the United States Armed Forces."
    type: average
    sql: ${TABLE}.rate_employment ;;
  }

  measure: rate_bachelors_degree_or_higher_25_64 {
    description: "Rate of the population with Bachelors Degree or Higher, Ages 25 to 64. The number of people in each geography who are between the ages of 25 and 64 who have attained a bachelors degree or higher."
    type: average
    sql: ${TABLE}.rate_bachelors_degree_or_higher_25_64 ;;
  }

}
