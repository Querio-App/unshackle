# AWS ECS Fargate

Use Fargate behind an HTTPS Application Load Balancer. Prerequisites: authenticated
AWS CLI, ECR repository, ECS cluster, two Secrets Manager secrets, and an execution
role that can pull ECR images, write CloudWatch logs and read only those two secrets
(and decrypt their KMS keys if applicable). The secrets contain the read-only
warehouse URL and bearer token. The runtime needs no AWS task role.

Use private subnets with outbound HTTPS/NAT for Git/ECR and access to the warehouse.
Create an ALB target group of type `ip`, forwarding to port 8000, with health-check
path `/mcp` and success code `401` (unauthenticated health probes intentionally cannot
read context). Attach it to an HTTPS listener with an ACM certificate. The task's
security group should accept port 8000 only from the ALB security group.

Set the following shell variables to your existing resource identifiers; they contain
configuration and secret references, never secret values:

```sh
export AWS_REGION=eu-west-1
export IMAGE=YOUR_ACCOUNT.dkr.ecr.eu-west-1.amazonaws.com/unshackle-mcp:latest
export EXECUTION_ROLE_ARN=arn:aws:iam::YOUR_ACCOUNT:role/unshackle-execution
export WAREHOUSE_SECRET_ARN=arn:aws:secretsmanager:eu-west-1:YOUR_ACCOUNT:secret:warehouse-url-SUFFIX
export TOKEN_SECRET_ARN=arn:aws:secretsmanager:eu-west-1:YOUR_ACCOUNT:secret:mcp-token-SUFFIX
export CONTEXT_REPO_URL=https://github.com/example/analytics-context.git
CLUSTER=your-cluster
SUBNET_A=subnet-your-private-a
SUBNET_B=subnet-your-private-b
TASK_SECURITY_GROUP=sg-your-task
TARGET_GROUP_ARN=arn:aws:elasticloadbalancing:eu-west-1:YOUR_ACCOUNT:targetgroup/YOUR_GROUP/ID

aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "${IMAGE%%/*}"
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
aws logs create-log-group --log-group-name /ecs/unshackle-mcp --region "$AWS_REGION"
python - <<'PY'
import json, os
from pathlib import Path
from string import Template
# Substitute only JSON string values; JSON serialization escapes configuration safely.
def render(value):
    if isinstance(value, str):
        return Template(value).substitute(os.environ)
    if isinstance(value, dict):
        return {key: render(item) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item) for item in value]
    return value
Path('/tmp/unshackle-task.json').write_text(json.dumps(render(json.loads(Path('deploy/ecs-task.json').read_text()))))
PY
TASK_DEFINITION=$(aws ecs register-task-definition --cli-input-json file:///tmp/unshackle-task.json \
  --query taskDefinition.taskDefinitionArn --output text)
aws ecs create-service --cluster "$CLUSTER" --service-name unshackle-mcp \
  --task-definition "$TASK_DEFINITION" --launch-type FARGATE --desired-count 1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_A,$SUBNET_B],securityGroups=[$TASK_SECURITY_GROUP],assignPublicIp=DISABLED}" \
  --load-balancers "targetGroupArn=$TARGET_GROUP_ARN,containerName=mcp,containerPort=8000" \
  --health-check-grace-period-seconds 120
```

If the log group already exists, retain it and skip its creation. For later releases,
register a new task definition and use `aws ecs update-service` with that revision.
Set log retention and deployment alarms according to your operating requirements.
The template has a non-root user, read-only root filesystem and one writable scratch
volume at `/tmp`. The image declares that volume with mode 1777 so the non-root process
can create isolated snapshots. Fargate provides ephemeral storage; retained snapshots
are removed on shutdown and disappear when the task is replaced.

Use the ALB DNS/custom domain plus `/mcp` for clients. Verify 401 without a token,
successful tool/resource calls with it, warehouse read-only permissions, and Git ref
refresh. These configuration examples were checked locally; no AWS deployment was
executed as part of this slice.

References: [Fargate bind mounts and permissions](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/bind-mounts.html),
[container security](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/security-tasks-containers.html).
