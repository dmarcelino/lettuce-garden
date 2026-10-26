#!/usr/bin/env bash
# Offline tests for lettucectl. docker, curl and dig are replaced by the stubs in
# tests/stubs, so nothing here touches Docker, DNS or Cloudflare.
#   tests/run.sh            run everything
#   tests/run.sh <pattern>  run the tests whose name contains <pattern>
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
FILTER=${1:-}
PASS=0 FAILED=()

# ── fixture ──────────────────────────────────────────────────────────────────
# Each test gets a fresh copy of the repo in $T/home and the stubs in $T/bin.
setup() {
  T=$(mktemp -d)
  H=$T/home
  mkdir -p "$H/upstream/docker" "$H/.secrets" "$T/bin"
  cp -r "$ROOT/lettucectl" "$ROOT/lib" "$ROOT/compose" "$ROOT/backup" "$ROOT/config.env.example" "$H/"
  cp "$ROOT"/tests/stubs/* "$T/bin/"
  touch "$H/upstream/docker/compose.yml"
  printf 'secret-api-token-123\n' >"$H/.secrets/cloudflare-api-token"
  chmod 600 "$H/.secrets/cloudflare-api-token"
  sed -e 's/^DOMAIN=.*/DOMAIN=example.org/' -e 's/^PROFILES=.*/PROFILES=cloudflared,search/' \
    -e 's/^CF_ACCOUNT_ID=.*/CF_ACCOUNT_ID=acc1/' -e 's/^CF_ZONE_ID=.*/CF_ZONE_ID=zone1/' \
    -e 's/^CF_TEAM_DOMAIN=.*/CF_TEAM_DOMAIN=team/' -e 's/^CF_GOOGLE_IDP_ID=.*/CF_GOOGLE_IDP_ID=idp-g/' \
    -e 's/^PUSH_CONTACT_EMAIL=.*/PUSH_CONTACT_EMAIL=ops@example.org/' \
    "$ROOT/config.env.example" >"$H/config.env"
  chmod 600 "$H/config.env"
  echo '{"services":{"app-server":{"ports":[],"cap_drop":["NET_RAW"],"environment":{"DISABLE_AUTOUPDATER":"1"}},"bff":{"network_mode":"service:app-server","cap_drop":["NET_RAW"]}}}' >"$T/compose-config.json"
  touch "$T/calls.log" "$T/argv.log" "$T/stdin.log" "$T/stub.env" "$T/containers.txt"
  OUT=$T/out
}

teardown() { rm -rf "$T"; }

# ctl ARGS... -> runs lettucectl with the stubs first on PATH; output in $OUT, status in $RC
ctl() {
  RC=0
  PATH="$T/bin:$PATH" "$H/lettucectl" "$@" >"$OUT" 2>&1 || RC=$?
}

stub() { printf '%s\n' "$*" >>"$T/stub.env"; }
calls() { grep "^curl " "$T/calls.log" | cut -d' ' -f2,3 | sed 's/?.*//'; }
body_of() { grep "^curl $1 $2 " "$T/calls.log" | tail -1 | cut -d' ' -f4-; }
senv() { awk -v k="$2" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END { printf "%s", v }' "$H/stacks/$1/stack.env"; }

fail() {
  printf '    %s\n' "$*"
  printf '    --- output ---\n'
  sed 's/^/    /' "$OUT" 2>/dev/null | tail -15
  return 1
}
ok_rc() { [[ $RC == 0 ]] || fail "expected success, got $RC"; }
bad_rc() { [[ $RC != 0 ]] || fail "expected failure, got success"; }
out_has() { grep -qF -- "$1" "$OUT" || fail "output lacks: $1"; }
out_lacks() { ! grep -qF -- "$1" "$OUT" || fail "output unexpectedly has: $1"; }
log_has() { grep -qF -- "$1" "$T/calls.log" || fail "call log lacks: $1"; }
log_lacks() { ! grep -qF -- "$1" "$T/calls.log" || fail "call log unexpectedly has: $1"; }
eq() { [[ $1 == "$2" ]] || fail "expected [$2], got [$1]"; }

# broker_config [BROKER_JSON] -> the rendered compose config also holds this
# secret-broker (default: the hardened one compose/secrets.yml renders)
broker_config() {
  local broker=${1:-'{"read_only":true,"cap_drop":["ALL"],"volumes":[{"type":"volume","source":"secret-broker-data","target":"/data"}]}'}
  jq -c --argjson b "$broker" '.services["secret-broker"] = $b' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
}

# config_edit JQ -> applies the jq filter JQ to the rendered compose config
config_edit() {
  jq -c "$1" "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
}

# triggers_config -> the rendered config also holds the gmail-triggers service
# compose/gmail-triggers.yml renders for alfred, and the services upstream gives
# google-creds; the repo copy has an executor
triggers_config() {
  config_edit '.services["gmail-triggers"] = {"read_only":true,"cap_drop":["ALL"],"user":"0","tmpfs":["/tmp"],"volumes":[
      {"type":"bind","source":"'"$H"'/stacks/alfred/state/garden/gmail-triggers","target":"/data"},
      {"type":"volume","source":"google-creds","target":"/creds","read_only":true},
      {"type":"volume","source":"google-policy","target":"/policy","read_only":true}]}
    | .services.bff.volumes = [{"type":"volume","source":"google-creds","target":"/app/google/creds"}]
    | .services["google-mcp"] = {"volumes":[{"type":"volume","source":"google-creds","target":"/creds"}]}'
  mkdir -p "$H/gmail-triggers/executor/tests"
  printf '// executor\n' >"$H/gmail-triggers/executor/run.mjs"
  printf '// test\n' >"$H/gmail-triggers/executor/tests/run.test.mjs"
}

run_test() {
  local name=$1
  [[ -z $FILTER || $name == *"$FILTER"* ]] || return 0
  setup
  local rc
  # A bare statement on purpose: inside `if`, `||` or `&&` bash ignores errexit
  # in the subshell too, and every assert relies on it to stop the test.
  (set -e; "$name")
  rc=$?
  if ((rc == 0)); then
    PASS=$((PASS + 1))
    printf 'ok    %s\n' "$name"
  else
    FAILED+=("$name")
    printf 'FAIL  %s\n' "$name"
  fi
  teardown
}

# ── validation ───────────────────────────────────────────────────────────────
test_add_rejects_bad_names() {
  local n
  for n in Alfred 1abc a_b trail- abcdefghijklmnopqrstu ""; do
    ctl add "$n" a@b.com
    bad_rc
    out_has "invalid stack name"
  done
  log_lacks "curl "
}

test_add_rejects_bad_email() {
  ctl add alfred not-an-email
  bad_rc
  out_has "invalid email address"
  [[ ! -e $H/stacks/alfred ]] || fail "stack dir created for a rejected add"
}

test_add_requires_init() {
  sed -i 's/^CF_ZONE_ID=.*/CF_ZONE_ID=/' "$H/config.env"
  ctl add alfred a@b.com
  bad_rc
  out_has "lettucectl init"
}

# ── init ─────────────────────────────────────────────────────────────────────
make_upstream_repo() {
  local src=$T/src
  git init -q "$src"
  mkdir -p "$src/docker"
  touch "$src/docker/compose.yml"
  git -C "$src" add -A
  git -C "$src" -c user.name=t -c user.email=t@t commit -qm init
  git -C "$src" tag v-test
  rm -rf "$H/upstream"
  sed -i -e "s|^LETTUCE_REPO=.*|LETTUCE_REPO=$src|" -e 's/^LETTUCE_REF=.*/LETTUCE_REF=v-test/' "$H/config.env"
}

test_init_creates_config_from_example() {
  rm "$H/config.env"
  ctl init
  bad_rc
  out_has "edit"
  [[ -f $H/config.env ]] || fail "config.env not created"
  eq "$(stat -c %a "$H/config.env")" 600
}

test_init_clones_and_caches_cloudflare_ids() {
  make_upstream_repo
  sed -i -e 's/^CF_\([A-Z_]*\)=.*/CF_\1=/' "$H/config.env"
  ctl init
  ok_rc
  [[ -f $H/upstream/docker/compose.yml ]] || fail "upstream not cloned"
  eq "$(git -C "$H/upstream" describe --tags)" v-test
  eq "$(grep ^CF_ACCOUNT_ID= "$H/config.env")" CF_ACCOUNT_ID=acc1
  eq "$(grep ^CF_ZONE_ID= "$H/config.env")" CF_ZONE_ID=zone1
  eq "$(grep ^CF_TEAM_DOMAIN= "$H/config.env")" CF_TEAM_DOMAIN=team
  eq "$(grep ^CF_GOOGLE_IDP_ID= "$H/config.env")" CF_GOOGLE_IDP_ID=idp-g
}

test_init_never_puts_the_api_token_in_argv() {
  make_upstream_repo
  ctl init
  ok_rc
  ! grep -q secret-api-token-123 "$T/argv.log" || fail "API token found in curl argv"
  grep -q 'Bearer secret-api-token-123' "$T/stdin.log" || fail "API token not sent via curl config on stdin"
}

test_init_fails_without_google_login_method() {
  make_upstream_repo
  stub STUB_CF_NO_GOOGLE=1
  ctl init
  bad_rc
  out_has "Google"
}

test_init_fails_when_zone_not_found() {
  make_upstream_repo
  stub STUB_CF_NO_ZONE=1
  ctl init
  bad_rc
  out_has "example.org"
}

test_init_rejects_world_readable_token() {
  make_upstream_repo
  chmod 644 "$H/.secrets/cloudflare-api-token"
  ctl init
  bad_rc
  out_has "600"
}

test_init_rejects_old_compose() {
  make_upstream_repo
  stub STUB_COMPOSE_VERSION=2.20.0
  ctl init
  bad_rc
  out_has "2.24"
}

# ── add ──────────────────────────────────────────────────────────────────────
test_add_creates_everything_in_order() {
  ctl add alfred Alfred@Example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "GET /zones/zone1/dns_records;POST /accounts/acc1/access/policies;POST /accounts/acc1/access/apps;POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;"
  log_has "compose -p lettuce-alfred"
  log_has "up -d --build"
  out_has "https://alfred.example.org"
}

test_add_payloads() {
  ctl add alfred Alfred@Example.com,hal@example.com
  ok_rc
  local b
  b=$(body_of POST /accounts/acc1/access/policies)
  eq "$(jq -c '[.decision, [.include[].email.email]]' <<<"$b")" '["allow",["alfred@example.com","hal@example.com"]]'
  b=$(body_of POST /accounts/acc1/access/apps)
  eq "$(jq -c '[.type, .domain, .destinations[0].uri, .allowed_idps, .auto_redirect_to_identity, .policies]' <<<"$b")" \
    '["self_hosted","alfred.example.org","alfred.example.org",["idp-g"],true,[{"id":"pol1","precedence":1}]]'
  b=$(body_of POST /accounts/acc1/cfd_tunnel)
  eq "$(jq -c '[.name, .config_src]' <<<"$b")" '["lettuce-alfred","cloudflare"]'
  b=$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)
  eq "$(jq -c '.config.ingress' <<<"$b")" '[{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}]'
  b=$(body_of POST /zones/zone1/dns_records)
  eq "$(jq -c '[.type, .name, .content, .proxied]' <<<"$b")" '["CNAME","alfred.example.org","tun1.cfargotunnel.com",true]'
}

