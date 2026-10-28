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
# first_body_of METHOD PATH -> the body of the first such call
first_body_of() { grep "^curl $1 $2 " "$T/calls.log" | head -1 | cut -d' ' -f4-; }
# ingress -> the tunnel routes of the last configuration put
ingress() { jq -c '.config.ingress' <<<"$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)"; }
# The ingress rules every stack ends with: alfred's garden host answers 404 to
# what no garden rule routes, and Lettuce's host goes to the app-server only.
LANDING='{"hostname":"alfred-garden.example.org","path":"^/(garden\\.css|icon-192\\.png)?$","service":"http://garden:8000"}'
GARDEN_404='{"hostname":"alfred-garden.example.org","service":"http_status:404"}'
LETTUCE_TAIL='{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}'

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

# What every garden service's environment renders to: the garden's own Access
# application and origin. The docker stub fills ${KEY} from the stack's env file.
# shellcheck disable=SC2016 # ${KEY} is for the stub, not for bash
GARDEN_ENV='{"CF_ACCESS_AUD":"${CF_GARDEN_AUD}","PUBLIC_ORIGIN":"${GARDEN_ORIGIN}"}'

# garden_config -> the rendered config also holds the garden service
# compose/garden.yml renders (on with any garden profile); the repo copy has
# the garden executor
garden_config() {
  # shellcheck disable=SC2016 # ${KEY} is for the stub, not for bash
  config_edit '.services.garden = {"read_only":true,"cap_drop":["ALL"],"tmpfs":["/tmp"],"networks":{"default":null},
    "environment":('"$GARDEN_ENV"' + {"LETTUCE_ORIGIN":"${PUBLIC_ORIGIN}"})}'
  mkdir -p "$H/garden/executor/tests"
  printf '// executor\n' >"$H/garden/executor/run.mjs"
  printf '// test\n' >"$H/garden/executor/tests/run.test.mjs"
}

# broker_config [BROKER_JSON] -> the rendered compose config also holds this
# secret-broker (default: the hardened one compose/secrets.yml renders) and the
# garden service
broker_config() {
  local broker=${1:-'{"read_only":true,"cap_drop":["ALL"],"environment":'"$GARDEN_ENV"',"volumes":[{"type":"volume","source":"secret-broker-data","target":"/data"}]}'}
  jq -c --argjson b "$broker" '.services["secret-broker"] = $b' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  garden_config
}

# config_edit JQ -> applies the jq filter JQ to the rendered compose config
config_edit() {
  jq -c "$1" "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
}

# triggers_config -> the rendered config also holds the gmail-triggers service
# compose/gmail-triggers.yml renders for alfred, the services upstream gives
# google-creds and the garden service; the repo copy has the executor
triggers_config() {
  config_edit '.services["gmail-triggers"] = {"read_only":true,"cap_drop":["ALL"],"user":"0","tmpfs":["/tmp"],
      "environment":'"$GARDEN_ENV"',"volumes":[
      {"type":"bind","source":"'"$H"'/stacks/alfred/state/garden/gmail-triggers","target":"/data"},
      {"type":"volume","source":"google-creds","target":"/creds","read_only":true},
      {"type":"volume","source":"google-policy","target":"/policy","read_only":true}]}
    | .services.bff.volumes = [{"type":"volume","source":"google-creds","target":"/app/google/creds"}]
    | .services["google-mcp"] = {"volumes":[{"type":"volume","source":"google-creds","target":"/creds"}]}'
  garden_config
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
  # Lettuce's application, then the garden's; the garden's DNS record, then
  # Lettuce's, whose id means the stack is complete.
  eq "$(calls | tr '\n' ';')" "GET /zones/zone1/dns_records;GET /zones/zone1/dns_records;POST /accounts/acc1/access/policies;POST /accounts/acc1/access/apps;POST /accounts/acc1/access/apps;POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;POST /zones/zone1/dns_records;"
  eq "$(grep '^curl POST /accounts/acc1/access/apps ' "$T/calls.log" | cut -d' ' -f4- | jq -r .name | paste -sd' ')" "lettuce-alfred lettuce-alfred-garden"
  eq "$(grep '^curl POST /zones/zone1/dns_records ' "$T/calls.log" | cut -d' ' -f4- | jq -r .name | paste -sd' ')" "alfred-garden.example.org alfred.example.org"
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
  b=$(first_body_of POST /accounts/acc1/access/apps)
  eq "$(jq -c '[.type, .domain, .destinations[0].uri, .allowed_idps, .auto_redirect_to_identity, .policies]' <<<"$b")" \
    '["self_hosted","alfred.example.org","alfred.example.org",["idp-g"],true,[{"id":"pol1","precedence":1}]]'
  # No script on the stack's origin may read the Access token.
  eq "$(jq -c '[.http_only_cookie_attribute, .same_site_cookie_attribute]' <<<"$b")" '[true,"lax"]'
  # The garden's application: its own host and audience, the same policy and cookie.
  local g
  g=$(body_of POST /accounts/acc1/access/apps)
  eq "$(jq -c '[.name, .domain, .destinations[0].uri, .policies]' <<<"$g")" \
    '["lettuce-alfred-garden","alfred-garden.example.org","alfred-garden.example.org",[{"id":"pol1","precedence":1}]]'
  eq "$(jq -c 'del(.name, .domain, .destinations)' <<<"$g")" "$(jq -c 'del(.name, .domain, .destinations)' <<<"$b")"
  b=$(body_of POST /accounts/acc1/cfd_tunnel)
  eq "$(jq -c '[.name, .config_src]' <<<"$b")" '["lettuce-alfred","cloudflare"]'
  eq "$(ingress)" "[$GARDEN_404,$LETTUCE_TAIL]"
  b=$(first_body_of POST /zones/zone1/dns_records)
  eq "$(jq -c '[.type, .name, .content, .proxied]' <<<"$b")" '["CNAME","alfred-garden.example.org","tun1.cfargotunnel.com",true]'
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
  eq "$(senv alfred GARDEN_HOSTNAME)" alfred-garden.example.org
  eq "$(senv alfred GARDEN_ORIGIN)" https://alfred-garden.example.org
  eq "$(senv alfred CF_GARDEN_APP_ID)" app2
  eq "$(senv alfred CF_GARDEN_AUD)" aud2
  eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" rec2
  eq "$(senv alfred CF_DNS_RECORD_ID)" rec1
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
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/secrets(/|$)","service":"http://secret-broker:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
  out_has "Its garden pages: https://alfred-garden.example.org/"
}

test_add_routes_gmail_triggers_with_the_profile() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers,secrets/' "$H/config.env"
  broker_config
  triggers_config
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/secrets(/|$)","service":"http://secret-broker:8000"},{"hostname":"alfred-garden.example.org","path":"^/gmail-triggers(/|$)","service":"http://gmail-triggers:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
}

