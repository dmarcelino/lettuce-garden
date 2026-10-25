# Gmail triggers: implementation plan

Status: implemented on `feature/gmail-triggers`, with one change from the design below found in review: **the file spool was replaced by an HTTP API** (see "Revision: no shared spool"). Not yet done: the step 0 spike against a running stack and the step 6 end-to-end test. The executor passes letta's flags as `--name=<value>` (not `--name <value>`): letta-code parses them with strict `parseArgs`, which misreads a value starting with `-`.

## Goal

Let each person on a stack say, from a web page and without any technical knowledge,
"when an email arrives in label X (optionally only from these senders), ask agent Y to do Z".
Polling Gmail costs no model turns; a turn happens only when a rule matches.

Everything ships in lettuce-multi-user. Upstream Lettuce and letta-code are not changed.

It is the first **garden sidecar**: an opt-in service with its own page at
`https://<stack>/garden/<sidecar>/`, here `https://<stack>/garden/gmail-triggers/`.
The vault keeps `/secrets/` (moving it is out of scope).

## How it works

```
 browser --tunnel--> gmail-triggers:8000 (stack network)          app-server container
 (Access)            /garden/gmail-triggers/  pages               (agents run here as root)
                     Gmail poller
                     rules + pending requests:                     executor (system cron, every minute)
                       $LETTA_STATE_DIR/garden/gmail-triggers  <--- GET /executor/v1/requests (token)
                     /executor/v1/ (never routed by the tunnel) <--- results, agents, heartbeat
                                                                    |
                                                                    +-- letta cron add --once --> task file
                                                                                                    |
                     google-creds (ro) --> Gmail API         bff --the one socket--> scheduler fires the task
```

1. The user creates a rule on the page:
   - **when:** labels, plus optional senders;
   - **who:** an agent, in a new conversation each time or the agent's default conversation;
   - **what:** a prompt;
   - **how often:** an hourly limit.
2. Every 2 minutes the sidecar asks Gmail what changed since its last `historyId` and
   matches new messages against the enabled rules.
3. On a match it stores a **pending request** in its own database. The prompt is the rule's
   prompt plus the matched message ids, sender and subject.
4. Every minute an **executor** inside the app-server container fetches pending requests from
   `http://gmail-triggers:8000/executor/v1/requests` and hands each to the letta-code CLI:
   `letta cron add --name="Gmail: <rule>" --prompt=... --at="in 1m" --once --agent=<id> --conversation=new`.
   It then posts the outcome back.
5. letta-code's scheduler sees the task file change (it watches its modification time), and
   fires the one-off task on its own connection, the BFF's. The agent reads the mail itself
   through google-mcp, under Settings -> Google's permissions.

The sidecar never talks to the app-server. The only path from it into Lettuce is a request
the executor fetches and letta-code's own CLI turns into a scheduled task.

## Revision: no shared spool

The first implementation shared a spool directory: the sidecar bind-mounted
`$LETTA_STATE_DIR/letta-home/garden/gmail-triggers/spool`, which the app-server sees under
`/root/.letta`. Review found that every path component below `letta-home` is writable by agents, so
an agent could replace one with a symlink, and Docker follows it **on the host** the next time the
container starts. The sidecar (uid 0, writes anything root owns) would then have any host directory
mounted, `/run` and its `docker.sock` included. A startup check can't close that race, because the
container restarts without lettucectl.

So the two sides now share no directory:
- Pending requests are rows in the sidecar's SQLite, written in the poll's transaction (which also
  removes the "crash between commit and file write" case).
- The executor talks to the sidecar over the stack network on `/executor/v1/`, outside
  `/garden/gmail-triggers`, so the tunnel never routes it. A per-stack bearer token
  (`GMAIL_TRIGGERS_EXECUTOR_TOKEN`, generated once into `stack.env`, installed for the executor as
  `/root/.letta/garden/gmail-triggers/token`) keeps out the other containers on the network.
  Agents can read it; with it they can do what they could with the spool (read, claim or drop
  requests, forge results) and nothing more.
