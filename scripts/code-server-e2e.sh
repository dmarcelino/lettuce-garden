#!/usr/bin/env bash
# End-to-end test of the code editor (docs/code-server.md). Opt-in and never run
# by tests/run.sh: it needs Docker, the stack's app-server image and, for the
# first build, the internet (code-server's release, Debian packages).
#
#   scripts/code-server-e2e.sh [--browser] [--keep] [APP_SERVER_IMAGE]
#
# APP_SERVER_IMAGE defaults to lettuce-app-server:v0.22.0-letta_0.34.21.
# --browser also drives the workbench in Chromium (Playwright's image, about 2 GB).
# --keep leaves everything running for inspection and prints how to remove it.
#
# It wires one stack's editor the way compose/code-server.yml does, without
# Cloudflare or Lettuce: the editor (read-only, no capabilities) on an internal
# network to the gate and one to the shell; the shell, built on the app-server
# image, on that one and an egress network; the workspaces and tools in a temp
# dir, created by root as agents would. Then:
# - the gate's checks against the real editor (code-server/tests/e2e/check_gate.py,
#   in the gate's test image, with a test signing key for the Access JWT);
# - the terminal's way into the shell (the SSH session exec-shell opens): git
#   with a hook that runs in the shell and not in the editor, and the
#   environment agent-env gives it (PATH, npm's prefix);
# - what the editor can't do: write its extensions dir, reach the internet,
#   listen on any port, be reached from the stack network or from the shell
#   (the gate, as nobody, reaches it only through its socket).
# - with --browser: agent-1's workspace gets what an agent could plant (a
#   terminal profile, an automatic task, its own TypeScript and tsserver node,
#   a tsconfig plugin, git.path, a git repository); Chromium opens the folder, a
#   TypeScript file and a terminal through the gate (cloudflared and Access
#   simulated by code-server/tests/e2e/serve_gate.py), asks to run a task, and
#   previews a planted README.md full of HTML that would run script, before and
#   after choosing "Disable" in the preview's security picker.
#   Opening the folder must ask nothing; nothing planted may run in the editor;
#   TypeScript (bundled) must run; the terminal must open in the shell; running
#   a task must ask for trust, which is never given; the preview must render and
#   the planted Markdown must make no request.
#
# Everything it creates is named cs-e2e-<random>; it refuses to start while
# anything named cs-e2e-* exists, and removes what it created on exit (the
# root-owned temp files through a throwaway container). Exit status: 0 when
# every check passes.
# shellcheck disable=SC2329 # cleanup is called by the trap
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP=lettuce-app-server:v0.22.0-letta_0.34.21
BROWSER=0
KEEP=0
DEBIAN=debian:12-slim
PLAYWRIGHT=mcr.microsoft.com/playwright/python:v1.63.0-noble
while (($#)); do
  case $1 in
    --browser) BROWSER=1; shift ;;
    --keep) KEEP=1; shift ;;
    -*) echo "error: unknown option $1" >&2; exit 2 ;;
    *) APP=$1; shift ;;
  esac
done

die() {
  echo "error: $*" >&2
  exit 1
}
for c in docker jq; do command -v "$c" >/dev/null || die "$c is required"; done
docker image inspect "$APP" >/dev/null 2>&1 || die "app-server image $APP not found"

R=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
P=cs-e2e-$R
leftovers=$(
  docker ps -a --filter name=cs-e2e- --format 'container {{.Names}}'
  docker network ls --filter name=cs-e2e- --format 'network {{.Name}}'
  docker volume ls --filter name=cs-e2e- --format 'volume {{.Name}}'
  docker images --format 'image {{.Repository}}:{{.Tag}}' | grep ' cs-e2e-' || true
)
[[ -z $leftovers ]] || die "cs-e2e resources already exist; inspect and remove them first:
$leftovers"

