# compose

The Compose overlays lettucectl merges on top of Lettuce's own `upstream/docker/compose.yml` for every stack, plus the host's one shared service. They have no profile of their own: each optional overlay adds its services behind a `garden-*` profile, and two are merged only when their profile is on. Nobody passes them to `docker compose` by hand: lettucectl's `compose` wrapper does, for `up` (which `add`, `users`, `restart`, `profiles` and `upgrade` run too) and for `lettucectl compose <name> ...`. The day-to-day view, including which feature each profile turns on, is in [docs/operations.md](../docs/operations.md#optional-features).

![What lettucectl up does with these files for one stack](../docs/diagrams/compose-up-workflow.svg)

An interactive version, [compose-up-workflow.html](../docs/diagrams/compose-up-workflow.html), opens in a browser from a clone of this repo.

## Files
Merged in this order by the `compose` wrapper (`compose_env` in [lib/common.sh](../lib/common.sh)):

| # | File | When | What it does |
|---|---|---|---|
| 1 | `upstream/docker/compose.yml` | always | Lettuce itself (not ours, not in this folder) |
| 2 | `hardening.yml` | always | `no-new-privileges` and a memory cap on every upstream service; `cap_drop: [NET_RAW]` on app-server, bff and channel-gateway (they share one network namespace); a pids cap on the app-server; `DISABLE_AUTOUPDATER=1` on app-server and channel-gateway |
| 3 | `multi.yml` | always | `ports: !reset []` on the app-server: nothing is published, so stacks never collide and cloudflared is the only way in (needs Compose >= 2.24) |
| 4 | `vault.yml` | `garden-vault` | the `garden-vault` service and its `vault-data` volume ([docs/vault.md](../docs/vault.md)) |
| 5 | `gmail-triggers.yml` | `garden-gmail-triggers` | the `garden-gmail-triggers` service, one host path (`$LETTA_STATE_DIR/garden/gmail-triggers:/data`) and upstream's Google volumes read-only ([docs/gmail-triggers.md](../docs/gmail-triggers.md)) |
| 6 | `pipelock.yml` | `garden-pipelock` | the `garden-pipelock` service and its `pipelock-data` volume ([docs/egress-firewall.md](../docs/egress-firewall.md)) |
| 7 | `code-server.yml` | `garden-code-server` | the gate `garden-code-server`, the editor `garden-code-server-editor` and the shell `garden-code-server-exec`; the networks `code-exec` (internal) and `code-egress`; the volumes `code-server-data`, `code-exec-keys` and `code-server-sock` ([docs/code-server.md](../docs/code-server.md)) |
| 8 | `garden.yml` | any of `garden-vault`, `garden-gmail-triggers`, `garden-code-server` | the `garden` service: the garden host's landing page and the executor API ([garden/](../garden/)) |
| 9 | `doc-tools.yml` | file merged only with `garden-doc-tools` | adds to the app-server a read-only mount of the tools' volume at `/opt/lettuce-doc-tools` and the stack's internal network to Gotenberg; both are `external` ([docs/doc-tools.md](../docs/doc-tools.md)) |
| 10 | `pipelock-proxy.yml` | file merged only with `garden-pipelock`, always last | sets `HTTPS_PROXY`, `HTTP_PROXY`, their lowercase forms and `NO_PROXY`/`no_proxy` on app-server, ddg-mcp and searxng, plus `NODE_USE_ENV_PROXY=1` on the app-server |
| - | `shared/gotenberg.yml` | `garden-doc-tools`, once per host | not a stack overlay: the Compose project `lettuce_shared`, one hardened Gotenberg for every stack, run by `compose_shared` |

Files 4 to 8 are always merged, but every service in them sits behind a profile, so with the profile off they add nothing to the running stack. Their volumes outlive the profile, so `check_stack` holds who may mount `vault-data`, `pipelock-data` and the code editor's volumes whatever the profiles.

Relative paths in every overlay resolve against `--project-directory`, which is `upstream/docker`, so build contexts read `../../<folder>`. The garden images take `garden-common` as an `additional_contexts` entry; the shell's image takes the app-server's image as `base: service:app-server`.

## The rule: overlays restrict, never redefine
An overlay may drop ports or capabilities, set `read_only`, cap resources or add a new service of ours. It does not replace an upstream service's image, entrypoint, command or internal environment (see "Relationship to upstream" in [AGENTS.md](../AGENTS.md)). `hardening.yml` and `multi.yml` only restrict.

There are two written exceptions, both merged only with their profile:
- `pipelock-proxy.yml` adds the standard proxy variables to three upstream services. Upstream sets none of them, and they change where traffic goes, not what the services do. It is merged after every other file, and only with the profile, because a profile cannot gate an environment variable; the `garden-pipelock` service itself stays in `pipelock.yml` so `stale_services` can remove it when the profile goes.
- `doc-tools.yml` adds a mount and a network to the app-server, since stdio MCP servers can only run inside it. It lists `default` too: naming any network drops the implicit one. A profile cannot gate a mount, hence the separate file.

Any other exception needs a written reason in AGENTS.md first.

