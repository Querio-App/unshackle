########################################### Evaluate Hyperparameter [Number of Clusters] ##################################################
view: ecomm_model_eval {
  derived_table: {
    sql: SELECT * FROM (
            SELECT 4 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model4`, (SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME})) UNION ALL
            SELECT 5 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model5`, (SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME})) UNION ALL
            SELECT 6 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model6`, (SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME})) UNION ALL
            SELECT 7 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model7`, (SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME})) UNION ALL
            SELECT 8 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model8`, (SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME})) UNION ALL
            SELECT 10 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model10`,(SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME})) UNION ALL
            SELECT 12 as clusters_num,* FROM ML.EVALUATE(MODEL `looker-private-demo.ecomm.kmeans_model12`,(SELECT * except(user_id) FROM ${ecomm_feature_generation.SQL_TABLE_NAME}))) ;;
  }
  dimension: clusters_num {
    type: number
    sql: ${TABLE}.clusters_num ;;
    html: <p style="color: white; background-color: #4285F4; text-align: center; font-size: 125%">{{ rendered_value }}</p> ;;
  }
  dimension: davies_bouldin_index {
    type: number
    value_format_name: decimal_3
  }
  dimension: mean_squared_distance {
    type: number
    value_format_name: decimal_3
  }
}

##################################### Evaluate our Feature Metadata for our model ################################################
view: ecomm_feature_info {
  derived_table: {
    sql: SELECT 4 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model4`) UNION ALL
         SELECT 5 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model5`) UNION ALL
         SELECT 6 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model6`) UNION ALL
         SELECT 7 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model7`) UNION ALL
         SELECT 8 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model8`) UNION ALL
         SELECT 10 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model10`) UNION ALL
         SELECT 12 as clusters_num, * except(median, category_count) FROM ML.FEATURE_INFO(MODEL `looker-private-demo.ecomm.kmeans_model12`)
       ;;
  }

  dimension: clusters_num {
    type: number
    sql: ${TABLE}.clusters_num ;;
  }

  dimension: input {
    type: string
    sql: ${TABLE}.input ;;
  }

  dimension: min {
    type: number
    sql: ${TABLE}.min ;;
  }

  dimension: max {
    type: number
    sql: ${TABLE}.max ;;
  }

  dimension: mean {
    type: number
    sql: ${TABLE}.mean ;;
  }

  dimension: stddev {
    type: number
    sql: ${TABLE}.stddev ;;
  }

  dimension: null_count {
    type: number
    sql: ${TABLE}.null_count ;;
  }

  measure: count {
    type: count
    drill_fields: [detail*]
  }

  set: detail {
    fields: [
      clusters_num,
      input,
      min,
      max,
      mean,
      stddev,
      null_count
    ]
  }
}
################################################ Evaluate Training metatdata for our Models##################################################
view: ecomm_training_info {
  derived_table: {
    sql:  SELECT 4 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model4`) UNION ALL
          SELECT 5 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model5`) UNION ALL
          SELECT 6 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model6`) UNION ALL
          SELECT 7 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model7`) UNION ALL
          SELECT 8 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model8`) UNION ALL
          SELECT 10 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model10`) UNION ALL
          SELECT 12 as clusters_num, * FROM ML.TRAINING_INFO(MODEL `looker-private-demo.ecomm.kmeans_model12`);;
  }

  dimension: clusters_num {
    type: number
    sql: ${TABLE}.clusters_num ;;
  }

  dimension: training_run {
    type: number
    sql: ${TABLE}.training_run ;;
  }

  dimension: iteration {
    type: number
    sql: ${TABLE}.iteration ;;
  }

  measure: loss {
    type: max
    sql: ${TABLE}.loss ;;
  }

  dimension: cluster_info {
    hidden: yes
    sql: ${TABLE}.cluster_info ;;
  }

  measure: duration_ms {
    type: max
    sql: ${TABLE}.duration_ms ;;
  }
  measure: count {
    type: count
    drill_fields: [detail*]
  }

  set: detail {
    fields: [
      clusters_num,
      training_run,
      iteration,
      loss,
      cluster_info,
      duration_ms
    ]
  }
}

view: cluster_info {
  measure: cluster_radius {type:max}
  dimension: centroid_id { type: number }
  measure: cluster_size { type: max }
  measure: cluster_total { type: sum sql: ${TABLE}.cluster_size;; }
}


################################################ Evaluate Centroid Metadata for a Specific # of Cluster ################################################
view: kmeans_model5 {
  derived_table: {
    sql: SELECT
      5 as clusters_num
      , *
      FROM ML.CENTROIDS(MODEL `looker-private-demo.ecomm.kmeans_model5`)
       ;;
  }

  dimension: clusters_num {
    type: number
  }

  dimension: centroid_id {
    type: string
    sql: CONCAT("Cluster #", CAST(${TABLE}.centroid_id AS STRING)) ;;
  }

  dimension: feature {
    type: string
    sql: CASE
            WHEN ${TABLE}.feature = "accessories_avg_qty" THEN "accessories"
            WHEN ${TABLE}.feature = "active_avg_qty" THEN "active"
            WHEN ${TABLE}.feature = "blazers_and_jackets_avg_qty" THEN "jackets"
            WHEN ${TABLE}.feature = "cancel_order" THEN "cancel"
            WHEN ${TABLE}.feature = "fashion_hoodie_avg_qty" THEN "fashion"
            WHEN ${TABLE}.feature = "jeans_avg_qty" THEN "jeans"
            WHEN ${TABLE}.feature = "outerwear_and_coats_avg_qty" THEN "outerwear"
            WHEN ${TABLE}.feature = "pants_and_capris_avg_qty" THEN "capris"
            WHEN ${TABLE}.feature = "pants_avg_qty" THEN "pants"
            WHEN ${TABLE}.feature = "shorts_avg_qty" THEN "shorts"
            WHEN ${TABLE}.feature = "skirts_avg_qty" THEN "skirts"
            WHEN ${TABLE}.feature = "sleep_and_lounge_avg_qty" THEN "sleepwear"
            WHEN ${TABLE}.feature = "sweaters_avg_qty" THEN "sweaters"
            WHEN ${TABLE}.feature = "swim_avg_qty" THEN "swim"
            WHEN ${TABLE}.feature = "tops_and_tees_avg_qty" THEN "tops_and_tees"
            WHEN ${TABLE}.feature = "underwear_avg_qty" THEN "underwear"
            WHEN ${TABLE}.feature = "dresses_avg_qty" THEN "dresses"
          ELSE
            ${TABLE}.feature
          END
         ;;
  }

  measure: avg_qty {
    type: max
    sql: ${TABLE}.numerical_value ;;
  }

  dimension: categorical_value {
    type: string
    sql: ${TABLE}.categorical_value ;;
  }

  measure: count {
    type: count
    drill_fields: [detail*]
  }

  set: detail {
    fields: [centroid_id, feature, avg_qty, categorical_value]
  }
}
