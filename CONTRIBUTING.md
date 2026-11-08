# Contributing to lettuce-garden

This page is the starting point for working on lettuce-garden itself: the CLI, the overlays, the garden sidecars, the agent skills and the docs. To install and run it, start from the [README](README.md) and [docs/](docs/) instead.

The rules live in the developer docs: the cross-cutting ones on this page, and each folder's own in its README, under "Rules". [AGENTS.md](AGENTS.md) is the short version for coding agents: one-line directives that link back here.

## Setting up
- Linux with Docker Engine and Docker Compose >= 2.24, `bash`, `jq`, `curl`, `dig` and `openssl`, as for running it.
- [shellcheck](https://github.com/koalaman/shellcheck) v0.11.0, the version CI pins: older releases report differently.
- Node 22 or newer for `node --test` on the host. The sidecars' tests run in Docker and need nothing else.
- No upstream clone is needed for the offline tests: `tests/run.sh` stubs Docker and Cloudflare.
- `./lettucectl` from a clone builds every image itself and keeps its instance in the clone's untracked `data/` (or the directory `LETTUCE_DATA` names); it refuses a data directory a release runs ([lib/README.md](lib/README.md#code-and-data-are-apart)). Admins run released images instead; how a release is cut: [release/README.md](release/README.md#cutting-a-release).

## Relationship to upstream
lettuce-garden composes [Lettuce](https://github.com/dmarchevsky/lettuce) and adds to it: one isolated stack per person, plus an opinionated set of extras (garden sidecars, MCP servers, agent skills, CLI commands). Lettuce is not ours, and every departure from its design is something `lettucectl upgrade` can break. Six rules follow from that.

**1. Never modify Lettuce without an explicit request.** That covers the `upstream/` clone and the upstream repository: its code, issues and PRs. When a change would fit better upstream, say so, and stop there. *Breaking it:* patching `upstream/docker/compose.yml` to add a mount, instead of adding an overlay.

**2. Plug in only through supported seams.** Those are `stack.env`, Compose profiles, the overlays in `compose/`, Lettuce's Settings and `letta connect`, the shared MCP list, the skills directory, and `host.docker.internal`. A feature that needs Lettuce internals (undocumented files under `/root/.letta`, the BFF's routes, the app-server protocol) is redesigned, or it names that dependency in [Upstream seams](#upstream-seams), so every upgrade checks it. *Breaking it:* a sidecar that reads a file the BFF writes, without a row in that table.

**3. Overlays restrict, never redefine.** They may drop ports, drop capabilities, set `read_only` or cap resources. They don't replace an upstream service's image, entrypoint, command or internal environment. Two written exceptions exist, `compose/pipelock-proxy.yml` and `compose/doc-tools.yml`; another needs a written reason first ([compose/README.md](compose/README.md#rules)). *Breaking it:* setting the app-server's `entrypoint` in an overlay.

**4. No second way into an agent.** The BFF owns the only connection to the app-server. No `lettucectl` command or sidecar opens another or starts a turn; `compose exec` for files and read-only CLI calls is the limit. *Breaking it:* a sidecar that connects to the app-server to send an agent a message.
- **The one exception** is a garden sidecar starting a turn through letta-code's own scheduler. Only Gmail triggers does this ([gmail-triggers/README.md](gmail-triggers/README.md#rules)): it opens no connection, the scheduler fires the task on the BFF's connection, and agents get nothing new.
- **Three features bypass the BFF**, each with its own UI and Access check, all on the stack's garden host (`<name>-garden.<domain>`, under its own Access application):
  - the vault's `/vault/`;
  - `/gmail-triggers/`, because trigger rules must live where agents can't edit them (inside Lettuce, they could);
  - `/code-server/`, the code editor, because a person needs the workspaces' files and a terminal from anywhere, which Lettuce has no UI for. It opens no connection to the app-server or the BFF and starts no turn, and its terminal is a separate container no stronger than an agent.

  The garden host's root, the landing page linking to them, belongs to the `garden` service: behind the same Access check, it reads the agent list the executor sends and opens no connection to Lettuce. Another bypass, or another sidecar starting turns through the scheduler, needs a written reason here first. Garden sidecars go under `/<sidecar>/` on the garden host, never on Lettuce's.

**5. Don't write into what Lettuce manages.** Its settings, MCP list, agent memory and workspaces are Lettuce's. Use its own mechanism where one exists; where we must write, own a namespace, as `skills` does with its manifest ([agent-skills/README.md](agent-skills/README.md#rules)). The one write into the MCP list is `lettucectl mcp`, since there is no supported way to set it across stacks: it owns exactly two kinds of entries ([lib/README.md](lib/README.md#mcp-registers-the-document-tools-and-the-vault-once-per-stack-and-only-those)). Never turn an upstream feature on by editing its files: a feature is on when its profile and its Settings switch are. *Breaking it:* editing Lettuce's settings file to turn Google on.

**6. Keep upstream's defences on.**
- Keep its auth settings at their strictest, and never disable a boot check.
- Nothing secret goes under `stacks/<name>/state/`: agent shells mount it read-write, as root.
- Don't shorten the BFF's `stop_grace_period`, which lets in-flight turns drain on a redeploy.
- Give new profiles names upstream is unlikely to claim. Upstream matches profile tokens exactly, so a shared name would turn on both; ours all start with `garden-`.
- Move upstream's version only with `lettucectl upgrade <tag>`: Lettuce tags pin the letta-code server version the UI was compiled against.

### Upstream seams
These are the Lettuce internals lettuce-garden depends on today ("named today"). A new one adds a row. Each folder README's "Upstream seams" section has the detail.

| Seam | What we rely on | Details |
|---|---|---|
| `cmd_shell` | letta-code's memory path, `$LETTA_LOCAL_BACKEND_DIR/memfs/$AGENT_ID/memory` | [lib/](lib/README.md#upstream-seams) |
| `toolbox` | the app-server image layout, `/opt/claude-code/bin` | [lib/](lib/README.md#upstream-seams) |
| `google_redirect_uri` | the BFF route `/api/google/oauth/callback` | [lib/](lib/README.md#upstream-seams) |
| `find_agent` | the JSON shape of `letta agents list` (`.items[]`) | [lib/](lib/README.md#upstream-seams) |
| `check_stack`, the overlays | upstream's service names, the volumes `google-creds` and `google-policy`, and `network_mode: service:app-server` for the BFF and channel-gateway | [compose/](compose/README.md#upstream-seams) |
| `compose/pipelock-proxy.yml` | who follows `HTTPS_PROXY`: letta-code's Node (22.19 today) ignores it even with `NODE_USE_ENV_PROXY=1`, and Node >= 22.21 follows it. searxng and ddg-mcp follow it only because their clients trust the environment. `NO_PROXY` is a hand-kept list of the services agents and the executor call inside the stack | [pipelock/](pipelock/README.md#upstream-seams) |
| the garden executor | `letta cron add` with `--name=`, `--description=`, `--prompt=`, `--at=in 1m`, `--once`, `--agent=`, `--conversation=new\|default` (the `=` form, since letta-code's strict `parseArgs` misreads a value starting with `-`) and its JSON output (`.id`); `letta agents list` (`.items[]`); the scheduler picking up a changed task file without a restart; the app-server entrypoint copying `/root/.letta/system-cron` to `/etc/cron.d`; the image's `/usr/local/bin/agent-env` | [garden/](garden/README.md#upstream-seams), [executor](garden/executor/README.md) |
| `lettucectl mcp` | the shared MCP list's file, `/root/.letta/mcp-home/.letta/settings.json`, and its shape; the BFF rendering it whole and reading it from disk before every change; the `mcp-servers` skill's description, which the BFF renders from the list only on a Settings -> MCP save and on every connect (`ensureMcpServers`), hence the BFF restart | [lib/](lib/README.md#upstream-seams) |
| doc-tools | how `letta mcp call` runs a stdio server (per call, in the agent's cwd, a fixed environment, stdin closed then SIGTERM then SIGKILL, stderr piped and never read, 60 s per request; a bare `tools`/`search` starts every server); the `mcp-servers` skill wrapper `/root/.letta/skills/mcp-servers/scripts/mcp.sh`; workspaces at `/work/<agent-id>`; `/opt/lettuce-doc-tools` free in the app-server image, glibc >= 2.28 and system ICU, a `/tmp` that allows exec | [doc-tools/](doc-tools/README.md#upstream-seams) |
| the vault and its skill | the vault's `http` entry in the MCP list; letta-code telling the agent when a background command finishes, which is how an agent waiting for an approval gets a new turn; `MAX_WAIT_SECONDS` below the MCP bridge's 120 s | [vault/](vault/README.md#upstream-seams), [agent-skills/](agent-skills/README.md#upstream-seams) |
| skills and `backup` | the global skills directory `/root/.letta/skills`, and the BFF rewriting upstream's `docker/agent-skills` there on every connect | [agent-skills/](agent-skills/README.md#upstream-seams), [backup/](backup/README.md#upstream-seams) |
| Gmail triggers | the Google files the BFF writes on the `google-policy` and `google-creds` volumes | [gmail-triggers/](gmail-triggers/README.md#upstream-seams) |
| `app_server_image`, `upstream_image_ns`, `fetch_upstream` | the app-server's image named `${LETTA_IMAGE_NS}lettuce-app-server:<release tag>`, `LETTA_IMAGE_NS` as upstream's pull-or-build switch, its images at `ghcr.io/<owner>/`, and GitHub's tag archives | [lib/](lib/README.md#upstream-seams) |
| `garden-code-server-exec` | built `FROM` the app-server's image: Debian with `apt`, `bash` as root's shell, the image `ENV` pointing `PATH`, `NPM_CONFIG_PREFIX`, `CARGO_HOME`, `GOBIN` and the `UV_*` variables at `/opt/agent-tools`, `/usr/local/bin/agent-env` reading PID 1's environment; the workspaces and tools mounted as the app-server mounts them | [code-server/](code-server/README.md#upstream-seams) |
| the landing page's agent links | code-server's `?folder=` query and the workspaces at `/work/<agent-id>` | [garden/](garden/README.md#upstream-seams) |

### Upgrading `LETTUCE_REF`
`lettucectl upgrade <tag>` downloads the new tag, but only a person can check the seams. A release's tested tag is `config.env.example`'s `LETTUCE_REF`, which `publish.yml` writes into `RELEASE`, so moving it there is what moves released installs (through `self-upgrade`). For each new tag:
1. Read Lettuce's release notes and changelog for anything the table above names.
2. Check each row against the new tag, starting with the ones its notes touch.
3. Run `node --version` in an app-server. From 22.21 letta-code's own traffic goes through pipelock too: check that `NO_PROXY` still covers what it calls inside the stack ([pipelock/README.md](pipelock/README.md#upstream-seams)).
4. Run every test (below), and `scripts/doc-tools-e2e.sh` and `scripts/code-server-e2e.sh --browser` against the new app-server image.
5. Update the table and the folder READMEs for whatever changed.

## What `check_stack` enforces
`check_stack` (`lib/common.sh`) gates every `up`. It reads `stack.env`, renders the merged config and refuses an unsafe stack, saying which rule it broke. `cmd_profiles` runs it with `--profiles NEW` on every stack before writing a new list. Like every check, it runs at `up`: a container Docker restarts on its own (a daemon restart, a reboot) is not re-checked.

Three helpers recur:
- `check_hardened` refuses a service that is not `read_only`, lacks `cap_drop: [ALL]`, is `privileged`, or has `cap_add`, `network_mode`, `pid`, `ipc`, `volumes_from` or `devices`;
- `check_sidecar` is `check_hardened` plus no host mount;
- `check_isolated` refuses `privileged`, `cap_add`, `network_mode`, `pid`, `ipc`, `volumes_from` and devices, for a container that must keep capabilities.

`volume_users` is the shared "who mounts this volume" helper.

### Every stack
| It refuses | Why |
|---|---|
| `stack.env` not mode 600 | it holds the stack's secrets |
| any `DEV_BYPASS_*` | it bypasses Access |
| required keys empty, or a short `SESSION_SECRET` | the stack would start insecure |
| `COMPOSE_PROFILES` without `cloudflared`, or different from `config.env` `PROFILES` | all stacks share one app-server image, built for that list |
| any published port in the rendered config | the host publishes nothing; cloudflared connects outbound |
| `DISABLE_AUTOUPDATER` other than `1` on the app-server or channel-gateway | letta-code would reinstall itself over the pinned version |
| the app-server, or any service with `network_mode: service:app-server`, keeping `NET_RAW` | agents could capture the Access JWT cloudflared sends the BFF ([vault/README.md](vault/README.md#rules)) |
| `vault-data` mounted by anything but `garden-vault`, `pipelock-data` by anything but `garden-pipelock`, `google-creds` by anything but `bff`, `google-mcp` and `garden-gmail-triggers` | these volumes outlive their profile, so the rule holds whatever the profiles |
| a `volumes_from` naming `garden-gmail-triggers`, `garden-vault`, `garden-pipelock`, or a service that mounts `google-creds`, `vault-data` or `pipelock-data` | it would copy their mounts |
| a garden service on another Access audience or origin (`check_garden_audience`) | [garden/README.md](garden/README.md#rules) |
| the code editor's volumes or namespaces shared, or the tools' volume or an image mount anywhere but the app-server | [code-server/](code-server/README.md#what-check_code_server-refuses), [doc-tools/](doc-tools/README.md#rules) |
| a profile whose dependency is off (`profile_unmet`: `garden-gmail-triggers` needs `google`) | `profiles` checks it before writing too |

### Per profile
| Profile | It refuses | Details |
|---|---|---|
| `garden-vault` | a `garden-vault` service that is missing, not hardened (`check_hardened`) or with a host mount (`check_sidecar`) | [vault/](vault/README.md#rules) |
| `garden-pipelock` | a `garden-pipelock` service that is missing, not hardened or with a host mount; an app-server whose `HTTPS_PROXY`, `https_proxy`, `HTTP_PROXY` or `http_proxy` is not `http://garden-pipelock:8888`, or whose `NO_PROXY` or `no_proxy` lacks `garden-vault`, `garden-gmail-triggers` or `garden` (`check_pipelock`) | [pipelock/](pipelock/README.md#rules) |
| any garden page (`garden-vault`, `garden-gmail-triggers`, `garden-code-server`) | `check_garden` | [garden/](garden/README.md#what-check_garden-refuses) |
| `garden-gmail-triggers` | `check_gmail_triggers` | [gmail-triggers/](gmail-triggers/README.md#what-check_gmail_triggers-refuses) |
| `garden-doc-tools` | `check_doc_tools`, and `check_gotenberg` before Gotenberg starts | [doc-tools/](doc-tools/README.md#what-check_stack-refuses) |
| `garden-code-server` | `check_code_server` | [code-server/](code-server/README.md#what-check_code_server-refuses) |

### Adding a rule
1. Add the check to `check_stack` or its `check_*` helper in `lib/common.sh`.
2. Add a `test_check_*` test: render a config with the feature's helper, break it with `config_edit` or `*_refused FILTER MESSAGE`, and assert the refusal ([tests/README.md](tests/README.md#per-feature)).
3. Document it: a row above, or the folder README's "Rules" for a profile's own check.
4. Add a directive to [AGENTS.md](AGENTS.md) when an agent's change could break it.

Two rules hold whatever the profiles: in release mode every published garden service runs the image `RELEASE` names, and the code editor's shell builds `FROM` the app-server's image as the render names it (`check_images`, [lib/README.md](lib/README.md#release-mode-is-a-file-not-a-setting)).

## Adding a garden sidecar
A new page on the garden host, `garden-<page>` at `/<page>/`. Each step is a rule somewhere; this list gathers them.
1. **The service:** a folder with the app, a `Dockerfile` with a `test` stage, and the `garden-common` build context ([garden-common/README.md](garden-common/README.md#how-it-plugs-in)). Use `garden_common.auth` for every page (Access JWT, Fetch Metadata, CSRF, COOP).
2. **The overlay:** `compose/<name>.yml`, merged for every stack (add it to the `compose` wrapper's list), the service behind its own `garden-<page>` profile, hardened, with `CF_ACCESS_AUD: ${CF_GARDEN_AUD}` and `PUBLIC_ORIGIN: ${GARDEN_ORIGIN}`. Don't pass it `COMPOSE_PROFILES` or `GARDEN_PROFILES` ([lib/README.md](lib/README.md#tunnel-routes-follow-the-profiles)).
3. **The route:** a `PROFILE_ROUTES` row in `lib/cloudflare.sh` (profile `garden-<page>` with its page at `/<page>/`, which a test checks), the profile in `compose/garden.yml`'s list (a test checks they match), and an entry in `LABELS` (`garden/src/garden_landing/`).
4. **Checks:** a `check_<name>` for its hardening, mounts and networks, and its tests; `check_garden_audience` picks it up from `PROFILE_ROUTES`.
5. **Pipelock:** if agents or the executor call it inside the stack, add it to `NO_PROXY` in `compose/pipelock-proxy.yml`, or pipelock refuses its private address. If what passes between it and the app-server must never reach pipelock (secret values, as with the vault, or a token, as with the executor's), also add it to the names `check_pipelock` requires in `NO_PROXY`.
6. **An endpoint agents call:** keep it outside the tunnelled `/<page>/` prefix, so the tunnel never routes it, and refuse an `Origin` header and Cloudflare's tunnel headers, as the vault's `/mcp` and `/redeem` and the executor API (`guard`) do. Agents are root in the app-server, so such an endpoint authenticates nothing about them; approval, if any, happens on the page. Registering it in Settings -> MCP would make `mcp` own a third kind of entry: that changes [its rule](lib/README.md#mcp-registers-the-document-tools-and-the-vault-once-per-stack-and-only-those) and rule 5 above.
7. **Storage:** a named volume outlives its profile, so add whatever-the-profiles rules to `check_stack` (only this service mounts it, no `volumes_from` reaches it; `volume_users`), and decide whether `backup` copies it (today it copies the vault's and pipelock's volumes by name). Prefer no host path. If it needs one, it must sit outside everything the app-server mounts (never inside `letta-home`, which agents can write), get symlink checks like `check_gmail_triggers`'s, and stay out of `$LETTA_STATE_DIR/garden`, where `check_gmail_triggers` refuses any service but Gmail triggers.
8. **The agent list:** if the page needs the agents' names, it answers the executor's status like `garden` does (`AgentAsks`), and `executor_wanted` must include its profile ([garden/executor/README.md](garden/executor/README.md)).
9. **Upstream:** if it starts turns or bypasses the BFF, write the reason under [Relationship to upstream](#relationship-to-upstream) first. If it relies on a Lettuce internal, add a row to [Upstream seams](#upstream-seams).
10. **Tests:** `tests/run-sidecar.sh` support and a job in the CI matrix (`.github/workflows/ci.yml`).
11. **Docs:** a guide in `docs/` ("Using it", "Running it", "Security model"), a row in the README's extras table and in `docs/operations.md`'s optional features, the folder README with its "Rules", and a flow diagram ([Writing docs](#writing-docs)). The garden page profiles are also listed by hand in [garden/README.md](garden/README.md#what-check_garden-refuses), the per-profile table above, `compose/README.md`, ARCHITECTURE.md and docs/architecture.md: add the new one to each.

## Where things are
- **[ARCHITECTURE.md](ARCHITECTURE.md):** the whole stack: containers, networks, volumes, routes, who calls whom, how a stack is assembled, and a map of the repo.
- **One README per folder**, each with its files, how it plugs in, its rules, how to build and test it, and the upstream behaviour it depends on:

  | Folder | What |
  |---|---|
  | [lib/](lib/README.md) | the CLI's internals (`lettucectl` itself is at the root), and the CLI-wide rules |
  | [compose/](compose/README.md) | the overlays and their merge order, and the host's shared Gotenberg |
  | [vault/](vault/README.md) | the vault |
  | [gmail-triggers/](gmail-triggers/README.md) | Gmail triggers |
  | [code-server/](code-server/README.md) | the code editor: the gate, the editor's image and the shell's image |
  | [garden/](garden/README.md) | the garden's landing page, and [garden/executor/](garden/executor/README.md), the executor that runs in the app-server |
  | [garden-common/](garden-common/README.md) | the sign-in and request checks, styles and icons the garden services share |
  | [pipelock/](pipelock/README.md) | the egress proxy's image and policy |
  | [doc-tools/](doc-tools/README.md) | the document tools' image and the PDF MCP server |
  | [agent-skills/](agent-skills/README.md) | the skills installed into every agent |
  | [backup/](backup/README.md) | the image `lettucectl backup` copies with |
  | [release/](release/README.md) | releases: the published images, their content tags, the lettucectl image and its wrapper |
  | [tests/](tests/README.md) | the CLI's offline tests: the stubs and helpers |

- **Design records**, kept as written, with a note where the build later differed: [code-server/DESIGN.md](code-server/DESIGN.md), [gmail-triggers/DESIGN.md](gmail-triggers/DESIGN.md), [garden/DESIGN.md](garden/DESIGN.md) (the garden host and the garden-wide executor), [lib/DESIGN.md](lib/DESIGN.md) (installing without a clone).
- **The user and admin guides** in [docs/](docs/): each feature's security model (what it guarantees and what it does not) is there; the folder README says how it is enforced.

## Building and testing
The CLI has no build step. From a clone, `lettucectl up` builds the sidecar images; a release pulls them. The commands (the same block as in AGENTS.md):
```bash
tests/run.sh                 # all offline tests (docker, curl, dig, gh, secret-tool are stubbed)
tests/run.sh <pattern>       # only tests whose function name contains <pattern>, e.g. tests/run.sh test_add_
shellcheck -x lettucectl lib/*.sh scripts/*.sh pipelock/entrypoint.sh tests/run.sh tests/run-sidecar.sh tests/run-pipelock.sh tests/stubs/* doc-tools/bin/* doc-tools/lib/launch.sh code-server/editor/bin/* code-server/editor/entrypoint.sh code-server/editor/tests/test.sh release/content-tag release/lettucectl-wrapper.sh
tests/run-sidecar.sh vault            # vault tests (pytest in its image's `test` stage; needs Docker)
tests/run-sidecar.sh vault -k vault   # extra args go to pytest
tests/run-sidecar.sh gmail-triggers    # Gmail triggers' tests (pytest; the executor's run with garden)
tests/run-sidecar.sh code-server       # the code editor's gate (pytest), plus the editor image's tests when no args are given
node --test 'garden/executor/tests/*.test.mjs'         # garden executor tests on the host
tests/run-pipelock.sh                  # the pipelock image and config against a real pipelock (needs Docker, not the internet)
node --test tests/mcp-servers.test.mjs # lib/mcp-servers.mjs against temp settings files
node --test tests/check-doc-links.test.mjs # the link checker against temp Markdown trees
node scripts/check-doc-links.mjs       # every relative link and anchor resolves; every folder README has its agents' pointer
tests/run-sidecar.sh garden            # the landing page and its executor API (pytest), plus the garden executor's tests (node --test in node:22.22.0-slim) when no args are given
tests/run-sidecar.sh doc-tools         # the PDF MCP's tests (pytest in the doc-tools image's `test` stage, Debian 12 like the app-server)
```
- **Before changing a folder, run every command in its README's "Build and test"**. Those sections are the one list of what covers what; they also name the suites another folder shares, such as the executor's and Gmail triggers' two ends of one protocol.
- **`tests/run-sidecar.sh <dir>` takes a folder with a Dockerfile:** `vault`, `gmail-triggers`, `code-server`, `garden` or `doc-tools`. `garden-common/` has no image or tests of its own; before changing it, run the four suites that use it (its [Build and test](garden-common/README.md#build-and-test)).
- **Before changing docs:** `node scripts/check-doc-links.mjs`.
- **End to end**, against real images (Docker, and the internet for the first build; throwaway containers only): `scripts/doc-tools-e2e.sh`, and `scripts/code-server-e2e.sh --browser` before keeping a new code editor extension or setting.

CI (`.github/workflows/ci.yml`) runs all of these but the end-to-end scripts on every branch push, by hand from Actions ("Run workflow"), and before every release (`publish.yml` calls it). A new test script or linted file goes there too, and in both copies of the block above.

## Writing docs
Each audience has its own place:

| Audience | Where | What goes there |
|---|---|---|
| Someone arriving with no context | [README.md](README.md) | what lettuce-garden offers, requirements, quickstart, and the index of every doc |
| People who use a stack | [docs/user-guide.md](docs/user-guide.md), and each guide's "Using it" section | what they see and do, in the product's own words |
| Admins who install and run it | the rest of [docs/](docs/) | `lettucectl` commands, data and backups, troubleshooting, and each feature's security model: what it protects and what it does not |
| Developers | this page, [ARCHITECTURE.md](ARCHITECTURE.md), the folder READMEs, the design records | wiring, function and file names, the rules and how each is enforced, tests, maintenance (pin bumps, upgrades of bundled tools) |
| Coding agents | [AGENTS.md](AGENTS.md) (`CLAUDE.md` links to it), and each folder's `AGENTS.md` pointer | one-line directives and the commands, each directive linking to its rule in the developer docs |

The rule of thumb: running `lettucectl`, using a page or knowing what is protected belongs in `docs/`; editing tracked files, how something is wired, the rules and tests belong with the developers. Each fact has one home, and the other places link to it.

- **A rule lives in the developer docs.** Changing one edits its folder README or this page, then the AGENTS.md directive if its one-line form changed. AGENTS.md never carries a rule's detail.
- **Never claim more than is true.** When a security model is written in plain language, every limit stays in the user doc: pipelock is advisory, the vault's `env` delivery is not a boundary, agents are root in their stack, and a folder in the code editor must never be trusted. Only the mechanism moves to the folder README.
- **Keep the guides' file names and `docs/cloudflare-setup.md`'s step numbers.** `lettucectl`'s messages cite them (`docs/cloudflare-setup.md, step 5`, `docs/vault.md`, `docs/doc-tools.md`, `docs/google-setup.md`), and `tests/run.sh` asserts some of those messages.
- **Don't add files to `doc-tools/` or `garden-common/` for docs.** `doc_tools_tag` (`lib/common.sh`) hashes everything in `doc-tools/` but `tests/`, `README.md`, `AGENTS.md`, `CLAUDE.md` and Python caches, and `garden-common/.dockerignore` leaves out only `README.md`, `AGENTS.md` and `CLAUDE.md`: any other file there rebuilds an image, and for `doc-tools/` refills the tools' volume and recreates every app-server.
- **Each folder README has an agents' pointer next to it:** an `AGENTS.md` that tells a coding agent to read the README's "Rules" and run its "Build and test" before changing the folder, and a `CLAUDE.md` symlink to it, which Claude Code loads when it works in that folder. The pointer holds no rule; a new folder README gets one too. `node scripts/check-doc-links.mjs` checks that every README has it.
- **Diagrams** are archify candidates in `docs/diagrams/`: `<name>.json` is the source, finalised to `<name>.html` (interactive) and exported to `<name>.svg` with the viewer's own SVG export; the docs embed the SVG. User-level diagrams (`lettuce-garden-at-a-glance` and one `*-flow` per extra) are embedded from README and `docs/`. The developer wiring diagrams are embedded from ARCHITECTURE.md and the folder READMEs, with one exception: `lettuce-garden-overview`, one stack with every garden profile on, is also embedded from docs/architecture.md, since admins operate those containers.
- **When the wiring changes**, update that folder's README and regenerate its diagram; a change to containers, networks, volumes or routes also updates ARCHITECTURE.md, [docs/architecture.md](docs/architecture.md) and the `lettuce-garden-overview` diagram. A change to `garden/executor/run.mjs`, `GARDEN_INSTALL`, `executor_cron` or an `/executor/v1/` route updates [garden/executor/README.md](garden/executor/README.md). A change in what a person or admin sees or does updates the guide and its flow diagram.

## Sensitive data
This repo is public, and every operator runs it against their own Cloudflare account, domain and users. Nothing tracked may identify a real deployment.
- **Never commit secrets:** API tokens, tunnel tokens, `SESSION_SECRET`, VAPID private keys, passwords, or any value copied out of `config.env`, `stack.env` or `.secrets/`. That includes test fixtures, docs, commit messages and PR descriptions.
- **Never commit personal or deployment-specific values:** real email addresses, names, domains or hostnames, Cloudflare account, zone, app, tunnel or record ids, IP addresses, or local paths such as `/home/<user>/...`.
- **Use the placeholders:** stack names `alfred` and `hal`, agent name `lucius`, emails like `alfred@gmail.com` or `hal@example.com`, domains under `example.com` or `example.org`, and the stub ids (`acc1`, `zone1`, `tun1`, ...).
- **Redact first** when pasting command output, logs or rendered Compose config into docs, tests or issues.
- **Review the staged diff for these values before committing.** When a new kind of instance file appears, add it to `.gitignore` rather than committing it. The untracked instance data today: `config.env`, `.secrets/cloudflare-api-token`, `upstream/`, `stacks/<name>/stack.env` and `stacks/<name>/state/`.
