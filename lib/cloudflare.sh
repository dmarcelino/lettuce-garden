# shellcheck shell=bash
# Cloudflare API v4 helpers (curl + jq). Sourced by lettucectl after common.sh.
#
# The API token never reaches argv: curl reads it from a config block on stdin,
# so it does not show up in `ps` or shell history. Request bodies carry no
# secrets and go in argv.
#
# Token permissions (docs/cloudflare-setup.md): Zone Read + DNS Write on the
# zone; account Cloudflare Tunnel Write, Access: Apps and Policies Write, and
# Access: Organizations, Identity Providers, and Groups Read.

CF_API=https://api.cloudflare.com/client/v4

# cf_api METHOD PATH [JSON_BODY] -> prints .result; returns 1 (with the API's
# errors on stderr) when the call is not successful.
cf_api() {
  local method=$1 path=$2 body=${3:-} token resp errors
  token=$(<"$(token_file)")
  token=${token//[$'\t\r\n ']/}
  local args=(-sS -X "$method" --config - -H 'Content-Type: application/json')
  [[ -n $body ]] && args+=(--data-binary "$body")
  if ! resp=$(printf 'header = "Authorization: Bearer %s"\n' "$token" | curl "${args[@]}" "$CF_API$path"); then
    printf 'Cloudflare API %s %s: request failed\n' "$method" "${path%%\?*}" >&2
    return 1
  fi
  if [[ $(jq -r '.success' <<<"$resp" 2>/dev/null) != true ]]; then
    errors=$(jq -r '[.errors[]? | "\(.code): \(.message)"] | join("; ")' <<<"$resp" 2>/dev/null) || errors=$resp
    printf 'Cloudflare API %s %s: %s\n' "$method" "${path%%\?*}" "${errors:-unknown error}" >&2
    return 1
  fi
  jq -c '.result' <<<"$resp"
}

# cf_zone DOMAIN -> "zone_id account_id", empty when the zone is not visible
cf_zone() {
  local r
  r=$(cf_api GET "/zones?name=$1") || return 1
  jq -r 'if length == 0 then empty else "\(.[0].id) \(.[0].account.id)" end' <<<"$r"
}

# cf_team ACCOUNT -> the <team> of <team>.cloudflareaccess.com
cf_team() {
  local r
  r=$(cf_api GET "/accounts/$1/access/organizations") || return 1
  jq -r '.auth_domain // empty | sub("\\.cloudflareaccess\\.com$"; "")' <<<"$r"
}

# cf_google_idp ACCOUNT -> id of the Google login method (Google Workspace as a fallback)
cf_google_idp() {
  local r
  r=$(cf_api GET "/accounts/$1/access/identity_providers") || return 1
  jq -r '(map(select(.type == "google")) + map(select(.type == "google-apps")))[0].id // empty' <<<"$r"
}

# cf_dns_name_taken ZONE HOST -> success when any record with that exact name exists
cf_dns_name_taken() {
  local r
  r=$(cf_api GET "/zones/$1/dns_records?name.exact=$2") || return 2
  [[ $(jq 'length' <<<"$r") != 0 ]]
}

cf_policy_body() {
  jq -cn --arg name "$1" --arg emails "$2" \
    '{name: $name, decision: "allow", include: ($emails | split(",") | map({email: {email: .}}))}'
}

# cf_policy_create ACCOUNT NAME EMAILS -> policy id
cf_policy_create() {
  local r
  r=$(cf_api POST "/accounts/$1/access/policies" "$(cf_policy_body "$2" "$3")") || return 1
  jq -r '.id' <<<"$r"
}

# cf_policy_update ACCOUNT POLICY_ID NAME EMAILS
cf_policy_update() {
  cf_api PUT "/accounts/$1/access/policies/$2" "$(cf_policy_body "$3" "$4")" >/dev/null
}

# cf_app_body NAME HOST IDP SESSION_DURATION POLICY_ID -> the stack's Access application as
# lettucectl defines it. Its cookie is HttpOnly, so no script on the stack's origin (Lettuce's,
# the garden pages', the code editor's) can read the person's Access token and hand it to an
# agent, and SameSite=Lax. Cloudflare's docs disagree on the defaults, so both are explicit.
cf_app_body() {
  jq -cn --arg name "$1" --arg host "$2" --arg idp "$3" --arg dur "$4" --arg pol "$5" '{
    type: "self_hosted", name: $name, domain: $host,
    destinations: [{type: "public", uri: $host}],
    allowed_idps: [$idp], auto_redirect_to_identity: true,
    session_duration: $dur, app_launcher_visible: false,
    http_only_cookie_attribute: true, same_site_cookie_attribute: "lax",
    policies: [{id: $pol, precedence: 1}]}'
}

