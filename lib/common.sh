# shellcheck shell=bash
# Shared helpers for lettucectl: messages, env files, config, validation, compose.
# Sourced by lettucectl; expects LETTUCE_HOME to be set.

info() { printf '%s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# ── env files ────────────────────────────────────────────────────────────────
# config.env and stack.env are plain KEY=value files, read by Docker Compose as
# well, so they are parsed here instead of being sourced: nothing in them runs.

# env_get FILE KEY -> prints the value (last occurrence wins); empty if absent.
env_get() {
  [[ -f $1 ]] || return 0
  awk -v k="$2" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END { printf "%s", v }' "$1"
}

# env_has FILE KEY -> success when KEY is present, even with an empty value.
env_has() {
  [[ -f $1 ]] && grep -q "^$2=" "$1"
}

# env_set FILE KEY VALUE -> replaces or appends KEY, keeping the file at 0600.
env_set() {
  local file=$1 key=$2 value=$3 tmp
  [[ $value != *$'\n'* ]] || die "refusing to write a multi-line value for $key"
  tmp=$(mktemp "$file.XXXXXX")
  if [[ -f $file ]]; then
    grep -v "^$key=" "$file" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

# env_unset FILE KEY
env_unset() {
  local file=$1 key=$2 tmp
  [[ -f $file ]] || return 0
  tmp=$(mktemp "$file.XXXXXX")
  grep -v "^$key=" "$file" >"$tmp" || true
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

file_mode() { stat -c '%a' "$1"; }

# version_ge A B -> success when version A >= B
version_ge() { printf '%s\n%s\n' "$2" "$1" | sort -V -C; }

# ── paths and config ─────────────────────────────────────────────────────────
config_file() { printf '%s/config.env' "$LETTUCE_HOME"; }
upstream_dir() { printf '%s/upstream' "$LETTUCE_HOME"; }
token_file() { printf '%s/.secrets/cloudflare-api-token' "$LETTUCE_HOME"; }
stack_dir() { printf '%s/stacks/%s' "$LETTUCE_HOME" "$1"; }
stack_env() { printf '%s/stacks/%s/stack.env' "$LETTUCE_HOME" "$1"; }

# cfg KEY -> value from config.env
cfg() { env_get "$(config_file)" "$1"; }

require_config() {
  local f
  f=$(config_file)
  [[ -f $f ]] || die "no config.env; run: lettucectl init"
  [[ $(file_mode "$f") == 600 ]] || die "config.env must be mode 600 (chmod 600 $f)"
  [[ -n $(cfg DOMAIN) && $(cfg DOMAIN) != example.com ]] || die "set DOMAIN in config.env"
  has_profile "$(cfg PROFILES)" cloudflared || die "PROFILES in config.env must contain cloudflared"
}

require_initialised() {
  require_config
  local k
  for k in CF_ACCOUNT_ID CF_ZONE_ID CF_TEAM_DOMAIN CF_GOOGLE_IDP_ID; do
    [[ -n $(cfg "$k") ]] || die "$k is empty; run: lettucectl init"
  done
  [[ -f $(upstream_dir)/docker/compose.yml ]] || die "upstream/ is missing; run: lettucectl init"
}

# has_profile "a,b,c" b -> exact comma-delimited token match, like upstream's BFF.
has_profile() {
  local IFS=, p
  for p in $1; do
    [[ $p == "$2" ]] && return 0
  done
  return 1
}

# normalise_profiles "cloudflared, search" -> "cloudflared,search" (dies on a bad
# name, a duplicate, or a list without cloudflared)
normalise_profiles() {
  local IFS=, p out=()
  for p in $1; do
    p=$(printf '%s' "$p" | tr -d '[:space:]')
    [[ -z $p ]] && continue
    [[ $p =~ ^[a-z][a-z0-9_-]*$ ]] || die "invalid profile name '$p'"
    if ((${#out[@]})) && has_profile "${out[*]}" "$p"; then
      die "profile '$p' is listed twice"
    fi
    out+=("$p")
  done
  if ((${#out[@]} == 0)) || ! has_profile "${out[*]}" cloudflared; then
    die "the profile list must contain cloudflared"
  fi
  p=$(profile_unmet "${out[*]}")
  [[ -z $p ]] || die "$p"
  printf '%s' "${out[*]}"
}

# profile_unmet "a,b,c" -> why a profile in the list cannot run without another
# that is missing; nothing when every dependency is on
profile_unmet() {
  if has_profile "$1" garden-gmail-triggers && ! has_profile "$1" google; then
    printf 'the garden-gmail-triggers profile needs google (it uses the connection made in Settings -> Google)'
  fi
}

# google_redirect_uri NAME -> the OAuth redirect URI that stack's Settings -> Google
# uses; upstream derives it from PUBLIC_ORIGIN.
google_redirect_uri() {
  printf '%s/api/google/oauth/callback' "$(env_get "$(stack_env "$1")" PUBLIC_ORIGIN)"
}

# ── validation ───────────────────────────────────────────────────────────────
validate_name() {
  [[ $1 =~ ^[a-z][a-z0-9-]{0,19}$ && $1 != *- ]] ||
    die "invalid stack name '$1': lowercase letters, digits and '-', start with a letter, max 20 chars"
}

# normalise_emails "A@x.com, b@y.org" -> "a@x.com,b@y.org" (dies on a bad address)
normalise_emails() {
  local IFS=, e out=()
  for e in $1; do
    e=$(printf '%s' "$e" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
    [[ -z $e ]] && continue
    [[ $e =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "invalid email address '$e'"
    out+=("$e")
  done
  ((${#out[@]})) || die "at least one email address is required"
  printf '%s' "${out[*]}"
}

list_stacks() {
  local d
  for d in "$LETTUCE_HOME"/stacks/*/stack.env; do
    [[ -f $d ]] || continue
    d=${d%/stack.env}
    printf '%s\n' "${d##*/}"
  done
}

require_stack() {
  [[ -f $(stack_env "$1") ]] || die "no stack named '$1' (see: lettucectl list)"
}

# expand_targets NAME|all -> stack names, one per line
expand_targets() {
  if [[ $1 == all ]]; then
    list_stacks
  else
    validate_name "$1"
    require_stack "$1"
    printf '%s\n' "$1"
  fi
}

# ── compose ──────────────────────────────────────────────────────────────────
# compose NAME ARGS... -> docker compose for one stack. `env -i` keeps variables
# exported in the caller's shell from overriding stack.env, and the explicit
# --env-file stops Compose from reading any upstream/docker/.env.
compose() { compose_env "$(stack_env "$1")" "$@"; }

# compose_env ENV_FILE NAME ARGS... -> `compose`, reading ENV_FILE in place of
# the stack's stack.env (check_stack renders a profile list before it is written)
compose_env() {
  local env_file=$1 name=$2 up profiles extra=() vars=()
  shift 2
  up=$(upstream_dir)
  profiles=$(env_get "$env_file" COMPOSE_PROFILES)
  # Only with the profile: without it, the app-server has no doc-tools mount or
  # network. The values are lettucectl's, never stack.env's.
  if has_profile "$profiles" garden-doc-tools; then
    extra+=(-f "$LETTUCE_HOME/compose/doc-tools.yml")
    vars+=(DOC_TOOLS_NETWORK="$(doc_tools_network "$name")" DOC_TOOLS_VOLUME="$(doc_tools_volume)")
  fi
  # Only with the profile: without it, no container gets a proxy setting.
  if has_profile "$profiles" garden-pipelock; then
    extra+=(-f "$LETTUCE_HOME/compose/pipelock-proxy.yml")
  fi
  env -i PATH="$PATH" HOME="$HOME" ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} "${vars[@]}" \
    docker compose -p "lettuce-$name" \
    --project-directory "$up/docker" \
    -f "$up/docker/compose.yml" \
    -f "$LETTUCE_HOME/compose/hardening.yml" \
    -f "$LETTUCE_HOME/compose/multi.yml" \
    -f "$LETTUCE_HOME/compose/vault.yml" \
    -f "$LETTUCE_HOME/compose/gmail-triggers.yml" \
    -f "$LETTUCE_HOME/compose/pipelock.yml" \
    -f "$LETTUCE_HOME/compose/code-server.yml" \
    -f "$LETTUCE_HOME/compose/garden.yml" \
    "${extra[@]}" \
    --env-file "$env_file" \
    "$@"
}

# compose_shared ARGS... -> docker compose for the host's shared services
# (compose/shared/), one project for every stack. Its name has an underscore,
# which no stack name can, so it can never be a stack's project. No stack.env:
# an explicit empty env file keeps Compose from reading any .env.
compose_shared() {
  env -i PATH="$PATH" HOME="$HOME" ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
    docker compose -p lettuce_shared \
    --project-directory "$LETTUCE_HOME/compose/shared" \
    -f "$LETTUCE_HOME/compose/shared/gotenberg.yml" \
    --env-file /dev/null \
    "$@"
}

# ── document tools ───────────────────────────────────────────────────────────
# doc_tools_tag -> the content hash of doc-tools/ (its tests and README excluded), the tag
# of the tools' image and volume every stack with the garden-doc-tools profile shares.
# Any change to what goes into the image is a new tag, so `up` rebuilds it,
# fills a new volume and recreates the app-servers; an unchanged tree reuses
# both. Ten hex characters.
doc_tools_tag() {
  local d=$LETTUCE_HOME/doc-tools
  [[ -f $d/Dockerfile ]] || die "doc-tools/Dockerfile is missing"
  (cd "$d" && find . -type f ! -path './tests/*' ! -path ./README.md ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 |
    LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-10)
}

doc_tools_image() { printf 'lettuce-doc-tools:%s' "$(doc_tools_tag)"; }

# doc_tools_volume -> the volume the app-servers mount: the image's tools, copied
# out by lettucectl (ensure_doc_tools_volume). Not an image mount: Docker 29
# brings those back empty after a daemon restart.
doc_tools_volume() { printf 'lettuce-doc-tools-%s' "$(doc_tools_tag)"; }

# doc_tools_network NAME -> the stack's internal network to the shared Gotenberg
doc_tools_network() { printf 'lettuce-doc-tools-%s' "$1"; }

# DOC_TOOLS_LABEL marks the networks lettucectl made, with the stack's name.
DOC_TOOLS_LABEL=lettuce.doc-tools.stack
# Where the app-server mounts the tools (compose/doc-tools.yml, the launchers and
# the MCP entries people add use it too), and where the image holds them.
DOC_TOOLS_TARGET=/opt/lettuce-doc-tools

# host_docker ARGS... -> plain docker, under `env -i` like `compose`
host_docker() {
  env -i PATH="$PATH" HOME="$HOME" ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} docker "$@"
}

# vault_volume NAME -> the Docker volume holding the stack's vault
vault_volume() { printf 'lettuce-%s_vault-data' "$1"; }

# has_vault NAME -> success when the vault's volume exists. Turning the garden-vault
# profile off keeps it, and `compose down -v` deletes it either way.
has_vault() { host_docker volume inspect "$(vault_volume "$1")" >/dev/null 2>&1; }

# receipts_volume NAME -> the Docker volume holding the stack's pipelock receipts
# and their signing key
receipts_volume() { printf 'lettuce-%s_pipelock-data' "$1"; }

# has_receipts NAME -> success when that volume exists; like the vault's, it
# outlives the profile and goes with `compose down -v`
has_receipts() { host_docker volume inspect "$(receipts_volume "$1")" >/dev/null 2>&1; }

# compose_label_mask CONFIG -> `--label KEY=` for each Compose label of an
# image's config (`image inspect`'s .Config, as JSON). A container inherits its
# image's labels, so Compose and `stale_services` would count a one-off
# container of a Compose-built image as one of the stack's; emptied, the labels
# no longer match the project filters.
compose_label_mask() {
  jq -r '.Labels // {} | keys[] | select(startswith("com.docker.compose.")) | "--label", "\(.)="' <<<"$1"
}

# stack_running NAME -> success when any of the stack's containers is running
stack_running() {
  local running
  running=$(compose "$1" ps --status running --services) || die "$1: docker compose ps failed"
  [[ -n $running ]]
}

# stale_services NAME -> services that have a container in the stack but are not
# enabled by its profiles: Compose leaves those running when a profile is dropped.
stale_services() {
  local enabled existing
  enabled=$(compose "$1" config --services) || die "$1: docker compose config failed"
  existing=$(host_docker ps -a --filter "label=com.docker.compose.project=lettuce-$1" \
    --format '{{.Label "com.docker.compose.service"}}') || die "$1: docker ps failed"
  comm -23 <(sort -u <<<"$existing") <(sort -u <<<"$enabled") | sed '/^$/d'
}

# ── push notifications ───────────────────────────────────────────────────────
# vapid_keypair -> "<public> <private>", base64url as web-push expects: the
# uncompressed P-256 point (65 bytes) and the private scalar (32 bytes). The PEM
# stays in memory.
vapid_keypair() {
  local pem pub priv
  pem=$(openssl ecparam -name prime256v1 -genkey -noout) || die "openssl could not generate a VAPID key"
  pub=$(openssl ec -pubout -outform DER 2>/dev/null <<<"$pem" | tail -c 65 | base64url)
  priv=$(openssl ec -outform DER 2>/dev/null <<<"$pem" | tail -c +8 | head -c 32 | base64url)
  ((${#pub} == 87 && ${#priv} == 43)) || die "openssl produced a malformed VAPID key"
  printf '%s %s' "$pub" "$priv"
}

base64url() { base64 -w0 | tr '+/' '-_' | tr -d '='; }

# ensure_push_keys NAME -> gives the stack its VAPID keypairs once and keeps
# them: a new keypair would invalidate every browser subscription. The BFF and
# the vault each get their own, so neither holds the other's private
# key. The contact address follows config.env.
ensure_push_keys() {
  local f contact pair prefix
  f=$(stack_env "$1")
  contact=$(cfg PUSH_CONTACT_EMAIL)
  [[ -n $contact ]] || die "PUSH_CONTACT_EMAIL is empty in config.env; set it to the operator's email address"
  contact=$(normalise_emails "$contact")
  [[ $contact != *,* ]] || die "PUSH_CONTACT_EMAIL in config.env must be a single address"
  [[ $(env_get "$f" PUSH_VAPID_CONTACT_EMAIL) == "$contact" ]] || env_set "$f" PUSH_VAPID_CONTACT_EMAIL "$contact"
  for prefix in PUSH VAULT; do
    if [[ -z $(env_get "$f" "${prefix}_VAPID_PUBLIC_KEY") || -z $(env_get "$f" "${prefix}_VAPID_PRIVATE_KEY") ]]; then
      pair=$(vapid_keypair)
      env_set "$f" "${prefix}_VAPID_PUBLIC_KEY" "${pair% *}"
      env_set "$f" "${prefix}_VAPID_PRIVATE_KEY" "${pair#* }"
    fi
  done
}

# ensure_executor_token NAME -> gives the stack, once, the token the garden
# executor presents to the /executor/v1/ APIs of garden and gmail-triggers.
# Nothing rotates it; to replace it, delete the line from stack.env and run
# `lettucectl up`.
ensure_executor_token() {
  local f
  f=$(stack_env "$1")
  [[ -n $(env_get "$f" GARDEN_EXECUTOR_TOKEN) ]] ||
    env_set "$f" GARDEN_EXECUTOR_TOKEN "$(openssl rand -hex 32)"
}

# env_without_profile FILE PROFILE -> the path of a private temp copy of the env
# FILE whose COMPOSE_PROFILES lacks PROFILE; the caller removes it
env_without_profile() {
  local tmp list p out=()
  tmp=$(mktemp "$1.XXXXXX")
  chmod 600 "$tmp"
  list=$(env_get "$1" COMPOSE_PROFILES)
  local IFS=,
  for p in $list; do
    [[ $p == "$2" ]] || out+=("$p")
  done
  { grep -v '^COMPOSE_PROFILES=' "$1" || true; printf 'COMPOSE_PROFILES=%s\n' "${out[*]}"; } >"$tmp"
  printf '%s' "$tmp"
}

# sync_garden_profiles NAME -> GARDEN_PROFILES, the garden pages the landing page
# links to, follows COMPOSE_PROFILES. Only the garden service gets it (not the
# vault, which a recreate would relock), and only this list, so a profile
# without a garden page changes nothing in its config.
sync_garden_profiles() {
  local f garden
  f=$(stack_env "$1")
  garden=$(garden_profiles "$(env_get "$f" COMPOSE_PROFILES)")
  [[ $(env_get "$f" GARDEN_PROFILES) == "$garden" ]] || env_set "$f" GARDEN_PROFILES "$garden"
}

# check_stack NAME [--profiles LIST] -> dies with the first reason the stack must
# not start. With --profiles, `profiles` checks the stack as it will be under
# LIST, before it writes the lists: the stack.env and config.env lists may differ
# then, and the config is rendered from a copy of stack.env with LIST in it.
check_stack() {
  local name=$1 f k p profiles rendered published updating sniffing tmp
  f=$(stack_env "$name")
  [[ -f $f ]] || die "$name: stack.env missing"
  [[ $(file_mode "$f") == 600 ]] || die "$name: stack.env must be mode 600"
  if grep -q '^DEV_BYPASS_' "$f"; then
    die "$name: DEV_BYPASS_* is set; it would bypass Cloudflare Access entirely"
  fi
  for k in SESSION_SECRET LETTA_STATE_DIR PUBLIC_ORIGIN ALLOWED_USERS COMPOSE_PROFILES \
    CF_ACCESS_TEAM_DOMAIN CF_ACCESS_AUD CLOUDFLARE_TUNNEL_TOKEN \
    PUSH_VAPID_PUBLIC_KEY PUSH_VAPID_PRIVATE_KEY PUSH_VAPID_CONTACT_EMAIL \
    VAULT_VAPID_PUBLIC_KEY VAULT_VAPID_PRIVATE_KEY; do
    [[ -n $(env_get "$f" "$k") ]] || die "$name: $k is empty"
  done
  k=$(env_get "$f" SESSION_SECRET)
  ((${#k} >= 32)) || die "$name: SESSION_SECRET is shorter than 32 characters"
  if [[ ${2:-} == --profiles ]]; then
    profiles=$3
  else
    profiles=$(env_get "$f" COMPOSE_PROFILES)
    has_profile "$profiles" cloudflared || die "$name: COMPOSE_PROFILES must contain cloudflared"
    [[ $profiles == "$(cfg PROFILES)" ]] ||
      die "$name: COMPOSE_PROFILES ($profiles) differs from config.env PROFILES ($(cfg PROFILES)); all stacks share one image (set both with: lettucectl profiles <list>)"
  fi
  k=$(profile_unmet "$profiles")
  [[ -z $k ]] || die "$name: $k"
  if [[ $profiles == "$(env_get "$f" COMPOSE_PROFILES)" ]]; then
    rendered=$(compose "$name" config --format json) || die "$name: docker compose config failed"
  else
    tmp=$(mktemp "$f.XXXXXX")
    chmod 600 "$tmp"
    { grep -v '^COMPOSE_PROFILES=' "$f" || true; printf 'COMPOSE_PROFILES=%s\n' "$profiles"; } >"$tmp"
    rendered=$(compose_env "$tmp" "$name" config --format json) || {
      rm -f "$tmp"
      die "$name: docker compose config failed"
    }
    rm -f "$tmp"
  fi
  published=$(jq '[.services[] | (.ports // []) | length] | add // 0' <<<"$rendered")
  [[ $published == 0 ]] || die "$name: $published published port(s) in the rendered config; nothing may be published"
  updating=$(jq -r '[.services | to_entries[]
    | select(.key == "app-server" or .key == "channel-gateway")
    | select(.value.environment.DISABLE_AUTOUPDATER != "1") | .key] | join(", ")' <<<"$rendered")
  [[ -z $updating ]] ||
    die "$name: DISABLE_AUTOUPDATER is not 1 for $updating; letta-code would reinstall itself over the pinned version (compose/hardening.yml)"
  sniffing=$(jq -r '[.services | to_entries[]
    | select(.key == "app-server" or .value.network_mode == "service:app-server")
    | select((.value.cap_drop // []) | (index("NET_RAW") or index("ALL")) | not) | .key] | join(", ")' <<<"$rendered")
  [[ -z $sniffing ]] ||
    die "$name: $sniffing must drop NET_RAW; agents in the app-server's network namespace could capture the Access JWT (compose/hardening.yml)"
  k=$(volume_users "$rendered" google-creds bff google-mcp garden-gmail-triggers)
  [[ -z $k ]] || die "$name: $k mount(s) google-creds (the Google token); only bff, google-mcp and garden-gmail-triggers may"
  # volumes_from would hand another service every mount of the one it names.
  k=$(jq -r '([.services | to_entries[]
      | select([(.value.volumes // [])[] | select(.source == "google-creds" or .source == "vault-data"
        or .source == "pipelock-data" or .source == "code-server-data" or .source == "code-exec-keys"
        or .source == "code-server-sock")] | length > 0)
      | .key] + ["garden-gmail-triggers", "garden-vault", "garden-pipelock", "garden-code-server", "garden-code-server-editor", "garden-code-server-exec"]) as $held
    | [.services | to_entries[] | .key as $k | (.value.volumes_from // [])[]
      | sub("^service:"; "") | sub(":(ro|rw)$"; "") | select(. as $t | $held | index($t)) | "\($k) from \(.)"]
    | join(", ")' <<<"$rendered")
  [[ -z $k ]] || die "$name: volumes_from reaches the Google token, the vault, the gmail-triggers rules, the pipelock receipts or the code editor ($k)"
  # Both volumes outlive their profile, so these hold whatever the profiles.
  k=$(volume_users "$rendered" vault-data garden-vault)
  [[ -z $k ]] || die "$name: $k mount(s) the vault's volume; only the vault service may"
  k=$(volume_users "$rendered" pipelock-data garden-pipelock)
  [[ -z $k ]] || die "$name: $k mount(s) pipelock's volume (its signing key and receipts); only garden-pipelock may"
  k=$(volume_users "$rendered" code-server-data garden-code-server-editor)
  [[ -z $k ]] || die "$name: $k mount(s) the code editor's volume (its state and SSH key); only garden-code-server-editor may"
  k=$(volume_users "$rendered" code-exec-keys garden-code-server-editor garden-code-server-exec)
  [[ -z $k ]] || die "$name: $k mount(s) the code editor's SSH keys; only garden-code-server-editor and garden-code-server-exec may"
  k=$(volume_users "$rendered" code-server-sock garden-code-server garden-code-server-editor)
  [[ -z $k ]] || die "$name: $k mount(s) the code editor's socket; only garden-code-server and garden-code-server-editor may"
  # A shared network namespace would reach the editor's socket-less ports and the shell's sshd.
  k=$(jq -r '[.services | to_entries[] | (.value.network_mode // "") as $m
      | select($m | test("^service:garden-code-server(-editor|-exec)?$") or (startswith("container:") and contains("code-server")))
      | "\(.key) shares the network namespace of the code editor (\($m))"] | join("; ")' <<<"$rendered")
  [[ -z $k ]] || die "$name: $k"
  if has_profile "$profiles" garden-vault; then
    check_sidecar "$name" "$rendered" garden-vault garden-vault compose/vault.yml
  fi
  if has_profile "$profiles" garden-pipelock; then
    check_pipelock "$name" "$rendered"
  fi
  if has_profile "$profiles" garden-code-server; then
    check_code_server "$name" "$rendered" "$(env_get "$f" LETTA_STATE_DIR)"
  fi
  if has_profile "$profiles" garden-gmail-triggers; then
    check_gmail_triggers "$name" "$rendered" "$(env_get "$f" LETTA_STATE_DIR)"
  fi
  k=$(garden_profiles "$profiles")
  if [[ -n $k ]]; then
    for p in GARDEN_ORIGIN CF_GARDEN_AUD; do
      [[ -n $(env_get "$f" "$p") ]] || die "$name: $p is empty"
    done
    [[ $(env_get "$f" GARDEN_EXECUTOR_TOKEN) =~ ^[0-9a-f]{64}$ ]] ||
      die "$name: GARDEN_EXECUTOR_TOKEN must be 64 lowercase hex characters (delete the line and run: lettucectl up $name)"
    check_garden "$name" "$rendered" "${k%%,*}"
  fi
  check_garden_audience "$name" "$rendered" "$f"
  # Whatever the profiles: the tools' volume (by key or by name) and image mounts
  # are the app-server's only.
  k=$(jq -r '[.volumes // {} | to_entries[] | select((.value.name // "") | startswith("lettuce-doc-tools")) | .key] as $tools
    | [.services | to_entries[] | select(.key != "app-server")
    | select([(.value.volumes // [])[] | select(.type == "image" or ((.source // "") as $s
        | $s == "doc-tools" or ($s | startswith("lettuce-doc-tools")) or ($tools | index($s))))] | length > 0)
    | .key] | join(", ")' <<<"$rendered")
  [[ -z $k ]] || die "$name: $k mount(s) an image or the document tools; only the app-server may (compose/doc-tools.yml)"
  if has_profile "$profiles" garden-doc-tools; then
    check_doc_tools "$name" "$rendered"
  fi
}

# check_doc_tools NAME RENDERED -> the app-server keeps what compose/doc-tools.yml
# gives it and nothing more: the tools' volume of this tree, read-only, at
# /opt/lettuce-doc-tools; its default network beside the stack's internal one
# to Gotenberg, which no other service joins.
check_doc_tools() {
  local name=$1 rendered=$2 vol net k
  vol=$(doc_tools_volume)
  net=$(doc_tools_network "$name")
  jq -e --arg t "$DOC_TOOLS_TARGET" '[.services["app-server"].volumes // [] | .[] | select(.target == $t)]
    | length == 1 and .[0].type == "volume" and .[0].source == "doc-tools" and .[0].read_only == true' <<<"$rendered" >/dev/null ||
    die "$name: the app-server must mount the volume $vol read-only at /opt/lettuce-doc-tools, and nothing else there (compose/doc-tools.yml)"
  jq -e --arg v "$vol" '.volumes["doc-tools"] | .external == true and .name == $v' <<<"$rendered" >/dev/null ||
    die "$name: the doc-tools volume must be the external $vol (compose/doc-tools.yml)"
  jq -e '.services["app-server"].networks // {} | has("default") and has("doc-tools")' <<<"$rendered" >/dev/null ||
    die "$name: the app-server must be on its default network and doc-tools; without default it loses the rest of the stack (compose/doc-tools.yml)"
  k=$(jq -r '[.services | to_entries[] | select(.key != "app-server") | select(.value.networks // {} | has("doc-tools")) | .key] | join(", ")' <<<"$rendered")
  [[ -z $k ]] || die "$name: $k join(s) the doc-tools network; only the app-server may (compose/doc-tools.yml)"
  jq -e --arg n "$net" '.networks["doc-tools"] | .external == true and .name == $n' <<<"$rendered" >/dev/null ||
    die "$name: the doc-tools network must be the external $net (compose/doc-tools.yml)"
  if host_docker network inspect "$net" >/dev/null 2>&1; then
    k=$(host_docker network inspect "$net" --format "{{.Internal}} {{index .Labels \"$DOC_TOOLS_LABEL\"}}")
    [[ $k == "true $name" ]] ||
      die "$name: $net must be an internal network lettucectl made for $name; remove it (docker network rm $net) and run: lettucectl up $name"
  fi
}


# check_gotenberg RENDERED -> the shared Gotenberg (compose/shared/gotenberg.yml,
# rendered) keeps its hardening (a non-root user and no-new-privileges too),
# publishes nothing, mounts nothing but tmpfs, joins only internal networks,
# and keeps the flags that stop a document from fetching URLs, none of them
# turned back off by a later --flag=false
check_gotenberg() {
  local rendered=$1 svc flag
  svc=$(jq -c '.services.gotenberg // empty' <<<"$rendered")
  [[ -n $svc ]] || die "the shared gotenberg service is missing (compose/shared/gotenberg.yml)"
  check_hardened shared gotenberg "$svc"
  jq -e '((.user // "") | . != "" and (test("^(0|root)(:|$)") | not))
    and ((.security_opt // []) | any(. == "no-new-privileges:true" or . == "no-new-privileges"))' <<<"$svc" >/dev/null ||
    die "shared: gotenberg must run as a non-root user with no-new-privileges (compose/shared/gotenberg.yml)"
  jq -e '(.ports // []) | length == 0' <<<"$svc" >/dev/null || die "shared: gotenberg must not publish ports"
  jq -e '(.volumes // []) | length == 0' <<<"$svc" >/dev/null || die "shared: gotenberg must not mount anything (tmpfs only)"
  jq -e '[.networks // {} | to_entries[] | select(.value.internal != true)] | length == 0' <<<"$rendered" >/dev/null ||
    die "shared: gotenberg's own network must be internal"
  for flag in --chromium-deny-private-ips --chromium-deny-public-ips --chromium-disable-javascript \
    --libreoffice-deny-private-ips --libreoffice-deny-public-ips --api-disable-download-from --webhook-disable; do
    jq -e --arg f "$flag" '(.command // []) | index($f) and all(.[]; (startswith($f + "=") | not) or . == $f + "=true")' <<<"$svc" >/dev/null ||
      die "shared: gotenberg must run with $flag; without it a document can fetch URLs inside the host (compose/shared/gotenberg.yml)"
  done
  jq -e '[(.command // [])[] | select(test("allow-list|disable-web-security|log-level=debug"))] | length == 0' <<<"$svc" >/dev/null ||
    die "shared: gotenberg must not set an allow-list, disable web security or log at debug (compose/shared/gotenberg.yml)"
}

# paths_overlap A B -> success when A and B are the same path or one contains
# the other ("/" contains every path)
paths_overlap() {
  local a=${1%/} b=${2%/}
  [[ -z $a || -z $b || $a == "$b" || $a == "$b"/* || $b == "$a"/* ]]
}

# reaches PATH TARGET -> success when PATH, as written or with its symlinks
# resolved, overlaps TARGET
reaches() {
  paths_overlap "$1" "$2" || paths_overlap "$(realpath -m -- "$1")" "$(realpath -m -- "$2")"
}

# volume_users RENDERED VOLUME SERVICE... -> the services, other than these, that
# mount the named volume VOLUME, comma-separated
volume_users() {
  local rendered=$1 volume=$2
  shift 2
  jq -r --arg v "$volume" '[.services | to_entries[] | select(.key as $k | $ARGS.positional | index($k) | not)
    | select([(.value.volumes // [])[] | select(.source == $v)] | length > 0) | .key] | join(", ")' --args "$@" <<<"$rendered"
}

# check_hardened NAME SERVICE SVC -> the sidecar SERVICE (rendered as the JSON
# SVC) keeps what its overlay gives it: a read-only root filesystem, no
# capabilities, and what check_isolated checks
check_hardened() {
  local name=$1 s=$2 svc=$3
  jq -e '.read_only == true' <<<"$svc" >/dev/null || die "$name: $s must have read_only: true"
  jq -e '(.cap_drop // []) | index("ALL")' <<<"$svc" >/dev/null || die "$name: $s must drop all capabilities (cap_drop: [ALL])"
  check_isolated "$name" "$s" "$svc"
}

# check_isolated NAME SERVICE SVC -> SERVICE gains no capability or privilege,
# keeps its own namespaces, borrows no mounts and has no devices
check_isolated() {
  local name=$1 s=$2 svc=$3 k
  k=$(jq -r '[(select(.privileged == true) | "privileged"), (select((.cap_add // []) | length > 0) | "cap_add"),
    (select(.network_mode != null) | "network_mode"), (select(.pid != null) | "pid"), (select(.ipc != null) | "ipc"),
    (select((.volumes_from // []) | length > 0) | "volumes_from"), (select((.devices // []) | length > 0) | "devices")]
    | join(", ")' <<<"$svc")
  [[ -z $k ]] || die "$name: $s must not set $k"
}

# check_sidecar NAME RENDERED PROFILE SERVICE OVERLAY -> the sidecar SERVICE,
# whose PROFILE is on, keeps the isolation OVERLAY gives it: it is rendered,
# hardened (check_hardened) and has no host paths
check_sidecar() {
  local name=$1 profile=$3 s=$4 overlay=$5 svc
  svc=$(jq -c --arg s "$s" '.services[$s] // empty' <<<"$2")
  [[ -n $svc ]] || die "$name: the $profile profile is on but $s is not in the rendered config ($overlay)"
  check_hardened "$name" "$s" "$svc"
  jq -e '[(.volumes // [])[] | select(.type != "volume" and .type != "tmpfs")] | length == 0' <<<"$svc" >/dev/null ||
    die "$name: $s must not mount host paths"
}

# check_pipelock NAME RENDERED -> the pipelock service keeps what
# compose/pipelock.yml gives it (check_sidecar), and the app-server uses it
# (compose/pipelock-proxy.yml) for everything but stack-internal traffic:
# pipelock must never see the vault's values or the executor's token (which
# goes to gmail-triggers and garden).
check_pipelock() {
  local name=$1 rendered=$2 k v
  check_sidecar "$name" "$rendered" garden-pipelock garden-pipelock compose/pipelock.yml
  for k in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy; do
    v=$(jq -r --arg k "$k" '.services["app-server"].environment[$k] // ""' <<<"$rendered")
    [[ $v == http://garden-pipelock:8888 ]] ||
      die "$name: the app-server's $k must be http://garden-pipelock:8888 with the garden-pipelock profile (compose/pipelock-proxy.yml)"
  done
  for k in NO_PROXY no_proxy; do
    for v in garden-vault garden-gmail-triggers garden; do
      jq -e --arg k "$k" --arg v "$v" '.services["app-server"].environment[$k] // "" | split(",") | index($v)' <<<"$rendered" >/dev/null ||
        die "$name: the app-server's $k must list $v; pipelock must not see its traffic (compose/pipelock-proxy.yml)"
    done
  done
}

# check_garden NAME RENDERED PROFILE -> the garden service (the landing page and
# the agent list), on while PROFILE (or any garden page profile) is, keeps what
# compose/garden.yml gives it: a hardened sidecar (check_sidecar) on the default
# network only, with no mounts at all (the agent list lives in memory).
check_garden() {
  local name=$1 rendered=$2 svc
  check_sidecar "$name" "$rendered" "$3" garden compose/garden.yml
  svc=$(jq -c '.services.garden' <<<"$rendered")
  jq -e '(.networks // {"default": null} | keys) == ["default"]' <<<"$svc" >/dev/null ||
    die "$name: garden must be on the default network only"
  jq -e '(.volumes // []) | length == 0' <<<"$svc" >/dev/null ||
    die "$name: garden must not mount anything (compose/garden.yml)"
}

# check_garden_audience NAME RENDERED ENV_FILE -> every garden service in the
# rendered config (garden_services, so a new garden page is held to it too)
# accepts only the garden's Access application and origin:
# CF_ACCESS_AUD is the env file's CF_GARDEN_AUD, never Lettuce's CF_ACCESS_AUD,
# and PUBLIC_ORIGIN is its GARDEN_ORIGIN. Lettuce's token reaches the BFF in
# plain HTTP inside the namespace agents share; the garden must refuse it.
check_garden_audience() {
  local name=$1 rendered=$2 f=$3 s aud origin
  for s in $(garden_services); do
    jq -e --arg s "$s" '.services | has($s)' <<<"$rendered" >/dev/null || continue
    aud=$(jq -r --arg s "$s" '.services[$s].environment.CF_ACCESS_AUD // ""' <<<"$rendered")
    origin=$(jq -r --arg s "$s" '.services[$s].environment.PUBLIC_ORIGIN // ""' <<<"$rendered")
    [[ -n $aud && $aud == "$(env_get "$f" CF_GARDEN_AUD)" && $aud != "$(env_get "$f" CF_ACCESS_AUD)" ]] ||
      die "$name: $s must accept only the garden's Access application (CF_ACCESS_AUD = CF_GARDEN_AUD), never Lettuce's, whose token agents could catch"
    [[ $origin == "$(env_get "$f" GARDEN_ORIGIN)" ]] ||
      die "$name: $s must have PUBLIC_ORIGIN = GARDEN_ORIGIN, the garden's own host"
  done
}

# check_gmail_triggers NAME RENDERED STATE_DIR -> the gmail-triggers service keeps
# what compose/gmail-triggers.yml gives it: hardened (check_hardened), one host
# path (STATE_DIR/garden/gmail-triggers), the Google volumes read-only and
# nothing else; and nothing else reaches the rules: no other bind mount or
# volume device at, in or above STATE_DIR/garden, and no symlink on the way there.
check_gmail_triggers() {
  local name=$1 rendered=$2 state=${3%/} svc garden k p o
  garden=$state/garden
  svc=$(jq -c '.services["garden-gmail-triggers"] // empty' <<<"$rendered")
  [[ -n $svc ]] || die "$name: the garden-gmail-triggers profile is on but garden-gmail-triggers is not in the rendered config (compose/gmail-triggers.yml)"
  check_hardened "$name" garden-gmail-triggers "$svc"
  k=$(jq -c '[(.volumes // [])[] | select(.type == "bind") | [.source, .target]]' <<<"$svc")
  [[ $k == "$(jq -cn --arg s "$garden/gmail-triggers" '[[$s, "/data"]]')" ]] ||
    die "$name: garden-gmail-triggers may mount from the host only $garden/gmail-triggers (at /data)"
  jq -e '[(.volumes // [])[] | select(.source == "google-creds" or .source == "google-policy") | select(.read_only != true)] | length == 0' <<<"$svc" >/dev/null ||
    die "$name: garden-gmail-triggers must mount google-creds and google-policy read-only"
  k=$(jq -r '[(.volumes // [])[] | select(.type != "bind")
    | select(.source != "google-creds" and .source != "google-policy") | .source // .type] | join(", ")' <<<"$svc")
  [[ -z $k ]] || die "$name: garden-gmail-triggers must not mount $k; only its state and the Google volumes"
  for p in "$state" "$garden" "$garden/gmail-triggers"; do
    [[ ! -L $p ]] || die "$name: $p is a symlink; Docker would follow it, so the gmail-triggers rules need real directories"
  done
  while IFS=$'\t' read -r k p; do
    ! reaches "$p" "$garden" || die "$name: $k mounts $p, which reaches $garden, where the gmail-triggers rules live; only garden-gmail-triggers may"
  done < <(jq -r '.services | to_entries[] | select(.key != "garden-gmail-triggers") | .key as $k
    | (.value.volumes // [])[] | select(.type == "bind") | [$k, .source] | @tsv' <<<"$rendered")
  while IFS=$'\t' read -r k p o; do
    if [[ $p != /* ]]; then
      [[ $o != *bind* ]] || die "$name: volume $k binds the relative path $p; use an absolute one"
      continue
    fi
    ! reaches "$p" "$garden" || die "$name: volume $k is bound to $p, which reaches $garden, where the gmail-triggers rules live"
  done < <(jq -r '(.volumes // {}) | to_entries[] | select(.value.driver_opts.device != null)
    | [.key, .value.driver_opts.device, (.value.driver_opts.o // "")] | @tsv' <<<"$rendered")
}

# check_code_server NAME RENDERED STATE_DIR -> the code editor's three services
# keep what compose/code-server.yml gives them (docs/code-server.md):
# - the gate (garden-code-server): a hardened sidecar (check_sidecar), on default only,
#   with the editor's socket read-only as its one mount;
# - the editor (garden-code-server-editor): hardened, the workspaces as its one host
#   path, its own volume, the SSH keys and its socket, on code-exec only,
#   listening only on its socket (no port: it has no Access check), started
#   without the port proxy, extensions or a marketplace, and with nothing that
#   switches workspace trust off or trusts other origins; each flag that matters
#   appears once, in the `--flag value` form, since code-server takes the last;
# - the shell (garden-code-server-exec): isolated (check_isolated) without NET_RAW, the
#   workspaces and the agents' tools as its host paths, the SSH keys read-only,
#   on code-exec and code-egress only, never the stack's default network;
# and nobody else on their networks, of which only code-egress leads out.
check_code_server() {
  local name=$1 rendered=$2 state=${3%/} gate editor shell k p net members
  gate=$(jq -c '.services["garden-code-server"] // empty' <<<"$rendered")
  editor=$(jq -c '.services["garden-code-server-editor"] // empty' <<<"$rendered")
  shell=$(jq -c '.services["garden-code-server-exec"] // empty' <<<"$rendered")
  for k in garden-code-server garden-code-server-editor garden-code-server-exec; do
    jq -e --arg s "$k" '.services | has($s)' <<<"$rendered" >/dev/null ||
      die "$name: the garden-code-server profile is on but $k is not in the rendered config (compose/code-server.yml)"
  done

  check_sidecar "$name" "$rendered" garden-code-server garden-code-server compose/code-server.yml
  jq -e '(.networks // {} | keys) == ["default"]' <<<"$gate" >/dev/null ||
    die "$name: garden-code-server (the gate) must be on the default network only"
  k=$(jq -c '[(.volumes // [])[] | [.type, .source, .target, (.read_only // false)]]' <<<"$gate")
  [[ $k == '[["volume","code-server-sock","/run/code-server",true]]' ]] ||
    die "$name: garden-code-server (the gate) may mount only the volume code-server-sock, read-only (at /run/code-server)"

  check_hardened "$name" garden-code-server-editor "$editor"
  k=$(jq -c '[(.volumes // [])[] | select(.type == "bind") | [.source, .target]]' <<<"$editor")
  [[ $k == "$(jq -cn --arg w "$state/workspaces" '[[$w, "/work"]]')" ]] ||
    die "$name: garden-code-server-editor may mount from the host only $state/workspaces (at /work)"
  k=$(jq -c '[(.volumes // [])[] | select(.type == "volume") | [.source, .target, (.read_only // false)]] | sort' <<<"$editor")
  [[ $k == '[["code-exec-keys","/keys",false],["code-server-data","/data",false],["code-server-sock","/run/code-server",false]]' ]] ||
    die "$name: garden-code-server-editor may mount only the volumes code-server-data (at /data), code-exec-keys (at /keys) and code-server-sock (at /run/code-server)"
  jq -e '(.networks // {} | keys) == ["code-exec"]' <<<"$editor" >/dev/null ||
    die "$name: garden-code-server-editor must be on the code-exec network only"
  # once FLAG VALUE: FLAG appears exactly once, followed by VALUE, and never as FLAG=...
  jq -e 'def once($f; $v): ([.[] | select(. == $f)] | length == 1) and (any(.[]; startswith($f + "=")) | not)
      and (index($f) as $i | $v == null or .[$i + 1] == $v);
    (.command // []) | once("--socket"; "/run/code-server/editor.sock") and once("--auth"; "none")
    and once("--disable-proxy"; null) and once("--extensions-dir"; "/opt/lettuce-editor/no-extensions")
    and once("--user-data-dir"; "/data/user") and once("--config"; "/opt/lettuce-editor/config.yaml")' <<<"$editor" >/dev/null ||
    die "$name: garden-code-server-editor must run with --socket /run/code-server/editor.sock, --auth none, --disable-proxy, --extensions-dir /opt/lettuce-editor/no-extensions, --user-data-dir /data/user and --config /opt/lettuce-editor/config.yaml, each once (compose/code-server.yml)"
  k=$(jq -r '[(.command // [])[] | select(test("^--(bind-addr|socket-fd|disable-workspace-trust|link|proxy-domain|trusted-origins|enable-proposed-api|install-extension|github-auth|builtin-extensions-dir|abs-proxy-base-path)(=|$)"))]
    | join(", ")' <<<"$editor")
  [[ -z $k ]] || die "$name: garden-code-server-editor must not run with $k (compose/code-server.yml)"
  jq -e '(.environment // {}).EXTENSIONS_GALLERY == "{}"' <<<"$editor" >/dev/null ||
    die "$name: garden-code-server-editor must have EXTENSIONS_GALLERY={} (no marketplace)"

  check_isolated "$name" garden-code-server-exec "$shell"
  jq -e '(.cap_drop // []) | (index("NET_RAW") or index("ALL"))' <<<"$shell" >/dev/null ||
    die "$name: garden-code-server-exec must drop NET_RAW"
  k=$(jq -c '[(.volumes // [])[] | select(.type == "bind") | [.source, .target]] | sort' <<<"$shell")
  [[ $k == "$(jq -cn --arg w "$state/workspaces" --arg t "$state/tools" '[[$t, "/opt/agent-tools"], [$w, "/work"]]')" ]] ||
    die "$name: garden-code-server-exec may mount from the host only $state/workspaces (at /work) and $state/tools (at /opt/agent-tools)"
  k=$(jq -c '[(.volumes // [])[] | select(.type == "volume") | [.source, .target, (.read_only // false)]]' <<<"$shell")
  [[ $k == '[["code-exec-keys","/keys",true]]' ]] ||
    die "$name: garden-code-server-exec may mount only the volume code-exec-keys, read-only (at /keys)"
  jq -e '(.networks // {} | keys) == ["code-egress", "code-exec"]' <<<"$shell" >/dev/null ||
    die "$name: garden-code-server-exec must be on the code-exec and code-egress networks only"

  for net in code-exec:garden-code-server-editor,garden-code-server-exec code-egress:garden-code-server-exec; do
    members=$(jq -r --arg n "${net%%:*}" '[.services | to_entries[] | select(.value.networks // {} | has($n)) | .key]
      | sort | join(",")' <<<"$rendered")
    [[ $members == "${net#*:}" ]] ||
      die "$name: only ${net#*:} may join the ${net%%:*} network (found: ${members:-none})"
  done
  jq -e '.networks["code-exec"].internal == true' <<<"$rendered" >/dev/null ||
    die "$name: the code-exec network must be internal (compose/code-server.yml)"
  for p in "$state" "$state/workspaces" "$state/tools"; do
    [[ ! -L $p ]] || die "$name: $p is a symlink; Docker would follow it into the code editor's containers"
  done
}
