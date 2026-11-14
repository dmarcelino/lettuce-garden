# landing

The `landing` service serves the landing page at the root of a stack's lollo host, `https://<name>-lollo.<domain>/`, with a link to each lollo page that is on and, with the code editor, a link to each agent's workspace. This folder also holds the lollo executor, `executor/run.mjs`, which runs in Lettuce's app-server, not in this image. The service has no profile of its own: it is on whenever any lollo page profile is (`lollo-vault`, `lollo-gmail-triggers`, `lollo-news-radar`, `lollo-code-server`). The person's view of the page is in [docs/user-guide.md](../docs/user-guide.md) ("What you get"), and its agent links in [docs/code-server.md](../docs/code-server.md) ("Using it"); the design record is [DESIGN.md](DESIGN.md); the executor's install and protocol are in [executor/README.md](executor/README.md).

![The landing page, the lollo executor and what they talk to](../docs/diagrams/lollo-architecture.svg)

An interactive version, [lollo-architecture.html](../docs/diagrams/lollo-architecture.html), opens in a browser from a clone of this repo.

## Files

| Path | What it does |
|---|---|
| `Dockerfile`, `pyproject.toml` | The service's image: Python 3.12 slim, `lollo-common` from its build context, pinned dependencies, a `test` stage; runs as uid 65534 on port 8000. |
| `src/lollo_landing/config.py` | Settings from the environment: the two origins, `LOLLO_PROFILES`, the executor token (64 hex characters or the service refuses to start); `page_path` (profile `lollo-<page>` serves `/<page>/`). |
| `src/lollo_landing/app.py` | The routes, `LABELS` (what the page calls each lollo page), the agent rows and their links. |
| `src/lollo_landing/agents.py` | `AgentList`: the last agent list, in memory only, and when to ask for a new one (`AgentAsks`). |
| `src/lollo_landing/icons.py` | The page's icon, drawn on first use (no binary files in the repo). |
| `src/lollo_landing/logs.py`, `__main__.py` | Logging to stdout (agent names are never logged) and the uvicorn entry point. |
| `src/lollo_landing/templates/` | `base.html` (the top bar, "Back to Lettuce"), `landing.html`, `error.html`. |
| `tests/` | pytest: the page, the executor API and `AgentAsks`, with the vault's `make_jwt` pattern and an injectable clock. |
| `executor/run.mjs` | The lollo executor (Node, no dependencies). |
| `executor/tests/run.test.mjs` | Its tests (`node --test`): local `node:http` servers play landing and gmail-triggers, a fake `letta` records its argv. |

## The landing page

- `/` lists, in `LOLLO_PROFILES` order, a link to `/<page>/` for each lollo page profile that is on, named from `LABELS` (a profile without an entry shows its own name). `/assets/lollo.css` and `/assets/icon-192.png` are its assets.
- With `lollo-code-server` on, the code editor's entry becomes a list of the agents by name, each a link to `/code-server/?folder=/work/<agent-id>`; agents sharing a name show their id too. Until the first list arrives the page says it is on its way. Opening the page asks the executor for a fresh list.
- Every route needs a verified Access JWT of the lollo's application; the page also checks Fetch Metadata (`admit` in `lollo-common`), the assets don't. The page, its assets and its errors set a CSP that allows no script, and COOP same-origin. A refusal renders without the person's email or the agents.
- The agent list is untrusted (agents can send their own): names are escaped text, and only ids shaped like agent ids are kept.

## The executor

`run.mjs` runs every minute from system cron in the app-server, as root, through upstream's `agent-env`. Over HTTP on the stack network, with the bearer token `LOLLO_EXECUTOR_TOKEN`, it asks lollo (`GET /executor/v1/status`) whether it wants the agent list and, with `--gmail-triggers` and `--news-radar`, claims those sidecars' requests (`GET /executor/v1/requests`) and turns each into `letta cron add --once`, posting the results back. When any of them wants the list it runs `letta agents list` once and sends it to each (`PUT /executor/v1/agents`). Details, limits and the install are in [executor/README.md](executor/README.md).

## How it plugs in

