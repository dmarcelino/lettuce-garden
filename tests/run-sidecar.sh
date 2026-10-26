#!/usr/bin/env bash
# Tests for a sidecar image (broker/, gmail-triggers/) or the document tools
# (doc-tools/). They run in a container built from the image's `test` stage,
# so the host needs only Docker.
# agent-skills/ is mounted for the broker's vault-run helper tests. For
# gmail-triggers, the executor's tests also run in a Node image, unless pytest
# arguments narrow the run. Node 22's --test takes files or globs, not a
# directory; the quoted glob is expanded by node inside the container.
#   tests/run-sidecar.sh <dir> [pytest args...]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NODE_IMAGE=node:22.22.0-slim

dir=${1:-}
if [[ ! $dir =~ ^[a-z][a-z0-9-]*$ || ! -f $ROOT/$dir/Dockerfile ]]; then
  echo "usage: tests/run-sidecar.sh <dir> [pytest args...]   (dir: broker, gmail-triggers, doc-tools)" >&2
  exit 2
fi
shift

image=lettuce-$dir:test
docker build -q --target test --build-context "garden-common=$ROOT/garden-common" \
  -t "$image" "$ROOT/$dir" >/dev/null

mounts=()
if [[ $dir == broker ]]; then
  mounts+=(-v "$ROOT/agent-skills:/agent-skills:ro")
fi
docker run --rm --network none "${mounts[@]}" "$image" python -m pytest -q -p no:cacheprovider "$@"

if [[ $dir == gmail-triggers && $# -eq 0 ]]; then
  docker run --rm --network none -v "$ROOT/gmail-triggers/executor:/executor:ro" \
    "$NODE_IMAGE" node --test '/executor/tests/*.test.mjs'
fi
