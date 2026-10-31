# garden

The `garden` service serves the landing page at the root of a stack's garden host, `https://<name>-garden.<domain>/`, with a link to each garden page that is on and, with the code editor, a link to each agent's workspace. This folder also holds the garden executor, `executor/run.mjs`, which runs in Lettuce's app-server, not in this image. The service has no profile of its own: it is on whenever any garden page profile is (`garden-vault`, `garden-gmail-triggers`, `garden-code-server`). The person's view of the page is in [docs/code-server.md](../docs/code-server.md) ("For the person using it"); the design record is [docs/garden-host-plan.md](../docs/garden-host-plan.md); the executor's install and protocol are in [docs/garden-executor.md](../docs/garden-executor.md).

![The landing page, the garden executor and what they talk to](../docs/diagrams/garden-architecture.svg)

An interactive version, [garden-architecture.html](../docs/diagrams/garden-architecture.html), opens in a browser from a clone of this repo.

## Files

| Path | What it does |
|---|---|
| `Dockerfile`, `pyproject.toml` | The service's image: Python 3.12 slim, `garden-common` from its build context, pinned dependencies, a `test` stage; runs as uid 65534 on port 8000. |
| `src/garden_landing/config.py` | Settings from the environment: the two origins, `GARDEN_PROFILES`, the executor token (64 hex characters or the service refuses to start); `page_path` (profile `garden-<page>` serves `/<page>/`). |
| `src/garden_landing/app.py` | The routes, `LABELS` (what the page calls each garden page), the agent rows and their links. |
| `src/garden_landing/agents.py` | `AgentList`: the last agent list, in memory only, and when to ask for a new one (`AgentAsks`). |
| `src/garden_landing/icons.py` | The page's icon, drawn on first use (no binary files in the repo). |
| `src/garden_landing/logs.py`, `__main__.py` | Logging to stdout (agent names are never logged) and the uvicorn entry point. |
| `src/garden_landing/templates/` | `base.html` (the top bar, "Back to Lettuce"), `landing.html`, `error.html`. |
| `tests/` | pytest: the page, the executor API and `AgentAsks`, with the vault's `make_jwt` pattern and an injectable clock. |
| `executor/run.mjs` | The garden executor (Node, no dependencies). |
| `executor/tests/run.test.mjs` | Its tests (`node --test`): local `node:http` servers play garden and gmail-triggers, a fake `letta` records its argv. |

## The landing page

- `/` lists, in `GARDEN_PROFILES` order, a link to `/<page>/` for each garden page profile that is on, named from `LABELS` (a profile without an entry shows its own name). `/assets/garden.css` and `/assets/icon-192.png` are its assets.
- With `garden-code-server` on, the code editor's entry becomes a list of the agents by name, each a link to `/code-server/?folder=/work/<agent-id>`; agents sharing a name show their id too. Until the first list arrives the page says it is on its way. Opening the page asks the executor for a fresh list.
- Every route needs a verified Access JWT of the garden's application; the page also checks Fetch Metadata (`admit` in `garden-common`), the assets don't. The page, its assets and its errors set a CSP that allows no script, and COOP same-origin. A refusal renders without the person's email or the agents.
- The agent list is untrusted (agents can send their own): names are escaped text, and only ids shaped like agent ids are kept.

## The executor

`run.mjs` runs every minute from system cron in the app-server, as root, through upstream's `agent-env`. Over HTTP on the stack network, with the bearer token `GARDEN_EXECUTOR_TOKEN`, it asks garden (`GET /executor/v1/status`) whether it wants the agent list and, with `--gmail-triggers`, claims Gmail triggers' requests (`GET /executor/v1/requests`) and turns each into `letta cron add --once`, posting the results back. When either wants the list it runs `letta agents list` once and sends it to each (`PUT /executor/v1/agents`). Details, limits and the install are in [docs/garden-executor.md](../docs/garden-executor.md).

## How it plugs in

- **Overlay:** `compose/garden.yml` defines the `garden` service behind all three garden page profiles: image `lettuce-garden:local`, the `default` network only, no volumes and no host paths, read-only root, no capabilities, `no-new-privileges`, 128 MB.
- **Environment:** `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS`, `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`), `LETTUCE_ORIGIN` (set from Lettuce's `PUBLIC_ORIGIN`, for "Back to Lettuce"), `GARDEN_PROFILES` and `GARDEN_EXECUTOR_TOKEN`.
- **Tunnel:** `cf_tunnel_configure` puts the garden host's rules first: each `PROFILE_ROUTES` row whose profile is on, then, when any is, `GARDEN_LANDING_PATH` (`^/(assets/[^/]+)?$`) to `http://garden:8000`, then a 404 for the rest of the garden host. So `/executor/v1/` and `/healthz` are never routed.
- **API for the executor:** `GET /executor/v1/status` and `PUT /executor/v1/agents`, behind `guard` from `garden-common` (the token, no `Origin`, no tunnel headers).
- **lettucectl:** `up` (`up_stack`) runs `ensure_executor_token` and `sync_garden_profiles`, which writes `GARDEN_PROFILES` in `stack.env` from `COMPOSE_PROFILES` (the `PROFILE_ROUTES` profiles that are on, in that order; only `garden` gets it). After `compose up`, `sync_executor` installs or removes the executor ([docs/garden-executor.md](../docs/garden-executor.md)). `check_stack`, with any garden page profile on, requires `GARDEN_ORIGIN`, `CF_GARDEN_AUD` and a well-formed `GARDEN_EXECUTOR_TOKEN`, and runs `check_garden` (a hardened sidecar on `default` only, mounting nothing); whatever the profiles, `check_garden_audience` holds every garden service, this one included, to the garden's Access application and origin. The full list is in [AGENTS.md](../AGENTS.md), "Key invariants".
- **A new garden page:** a `PROFILE_ROUTES` row in `lib/cloudflare.sh` (profile `garden-<page>`, page at `/<page>/`), the profile in `compose/garden.yml` and an entry in `LABELS`; tests check that the first two match.

## Build and test

```bash
tests/run-sidecar.sh garden                      # the landing page and its executor API (pytest), plus the executor's tests
                                                 # (node --test in node:22.22.0-slim) when no args are given
node --test 'garden/executor/tests/*.test.mjs'   # the executor's tests on the host
```

`lettucectl up` builds the image. Run `tests/run-sidecar.sh garden` before changing this folder or `garden-common/`, and `tests/run.sh` before changing what lettucectl does with it.

## Upstream seams

Named in [AGENTS.md](../AGENTS.md), "Relationship to upstream"; `lettucectl upgrade` must check them:

- The executor: `letta cron add` with `--name=`, `--description=`, `--prompt=`, `--at=in 1m`, `--once`, `--agent=`, `--conversation=new|default` (the `=` form) and its JSON output (`.id`); `letta agents list` (`.items[]`); the scheduler picking up a changed task file without a restart; the app-server entrypoint copying `/root/.letta/system-cron` to `/etc/cron.d`; the image's `/usr/local/bin/agent-env`.
- The landing page's agent links, with the code editor on: code-server's `?folder=` query and the workspaces at `/work/<agent-id>`; the list itself comes from `letta agents list` through the executor.