test_add_writes_private_stack_env() {
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
  eq "$(stat -c %a "$H/stacks/alfred")" 700
  eq "$(senv alfred PUBLIC_ORIGIN)" https://alfred.example.org
  eq "$(senv alfred ALLOWED_USERS)" alfred@example.com
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  eq "$(senv alfred LETTA_STATE_DIR)" "$H/stacks/alfred/state"
  eq "$(senv alfred CF_ACCESS_TEAM_DOMAIN)" team
  eq "$(senv alfred CF_ACCESS_AUD)" aud1
  eq "$(senv alfred CLOUDFLARE_TUNNEL_TOKEN)" tuntok1
  eq "$(senv alfred TZ)" UTC
  local s
  s=$(senv alfred SESSION_SECRET)
  ((${#s} == 64)) || fail "SESSION_SECRET length ${#s}"
  s=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  ((${#s} == 87)) || fail "PUSH_VAPID_PUBLIC_KEY length ${#s}"
  s=$(senv alfred PUSH_VAPID_PRIVATE_KEY)
  ((${#s} == 43)) || fail "PUSH_VAPID_PRIVATE_KEY length ${#s}"
  eq "$(senv alfred PUSH_VAPID_CONTACT_EMAIL)" ops@example.org
  s=$(senv alfred BROKER_VAPID_PUBLIC_KEY)
  ((${#s} == 87)) || fail "BROKER_VAPID_PUBLIC_KEY length ${#s}"
  s=$(senv alfred BROKER_VAPID_PRIVATE_KEY)
  ((${#s} == 43)) || fail "BROKER_VAPID_PRIVATE_KEY length ${#s}"
  [[ $(senv alfred BROKER_VAPID_PRIVATE_KEY) != "$(senv alfred PUSH_VAPID_PRIVATE_KEY)" ]] || fail "broker shares the BFF's VAPID key"
  ! grep -q '^DEV_BYPASS' "$H/stacks/alfred/stack.env" || fail "DEV_BYPASS in stack.env"
}

test_add_secrets_differ_between_stacks() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  [[ $(senv alfred SESSION_SECRET) != "$(senv hal SESSION_SECRET)" ]] || fail "same SESSION_SECRET"
  [[ $(senv alfred PUSH_VAPID_PRIVATE_KEY) != "$(senv hal PUSH_VAPID_PRIVATE_KEY)" ]] || fail "same VAPID key"
}

test_add_routes_the_secrets_path_with_the_profile() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(jq -c '.config.ingress' <<<"$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)")" \
    '[{"hostname":"alfred.example.org","path":"^/garden/secrets(/|$)","service":"http://secret-broker:8000"},{"hostname":"alfred.example.org","path":"^/garden/?$","service":"http://secret-broker:8000"},{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}]'
}

test_add_routes_gmail_triggers_with_the_profile() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers,secrets/' "$H/config.env"
  broker_config
  triggers_config
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(jq -c '.config.ingress' <<<"$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)")" \
    '[{"hostname":"alfred.example.org","path":"^/garden/secrets(/|$)","service":"http://secret-broker:8000"},{"hostname":"alfred.example.org","path":"^/garden/gmail-triggers(/|$)","service":"http://gmail-triggers:8000"},{"hostname":"alfred.example.org","path":"^/garden/?$","service":"http://secret-broker:8000"},{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}]'
}

# The landing page links each garden profile to /garden/<profile>/, so every
# PROFILE_ROUTES row must route that path to its sidecar.
test_garden_routes_follow_the_profile_names() {
  local r p path svc
  # shellcheck source=/dev/null
  source <(sed -n '/^PROFILE_ROUTES=(/,/^)/p' "$ROOT/lib/cloudflare.sh")
  ((${#PROFILE_ROUTES[@]} > 0)) || fail "no PROFILE_ROUTES"
  for r in "${PROFILE_ROUTES[@]}"; do
    read -r p path svc <<<"$r"
    eq "$path" "^/garden/$p(/|\$)"
  done
}

test_up_gives_the_sidecars_only_the_garden_profiles() {
  added
  ctl up alfred
  ok_rc
  eq "$(senv alfred GARDEN_PROFILES)" ""
  with_triggers
  broker_config
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers,secrets/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google,gmail-triggers,secrets/' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  # In PROFILE_ROUTES order, which is the landing page's.
  eq "$(senv alfred GARDEN_PROFILES)" secrets,gmail-triggers
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
}

# A profile that adds no garden page leaves GARDEN_PROFILES, and so the
# sidecars' config, alone: Compose would recreate them, and the vault relock.
test_profiles_leave_garden_profiles_alone_for_other_profiles() {
  broker_config
  added
  ctl profiles cloudflared,search,secrets
  ok_rc
  eq "$(senv alfred GARDEN_PROFILES)" secrets
  ctl profiles cloudflared,secrets
  ok_rc
  eq "$(senv alfred GARDEN_PROFILES)" secrets
  ctl profiles cloudflared,search
  ok_rc
  eq "$(senv alfred GARDEN_PROFILES)" ""
}

test_add_falls_back_when_name_resolves() {
  echo "alfred.example.org A 1.2.3.4" >"$T/dns.txt"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(senv alfred PUBLIC_ORIGIN)" https://lettuce-alfred.example.org
}

test_add_falls_back_when_name_is_in_the_zone() {
  stub STUB_CF_TAKEN=alfred.example.org
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(senv alfred PUBLIC_ORIGIN)" https://lettuce-alfred.example.org
}

test_add_fails_when_both_names_taken() {
  printf '%s\n' "mail.example.org CNAME mail.example.net." "lettuce-mail.example.org TXT x" >"$T/dns.txt"
  ctl add mail a@b.com
  bad_rc
  out_has "lettuce-mail.example.org"
  [[ ! -e $H/stacks/mail ]] || fail "stack dir left behind"
}

test_add_warns_about_wildcard_records() {
  stub STUB_DNS_WILDCARD=5.6.7.8
  ctl add alfred alfred@example.com
  ok_rc
  out_has "wildcard"
  eq "$(senv alfred PUBLIC_ORIGIN)" https://alfred.example.org
}

test_add_resumes_after_a_failure() {
  stub 'STUB_CF_FAIL="POST /accounts/acc1/cfd_tunnel"'
  ctl add alfred alfred@example.com
  bad_rc
  out_has "stubbed failure"
  out_has "lettucectl add alfred"
  eq "$(senv alfred CF_ACCESS_APP_ID)" app1
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;"
}

test_add_prints_the_google_redirect_uri() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  ctl add alfred alfred@example.com
  ok_rc
  out_has "https://alfred.example.org/api/google/oauth/callback"
}

test_add_keeps_the_google_hint_when_the_executor_cannot_be_installed() {
  triggers_config
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  stub STUB_EXEC_RC=1
  ctl add alfred alfred@example.com
  bad_rc
  out_has "https://alfred.example.org/api/google/oauth/callback"
  out_has "the gmail-triggers executor is out of step on: alfred; fix the cause, then run: lettucectl up <name>"
  out_lacks "lettucectl add alfred"
}

test_add_skips_the_google_hint_without_the_profile() {
  ctl add alfred alfred@example.com
  ok_rc
  out_lacks "oauth/callback"
}

test_add_refuses_an_existing_stack() {
  ctl add alfred alfred@example.com
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  bad_rc
  out_has "already exists"
  log_lacks "curl "
}

# ── check / up ───────────────────────────────────────────────────────────────
added() {
  ctl add alfred alfred@example.com
  ok_rc
  : >"$T/calls.log"
}

test_up_refuses_dev_bypass() {
  added
  echo "DEV_BYPASS_EMAIL=x@y.com" >>"$H/stacks/alfred/stack.env"
  ctl up alfred
  bad_rc
  out_has "DEV_BYPASS"
  log_lacks "up -d"
}

test_up_refuses_empty_tunnel_token() {
  added
  sed -i 's/^CLOUDFLARE_TUNNEL_TOKEN=.*/CLOUDFLARE_TUNNEL_TOKEN=/' "$H/stacks/alfred/stack.env"
  ctl up alfred
  bad_rc
  out_has "CLOUDFLARE_TUNNEL_TOKEN"
  log_lacks "up -d"
}

test_up_refuses_profile_drift() {
  added
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,search,codex/' "$H/config.env"
  ctl up alfred
  bad_rc
  out_has "differs"
  log_lacks "up -d"
}

test_up_refuses_published_ports() {
  added
  echo '{"services":{"app-server":{"ports":[{"target":8080,"published":"8090"}]}}}' >"$T/compose-config.json"
  ctl up alfred
  bad_rc
  out_has "published port"
  log_lacks "up -d"
}

test_up_refuses_auto_update() {
  added
  echo '{"services":{"app-server":{"ports":[],"environment":{}}}}' >"$T/compose-config.json"
  ctl up alfred
  bad_rc
  out_has "DISABLE_AUTOUPDATER"
  log_lacks "up -d"
}

test_up_refuses_auto_update_in_channel_gateway() {
  added
  echo '{"services":{"app-server":{"environment":{"DISABLE_AUTOUPDATER":"1"}},"channel-gateway":{}}}' >"$T/compose-config.json"
  ctl up alfred
  bad_rc
  out_has "channel-gateway"
  log_lacks "up -d"
}

test_up_adds_push_keys_to_existing_stack() {
  added
  sed -i '/^PUSH_VAPID_/d' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  [[ -n $(senv alfred PUSH_VAPID_PUBLIC_KEY) ]] || fail "no PUSH_VAPID_PUBLIC_KEY"
  [[ -n $(senv alfred PUSH_VAPID_PRIVATE_KEY) ]] || fail "no PUSH_VAPID_PRIVATE_KEY"
  eq "$(senv alfred PUSH_VAPID_CONTACT_EMAIL)" ops@example.org
  log_has "up -d"
}

test_up_adds_broker_push_keys_to_existing_stack() {
  added
  local pub
  pub=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  sed -i '/^BROKER_VAPID_/d' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  [[ -n $(senv alfred BROKER_VAPID_PUBLIC_KEY) ]] || fail "no BROKER_VAPID_PUBLIC_KEY"
  [[ -n $(senv alfred BROKER_VAPID_PRIVATE_KEY) ]] || fail "no BROKER_VAPID_PRIVATE_KEY"
  eq "$(senv alfred PUSH_VAPID_PUBLIC_KEY)" "$pub"
}

test_up_keeps_existing_push_keys() {
  added
  local pub priv
  pub=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  priv=$(senv alfred BROKER_VAPID_PRIVATE_KEY)
  ctl up alfred
  ok_rc
  eq "$(senv alfred PUSH_VAPID_PUBLIC_KEY)" "$pub"
  eq "$(senv alfred BROKER_VAPID_PRIVATE_KEY)" "$priv"
}

test_up_refuses_missing_push_contact() {
  added
  sed -i 's/^PUSH_CONTACT_EMAIL=.*/PUSH_CONTACT_EMAIL=/' "$H/config.env"
  ctl up alfred
  bad_rc
  out_has "PUSH_CONTACT_EMAIL"
  log_lacks "up -d"
}

test_check_refuses_missing_push_keys() {
  added
  sed -i '/^PUSH_VAPID_PRIVATE_KEY=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "PUSH_VAPID_PRIVATE_KEY"
}

test_check_refuses_net_raw_in_the_app_server_namespace() {
  added
  local base case
  base=$(cat "$T/compose-config.json")
  # Agents are root in the app-server; with NET_RAW they could capture the Access
  # JWT on its way to the BFF and use it on the vault. SERVICE|EDIT
  for case in \
    'app-server|del(.services["app-server"].cap_drop)' \
    'bff|.services.bff.cap_drop = ["MKNOD"]' \
    'channel-gateway|.services["channel-gateway"] = {"network_mode":"service:app-server","environment":{"DISABLE_AUTOUPDATER":"1"}}'; do
    jq -c "${case#*|}" <<<"$base" >"$T/compose-config.json"
    ctl check alfred
    bad_rc
    out_has "NET_RAW"
    out_has "${case%%|*}"
  done
}

test_check_accepts_dropping_every_capability() {
  added
  jq -c '.services["app-server"].cap_drop = ["ALL"]' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  ctl check alfred
  ok_rc
}

# with_secrets [BROKER_JSON] -> alfred runs the `secrets` profile and the
# rendered config holds this secret-broker (default: the hardened one)
with_secrets() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,secrets/' "$H/stacks/alfred/stack.env"
  broker_config "$@"
}

test_check_accepts_the_hardened_broker() {
  added
  with_secrets
  ctl check alfred
  ok_rc
}

test_check_refuses_missing_broker_keys() {
  added
  sed -i '/^BROKER_VAPID_PRIVATE_KEY=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "BROKER_VAPID_PRIVATE_KEY"
}

test_check_refuses_a_broker_that_is_not_hardened() {
  added
  local b
  for b in \
    '{"cap_drop":["ALL"],"volumes":[]}' \
    '{"read_only":true,"volumes":[]}' \
    '{"read_only":true,"cap_drop":["NET_RAW"],"volumes":[]}' \
    '{"read_only":true,"cap_drop":["ALL"],"volumes":[{"type":"bind","source":"/srv","target":"/data"}]}'; do
    with_secrets "$b"
    ctl check alfred
    bad_rc
    out_has "secret-broker"
  done
}

test_check_refuses_a_broker_with_privileges_or_shared_namespaces() {
  added
  local k v
  while read -r k v; do
    with_secrets '{"read_only":true,"cap_drop":["ALL"],"volumes":[],"'"$k"'":'"$v"'}'
    ctl check alfred
    bad_rc
    out_has "alfred: secret-broker must not set $k"
  done <<'EOF'
privileged true
cap_add ["SYS_ADMIN"]
network_mode "service:app-server"
pid "host"
ipc "host"
volumes_from ["app-server"]
devices [{"source":"/dev/fuse","target":"/dev/fuse","permissions":"rwm"}]
EOF
}

test_check_refuses_another_service_on_the_broker_volume() {
  added
  with_secrets
  jq -c '.services.bff.volumes = [{"type":"volume","source":"secret-broker-data","target":"/x"}]' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  ctl check alfred
  bad_rc
  out_has "bff"
}

test_check_refuses_secrets_profile_without_the_broker() {
  added
  with_secrets
  jq -c 'del(.services["secret-broker"])' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  ctl check alfred
  bad_rc
  out_has "secret-broker"
}

# pipelock_config [PIPELOCK_JSON] -> the rendered config also holds this pipelock
# (default: the hardened one compose/pipelock.yml renders) and the app-server's
# proxy settings from compose/pipelock-proxy.yml
pipelock_config() {
  local svc=${1:-'{"read_only":true,"cap_drop":["ALL"],"tmpfs":["/tmp"],"volumes":[{"type":"volume","source":"pipelock-data","target":"/data"}]}'}
  local np=localhost,127.0.0.1,::1,pipelock,secret-broker,gmail-triggers,searxng,ddg-mcp,google-mcp,host.docker.internal
  config_edit '.services.pipelock = '"$svc"'
    | .services["app-server"].environment += {"HTTPS_PROXY":"http://pipelock:8888","HTTP_PROXY":"http://pipelock:8888",
      "https_proxy":"http://pipelock:8888","http_proxy":"http://pipelock:8888","NO_PROXY":"'"$np"'","no_proxy":"'"$np"'",
      "NODE_USE_ENV_PROXY":"1"}'
}

# with_pipelock [PIPELOCK_JSON] -> alfred runs the `pipelock` profile and the
# rendered config holds this pipelock (default: the hardened one)
with_pipelock() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,pipelock/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,pipelock/' "$H/stacks/alfred/stack.env"
  pipelock_config "$@"
}

test_check_accepts_the_hardened_pipelock() {
  added
  with_pipelock
  ctl check alfred
  ok_rc
}

test_check_refuses_a_pipelock_that_is_not_hardened() {
  added
  local p
  for p in \
    '{"cap_drop":["ALL"],"volumes":[]}' \
    '{"read_only":true,"volumes":[]}' \
    '{"read_only":true,"cap_drop":["ALL"],"network_mode":"service:app-server","volumes":[]}' \
    '{"read_only":true,"cap_drop":["ALL"],"volumes":[{"type":"bind","source":"/srv","target":"/data"}]}'; do
    with_pipelock "$p"
    ctl check alfred
    bad_rc
    out_has "alfred: pipelock must"
  done
}

test_check_refuses_another_service_on_the_pipelock_volume() {
  added
  with_pipelock
  config_edit '.services.bff.volumes = [{"type":"volume","source":"pipelock-data","target":"/x"}]'
  ctl check alfred
  bad_rc
  out_has "alfred: bff mount(s) pipelock's volume"
}

test_check_refuses_the_pipelock_volume_without_the_profile() {
  added
  config_edit '.services.bff.volumes = [{"type":"volume","source":"pipelock-data","target":"/x"}]'
  ctl check alfred
  bad_rc
  out_has "alfred: bff mount(s) pipelock's volume"
}

test_check_refuses_the_vault_volume_without_the_profile() {
  added
  config_edit '.services.bff.volumes = [{"type":"volume","source":"secret-broker-data","target":"/x"}]'
  ctl check alfred
  bad_rc
  out_has "alfred: bff mount(s) the vault's volume"
}

test_check_refuses_volumes_from_pipelock() {
  added
  config_edit '.services.searxng = {"volumes_from":["pipelock"]}'
  ctl check alfred
  bad_rc
  out_has "(searxng from pipelock)"
}

test_check_refuses_pipelock_profile_without_the_service() {
  added
  with_pipelock
  config_edit 'del(.services.pipelock)'
  ctl check alfred
  bad_rc
  out_has "pipelock is not in the rendered config"
}

test_check_refuses_an_app_server_that_skips_pipelock() {
  added
  local k
  for k in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy; do
    with_pipelock
    config_edit '.services["app-server"].environment["'"$k"'"] = "http://elsewhere:3128"'
    ctl check alfred
    bad_rc
    out_has "the app-server's $k must be http://pipelock:8888"
  done
}

test_check_refuses_a_proxy_that_would_see_the_vault_or_the_executor() {
  added
  local k v
  for k in NO_PROXY no_proxy; do
    for v in secret-broker gmail-triggers; do
      with_pipelock
      config_edit '.services["app-server"].environment["'"$k"'"] |= (split(",") - ["'"$v"'"] | join(","))'
      ctl check alfred
      bad_rc
      out_has "the app-server's $k must list $v"
    done
  done
}

test_check_ignores_proxy_settings_without_pipelock() {
  added
  ctl check alfred
  ok_rc
}

# with_triggers -> alfred runs the gmail-triggers profile (with google), and the
# rendered config holds the hardened service
with_triggers() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google,gmail-triggers/' "$H/stacks/alfred/stack.env"
  triggers_config
}

test_check_accepts_the_hardened_gmail_triggers() {
  added
  with_triggers
  ctl check alfred
  ok_rc
}

test_check_refuses_gmail_triggers_without_google() {
  added
  with_triggers
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,gmail-triggers/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,gmail-triggers/' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "alfred: the gmail-triggers profile needs google"
}

# triggers_refused JQ EXPECTED -> with alfred's hardened gmail-triggers edited by
# the jq filter JQ, `check` fails and says EXPECTED
triggers_refused() {
  added
  with_triggers
  config_edit "$1"
  ctl check alfred
  bad_rc
  out_has "alfred: $2"
}
GT='.services["gmail-triggers"]'

test_check_refuses_gmail_triggers_missing() {
  triggers_refused "del($GT)" "the gmail-triggers profile is on but gmail-triggers is not in the rendered config"
}

test_check_refuses_gmail_triggers_without_read_only() {
  triggers_refused "del($GT.read_only)" "gmail-triggers must have read_only: true"
}

test_check_refuses_gmail_triggers_with_capabilities() {
  triggers_refused "$GT.cap_drop = [\"NET_RAW\"]" "gmail-triggers must drop all capabilities"
}

test_check_refuses_gmail_triggers_privileged() {
  triggers_refused "$GT.privileged = true" "gmail-triggers must not set privileged"
}

test_check_refuses_gmail_triggers_cap_add() {
  triggers_refused "$GT.cap_add = [\"SYS_ADMIN\"]" "gmail-triggers must not set cap_add"
}

test_check_refuses_gmail_triggers_network_mode() {
  # In the app-server's namespace an agent could take over its port.
  triggers_refused "$GT.network_mode = \"service:app-server\"" "gmail-triggers must not set network_mode"
}

test_check_refuses_gmail_triggers_pid() {
  triggers_refused "$GT.pid = \"host\"" "gmail-triggers must not set pid"
}

test_check_refuses_gmail_triggers_ipc() {
  triggers_refused "$GT.ipc = \"host\"" "gmail-triggers must not set ipc"
}

test_check_refuses_gmail_triggers_volumes_from() {
  triggers_refused "$GT.volumes_from = [\"app-server\"]" "gmail-triggers must not set volumes_from"
}

test_check_refuses_gmail_triggers_devices() {
  triggers_refused "$GT.devices = [{\"source\":\"/dev/fuse\",\"target\":\"/dev/fuse\",\"permissions\":\"rwm\"}]" "gmail-triggers must not set devices"
}

test_check_refuses_gmail_triggers_with_another_host_path() {
  triggers_refused "$GT.volumes += [{\"type\":\"bind\",\"source\":\"/srv\",\"target\":\"/srv\"}]" "gmail-triggers may mount from the host only"
}

test_check_refuses_gmail_triggers_without_its_state() {
  triggers_refused "del($GT.volumes[0])" "gmail-triggers may mount from the host only"
}

test_check_refuses_gmail_triggers_on_a_parent_of_its_state() {
  triggers_refused "$GT.volumes[0].source = \"$H/stacks/alfred/state/garden\"" "gmail-triggers may mount from the host only"
}

test_check_refuses_gmail_triggers_with_its_state_elsewhere() {
  triggers_refused "$GT.volumes[0].target = \"/root\"" "gmail-triggers may mount from the host only"
}

test_check_refuses_gmail_triggers_the_old_spool() {
  triggers_refused "$GT.volumes += [{\"type\":\"bind\",\"source\":\"$H/stacks/alfred/state/letta-home/garden/gmail-triggers/spool\",\"target\":\"/spool\"}]" \
    "gmail-triggers may mount from the host only"
}

test_check_refuses_gmail_triggers_writable_google_creds() {
  triggers_refused "del($GT.volumes[1].read_only)" "gmail-triggers must mount google-creds and google-policy read-only"
}

test_check_refuses_gmail_triggers_writable_google_policy() {
  triggers_refused "$GT.volumes[2].read_only = false" "gmail-triggers must mount google-creds and google-policy read-only"
}

test_check_refuses_gmail_triggers_other_volumes() {
  triggers_refused "$GT.volumes += [{\"type\":\"volume\",\"source\":\"bff-auth\",\"target\":\"/x\"}]" "gmail-triggers must not mount bff-auth"
}

test_check_refuses_another_service_on_the_rules() {
  triggers_refused ".services[\"app-server\"].volumes = [{\"type\":\"bind\",\"source\":\"$H/stacks/alfred/state/garden/gmail-triggers\",\"target\":\"/x\"}]" \
    "app-server mounts $H/stacks/alfred/state/garden/gmail-triggers, which reaches $H/stacks/alfred/state/garden"
}

test_check_refuses_another_service_on_a_parent_of_the_rules() {
  triggers_refused ".services.bff.volumes += [{\"type\":\"bind\",\"source\":\"$H/stacks/alfred/state\",\"target\":\"/x\"}]" \
    "bff mounts $H/stacks/alfred/state, which reaches"
}

test_check_refuses_another_service_on_a_symlink_to_the_rules() {
  ln -s "$H/stacks/alfred/state/garden" "$T/link"
  triggers_refused ".services.bff.volumes += [{\"type\":\"bind\",\"source\":\"$T/link/gmail-triggers\",\"target\":\"/x\"}]" \
    "bff mounts $T/link/gmail-triggers, which reaches"
}

test_check_refuses_volumes_from_gmail_triggers() {
  triggers_refused ".services.bff.volumes_from = [\"gmail-triggers:ro\"]" "volumes_from reaches the Google token, the vault, the gmail-triggers rules or the pipelock receipts (bff from gmail-triggers)"
}

test_check_refuses_a_volume_bound_to_the_rules() {
  triggers_refused ".volumes = {\"rules\":{\"driver\":\"local\",\"driver_opts\":{\"type\":\"none\",\"o\":\"bind\",\"device\":\"$H/stacks/alfred/state/garden/\"}}}" \
    "volume rules is bound to $H/stacks/alfred/state/garden/, which reaches"
}

test_check_refuses_a_volume_bound_to_a_parent_of_the_rules() {
  triggers_refused ".volumes = {\"st\":{\"driver_opts\":{\"type\":\"none\",\"o\":\"bind\",\"device\":\"$H/stacks\"}}}" \
    "volume st is bound to $H/stacks, which reaches"
}

test_check_refuses_a_volume_bound_to_a_relative_path() {
  triggers_refused '.volumes = {"st":{"driver_opts":{"type":"none","o":"bind,ro","device":"../state"}}}' "volume st binds the relative path ../state"
}

test_check_accepts_volumes_bound_elsewhere() {
  added
  with_triggers
  config_edit '.volumes = {"srv":{"driver_opts":{"type":"none","o":"bind","device":"/srv/x"}},"nfs":{"driver_opts":{"type":"nfs","o":"addr=192.0.2.10","device":":/x"}}}'
  ctl check alfred
  ok_rc
}

test_check_refuses_a_symlinked_state_dir() {
  added
  mv "$H/stacks/alfred/state" "$H/stacks/alfred/real-state"
  ln -s real-state "$H/stacks/alfred/state"
  with_triggers
  ctl check alfred
  bad_rc
  out_has "alfred: $H/stacks/alfred/state is a symlink"
}

test_check_refuses_a_symlinked_garden() {
  added
  mkdir -p "$T/elsewhere"
  ln -s "$T/elsewhere" "$H/stacks/alfred/state/garden"
  with_triggers
  ctl check alfred
  bad_rc
  out_has "alfred: $H/stacks/alfred/state/garden is a symlink"
}

test_check_refuses_a_symlinked_rules_dir() {
  added
  mkdir -p "$T/elsewhere" "$H/stacks/alfred/state/garden"
  ln -s "$T/elsewhere" "$H/stacks/alfred/state/garden/gmail-triggers"
  with_triggers
  ctl check alfred
  bad_rc
  out_has "alfred: $H/stacks/alfred/state/garden/gmail-triggers is a symlink"
}

test_check_refuses_volumes_from_a_google_creds_holder_whatever_the_profiles() {
  added
  config_edit '.services.bff.volumes = [{"type":"volume","source":"google-creds","target":"/app/google/creds"}]
    | .services.searxng = {"volumes_from":["service:bff"]}'
  ctl check alfred
  bad_rc
  out_has "alfred: volumes_from reaches the Google token, the vault, the gmail-triggers rules or the pipelock receipts (searxng from bff)"
}

test_check_refuses_volumes_from_the_broker() {
  added
  config_edit '.services.searxng = {"volumes_from":["secret-broker"]}'
  ctl check alfred
  bad_rc
  out_has "(searxng from secret-broker)"
}

test_check_accepts_volumes_from_elsewhere() {
  added
  config_edit '.services.searxng = {"volumes_from":["ddg-mcp"]}'
  ctl check alfred
  ok_rc
}

test_check_refuses_a_missing_executor_token() {
  added
  with_triggers
  sed -i '/^GMAIL_TRIGGERS_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "alfred: GMAIL_TRIGGERS_EXECUTOR_TOKEN must be 64 lowercase hex characters"
}

test_check_refuses_a_malformed_executor_token() {
  added
  with_triggers
  sed -i 's/^GMAIL_TRIGGERS_EXECUTOR_TOKEN=.*/GMAIL_TRIGGERS_EXECUTOR_TOKEN=ABCDEF/' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "GMAIL_TRIGGERS_EXECUTOR_TOKEN must be 64 lowercase hex characters"
}

test_check_needs_no_executor_token_without_the_profile() {
  added
  sed -i '/^GMAIL_TRIGGERS_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  ok_rc
}

test_up_creates_the_executor_token_once() {
  added
  local tok
  tok=$(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN)
  [[ $tok =~ ^[0-9a-f]{64}$ ]] || fail "bad token [$tok]"
  ctl up alfred
  ok_rc
  eq "$(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN)" "$tok"
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
  sed -i '/^GMAIL_TRIGGERS_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  [[ $(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN) =~ ^[0-9a-f]{64}$ ]] || fail "token not recreated"
}

test_executor_tokens_differ_between_stacks() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  [[ $(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN) != "$(senv hal GMAIL_TRIGGERS_EXECUTOR_TOKEN)" ]] || fail "same token"
}

test_check_refuses_google_creds_in_other_services_whatever_the_profiles() {
  added
  config_edit '.services.searxng = {"volumes":[{"type":"volume","source":"google-creds","target":"/x","read_only":true}]}'
  ctl check alfred
  bad_rc
  out_has "alfred: searxng mount(s) google-creds"
}

test_check_lets_the_upstream_google_services_mount_google_creds() {
  added
  config_edit '.services.bff.volumes = [{"type":"volume","source":"google-creds","target":"/app/google/creds"}]
    | .services["google-mcp"] = {"volumes":[{"type":"volume","source":"google-creds","target":"/creds"}]}'
  ctl check alfred
  ok_rc
}

# exec_args -> the arguments of the last `compose exec ... sh -c SCRIPT`, one per line
exec_args() { tr '\0' '\n' <"$T/exec-args"; }

GT_CRON_TEXT='# Installed by lettucectl (gmail-triggers profile). Removed with the profile.
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
* * * * * root /usr/local/bin/agent-env node /root/.letta/garden/gmail-triggers/bin/run.mjs >> /root/.letta/garden/gmail-triggers/executor.log 2>&1'

test_up_installs_the_gmail_triggers_executor() {
  added
  with_triggers
  ctl up alfred
  ok_rc
  log_has "exec -T app-server sh -c"
  # Only the executor and its token ship, not its tests.
  eq "$(shipped)" "run.mjs token"
  eq "$(exec_args | head -7 | paste -sd' ')" "sh install /root/.letta/garden/gmail-triggers lettuce-garden-gmail-triggers /root/.letta/system-cron /etc/cron.d /etc/lettuce-garden/gmail-triggers-token"
  local a=()
  mapfile -d '' a <"$T/exec-args"
  eq "${a[7]}" "$GT_CRON_TEXT"
  eq "${#a[@]}" 8
}

test_up_passes_the_executor_token_only_on_stdin() {
  added
  with_triggers
  ctl up alfred
  ok_rc
  local tok
  tok=$(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN)
  eq "$(tar -xOf "$T/exec-stdin.log" token)" "$tok"
  eq "$(tar -tvf "$T/exec-stdin.log" token | cut -c1-10)" "-rw-------"
  ! grep -qF "$tok" "$T/calls.log" || fail "token in docker argv"
  ! grep -qF "$tok" "$T/exec-args" || fail "token in exec args"
  ! grep -qF "$tok" "$OUT" || fail "token in output"
}

test_up_fails_clearly_when_the_executor_cannot_be_installed() {
  added
  with_triggers
  stub STUB_EXEC_RC=1
  ctl up alfred
  bad_rc
  out_has "alfred: could not install the gmail-triggers executor; the stack is up but its Gmail triggers will not run"
  out_has "the gmail-triggers executor is out of step on: alfred; fix the cause, then run: lettucectl up <name>"
}

test_up_all_continues_past_a_failed_executor_install() {
  triggers_config
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  # One rendered config serves both stacks in the stub, so hal shares alfred's
  # state path to pass the bind-mount check.
  sed -i "s|^LETTA_STATE_DIR=.*|LETTA_STATE_DIR=$H/stacks/alfred/state|" "$H/stacks/hal/stack.env"
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google,gmail-triggers/' "$H/stacks/alfred/stack.env" "$H/stacks/hal/stack.env"
  : >"$T/calls.log"
  stub STUB_EXEC_RC=1
  ctl up all
  bad_rc
  log_has "-p lettuce-alfred"
  grep -q '^docker compose -p lettuce-hal .* up -d --build' "$T/calls.log" || fail "hal not started after alfred's failed install"
  out_has "the gmail-triggers executor is out of step on: alfred hal; fix the cause, then run: lettucectl up <name>"
}

test_up_without_gmail_triggers_runs_no_exec() {
  added
  ctl up alfred
  ok_rc
  log_lacks " exec "
}

test_up_removes_the_executor_when_the_profile_is_off() {
  added
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  ctl up alfred
  ok_rc
  log_has "exec -T app-server sh -c"
  eq "$(exec_args | paste -sd' ')" "sh uninstall /root/.letta/garden/gmail-triggers lettuce-garden-gmail-triggers /root/.letta/system-cron /etc/cron.d /etc/lettuce-garden/gmail-triggers-token"
  out_has "alfred: removed the gmail-triggers executor"
}

test_up_removes_an_executor_known_by_its_cron_file() {
  added
  mkdir -p "$H/stacks/alfred/state/letta-home/system-cron"
  touch "$H/stacks/alfred/state/letta-home/system-cron/lettuce-garden-gmail-triggers"
  ctl up alfred
  ok_rc
  log_has "sh uninstall /root/.letta/garden/gmail-triggers"
}

test_up_fails_clearly_when_the_executor_cannot_be_removed() {
  added
  mkdir -p "$H/stacks/alfred/state/letta-home/system-cron"
  touch "$H/stacks/alfred/state/letta-home/system-cron/lettuce-garden-gmail-triggers"
  stub STUB_EXEC_RC=1
  ctl up alfred
  bad_rc
  out_has "alfred: could not remove the gmail-triggers executor"
}

test_up_refuses_readable_stack_env() {
  added
  chmod 644 "$H/stacks/alfred/stack.env"
  ctl up alfred
  bad_rc
  out_has "600"
}

test_compose_ignores_the_callers_environment() {
  added
  PUBLIC_ORIGIN=https://evil.example ctl up alfred
  ok_rc
  log_lacks "PUBLIC_ORIGIN=https://evil.example"
  log_has "--project-directory $H/upstream/docker -f $H/upstream/docker/compose.yml -f $H/compose/hardening.yml -f $H/compose/multi.yml -f $H/compose/secrets.yml -f $H/compose/gmail-triggers.yml -f $H/compose/pipelock.yml --env-file $H/stacks/alfred/stack.env"
  log_lacks "pipelock-proxy.yml"
}

test_compose_adds_the_proxy_overlay_only_with_pipelock() {
  added
  with_pipelock
  ctl up alfred
  ok_rc
  log_has "-f $H/compose/pipelock.yml -f $H/compose/pipelock-proxy.yml --env-file $H/stacks/alfred/stack.env"
}

test_up_all_starts_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  ctl up all
  ok_rc
  log_has "-p lettuce-alfred"
  log_has "-p lettuce-hal"
}

# ── users ────────────────────────────────────────────────────────────────────
test_users_updates_policy_and_allowlist_together() {
  added
  ctl users alfred alfred@example.com,Partner@Example.com
  ok_rc
  eq "$(jq -c '[.include[].email.email]' <<<"$(body_of PUT /accounts/acc1/access/policies/pol1)")" '["alfred@example.com","partner@example.com"]'
  eq "$(senv alfred ALLOWED_USERS)" alfred@example.com,partner@example.com
  log_has "up -d"
}

# ── remove ───────────────────────────────────────────────────────────────────
test_remove_needs_the_name_typed() {
  added
  ctl remove alfred <<<"nope"
  bad_rc
  log_lacks "curl DELETE"
  log_lacks "down"
}

test_remove_deletes_cloudflare_resources_and_keeps_data() {
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  log_has "down -v"
  eq "$(calls | tr '\n' ';')" "DELETE /zones/zone1/dns_records/rec1;DELETE /accounts/acc1/cfd_tunnel/tun1/connections;DELETE /accounts/acc1/cfd_tunnel/tun1;DELETE /accounts/acc1/access/apps/app1;DELETE /accounts/acc1/access/policies/pol1;"
  [[ -d $H/stacks/alfred/state ]] || fail "state deleted without --purge"
  eq "$(senv alfred CF_TUNNEL_ID)" ""
}

test_remove_purge_deletes_the_stack_directory() {
  added
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  log_has "run --rm --no-deps"
  # `compose run` recreates the project network; a final `down` must remove it again.
  eq "$(grep '^docker compose' "$T/calls.log" | tail -1 | grep -o ' down$')" " down"
  [[ ! -e $H/stacks/alfred ]] || fail "stack directory still exists"
}

# state_elsewhere -> alfred's LETTA_STATE_DIR is $T/state, not stacks/alfred/state
state_elsewhere() {
  mkdir -p "$T/state"
  sed -i "s|^LETTA_STATE_DIR=.*|LETTA_STATE_DIR=$T/state|" "$H/stacks/alfred/stack.env"
}

test_remove_purge_empties_the_gmail_trigger_rules_too() {
  # The rules are root-owned and outside the app-server's mounts, so `rm -rf`
  # as the operator would fail on them unless the container empties them too.
  # They live under stack.env's LETTA_STATE_DIR, like every other mount.
  added
  state_elsewhere
  mkdir -p "$T/state/garden/gmail-triggers" "$H/stacks/alfred/state/garden/gmail-triggers"
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  log_has "-v $T/state/garden:/garden"
  log_lacks "-v $H/stacks/alfred/state/garden:/garden"
  log_has "/root/.letta /data /work /garden -mindepth 1 -delete"
}

test_remove_purge_mounts_no_garden_when_there_is_none() {
  added
  state_elsewhere
  mkdir -p "$H/stacks/alfred/state/garden/gmail-triggers"
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  log_lacks ":/garden"
}

test_remove_purge_refuses_a_symlinked_garden() {
  # Docker would follow it, and the purge would empty whatever it points at.
  added
  mkdir -p "$T/elsewhere"
  touch "$T/elsewhere/keep"
  ln -s "$T/elsewhere" "$H/stacks/alfred/state/garden"
  ctl remove alfred --purge <<<"alfred"
  bad_rc
  out_has "$H/stacks/alfred/state/garden is a symlink"
  log_lacks ":/garden"
  log_lacks "run --rm --no-deps"
}

test_remove_continues_past_already_deleted_resources() {
  added
  stub 'STUB_CF_FAIL="DELETE /accounts/acc1/cfd_tunnel/tun1"'
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "stubbed failure"
  log_has "curl DELETE /accounts/acc1/access/apps/app1"
}

test_remove_reminds_to_delete_the_google_redirect_uri() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  added
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  out_has "delete https://alfred.example.org/api/google/oauth/callback"
}

test_remove_says_the_vault_goes_too() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Its vault"
}

test_remove_warns_about_a_vault_whose_profile_is_off() {
  # Turning `secrets` off keeps the volume, and `down -v` still deletes it.
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Its vault"
  log_has "volume inspect lettuce-alfred_secret-broker-data"
}

test_remove_says_where_the_gmail_triggers_are() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  triggers_config
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Its Gmail triggers stop with it; their rules are in its state, so they go only with --purge."
}

test_remove_finds_gmail_triggers_under_the_state_dir() {
  added
  state_elsewhere
  mkdir -p "$T/state/garden/gmail-triggers"
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Its Gmail triggers stop with it"
}

test_remove_says_nothing_of_gmail_triggers_outside_the_state_dir() {
  added
  state_elsewhere
  mkdir -p "$H/stacks/alfred/state/garden/gmail-triggers"
  ctl remove alfred <<<"alfred"
  ok_rc
  out_lacks "Gmail triggers"
}

test_remove_says_nothing_of_gmail_triggers_never_used() {
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  out_lacks "Gmail triggers"
}

test_remove_says_the_receipts_go_too() {
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Its pipelock receipts and their signing key are deleted too"
  log_has "volume inspect lettuce-alfred_pipelock-data"
}

test_remove_says_nothing_of_receipts_never_written() {
  added
  stub STUB_VOLUMES_MISSING=lettuce-alfred_pipelock-data
  ctl remove alfred <<<"alfred"
  ok_rc
  out_lacks "pipelock"
  out_has "Its vault"
}

test_remove_says_nothing_of_a_vault_that_was_never_created() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  added
  stub STUB_VOLUME_RC=1
  ctl remove alfred <<<"alfred"
  ok_rc
  out_lacks "Its vault"
}

# ── profiles ─────────────────────────────────────────────────────────────────
test_profiles_sets_config_and_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  ctl profiles "cloudflared, search,google"
  ok_rc
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search,google
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search,google
  eq "$(senv hal COMPOSE_PROFILES)" cloudflared,search,google
  eq "$(stat -c %a "$H/stacks/hal/stack.env")" 600
  log_has "-p lettuce-alfred"
  log_has "-p lettuce-hal"
  log_has "up -d --build"
  log_lacks " rm -sf"
  out_has "https://alfred.example.org/api/google/oauth/callback"
  out_has "https://hal.example.org/api/google/oauth/callback"
}

# containers NAME SERVICE... -> the stub's `docker ps` reports these containers for the stack
containers() {
  local n=$1 s
  shift
  for s in "$@"; do
    printf 'lettuce-%s %s\n' "$n" "$s" >>"$T/containers.txt"
  done
}

test_profiles_removes_services_of_dropped_profiles() {
  added
  containers alfred app-server bff cloudflared searxng ddg-mcp
  ctl profiles cloudflared
  ok_rc
  log_has "up -d --build"
  grep -q '^docker compose -p lettuce-alfred .* rm -sf ddg-mcp searxng$' "$T/calls.log" || fail "dropped services not removed"
  out_lacks "oauth/callback"
}

test_profiles_dropping_pipelock_removes_it_and_the_proxy_settings() {
  added
  with_pipelock
  containers alfred app-server bff cloudflared pipelock
  : >"$T/calls.log"
  ctl profiles cloudflared
  ok_rc
  grep -q '^docker compose -p lettuce-alfred .* rm -sf pipelock$' "$T/calls.log" || fail "pipelock not removed"
  grep '^docker compose -p lettuce-alfred .* up -d' "$T/calls.log" | grep -q pipelock-proxy.yml && fail "up still merged the proxy settings"
  true
}

test_profiles_removes_leftovers_on_a_rerun() {
  added
  containers alfred app-server bff cloudflared searxng ddg-mcp google-mcp
  ctl profiles cloudflared,search
  ok_rc
  grep -q '^docker compose -p lettuce-alfred .* rm -sf google-mcp$' "$T/calls.log" || fail "leftover google-mcp not removed"
}

test_profiles_same_list_restarts_nothing() {
  added
  containers alfred app-server bff cloudflared searxng ddg-mcp
  ctl profiles cloudflared,search
  ok_rc
  out_has "nothing to change"
  log_lacks "up -d"
  log_lacks " rm -sf"
}

test_profiles_prints_redirect_uris_whenever_google_is_on() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  added
  ctl profiles cloudflared,google
  ok_rc
  out_has "https://alfred.example.org/api/google/oauth/callback"
  log_lacks "up -d"
}

test_profiles_checks_every_stack_before_writing() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  sed -i 's/^CLOUDFLARE_TUNNEL_TOKEN=.*/CLOUDFLARE_TUNNEL_TOKEN=/' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search,google
  bad_rc
  out_has "CLOUDFLARE_TUNNEL_TOKEN"
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  eq "$(senv hal COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

test_profiles_repairs_drift() {
  added
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared/' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search
  ok_rc
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_has "up -d --build"
}

test_profiles_routes_secrets_for_every_stack() {
  broker_config
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  ctl profiles cloudflared,search,secrets
  ok_rc
  eq "$(grep -c '^curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations ' "$T/calls.log")" 2
  grep '^curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations ' "$T/calls.log" | grep -qF '"hostname":"hal.example.org","path":"^/garden/secrets(/|$)"' ||
    fail "hal has no /garden/secrets route"
  # The route is in place before the stacks start.
  [[ $(grep -n '^curl PUT' "$T/calls.log" | tail -1 | cut -d: -f1) -lt $(grep -n 'up -d --build' "$T/calls.log" | head -1 | cut -d: -f1) ]] ||
    fail "stacks started before their route"
}

test_profiles_dropping_secrets_removes_the_route() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  added
  ctl profiles cloudflared
  ok_rc
  log_has "curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations"
  log_lacks "secret-broker"
}

test_profiles_reports_a_failed_route_update() {
  broker_config
  added
  stub 'STUB_CF_FAIL="PUT /accounts/acc1/cfd_tunnel/tun1/configurations"'
  ctl profiles cloudflared,search,secrets
  bad_rc
  out_has "run the same command again"
}

test_profiles_routes_gmail_triggers_and_installs_the_executor() {
  triggers_config
  added
  containers alfred app-server
  sed -i '/^GMAIL_TRIGGERS_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search,google,gmail-triggers
  ok_rc
  [[ $(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN) =~ ^[0-9a-f]{64}$ ]] || fail "profiles gave no executor token"
  eq "$(jq -c '.config.ingress' <<<"$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)")" \
    '[{"hostname":"alfred.example.org","path":"^/garden/gmail-triggers(/|$)","service":"http://gmail-triggers:8000"},{"hostname":"alfred.example.org","path":"^/garden/?$","service":"http://gmail-triggers:8000"},{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}]'
  log_has "sh install /root/.letta/garden/gmail-triggers"
  [[ $(grep -n '^curl PUT' "$T/calls.log" | tail -1 | cut -d: -f1) -lt $(grep -n 'up -d --build' "$T/calls.log" | head -1 | cut -d: -f1) ]] ||
    fail "stack started before its route"
}

test_profiles_dropping_gmail_triggers_removes_route_container_and_executor() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  triggers_config
  added
  containers alfred app-server bff cloudflared google-mcp gmail-triggers
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  ctl profiles cloudflared,google
  ok_rc
  log_has "curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations"
  log_lacks "gmail-triggers:8000"
  grep -q '^docker compose -p lettuce-alfred .* rm -sf gmail-triggers$' "$T/calls.log" || fail "gmail-triggers container not removed"
  log_has "sh uninstall /root/.letta/garden/gmail-triggers"
  out_has "alfred: removed the gmail-triggers executor"
}