# The landing page links each garden profile to /<profile>/ on the garden host,
# so every PROFILE_ROUTES row must route that path to its sidecar.
test_garden_routes_follow_the_profile_names() {
  local r p path svc
  # shellcheck source=/dev/null
  source <(sed -n '/^PROFILE_ROUTES=(/,/^)/p' "$ROOT/lib/cloudflare.sh")
  ((${#PROFILE_ROUTES[@]} > 0)) || fail "no PROFILE_ROUTES"
  for r in "${PROFILE_ROUTES[@]}"; do
    read -r p path svc <<<"$r"
    eq "$path" "^/$p(/|\$)"
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
  eq "$(senv alfred GARDEN_ORIGIN)" https://alfred-garden.example.org
}

test_add_falls_back_when_the_garden_name_is_taken() {
  echo "alfred-garden.example.org CNAME elsewhere.example.net." >"$T/dns.txt"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(senv alfred PUBLIC_ORIGIN)" https://alfred.example.org
  eq "$(senv alfred GARDEN_HOSTNAME)" lettuce-alfred-garden.example.org
  eq "$(senv alfred GARDEN_ORIGIN)" https://lettuce-alfred-garden.example.org
  stub STUB_CF_TAKEN=alfred-garden.example.org
  rm "$T/dns.txt"
  ctl add hal hal@example.com
  ok_rc
  eq "$(senv hal GARDEN_HOSTNAME)" hal-garden.example.org
}

test_add_fails_when_both_garden_names_taken() {
  stub 'STUB_CF_TAKEN="alfred-garden.example.org lettuce-alfred-garden.example.org"'
  ctl add alfred alfred@example.com
  bad_rc
  out_has "both alfred-garden.example.org and lettuce-alfred-garden.example.org are taken"
  [[ ! -e $H/stacks/alfred ]] || fail "stack dir left behind"
  log_lacks "curl POST"
}

# <name>-garden.<domain> is the garden host of <name>, so no new stack may take it.
test_add_refuses_a_name_ending_in_garden() {
  ctl add alfred-garden alfred@example.com
  bad_rc
  out_has "invalid stack name 'alfred-garden': it may not end in -garden"
  log_lacks "curl "
  [[ ! -e $H/stacks/alfred-garden ]] || fail "stack dir created for a refused name"
}

test_an_existing_stack_ending_in_garden_keeps_working() {
  added
  mv "$H/stacks/alfred" "$H/stacks/hal-garden"
  ctl up hal-garden
  ok_rc
  log_has "-p lettuce-hal-garden"
  ctl check hal-garden
  ok_rc
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
  eq "$(senv alfred CF_GARDEN_APP_ID)" app2
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;POST /zones/zone1/dns_records;"
}

test_add_resumes_after_a_failed_garden_application() {
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl add alfred alfred@example.com
  bad_rc
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "POST /accounts/acc1/access/apps;POST /accounts/acc1/access/apps;POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;POST /zones/zone1/dns_records;"
  eq "$(senv alfred CF_GARDEN_AUD)" aud2
}

test_add_resumes_after_a_failed_garden_dns_record() {
  # The garden's record comes first, so this fails before Lettuce's.
  stub 'STUB_CF_FAIL="POST /zones/zone1/dns_records"'
  ctl add alfred alfred@example.com
  bad_rc
  eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" ""
  eq "$(senv alfred CF_DNS_RECORD_ID)" ""
  log_lacks "up -d"
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;POST /zones/zone1/dns_records;"
  eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" rec2
  eq "$(senv alfred CF_DNS_RECORD_ID)" rec1
}

test_add_resumes_a_setup_from_before_the_garden_host() {
  stub 'STUB_CF_FAIL="POST /accounts/acc1/cfd_tunnel"'
  ctl add alfred alfred@example.com
  bad_rc
  sed -i '/^GARDEN_/d; /^CF_GARDEN_/d' "$H/stacks/alfred/stack.env"
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "GET /zones/zone1/dns_records;POST /accounts/acc1/access/apps;POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;POST /zones/zone1/dns_records;"
  eq "$(senv alfred GARDEN_ORIGIN)" https://alfred-garden.example.org
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
  out_has "the garden executor is out of step on: alfred; fix the cause, then run: lettucectl up <name>"
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
  local np=localhost,127.0.0.1,::1,pipelock,secret-broker,gmail-triggers,garden,searxng,ddg-mcp,google-mcp,host.docker.internal
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
    for v in secret-broker gmail-triggers garden; do
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
  triggers_refused ".services.bff.volumes_from = [\"gmail-triggers:ro\"]" "volumes_from reaches the Google token, the vault, the gmail-triggers rules, the pipelock receipts or the code editor (bff from gmail-triggers)"
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
  out_has "alfred: volumes_from reaches the Google token, the vault, the gmail-triggers rules, the pipelock receipts or the code editor (searxng from bff)"
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
  sed -i '/^GARDEN_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "alfred: GARDEN_EXECUTOR_TOKEN must be 64 lowercase hex characters"
}

test_check_refuses_a_malformed_executor_token() {
  added
  with_triggers
  sed -i 's/^GARDEN_EXECUTOR_TOKEN=.*/GARDEN_EXECUTOR_TOKEN=ABCDEF/' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "GARDEN_EXECUTOR_TOKEN must be 64 lowercase hex characters"
}

test_check_refuses_a_missing_executor_token_with_any_garden_profile() {
  added
  with_secrets
  sed -i '/^GARDEN_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "alfred: GARDEN_EXECUTOR_TOKEN must be 64 lowercase hex characters"
}

# Stacks from before the garden executor hold their token under its old name.
test_up_moves_the_gmail_triggers_executor_token_over_once() {
  added
  local tok
  tok=$(senv alfred GARDEN_EXECUTOR_TOKEN)
  sed -i "s/^GARDEN_EXECUTOR_TOKEN=.*/GMAIL_TRIGGERS_EXECUTOR_TOKEN=$tok/" "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  eq "$(senv alfred GARDEN_EXECUTOR_TOKEN)" "$tok"
  ! grep -q '^GMAIL_TRIGGERS_EXECUTOR_TOKEN=' "$H/stacks/alfred/stack.env" || fail "the old key is still there"
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
  # With both, the new one stays and the old one goes.
  echo "GMAIL_TRIGGERS_EXECUTOR_TOKEN=$(printf 'a%.0s' {1..64})" >>"$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  eq "$(senv alfred GARDEN_EXECUTOR_TOKEN)" "$tok"
  ! grep -q '^GMAIL_TRIGGERS_EXECUTOR_TOKEN=' "$H/stacks/alfred/stack.env" || fail "the old key is still there"
}

test_check_needs_no_executor_token_without_the_profile() {
  added
  sed -i '/^GARDEN_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  ok_rc
}

test_up_creates_the_executor_token_once() {
  added
  local tok
  tok=$(senv alfred GARDEN_EXECUTOR_TOKEN)
  [[ $tok =~ ^[0-9a-f]{64}$ ]] || fail "bad token [$tok]"
  ctl up alfred
  ok_rc
  eq "$(senv alfred GARDEN_EXECUTOR_TOKEN)" "$tok"
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
  sed -i '/^GARDEN_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  [[ $(senv alfred GARDEN_EXECUTOR_TOKEN) =~ ^[0-9a-f]{64}$ ]] || fail "token not recreated"
}

test_executor_tokens_differ_between_stacks() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  [[ $(senv alfred GARDEN_EXECUTOR_TOKEN) != "$(senv hal GARDEN_EXECUTOR_TOKEN)" ]] || fail "same token"
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

# executor_cron_text [FLAGS] -> the cron file the executor is installed with
executor_cron_text() {
  printf '%s\n' '# Installed by lettucectl (gmail-triggers or code-server profile). Removed with the last of them.' \
    'PATH=/usr/local/bin:/usr/bin:/bin' 'MAILTO=""'
  printf '%s' "* * * * * root /usr/local/bin/agent-env node /root/.letta/garden/executor/bin/run.mjs${1:+ $1} >> /root/.letta/garden/executor/executor.log 2>&1"
}
GT_CRON_TEXT=$(executor_cron_text --gmail-triggers)
EXECUTOR_ARGS="/root/.letta/garden/executor lettuce-garden-executor /root/.letta/system-cron /etc/cron.d /etc/lettuce-garden/executor-token"
LEGACY_ARGS="/root/.letta/garden/gmail-triggers lettuce-garden-gmail-triggers /root/.letta/system-cron /etc/cron.d /etc/lettuce-garden/gmail-triggers-token"
# legacy_executor -> alfred's letta-home shows the gmail-triggers executor from before the garden one
legacy_executor() { mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"; }

test_up_installs_the_gmail_triggers_executor() {
  added
  with_triggers
  ctl up alfred
  ok_rc
  log_has "exec -T app-server sh -c"
  # Only the executor and its token ship, not its tests.
  eq "$(shipped)" "run.mjs token"
  eq "$(exec_args | head -7 | paste -sd' ')" "sh install $EXECUTOR_ARGS"
  local a=()
  mapfile -d '' a <"$T/exec-args"
  eq "${a[7]}" "$GT_CRON_TEXT"
  eq "${#a[@]}" 8
}

# The executor feeds the garden's agent list, which only the code editor uses,
# so the code editor installs it too; only gmail-triggers turns on its request loop.
test_up_installs_the_executor_with_the_code_editor() {
  added
  with_code
  ctl up alfred
  ok_rc
  eq "$(exec_args | head -7 | paste -sd' ')" "sh install $EXECUTOR_ARGS"
  local a=()
  mapfile -d '' a <"$T/exec-args"
  eq "${a[7]}" "$(executor_cron_text)"
}

# The vault alone has no use for the executor: none is installed, and one left
# from before goes.
test_up_with_only_the_vault_has_no_executor() {
  added
  with_secrets
  ctl up alfred
  ok_rc
  log_lacks " exec "
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
  ctl up alfred
  ok_rc
  eq "$(exec_args | paste -sd' ')" "sh uninstall $EXECUTOR_ARGS"
  out_has "alfred: removed the garden executor (neither gmail-triggers nor code-server is on)"
}

test_up_replaces_the_gmail_triggers_executor_once_installed() {
  added
  with_triggers
  legacy_executor
  ctl up alfred
  ok_rc
  eq "$(grep -c 'exec -T app-server sh -c' "$T/calls.log")" 2
  (($(line_of " install $EXECUTOR_ARGS") < $(line_of " uninstall $LEGACY_ARGS"))) || fail "the old executor went before the new one was in"
  eq "$(exec_args | paste -sd' ')" "sh uninstall $LEGACY_ARGS"
}

test_up_keeps_the_gmail_triggers_executor_when_the_install_fails() {
  added
  with_triggers
  legacy_executor
  stub STUB_EXEC_RC=1
  ctl up alfred
  bad_rc
  eq "$(grep -c 'exec -T app-server sh -c' "$T/calls.log")" 1
  log_lacks "sh uninstall"
}

test_up_passes_the_executor_token_only_on_stdin() {
  added
  with_triggers
  ctl up alfred
  ok_rc
  local tok
  tok=$(senv alfred GARDEN_EXECUTOR_TOKEN)
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
  out_has "alfred: could not install the garden executor; the stack is up, but its garden gets no agent list and its Gmail triggers will not run"
  out_has "the garden executor is out of step on: alfred; fix the cause, then run: lettucectl up <name>"
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
  out_has "the garden executor is out of step on: alfred hal; fix the cause, then run: lettucectl up <name>"
}

test_up_without_a_garden_profile_runs_no_exec() {
  added
  ctl up alfred
  ok_rc
  log_lacks " exec "
}

test_up_removes_the_executor_when_the_profile_is_off() {
  added
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
  ctl up alfred
  ok_rc
  log_has "exec -T app-server sh -c"
  eq "$(exec_args | paste -sd' ')" "sh uninstall $EXECUTOR_ARGS"
  out_has "alfred: removed the garden executor"
}

test_up_removes_the_gmail_triggers_executor_when_no_garden_profile_is_on() {
  added
  legacy_executor
  ctl up alfred
  ok_rc
  eq "$(grep -c 'exec -T app-server sh -c' "$T/calls.log")" 1
  eq "$(exec_args | paste -sd' ')" "sh uninstall $LEGACY_ARGS"
  out_has "alfred: removed the garden executor"
  # Both: each goes.
  : >"$T/calls.log"
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
  ctl up alfred
  ok_rc
  log_has " uninstall $EXECUTOR_ARGS"
  log_has " uninstall $LEGACY_ARGS"
}

test_up_removes_an_executor_known_by_its_cron_file() {
  local job
  for job in lettuce-garden-executor lettuce-garden-gmail-triggers; do
    teardown; setup
    added
    mkdir -p "$H/stacks/alfred/state/letta-home/system-cron"
    touch "$H/stacks/alfred/state/letta-home/system-cron/$job"
    ctl up alfred
    ok_rc
    grep -q "sh uninstall /root/.letta/garden/[a-z-]* $job " "$T/calls.log" || fail "no uninstall of $job"
  done
}

test_up_fails_clearly_when_the_executor_cannot_be_removed() {
  added
  mkdir -p "$H/stacks/alfred/state/letta-home/system-cron"
  touch "$H/stacks/alfred/state/letta-home/system-cron/lettuce-garden-gmail-triggers"
  stub STUB_EXEC_RC=1
  ctl up alfred
  bad_rc
  out_has "alfred: could not remove the garden executor"
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
  log_has "--project-directory $H/upstream/docker -f $H/upstream/docker/compose.yml -f $H/compose/hardening.yml -f $H/compose/multi.yml -f $H/compose/secrets.yml -f $H/compose/gmail-triggers.yml -f $H/compose/pipelock.yml -f $H/compose/code-server.yml -f $H/compose/garden.yml --env-file $H/stacks/alfred/stack.env"
  log_lacks "pipelock-proxy.yml"
}

test_compose_adds_the_proxy_overlay_only_with_pipelock() {
  added
  with_pipelock
  ctl up alfred
  ok_rc
  log_has "-f $H/compose/pipelock.yml -f $H/compose/code-server.yml -f $H/compose/garden.yml -f $H/compose/pipelock-proxy.yml --env-file $H/stacks/alfred/stack.env"
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
  eq "$(calls | tr '\n' ';')" "DELETE /zones/zone1/dns_records/rec1;DELETE /zones/zone1/dns_records/rec2;DELETE /accounts/acc1/cfd_tunnel/tun1/connections;DELETE /accounts/acc1/cfd_tunnel/tun1;DELETE /accounts/acc1/access/apps/app2;DELETE /accounts/acc1/access/apps/app1;DELETE /accounts/acc1/access/policies/pol1;"
  [[ -d $H/stacks/alfred/state ]] || fail "state deleted without --purge"
  local k
  for k in CF_TUNNEL_ID CF_DNS_RECORD_ID CF_GARDEN_DNS_RECORD_ID CF_GARDEN_APP_ID CF_GARDEN_AUD CF_ACCESS_APP_ID CF_ACCESS_POLICY_ID; do
    eq "$(senv alfred "$k")" ""
  done
}

test_remove_keeps_the_ids_it_could_not_delete_and_retries_them() {
  added
  stub 'STUB_CF_FAIL="DELETE /accounts/acc1/access/apps/app2"'
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Access application app2"
  eq "$(senv alfred CF_GARDEN_APP_ID)" app2
  eq "$(senv alfred CF_ACCESS_APP_ID)" ""
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl remove alfred <<<"alfred"
  ok_rc
  eq "$(calls | tr '\n' ';')" "DELETE /accounts/acc1/access/apps/app2;"
  eq "$(senv alfred CF_GARDEN_AUD)" ""
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
  grep '^curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations ' "$T/calls.log" | grep -qF '"hostname":"hal-garden.example.org","path":"^/secrets(/|$)"' ||
    fail "hal's garden host has no /secrets route"
  # The route is in place before the stacks start.
  [[ $(grep -n '^curl PUT' "$T/calls.log" | tail -1 | cut -d: -f1) -lt $(grep -n 'up -d --build' "$T/calls.log" | head -1 | cut -d: -f1) ]] ||
    fail "stacks started before their route"
}

test_profiles_reapplies_every_stacks_access_application() {
  broker_config
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  local created
  created=$(first_body_of POST /accounts/acc1/access/apps)
  : >"$T/calls.log"
  ctl profiles cloudflared,search,secrets
  ok_rc
  # Stacks made before a setting existed (the HttpOnly cookie) get it too.
  eq "$(grep -c '^curl PUT /accounts/acc1/access/apps/app1 ' "$T/calls.log")" 2
  eq "$(body_of PUT /accounts/acc1/access/apps/app1 | jq -c 'del(.name, .domain, .destinations)')" \
    "$(jq -c 'del(.name, .domain, .destinations)' <<<"$created")"
  grep '^curl PUT /accounts/acc1/access/apps/app1 ' "$T/calls.log" | grep -qF '"name":"lettuce-hal","domain":"hal.example.org"' ||
    fail "hal's application was not re-applied with its own name and host"
  # And the garden's application, with its own name and host.
  eq "$(grep -c '^curl PUT /accounts/acc1/access/apps/app2 ' "$T/calls.log")" 2
  eq "$(body_of PUT /accounts/acc1/access/apps/app2 | jq -c 'del(.name, .domain, .destinations)')" \
    "$(jq -c 'del(.name, .domain, .destinations)' <<<"$created")"
  grep '^curl PUT /accounts/acc1/access/apps/app2 ' "$T/calls.log" | grep -qF '"name":"lettuce-hal-garden","domain":"hal-garden.example.org"' ||
    fail "hal's garden application was not re-applied with its own name and host"
  [[ $(grep -n '^curl PUT /accounts/acc1/access/apps' "$T/calls.log" | tail -1 | cut -d: -f1) -lt $(grep -n 'up -d --build' "$T/calls.log" | head -1 | cut -d: -f1) ]] ||
    fail "stacks started before their Access application was updated"
}

test_profiles_reports_a_failed_access_application_update() {
  local app
  for app in app1 app2; do
    teardown; setup
    added
    stub "STUB_CF_FAIL=\"PUT /accounts/acc1/access/apps/$app\""
    ctl profiles cloudflared,search
    bad_rc
    out_has "could not update the tunnel routes or Access application of: alfred"
    out_has "run the same command again"
  done
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
  sed -i '/^GARDEN_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search,google,gmail-triggers
  ok_rc
  [[ $(senv alfred GARDEN_EXECUTOR_TOKEN) =~ ^[0-9a-f]{64}$ ]] || fail "profiles gave no executor token"
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/gmail-triggers(/|$)","service":"http://gmail-triggers:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
  log_has "sh install $EXECUTOR_ARGS"
  [[ $(grep -n '^curl PUT' "$T/calls.log" | tail -1 | cut -d: -f1) -lt $(grep -n 'up -d --build' "$T/calls.log" | head -1 | cut -d: -f1) ]] ||
    fail "stack started before its route"
}

test_profiles_dropping_gmail_triggers_removes_route_container_and_executor() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers/' "$H/config.env"
  triggers_config
  added
  containers alfred app-server bff cloudflared google-mcp gmail-triggers garden
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
  ctl profiles cloudflared,google
  ok_rc
  log_has "curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations"
  log_lacks "gmail-triggers:8000"
  log_lacks "garden:8000"
  # The last garden profile is off, so the garden goes too.
  grep -q '^docker compose -p lettuce-alfred .* rm -sf garden gmail-triggers$' "$T/calls.log" || fail "gmail-triggers and garden containers not removed"
  log_has "sh uninstall $EXECUTOR_ARGS"
  out_has "alfred: removed the garden executor"
}

test_profiles_dropping_gmail_triggers_keeps_the_garden_for_the_vault_but_not_the_executor() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers,secrets/' "$H/config.env"
  triggers_config
  broker_config
  added
  containers alfred app-server bff cloudflared google-mcp gmail-triggers secret-broker garden
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
  : >"$T/calls.log"
  ctl profiles cloudflared,google,secrets
  ok_rc
  grep -q '^docker compose -p lettuce-alfred .* rm -sf gmail-triggers$' "$T/calls.log" || fail "only gmail-triggers should go"
  log_lacks "sh install"
  eq "$(exec_args | paste -sd' ')" "sh uninstall $EXECUTOR_ARGS"
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/secrets(/|$)","service":"http://secret-broker:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
}

test_profiles_rerun_removes_a_leftover_executor() {
  added
  containers alfred app-server
  legacy_executor
  ctl profiles cloudflared,search
  ok_rc
  out_has "nothing to change"
  log_has "sh uninstall $LEGACY_ARGS"
}

test_profiles_reports_an_executor_it_could_not_remove() {
  added
  containers alfred app-server
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/gmail-triggers/bin"
  stub STUB_EXEC_RC=1
  ctl profiles cloudflared,search
  bad_rc
  out_has "alfred: could not remove the garden executor"
  out_has "could not sync the garden executor of: alfred"
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
  log_has "sh install $EXECUTOR_ARGS"
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
  out_has "alfred: could not install the garden executor"
  out_has "hal: could not install the garden executor"
  out_has "could not sync the garden executor of: alfred hal"
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
  sed -i 's/^GARDEN_EXECUTOR_TOKEN=.*/GARDEN_EXECUTOR_TOKEN=not-hex/' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search,google,gmail-triggers
  bad_rc
  out_has "alfred: GARDEN_EXECUTOR_TOKEN must be 64 lowercase hex characters"
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
  out_has "https://hal-garden.example.org/"
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

# ── garden executor ──────────────────────────────────────────────────────────
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
INSTALLED=". ./etc ./etc/cron.d ./etc/cron.d/lettuce-garden-executor ./etc/lettuce-garden ./etc/lettuce-garden/executor-token ./letta ./letta/garden ./letta/garden/executor ./letta/garden/executor/bin ./letta/garden/executor/bin/run.mjs ./letta/system-cron ./letta/system-cron/lettuce-garden-executor"

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
  g=$r/letta/garden/executor
  run_exec_script "$r"
  eq "$(files_in "$r")" "$INSTALLED"
  eq "$(cat "$g/bin/run.mjs")" "// executor"
  # The token is in the container layer, not under the state directory.
  eq "$(cat "$r/etc/lettuce-garden/executor-token")" "$(senv alfred GARDEN_EXECUTOR_TOKEN)"
  eq "$(stat -c %a "$r/etc/lettuce-garden/executor-token")" 600
  eq "$(stat -c %a "$r/etc/lettuce-garden")" 700
  eq "$(cat "$r/letta/system-cron/lettuce-garden-executor")" "$GT_CRON_TEXT"
  cmp -s "$r/letta/system-cron/lettuce-garden-executor" "$r/etc/cron.d/lettuce-garden-executor" || fail "cron copies differ"
  eq "$(stat -c %a "$r/etc/cron.d/lettuce-garden-executor")" 644
  eq "$(stat -c %a "$r/letta/system-cron/lettuce-garden-executor")" 644
}

test_executor_script_replaces_its_files() {
  installed_up
  local r=$T/root g
  g=$r/letta/garden/executor
  mkdir -p "$g/bin" "$r/etc/lettuce-garden"
  touch "$g/bin/notes.txt" "$g/executor.log"
  printf 'stale\n' >"$g/bin/run.mjs"
  printf 'old-token\n' >"$r/etc/lettuce-garden/executor-token"
  run_exec_script "$r"
  eq "$(files_in "$g")" ". ./bin ./bin/notes.txt ./bin/run.mjs ./executor.log"
  eq "$(cat "$g/bin/run.mjs")" "// executor"
  eq "$(cat "$r/etc/lettuce-garden/executor-token")" "$(senv alfred GARDEN_EXECUTOR_TOKEN)"
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
  eq "$(cat "$r/letta/garden/executor/bin/run.mjs")" "// executor"
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
  touch "$r/letta/garden/executor/executor.log"
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google/' "$H/stacks/alfred/stack.env"
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
  ctl up alfred
  ok_rc
  run_exec_script "$r" /dev/null
  eq "$(files_in "$r")" ". ./etc ./etc/cron.d ./letta ./letta/garden ./letta/garden/executor ./letta/garden/executor/executor.log ./letta/system-cron"
  # Again, on what is left: nothing to do, no error.
  run_exec_script "$r" /dev/null
}

# The install's clean-up of the gmail-triggers executor removes only that one.
test_executor_script_removes_the_gmail_triggers_executor_and_keeps_the_new_one() {
  installed_up
  local r=$T/root
  run_exec_script "$r"
  mkdir -p "$r/letta/garden/gmail-triggers/bin"
  touch "$r/letta/garden/gmail-triggers/bin/run.mjs" "$r/letta/system-cron/lettuce-garden-gmail-triggers" \
    "$r/etc/cron.d/lettuce-garden-gmail-triggers" "$r/etc/lettuce-garden/gmail-triggers-token"
  legacy_executor
  ctl up alfred
  ok_rc
  eq "$(exec_args | paste -sd' ')" "sh uninstall $LEGACY_ARGS"
  run_exec_script "$r" /dev/null
  # Its directory stays, like the new one's, for its log.
  eq "$(files_in "$r")" "${INSTALLED/.\/letta\/system-cron /./letta/garden/gmail-triggers ./letta/system-cron }"
}

test_executor_script_uninstall_keeps_a_token_dir_in_use() {
  installed_up
  local r=$T/root
  run_exec_script "$r"
  touch "$r/etc/lettuce-garden/other"
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google/' "$H/stacks/alfred/stack.env"
  mkdir -p "$H/stacks/alfred/state/letta-home/garden/executor/bin"
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
  mkdir -p "$src/state/letta-home/"{mod-cache,skills/serving-web-apps,skills/fetching-web-pages,skills/from-settings} \
    "$src/state/letta-home/claude/projects" "$src/state/workspaces/agent-1" "$src/state/tools/bin" "$src/vault" "$dst"
  touch "$src/stack.env" "$src/state/letta-home/mod-cache/x" "$src/state/letta-home/claude/projects/t.jsonl" \
    "$src/state/letta-home/skills/serving-web-apps/SKILL.md" "$src/state/letta-home/skills/fetching-web-pages/SKILL.md" \
    "$src/state/letta-home/skills/from-settings/SKILL.md" "$src/state/workspaces/agent-1/notes.md" \
    "$src/state/tools/bin/uv" "$src/vault/vault.db"
  printf 'fetching-web-pages\n' >"$src/state/letta-home/skills/.lettucectl-skills"
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

# ── document tools ───────────────────────────────────────────────────────────
# docs_tree -> the test copy has a doc-tools/ to hash (setup copies only the CLI)
docs_tree() {
  mkdir -p "$H/doc-tools/tests"
  printf 'FROM scratch\n' >"$H/doc-tools/Dockerfile"
}

# docs_tag -> the tag lettucectl computes for the test copy's doc-tools/
docs_tag() { (LETTUCE_HOME=$H && . "$H/lib/common.sh" && doc_tools_tag); }

# docs_config [NAME] -> the rendered config also holds what compose/doc-tools.yml
# renders for NAME (default alfred), and the shared project renders a Gotenberg
# with the flags of the real compose/shared/gotenberg.yml
docs_config() {
  local name=${1:-alfred}
  docs_tree
  config_edit '.services["app-server"].volumes = [{"type":"volume","source":"doc-tools",
      "target":"/opt/lettuce-doc-tools","read_only":true,"volume":{"nocopy":true}}]
    | .volumes = {"doc-tools":{"name":"lettuce-doc-tools-'"$(docs_tag)"'","external":true}}
    | .services["app-server"].networks = {"default":null,"doc-tools":null}
    | .networks = {"default":{"name":"lettuce-'"$name"'_default"},"doc-tools":{"name":"lettuce-doc-tools-'"$name"'","external":true}}'
  sed -n 's/^      - \(--.*\)$/\1/p' "$ROOT/compose/shared/gotenberg.yml" |
    jq -R . | jq -sc '{services: {gotenberg: {read_only: true, cap_drop: ["ALL"], user: "1001:1001",
      security_opt: ["no-new-privileges:true"], command: (["gotenberg"] + .), networks: {internal: null}}}, networks: {internal: {internal: true}}}' >"$T/shared-config.json"
}

