-- name: dbt customers
-- description: Reference model SQL; importing does not execute or verify it.
-- datasource: warehouse
-- source: dbt:models/customers.sql
SELECT customer_id, customer_name, region FROM public.customers
