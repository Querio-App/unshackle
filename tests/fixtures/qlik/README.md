# Qlik Sense app

A hand-written Qlik Sense app in the raw layout `unshackle extract qlik` writes, over the
Dunder Mifflin data in `tests/data/dunder_mifflin/`. Used by `tests/test_qlik.py`; the
tests also import the app folder as a `qlik app unbuild` folder.

| File | What it covers |
| --- | --- |
| `tenant.json`, `spaces.json`, `apps.json`, `_errors.json` | the Qlik URL (`example.eu.qlikcloud.com`), one shared space, one app with item views and Section Access |
| `apps/<id>/script.qvs` | file loads of the five tables (`LOAD *`, field lists, `Upper(paper_type) AS PAPER_TYPE`, an `If()` field), a resident `GROUP BY` table, a script variable, an inline Section Access table with `*`, two values per user and `OMIT` |
| `dimensions.json` | 6 master dimensions, one calculated (`=MonthStart(purchased_at)`) |
| `measures.json` | 9 master measures: plain aggregations, set modifiers (quoted, `$(variable)`, `-=` on a joined table), ratios, a variable divisor, `Aggr()` and `TOTAL` |
| `variables.json` | a script-created variable and one defined in the app |
| `objects/` | two sheets: KPIs, a filter pane, vertical and horizontal bar charts, a line chart with two measures, a table, a pie chart with an inline measure |
| `bookmarks.json` | one bookmark with a selection on `status` |
| `tables-and-keys.json` | the data model the script builds: per-table rows, fields, type tags and distinct counts, computed from the data in `tests/data/dunder_mifflin/` |
| `app-properties.json`, `connections.yml` | app title and owner; no data connections (the data is files in the space) |

Ids are fake (`00000000-0000-4000-8000-...`, `sheet01`), and the Section Access users
(`DM\DAVID`, `DM\PAM`, `DM\STANLEY`, `DM\OSCAR`) are fictional. The numbers the tests
expect are computed from the same data in the tests, following Qlik's documented semantics.