- **Overlay:** `compose/landing.yml` defines the `landing` service behind all three lollo page profiles: image `lollo-landing:local`, the `default` network only, no volumes and no host paths, read-only root, no capabilities, `no-new-privileges`, 128 MB.
- **Environment:** `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_LOLLO_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS`, `PUBLIC_ORIGIN` (set from `LOLLO_ORIGIN`), `LETTUCE_ORIGIN` (set from Lettuce's `PUBLIC_ORIGIN`, for "Back to Lettuce"), `LOLLO_PROFILES` and `LOLLO_EXECUTOR_TOKEN`.
- **Tunnel:** `cf_tunnel_configure` puts the lollo host's rules first: each `PROFILE_ROUTES` row whose profile is on, then, when any is, `LOLLO_LANDING_PATH` (`^/(assets/[^/]+)?$`) to `http://landing:8000`, then a 404 for the rest of the lollo host. So `/executor/v1/` and `/healthz` are never routed. `landing` serves every asset under `/assets/`, and `test_every_page_route_is_the_root_or_under_assets` checks that its routes stay within what the tunnel sends it.
- **API for the executor:** `GET /executor/v1/status` and `PUT /executor/v1/agents`, behind `guard` from `lollo-common` (the token, no `Origin`, no tunnel headers).
- **lolloctl:** `up` (`up_stack`) runs `ensure_executor_token` and `sync_lollo_profiles`, which writes `LOLLO_PROFILES` in `stack.env` from `COMPOSE_PROFILES` (the `PROFILE_ROUTES` profiles that are on, in that order; only `landing` gets it). After `compose up`, `sync_executor` installs or removes the executor ([executor/README.md](executor/README.md)). `check_stack`, with any lollo page profile on, requires `LOLLO_ORIGIN`, `CF_LOLLO_AUD` and a well-formed `LOLLO_EXECUTOR_TOKEN`, and runs `check_landing` (a hardened sidecar on `default` only, mounting nothing); whatever the profiles, `check_lollo_audience` holds every lollo service, this one included, to the lollo's Access application and origin ([Rules](#rules)).
- **A new lollo page:** a `PROFILE_ROUTES` row in `lib/cloudflare.sh` (profile `lollo-<page>`, page at `/<page>/`), the profile in `compose/landing.yml` and an entry in `LABELS`; tests check that the first two match. The whole checklist: [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-lollo-sidecar).

## Rules

### The lollo accepts only its own Access application
Whatever the profiles, `check_lollo_audience` refuses a lollo service (`lollo_services`: each `PROFILE_ROUTES` service and `landing`) whose `CF_ACCESS_AUD` is not `CF_LOLLO_AUD`, or is Lettuce's `CF_ACCESS_AUD`, or whose `PUBLIC_ORIGIN` is not `LOLLO_ORIGIN`. cloudflared hands Lettuce's token to the BFF in plain HTTP, inside the namespace agents share, so a lollo page that took it could be driven by an agent that caught it.

### What `check_landing` refuses
With any lollo page profile (`lollo-vault`, `lollo-gmail-triggers`, `lollo-news-radar`, `lollo-code-server`): `LOLLO_ORIGIN` or `CF_LOLLO_AUD` empty (the lollo host is missing, and `up` creates it), a missing or malformed `LOLLO_EXECUTOR_TOKEN`, and a `landing` service that is missing, not a hardened sidecar (`check_sidecar`), on a network other than `default`, or mounting anything.

### The landing page stays passive
It has no script (its CSP allows none), touches no other sidecar and opens no connection to Lettuce: it reads only the agent list the executor sends. Lollo sidecars go under `/<sidecar>/` on the lollo host, never on Lettuce's. The Fetch Metadata rules every lollo page follows are in [lollo-common](../lollo-common/README.md#rules).

## Build and test

```bash
tests/run-sidecar.sh landing                      # the landing page and its executor API (pytest), plus the executor's tests
                                                 # (node --test in node:22.22.0-slim) when no args are given
node --test 'landing/executor/tests/*.test.mjs'   # the executor's tests on the host
```

`lolloctl up` builds the image. Run `tests/run-sidecar.sh landing` before changing this folder or `lollo-common/`, and `tests/run.sh` before changing what lolloctl does with it.

## Upstream seams

Listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams); every Lettuce upgrade checks them:

- The executor: `letta cron add` with `--name=`, `--description=`, `--prompt=`, `--at=in 1m`, `--once`, `--agent=`, `--conversation=new|default` (the `=` form) and its JSON output (`.id`); `letta agents list` (`.items[]`); the scheduler picking up a changed task file without a restart; the app-server entrypoint copying `/root/.letta/system-cron` to `/etc/cron.d`, and the BFF listing that folder in the Tasks tab (since v0.26: [the cron file](executor/README.md)); the image's `/usr/local/bin/agent-env`.
- The landing page's agent links, with the code editor on: code-server's `?folder=` query and the workspaces at `/work/<agent-id>`; the list itself comes from `letta agents list` through the executor.
