# Optional synthetic demo on AWS ECS Fargate

For your own context and warehouse, start with [HTTP/container hosting](README.md).
This example creates an isolated fixture, not a production database. Run from the
unshackle source checkout in Bash; review its billable resources and teardown first.

This recipe creates an ECR repository, a dedicated VPC/two public subnets, HTTPS
Application Load Balancer, one Fargate task, CloudWatch log group, scoped execution
role and three generated Secrets Manager secrets. The task contains MCP and a
synthetic PostgreSQL 16 database. **Every task replacement reseeds the database;
this is disposable fictional data, not a production warehouse.** DesiredCount is
restricted to 0 or 1. Updates intentionally interrupt service instead of running
multiple independent fixtures. No database port is exposed through the task's
security group. MCP port 8000 accepts traffic only from the ALB.

The AWS wrapper extends the existing root Dockerfile and entrypoint. It uses
`postgresql+psycopg` (native cancellation), creates only SELECT/USAGE/CONNECT grants
for `example_reader`, and proves direct database DELETE/CREATE denial at every
startup, even after disabling the role's default read-only transaction setting.
The admin password is injected only into the fixture container. Public access
requires HTTPS plus the generated bearer token; unauthenticated `/mcp` returns
401, also used by ALB health checks. An ALB health check alone proves neither Git
contents nor database query parity.

## One-time signup, billing, tools and login

