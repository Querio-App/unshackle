#!/usr/bin/env python3
"""Read-only fresh-name checks. No absence assumption on auth/API failures."""
import json
import os
import subprocess


def aws(*args):
    return subprocess.run(
        ["aws", "--profile", os.environ["AWS_PROFILE"], "--region", os.environ["AWS_REGION"],
         "--output", "json", "--no-cli-pager", *args],
        capture_output=True, text=True, check=False,
    )


def absent(args, error_code, detail=""):
    result = aws(*args)
    if result.returncode == 0:
        raise RuntimeError("Name already exists: " + " ".join(args[:2]))
    if f"({error_code})" not in result.stderr or detail not in result.stderr:
        raise RuntimeError("Read-only check failed; resolve authentication/API permissions: " + " ".join(args[:2]))


def records():
    result = aws("route53", "list-resource-record-sets", "--hosted-zone-id", os.environ["HOSTED_ZONE_ID"])
    if result.returncode:
        raise RuntimeError("Cannot inspect DNS; resolve authentication/API permissions")
    return json.loads(result.stdout)["ResourceRecordSets"]


def main():
    prefix = os.environ["PREFIX"]
    for name in (prefix, prefix + "-images"):
        absent(("cloudformation", "describe-stacks", "--stack-name", name), "ValidationError", "does not exist")
    absent(("ecr", "describe-repositories", "--repository-names", prefix + "-images"), "RepositoryNotFoundException")
    absent(("secretsmanager", "describe-secret", "--secret-id", prefix + "/git"), "ResourceNotFoundException")
    result = aws("ecs", "describe-clusters", "--clusters", prefix)
    if result.returncode:
        raise RuntimeError("Cannot inspect ECS; resolve authentication/API permissions")
    data = json.loads(result.stdout)
    if data.get("clusters") or any(f.get("reason") != "MISSING" for f in data.get("failures", [])):
        raise RuntimeError("Cluster name exists or cannot be verified")
    # A deleted stack/cluster can leave revisions behind. Teardown removes the
    # exact family, so refuse every pre-existing revision before claiming it.
    for status in ("ACTIVE", "INACTIVE", "DELETE_IN_PROGRESS"):
        result = aws("ecs", "list-task-definitions", "--family-prefix", prefix, "--status", status)
        if result.returncode:
            raise RuntimeError("Cannot inspect task definitions; resolve authentication/API permissions")
        revisions = json.loads(result.stdout)["taskDefinitionArns"]
        if any(arn.rsplit("/", 1)[-1].split(":")[0] == prefix for arn in revisions):
            raise RuntimeError("Task definition family already exists")
    domain = os.environ["DOMAIN_NAME"].rstrip(".").lower()
    if any(record["Name"].rstrip(".").lower() == domain for record in records()):
        raise RuntimeError("DNS hostname already exists")
    print("PASS: stack/repository/cluster/task-family/optional-secret names and DNS hostname are unused")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