## How it plugs in
- **The wrapper.** Every Compose call goes through `compose NAME ...` (or `compose_env ENV_FILE NAME ...`), which runs `docker compose` under `env -i`, so the caller's shell cannot override `stack.env`, with `-p lettuce-<name>`, `--project-directory upstream/docker`, the files above and `--env-file stacks/<name>/stack.env`. With `garden-doc-tools` it also passes `DOC_TOOLS_VOLUME` and `DOC_TOOLS_NETWORK`, which lettucectl computes and never writes to `stack.env`. `compose_shared` does the same for `shared/gotenberg.yml` with `-p lettuce_shared` and `--env-file /dev/null`; no stack name can contain an underscore, so it never matches a stack's project.
- **Variables.** The overlays read from `stack.env`: `LETTA_STATE_DIR`, `TZ`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_ISSUER`, `CF_GARDEN_AUD` (passed to every garden service as its `CF_ACCESS_AUD`), `GARDEN_ORIGIN` (as their `PUBLIC_ORIGIN`), `PUBLIC_ORIGIN` (the garden's `LETTUCE_ORIGIN`), `ALLOWED_USERS`, `GARDEN_EXECUTOR_TOKEN`, `GARDEN_PROFILES` (the `garden` service only), `VAULT_VAPID_*` and `PUSH_VAPID_CONTACT_EMAIL`.
- **What `up` runs.** For each stack, `up_stack` makes sure of the VAPID keys and executor token and syncs `GARDEN_PROFILES`, then `check_stack` renders the merged config (`compose NAME config --format json`) and dies on the first unsafe setting. With `garden-doc-tools` it fills the tools' volume and creates the stack's network, then runs `compose NAME up -d --build --remove-orphans`. After that, `sync_executor` installs or removes the garden executor in the app-server and `drop_mcp_servers` removes the MCP entries of profiles that are off. Once every stack is done, `sync_doc_tools` starts or stops the shared Gotenberg and attaches it to each stack's network. Failures in those last three steps are collected, and `up` fails at the end.
- **Tunnel routes.** The overlays publish nothing. cloudflared reaches `app-server:8080` and, on the garden host, `garden-vault:8000`, `garden-gmail-triggers:8000`, `garden-code-server:8000` and `garden:8000` over the stack network (`PROFILE_ROUTES` in [lib/cloudflare.sh](../lib/cloudflare.sh)). A new garden sidecar needs a route there and its profile in `garden.yml`'s list.
- **Checks.** `check_stack` holds the overlays to what they set: no published port; `DISABLE_AUTOUPDATER=1`; no `NET_RAW` in the app-server's namespace; each sidecar hardened (`check_hardened`, `check_sidecar`) with only the mounts and networks its overlay gives it; volumes mounted only by their owner (`volume_users`); every garden service on the garden's own Access audience and origin (`check_garden_audience`); the proxy variables and `NO_PROXY` (`check_pipelock`); the doc-tools mount and network (`check_doc_tools`); and, before Gotenberg starts, its hardening and deny flags (`check_gotenberg`). The full list is in "Key invariants" in [AGENTS.md](../AGENTS.md). An overlay change that one of these refuses needs the check, and its tests, changed with it; new safety rules go in `check_stack` and in that list.

## Build and test
The services' images are built by `lettucectl up` (`--build`); the overlays have no build step of their own. Before changing an overlay:
```bash
tests/run.sh                 # all offline tests; the check_* rules are test_check_*
tests/run.sh test_compose_   # the wrapper's file order and env -i
shellcheck -x lettucectl lib/*.sh scripts/*.sh pipelock/entrypoint.sh tests/run.sh tests/run-sidecar.sh tests/run-pipelock.sh tests/stubs/* doc-tools/bin/* doc-tools/lib/launch.sh code-server/editor/bin/* code-server/editor/entrypoint.sh code-server/editor/tests/test.sh
```
A change to a sidecar's service also runs that sidecar's `tests/run-sidecar.sh <dir>`. `scripts/doc-tools-e2e.sh` and `scripts/code-server-e2e.sh` exercise `doc-tools.yml`, `shared/gotenberg.yml` and `code-server.yml` against the real images.

## Upstream seams
- `upstream/docker/compose.yml`: its service names (`app-server`, `bff`, `cloudflared`, `searxng`, `ddg-mcp`, `google-mcp`, `channel-gateway`), its volumes `google-creds` and `google-policy`, and the BFF's and channel-gateway's `network_mode: service:app-server`. `hardening.yml`, `multi.yml` and the proxy and doc-tools overlays address services by those names.
- `pipelock-proxy.yml`: who follows `HTTPS_PROXY`. letta-code's Node 22.19 ignores it; from Node 22.21 it follows it. On every `lettucectl upgrade`, check `node --version` in the app-server. `NO_PROXY` is a hand-kept list: a new service that agents call inside the stack goes in it, or pipelock refuses its private address. searxng follows the variables only because its HTTP client (curl_cffi) does by default.
- `code-server.yml`: the shell's image is built `FROM` the app-server's image, so it depends on that image being Debian with `apt`.
