# pipelock

The image of the per-stack egress proxy, [pipelock](https://github.com/pipelab-org/pipelock), and its config. With the `garden-pipelock` profile each stack runs it as the service `garden-pipelock`, and the agents' tools in the app-server, SearXNG and the DuckDuckGo reader send their web traffic through it. It checks destinations, reads pages fetched through its `/fetch` endpoint, and signs a receipt for every decision. It is advisory, not a boundary: anything that ignores or unsets `HTTPS_PROXY` goes around it. For users and admins (what it does and does not do, running it, reading the receipts): [docs/egress-firewall.md](../docs/egress-firewall.md).

![What passes pipelock, and what does not](../docs/diagrams/egress-firewall-architecture.svg)

An interactive version, [egress-firewall-architecture.html](../docs/diagrams/egress-firewall-architecture.html), opens in a browser from a clone of this repo.

## Files
| File | What it does |
|---|---|
| `Dockerfile` | Builds pipelock from source at a pinned release (`PIPELOCK_VERSION`, with the tag's commit checked) without the `enterprise` build tag, and fails when anything under `enterprise/` would be linked in, so the binary is Apache-2.0 only. Runs on Alpine as user 65532, which owns `/data`. Health check: `pipelock healthcheck`. |
| `entrypoint.sh` | Creates the receipt signing key on `/data/keys` on first start and never replaces it, rewrites its public half for `verify-receipt`, then runs `pipelock run` on `0.0.0.0:8888`. |
| `pipelock.yaml` | The policy, baked into the image and the same for every stack: balanced mode, the shipped blocklist, 1200 requests per minute per site (model calls included), prompt injection in `/fetch` pages blocked instead of logged, the forward proxy on (no TLS interception, 30 minute idle timeout for long LLM calls), metrics only on `127.0.0.1`, and the signed flight recorder in `/data/evidence`. |

## How it plugs in
- **Service** (`compose/pipelock.yml`): `garden-pipelock`, image `lettuce-pipelock:local` built by `up` from this directory, behind the `garden-pipelock` profile. Read-only root, `/tmp` on tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, memory and process limits. It reads only `TZ`. It is on the stack's default network and publishes no port; the tunnel never routes to it.
- **Volume:** `pipelock-data` (`lettuce-<name>_pipelock-data`) holds the signing key and the receipts. It outlives the profile; `remove` deletes it with the stack (`compose down -v`).
- **Proxy settings** (`compose/pipelock-proxy.yml`): merged by the `compose` wrapper (`lib/common.sh`) only with the profile. It sets `HTTPS_PROXY`, `HTTP_PROXY` and their lowercase forms to `http://garden-pipelock:8888` on `app-server`, `ddg-mcp` and `searxng`, `NODE_USE_ENV_PROXY=1` on the app-server, and a hand-kept `NO_PROXY` list of the services agents call inside the stack (`garden-vault`, `garden-gmail-triggers`, `garden`, the search sidecars, `google-mcp`, `gotenberg`, localhost, `host.docker.internal`). A new service agents call inside the stack goes in that list, or pipelock refuses its private address. `compose_env` appends `PIPELOCK_DIRECT` to it: `config.env`'s list for every stack, then the stack's own, each entry a host name or IP address with an optional port, or a `.domain` suffix (`pipelock_direct` refuses anything else), for hosts such as a model server on the LAN.
- **`/fetch`:** the same listener serves `http://garden-pipelock:8888/fetch?url=...`. The `fetching-web-pages` skill ([agent-skills/](../agent-skills/README.md)) tells agents to read pages through it; `lettucectl skills` installs it.
- **lettucectl:**
  - `check_stack` refuses, whatever the profiles, a service other than `garden-pipelock` mounting `pipelock-data`, or a `volumes_from` that reaches it. With the profile: a `garden-pipelock` that is missing, not hardened or with a host mount (`check_sidecar`), and an app-server whose proxy variables do not point at it or whose `NO_PROXY` or `no_proxy` lacks `garden-vault`, `garden-gmail-triggers` or `garden` (`check_pipelock`): pipelock must never see the vault's values or the executor's token. Every `check_stack` rule: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).
  - `profiles` turns it on and off for every stack; `stale_services` removes the container when the profile goes.
  - `receipts` verifies the receipts and writes HTML reports (`pipelock_setup`, `pipelock_run`): a one-off container of the same image with no network, no capabilities and the volume read-only.
  - `backup` copies the volume, read-only, whenever it exists.

## Rules

### Pipelock is advisory, and docs must say so
The stack network keeps its direct route out. The BFF, the vault, google-mcp and Gmail triggers connect directly, and any agent can unset the variable. letta-code follows it, model provider calls included, since its Node does (see below). There is no TLS interception, so for HTTPS pipelock sees only the host name. It checks destinations (the blocklist, private addresses, data hidden in host names, rates) and signs a receipt per decision. It scans content only for pages fetched through `/fetch`, where `pipelock.yaml` turns prompt injection from balanced mode's log-only warning into a block (`tests/run-pipelock.sh` checks it). Docs never claim more ([docs/egress-firewall.md](../docs/egress-firewall.md#what-it-does-not-do)). Making it a boundary needs an internal network, and a way out for the services that connect directly by design.

### `NO_PROXY` lists every service called inside the stack
`NO_PROXY` in `compose/pipelock-proxy.yml` is a hand-kept list. A service that agents or the executor call inside the stack (a new garden sidecar, a new upstream service) goes in it, or pipelock refuses its private address. `check_pipelock` requires `garden-vault`, `garden-gmail-triggers` and `garden` in it, so pipelock never sees the vault's values or the executor's token.

### Its key and receipts stay on its volume
The signing key and the receipts live only on `pipelock-data`. The entrypoint generates the key once, and lettucectl never rotates it. Nothing else mounts the volume but `backup`'s and `receipts`' one-off containers, read-only.

### Licence
The image is built without the `enterprise` build tag, and the build fails when anything under `enterprise/` would be linked in, so the binary stays Apache-2.0 only.

## Build and test
```bash
tests/run-pipelock.sh       # the built image and its config against a real pipelock (Docker, no internet)
tests/run.sh pipelock       # the CLI's checks and wiring (stubbed docker)
tests/run.sh receipts
shellcheck -x pipelock/entrypoint.sh
```
`tests/run-pipelock.sh` runs the image with this `pipelock.yaml` plus a `trusted_domains` entry on an internal Docker network, and checks that a clean page passes, a page with prompt injection and a private address get a 403, the injection's receipt is a signed block, and the chain verifies. `up` rebuilds the image after a change to this directory.

## Upstream seams
Listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams), under `compose/pipelock-proxy.yml`:
- letta-code runs on the Node of its own image, which follows `HTTPS_PROXY` with `NODE_USE_ENV_PROXY=1` from 22.21 (22.23 since Lettuce v0.42.3). So its own traffic goes through pipelock: its calls to the model provider (a model server on a private address other than `host.docker.internal` is refused) and to MCP servers outside the stack. On every `lettucectl upgrade`, check `node --version` in the app-server, and that `NO_PROXY` still covers what letta-code calls inside the stack. Node also warns on stderr that its proxy agent is experimental, in every process that uses it, agents' tools included. That is left alone: hiding it needs `NODE_OPTIONS`, which upstream's image sets and an overlay may not redefine. The executor's cron line unsets the variable, since everything it calls is inside the stack.
- searxng documents only `outgoing.proxies`, and follows the proxy variables only because its HTTP client (curl_cffi) defaults to `trust_env`; ddg-mcp's clients (httpx, curl_cffi) do too.
