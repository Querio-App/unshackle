-- Synthetic Dunder Mifflin paper company: accounts, products, orders, shippings, reviews.
-- Deterministic (setseed), small, Postgres only. Loaded by docker-compose.yml on first start.
SELECT setseed(0.42);

CREATE TABLE accounts (
  id integer PRIMARY KEY,
  name text NOT NULL,
  industry text NOT NULL,
  business_size integer NOT NULL,
  payment_cycle text NOT NULL,
  account_manager text NOT NULL,
  created_at timestamp NOT NULL
);

CREATE TABLE products (
  id integer PRIMARY KEY,
  name text NOT NULL,
  paper_type text NOT NULL,
  price numeric(10, 2) NOT NULL
);

CREATE TABLE orders (
  id integer PRIMARY KEY,
  account_id integer NOT NULL REFERENCES accounts (id),
  product_id integer NOT NULL REFERENCES products (id),
  status text NOT NULL,
  quantity integer NOT NULL,
  pre_discount_revenue numeric(12, 2) NOT NULL,
  discount_percentage numeric(5, 2) NOT NULL,
  post_discount_revenue numeric(12, 2) NOT NULL,
  purchased_at timestamp NOT NULL,
  cancelled_at timestamp,
  returned_at timestamp
);

CREATE TABLE shippings (
  id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES orders (id),
  mode text NOT NULL,
  region text NOT NULL,
  days_to_ship integer NOT NULL,
  shipped_at timestamp NOT NULL
);

CREATE TABLE reviews (
  id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES orders (id),
  rating integer NOT NULL,
  reviewed_at timestamp NOT NULL
);

INSERT INTO accounts
SELECT
  i,
  (ARRAY['Blue Cross of Pennsylvania', 'Lackawanna County', 'Scranton White Pages', 'Stamford Medical', 'Harper Collins',
         'Nashua Office Supply', 'Utica Paper Co', 'Akron Sports', 'Binghamton Bank', 'Yonkers Law'])[1 + (i - 1) % 10]
    || CASE WHEN i > 10 THEN ' ' || ((i - 1) / 10 + 1)::text ELSE '' END,
  (ARRAY['Healthcare', 'Government', 'Media', 'Finance', 'Legal', 'Retail'])[1 + floor(random() * 6)::int],
  15 + floor(random() * 4600)::int,
  CASE WHEN r < 0.34 THEN 'ANNUAL' WHEN r < 0.59 THEN 'BIWEEK' WHEN r < 0.84 THEN 'WEEK' ELSE 'MONTH' END,
  CASE WHEN m < 0.22 THEN 'Dwight Schrute'
       ELSE (ARRAY['Jim Halpert', 'Phil Leonard', 'Stanley Hudson', 'Andy Bernard', 'Karen Filippelli'])[1 + floor(random() * 5)::int] END,
  timestamp '2022-01-01' + (i * interval '9 days')
FROM (SELECT i, random() AS r, random() AS m FROM generate_series(1, 40) AS i) s;

INSERT INTO products
SELECT
  i,
  t || ' ' || (ARRAY['Letter', 'Legal', 'A4', 'Tabloid', 'Premium'])[1 + (i - 1) % 5],
  t,
  round((8 + random() * 60)::numeric, 2)
FROM (
  SELECT i, (ARRAY['Multipurpose', 'Printer', 'Cardstock', 'Kraft Roll', 'Copy'])[1 + (i - 1) / 5] AS t
  FROM generate_series(1, 25) AS i
) s;

INSERT INTO orders
SELECT
  i,
  1 + floor(random() * 40)::int,
  1 + floor(random() * 25)::int,
  CASE WHEN s < 0.84 THEN 'COMPLETED' WHEN s < 0.94 THEN 'CANCELLED' ELSE 'RETURNED' END,
  q,
  0, d, 0,
  timestamp '2024-01-01' + (i * interval '11 hours') + (floor(random() * 600) * interval '1 minute'),
  NULL, NULL
FROM (
  SELECT i, random() AS s, greatest(9, least(228, round(110 + 35 * (random() + random() + random() - 1.5) * 2)))::int AS q,
         round((0.35 + random() * random() * 30)::numeric, 2) AS d
  FROM generate_series(1, 1500) AS i
) s;

UPDATE orders o SET
  pre_discount_revenue = round(o.quantity * p.price, 2),
  post_discount_revenue = round(o.quantity * p.price * (1 - o.discount_percentage / 100), 2),
  cancelled_at = CASE WHEN o.status = 'CANCELLED' THEN o.purchased_at + interval '66 days' END,
  returned_at = CASE WHEN o.status = 'RETURNED' THEN o.purchased_at + interval '58 days' END
FROM products p WHERE p.id = o.product_id;

INSERT INTO shippings
SELECT
  o.id, o.id,
  CASE WHEN random() < 0.51 THEN 'EXPRESS' ELSE 'REGULAR' END,
  CASE WHEN r < 0.34 THEN 'West' WHEN r < 0.65 THEN 'South' WHEN r < 0.87 THEN 'Northeast' ELSE 'Midwest' END,
  dts,
  o.purchased_at + dts * interval '1 day'
FROM (SELECT id, purchased_at, random() AS r, 3 + floor(random() * 5)::int AS dts FROM orders ORDER BY id) o;

INSERT INTO reviews
SELECT
  row_number() OVER (ORDER BY o.id), o.id,
  CASE WHEN r < 0.29 THEN 5 WHEN r < 0.61 THEN 4 WHEN r < 0.92 THEN 3 WHEN r < 0.97 THEN 2 ELSE 1 END,
  o.purchased_at + (7 + floor(random() * 24)::int) * interval '1 day'
FROM (SELECT id, purchased_at, random() AS keep, random() AS r FROM orders WHERE status = 'COMPLETED' ORDER BY id) o
WHERE o.keep < 0.67;
