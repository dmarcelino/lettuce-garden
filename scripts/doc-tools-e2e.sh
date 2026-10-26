#!/usr/bin/env bash
# End-to-end test and measurements for the doc-tools profile (docs/doc-tools.md).
# Opt-in and never run by tests/run.sh: it needs Docker, the stack's app-server
# image and the Gotenberg image, and takes a few minutes.
#
#   scripts/doc-tools-e2e.sh [--report FILE] [--no-idle-wait] [APP_SERVER_IMAGE]
#
# APP_SERVER_IMAGE defaults to lettuce-app-server:v0.22.0-letta_0.34.21.
# --report FILE also writes the results and measurements there as Markdown.
# --no-idle-wait skips the 90 s wait for Gotenberg's idle measurement.
#
# It simulates four stacks (a-d) the way compose/doc-tools.yml and lettucectl
# wire them, without Cloudflare, the BFF or an agent: per stack, a default
# network and an internal doc-tools network, an app-server container (the real
# image, kept idle with `sleep`) on both with the doc-tools image mounted
# read-only, and Lettuce's MCP settings with the `office` and `pdf` stdio
# entries. Calls go through `letta mcp call` from the agent's workspace, as the
# upstream mcp-servers skill makes them. One Gotenberg from
# compose/shared/gotenberg.yml joins every doc-tools network as `gotenberg`.
# Documents are generated and benign.
#
# Everything it creates is named and labelled doctools-e2e-<random>; it refuses
# to start while anything named doctools-e2e-* exists, and removes what it
# created on exit, including the root-owned files in its temp dir (through a
# throwaway container). It builds the doc-tools image when missing and keeps it,
# as `lettucectl up` would. Exit status: 0 when every scenario passes.
# Scenarios and cleanup are called by name (run_scenario, trap).
# shellcheck disable=SC2329
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LETTUCE_HOME=$ROOT
# shellcheck source=lib/common.sh
source "$ROOT/lib/common.sh"

APP=lettuce-app-server:v0.22.0-letta_0.34.21
REPORT=
IDLE_WAIT=1
while (($#)); do
  case $1 in
    --report) REPORT=${2:?--report needs a file}; shift 2 ;;
    --no-idle-wait) IDLE_WAIT=0; shift ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) APP=$1; shift ;;
  esac
done

for c in docker jq; do command -v "$c" >/dev/null || die "$c is required"; done
docker image inspect "$APP" >/dev/null 2>&1 || die "app-server image $APP not found"

R=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
P=doctools-e2e-$R
LBL=doctools-e2e=$R
STACKS=(a b c d)
TMP=
SAMPLER_PID=
ALIAS_TAG=

# ── refuse to run next to leftovers ──────────────────────────────────────────
leftovers=$(
  docker ps -a --filter name=doctools-e2e- --format 'container {{.Names}}'
  docker network ls --filter name=doctools-e2e- --format 'network {{.Name}}'
  docker volume ls --filter name=doctools-e2e- --format 'volume {{.Name}}'
  docker images --format 'image {{.Repository}}:{{.Tag}}' | grep ' doctools-e2e-' || true
)
[[ -z $leftovers ]] || die "doctools-e2e resources already exist; inspect and remove them first:
$leftovers"

# ── cleanup ──────────────────────────────────────────────────────────────────
compose_e2e() {
  env -i PATH="$PATH" HOME="$HOME" ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
    docker compose -p "$P-shared" \
    --project-directory "$ROOT/compose/shared" \
    -f "$ROOT/compose/shared/gotenberg.yml" \
    --env-file /dev/null \
    "$@"
}

