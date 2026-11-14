# Lollo hostname: implementation plan

Status: implemented (2026-10-28), after the code editor (PR #13,
code-server/DESIGN.md). The migration ran on every stack (2026-10-29), and its code (moving
existing stacks to their lollo host, the old routes on Lettuce's host kept during the move, the
executor's old token and install, and the vault's push origin) was removed afterwards; the plan
below stays as the design record. Where the build differs from the plan below:

- **The executor sends the agent list to both sidecars.** Rather than Gmail triggers reading names
  from `landing`, the executor asks each (`landing`'s `/executor/v1/status`, Gmail triggers'
  `/requests`) whether it wants the list, runs `letta agents list` once, and sends it to each that
  answered. Gmail triggers' agent handling stays as it was, and no sidecar calls another.
- **The sidecars get no `LOLLO_PROFILES`.** Only `landing` needs it, so a profile change no longer
  recreates the vault (which locks it).
- **`profiles` migrates before it checks,** since `check_stack` refuses a stack with a lollo profile
  and no lollo audience; and it restarts migrated stacks even when the list is unchanged, so their
  sidecars take the new audience.
- **Shared code moved into lollo-common:** the executor API's guard and agent-list validation
  (`executor.py`), `text.py` and the icons (`icons.py`).
- **`list` shows the lollo URL.**
- **Lettuce's host keeps the old paths until the move is done.** While a move marker
  in `stack.env` is set (from the start of `ensure_lollo_host` until the stack's next successful `up`), the tunnel
  also routes the old per-profile paths on Lettuce's host to the running
  sidecars, so the lollo pages work at every step; `up` drops them after `compose up`, then clears
  the marker.
- **The lollo name follows Lettuce's.** It is `<first label of LETTUCE_HOSTNAME>-lollo.<DOMAIN>`, so
  a stack on its `HOSTNAME_FALLBACK` name gets `lollo-alfred-lollo`; when that is taken,
  `<label>-lollo-2` to `-9`, the first free one. A name taken between its pick and its DNS record is
  replaced the same way (Access application, routes, then the record), with no hand edit.
- **The landing page's assets are under `/assets/`,** so the tunnel routes `^/(assets/[^/]+)?$` to
  `landing` and a new asset needs no route change.

## Goal

Move every lollo page off Lettuce's hostname onto a second hostname per stack, owned entirely by
this repo:

| Today | After |
|---|---|
| `https://alfred.example.com/lollo/` | `https://alfred-lollo.example.com/` |
| `https://alfred.example.com/lollo/secrets/` | `https://alfred-lollo.example.com/secrets/` |
| `https://alfred.example.com/lollo/gmail-triggers/` | `https://alfred-lollo.example.com/gmail-triggers/` |
| `https://alfred.example.com/lollo/code-server/` | `https://alfred-lollo.example.com/code-server/` |

- Lettuce's hostname becomes pure upstream: one tunnel rule, to the app-server.
- The lollo hostname is entirely ours, with the landing page at its root.
- No manual setup: `lolloctl` creates and migrates every Cloudflare resource with the token it
  already has.
- The landing page lists the agents by name (nobody remembers an agent id), fed by the Gmail
  executor's pattern, made lollo-wide.

## Why

- **The code editor leaves Lettuce's origin.** Today a bug in code-server or a kept extension means
  script that can drive Lettuce as the person (docs/code-server.md, the remaining same-origin risk).
  On another origin it can't read Lettuce's pages, and Lettuce's `/ws` accepts only its exact
  `PUBLIC_ORIGIN` (upstream `bff/src/auth/origin.ts`, `checkUpgradeOrigin`).
- **The vault stops accepting Lettuce's token.** cloudflared hands the BFF the Access JWT in plain
  HTTP inside the namespace agents share; an agent that binds the BFF's port while it is down could
  catch it and use it on the vault, approvals included. AGENTS.md says only a separate Access
  application closes this. The lollo gets its own application, so its sidecars accept only its
  audience.
- **Lettuce's own bugs can't reach the lollo pages,** by browser rule rather than only by our Fetch
  Metadata checks.
- **Routing gets simpler:** no `/lollo` path rules ahead of the app-server, and no clash if upstream
  ever uses `/lollo`.

The editor still shares the lollo origin with the vault and Gmail triggers. The Fetch Metadata
checks, COOP and CSRF tokens keep guarding that, as they do today; a third hostname for the editor
would turn them into a browser rule, at the cost of one more name for users.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Name | `<label>-lollo.<DOMAIN>`, `<label>` being Lettuce's host's first label | One DNS label, so Cloudflare's free certificate (`*.<DOMAIN>`) covers it; typing the stack's name in the address bar offers both hosts |
| Access | A second application per stack, `lollo-<name>-pages`, with the stack's existing policy attached | Its own audience closes the token gap; one policy keeps `lolloctl users` updating one list |
| When | Every stack gets the lollo host, whatever its profiles | Profile changes then never create or delete Access applications or DNS records; with no lollo page on, the host answers 404 |
| Migration | `profiles` and `up` create a missing lollo host for a complete stack | No manual step; `up` stays offline once the host exists |
| Landing page and agent list | A new `landing` service | The vault stops serving the landing page and never receives executor-fed data; one owner for the agent list |
| Agent names | The executor sends `letta agents list` to `landing` | Agents are created in Lettuce's UI at any time, so a list `lolloctl` writes would go stale; the executor already does this for Gmail triggers |

## 1. Hostname and Cloudflare (`lolloctl`, `lib/cloudflare.sh`)

- `validate_name` refuses stack names ending in `-lollo`, so no stack's hostname is another stack's
  lollo host.
- The lollo name is derived from Lettuce's chosen hostname (`pick_lollo_hostname`):
  `<label>-lollo`, then `<label>-lollo-2` to `-9`, the first that exists neither in DNS nor in the
  zone; otherwise it fails with a clear message.
- New `stack.env` keys: `LOLLO_HOSTNAME`, `LOLLO_ORIGIN`, `CF_LOLLO_APP_ID`, `CF_LOLLO_AUD`,
  `CF_LOLLO_DNS_RECORD_ID`.
- The lollo application: `cf_app_body` with the lollo host (same IdP and session length,
  HttpOnly, SameSite=Lax) and the stack's `CF_ACCESS_POLICY_ID`. Policies are account-level and
  attached by id (`cf_policy_create` posts to `/access/policies`), so `cmd_users` is unchanged.
- `add` order: policy -> Lettuce application -> lollo application -> tunnel -> tunnel token ->
  tunnel routes -> lollo DNS record -> Lettuce DNS record. `CF_DNS_RECORD_ID` stays the last id
  written, so it still means "complete". `remove` deletes in reverse and unsets each id only on
  success. Tests assert both orders.
- `cf_tunnel_configure`: the Lettuce host goes only to `app-server`. The lollo host routes
  `^/secrets(/|$)`, `^/gmail-triggers(/|$)` and `^/code-server(/|$)` to their sidecars for the
  profiles that are on, and everything else to `landing`, or to `http_status:404` when no lollo
  profile is on. `PROFILE_ROUTES` loses the `/lollo` prefix.
- Migration (`ensure_lollo_host NAME`): for a complete stack without `CF_LOLLO_DNS_RECORD_ID`,
  create whatever of the lollo application and DNS record is missing, storing each id as it
  exists, then re-apply the routes. `profiles` calls it before its checks; `up` calls it
  before `check_stack` and before any container is recreated, and stops there on a Cloudflare
  failure ("run the same command again"), so a stack never has sidecars waiting for an audience
  that doesn't exist yet. Once the host exists, `up` makes no Cloudflare call.

## 2. The `landing` service (`landing/`, `compose/landing.yml`)

- A hardened sidecar like the vault (`check_sidecar`): read-only, `cap_drop: [ALL]`, on `default`
  only, no mounts. The agent list lives in memory.
- On whenever any lollo profile is on: the overlay lists every lollo profile under `profiles`
  (Compose starts a service when any of its profiles is on).
- Serves:
  - `/`: the landing page, rendered on the server with no script. Links to `/secrets/`,
    `/gmail-triggers/` and `/code-server/` for the profiles in `LOLLO_PROFILES`; with the editor
    on, the agents by name, each linking to `/code-server/?folder=/work/<id>`.
  - `/executor/v1/agents` (and its `wanted` flag): the executor's bearer token, `Origin` and
    Cloudflare's `cf-access-jwt-assertion`, `cf-connecting-ip` and `cf-ray` refused, never routed by
    the tunnel (the root rule matches `/` only).
- Replaces `landing_routes` in the vault, Gmail triggers and the gate, and `landing-agents.js` with
  its CSP hash and its dependency on Lettuce's `/ws` `agent_list` (that entry leaves AGENTS.md's
  list of upstream dependencies).
