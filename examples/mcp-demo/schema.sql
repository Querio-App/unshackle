-- Fictional dataset only. Destructive setup belongs in a fresh demo database.
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    region TEXT NOT NULL
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    ordered_at DATE NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('paid', 'refunded', 'cancelled'))
);
COMMENT ON TABLE orders IS 'One row per order. Recognized revenue includes only status = paid; refunded and cancelled orders are excluded.';
COMMENT ON COLUMN orders.amount IS 'Order amount in USD; include only paid orders when calculating recognized revenue.';
COMMENT ON COLUMN customers.region IS 'Customer sales region, not order destination.';
INSERT INTO customers VALUES
    (1, 'Acme Paper', 'North'), (2, 'Beacon Office', 'South'), (3, 'Cedar Supplies', 'North');
INSERT INTO orders VALUES
    (101, 1, '2025-01-01', 100.00, 'paid'),
    (102, 1, '2025-01-02',  50.00, 'paid'),
    (103, 2, '2025-01-02',  80.00, 'paid'),
    (104, 2, '2025-02-01',  40.00, 'refunded'),
    (105, 3, '2025-02-03', 120.00, 'paid'),
    (106, 3, '2025-02-05',  30.00, 'cancelled');
