# Lettuce Garden

An opinionated, batteries-included way to run [Lettuce](https://github.com/dmarchevsky/lettuce) for several people on one host: one isolated stack per person behind Cloudflare Access, plus extras such as a secrets vault, Gmail triggers, a code editor, an egress proxy, MCP servers and agent skills. Each stack sits behind its own Cloudflare Tunnel and Cloudflare Access applications (one for Lettuce, one for the garden extras), and people sign in with Google.

Each person gets:
- a private Letta agent server with its own memory, conversations, workspaces and secrets;
- the Lettuce web UI (a PWA) at `https://<name>.<your-domain>`;
- the garden extras that are on, at their own address, `https://<name>-garden.<your-domain>/`, behind the same Google sign-in.

Nobody can reach another person's stack. **Cloudflare authenticates the people; Docker separates the assistants.** Upstream Lettuce needs no multi-user code for this.

```
              Internet
                 │  https://alfred.example.com      https://hal.example.com
                 ▼
   ┌───────────────────────── Cloudflare ─────────────────────────┐
   │  Access app "lettuce-alfred"      Access app "lettuce-hal"   │
   │  allow: alfred@…  (Google login)  allow: hal@…               │
   │  Tunnel "lettuce-alfred"          Tunnel "lettuce-hal"       │
   └──────────┬──────────────────────────────────┬────────────────┘
              │ outbound-only connections        │
   ┌──────────┴────────── your host ─────────────┴────────────────┐
   │ compose project lettuce-alfred    compose project lettuce-hal│
   │  cloudflared → app-server:8080    cloudflared → app-server   │
   │  app-server + bff (+ sidecars)    app-server + bff           │
   │  stacks/alfred/state/             stacks/hal/state/          │
   │                  no ports published on the host              │
   └──────────────────────────────────────────────────────────────┘
```

## Why this shape
- **No multi-user code.** Lettuce is single-user: agents in one stack can read each other's memory and tokens. Giving everyone a separate stack isolates them using what Docker already provides.
- **No open ports.** `cloudflared` connects outbound, so the host can run behind NAT, and its firewall can stay closed except for SSH. `compose/multi.yml` removes every published port.
- **Defence in depth.** Each stack is checked three times:
  - The Access policy admits only that stack's people.
  - The Lettuce BFF verifies the Access JWT against that application's own AUD tag, so a token minted for another stack is rejected.
  - `ALLOWED_USERS` must also match. `lettucectl users` updates the policy and `ALLOWED_USERS` together, because upstream says they must be kept in sync.
- **One command per person.** `lettucectl add <name> <email>` creates the Access policy and application, the tunnel and its routing, and the DNS record through the Cloudflare API, then starts the stack.

## Inside one stack
Every container of one stack with every profile on, its networks and volumes, and how they reach each other:

![Every container of one stack, its networks, volumes and routes](docs/diagrams/lettuce-garden-overview.svg)

**[docs/architecture.md](docs/architecture.md)** has the same in tables (containers, networks, volumes, tunnel routes, who calls whom) and links the interactive version.

## Garden profiles
Lettuce Garden's extras are Compose profiles whose names start with `garden-`. Profiles are global: `PROFILES` in `config.env` turns them on for every stack, and `lettucectl profiles <list>` changes them everywhere at once. Each one's folder README covers how it is built and wired; its guide in `docs/` covers how to use it and its security model.

| Profile | What each person gets | Where it runs | Details |
|---|---|---|---|
| `garden-vault` | An encrypted vault for addresses, cards, documents and logins. Agents ask for a secret over MCP, and the person approves each request on the vault's page or by push notification. | `garden-vault`, at `/vault/` on the garden host | [vault/](vault/README.md), [docs/vault.md](docs/vault.md) |
| `garden-gmail-triggers` | Rules such as "when an email arrives in label X, ask agent Y to do Z". The sidecar polls Gmail read-only, and the garden executor in the app-server starts each turn through letta-code's scheduler. Needs `google`. | `garden-gmail-triggers`, at `/gmail-triggers/` on the garden host | [gmail-triggers/](gmail-triggers/README.md), [docs/gmail-triggers.md](docs/gmail-triggers.md) |
| `garden-code-server` | VS Code in the browser on the agents' workspaces. Its terminals are SSH sessions into a shell that has the agents' tools and is no stronger than an agent. | the gate `garden-code-server` at `/code-server/` on the garden host, the editor and the shell behind it | [code-server/](code-server/README.md), [docs/code-server.md](docs/code-server.md) |
| `garden-pipelock` | An egress proxy for the agents' tools, which checks where they connect, scans pages fetched through it and signs a receipt for every decision. Advisory for now: the stack keeps its direct route out. | `garden-pipelock`, inside the stack only | [pipelock/](pipelock/README.md), [docs/egress-firewall.md](docs/egress-firewall.md) |
| `garden-doc-tools` | Office and PDF tools as MCP servers: create, edit and convert Word, PowerPoint, Excel and PDF files in the agents' workspaces. | in the app-server, from a read-only volume, plus the host's one Gotenberg | [doc-tools/](doc-tools/README.md), [docs/doc-tools.md](docs/doc-tools.md) |

With any of `garden-vault`, `garden-gmail-triggers` or `garden-code-server` on, the `garden` service serves the landing page at the root of the garden host, `https://<name>-garden.<your-domain>/`, linking to the pages that are on ([garden/](garden/README.md)). Upstream's own profiles (`cloudflared`, which is required, `search`, `google` and others) are described in Lettuce's `docs/CONFIGURATION.md`.

Shared parts have their own READMEs: [compose/](compose/README.md) (the overlays and their merge order), [garden-common/](garden-common/README.md) (sign-in and request checks the garden services share), [agent-skills/](agent-skills/README.md) (skills installed into every agent), [backup/](backup/README.md) (the backup image) and [lib/](lib/README.md) (the CLI's internals).

## Requirements
- Linux host with Docker Engine and Docker Compose >= 2.24, plus `git`, `curl`, `jq`, `openssl` and `dig` (`dnsutils` / `bind-utils`).
- A domain whose DNS is on Cloudflare. The free plan is enough.
- Cloudflare Zero Trust. The free plan covers up to 50 users.
- A Google Cloud OAuth client for the Google login method (free).
- RAM for every stack you run. Each app-server is capped at 8 GiB, most other services at 1 GiB and cloudflared at 256 MiB (`compose/hardening.yml`). These are caps, not reservations; measure idle use with `docker stats`.

## Quickstart
1. Do the one-time Cloudflare and Google setup: **[docs/cloudflare-setup.md](docs/cloudflare-setup.md)**. Allow about an hour, most of it waiting for DNS.
2. Configure and start:
   ```bash
   git clone <this repo> ~/lettuce && cd ~/lettuce
   ./lettucectl init                 # creates config.env from the example; edit it
   $EDITOR config.env                # DOMAIN, PROFILES, TZ, PUSH_CONTACT_EMAIL
   ./lettucectl init                 # clones Lettuce, checks the token, caches Cloudflare ids
   ./lettucectl add alfred alfred@gmail.com
   ./lettucectl add hal hal@example.com
   ```
3. Each person opens their URL, signs in with Google, and connects a model provider in Lettuce under **Settings -> Providers & models**. One API key per person keeps spending separate.

Day-to-day commands, optional features, upgrades, backups and troubleshooting are in **[docs/operations.md](docs/operations.md)**. To give agents Gmail, Calendar, Tasks and Contacts, see **[docs/google-setup.md](docs/google-setup.md)**. To give each person an encrypted vault for their address, cards, documents and logins, which agents may use only when that person agrees, see **[docs/vault.md](docs/vault.md)**. To send the agents' web traffic through a proxy that checks where it goes and signs a receipt for each request (advisory for now), see **[docs/egress-firewall.md](docs/egress-firewall.md)**. To let agents create, edit and convert Word, PowerPoint, Excel and PDF files, see **[docs/doc-tools.md](docs/doc-tools.md)**. To let each person open their agents' workspaces in VS Code in the browser, with a terminal that has the agents' tools, see **[docs/code-server.md](docs/code-server.md)**.

## What lives where
| Path | Tracked | What |
|---|---|---|
| `lettucectl`, [`lib/`](lib/README.md) | yes | the CLI |
| `compose/hardening.yml` | yes | `no-new-privileges`, memory/pid caps for every service, no `NET_RAW` where agents run, letta-code self-update off |
| `compose/multi.yml` | yes | drops all published ports, so stacks never collide |
| `compose/vault.yml`, [`vault/`](vault/README.md) | yes | the optional vault (`garden-vault` profile) and its source |
| `compose/gmail-triggers.yml`, [`gmail-triggers/`](gmail-triggers/README.md) | yes | optional Gmail triggers (`garden-gmail-triggers` profile) and their source |
| `compose/pipelock.yml`, `compose/pipelock-proxy.yml`, [`pipelock/`](pipelock/README.md) | yes | the optional egress proxy (`garden-pipelock` profile), its proxy settings and its image |
| `compose/code-server.yml`, [`code-server/`](code-server/README.md) | yes | the optional code editor (`garden-code-server` profile): its gate, the editor's image and the shell's image |
| `compose/garden.yml`, [`garden/`](garden/README.md) | yes | the garden's landing page (on with `garden-vault`, `garden-gmail-triggers` or `garden-code-server`) and the garden executor, which runs Gmail triggers' requests and sends the landing page the agent list |
| `compose/doc-tools.yml`, `compose/shared/gotenberg.yml`, [`doc-tools/`](doc-tools/README.md) | yes | the optional Office and PDF tools (`garden-doc-tools` profile): the app-server's mount and network, the host's shared Gotenberg, and the tools' image and PDF server |
| [`garden-common/`](garden-common/README.md) | yes | code the garden services share (sign-in checks, the executor API's checks, page styles, icons) |
| [`agent-skills/`](agent-skills/README.md) | yes | skills `lettucectl skills` installs for every agent of every stack |
| [`backup/`](backup/README.md) | yes | the image `lettucectl backup` copies with (it also fills the doc-tools volume) |
| `docs/` | yes | guides, design records and [diagrams](docs/diagrams/) (archify: `.json` source, interactive `.html`, `.svg`) |
| `config.env.example` | yes | template for `config.env` |
| `scripts/dns-preflight.sh` | yes | compares two nameservers before and after moving a domain |
| `tests/` | yes | offline tests (`tests/run.sh`; docker, curl and dig are stubbed) the sidecars' tests (`tests/run-sidecar.sh vault`, `tests/run-sidecar.sh gmail-triggers`, `tests/run-sidecar.sh code-server`, `tests/run-sidecar.sh garden`, `tests/run-sidecar.sh doc-tools`) and pipelock's (`tests/run-pipelock.sh`) |
| `config.env` | **no** | your settings plus cached Cloudflare ids (mode 600) |
| `.secrets/cloudflare-api-token` | **no** | Cloudflare API token (mode 600) |
| `upstream/` | **no** | Lettuce, cloned at `LETTUCE_REF` |
| `stacks/<name>/stack.env` | **no** | that stack's secrets and Cloudflare ids (mode 600) |
| `stacks/<name>/state/` | **no** | that stack's data: `letta-home/`, `letta-data/`, `workspaces/`, and `garden/` for Gmail trigger rules |

The compose files of each stack are applied in this order ([compose/README.md](compose/README.md) describes each):
1. `upstream/docker/compose.yml`
2. `compose/hardening.yml`
3. `compose/multi.yml`
4. `compose/vault.yml` (adds nothing unless the `garden-vault` profile is on)
5. `compose/gmail-triggers.yml` (adds nothing unless the `garden-gmail-triggers` profile is on)
6. `compose/pipelock.yml` (adds nothing unless the `garden-pipelock` profile is on)
7. `compose/code-server.yml` (adds nothing unless the `garden-code-server` profile is on)
8. `compose/garden.yml` (adds nothing unless a garden page profile is on: `garden-vault`, `garden-gmail-triggers` or `garden-code-server`)
9. `compose/doc-tools.yml` (merged only when the `garden-doc-tools` profile is on)
10. `compose/pipelock-proxy.yml` (merged only when the `garden-pipelock` profile is on)

Each stack runs as Compose project `lettuce-<name>` with `--env-file stacks/<name>/stack.env`. With `garden-doc-tools`, the host also runs the Compose project `lettuce_shared` (one Gotenberg for every stack).

## Limits and caveats
- **Version pinning.** Lettuce is compiled against one exact letta-code release and pins the server image to it. Move between upstream tags only with `lettucectl upgrade <tag>`.
- **Shared app-server image.** All stacks share one image, whose contents depend on `PROFILES`. That is why profiles are global, and why `check` refuses a stack whose profiles differ.
- **Agent web apps.** Upstream's agent-served apps on ports 3000-3099 are not exposed: they are unauthenticated, and one host cannot publish the same range twice.
- **Data on disk.** A stack's data is only as private as the host. Whoever has root on it can read every stack.

## Licence
MIT, for the contents of this repository only. See [LICENSE](LICENSE).