test_profiles_rerun_removes_a_leftover_executor() {
  added
  containers alfred app-server
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  ctl profiles cloudflared,search
  ok_rc
  out_has "nothing to change"
  log_has "sh uninstall /root/.letta/garden/gmail-triggers"
}

test_profiles_reports_an_executor_it_could_not_remove() {
  added
  containers alfred app-server
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  stub STUB_EXEC_RC=1
  ctl profiles cloudflared,search
  bad_rc
  out_has "alfred: could not remove the gmail-triggers executor"
  out_has "could not sync the gmail-triggers executor of: alfred"
  out_has "run the same command again"
}

test_profiles_skips_a_stopped_stack_when_removing_the_executor() {
  added
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  ctl profiles cloudflared,search
  ok_rc
  out_has "alfred: not running; its executor is removed at its next up"
  log_lacks "sh uninstall"
}

test_profiles_rerun_repairs_a_failed_executor_install() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  triggers_config
  added
  containers alfred app-server
  ctl profiles cloudflared,google,gmail-triggers
  ok_rc
  out_has "nothing to change"
  log_has "sh install /root/.letta/garden/gmail-triggers"
}

test_profiles_continues_past_a_failed_executor_install() {
  triggers_config
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  # One rendered config serves both stacks in the stub, so hal shares alfred's
  # state path to pass the bind-mount check.
  sed -i "s|^LETTA_STATE_DIR=.*|LETTA_STATE_DIR=$H/stacks/alfred/state|" "$H/stacks/hal/stack.env"
  containers alfred app-server
  containers hal app-server
  : >"$T/calls.log"
  stub STUB_EXEC_RC=1
  ctl profiles cloudflared,search,google,gmail-triggers
  bad_rc
  grep -q '^docker compose -p lettuce-hal .* up -d --build' "$T/calls.log" || fail "hal not started"
  out_has "alfred: could not install the gmail-triggers executor"
  out_has "hal: could not install the gmail-triggers executor"
  out_has "could not sync the gmail-triggers executor of: alfred hal"
  out_has "run the same command again"
  eq "$(senv hal COMPOSE_PROFILES)" cloudflared,search,google,gmail-triggers
}

