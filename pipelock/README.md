# pipelock

The image of the per-stack egress proxy, [pipelock](https://github.com/pipelab-org/pipelock), and its config. With the `garden-pipelock` profile each stack runs it as the service `garden-pipelock`, and the agents' tools in the app-server, SearXNG and the DuckDuckGo reader send their web traffic through it. It checks destinations, reads pages fetched through its `/fetch` endpoint, and signs a receipt for every decision. It is advisory, not a boundary: anything that ignores or unsets `HTTPS_PROXY` goes around it. [docs/egress-firewall.md](../docs/egress-firewall.md) is the guide, with what it does and does not do.

![What passes pipelock, and what does not](../docs/diagrams/egress-firewall-architecture.svg)

An interactive version, [egress-firewall-architecture.html](../docs/diagrams/egress-firewall-architecture.html), opens in a browser from a clone of this repo.

## Files
| File | What it does |
|---|---|
| `Dockerfile` | Builds pipelock from source at a pinned release (`PIPELOCK_VERSION`, with the tag's commit checked) without the `enterprise` build tag, and fails when anything under `enterprise/` would be linked in, so the binary is Apache-2.0 only. Runs on Alpine as user 65532, which owns `/data`. Health check: `pipelock healthcheck`. |
| `entrypoint.sh` | Creates the receipt signing key on `/data/keys` on first start and never replaces it, rewrites its public half for `verify-receipt`, then runs `pipelock run` on `0.0.0.0:8888`. |
| `pipelock.yaml` | The policy, baked into the image and the same for every stack: balanced mode, the shipped blocklist, 600 requests per minute per site, prompt injection in `/fetch` pages blocked instead of logged, the forward proxy on (no TLS interception, 30 minute idle timeout for long LLM calls), metrics only on `127.0.0.1`, and the signed flight recorder in `/data/evidence`. |

## How it plugs in
- **Service** (`compose/pipelock.yml`): `garden-pipelock`, image `lettuce-pipelock:local` built by `up` from this directory, behind the `garden-pipelock` profile. Read-only root, `/tmp` on tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, memory and process limits. It reads only `TZ`. It is on the stack's default network and publishes no port; the tunnel never routes to it.
- **Volume:** `pipelock-data` (`lettuce-<name>_pipelock-data`) holds the signing key and the receipts. It outlives the profile; `remove` deletes it with the stack (`compose down -v`).
- **Proxy settings** (`compose/pipelock-proxy.yml`): merged by the `compose` wrapper (`lib/common.sh`) only with the profile. It sets `HTTPS_PROXY`, `HTTP_PROXY` and their lowercase forms to `http://garden-pipelock:8888` on `app-server`, `ddg-mcp` and `searxng`, `NODE_USE_ENV_PROXY=1` on the app-server, and a hand-kept `NO_PROXY` list of the services agents call inside the stack (`garden-vault`, `garden-gmail-triggers`, `garden`, the search sidecars, `google-mcp`, `gotenberg`, localhost, `host.docker.internal`). A new service agents call inside the stack goes in that list, or pipelock refuses its private address.
- **`/fetch`:** the same listener serves `http://garden-pipelock:8888/fetch?url=...`. The `fetching-web-pages` skill ([agent-skills/](../agent-skills/README.md)) tells agents to read pages through it; `lettucectl skills` installs it.
- **lettucectl:**
  - `check_stack` refuses, whatever the profiles, a service other than `garden-pipelock` mounting `pipelock-data`, or a `volumes_from` that reaches it. With the profile: a `garden-pipelock` that is missing, not hardened or with a host mount (`check_sidecar`), and an app-server whose proxy variables do not point at it or whose `NO_PROXY` lacks `garden-vault`, `garden-gmail-triggers` or `garden` (`check_pipelock`). See [Key invariants](../AGENTS.md#key-invariants).
  - `profiles` turns it on and off for every stack; `stale_services` removes the container when the profile goes.
  - `receipts` verifies the receipts and writes HTML reports (`pipelock_setup`, `pipelock_run`): a one-off container of the same image with no network, no capabilities and the volume read-only.
  - `backup` copies the volume, read-only, whenever it exists.

## Build and test
```bash
tests/run-pipelock.sh       # the built image and its config against a real pipelock (Docker, no internet)
tests/run.sh pipelock       # the CLI's checks and wiring (stubbed docker)
tests/run.sh receipts
shellcheck -x pipelock/entrypoint.sh
```
`tests/run-pipelock.sh` runs the image with this `pipelock.yaml` plus a `trusted_domains` entry on an internal Docker network, and checks that a clean page passes, a page with prompt injection and a private address get a 403, the injection's receipt is a signed block, and the chain verifies. `up` rebuilds the image after a change to this directory.

## Upstream seams
Named in [AGENTS.md](../AGENTS.md) under `compose/pipelock-proxy.yml`:
- letta-code runs on the Node of its own image (22.19 today), which ignores `HTTPS_PROXY`. On every `lettucectl upgrade`, check `node --version` in the app-server: from 22.21 letta-code's own traffic goes through pipelock too, and `NO_PROXY` must still cover what it calls inside the stack.
- searxng follows the proxy variables only because its HTTP client (curl_cffi) trusts the environment by default; ddg-mcp's clients (httpx, curl_cffi) do too.