- A claimed request is never offered again: a lost turn beats a duplicate one.
- `check_gmail_triggers` allows one bind mount (the rules directory), refuses a symlink on its path,
  and also refuses `privileged`, `cap_add`, `network_mode`, `pid`, `ipc`, `volumes_from`, `devices`,
  and volumes bound there through `driver_opts`.

A second code review then changed:
- The cron line runs `node` through upstream's `/usr/local/bin/agent-env` (its documented way to
  give cron jobs PID 1's environment), so the executor no longer reads `/proc/1/environ`.
- The executor token moved to `/etc/lettuce-garden/gmail-triggers-token` in the app-server's
  container layer: nothing secret goes under `state/`, and `up` reinstalls it after every recreate.
- The installer replaces its two files atomically, without a manifest.
- A failed install no longer stops `up`/`profiles` in the middle, and `profiles` re-syncs the
  executor on every run; a stopped stack is skipped when removing it.
- One failing request no longer ends the executor's batch.
- The agent list is fetched only when the sidecar asks (`agents_wanted`).
- Gmail's rate-limit 403s count as passing errors, not as "Gmail not granted".
- `remove` uses `LETTA_STATE_DIR`.

The sections below still describe the spool where they were written before this revision; the
API is in the "Executor API" section, which replaces "Spool".

## Upstream's rules, checked

Against upstream Lettuce's `AGENTS.md` (main):

| Upstream rule | This design |
|---|---|
| **Exactly one upstream WebSocket, owned by the BFF.** A closing connection kills its in-flight turns, and the scheduler is bound to the connection that attaches first | No new connection. Tasks are created through the CLI, and the scheduler fires them on the BFF's connection |
| **Upstream is not ours to patch** | Only published interfaces: the `letta cron` CLI and the system-cron directory the image's entrypoint already loads |
| **All durable state under `LETTA_STATE_DIR`** (one `tar` backs up a stack) | Rules and spool both live under the stack's state directory |
| **Never recreate `app-server` on its own** | The app-server's compose config doesn't change; the executor is installed with `compose exec`. lettucectl still brings up whole stacks only |
| `google-creds` / `google-policy` are kept away from other services | **Deliberate exception:** `gmail-triggers` mounts both read-only. The alternative is its own Google consent (see Decisions) |

## This repo's rules, checked

Against CLAUDE.md's "Relationship to upstream":

| Rule | This design |
|---|---|
| **No second way into an agent**: no sidecar opens a connection or starts a turn; `compose exec` for files and read-only CLI calls is the limit | **Breaks the "starts a turn" part on purpose**: starting turns is the feature. Covered by a narrow, written exception (below) |
| **Plug in only through supported seams**, or name the dependency so an upgrade checks it | The `letta cron add` CLI and `/root/.letta/system-cron` are not seams. Both get named under "Named today" |
| A route that bypasses the BFF needs a written reason | `/garden/gmail-triggers/` gets one (below) |
| **Don't write into what Lettuce manages**; own a namespace | The executor and spool live in `/root/.letta/garden/gmail-triggers`, which we own. Its cron file starts with `lettuce-garden-`. Tasks are created through letta-code's own CLI, never by editing its task file |
| **Overlays restrict, never redefine** | The overlay only adds a service; `app-server` is untouched |
| **Nothing secret under `stacks/<name>/state/`** | Rules and spool hold labels, addresses, prompts and message ids, not credentials. The Google token stays in its named volume |
| `NET_RAW` dropped in the app-server's network namespace | Not affected: nothing joins that namespace |

**The exception**, worded for CLAUDE.md:

> **No second way into an agent.** ... The one exception is a garden sidecar starting a turn
> through letta-code's own scheduler. It does this by handing a request to an executor in the
> app-server, which runs `letta cron add --once`. It opens no connection, and the scheduler
> fires the task on the BFF's connection. It gives agents nothing new, since they can already
> run `letta cron add` themselves. Only `gmail-triggers` uses it, only for rules the person
> made on its Access-protected page, and only within each rule's hourly limit.