cleanup() {
  local rc=$?
  set +e
  [[ -z $SAMPLER_PID ]] || kill "$SAMPLER_PID" 2>/dev/null
  info "cleanup: removing $P-*"
  docker ps -aq --filter "label=$LBL" | xargs -r docker rm -f >/dev/null
  compose_e2e down -v --remove-orphans >/dev/null 2>&1
  docker ps -aq --filter "name=$P-" | xargs -r docker rm -f >/dev/null
  docker network ls -q --filter "label=$LBL" | xargs -r docker network rm >/dev/null
  docker network ls -q --filter "name=$P-" | xargs -r docker network rm >/dev/null
  docker volume ls -q --filter "name=$P-" | xargs -r docker volume rm >/dev/null
  [[ -z $ALIAS_TAG ]] || docker image rm "$ALIAS_TAG" >/dev/null
  if [[ -n $TMP && -d $TMP && $TMP == */"$P".* ]]; then
    # The containers wrote as root into the bind mounts.
    docker run --rm --name "$P-cleanup" --label "$LBL" --network none -v "$TMP:/t" \
      --entrypoint sh "$APP" -c 'rm -rf /t/..?* /t/.[!.]* /t/*' >/dev/null 2>&1
    rm -rf "$TMP"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

TMP=$(mktemp -d "${TMPDIR:-/tmp}/$P.XXXXXX")
mkdir -p "$TMP/out"

# ── results ──────────────────────────────────────────────────────────────────
RESULTS=()
MROWS=()
CALLROWS=()
NOTES=()
FAILED=0
FAIL_REASON=

run_scenario() { # run_scenario TITLE FUNCTION
  local t0=$SECONDS
  FAIL_REASON=
  info ""
  info "== $1"
  if "$2"; then
    RESULTS+=("PASS | $1 | $((SECONDS - t0)) s | |")
    info "PASS  $1"
  else
    FAILED=1
    RESULTS+=("FAIL | $1 | $((SECONDS - t0)) s | ${FAIL_REASON//|/\/}")
    info "FAIL  $1: $FAIL_REASON"
  fi
}

# expect WHAT CMD... -> runs CMD; on failure, WHAT is the scenario's reason
expect() {
  local what=$1
  shift
  "$@" && return 0
  FAIL_REASON=$what
  return 1
}
has() { [[ $1 == *"$2"* ]]; }
lacks() { [[ $1 != *"$2"* ]]; }
mrow() { MROWS+=("| $1 | $2 | $3 |"); }
mib() { awk -v b="$1" 'BEGIN { printf "%.0f MiB", b / 1048576 }'; }

# ── helpers that run inside the simulated app-servers ───────────────────────
write_helpers() { # write_helpers DIR
  cat >"$1/call.mjs" <<'EOF'
// node call.mjs SERVER TOOL JSON: one `letta mcp call`, as the mcp-servers skill
// runs it, printed as one line of JSON: {ms, rc, isError, result | text}.
import { spawnSync } from "node:child_process";
const [server, tool, args] = process.argv.slice(2);
const t0 = process.hrtime.bigint();
const r = spawnSync("letta", ["mcp", "call", `mcp__${server}__${tool}`, "--args", args,
  "--agent", "agent-local-mcp-global"],
  { env: { ...process.env, HOME: "/root/.letta/mcp-home" }, encoding: "utf8", maxBuffer: 64 << 20 });
const out = { ms: Number((process.hrtime.bigint() - t0) / 1000000n), rc: r.status };
try {
  const j = JSON.parse(r.stdout);
  if (j.error) { out.isError = true; out.text = JSON.stringify(j.error); }
  else {
    out.isError = !!j.isError;
    let v = (j.content || []).map((c) => c.text ?? "").join("\n");
    for (let i = 0; i < 2 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch { break; } }
    if (typeof v === "string") out.text = v; else out.result = v;
  }
} catch { out.isError = true; out.text = ((r.stdout || "") + (r.stderr || "")).slice(0, 2000); }
console.log(JSON.stringify(out));
EOF
  cat >"$1/measure.py" <<'EOF'
# python3 -I measure.py NAME COMMAND...: runs an MCP server, passing stdio and
# signals through, then logs its wall time and peak RSS.
import json, resource, signal, subprocess, sys, time
name, cmd = sys.argv[1], sys.argv[2:]
t0 = time.monotonic()
p = subprocess.Popen(cmd)
for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s, lambda n, f: p.send_signal(n))
rc = p.wait()
ms = int((time.monotonic() - t0) * 1000)
kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
with open("/tmp/doctools-e2e-measure.jsonl", "a") as f:
    f.write(json.dumps({"server": name, "ms": ms, "maxrss_kb": kb, "rc": rc}) + "\n")
sys.exit(rc if rc >= 0 else 128 - rc)
EOF
  cat >"$1/zipcat.py" <<'EOF'
# python3 -I zipcat.py FILE REGEX: the members of an Office file whose names match
import re, sys, zipfile
pattern = re.compile(sys.argv[2])
with zipfile.ZipFile(sys.argv[1]) as z:
    for n in sorted(z.namelist()):
        if pattern.fullmatch(n):
            sys.stdout.write(z.read(n).decode("utf-8", "replace") + "\n")
EOF
}

# write_settings FILE: Lettuce's MCP list with the measuring entries; office and
# pdf are added by lib/mcp-servers.mjs, as `lettucectl mcp` adds them
write_settings() {
  jq -n '{
    autoConversationTitles: false,
    autoConversationTitlesRollbackApplied: true,
    agents: [{
      agentId: "agent-local-mcp-global",
      baseUrl: "local:/data/local-backend",
      mcpServers: [
        {name: "officem", transport: "stdio", command: "/usr/bin/python3",
         args: ["-I", "/root/.letta/e2e/measure.py", "office", "/opt/lettuce-doc-tools/bin/officeagent"]},
        {name: "pdfm", transport: "stdio", command: "/usr/bin/python3",
         args: ["-I", "/root/.letta/e2e/measure.py", "pdf", "/opt/lettuce-doc-tools/bin/pdf-mcp"]}
      ]
    }]
  }' >"$1"
}

ws() { printf '%s/%s/work/agent-e2e-%s' "$TMP" "$1" "$1"; } # host path of a stack's workspace