# with_docs -> alfred runs the doc-tools profile (docs_config)
with_docs() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,doc-tools/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,doc-tools/' "$H/stacks/alfred/stack.env"
  docs_config
}

# docs_refused JQ MESSAGE -> `check` refuses alfred's rendered config after JQ
docs_refused() {
  config_edit "$1"
  ctl check alfred
  bad_rc
  out_has "$2"
}

# tools_volume NAME COMPLETE [IN_USE] -> a tools volume lettucectl made exists
tools_volume() { echo "$1 ${1#lettuce-doc-tools-} $2 ${3:-false}" >>"$T/volumes"; }

# The tools volume's fill and marker check, as calls.log shows them.
CHECK_RUN="run --rm --init --network none --read-only --tmpfs /tmp --security-opt no-new-privileges --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER"
FILL_RUN="$CHECK_RUN -i"

# line_of TEXT -> the number of the first calls.log line containing TEXT
line_of() { grep -nF -- "$1" "$T/calls.log" | head -1 | cut -d: -f1; }

test_doc_tools_tag_follows_what_goes_into_the_image() {
  docs_tree
  local a
  a=$(docs_tag)
  [[ $a =~ ^[0-9a-f]{10}$ ]] || fail "bad tag $a"
  printf 'x\n' >"$H/doc-tools/tests/test_x.py"
  eq "$(docs_tag)" "$a"
  printf 'RUN true\n' >>"$H/doc-tools/Dockerfile"
  [[ $(docs_tag) != "$a" ]] || fail "the tag did not change with the Dockerfile"
}

