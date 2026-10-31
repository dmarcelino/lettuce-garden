# lib

The code behind `lettucectl`, the Bash CLI at the repo root. `lettucectl` holds argument handling and one `cmd_*` function per subcommand, dispatched from `main`; it sources the two shell libraries here, and streams the Node script into a stack's app-server when it needs to change Settings -> MCP. No profile: the CLI runs on the host for every stack. The operator's guides are [docs/operations.md](../docs/operations.md) and [docs/cloudflare-setup.md](../docs/cloudflare-setup.md).

![lettucectl, its libraries and what they reach](../docs/diagrams/lettucectl-architecture.svg)

An interactive version, [lettucectl-architecture.html](../docs/diagrams/lettucectl-architecture.html), opens in a browser from a clone of this repo.

## Files
| File | What it does |
|---|---|
| `common.sh` | Messages (`info`, `warn`, `die`); the env-file helpers (`env_get`, `env_set`, `env_unset`, `cfg`); paths; validation of names, emails and profiles; the `compose`, `compose_env`, `compose_shared` and `host_docker` wrappers; doc-tools names and tags; VAPID key and executor token generation; `stale_services`; and `check_stack` with its `check_*` helpers |
| `cloudflare.sh` | Cloudflare API v4: `cf_api` (curl + jq) and one `cf_*` helper per resource (zone, Zero Trust team, Google login method, DNS lookup, Access policy and applications, tunnel, tunnel token, tunnel ingress, DNS CNAME); `PROFILE_ROUTES`, the garden host's routes per profile |
| `mcp-servers.mjs` | Adds or removes one group of lettucectl's servers (`doc-tools` or `vault`) in the stack's shared MCP list. Node, no dependencies; never run on the host |

`lettucectl` sources `common.sh`, then `cloudflare.sh`; both expect `LETTUCE_HOME` to be set and define functions only.

