 view: blockgroups_and_zip_codes {
  # Or, you could make this view a derived table, like this:
  derived_table: {
    sql: SELECT
      b.geo_id,
      z.zip_code,
      ST_AREA(ST_INTERSECTION(b.blockgroup_geom,z.zip_code_geom))/b.bg_size AS overlap_size,
      b.blockgroup_geom
    FROM
      utah_zip z
      ,utah_bg b
    WHERE
      ST_INTERSECTS(b.blockgroup_geom, z.zip_code_geom
        ;;
  }
}
