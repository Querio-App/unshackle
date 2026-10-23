view: instance_dimension_lookup {
  derived_table: {
    sql:
      SELECT
        'mysql-primary' AS instance_id,
        'Cloud SQL MySQL Primary' AS instance_name,
        'MySQL' AS engine_type,
        'Standard Tier' AS machine_type,
        'Regional' AS region,
        'Primary Zone' AS zone,
        'MySQL 8.0' AS database_version
      UNION ALL
      SELECT
        'postgres-primary' AS instance_id,
        'Cloud SQL PostgreSQL Primary' AS instance_name,
        'PostgreSQL' AS engine_type,
        'Standard Tier' AS machine_type,
        'Regional' AS region,
        'Primary Zone' AS zone,
        'PostgreSQL 15' AS database_version ;;
  }

  dimension: instance_id {
    primary_key: yes
    type: string
    sql: ${TABLE}.instance_id ;;
    description: "Unique identifier of the Cloud SQL database instance"
  }

  dimension: instance_name {
    type: string
    sql: ${TABLE}.instance_name ;;
    description: "Human-readable display name of the database instance"
  }

  dimension: engine_type {
    type: string
    sql: ${TABLE}.engine_type ;;
    description: "Database engine family (MySQL or PostgreSQL)"
  }

  dimension: machine_type {
    type: string
    sql: ${TABLE}.machine_type ;;
    description: "GCP Cloud SQL machine type tier"
  }

  dimension: region {
    type: string
    sql: ${TABLE}.region ;;
    description: "Google Cloud deployment region"
  }

  dimension: zone {
    type: string
    sql: ${TABLE}.zone ;;
    description: "Google Cloud availability zone"
  }

  dimension: database_version {
    type: string
    sql: ${TABLE}.database_version ;;
    description: "Major and minor engine version release"
  }
}
