# lib

The code behind `lettucectl`, the Bash CLI at the repo root. Admins run it from its published image, through a wrapper ([release/](../release/README.md)); developers run it from a clone. [DESIGN.md](DESIGN.md) is the design record of that split. `lettucectl` holds argument handling and one `cmd_*` function per subcommand, dispatched from `main`; it sources the two shell libraries here, and streams the Node script into a stack's app-server when it needs to change Settings -> MCP. No profile: the CLI runs on the host for every stack. The operator's guides are [docs/operations.md](../docs/operations.md) and [docs/cloudflare-setup.md](../docs/cloudflare-setup.md).

![lettucectl, its libraries and what they reach](../docs/diagrams/lettucectl-architecture.svg)

An interactive version, [lettucectl-architecture.html](../docs/diagrams/lettucectl-architecture.html), opens in a browser from a clone of this repo.

## Files
| File | What it does |
|---|---|
| `common.sh` | Messages (`info`, `warn`, `die`); the env-file helpers (`env_get`, `env_set`, `env_unset`, `cfg`); release mode (`release_mode`, `image_name`); paths and the data directory (`resolve_data_dir`); Lettuce's source and images (`fetch_upstream`, `upstream_image_ns`); the same-path check (`check_mounts`, `require_mounted`); `stacks.conf` (`conf_entries`, `conf_set`, `conf_unset`); validation of names, emails and profiles; the `compose`, `compose_env`, `compose_shared` and `host_docker` wrappers; doc-tools names and tags; VAPID key and executor token generation; `stale_services`; and `check_stack` with its `check_*` helpers |
| `cloudflare.sh` | Cloudflare API v4: `cf_api` (curl + jq) and one `cf_*` helper per resource (Zero Trust team, Google login method, DNS lookup, Access policy and applications, tunnel, tunnel token, tunnel ingress, DNS CNAME); `PROFILE_ROUTES`, the garden host's routes per profile |
| `mcp-servers.mjs` | Adds or removes one group of lettucectl's servers (`doc-tools` or `vault`) in the stack's shared MCP list. Node, no dependencies; never run on the host |
| `DESIGN.md` | Design record: installing without a clone (a published `lettucectl` image, published garden images, the data directory, `stacks.conf` and `apply`) |

`lettucectl` sources `common.sh`, then `cloudflare.sh`; both expect `LETTUCE_HOME` (the code) to be set and define functions only. `main` then sets `LETTUCE_DATA` (`resolve_data_dir`) and, in release mode, runs `check_mounts` before any command.

