# outlook/ — the Outlook sidecar

A garden sidecar (Python, MCP SDK 2.x, Starlette, httpx, cryptography) that gives a
stack's agents one Outlook mailbox under a per-stack policy. Built by lettucectl via
`compose/outlook.yml`. Usage and the security model are in
[docs/outlook.md](../docs/outlook.md); the design record is
[docs/outlook-plan.md](../docs/outlook-plan.md).

## What it serves

One Starlette app (`app.py`):

- `/mcp` — the agent-facing MCP endpoint, reached as `http://garden-outlook:8000/mcp`
  on the stack network. Transport security pins the host to `garden-outlook`; no
  Access JWT (reachability is not the control — the policy is).
- `/outlook/` — the person's page, behind the garden's Cloudflare Access application
  (`garden_common.auth`, Fetch-Metadata + CSRF + origin checks, same as the vault).
  Sets the policy, shows the connect bookmarklet, receives the handover at
  `/outlook/connect`, and disconnects.
- `/healthz`.

## Modules (`src/garden_outlook/`)

- `levels.py` — the policy model: services (`mail`, `calendar`, `tasks`) and their
  cumulative levels, `Policy.permits`, `calendar_allows_attendees` (the gate that
  makes `self-events` no-invite), shared-mailbox membership, lenient `parse_policy`
  and strict `apply_update`.
- `recipients.py` — the allowed-recipients guard (bare address or `@domain`, exact or
  domain match, own account always allowed, empty = unrestricted, fails closed).
  Mirrors upstream Lettuce PR #83's entry format.
- `tokens.py` — the connected session: `session_from_payload` validates the handover;
  `SessionStore` seals it with `crypto.py` (AES-256-GCM, key from `OUTLOOK_SECRET` +
  a per-volume salt) and holds it in memory; `apply_refresh` updates it in place.
- `outlook_client.py` — the Microsoft Graph client and **FOCI token redemption**
  (exchange the browser's first-party refresh token for a Graph token; no app
  registration). The one place to fix when Graph changes or a tenant refuses the
  exchange. `trust_env=False`: the token never goes through pipelock.
- `service.py` — the façade: the session store, the policy (persisted to
  `policy.json`), and the client.
- `mcp_tools.py` — the tools. Each live-checks the policy (`require`), shared-mailbox
  reads (`shared`), and sends/invitations against the allowed-recipients list
  (`check_recipients`); `self-events` refuses attendees. Outlook's PascalCase JSON is
  mapped to compact output, and mail/event/task text carries the untrusted-content
  note.
- `bookmarklet.py` — the connect bookmarklet, built for the stack's garden origin. It
  reads the Outlook web app's cached session and opens `/outlook/#connect=<...>`; the
  material rides in the fragment (not a cross-origin fetch Outlook's CSP would block,
  not a query string a log would keep). `static/app.js` reads the fragment and POSTs
  it to `/outlook/connect`.
- `logs.py`, `config.py`, `icons.py`, `__main__.py` — logging (scrubs the tokens as a
  backstop), settings from the environment, the drawn icon, the entry point.

## How the guarantees are enforced

- **The token never reaches agents.** It lives only in this container, sealed on the
  `outlook-data` volume; agents get MCP tools. `check_stack`'s `check_outlook` +
  `volume_users` refuse any config where another service mounts `outlook-data` or
  where this service loses its hardening (`read_only`, `cap_drop: [ALL]`,
  `no-new-privileges`, no bind mounts, no extra namespaces).
- **The policy is the boundary.** Enforcement is at execution in `mcp_tools.py`, not a
  removable tool from the list, so calling `/mcp` directly gains nothing.
- **The page cannot be reached by agents.** It is on the garden host, under the
  garden's own Access audience (`check_garden_audience`, which covers every
  `PROFILE_ROUTES` sidecar, this one included).

## Tests

`tests/run-sidecar.sh outlook` (pytest in the image's `test` stage; no network):

- `test_levels.py`, `test_recipients.py`, `test_freebusy.py` — the policy model, the
  allow-list, and the free/busy gap maths (all dependency-free).
- `test_tokens.py` — handover validation, the seal/load round-trip, that a wrong
  secret cannot read the session, and that the log filter scrubs the token.
- `test_client.py` — refresh-on-expiry and safe error text, against `httpx.MockTransport`.
- `test_enforcement.py` — the tools driven through the real app over `/mcp`: level
  denials, the allowed-recipients check (including re-checking a draft before send),
  the `self-events` no-attendee rule, shared-mailbox gating, and that a mail body is
  returned to the agent but never logged (a canary string).

## Maintenance

- Dependencies are pinned in `pyproject.toml`; this is an application image, rebuilt
  on every `lettucectl up`.
- `outlook_client.py` is the one place that knows the Graph shapes and the FOCI
  redemption. If Microsoft changes Graph or how the web app caches its refresh token,
  fix the client
  and `bookmarklet.py`; the rest is unaffected.