LAST=
# call STACK SERVER TOOL JSON [WORKSPACE] -> LAST holds the result line
call() {
  local where=${5:-agent-e2e-$1}
  LAST=$(docker exec -w "/work/$where" "$P-$1-app" node /root/.letta/e2e/call.mjs "$2" "$3" "$4" 2>/dev/null) ||
    LAST='{"isError":true,"text":"docker exec failed"}'
  [[ -n $LAST ]] || LAST='{"isError":true,"text":"no output"}'
}
# A call succeeded when the server returned no error and, for an Office plan,
# the plan was valid, committed and error-free.
succeeded() {
  jq -e '.isError == false and ((.result | type) != "object" or
    ((.result.isValid // true) and (.result.committed // true) and ((.result.errors // []) | length == 0)))' \
    <<<"$1" >/dev/null 2>&1
}
brief() { jq -r '(.text // (.result.errors // [] | map(.code + ": " + .message) | join("; ")) // "") | .[:240]' <<<"$1"; }
ok() { # ok STACK SERVER TOOL JSON [WORKSPACE]
  call "$@"
  succeeded "$LAST" && return 0
  FAIL_REASON="$1: $3 $4 failed: $(brief "$LAST")"
  return 1
}
refused() { # refused STACK SERVER TOOL JSON [WORKSPACE]
  call "$@"
  succeeded "$LAST" || return 0
  FAIL_REASON="$1: $3 $4 was not refused"
  return 1
}
field() { jq -r "$1" <<<"$LAST"; }
outcome() { if succeeded "$1"; then echo ok; else brief "$1"; fi; }

create_args() { jq -nc --arg n "$1" --arg p "$2" '{connectionId: "workspace", name: $n, planJson: $p}'; }
edit_args() {
  jq -nc --arg s "$1" --arg p "$2" '{connectionId: "workspace", source: $s, planJson: $p, saveMode: "Replace", newName: ""}'
}
open_args() {
  jq -nc --arg s "$1" '{connectionId: "workspace", source: $s, fidelity: "content", paragraphOffset: 0, paragraphLimit: 200}'
}
TEXT=
pdf_text() { # pdf_text STACK PATH [PASSWORD] -> TEXT
  local args
  args=$(jq -nc --arg p "$2" --arg pw "${3:-}" '{path: $p} + (if $pw == "" then {} else {password: $pw} end)')
  ok "$1" pdf pdf_extract_text "$args" || return 1
  TEXT=$(field .result.text)
}
XML=
zipxml() { # zipxml STACK FILE REGEX -> XML, the matching members of an Office file
  XML=$(docker exec -w "/work/agent-e2e-$1" "$P-$1-app" python3 -I /root/.letta/e2e/zipcat.py "$2" "$3")
}
# shellcheck disable=SC2001 # a regex, not a fixed string
strip_tags() { sed 's/<[^>]*>//g' <<<"$1"; }
ws_hash() { # ws_hash STACK -> hash of the workspace's files, the OfficeAgent index excluded
  (cd "$(ws "$1")" && find . -path ./.officeagent -prune -o -type f -print0 | LC_ALL=C sort -z |
    xargs -0 -r sha256sum | sha256sum | cut -c1-16)
}
ip_of() { docker inspect -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" "$1"; }

# ── Gotenberg memory, as `docker stats` counts it ───────────────────────────
GOT=
GOT_CG=
mem_bytes() {
  if [[ -n $GOT_CG ]]; then
    local cur inact
    read -r cur <"$GOT_CG/memory.current"
    inact=$(awk '$1 == "inactive_file" { print $2 }' "$GOT_CG/memory.stat")
    echo $((cur - inact))
  else
    docker stats --no-stream --format '{{.MemUsage}}' "$GOT" | awk '{
      v = $1; u = v; sub(/[0-9.]+/, "", u); sub(/[A-Za-z]+$/, "", v)
      m = (u == "GiB") ? 1073741824 : (u == "MiB") ? 1048576 : (u == "KiB") ? 1024 : 1
      printf "%.0f\n", v * m }'
  fi
}
sampler_start() {
  : >"$TMP/samples"
  (while :; do
    mem_bytes >>"$TMP/samples" 2>/dev/null || true
    sleep 0.2
  done) &
  SAMPLER_PID=$!
}
sampler_stop() { # -> peak bytes
  kill "$SAMPLER_PID" 2>/dev/null || true
  wait "$SAMPLER_PID" 2>/dev/null || true
  SAMPLER_PID=
  sort -n "$TMP/samples" | tail -n1
}

# ── documents (generated, benign) ────────────────────────────────────────────
BRIEF_PLAN='[{"op":"changeText","target":{"paraId":"auto-0000","expect":""},"with":"Project brief","mode":"Direct"},{"op":"insertParagraphs","target":{"paraId":"auto-0000","expect":"Project brief"},"position":"After","mode":"Direct","paragraphs":[{"text":"The supplier is Example Supplies."},{"text":"Delivery is due within thirty days."},{"text":"Payment terms are net 60."}]}]'
DECK_PLAN='[{"op":"changeText","target":{"paraId":"slide256/shape2/p0","expect":""},"with":"Quarterly Review","mode":"Direct"},{"op":"insertSlide","slide":{"layout":"titleAndContent","title":"Next Steps","body":["Plan the garden","Paint the fence"]}}]'
BOOK_PLAN='[{"op":"setCell","target":{"sheetId":1,"address":"A1"},"value":"Item"},{"op":"setCell","target":{"sheetId":1,"address":"B1"},"value":"Amount"},{"op":"setCell","target":{"sheetId":1,"address":"B2"},"value":"40"},{"op":"setCell","target":{"sheetId":1,"address":"B3"},"value":"2.5"},{"op":"setCell","target":{"sheetId":1,"address":"B4"},"formula":"SUM(B2:B3)*2"}]'
marker_plan() { # marker_plan TEXT -> a one-paragraph Word plan
  jq -nc --arg t "$1" '[{op: "changeText", target: {paraId: "auto-0000", expect: ""}, with: $t, mode: "Direct"}]'
}
deck_plan() { # deck_plan N -> a deck of N content slides
  jq -nc --argjson n "$1" '[range($n) | {op: "insertSlide", slide: {layout: "titleAndContent",
    title: "Slide \(. + 1)", body: ["First point", "Second point", "Third point"]}}]'
}
book_plan() { # book_plan ROWS -> values in A, formulas in B
  jq -nc --argjson n "$1" '[range(1; $n + 1) | ({op: "setCell", target: {sheetId: 1, address: "A\(.)"}, value: "\(.)"},
    {op: "setCell", target: {sheetId: 1, address: "B\(.)"}, formula: "A\(.)*2+SUM(A1:A\(.))"})]'
}

# ═════════════════════════════════════════════════════════════════════════════
# Setup
# ═════════════════════════════════════════════════════════════════════════════
IMG=$(doc_tools_image)
info "run $P, app-server $APP, doc-tools $IMG, temp $TMP"
if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  info "building $IMG"
  docker build -q -t "$IMG" "$ROOT/doc-tools" >/dev/null
fi

# Mount the image exactly as compose/doc-tools.yml does. If Docker refuses the
# product's reference, record why and go on with a short alias of the same
# image, so the rest still runs.
MOUNT_IMG=$IMG
MOUNT_ERR=
if ! MOUNT_ERR=$(docker run --rm --name "$P-probe" --label "$LBL" --network none \
  --mount "type=image,source=$IMG,target=/opt/lettuce-doc-tools,readonly" \
  --entrypoint test "$APP" -x /opt/lettuce-doc-tools/bin/pdf-mcp 2>&1); then
  ALIAS_TAG=$P:img
  docker tag "$IMG" "$ALIAS_TAG"
  MOUNT_IMG=$ALIAS_TAG
  # The engine's message names a hex-encoded path; keep the rest.
  MOUNT_ERR=$(grep -v '^WARNING' <<<"$MOUNT_ERR" | head -n1 | sed -E 's/[0-9a-f]{64,}/<hex>/g' | cut -c1-300)
  info "the image mount of $IMG failed; using $ALIAS_TAG: $MOUNT_ERR"
else
  MOUNT_ERR=
fi