## How it plugs in
- **`check_stack` gates every `up`.** It reads `stack.env`, renders the merged config and dies on the first unsafe setting. What it refuses, and which profile turns each check on: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).
- **Cloudflare.** `cf_api METHOD PATH [BODY]` prints `.result`, or the API's errors and returns 1. `init`, `add`, `users`, `remove` and `profiles` call it; `up` never does.
- **The MCP list.** `mcp_servers` in `lettucectl` runs `compose NAME exec -T app-server node --input-type=module - add|remove FILE GROUP` with `mcp-servers.mjs` on stdin (the rules below).
- **Other writers of `stack.env`:** `up` (VAPID keys, the executor token, `GARDEN_PROFILES`), `users` (`ALLOWED_USERS`, after the Access policy), `profiles` (`COMPOSE_PROFILES`, together with `config.env` `PROFILES`), and `mcp`, `add` and `up` (the MCP groups' markers). `init` writes `config.env`: the zone, account, Zero Trust team and Google login method ids, and `LETTA_IMAGE_NS` (as `upgrade` does). `add`, `users` and `remove` write `stacks.conf`.

## Rules

### Code and data are apart
`LETTUCE_HOME` is the code: a clone, or `/opt/lettuce-garden` in the image. `LETTUCE_DATA` is the instance: `config.env`, `stacks.conf`, `.secrets/`, `upstream/<tag>/` and `stacks/`. Every path to the instance goes through `config_file`, `token_file`, `stack_dir`, `stack_env`, `stacks_conf` and `upstream_dir`; every path to the code through `LETTUCE_HOME`. In release mode the wrapper passes `LETTUCE_DATA`; from a clone it defaults to the clone's `data/`, so no clone or worktree reaches the real stacks by default, and a clone refuses a data directory a release runs (`bin/lettucectl` in it). `backup` and `receipts` refuse a destination inside either.

### Release mode is a file, not a setting
`$LETTUCE_HOME/RELEASE` exists only in the published image (`publish.yml` writes it), so an admin cannot point a clone at published images by mistake. In release mode the overlays get each published image's name from it (`GARDEN_IMAGE_*`, through `compose_env`), `backup` and `receipts` use its names, and `check_images` refuses a garden service that runs any other. From a clone, the same variables are unset and the overlays name the local builds, `lettuce-<image>:local`.

### `up` builds only what is built on this host
`build_local_images` builds upstream's services when `config.env`'s `LETTA_IMAGE_NS` is empty (Lettuce publishes no images for this host), then the garden's: from a clone all of them, in release mode only the code editor's shell. Two calls, in that order: the shell is built `FROM` the app-server's image by name (`APP_SERVER_IMAGE`, `docker-image://`), never `service:app-server`, which would rebuild a pulled app-server. Then `up -d --no-build --pull missing`: a published image that is missing fails rather than building from a context the image does not carry. Whether `LETTA_IMAGE_NS` is set is looked up by `init` and `upgrade` (`upstream_image_ns`), never by `up`. Only the registry's "manifest unknown" or "denied" means not published; any other failure stops the command and leaves `LETTA_IMAGE_NS` as it was, so an outage never switches every stack to building upstream's images.

### Every host path is the same inside lettucectl's container
Docker reads the paths lettucectl gives it on the host. A path mounted elsewhere in lettucectl's container would make Docker create an empty, root-owned directory in its place, and a stack would start without its data. In release mode `check_mounts` reads the container's own mounts (`docker inspect "$HOSTNAME"`) and refuses any bind mount whose source and destination differ, and a `LETTUCE_DATA` that is not one of them; it fails closed when it cannot find its container. `require_mounted` refuses a `backup` or `receipts` destination that the wrapper did not mount.

### `stacks.conf` is kept in step, and `apply` never removes
`apply` makes the stacks match `stacks.conf` through `add` and `users` (its update, `users_update`, without its `up`), then an `up` for each stack `add` did not just start, so none starts twice, each on its own so one that fails doesn't stop the others, and `skills all`. It never removes a stack and never changes a profile: it reports both, with the command to run. So that it never undoes them, `add` writes the stack's line as it starts (an interrupted add is resumed by `apply`), `users` rewrites it after the Access policy, and `remove` drops it once the name is confirmed (a stack Cloudflare could not fully delete is then reported, not set up again). A write that fails fails the command. An `add` that resumes takes the addresses it is given, updating the Access policy with `ALLOWED_USERS` if the policy exists, so an edit to `stacks.conf` between two tries is kept. `apply` runs each step in a subshell: one stack that fails does not keep the others from being set up and started, and `apply` fails at the end, naming the steps that did not finish. `stacks.conf` is parsed (`conf_entries`), never sourced.

### Env files are parsed, never sourced
`config.env` (global settings plus cached Cloudflare ids) and `stacks/<name>/stack.env` (that stack's secrets and Cloudflare ids) are plain `KEY=value` files that Docker Compose also reads. Sourcing one would run whatever it holds. Read and write them only with `env_get`, `env_set`, `env_unset` and `cfg`. `env_get` reads a key with awk (the last occurrence wins). `env_set` and `env_unset` rewrite the file through a temp file and `mv`, so the change is atomic and the file keeps mode 600. No quotes or inline comments in these files.

### Every Compose call goes through `compose`
Every `docker compose` call for a stack goes through `compose NAME ...`. It runs under `env -i`, so the caller's shell can't override `stack.env`. It passes the project `lettuce-<name>`, `--project-directory upstream/docker`, the overlays in order, plus `compose/doc-tools.yml` and then `compose/pipelock-proxy.yml` when `COMPOSE_PROFILES` has `garden-doc-tools` or `garden-pipelock`, and an explicit `--env-file` ([compose/README.md](../compose/README.md) has the order). With `garden-pipelock` it passes `PIPELOCK_DIRECT` (`config.env`'s list, then the stack's own, checked by `pipelock_direct`), which `pipelock-proxy.yml` appends to `NO_PROXY`. With `garden-doc-tools` it also passes `DOC_TOOLS_VOLUME` (`lettuce-doc-tools-<tag>`, where `doc_tools_tag` hashes `doc-tools/` without its tests and README) and `DOC_TOOLS_NETWORK` (`lettuce-doc-tools-<name>`) through `env -i`. lettucectl computes both; they are never written to `stack.env`. `compose_env ENV_FILE NAME ...` is the same with another env file: `check_stack --profiles` uses it to render a profile list before writing it. Plain `docker` calls go through `host_docker`, also under `env -i`.

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

`cmd_remove` stops the stack (`compose down -v`), removes its doc-tools network, then deletes in reverse: Lettuce's DNS record, the garden's DNS record, the tunnel (after its connections; unsetting `CF_TUNNEL_ID` and `CLOUDFLARE_TUNNEL_TOKEN`), the garden's Access application, Lettuce's, and the policy. Each id is unset only when its `DELETE` succeeds; the others stay in `stack.env`, so `remove` can be retried. `--purge` deletes the data only when nothing is left in Cloudflare. Tests assert the exact call order (`test_add_creates_everything_in_order`, `test_remove_*`): a new step keeps both orders. `up` makes no Cloudflare call.

### The Cloudflare API token never goes in argv
`cf_api` reads the token from `.secrets/cloudflare-api-token` and hands it to curl as a config block on stdin (`--config -`), so it never appears in argv, `ps` or shell history. Request bodies carry no secrets and go in argv. `test_init_never_puts_the_api_token_in_argv` checks it.

### Profiles change everywhere at once
All stacks share one app-server image, whose contents depend on the profiles, so the list is global. `cmd_profiles` first runs `check_stack NAME --profiles NEW` on every stack. That renders the config from a temporary copy of `stack.env` with the new list (through `compose_env`), so every check sees the new list. Only then does it write `config.env` `PROFILES` and every `stack.env` `COMPOSE_PROFILES` together, and run `up` on each stack. An unchanged list skips all of that. It always removes `stale_services`: containers whose profile is off, which Compose leaves running. It compares what exists rather than what changed, so a re-run repairs an interrupted one. Docs never tell operators to edit the lists by hand.

### Access policy and `ALLOWED_USERS` change together
Upstream requires the two to stay in sync, so `cmd_users` changes both.

### lettucectl owns the stack's Access applications' settings
Each stack has two Access applications, Lettuce's and the garden's (`lettuce-<name>-garden`, for `GARDEN_HOSTNAME`), sharing the stack's one policy, so `users` changes both. `cf_app_body` is the one definition of their settings. `add` posts it (`cf_app_create`), and `route_stack` puts it back on both on every `profiles` run (`cf_app_update`). A `PUT` replaces the application's settings, so a change made in the dashboard does not survive. Their cookie is HttpOnly, so no script on either origin (the code editor's tab included) can read the Access token and hand it to an agent, and SameSite=Lax. `check` does not read them back: it runs offline.

### Tunnel routes follow the profiles
- **Lettuce's host goes only to the app-server.** Every garden page is on the garden host, `GARDEN_HOSTNAME`.
- **The garden host's name** comes from `pick_garden_hostname`: `<label>-garden.<domain>`, `<label>` being the first label of `LETTUCE_HOSTNAME`. A stack on its `HOSTNAME_FALLBACK` name therefore gets `lettuce-<name>-garden`. When that name is taken, it uses the first free of `<label>-garden-2` to `-9`. `add` refuses new stack names ending in `-garden`.
- **The rules, in order.** `cf_tunnel_configure` puts the garden host's rules first:
  1. each route of `PROFILE_ROUTES` (`cloudflare.sh`) whose profile the stack has: `^/vault(/|$)` -> `garden-vault:8000`, `^/gmail-triggers(/|$)` -> `garden-gmail-triggers:8000`, `^/code-server(/|$)` -> `garden-code-server:8000`;
  2. with any of them on, the landing page and its assets (`GARDEN_LANDING_PATH`, `^/(assets/[^/]+)?$`) -> `garden:8000`. `garden` serves every asset under `/assets/`, so a new asset needs no route;
  3. a 404 for the rest of the garden host, so `/executor/v1/` and `/healthz` are never routed;
  4. then Lettuce's host -> `app-server:8080`.
- **`GARDEN_PROFILES`.** `up` writes it in `stack.env` (`sync_garden_profiles`): the profiles of `PROFILE_ROUTES` that are on, in its order. Only `garden` gets it. Never pass `COMPOSE_PROFILES` or `GARDEN_PROFILES` to the other sidecars: any profile change would recreate them, and recreating the vault locks it.
- `add` sets the routes, and `profiles` re-applies every stack's routes on every run, before `up`. A new garden sidecar adds a route: [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-garden-sidecar).

### Google redirect URIs are manual
Google has no API for an OAuth client's redirect URIs. So with the `google` profile, `add` and `profiles` print each stack's URI, and `remove` reminds the operator to delete it (`google_redirect_uri`, `google_reminder`).

### VAPID keys are generated once per stack
`ensure_push_keys` generates them on the first `up` and never rotates them: a new keypair invalidates every browser's push subscription. The BFF (`PUSH_VAPID_*`) and the vault (`VAULT_VAPID_*`) each have their own pair.

### Upstream's version moves only through `upgrade`
`lettucectl upgrade <tag>` is the only way to move `LETTUCE_REF` (and `self-upgrade`, through it, to the tag a release was tested with, never back): Lettuce tags pin the letta-code server version the UI was compiled against. `fetch_upstream` downloads GitHub's archive of the tag into `upstream/<tag>.partial/` and renames it when complete, so `upstream/<tag>/` is always whole; `upgrade` removes the other tags once every stack is up. What to check on every upgrade: [CONTRIBUTING.md](../CONTRIBUTING.md#upgrading-lettuce_ref).

### `restart`, never a single-service restart
The BFF shares the app-server's network namespace and dies when the app-server is recreated on its own, so `up` and `restart` act on the whole stack. The one exception is the BFF alone (`refresh_mcp_skill`), which leaves that namespace as it is.

### `toolbox` keeps the operator's credentials out of the stack
`toolbox` is a `docker run` of the stack's app-server image with one agent's workspace, never a `compose exec`: agents share the app-server as root, and the operator's tokens must not land where they can read them.
- Tokens go through Docker's environment as `-e NAME`, never argv.
- The image's inherited `com.docker.compose.*` labels are emptied, or Compose and `stale_services` would count the toolbox as a stack container.
- `~/.config/lettucectl/toolbox` is parsed, never sourced.
- The agents' tools (`state/tools`) are mounted at `/opt/agent-tools` only with `--tools`, read-only unless `--tools=rw`. A missing directory is refused rather than created by Docker.
- In release mode the operator's home is `LETTUCECTL_HOST_HOME` (the wrapper mounts it read-only at the same path), and what only the host's commands can give (`gh auth token`, `secret-tool`, the toolbox file's `secret` lines) the wrapper runs on the host and passes as `LETTUCECTL_*` variables, by name.

### `mcp` registers the document tools, the vault and Outlook once per stack, and only those
There is no supported way to set Settings -> MCP across stacks, so `lettucectl mcp` is the one write into Lettuce's MCP list. It owns three kinds of entries and touches nothing else:
- **`doc-tools`** (profile `garden-doc-tools`): entries whose command is under `/opt/lettuce-doc-tools/`. It adds `office` and `pdf` when missing. A same-named server with another command is the person's and stays.
- **`vault`** (profile `garden-vault`): HTTP entries whose URL is exactly `http://garden-vault:8000/mcp`, under any name. It adds `{"name": "vault", "transport": "http", "url": "http://garden-vault:8000/mcp"}` only when none is there, and nothing when another server is named `vault` (the person's). Registering the vault gives agents nothing new: they reach that URL over the stack network anyway, and approval stays in the vault.
- **`outlook`** (profile `garden-outlook`): like the vault, HTTP entries whose URL is exactly `http://garden-outlook:8000/mcp`, under any name. It adds `{"name": "outlook", "transport": "http", "url": "http://garden-outlook:8000/mcp"}` only when none is there, and nothing when another server is named `outlook`. It gives agents nothing new either: the policy is enforced in the sidecar ([outlook/README.md](../outlook/README.md#rules)).

How it writes:
- `mcp-servers.mjs` is streamed into the app-server (the script on stdin) and touches only its group's entries.
- It refuses a file of any other shape.
- It writes a temp file, renamed only when the original is unchanged, and reads the file back after 2 s to catch an overwrite by the BFF (up to 4 tries).
- On a new stack it waits up to 90 s for the BFF to create the file.

When it runs:
- Success sets the group's marker in `stack.env` (`DOC_TOOLS_MCP_REGISTERED=1`, `VAULT_MCP_REGISTERED=1`, `OUTLOOK_MCP_REGISTERED=1`; `MCP_GROUPS`, `MCP_PROFILE` and `MCP_MARKER` in `lettucectl`). `mcp` registers every group whose profile is on and skips a marked group unless `--force`, so a server the person removed stays removed. It refuses only when all three profiles are off.
- `add` registers a new stack's groups after `sync_doc_tools` (the document tools only when that succeeded). `profiles` and `up` never register.
- With a group's profile off, `up` and every `profiles` run remove that group's entries in each stack marked for it, and clear the mark. A stdio server that cannot start, or an HTTP server that does not answer, breaks every unfiltered `letta mcp tools` call. A failure keeps the mark, so the next run retries.
- Whenever a stack's list changed, `refresh_mcp_skill` restarts its BFF alone (`compose NAME restart --no-deps bff`), once per stack per command, so the `mcp-servers` description agents see names the new list. Its `stop_grace_period` lets a turn finish. A failed restart only warns.
- `add` and `profiles` say, for each group whose profile is on, whether each stack is registered, or print the entries and `lettucectl mcp <name>` (`mcp_reminder`).

## Build and test
No build step. Before changing the CLI, run `tests/run.sh` (or `tests/run.sh <pattern>`, e.g. `tests/run.sh test_add_`), shellcheck and, for `mcp-servers.mjs`, `node --test tests/mcp-servers.test.mjs` (for `scripts/check-doc-links.mjs`, `node --test tests/check-doc-links.test.mjs`); the exact commands are in [CONTRIBUTING.md](../CONTRIBUTING.md#building-and-testing). The curl stub fails on unknown routes, so a new Cloudflare call needs a route in `tests/stubs/curl`. [tests/README.md](../tests/README.md) describes the stubs and helpers.

## Upstream seams
- `mcp-servers.mjs`: the shape of the shared MCP list (`/root/.letta/mcp-home/.letta/settings.json`, one `agent-local-mcp-global` entry in `agents` whose `mcpServers` every `letta mcp` call reads), which the BFF renders whole, reads back from disk before every change, and shows as it is (so a person can see and remove our entries). A stdio entry with no args or env passes its validation, and so does the vault's `http` entry of only `name`, `transport` and `url`.
- `common.sh`: `google_redirect_uri` assumes the BFF route `/api/google/oauth/callback`.
- `lettucectl`: `cmd_shell` assumes letta-code's memory path, `$LETTA_LOCAL_BACKEND_DIR/memfs/$AGENT_ID/memory`; `toolbox` the app-server image layout, `/opt/claude-code/bin`; `find_agent` the JSON shape of `letta agents list` (`.items[]`).
- `check_stack` reads upstream's service names and volumes from the rendered config (`app-server`, `bff`, `channel-gateway`, `google-mcp`, `google-creds`).
- `app_server_image` assumes upstream names the app-server's image `${LETTA_IMAGE_NS}lettuce-app-server:<its release tag>`; `check_images` compares it with the render whenever the code editor's shell is built from it. `upstream_image_ns` assumes upstream publishes to `ghcr.io/<owner>/` and that `LETTA_IMAGE_NS` is its switch.
- `fetch_upstream` assumes GitHub's tag archives (`/archive/refs/tags/<tag>.tar.gz`, one top-level directory).
