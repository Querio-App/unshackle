# Cloud deployment validation

Preparation date: 2026-11-19. The guides are intended to be executed after account
access and deployment authorization are established. **No cloud deployment was run
for this preparation.** Local validation cannot prove provider IAM, billing, quotas,
networking, certificate issuance, or hosted client behavior.

| Gate | Railway | GCP Cloud Run | AWS ECS Fargate |
| --- | --- | --- | --- |
| Guide and scoped lifecycle commands | [Guide](railway.md) | [Guide](cloud-run.md) | [Guide](aws.md) |
| Follow-up issue | [#210](https://github.com/Querio-App/unshackle/issues/210) | [#211](https://github.com/Querio-App/unshackle/issues/211) | [#212](https://github.com/Querio-App/unshackle/issues/212) |
| Local syntax/config/fixture checks | Pending | Pending | Pending |
| Usable CLI authentication | Pending login | CLI absent during preparation | Unverified |
| Billing/project/region/quota readiness | Unverified | Unverified | Unverified |
| Build/push and hosted deployment | Not run | Not run | Not run |
| Public HTTPS, missing-token 401, authenticated MCP | Not run | Not run | Not run |
| Read-only role, five answers, caps/cancellation | Not run hosted | Not run hosted | Not run hosted |
| Claude Code hosted five-answer run | Not run | Not run | Not run |
| Codex CLI hosted five-answer run | Not run | Not run | Not run |
| Cursor hosted five-answer run | Not run | Not run | Not run |
| Service update and Git refresh | Not run hosted | Not run hosted | Not run hosted |
| Scoped teardown/resource absence | Not run | Not run | Not run |

The earlier [acceptance evidence](../mcp-acceptance.md) covers three real clients over
stdio and local HTTP, plus local Postgres and a manually generated drift PR. It does
not cover any provider above or scheduled GitHub Actions.

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