test_compose_adds_the_doc_tools_overlay_and_its_values_only_with_the_profile() {
  added
  with_docs
  ctl up alfred
  ok_rc
  log_has "-f $H/compose/pipelock.yml -f $H/compose/code-server.yml -f $H/compose/garden.yml -f $H/compose/doc-tools.yml --env-file $H/stacks/alfred/stack.env"
  log_has "docker-env DOC_TOOLS_NETWORK=lettuce-doc-tools-alfred DOC_TOOLS_VOLUME=lettuce-doc-tools-$(docs_tag)"
  grep -q '^DOC_TOOLS' "$H/stacks/alfred/stack.env" && fail "doc-tools values written to stack.env"
  true
}

test_up_with_doc_tools_builds_creates_and_connects_before_reporting() {
  added
  with_docs
  stub STUB_IMAGES_MISSING=lettuce-doc-tools
  ctl up alfred
  ok_rc
  log_has "docker build -q -t lettuce-doc-tools:$(docs_tag) $H/doc-tools"
  log_has "docker network create --internal --label lettuce.doc-tools.stack=alfred lettuce-doc-tools-alfred"
  log_has "docker network connect --alias gotenberg lettuce-doc-tools-alfred gotenberg-id"
  # The network exists before the app-server joins it, and Gotenberg joins after.
  (($(line_of "network create") < $(line_of "-p lettuce-alfred --project-directory $H/upstream/docker -f $H/upstream/docker/compose.yml -f $H/compose/hardening.yml -f $H/compose/multi.yml -f $H/compose/secrets.yml -f $H/compose/gmail-triggers.yml -f $H/compose/pipelock.yml -f $H/compose/code-server.yml -f $H/compose/garden.yml -f $H/compose/doc-tools.yml --env-file $H/stacks/alfred/stack.env up -d"))) ||
    fail "the network was created after compose up"
  (($(line_of "-p lettuce_shared") > $(line_of "up -d --build --remove-orphans"))) || fail "gotenberg started before the stack"
}

test_up_with_doc_tools_fills_the_tools_volume_before_starting() {
  added
  with_docs
  stub STUB_IMAGES_MISSING=lettuce-doc-tools
  local tag vol
  tag=$(docs_tag)
  vol=lettuce-doc-tools-$tag
  ctl up alfred
  ok_rc
  log_has "docker volume create --label lettuce.doc-tools.tag=$tag $vol"
  log_has "docker create --label lettucectl.doc-tools=fill lettuce-doc-tools:$tag /none"
  log_has "docker cp doc-tools-cid:/opt/lettuce-doc-tools/. -"
  log_has "docker $FILL_RUN --label lettucectl.doc-tools=fill -v $vol:/dst lettuce-backup:local sh -c"
  log_has "docker rm -f doc-tools-cid"
  eq "$(cat "$T/fill-stdin")" "TOOLS-TAR"
  grep -q 'tar -x' "$T/run-script" || fail "the fill does not unpack"
  grep -q '/dst/.lettuce-doc-tools-complete' "$T/run-script" || fail "the fill writes no marker"
  # image -> volume -> fill -> compose up, and the volume is complete.
  (($(line_of "docker build -q -t lettuce-doc-tools") < $(line_of "volume create"))) || fail "volume made before the image"
  (($(line_of "docker rm -f doc-tools-cid") < $(line_of "up -d --build --remove-orphans"))) || fail "the stack started before the fill"
  grep -qx "$vol $tag true false" "$T/volumes" || fail "the volume is not complete: $(cat "$T/volumes")"
}

test_up_keeps_a_complete_tools_volume() {
  added
  with_docs
  tools_volume "lettuce-doc-tools-$(docs_tag)" true
  ctl up alfred
  ok_rc
  log_has "docker $CHECK_RUN -v lettuce-doc-tools-$(docs_tag):/dst:ro lettuce-backup:local test -f /dst/.lettuce-doc-tools-complete"
  log_lacks "volume create"
  log_lacks "docker create"
  log_lacks "volume rm"
}

test_up_refills_a_tools_volume_without_its_marker() {
  added
  with_docs
  local vol
  vol=lettuce-doc-tools-$(docs_tag)
  tools_volume "$vol" false
  ctl up alfred
  ok_rc
  log_has "volume rm $vol"
  log_has "volume create --label lettuce.doc-tools.tag=$(docs_tag) $vol"
  grep -qx "$vol $(docs_tag) true false" "$T/volumes" || fail "not refilled: $(cat "$T/volumes")"
}

test_up_stops_when_the_tools_volume_cannot_be_filled() {
  added
  with_docs
  stub STUB_RUN_FAIL=tar
  ctl up alfred
  bad_rc
  out_has "could not fill the volume lettuce-doc-tools-$(docs_tag)"
  log_has "docker rm -f doc-tools-cid"
  log_lacks "up -d --build"
  # An incomplete volume in use is not removed under a running app-server.
  sed -i '/^STUB_RUN_FAIL=/d' "$T/stub.env"
  : >"$T/volumes"
  tools_volume "lettuce-doc-tools-$(docs_tag)" false true
  ctl up alfred
  bad_rc
  out_has "lettuce-doc-tools-$(docs_tag) is incomplete and in use; stop every stack with doc-tools (lettucectl down all), then run: lettucectl up all"
  log_lacks "up -d --build"
}

test_up_refuses_a_tools_volume_lettucectl_did_not_make() {
  added
  with_docs
  local vol
  vol=lettuce-doc-tools-$(docs_tag)
  # Made by hand: no label, though it has the marker.
  echo "$vol - true false" >>"$T/volumes"
  ctl up alfred
  bad_rc
  out_has "$vol was not made by lettucectl"
  log_lacks "up -d --build"
  log_lacks "volume rm $vol"
  # Labelled, but a bind to a host directory (driver options).
  : >"$T/volumes"
  echo "$vol $(docs_tag) true false local 3" >>"$T/volumes"
  ctl up alfred
  bad_rc
  out_has "$vol was not made by lettucectl"
  : >"$T/volumes"
  echo "$vol $(docs_tag) true false nfs 0" >>"$T/volumes"
  ctl up alfred
  bad_rc
  out_has "$vol was not made by lettucectl"
}

test_up_builds_no_image_for_a_complete_tools_volume() {
  added
  with_docs
  tools_volume "lettuce-doc-tools-$(docs_tag)" true
  stub STUB_IMAGES_MISSING=lettuce-doc-tools
  ctl up alfred
  ok_rc
  log_lacks "docker build -q -t lettuce-doc-tools"
}

test_up_removes_containers_an_interrupted_fill_left() {
  added
  with_docs
  printf 'old-cid\n' >"$T/fill-containers"
  ctl up alfred
  ok_rc
  log_has "docker rm -f old-cid"
  (($(line_of "docker rm -f old-cid") < $(line_of "docker create --label lettucectl.doc-tools=fill"))) ||
    fail "the leftover was not removed before the fill"
}

test_up_fills_the_tools_volume_under_a_lock() {
  added
  with_docs
  mkdir -p "$H/stacks"
  # Another lettucectl holds the lock for a moment: this one waits, then fills.
  flock "$H/stacks/.doc-tools.lock" sleep 2 &
  sleep 0.3
  local t0=$SECONDS
  ctl up alfred
  ok_rc
  wait
  ((SECONDS - t0 >= 1)) || fail "up did not wait for the lock"
  out_has "waiting for another lettucectl to finish with the volume"
  log_has "volume create --label lettuce.doc-tools.tag=$(docs_tag)"
}

test_up_removes_old_tools_volumes_and_leaves_one_in_use() {
  added
  with_docs
  tools_volume lettuce-doc-tools-0000000001 true
  tools_volume lettuce-doc-tools-0000000002 true true
  ctl up alfred
  ok_rc
  log_has "volume rm lettuce-doc-tools-0000000001"
  log_has "volume rm lettuce-doc-tools-0000000002"
  out_lacks "0000000002"
  grep -q '^lettuce-doc-tools-0000000001 ' "$T/volumes" && fail "the old volume stayed"
  grep -q '^lettuce-doc-tools-0000000002 ' "$T/volumes" || fail "the volume in use was removed"
  grep -q "^lettuce-doc-tools-$(docs_tag) " "$T/volumes" || fail "the current volume was removed"
}

test_up_again_connects_nothing_twice() {
  added
  with_docs
  ctl up alfred
  ok_rc
  : >"$T/calls.log"
  ctl up alfred
  ok_rc
  log_lacks "network create"
  log_lacks "network connect"
  log_lacks "docker build"
  log_lacks "volume create"
}

test_up_without_doc_tools_makes_no_network_or_gotenberg() {
  added
  ctl up alfred
  ok_rc
  log_lacks "network create"
  log_lacks "-p lettuce_shared --project-directory $H/compose/shared -f $H/compose/shared/gotenberg.yml --env-file /dev/null up"
  log_lacks "doc-tools.yml"
}

test_check_accepts_the_doc_tools_overlay() {
  added
  with_docs
  ctl check alfred
  ok_rc
}

test_check_refuses_a_doc_tools_mount_that_is_not_the_tools_volume_read_only() {
  added
  with_docs
  local ok
  ok=$(jq -c '.services["app-server"].volumes[0]' "$T/compose-config.json")
  docs_refused '.services["app-server"].volumes[0].read_only = false' "read-only at /opt/lettuce-doc-tools"
  docs_refused '.services["app-server"].volumes[0] = {"type":"image","source":"lettuce-doc-tools:'"$(docs_tag)"'","target":"/opt/lettuce-doc-tools","read_only":true}' "read-only at /opt/lettuce-doc-tools"
  docs_refused '.services["app-server"].volumes[0] = {"type":"bind","source":"/tmp/x","target":"/opt/lettuce-doc-tools","read_only":true}' "read-only at /opt/lettuce-doc-tools"
  docs_refused '.services["app-server"].volumes = ['"$ok"', {"type":"bind","source":"/tmp/x","target":"/opt/lettuce-doc-tools"}]' "read-only at /opt/lettuce-doc-tools"
  docs_refused '.services["app-server"].volumes = ['"$ok"'] | .volumes["doc-tools"].external = false' "must be the external lettuce-doc-tools-$(docs_tag)"
  docs_refused '.volumes["doc-tools"] = {"name":"lettuce-doc-tools-0000000001","external":true}' "must be the external lettuce-doc-tools-$(docs_tag)"
}

test_check_refuses_an_app_server_without_its_default_network() {
  added
  with_docs
  docs_refused '.services["app-server"].networks = {"doc-tools":null}' "without default it loses the rest of the stack"
}

test_check_refuses_another_service_on_the_doc_tools_network() {
  added
  with_docs
  docs_refused '.services.bff.networks = {"doc-tools":null}' "bff join(s) the doc-tools network"
}

test_check_refuses_a_doc_tools_network_that_is_not_lettucectls() {
  added
  with_docs
  docs_refused '.networks["doc-tools"].external = false' "must be the external lettuce-doc-tools-alfred"
  config_edit '.networks["doc-tools"].external = true'
  echo "lettuce-doc-tools-alfred - false -" >>"$T/networks"
  ctl check alfred
  bad_rc
  out_has "must be an internal network lettucectl made for alfred"
}

test_check_refuses_another_service_mounting_the_tools_whatever_the_profiles() {
  added
  config_edit '.services.bff.volumes = [{"type":"image","source":"lettuce-doc-tools:x","target":"/x","read_only":true}]'
  ctl check alfred
  bad_rc
  out_has "bff mount(s) an image or the document tools"
  config_edit '.services.bff.volumes = [{"type":"volume","source":"tools","target":"/x","read_only":true}]
    | .volumes = {"tools":{"name":"lettuce-doc-tools-0000000001","external":true}}'
  ctl check alfred
  bad_rc
  out_has "bff mount(s) an image or the document tools"
  config_edit '.services.bff.volumes = [{"type":"volume","source":"lettuce-doc-tools-0000000001","target":"/x"}] | .volumes = {}'
  ctl check alfred
  bad_rc
  out_has "bff mount(s) an image or the document tools"
}

