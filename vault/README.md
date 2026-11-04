# vault

The vault service: each person's secrets, encrypted with their passphrase, shared with agents only when the person approves. It is Python (Starlette, MCP SDK 2.x, `cryptography`), runs as the `garden-vault` service behind the `garden-vault` profile, and serves its pages at `/vault/` on the stack's garden host (`<name>-garden.<domain>`). Usage and the security model are in [docs/vault.md](../docs/vault.md).

![The vault's containers, trust boundaries and requests](../docs/diagrams/vault-architecture.svg)

An interactive version, [vault-architecture.html](../docs/diagrams/vault-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `Dockerfile` | `base` installs `garden-common` from its build context, then this package; `test` adds pytest and `tests/`; the final stage runs as uid 10001, which owns `/data` |
| `pyproject.toml` | The package and its pinned dependencies |
| `src/garden_vault/__main__.py` | Entry point: reads the settings, builds the store, service, pusher and app, runs uvicorn on port 8000 without an access log |
| `src/garden_vault/config.py` | `PREFIX` (`/vault`), the limits (`REQUEST_TTL_SECONDS`, `HANDLE_TTL_SECONDS`, `MAX_HANDLES`, `MAX_PENDING`, `AUTO_LOCK_SECONDS`, `ERASE_DELAY_SECONDS`, `ERASE_GRACE_SECONDS`, `ERASE_REQUESTS_PER_HOUR`, ...) and `Settings` from the environment |
| `src/garden_vault/crypto.py` | scrypt key derivation and AES-256-GCM; the data key is stored only wrapped |
| `src/garden_vault/vault.py` | The encrypted SQLite store (`/data/vault.db`): setup, unlock, lock and auto-lock, erase (at once, or scheduled while locked), secrets (`build_fields`, custom fields included), audit log, push subscriptions. `FORMAT` in `vault_meta` is the fields' shape: an unlock that finds another one deletes every secret (no conversion) and records how many |
| `src/garden_vault/kinds.py` | The secret kinds, Bitwarden's five item types with its fields: public and private fields, the private ones that may be shared without asking (`FieldSpec.auto`; a kind with any is `auto_allowed`), custom field keys (`custom_key`) and the preview |
| `src/garden_vault/bitwarden.py` | Reads Bitwarden's unencrypted JSON export into kinds (`read_export`); refuses encrypted exports; per item, what was left out and why |
| `src/garden_vault/service.py` | Agent requests, approvals, handles for the default `placeholders` delivery (in memory; `alias` prefixes their variable names), `text` delivery only with a `reason`, the `rules` sent with shared values, and the notices and audit entries of a scheduled erase |
| `src/garden_vault/mcp_tools.py` | The three MCP tools: `list_secrets`, `request_secret`, `get_result` |
| `src/garden_vault/app.py` | The routes: `/mcp`, `/redeem` (for `vault-run`: `{"handles": [...], "taken": [...]}`, all or none, `Service.redeem`), `/healthz` and the UI under `/vault/`, the Bitwarden import (`/vault/secrets/import`: refused unless it declares a `Content-Length` of at most 5 MB plus 64 KB, checked before the body is read (`page(max_body=...)`); then read into memory, spooled to the tmpfs `/tmp` past 1 MB) included |
| `src/garden_vault/push.py` | Web Push to the person's devices (VAPID) |
| `src/garden_vault/logs.py` | Logging that redacts any value being shared |
| `src/garden_vault/icons.py` | The app icons, drawn as PNG at first use |
| `src/garden_vault/templates/`, `static/` | The pages (Jinja), their stylesheet and manifest, `app.js` (push sign-up) and `sw.js` (the service worker that shows notifications) |
| `tests/` | pytest: the app, crypto and store, push and logs, the service, the Bitwarden import, `vault-run` against a stub `/redeem`, and `vault-fill` |

The agent side, `vault-run`, `vault-fill` and their skill, lives in [agent-skills/using-vault-secrets](../agent-skills/using-vault-secrets).

## How it plugs in
- **Overlay:** [compose/vault.yml](../compose/vault.yml) defines `garden-vault` (image `lettuce-vault:local`, built from this directory with the `garden-common` build context): read-only root, `/tmp` as tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, 512 MB (scrypt needs 128 MiB per unlock), no ports, on the stack's default network. Its one mount is the named volume `vault-data` at `/data`.
- **Environment:** `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`, the garden's Access application), `CF_ACCESS_ISSUER`, `ALLOWED_USERS`, `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`), `VAULT_VAPID_PUBLIC_KEY`, `VAULT_VAPID_PRIVATE_KEY`, `PUSH_VAPID_CONTACT_EMAIL`, `TZ`. `VAULT_DB_PATH` defaults to `/data/vault.db`.
- **Tunnel:** the `PROFILE_ROUTES` row in [lib/cloudflare.sh](../lib/cloudflare.sh) sends `^/vault(/|$)` on the garden host to `http://garden-vault:8000`. Nothing else is routed to it: `/mcp` and `/redeem` answer only `Host: garden-vault` with no `Origin`, from the stack network.
- **Requests checked in the app:** every UI route goes through `garden_common.auth` (Access JWT, Fetch Metadata, Origin and CSRF on posts, COOP); see [garden-common](../garden-common/README.md).
- **Agents:** they reach `http://garden-vault:8000/mcp` over the stack network. `lettucectl mcp` registers it in Settings -> MCP once per stack (group `vault` in `lib/mcp-servers.mjs`, marker `VAULT_MCP_REGISTERED`), `add` does so for a new stack, and `up`/`profiles` remove the entry when the profile is off. `lettucectl skills` installs `vault-run` and `vault-fill`.
- **lettucectl:** `ensure_push_keys` gives the vault its own VAPID pair once; `vault_volume`/`has_vault` let `backup` copy `vault-data` read-only and `remove` warn before `compose down -v` deletes it.
- **`check_stack`:** `garden-vault` must be a hardened sidecar with no host path (`check_sidecar`), `vault-data` is mounted by nothing else and no `volumes_from` reaches it, every garden service accepts only `CF_GARDEN_AUD` and `GARDEN_ORIGIN` (`check_garden_audience`), the app-server's namespace drops `NET_RAW`, and with pipelock `NO_PROXY` lists `garden-vault`. The full rules are in [AGENTS.md](../AGENTS.md) ("Key invariants").

## Build and test
`lettucectl up` builds the image. The tests need only Docker:
```bash
tests/run-sidecar.sh vault            # pytest in the image's test stage
tests/run-sidecar.sh vault -k vault   # extra args go to pytest
```
Run them before changing this directory or `garden-common/`.

## Upstream seams
These are the Lettuce behaviours the vault depends on, as AGENTS.md names them:
- The shared MCP list accepts the vault's `http` entry of only `name`, `transport` and `url`, and `lettucectl mcp` writes it into `/root/.letta/mcp-home/.letta/settings.json`.
- The `using-vault-secrets` skill relies on the `mcp-servers` skill wrapper and on letta-code telling the agent when a background command finishes, which is how an agent waiting for an approval gets a new turn.
- `MAX_WAIT_SECONDS` stays below the 120 s Lettuce's MCP bridge gives a tool call (see `config.py`).
