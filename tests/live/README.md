# Live tests

Fixtures prove the parsers read what we think a tool exports. Live tests prove the
whole path against a **real instance**: the tool runs in Docker, content is created
through its API, `unshackle init / inventory / extract / build` run in a temp repo, and
`unshackle verify` checks that the migrated SQL returns the tool's own numbers on the
same Postgres. A tool is "verified live" only when its live test passes.

They are skipped unless `UNSHACKLE_LIVE=1`, so `pytest` and CI stay fast and offline.

Every test uses the same data: the fictional Dunder Mifflin paper company in
`data/dunder_mifflin/*.parquet` (five tables, see its README), loaded into a Postgres
`warehouse` service by `harness.Stack.load_data()`.

| Test | What runs | Time (images cached) |
| --- | --- | --- |
| `test_warehouse_live.py` | Postgres + the dataset + `unshackle verify` (no BI tool) | ~5 s |
| `test_lightdash_live.py` | Lightdash 3.23.1, MinIO; dbt + Lightdash CLI deploy; 4 charts, 1 dashboard; 6 parity cases | ~1.5 min |
| `test_metabase_live.py` | Metabase v0.64.1; native, query-builder and metric cards, a model, a snippet, a two-tab dashboard with filters; 5 parity cases | ~30 s |
| `test_superset_live.py` | Superset 6.1.0 (built with the Postgres driver); datasets with metrics, 5 charts, a dashboard with a native filter, a role, RLS; 9 parity cases | ~40 s |
| `test_redash_live.py` | Redash 26.9.0, its metadata Postgres and Redis; 4 parameterized queries, a dashboard; 6 parity cases | ~30 s |

The first run pulls images (Lightdash is a ~3.2 GB download, ~15 GB unpacked) and, through
`npx`, the Lightdash CLI. The Superset image is built locally from `superset/Dockerfile`.

## Run

Needs Docker with `docker-compose` (or the `docker compose` plugin), Node.js (for `npx
@lightdash/cli`) and a dbt with the Postgres adapter in its own virtualenv:

```bash
pip install -e ".[dev,live]"
python -m venv /tmp/dbt && /tmp/dbt/bin/pip install "dbt-core~=1.10.0" "dbt-postgres~=1.9"

UNSHACKLE_LIVE=1 UNSHACKLE_LIVE_DBT=/tmp/dbt/bin/dbt pytest tests/live -s   # all of them
UNSHACKLE_LIVE=1 pytest tests/live/test_metabase_live.py -s                  # one tool
```

`-s` prints the build summary and the `unshackle verify` output. Only the Lightdash test
needs Node.js and dbt. The tests run one after another; each stack is torn down before
the next starts, so they share the host ports.

| Variable | Default | |
| --- | --- | --- |
| `UNSHACKLE_LIVE` | unset | `1` runs the live tests |
| `UNSHACKLE_LIVE_DBT` | `dbt` on PATH | dbt with the postgres adapter |
| `UNSHACKLE_LIVE_KEEP` | unset | `1` leaves the containers up after the test (pair with `--basetemp=/tmp/live` to keep the repo) |
| `UNSHACKLE_LIVE_PG_PORT` | `55432` | host port of the warehouse |
| `UNSHACKLE_LIVE_PG_PASSWORD`, `UNSHACKLE_LIVE_SECRET` | generated per run | warehouse password and the tools' secret keys |
| `UNSHACKLE_LIVE_LIGHTDASH_PORT` | `58080` | host port of Lightdash |
| `UNSHACKLE_LIVE_METABASE_PORT` | `3300` | host port of Metabase |
| `UNSHACKLE_LIVE_SUPERSET_PORT` | `8188` | host port of Superset |
| `UNSHACKLE_LIVE_REDASH_PORT` | `5055` | host port of Redash |
| `UNSHACKLE_LIVE_LIGHTDASH_VERSION` | `3.23.1` | image tag and CLI version (keep them equal) |
| `UNSHACKLE_LIVE_LIGHTDASH_CLI` | `npx --yes @lightdash/cli@<version>` | a preinstalled `lightdash` binary |
| `UNSHACKLE_LIVE_COMPOSE` | `docker-compose`, else `docker compose` | compose command |

Passwords, secret keys and API keys are generated per run for the throwaway containers
(the Metabase, Superset and Redash admin passwords are fixed test values); nothing real is
used and nothing is stored. Each stack is a compose project named `unshackle-live-<tool>`,
started clean and torn down with `down -v` at the end. To clean up by hand (values only
need to be non-empty):

```bash
UNSHACKLE_LIVE_PG_PASSWORD=x UNSHACKLE_LIVE_SECRET=x docker-compose -p unshackle-live-<tool> \
  -f tests/live/docker-compose.warehouse.yml -f tests/live/<tool>/docker-compose.yml down -v
```

## Lightdash notes

- The `lightdash/lightdash` image is amd64 only. On Apple silicon it runs under
  emulation; Lightdash itself is fine under QEMU but the dbt inside the image segfaults
  (loading adapter plugins). So the dbt project is run and compiled by the host dbt and
  deployed with `lightdash deploy` to a project with `dbtConnection: none`, which is
  also what dbt Core users without a git connection do.
- Lightdash needs S3-compatible storage for results, hence MinIO.
- Source numbers come from Lightdash running the query itself (`POST
  /api/v2/projects/{uuid}/query/chart` and `/query/metric-query`, raw values), not
  from compiled SQL we run ourselves.
- `tests/live/fixtures/lightdash/` is the raw extract from a live run, used by regular
  (offline) regression tests in `tests/test_lightdash.py`.

## Add a tool

1. `tests/live/<tool>/docker-compose.yml`: the tool and whatever it needs. The warehouse
   service is `warehouse` (Postgres 16, db `dunder_mifflin`, user `unshackle`, password
   `${UNSHACKLE_LIVE_PG_PASSWORD}`), reachable from the tool as `warehouse:5432`. Pin the
   image tag and publish the tool on a fixed host port (overridable by an env var).
   Relative paths (`build:`, volumes) resolve from `tests/live/`, the first compose file's
   directory.
2. `tests/live/test_<tool>_live.py` with
   `pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]`:
   - `with Stack("<tool>") as stack:` then `stack.load_data()`;
   - `wait_for(...)` on the tool's health endpoint, then bootstrap an admin and API
     key through its API and create content (questions, dashboards) over the dataset;
     a longer bootstrap goes in `tests/live/<tool>/bootstrap.py`, loaded with
     `bootstrap_module("<tool>")`;
   - `run_unshackle(repo, "init" | "extract" | "build", ...)` with the tool's env vars;
   - get the tool's own results for each chart (its query API, raw values), save them
     with `write_csv` and write cases with `write_case` (format: `skills/verify/SKILL.md`);
   - `run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url}, check=False)`
     and assert every result in `migration/parity/results.json` is `pass`.
3. Validate the output with the same helpers as the offline tests:
   `test_ossie._check` for `semantic/*.ossie.yaml`, `conftest.notebook_source` for notebooks.
4. When the live run finds a bug, copy the raw extract to `tests/live/fixtures/<tool>/`
   and add an offline regression test next to the source's other tests.