test_up_refuses_a_gotenberg_without_its_deny_flags() {
  added
  with_docs
  jq -c '.services.gotenberg.command -= ["--chromium-deny-private-ips"]' "$T/shared-config.json" >"$T/s.json"
  mv "$T/s.json" "$T/shared-config.json"
  ctl up alfred
  bad_rc
  out_has "must run with --chromium-deny-private-ips"
  log_lacks "-f $H/compose/shared/gotenberg.yml --env-file /dev/null up"
}

test_up_refuses_a_gotenberg_that_turns_a_deny_flag_back_off() {
  added
  with_docs
  jq -c '.services.gotenberg.command += ["--libreoffice-deny-public-ips=false"]' "$T/shared-config.json" >"$T/s.json"
  mv "$T/s.json" "$T/shared-config.json"
  ctl up alfred
  bad_rc
  out_has "must run with --libreoffice-deny-public-ips"
  log_lacks "-f $H/compose/shared/gotenberg.yml --env-file /dev/null up"
}

test_up_refuses_a_gotenberg_running_as_root_or_able_to_gain_privileges() {
  local f
  added
  with_docs
  cp "$T/shared-config.json" "$T/base.json"
  for f in 'del(.services.gotenberg.user)' '.services.gotenberg.user = "root"' '.services.gotenberg.user = "0:1001"' \
    'del(.services.gotenberg.security_opt)'; do
    jq -c "$f" "$T/base.json" >"$T/shared-config.json"
    : >"$T/calls.log"
    ctl up alfred
    bad_rc
    out_has "gotenberg must run as a non-root user with no-new-privileges"
    log_lacks "-f $H/compose/shared/gotenberg.yml --env-file /dev/null up"
  done
}

test_up_refuses_a_gotenberg_that_publishes_a_port_or_is_not_hardened() {
  added
  with_docs
  jq -c '.services.gotenberg.ports = [{"target":3000,"published":"3000"}]' "$T/shared-config.json" >"$T/s.json"
  mv "$T/s.json" "$T/shared-config.json"
  ctl up alfred
  bad_rc
  out_has "gotenberg must not publish ports"
  jq -c 'del(.services.gotenberg.ports) | .services.gotenberg.read_only = false' "$T/shared-config.json" >"$T/s.json"
  mv "$T/s.json" "$T/shared-config.json"
  ctl up alfred
  bad_rc
  out_has "gotenberg must have read_only: true"
}

test_up_fails_at_the_end_when_gotenberg_cannot_start() {
  added
  with_docs
  stub STUB_SHARED_UP_RC=1
  ctl up alfred
  bad_rc
  log_has "up -d --build --remove-orphans"
  out_has "the document tools (shared Gotenberg, networks) are out of step"
}

test_sync_removes_the_networks_of_stacks_that_are_gone() {
  added
  with_docs
  echo "lettuce-doc-tools-ghost ghost true gotenberg-id" >>"$T/networks"
  ctl up alfred
  ok_rc
  log_has "network disconnect -f lettuce-doc-tools-ghost gotenberg-id"
  log_has "network rm lettuce-doc-tools-ghost"
  grep -q '^lettuce-doc-tools-ghost ' "$T/networks" && fail "the orphan network is still there"
  grep -q '^lettuce-doc-tools-alfred ' "$T/networks" || fail "alfred's network went too"
}

test_sync_connects_gotenberg_only_to_networks_lettucectl_made() {
  added
  ctl add hal hal@example.com
  ok_rc
  with_docs
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,doc-tools/' "$H/stacks/hal/stack.env"
  # hal's name is taken by a network with a route out, made by someone else.
  echo "lettuce-doc-tools-hal - false -" >>"$T/networks"
  : >"$T/calls.log"
  ctl up alfred
  bad_rc
  out_has "lettuce-doc-tools-hal must be an internal network lettucectl made for hal"
  log_has "network connect --alias gotenberg lettuce-doc-tools-alfred gotenberg-id"
  log_lacks "network connect --alias gotenberg lettuce-doc-tools-hal"
  # Nor one labelled for another stack, and Gotenberg leaves one it is already on.
  sed -i 's/^lettuce-doc-tools-hal .*/lettuce-doc-tools-hal alfred true gotenberg-id/' "$T/networks"
  : >"$T/calls.log"
  ctl up alfred
  bad_rc
  log_has "network disconnect -f lettuce-doc-tools-hal gotenberg-id"
  log_lacks "network connect --alias gotenberg lettuce-doc-tools-hal"
}

test_sync_fails_when_it_cannot_list_the_networks() {
  added
  with_docs
  echo "lettuce-doc-tools-ghost ghost true -" >>"$T/networks"
  stub STUB_NETWORK_LS_RC=1
  ctl up alfred
  bad_rc
  out_has "the document tools (shared Gotenberg, networks) are out of step"
}

test_remove_drops_the_stacks_network_and_keeps_gotenberg() {
  added
  with_docs
  ctl up alfred
  ok_rc
  : >"$T/calls.log"
  ctl remove alfred <<<"alfred"
  ok_rc
  (($(line_of "down -v") < $(line_of "network disconnect -f lettuce-doc-tools-alfred gotenberg-id"))) || fail "network dropped before down"
  log_has "network rm lettuce-doc-tools-alfred"
  log_lacks "-f $H/compose/shared/gotenberg.yml --env-file /dev/null down"
  grep -qx 'lettuce_shared gotenberg' "$T/containers.txt" || fail "gotenberg removed with the stack"
}

test_remove_purge_renders_the_stack_without_doc_tools() {
  added
  with_docs
  ctl up alfred
  ok_rc
  : >"$T/calls.log"
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  grep 'run --rm --no-deps' "$T/calls.log" | grep -q doc-tools.yml && fail "the purge run mounts the doc-tools image"
  eq "$(grep '^docker compose' "$T/calls.log" | tail -1 | grep -o ' down$')" " down"
  [[ ! -e $H/stacks/alfred ]] || fail "stack directory still exists"
  true
}

test_restart_keeps_gotenberg_attached() {
  added
  with_docs
  ctl up alfred
  ok_rc
  : >"$T/calls.log"
  ctl restart alfred
  ok_rc
  log_lacks "network rm"
  log_lacks "network disconnect"
}

test_profiles_dropping_doc_tools_stops_gotenberg_and_removes_the_networks() {
  added
  with_docs
  ctl up alfred
  ok_rc
  : >"$T/calls.log"
  ctl profiles cloudflared
  ok_rc
  log_has "-p lettuce_shared --project-directory $H/compose/shared -f $H/compose/shared/gotenberg.yml --env-file /dev/null down"
  log_has "network rm lettuce-doc-tools-alfred"
  [[ ! -s $T/networks ]] || fail "networks left: $(cat "$T/networks")"
  log_has "volume rm lettuce-doc-tools-$(docs_tag)"
  [[ ! -s $T/volumes ]] || fail "volumes left: $(cat "$T/volumes")"
  # A re-run has nothing left to do.
  : >"$T/calls.log"
  ctl profiles cloudflared
  ok_rc
  log_lacks "down"
  log_lacks "network rm"
  log_lacks "volume rm"
}

test_profiles_adding_doc_tools_prints_the_mcp_entries_and_registers_nothing() {
  added
  docs_config
  ctl profiles cloudflared,doc-tools
  ok_rc
  log_has "network connect --alias gotenberg lettuce-doc-tools-alfred gotenberg-id"
  out_has "office -> /opt/lettuce-doc-tools/bin/officeagent"
  out_has "pdf -> /opt/lettuce-doc-tools/bin/pdf-mcp"
  out_has "or run: lettucectl mcp alfred"
  log_lacks "node --input-type=module"
}

test_add_with_doc_tools_registers_the_mcp_servers() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,doc-tools/' "$H/config.env"
  docs_config
  mcp_answer '{"added":["office","pdf"],"present":[],"conflicts":[],"removed":[],"attempts":1}'
  ctl add alfred alfred@example.com
  ok_rc
  log_has "network connect --alias gotenberg lettuce-doc-tools-alfred gotenberg-id"
  log_has "$MCP_ADD"
  # After the stack and Gotenberg are up.
  (($(line_of "$MCP_ADD") > $(line_of "network connect --alias gotenberg"))) || fail "registered before Gotenberg"
  (($(line_of "restart --no-deps bff") > $(line_of "$MCP_ADD"))) || fail "the BFF was not restarted after registering"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" 1
  out_has "office and pdf are in https://alfred.example.org Settings -> MCP"
}

test_add_still_succeeds_when_registering_the_mcp_servers_fails() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,doc-tools/' "$H/config.env"
  docs_config
  stub STUB_EXEC_RC=3
  ctl add alfred alfred@example.com
  ok_rc
  out_has "run: lettucectl mcp alfred"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" ""
}

# The `node -` call that adds or removes the document tools in Settings -> MCP.
MCP_ADD="exec -T app-server node --input-type=module - add /root/.letta/mcp-home/.letta/settings.json"
MCP_REMOVE="exec -T app-server node --input-type=module - remove /root/.letta/mcp-home/.letta/settings.json"

# mcp_answer JSON -> what lib/mcp-servers.mjs prints in the stack (the exec's output)
mcp_answer() { printf '%s\n' "$1" >"$T/exec-stdout"; }

test_mcp_adds_the_document_tools_to_settings() {
  added
  with_docs
  mcp_answer '{"added":["office","pdf"],"present":[],"conflicts":[],"removed":[],"attempts":1}'
  ctl mcp alfred
  ok_rc
  log_has "$MCP_ADD"
  # The script goes in on stdin: nothing is copied into the stack.
  grep -q 'agent-local-mcp-global' "$T/exec-stdin.log" || fail "the script was not streamed"
  out_has "alfred: added office, pdf to Settings -> MCP"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" 1
  # The BFF re-renders the mcp-servers skill, the agents' list of servers, when it reconnects.
  log_has "--env-file $H/stacks/alfred/stack.env restart --no-deps bff"
  (($(line_of "restart --no-deps bff") > $(line_of "$MCP_ADD"))) || fail "the BFF restarted before the change"
  out_has "alfred: restarting its BFF so agents see the change"
}

test_mcp_restarts_no_bff_when_nothing_changed() {
  added
  with_docs
  mcp_answer '{"added":[],"present":["office","pdf"],"conflicts":[],"removed":[],"attempts":1}'
  ctl mcp alfred
  ok_rc
  log_has "$MCP_ADD"
  log_lacks "restart --no-deps bff"
}

test_mcp_still_succeeds_when_the_bff_cannot_restart() {
  added
  with_docs
  stub STUB_RESTART_RC=1
  mcp_answer '{"added":["office","pdf"],"present":[],"conflicts":[],"removed":[],"attempts":1}'
  ctl mcp alfred
  ok_rc
  out_has "alfred: could not restart its BFF; agents see the change after: lettucectl restart alfred"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" 1
}

test_mcp_runs_once_per_stack_unless_forced() {
  added
  with_docs
  echo "DOC_TOOLS_MCP_REGISTERED=1" >>"$H/stacks/alfred/stack.env"
  ctl mcp alfred
  ok_rc
  log_lacks "node --input-type=module"
  log_lacks "restart --no-deps bff"
  out_has "alfred: registered before; a server the person removed stays removed (--force adds it again)"
  mcp_answer '{"added":["pdf"],"present":["office"],"conflicts":[],"removed":[],"attempts":1}'
  ctl mcp alfred --force
  ok_rc
  log_has "$MCP_ADD"
  out_has "alfred: added pdf to Settings -> MCP (already there: office)"
}

test_mcp_leaves_a_persons_server_of_the_same_name() {
  added
  with_docs
  mcp_answer '{"added":["office"],"present":[],"conflicts":["pdf"],"removed":[],"attempts":1}'
  ctl mcp alfred
  ok_rc
  out_has "alfred: Settings -> MCP already has a pdf server with another command; it was left as it is"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" 1
}

test_mcp_needs_the_doc_tools_profile() {
  added
  ctl mcp alfred
  bad_rc
  out_has "the doc-tools profile is off"
  log_lacks "node --input-type=module"
}

test_mcp_failures_fail_at_the_end_without_a_mark() {
  added
  with_docs
  stub STUB_EXEC_RC=3
  ctl mcp alfred
  bad_rc
  out_has "could not register the document tools in: alfred"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" ""
}

