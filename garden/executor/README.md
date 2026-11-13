# Garden executor

The garden executor, `garden/executor/run.mjs`, is how the garden sidecars get anything done inside Lettuce's app-server. (Other code from this repo runs there too, at an agent's call: `vault-run` and the document tools.) The garden sidecars never connect to Lettuce; once a minute the executor asks them over HTTP on the stack network what they want, runs `letta` for them and sends the results back. It gives the landing page ([garden/](../README.md)) the agent list and turns the requests of Gmail triggers ([docs/gmail-triggers.md](../../docs/gmail-triggers.md)) and News Radar ([docs/news-radar.md](../../docs/news-radar.md)) into one-off letta-code tasks. This page is the maintainer's reference for how it is installed and what it says to whom. The diagram is in [garden/README.md](../README.md).

## Install

**The rule:** the executor is installed by `up`, only through `compose exec`, and the install replaces only its own files. `lettucectl` installs it; nothing else writes it. `sync_executor` runs after `compose up` in `up` and `add`, and on every running stack in `profiles`:

- **When:** with `garden-gmail-triggers`, `garden-news-radar` or `garden-code-server` on (`executor_wanted`). The vault alone gives it no work. Otherwise it uninstalls when the host shows an install (`executor_installed`: the `bin` directory or the system-cron file under `letta-home`). `profiles` skips a stopped stack, whose next `up` installs or removes it.
- **How:** a tar of `run.mjs` and the token (built in a private temp directory, never in argv) is streamed to `compose exec -T app-server sh -c "$GARDEN_INSTALL" sh install ...`. The in-container script puts each file in place by a rename from a dot file next to it, and changes nothing unless the stream holds both files. A failed install warns and fails `up`/`add`/`profiles` at the end, not in the middle.
- **The token:** `GARDEN_EXECUTOR_TOKEN` in `stack.env`, 64 lowercase hex characters, made once by `ensure_executor_token` (in `up` and on every `profiles` run) and never rotated. To replace it, delete the line and run `lettucectl up <name>`. `garden`, `gmail-triggers` and `news-radar` get it from `stack.env` through Compose.

What ends up in the app-server:

| Path | Mode | What |
|---|---|---|
| `/root/.letta/garden/executor/bin/run.mjs` | 0644 | The executor, on the `letta-home` bind mount, so a restart keeps it. |
| `/etc/lettuce-garden/executor-token` | 0600 (directory 0700) | The token, in the container layer: not under `state/`, not in backups. `up` reinstalls it after every recreate. |
| `/root/.letta/system-cron/lettuce-garden-executor` | 0644 | The cron file, named without a dot because cron skips such files; upstream's entrypoint copies `system-cron` to `/etc/cron.d` at every start. Since Lettuce v0.26 the BFF also lists every file there, read-only, in the Tasks tab; with no `# agent:` line, this one shows under every agent. |
| `/etc/cron.d/lettuce-garden-executor` | 0644 | The same file, written too so it starts now. |
| `/root/.letta/garden/executor/executor.log` | | Its output; truncated once it passes 256 KiB. |
| `/root/.letta/garden/executor/.executor.lock` | 0700 | A lock directory, held for one run; taken over when older than 10 minutes. |

The cron line (`executor_cron`), with `--gmail-triggers` and `--news-radar` exactly when their profiles are on:

```
* * * * * root /usr/local/bin/agent-env env -u NODE_USE_ENV_PROXY node /root/.letta/garden/executor/bin/run.mjs --gmail-triggers >> /root/.letta/garden/executor/executor.log 2>&1
```