- The list is untrusted (agents are root in the app-server): ids must match `agent-[0-9a-f-]+`,
  names are escaped text with capped length and count, and agents sharing a name are told apart as
  Gmail triggers already does.
- Freshness: the page shows the last list; opening it sets `wanted`, and the executor sends a new
  one within a minute. After a restart the page says the list is on its way.

## 3. The lollo-wide executor

- `gmail-triggers/executor/run.mjs` becomes the lollo executor (`landing/executor/run.mjs`). Every
  minute it sends `letta agents list` to `landing` when `wanted` is set or its list is older than 15
  minutes, and, with `gmail-triggers` on, runs the request loop it runs today.
- Gmail triggers reads agent names from `landing` over the stack network instead of keeping its own
  copy.
- `sync_executor` installs it when it has work, with `gmail-triggers` or `code-server` on (the vault
  alone gives it none), and uninstalls it otherwise. The cron
  file becomes `lollo-rozzo-executor`; uninstall also removes the old
  `lollo-rozzo-gmail-triggers` files.
- `GMAIL_TRIGGERS_EXECUTOR_TOKEN` becomes `LOLLO_EXECUTOR_TOKEN`: `ensure_executor_token` moves an
  existing value over once (stacks' `stack.env` files hold it), then the old key is gone.
- `landing` joins `NO_PROXY` (`compose/pipelock-proxy.yml`) and `check_pipelock`'s required list.

## 4. The sidecars

- `PREFIX` becomes `/secrets`, `/gmail-triggers` and `/code-server` (each `config.py`,
  `lollo-common/src/lollo_common/lollo.py`, the gate).
- Their overlays pass `PUBLIC_ORIGIN=${LOLLO_ORIGIN}` and `CF_ACCESS_AUD=${CF_LOLLO_AUD}`. The
  vault then refuses Lettuce's token; the gate's `Origin` check follows the lollo origin.
- Kept: Fetch Metadata checks, COOP, CSRF tokens (the editor still shares the lollo origin), and
  the gate's `Service-Worker-Allowed` narrowed to its prefix.
- Vault push: subscriptions belong to the old origin. On its first start under a new origin the
  vault drops the subscriptions it holds, and its page offers to turn notifications on again;
  otherwise old notifications would link to dead paths on Lettuce's host.

## 5. Checks (`check_stack`)

- With any lollo profile: `landing` present and hardened (`check_sidecar`), only on `default`, no
  mounts; `LOLLO_ORIGIN` and `CF_LOLLO_AUD` set.
- Every lollo sidecar's `CF_ACCESS_AUD` equals `CF_LOLLO_AUD` and differs from the BFF's: the
  test that keeps the token gap closed.
- With `pipelock`: `landing` in `NO_PROXY`.

## 6. Tests and docs

- CLI (`tests/run.sh`): `add` and `remove` order; migration from `profiles` and `up` (resume after a
  failure at each step, a re-run doing nothing, `up` stopping before containers on a Cloudflare
  failure, `up` offline once migrated); tunnel routes for each profile combination; the new checks;
  the token rename; the executor installed with `gmail-triggers` or `code-server`.
- `landing` (new `tests/run-sidecar.sh landing`): the landing page and the untrusted agent list.
- Sidecar and gate tests: the new prefixes, audience and origin.
- Executor tests: the agent list without Gmail triggers.
- `scripts/code-server-e2e.sh` on the new paths; CI runs the new suite.
- Docs: AGENTS.md (layout, invariants, upstream dependencies, "No second way into an agent"),
  README, docs/cloudflare-setup.md (confirm the token's permissions are unchanged),
  docs/operations.md (migration: bookmarks change, vault notifications must be turned on again, the
  vault is recreated and locks once), docs/secret-broker.md, docs/gmail-triggers.md,
  docs/code-server.md.

## Risks

1. **A second login prompt.** Cloudflare Access is expected to reuse the login across applications
   of the same team, which is unverified. Check it on one stack before building the rest: if it
   prompts, "seamless" fails.
2. **Half-done migration.** Cloudflare, `stack.env` and containers change in separate steps, and
   `profiles` gives every stack its lollo host before any check or build, so a later failure can
   leave a stack moved on Cloudflare while its old sidecars (old prefixes, Lettuce's audience) still
   run. Lettuce's host therefore keeps routing the old paths to them while
   a move marker in `stack.env` is set, and only the `up` that recreated them drops those paths, then the
   marker; a failed route update there keeps the marker for the next `up` or `profiles`. The lollo
   pages, vault approvals included, stay reachable at every step, and re-runs resume.
3. **The vault locks once.** Its audience and origin change, so it is recreated on every stack.
4. **Same site, different origin.** Lettuce's SameSite=Lax cookie still goes with blind requests
   from the lollo tab to Lettuce. `/ws` checks the exact origin; Lettuce's HTTP endpoints were not
   checked.

## Not in scope

- Whether the Lettuce application keeps HttpOnly: keep it unless Lettuce's web client is confirmed
  not to read the cookie from script.
- Old links to the pages' paths on Lettuce's host: they land in Lettuce, which shows its own 404; a tunnel can't redirect.
- A third hostname for the editor.