info "starting Gotenberg ($P-shared)"
compose_e2e up -d --quiet-pull >/dev/null 2>&1 || compose_e2e up -d
GOT=$(compose_e2e ps -q gotenberg)
[[ -n $GOT ]] || die "Gotenberg did not start"
GOT_ID=$(docker inspect -f '{{.Id}}' "$GOT")
GOT_CG=/sys/fs/cgroup/system.slice/docker-$GOT_ID.scope
[[ -r $GOT_CG/memory.current && -r $GOT_CG/memory.stat ]] || GOT_CG=

for s in "${STACKS[@]}"; do
  d=$TMP/$s
  mkdir -p "$d/work/agent-e2e-$s" "$d/home/mcp-home/.letta" "$d/home/e2e" "$d/data"
  [[ $s != a ]] || mkdir -p "$d/work/agent-e2e-m"
  write_helpers "$d/home/e2e"
  write_settings "$d/home/mcp-home/.letta/settings.json"
  docker network create --label "$LBL" "$P-$s-default" >/dev/null
  docker network create --internal --label "$LBL" "$P-$s-doctools" >/dev/null
  docker run -d --name "$P-$s-app" --label "$LBL" --init --network "$P-$s-default" \
    -e LETTA_LOCAL_BACKEND_EXPERIMENTAL=true -e LETTA_LOCAL_BACKEND_DIR=/data/local-backend -e USER_CWD=/work \
    -v "$d/work:/work" -v "$d/home:/root/.letta" -v "$d/data:/data" \
    --mount "type=image,source=$MOUNT_IMG,target=/opt/lettuce-doc-tools,readonly" \
    -w /work --entrypoint sleep "$APP" infinity >/dev/null 2>"$TMP/run.err" ||
    die "app-server $s did not start: $(cat "$TMP/run.err")"
  docker network connect "$P-$s-doctools" "$P-$s-app"
  docker network connect --alias gotenberg "$P-$s-doctools" "$GOT"
  added=$(docker exec -i "$P-$s-app" node --input-type=module - add /root/.letta/mcp-home/.letta/settings.json 0 0 \
    <"$ROOT/lib/mcp-servers.mjs" | jq -r '.added | join(",")') || die "lib/mcp-servers.mjs failed in $s"
  [[ $added == office,pdf ]] || die "lib/mcp-servers.mjs added '$added' in $s, expected office,pdf"
done

info "waiting for Gotenberg"
for _ in $(seq 60); do
  docker exec "$P-a-app" curl -fsS -m 2 -o /dev/null http://gotenberg:3000/health 2>/dev/null && break
  sleep 1
done
sleep 5
IDLE_START=$(mem_bytes)

# ═════════════════════════════════════════════════════════════════════════════
# Scenarios
# ═════════════════════════════════════════════════════════════════════════════
s0_wiring() {
  local s nets
  for s in "${STACKS[@]}"; do
    expect "$s: the image mount is writable" \
      docker exec "$P-$s-app" sh -c '! touch /opt/lettuce-doc-tools/.w 2>/dev/null' || return 1
    nets=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$P-$s-app")
    expect "$s: app-server networks are '$nets'" test "$nets" = "$P-$s-default $P-$s-doctools " || return 1
    expect "$s: doc-tools network is not internal" \
      test "$(docker network inspect -f '{{.Internal}}' "$P-$s-doctools")" = true || return 1
    expect "$s: gotenberg unreachable" \
      docker exec "$P-$s-app" curl -fsS -m 5 -o /dev/null http://gotenberg:3000/health || return 1
  done
  expect "Docker refused to mount $IMG at /opt/lettuce-doc-tools, as compose/doc-tools.yml does \
(reference + target = ${#IMG} + 22 characters): $MOUNT_ERR" test -z "$MOUNT_ERR" || return 1
}

s1_office() {
  local before
  ok a office create_document "$(create_args brief.docx "$BRIEF_PLAN")" || return 1
  expect "brief.docx not written" test -s "$(ws a)/brief.docx" || return 1

  ok a office edit_document "$(edit_args brief.docx \
    '[{"op":"changeText","target":{"find":"net 60"},"with":"net 30","mode":"Tracked"}]')" || return 1
  zipxml a brief.docx 'word/document\.xml' || return 1
  expect "no tracked insertion and deletion after a Tracked edit" grep -q '<w:ins ' <<<"$XML" || return 1
  expect "no tracked deletion after a Tracked edit" grep -q '<w:del ' <<<"$XML" || return 1
  expect "the new text is missing" has "$(strip_tags "$XML")" "net 30" || return 1

  ok a office edit_document "$(edit_args brief.docx \
    '[{"op":"comment","action":"Add","author":"E2E","initials":"EE","target":{"find":"thirty days"},"text":"Confirm the delivery date."}]')" || return 1
  ok a office edit_document "$(edit_args brief.docx \
    '[{"op":"changeText","target":{"find":"Example Supplies"},"with":"Sample Goods","mode":"Tracked"}]')" || return 1
  zipxml a brief.docx 'word/comments\.xml' || return 1
  expect "the comment was lost by a later edit" has "$(strip_tags "$XML")" "Confirm the delivery date." || return 1
  zipxml a brief.docx 'word/document\.xml' || return 1
  expect "the comment's anchor was lost by a later edit" grep -q 'commentReference' <<<"$XML" || return 1
  expect "the second edit is missing" has "$(strip_tags "$XML")" "Sample Goods" || return 1

  ok a office create_document "$(create_args deck.pptx "$DECK_PLAN")" || return 1
  ok a office edit_document "$(edit_args deck.pptx \
    '[{"op":"changeText","target":{"find":"Paint the fence"},"with":"Fix the gate","mode":"Direct"}]')" || return 1
  zipxml a deck.pptx 'ppt/slides/slide[0-9]+\.xml' || return 1
  local slides
  slides=$(strip_tags "$XML")
  expect "deck: the slide change is missing" has "$slides" "Fix the gate" || return 1
  expect "deck: the old slide text is still there" lacks "$slides" "Paint the fence" || return 1
  expect "deck: the title is missing" has "$slides" "Quarterly Review" || return 1

  ok a office create_document "$(create_args book.xlsx "$BOOK_PLAN")" || return 1
  expect "book.xlsx: the formula was not calculated to 85 on create" \
    test "$(field '.result.calculation.cells[]? | select(.address == "B4") | .value')" = 85 || return 1
  ok a office open_document "$(open_args book.xlsx)" || return 1
  expect "book.xlsx: B4 does not read back as 85" \
    test "$(field '.result.cells[] | select(.anchor.address == "B4") | .displayValue')" = 85 || return 1
  zipxml a book.xlsx 'xl/worksheets/sheet1\.xml' || return 1
  expect "book.xlsx: no cached value 85 in the file" grep -Eq '<(x:)?v>85<' <<<"$XML" || return 1

  before=$(ws_hash a)
  refused a office edit_document "$(edit_args brief.docx \
    '[{"op":"changeText","target":{"paraId":"w14:0BADF00D","expect":"nothing"},"with":"X","mode":"Tracked"}]')" || return 1
  expect "bad anchor: writeOutcome is not notWritten" test "$(field .result.writeOutcome)" = notWritten || return 1
  refused a office edit_document "$(edit_args ../x.docx \
    '[{"op":"changeText","target":{"find":"net"},"with":"X","mode":"Tracked"}]')" || return 1
  refused a office create_document "$(create_args ../x.docx '')" || return 1
  refused a office edit_document "$(edit_args deck.pptx \
    '[{"op":"changeText","target":{"find":"Fix the gate"},"with":"X","mode":"Tracked"}]')" || return 1
  expect "a refused operation changed the workspace" test "$(ws_hash a)" = "$before" || return 1
  expect "a refused operation wrote outside the workspace" test ! -e "$TMP/a/work/x.docx" || return 1
}