## How it plugs in
- **Env files are parsed, never sourced.** `config.env` (global settings plus cached Cloudflare ids) and `stacks/<name>/stack.env` (that stack's secrets and Cloudflare ids) are plain `KEY=value` files that Docker Compose also reads. `env_get` reads a key with awk (last occurrence wins), and `env_set`/`env_unset` rewrite the file through a temp file and `mv`, keeping it mode 600. Nothing in them runs, so no quotes or inline comments.
- **Compose.** Every `docker compose` call for a stack goes through `compose NAME ...`, which runs it under `env -i` with `-p lettuce-<name>`, `--project-directory upstream/docker`, the overlays in order and `--env-file stack.env` (see [compose/README.md](../compose/README.md)). `compose_env ENV_FILE NAME ...` is the same with another env file, which `check_stack --profiles` uses to render a profile list before it is written. Plain `docker` calls go through `host_docker`, also under `env -i`.
- **`check_stack` gates every `up`.** It reads `stack.env`, renders the merged config and dies on the first unsafe setting. The rules, and which profile turns each one on, are listed under "Key invariants" in [AGENTS.md](../AGENTS.md); new safety rules belong there and in `check_stack`.
- **Cloudflare.** `cf_api METHOD PATH [BODY]` reads the token from `.secrets/cloudflare-api-token` and hands it to curl as a config block on stdin (`--config -`), so it never appears in argv, `ps` or shell history. Request bodies carry no secrets and go in argv. It prints `.result`, or the API's errors and returns 1. `init`, `add`, `users`, `remove` and `profiles` call it; `up` never does.
- **The MCP list.** `mcp_servers` in `lettucectl` runs `compose NAME exec -T app-server node --input-type=module - add|remove FILE GROUP` with `mcp-servers.mjs` on stdin. The script owns only its group's entries in `/root/.letta/mcp-home/.letta/settings.json`, refuses a file of any other shape, and writes a temp file it renames only when the original is unchanged, then reads it back to catch an overwrite by the BFF. See docs/doc-tools.md and docs/vault.md for what each group registers.

### `stack.env` is the state record for `add` and `remove`
`cmd_add` creates the stack's Cloudflare resources in a fixed order and stores each id in `stack.env` as soon as it exists, so a failed run resumes where it stopped:

| # | Resource | Helper | Stored as |
|---|---|---|---|
| 1 | Access policy (the stack's people) | `cf_policy_create` | `CF_ACCESS_POLICY_ID` |
| 2 | Lettuce's Access application | `cf_app_create` | `CF_ACCESS_APP_ID`, `CF_ACCESS_AUD` |
| 3 | The garden's Access application | `create_garden_app` | `CF_GARDEN_APP_ID`, `CF_GARDEN_AUD` |
| 4 | Tunnel | `cf_tunnel_create` | `CF_TUNNEL_ID` |
| 5 | Tunnel token | `cf_tunnel_token` | `CLOUDFLARE_TUNNEL_TOKEN` |
| 6 | Tunnel ingress (routes) | `configure_tunnel` | nothing; reapplied on every run and every `profiles` |
| 7 | The garden host's DNS CNAME | `create_garden_dns` | `CF_GARDEN_DNS_RECORD_ID` |
| 8 | Lettuce's DNS CNAME | `cf_dns_cname_create` | `CF_DNS_RECORD_ID` |

`CF_DNS_RECORD_ID` comes last: once it is set the stack is complete, and `add` refuses to run again. Then `add` starts the stack (`up_stack`, `sync_executor`, `sync_doc_tools`) and registers its MCP groups.

`cmd_remove` stops the stack (`compose down -v`), removes its doc-tools network, then deletes in reverse: Lettuce's DNS record, the garden's DNS record, the tunnel (after its connections; unsetting `CF_TUNNEL_ID` and `CLOUDFLARE_TUNNEL_TOKEN`), the garden's Access application, Lettuce's, and the policy. Each id is unset only when its `DELETE` succeeds; the others stay in `stack.env`, so `remove` can be retried. `--purge` deletes the data only when nothing is left in Cloudflare. Tests assert the exact call order (`test_add_creates_everything_in_order`, `test_remove_*`); a new step keeps both orders.

Other commands write `stack.env` too: `up` (VAPID keys, the executor token, `GARDEN_PROFILES`), `users` (`ALLOWED_USERS`, after the Access policy), `profiles` (`COMPOSE_PROFILES`, together with `config.env` `PROFILES`), and `mcp`, `add` and `up` (the MCP groups' markers). `init` writes `config.env`: the zone, account, Zero Trust team and Google login method ids.

## Build and test
No build step. Before changing the CLI:
```bash
tests/run.sh                 # all offline tests (docker, curl, dig, gh, secret-tool are stubbed)
tests/run.sh <pattern>       # e.g. tests/run.sh test_add_
shellcheck -x lettucectl lib/*.sh scripts/*.sh pipelock/entrypoint.sh tests/run.sh tests/run-sidecar.sh tests/run-pipelock.sh tests/stubs/* doc-tools/bin/* doc-tools/lib/launch.sh code-server/editor/bin/* code-server/editor/entrypoint.sh code-server/editor/tests/test.sh
node --test tests/mcp-servers.test.mjs   # mcp-servers.mjs against temp settings files
```
The curl stub fails on unknown routes, so a new Cloudflare call needs a route in `tests/stubs/curl`. "Tests" in [AGENTS.md](../AGENTS.md) describes the stubs and helpers.

## Upstream seams
- `mcp-servers.mjs`: the shape of the shared MCP list (`/root/.letta/mcp-home/.letta/settings.json`, one `agent-local-mcp-global` entry in `agents` whose `mcpServers` every `letta mcp` call reads), which the BFF renders whole and reads back before every change.
- `common.sh`: `google_redirect_uri` assumes the BFF route `/api/google/oauth/callback`.
- `check_stack` reads upstream's service names and volumes from the rendered config (`app-server`, `bff`, `channel-gateway`, `google-mcp`, `google-creds`).
