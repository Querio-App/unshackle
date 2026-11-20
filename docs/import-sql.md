# Import SQL and embedded Python queries

```sh
unshackle init --repo .
unshackle import sql ./inputs/queries ./inputs/analysis --repo .
```

Each `.sql` or `.py` source becomes a Jupyter notebook when it contains SQL or an
uncertain query. Directories are searched recursively. Hidden directories/files,
`migration/`, `node_modules/`, `__pycache__/` and symlinks are skipped. An explicitly
supplied symlink is rejected. Do not import credentials or private configuration;
original SQL and relevant Python expressions are preserved in the output.

Python is parsed with `ast`, never imported or executed. Literal strings, adjacent
string literals and literal `+` concatenation are supported. Calls such as
`cursor.execute`, `pd.read_sql`, `read_sql_query` and `text` identify SQL arguments.
SQL-looking literals elsewhere are also captured. Imported Python's other behavior
is not reproduced.

Only a single parseable SELECT/WITH query becomes executable notebook SQL. Dynamic
f-strings, formatting, variable references, transformations, malformed Python,
unbound query parameters, unsupported SQL and multiple statements produce a
line-specific `TODO(unshackle)`. Malformed Python retains only its source path and
error line: query boundaries are unknown, so neither the whole file nor the failing
line is copied, avoiding unrelated credentials. For other unsupported expressions,
their original source appears in notebook Markdown
and `source_config.source_expression` in the saved intermediate representation;
the corresponding tile has no executable SQL. Translation never guesses a dynamic
value. Python variable flow, custom wrappers without SQL literals, and runtime
query construction cannot be reliably recovered. Review the source for those
cases. SQL parsing is conservative and dialect-specific syntax may need manual
translation. Import itself never connects to the warehouse or executes SQL.

Notebook identifiers contain a digest of each source's path relative to the common
input root, so equal basenames in different directories do not collide. Use the
same set of input roots on each refresh; changing that root changes notebook IDs.
Inputs can move together to another directory without changing generated output.
Source locations contain relative paths and 1-based line numbers, never absolute
machine paths.

Output follows `[paths].notebooks` in `unshackle.toml`. Repeating the same import is
stable. The normal writer preserves manually edited notebooks and other imports'
files. Resolve TODOs and inspect the recovered SQL before running notebooks with a
read-only warehouse role. Parsing is not a database permission boundary.

Inspect the generated files and `unshackle todo` before querying. Next:
[connect locally](mcp.md) or [refresh context](maintain.md).
