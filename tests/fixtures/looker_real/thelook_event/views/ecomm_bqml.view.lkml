############################################# Feature Generation #############################################
view: ecomm_feature_generation {
  derived_table: {
    sql: (SELECT * except(user_id1) FROM
              (SELECT user_id
                      ,avg(sock__qty) as sock_avg_qty
                      ,avg(accessories_qty) as accessories_avg_qty
                      ,avg(swim_qty) as swim_avg_qty
                      ,avg(outerwear_and_coats_qty) as outerwear_and_coats_avg_qty
                      ,avg(tops_and_tees_qty) as tops_and_tees_avg_qty
                      ,avg(fashion_hoodies_qty) as fashion_hoodie_avg_qty
                      ,avg(shorts_qty) as shorts_avg_qty
                      ,avg(active_qty) as active_avg_qty
                      ,avg(jeans_qty) as jeans_avg_qty
                      ,avg(pants_qty) as pants_avg_qty
                      ,avg(sweaters_qty) as sweaters_avg_qty
                      ,avg(leggings_qty) as leggings_avg_qty
                      ,avg(underwear_qty) as underwear_avg_qty
                      ,avg(maternity_qty) as maternity_avg_qty
                      ,avg(dresses_qty) as dresses_avg_qty
                      ,avg(pants_and_capris_qty) as pants_and_capris_avg_qty
                      ,avg(skirts_qty) as skirts_avg_qty
                      ,avg(sleep_and_lounge_qty) as sleep_and_lounge_avg_qty
                      ,avg(blazers_and_jackets_qty) as blazers_and_jackets_avg_qty
                      ,avg(jumpsuits_and_rompers_qty) as jumpsuits_and_rompers_avg_qty
                      ,avg(clothing_sets_qty) as clothing_sets_avg_qty
                      ,avg(avg_price_per_order) as avg_unit_price
                      ,avg(quantity_per_order) as avg_quantity_per_order
                FROM (
                  select oi.user_id
                        ,oi.order_id
                        ,count(*) as quantity_per_order
                        ,sum(case when inv.product_category="Socks" then 1 else 0 end) AS sock__qty
                        ,sum(case when inv.product_category="Accessories" then 1 else 0 end) AS accessories_qty
                        ,sum(case when inv.product_category="Swim" then 1 else 0 end) AS swim_qty
                        ,sum(case when inv.product_category="Outerwear & Coats" then 1 else 0 end) AS outerwear_and_coats_qty
                        ,sum(case when inv.product_category="Tops & Tees" then 1 else 0 end) AS tops_and_tees_qty
                        ,sum(case when inv.product_category="Fashion Hoodies & Sweatshirts" then 1 else 0 end) AS fashion_hoodies_qty
                        ,sum(case when inv.product_category="Shorts" then 1 else 0 end) AS shorts_qty
                        ,sum(case when inv.product_category="Active" then 1 else 0 end) AS active_qty
                        ,sum(case when inv.product_category="Jeans" then 1 else 0 end) AS jeans_qty
                        ,sum(case when inv.product_category="Pants" then 1 else 0 end) AS pants_qty
                        ,sum(case when inv.product_category="Sweaters" then 1 else 0 end) AS sweaters_qty
                        ,sum(case when inv.product_category="Leggings" then 1 else 0 end) AS leggings_qty
                        ,sum(case when inv.product_category="Underwear" then 1 else 0 end) AS underwear_qty
                        ,sum(case when inv.product_category="Maternity" then 1 else 0 end) AS maternity_qty
                        ,sum(case when inv.product_category="Dresses" then 1 else 0 end) AS dresses_qty
                        ,sum(case when inv.product_category="Pants & Capris" then 1 else 0 end) AS pants_and_capris_qty
                        ,sum(case when inv.product_category="Skirts" then 1 else 0 end) AS skirts_qty
                        ,sum(case when inv.product_category="Blazers & Jackets" then 1 else 0 end) AS blazers_and_jackets_qty
                        ,sum(case when inv.product_category="Sleep & Lounge" then 1 else 0 end) AS sleep_and_lounge_qty
                        ,sum(case when inv.product_category="Jumpsuits & Rompers" then 1 else 0 end) AS jumpsuits_and_rompers_qty
                        ,sum(case when inv.product_category="Clothing Sets" then 1 else 0 end) AS clothing_sets_qty
                        ,avg(oi.sale_price) as avg_price_per_order
                  FROM `looker-private-demo.ecomm.order_items` as oi
                  JOIN `looker-private-demo.ecomm.inventory_items` as inv ON inv.id = oi.inventory_item_id
                  WHERE status = "Complete"
                  GROUP BY 1,2
                )
              GROUP BY 1
              ORDER BY user_id) as product
      JOIN (SELECT user_id as user_id1
            ,count(*) as event_usage
            ,sum(case when event_type="Home" then 1 else 0 end) AS home_visits
            ,sum(case when event_type="Purchase" then 1 else 0 end) AS purchases
            ,sum(case when event_type="Card" then 1 else 0 end) AS add_to_cart
            ,sum(case when event_type="Cancel" then 1 else 0 end) AS cancel_order
            ,sum(case when event_type="Email" then 1 else 0 end) AS email
            FROM `looker-private-demo.ecomm.events`
            WHERE user_id IS NOT NULL
            GROUP BY 1) as events
      ON product.user_id = events.user_id1)
 ;;
  }
}