TMP=$(mktemp -d)
cleanup() {
  if ((KEEP)); then
    echo "kept: containers, networks and volumes named $P-*, images $P-*, files in $TMP"
    echo "remove: KEEP= by hand, or: docker rm -f \$(docker ps -aq --filter name=$P-); docker network rm \$(docker network ls -q --filter name=$P-); docker volume rm $P-data $P-keys $P-sock"
    return
  fi
  docker rm -f "$P-editor" "$P-exec" "$P-probe" "$P-gate" >/dev/null 2>&1 || true
  docker network rm "$P-exec" "$P-egress" "$P-default" >/dev/null 2>&1 || true
  docker volume rm "$P-data" "$P-keys" "$P-sock" >/dev/null 2>&1 || true
  docker rmi "$P-gate:test" "$P-editor:e2e" "$P-exec:e2e" >/dev/null 2>&1 || true
  docker run --rm -v "$TMP:/t" "$DEBIAN" sh -c 'rm -rf /t/*' >/dev/null 2>&1 || true
  rmdir "$TMP" 2>/dev/null || true
}
trap cleanup EXIT

passed=0 failed=0
result() {
  if [[ $1 == ok ]]; then
    passed=$((passed + 1))
    echo "ok    $2"
  else
    failed=$((failed + 1))
    echo "FAIL  $2"
  fi
}
# expect NAME COMMAND... -> passes when COMMAND succeeds
expect() {
  local name=$1
  shift
  if "$@" >/dev/null 2>&1; then result ok "$name"; else result fail "$name"; fi
}
# refuse NAME COMMAND... -> passes when COMMAND fails
refuse() {
  local name=$1
  shift
  if "$@" >/dev/null 2>&1; then result fail "$name"; else result ok "$name"; fi
}

echo "building the images (the first build downloads code-server)"
docker build -q --target test --build-context "garden-common=$ROOT/garden-common" \
  -t "$P-gate:test" "$ROOT/code-server" >/dev/null
docker build -q -t "$P-editor:e2e" "$ROOT/code-server/editor" >/dev/null
docker build -q --build-context "base=docker-image://$APP" -t "$P-exec:e2e" "$ROOT/code-server/exec" >/dev/null

# What agents leave: root-owned workspaces, an HTML file, a tools dir.
docker run --rm -v "$TMP:/t" "$DEBIAN" sh -c '
  mkdir -p /t/workspaces/agent-1 /t/tools
  echo "<html><script>parent.x=1</script>agent-page</html>" >/t/workspaces/agent-1/page.html'

docker network create --internal "$P-exec" >/dev/null
docker network create "$P-egress" >/dev/null
docker network create "$P-default" >/dev/null
docker volume create "$P-data" >/dev/null
docker volume create "$P-keys" >/dev/null
docker volume create "$P-sock" >/dev/null

# As compose/code-server.yml runs it: on its socket, which only the gate mounts besides it.
docker create --name "$P-editor" --network "$P-exec" --network-alias code-server-editor \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --user 0 \
  -e EXTENSIONS_GALLERY='{}' \
  -v "$TMP/workspaces:/work" -v "$P-data:/data" -v "$P-keys:/keys" -v "$P-sock:/run/code-server" \
  "$P-editor:e2e" --socket /run/code-server/editor.sock --socket-mode 666 --auth none --disable-proxy \
  --disable-telemetry --disable-update-check --disable-getting-started-override \
  --extensions-dir /opt/lettuce-editor/no-extensions --user-data-dir /data/user >/dev/null
docker start "$P-editor" >/dev/null
# listening -> success once the editor serves (its log is read whole: grep -q
# would end the pipe early and fail it under pipefail)
listening() { [[ $(docker logs "$P-editor" 2>&1) == *'HTTP server listening'* ]]; }
for _ in $(seq 60); do
  listening && break
  sleep 1
done
listening || die "the editor did not start: $(docker logs "$P-editor" 2>&1 | tail -5)"

docker create --name "$P-exec" --network "$P-exec" --network-alias code-server-exec --init \
  --cap-drop NET_RAW --security-opt no-new-privileges:true -w /work \
  -v "$TMP/workspaces:/work" -v "$TMP/tools:/opt/agent-tools" -v "$P-keys:/keys:ro" \
  "$P-exec:e2e" >/dev/null
docker network connect "$P-egress" "$P-exec"
docker start "$P-exec" >/dev/null
sleep 2

