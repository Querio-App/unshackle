# Synthetic static extraction fixture. This file must never execute.
raise RuntimeError("imported Python executed")

literal = "SELECT count(*) AS orders FROM orders"
concatenated = "SELECT customer_id, " + "amount FROM orders"
adjacent = ("SELECT id " "FROM orders")
dynamic = f"SELECT * FROM {table_name}"
formatted = "SELECT * FROM {}".format(table_name)
percent = "SELECT * FROM %s" % table_name
cursor.execute(query_from_elsewhere)
cursor.execute("SELECT id FROM orders")
