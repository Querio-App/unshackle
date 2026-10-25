# unshackle's BigQuery SQL for the evidence, written by tests/live/looker/bigquery_checks.py: do not edit.

view: chk_tile_fanout_by_customer {
  derived_table: {
    sql:
WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01'), ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
      SELECT 12, 1, 6.0, 10.0 UNION ALL
      SELECT 13, 1, 1.0, 10.0 UNION ALL
      SELECT 21, 2, 9.0, 20.0 UNION ALL
      SELECT 31, 3, 2.5, 5.0 UNION ALL
      SELECT 32, 3, 2.5, 5.0 UNION ALL
      SELECT 51, 5, 8.0, 3.0)
SELECT
    ev_orders.customer AS ev_orders_customer,
    COUNT(DISTINCT ev_orders.id) AS ev_orders_count,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_orders.shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) AS ev_orders_total_shipping,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_orders.shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN ev_orders.shipping IS NOT NULL THEN ev_orders.id END) AS FLOAT64), 0) AS ev_orders_avg_shipping,
    COUNT(DISTINCT ev_items.item_id) AS ev_items_count,
    COALESCE(SUM(ev_items.price), 0) AS ev_items_total_price,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_items.order_shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) AS ev_items_shipping_sum_distinct,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_items.order_shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN ev_items.order_shipping IS NOT NULL THEN ev_items.order_id END) AS FLOAT64), 0) AS ev_items_shipping_avg_distinct
FROM ev_orders AS ev_orders
LEFT JOIN ev_items AS ev_items ON ev_orders.id = ev_items.order_id
GROUP BY 1
ORDER BY ev_orders_customer ;;
  }
  dimension: ev_orders_customer { sql: ${TABLE}.ev_orders_customer ;; }
  dimension: ev_orders_count { sql: ${TABLE}.ev_orders_count ;; }
  dimension: ev_orders_total_shipping { sql: ${TABLE}.ev_orders_total_shipping ;; }
  dimension: ev_orders_avg_shipping { sql: ${TABLE}.ev_orders_avg_shipping ;; }
  dimension: ev_items_count { sql: ${TABLE}.ev_items_count ;; }
  dimension: ev_items_total_price { sql: ${TABLE}.ev_items_total_price ;; }
  dimension: ev_items_shipping_sum_distinct { sql: ${TABLE}.ev_items_shipping_sum_distinct ;; }
  dimension: ev_items_shipping_avg_distinct { sql: ${TABLE}.ev_items_shipping_avg_distinct ;; }
}

view: chk_tile_fanout_total {
  derived_table: {
    sql:
WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01'), ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
      SELECT 12, 1, 6.0, 10.0 UNION ALL
      SELECT 13, 1, 1.0, 10.0 UNION ALL
      SELECT 21, 2, 9.0, 20.0 UNION ALL
      SELECT 31, 3, 2.5, 5.0 UNION ALL
      SELECT 32, 3, 2.5, 5.0 UNION ALL
      SELECT 51, 5, 8.0, 3.0)
SELECT
    COUNT(DISTINCT ev_orders.id) AS ev_orders_count,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_orders.shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) AS ev_orders_total_shipping,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_orders.shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_orders.id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN ev_orders.shipping IS NOT NULL THEN ev_orders.id END) AS FLOAT64), 0) AS ev_orders_avg_shipping,
    COUNT(DISTINCT ev_items.item_id) AS ev_items_count,
    COALESCE(SUM(ev_items.price), 0) AS ev_items_total_price
FROM ev_orders AS ev_orders
LEFT JOIN ev_items AS ev_items ON ev_orders.id = ev_items.order_id
ORDER BY ev_orders_count DESC ;;
  }
  dimension: ev_orders_count { sql: ${TABLE}.ev_orders_count ;; }
  dimension: ev_orders_total_shipping { sql: ${TABLE}.ev_orders_total_shipping ;; }
  dimension: ev_orders_avg_shipping { sql: ${TABLE}.ev_orders_avg_shipping ;; }
  dimension: ev_items_count { sql: ${TABLE}.ev_items_count ;; }
  dimension: ev_items_total_price { sql: ${TABLE}.ev_items_total_price ;; }
}

view: chk_tile_distinct_total {
  derived_table: {
    sql:
WITH ev_items AS (SELECT 11 AS item_id, 1 AS order_id, 4.0 AS price, 10.0 AS order_shipping UNION ALL
      SELECT 12, 1, 6.0, 10.0 UNION ALL
      SELECT 13, 1, 1.0, 10.0 UNION ALL
      SELECT 21, 2, 9.0, 20.0 UNION ALL
      SELECT 31, 3, 2.5, 5.0 UNION ALL
      SELECT 32, 3, 2.5, 5.0 UNION ALL
      SELECT 51, 5, 8.0, 3.0)
SELECT
    COUNT(DISTINCT ev_items.item_id) AS ev_items_count,
    COALESCE(SUM(ev_items.price), 0) AS ev_items_total_price,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_items.order_shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) AS ev_items_shipping_sum_distinct,
    CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE(ev_items.order_shipping, 0) AS NUMERIC) + CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)) - SUM(DISTINCT CAST(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST(ev_items.order_id AS STRING))), 1, 15)) AS INT64) AS NUMERIC)), 0), 6) AS FLOAT64) / NULLIF(CAST(COUNT(DISTINCT CASE WHEN ev_items.order_shipping IS NOT NULL THEN ev_items.order_id END) AS FLOAT64), 0) AS ev_items_shipping_avg_distinct,
    COALESCE(SUM(ev_items.order_shipping), 0) AS ev_items_shipping_naive_sum
