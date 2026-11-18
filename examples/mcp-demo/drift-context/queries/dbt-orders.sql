-- name: dbt orders
-- description: Reference model SQL; importing does not execute or verify it.
-- datasource: warehouse
-- source: dbt:models/orders.sql
SELECT order_id, customer_id, ordered_at, amount, status FROM public.orders
