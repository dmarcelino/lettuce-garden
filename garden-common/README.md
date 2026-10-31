# garden-common

The Python package `garden_common`, shared by the four garden images: `garden` (the landing page), `vault`, `gmail-triggers` and `code-server` (the code editor's gate). It holds the checks every garden request goes through (the Access JWT, Fetch Metadata, Origin and CSRF, COOP), the guard of the executor API at `/executor/v1/`, the agent-list rules, text clean-up, the icon encoder and the garden stylesheet. It is a library, not a service: it has no compose profile, overlay or route of its own. The security model it implements is described in [docs/vault.md](../docs/vault.md), [docs/gmail-triggers.md](../docs/gmail-triggers.md) and [docs/code-server.md](../docs/code-server.md).

![How a garden request is checked](../docs/diagrams/garden-request-checks.svg)

An interactive version, [garden-request-checks.html](../docs/diagrams/garden-request-checks.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `pyproject.toml` | The package and its pinned dependencies (`anyio`, `pyjwt[crypto]`, `starlette`), kept in step with the sidecars that install it |
| `.dockerignore` | Keeps caches, build output and this README out of the build context |
| `src/garden_common/auth.py` | `Authenticator` (the `Cf-Access-Jwt-Assertion` JWT: RS256 against the team's keys, the garden's audience and issuer, an email in `ALLOWED_USERS`; `csrf_token`, `check_csrf`, `check_origin`), `check_fetch` (Fetch Metadata for `PAGE`, `FORM`, `FETCH`), `admit` (both), `COOP` |
| `src/garden_common/executor.py` | `guard` (the executor API: `/executor/v1/` only, no `Origin` or tunnel headers, the bearer token), `read_json` and `read_agents` (capped, validated bodies), `AgentAsks` (when a sidecar wants a new agent list), `EXECUTOR_TOKEN` |
| `src/garden_common/text.py` | `one_line`, `encodable` and `CONTROL`: outside text made safe to store and show |
| `src/garden_common/icons.py` | `png`: a small PNG encoder; each sidecar draws its own icon with it |
| `src/garden_common/garden.py`, `static/garden.css` | `stylesheet()`: the garden's look, which every garden page links to |

Who uses what: the vault uses `admit` with `PAGE`, `FORM` and `FETCH`; gmail-triggers with `PAGE` and `FORM` plus `guard`; the landing page with `PAGE` plus `guard`; the code editor's gate uses `Authenticator` and `COOP` with Sec-Fetch rules of its own.

## How it plugs in
- **Installed from a build context, not PyPI.** Each overlay (`compose/garden.yml`, `vault.yml`, `gmail-triggers.yml`, `code-server.yml`) passes this directory as the build context named `garden-common` (`additional_contexts`), and each Dockerfile copies it and runs `pip install` on it before its own package. `tests/run-sidecar.sh` and `scripts/code-server-e2e.sh` pass it with `--build-context`. It is never a pip dependency by name.
- **The whole directory is copied into four images.** Any file here changes that layer and rebuilds all four, so `README.md` is in `.dockerignore`: editing these docs rebuilds nothing.
- **Settings it expects:** an object with `team_domain`, `audience`, `issuer`, `allowed_users` and `public_origin` (`AccessSettings`). The overlays fill them from `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS` and `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`); `check_garden_audience` refuses a garden service with Lettuce's audience or another origin. The executor guard takes `GARDEN_EXECUTOR_TOKEN`, which `check_stack` requires to be 64 lowercase hex characters. See [AGENTS.md](../AGENTS.md) ("Key invariants").

## Build and test
The package has no tests or image of its own: the sidecars' tests cover it. Before changing it, run all four:
```bash
tests/run-sidecar.sh garden
tests/run-sidecar.sh vault
tests/run-sidecar.sh gmail-triggers
tests/run-sidecar.sh code-server
```