**The route's reason**, worded for CLAUDE.md:

> `/garden/gmail-triggers/` (its own UI and Access check) bypasses the BFF because the rules
> must live where agents can't edit them. Inside Lettuce, agents could.

**Named dependencies**, added under "Named today":

> - `gmail-triggers` executor: `letta cron add` and its flags (`--name`, `--description`,
>   `--prompt`, `--at`, `--once`, `--agent`, `--conversation`), the scheduler picking up a
>   changed task file without a restart, and the entrypoint copying `/root/.letta/system-cron`
>   to `/etc/cron.d`.
> - `gmail-triggers` agent picker: `letta agents list` (shared with `find_agent`).

A dropped earlier design held a second app-server connection open from a relay container,
which breaks upstream's one-socket rule and this repo's rule alike. In letta-code, `startScheduler` stores the connecting socket
as the fire context for "Send now", and the scheduler binds to the first client. A second
connection could therefore end up owning scheduled turns that are not even its own.

## Where each piece runs

- **The page and poller run on the stack network, not in the app-server's network
  namespace.** Agent shells run in that namespace as root and can bind any free port. A page
  on a fixed port there could be taken over during a restart. The taker would receive the
  user's `Cf-Access-Jwt-Assertion`, which is valid for every page on the stack, the vault
  included. On the stack network the page has its own hostname, which no agent can take over.
- **The executor runs inside the app-server container** because that's where the `letta` CLI
  and the task file are. It gives nothing new to agents: they're root in that container and
  can already run `letta cron add` for any agent.
- **Rules live where agents can't reach them.** `$LETTA_STATE_DIR/garden/gmail-triggers` is
  not under any app-server mount, so agents can't add, change or read rules. That's the
  safety net the filters exist for.
- **The spool is where agents can reach it**: `$LETTA_STATE_DIR/letta-home/garden/gmail-triggers/spool`,
  which the app-server sees as `/root/.letta/garden/gmail-triggers/spool`. The sidecar mounts only
  that subdirectory, never the executor next to it, so it can't change code that runs as root in
  the app-server. Agents could add, change or
  drop requests there. Adding one only does what they could already do. Changing or dropping
  them can only spoil a trigger (denial of service); it can't widen access. The page treats
  everything it reads back from the spool as untrusted.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Profile | `gmail-triggers`; `check_stack` refuses it without `google` | It reuses the Google connection made in Settings -> Google |