FROM ev_items AS ev_items
ORDER BY ev_items_count DESC ;;
  }
  dimension: ev_items_count { sql: ${TABLE}.ev_items_count ;; }
  dimension: ev_items_total_price { sql: ${TABLE}.ev_items_total_price ;; }
  dimension: ev_items_shipping_sum_distinct { sql: ${TABLE}.ev_items_shipping_sum_distinct ;; }
  dimension: ev_items_shipping_avg_distinct { sql: ${TABLE}.ev_items_shipping_avg_distinct ;; }
  dimension: ev_items_shipping_naive_sum { sql: ${TABLE}.ev_items_shipping_naive_sum ;; }
}

view: chk_tile_measure_types {
  derived_table: {
    sql:
WITH ev_orders AS (SELECT 1 AS id, 'acme' AS customer, 10.0 AS shipping, DATE '2026-01-05' AS ordered UNION ALL
      SELECT 2, 'acme', 20.0, DATE '2026-01-20' UNION ALL
      SELECT 3, 'bolt', 5.0, DATE '2026-02-02' UNION ALL
      SELECT 4, 'core', 7.5, DATE '2026-02-14' UNION ALL
      SELECT 5, CAST(NULL AS STRING), 3.0, DATE '2026-03-01')
SELECT
    ev_orders.customer AS ev_orders_customer,
    COALESCE(SUM(ev_orders.shipping), 0) AS ev_orders_total_shipping,
    100.0 * COALESCE(SUM(ev_orders.shipping), 0) / NULLIF(SUM(COALESCE(SUM(ev_orders.shipping), 0)) OVER (), 0) AS ev_orders_shipping_pct,
    SUM(COALESCE(SUM(ev_orders.shipping), 0)) OVER (ORDER BY ev_orders.customer ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ev_orders_shipping_running
FROM ev_orders AS ev_orders
GROUP BY 1
ORDER BY ev_orders_customer ;;
  }
  dimension: ev_orders_customer { sql: ${TABLE}.ev_orders_customer ;; }
  dimension: ev_orders_total_shipping { sql: ${TABLE}.ev_orders_total_shipping ;; }
  dimension: ev_orders_shipping_pct { sql: ${TABLE}.ev_orders_shipping_pct ;; }
  dimension: ev_orders_shipping_running { sql: ${TABLE}.ev_orders_shipping_running ;; }
}

view: chk_tile_liquid_default {
  derived_table: {
    sql:
SELECT
    ev_liquid.customer AS ev_liquid_customer,
    COUNT(*) AS ev_liquid_count,
    COALESCE(SUM(ev_liquid.amount), 0) AS ev_liquid_chosen_total,
    ('amount') AS ev_liquid_chosen_label
FROM (SELECT customer, shipping, amount FROM (
        SELECT 'acme' AS customer, 10.0 AS shipping, 100.0 AS amount UNION ALL
        SELECT 'acme', 20.0, 50.0 UNION ALL
        SELECT 'bolt', 5.0, 70.0 UNION ALL
        SELECT CAST(NULL AS STRING), 3.0, 30.0
      )
      WHERE 1=1) AS ev_liquid
GROUP BY 1
ORDER BY ev_liquid_customer ;;
  }
  dimension: ev_liquid_customer { sql: ${TABLE}.ev_liquid_customer ;; }
  dimension: ev_liquid_count { sql: ${TABLE}.ev_liquid_count ;; }
  dimension: ev_liquid_chosen_total { sql: ${TABLE}.ev_liquid_chosen_total ;; }
  dimension: ev_liquid_chosen_label { sql: ${TABLE}.ev_liquid_chosen_label ;; }
}

view: chk_tile_liquid_set {
  derived_table: {
    sql:
SELECT
    ev_liquid.customer AS ev_liquid_customer,
    COUNT(*) AS ev_liquid_count,
    COALESCE(SUM(ev_liquid.shipping), 0) AS ev_liquid_chosen_total,
    ('shipping') AS ev_liquid_chosen_label
FROM (SELECT customer, shipping, amount FROM (
        SELECT 'acme' AS customer, 10.0 AS shipping, 100.0 AS amount UNION ALL
        SELECT 'acme', 20.0, 50.0 UNION ALL
        SELECT 'bolt', 5.0, 70.0 UNION ALL
        SELECT CAST(NULL AS STRING), 3.0, 30.0
      )
      WHERE ((customer <> 'acme' OR customer IS NULL))) AS ev_liquid
GROUP BY 1
ORDER BY ev_liquid_customer ;;
  }
  dimension: ev_liquid_customer { sql: ${TABLE}.ev_liquid_customer ;; }
  dimension: ev_liquid_count { sql: ${TABLE}.ev_liquid_count ;; }
  dimension: ev_liquid_chosen_total { sql: ${TABLE}.ev_liquid_chosen_total ;; }
  dimension: ev_liquid_chosen_label { sql: ${TABLE}.ev_liquid_chosen_label ;; }
}

view: chk_tile_nested_by_id {
  derived_table: {
    sql:
WITH ev_nested AS (SELECT 1 AS id, [STRUCT('x' AS sku, 2 AS qty), STRUCT('y' AS sku, 3 AS qty)] AS lines UNION ALL
      SELECT 2, ARRAY<STRUCT<sku STRING, qty INT64>>[] UNION ALL
      SELECT 3, [STRUCT('x' AS sku, 1 AS qty)]), ev_nested_lines AS (SELECT ev_nested.id AS ev_nested_key, ev_nested_lines.* FROM ev_nested AS ev_nested LEFT JOIN UNNEST(ev_nested.lines) AS ev_nested_lines)
SELECT
    ev_nested.id AS ev_nested_id,
    COUNT(DISTINCT ev_nested.id) AS ev_nested_count,
    COUNT(*) AS ev_nested_lines_count,
    COALESCE(SUM(ev_nested_lines.qty), 0) AS ev_nested_lines_total_qty
FROM ev_nested AS ev_nested
LEFT JOIN ev_nested_lines AS ev_nested_lines ON ev_nested.id = ev_nested_lines.ev_nested_key
GROUP BY 1
ORDER BY ev_nested_id ;;
  }
  dimension: ev_nested_id { sql: ${TABLE}.ev_nested_id ;; }
  dimension: ev_nested_count { sql: ${TABLE}.ev_nested_count ;; }
  dimension: ev_nested_lines_count { sql: ${TABLE}.ev_nested_lines_count ;; }
  dimension: ev_nested_lines_total_qty { sql: ${TABLE}.ev_nested_lines_total_qty ;; }
}

view: chk_tile_nested_by_sku {
  derived_table: {
    sql:
WITH ev_nested AS (SELECT 1 AS id, [STRUCT('x' AS sku, 2 AS qty), STRUCT('y' AS sku, 3 AS qty)] AS lines UNION ALL
      SELECT 2, ARRAY<STRUCT<sku STRING, qty INT64>>[] UNION ALL
      SELECT 3, [STRUCT('x' AS sku, 1 AS qty)]), ev_nested_lines AS (SELECT ev_nested.id AS ev_nested_key, ev_nested_lines.* FROM ev_nested AS ev_nested LEFT JOIN UNNEST(ev_nested.lines) AS ev_nested_lines)
SELECT
    ev_nested_lines.sku AS ev_nested_lines_sku,
    COUNT(DISTINCT ev_nested.id) AS ev_nested_count,
    COALESCE(SUM(ev_nested_lines.qty), 0) AS ev_nested_lines_total_qty
FROM ev_nested AS ev_nested
LEFT JOIN ev_nested_lines AS ev_nested_lines ON ev_nested.id = ev_nested_lines.ev_nested_key
GROUP BY 1
ORDER BY ev_nested_lines_sku ;;
  }
  dimension: ev_nested_lines_sku { sql: ${TABLE}.ev_nested_lines_sku ;; }
  dimension: ev_nested_count { sql: ${TABLE}.ev_nested_count ;; }
  dimension: ev_nested_lines_total_qty { sql: ${TABLE}.ev_nested_lines_total_qty ;; }
}

view: chk_tile_nested_total {
  derived_table: {
    sql:
WITH ev_nested AS (SELECT 1 AS id, [STRUCT('x' AS sku, 2 AS qty), STRUCT('y' AS sku, 3 AS qty)] AS lines UNION ALL
      SELECT 2, ARRAY<STRUCT<sku STRING, qty INT64>>[] UNION ALL
      SELECT 3, [STRUCT('x' AS sku, 1 AS qty)]), ev_nested_lines AS (SELECT ev_nested.id AS ev_nested_key, ev_nested_lines.* FROM ev_nested AS ev_nested LEFT JOIN UNNEST(ev_nested.lines) AS ev_nested_lines)
SELECT
    COUNT(DISTINCT ev_nested.id) AS ev_nested_count,
    COUNT(*) AS ev_nested_lines_count,
    COALESCE(SUM(ev_nested_lines.qty), 0) AS ev_nested_lines_total_qty
FROM ev_nested AS ev_nested
LEFT JOIN ev_nested_lines AS ev_nested_lines ON ev_nested.id = ev_nested_lines.ev_nested_key
ORDER BY ev_nested_count DESC ;;
  }
  dimension: ev_nested_count { sql: ${TABLE}.ev_nested_count ;; }
  dimension: ev_nested_lines_count { sql: ${TABLE}.ev_nested_lines_count ;; }
  dimension: ev_nested_lines_total_qty { sql: ${TABLE}.ev_nested_lines_total_qty ;; }
}

view: chk_time_monday {
  derived_table: {
    sql:
WITH ev_time AS (SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
      SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
      SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
      SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
      SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
      SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE))
SELECT id, CAST(ts AS DATE) AS created_date, FORMAT_DATE('%F', DATE_TRUNC(CAST(ts AS DATE), WEEK(MONDAY))) AS created_week, FORMAT_DATE('%Y-%m', CAST(ts AS DATE)) AS created_month, FORMAT_DATE('%Y-%m', DATE_TRUNC(CAST(ts AS DATE), QUARTER)) AS created_quarter, EXTRACT(YEAR FROM ts) AS created_year, CASE MOD(EXTRACT(DAYOFWEEK FROM CAST(ts AS DATE)) + 5, 7) WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday' WHEN 3 THEN 'Thursday' WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday' WHEN 6 THEN 'Sunday' END AS created_day_of_week, MOD(EXTRACT(DAYOFWEEK FROM CAST(ts AS DATE)) + 5, 7) AS created_day_of_week_index, EXTRACT(MONTH FROM ts) AS created_month_num, CASE EXTRACT(MONTH FROM ts) WHEN 1 THEN 'January' WHEN 2 THEN 'February' WHEN 3 THEN 'March' WHEN 4 THEN 'April' WHEN 5 THEN 'May' WHEN 6 THEN 'June' WHEN 7 THEN 'July' WHEN 8 THEN 'August' WHEN 9 THEN 'September' WHEN 10 THEN 'October' WHEN 11 THEN 'November' WHEN 12 THEN 'December' END AS created_month_name, 'Q' || CAST(EXTRACT(QUARTER FROM CAST(ts AS DATE)) AS STRING) AS created_quarter_of_year, EXTRACT(ISOWEEK FROM CAST(ts AS DATE)) AS created_week_of_year, EXTRACT(DAY FROM ts) AS created_day_of_month, EXTRACT(DAYOFYEAR FROM CAST(ts AS DATE)) AS created_day_of_year, EXTRACT(HOUR FROM ts) AS created_hour_of_day FROM ev_time ;;
  }
  dimension: id { sql: ${TABLE}.id ;; }
  dimension: created_date { sql: ${TABLE}.created_date ;; }
  dimension: created_week { sql: ${TABLE}.created_week ;; }
  dimension: created_month { sql: ${TABLE}.created_month ;; }
  dimension: created_quarter { sql: ${TABLE}.created_quarter ;; }
  dimension: created_year { sql: ${TABLE}.created_year ;; }
  dimension: created_day_of_week { sql: ${TABLE}.created_day_of_week ;; }
  dimension: created_day_of_week_index { sql: ${TABLE}.created_day_of_week_index ;; }
  dimension: created_month_num { sql: ${TABLE}.created_month_num ;; }
  dimension: created_month_name { sql: ${TABLE}.created_month_name ;; }
  dimension: created_quarter_of_year { sql: ${TABLE}.created_quarter_of_year ;; }
  dimension: created_week_of_year { sql: ${TABLE}.created_week_of_year ;; }
  dimension: created_day_of_month { sql: ${TABLE}.created_day_of_month ;; }
  dimension: created_day_of_year { sql: ${TABLE}.created_day_of_year ;; }
  dimension: created_hour_of_day { sql: ${TABLE}.created_hour_of_day ;; }
}