test_profiles_checks_gmail_triggers_under_the_new_list_before_writing() {
  added
  # The rendered config has no gmail-triggers service.
  ctl profiles cloudflared,search,google,gmail-triggers
  bad_rc
  out_has "alfred: the gmail-triggers profile is on but gmail-triggers is not in the rendered config"
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

test_profiles_checks_the_executor_token_before_writing() {
  triggers_config
  added
  sed -i 's/^GMAIL_TRIGGERS_EXECUTOR_TOKEN=.*/GMAIL_TRIGGERS_EXECUTOR_TOKEN=not-hex/' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search,google,gmail-triggers
  bad_rc
  out_has "alfred: GMAIL_TRIGGERS_EXECUTOR_TOKEN must be 64 lowercase hex characters"
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

test_profiles_checks_symlinks_under_the_new_list_before_writing() {
  triggers_config
  added
  mkdir -p "$T/elsewhere"
  ln -s "$T/elsewhere" "$H/stacks/alfred/state/garden"
  ctl profiles cloudflared,search,google,gmail-triggers
  bad_rc
  out_has "alfred: $H/stacks/alfred/state/garden is a symlink"
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
}

test_profiles_renders_the_new_list_for_the_check() {
  triggers_config
  added
  ctl profiles cloudflared,search,google,gmail-triggers
  ok_rc
  # The first rendering, before anything is written, already uses the new list.
  eq "$(grep '^docker-env-file ' "$T/calls.log" | head -1)" "docker-env-file COMPOSE_PROFILES=cloudflared,search,google,gmail-triggers"
  eq "$(grep -c '^docker-env-file COMPOSE_PROFILES=cloudflared,search$' "$T/calls.log")" 0
  # The rendering's temp env file is gone.
  eq "$(find "$H/stacks/alfred" -maxdepth 1 -name 'stack.env.*' | wc -l)" 0
}

test_profiles_refuses_gmail_triggers_without_google() {
  added
  ctl profiles cloudflared,gmail-triggers
  bad_rc
  out_has "the gmail-triggers profile needs google"
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  log_lacks "up -d"
}

test_profiles_refuses_a_bad_list() {
  added
  local l
  for l in search "cloudflared,Search" "cloudflared,search,search" "cloudflared,a.b" ","; do
    ctl profiles "$l"
    bad_rc
  done
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

test_profiles_without_arguments_shows_them() {
  added
  ctl profiles
  ok_rc
  out_has "config.env"
  grep -qE '^alfred +cloudflared,search$' "$OUT" || fail "alfred's profiles not shown"
  log_lacks "up -d"
}

test_profiles_view_needs_only_config() {
  added
  sed -i 's/^CF_ZONE_ID=.*/CF_ZONE_ID=/' "$H/config.env"
  ctl profiles
  ok_rc
  out_has "cloudflared,search"
}

# ── list ─────────────────────────────────────────────────────────────────────
test_list_shows_stacks_and_hosts() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ctl list
  ok_rc
  out_has "alfred"
  out_has "https://hal.example.org"
}

# ── shell ────────────────────────────────────────────────────────────────────
# agents NAME... -> what `letta agents list` answers: one agent per name, id agent-<n>
agents() {
  local i=0 n items=()
  for n in "$@"; do
    i=$((i + 1))
    items+=("$(jq -nc --arg id "agent-$i" --arg n "$n" '{id: $id, name: $n, system: "long prompt"}')")
  done
  printf '{"items":[%s]}\n' "$(IFS=,; echo "${items[*]}")" >"$T/agents.json"
}

test_shell_without_agent_opens_work() {
  added
  ctl shell alfred </dev/null
  ok_rc
  log_has "-p lettuce-alfred"
  log_has "exec -T -w /work app-server bash"
  log_lacks "letta agents list"
}

# workspaces ID... -> alfred's agents with these ids have a workspace
workspaces() {
  local id
  for id in "$@"; do mkdir -p "$H/stacks/alfred/state/workspaces/$id"; done
}

test_shell_opens_the_agents_workspace() {
  added
  agents Alfred Lucius
  workspaces agent-2
  ctl shell alfred lucius </dev/null
  ok_rc
  out_has "alfred: Lucius (agent-2)"
  log_has "exec -T -w /work/agent-2 -e AGENT_ID=agent-2 -e LETTA_AGENT_ID=agent-2 -e AGENT_NAME=Lucius -e USER_CWD=/work/agent-2 app-server bash -c"
  # shellcheck disable=SC2016 # the literal script lettucectl passes to the container
  log_has 'export MEMORY_DIR=$LETTA_LOCAL_BACKEND_DIR/memfs/$AGENT_ID/memory'
}

test_shell_passes_stdin_to_the_agents_shell() {
  added
  agents Lucius
  workspaces agent-1
  ctl shell alfred lucius <<<'ls'
  ok_rc
  eq "$(cat "$T/exec-stdin.log")" ls
}

test_shell_accepts_an_agent_id() {
  added
  agents Alfred Lucius
  workspaces agent-1
  ctl shell alfred agent-1 </dev/null
  ok_rc
  log_has "-e AGENT_NAME=Alfred"
}

test_shell_keeps_spaces_in_agent_names() {
  added
  agents "The Finn"
  workspaces agent-1
  ctl shell alfred "the finn" </dev/null
  ok_rc
  log_has "-e AGENT_NAME=The Finn -e"
}

test_shell_refuses_an_agent_without_a_workspace() {
  added
  agents Alfred Lucius
  ctl shell alfred lucius </dev/null
  bad_rc
  out_has "alfred: no workspace for Lucius at $H/stacks/alfred/state/workspaces/agent-2"
  log_lacks "-w /work"
}

test_shell_lists_agents_when_none_match() {
  added
  agents Alfred Lucius
  ctl shell alfred hal </dev/null
  bad_rc
  out_has "alfred has no agent 'hal'; agents: Alfred, Lucius"
  log_lacks "-w /work"
}

test_shell_refuses_ambiguous_agent_names() {
  added
  agents Saburo saburo
  ctl shell alfred Saburo </dev/null
  bad_rc
  out_has "use an id: agent-1 agent-2"
  log_lacks "-w /work"
}

test_shell_fails_when_agents_cannot_be_listed() {
  added
  stub STUB_AGENTS_RC=1
  ctl shell alfred lucius </dev/null
  bad_rc
  out_has "could not list agents"
}

test_shell_rejects_unknown_stacks() {
  ctl shell hal </dev/null
  bad_rc
  out_has "no stack named 'hal'"
}

# ── toolbox ──────────────────────────────────────────────────────────────────
# toolbox_ready -> alfred with agents Alfred and Lucius (agent-2), an app-server
# image and Lucius's workspace; the operator's home is the empty $T/u
toolbox_ready() {
  added
  agents Alfred Lucius
  jq -c '.services["app-server"].image = "lettuce-app-server:test"' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  mkdir -p "$H/stacks/alfred/state/workspaces/agent-2" "$T/u"
}

# tb [--OPTION...] ARGS... -> `toolbox --OPTION... alfred ARGS...` with $T/u as home,
# so nothing of the real operator's is read; TB_SOCK and TB_CLAUDE stand in for
# their variables
tb() {
  local opts=()
  while [[ ${1:-} == --* ]]; do
    opts+=("$1")
    shift
  done
  HOME=$T/u XDG_CONFIG_HOME="" CLAUDE_CONFIG_DIR="" SSH_AUTH_SOCK=${TB_SOCK:-} \
    CLAUDE_CODE_OAUTH_TOKEN=${TB_CLAUDE:-} ctl toolbox "${opts[@]}" alfred "$@" </dev/null
}

# toolbox_tools -> the operator has ssh, git, gh and Claude Code set up
toolbox_tools() {
  mkdir -p "$T/u/.ssh" "$T/u/.claude/skills"
  touch "$T/u/.ssh/known_hosts" "$T/u/.gitconfig" "$T/u/.claude/CLAUDE.md" "$T/agent.sock"
  TB_SOCK=$T/agent.sock
  stub STUB_GH_TOKEN=gh-tok-1
  stub STUB_SECRET_TOOL=claude-tok-1
}

# toolbox_file LINE... -> the operator's personal toolbox file
toolbox_file() {
  mkdir -p "$T/u/.config/lettucectl"
  printf '%s\n' "$@" >"$T/u/.config/lettucectl/toolbox"
  chmod 600 "$T/u/.config/lettucectl/toolbox"
}

run_env() { cat "$T/run-env.log" 2>/dev/null; }

test_toolbox_opens_the_agents_workspace() {
  toolbox_ready
  tb lucius
  ok_rc
  out_has "alfred: Lucius (agent-2)"
  log_has "--hostname toolbox-alfred --label lettucectl.toolbox=alfred --label lettucectl.agent=agent-2 --security-opt no-new-privileges --memory 8g --pids-limit 4096 --tmpfs /root:mode=700,exec --tmpfs /tmp:exec"
  log_has "-v $H/stacks/alfred/state/workspaces/agent-2:/work/agent-2 -w /work/agent-2"
  log_has "-e AGENT_ID=agent-2 -e LETTA_AGENT_ID=agent-2 -e AGENT_NAME=Lucius -e USER_CWD=/work/agent-2"
  log_has "-e PATH=/opt/claude-code/bin:/usr/local/bin:/usr/bin:/bin"
  log_has " lettuce-app-server:test bash"
}

test_toolbox_clears_the_images_compose_labels() {
  toolbox_ready
  tb lucius
  ok_rc
  # Inherited from the Compose-built image, they would make the toolbox one of the
  # stack's app-server containers to Compose and `stale_services`.
  log_has "docker run --rm --init -i --label com.docker.compose.project= --label com.docker.compose.service= --hostname"
  log_lacks "org.opencontainers.image.title="
}

test_toolbox_runs_a_command() {
  toolbox_ready
  tb lucius claude --resume
  ok_rc
  log_has " lettuce-app-server:test claude --resume"
}

test_toolbox_skips_what_the_host_lacks() {
  toolbox_ready
  tb lucius
  ok_rc
  log_lacks "ssh-agent.sock"
  log_lacks ".gitconfig"
  log_lacks "/root/.claude"
  log_lacks "GH_TOKEN"
  log_lacks "CLAUDE_CODE_OAUTH_TOKEN"
  out_has "toolbox: nothing of yours found"
  out_has "claude setup-token"
}

test_toolbox_brings_in_the_operators_tools() {
  toolbox_ready
  toolbox_tools
  tb lucius
  ok_rc
  out_has "toolbox: ssh-agent, git, gh, claude config, claude token"
  out_lacks "claude setup-token"
  log_has "-v $T/agent.sock:/run/ssh-agent.sock -e SSH_AUTH_SOCK=/run/ssh-agent.sock"
  log_has "-v $T/u/.ssh/known_hosts:/root/.ssh/known_hosts:ro"
  log_has "-v $T/u/.gitconfig:/root/.gitconfig:ro"
  log_has "-e GIT_CONFIG_KEY_1=credential.https://github.com.helper -e GIT_CONFIG_VALUE_1=!gh auth git-credential"
  log_has "-v $T/u/.claude/CLAUDE.md:/root/.claude/CLAUDE.md:ro"
  log_has "-v $T/u/.claude/skills:/root/.claude/skills:ro"
  log_lacks "settings.json"
}

test_toolbox_passes_tokens_by_name_only() {
  toolbox_ready
  toolbox_tools
  tb lucius
  ok_rc
  log_has "-e GH_TOKEN"
  log_has "-e CLAUDE_CODE_OAUTH_TOKEN"
  log_lacks "gh-tok-1"
  log_lacks "claude-tok-1"
  run_env | grep -qx "GH_TOKEN=gh-tok-1" || fail "GH_TOKEN not passed: $(run_env)"
  run_env | grep -qx "CLAUDE_CODE_OAUTH_TOKEN=claude-tok-1" || fail "Claude token not passed: $(run_env)"
}

test_toolbox_prefers_the_callers_claude_token() {
  toolbox_ready
  toolbox_tools
  TB_CLAUDE=env-tok-1 tb lucius
  ok_rc
  run_env | grep -qx "CLAUDE_CODE_OAUTH_TOKEN=env-tok-1" || fail "caller's token not used: $(run_env)"
}

test_toolbox_reads_the_personal_file() {
  toolbox_ready
  mkdir -p "$T/u/shared" "$T/u/.aws" "$T/data"
  touch "$T/u/.aws/config"
  toolbox_file "# extras" "" \
    "mount ~/shared" \
    "mount ~/.aws/config ~/.aws/config" \
    "mount $T/data /data rw" \
    "secret TEST_TOKEN printf tok-123" \
    "env AWS_PROFILE=dev"
  tb lucius
  ok_rc
  out_has "+5 from $T/u/.config/lettucectl/toolbox"
  log_has "-v $T/u/shared:$T/u/shared:ro"
  log_has "-v $T/u/.aws/config:/root/.aws/config:ro"
  log_has "-v $T/data:/data:rw"
  log_has "-e AWS_PROFILE=dev"
  log_has "-e TEST_TOKEN"
  log_lacks "tok-123"
  run_env | grep -qx "TEST_TOKEN=tok-123" || fail "secret not passed: $(run_env)"
}

test_toolbox_file_overrides_built_in_tokens() {
  toolbox_ready
  toolbox_tools
  toolbox_file "secret GH_TOKEN printf file-tok"
  tb lucius
  ok_rc
  eq "$(run_env | grep -c '^GH_TOKEN=')" 1
  run_env | grep -qx "GH_TOKEN=file-tok" || fail "file token not used: $(run_env)"
}

test_toolbox_never_sources_the_personal_file() {
  toolbox_ready
  # shellcheck disable=SC2016 # the literal line a sourced file would run
  toolbox_file 'mount $(touch '"$T"'/pwned)'
  tb lucius
  bad_rc
  [[ ! -e $T/pwned ]] || fail "the personal file was executed"
  log_lacks "docker run"
}

test_toolbox_refuses_unknown_directives() {
  toolbox_ready
  toolbox_file "volume ~/shared"
  tb lucius
  bad_rc
  out_has "unknown directive 'volume'"
  log_lacks "docker run"
}

test_toolbox_refuses_missing_mounts() {
  toolbox_ready
  toolbox_file "mount ~/nope"
  tb lucius
  bad_rc
  out_has "$T/u/nope does not exist"
  log_lacks "docker run"
}

test_toolbox_refuses_a_personal_file_others_can_write() {
  toolbox_ready
  toolbox_file "env A=b"
  chmod 666 "$T/u/.config/lettucectl/toolbox"
  tb lucius
  bad_rc
  out_has "writable by others"
  log_lacks "docker run"
}

test_toolbox_fails_when_a_secret_command_fails() {
  toolbox_ready
  toolbox_file "secret TEST_TOKEN false"
  tb lucius
  bad_rc
  out_has "secret TEST_TOKEN"
  log_lacks "docker run"
}

test_toolbox_needs_the_stack_image() {
  toolbox_ready
  stub STUB_IMAGE_RC=1
  tb lucius
  bad_rc
  out_has "lettucectl up alfred"
  log_lacks "docker run"
}

test_toolbox_needs_the_agents_workspace() {
  toolbox_ready
  rmdir "$H/stacks/alfred/state/workspaces/agent-2"
  tb lucius
  bad_rc
  out_has "no workspace"
  log_lacks "docker run"
}

test_toolbox_lists_agents_when_none_match() {
  toolbox_ready
  tb hal
  bad_rc
  out_has "alfred has no agent 'hal'; agents: Alfred, Lucius"
  log_lacks "docker run"
}

test_toolbox_requires_an_agent() {
  toolbox_ready
  tb
  bad_rc
  out_has "usage: lettucectl toolbox"
}

test_toolbox_leaves_out_agent_tools_by_default() {
  toolbox_ready
  mkdir -p "$H/stacks/alfred/state/tools"
  tb lucius
  ok_rc
  log_lacks "/opt/agent-tools"
  out_lacks "agent tools"
}

test_toolbox_mounts_agent_tools_read_only() {
  toolbox_ready
  mkdir -p "$H/stacks/alfred/state/tools"
  tb --tools lucius
  ok_rc
  log_has "-v $H/stacks/alfred/state/tools:/opt/agent-tools:ro"
  out_has "toolbox: agent tools (read-only)"
}

test_toolbox_mounts_agent_tools_writable() {
  toolbox_ready
  mkdir -p "$H/stacks/alfred/state/tools"
  tb --tools=rw lucius
  ok_rc
  log_has "-v $H/stacks/alfred/state/tools:/opt/agent-tools:rw"
  out_has "toolbox: agent tools (writable)"
}

test_toolbox_tools_flag_keeps_the_command() {
  toolbox_ready
  mkdir -p "$H/stacks/alfred/state/tools"
  tb --tools lucius claude --resume
  ok_rc
  log_has " lettuce-app-server:test claude --resume"
}

test_toolbox_tools_needs_the_tools_dir() {
  toolbox_ready
  tb --tools lucius
  bad_rc
  out_has "lettucectl up alfred"
  log_lacks "docker run"
}

test_toolbox_refuses_an_unknown_tools_mode() {
  toolbox_ready
  mkdir -p "$H/stacks/alfred/state/tools"
  tb --tools=wx lucius
  bad_rc
  out_has "usage: lettucectl toolbox"
  log_lacks "docker run"
}

# ── skills ───────────────────────────────────────────────────────────────────
# skill NAME... -> an agent skill in the repo copy
skill() {
  local s
  for s in "$@"; do
    mkdir -p "$H/agent-skills/$s"
    printf -- '---\nname: %s\ndescription: test\n---\n' "$s" >"$H/agent-skills/$s/SKILL.md"
  done
}
shipped() { tar -tf "$T/exec-stdin.log" | LC_ALL=C sort | paste -sd' '; }

test_skills_installs_every_skill_dir() {
  added
  skill alpha beta
  mkdir -p "$H/agent-skills/notes"
  touch "$H/agent-skills/notes/README.md"
  ctl skills alfred
  ok_rc
  out_has "alfred: 2 skill(s) installed"
  eq "$(shipped)" "alpha/ alpha/SKILL.md beta/ beta/SKILL.md"
  log_has "-p lettuce-alfred"
  log_has "exec -T app-server sh -c"
  log_has "sh /root/.letta/skills alpha beta"
}

test_skills_all_covers_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  skill alpha
  : >"$T/calls.log"
  ctl skills all
  ok_rc
  out_has "alfred: 1 skill(s) installed"
  out_has "hal: 1 skill(s) installed"
  log_has "-p lettuce-hal"
}

test_skills_removes_all_when_the_repo_has_none() {
  added
  ctl skills alfred
  ok_rc
  out_has "alfred: 0 skill(s) installed"
  eq "$(shipped)" ""
}

test_skills_refuses_upstream_names() {
  added
  mkdir -p "$H/upstream/docker/agent-skills/serving-web-apps"
  skill serving-web-apps
  ctl skills alfred
  bad_rc
  out_has "skill 'serving-web-apps' is also shipped by upstream Lettuce"
  log_lacks " exec "
}

test_skills_refuses_bad_names() {
  added
  skill My.Skill
  ctl skills alfred
  bad_rc
  out_has "invalid skill name 'My.Skill'"
  log_lacks " exec "
}

test_skills_fails_when_the_stack_is_down() {
  added
  skill alpha
  stub STUB_EXEC_RC=1
  ctl skills alfred
  bad_rc
  out_has "alfred: could not install skills (is the stack up?)"
}

test_skills_rejects_unknown_stacks() {
  ctl skills hal
  bad_rc
  out_has "no stack named 'hal'"
}

test_skills_ships_the_repo_skills() {
  added
  cp -r "$ROOT/agent-skills" "$H/"
  ctl skills alfred
  ok_rc
  tar -tf "$T/exec-stdin.log" | grep -qx "scheduling-scripts/SKILL.md" || fail "scheduling-scripts not shipped"
  grep -qx "name: scheduling-scripts" "$ROOT/agent-skills/scheduling-scripts/SKILL.md" || fail "SKILL.md name mismatch"
  for f in using-vault-secrets/SKILL.md using-vault-secrets/vault-run; do
    tar -tf "$T/exec-stdin.log" | grep -qx "$f" || fail "$f not shipped"
  done
  grep -qx "name: using-vault-secrets" "$ROOT/agent-skills/using-vault-secrets/SKILL.md" || fail "SKILL.md name mismatch"
}

# The script lettucectl runs in the container, run here on a stand-in skills dir.
test_skills_script_replaces_only_its_own_skills() {
  added
  skill alpha beta
  ctl skills alfred
  ok_rc
  local d=$T/skills
  mkdir -p "$d/serving-web-apps" "$d/old" "$d/alpha"
  touch "$d/serving-web-apps/SKILL.md" "$d/old/SKILL.md" "$d/alpha/stale.txt"
  printf 'old\nalpha\n' >"$d/.lettucectl-skills"
  sh -c "$(cat "$T/exec-script")" sh "$d" alpha beta <"$T/exec-stdin.log"
  eq "$(cd "$d" && find . | LC_ALL=C sort | paste -sd' ')" \
    ". ./.lettucectl-skills ./alpha ./alpha/SKILL.md ./beta ./beta/SKILL.md ./serving-web-apps ./serving-web-apps/SKILL.md"
  eq "$(paste -sd' ' "$d/.lettucectl-skills")" "alpha beta"
}

test_skills_script_keeps_skills_on_a_broken_stream() {
  added
  skill alpha
  ctl skills alfred
  ok_rc
  local d=$T/skills
  mkdir -p "$d/alpha"
  touch "$d/alpha/SKILL.md"
  printf 'alpha\n' >"$d/.lettucectl-skills"
  ! sh -c "$(cat "$T/exec-script")" sh "$d" alpha <<<'not a tar' 2>/dev/null || fail "script accepted a broken stream"
  eq "$(cd "$d" && find . | LC_ALL=C sort | paste -sd' ')" ". ./.lettucectl-skills ./alpha ./alpha/SKILL.md"
}

# ── gmail-triggers executor ──────────────────────────────────────────────────
# run_exec_script ROOT [STDIN] -> runs the last exec's script with its arguments
# as lettucectl passed them, but with the container's /root/.letta and /etc
# under ROOT; stdin is the streamed tar unless STDIN is given
run_exec_script() {
  local r=$1 in=${2:-$T/exec-stdin.log} a args=()
  while IFS= read -r -d '' a; do
    case $a in
      /root/.letta/*) a=$r/letta/${a#/root/.letta/} ;;
      /etc/*) a=$r/etc/${a#/etc/} ;;
    esac
    args+=("$a")
  done <"$T/exec-args"
  sh -c "$(cat "$T/exec-script")" "${args[@]}" <"$in"
}
files_in() { (cd "$1" && find . | LC_ALL=C sort | paste -sd' '); }
INSTALLED=". ./etc ./etc/cron.d ./etc/cron.d/lettuce-garden-gmail-triggers ./etc/lettuce-garden ./etc/lettuce-garden/gmail-triggers-token ./letta ./letta/garden ./letta/garden/gmail-triggers ./letta/garden/gmail-triggers/bin ./letta/garden/gmail-triggers/bin/run.mjs ./letta/system-cron ./letta/system-cron/lettuce-garden-gmail-triggers"

# installed_up -> alfred with the profile, after an `up` whose install script can be replayed
installed_up() {
  added
  with_triggers
  ctl up alfred
  ok_rc
}

test_executor_script_installs_files_token_and_cron() {
  installed_up
  local r=$T/root g
  g=$r/letta/garden/gmail-triggers
  run_exec_script "$r"
  eq "$(files_in "$r")" "$INSTALLED"
  eq "$(cat "$g/bin/run.mjs")" "// executor"
  # The token is in the container layer, not under the state directory.
  eq "$(cat "$r/etc/lettuce-garden/gmail-triggers-token")" "$(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN)"
  eq "$(stat -c %a "$r/etc/lettuce-garden/gmail-triggers-token")" 600
  eq "$(stat -c %a "$r/etc/lettuce-garden")" 700
  eq "$(cat "$r/letta/system-cron/lettuce-garden-gmail-triggers")" "$GT_CRON_TEXT"
  cmp -s "$r/letta/system-cron/lettuce-garden-gmail-triggers" "$r/etc/cron.d/lettuce-garden-gmail-triggers" || fail "cron copies differ"
  eq "$(stat -c %a "$r/etc/cron.d/lettuce-garden-gmail-triggers")" 644
  eq "$(stat -c %a "$r/letta/system-cron/lettuce-garden-gmail-triggers")" 644
}

test_executor_script_replaces_its_files() {
  installed_up
  local r=$T/root g
  g=$r/letta/garden/gmail-triggers
  mkdir -p "$g/bin" "$r/etc/lettuce-garden"
  touch "$g/bin/notes.txt" "$g/executor.log"
  printf 'stale\n' >"$g/bin/run.mjs"
  printf 'old-token\n' >"$r/etc/lettuce-garden/gmail-triggers-token"
  run_exec_script "$r"
  eq "$(files_in "$g")" ". ./bin ./bin/notes.txt ./bin/run.mjs ./executor.log"
  eq "$(cat "$g/bin/run.mjs")" "// executor"
  eq "$(cat "$r/etc/lettuce-garden/gmail-triggers-token")" "$(senv alfred GMAIL_TRIGGERS_EXECUTOR_TOKEN)"
  run_exec_script "$r"
  eq "$(files_in "$g")" ". ./bin ./bin/notes.txt ./bin/run.mjs ./executor.log"
}

test_executor_script_keeps_the_install_on_a_broken_stream() {
  installed_up
  local r=$T/root before
  run_exec_script "$r"
  before=$(files_in "$r")
  printf 'not a tar' >"$T/broken"
  ! run_exec_script "$r" "$T/broken" 2>/dev/null || fail "script accepted a broken stream"
  eq "$(files_in "$r")" "$before"
  eq "$(cat "$r/letta/garden/gmail-triggers/bin/run.mjs")" "// executor"
}

# partial_stream FILE -> a tar holding only FILE
partial_stream() {
  rm -rf "$T/part"
  mkdir -p "$T/part"
  printf 'x\n' >"$T/part/$1"
  tar -C "$T/part" -cf "$T/part.tar" "$1"
}

test_executor_script_changes_nothing_on_an_incomplete_stream() {
  installed_up
  local r=$T/root f
  mkdir -p "$r"
  for f in run.mjs token; do
    partial_stream "$f"
    ! run_exec_script "$r" "$T/part.tar" 2>/dev/null || fail "script accepted a stream with only $f"
    eq "$(files_in "$r")" "."
  done
}

test_executor_script_uninstall_removes_executor_token_and_both_cron_files() {
  installed_up
  local r=$T/root
  run_exec_script "$r"
  touch "$r/letta/garden/gmail-triggers/executor.log"
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google/' "$H/stacks/alfred/stack.env"
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  ctl up alfred
  ok_rc
  run_exec_script "$r" /dev/null
  eq "$(files_in "$r")" ". ./etc ./etc/cron.d ./letta ./letta/garden ./letta/garden/gmail-triggers ./letta/garden/gmail-triggers/executor.log ./letta/system-cron"
  # Again, on what is left: nothing to do, no error.
  run_exec_script "$r" /dev/null
}

test_executor_script_uninstall_keeps_a_token_dir_in_use() {
  installed_up
  local r=$T/root
  run_exec_script "$r"
  touch "$r/etc/lettuce-garden/other"
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google/' "$H/stacks/alfred/stack.env"
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  ctl up alfred
  ok_rc
  run_exec_script "$r" /dev/null
  eq "$(files_in "$r/etc/lettuce-garden")" ". ./other"
}

# ── receipts ─────────────────────────────────────────────────────────────────
# receipt_files -> the pipelock containers list two sessions: proxy.run.b (newer,
# two files) and proxy.run.a
receipt_files() {
  printf '%s\n' "1760000000 /data/evidence/evidence-proxy.run.a-1.jsonl" \
    "1760000300 /data/evidence/evidence-proxy.run.b-1.jsonl" "1760000600 /data/evidence/evidence-proxy.run.b-2.jsonl" >"$T/run-stdout"
}

PIPELOCK_RUN="docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --label com.docker.compose.project= --label com.docker.compose.service= --label lettucectl.receipts=alfred -v lettuce-alfred_pipelock-data:/data:ro"

test_receipts_verifies_and_lists_sessions_newest_first() {
  added
  receipt_files
  ctl receipts alfred
  ok_rc
  log_has "$PIPELOCK_RUN --entrypoint pipelock lettuce-pipelock:local verify-receipt --whole-recorder --chain /data/evidence --key /data/keys/receipt-signing.json.pub"
  eq "$(grep -o 'proxy\.run\.[ab]$' "$OUT" | paste -sd' ')" "proxy.run.b proxy.run.a"
}

test_receipts_writes_the_newest_session_by_default() {
  added
  receipt_files
  ctl receipts alfred "$T/report.html"
  ok_rc
  log_has "$PIPELOCK_RUN --entrypoint pipelock lettuce-pipelock:local evidence view --receipt-dir /data/evidence --session proxy.run.b --trusted-signer file=/data/keys/receipt-signing.json.pub"
  eq "$(stat -c %a "$T/report.html")" 600
  out_has "alfred: session proxy.run.b -> $T/report.html"
  eq "$(grep -c 'image inspect lettuce-pipelock:local' "$T/calls.log")" 1
}

test_receipts_makes_an_existing_report_private() {
  added
  receipt_files
  echo old >"$T/report.html"
  chmod 644 "$T/report.html"
  ctl receipts alfred "$T/report.html"
  ok_rc
  eq "$(stat -c %a "$T/report.html")" 600
  grep -q proxy.run.b "$T/report.html" || fail "report not replaced"
}

test_receipts_keeps_an_existing_file_when_it_fails() {
  added
  receipt_files
  echo keep >"$T/report.html"
  stub STUB_RUN_FAIL=--receipt-dir
  ctl receipts alfred "$T/report.html"
  bad_rc
  eq "$(cat "$T/report.html")" keep
  eq "$(find "$T" -maxdepth 1 -name '.receipts.*')" ""
}

verifications() { grep -c "verify-receipt" "$T/calls.log" || true; }

test_receipts_retries_a_verification_that_fails_once() {
  added
  receipt_files
  stub STUB_RUN_FAIL=verify-receipt STUB_RUN_FAIL_TIMES=1
  ctl receipts alfred
  ok_rc
  eq "$(verifications)" 2
  out_has "proxy.run.b"
}

test_receipts_says_when_they_do_not_verify() {
  added
  receipt_files
  stub STUB_RUN_FAIL=verify-receipt
  ctl receipts alfred
  bad_rc
  eq "$(verifications)" 3
  out_has "alfred: the receipts do not verify"
  out_lacks "pipelock is running"
}

test_receipts_says_when_pipelock_was_writing() {
  added
  receipt_files
  containers alfred app-server pipelock
  stub STUB_RUN_FAIL=verify-receipt
  ctl receipts alfred
  bad_rc
  out_has "pipelock is running"
}

test_receipts_writes_a_named_session() {
  added
  receipt_files
  ctl receipts alfred "$T/report.html" proxy.run.a
  ok_rc
  log_has "--session proxy.run.a "
  ctl receipts alfred "$T/report.html" proxy.run.zz
  bad_rc
  out_has "no session proxy.run.zz"
}

test_receipts_refuses_a_report_inside_the_repo() {
  added
  receipt_files
  ctl receipts alfred "$H/report.html"
  bad_rc
  out_has "cannot be inside $H"
  [[ ! -e $H/report.html ]] || fail "report written inside the repo"
}

test_receipts_needs_the_volume() {
  added
  stub STUB_VOLUMES_MISSING=lettuce-alfred_pipelock-data
  ctl receipts alfred
  bad_rc
  out_has "no pipelock receipts"
  log_lacks "docker run"
}

test_receipts_says_when_there_are_none_yet() {
  added
  ctl receipts alfred
  bad_rc
  out_has "pipelock has written no receipts yet"
  log_lacks "verify-receipt"
}

test_receipts_fails_when_its_container_fails() {
  added
  receipt_files
  stub STUB_RUN_RC=1
  ctl receipts alfred
  bad_rc
}

# ── backup ───────────────────────────────────────────────────────────────────
# backed -> a fresh alfred stack with a backup destination at $B
backed() {
  added
  B=$T/bk
  mkdir -p "$B"
}
backup_runs() { grep -c "^docker run .* lettuce-backup:local sh -c" "$T/calls.log" || true; }
snapshots() { find "$B/alfred" -mindepth 1 -maxdepth 1 ! -name .lock -printf '%f\n' | LC_ALL=C sort | paste -sd' '; }

test_backup_rejects_bad_destinations() {
  backed
  ctl backup alfred "$T/missing"
  bad_rc
  out_has "is not a directory"
  ctl backup alfred backup-host:/srv/backups
  bad_rc
  out_has "cannot contain ':' or ','"
  ctl backup alfred "$H/stacks/alfred"
  bad_rc
  out_has "cannot be inside"
  # The repo is public, and a backup holds stack.env and logins.
  mkdir -p "$H/backups"
  ctl backup alfred "$H/backups"
  bad_rc
  out_has "cannot be inside $H"
  ctl backup alfred "$H"
  bad_rc
  out_has "cannot be inside $H"
  ctl backup alfred "$B" --pause --live
  bad_rc
  out_has "use --pause or --live, not both"
  ctl backup alfred "$B" --keep 0
  bad_rc
  out_has "--keep needs"
  ctl backup alfred
  bad_rc
  out_has "usage: lettucectl backup"
  log_lacks "docker run"
}

test_backup_copies_a_stopped_stack_without_asking() {
  backed
  stub STUB_VOLUME_RC=1
  ctl backup alfred "$B"
  ok_rc
  out_lacks "Choice"
  out_lacks "vault"
  log_lacks " pause"
  log_has "docker build -q -t lettuce-backup:local $H/backup"
  # --init: sh as PID 1 would ignore the signal docker forwards on Ctrl-C.
  log_has "docker run --rm --init --network none --read-only --tmpfs /tmp --security-opt no-new-privileges --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --label lettucectl.backup=alfred"
  log_has "-v $H/stacks/alfred/state:/src/state:ro -v $H/stacks/alfred/stack.env:/src/stack.env:ro -v $B/alfred:/dst lettuce-backup:local"
  log_has "sh rsync /src /dst $(id -u):$(id -g)  .partial-"
  log_has "state/letta-home/mod-cache state/letta-home/claude/paste-cache state/letta-home/claude/backups"
  log_lacks "state/tools"
  log_lacks "/src/vault"
  [[ $(snapshots) =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || fail "expected one snapshot, got: $(snapshots)"
  eq "$(stat -c %a "$B/alfred")" 700
  out_has "alfred: $B/alfred/$(snapshots)"
}

test_backup_leaves_out_upstream_skills_and_tools() {
  backed
  mkdir -p "$H/upstream/docker/agent-skills/serving-web-apps"
  ctl backup alfred "$B" --no-tools
  ok_rc
  log_has "state/letta-home/skills/serving-web-apps state/tools"
}

test_backup_links_to_the_previous_snapshot() {
  backed
  mkdir -p "$B/alfred/20260101T000000Z" "$B/alfred/20260102T000000Z" "$B/alfred/notes"
  ctl backup alfred "$B"
  ok_rc
  log_has "$(id -u):$(id -g) 20260102T000000Z .partial-"
}

test_backup_removes_what_an_interrupted_run_left() {
  backed
  mkdir -p "$B/alfred/.partial-20260101T000000Z"
  ctl backup alfred "$B"
  ok_rc
  log_has "-v $B/alfred:/dst lettuce-backup:local rm -rf -- /dst/.partial-20260101T000000Z"
}

test_backup_includes_the_vault() {
  backed
  with_secrets
  ctl backup alfred "$B"
  ok_rc
  log_has "docker volume inspect lettuce-alfred_secret-broker-data"
  log_has "-v lettuce-alfred_secret-broker-data:/src/vault:ro"
}

# Turning the profile off keeps the volume; it still holds the vault.
test_backup_includes_a_vault_left_by_a_dropped_profile() {
  backed
  ctl backup alfred "$B"
  ok_rc
  log_has "-v lettuce-alfred_secret-broker-data:/src/vault:ro"
}

test_backup_skips_a_vault_that_does_not_exist() {
  backed
  with_secrets
  stub STUB_VOLUME_RC=1
  ctl backup alfred "$B"
  ok_rc
  out_has "the vault volume lettuce-alfred_secret-broker-data does not exist yet"
  log_lacks "/src/vault"
}

test_backup_includes_the_receipts() {
  backed
  stub STUB_VOLUMES_MISSING=lettuce-alfred_secret-broker-data
  ctl backup alfred "$B"
  ok_rc
  log_has "docker volume inspect lettuce-alfred_pipelock-data"
  log_has "-v lettuce-alfred_pipelock-data:/src/pipelock:ro -v $B/alfred:/dst"
  log_lacks "/src/vault"
}

test_backup_skips_receipts_that_do_not_exist() {
  backed
  stub STUB_VOLUMES_MISSING=lettuce-alfred_pipelock-data
  ctl backup alfred "$B"
  ok_rc
  log_has "-v lettuce-alfred_secret-broker-data:/src/vault:ro -v $B/alfred:/dst"
  log_lacks "/src/pipelock"
}

test_backup_pauses_a_running_stack_when_asked() {
  backed
  containers alfred app-server bff
  ctl backup alfred "$B" <<<"p"
  ok_rc
  out_has "alfred is running. A live copy can catch"
  eq "$(grep -oE ' (pause|unpause)$|lettuce-backup:local sh -c' "$T/calls.log" | paste -sd,)" \
    " pause,lettuce-backup:local sh -c, unpause"
}

test_backup_copies_live_when_asked() {
  backed
  containers alfred app-server
  ctl backup alfred "$B" <<<"l"
  ok_rc
  log_lacks " pause"
  eq "$(backup_runs)" 1
}

test_backup_aborts_without_an_answer() {
  backed
  containers alfred app-server
  ctl backup alfred "$B" </dev/null
  bad_rc
  out_has "aborted (to answer ahead of time, as from cron: --pause or --live)"
  eq "$(backup_runs)" 0
  ctl backup alfred "$B" <<<"y"
  bad_rc
  eq "$(backup_runs)" 0
}

test_backup_flags_answer_ahead_of_time() {
  backed
  containers alfred app-server
  ctl backup alfred "$B" --pause </dev/null
  ok_rc
  out_lacks "Choice"
  log_has " unpause"
}

test_backup_unpauses_when_the_copy_fails() {
  backed
  containers alfred app-server
  stub STUB_RUN_RC=1
  ctl backup alfred "$B" --pause
  bad_rc
  out_has "alfred: the copy failed"
  log_has " unpause"
  eq "$(snapshots)" ""
}

test_backup_unpauses_when_the_pause_fails_partway() {
  backed
  containers alfred app-server bff
  stub STUB_PAUSE_RC=1
  ctl backup alfred "$B" --pause
  bad_rc
  out_has "alfred: could not pause the stack"
  log_has " unpause"
  eq "$(backup_runs)" 0
}

test_backup_refuses_a_second_run_of_the_same_stack() {
  command -v flock >/dev/null || return 0
  backed
  mkdir -p "$B/alfred/.partial-20260101T000000Z"
  local fd
  exec {fd}>"$B/alfred/.lock"
  flock "$fd"
  ctl backup alfred "$B"
  exec {fd}>&-
  bad_rc
  out_has "alfred: another backup of alfred is running"
  [[ -d $B/alfred/.partial-20260101T000000Z ]] || fail "the running backup's copy was deleted"
  eq "$(backup_runs)" 0
}

test_backup_all_goes_on_after_a_failure() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  B=$T/bk
  mkdir -p "$B"
  containers alfred app-server
  ctl backup all "$B" </dev/null
  bad_rc
  out_has "aborted"
  out_has "hal: $B/hal/"
  out_has "backup failed for: alfred"
  eq "$(find "$B" -mindepth 2 -maxdepth 2 -name '20*' | sed "s|^$B/||; s|/.*||")" hal
}

test_backup_compress_writes_a_private_archive() {
  backed
  ctl backup alfred "$B" --compress
  ok_rc
  log_has "sh tar /src     state/letta-home/mod-cache"
  log_lacks ":/dst"
  [[ $(snapshots) =~ ^[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]] || fail "expected one archive, got: $(snapshots)"
  eq "$(stat -c %a "$B/alfred/$(snapshots)")" 600
}

test_backup_keep_removes_older_snapshots() {
  backed
  mkdir -p "$B/alfred/20260101T000000Z" "$B/alfred/20260102T000000Z" "$B/alfred/20260103T000000Z" "$B/alfred/notes"
  ctl backup alfred "$B" --keep 2
  ok_rc
  log_has "rm -rf -- /dst/20260101T000000Z /dst/20260102T000000Z"
  log_lacks "/dst/20260103T000000Z"
  log_lacks "/dst/notes"
  out_has "alfred: removed 2 older backup(s)"
}

test_backup_keep_removes_older_archives() {
  backed
  mkdir -p "$B/alfred/20260103T000000Z"
  touch "$B/alfred/20260101T000000Z.tar.gz" "$B/alfred/20260102T000000Z.tar.gz" "$B/alfred/notes.tar.gz"
  ctl backup alfred "$B" --compress --keep 2
  ok_rc
  [[ ! -e $B/alfred/20260101T000000Z.tar.gz ]] || fail "oldest archive kept"
  [[ -e $B/alfred/20260102T000000Z.tar.gz && -e $B/alfred/notes.tar.gz && -d $B/alfred/20260103T000000Z ]] ||
    fail "removed more than the oldest archive: $(snapshots)"
}

test_backup_all_covers_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  B=$T/bk
  mkdir -p "$B"
  ctl backup all "$B"
  ok_rc
  [[ -d $B/alfred && -d $B/hal ]] || fail "not every stack backed up"
  log_has "lettucectl.backup=hal"
}

# The script lettucectl runs in the container, run here with the host's rsync and
# tar on a stand-in state.
test_backup_script_copies_what_matters() {
  command -v rsync >/dev/null || return 0
  backed
  mkdir -p "$H/upstream/docker/agent-skills/serving-web-apps"
  ctl backup alfred "$B" --no-tools
  ok_rc
  local src=$T/src dst=$T/dst args got
  mkdir -p "$src/state/letta-home/"{mod-cache,skills/serving-web-apps,skills/scheduling-scripts,skills/from-settings} \
    "$src/state/letta-home/claude/projects" "$src/state/workspaces/agent-1" "$src/state/tools/bin" "$src/vault" "$dst"
  touch "$src/stack.env" "$src/state/letta-home/mod-cache/x" "$src/state/letta-home/claude/projects/t.jsonl" \
    "$src/state/letta-home/skills/serving-web-apps/SKILL.md" "$src/state/letta-home/skills/scheduling-scripts/SKILL.md" \
    "$src/state/letta-home/skills/from-settings/SKILL.md" "$src/state/workspaces/agent-1/notes.md" \
    "$src/state/tools/bin/uv" "$src/vault/vault.db"
  printf 'scheduling-scripts\n' >"$src/state/letta-home/skills/.lettucectl-skills"
  # The same excludes lettucectl passed, after MODE SRC DST OWNER PREV OUT.
  args=$(grep -o ' \.partial-[^ ]* .*' "$T/calls.log" | head -1 | cut -d' ' -f3-)
  # shellcheck disable=SC2086 # the excludes are single words
  sh -c "$(cat "$T/run-script")" sh rsync "$src" "$dst" "$(id -u):$(id -g)" "" s1 $args
  got=$(cd "$dst/s1" && find . -type f | LC_ALL=C sort | paste -sd' ')
  eq "$got" "./stack.env ./state/letta-home/claude/projects/t.jsonl ./state/letta-home/skills/from-settings/SKILL.md ./state/workspaces/agent-1/notes.md ./vault/vault.db"
  # shellcheck disable=SC2086
  sh -c "$(cat "$T/run-script")" sh rsync "$src" "$dst" "$(id -u):$(id -g)" s1 s2 $args
  eq "$(stat -c %h "$dst/s2/state/workspaces/agent-1/notes.md")" 2
  # shellcheck disable=SC2086
  sh -c "$(cat "$T/run-script")" sh tar "$src" "" "" "" "" $args >"$T/a.tar.gz"
  got=$(tar -tzf "$T/a.tar.gz" | grep -v '/$' | LC_ALL=C sort | paste -sd' ')
  eq "$got" "./stack.env ./state/letta-home/claude/projects/t.jsonl ./state/letta-home/skills/from-settings/SKILL.md ./state/workspaces/agent-1/notes.md ./vault/vault.db"
}

for t in $(declare -F | awk '$3 ~ /^test_/ { print $3 }'); do
  run_test "$t"
done
printf '\n%d passed, %d failed\n' "$PASS" "${#FAILED[@]}"
((${#FAILED[@]} == 0))
