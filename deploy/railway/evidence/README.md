# Railway acceptance, 2026-11-19

Actual Railway HTTPS and all three real clients passed the synthetic five-question
acceptance. Railway resources are deleted; the synthetic GitHub context repository
still requires a `delete_repo` permission grant to finish cleanup. It contains only
generated fictional context, with no credentials. Local tests are separate evidence.

| Evidence | Result |
| --- | --- |
| [Live run and provenance](live-run.json) | Auth, protocol, four tools/resources, five reference answers, row cap, read-only grants, deadline/recovery, Git refresh, source uploads, Railway deletion |
| [Claude Code 2.1.308](claude-railway-http.json) | 18 completed MCP calls, 6 SQL queries; all five answers match |
| [Codex CLI 0.169.0](codex-railway-http.json) | 13 completed MCP calls, 5 SQL queries; all five answers match |
| [Cursor 2026.11.17-f532c45](cursor-railway-http.json) | 19 completed MCP calls, 5 SQL queries; all five answers match |

Each client used the existing `examples/mcp-demo/client_matrix.py` harness with
`--http-url` targeting the real Railway HTTPS `/mcp` endpoint. Configuration was
temporary. Expected answers were withheld from the model. Evidence retains actual
context/query calls and query rows, not just a successful client exit. `answer_evidence`
indexes the tool call, columns and rows supporting each final answer: Claude combined
revenue/average in one query and selected the first row of a full customer ranking.

Cursor attempted one user-local response-style skill read; permissions denied it.
Its only successful non-query discovery operation fetched the MCP tool definitions.
No local fixture data was supplied to any model outside MCP. Raw transcripts,
endpoint/account/resource/session identifiers and credentials are not committed.

Runtime is unchanged from `e95d4bd47833625d764ad987580162ec84efd218`.
Initial cloud source was signed `154f983df1f920685cd1cd34b3718e82b8722b09`
(see the JSON for the authoritative full SHA). Railway rejected Docker `VOLUME`,
so the uploaded Dockerfile omitted exactly `VOLUME ["/tmp"]`; the derived file hash
and actual image digest are recorded. That provider correction was signed separately.
Final cloud update was built from signed
`94f93f37f29734aea7bb62707b48b79273a8f90f`. Clients ran against the initial image;
the final updated deployment passed the full probe separately. Both image digests
and source commits are in `live-run.json`. No shared Dockerfile/runtime change occurred.

The public context's Git marker appeared after 52.21 seconds and disappeared after
its revert in 58.43 seconds, without a service restart. The poll interval was 60
seconds. The final 35-second sleep request was interrupted by the 30-second query
budget; the HTTP round trip took 33.01 seconds. A subsequent query succeeded, and
Postgres reported no remaining active sleep query for the reader. Direct Postgres
UPDATE, DELETE, CREATE and TEMP were denied even with transaction read-only mode off.

Cleanup found two real provider constraints. Railway CLI deletion initially scheduled
a 48-hour grace period. The official live API's `projectScheduleDeleteForce(id)`
removed the exact scheduled project immediately: list absence, “Project not found”
and the former endpoint's HTTP 404 were checked. GitHub deletion returned HTTP 403
because the authenticated token lacks `delete_repo`; the private ownership record
identifies the exact repository for the user/lead. Do not label that remaining item
as completed cleanup.

Railway reported $0.00048289943825925925 of project usage before deletion. This is a
point-in-time API observation, not a final bill or a free-hosting promise. No plan
upgrade was purchased. One project, two services, one database volume and one service
domain were owned by this run; temporary database TCP proxies were removed. An
unrelated existing project was left untouched.

Local validation: 7 focused checks with isolated Postgres passed; full suite
1,540 passed, 21 skipped; Tableau demo passed. Focused checks cover fixture grants,
context secret isolation, real local Postgres/HTTP, native cancellation, shell syntax,
secret stdin delivery, and execution of all three documented initial/update/rollback
build sequences with a mock Railway CLI. Actual rollback, private Git SSH mounts,
custom domains and other plans/regions were not executed.