view: chk_time_sunday {
  derived_table: {
    sql:
WITH ev_time AS (SELECT 1 AS id, TIMESTAMP '2026-12-31 23:30:00' AS ts, DATE '2026-12-31' AS d UNION ALL
      SELECT 2, TIMESTAMP '2027-01-03 10:00:00', DATE '2027-01-03' UNION ALL
      SELECT 3, TIMESTAMP '2027-01-04 00:15:00', DATE '2027-01-04' UNION ALL
      SELECT 4, TIMESTAMP '2026-03-01 12:00:00', DATE '2026-03-01' UNION ALL
      SELECT 5, TIMESTAMP '2026-04-01 00:00:00', DATE '2026-04-01' UNION ALL
      SELECT 6, CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE))
SELECT id, CAST(ts AS DATE) AS created_date, FORMAT_DATE('%F', DATE_TRUNC(CAST(ts AS DATE), WEEK(SUNDAY))) AS created_week, FORMAT_DATE('%Y-%m', CAST(ts AS DATE)) AS created_month, FORMAT_DATE('%Y-%m', DATE_TRUNC(CAST(ts AS DATE), QUARTER)) AS created_quarter, EXTRACT(YEAR FROM ts) AS created_year, CASE MOD(EXTRACT(DAYOFWEEK FROM CAST(ts AS DATE)) + 5, 7) WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday' WHEN 3 THEN 'Thursday' WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday' WHEN 6 THEN 'Sunday' END AS created_day_of_week, MOD(MOD(EXTRACT(DAYOFWEEK FROM CAST(ts AS DATE)) + 5, 7) + 1, 7) AS created_day_of_week_index, EXTRACT(MONTH FROM ts) AS created_month_num, CASE EXTRACT(MONTH FROM ts) WHEN 1 THEN 'January' WHEN 2 THEN 'February' WHEN 3 THEN 'March' WHEN 4 THEN 'April' WHEN 5 THEN 'May' WHEN 6 THEN 'June' WHEN 7 THEN 'July' WHEN 8 THEN 'August' WHEN 9 THEN 'September' WHEN 10 THEN 'October' WHEN 11 THEN 'November' WHEN 12 THEN 'December' END AS created_month_name, 'Q' || CAST(EXTRACT(QUARTER FROM CAST(ts AS DATE)) AS STRING) AS created_quarter_of_year, EXTRACT(ISOWEEK FROM CAST(ts AS DATE)) AS created_week_of_year, EXTRACT(DAY FROM ts) AS created_day_of_month, EXTRACT(DAYOFYEAR FROM CAST(ts AS DATE)) AS created_day_of_year, EXTRACT(HOUR FROM ts) AS created_hour_of_day FROM ev_time ;;
  }
  dimension: id { sql: ${TABLE}.id ;; }
  dimension: created_date { sql: ${TABLE}.created_date ;; }
  dimension: created_week { sql: ${TABLE}.created_week ;; }
  dimension: created_month { sql: ${TABLE}.created_month ;; }
  dimension: created_quarter { sql: ${TABLE}.created_quarter ;; }
  dimension: created_year { sql: ${TABLE}.created_year ;; }
  dimension: created_day_of_week { sql: ${TABLE}.created_day_of_week ;; }
  dimension: created_day_of_week_index { sql: ${TABLE}.created_day_of_week_index ;; }
  dimension: created_month_num { sql: ${TABLE}.created_month_num ;; }
  dimension: created_month_name { sql: ${TABLE}.created_month_name ;; }
  dimension: created_quarter_of_year { sql: ${TABLE}.created_quarter_of_year ;; }
  dimension: created_week_of_year { sql: ${TABLE}.created_week_of_year ;; }
  dimension: created_day_of_month { sql: ${TABLE}.created_day_of_month ;; }
  dimension: created_day_of_year { sql: ${TABLE}.created_day_of_year ;; }
  dimension: created_hour_of_day { sql: ${TABLE}.created_hour_of_day ;; }
}

