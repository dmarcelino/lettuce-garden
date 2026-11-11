# outlook

The Outlook sidecar: one Outlook mailbox (mail, calendar, tasks, and read-only shared mailboxes) for a stack's agents, under a policy the person sets on a page. It is Python (MCP SDK 2.x, Starlette, httpx, cryptography), runs as the `garden-outlook` service behind the `garden-outlook` profile, serves its page at `/outlook/` on the stack's garden host, and gives agents MCP tools at `http://garden-outlook:8000/mcp`. It calls Microsoft Graph with a token it gets by redeeming the browser's first-party refresh token (FOCI), so it needs no app registration. For users and admins (using it, running it, the security model): [docs/outlook.md](../docs/outlook.md); the design record is [DESIGN.md](DESIGN.md).

![The Outlook sidecar's containers, trust boundaries and requests](../docs/diagrams/outlook-architecture.svg)

An interactive version, [outlook-architecture.html](../docs/diagrams/outlook-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `Dockerfile` | `base` installs `garden-common` from its build context, then this package; `test` adds pytest and `tests/`; the final stage runs as the fixed non-root user 10001, which owns `/data` |
| `pyproject.toml` | The package and its pinned dependencies |
| `src/garden_outlook/__main__.py` | Entry point: reads the settings, builds the app, runs uvicorn on port 8000 |
| `src/garden_outlook/config.py` | `PREFIX` (`/outlook`), Graph's base URL and scope, the limits (`HTTP_TIMEOUT_SECONDS`, `MAX_BODY_CHARS`, `MAX_PAGE`, ...) and `Settings` from the environment |
| `src/garden_outlook/app.py` | `/mcp` (the agents' endpoint), the page under `/outlook/` (connect, disconnect, policy), `/healthz` |
| `src/garden_outlook/levels.py` | The policy model: services (`mail`, `calendar`, `tasks`) and their cumulative levels, `Policy.permits`, `calendar_allows_attendees` (what makes `self-events` refuse invitations), shared-mailbox membership, a lenient `parse_policy` and a strict `apply_update` |
| `src/garden_outlook/recipients.py` | The allowed-recipients guard: a bare address or `@domain`, exact or domain match, the account's own address always allowed, empty meaning unrestricted; it fails closed |
| `src/garden_outlook/tokens.py` | The connected session: `session_from_payload` validates the handover; `SessionStore` seals it with `crypto.py` and holds it in memory; `apply_refresh` updates it in place |
| `src/garden_outlook/crypto.py` | AES-256-GCM, the key derived with scrypt from `OUTLOOK_SECRET` and a per-volume salt kept beside the file |
| `src/garden_outlook/outlook_client.py` | The Microsoft Graph client and the FOCI redemption; `trust_env=False`, so the token never goes through a proxy |
| `src/garden_outlook/service.py` | What the app and the tools share: the session store, the policy (persisted to `/data/policy.json`) and the client |
| `src/garden_outlook/mcp_tools.py` | The tools. Each checks the policy when it runs (`require`), shared-mailbox reads (`shared`), and sends and invitations against the allowed recipients (`check_recipients`). Mail, event and task text carries the untrusted-content note |
| `src/garden_outlook/freebusy.py` | The free/busy gap maths behind `find_free_time` |
| `src/garden_outlook/bookmarklet.py` | The connect bookmarklet, built for the stack's garden origin |
| `src/garden_outlook/logs.py`, `icons.py` | Logging to stdout (scrubs the tokens as a backstop), and the app icons drawn as PNG |
| `src/garden_outlook/templates/`, `static/` | The page (Jinja), its stylesheet, and `app.js`, which reads the bookmarklet's handover from the URL fragment and posts it to `/outlook/connect` |
| `tests/` | pytest: the policy model, the allowed recipients, free/busy, the session and its seal, the client, and the tools through the real app |

## How it plugs in
- **Overlay:** [compose/outlook.yml](../compose/outlook.yml) defines `garden-outlook` (image `${GARDEN_IMAGE_OUTLOOK:-lettuce-outlook:local}`, built from this directory with the `garden-common` build context from a clone, published for a release: `release/images`): read-only root, `/tmp` as tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, 256 MB, no ports, on the stack's default network.
- **Mounts:** only its named volume `outlook-data` at `/data`: the sealed session, its salt and the policy. No host path.
- **Environment:** `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS`, `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`), `OUTLOOK_SECRET`, `TZ`. `ensure_outlook_secret` generates `OUTLOOK_SECRET` once per stack, on every `up` and `profiles` run, whatever the profiles.
- **Tunnel:** the `PROFILE_ROUTES` row in [lib/cloudflare.sh](../lib/cloudflare.sh) sends `^/outlook(/|$)` on the garden host to `http://garden-outlook:8000`. `/mcp` and `/healthz` are outside that prefix, so the tunnel answers them with 404.
- **Requests checked in the app:** the page goes through `garden_common.auth` (Access JWT, Fetch Metadata, Origin and CSRF, COOP; [garden-common](../garden-common/README.md)). `/mcp` accepts only the host `garden-outlook` and refuses any `Origin` (the MCP SDK's transport security), as the vault's does.
- **Settings -> MCP:** `lettucectl mcp` registers `{"name": "outlook", "transport": "http", "url": "http://garden-outlook:8000/mcp"}` (the `outlook` group of `lib/mcp-servers.mjs`, marker `OUTLOOK_MCP_REGISTERED`); its rule is in [lib/README.md](../lib/README.md#mcp-registers-the-document-tools-the-vault-and-outlook-once-per-stack-and-only-those).
- **Pipelock:** `garden-outlook` is in the app-server's `NO_PROXY`, so agents' calls to it never pass pipelock, and the service itself calls Microsoft directly.
- **`check_stack`:** with the profile, `check_sidecar` (hardened, no host mount) and an `OUTLOOK_SECRET` of at least 32 characters; whatever the profiles, `outlook-data` mounted only by `garden-outlook` and no `volumes_from` reaching it; `check_garden_audience`; with pipelock, `NO_PROXY` listing `garden-outlook`. The details are under [Rules](#rules); every `check_stack` rule: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).

## Rules
The guarantees and limits people see are in [docs/outlook.md](../docs/outlook.md#security-model); these are the rules that keep them.

### The policy is enforced in the sidecar, at execution
Agents reach `/mcp` over the stack network, so the tool list is not the control: every tool checks the stored policy when it runs (the level per service, the allowed recipients, `self-events` refusing attendees, shared mailboxes read-only). An agent calling the endpoint directly gains nothing a lower level would not give it. Never add a tool that changes the policy, the allowed recipients or the shared mailboxes, or that returns the token: those are set only on the page, under the garden's own Access application (`check_garden_audience`), which agents cannot forge.

### The token stays in this container
The session (bearer and refresh tokens) is sealed with `OUTLOOK_SECRET`, which lives only in this service's environment, on `outlook-data`, which nothing else mounts. Never log the tokens or write them unsealed, never pass the service a proxy (pipelock must never see the token), and never mount `outlook-data` elsewhere. `OUTLOOK_SECRET` is never rotated by lettucectl; replacing it makes the stored session unreadable, and the person reconnects.

### `backup` does not copy `outlook-data`
A backup should not carry a live mail credential, and reconnecting is cheap. Keep it out of `lettucectl backup`.

### What `check_stack` refuses
With the profile on, `check_stack` refuses:
- a `garden-outlook` service that is missing, not hardened (`check_hardened`) or with a host mount (`check_sidecar`);
- an `OUTLOOK_SECRET` shorter than 32 characters.

Whatever the profiles, `outlook-data` may be mounted only by `garden-outlook`, and no `volumes_from` may name `garden-outlook` or a service that mounts `outlook-data`. With `garden-pipelock`, the app-server's `NO_PROXY` and `no_proxy` must list `garden-outlook` (`check_pipelock`).

### Docs never claim more than is true
Graph is supported; how the token is obtained (FOCI redemption of the browser's first-party refresh token) is not, and a strict tenant can refuse it. The allowed-recipients list guards the account's identity, not the data an agent can read within its level. `outlook_client.py` is the one place that knows Graph's shapes and the redemption; when Microsoft changes either, fix it there (and `bookmarklet.py`, if the web app changes how it caches its session).

## Build and test
From a clone, `lettucectl up` builds the image; a release pulls it. The tests need only Docker:
```bash
tests/run-sidecar.sh outlook            # pytest in the image's test stage
tests/run.sh outlook                    # the CLI's checks, route and MCP group for this profile
node --test tests/mcp-servers.test.mjs  # lib/mcp-servers.mjs, the outlook group included
```
Run them before changing this directory or `garden-common/`.

The tests never use the network: Graph and Microsoft's token endpoint are an `httpx.MockTransport`, Access tokens follow the vault's `make_jwt` pattern, and a canary mail body is asserted to reach the agent but never the logs.

## Upstream seams
These are the Lettuce behaviours Outlook depends on (all of them are listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams)):
- The shared MCP list accepts the `http` entry of only `name`, `transport` and `url`, and `lettucectl mcp` writes it into `/root/.letta/mcp-home/.letta/settings.json`.
- `HTTP_TIMEOUT_SECONDS` stays below the 120 s Lettuce's MCP bridge gives a tool call (see `config.py`).
