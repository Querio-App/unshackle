#!/usr/bin/env python3
"""Read-only cleanup verification; retain state on failure and retry after settling."""
import json
import os
from pathlib import Path

from preflight import absent, aws, records


def read(*args):
    result = aws(*args)
    if result.returncode:
        raise RuntimeError("Verification API failed; resolve auth/permissions before claiming cleanup: " + " ".join(args[:2]))
    return json.loads(result.stdout)


def main():
    prefix, state = os.environ["PREFIX"], Path(os.environ["STATE"])
    for name in (prefix, prefix + "-images"):
        absent(("cloudformation", "describe-stacks", "--stack-name", name), "ValidationError", "does not exist")
    absent(("ecr", "describe-repositories", "--repository-names", prefix + "-images"), "RepositoryNotFoundException")
    absent(("acm", "describe-certificate", "--certificate-arn", (state / "certificate-arn").read_text().strip()), "ResourceNotFoundException")
    resources = json.loads((state / "resources.json").read_text())["StackResourceSummaries"]
    secret_ids = [r["PhysicalResourceId"] for r in resources if r["ResourceType"] == "AWS::SecretsManager::Secret"]
    if (state / "git-secret-arn").exists():
        secret_ids.append((state / "git-secret-arn").read_text().strip())
    for secret_id in secret_ids:
        absent(("secretsmanager", "describe-secret", "--secret-id", secret_id), "ResourceNotFoundException")
    cluster = read("ecs", "describe-clusters", "--clusters", prefix)
    if any(c["status"] == "ACTIVE" for c in cluster.get("clusters", [])):
        raise RuntimeError("Cluster is still active")
    if any(f.get("reason") != "MISSING" for f in cluster.get("failures", [])):
        raise RuntimeError("Cluster absence cannot be verified")
    for status in ("ACTIVE", "INACTIVE", "DELETE_IN_PROGRESS"):
        revisions = read("ecs", "list-task-definitions", "--family-prefix", prefix, "--status", status)
        if any(arn.rsplit("/", 1)[-1].split(":")[0] == prefix for arn in revisions["taskDefinitionArns"]):
            raise RuntimeError("Task definition deletion is not finished; retain state and retry")
    groups = read("logs", "describe-log-groups", "--log-group-name-prefix", "/ecs/" + prefix)
    if any(g["logGroupName"] == "/ecs/" + prefix for g in groups["logGroups"]):
        raise RuntimeError("Log group remains")
    owned_names = {os.environ["DOMAIN_NAME"].rstrip(".").lower(),
                   json.loads((state / "acm-record.json").read_text())["Name"].rstrip(".").lower()}
    if any(r["Name"].rstrip(".").lower() in owned_names for r in records()):
        raise RuntimeError("Owned DNS record remains")
    print("PASS: scoped AWS resources absent; verify GitHub/local cleanup before removing state")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