| Google token | Mount `google-creds` and `google-policy` **read-only**; refresh the access token in memory and never write it back | No second OAuth client or redirect URI; no fight with workspace-mcp, which rewrites the file. Alternative if the exception is unwanted: the page runs its own consent with `gmail.metadata`, which can't read bodies, at the cost of one more manual redirect URI per stack |
| Gmail access | Needs the grant to include `gmail.readonly` (or wider); otherwise the page says what to change in Settings -> Google | `history.list`, `messages.get` (metadata only), `labels.list` |
| Google switched off | Read `google-policy/sidecar.json` each poll; when it says disabled, poll nothing and say so on the page | Settings -> Google stays the single switch |
| Path into Lettuce | Spool + executor + `letta cron add --once` | No second socket; the scheduler owns the turn |
| Conversation target | "A new conversation each time" (`new`) or the agent's default conversation | The CLI takes these without knowing conversation ids; picking an existing conversation needs a conversation list the CLI may not have (spike) |
| Agent picker | The executor writes `letta agents list` to the spool each run; the page reads it | No socket; at most a minute stale |
| What the agent is told | Rule prompt + message ids + sender address + subject (one line, max 120 chars, marked as untrusted text); never the body or snippet | Email content is untrusted input |
| Own mail | Ignore messages with the `SENT` label or from the account's own address | An agent replying must not set off its own trigger again |
| Sender filter | Exact addresses; per-rule "only if Gmail verified the sender" (DMARC pass in `Authentication-Results`), on by default when senders are set | `From` alone is easy to forge |
| Bursts | One request per rule per poll with all its matches; hourly limit per rule (default 6); matches over the limit wait for the next allowed run | Bounds model cost |
| Missed history | When the stored `historyId` has expired (Gmail 404), start again from now and show "mail between A and B was not checked" | Never replay old mail |
| Spool file handling | Write `tmp` with `O_CREAT|O_EXCL|O_NOFOLLOW`, then `rename`; read only regular files, never through symlinks, with a size cap; validate every field | Agents can plant symlinks or junk in the spool |
| Executor | A small Node script (Node is in the image, as letta-code needs it). It runs `letta` with an argument array (no shell), with the environment of PID 1 (`/proc/1/environ`) so `LETTA_LOCAL_BACKEND_*` match the server | Request text is untrusted; cron jobs get an empty environment |
| Sidecar user | uid 0 with `cap_drop: [ALL]`, `no-new-privileges`, read-only root filesystem | The state directories are root-owned (the app-server runs as root). Without `CAP_DAC_OVERRIDE`, root can write only what root owns, and only what is mounted. The vault's named volume lets it run as uid 10001; host paths would need a chown step |
| Code sharing | Move the vault's Access JWT + CSRF code (`broker/.../auth.py`) into a small shared package used by both images | It's security-critical; two copies would drift |
| Version tie | The executor records the `letta` CLI's exit status and stderr per request, and the page shows failures. `lettucectl upgrade` docs add a check of `letta cron add` | The CLI's flags are the interface, named in CLAUDE.md's dependency list |
| Starting turns | The one written exception to "no second way into an agent": through letta-code's scheduler only, for rules made on the Access-protected page, within each rule's hourly limit | Starting turns is the feature; this route adds nothing agents can't already do |

## Look and feel

Lettuce's visual language with the colours reversed. Lettuce has only a dark theme
(`upstream/web/src/styles.css`, `color-scheme: dark`), so the garden page is light-only.
Its filled controls take Lettuce's dark ink with the accent as text, the reverse of Lettuce's
accent fill with dark text.

Keep from Lettuce:
- square corners (`--radius: 0`) and pill-shaped badges only;
- 40px controls (16px font on touch);
- a 640px shell;
- `15px/1.5 system-ui`;
- menu-row toggles (a 20px square box, filled when on) instead of switches;
- bottom sheets for confirmations.

Tokens (`:root` in the sidecar's CSS):

| Token | Lettuce (dark) | Garden (reversed) |
|---|---|---|
| `--bg` | #0f1116 | #e7eaf0 |
| `--surface` | #171a21 | #f6f7fa |
| `--surface-2` | #1e222b | #ffffff |
| `--border` | #262b36 | #c5ccd8 |
| `--text` | #e7eaf0 | #0f1116 |
| `--muted` | #939cad | #4f5868 |
| `--accent` (fills) | #7aa2f7 | #0b1020 |
| `--on-accent` | #0b1020 | #7aa2f7 |
| `--accent-ink` (links, selected borders) | #7aa2f7 | #2c4fa8 |
| `--ok` / `--warn` / `--bad` | #4ade80 / #fbbf24 / #f87171 | #15803d / #a16207 / #b91c1c |

- Icon: Lettuce's rounded square reversed, an #7aa2f7 square with a #0b1020 glyph, drawn
  as PNGs in Python like the vault's `icons.py` (the vault's CSP forbids data URIs).
- `theme-color`: #e7eaf0.
- Check contrast to WCAG AA once the page exists.
- Put the CSS in the shared package (`garden.css`) so later sidecars, and the vault if
  wanted, can use the same file.
- Borrow upstream's UI gate: phone and desktop mockups before implementing, then a visual
  pass after.

## Pages

All pages sit under `/garden/gmail-triggers/` and each needs a verified Access JWT. POSTs
need CSRF and answer with a 303 redirect, like the vault. The page needs no JavaScript.