s2_pdf() {
  local f stem p pages=0 sizes w
  w=$(ws a)
  for f in brief.docx:"Project brief" deck.pptx:"Quarterly Review" book.xlsx:85; do
    stem=${f%%.*} p=${f#*:} f=${f%%:*}
    ok a pdf pdf_convert "$(jq -nc --arg p "$f" '{path: $p}')" || return 1
    pages=$((pages + $(field .result.pages)))
    pdf_text a "$stem.pdf" || return 1
    expect "$stem.pdf lacks '$p'" has "$TEXT" "$p" || return 1
    ok a pdf pdf_convert "$(jq -nc --arg p "$f" --arg o "$stem-pdfa.pdf" '{path: $p, output: $o, pdfa: "PDF/A-2b"}')" ||
      return 1
    expect "$stem-pdfa.pdf has no PDF/A-2 identification" grep -aq 'pdfaid:part>2<' "$w/$stem-pdfa.pdf" || return 1
    expect "$stem-pdfa.pdf has no PDF/A conformance B" grep -aq 'pdfaid:conformance>B<' "$w/$stem-pdfa.pdf" || return 1
  done

  ok a pdf pdf_pages '{"operation":"merge","paths":["brief.pdf","deck.pdf","book.pdf"],"output":"merged.pdf"}' || return 1
  expect "merged.pdf has $(field .result.pages) pages, expected $pages" test "$(field .result.pages)" = "$pages" || return 1
  ok a pdf pdf_pages '{"operation":"split","paths":["merged.pdf"],"output_dir":"parts"}' || return 1
  expect "split wrote $(field '.result.outputs | length') parts, expected $pages" \
    test "$(field '.result.outputs | length')" = "$pages" || return 1
  expect "split parts missing on disk" test "$(find "$w/parts" -name '*.pdf' | wc -l)" = "$pages" || return 1
  ok a pdf pdf_info '{"path":"merged.pdf"}' || return 1
  sizes=$(jq -c .result.page_sizes <<<"$LAST")
  ok a pdf pdf_pages '{"operation":"rotate","paths":["merged.pdf"],"pages":"1","angle":90,"output":"rotated.pdf"}' || return 1
  ok a pdf pdf_info '{"path":"rotated.pdf"}' || return 1
  expect "rotate did not change any page size ($sizes)" test "$(jq -c .result.page_sizes <<<"$LAST")" != "$sizes" || return 1
  expect "rotate changed the page count" test "$(field .result.page_count)" = "$pages" || return 1

  ok a pdf pdf_stamp '{"operation":"watermark","path":"brief.pdf","output":"watermarked.pdf","text":"E2E SAMPLE"}' || return 1
  pdf_text a watermarked.pdf || return 1
  expect "the watermark text is missing" has "$TEXT" "E2E SAMPLE" || return 1

  ok a pdf pdf_secure '{"operation":"encrypt","path":"brief.pdf","output":"locked.pdf","user_password":"e2e-open-sesame"}' ||
    return 1
  refused a pdf pdf_extract_text '{"path":"locked.pdf"}' || return 1
  refused a pdf pdf_extract_text '{"path":"locked.pdf","password":"wrong"}' || return 1
  pdf_text a locked.pdf e2e-open-sesame || return 1
  expect "locked.pdf does not open with its password" has "$TEXT" "Project brief" || return 1
  expect "locked.pdf is not AES-256 (V5/R6)" grep -aEq '/V ?5' "$w/locked.pdf" || return 1

  pdf_text a brief.pdf || return 1
  ! has "$TEXT" "net 60" || NOTES+=("brief.pdf, converted from a docx with unaccepted tracked changes, \
holds the deleted text too ('net 60' next to 'net 30'): LibreOffice prints pending changes.")
  expect "brief.pdf lacks the term to redact" has "$TEXT" "thirty days" || return 1
  ok a pdf pdf_redact '{"path":"brief.pdf","output":"redacted.pdf","terms":["thirty days"]}' || return 1
  pdf_text a redacted.pdf || return 1
  expect "the redacted term is still in the text" lacks "$TEXT" "thirty days" || return 1
  expect "redaction removed more than the term" has "$TEXT" "Project brief" || return 1

  ok a pdf pdf_render_pages '{"path":"brief.pdf","output_dir":"shots","pages":"1","dpi":60}' || return 1
  expect "no PNG rendered" test "$(head -c4 "$w/shots/brief-p1.png" | od -An -c | tr -d ' ')" = '211PNG' || return 1
}

# Where an app-server keeps anything: its bind mounts and /tmp.
SEARCH=(/work /root/.letta /data /tmp)
s3_isolation() {
  local wa wb name ip net
  wa=$(ws a) wb=$(ws b)
  ok b office create_document "$(create_args b-note.docx "$(marker_plan "Note kept by stack b")")" || return 1
  ok b pdf pdf_convert '{"path":"b-note.docx"}' || return 1
  for name in brief.docx deck.pptx book.xlsx merged.pdf; do
    expect "b sees a's $name" test -z "$(docker exec "$P-b-app" find "${SEARCH[@]}" -name "$name" 2>/dev/null)" || return 1
  done
  expect "a sees b's b-note.docx" test -z "$(docker exec "$P-a-app" find "${SEARCH[@]}" -name b-note.docx 2>/dev/null)" ||
    return 1
  expect "b's workspace holds a file of a" test ! -e "$wb/brief.docx" || return 1
  expect "a's workspace holds a file of b" test ! -e "$wa/b-note.docx" || return 1

  docker exec -d "$P-a-app" node -e \
    'require("http").createServer((q, r) => r.end("e2e-listener")).listen(8099, "0.0.0.0")'
  sleep 1
  for net in "$P-a-default" "$P-a-doctools"; do
    ip=$(ip_of "$P-a-app" "$net")
    expect "control: a cannot reach its own listener on $net ($ip)" \
      test "$(docker exec "$P-a-app" curl -s -m 3 "http://$ip:8099/")" = e2e-listener || return 1
    expect "b reached a's app-server on $net ($ip)" \
      docker exec "$P-b-app" sh -c "! curl -s -m 3 -o /dev/null http://$ip:8099/" || return 1
  done
  ip=$(ip_of "$P-a-app" "$P-a-doctools")
  expect "control: Gotenberg cannot reach a on a's doc-tools network ($ip)" \
    docker exec "$GOT" curl -fsS -m 3 -o /dev/null "http://$ip:8099/" || return 1
  expect "b cannot reach gotenberg" docker exec "$P-b-app" curl -fsS -m 5 -o /dev/null http://gotenberg:3000/health ||
    return 1
  expect "a cannot reach gotenberg" docker exec "$P-a-app" curl -fsS -m 5 -o /dev/null http://gotenberg:3000/health ||
    return 1

  docker exec "$P-b-app" sh -c 'mkdir -p /work/agent-e2e-other && cp /work/agent-e2e-b/b-note.pdf /work/agent-e2e-other/secret.pdf &&
    cp /work/agent-e2e-b/b-note.pdf /tmp/outside.pdf &&
    ln -sfn /work/agent-e2e-other/secret.pdf /work/agent-e2e-b/link.pdf &&
    ln -sfn /work/agent-e2e-other /work/agent-e2e-b/dirlink'
  ok b pdf pdf_info '{"path":"b-note.pdf"}' || return 1
  local p
  for p in ../agent-e2e-other/secret.pdf /work/agent-e2e-other/secret.pdf /tmp/outside.pdf link.pdf dirlink/secret.pdf; do
    refused b pdf pdf_info "$(jq -nc --arg p "$p" '{path: $p}')" || return 1
  done
  refused b pdf pdf_create '{"output":"../agent-e2e-other/written.pdf","text":"should not exist"}' || return 1
  refused b pdf pdf_create '{"output":"dirlink/written.pdf","text":"should not exist"}' || return 1
  expect "pdf_create wrote outside the workspace" \
    docker exec "$P-b-app" sh -c '! ls /work/agent-e2e-other/written.pdf 2>/dev/null' || return 1
}

s4_concurrency() {
  local s i pid pids=() out text other
  for s in a b; do
    for i in 1 2 3; do
      ok "$s" office create_document "$(create_args "conc-$i.docx" "$(marker_plan "Marker CONC-$s-$i-$R")")" || return 1
    done
  done
  local t0=$SECONDS
  for s in a b; do
    for i in 1 2 3; do
      (call "$s" pdf pdf_convert "{\"path\":\"conc-$i.docx\"}" && printf '%s\n' "$LAST" >"$TMP/out/conc-$s-$i.json") &
      pids+=($!)
    done
  done
  for pid in "${pids[@]}"; do wait "$pid" || true; done
  info "   6 concurrent conversions in $((SECONDS - t0)) s"
  for s in a b; do
    for i in 1 2 3; do
      out=$(cat "$TMP/out/conc-$s-$i.json" 2>/dev/null || true)
      [[ -n $out ]] || out='{"isError":true,"text":"no result"}'
      expect "$s conc-$i.docx: conversion failed: $(brief "$out")" succeeded "$out" || return 1
      pdf_text "$s" "conc-$i.pdf" || return 1
      text=$TEXT
      expect "$s conc-$i.pdf lacks its own marker" has "$text" "CONC-$s-$i-$R" || return 1
      for other in a-1 a-2 a-3 b-1 b-2 b-3; do
        [[ $other == "$s-$i" ]] && continue
        expect "$s conc-$i.pdf holds another document's marker CONC-$other" lacks "$text" "CONC-$other-$R" || return 1
      done
    done
  done
  expect "two concurrent outputs are identical" \
    test "$(sha256sum "$(ws a)"/conc-?.pdf "$(ws b)"/conc-?.pdf | cut -d' ' -f1 | sort -u | wc -l)" = 6 || return 1
}

s5_restart_remove() {
  local pid
  (call a pdf pdf_convert '{"path":"conc-1.docx","output":"during-restart.pdf"}' &&
    printf '%s\n' "$LAST" >"$TMP/out/during.json") &
  pid=$!
  docker restart -t 2 "$P-b-app" >/dev/null
  wait "$pid" || true
  expect "a's conversion during b's restart failed: $(brief "$(cat "$TMP/out/during.json" 2>/dev/null || echo '{}')")" \
    succeeded "$(cat "$TMP/out/during.json" 2>/dev/null || echo '{}')" || return 1
  ok a pdf pdf_convert '{"path":"conc-2.docx","output":"after-restart.pdf"}' || return 1
  ok b pdf pdf_convert '{"path":"b-note.docx","output":"b-after-restart.pdf"}' || return 1
  pdf_text b b-after-restart.pdf || return 1
  expect "b's output after its restart lacks its text" has "$TEXT" "Note kept by stack b" || return 1

  # What `lettucectl remove` does for the doc-tools part.
  docker network disconnect -f "$P-b-doctools" "$GOT"
  docker rm -f "$P-b-app" >/dev/null
  docker network rm "$P-b-doctools" "$P-b-default" >/dev/null
  expect "Gotenberg is still on b's network" \
    lacks "$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$GOT")" "$P-b-" || return 1
  ok a pdf pdf_convert '{"path":"conc-3.docx","output":"after-remove.pdf"}' || return 1
  pdf_text a after-remove.pdf || return 1
  expect "a's output after b's removal lacks its marker" has "$TEXT" "CONC-a-3-$R" || return 1
}