echo "the gate, in front of the real editor"
# As in a stack: nobody, with the socket read-only, and no network to the editor.
if docker run --rm --network none --user 65534 -v "$P-sock:/run/code-server:ro" "$P-gate:test" \
  python tests/e2e/check_gate.py; then
  result ok "gate checks"
else
  result fail "gate checks"
fi

echo "the shell"
# in_shell COMMAND -> runs COMMAND in agent-1's workspace in the shell, the way a
# terminal does: over the editor's SSH session, through agent-env
in_shell() {
  docker exec "$P-editor" ssh -F /opt/lettuce-editor/ssh_config -T code-server-exec \
    "cd -- /work/agent-1 && exec agent-env bash -c $(printf '%q' "$1")"
}
GITC="git -c user.name=e2e -c user.email=e2e@example.com"
expect "git runs from the terminal" in_shell "git init -q . && $GITC commit -q --allow-empty -m first"
in_shell "printf '#!/bin/sh\ntouch /tmp/hook-ran\n' >.git/hooks/pre-commit && chmod +x .git/hooks/pre-commit"
expect "a commit from the terminal runs its hook" in_shell "$GITC commit -q --allow-empty -m second"
expect "the hook ran in the shell" docker exec "$P-exec" test -e /tmp/hook-ran
refuse "the hook did not run in the editor" docker exec "$P-editor" test -e /tmp/hook-ran
# shellcheck disable=SC2016 # expanded in the shell
shell_env=$(in_shell 'echo $PATH; npm config get prefix' 2>&1 || true)
expect "the terminal's PATH has the agents' tools" grep -q '/opt/agent-tools/bin' <<<"$shell_env"
expect "npm installs into /opt/agent-tools" grep -qx '/opt/agent-tools' <<<"$shell_env"
expect "the terminal prints the notice" bash -c "docker exec -w /work/agent-1 $P-editor bash -c 'exec 0</dev/null; /opt/lettuce-editor/bin/exec-shell' 2>/dev/null | grep -q 'never store tokens or passwords here'"

echo "the editor's limits"
refuse "nothing can be installed into its extensions dir" docker exec "$P-editor" touch /opt/lettuce-editor/no-extensions/x
refuse "its root is read-only" docker exec "$P-editor" touch /opt/code-server/x
refuse "it has no way to the internet" docker exec "$P-editor" bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'
# listeners CONTAINER -> the TCP sockets listening in CONTAINER (state 0A), one per
# line, but Docker's own DNS server, which every container on a user-defined
# network has at 127.0.0.11 (0B00007F)
listeners() { docker exec "$1" awk 'NR > 1 && $4 == "0A" && $2 !~ /^0B00007F:/' /proc/net/tcp /proc/net/tcp6; }
expect "it listens on no port, only on its socket" test -z "$(listeners "$P-editor")"
refuse "the shell can't reach the editor" docker exec "$P-exec" bash -c 'exec 3<>/dev/tcp/code-server-editor/8080'
docker run -d --name "$P-probe" --network "$P-default" "$DEBIAN" sleep 300 >/dev/null
refuse "the stack network can't reach the editor" docker exec "$P-probe" bash -c 'exec 3<>/dev/tcp/code-server-editor/8080'
refuse "the stack network can't reach the shell" docker exec "$P-probe" bash -c 'exec 3<>/dev/tcp/code-server-exec/22'
docker exec "$P-editor" ssh-keygen -q -t ed25519 -N '' -f /tmp/stranger
refuse "the shell lets in no other key" docker exec "$P-editor" ssh -F /dev/null -i /tmp/stranger -o IdentitiesOnly=yes \
  -o BatchMode=yes -o UserKnownHostsFile=/data/ssh/known_hosts root@code-server-exec true

