# Cloud deployment validation

Validation date: 2026-11-19. Railway and GCP live rehearsals were separately
authorized after account login. AWS remains preparation-only. Local validation
cannot prove provider IAM, billing, quotas, networking, certificate issuance or
hosted client behavior; each row below records only the evidence obtained.

| Gate | Railway | GCP Cloud Run | AWS ECS Fargate |
| --- | --- | --- | --- |
| Guide and scoped lifecycle commands | [Guide](railway.md) | [Guide](cloud-run.md) | [Guide](aws.md) |
| Follow-up issue | [#210](https://github.com/Querio-App/unshackle/issues/210) | [#211](https://github.com/Querio-App/unshackle/issues/211) | [#212](https://github.com/Querio-App/unshackle/issues/212) |
| Draft PR | [#215](https://github.com/Querio-App/unshackle/pull/215) | [#216](https://github.com/Querio-App/unshackle/pull/216) | [#214](https://github.com/Querio-App/unshackle/pull/214) |
| Local syntax/config/fixture checks | Passed | Passed | Passed: lint, fixtures, actual containers |
| Usable CLI authentication | Verified, CLI 5.75.0 | Verified, CLI 594.0.0 | Unverified; no AWS calls |
| Billing/project/region/quota readiness | Isolated personal-workspace project created | New disposable project and billing linked | Unverified; domain/Route53 zone also required |
| Build/push and hosted deployment | Passed after provider Dockerfile correction | Image pushed and digest verified; Cloud Run not deployed | Not run |
| Public HTTPS, missing-token 401, authenticated MCP | Passed | Not run | Not run |
| Read-only role, five answers, caps/cancellation | Passed hosted | Actual Cloud SQL seed/write denial/native cancellation passed; hosted MCP not run | Local containers only |
| Claude Code hosted five-answer run | [5/5](../../deploy/railway/evidence/claude-railway-http.json) | Not run | Not run |
| Codex CLI hosted five-answer run | [5/5](../../deploy/railway/evidence/codex-railway-http.json) | Not run | Not run |
| Cursor hosted five-answer run | [5/5](../../deploy/railway/evidence/cursor-railway-http.json) | Not run | Not run |
| Service update and Git refresh | Update passed; marker add/revert refreshed without restart | Not run | Not run hosted |
| Scoped teardown/resource absence | Railway project absent; context repository subsequently retained for the team demo | Owned services/resources absent; project `DELETE_REQUESTED` | No cloud resources created |

Railway evidence includes [deployment, refresh and cleanup results](../../deploy/railway/evidence/live-run.json).
All three clients used the initial image; the final update was checked separately
with the full protocol/query probe. A private repository, actual rollback and custom
domain were not exercised. The observed project usage snapshot before deletion was
about USD 0.000483; this is not a final bill.

GCP [partial live evidence](../../deploy/gcp/evidence/run.json) covers account/billing
verification, a new project, actual Cloud SQL seed/permission/cancellation checks,
scoped IAM/secrets, and an amd64 image push with digest readback. Context publication
was held because the GitHub token lacked repository deletion permission. Cloud Run,
its three hosted clients, refresh and updates were not run. The owned SQL instance,
registry/images, secrets and runtime account were verified absent; project shutdown
is asynchronous. No GCP billing total was observed.

Before resuming, the account holder can grant GitHub's `delete_repo` scope with
`gh auth refresh -h github.com -s delete_repo`, or arrange deletion of only the
recorded temporary GCP context repository if one is created. Credentials need not be shared. Use a new
isolated GCP rehearsal after the prior project shutdown; follow the guide and
complete the remaining hosted gates before changing these statuses.

The owner subsequently authorized a separate [persistent Dunder Mifflin demo](team-demo.md).
It reuses the original Railway synthetic context repository on a dedicated branch,
preserving the rehearsal history. That repository is now intentionally retained,
not awaiting deletion. The old Railway cloud project was removed; the new demo
resources remain running by request. Do not run the old context-deletion command
against this retained repository. Its new acceptance is separate from this matrix.

The GCP test cleanup and shared local fixture generator now remove their own
anonymous Postgres volumes, with exact-volume verification. Possible volumes from
two earlier GCP test runs could not be linked to retained container records; their
cleanup is unverified, and no broad Docker prune was performed. Railway also retains
a stale, non-secret local CLI directory association after project removal; its guide
now unlinks before deletion. Neither limitation is presented as complete cleanup.

The earlier [acceptance evidence](../mcp-acceptance.md) covers three real clients over
stdio and local HTTP, plus local Postgres and a manually generated drift PR. It does
not itself cover any provider above or scheduled GitHub Actions. The provider rows
use additional live evidence, kept separate from those earlier local runs.

## Live validation record

For each provider, append a dated record only after running the documented commands:

1. Record CLI versions, selected region, image digest/source commit and context commit.
   Keep account identifiers, credential files and raw transcripts outside the repo.
2. Record signup/login/billing prerequisites and any command corrections needed for the
   actual account. An installed CLI, cached profile or successful help command is not
   authentication evidence.
3. Record owned resources in a private inventory before and after deploy. Confirm TLS,
   missing/wrong token rejection and authenticated MCP resources/tools.
4. Run the synthetic warehouse probe: independent five answers, direct write denial,
   cap, cancellation and a subsequent successful query. No production data is needed.
5. Run the [three real clients](README.md#record-hosted-acceptance), recording exact
   versions and sanitized tool/result evidence. Separate failed attempts and SDK probes.
6. Update the service image and context ref; verify the intended image/context, including
   last-good behavior on a failed refresh. Note idle CPU/minimum-instance configuration.
7. Execute scoped teardown and verify every owned resource is gone or pending its
   documented deletion window. Include volumes/backups, registries/images, secrets,
   log groups, roles, network resources, DNS records and projects created by the guide.
   Shared accounts, existing DNS zones and model subscriptions are not demo resources.

A remaining resource or delayed deletion belongs in the record with its expected
billing/deletion behavior. Do not mark the provider accepted until deployment, all
three clients, update and teardown have evidence. Fix the published commands from
actual results before replacing any “Not run” entry.