s6_persistence() {
  local f before
  f=$TMP/a/home/mcp-home/.letta/settings.json
  before=$(sha256sum <"$f")
  docker restart -t 2 "$P-a-app" >/dev/null
  ok a office list_connections '{}' || return 1
  expect "office lost its workspace connection" test "$(field '.result | tostring | contains("workspace")')" = true ||
    return 1
  ok a office open_document "$(open_args brief.docx)" || return 1
  ok a pdf pdf_info '{"path":"brief.pdf"}' || return 1
  ok a pdf pdf_convert '{"path":"brief.docx","output":"after-a-restart.pdf"}' || return 1
  expect "settings.json changed" test "$(sha256sum <"$f")" = "$before" || return 1
}

# ═════════════════════════════════════════════════════════════════════════════
# Measurements
# ═════════════════════════════════════════════════════════════════════════════
measure_conversions() {
  local s t0 pid pids=() okn=0 n=0 f peak
  info ""
  info "== measurements: conversions"
  for s in "${STACKS[@]}"; do
    ok "$s" office create_document "$(create_args m.docx "$BRIEF_PLAN")" || info "   $s m.docx: $FAIL_REASON"
    ok "$s" office create_document "$(create_args m.pptx "$(deck_plan 5)")" || info "   $s m.pptx: $FAIL_REASON"
    ok "$s" office create_document "$(create_args m.xlsx "$(book_plan 50)")" || info "   $s m.xlsx: $FAIL_REASON"
  done

  sampler_start
  call a pdf pdf_convert '{"path":"m.docx","output":"warm.pdf"}'
  peak=$(sampler_stop)
  mrow "First conversion after Gotenberg start (LibreOffice cold start), docx" \
    "$(field .ms) ms" "peak $(mib "$peak"); $(outcome "$LAST")"

  run_batch() { # run_batch PHASE STACK... -> converts m.docx, m.pptx, m.xlsx in each stack at once
    local phase=$1 s f
    shift
    pids=() n=0 okn=0
    for s in "$@"; do
      for f in docx pptx xlsx; do
        (call "$s" pdf pdf_convert "{\"path\":\"m.$f\",\"output\":\"$phase-$f.pdf\",\"overwrite\":true}" &&
          printf '%s\n' "$LAST" >"$TMP/out/$phase-$s-$f.json") &
        pids+=($!)
        n=$((n + 1))
      done
    done
    for pid in "${pids[@]}"; do wait "$pid" || true; done
    for s in "$@"; do
      for f in docx pptx xlsx; do
        if succeeded "$(cat "$TMP/out/$phase-$s-$f.json" 2>/dev/null || echo '{}')"; then
          okn=$((okn + 1))
        else
          info "   $phase $s m.$f: $(brief "$(cat "$TMP/out/$phase-$s-$f.json" 2>/dev/null || echo '{}')")"
        fi
      done
    done
  }

  sampler_start
  t0=$(date +%s%N)
  run_batch one a
  peak=$(sampler_stop)
  mrow "Gotenberg peak RAM, 1 stack converting (3 files at once)" "$(mib "$peak")" \
    "$okn/$n ok in $((($(date +%s%N) - t0) / 1000000)) ms"
  sampler_start
  t0=$(date +%s%N)
  run_batch four "${STACKS[@]}"
  peak=$(sampler_stop)
  mrow "Gotenberg peak RAM, 4 stacks converting (12 files at once)" "$(mib "$peak")" \
    "$okn/$n ok in $((($(date +%s%N) - t0) / 1000000)) ms"
  [[ $okn == "$n" ]] || CONVERSION_NOTE="$((n - okn)) of $n conversions failed with 4 stacks converting at once"
}
CONVERSION_NOTE=