test_profiles_dropping_doc_tools_removes_the_registered_servers() {
  added
  with_docs
  ctl up alfred
  ok_rc
  echo "DOC_TOOLS_MCP_REGISTERED=1" >>"$H/stacks/alfred/stack.env"
  : >"$T/calls.log"
  mcp_answer '{"added":[],"present":[],"conflicts":[],"removed":["office","pdf"],"attempts":1}'
  ctl profiles cloudflared
  ok_rc
  log_has "$MCP_REMOVE"
  out_has "alfred: removed office, pdf from Settings -> MCP (the doc-tools profile is off)"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" ""
  (($(line_of "restart --no-deps bff") > $(line_of "$MCP_REMOVE"))) || fail "the BFF was not restarted after the removal"
}

test_up_restarts_no_bff_when_a_removal_finds_nothing() {
  added
  echo "DOC_TOOLS_MCP_REGISTERED=1" >>"$H/stacks/alfred/stack.env"
  mcp_answer '{"added":[],"present":[],"conflicts":[],"removed":[],"attempts":1}'
  ctl up alfred
  ok_rc
  log_has "$MCP_REMOVE"
  log_lacks "restart --no-deps bff"
}

test_up_retries_a_removal_that_failed_and_skips_unregistered_stacks() {
  added
  echo "DOC_TOOLS_MCP_REGISTERED=1" >>"$H/stacks/alfred/stack.env"
  stub STUB_EXEC_RC=1
  ctl up alfred
  bad_rc
  log_has "$MCP_REMOVE"
  out_has "could not remove the document tools from Settings -> MCP in: alfred"
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" 1
  # Once removed, the stack is left alone.
  sed -i '/^STUB_EXEC_RC=/d' "$T/stub.env"
  ctl up alfred
  ok_rc
  eq "$(senv alfred DOC_TOOLS_MCP_REGISTERED)" ""
  : >"$T/calls.log"
  ctl up alfred
  ok_rc
  log_lacks "node --input-type=module"
}

test_shared_project_is_not_a_stack_named_shared() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,doc-tools/' "$H/config.env"
  docs_config shared
  ctl add shared shared@example.com
  ok_rc
  log_has "-p lettuce-shared --project-directory $H/upstream/docker"
  # The stack's files never go to the shared project, nor the other way round.
  grep -- '-p lettuce_shared ' "$T/calls.log" | grep -q upstream && fail "the shared project got a stack's files"
  grep -- '-p lettuce-shared ' "$T/calls.log" | grep -q compose/shared && fail "the stack got the shared project's files"
  true
}

# ── code editor (code-server) ────────────────────────────────────────────────
# code_config -> the rendered config also holds the three services and networks
# compose/code-server.yml renders for alfred (taken from a real render), and
# the garden service
code_config() {
  local st=$H/stacks/alfred/state
  config_edit '.services["code-server"] = {"read_only":true,"cap_drop":["ALL"],"tmpfs":["/tmp"],
      "environment":'"$GARDEN_ENV"',"networks":{"default":null},
      "volumes":[{"type":"volume","source":"code-server-sock","target":"/run/code-server","read_only":true,"volume":{}}]}
    | .services["code-server-editor"] = {"read_only":true,"cap_drop":["ALL"],"user":"0","tmpfs":["/tmp"],
      "command":["--socket","/run/code-server/editor.sock","--socket-mode","666","--auth","none","--disable-proxy",
        "--disable-telemetry","--disable-update-check","--disable-getting-started-override","--extensions-dir",
        "/opt/lettuce-editor/no-extensions","--user-data-dir","/data/user","--config","/opt/lettuce-editor/config.yaml"],
      "environment":{"EXTENSIONS_GALLERY":"{}","TZ":"UTC"},
      "networks":{"code-exec":null},
      "volumes":[{"type":"bind","source":"'"$st"'/workspaces","target":"/work","bind":{"create_host_path":true}},
        {"type":"volume","source":"code-server-data","target":"/data","volume":{}},
        {"type":"volume","source":"code-exec-keys","target":"/keys","volume":{}},
        {"type":"volume","source":"code-server-sock","target":"/run/code-server","volume":{}}]}
    | .services["code-server-exec"] = {"cap_drop":["NET_RAW"],"init":true,
      "depends_on":{"code-server-editor":{"condition":"service_healthy","required":true}},
      "networks":{"code-egress":null,"code-exec":null},
      "volumes":[{"type":"bind","source":"'"$st"'/workspaces","target":"/work","bind":{"create_host_path":true}},
        {"type":"bind","source":"'"$st"'/tools","target":"/opt/agent-tools","bind":{"create_host_path":true}},
        {"type":"volume","source":"code-exec-keys","target":"/keys","read_only":true,"volume":{}}]}
    | .networks = {"code-exec":{"internal":true},"code-egress":{},"default":{}}'
  garden_config
}

# with_code -> alfred runs the code-server profile, with the services it renders
with_code() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,code-server/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,code-server/' "$H/stacks/alfred/stack.env"
  code_config
}

# code_refused JQ EXPECTED -> with alfred's code editor edited by the jq filter
# JQ, `check` fails and says EXPECTED
code_refused() {
  added
  with_code
  config_edit "$1"
  ctl check alfred
  bad_rc
  out_has "alfred: $2"
}
CG='.services["code-server"]'
CE='.services["code-server-editor"]'
CX='.services["code-server-exec"]'

test_check_accepts_the_code_editor_as_rendered() {
  added
  with_code
  ctl check alfred
  ok_rc
}

test_check_refuses_a_code_editor_service_missing() {
  local s
  for s in code-server code-server-editor code-server-exec; do
    code_refused "del(.services[\"$s\"])" "the code-server profile is on but $s is not in the rendered config"
    teardown
    setup
  done
}

test_check_refuses_a_gate_without_its_hardening_or_on_another_network() {
  code_refused "del($CG.read_only)" "code-server must have read_only: true"
  teardown; setup
  code_refused "$CG.volumes = [{\"type\":\"bind\",\"source\":\"/srv\",\"target\":\"/srv\"}]" "code-server must not mount host paths"
  teardown; setup
  code_refused "$CG.networks[\"code-exec\"] = null" "code-server (the gate) must be on the default network only"
}

test_check_refuses_a_gate_without_the_editors_socket_or_with_more() {
  local m="code-server (the gate) may mount only the volume code-server-sock, read-only (at /run/code-server)"
  code_refused "$CG.volumes = []" "$m"
  teardown; setup
  code_refused "$CG.volumes[0].read_only = false" "$m"
  teardown; setup
  code_refused "$CG.volumes += [{\"type\":\"volume\",\"source\":\"scratch\",\"target\":\"/scratch\"}]" "$m"
}

test_check_refuses_an_editor_without_its_hardening() {
  code_refused "$CE.cap_drop = [\"NET_RAW\"]" "code-server-editor must drop all capabilities"
  teardown; setup
  code_refused "$CE.cap_add = [\"SYS_PTRACE\"]" "code-server-editor must not set cap_add"
  teardown; setup
  code_refused "$CE.pid = \"host\"" "code-server-editor must not set pid"
}

test_check_refuses_an_editor_with_other_mounts() {
  code_refused "$CE.volumes += [{\"type\":\"bind\",\"source\":\"$H/stacks/alfred/state/tools\",\"target\":\"/opt/agent-tools\"}]" \
    "code-server-editor may mount from the host only $H/stacks/alfred/state/workspaces (at /work)"
  teardown; setup
  local m="code-server-editor may mount only the volumes code-server-data (at /data), code-exec-keys (at /keys) and code-server-sock (at /run/code-server)"
  code_refused "$CE.volumes += [{\"type\":\"volume\",\"source\":\"scratch\",\"target\":\"/scratch\"}]" "$m"
  teardown; setup
  code_refused "$CE.volumes = [$CE.volumes[0], $CE.volumes[1], $CE.volumes[2]]" "$m"
}

test_check_refuses_an_editor_on_the_stack_network() {
  code_refused "$CE.networks.default = null" "code-server-editor must be on the code-exec network only"
}

test_check_refuses_an_editor_that_runs_workspace_code_or_trusts_more() {
  local flag m="code-server-editor must run with --socket /run/code-server/editor.sock, --auth none, --disable-proxy, --extensions-dir /opt/lettuce-editor/no-extensions, --user-data-dir /data/user and --config /opt/lettuce-editor/config.yaml, each once"
  code_refused "$CE.command -= [\"--disable-proxy\"]" "$m"
  teardown; setup
  code_refused "$CE.command |= map(if . == \"none\" then \"password\" else . end)" "$m"
  teardown; setup
  code_refused "$CE.command |= map(if . == \"/opt/lettuce-editor/no-extensions\" then \"/data/extensions\" else . end)" "$m"
  teardown; setup
  code_refused "$CE.command |= map(if . == \"/run/code-server/editor.sock\" then \"/tmp/editor.sock\" else . end)" "$m"
  # Without --config, code-server reads and writes its config file on /data.
  teardown; setup
  code_refused "$CE.command -= [\"--config\", \"/opt/lettuce-editor/config.yaml\"]" "$m"
  # code-server takes the last of a repeated flag, in either form.
  for flag in '"--auth","password"' '"--auth=password"' '"--extensions-dir","/work/x"' '"--extensions-dir=/work/x"' \
    '"--user-data-dir","/work/x"' '"--user-data-dir=/work/x"' '"--socket=/tmp/x"' '"--disable-proxy=false"' \
    '"--config","/data/config.yaml"' '"--config=/data/config.yaml"'; do
    teardown; setup
    code_refused "$CE.command += [$flag]" "$m"
  done
  for flag in --bind-addr --socket-fd; do
    teardown; setup
    code_refused "$CE.command += [\"$flag\", \"3\"]" "code-server-editor must not run with $flag"
  done
  for flag in --disable-workspace-trust --trusted-origins=evil.example.org --link --enable-proposed-api \
    --install-extension --github-auth --builtin-extensions-dir; do
    teardown; setup
    code_refused "$CE.command += [\"$flag\"]" "code-server-editor must not run with $flag"
  done
  teardown; setup
  code_refused "$CE.environment.EXTENSIONS_GALLERY = \"{\\\"serviceUrl\\\":\\\"https://open-vsx.org/vscode/gallery\\\"}\"" \
    "code-server-editor must have EXTENSIONS_GALLERY={} (no marketplace)"
}

test_check_refuses_a_shell_with_more_reach() {
  code_refused "$CX.networks.default = null" "code-server-exec must be on the code-exec and code-egress networks only"
  teardown; setup
  code_refused "$CX.cap_drop = []" "code-server-exec must drop NET_RAW"
  teardown; setup
  code_refused "$CX.privileged = true" "code-server-exec must not set privileged"
  teardown; setup
  code_refused "$CX.network_mode = \"service:app-server\"" "code-server-exec must not set network_mode"
  teardown; setup
  code_refused "$CX.volumes += [{\"type\":\"bind\",\"source\":\"$H/stacks/alfred/state/letta-home\",\"target\":\"/root/.letta\"}]" \
    "code-server-exec may mount from the host only"
  teardown; setup
  code_refused "$CX.volumes[2].read_only = false" "code-server-exec may mount only the volume code-exec-keys, read-only"
}

test_check_refuses_others_on_the_code_editor_networks() {
  code_refused '.services["app-server"].networks = {"default":null,"code-exec":null}' \
    "only code-server-editor,code-server-exec may join the code-exec network (found: app-server,code-server-editor,code-server-exec)"
  teardown; setup
  code_refused '.services.cloudflared = {"networks":{"default":null,"code-egress":null}}' \
    "only code-server-exec may join the code-egress network"
  teardown; setup
  code_refused '.networks["code-exec"].internal = false' "the code-exec network must be internal"
}

test_check_refuses_a_symlinked_workspace_for_the_code_editor() {
  added
  with_code
  mkdir -p "$T/elsewhere"
  rm -rf "$H/stacks/alfred/state/workspaces"
  ln -s "$T/elsewhere" "$H/stacks/alfred/state/workspaces"
  ctl check alfred
  bad_rc
  out_has "alfred: $H/stacks/alfred/state/workspaces is a symlink"
}

