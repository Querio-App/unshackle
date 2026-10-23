map_layer: us_blockgroups {
  feature_key: "blockgroup_geom"
  url: "https://github.com/shanecglass/Looker-Demos/blob/master/geos/blockgroups3%20(1)%20(3).json"
  format:  topojson
  property_key: "geo_id"
  label: "Block Group"
}

view: banks_by_blockgroup {
  derived_table: {
    sql:with  _fips AS
        (SELECT
          state_fips_code
         FROM
          `bigquery-public-data.census_utility.fips_codes_states`
         WHERE
          state_name = "Colorado")

      ,  _bg AS
        (SELECT
          b.geo_id,
          b.blockgroup_geom,
          ST_AREA(blockgroup_geom) bg_size
         FROM
           `bigquery-public-data.geo_census_blockgroups.us_blockgroups_national` b
            , _fips u
          WHERE
            b.state_fips_code = u.state_fips_code)

      , _zip AS
        (SELECT
          z.zip_code,
          z.zip_code_geom,
        FROM
        `bigquery-public-data.geo_us_boundaries.zip_codes` z
        , _fips u
          WHERE
            z.state_fips_code = u.state_fips_code)

      , bq_zip_overlap AS(
        SELECT
          b.geo_id,
          z.zip_code,
          ST_AREA(ST_INTERSECTION(b.blockgroup_geom,z.zip_code_geom))/b.bg_size AS overlap_size,
          b.blockgroup_geom
        FROM
           _zip z
          , _bg b
        WHERE
          ST_INTERSECTS(b.blockgroup_geom, z.zip_code_geom))

      ,locations AS
        (SELECT
          SUM(overlap_size * count_locations) AS locations_per_bg
          , l.zip_code
      FROM
        (SELECT
          COUNT(CONCAT(institution_name, " : ", branch_name)) AS count_locations,
          zip_code
         FROM
           `bigquery-public-data.fdic_banks.fdic_locations`
         WHERE
          state IS NOT NULL
          AND state_name IS NOT NULL
        GROUP BY
          2) l,
       bq_zip_overlap
      GROUP BY
        l.zip_code)

      SELECT
        b.geo_id,
        locations_per_bg
        ,b.zip_code
      FROM
        bq_zip_overlap b
      JOIN
        locations l USING (zip_code)
      ORDER BY
        locations_per_bg desc

             ;;
  }
  dimension:  geo_id{
    type: string
    map_layer_name: us_blockgroups
    sql: ${TABLE}.geo_id ;;
  }
  measure: locations_by_blockgroup {
    type: average
    sql: ${TABLE}.locations_per_bg ;;
  }
  dimension: zipcode {
    type: zipcode
    sql: ${TABLE}.zip_code ;;
  }
}