measure_calls() {
  local m=agent-e2e-m
  info ""
  info "== measurements: per call (stack a, workspace $m)"
  docker exec "$P-a-app" rm -f /tmp/doctools-e2e-measure.jsonl
  mcall() { # mcall SERVER TOOL JSON [LABEL]
    local label=${4:-$2} line
    call a "$1" "$2" "$3" "$m"
    line=$(docker exec "$P-a-app" tail -n1 /tmp/doctools-e2e-measure.jsonl 2>/dev/null || echo '{}')
    CALLROWS+=("| ${1%m} | $label | $(field .ms) | $(jq -r '.ms // "?"' <<<"$line") | $(jq -r 'if .maxrss_kb then (.maxrss_kb / 1024 | floor) else "?" end' <<<"$line") | $(outcome "$LAST") |")
    docker exec "$P-a-app" sh -c ': >/tmp/doctools-e2e-measure.jsonl'
  }
  mcall officem describe_capabilities '{}'
  mcall officem create_document "$(create_args brief.docx "$BRIEF_PLAN")" "create_document (docx, 4 paragraphs)"
  mcall officem edit_document "$(edit_args brief.docx '[{"op":"changeText","target":{"find":"net 60"},"with":"net 30","mode":"Tracked"}]')" \
    "edit_document (docx, tracked)"
  mcall officem open_document "$(open_args brief.docx)" "open_document (docx)"
  mcall officem create_document "$(create_args deck.pptx "$(deck_plan 10)")" "create_document (pptx, 10 slides)"
  mcall officem create_document "$(create_args book.xlsx "$(book_plan 100)")" "create_document (xlsx, 100 values + 100 formulas)"
  mcall pdfm pdf_convert '{"path":"brief.docx"}' "pdf_convert (docx)"
  mcall pdfm pdf_convert '{"path":"deck.pptx","pdfa":"PDF/A-2b"}' "pdf_convert (pptx, PDF/A-2b)"
  mcall pdfm pdf_convert '{"path":"book.xlsx"}' "pdf_convert (xlsx)"
  mcall pdfm pdf_info '{"path":"deck.pdf"}'
  mcall pdfm pdf_extract_text '{"path":"deck.pdf"}'
  mcall pdfm pdf_create '{"output":"note.pdf","text":"A short note.\n\nSecond paragraph."}' "pdf_create (text)"
  mcall pdfm pdf_pages '{"operation":"merge","paths":["brief.pdf","deck.pdf","book.pdf"],"output":"merged.pdf"}' "pdf_pages merge"
  mcall pdfm pdf_pages '{"operation":"split","paths":["merged.pdf"],"output_dir":"parts"}' "pdf_pages split"
  mcall pdfm pdf_stamp '{"operation":"watermark","path":"merged.pdf","output":"wm.pdf","text":"SAMPLE"}' "pdf_stamp watermark"
  mcall pdfm pdf_redact '{"path":"brief.pdf","output":"red.pdf","terms":["thirty days"]}' "pdf_redact"
  mcall pdfm pdf_secure '{"operation":"encrypt","path":"brief.pdf","output":"enc.pdf","user_password":"e2e"}' "pdf_secure encrypt"
  mcall pdfm pdf_render_pages '{"path":"deck.pdf","output_dir":"shots","pages":"1-3","dpi":110}' "pdf_render_pages (3 pages, 110 dpi)"
}

