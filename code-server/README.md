# code-server

The code editor: VS Code in the browser ([code-server](https://github.com/coder/code-server)) on a stack's agent workspaces, at `https://<name>-garden.<domain>/code-server/`. This folder is three images. The folder itself is the gate (`garden-code-server`, the Python package `code_server_gate`), the only way to the editor. `editor/` is the editor (`garden-code-server-editor`). `exec/` is the shell (`garden-code-server-exec`), where terminals and Source Control's git run. All three are behind the `garden-code-server` profile. The user guide and security model are in [docs/code-server.md](../docs/code-server.md); the design record is [docs/code-server-plan.md](../docs/code-server-plan.md).

![The code editor's containers, networks, volumes and what they reach](../docs/diagrams/code-server-architecture.svg)

An interactive version, [code-server-architecture.html](../docs/diagrams/code-server-architecture.html), opens in a browser from a clone of this repo.

## Files

| Path | What it does |
|---|---|
| `Dockerfile`, `pyproject.toml` | The gate's image: Python 3.12 slim, `garden-common` from its build context, pinned dependencies, a `test` stage; runs as uid 65534 on port 8000. |
| `src/code_server_gate/config.py` | Settings from the environment; `PREFIX` (`/code-server`) and `EDITOR_SOCKET` (`/run/code-server/editor.sock`). |
| `src/code_server_gate/app.py` | The gate: the Access JWT on every request and WebSocket, Fetch Metadata, the prefix, request and response header allow-lists, which responses may load as pages, and the HTTP and WebSocket proxy over the editor's socket. |
| `src/code_server_gate/__main__.py` | Starts uvicorn; keeps httpx and access logs off, since the editor's URLs carry file paths. |
| `src/code_server_gate/templates/error.html` | The gate's own error page (no script). |
| `tests/` | Gate tests (pytest): `FakeEditor` plays the editor over `httpx.MockTransport` and a fake WebSocket peer. |
| `tests/e2e/` | Run by `scripts/code-server-e2e.sh`, not by pytest: `check_gate.py` (the gate against the real editor), `serve_gate.py` (the gate as cloudflared would feed it, for `--browser`) and `browser_check.py` (drives the workbench in Chromium). |
| `editor/Dockerfile` | code-server pinned by version and SHA-256 per architecture; built-in extensions pruned to `extensions-kept`; the Markdown preview pinned to Strict and its Markdown Editor removed; the git extension's two trust calls resolved to true; no git, no `node` on `PATH`, an empty `/etc/shells`, `exec-shell` as root's login shell. Each edit fails the build when a release no longer matches. |
| `editor/entrypoint.sh` | On every start: makes the SSH keys once, rewrites `authorized_keys` and `known_hosts`, puts the fixed settings back over the person's, removes a stale socket, sets `SHELL` and `VSCODE_CLI=1`, then runs code-server with umask 022. |
| `editor/settings.json` | The fixed settings: Restricted Mode for good, the two extensions allowed in it, `git.path`, the terminal profiles. |
| `editor/config.yaml` | The read-only config file code-server reads (`--config`) instead of one on its volume. |
| `editor/extensions-kept` | The allow-list of built-in extensions, with why the rest are left out. |
| `editor/ssh_config` | The SSH client: one host, the editor's key, the pinned host key, nothing forwarded, one shared connection. |
| `editor/bin/exec-shell` | The terminal profile: prints the notice, then `ssh -tt` into the shell, in the open folder (or `/work`), through `agent-env`. |
| `editor/bin/shell-git` | `git.path`: runs each git call over SSH in the shell, in the same folder (or `/`), passing on `GIT_OPTIONAL_LOCKS`; opens the shared connection once under a lock. |
| `editor/tests/test.sh` | The editor image's tests, run in its `test` stage with fake `ssh`, `agent-env` and code-server. |
| `exec/Dockerfile` | The shell: `FROM base` (the app-server's image) plus `openssh-server`, running `sshd -D`. |
| `exec/sshd_config` | Key login as root only, keys from `/keys`, nothing forwarded, no environment accepted, `MaxSessions 64`. |

## How it plugs in

- **Overlay:** `compose/code-server.yml`, merged for every stack; it adds nothing without the profile.

  | Service | Networks | Mounts | Hardening |
  |---|---|---|---|
  | `garden-code-server` (gate) | `default` | `code-server-sock` at `/run/code-server`, read-only | read-only root, no capabilities, uid 65534 |
  | `garden-code-server-editor` | `code-exec` | `$LETTA_STATE_DIR/workspaces` at `/work`, `code-server-data` at `/data`, `code-exec-keys` at `/keys`, `code-server-sock` at `/run/code-server` | read-only root, no capabilities, root (agents write the workspaces as root), no port |
  | `garden-code-server-exec` (shell) | `code-exec`, `code-egress` | `$LETTA_STATE_DIR/workspaces` at `/work`, `$LETTA_STATE_DIR/tools` at `/opt/agent-tools` (both read-write, as in the app-server), `code-exec-keys` at `/keys`, read-only | Docker's default capabilities minus `NET_RAW`, `init: true`; starts once the editor is healthy (its socket and `authorized_keys` exist) |

  `code-exec` is internal; `code-egress` is the shell's only way out, directly, not through pipelock.
- **Request path:** the tunnel sends `^/code-server(/|$)` on the garden host to the gate on port 8000 (`PROFILE_ROUTES` in `lib/cloudflare.sh`). The gate strips the prefix and talks to `http://garden-code-server-editor` over the Unix socket (httpx for HTTP, `websockets` for WebSockets), with `X-Forwarded-Host` set to the garden host; it puts the prefix back on redirects.
- **Terminals and git:** `exec-shell` and `shell-git` run `ssh -F /opt/lettuce-editor/ssh_config garden-code-server-exec` over `code-exec`; the remote command is `cd` into the folder, then `agent-env`, so the shell gets the container's `PATH` and package managers.
- **Environment:** the gate reads `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS` and `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`). The editor gets `EXTENSIONS_GALLERY={}` (no marketplace). Nothing new goes in `stack.env`.
- **The garden:** with this profile on, the landing page ([garden/](../garden/README.md)) lists the agents, each a link to `/code-server/?folder=/work/<agent-id>`, and `sync_executor` installs the garden executor to send it the agent list.
- **lettucectl:** `up` builds the three images. `check_stack` runs `check_code_server` when the profile is on, and whatever the profiles refuses another service mounting `code-server-data`, `code-exec-keys` or `code-server-sock`, or sharing one of the three services' network namespaces; `check_garden_audience` holds the gate to the garden's Access application. In short: the gate a hardened sidecar on `default` with only the socket volume, read-only; the editor hardened, on `code-exec` only, with its exact mounts and the command-line flags that matter exactly once (and none of the ones that would open it up); the shell isolated, without `NET_RAW`, with its exact mounts and networks; nobody else on `code-exec` or `code-egress`; `code-exec` internal; no symlinks on the way to the workspaces or tools. The full list is in [AGENTS.md](../AGENTS.md), "Key invariants".

## Build and test

```bash
tests/run-sidecar.sh code-server            # the gate's pytest, plus the editor image's tests when no args are given
scripts/code-server-e2e.sh [--browser]      # the real images: the gate against the real editor, the terminal's way into the shell,
                                            # the editor's isolation; --browser drives the workbench in Chromium (needs Docker)
shellcheck -x code-server/editor/bin/* code-server/editor/entrypoint.sh code-server/editor/tests/test.sh
```

`lettucectl up` builds the images; there is no other build step. Run `tests/run-sidecar.sh code-server` before changing this folder, and `scripts/code-server-e2e.sh --browser` before keeping a new built-in extension or setting. Moving to a new code-server release is described in [docs/code-server.md](../docs/code-server.md), "For the operator".

## Upstream seams

These are named in [AGENTS.md](../AGENTS.md), "Relationship to upstream"; `lettucectl upgrade` must check them:

- `exec/` is built `FROM` the app-server's image (`additional_contexts: base: service:app-server`), so it depends on that image being Debian with `apt`, `bash` as root's shell, the image `ENV` that points `PATH`, `NPM_CONFIG_PREFIX`, `CARGO_HOME`, `GOBIN` and the `UV_*` variables at `/opt/agent-tools`, and `/usr/local/bin/agent-env` reading PID 1's environment; and on the workspaces at `$LETTA_STATE_DIR/workspaces` and the tools at `$LETTA_STATE_DIR/tools`, mounted as the app-server mounts them.
- The landing page's agent links depend on code-server's `?folder=` query and the workspaces at `/work/<agent-id>`.
