# Dunder Mifflin

Fictional data for a fictional paper company, used by the offline tests to run generated SQL
(DuckDB reads the Parquet files directly).
Names, addresses, emails, orders and reviews are all made up.

| Table | Rows | Key |
| --- | ---: | --- |
| `accounts` | 181 | `account_id` |
| `products` | 15 | `product_id` |
| `orders` | 3,503 | `order_id` |
| `shippings` | 2,812 | `shipping_id` |
| `reviews` | 1,655 | `review_id` |

`orders` joins `accounts` on `account_id`, `products` on `product_id` and `shippings`
on `shipping_id` (some orders have no shipping row); `reviews` joins `orders` on
`order_id`. Orders run from January 2024 to October 2026.

Licensed under the repository's Apache 2.0 license.
