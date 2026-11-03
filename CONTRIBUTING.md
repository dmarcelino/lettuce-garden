# Contributing to lettuce-garden

This page is the starting point for working on lettuce-garden itself: the CLI, the overlays, the garden sidecars, the agent skills and the docs. To install and run it, start from the [README](README.md) and [docs/](docs/) instead.

## What you are changing
lettuce-garden composes [Lettuce](https://github.com/dmarchevsky/lettuce) and adds to it: one isolated stack per person, plus an opinionated set of extras (garden sidecars, MCP servers, agent skills, CLI commands). Lettuce is not ours, and every departure from its design is something `lettucectl upgrade` can break. So:
- Never modify Lettuce (the `upstream/` clone or the upstream repository) as part of a change here. When a change would fit better upstream, say so.
- Plug in only through Lettuce's supported seams; overlays restrict, never redefine; there is no second way into an agent; don't write into what Lettuce manages; keep upstream's defences on.

[AGENTS.md](AGENTS.md) is the authoritative rule book for these, for people and coding agents alike. Read its "Relationship to upstream" (which also names every upstream internal the repo depends on) and "Key invariants" (what `lettucectl check` enforces and why) before changing anything they cover. A new safety rule belongs in "Key invariants".

## Where things are
- **[ARCHITECTURE.md](ARCHITECTURE.md):** the whole stack: containers, networks, volumes, routes, who calls whom, how a stack is assembled, and a map of the repo.
- **One README per folder**, each with its files, how it plugs in, how to build and test it, and the upstream behaviour it depends on:

  | Folder | What |
  |---|---|
  | [lib/](lib/README.md) | the CLI's internals (`lettucectl` itself is at the root) |
  | [compose/](compose/README.md) | the overlays and their merge order, and the host's shared Gotenberg |
  | [vault/](vault/README.md) | the vault |
  | [gmail-triggers/](gmail-triggers/README.md) | Gmail triggers |
  | [code-server/](code-server/README.md) | the code editor: the gate, the editor's image and the shell's image |
  | [garden/](garden/README.md) | the garden's landing page, and [garden/executor/](garden/executor/README.md), the executor that runs in the app-server |
  | [garden-common/](garden-common/README.md) | the sign-in and request checks, styles and icons every garden service shares |
  | [pipelock/](pipelock/README.md) | the egress proxy's image and policy |
  | [doc-tools/](doc-tools/README.md) | the document tools' image and the PDF MCP server |
  | [agent-skills/](agent-skills/README.md) | the skills installed into every agent |
  | [backup/](backup/README.md) | the image `lettucectl backup` copies with |

- **Design records**, kept as written, with a note where the build later differed: [code-server/DESIGN.md](code-server/DESIGN.md), [gmail-triggers/DESIGN.md](gmail-triggers/DESIGN.md), [garden/DESIGN.md](garden/DESIGN.md) (the garden host and the garden-wide executor).
- **The user and admin guides** in [docs/](docs/): each feature's security model (what it guarantees and what it does not) is there; the folder README says how it is enforced.

## Building and testing
The CLI has no build step, and `lettucectl up` builds the sidecar images. Every test runs offline unless noted:
- **Before changing the CLI** (`lettucectl`, `lib/`, `scripts/`): `tests/run.sh` (docker, curl, dig, gh and secret-tool are stubbed; `tests/run.sh <pattern>` runs only the tests whose name contains it) and the `shellcheck` line in AGENTS.md "Commands".
- **Before changing a garden service** (`vault/`, `gmail-triggers/`, `code-server/`, `garden/`), `garden-common/` (which those four use) or `doc-tools/`: `tests/run-sidecar.sh <dir>` (needs Docker).
- **Before changing `pipelock/`:** `tests/run-pipelock.sh` (needs Docker, not the internet).
- **`lib/mcp-servers.mjs`:** `node --test tests/mcp-servers.test.mjs`; **the garden executor:** `node --test 'garden/executor/tests/*.test.mjs'`.
- **End to end**, against real images (Docker, and the internet for the first build; throwaway containers only): `scripts/doc-tools-e2e.sh`, and `scripts/code-server-e2e.sh --browser` before keeping a new code editor extension or setting.

AGENTS.md "Commands" has the full list, and its "Tests" section describes the stubs and helpers. CI (`.github/workflows/ci.yml`) runs shellcheck, `tests/run.sh`, the `mcp-servers` tests, every sidecar's tests and pipelock's on every branch push, and by hand from Actions ("Run workflow"). A new test script or linted file goes there too.

## Writing docs
Each audience has its own place:

| Audience | Where | What goes there |
|---|---|---|
| Someone arriving with no context | [README.md](README.md) | what lettuce-garden offers, requirements, quickstart, and the index of every doc |
| People who use a stack | [docs/user-guide.md](docs/user-guide.md), and each guide's "Using it" section | what they see and do, in the product's own words |
| Admins who install and run it | the rest of [docs/](docs/) | `lettucectl` commands, data and backups, troubleshooting, and each feature's security model: what it protects and what it does not |
| Developers | [ARCHITECTURE.md](ARCHITECTURE.md), the folder READMEs, the design records, this page | wiring, function and file names, how each guarantee is enforced, tests, maintenance (pin bumps, upgrades of bundled tools) |

The rule of thumb: running `lettucectl`, using a page or knowing what is protected belongs in `docs/`; editing tracked files, how something is wired, and tests belong with the developers. Each fact has one home, and the other places link to it.

- **Never claim more than is true.** When a security model is written in plain language, every limit stays in the user doc: pipelock is advisory, the vault's `env` delivery is not a boundary, agents are root in their stack, and a folder in the code editor must never be trusted. Only the mechanism moves to the folder README.
- **Keep the guides' file names and `docs/cloudflare-setup.md`'s step numbers.** `lettucectl`'s messages cite them (`docs/cloudflare-setup.md, step 5`, `docs/vault.md`, `docs/doc-tools.md`, `docs/google-setup.md`), and `tests/run.sh` asserts some of those messages.
- **Don't add files to `doc-tools/` or `garden-common/` for docs.** `doc_tools_tag` (`lib/common.sh`) hashes everything in `doc-tools/` but its tests and `README.md`, and `garden-common/.dockerignore` leaves out only `README.md`: any other file there rebuilds an image, and for `doc-tools/` refills the tools' volume and recreates every app-server.
- **Diagrams** are archify candidates in `docs/diagrams/`: `<name>.json` is the source, finalised to `<name>.html` (interactive) and exported to `<name>.svg` with the viewer's own SVG export; the docs embed the SVG. User-level diagrams (`lettuce-garden-at-a-glance` and one `*-flow` per extra) are embedded from README and `docs/`. The developer wiring diagrams are embedded from ARCHITECTURE.md and the folder READMEs, with one exception: `lettuce-garden-overview`, one stack with every garden profile on, is also embedded from docs/architecture.md, since admins operate those containers.
- **When the wiring changes**, update that folder's README and regenerate its diagram; a change to containers, networks, volumes or routes also updates ARCHITECTURE.md, [docs/architecture.md](docs/architecture.md) and the `lettuce-garden-overview` diagram. A change to `garden/executor/run.mjs`, its install or an `/executor/v1/` route updates [garden/executor/README.md](garden/executor/README.md). A change in what a person or admin sees or does updates the guide.

## Sensitive data
This repo is public, and every operator runs it against their own Cloudflare account, domain and users. Nothing tracked may identify a real deployment: no secrets, real emails, names, domains, Cloudflare ids, IP addresses or local paths, in code, tests, docs, commit messages or PR descriptions. Use the placeholders (stacks `alfred` and `hal`, agent `lucius`, `example.com` domains, the stub ids `acc1`, `zone1`, `tun1`...) and redact pasted output. The full rules are in AGENTS.md, "Sensitive and personal data".
