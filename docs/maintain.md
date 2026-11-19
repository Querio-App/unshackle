# Maintain your context

Start on a branch with a clean or safely saved working tree. Keep the last reviewed
context revision and the inputs used to produce it. Refresh changes definitions;
verification checks their meaning against source numbers.

## Refresh the source you use

For a BI tool, preserve the original object scope and include its dependencies:

```sh
unshackle extract <tool> --only <id>
unshackle status
unshackle build <tool>
unshackle todo
unshackle verify
```

For manual BI files, repeat `import` with all intended exports before `build`.
Read `migration/report.md` for hand-edited files kept by the writer. Merge upstream
changes into those files using the [migration skill](../skills/migrate/SKILL.md#re-runs);
do not overwrite edits or delete unresolved TODOs to make a report look complete.

For non-BI context, rerun the corresponding [warehouse](import-warehouse.md),
[dbt](import-dbt.md) or [SQL](import-sql.md) import. Use the same warehouse identity,
refresh dbt compiled artifacts through your normal dbt process, and pass every SQL
input root again. Imports already write output; there is no separate BI build step.

For a combined refresh with a schema/reference report, configure and run
[drift](drift.md). Export `UNSHACKLE_WAREHOUSE_URL` into the process environment first;
the module does not load `.env`:

```sh
python -m unshackle.drift --repo . --config migration/imports.json \
  --report /tmp/unshackle-drift.md
```

Review removed/added columns and uncertain query references. A rename is reported as
removal plus addition. Drift does not run SQL or establish parity. Recheck affected
questions against pinned source filters and closed periods using the
[verify skill](../skills/verify/SKILL.md). Commit reviewed context changes before the
hosted server polls them; a pinned ref needs a deliberate update.

## Troubleshoot before retrying

| Symptom | Check and stopping condition |
| --- | --- |
| Missing source objects or tables | Check credential visibility, selected source IDs and warehouse/schema. Ask for scoped access; do not assume deletion or elevate automatically. |
| Import cannot connect | Confirm URL scheme, installed driver, network/TLS and role privately. Stop on authentication failure; never publish raw secrets or adapter errors. |
| dbt SQL still has Jinja or relation TODOs | Refresh compiled artifacts and inspect the manifest. Do not infer a physical table from unresolved configuration. |
| Refresh failed halfway | Earlier imports may have written local files. Review `git diff` before retrying or discarding anything. |
| MCP context works but SQL fails | `overview` only reports configuration. Check the server's warehouse role/driver and run a small SELECT. MCP queries support PostgreSQL/psycopg, SQLite and DuckDB. |
| Hosted 401 or startup failure | Check the bearer header; then Git URL/ref and initial fetch access. Failed later refreshes keep the last good snapshot. |
| Numbers differ | Compare grain, joins, filters, dates/timezones and datasource. Follow the verify skill; do not remove TODOs or loosen tolerance to hide a mismatch. |
| Result table missing | Reconnect and make a fresh call; use fewer columns/rows for large payloads. JSON/text remains available; see [table limits](mcp-result-table.md). |

## Roll back or stop

For a context regression, preserve local work and revert the specific reviewed change
on a branch, review it, then publish through your normal PR process. Alternatively,
configure the server's `--repo-ref` / `UNSHACKLE_REPO_REF` to a known-good context commit
and restart it. A pinned commit stops automatic advancement. Confirm the restored
content with `search`/`read` and rerun the affected question. Runtime/image rollback
and warehouse rollback are separate; changing Git does not undo database changes.

Local stdio stops with its client; remove only its registration when no longer needed.
For a foreground HTTP process, stop that process. For Compose, use the same deployment
file/project to run `docker compose -f deploy/compose.yml down`. Hosted services may
continue billing after client removal: use the exact deployment's [shutdown instructions](deploy/README.md#updates-access-and-shutdown).
Revoke shared access by rotating the server token and updating clients. Removing one
connector does not revoke copies of its token. Keep unrelated services and repositories.