`agent-env` (upstream's) gives it PID 1's environment, so `node` and `letta` are on `PATH`. It drops `NODE_USE_ENV_PROXY`, which `garden-pipelock` sets: everything the executor calls is inside the stack, its token must never reach pipelock, and Node (from 22.21) would log a warning on every run. Uninstall removes both cron files, the token (and its directory once empty) and `bin/`.

## A run

1. Take the lock, or stop if another run holds it.
2. Accept the queue flags, `--gmail-triggers` and `--news-radar`, each at most once and in any order (`QUEUES` in `run.mjs`); anything else stops the run before any call.
3. Read the token: a regular file (no symlink, no FIFO), at most 1 KiB, 64 hex characters. The token is never logged.
4. Find `letta` on `PATH`; without it, send each flagged queue a heartbeat with `letta_ok: false` and stop. `letta` runs in `$HOME`, or `/`.
5. `GET` garden's status.
6. For each flagged queue, gmail-triggers first, then news-radar: `GET` its requests, which claims them. For each, validate it, run `letta cron add`, and `POST` the result to that sidecar.
7. When any answer said `agents_wanted: true`: run `letta agents list --limit 1000` once and `PUT` the list to each sidecar that answered (garden when step 5 succeeded, each queue whose requests step 6 claimed).
8. To each queue whose requests step 6 claimed: `POST` a heartbeat, with the agent list's error if it failed. A queue whose requests could not be claimed gets none, so once its last heartbeat is older than `HEARTBEAT_STALE_SECONDS` its page says Lettuce isn't picking up requests.
9. Release the lock.

One sidecar being unreachable never stops what the run does for the others, and one request failing doesn't stop the batch.

## The API

The sidecars serve it under `/executor/v1/` (`garden_common/executor.py`: `guard`, and `request_routes` for the request queues), at `http://garden:8000`, `http://garden-gmail-triggers:8000` and `http://garden-news-radar:8000`. The tunnel never routes that prefix. Every call:

- needs `Authorization: Bearer <GARDEN_EXECUTOR_TOKEN>` (compared in constant time; 401 otherwise);
- is refused (403) with an `Origin` header or any of Cloudflare's `cf-access-jwt-assertion`, `cf-connecting-ip` and `cf-ray`, and (404) when the path as sent does not start with `/executor/v1/`;
- carries JSON with `"v": 1` both ways (bodies up to 16 KiB, the agent list up to 1 MiB); answers are `no-store`.

The executor gives each call 15 s, reads at most 2 MiB of an answer and follows no redirect.

| Sidecar | Call | Body | Answer |
|---|---|---|---|
| garden | `GET /executor/v1/status` | | `{"v": 1, "agents_wanted": bool}`: true only with `garden-code-server` on and a list wanted (below) |
| garden | `PUT /executor/v1/agents` | `{"v": 1, "agents": [{"id", "name"}]}` | 204 |
| gmail-triggers | `GET /executor/v1/requests` | | `{"v": 1, "requests": [...], "agents_wanted": bool}`; up to 20 requests, each handed out once |
| gmail-triggers | `POST /executor/v1/results` | `{"v": 1, "id", "ok": true, "task_id"}` or `{"v": 1, "id", "ok": false, "error"}` | 204; 404 for an id that is not a claimed request |
| gmail-triggers | `PUT /executor/v1/agents` | as for garden | 204 |
| gmail-triggers | `POST /executor/v1/heartbeat` | `{"v": 1, "letta_ok": bool, "error"?}` | 204 |
| news-radar | the four calls of gmail-triggers | the same | the same; a request is one report |

**Requests.** Each needs an `id` of 32 hex characters (otherwise it is skipped; a repeated id in one batch is skipped too), an `agent_id` shaped like `agent-[A-Za-z0-9-]{1,80}`, a `conversation` of `new`, `default` or a conversation id shaped like `[A-Za-z0-9][A-Za-z0-9_-]{0,99}` (News Radar sends the one a monitor was set up in; Gmail triggers sends only `new` or `default`), a `name` (up to 100 characters) and `description` (up to 200) without control characters, and a `prompt` (up to 8000, control characters allowed, no NUL). A request that fails validation gets `ok: false` with the reason, which never quotes the values. A valid one becomes, as an argument array with no shell:

```
letta cron add --name=<name> --description=<description> --prompt=<prompt> --at=in 1m --once --agent=<agent_id> --conversation=<new|default|conversation id>
```

The `--flag=value` form is needed because letta's argument parser refuses a separate value that starts with a dash. The result's `task_id` is the `.id` of the JSON letta prints (up to 200 characters, no control characters); on failure, `error` is letta's stderr cut to 500 characters, or a short reason. letta-code's scheduler then starts the task on the BFF's connection; the executor opens none.

**The agent list.** From `letta agents list`'s `.items[]`: entries with a string `id`, names cut to 200 characters, at most 1000. The receiving side (`read_agents`) also drops ids not shaped like agent ids, overlong names and repeated ids, and makes each name one line (the id when nothing is left).

**When a list is wanted.** Each sidecar keeps an `AgentAsks` state: it wants a list when it has none, when its list is older than 15 minutes (`AGENTS_FRESH_SECONDS`), or when a page asked for one since the executor last read its status. garden asks when the landing page is opened, gmail-triggers when its trigger form is shown. Reading the status marks the asks so far as seen; the list that follows answers only those.

## Trust

Agents are root in the app-server, so they can read the token and the installed script and call the API themselves: read, claim or forge what the executor sees, or answer in a sidecar's place. The token keeps out the stack's other containers, not agents. So both sides treat the other's data as untrusted: the executor validates every field (of a conversation id only its shape: letta-code decides whether that conversation exists and whether it is the named agent's, and a sidecar naming one gets no more than an agent has) and runs `letta` from an argument array, the sidecars validate every body, and garden shows agent names only as escaped text and links only to ids shaped like agent ids. A claimed request is never offered twice. For Gmail triggers a lost turn beats a duplicate; News Radar puts the items of a failed or lost report into the next one, since a repeated report is harmless. **The sidecars and the executor share no directory.** A bind source inside `letta-home` would be agent-writable, and an agent could swap a component for a symlink that Docker follows on the host; so they talk over HTTP on the stack network. This holds for `garden`, `gmail-triggers` and `news-radar` alike.

The executor starts turns only through letta-code's own scheduler, the written exception to "no second way into an agent" ([CONTRIBUTING.md](../../CONTRIBUTING.md#relationship-to-upstream), [gmail-triggers/README.md](../../gmail-triggers/README.md#rules), [news-radar/README.md](../../news-radar/README.md#rules)). What people are told: the security models in [docs/gmail-triggers.md](../../docs/gmail-triggers.md#security-model) and [docs/news-radar.md](../../docs/news-radar.md#security-model).

## Build and test

```bash
node --test 'garden/executor/tests/*.test.mjs'   # on the host: local node:http servers play garden, gmail-triggers and news-radar, a fake letta records its argv
tests/run-sidecar.sh garden                      # the garden service's tests, plus the executor's in node:22.22.0-slim
tests/run-sidecar.sh gmail-triggers              # the other end of the Gmail request and claim protocol
tests/run-sidecar.sh news-radar                   # the other end of News Radar's report queue
tests/run.sh executor                            # the CLI's install, removal and cron (stubbed docker)
```
There is no build step: `sync_executor` installs `run.mjs` as it is.

Overrides for tests only: `GARDEN_URL`, `GT_URL`, `NR_URL`, `GT_TOKEN_FILE`, `GT_LOCK`, `GT_LOG`.
