# Code editor (code-server): implementation plan

Status: implemented on branch `vscode` (2026-10-27), steps 0-5; step 6, a real stack, is still to
do. The source review below was checked at runtime (step 0, results at the end), which changed two
things: the git extension is out (Source Control asks to trust each repository's folder), and
terminals are allowed in untrusted workspaces by setting. A code review after the first real stack
(2026-10-27) moved the editor off the network: the plan below gives it a port on an internal network
shared with the gate, but the shell, on the editor's other network, could reach that port too, and
code-server has no Access check of its own. It now listens only on a Unix socket that only the gate
mounts (compose/code-server.yml). docs/code-server.md is the user guide and security model.

## Goal

Let each person on a stack open their agents' workspaces in VS Code in the browser, from anywhere,
without SSH access to the host: browse and edit files, search, diff, and a terminal (git included) with
the same tools the agents have. It lives at `https://<stack>/garden/code-server/`, next to the other
garden pages, behind the stack's own Cloudflare Access login.

`lettucectl shell` and `toolbox` already do this, but only for the operator, on the host. This is
the person-facing version.

Everything ships in lettuce-garden. Upstream Lettuce and letta-code are not changed.

## The model: an editor that runs nothing, and a shell next to it

The editor shares an origin with Lettuce and the vault (one hostname per stack). Whatever controls
the editor's server process controls script on that origin: it serves the workbench, and code-server
serves webviews from the same origin on purpose (`patches/webview.diff`, "Serve webviews from the
same origin"), so any code in its extension host can open a webview that runs as the person.
Agents write the workspaces. So the design rests on one rule: **nothing an agent wrote ever runs
in the editor's container.**

That splits the feature in two:
- **The editor** (`code-server-editor`): code-server and its own state, nothing else. It edits files
  and runs the built-in language features. It holds no toolchain, no shell profiles of its own and
  no workspace code, and it never trusts a workspace.
- **The shell** (`code-server-exec`): the agents' toolchain, built on the app-server's image, with
  the workspaces and the agents' tools mounted as in the app-server. Every terminal runs here over
  SSH, and git runs from the terminal. It is `shell`'s model: exactly as strong as an
  agent, holding nothing an agent can't already get. No logins: no GitHub or Copilot, no Claude
  token, no Settings Sync. Pushing to a remote stays a `toolbox` job.

`shell` itself is a `compose exec` into the app-server. The editor can't use it: that needs the
Docker socket, and an SSH server in the app-server would change an upstream service. So the shell
is its own container.

## Why code-server

Checked on 2026-10-26:

| | code-server (Coder) | openvscode-server (Gitpod) | Microsoft `code serve-web` |
|---|---|---|---|
| Licence | MIT | MIT | VS Code Server licence: may not "host, share, publish, rent or lease the software" or provide it "for others to use" |
| Releases | v4.143.1 on 2026-10-23 (Code 1.143.1); 15 releases since 2026-07-17, about weekly | v1.109.5 on 2026-02-20; nothing since. Its forks (namespacelabs, oyren-ai) are still on 1.109.5 | n/a |
| Sub-path | Relative asset paths (`patches/base-path.diff`); needs a proxy that strips the prefix | `--server-base-path` | `--server-base-path` |
| Port proxy | `/proxy/<port>/`, `/absproxy/<port>/`; off with `--disable-proxy` (403 on every proxy route, checked in v4.143.1's `src/node/http.ts` and its tests) | n/a | n/a |
| Extensions | Open VSX, or `EXTENSIONS_GALLERY` | Open VSX; extension signature checks off in every release (open issue) | Microsoft marketplace |

An internet-facing editor needs current security fixes, so code-server it is.

## What keeps agent-written code out of the editor

Source review of code-server v4.143.1 (VS Code 1.143.1, commit `e646868`) and its Linux release
tarball, 2026-10-27. VS Code runs workspace-controlled programs through four doors; each is closed
by something agents can't change.

1. **Tasks, debugging and workspace terminal settings: Restricted Mode, permanently.**
   - The task service and the debug service both refuse to start without workspace trust
     (`abstractTaskService.ts` `_trust()`, `debugService.ts`). This matters because `tasks.json`
     decides where a task runs: `options.shell.executable` replaces the automation profile, and a
     `process` task runs its command directly in the editor's pty host
     (`terminalTaskSystem.ts`, `_createShellLaunchConfig`). No setting turns tasks off; only
     Restricted Mode does.
   - Restricted Mode ignores workspace values of `restricted` settings. Terminal profiles, the
     default and automation profiles, `terminal.integrated.env.*` and `terminal.integrated.cwd` are
     all `restricted` (`terminalPlatformConfiguration.ts`, `terminalConfiguration.ts`).
   - Restricted Mode normally disables the TypeScript extension (`untrustedWorkspaces:
     {supported: false}`). `extensions.supportUntrustedWorkspaces` overrides that per extension,
     and it is application-scoped, so only the editor's own user settings can set it, never a
     workspace (`extensions.contribution.ts`). The editor sets it to `true` for
     `vscode.typescript-language-features` only.
   - A terminal asks for workspace trust before it starts (`terminalInstance.ts`,
     `_ensureWorkspaceTrustedForProcess`), with a "Trust Folder & Continue" button; in a remote
     workspace like code-server's it starts anyway when refused. The editor sets
     `terminal.integrated.allowInUntrustedWorkspace` (a `restricted` setting, so a workspace can't
     touch it) so that no terminal ever offers trust: its shell runs in the shell container, where a
     workspace's code is expected to run.
   - The editor never trusts: `security.workspace.trust.startupPrompt: never`, banner always on,
     never `--disable-workspace-trust`. The person can still click "Trust" (code-server leaves
     `canSetWorkspaceTrust()` true; running a task asks), and that reopens doors 1 and 3. The choice
     is stored at `APPLICATION_SHARED` scope, which code-server keeps in the browser (after a trust,
     the server's `state.vscdb` held none), so the editor can't drop it at start. The docs say
     never to trust, and how to undo it.
2. **Extensions that execute things: not in the image.** The image deletes every built-in extension
   not on an allow-list (`code-server/editor/extensions-kept`), so a new built-in in a later release
   is out until we add it. `--extensions-dir` points at an empty read-only directory, so nothing can be installed,
   and the marketplace is off (`EXTENSIONS_GALLERY={}`: the workbench's product configuration
   then carries an empty gallery).
   - **Kept:** language grammars and themes, `typescript-language-features`, `json-`, `html-` and
     `css-language-features`, `emmet`, `git-base` (grammars only), `merge-conflict`, `references-view`,
     `search-result`, `media-preview`, `configuration-editing`.
   - **Left out**, with the reason found in the tarball:
     - they spawn processes: `npm`, `grunt`, `gulp`, `jake` (task providers), `php-language-features`
       (runs `php.validate.executablePath`, which a workspace can set), `terminal-suggest`,
       `tunnel-forwarding`;
     - debugging: `ms-vscode.js-debug` and `-companion`, `debug-auto-launch`, `debug-server-ready`;
     - they render workspace content in same-origin webviews: `markdown-language-features`,
       `markdown-math`, `mermaid-markdown-features`, `ipynb`, `notebook-renderers`,
       `ms-vscode.vscode-js-profile-table`, `simple-browser`;
     - logins and outside services: `copilot`, `prompt-basics`, `github`, `github-authentication`,
       `microsoft-authentication`, `extension-editing`;
     - `git`: see door 4.
   - `emmet` stays: a workspace can set `emmet.extensionsPath` even in Restricted Mode, but the
     helper only reads `snippets.json` and `syntaxProfiles.json` from it as JSONC.
3. **TypeScript: only the bundled version, only bundled plugins.**
   - A workspace's own TypeScript (`typescript.tsdk`) and Node (`typescript.tsserver.nodePath`) are
     used only in a trusted workspace (`versionManager.ts`, `nodeManager.ts` check
     `vscode.workspace.isTrusted`). The bundled TypeScript is 6.0.3.
   - Plugins named in a `tsconfig.json` are loaded only from the extension probe locations and the
     bundled `node_modules`. The project's directory is searched only with
     `--allowLocalPluginLoads`, which the extension never passes (`spawner.ts`), and plugin names
     must be bare package names (TypeScript's `enablePlugin`). `typescript.tsserver.pluginPaths` and
     `typescript.npm` are machine-scoped, so a workspace can't set them.
   - Automatic type acquisition is off in the editor's settings. A workspace can turn it back on (the
     setting is window-scoped), but it then needs `npm`, which the editor image doesn't have, and
     the network, which the editor isn't on.
4. **git: from the terminal only.** The plan was a `git.path` wrapper running git in the shell over
   SSH, with the git extension allowed in Restricted Mode. The browser spike showed that VS Code
   1.143's git extension calls `requestResourceTrust` before opening any repository (`model.ts`,
   `openRepository`), with no setting to skip it: every folder with a repository opens on a "Trust
   Folder & Continue" dialog, and trusting it reopens doors 1 and 3. Source Control and permanent
   Restricted Mode can't coexist, so the git extension is not in the image. git runs from the
   terminal, in the shell container, where hooks, `core.fsmonitor`, filters and the rest of a
   repository's config run where agents already run.

The editor image itself has no toolchain: no `node` on `PATH` (code-server's own sits under its
install directory), no `npm`, no compilers, an empty `/etc/shells` (so VS Code detects no local
shell profiles) and `openssh-client` for the terminal.

Formatting uses the built-in formatters only (TypeScript/JavaScript, JSON, HTML, CSS/SCSS/LESS),
which run in the extension host from the image. Prettier, Black, ESLint and the like run from the
terminal (`npx prettier --write .`), in the shell container. Debugging also happens in the terminal
(`node inspect`, `python -m pdb`, `dlv`): VS Code runs debug adapters in the extension host, and a
launch configuration is workspace data, so the editor has no debugger. A debug adapter running in
the shell container would need an extension of our own; not in this plan.

## The remaining same-origin risk, in layers

Script in the editor's tab can do whatever the person can do on the origin: drive Lettuce, change
Settings, approve tool calls, read a vault page's form token and post an approval. With the split
above, agents can no longer put script there through the editor's server. What is left is a bug in
code-server or a kept built-in (rendering crafted file content, git or terminal output), and a
person trusting a folder. Three layers, none of them a boundary against a code-server bug; the docs
say so, as they do for pipelock.

1. **Keep untrusted script out of the editor's tab.** The split and the allow-list above, plus the
   gate's response rules (below): only the workbench and code-server's webview host pages may load
   as documents, and every other response is served `nosniff` with `Content-Security-Policy:
   sandbox`, so a file route (`/vscode-remote-resource?path=...`) can't serve an agent's HTML or SVG
   as a page on the origin. code-server is kept current: its pin moves with its weekly releases.
2. **Make the garden pages refuse same-origin script.** This goes in `garden-common`, so the vault,
   gmail-triggers and the landing page all get it. It also protects them from script that gets into
   Lettuce itself, so it is worth doing on its own and lands first. It relies only on headers
   browsers set and scripts can't change:
   - Page `GET`s require `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. No
     `Sec-Fetch-Site` rule: after an Access re-login the page arrives through a cross-site redirect
     chain. A `fetch()` from the editor's tab can no longer read a page or its form token.
   - Form posts require `navigate`/`document` and `Sec-Fetch-Site: same-origin`.
   - The vault's own `fetch()` endpoints (the push subscription in `static/app.js`) keep
     `same-origin`/`cors` mode. They still need the form token, which a script now can't read.
   - Every garden page sends `Cross-Origin-Opener-Policy: same-origin`. The editor's pages must
     not (the gate never adds it): two same-origin documents with different COOP values land in
     separate browsing context groups, so a garden page the editor's tab opens in a popup is cut off
     from it.
   - `frame-ancestors 'none'` is already set on both sidecars' pages and stays.
   - A request without `Sec-Fetch-*` headers is refused. Every browser Lettuce supports sends them;
     agents calling `/mcp` and `/redeem` don't touch these routes.
   - The Access application's cookie gets `http_only_cookie_attribute: true` and
     `same_site_cookie_attribute: lax` (`cf_app_create` sets neither today), so script on the origin
     can't read the person's Access token and hand it to an agent, which could then approve in the
     vault directly. `add` sets them; existing stacks get them from `profiles` (it already
     re-applies every stack's routes); `check` reads them back.
3. **Lettuce itself is not ours to harden.** Script in the editor's tab can still drive Lettuce as
   the person: the BFF checks `Origin` on WebSocket upgrades only, and its HTTP routes rely on the
   `SameSite=Lax` session cookie, which a same-origin request carries. The docs state the rule
   plainly: the editor's tab is as trusted as Lettuce's own, never trust a folder in it, and nothing
   may be installed into it. Fetch Metadata checks and COOP in Lettuce would fit upstream; we say so
   there and stop.

## How it works

```
 browser --Access--> tunnel: ^/garden/code-server(/|$) -> code-server:8000
                                                   |
   code-server (the gate; Python, garden-common; networks default + code-editor)
     - verifies the Access JWT on every request and every WebSocket upgrade, and closes
       WebSockets when the JWT expires
     - WebSocket upgrades: Origin must be exactly PUBLIC_ORIGIN
     - strips /garden/code-server, adds it back to redirects
     - forwards an allow-list of request headers (never Cookie or cf-access-jwt-assertion)
       and of response headers (never Set-Cookie or COOP; Service-Worker-Allowed narrowed to
       the prefix)
     - lets only the workbench and the webview host load as documents; nosniff and
       CSP sandbox on everything else
     - serves /garden/ (landing_routes) like the other garden sidecars
                                                   |
   network code-editor (internal: the gate and the editor only)
                                                   |
   code-server-editor:8080   image lettuce-code-server-editor:local (Debian slim, code-server
       |   pruned to the allow-list, ssh client)
       |   --auth none --disable-proxy --disable-telemetry --disable-update-check
       |   --disable-getting-started-override --extensions-dir <empty, read-only>
       |   --user-data-dir /data/user --config <the image's, read-only>;
       |   EXTENSIONS_GALLERY={}
       |   /work  <- state/workspaces (rw; the editor writes files, runs none)
       |   /data  <- volume code-server-data (settings, state, the SSH client key)
       |   /keys  <- volume code-exec-keys (rw: writes the shell's host key and authorized_keys)
       |
   network code-exec (internal: the editor and the shell only)
       |   terminals: ssh -tt, through agent-env
       |
   code-server-exec:22   image lettuce-code-server-exec:local (FROM the app-server's image + openssh-server)
       |   /work             <- state/workspaces (rw, as in the app-server)
       |   /opt/agent-tools  <- state/tools (rw, as in the app-server)
       |   /keys             <- volume code-exec-keys (ro)
       |
   network code-egress (not internal: the shell only) --> internet, for npm, pip, git fetch

 Agents (app-server, BFF namespace) are on `default`: they reach the gate, which wants a JWT
 they don't have, and never the editor or the shell.
```

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Address | `/garden/code-server/` on the stack's hostname, a `PROFILE_ROUTES` row like the other garden sidecars | One hostname per stack; no new DNS, Access or certificate state |
| Separate hostname | Ruled out | Operator decision |
| Separate port (`:8443`) | Ruled out after a spike (2026-10-27) | Access let signed-in requests through on 8443 although its docs say it strips the port; the tunnel matches rules without the port, so Lettuce's whole API also answered on 8443, same-origin with the editor. Closing that needs a zone rule that fails open if removed, plus a token permission; some networks block 8443 |
| Profile | `code-server` | Names what runs; upstream is unlikely to claim it (to confirm, see open questions) |
| Gate | A garden sidecar (`code-server`, Python, garden-common): JWT check, prefix strip, WebSocket pass-through, header allow-lists, document rules, the `/garden/` landing page | code-server has no Access check and needs a prefix-stripping proxy; the JWT check is the one the vault uses |
| Editor | `code-server-editor`, `--auth none`, reachable only from the gate; runs no workspace code (above) | Access is the login; the editor's server process decides what runs on the origin |
| Shell | `code-server-exec`, an SSH server on the app-server's image; the editor's terminal profiles are an SSH wrapper | Terminals, and git with its hooks, get the agents' toolchain without running in the editor |
| Terminal | The terminal profile `exec-shell` prints a three-line notice, then runs `ssh -tt` to the shell with `cd -- <dir> && exec agent-env bash -i`. The notice: "Shell with your agents' tools and rights (workspace /work/<agent-id>). / /work and /opt/agent-tools are shared with every agent: never store tokens or passwords here / (no npm/cargo logins, no credentials in config files). Other installs vanish on the next restart." | An SSH session starts with a fresh environment and `/etc/profile` resets `PATH`, so without upstream's `agent-env` (PID 1's environment) the agents' tools aren't on `PATH` and `npm i -g` installs into the container's own layer. `bash -i`, not a login shell, keeps `/etc/profile` from overwriting it. The notice comes from the editor image, so nothing in the shell can change or hide it; sshd shows no motd for a session given a command |
| SSH keys | The editor's entrypoint creates a client key (on `code-server-data`) and the shell's host key once, and writes the host key and `authorized_keys` to `code-exec-keys`; the shell mounts that read-only. Key login only, no forwarding of any kind | No key passes through lettucectl or `stack.env`; the host key is pinned; the shell never holds the client key |
| Networks | `code-editor` (internal; gate and editor), `code-exec` (internal; editor and shell), `code-egress` (shell only), declared in the overlay | Agents reach neither container; the editor reaches nothing but the shell; the shell reaches the internet but not the stack. No upstream service changes |
| Access token and cookies | The gate never forwards `Cookie`, `cf-access-jwt-assertion` or `Set-Cookie` | The person's token and Lettuce's session cookie never reach the editor; the editor can't set cookies on the origin |
| Workspace Trust | On, never granted (Restricted Mode); TypeScript allowed in it through `extensions.supportUntrustedWorkspaces`, terminals through `terminal.integrated.allowInUntrustedWorkspace` | Restricted Mode is the only switch for tasks, debugging and workspace terminal settings |
| git | From the terminal only; no git extension, no Source Control view | The git extension asks to trust each repository's folder (door 4) |
| Settings | The editor's entrypoint merges the fixed settings (trust, the TypeScript override, terminals allowed, terminal profiles, type acquisition off, no schema downloads, no update checks or telemetry) into the user settings on every start; a file that isn't plain JSON is kept aside | The person can change them in the UI; a restart puts them back |
| Folders | One agent's workspace per window, `/work/<agent-id>`; the docs say never to open `/work` itself | Search and file watching per agent; a slip with "Trust" covers one agent, not all |
| Extensions | No marketplace; built-in allow-list; nothing installable | Doors 2 and 3 above |
| Images | `lettuce-code-server:local` (the gate, from `code-server/`), `lettuce-code-server-editor:local` (from `code-server/editor/`: Debian slim, `openssh-client`, `jq`, the release tarball pinned by version and SHA-256 per architecture, pruned to the allow-list) and `lettuce-code-server-exec:local` (from `code-server/exec/`: `FROM` the app-server's image plus `openssh-server`), all built by `compose up --build` like the other sidecars | No image mounts, so no Docker 28 / Compose 2.35 requirement; the shell's base follows upstream's image without restating its tag |
| User | Editor: uid 0, `cap_drop: [ALL]`, `no-new-privileges`, `read_only`; tmpfs `/tmp`. Shell: root with Docker's default capabilities minus `NET_RAW`, `no-new-privileges`, writable root (so `apt` and `pip` work until the next recreate). Gate: like gmail-triggers, no host paths | Agents write the workspaces as root; without `DAC_OVERRIDE`, the editor edits only what root owns |
| Editor state | Named volume `code-server-data`, mounted by nothing else; not backed up | Not under `state/`, which agents mount read-write; nothing in it can't be recreated |
| Agent memory | Not mounted | It's Lettuce's ("don't write into what Lettuce manages"); `shell` stays the way in for it |
| Port preview | Off (`--disable-proxy`) | Its routes would serve anything a terminal process listens on, on Lettuce's origin; the shell isn't reachable from the editor's proxy anyway |
| Resources | Editor: `mem_limit` 2g and `pids_limit` 512 (language servers only; not measured on a real stack yet). Shell: like `toolbox` (8g, 4096). Gate: like gmail-triggers | Builds run in the shell |
| Egress | The shell connects directly through `code-egress`, also with the `pipelock` profile; the editor has none | pipelock is on `default`, which neither may join; pipelock is advisory anyway (docs/egress-firewall.md) |

## This repo's rules, checked

Against CLAUDE.md's "Relationship to upstream":

| Rule | This design |
|---|---|
| **No second way into an agent** | No connection to the BFF or app-server; it starts no turn. It is a third feature that bypasses the BFF with its own UI and Access check, so it gets a written reason next to the vault and gmail-triggers, under `/garden/<sidecar>/` as required |
| **Overlays restrict, never redefine** | Three new services and three new networks; no upstream service changes |
| **Plug in only through supported seams** | The shell is built `FROM` upstream's app-server image and depends on its layout: Debian with `apt`, `bash`, the image `ENV` that points `PATH`, `NPM_CONFIG_PREFIX`, `CARGO_HOME`, `GOBIN` and the `UV_*` variables at `/opt/agent-tools`, and `/usr/local/bin/agent-env` (its own comment says "Cron only"; the terminal becomes its second user). Named under "Named today" so `upgrade` checks it. The editor depends on nothing of Lettuce's |
| **Don't write into what Lettuce manages** | Workspaces and tools are written by the person, as with `shell`; settings, the MCP list and memory are not mounted. The editor writes no `.vscode/` folder of its own into a workspace |
| **Nothing secret under `state/`** | Editor state and SSH keys live on their own volumes |
| **Keep upstream's defences on** | Access policy unchanged (its cookie gains `HttpOnly`); nothing joins the app-server's namespace |
| **The vault is the approval gate** | Strengthened: approvals can no longer be forged by same-origin script (layer 2) |

## Code layout

```
code-server/                  the gate (Python, Starlette, httpx, a WebSocket client; pinned like gmail-triggers)
  Dockerfile
  pyproject.toml
  src/code_server_gate/       app.py (routes, proxy, header and document rules), config.py
  tests/                      pytest; tests/e2e/ holds the e2e script's checks (check_gate.py against the
                              real editor, serve_gate.py and browser_check.py for --browser)
  editor/                     the editor image
    Dockerfile                Debian slim + openssh-client + the pinned tarball (VERSION, SHA256_AMD64,
                              SHA256_ARM64), built-in extensions pruned to EXTENSIONS_KEPT
    extensions-kept           the built-in extensions that stay
    entrypoint.sh             creates the keys once, merges settings.json, execs code-server
    settings.json             the fixed settings
    ssh_config                the one host, its pinned key, nothing forwarded
    bin/exec-shell            terminal profile: prints the notice, then ssh -tt to the shell, in the same
                              directory, through agent-env
    tests/test.sh             the image's tests (its `test` stage)
  exec/                       the shell image
    Dockerfile                FROM the app-server's image + openssh-server
    sshd_config               keys from /keys, root by key only, no forwarding, no environment
garden-common/src/garden_common/
  auth.py                     + Fetch Metadata checks
  (pages)                     + COOP on every garden response
compose/
  code-server.yml             services code-server, code-server-editor and code-server-exec
                              (profile code-server), networks code-editor, code-exec and
                              code-egress, volumes code-server-data and code-exec-keys
docs/
  code-server.md              user guide and security model
```

## Compose overlay (`compose/code-server.yml`)

- Always merged, so `stale_services` removes all three containers when the profile is off (as with
  `pipelock`).
- **`code-server` (the gate):** built from `code-server/` with the `garden-common` context, as
  gmail-triggers is; environment `CF_ACCESS_*`, `ALLOWED_USERS`, `PUBLIC_ORIGIN`, `GARDEN_PROFILES`;
  networks `default` and `code-editor`; `read_only`, `cap_drop: [ALL]`, no host paths.
- **`code-server-editor`:** built from `code-server/editor/`; the flags above; volumes
  `state/workspaces:/work`, `code-server-data:/data`, `code-exec-keys:/keys`; networks
  `code-editor` and `code-exec`.
- **`code-server-exec`:** built from `code-server/exec/` with `additional_contexts: {base:
  "service:app-server"}` and `FROM base`, so it is built from whatever image upstream's app-server
  resolves to, after it; `sshd -D -e`; volumes `state/workspaces:/work`, `state/tools:/opt/agent-tools`,
  `code-exec-keys:/keys:ro`; networks `code-exec` and `code-egress`; `depends_on` the editor (the
  keys exist first).

## lettucectl changes

- **`check_stack`**, with the profile (`check_code_server`):
  - the gate: `check_sidecar`; networks exactly `default` and `code-editor`.
  - the editor: `check_hardened`; host mounts exactly `state/workspaces:/work`; named volumes
    exactly `code-server-data` and `code-exec-keys`; networks exactly `code-editor` and
    `code-exec`; its command has `--auth none`, `--disable-proxy` and `--extensions-dir` with the
    image's empty directory, and lacks `--disable-workspace-trust`, `--link`, `--proxy-domain`,
    `--trusted-origins`, `--enable-proposed-api`, `--install-extension`, `--github-auth`,
    `--builtin-extensions-dir` and `--abs-proxy-base-path`; `EXTENSIONS_GALLERY` is `{}`.
  - the shell: host mounts exactly `state/workspaces:/work` and `state/tools:/opt/agent-tools`;
    `code-exec-keys` read-only and no other named volume; networks exactly `code-exec` and
    `code-egress`; no `NET_RAW`; no `network_mode`, `pid`, `ipc`, `volumes_from` or `privileged`;
    no published port (already refused for every service).
  - `code-editor` internal with only the gate and the editor; `code-exec` internal with only the
    editor and the shell; `code-egress` with only the shell.
  - neither the workspace nor the tools path, nor `$LETTA_STATE_DIR`, is a symlink.

  Whatever the profiles: `code-server-data` is mounted by nothing but `code-server-editor`, and
  `code-exec-keys` by nothing but the editor and (read-only) the shell; no `volumes_from` names one
  of the three services.
- **Access cookie:** `cf_app_body` is the application as lettucectl defines it, with
  `http_only_cookie_attribute: true` and `same_site_cookie_attribute: lax`; `cf_app_create` posts it
  and `cf_app_update` puts it (a `PUT` replaces the application's settings). `route_stack` applies it
  with the routes, so `profiles` brings every existing stack in line on every run. `check` does not
  read it back: `check` runs offline at every `up`, and enabling the profile goes through `profiles`
  anyway. The token already has "Access: Apps and Policies: Edit".
- **`compose` wrapper:** `compose/code-server.yml` always, after `compose/pipelock.yml`.
- **Tunnel:** a `PROFILE_ROUTES` row, `code-server ^/garden/code-server(/|$) http://code-server:8000`.
  `add`, `remove` and `profiles` already handle routes.
- **`/garden/` landing page:** `LABELS` gains `code-server` ("Code editor", "Your agents' files in
  VS Code"); `landing_routes` is served by the gate like the other sidecars.

## Tests

- **`tests/run.sh`:**
  - each `check_code_server` refusal, via `config_edit` on a `code_config` / `with_code` fixture
    taken from a real render of the overlay;
  - the compose files, and the tunnel route with and without the profile;
  - `stale_services` removing all three containers;
  - the Access cookie attributes in `add`'s Cloudflare call, and `profiles` re-applying every
    stack's application (and reporting a failure).
- **Gate tests** (`tests/run-sidecar.sh code-server`, pytest; never the network):
  - a request or WebSocket upgrade without a valid JWT is refused, using the vault's `make_jwt`
    pattern; a WebSocket is closed at the JWT's `exp`;
  - a WebSocket with a foreign `Origin` is refused;
  - the prefix is stripped and added back to redirects;
  - only allow-listed headers pass either way: never `Cookie`, the JWT header, `Set-Cookie` or
    COOP; `Service-Worker-Allowed` narrowed to the prefix;
  - a document or iframe load of anything but the workbench and the webview host is refused, and
    every other response carries `nosniff` and `CSP: sandbox`; the workbench never gets COOP;
  - streaming bodies both ways; the landing page.
- **garden-common tests**, run through the vault's and gmail-triggers' suites:
  - a page `GET` in `cors`/`same-origin`/`no-cors` mode is refused, and so is one with no
    `Sec-Fetch-*` headers; a cross-site navigation (Access re-login) is not;
  - a form post outside `navigate`/`document`/`same-origin` is refused;
  - the vault's push endpoints still work from its own script;
  - every garden response carries COOP.
- **Editor image tests** (run by `tests/run-sidecar.sh code-server`, no network): the kept
  built-ins equal the allow-list; no `node`, `npm` or shell profile is found from `PATH` or
  `/etc/shells`; code-server runs from the image; the keys are made once and follow either volume;
  the merged settings hold the fixed keys after an edit; `exec-shell` prints the notice, asks a fake
  `ssh` for `agent-env env TERM=... bash -i` in the directory (any name), and passes the exit status.
- **`scripts/code-server-e2e.sh [--browser]`** (needs Docker and the internet for the first build;
  throwaway containers only): the real images, wired like the overlay. It checks:
  - through the real gate (with a test signing key): the workbench, its scripts and the webview host
    load under `/garden/code-server/`; the gallery is empty; an agent's HTML file can't load as a
    page and loads only sandboxed; `/proxy/1234/` answers 403; no JWT, no editor; a WebSocket
    reaches the editor, and one from `:8443` is refused;
  - over the terminal's SSH session: git and a `pre-commit` hook run in the shell, not the editor;
    `PATH` has the `/opt/agent-tools` directories and npm's prefix is `/opt/agent-tools`; the
    notice is printed;
  - the editor can't write its extensions directory or its root, can't reach the internet, and the
    stack network can't reach the editor or the shell; the shell lets in no other key;
  - with `--browser`, in Chromium through the gate (cloudflared and Access simulated by
    `serve_gate.py`), with a planted terminal profile, automatic task, workspace TypeScript and
    `tsserver` node, `tsconfig` plugin and `git.path`: opening the folder and a terminal asks
    nothing; Restricted Mode shows; nothing planted runs in the editor; the bundled TypeScript
    runs; what is typed in the terminal runs in the shell, not the editor; running a task asks for
    trust.
- **CI:** `tests/run-sidecar.sh code-server` (the gate's and the editor image's suites) and the new
  shell files in `.github/workflows/ci.yml`. The e2e script stays opt-in, like doc-tools'.

## Steps

0. **Spike** at runtime: done (results below).
1. Garden page hardening in `garden-common` (Fetch Metadata, COOP) and the Access cookie
   attributes, with their tests: done.
2. `code-server/editor/` and `code-server/exec/`, with the editor image tests: done.
3. The gate (`code-server/`) and its tests: done.
4. `compose/code-server.yml`, the `compose` wrapper, `check_code_server`, the `PROFILE_ROUTES` row,
   `LABELS`, and their tests: done.
5. `docs/code-server.md`, README, docs/operations.md, docs/secret-broker.md, docs/gmail-triggers.md,
   AGENTS.md, CI: done.
6. **To do:** end-to-end on a real stack: `profiles` with `code-server` (it also re-applies every
   stack's Access application), sign in, edit, run a terminal command and a commit, try to read a
   vault page with `fetch()` from the editor's tab (must fail), measure the editor's memory, toggle
   the profile off and on.

## Spike results (2026-10-27)

Run with `scripts/code-server-e2e.sh --browser` on code-server v4.143.1, the app-server image
`v0.22.0-letta_0.34.21`, Compose 2.40 and Chromium from Playwright 1.63:
- **Behind the gate:** the workbench, its scripts (relative `stable-<commit>/static/...`), the
  webview host and WebSockets work with the prefix stripped and no cookies at all. code-server's
  WebSocket origin check passes with `X-Forwarded-Host` (it honours `Forwarded`, then
  `X-Forwarded-Host`, then `Host`). The editor runs with a read-only root and no capabilities.
- **Marketplace:** `EXTENSIONS_GALLERY={}` leaves the product configuration with an empty gallery;
  the workbench logs that it ignores the gallery. code-server still lists `https://open-vsx.org` as
  a trusted link domain, which is harmless.
- **TypeScript:** runs in Restricted Mode with the bundled TypeScript; a planted `typescript.tsdk`,
  `tsserver.nodePath` and `tsconfig` plugin did not run (source review: TypeScript searches the
  project only with `--allowLocalPluginLoads`, which the extension never passes; names must be bare
  package names).
- **Tasks, profiles:** a planted `process` task with `runOn: folderOpen` and
  `task.allowAutomaticTasks: on` did not run; running a task asks for trust; a planted default
  terminal profile was ignored.
- **git:** the extension asks to trust each repository's folder; it is out (door 4). In the first
  browser run the automation pressed that dialog's default button, the folder became trusted, and
  the planted terminal profile ran in the editor: exactly what a person clicking "Trust" would get.
- **Terminals:** without `terminal.integrated.allowInUntrustedWorkspace`, opening one asks for
  trust; with it, it opens in the shell with no dialog.
- **Trust storage:** at `APPLICATION_SHARED` scope; after a trust, the server's `state.vscdb` held
  none, so code-server keeps it in the browser and the editor can't clear it at start.
- **Settings:** application-scoped settings take effect from `<user-data-dir>/User/settings.json`.
- **Compose:** `additional_contexts: {base: "service:<name>"}` builds the base service first and uses
  its image (Compose 2.40; the docs give no minimum version).
- **Fetch Metadata:** Chromium sends no `Sec-Fetch-*` headers to a plain-HTTP origin other than
  localhost (the browser harness uses localhost for that reason); on the stack's HTTPS origin it
  does. Firefox and Safari send them too (Firefox 90+, Safari 16.4+); a PWA and a page opened from a
  notification are ordinary navigations. Not yet seen on a real stack.
- Not settled: whether the Access login is unaffected by `same_site_cookie_attribute: lax`, the
  editor's memory on a real stack, and whether `code-server` is a profile name upstream will never
  claim.

## Not in scope

- The person's own credentials in the editor or the shell (GitHub, Copilot, Claude, Settings Sync):
  `toolbox`.
- Extensions from a marketplace, or any extension not in the image.
- A debugger in the editor, and third-party formatters or linters in it (run them in the terminal).
- Notebooks. (The Markdown preview was added after the spike, pinned to its Strict policy and without the Markdown Editor; docs/code-server.md.)
- Editing agent memory: `lettucectl shell <name> <agent>`.
- Port preview of dev servers run in the terminal.
- A list of agents by name on the landing page (it needs `letta agents list`; the folder picker
  shows ids).
- One editor per agent, or per-agent permissions inside a stack.
- Routing the shell's traffic through pipelock.
- Hardening Lettuce's own pages against same-origin script (upstream's).