test_check_guards_the_code_editor_volumes_whatever_the_profiles() {
  added
  config_edit '.services["app-server"].volumes = [{"type":"volume","source":"code-server-data","target":"/x"}]'
  ctl check alfred
  bad_rc
  out_has "alfred: app-server mount(s) the code editor's volume"
  teardown; setup
  added
  config_edit '.services.bff.volumes = [{"type":"volume","source":"code-exec-keys","target":"/keys"}]'
  ctl check alfred
  bad_rc
  out_has "alfred: bff mount(s) the code editor's SSH keys"
  teardown; setup
  added
  config_edit '.services.searxng = {"volumes_from":["code-server-exec"]}'
  ctl check alfred
  bad_rc
  out_has "volumes_from reaches"
  teardown; setup
  added
  config_edit '.services.bff.volumes = [{"type":"volume","source":"code-server-sock","target":"/run/code-server"}]'
  ctl check alfred
  bad_rc
  out_has "alfred: bff mount(s) the code editor's socket; only code-server and code-server-editor may"
}

test_check_refuses_sharing_a_code_editor_network_namespace_whatever_the_profiles() {
  local mode
  for mode in service:code-server-editor service:code-server service:code-server-exec container:lettuce-alfred-code-server-editor-1; do
    teardown; setup
    added
    config_edit ".services.searxng = {\"network_mode\":\"$mode\"}"
    ctl check alfred
    bad_rc
    out_has "alfred: searxng shares the network namespace of the code editor ($mode)"
  done
}

test_compose_always_merges_the_code_editor_overlay() {
  added
  ctl up alfred
  ok_rc
  log_has "-f $H/compose/pipelock.yml -f $H/compose/code-server.yml -f $H/compose/garden.yml --env-file"
}

test_profiles_routes_the_code_editor_and_lists_it_on_the_landing_page() {
  added
  code_config
  ctl profiles cloudflared,search,code-server
  ok_rc
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/code-server(/|$)","service":"http://code-server:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
  eq "$(senv alfred GARDEN_PROFILES)" code-server
}

test_profiles_dropping_the_code_editor_removes_its_containers() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,code-server/' "$H/config.env"
  code_config
  added
  containers alfred app-server bff cloudflared code-server code-server-editor code-server-exec
  ctl profiles cloudflared
  ok_rc
  log_lacks "code-server:8000"
  grep -q '^docker compose -p lettuce-alfred .* rm -sf code-server code-server-editor code-server-exec$' "$T/calls.log" ||
    fail "the code editor's containers were not removed"
}

# ── garden host ──────────────────────────────────────────────────────────────
# pre_garden -> alfred is a complete stack from before the garden host
pre_garden() {
  added
  sed -i '/^GARDEN_HOSTNAME=/d; /^GARDEN_ORIGIN=/d; /^CF_GARDEN_/d' "$H/stacks/alfred/stack.env"
}
MIGRATION="GET /zones/zone1/dns_records;POST /accounts/acc1/access/apps;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;"

test_up_makes_no_cloudflare_call_once_the_garden_host_exists() {
  added
  ctl up alfred
  ok_rc
  log_lacks "curl "
  log_has "up -d --build"
}

test_up_creates_the_garden_host_of_an_older_stack_before_its_containers() {
  pre_garden
  ctl up alfred
  ok_rc
  eq "$(calls | tr '\n' ';')" "$MIGRATION"
  eq "$(senv alfred GARDEN_HOSTNAME)" alfred-garden.example.org
  eq "$(senv alfred GARDEN_ORIGIN)" https://alfred-garden.example.org
  eq "$(senv alfred CF_GARDEN_APP_ID)" app2
  eq "$(senv alfred CF_GARDEN_AUD)" aud2
  eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" rec2
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
  eq "$(jq -c '[.name, .domain, .policies]' <<<"$(body_of POST /accounts/acc1/access/apps)")" \
    '["lettuce-alfred-garden","alfred-garden.example.org",[{"id":"pol1","precedence":1}]]'
  eq "$(jq -r .name <<<"$(body_of POST /zones/zone1/dns_records)")" alfred-garden.example.org
  # The tunnel routes the garden host before its name resolves.
  eq "$(ingress)" "[$GARDEN_404,$LETTUCE_TAIL]"
  (($(grep -n '^curl ' "$T/calls.log" | tail -1 | cut -d: -f1) < $(line_of "config --format json"))) ||
    fail "the stack was checked before its garden host existed"
  out_has "alfred: its garden host https://alfred-garden.example.org/ is ready for when a garden profile is on"
  # Done: a re-run makes no Cloudflare call.
  : >"$T/calls.log"
  ctl up alfred
  ok_rc
  log_lacks "curl "
}

test_up_resumes_a_garden_host_that_stopped_part_way() {
  local step rest
  # STEP|what the re-run calls
  for step in \
    "POST /accounts/acc1/access/apps|POST /accounts/acc1/access/apps;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;" \
    "PUT /accounts/acc1/cfd_tunnel/tun1/configurations|PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;" \
    "POST /zones/zone1/dns_records|PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;"; do
    teardown; setup
    # With a garden page, the stack can't start without its garden host.
    sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
    broker_config
    pre_garden
    stub "STUB_CF_FAIL=\"${step%%|*}\""
    ctl up alfred
    bad_rc
    out_has "alfred: could not create its garden host on Cloudflare, so nothing was started"
    out_has "run the same command again"
    log_lacks "docker compose"
    eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" ""
    eq "$(senv alfred GARDEN_HOSTNAME)" alfred-garden.example.org
    rest=${step#*|}
    : >"$T/stub.env"
    : >"$T/calls.log"
    ctl up alfred
    ok_rc
    eq "$(calls | tr '\n' ';')" "$rest"
    eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" rec2
  done
}

test_up_gives_an_older_stack_the_fallback_garden_name_when_taken() {
  pre_garden
  stub STUB_CF_TAKEN=alfred-garden.example.org
  ctl up alfred
  ok_rc
  eq "$(senv alfred GARDEN_ORIGIN)" https://lettuce-alfred-garden.example.org
}

test_up_leaves_an_unfinished_stack_to_add() {
  stub 'STUB_CF_FAIL="POST /accounts/acc1/cfd_tunnel"'
  ctl add alfred alfred@example.com
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl up alfred
  bad_rc
  log_lacks "curl "
}

test_profiles_creates_the_garden_host_before_checking_the_new_list() {
  pre_garden
  broker_config
  ctl profiles cloudflared,search,secrets
  ok_rc
  eq "$(senv alfred CF_GARDEN_AUD)" aud2
  # The move routes the old list; the new list is routed once it is written.
  eq "$(calls | tr '\n' ';')" "${MIGRATION}PUT /accounts/acc1/cfd_tunnel/tun1/configurations;PUT /accounts/acc1/access/apps/app1;PUT /accounts/acc1/access/apps/app2;"
  eq "$(jq -c .config.ingress <<<"$(first_body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)")" "[$GARDEN_404,$LETTUCE_TAIL]"
  (($(line_of "curl POST /zones/zone1/dns_records") < $(line_of "docker-env-file"))) ||
    fail "the new list was checked before the garden host existed"
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/secrets(/|$)","service":"http://secret-broker:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
  log_has "curl PUT /accounts/acc1/access/apps/app2"
}

test_profiles_with_the_same_list_moves_an_older_stack_to_its_garden_host() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  pre_garden
  containers alfred app-server secret-broker
  ctl profiles cloudflared,secrets
  ok_rc
  out_has "nothing to change"
  out_has "alfred: the garden pages are now at https://alfred-garden.example.org/"
  # The routes the move put are this list's: they are not put again.
  eq "$(calls | tr '\n' ';')" "${MIGRATION}PUT /accounts/acc1/access/apps/app1;PUT /accounts/acc1/access/apps/app2;"
  # Its garden services take the new audience and origin.
  log_has "up -d --build"
  # A stack already moved is not restarted.
  : >"$T/calls.log"
  ctl profiles cloudflared,secrets
  ok_rc
  log_lacks "up -d"
  log_lacks "curl POST"
}

test_profiles_stops_before_writing_when_the_garden_host_fails() {
  pre_garden
  broker_config
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl profiles cloudflared,search,secrets
  bad_rc
  out_has "alfred: could not create its garden host on Cloudflare, so no profile was changed"
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

# up_lines NAME -> the `compose up` calls made for stack NAME
up_lines() { grep "up -d --build" "$T/calls.log" | grep -c "stacks/$1/stack.env" || true; }

# A failed `profiles` can stop after moving one stack's Cloudflare side; the
# re-run must still move that stack's containers and give it its token.
test_profiles_rerun_finishes_a_stack_a_failed_run_moved() {
  local tok
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  pre_garden
  ctl add hal hal@example.com
  sed -i '/^GARDEN_HOSTNAME=/d; /^GARDEN_ORIGIN=/d; /^CF_GARDEN_/d' "$H/stacks/hal/stack.env"
  # alfred predates the token's rename.
  tok=$(senv alfred GARDEN_EXECUTOR_TOKEN)
  sed -i "s/^GARDEN_EXECUTOR_TOKEN=.*/GMAIL_TRIGGERS_EXECUTOR_TOKEN=$tok/" "$H/stacks/alfred/stack.env"
  containers alfred app-server secret-broker
  containers hal app-server secret-broker
  : >"$T/calls.log"
  stub 'STUB_CF_TAKEN="hal-garden.example.org lettuce-hal-garden.example.org"'
  ctl profiles cloudflared,secrets
  bad_rc
  eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" rec2
  log_lacks "up -d"
  sed -i '/STUB_CF_TAKEN/d' "$T/stub.env"
  : >"$T/calls.log"
  : >"$T/exec-stdin.log"
  ctl profiles cloudflared,secrets
  ok_rc
  # alfred's garden services take the garden's audience, though this run moved only hal.
  eq "$(up_lines alfred)" 1
  eq "$(up_lines hal)" 1
  eq "$(senv alfred GARDEN_EXECUTOR_TOKEN)" "$tok"
  ! grep -q '^GMAIL_TRIGGERS_EXECUTOR_TOKEN=' "$H/stacks/alfred/stack.env" || fail "the old key is still there"
  # A third run restarts nothing.
  : >"$T/calls.log"
  ctl profiles cloudflared,secrets
  ok_rc
  log_lacks "up -d"
}

# An unchanged `profiles` starts no stack the operator stopped; its next `up` moves it.
test_profiles_leaves_a_stopped_older_stack_to_its_next_up() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  pre_garden
  ctl profiles cloudflared,secrets
  ok_rc
  log_has "curl POST /zones/zone1/dns_records"
  log_lacks "up -d"
  : >"$T/calls.log"
  ctl up alfred
  ok_rc
  log_lacks "curl "
  eq "$(up_lines alfred)" 1
  # Moved: a running stack is not restarted again.
  containers alfred app-server secret-broker
  : >"$T/calls.log"
  ctl profiles cloudflared,secrets
  ok_rc
  log_lacks "up -d"
}

test_the_executor_is_never_installed_without_a_token() {
  added
  with_triggers
  sed -i '/^GARDEN_EXECUTOR_TOKEN=/d' "$H/stacks/alfred/stack.env"
  containers alfred app-server gmail-triggers
  # profiles syncs the executor on a running stack even when the list is unchanged.
  sed -i "s/^PROFILES=.*/PROFILES=$(senv alfred COMPOSE_PROFILES)/" "$H/config.env"
  : >"$T/exec-stdin.log"
  ctl profiles "$(senv alfred COMPOSE_PROFILES)"
  if [[ -s $T/exec-stdin.log ]] && tar -tf "$T/exec-stdin.log" token >/dev/null 2>&1; then
    [[ $(tar -xOf "$T/exec-stdin.log" token) =~ ^[0-9a-f]{64}$ ]] || fail "an executor was installed without a token"
  fi
}

# restart stops the stack only once its garden host exists.
test_restart_stops_nothing_when_the_garden_host_fails() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  pre_garden
  containers alfred app-server secret-broker
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl restart alfred
  bad_rc
  out_has "could not create its garden host on Cloudflare"
  log_lacks " down"
  log_lacks "up -d"
}

# Without a garden page, a stack does not need its garden host to start.
test_up_starts_a_stack_without_garden_pages_when_cloudflare_fails() {
  pre_garden
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl up alfred
  ok_rc
  out_has "could not create its garden host on Cloudflare"
  log_has "up -d --build"
  eq "$(senv alfred CF_GARDEN_DNS_RECORD_ID)" ""
}