# cf_app_create ACCOUNT NAME HOST IDP SESSION_DURATION POLICY_ID -> "app_id aud"
cf_app_create() {
  local r
  r=$(cf_api POST "/accounts/$1/access/apps" "$(cf_app_body "${@:2}")") || return 1
  jq -r '"\(.id) \(.aud)"' <<<"$r"
}

# cf_app_update ACCOUNT APP_ID NAME HOST IDP SESSION_DURATION POLICY_ID: puts the
# application back to cf_app_body (a PUT replaces its settings), so stacks made
# before a setting existed get it too
cf_app_update() {
  cf_api PUT "/accounts/$1/access/apps/$2" "$(cf_app_body "${@:3}")" >/dev/null
}

# cf_tunnel_create ACCOUNT NAME -> tunnel id (remotely managed: ingress lives in Cloudflare)
cf_tunnel_create() {
  local r
  r=$(cf_api POST "/accounts/$1/cfd_tunnel" "$(jq -cn --arg n "$2" '{name: $n, config_src: "cloudflare"}')") || return 1
  jq -r '.id' <<<"$r"
}

# cf_tunnel_token ACCOUNT TUNNEL_ID -> the token cloudflared runs with
cf_tunnel_token() {
  local r
  r=$(cf_api GET "/accounts/$1/cfd_tunnel/$2/token") || return 1
  jq -r '.' <<<"$r"
}

# Routes that profiles add on the stack's hostname, ahead of the app-server:
# "<profile> <path regex> <service>". Each service has its own UI and Access check.
# Each serves /garden/<profile>/, which the garden landing page links to.
PROFILE_ROUTES=(
  'secrets ^/garden/secrets(/|$) http://secret-broker:8000'
  'gmail-triggers ^/garden/gmail-triggers(/|$) http://gmail-triggers:8000'
  'code-server ^/garden/code-server(/|$) http://code-server:8000'
)

# garden_profiles PROFILES -> the PROFILE_ROUTES profiles in PROFILES, comma-separated,
# in PROFILE_ROUTES order
garden_profiles() {
  local r p out=''
  for r in "${PROFILE_ROUTES[@]}"; do
    p=${r%% *}
    has_profile "$1" "$p" && out+=${out:+,}$p
  done
  printf '%s\n' "$out"
}

# cf_tunnel_configure ACCOUNT TUNNEL_ID HOST PROFILES -> HOST to the stack's
# app-server, 404 for anything else; first, the PROFILE_ROUTES of the profiles
# that are on (same hostname, same Access app), then /garden itself to the
# first of them, which renders the landing page.
cf_tunnel_configure() {
  local r p path svc landing='' routes='[]'
  for r in "${PROFILE_ROUTES[@]}"; do
    read -r p path svc <<<"$r"
    has_profile "${4:-}" "$p" || continue
    routes=$(jq -c --arg h "$3" --arg p "$path" --arg s "$svc" '. + [{hostname: $h, path: $p, service: $s}]' <<<"$routes")
    landing=${landing:-$svc}
  done
  if [[ -n $landing ]]; then
    routes=$(jq -c --arg h "$3" --arg s "$landing" '. + [{hostname: $h, path: "^/garden/?$", service: $s}]' <<<"$routes")
  fi
  cf_api PUT "/accounts/$1/cfd_tunnel/$2/configurations" "$(jq -cn --arg h "$3" --argjson r "$routes" '{config: {ingress: (
    $r + [{hostname: $h, service: "http://app-server:8080"}, {service: "http_status:404"}])}}')" >/dev/null
}

# cf_dns_cname_create ZONE HOST TUNNEL_ID COMMENT -> record id
cf_dns_cname_create() {
  local r
  r=$(cf_api POST "/zones/$1/dns_records" "$(jq -cn --arg h "$2" --arg t "$3.cfargotunnel.com" --arg c "$4" \
    '{type: "CNAME", name: $h, content: $t, proxied: true, ttl: 1, comment: $c}')") || return 1
  jq -r '.id' <<<"$r"
}
