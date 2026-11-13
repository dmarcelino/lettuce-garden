# vault

The vault service: each person's secrets, encrypted with their passphrase, shared with agents only when the person approves. It is Python (Starlette, MCP SDK 2.x, `cryptography`), runs as the `garden-vault` service behind the `garden-vault` profile, and serves its pages at `/vault/` on the stack's garden host (`<name>-garden.<domain>`). For users and admins (using it, running it, the security model): [docs/vault.md](../docs/vault.md).

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
| `src/garden_vault/vault.py` | The encrypted SQLite store (`/data/vault.db`): setup, unlock, lock and auto-lock, erase (at once, or scheduled while locked), secrets (`build_fields`, custom fields included), audit log, push subscriptions |
| `src/garden_vault/kinds.py` | The secret kinds, Bitwarden's five item types with its fields: public and private fields, the private ones that may be shared without asking (`FieldSpec.auto`; a kind with any is `auto_allowed`), the ones masked on screen like Bitwarden's password fields (`FieldSpec.masked`), custom field keys (`custom_key`) and the preview |
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
- **lettucectl:** `add` gives the vault its own VAPID pair (`push_env_lines`); `vault_volume`/`has_vault` let `backup` copy `vault-data` read-only and `remove` warn before `compose down -v` deletes it.
- **`check_stack`:** `garden-vault` must be a hardened sidecar with no host path (`check_sidecar`), `vault-data` is mounted by nothing else and no `volumes_from` reaches it, every garden service accepts only `CF_GARDEN_AUD` and `GARDEN_ORIGIN` (`check_garden_audience`), the app-server's namespace drops `NET_RAW`, and with pipelock `NO_PROXY` lists `garden-vault`. Why each matters: [Rules](#rules) below; every `check_stack` rule: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).

## Build and test
`lettucectl up` builds the image. The tests need only Docker:
```bash
tests/run-sidecar.sh vault            # pytest in the image's test stage
tests/run-sidecar.sh vault -k vault   # extra args go to pytest
```
Run them before changing this directory or `garden-common/`.

The tests never use the network: pushes go to `FakeSender`, Access tokens come from `make_jwt` with a test RSA key, `FAST_KDF` makes scrypt cheap, and a fake `Clock` drives time. Fake values (`TEST_SECRET_DO_NOT_USE_938742`, a card number, a street) are asserted never to reach the vault file on disk, logs or pushes, nor pages before Show when they are masked (the card number). `test_vault_run.py` runs `agent-skills/using-vault-secrets/vault-run` against a loopback stub of `/redeem`, and `test_vault_fill.py` runs `vault-fill` (alone, and piped inside `vault-run`); `tests/run-sidecar.sh vault` mounts `agent-skills/` into the test container for them. `test_bitwarden.py` reads a fake export of every item type.

## Rules

### The vault is the approval gate, not Lettuce
Agents share the BFF's network namespace and can call the vault directly, so Lettuce's UI can't be what approves a request. Approval, and every `/vault/` page on the garden host, require a verified Access JWT (`garden_common.auth`).

That is the same JWT cloudflared sends the BFF in plain HTTP inside that namespace. So every container there drops `NET_RAW` (`compose/hardening.yml`, refused by `check_stack` otherwise), and agents can't capture it. An agent could still bind the BFF's port while the BFF is down and the app-server is not, and catch the JWT that way. But the vault, on the garden host, accepts only the garden's Access application (`CF_GARDEN_AUD`; `check_garden_audience`), so Lettuce's token is useless to it.

### What agents get, and what that does not protect
- Agents read names and public fields with `list_secrets`. They get private values only through `request_secret`: after the person approves those fields, or without asking for the fields the person allowed (only fields with `FieldSpec.auto`, in a secret set to `auto`; a request with any other field waits for approval as a whole).
- By default (`delivery: "placeholders"`) the agent gets a handle instead of the values. `vault-run` redeems one or more at `/redeem` in one call, all or none: an unknown handle, or a name two handles share or `vault-run`'s environment already has, uses up none. Each is single use, `HANDLE_TTL_SECONDS`, at most `MAX_HANDLES` unused, `garden-vault` host and no `Origin` only. A lock drops handles and uncollected values. `vault-run` runs one command with them (`vault-fill` puts them into a template) and scrubs them from its output.
- That keeps values out of the conversation and away from the AI provider, but it is not a boundary: agents are root in the app-server and can redeem a handle themselves or open the filled file, and values under 6 characters are not scrubbed. Docs must not claim more ([docs/vault.md](../docs/vault.md#security-model) says so in plain language).
- `delivery: "text"` needs a `reason`, shown to the person and audited. Values shared as text always come with `rules` (`service.rules`): the approved purpose only, no outside service unless the purpose needs it, never repeated in a reply, memory or files. Those are instructions to the agent, not a boundary.

### Stored format
Stored secrets carry no version, so a change to a kind's fields must read what is already stored. A missing attribute takes a safe default (`Field.masked` missing reads as masked); a change that cannot do that brings its migration in the same change.

### Masked is not private
`sensitive` (private) decides what agents must ask for; `masked` decides what the person's own screen hides. Masked follows Bitwarden's form: its password-type fields (a login's password and TOTP, a card's number and code, an identity's SSN and passport number, an SSH private key) and Hidden custom fields. Masked values are never sent to the edit page (blank keeps them) and show as dots until "Show hidden fields", which History records. Every other value, private or not, is shown and prefilled.

### Erasing without the passphrase
The unlock page's erase is the one way to destroy the vault without knowing the passphrase, so the Access sign-in is all it checks. What stands in for the passphrase is time and noise: it runs only `ERASE_DELAY_SECONDS` after it was asked for, never while the vault is unlocked, every step (scheduled, cancelled, run, and the extra `ERASE_GRACE_SECONDS` after a restart) is pushed to every device under its own tag, unlocking cancels it, and `ERASE_REQUESTS_PER_HOUR` caps how often one person can set off those notices. The schedule lives in `vault_meta` (plaintext), so a restart keeps it, and `reset()` clears it.

### Never
- Never shorten an erase without the passphrase below its delay, run it while unlocked, or let it run without a notice to every device.
- Never add an MCP tool that edits secrets or bypasses approval.
- Never write vault data, keys or the passphrase to disk unencrypted, or to logs.
- Never mount `vault-data` into another service. Only `backup`'s one-off container may, read-only.
- Never let agent activity extend the auto-lock. The data key exists only in memory while the vault is unlocked.
- Never pass `COMPOSE_PROFILES` or `GARDEN_PROFILES` to the vault: a profile change would recreate it, and recreating it locks it ([lib/README.md](../lib/README.md#tunnel-routes-follow-the-profiles)).

## Upstream seams
These are the Lettuce behaviours the vault depends on (all of them are listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams)):
- The shared MCP list accepts the vault's `http` entry of only `name`, `transport` and `url`, and `lettucectl mcp` writes it into `/root/.letta/mcp-home/.letta/settings.json`.
- The `using-vault-secrets` skill relies on the `mcp-servers` skill wrapper and on letta-code telling the agent when a background command finishes, which is how an agent waiting for an approval gets a new turn, and on `letta mcp call <tool> --args-file -` reading the arguments from stdin and exiting 2 when the tool reports an error (the skill's email recipe pipes `vault-fill` into it, under `bash` for `pipefail`).
- `MAX_WAIT_SECONDS` stays below the 120 s Lettuce's MCP bridge gives a tool call (see `config.py`).
