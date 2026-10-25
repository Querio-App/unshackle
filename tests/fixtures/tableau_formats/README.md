# Workbooks and data sources from several Tableau versions

Test files from Tableau's open-source repositories, used by `tests/test_tableau_formats.py`. They cover the
XML versions Tableau has written: `8.2`, `9.3`, `10.0` and `10.1`, and `18.1`, the format of
every release since 2018.1 (the `source-build` attribute names the release that saved it).

Only XML is kept: no data, extracts, images or sample workbooks. Each file was changed only to
replace database hosts, user names, site names and Tableau Server URLs with `example.*` values
and GUIDs with fake ones (`00000000-0000-4000-8000-…`).

| Fixture | Saved by | Source |
| --- | --- | --- |
| `workbooks/v8.2-datasource-in-v9.3.twb` | 9.3.1 (a version 8.2 data source) | document-api-python `test/assets/TABLEAU_82_TWB.twb` |
| `workbooks/v9.3-postgres-sheets.twb` | 9.3.3 | document-api-python `test/assets/datasource_test.twb` |
| `workbooks/v9.3-ephemeral-field.twb` | 9.3.3 | document-api-python `test/assets/ephemeral_field.twb` |
| `workbooks/v10.0-cross-database-no-tables.twb` | 10.0 | document-api-python `test/assets/TABLEAU_10_TWB.twb` |
| `workbooks/v10.1-cross-database-join.twb` | 10.1 | document-api-python `test/assets/multiple_connections.twb` |
| `workbooks/v2021.1-sqlserver-dashboard.twb` | 2021.1.2 | document-api-python `test/assets/filtering.twb` |
| `workbooks/v2024.2-csv-dashboard.twb` | 2024.2.0 | connector-plugin-sdk `tdvt/TDVT Results.twb` |
| `datasources/v9.3-sqlserver.tds` | 9.3 | document-api-python `test/assets/TABLEAU_93_TDS.tds` |
| `datasources/v9.3-postgres.tds` | 9.3 | document-api-python `test/assets/datasource_test.tds` |
| `datasources/v9.3-unicode.tds` | 9.3 | document-api-python `test/assets/unicode.tds` |
| `datasources/v10.0-cross-database-no-tables.tds` | 10.0 | document-api-python `test/assets/TABLEAU_10_TDS.tds` |
| `datasources/v10.0-postgres-calcs.tds` | 10.0 | document-api-python `test/assets/field_change_test.tds` |
| `datasources/f18.1-object-model.tds` | format 18.1, 2020.2+ (relationships) | document-api-python `samples/show-fields/nested.tds` |
| `datasources/f18.1-postgres-odbc.tds` | format 18.1 | connector-plugin-sdk `tdvt/samples/tds/cast_calcs.postgres_odbc.tds` |
| `datasources/f18.1-postgres-jdbc.tds` | format 18.1 | connector-plugin-sdk `tdvt/samples/tds/Staples.postgres_jdbc.tds` |
| `datasources/f18.1-hana.tds` | format 18.1 | connector-plugin-sdk `tdvt/test/tool_test/tds/cast_calcs.hana.tds` |
| `datasources/f18.1-hyper-extract.tds` | format 18.1, with an extract | connector-plugin-sdk `tdvt/test/tool_test/tds/cast_calcs.tde.tds` |
| `packaged/v10.0-sqlserver.tdsx` | 10.0, a packaged data source | document-api-python `test/assets/TABLEAU_10_TDSX.tdsx` |

Sources, both under the MIT License:

- https://github.com/tableau/document-api-python at commit `447357fc1ad21945b31c0ed20d69593c13cc7921`,
  Copyright (c) 2016 Tableau.
- https://github.com/tableau/connector-plugin-sdk at commit `1f9571b2503c19e685b957da9b489d923c5fbb8a`,
  Copyright (c) 2018 Tableau.

The license of both:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software
> and associated documentation files (the "Software"), to deal in the Software without
> restriction, including without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
> Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
> BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
> DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
