# Agents Guidance

This file provides guidance to agents when working with code in this repository. It is the short version: directives and commands. The rules themselves, with their reasons and how each is enforced, live in the developer docs; every directive links to its rule.

## What this is
Lettuce Garden is an opinionated, batteries-included way to run [Lettuce](https://github.com/dmarchevsky/lettuce) for several people on one host: one isolated stack per person behind Cloudflare Access, plus extras such as a secrets vault, Gmail triggers, News Radar, a code editor, an egress proxy, MCP servers and agent skills. `lettucectl`, a Bash CLI, runs the single-user stacks, one per person. Each stack is its own Docker Compose project behind its own Cloudflare Tunnel and two Cloudflare Access applications (Google login), one for Lettuce and one for the garden pages. There is no multi-user code: isolation comes from separate stacks, authentication from Cloudflare.

## Read before you change
- **Before editing a folder, read its README's "Rules" section, and before you finish run every command in its "Build and test".** Each folder with a README has an `AGENTS.md` pointer to it (Claude Code loads its `CLAUDE.md` symlink when it works there). Before a cross-cutting change (an upstream seam, `check_stack`, a new profile or garden sidecar, a Lettuce upgrade), read the matching section of [CONTRIBUTING.md](CONTRIBUTING.md).
- **When a directive here and a developer doc disagree, the developer doc wins**; fix this file.
- [ARCHITECTURE.md](ARCHITECTURE.md) maps the whole stack and the repo.

The "Build and test" sections are the one list of checks; this table only links to them.

| Folder | Rules | Checks |
|---|---|---|
| `lettucectl`, `lib/`, `scripts/` | [lib/README.md](lib/README.md#rules), [CONTRIBUTING.md](CONTRIBUTING.md#what-check_stack-enforces) | [lib](lib/README.md#build-and-test) |
| `compose/` | [compose/README.md](compose/README.md#rules) | [compose](compose/README.md#build-and-test) |
| `vault/` | [vault/README.md](vault/README.md#rules) | [vault](vault/README.md#build-and-test) |
| `gmail-triggers/` | [gmail-triggers/README.md](gmail-triggers/README.md#rules) | [gmail-triggers](gmail-triggers/README.md#build-and-test) |
| `news-radar/` | [news-radar/README.md](news-radar/README.md#rules) | [news-radar](news-radar/README.md#build-and-test) |
| `code-server/` | [code-server/README.md](code-server/README.md#rules) | [code-server](code-server/README.md#build-and-test) |
| `garden/` | [garden/README.md](garden/README.md#rules) | [garden](garden/README.md#build-and-test) |
| `garden/executor/` | [install](garden/executor/README.md#install), [trust](garden/executor/README.md#trust) | [executor](garden/executor/README.md#build-and-test) |
| `garden-common/` | [garden-common/README.md](garden-common/README.md#rules) | [garden-common](garden-common/README.md#build-and-test) (the five suites that use it) |
| `pipelock/` | [pipelock/README.md](pipelock/README.md#rules) | [pipelock](pipelock/README.md#build-and-test) |
| `doc-tools/` | [doc-tools/README.md](doc-tools/README.md#rules) | [doc-tools](doc-tools/README.md#build-and-test) |
| `agent-skills/` | [agent-skills/README.md](agent-skills/README.md#rules) | [agent-skills](agent-skills/README.md#build-and-test) |
| `backup/` | [backup/README.md](backup/README.md#rules) | [backup](backup/README.md#build-and-test) |
| `release/`, `VERSION`, `REBUILD`, `.github/workflows/publish.yml` | [release/README.md](release/README.md#rules) | [release](release/README.md#build-and-test) |
| `tests/` | [tests/README.md](tests/README.md) (the stubs and helpers) | [tests](tests/README.md#build-and-test) |
| `docs/`, any `*.md` | [CONTRIBUTING.md](CONTRIBUTING.md#writing-docs) | `node scripts/check-doc-links.mjs` |

## Directives

### Upstream ([CONTRIBUTING.md](CONTRIBUTING.md#relationship-to-upstream))
- Never modify Lettuce without an explicit request: neither the `upstream/` clone nor the upstream repository (code, issues, PRs). Say when a change would fit better upstream, and stop there.
- Plug in only through the supported seams. A new dependency on a Lettuce internal is redesigned away, or adds a row to the upstream seams named today in [CONTRIBUTING.md](CONTRIBUTING.md#upstream-seams).
- Overlays restrict, never redefine: never replace an upstream service's image, entrypoint, command or internal env. The written exceptions are `pipelock-proxy.yml` (proxy variables on three upstream services) and `doc-tools.yml` (a mount and a network on the app-server); leave them be. A new one needs a written reason in [compose/README.md](compose/README.md#rules) first, then a mention here.
- No second way into an agent: no command or sidecar opens a connection to the app-server or starts a turn; `compose exec` for files and read-only CLI calls is the limit. Gmail triggers and News Radar, through the garden executor and letta-code's scheduler, are the only exceptions. Another sidecar starting turns, or a new way around the BFF, needs a written reason in CONTRIBUTING first, and goes under `/<sidecar>/` on the garden host, never on Lettuce's.
- Don't write into what Lettuce manages (settings, MCP list, memory, workspaces). Where we must write, we own a namespace: `skills` its manifest, the executor install its own files; the one write into the MCP list is `mcp`, with its three kinds of entries. Never turn an upstream feature on by editing its files.
- Keep upstream's defences on: strictest auth settings, no disabled boot check, nothing secret under `stacks/<name>/state/`, never shorten the BFF's `stop_grace_period`.
- Name new profiles `garden-<name>`. Move `LETTUCE_REF` only with `lettucectl upgrade <tag>`, and walk [the upgrade checklist](CONTRIBUTING.md#upgrading-lettuce_ref).

### The CLI ([lib/README.md](lib/README.md#rules))
- Never source `config.env` or `stack.env`; use `env_get`, `env_set`, `env_unset` and `cfg`. No quotes or inline comments in them. `stacks.conf` too: `conf_entries`, `conf_set`, `conf_unset`.
- Instance paths go through `LETTUCE_DATA` (`config_file`, `stack_dir`, `upstream_dir`...), code paths through `LETTUCE_HOME`; never mix them ([rule](lib/README.md#code-and-data-are-apart)).
- `apply` never removes a stack or changes a profile; `add`, `users` and `remove` keep `stacks.conf` in step ([rule](lib/README.md#stacksconf-is-kept-in-step-and-apply-never-removes)).
- `up` never builds a published image in release mode, and the shell's base is the app-server's image by name, never `service:app-server` ([rule](lib/README.md#up-builds-only-what-is-built-on-this-host)).
- In release mode every host path lettucectl hands Docker is mounted at the same path in its container; a new one goes in the wrapper and through `require_mounted` ([rule](lib/README.md#every-host-path-is-the-same-inside-lettucectls-container)).
- Every Compose call goes through `compose NAME ...` (or `compose_env`, `compose_shared`); plain `docker` through `host_docker`.
- Keep `add`'s creation order and `remove`'s reverse order, storing each id as soon as it exists and unsetting it only on success; tests assert the exact order. `up` makes no Cloudflare call.
- Never put the Cloudflare API token, or any token, in argv: tokens go on stdin or as `-e NAME`.
- `users` changes the Access policy and `ALLOWED_USERS` together. `cf_app_body` is the one definition of both Access applications' settings.
- Profiles change only through `lettucectl profiles`, for every stack at once; never tell operators to edit the lists by hand.
- Never pass `COMPOSE_PROFILES` or `GARDEN_PROFILES` to a sidecar other than `garden`: a profile change would recreate it, and recreating the vault locks it.
- Never rotate the VAPID keys: every push subscription would break.
- Use `lettucectl restart`, never a single-service restart; the BFF alone (`refresh_mcp_skill`) is the one exception.
- `toolbox` is a `docker run`, never a `compose exec`, and keeps the operator's credentials out of the stack.
- A new safety rule goes into `check_stack`, with a `test_check_*` test, and into [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-rule) or its folder's "Rules". Remember that a container Docker restarts on its own is not re-checked.

### Features
- **Vault** ([rules](vault/README.md#rules)): never add an MCP tool that edits secrets or bypasses approval; never write vault data, keys or the passphrase unencrypted or to logs; never mount `vault-data` elsewhere (only `backup`, read-only); agent activity must not extend the auto-lock; an erase without the passphrase always waits its delay and tells every device.
- **Code editor** ([rules](code-server/README.md#rules)): nothing an agent wrote may run in the editor's container. Never add an extension, setting or flag that runs code, trusts a workspace or opens a port.
- **Garden pages** ([garden-common rules](garden-common/README.md#rules), [garden rules](garden/README.md#rules)): every page checks Fetch Metadata (`check_fetch`) and sets COOP; the landing page has no script; every garden service accepts only `CF_GARDEN_AUD` and `GARDEN_ORIGIN`.
- **Executor** ([install](garden/executor/README.md#install), [trust](garden/executor/README.md#trust)): only `sync_executor` installs or removes it (from `up`, `add` and `profiles`), through `compose exec`, replacing only its own files; its token is never rotated automatically. The executor runs `letta` from an argument array, never a shell. It and the sidecars share no directory and treat each other's data as untrusted.
- **News Radar** ([rules](news-radar/README.md#rules)): a monitor starts a turn only at its schedule, when something is new, within the daily cap; the service never creates `letta cron` tasks; specs are data, never code, and no page script runs; the fetcher reaches public addresses only and honours robots.txt; no model in the service.
- **Pipelock** ([rules](pipelock/README.md#rules)): it is advisory; never describe it as a boundary. A service agents or the executor call inside the stack goes in `NO_PROXY`.
- **Doc-tools** ([rules](doc-tools/README.md#rules)): lettucectl owns the image, volume and networks; Compose only uses them. Only the app-server mounts the tools.
- **Skills** ([rules](agent-skills/README.md#rules)): only `skills` installs them, replacing only its own; names never match upstream's; text stays generic.
- **Backup** ([rules](backup/README.md#rules)): it reads the state only through its own container, never into this repo.
- **Releases** ([rules](release/README.md#rules)): images are named by content, never by release; `doc-tools` and `code-server/exec` are never published; no `latest`; a file lettucectl reads at run time goes into the `lettucectl` image.

### Docs ([CONTRIBUTING.md](CONTRIBUTING.md#writing-docs))
- README introduces and indexes, `docs/` is for users and admins, and CONTRIBUTING, ARCHITECTURE and the folder READMEs are for developers. This file holds directives only.
- A rule change edits its developer doc, then the directive here if its one-line form changed.
- Never claim more than is true: pipelock advisory, the vault's placeholders not a boundary, never trust a folder in the code editor.
- Keep the guides' file names and `docs/cloudflare-setup.md`'s step numbers: `lettucectl` messages cite them and tests assert some.
- Never add a file other than `README.md`, `AGENTS.md` and `CLAUDE.md` to `doc-tools/` or `garden-common/` for docs: it would rebuild images.
- A new folder README gets an `AGENTS.md` pointer and a `CLAUDE.md` symlink to it; `node scripts/check-doc-links.mjs` fails without them.
- A wiring change updates the folder README and its diagram; a container, network, volume or route change also updates ARCHITECTURE.md, docs/architecture.md and `lettuce-garden-overview`; a change to what people or admins see updates the guide and its flow diagram.

### Sensitive data ([CONTRIBUTING.md](CONTRIBUTING.md#sensitive-data))
- Never commit secrets or values from `config.env`, `stack.env` or `.secrets/`, nor real emails, names, domains, Cloudflare ids, IPs or `/home/<user>` paths: not in code, tests, docs, commits or PR descriptions.
- Use the placeholders: `alfred`, `hal`, `lucius`, `alfred@gmail.com`, `hal@example.com`, `example.com`, the stub ids. Redact pasted output.
- Review the staged diff before committing; add new instance files to `.gitignore`.

## Commands
```bash
tests/run.sh                 # all offline tests (docker, curl, dig, gh, secret-tool are stubbed)
tests/run.sh <pattern>       # only tests whose function name contains <pattern>, e.g. tests/run.sh test_add_
shellcheck -x lettucectl lib/*.sh scripts/*.sh pipelock/entrypoint.sh tests/run.sh tests/run-sidecar.sh tests/run-pipelock.sh tests/stubs/* doc-tools/bin/* doc-tools/lib/launch.sh code-server/editor/bin/* code-server/editor/entrypoint.sh code-server/editor/tests/test.sh release/content-tag release/lettucectl-wrapper.sh
tests/run-sidecar.sh vault            # vault tests (pytest in its image's `test` stage; needs Docker)
tests/run-sidecar.sh vault -k vault   # extra args go to pytest
tests/run-sidecar.sh gmail-triggers    # Gmail triggers' tests (pytest; the executor's run with garden)
tests/run-sidecar.sh news-radar         # News Radar's tests (pytest: schedules, fetcher, extractors, reports, page)
tests/run-sidecar.sh code-server       # the code editor's gate (pytest), plus the editor image's tests when no args are given
node --test 'garden/executor/tests/*.test.mjs'         # garden executor tests on the host
tests/run-pipelock.sh                  # the pipelock image and config against a real pipelock (needs Docker, not the internet)
node --test tests/mcp-servers.test.mjs # lib/mcp-servers.mjs against temp settings files
node --test tests/check-doc-links.test.mjs # the link checker against temp Markdown trees
node scripts/check-doc-links.mjs       # every relative link and anchor resolves; every folder README has its agents' pointer
tests/run-sidecar.sh garden            # the landing page and its executor API (pytest), plus the garden executor's tests (node --test in node:22.22.0-slim) when no args are given
tests/run-sidecar.sh doc-tools         # the PDF MCP's tests (pytest in the doc-tools image's `test` stage, Debian 12 like the app-server)
```
The same block is in [CONTRIBUTING.md](CONTRIBUTING.md#building-and-testing); keep both and `.github/workflows/ci.yml` in step. The CLI has no build step; from a clone `lettucectl up` builds the sidecar images, and a release pulls them. A clone's instance data is in its untracked `data/` (`config.env`, `stacks.conf`, `.secrets/`, `upstream/`, `stacks/`), never committed.
