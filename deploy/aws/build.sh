#!/usr/bin/env bash
# Local image builds only. Push is an explicit separate guide step.
set -euo pipefail
if [[ $# != 2 ]]; then echo 'Usage: build.sh IMAGE_REPOSITORY IMMUTABLE_TAG' >&2; exit 2; fi
image=$1 tag=$2
root=$(cd "$(dirname "$0")/../.." && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp "$root/examples/mcp-demo/schema.sql" "$stage/schema.sql"
cp "$root/deploy/aws/reader.sh" "$stage/reader.sh"
cp "$root/deploy/aws/fixture.Dockerfile" "$stage/Dockerfile"
docker build --platform linux/amd64 -t "$image:base-$tag" "$root"
docker build --platform linux/amd64 --build-arg BASE_IMAGE="$image:base-$tag" \
  -t "$image:mcp-$tag" "$root/deploy/aws"
docker build --platform linux/amd64 -t "$image:fixture-$tag" "$stage"