1. [Create/activate an AWS account](https://repost.aws/knowledge-center/create-and-activate-aws-account),
   complete identity/payment verification, choose an appropriate billing plan,
   secure root with MFA and establish a non-root deployment identity. Signup,
   payment verification and browser authentication cannot honestly be replaced by
   unattended CLI commands. Never paste root/access keys into this repository.
2. Use an identity allowed to manage CloudFormation, ECR, ECS/Fargate, EC2 networking,
   ALB, ACM, Route53 records in your zone, CloudWatch Logs, Secrets Manager, and
   IAM role creation/pass-role. It needs `iam:CreateServiceLinkedRole` for ECS/ELB
   on a fresh account, plus `SignInLocalDevelopmentAccess` for `aws login`.
   Organization SCPs, regional quotas, billing restrictions and Fargate capacity
   can still block provisioning. No IAM escalation or account policy changes are
   performed by the scripts.
3. Own a domain and control its authoritative DNS. This minimal recipe requires
   an **existing public Route53 hosted zone in the same AWS account**, already
   delegated by the registrar. Domain registration/delegation is a prerequisite,
   not part of teardown. A new user can buy a domain through their registrar,
   create a public Route53 zone in the console and copy its nameservers to the
   registrar. Those steps may charge independently. ALB's AWS DNS name does not
   provide a usable managed HTTPS certificate for this recipe.
4. Install [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
   (2.32.0+ for `aws login`), Docker with a running engine, Git, jq, Python 3.11+,
   uv and GitHub CLI. On macOS the official installer is:

   ```bash
   curl -fsSLo /tmp/AWSCLIV2.pkg https://awscli.amazonaws.com/AWSCLIV2.pkg
   sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
   rm /tmp/AWSCLIV2.pkg
   # Linux: download/inspect the official installer, then run it.
   # curl -fsSLo /tmp/aws-install.sh https://awscli.amazonaws.com/v2/install.sh
   # bash /tmp/aws-install.sh
   aws --version
   docker version
   uv venv
   source .venv/bin/activate
   uv pip install -e '.[dev]'
   ```

5. **Later, when deployment is authorized**, log in interactively using temporary
   credentials. These instructions are not authorization for a preparation agent
   to start login/MFA. Existing organization MFA/SSO workflows take precedence.

   ```bash
   aws login --profile unshackle-demo
   # Identity Center alternative (requires an existing SSO assignment):
   # aws configure sso --profile unshackle-demo
   # aws sso login --profile unshackle-demo
   gh auth login
   ```

   Official [browser login](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
   and [SSO configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).
   Follow your organization’s MFA/SSO policy; keep account identifiers and credentials
   out of tracked examples.

## Configure names, region and costs

Use a fresh prefix and hostname. Keep the same shell/state directory for updates
and cleanup. After a shell restart, set `STATE` to that exact directory and
`source "$STATE/config.sh"`, then redefine `aws_cli` below. No all-account
discovery/delete loop is used. The `aws_cli` function
passes the selected profile and region on every call.

```bash
set -euo pipefail
umask 077
export AWS_PROFILE=unshackle-demo AWS_REGION=eu-west-1
export AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off
export PREFIX="unshackle-aws-$(python -c 'import secrets; print(secrets.token_hex(4))')"
export DOMAIN_NAME="$PREFIX.example.com"  # replace example.com with your owned domain
export HOSTED_ZONE_ID=Z_REPLACE_WITH_YOUR_ZONE
export CONTEXT_GITHUB_REPO="YOUR_GITHUB_OWNER/$PREFIX-context"
export STATE="$HOME/.local/state/unshackle/$PREFIX"
export CONTEXT_DIR="$STATE/context"
export CONTEXT_REPO_REF=main
export GIT_SECRET_ARN=''
export CPU=512 MEMORY=2048 DESIRED_COUNT=1
[[ "$PREFIX" =~ ^unshackle-[a-z0-9-]+$ ]]
test ${#PREFIX} -le 40
test ! -e "$STATE"  # new deployment only; reuse the saved state for updates
mkdir -p "$STATE"
aws_cli() { command aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"; }
aws_cli sts get-caller-identity --query '{Account:Account,Arn:Arn}'
# Check this is your intended account before any write.
aws_cli route53 get-hosted-zone --id "$HOSTED_ZONE_ID" \
  --query '{Name:HostedZone.Name,Private:HostedZone.Config.PrivateZone}'
python deploy/aws/preflight.py
declare -p AWS_PROFILE AWS_REGION PREFIX DOMAIN_NAME HOSTED_ZONE_ID CONTEXT_GITHUB_REPO \
  STATE CONTEXT_DIR CONTEXT_REPO_REF GIT_SECRET_ARN CPU MEMORY DESIRED_COUNT > "$STATE/config.sh"
```

The read-only preflight refuses existing stack/repository/cluster/optional-secret
names, exact-family ECS revisions in ACTIVE/INACTIVE/DELETE_IN_PROGRESS, and
hostname records. Prefix peers are excluded. Auth/API failures are errors, never evidence of absence.
A local state directory alone does not prove cloud ownership. Use a new prefix if
any check fails because a resource exists. Do not adopt an existing stack.

Cost defaults: 0.5 vCPU / 2 GiB Linux x86 Fargate, one task, seven-day logs, two ALB
subnets and one task public IPv4 address. Public-subnet egress avoids NAT gateway
charges; security groups still deny public task ingress. ALB hourly/LCU, public
IPv4, Fargate, Secrets Manager, ECR storage/scanning, logs, DNS and transfer charges
can apply while idle. `CPU=1024` and `MEMORY=2048|3072|4096` are supported; a smaller
instance is not assumed sufficient. `DESIRED_COUNT=0` stops task compute only;
ALB and other charges remain until teardown. No free-tier/cost claim is made.
See [Fargate sizes](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html)
and the [AWS pricing calculator](https://calculator.aws/).

## Generate and publish synthetic Git context

The existing fixture generator runs a uniquely named local Postgres container,
imports warehouse/dbt/SQL/Python context, then removes the container automatically.
Only synthetic context is published. Local passwords are removed before Git init.

```bash
python examples/mcp-demo/local.py --output "$CONTEXT_DIR"
rm "$CONTEXT_DIR/.env" "$CONTEXT_DIR/local-container.json"
git -C "$CONTEXT_DIR" init -b main
git -C "$CONTEXT_DIR" add .
git -C "$CONTEXT_DIR" commit -s -m 'Add synthetic MCP context'
gh repo create "$CONTEXT_GITHUB_REPO" --public --source "$CONTEXT_DIR" --remote origin --push
export CONTEXT_REPO_URL="https://github.com/$CONTEXT_GITHUB_REPO.git"
# Isolated environment: the hosted Git refresh cannot use your local HTTPS helper.
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
  git -c credential.helper= ls-remote --exit-code "$CONTEXT_REPO_URL" "$CONTEXT_REPO_REF"
```

The server's actual shallow fetch must succeed before HTTP starts. Branches, tags
and exact commit SHAs work; `ls-remote` verifies named refs, not arbitrary SHAs.
For a pinned SHA use a temporary `git init`, `git fetch --depth=1 URL SHA` and remove
that checkout. Use a moving branch only if automatic 60-second refresh is intended.
Never embed a token in a Git URL. HTTPS credential helpers, `.netrc` and GitHub CLI
login are **not** available inside hosted Git refresh.

### Optional private GitHub repository

Create the repository with `--private` instead. Use a repository-scoped **read-only
SSH deploy key**, not an HTTPS token. Run before app deployment. This creates one
additional owned secret and a deploy key; cleanup includes both. Do not enable
shell tracing, print the key, or pass secret contents as command arguments.

```bash
ssh-keygen -q -t ed25519 -N '' -f "$STATE/git-key"
gh api --method POST "repos/$CONTEXT_GITHUB_REPO/keys" \
  -f title="$PREFIX" -F key=@"$STATE/git-key.pub" -F read_only=true \
  --jq .id > "$STATE/deploy-key-id"
# GitHub's HTTPS-authenticated metadata publishes SSH host keys. Do not trust
# an unverified ssh-keyscan result or disable StrictHostKeyChecking.
curl -fsS https://api.github.com/meta > "$STATE/github-meta.json"
jq -r '.ssh_keys[] | "github.com " + .' "$STATE/github-meta.json" > "$STATE/known_hosts"
export CONTEXT_REPO_URL="ssh://git@github.com/$CONTEXT_GITHUB_REPO.git"
GIT_SSH_COMMAND="ssh -i $STATE/git-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$STATE/known_hosts" \
  git ls-remote --exit-code "$CONTEXT_REPO_URL" "$CONTEXT_REPO_REF"
jq -n --rawfile private_key "$STATE/git-key" --rawfile known_hosts "$STATE/known_hosts" \
  '{private_key:$private_key,known_hosts:$known_hosts}' > "$STATE/git-secret.json"
aws_cli secretsmanager create-secret --name "$PREFIX/git" \
  --secret-string "file://$STATE/git-secret.json" --query ARN --output text > "$STATE/git-secret-arn"
export GIT_SECRET_ARN=$(cat "$STATE/git-secret-arn")
rm "$STATE/git-secret.json" "$STATE/git-key" "$STATE/git-key.pub"
```

The AWS image writes mode-0600 keys/known_hosts under `/tmp/unshackle-git`, removes
secret env variables, and selects a fixed-path SSH wrapper through PATH. The
runtime deliberately sanitizes Git's environment and overrides GIT_SSH_COMMAND;
no custom environment variable or HOME override is required. The mount is lost
on task replacement. Use the default Secrets Manager KMS key for this example;
a customer-managed key requires explicit execution-role `kms:Decrypt` permission.
See [GitHub deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
and [GitHub metadata](https://docs.github.com/en/rest/meta/meta#get-github-meta-information).

## Create owned HTTPS certificate and DNS validation

A dedicated new hostname prevents
sharing ACM validation records with another certificate. `CREATE`, not `UPSERT`,
fails if the record already exists. If it fails, stop; do not claim ownership or
continue to the deletion commands until the conflict is resolved.

```bash
aws_cli acm request-certificate --domain-name "$DOMAIN_NAME" \
  --validation-method DNS --tags Key=unshackle-owner,Value="$PREFIX" \
  --query CertificateArn --output text > "$STATE/certificate-arn"
export CERTIFICATE_ARN=$(cat "$STATE/certificate-arn")
# ACM may take several seconds to supply its validation record.
for attempt in {1..30}; do
  aws_cli acm describe-certificate --certificate-arn "$CERTIFICATE_ARN" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord' > "$STATE/acm-record.json"
  if jq -e '.Name and .Value' "$STATE/acm-record.json" >/dev/null; then break; fi
  sleep 2
done
jq -e '.Name and .Value' "$STATE/acm-record.json" >/dev/null
jq '{Changes:[{Action:"CREATE",ResourceRecordSet:{Name:.Name,Type:.Type,TTL:60,ResourceRecords:[{Value:.Value}]}}]}' \
  "$STATE/acm-record.json" > "$STATE/validation-create.json"
aws_cli route53 change-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch "file://$STATE/validation-create.json" > "$STATE/validation-created.json"
aws_cli acm wait certificate-validated --certificate-arn "$CERTIFICATE_ARN"
```

[ACM CLI](https://docs.aws.amazon.com/cli/latest/reference/acm/request-certificate.html),
[DNS validation](https://docs.aws.amazon.com/acm/latest/userguide/dns-validation.html),
[exact record creation/deletion](https://docs.aws.amazon.com/cli/latest/reference/route53/change-resource-record-sets.html).
The certificate must be in the same region as the ALB. Do not remove its validation
record while the certificate is in use; renewal depends on that record.

## Build, push and deploy

```bash
aws_cli cloudformation deploy --stack-name "$PREFIX-images" \
  --template-file deploy/aws/registry.yaml --tags unshackle-owner="$PREFIX"
export IMAGE_REPOSITORY=$(aws_cli cloudformation describe-stacks --stack-name "$PREFIX-images" \
  --query 'Stacks[0].Outputs[?OutputKey==`ImageRepository`].OutputValue | [0]' --output text)
export IMAGE_TAG=$(git rev-parse --short=12 HEAD)
declare -p IMAGE_REPOSITORY IMAGE_TAG CONTEXT_REPO_URL CONTEXT_REPO_REF GIT_SECRET_ARN \
  CERTIFICATE_ARN >> "$STATE/config.sh"
bash deploy/aws/build.sh "$IMAGE_REPOSITORY" "$IMAGE_TAG"
# Dedicated private Docker auth file; never reuse or print a general config.
export DOCKER_CONFIG="$STATE/docker"
mkdir -p "$DOCKER_CONFIG"
aws_cli ecr get-login-password | docker login --username AWS --password-stdin "${IMAGE_REPOSITORY%%/*}"
docker push "$IMAGE_REPOSITORY:mcp-$IMAGE_TAG"
docker push "$IMAGE_REPOSITORY:fixture-$IMAGE_TAG"
docker logout "${IMAGE_REPOSITORY%%/*}"
rm -rf "$DOCKER_CONFIG"
unset DOCKER_CONFIG

# No secret VALUES are CloudFormation parameters or outputs.
aws_cli cloudformation deploy --stack-name "$PREFIX" --template-file deploy/aws/stack.yaml \
  --capabilities CAPABILITY_IAM --tags unshackle-owner="$PREFIX" \
  --parameter-overrides "ImageRepository=$IMAGE_REPOSITORY" "ImageTag=$IMAGE_TAG" \
  "ContextRepoUrl=$CONTEXT_REPO_URL" "ContextRepoRef=$CONTEXT_REPO_REF" \
  "DomainName=$DOMAIN_NAME" "HostedZoneId=$HOSTED_ZONE_ID" "CertificateArn=$CERTIFICATE_ARN" \
  "GitSecretArn=$GIT_SECRET_ARN" "Cpu=$CPU" "Memory=$MEMORY" "DesiredCount=$DESIRED_COUNT"
aws_cli ecs wait services-stable --cluster "$PREFIX" --services mcp
aws_cli cloudformation describe-stacks --stack-name "$PREFIX" \
  --query 'Stacks[0].Outputs' > "$STATE/outputs.json"
export UNSHACKLE_MCP_URL=$(jq -r '.[] | select(.OutputKey=="McpUrl") | .OutputValue' "$STATE/outputs.json")
TOKEN_SECRET_ARN=$(jq -r '.[] | select(.OutputKey=="TokenSecretArn") | .OutputValue' "$STATE/outputs.json")
# Capture privately; no token printed or placed in command argv/history.
aws_cli secretsmanager get-secret-value --secret-id "$TOKEN_SECRET_ARN" \
  --query SecretString --output text > "$STATE/mcp-token"
export UNSHACKLE_MCP_TOKEN=$(cat "$STATE/mcp-token")
rm "$STATE/mcp-token"
```

The build is linux/amd64 even on Apple Silicon. ECR uses immutable tags: rebuilding
an existing tag cannot overwrite the pushed image. Each task shares localhost with
its sidecar; PostgreSQL initialization completes before MCP starts. There is no
NAT, default VPC dependency, RDS database, EBS volume or persistent fixture backup.
The execution role reads only these secrets; the application has no AWS task role.
See [CloudFormation deploy](https://docs.aws.amazon.com/cli/latest/reference/cloudformation/deploy.html),
[ECR stdin login](https://docs.aws.amazon.com/cli/latest/reference/ecr/get-login-password.html),
[awsvpc networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-networking-awsvpc.html),
[secret injection/restart requirements](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html).

## Verify and connect clients

```bash
python deploy/aws/probe.py
```

The probe performs missing/wrong bearer rejection, MCP initialization, tool/resource
listing and reads, context search/notebook reads, all five hand-derived queries,
a two-row cap, SQL write rejection, `pg_sleep(60)` cancellation at the server's
30-second deadline, and a successful query afterward. Direct database write denial
is checked by the provider entrypoint before HTTP starts; look for
`Synthetic fixture reader checks passed` in the MCP logs. Those direct checks
begin a READ WRITE transaction, so passing requires actual denied database grants.
They roll back and never modify the fixture. PostgreSQL permissions, not SQL
parsing alone, enforce read-only behavior ([GRANT](https://www.postgresql.org/docs/current/sql-grant.html)).

Expected numbers: paid revenue `100+50+80+120=350`; North `270`, South `80`;
top customer Acme Paper `150`; January `230`, February `120`; average paid order
`350/4=87.5`. Refunded `40` and cancelled `30` are excluded. Source questions and
SQL are in `examples/mcp-demo/questions.json`.

Use the shared [exact Claude/Codex/Cursor HTTP configurations](README.md#connect-clients)
with `UNSHACKLE_MCP_URL` ending `/mcp` and `UNSHACKLE_MCP_TOKEN` loaded above.
Ask your chosen client the paid-revenue question and inspect the query result.
The optional [example harness](../../examples/mcp-demo/README.md) consumes client
usage; keep its outputs private and outside Git.

## Logs, failures and updates

```bash
aws_cli logs tail "/ecs/$PREFIX" --since 15m
aws_cli ecs describe-services --cluster "$PREFIX" --services mcp \
  --query 'services[0].{Events:events[0:10],Deployments:deployments}'
aws_cli cloudformation describe-stack-events --stack-name "$PREFIX" \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED` || ResourceStatus==`UPDATE_FAILED`].[LogicalResourceId,ResourceStatusReason]'
TARGET_GROUP_ARN=$(jq -r '.[] | select(.OutputKey=="TargetGroupArn") | .OutputValue' "$STATE/outputs.json")
aws_cli elbv2 describe-target-health --target-group-arn "$TARGET_GROUP_ARN"
```

Do not enable `--debug`, shell xtrace, dump container environments or print secrets.
Auth/session failures require the user's normal login/MFA workflow. Missing ECR
images: check region, immutable tag and amd64 build. Secret initialization failures:
check execution-role access. Failed Git startup: check URL/ref, deploy-key access
and verified host keys. Private Git must use SSH. 502/503: inspect ECS events,
sidecar initialization, reader checks and 401 health matcher. TLS: verify zone
nameserver delegation, hostname and certificate region. No task ingress should
allow 5432 or public 8000. Default outbound access is needed for Git/ECR/secrets/logs.

For a code update, commit first, set a new `IMAGE_TAG`, repeat build/push and the
same `cloudformation deploy` command. For a changed Git SHA/ref, rerun deployment
with the new ref. With `main`, context commits refresh automatically; verify the
new context after at least one poll interval. Failed refresh keeps the previous
snapshot. For secret rotation, replace the secret value via a restricted file and
force task replacement (the disposable DB reseeds with the new reader password):

```bash
aws_cli ecs update-service --cluster "$PREFIX" --service mcp --force-new-deployment >/dev/null
aws_cli ecs wait services-stable --cluster "$PREFIX" --services mcp
# Reload the current token privately if rotated, then repeat probe + client checks.
```

For production, use the existing [ecs-task.json](../../deploy/ecs-task.json) with a
separate read-only warehouse URL secret and no fixture sidecar or AWS fixture
entrypoint. Supply private networking and database TLS as appropriate, verified
native PostgreSQL cancellation, real database grants, backup/availability policy
and sizing. That path requires its own reviewed infrastructure;
this disposable recipe is not a production topology.

## Scoped teardown (also after partial failure)

Keep the saved state until cleanup finishes. These commands permanently remove
**this prefix's** fixture, secrets, logs and images. Review the exact prefix/account
first. Delete only resources successfully created in this run; for a partially
failed run skip absent stacks/steps, inspect `DELETE_FAILED` events and retry after
resolving the named dependency. Do not sweep resources across an account.

```bash
aws_cli sts get-caller-identity --query '{Account:Account,Arn:Arn}'
aws_cli cloudformation list-stack-resources --stack-name "$PREFIX" > "$STATE/resources.json"
aws_cli cloudformation delete-stack --stack-name "$PREFIX"
aws_cli cloudformation wait stack-delete-complete --stack-name "$PREFIX"
# Removes cluster/service/task, ENIs, networks, ALB/listener/targets, A record,
# IAM execution role, seven-day log group and three generated secrets.

# Remove old revisions left by updates; exact family check excludes prefix peers.
for status in ACTIVE INACTIVE; do
  aws_cli ecs list-task-definitions --family-prefix "$PREFIX" --status "$status" \
    --query taskDefinitionArns --output json | jq -r '.[]' > "$STATE/task-revisions"
  while IFS= read -r arn; do
    [[ "$arn" == */"$PREFIX":* ]] || continue
    if [[ "$status" == ACTIVE ]]; then
      aws_cli ecs deregister-task-definition --task-definition "$arn" >/dev/null
    fi
    aws_cli ecs delete-task-definitions --task-definitions "$arn" >/dev/null
  done < "$STATE/task-revisions"
done
aws_cli cloudformation delete-stack --stack-name "$PREFIX-images"
aws_cli cloudformation wait stack-delete-complete --stack-name "$PREFIX-images"
# EmptyOnDelete removes all images in this stack-owned repository only.

aws_cli acm delete-certificate --certificate-arn "$(cat "$STATE/certificate-arn")"
# Only run if validation-created.json contains a successful ChangeInfo.Id.
jq -e '.ChangeInfo.Id' "$STATE/validation-created.json" >/dev/null
jq '.Changes[].Action="DELETE"' "$STATE/validation-create.json" > "$STATE/validation-delete.json"
aws_cli route53 change-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch "file://$STATE/validation-delete.json" >/dev/null

if [[ -f "$STATE/git-secret-arn" ]]; then
  aws_cli secretsmanager delete-secret --secret-id "$(cat "$STATE/git-secret-arn")" \
    --force-delete-without-recovery >/dev/null
fi
if [[ -f "$STATE/deploy-key-id" ]]; then
  gh api --method DELETE "repos/$CONTEXT_GITHUB_REPO/keys/$(cat "$STATE/deploy-key-id")"
fi
# Only the synthetic context repository created above (requires GitHub delete_repo scope).
gh repo delete "$CONTEXT_GITHUB_REPO" --yes
# If an interrupted push left Docker credentials, remove the private config too.
rm -rf "$STATE/docker"
unset UNSHACKLE_MCP_TOKEN UNSHACKLE_MCP_URL GIT_SECRET_ARN DOCKER_CONFIG
# Remove each local image tag built during this run (repeat for older IMAGE_TAGs).
docker image rm "$IMAGE_REPOSITORY:mcp-$IMAGE_TAG" "$IMAGE_REPOSITORY:fixture-$IMAGE_TAG" \
  "$IMAGE_REPOSITORY:base-$IMAGE_TAG"
python deploy/aws/verify_deleted.py
# Keep STATE until GitHub deletion and every scoped verification succeed.
# gh repo view must return HTTP 404; auth/network errors are NOT proof of deletion.
gh api "repos/$CONTEXT_GITHUB_REPO" --silent
# Expected: HTTP 404. Inspect that result before deleting the local state.
# rm -rf "$STATE"
```

CloudFormation [force-deletes its generated secrets](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html); ECR
[`EmptyOnDelete`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecr-repository.html)
removes this repository's contents. ACM validation records are explicitly deleted
from the saved exact CREATE payload. Never remove a validation record shared by
another certificate. ECS [task definition deletion](https://docs.aws.amazon.com/cli/latest/reference/ecs/delete-task-definitions.html)
can remain `DELETE_IN_PROGRESS` while historical references settle; verify it
finishes. CloudFormation deleted-stack metadata and normal CloudTrail audit
history may remain. ECS/ELB service-linked roles are shared account prerequisites;
retain them, along with the pre-existing domain/hosted zone, deployment identity
and billing account. Their creation does not establish exclusive ownership.

The read-only verifier checks exact stack/ECR/certificate/secret/log/DNS absence,
cluster status and every exact-family task revision. It treats only documented
not-found errors as absence; expired auth and API errors fail. Retain the state
and rerun if asynchronous deletion has not settled. For partial deployment, save
`resources.json` before deleting any existing app stack; if no app stack was ever
created, use `{"StackResourceSummaries":[]}` as its inventory. If the certificate
was never created, omit its verification rather than inventing an ARN. Always
review the failure against the actual successful steps; do not discard state to
suppress a verification failure. Domain/hosted-zone charges remain independently.
If the local context generator was interrupted before its `finally` cleanup,
remove only the exact container name with prefix `unshackle-mcp-example-` recorded in its
local-container.json; never prune all Docker resources.