measure_disk() {
  local s w docs idx rw tmpb
  for s in a c; do
    w=$(ws "$s")
    docs=$(du -sb --exclude=.officeagent "$w" | cut -f1)
    idx=$(du -sb "$w/.officeagent" 2>/dev/null | cut -f1)
    rw=$(docker container inspect --size -f '{{.SizeRw}}' "$P-$s-app")
    tmpb=$(docker exec "$P-$s-app" du -sb /tmp/lettuce-doc-tools | cut -f1)
    mrow "Disk, stack $s: documents in the workspace / .officeagent index" "$docs B / ${idx:-0} B" \
      "the documents the tests made"
    mrow "Disk, stack $s: container layer (SizeRw) / of which /tmp/lettuce-doc-tools" "$rw B / $tmpb B" \
      "the image mount adds nothing per stack"
  done
  mrow "Disk, per stack: MCP settings (mcp-home)" "$(stat -c %s "$TMP/a/home/mcp-home/.letta/settings.json") B" \
    "unchanged by calls"
}

measure_images() {
  local img
  for img in "$IMG" "$(compose_e2e config --images | head -n1)"; do
    mrow "Image $img" "$(docker images --format '{{.Size}}' "${img%@*}" | head -n1) on disk" \
      "content $(mib "$(docker image inspect -f '{{.Size}}' "${img%@*}")")"
  done
}

# ═════════════════════════════════════════════════════════════════════════════
mrow "Gotenberg idle RAM after start (engines not started)" "$(mib "$IDLE_START")" ""
measure_conversions
measure_calls
run_scenario "0. Wiring: image mount, networks, gotenberg alias" s0_wiring
run_scenario "1. Office in a: docx tracked edit and comment, pptx, xlsx formula, invalid operations" s1_office
run_scenario "2. PDF in a: convert, PDF/A-2b, merge, split, rotate, watermark, encrypt, redact, render" s2_pdf
run_scenario "3. Isolation between a and b: files, networks, gotenberg, pdf-mcp jail" s3_isolation
run_scenario "4. Concurrency: 3 conversions each in a and b at once" s4_concurrency
run_scenario "5. Restart b while a converts; remove b; a still converts" s5_restart_remove
run_scenario "6. Registration survives an app-server restart" s6_persistence
measure_images
measure_disk
if ((IDLE_WAIT)); then
  info ""
  info "== waiting 90 s for Gotenberg's idle shutdown (60 s)"
  sleep 90
  mrow "Gotenberg idle RAM 90 s after the last job" "$(mib "$(mem_bytes)")" "engines stop after 60 s idle"
fi
[[ -z $CONVERSION_NOTE ]] || NOTES+=("$CONVERSION_NOTE")

report() {
  echo "## doc-tools E2E ($P)"
  echo
  echo "App-server image \`$APP\`, doc-tools image \`$IMG\`$([[ -z $ALIAS_TAG ]] || echo " (mounted through the alias \`$ALIAS_TAG\`)")."
  echo
  echo "| Result | Scenario | Time | Reason |"
  echo "|---|---|---|---|"
  printf '| %s\n' "${RESULTS[@]}"
  echo
  echo "| Measurement | Value | Notes |"
  echo "|---|---|---|"
  printf '%s\n' "${MROWS[@]}"
  echo
  echo "Per call, each through \`letta mcp call\` (wall includes the letta CLI, about 0.6 s; server = the server process's lifetime):"
  echo
  echo "| Server | Call | Wall ms | Server ms | Peak RSS MiB | Result |"
  echo "|---|---|---|---|---|---|"
  printf '%s\n' "${CALLROWS[@]}"
  if ((${#NOTES[@]})); then
    echo
    echo "Observations:"
    echo
    printf -- '- %s\n' "${NOTES[@]}"
  fi
}
info ""
report
[[ -z $REPORT ]] || report >"$REPORT"
exit "$FAILED"