if ((BROWSER)); then
  echo "the workbench in a browser, with a planted workspace"
  docker run --rm -v "$TMP/workspaces/agent-1:/w" "$DEBIAN" sh -c '
    set -e
    cd /w
    mkdir -p .vscode node_modules/evil-plugin node_modules/typescript/lib
    echo "export const x: number = 1;" >index.ts
    echo "{\"compilerOptions\": {\"plugins\": [{\"name\": \"evil-plugin\"}]}}" >tsconfig.json
    echo "{\"name\": \"evil-plugin\", \"main\": \"index.js\"}" >node_modules/evil-plugin/package.json
    echo "require(\"fs\").writeFileSync(\"/tmp/planted-plugin\", \"\"); module.exports = () => ({create: (i) => i.languageService});" >node_modules/evil-plugin/index.js
    echo "require(\"fs\").writeFileSync(\"/tmp/planted-tsdk\", \"\");" >node_modules/typescript/lib/tsserver.js
    for m in nodepath gitpath; do printf "#!/bin/sh\ntouch /tmp/planted-%s\n" "$m" >"$m.sh"; chmod +x "$m.sh"; done
    cat >.vscode/settings.json <<EOF
{
  "terminal.integrated.profiles.linux": {"evil": {"path": "/bin/sh", "args": ["-c", "touch /tmp/planted-profile; sleep 60"]}},
  "terminal.integrated.defaultProfile.linux": "evil",
  "terminal.integrated.automationProfile.linux": {"path": "/bin/sh", "args": ["-c", "touch /tmp/planted-automation"]},
  "typescript.tsdk": "node_modules/typescript/lib",
  "typescript.enablePromptUseWorkspaceTsdk": true,
  "typescript.tsserver.nodePath": "/work/agent-1/nodepath.sh",
  "js/ts.tsserver.node.path": "/work/agent-1/nodepath.sh",
  "git.path": "/work/agent-1/gitpath.sh",
  "task.allowAutomaticTasks": "on"
}
EOF
    cat >.vscode/tasks.json <<EOF
{"version": "2.0.0", "tasks": [{"label": "build", "type": "process", "command": "/bin/sh",
  "args": ["-c", "touch /tmp/planted-task"], "runOptions": {"runOn": "folderOpen"}}]}
EOF
    cat >README.md <<EOF
# Planted preview

<script>new Image().src = "/planted-md-script";</script>
<img src="x" data-u="/planted-md-onerror" onerror="new Image().src = this.dataset.u">
<svg data-u="/planted-md-svg" onload="new Image().src = this.dataset.u"></svg>
<iframe src="/planted-md-iframe"></iframe>
<object data="/planted-md-object"></object>

![control](http://localhost:8000/control-md-image.png)
EOF'
  docker create --name "$P-gate" --network "$P-default" --user 65534 -v "$P-sock:/run/code-server:ro" \
    "$P-gate:test" python tests/e2e/serve_gate.py >/dev/null
  docker start "$P-gate" >/dev/null
  sleep 3
  # The image has the browsers; the Python package of the same version is installed on the fly.
  # The browser shares the gate's network namespace: it opens http://localhost:8000.
  if docker run --rm --network "container:$P-gate" -v "$ROOT/code-server/tests/e2e:/e2e:ro" "$PLAYWRIGHT" \
    sh -c 'python3 -m pip install -q --break-system-packages playwright==1.63.0 && python3 /e2e/browser_check.py'; then
    result ok "browser checks"
  else
    result fail "browser checks"
  fi
  planted=$(docker exec "$P-editor" sh -c 'ls /tmp/planted-* 2>/dev/null' || true)
  if [[ -z $planted ]]; then result ok "nothing planted ran in the editor"; else result fail "planted code ran in the editor: $planted"; fi
  # running COMMAND_PATTERN CONTAINER -> success when a process there matches
  running() {
    local all
    all=$(docker exec "$2" sh -c "for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' <\"\$f\"; echo; done 2>/dev/null")
    grep -qF -- "$1" <<<"$all"
  }
  expect "TypeScript runs in the editor (bundled)" running '/opt/code-server/lib/vscode/extensions/node_modules/typescript/lib/tsserver.js' "$P-editor"
  expect "what is typed in the terminal runs in the shell" docker exec "$P-exec" test -e /tmp/typed-in-terminal
  refuse "and not in the editor" docker exec "$P-editor" test -e /tmp/typed-in-terminal
fi

echo
echo "$passed passed, $failed failed"
((failed == 0))
