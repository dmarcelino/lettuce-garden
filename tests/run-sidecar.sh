#!/usr/bin/env bash
# Tests for a sidecar image (landing/, vault/, gmail-triggers/, news-radar/, code-server/) or
# the document tools (doc-tools/). They run in a container built from the image's
# `test` stage, so the host needs only Docker.
# agent-skills/ is mounted for the vault's vault-run helper tests. For landing,
# the lollo executor's tests (landing/executor/) also run in a Node image,
# unless pytest arguments narrow the run. Node 22's --test takes files or globs, not a
# directory; the quoted glob is expanded by node inside the container. For
# code-server (the gate), the editor image's own tests also run, unless pytest
# arguments narrow the run; its first build downloads code-server.
#   tests/run-sidecar.sh <dir> [pytest args...]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NODE_IMAGE=node:22.22.0-slim

dir=${1:-}
if [[ ! $dir =~ ^[a-z][a-z0-9-]*$ || ! -f $ROOT/$dir/Dockerfile ]]; then
  echo "usage: tests/run-sidecar.sh <dir> [pytest args...]   (dir: landing, vault, gmail-triggers, news-radar, code-server, doc-tools)" >&2
  exit 2
fi
shift

image=lollo-$dir:test
docker build -q --target test --build-context "lollo-common=$ROOT/lollo-common" \
  -t "$image" "$ROOT/$dir" >/dev/null

mounts=()
if [[ $dir == vault ]]; then
  mounts+=(-v "$ROOT/agent-skills:/agent-skills:ro")
fi
docker run --rm --network none "${mounts[@]}" "$image" python -m pytest -q -p no:cacheprovider "$@"

if [[ $dir == landing && $# -eq 0 ]]; then
  docker run --rm --network none -v "$ROOT/landing/executor:/executor:ro" \
    "$NODE_IMAGE" node --test '/executor/tests/*.test.mjs'
fi

if [[ $dir == code-server && $# -eq 0 ]]; then
  docker build -q --target test -t lollo-code-server-editor:test "$ROOT/code-server/editor" >/dev/null
  docker run --rm --network none lollo-code-server-editor:test
fi