test_check_points_an_unfinished_older_stack_at_add() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  # Stopped at its DNS records: everything else exists.
  stub 'STUB_CF_FAIL="POST /zones/zone1/dns_records"'
  ctl add alfred alfred@example.com
  sed -i '/^GARDEN_HOSTNAME=/d; /^GARDEN_ORIGIN=/d; /^CF_GARDEN_/d' "$H/stacks/alfred/stack.env"
  : >"$T/stub.env"
  ctl up alfred
  bad_rc
  out_has "lettucectl add alfred"
}

# Possible only while a stack has no garden host: Lettuce's host alone.
test_tunnel_routes_without_a_garden_host() {
  (
    LETTUCE_HOME=$H
    . "$H/lib/common.sh"
    . "$H/lib/cloudflare.sh"
    PATH="$T/bin:$PATH" cf_tunnel_configure acc1 tun1 alfred.example.org "" cloudflared,secrets,code-server
  ) >"$OUT" 2>&1 || fail "cf_tunnel_configure failed"
  eq "$(ingress)" "[$LETTUCE_TAIL]"
}

test_tunnel_routes_every_garden_page_on_the_garden_host_only() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers,secrets,code-server/' "$H/config.env"
  broker_config
  triggers_config
  code_config
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(ingress)" '[{"hostname":"alfred-garden.example.org","path":"^/secrets(/|$)","service":"http://secret-broker:8000"},{"hostname":"alfred-garden.example.org","path":"^/gmail-triggers(/|$)","service":"http://gmail-triggers:8000"},{"hostname":"alfred-garden.example.org","path":"^/code-server(/|$)","service":"http://code-server:8000"},'"$LANDING,$GARDEN_404,$LETTUCE_TAIL]"
  eq "$(senv alfred GARDEN_PROFILES)" secrets,gmail-triggers,code-server
}

# Only the garden service gets GARDEN_PROFILES: a recreate would relock the vault.
test_only_the_garden_overlay_passes_the_garden_profiles() {
  eq "$(grep -l 'GARDEN_PROFILES' "$ROOT"/compose/*.yml | xargs -n1 basename | paste -sd' ')" garden.yml
}

# with_all_gardens -> alfred runs every garden profile, with every garden service rendered
with_all_gardens() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google,gmail-triggers,secrets,code-server/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,google,gmail-triggers,secrets,code-server/' "$H/stacks/alfred/stack.env"
  broker_config
  triggers_config
  code_config
}

# garden_refused JQ EXPECTED -> with alfred's garden services edited by the jq
# filter JQ, `check` fails and says EXPECTED (on a fresh fixture, so a test may
# call it more than once)
garden_refused() {
  teardown
  setup
  added
  with_all_gardens
  config_edit "$1"
  ctl check alfred
  bad_rc
  out_has "alfred: $2"
}
GS='.services.garden'

test_check_accepts_every_garden_service_as_rendered() {
  added
  with_all_gardens
  ctl check alfred
  ok_rc
}

test_check_refuses_a_garden_profile_without_the_garden() {
  added
  with_secrets
  config_edit "del($GS)"
  ctl check alfred
  bad_rc
  out_has "alfred: the secrets profile is on but garden is not in the rendered config (compose/garden.yml)"
}

test_check_refuses_a_garden_that_is_not_hardened() {
  garden_refused "del($GS.read_only)" "garden must have read_only: true"
  garden_refused "$GS.cap_drop = [\"NET_RAW\"]" "garden must drop all capabilities"
  garden_refused "$GS.network_mode = \"service:app-server\"" "garden must not set network_mode"
  garden_refused "$GS.privileged = true" "garden must not set privileged"
}

test_check_refuses_a_garden_on_another_network_or_with_mounts() {
  garden_refused "$GS.networks.other = null" "garden must be on the default network only"
  garden_refused "$GS.networks = {\"backend\": null}" "garden must be on the default network only"
  garden_refused "$GS.volumes = [{\"type\":\"bind\",\"source\":\"/srv\",\"target\":\"/srv\"}]" "garden must not mount host paths"
  garden_refused "$GS.volumes = [{\"type\":\"volume\",\"source\":\"bff-auth\",\"target\":\"/x\"}]" "garden must not mount anything"
}

test_check_refuses_a_garden_profile_without_its_host_or_audience() {
  local k
  for k in GARDEN_ORIGIN CF_GARDEN_AUD; do
    teardown; setup
    added
    with_secrets
    sed -i "/^$k=/d" "$H/stacks/alfred/stack.env"
    ctl check alfred
    bad_rc
    out_has "alfred: $k is empty; the garden pages need their own host and Access application (run: lettucectl up alfred"
  done
}

test_check_refuses_a_garden_service_that_accepts_lettuces_token() {
  local s
  for s in secret-broker gmail-triggers code-server garden; do
    garden_refused ".services[\"$s\"].environment.CF_ACCESS_AUD = \"\${CF_ACCESS_AUD}\"" \
      "$s must accept only the garden's Access application (CF_ACCESS_AUD = CF_GARDEN_AUD), never Lettuce's"
    garden_refused "del(.services[\"$s\"].environment.CF_ACCESS_AUD)" "$s must accept only the garden's Access application"
    garden_refused ".services[\"$s\"].environment.PUBLIC_ORIGIN = \"\${PUBLIC_ORIGIN}\"" \
      "$s must have PUBLIC_ORIGIN = GARDEN_ORIGIN, the garden's own host"
  done
}

test_check_refuses_a_garden_audience_that_is_lettuces() {
  added
  with_all_gardens
  sed -i 's/^CF_GARDEN_AUD=.*/CF_GARDEN_AUD=aud1/' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "must accept only the garden's Access application"
}

test_check_refuses_a_garden_service_with_lettuces_token_whatever_the_profiles() {
  added
  broker_config
  # shellcheck disable=SC2016 # ${KEY} is for the stub, not for bash
  config_edit '.services["secret-broker"].environment.CF_ACCESS_AUD = "${CF_ACCESS_AUD}"'
  ctl check alfred
  bad_rc
  out_has "alfred: secret-broker must accept only the garden's Access application"
}

test_profiles_dropping_every_garden_profile_removes_the_garden() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets,code-server/' "$H/config.env"
  broker_config
  code_config
  added
  containers alfred app-server bff cloudflared secret-broker garden code-server code-server-editor code-server-exec
  ctl profiles cloudflared,secrets
  ok_rc
  grep -q '^docker compose -p lettuce-alfred .* rm -sf code-server code-server-editor code-server-exec$' "$T/calls.log" ||
    fail "the code editor's containers were not removed, or the garden went with them"
  : >"$T/calls.log"
  # (The stub still lists the code editor's containers.)
  sed -i '/ code-server/d' "$T/containers.txt"
  ctl profiles cloudflared
  ok_rc
  grep -q '^docker compose -p lettuce-alfred .* rm -sf garden secret-broker$' "$T/calls.log" || fail "the garden was not removed"
  eq "$(ingress)" "[$GARDEN_404,$LETTUCE_TAIL]"
}

# A stack with no garden page in its list now or in the new one goes on without
# its garden host, as `up` does, and Cloudflare is asked only once.
test_profiles_goes_on_when_the_garden_host_fails_for_a_stack_without_garden_pages() {
  pre_garden
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl profiles cloudflared,search,google
  ok_rc
  out_has "alfred: could not create its garden host on Cloudflare; it has no garden page, so it goes on without"
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search,google
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search,google
  log_has "up -d --build"
  eq "$(grep -c 'curl POST /accounts/acc1/access/apps' "$T/calls.log")" 1
  # Lettuce's routes and application are still put back; the garden has no application yet.
  log_has "curl PUT /accounts/acc1/access/apps/app1"
  log_lacks "curl PUT /accounts/acc1/access/apps/ "
}

# A stack whose current list has a garden page stops the command, even when the new one drops it.
test_profiles_stops_when_the_garden_host_fails_for_a_stack_with_garden_pages() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  pre_garden
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl profiles cloudflared
  bad_rc
  out_has "alfred: could not create its garden host on Cloudflare, so no profile was changed"
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,secrets
  log_lacks "up -d"
}

# Without a garden page, there is nothing to move: no restart, and no claim that pages moved.
test_a_garden_host_for_a_stack_without_garden_pages_moves_nothing() {
  pre_garden
  containers alfred app-server
  ctl profiles cloudflared,search
  ok_rc
  out_has "alfred: its garden host https://alfred-garden.example.org/ is ready for when a garden profile is on"
  out_lacks "the garden pages are now at"
  eq "$(senv alfred GARDEN_MOVE_PENDING)" ""
  log_lacks "up -d"
}

test_restart_asks_cloudflare_for_the_garden_host_once() {
  pre_garden
  containers alfred app-server
  stub 'STUB_CF_FAIL="POST /accounts/acc1/access/apps"'
  ctl restart alfred
  ok_rc
  eq "$(grep -c 'curl POST /accounts/acc1/access/apps' "$T/calls.log")" 1
  eq "$(grep -c 'could not create its garden host' "$OUT")" 1
  log_has "up -d --build"
}

# One stack whose move fails must not stop the others, nor the rest of the run.
test_profiles_same_list_continues_past_a_stack_that_cannot_move() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  pre_garden
  ctl add hal hal@example.com
  sed -i '/^GARDEN_HOSTNAME=/d; /^GARDEN_ORIGIN=/d; /^CF_GARDEN_/d' "$H/stacks/hal/stack.env"
  containers alfred app-server secret-broker
  containers hal app-server secret-broker google-mcp
  echo "DEV_BYPASS_EMAIL=x@y.com" >>"$H/stacks/alfred/stack.env"
  : >"$T/calls.log"
  ctl profiles cloudflared,secrets
  bad_rc
  out_has "DEV_BYPASS"
  eq "$(up_lines alfred)" 0
  eq "$(up_lines hal)" 1
  grep -q '^docker compose -p lettuce-hal .* rm -sf google-mcp$' "$T/calls.log" || fail "hal's stale services were not removed"
  out_has "could not move the garden services of: alfred"
  out_has "run the same command again"
  eq "$(senv alfred GARDEN_MOVE_PENDING)" 1
  eq "$(senv hal GARDEN_MOVE_PENDING)" ""
}

# Every garden sidecar of PROFILE_ROUTES is held to the garden's audience,
# including one added later.
test_check_holds_every_routed_garden_sidecar_to_the_garden_audience() {
  added
  with_secrets
  sed -i "s#^  'code-server .*#&\n  'notes ^/notes(/|\$) http://notes:8000'#" "$H/lib/cloudflare.sh"
  grep -q "^  'notes " "$H/lib/cloudflare.sh" || fail "fixture: no notes route"
  # shellcheck disable=SC2016 # ${KEY} is for the stub, not for bash
  config_edit '.services.notes = {"environment":{"CF_ACCESS_AUD":"${CF_ACCESS_AUD}","PUBLIC_ORIGIN":"${GARDEN_ORIGIN}"}}'
  ctl check alfred
  bad_rc
  out_has "alfred: notes must accept only the garden's Access application"
}

# Compose starts the garden with any of its profiles; they must be the garden pages'.
test_the_garden_overlay_has_exactly_the_garden_profiles() {
  local routes yml
  routes=$(. "$ROOT/lib/cloudflare.sh" && for r in "${PROFILE_ROUTES[@]}"; do echo "${r%% *}"; done | sort | paste -sd,)
  yml=$(sed -n 's/^ *profiles: *\[\(.*\)\]/\1/p' "$ROOT/compose/garden.yml" | tr -d '" ' | tr , '\n' | sort | paste -sd,)
  [[ -n $routes ]] || fail "no PROFILE_ROUTES"
  eq "$yml" "$routes"
}

# Only the garden service links back to Lettuce; the vault would relock on a change.
test_only_the_garden_overlay_passes_lettuces_origin() {
  eq "$(grep -l 'LETTUCE_ORIGIN' "$ROOT"/compose/*.yml | xargs -n1 basename | paste -sd' ')" garden.yml
}

for t in $(declare -F | awk '$3 ~ /^test_/ { print $3 }'); do
  run_test "$t"
done
printf '\n%d passed, %d failed\n' "$PASS" "${#FAILED[@]}"
((${#FAILED[@]} == 0))
