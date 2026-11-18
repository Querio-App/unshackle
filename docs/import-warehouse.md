# Import warehouse metadata

Set `UNSHACKLE_WAREHOUSE_URL` in your environment or ignored `.env`, then run:

```sh
unshackle import warehouse --repo .
```

Install the SQLAlchemy driver for your database first. For Postgres, install
`psycopg[binary]` and use the `postgresql+psycopg://` scheme. Use a read-only role
that can inspect the intended schemas. Import reads metadata, never table rows.
The URL, hostname, username and password are not written to the bundle or catalog.
Driver failures are reported without their potentially sensitive exception text.

The importer uses [SQLAlchemy reflection](https://docs.sqlalchemy.org/en/20/core/reflection.html)
for visible base tables, columns, types, nullable flags, primary and foreign keys,
and comments. It excludes system schemas. Views, materialized views, indexes,
check constraints and server defaults are not imported. A dialect that explicitly
cannot provide comments or keys produces a warning. Permission or connection
errors abort the whole import instead of publishing a partial schema. Metadata
silently hidden by the database's permission model cannot be detected; refresh
using the same role and connection. One configured warehouse is supported.

Catalog files follow `[paths].catalog` in `unshackle.toml`. Table and column order
is stable; composite key column order retains its meaning. Normalized filename
collisions fail rather than silently overwrite a table. No timestamps are added.

`migration/warehouse-schema.json` records the previous generated schema. Keep this
file with the catalog in Git. On refresh, values that still equal their generated
values are updated or removed. Handwritten descriptions, annotations, and columns
introduced by other imports survive. Removed generated columns disappear from the
active `columns` list; any edited annotations move to `removed_columns` with an
explicit TODO. Removed tables with annotations remain with a TODO; unedited
removed tables are pruned by the normal writer. A rename appears as a removal and
an addition, never a guessed match. Renames do not transfer annotations blindly.

The snapshot contains only machine metadata, not annotations or connection
credentials. Database comments themselves are copied verbatim; review them before
publishing. Do not point the same catalog at a different warehouse and treat that
as a refresh. Missing the snapshot makes the first refresh conservative: existing
catalog content is retained. Other imports keep their existing merge behavior.
