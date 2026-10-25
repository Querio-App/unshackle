# Qlik Cloud extract

A real `unshackle extract qlik` of the app that `tests/live/test_qlik_live.py` builds in a
Qlik Cloud tenant (engine 12.2969): the five Dunder Mifflin tables as Parquet data files,
a load script with a calculated field, a resident `GROUP BY` table and an inline Section
Access block, 6 master dimensions, 9 master measures, a variable, two sheets and a bookmark.
Used by `tests/test_qlik.py`.

The files are as the engine and REST APIs returned them, except that the tenant host is
`example.eu.qlikcloud.com`, every id (app, space, user, objects, master items) is a fake
one, and the bot user that ran the test is `DM\MICHAEL` in the script and app properties.
The script's other Section Access users were fictional to begin with.

The numbers Qlik's engine returned for this app in the live run are asserted against
the generated SQL on the same data in `test_tiles_and_metrics_return_qlik_numbers_on_duckdb`.