view: chk_filters {
  derived_table: {
    sql:
WITH ev_f AS (SELECT 1 AS id, 'apple' AS s, 1.0 AS n, DATE '2026-12-31' AS d, TIMESTAMP '2026-12-31 23:30:00' AS ts, TRUE AS y UNION ALL
      SELECT 2, 'Apple', 5.0, DATE '2027-01-03', TIMESTAMP '2027-01-03 10:00:00', FALSE UNION ALL
      SELECT 3, 'banana', 7.0, DATE '2026-03-01', TIMESTAMP '2026-03-01 12:00:00', CAST(NULL AS BOOL) UNION ALL
      SELECT 4, '', -5.0, DATE '2026-04-01', TIMESTAMP '2026-04-01 00:00:00', TRUE UNION ALL
      SELECT 5, 'a,b', 10.0, DATE '2026-12-01', TIMESTAMP '2026-12-01 08:00:00', FALSE UNION ALL
      SELECT 6, 'pineapple', 2.5, DATE '2027-01-04', TIMESTAMP '2027-01-04 00:15:00', FALSE UNION ALL
      SELECT 7, CAST(NULL AS STRING), CAST(NULL AS FLOAT64), CAST(NULL AS DATE), CAST(NULL AS TIMESTAMP), CAST(NULL AS BOOL))
SELECT COUNTIF((s) LIKE 'app%') AS s_starts, COUNTIF((s) LIKE '%ple') AS s_ends, COUNTIF((s) LIKE '%an%') AS s_contains, COUNTIF(((s) NOT LIKE 'app%' OR (s) IS NULL)) AS s_not_starts, COUNTIF((s) = 'apple') AS s_eq_lower, COUNTIF((s) = 'APPLE') AS s_eq_upper, COUNTIF((s) IN ('apple', 'banana')) AS s_list, COUNTIF((s) = 'a,b') AS s_escaped_comma, COUNTIF(((s) IS NULL OR (s) = '')) AS s_empty, COUNTIF((s) IS NULL) AS s_null, COUNTIF(((n) >= 1 AND (n) <= 5)) AS n_closed, COUNTIF(((n) > 1 AND (n) < 5)) AS n_open, COUNTIF(((n) >= 1 AND (n) < 5)) AS n_half_open, COUNTIF((n) BETWEEN 1 AND 5) AS n_to, COUNTIF(((n) >= 1 AND (n) <= 5)) AS n_and, COUNTIF(((n) > 5 OR (n) < 2)) AS n_or, COUNTIF(NOT ((n) = 5)) AS n_not, COUNTIF((n) = -5) AS n_minus, COUNTIF((n) IN (5, 7)) AS n_list, COUNTIF(NOT ((n) IN (5, 7))) AS n_not_list, COUNTIF((n) IS NULL) AS n_null, COUNTIF((n) IS NOT NULL) AS n_not_null, COUNTIF((n) > 5) AS n_gt, COUNTIF(((d) >= '2026-12-31' AND (d) < '2027-01-01')) AS d_day, COUNTIF(((d) >= '2026-12-01' AND (d) < '2027-01-01')) AS d_month, COUNTIF(((d) >= '2026-01-01' AND (d) < '2027-01-01')) AS d_year, COUNTIF((d) < '2027-01-01') AS d_before, COUNTIF((d) >= '2027-01-01') AS d_after, COUNTIF(((d) >= '2026-12-01' AND (d) < '2027-01-01')) AS d_range, COUNTIF((d) IS NULL) AS d_null, COUNTIF((d) IS NOT NULL) AS d_not_null, COUNTIF(((ts) >= '2026-12-31 00:00:00' AND (ts) < '2027-01-01 00:00:00')) AS t_day, COUNTIF((ts) < '2027-01-03 10:00:00') AS t_before, COUNTIF(((ts) >= '2026-12-01 00:00:00' AND (ts) < '2027-01-01 00:00:00')) AS t_month, COUNTIF((CASE WHEN n < 0 THEN 'Below 0' WHEN n >= 0 AND n < 5 THEN '0 to 4' WHEN n >= 5 AND n < 10 THEN '5 to 9' WHEN n >= 10 THEN '10 or Above' ELSE 'Undefined' END) = '5 to 9') AS tier_filtered, COUNTIF((n > 3) = true) AS n_sum_big FROM ev_f ;;
  }
  dimension: s_starts { sql: ${TABLE}.s_starts ;; }
  dimension: s_ends { sql: ${TABLE}.s_ends ;; }
  dimension: s_contains { sql: ${TABLE}.s_contains ;; }
  dimension: s_not_starts { sql: ${TABLE}.s_not_starts ;; }
  dimension: s_eq_lower { sql: ${TABLE}.s_eq_lower ;; }
  dimension: s_eq_upper { sql: ${TABLE}.s_eq_upper ;; }
  dimension: s_list { sql: ${TABLE}.s_list ;; }
  dimension: s_escaped_comma { sql: ${TABLE}.s_escaped_comma ;; }
  dimension: s_empty { sql: ${TABLE}.s_empty ;; }
  dimension: s_null { sql: ${TABLE}.s_null ;; }
  dimension: n_closed { sql: ${TABLE}.n_closed ;; }
  dimension: n_open { sql: ${TABLE}.n_open ;; }
  dimension: n_half_open { sql: ${TABLE}.n_half_open ;; }
  dimension: n_to { sql: ${TABLE}.n_to ;; }
  dimension: n_and { sql: ${TABLE}.n_and ;; }
  dimension: n_or { sql: ${TABLE}.n_or ;; }
  dimension: n_not { sql: ${TABLE}.n_not ;; }
  dimension: n_minus { sql: ${TABLE}.n_minus ;; }
  dimension: n_list { sql: ${TABLE}.n_list ;; }
  dimension: n_not_list { sql: ${TABLE}.n_not_list ;; }
  dimension: n_null { sql: ${TABLE}.n_null ;; }
  dimension: n_not_null { sql: ${TABLE}.n_not_null ;; }
  dimension: n_gt { sql: ${TABLE}.n_gt ;; }
  dimension: d_day { sql: ${TABLE}.d_day ;; }
  dimension: d_month { sql: ${TABLE}.d_month ;; }
  dimension: d_year { sql: ${TABLE}.d_year ;; }
  dimension: d_before { sql: ${TABLE}.d_before ;; }
  dimension: d_after { sql: ${TABLE}.d_after ;; }
  dimension: d_range { sql: ${TABLE}.d_range ;; }
  dimension: d_null { sql: ${TABLE}.d_null ;; }
  dimension: d_not_null { sql: ${TABLE}.d_not_null ;; }
  dimension: t_day { sql: ${TABLE}.t_day ;; }
  dimension: t_before { sql: ${TABLE}.t_before ;; }
  dimension: t_month { sql: ${TABLE}.t_month ;; }
  dimension: tier_filtered { sql: ${TABLE}.tier_filtered ;; }
  dimension: n_sum_big { sql: ${TABLE}.n_sum_big ;; }
}

