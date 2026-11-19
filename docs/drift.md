# Scheduled warehouse drift

Copy [`examples/workflows/unshackle-drift.yml`](../examples/workflows/unshackle-drift.yml)
into your context repository's `.github/workflows/`. It is an inactive example in
unshackle itself. The workflow reruns imports, compares deterministic warehouse
snapshots, checks static query references, and creates or updates
`automation/unshackle-drift`. It does not merge the pull request.

Create `migration/imports.json` in that context repo:

```json
{
  "dialect": "postgres",
  "imports": [
    {"source": "warehouse"},
    {"source": "dbt", "paths": ["inputs/dbt-project"]},
    {"source": "sql", "paths": ["inputs/queries", "inputs/analysis.py"]}
  ]
}
```

Configure warehouse exactly once and each other source at most once. dbt accepts
one project path; SQL accepts several paths. Paths are relative to the context
repository. Keep input files in dedicated input directories, separate from generated
output. The warehouse refresh runs first; dbt and SQL then preserve their own
context. This module never executes imported Python or notebook cells.

Set the repository secret `UNSHACKLE_WAREHOUSE_URL` to a SQLAlchemy URL for a
read-only warehouse role. Use a read replica for production. Set repository variable
`UNSHACKLE_REF` to a reviewed unshackle commit or release containing the drift
module; the template defaults to `main`. The example installs the Postgres psycopg
driver; change the driver and SQLGlot `dialect` for another warehouse. These are
configuration, not extra secrets. Never put connection URLs in `imports.json`.

Enable “Allow GitHub Actions to create and approve pull requests” in repository
Actions settings. The template grants its built-in `GITHUB_TOKEN` contents and
pull-request write permissions. Its fixed branch updates one PR across repeated
runs; no new PR is created when generated artifacts are unchanged. Only exact
changed artifact paths are passed to the PR action. On unchanged runs it receives
only the existing snapshot path, never an empty or broad file selection. Per-run migration reports and
last-build summaries are restored, and the PR body is written outside the checkout,
so these cannot cause report-only churn. The action runs after every successful refresh, so an open drift PR whose changes
have disappeared is closed and its automation branch deleted, using the action's
[documented no-diff behavior](https://github.com/peter-evans/create-pull-request#action-behaviour). The default token does not trigger further
PR workflows; consult the [create-pull-request token documentation](https://github.com/peter-evans/create-pull-request#token)
if those checks must run. Pin action versions to reviewed SHAs under your repository's
normal dependency policy.

Run the same refresh locally before enabling a schedule. Export
`UNSHACKLE_WAREHOUSE_URL` into the process environment first; unlike the CLI,
`python -m unshackle.drift` does not load the repository’s `.env`. Keep the report
path outside the repository:

```sh
python -m unshackle.drift --repo . --config migration/imports.json \
  --report /tmp/unshackle-drift.md
```

The JSON snapshot is `migration/warehouse-schema.json`, version 1, keyed by catalog
paths that respect Layout overrides. Diffs include added/removed tables and columns,
column types/nullability/comments, and key/table metadata changes. A column rename
is reported as one removal and one addition. Similar names are never treated as
proof of a rename. The PR also identifies generated SQL/notebook tiles that reference
missing columns. Static checks do not execute queries or establish parity.

References to ambiguous or absent tables, derived/CTE projections, unresolved Jinja,
Python reference files, another datasource, or source TODOs receive explicit
uncertainty. Missing columns inside statically resolved CTE bodies are still checked.
JOIN USING keys, whole-row relation references and PostgreSQL system columns are
also marked uncertain when ordinary catalog columns cannot establish their meaning.
This is conservative scope checking, not complete SQL lineage. Supply a dialect for
warehouse-specific syntax. One warehouse snapshot cannot validate another connection.

Hand edits remain protected by the existing writers. A failed refresh stops before
PR creation; local files written by earlier imports may remain and should be reviewed
before retrying. Error messages omit adapter exception text because it can contain
connection credentials. Debug access/configuration locally without publishing secrets.