############################################# Fit Model #####################################################
view: ecomm_fit {
  derived_table: {
    sql_create:
      CREATE OR REPLACE MODEL ${SQL_TABLE_NAME}
        OPTIONS(
                model_type='kmeans',
                num_clusters=5
              ) AS
      SELECT * except(user_id)
      FROM ${ecomm_feature_generation.SQL_TABLENAME};;
  }
}


############################################# Predict Clusters Customers belong in using Model #####################################################
view: ecomm_predict {
  derived_table: {
    sql: SELECT
        * EXCEPT(nearest_centroids_distance)
      FROM
        ML.PREDICT(MODEL `looker-private-demo.ecomm.kmeans_model5`,
        (SELECT * FROM
                    ${ecomm_feature_generation.SQL_TABLE_NAME}))
       ;;
  }

  dimension: centroid_id {
    type: number
    sql: ${TABLE}.CENTROID_ID;;
  }

  dimension: centroid_label {
    type: string
    sql:CASE WHEN ${TABLE}.CENTROID_ID = 1 THEN "Active Lifestyle Shoppers 👨‍💻"
             WHEN ${TABLE}.CENTROID_ID = 2 THEN "Tops & Tees Activist 👕"
             WHEN ${TABLE}.CENTROID_ID = 3 THEN "Capris Cadettes 🩳"
             WHEN ${TABLE}.CENTROID_ID = 4 THEN "Outdoor Adventurer 🏔"
             ELSE "Undergarment Afficianto 🩲"
        END;;
    action: {
      label: "Email Promotion to Cohort"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://sendgrid.com/favicon.ico"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        name: "Subject"
        required: yes
        default: "New {{ products.category._value }} Arrivals!"
      }
      form_param: {
        name: "Body"
        type: textarea
        required: yes
        default:
        "Dear {{ ecomm_predict.centroid_label }},

        We've got a new product instore that we'd love for you to check out. Offering a 10% discount on our new product - {{ products.item_name }}.
        Just used code NEWARRIVAL on your next checkout!

        Your friends at the Look"
      }
    }

    action: {
      label: "Start Adwords Campaign"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://www.google.com/s2/favicons?domain=www.adwords.google.com"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        type: select
        name: "Campaign Type"
        option: { name: "Spend" label: "Spend" }
        option: { name: "Leads" label: "Leads" }
        option: { name: "Website Traffic" label: "Website Traffic" }
        required: yes
      }
      form_param: {
        name: "Campaign Name"
        type: string
        required: yes
        default: "{{ products.item_name }} Campaign"
      }

      form_param: {
        name: "Product Category"
        type: string
        required: yes
        default: "{{ products.category }}"
      }

      form_param: {
        name: "Budget"
        type: string
        required: yes
      }

      form_param: {
        name: "Keywords"
        type: string
        required: yes
        default: "{{ products.item_name }}"
      }
    }


  }

  dimension: user_id {
    type: number
    sql: ${TABLE}.user_id ;;
  }

  dimension: sock_avg_qty {
    type: number
    sql: ${TABLE}.sock_avg_qty ;;
  }

  dimension: accessories_avg_qty {
    type: number
    sql: ${TABLE}.accessories_avg_qty ;;
  }

  dimension: swim_avg_qty {
    type: number
    sql: ${TABLE}.swim_avg_qty ;;
  }

  dimension: outerwear_and_coats_avg_qty {
    type: number
    sql: ${TABLE}.outerwear_and_coats_avg_qty ;;
  }

  dimension: tops_and_tees_avg_qty {
    type: number
    sql: ${TABLE}.tops_and_tees_avg_qty ;;
  }

  dimension: fashion_hoodie_avg_qty {
    type: number
    sql: ${TABLE}.fashion_hoodie_avg_qty ;;
  }

  dimension: shorts_avg_qty {
    type: number
    sql: ${TABLE}.shorts_avg_qty ;;
  }

  dimension: active_avg_qty {
    type: number
    sql: ${TABLE}.active_avg_qty ;;
  }

  dimension: jeans_avg_qty {
    type: number
    sql: ${TABLE}.jeans_avg_qty ;;
  }

  dimension: pants_avg_qty {
    type: number
    sql: ${TABLE}.pants_avg_qty ;;
  }

  dimension: sweaters_avg_qty {
    type: number
    sql: ${TABLE}.sweaters_avg_qty ;;
  }

  dimension: leggings_avg_qty {
    type: number
    sql: ${TABLE}.leggings_avg_qty ;;
  }

  dimension: underwear_avg_qty {
    type: number
    sql: ${TABLE}.underwear_avg_qty ;;
  }

  dimension: maternity_avg_qty {
    type: number
    sql: ${TABLE}.maternity_avg_qty ;;
  }

  dimension: dresses_avg_qty {
    type: number
    sql: ${TABLE}.dresses_avg_qty ;;
  }

  dimension: pants_and_capris_avg_qty {
    type: number
    sql: ${TABLE}.pants_and_capris_avg_qty ;;
  }

  dimension: skirts_avg_qty {
    type: number
    sql: ${TABLE}.skirts_avg_qty ;;
  }

  dimension: blazers_and_jackets_avg_qty {
    type: number
    sql: ${TABLE}.blazers_and_jackets_avg_qty ;;
  }

  dimension: jumpsuits_and_rompers_avg_qty {
    type: number
    sql: ${TABLE}.jumpsuits_and_rompers_avg_qty ;;
  }

  dimension: clothing_sets_avg_qty {
    type: number
    sql: ${TABLE}.clothing_sets_avg_qty ;;
  }

  dimension: avg_quantity_per_order {
    type: number
    sql: ${TABLE}.avg_quantity_per_order ;;
  }

  dimension: home_visits {
    type: number
    sql: ${TABLE}.home_visits ;;
  }

  dimension: add_to_cart {
    type: number
    sql: ${TABLE}.add_to_cart ;;
  }

  dimension: cancel_order {
    type: number
    sql: ${TABLE}.cancel_order ;;
  }

  dimension: email {
    type: number
    sql: ${TABLE}.email ;;
  }

  measure: event_usage {
    label: "Avg # of Web Events"
    type: average
    sql: ${TABLE}.event_usage ;;
    value_format_name: decimal_2
  }

  measure: purchases {
    label: "Avg # of Orders"
    type: average
    sql: ${TABLE}.purchases ;;
    value_format_name: decimal_2
  }

  measure: avg_unit_price {
    label: "Avg Unit Price"
    type: average
    sql: ${TABLE}.avg_unit_price ;;
    value_format_name: usd
  }

  measure: sleep_and_lounge_avg_qty {
    type: average
    sql: ${TABLE}.sleep_and_lounge_avg_qty ;;
  }

  measure: count {
    type: count
    drill_fields: [users.id, users.name, users.email, users.city, users.state]
  }
}
