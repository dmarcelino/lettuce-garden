# garden-common

The Python package `garden_common`, shared by the five garden images: `garden` (the landing page), `vault`, `gmail-triggers`, `news-radar` and `code-server` (the code editor's gate). It holds the checks every garden request goes through (the Access JWT, Fetch Metadata, Origin and CSRF, COOP), the guard of the executor API at `/executor/v1/` and the request queue's routes, the agent-list rules, text clean-up, the icon encoder and the garden stylesheet. It is a library, not a service: it has no compose profile, overlay or route of its own. The security model it implements is described in [docs/vault.md](../docs/vault.md), [docs/gmail-triggers.md](../docs/gmail-triggers.md) and [docs/code-server.md](../docs/code-server.md).

![How a garden request is checked](../docs/diagrams/garden-request-checks.svg)

An interactive version, [garden-request-checks.html](../docs/diagrams/garden-request-checks.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `pyproject.toml` | The package and its pinned dependencies (`anyio`, `pyjwt[crypto]`, `starlette`), kept in step with the sidecars that install it |
| `.dockerignore` | Keeps caches, build output, this README and the agents' pointer to it (`AGENTS.md`, `CLAUDE.md`) out of the build context |
| `src/garden_common/auth.py` | `Authenticator` (the `Cf-Access-Jwt-Assertion` JWT: RS256 against the team's keys, the garden's audience and issuer, an email in `ALLOWED_USERS`; `csrf_token`, `check_csrf`, `check_origin`), `check_fetch` (Fetch Metadata for `PAGE`, `FORM`, `FETCH`), `admit` (both), `COOP` |
| `src/garden_common/executor.py` | `guard` (the executor API: `/executor/v1/` only, no `Origin` or tunnel headers, the bearer token), `read_json` and `read_agents` (capped, validated bodies), `AgentAsks` (when a sidecar wants a new agent list), `request_routes` and `RequestQueue` (the requests, results, agents and heartbeat routes of a sidecar whose requests the executor runs), `EXECUTOR_TOKEN` |
| `src/garden_common/text.py` | `one_line`, `encodable` and `CONTROL`: outside text made safe to store and show |
| `src/garden_common/icons.py` | `png`: a small PNG encoder; each sidecar draws its own icon with it |
| `src/garden_common/garden.py`, `static/garden.css` | `stylesheet()`: the garden's look, which every garden page links to |

Who uses what: the vault uses `admit` with `PAGE`, `FORM` and `FETCH`; gmail-triggers and news-radar with `PAGE` and `FORM` plus `request_routes`; the landing page with `PAGE` plus `guard`; the code editor's gate uses `Authenticator` and `COOP` with Sec-Fetch rules of its own.

## How it plugs in
- **Installed from a build context, not PyPI.** Each overlay (`compose/garden.yml`, `vault.yml`, `gmail-triggers.yml`, `news-radar.yml`, `code-server.yml`) passes this directory as the build context named `garden-common` (`additional_contexts`), and each Dockerfile copies it and runs `pip install` on it before its own package. `tests/run-sidecar.sh` and `scripts/code-server-e2e.sh` pass it with `--build-context`. It is never a pip dependency by name.
- **The whole directory is copied into four images.** Any file here changes that layer and rebuilds all five, so `README.md`, `AGENTS.md` and `CLAUDE.md` are in `.dockerignore`: editing these docs rebuilds nothing.
- **Settings it expects:** an object with `team_domain`, `audience`, `issuer`, `allowed_users` and `public_origin` (`AccessSettings`). The overlays fill them from `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS` and `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`); `check_garden_audience` refuses a garden service with Lettuce's audience or another origin. The executor guard takes `GARDEN_EXECUTOR_TOKEN`, which `check_stack` requires to be 64 lowercase hex characters. Every `check_stack` rule: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).

## Rules

### The garden pages refuse same-origin script
The code editor shares the garden host's origin with the vault and Gmail triggers, and code-server serves webviews from that origin. So a page on the garden host must not trust a request just because it comes from its own origin. Every garden page checks Fetch Metadata (`check_fetch`):
- pages load only as top-level documents, from any site (for the Access re-login redirect);
- form posts come only from the same origin;
- the vault's JSON calls come only from its own script.

A refusal renders without the person's email, since pages carrying it carry the form token. Stylesheets, scripts, icons and the manifest are assets (`asset=True`) and stay unchecked. The landing page has no script at all (its CSP allows none) and touches no other sidecar. Every garden response sets COOP same-origin; the code editor's gate applies `Sec-Fetch-*` rules of its own ([code-server/README.md](../code-server/README.md#rules)).

The garden host keeps the garden pages off Lettuce's origin; these checks guard the pages from the code editor on the garden's own origin.

### The executor API is not for browsers or the internet
`guard` admits a request to `/executor/v1/` only with the bearer token, no `Origin`, and none of Cloudflare's `cf-access-jwt-assertion`, `cf-connecting-ip` or `cf-ray` headers. `/executor/v1/` is also outside every tunnelled prefix. The token keeps out the other containers, not agents, who can read it ([garden/executor/README.md](../garden/executor/README.md#trust)).

## Build and test
The package has no tests or image of its own: the sidecars' tests cover it. Before changing it, run all five:
```bash
tests/run-sidecar.sh garden
tests/run-sidecar.sh vault
tests/run-sidecar.sh gmail-triggers
tests/run-sidecar.sh news-radar
tests/run-sidecar.sh code-server
```
