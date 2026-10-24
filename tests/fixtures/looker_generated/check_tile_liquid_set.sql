WITH chk_tile_liquid_set AS (SELECT
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
    ORDER BY ev_liquid_customer )
SELECT
    chk_tile_liquid_set.ev_liquid_customer  AS chk_tile_liquid_set_ev_liquid_customer,
    chk_tile_liquid_set.ev_liquid_count  AS chk_tile_liquid_set_ev_liquid_count,
    chk_tile_liquid_set.ev_liquid_chosen_total  AS chk_tile_liquid_set_ev_liquid_chosen_total,
    chk_tile_liquid_set.ev_liquid_chosen_label  AS chk_tile_liquid_set_ev_liquid_chosen_label
FROM chk_tile_liquid_set
GROUP BY
    1,
    2,
    3,
    4
ORDER BY
    1
LIMIT 500