view: chk_ossie_ev_f {
  derived_table: {
    sql:
WITH ev_f AS (SELECT 1 AS id, 'apple' AS s, 1.0 AS n, DATE '2026-12-31' AS d, TIMESTAMP '2026-12-31 23:30:00' AS ts, TRUE AS y UNION ALL
      SELECT 2, 'Apple', 5.0, DATE '2027-01-03', TIMESTAMP '2027-01-03 10:00:00', FALSE UNION ALL
      SELECT 3, 'banana', 7.0, DATE '2026-03-01', TIMESTAMP '2026-03-01 12:00:00', CAST(NULL AS BOOL) UNION ALL
      SELECT 4, '', -5.0, DATE '2026-04-01', TIMESTAMP '2026-04-01 00:00:00', TRUE UNION ALL
      SELECT 5, 'a,b', 10.0, DATE '2026-12-01', TIMESTAMP '2026-12-01 08:00:00', FALSE UNION ALL
      SELECT 6, 'pineapple', 2.5, DATE '2027-01-04', TIMESTAMP '2027-01-04 00:15:00', FALSE UNION ALL
      SELECT 7, CAST(NULL AS STRING), CAST(NULL AS FLOAT64), CAST(NULL AS DATE), CAST(NULL AS TIMESTAMP), CAST(NULL AS BOOL)),
fields AS (SELECT s AS `s`, n AS `n`, y AS `y`, n > 3 AS `n_big`, CASE WHEN n < 0 THEN 'Below 0' WHEN n >= 0 AND n < 5 THEN '0 to 4' WHEN n >= 5 AND n < 10 THEN '5 to 9' WHEN n >= 10 THEN '10 or Above' ELSE 'Undefined' END AS `n_tier`, CASE WHEN n < 0.0 THEN 'T00 (-inf,0.0)' WHEN n >= 0.0 AND n < 5.0 THEN 'T01 [0.0,5.0)' WHEN n >= 5.0 AND n < 10.0 THEN 'T02 [5.0,10.0)' WHEN n >= 10.0 THEN 'T03 [10.0,inf)' ELSE 'TXX Undefined' END AS `n_tier_classic`, CASE WHEN s = 'apple' THEN 'Fruit A' WHEN n > 5 THEN 'Big' ELSE 'Other' END AS `s_case`, ts AS `at`, d AS `on`, id AS `id` FROM ev_f)
SELECT COUNT(id) AS ev_f_count, COUNT(CASE WHEN s LIKE 'app%' THEN id END) AS s_starts, COUNT(CASE WHEN s LIKE '%ple' THEN id END) AS s_ends, COUNT(CASE WHEN s LIKE '%an%' THEN id END) AS s_contains, COUNT(CASE WHEN (s NOT LIKE 'app%' OR s IS NULL) THEN id END) AS s_not_starts, COUNT(CASE WHEN s = 'apple' THEN id END) AS s_eq_lower, COUNT(CASE WHEN s = 'APPLE' THEN id END) AS s_eq_upper, COUNT(CASE WHEN s IN ('apple', 'banana') THEN id END) AS s_list, COUNT(CASE WHEN s = 'a,b' THEN id END) AS s_escaped_comma, COUNT(CASE WHEN (s IS NULL OR s = '') THEN id END) AS s_empty, COUNT(CASE WHEN s IS NULL THEN id END) AS s_null, COUNT(CASE WHEN (n >= 1 AND n <= 5) THEN id END) AS n_closed, COUNT(CASE WHEN (n > 1 AND n < 5) THEN id END) AS n_open, COUNT(CASE WHEN (n >= 1 AND n < 5) THEN id END) AS n_half_open, COUNT(CASE WHEN n BETWEEN 1 AND 5 THEN id END) AS n_to, COUNT(CASE WHEN (n >= 1 AND n <= 5) THEN id END) AS n_and, COUNT(CASE WHEN (n > 5 OR n < 2) THEN id END) AS n_or, COUNT(CASE WHEN NOT (n = 5) THEN id END) AS n_not, COUNT(CASE WHEN n = -5 THEN id END) AS n_minus, COUNT(CASE WHEN n IN (5, 7) THEN id END) AS n_list, COUNT(CASE WHEN NOT (n IN (5, 7)) THEN id END) AS n_not_list, COUNT(CASE WHEN n IS NULL THEN id END) AS n_null, COUNT(CASE WHEN n IS NOT NULL THEN id END) AS n_not_null, COUNT(CASE WHEN n > 5 THEN id END) AS n_gt, COUNT(CASE WHEN (TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) >= '2026-12-31' AND TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) < '2027-01-01') THEN id END) AS d_day, COUNT(CASE WHEN (TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) >= '2026-12-01' AND TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) < '2027-01-01') THEN id END) AS d_month, COUNT(CASE WHEN (TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) >= '2026-01-01' AND TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) < '2027-01-01') THEN id END) AS d_year, COUNT(CASE WHEN TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) < '2027-01-01' THEN id END) AS d_before, COUNT(CASE WHEN TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) >= '2027-01-01' THEN id END) AS d_after, COUNT(CASE WHEN (TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) >= '2026-12-01' AND TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) < '2027-01-01') THEN id END) AS d_range, COUNT(CASE WHEN TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) IS NULL THEN id END) AS d_null, COUNT(CASE WHEN TIMESTAMP_TRUNC(CAST(`on` AS TIMESTAMP), DAY) IS NOT NULL THEN id END) AS d_not_null, COUNT(CASE WHEN (TIMESTAMP_TRUNC(CAST(`at` AS TIMESTAMP), SECOND) >= '2026-12-31 00:00:00' AND TIMESTAMP_TRUNC(CAST(`at` AS TIMESTAMP), SECOND) < '2027-01-01 00:00:00') THEN id END) AS t_day, COUNT(CASE WHEN TIMESTAMP_TRUNC(CAST(`at` AS TIMESTAMP), SECOND) < '2027-01-03 10:00:00' THEN id END) AS t_before, COUNT(CASE WHEN (TIMESTAMP_TRUNC(CAST(`at` AS TIMESTAMP), SECOND) >= '2026-12-01 00:00:00' AND TIMESTAMP_TRUNC(CAST(`at` AS TIMESTAMP), SECOND) < '2027-01-01 00:00:00') THEN id END) AS t_month, COUNT(CASE WHEN n_tier = '5 to 9' THEN id END) AS tier_filtered, CASE WHEN COUNT(n) <= 10000 THEN (ARRAY_AGG(n IGNORE NULLS ORDER BY n LIMIT 10000)[OFFSET(CAST(FLOOR(COUNT(n) * 0.5 - 0.0000001) AS INT64))] + ARRAY_AGG(n IGNORE NULLS ORDER BY n LIMIT 10000)[OFFSET(CAST(FLOOR(COUNT(n) * 0.5) AS INT64))]) / 2 ELSE APPROX_QUANTILES(n, 1000)[OFFSET(500)] END AS n_median, COUNT(DISTINCT s) AS s_distinct, COALESCE(SUM(n), 0) AS n_sum, AVG(n) AS n_avg, MIN(n) AS n_min, MAX(`on`) AS n_max_date, 1.0 * COALESCE(SUM(n), 0) / nullif(COUNT(id), 0) AS n_per_row, COALESCE(SUM(CASE WHEN n_big = true THEN n END), 0) AS n_sum_big FROM fields ;;
  }
  dimension: ev_f_count { sql: ${TABLE}.ev_f_count ;; }
  dimension: s_starts { sql: ${TABLE}.s_starts ;; }
  dimension: s_ends { sql: ${TABLE}.s_ends ;; }
  dimension: s_contains { sql: ${TABLE}.s_contains ;; }
  dimension: s_not_starts { sql: ${TABLE}.s_not_starts ;; }
  dimension: s_eq_lower { sql: ${TABLE}.s_eq_lower ;; }
  dimension: s_eq_upper { sql: ${TABLE}.s_eq_upper ;; }
  dimension: s_list { sql: ${TABLE}.s_list ;; }
  dimension: s_escaped_comma { sql: ${TABLE}.s_escaped_comma ;; }
  dimension: s_empty { sql: ${TABLE}.s_empty ;; }
  dimension: s_null { sql: ${TABLE}.s_null ;; }
  dimension: n_closed { sql: ${TABLE}.n_closed ;; }
  dimension: n_open { sql: ${TABLE}.n_open ;; }
  dimension: n_half_open { sql: ${TABLE}.n_half_open ;; }
  dimension: n_to { sql: ${TABLE}.n_to ;; }
  dimension: n_and { sql: ${TABLE}.n_and ;; }
  dimension: n_or { sql: ${TABLE}.n_or ;; }
  dimension: n_not { sql: ${TABLE}.n_not ;; }
  dimension: n_minus { sql: ${TABLE}.n_minus ;; }
  dimension: n_list { sql: ${TABLE}.n_list ;; }
  dimension: n_not_list { sql: ${TABLE}.n_not_list ;; }
  dimension: n_null { sql: ${TABLE}.n_null ;; }
  dimension: n_not_null { sql: ${TABLE}.n_not_null ;; }
  dimension: n_gt { sql: ${TABLE}.n_gt ;; }
  dimension: d_day { sql: ${TABLE}.d_day ;; }
  dimension: d_month { sql: ${TABLE}.d_month ;; }
  dimension: d_year { sql: ${TABLE}.d_year ;; }
  dimension: d_before { sql: ${TABLE}.d_before ;; }
  dimension: d_after { sql: ${TABLE}.d_after ;; }
  dimension: d_range { sql: ${TABLE}.d_range ;; }
  dimension: d_null { sql: ${TABLE}.d_null ;; }
  dimension: d_not_null { sql: ${TABLE}.d_not_null ;; }
  dimension: t_day { sql: ${TABLE}.t_day ;; }
  dimension: t_before { sql: ${TABLE}.t_before ;; }
  dimension: t_month { sql: ${TABLE}.t_month ;; }
  dimension: tier_filtered { sql: ${TABLE}.tier_filtered ;; }
  dimension: n_median { sql: ${TABLE}.n_median ;; }
  dimension: s_distinct { sql: ${TABLE}.s_distinct ;; }
  dimension: n_sum { sql: ${TABLE}.n_sum ;; }
  dimension: n_avg { sql: ${TABLE}.n_avg ;; }
  dimension: n_min { sql: ${TABLE}.n_min ;; }
  dimension: n_max_date { sql: ${TABLE}.n_max_date ;; }
  dimension: n_per_row { sql: ${TABLE}.n_per_row ;; }
  dimension: n_sum_big { sql: ${TABLE}.n_sum_big ;; }
}