- **Triggers**: one row per rule, with an on/off toggle, last run time and outcome, and a
  "New trigger" button. A status banner covers:
  - Google not connected;
  - Gmail not allowed in Settings -> Google;
  - Google needs reconnecting (refresh failed with `invalid_grant`);
  - Lettuce not picking up requests (no executor heartbeat in the spool for 3 minutes);
  - the executor reporting CLI failures.
- **New / edit trigger** (one form):
  - Name.
  - **When mail arrives in**: labels from `labels.list`, Inbox included.
  - **Only from**: optional senders, one per line, plus the "verified sender" toggle.
  - **Ask**: an agent picker, from the executor's snapshot.
  - **In**: "a new conversation each time" or "the agent's main conversation".
  - **To**: the prompt, with two or three example prompts that fill the box on click (plain
    links to `?example=n`, no script).
  - **At most**: runs per hour.
  - A plain-language note on what the agent sees and doesn't see.
  - Delete, with a bottom-sheet confirmation.
- **History** (per rule): when it matched, how many emails, sender and subject, and what the
  executor reported (task created, or the error).
- **Send a test** (per rule): queues one request with "(test run, no email)" in place of the
  ids, so the user can see what the agent does.

## Executor API

Replaces the spool below (kept for the record). Bearer token, no `Origin`, JSON with `"v": 1`,
size-capped bodies:
- `GET /executor/v1/requests`: up to 20 pending requests, oldest first, each claimed at most once, plus `agents_wanted` (no agent list yet, older than 15 minutes, or the trigger form was opened since the last one).
- `POST /executor/v1/results`: `{id, ok, task_id | error}` for a claimed request.
- `PUT /executor/v1/agents`: the agent list from `letta agents list`.
- `POST /executor/v1/heartbeat`: `{letta_ok, error?}`; the page warns after 3 minutes without one.

## Spool (superseded)

`/spool` in the sidecar, `/root/.letta/garden/gmail-triggers/spool` in the app-server:

```
requests/<uuid>.json   sidecar -> executor: {v:1, id, rule_id, agent_id, conversation: "new"|"default",
                       name, prompt, created_at}; removed by the executor once handled
results/<uuid>.json    executor -> sidecar: {v:1, id, ok, task_id?, error?, at}; removed by the sidecar
                       once recorded in its own run log
agents.json            executor -> sidecar: [{id, name}] from `letta agents list`, every run
heartbeat              executor -> sidecar: last run time
```

The executor itself is `/root/.letta/garden/gmail-triggers/bin/run.mjs` (installed by lettucectl,
manifest-tracked like skills), outside the spool. The cron file is
`/root/.letta/system-cron/lettuce-garden-gmail-triggers`, copied to
`/etc/cron.d/` on install. The name has no dot, because cron ignores files with one.

## Code layout

```
garden-common/                 shared Python package (garden_common)
  src/garden_common/auth.py    moved from broker/src/secret_broker/auth.py
  src/garden_common/static/garden.css
gmail-triggers/                the sidecar (Python 3.12, Starlette, Jinja2, httpx)
  Dockerfile                   base / test / runtime stages, like broker/Dockerfile
  pyproject.toml               pinned dependencies
  src/gmail_triggers/
    config.py                  settings from the environment
    google.py                  read policy + creds, refresh the token in memory, Gmail calls
    store.py                   SQLite in /data: rules, historyId, handled message ids, run log
    matcher.py                 pure: history records + rules -> matches (labels, senders,
                               verified sender, own mail)
    spool.py                   safe request writes; result, agents and heartbeat reads
    triggers.py                prompt text, batching, hourly limits
    poller.py                  the 2-minute loop
    app.py                     pages, /healthz
    icons.py, templates/, static/
  executor/run.mjs             the in-app-server executor (Node, no dependencies)
  tests/
compose/gmail-triggers.yml     the service behind the gmail-triggers profile
docs/gmail-triggers.md         user guide and security model
```

