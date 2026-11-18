-- Recognized revenue excludes refunded and cancelled orders.
SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid';
