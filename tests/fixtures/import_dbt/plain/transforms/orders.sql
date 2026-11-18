select order_id, ordered_at, amount from {{ ref('stg_orders') }}