The `garden-common` package reaches both images through Compose `additional_contexts` (and
`--build-context` in the test script). The vault's import paths change, but its behaviour
doesn't.

## Compose overlay (`compose/gmail-triggers.yml`)

One service, **`gmail-triggers`**:
- `profiles: ["gmail-triggers"]`;
- `user: "0"`, `read_only`, `tmpfs: /tmp`, `cap_drop: [ALL]`, `no-new-privileges`, memory and
  pids limits;
- volumes:
  - `${LETTA_STATE_DIR}/garden/gmail-triggers:/data`;
  - `${LETTA_STATE_DIR}/letta-home/garden/gmail-triggers/spool:/spool`;
  - `google-creds:/creds:ro`;
  - `google-policy:/policy:ro`;
- environment: the Access settings (`CF_ACCESS_*`, `ALLOWED_USERS`, `PUBLIC_ORIGIN`) and `TZ`;
- no ports, on the default stack network.

Nothing in the overlay touches `app-server`.

## lettucectl changes

- `compose()` in `lib/common.sh`: add `-f compose/gmail-triggers.yml` after `secrets.yml`.
  Update the CLAUDE.md invariant, which says four compose files.
- `cf_tunnel_configure` in `lib/cloudflare.sh`: when the profile is on, add
  `{path: "^/garden/gmail-triggers(/|$)", service: "http://gmail-triggers:8000"}` before
  the app-server rule. Turn the per-profile routes into one small table (profile -> path,
  service) so later garden sidecars add a row instead of another `if`.
- **Executor install**: after `up` with the profile on, install `gmail-triggers/executor/` and
  the cron file through `compose exec app-server`, the way `SKILLS_INSTALL` streams a tar.
  - Manifest-tracked, so a re-run replaces exactly what it installed.
  - `profiles` removes the executor and the cron file from every stack when the profile goes;
    the spool's requests stay until the profile comes back.
  - A stack restart keeps it: the files live in `letta-home`, and the entrypoint reloads
    `system-cron`.
- `check_stack`, plus a new `check_gmail_triggers` when the profile is on. It refuses:
  - `gmail-triggers` without `google`;
  - the service missing from the rendered config;
  - no `read_only`, or no `cap_drop: [ALL]`;
  - any bind mount other than exactly the two state paths above;
  - `google-creds` or `google-policy` mounted writable;
  - any service other than `gmail-triggers` mounting `$LETTA_STATE_DIR/garden`;
  - `google-creds` mounted by anything other than `bff`, `google-mcp` and `gmail-triggers`.
    This is new and also guards the existing setup.

  Share the "nobody else mounts this" check with `check_broker`.
- `cmd_remove`: say that the stack's triggers go with its state (`--purge`) and that the
  executor stops with the stack.
- No Google redirect URI changes.

## Tests

lettucectl (`tests/run.sh`):
- Stubs:
  - docker stub: `gmail-triggers) echo gmail-triggers ;;`;
  - a `triggers_config` helper, like `broker_config` / `with_secrets`, that writes a valid
    rendered config.
- `add` with the profile routes `/garden/gmail-triggers` before the app rule, and only
  with the profile.
- `profiles` adding and dropping the profile adds and removes the route on every stack,
  removes the container, and uninstalls the executor.
- The executor install script, run on a temp dir through the exec stub's saved script:
  - it installs the files and the cron file;
  - a re-run replaces only its own files;
  - uninstall removes both copies of the cron file.
- One `check` refusal test per rule above.
- The compose file order includes the new overlay.

Sidecar (pytest, no network):
- **Gmail fake**: Gmail through an `httpx.MockTransport`, plus a fake clock.
- **Matching**: labels, senders, verified sender, `SENT` and own address ignored.
- **Batching and limits**: one request per rule per poll; the hourly limit holds back
  matches and sends them later.
- **Restarts and history**: nothing is handled twice after a restart; an expired
  `historyId` resets and records the gap.
- **What reaches the agent**:
  - the request file never contains a fake body or snippet (fixed test value, asserted
    absent);
  - subjects lose newlines and are truncated.
- **Spool**:
  - a symlink planted at a request, result or `agents.json` path is refused and nothing
    outside `/spool` is read or written;
  - oversized or malformed results are ignored;
  - a missing heartbeat shows the banner.
- **Pages**:
  - every route refuses a missing or invalid Access JWT, GETs included;
  - POSTs refuse a missing CSRF token or a foreign Origin.
- **Google**:
  - disabled in policy means no Gmail calls;
  - a token refresh never writes to `/creds` (read-only in tests too);
  - `invalid_grant` shows "reconnect".

Executor (`node --test` in a pinned `node` image, with a fake `letta` on `PATH` that records
its argv):
- every field reaches `letta` as its own argument;
- shell metacharacters in the prompt stay literal;
- invalid ids, unknown conversation values and oversized prompts are refused with a result
  file;
- symlinks in `requests/` are skipped;
- a failing `letta` writes `ok: false` with its stderr;
- the environment comes from the given `environ` file.

Generalise `tests/run-broker.sh` into `tests/run-sidecar.sh <dir> [pytest args]` (plus the
executor tests for `gmail-triggers`), and update CLAUDE.md's commands.

## Steps

0. **Spike on a dev stack**, by hand, before writing the sidecar. Check the open questions
   below against the pinned letta-code (0.34.9). The CLI and scheduler code was read in a
   newer checkout.
1. **Shared package**: move `auth.py` into `garden-common`; the vault uses it; vault tests
   pass unchanged.
2. **Sidecar core**: `google.py`, `store.py`, `matcher.py`, `spool.py`, `triggers.py`,
   `poller.py`, and the executor, with their tests.
3. **Pages and theme**: mockups (phone and desktop) first, then templates, `garden.css`,
   icons, and the page tests.
4. **lettucectl**: overlay, route table, executor install and uninstall,
   `check_gmail_triggers`, and the lettucectl tests. Run `shellcheck`.
5. **Docs**:
   - `docs/gmail-triggers.md` (user guide plus security model, including what agents can do
     to the spool);
   - README profile list and `docs/operations.md`;
   - CLAUDE.md layout, invariants and commands, including the `/garden/<sidecar>` route
     convention and "garden sidecars never open an app-server connection";
   - CLAUDE.md "Relationship to upstream": the exception, the route's reason and the named
     dependencies, worded as in "This repo's rules, checked". This text lands in the same
     change as the code, never before it.

   Optionally, a generic agent skill telling agents to send people to the page when they
   ask to react to incoming mail.
6. **End-to-end on a real stack**: label an email, see the task in the Tasks tab and the
   agent's reply. Toggle Google off and see polling stop. Restart the stack and see the
   executor still running.

## Open questions for the spike

- Run `letta cron add --at "in 1m" --once --conversation new` from a cron job inside the
  app-server, with PID 1's environment. Check that:
  - the running scheduler picks it up without a restart;
  - it fires on the BFF's connection;
  - the run shows in the Tasks tab.
- Does `--conversation default` (or omitting it) target the agent's main conversation? Is
  there a CLI way to list conversations, so a rule could target an existing one?
- Does `letta agents list` behave from a cron job the way it does under `compose exec`?
- Do fired one-off tasks pile up in the Tasks tab, or does the scheduler clear them out?
- Are scheduled runs held for tool approval, and if so, does the user see the request in
  Lettuce for a conversation created by the run?
- Does the entrypoint's copy of `system-cron` and a running cron daemon still hold in the
  pinned image?

## Not in scope

- Renaming the project, or moving `/secrets/` under `/garden/`.
- Gmail push (Pub/Sub); polling every 2 minutes is enough.
- Rules that reply, label or archive by themselves; the agent does that, under Settings ->
  Google's permissions.
- Other mail providers.
- An upstream change letting the BFF accept triggers itself. It's the cleanest fit for
  upstream's architecture, but it belongs to Lettuce, not lettuce-multi-user.
