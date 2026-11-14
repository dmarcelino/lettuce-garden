# Lollo Rozzo

An opinionated, batteries-included way to run [Lettuce](https://github.com/dmarchevsky/lettuce) for several people on one host. Lettuce is a web app (a PWA) for Letta agents: assistants with their own memory, conversations and workspaces, which run on a letta-code server. It is built for one person. Lollo Rozzo gives each person an isolated stack of their own, behind Cloudflare Access with Google sign-in, plus extras: a secrets vault, Gmail triggers, a code editor, an egress proxy, Office and PDF tools, and agent skills.

You run it with `lolloctl`, a CLI that runs from a published image: who has a stack is one file, and one command (`lolloctl apply`) gives each person their own Cloudflare Tunnel, sign-in and DNS records, and starts their stack. The host needs only Docker.

![Each person signs in through Cloudflare to their own stack: Lettuce and their lollo pages](docs/diagrams/lollo-rozzo-at-a-glance.svg)

## What each person gets
- a private Letta agent server with its own memory, conversations, workspaces and secrets;
- the Lettuce web UI (a PWA) at `https://<name>.<your-domain>`;
- the lollo extras that are on, at their own address, `https://<name>-lollo.<your-domain>/`, behind the same Google sign-in.

Nobody can reach another person's stack; the one piece stacks share, the document tools' converter, is [covered in its guide](docs/doc-tools.md#security-model). **Cloudflare authenticates the people; Docker separates the assistants.** Upstream Lettuce needs no multi-user code for this, and the host publishes no ports: each stack's `cloudflared` connects outbound. [How it works](docs/architecture.md) explains the design, the two addresses, what runs and where the data lives, and its [glossary](docs/architecture.md#glossary) explains the terms these docs use.

## Lollo extras
Each extra is a Compose profile whose name starts with `lollo-`. Profiles are global: `lolloctl profiles <list>` turns them on or off for every stack at once.

| Profile | What each person gets | Guide |
|---|---|---|
| `lollo-vault` | An encrypted vault with Bitwarden's item types (logins, cards, identities, notes, SSH keys), importable from a Bitwarden export. Agents ask for a secret over MCP, and the person approves each request on the vault's page or by push notification, unless they let an everyday detail (an address, a note) be used without asking. Agents fill values in with placeholders, so they don't reach the AI provider. | [Vault](docs/vault.md) |
| `lollo-gmail-triggers` | Rules such as "when an email arrives in label X, ask agent Y to do Z". Lollo Rozzo polls Gmail read-only and starts each agent through Lettuce's own scheduler. Needs `google`. | [Gmail triggers](docs/gmail-triggers.md) |
| `lollo-news-radar` | Ask an agent to keep an eye on something (films, concerts, events, news on a topic) and report on a schedule. A small service checks the sources without a model; the agent reports only when something is new. | [News Radar](docs/news-radar.md) |
| `lollo-code-server` | VS Code in the browser on the agents' workspaces. Its terminals run in a separate container that has the agents' tools and is no stronger than an agent. | [Code editor](docs/code-server.md) |
| `lollo-pipelock` | An egress proxy for letta-code and the agents' tools, which checks where they connect (model calls included), scans pages fetched through it and signs a receipt for every decision. Advisory for now: the stack keeps its direct route out. | [Egress proxy](docs/egress-firewall.md) |
| `lollo-doc-tools` | Office and PDF tools as MCP servers: create, edit and convert Word, PowerPoint, Excel and PDF files in the agents' workspaces. | [Document tools](docs/doc-tools.md) |

With any of the vault, Gmail triggers, News Radar or the code editor on, the lollo address has a landing page linking to the pages that are on. Upstream's own profiles (`cloudflared`, which is required, `search`, `google` and others) are described in Lettuce's `docs/CONFIGURATION.md`, and summarised in [operations.md](docs/operations.md#optional-features).

## Requirements
- A Linux host (`amd64` or `arm64`) with Docker Engine 25 or later. That is all it needs: `lolloctl` runs from its own image, which brings Docker Compose and every other tool. The document tools (`lollo-doc-tools`) need an `amd64` host.
- A domain whose DNS is on Cloudflare. The free plan is enough.
- Cloudflare Zero Trust. The free plan covers up to 50 users; Cloudflare asks for payment details even for it, and charges nothing.
- A Google Cloud OAuth client for the Google login method (free).
- An account with a model provider for each person (Anthropic, OpenAI and others), paid per use: lollo-rozzo runs the agents, the provider runs the model.
- RAM for every stack you run. Each app-server is capped at 8 GiB, upstream's other services at 1 GiB and cloudflared at 256 MiB (`compose/hardening.yml`); the lollo's services at 128 MiB to 512 MiB, the code editor at 2 GiB for the editor plus 8 GiB for its shell, and the document tools' Gotenberg, one per host, at 2 GiB ([caps per service](docs/architecture.md#what-runs-in-each-stack)). These are caps, not reservations; measure idle use with `docker stats`.

## Quickstart
1. Do the one-time Cloudflare and Google setup: **[docs/cloudflare-setup.md](docs/cloudflare-setup.md)**. It takes about an hour of work. Moving your domain's DNS to Cloudflare then takes minutes to hours to become active, and up to 48 h; if DNSSEC is on, turning it off first adds 24-48 h. A domain you don't use for anything else avoids the risk of breaking its email.
2. Install `lolloctl`: create its data directory, owned by you, and put the `lolloctl` file of the latest release (this repository's GitHub Releases) in its `bin/`:
   ```bash
   sudo install -d -o "$USER" -g "$(id -gn)" -m 700 /srv/lollo-rozzo /srv/lollo-rozzo/bin
   curl -fsSL -o /srv/lollo-rozzo/bin/lolloctl https://github.com/<owner>/lollo-rozzo/releases/download/<version>/lolloctl
   chmod 755 /srv/lollo-rozzo/bin/lolloctl
   mkdir -p ~/.local/bin && ln -s /srv/lollo-rozzo/bin/lolloctl ~/.local/bin/lolloctl
   ```
   Check it against the release's `SHA256SUMS`, and with the `gh` CLI, where it comes from: `gh attestation verify /srv/lollo-rozzo/bin/lolloctl --repo <owner>/lollo-rozzo`. [docs/operations.md](docs/operations.md#installing-and-upgrading) says more.
3. Configure and start:
   ```bash
   lolloctl init                   # creates /srv/lollo-rozzo/config.env from the example; edit it
   $EDITOR /srv/lollo-rozzo/config.env   # set DOMAIN, PUSH_CONTACT_EMAIL (required), TZ and PROFILES
   lolloctl init                   # checks the Cloudflare setup step by step, downloads Lettuce, caches ids
   $EDITOR /srv/lollo-rozzo/stacks.conf  # one line per person: alfred alfred@gmail.com
   lolloctl apply                  # shows the plan, then creates and starts every stack
   ```
   The example's `PROFILES=cloudflared,search` is a working start (web search for the agents); add the [lollo extras](#lollo-extras) you want, e.g. `cloudflared,search,lollo-vault`. You can change the list later with `lolloctl profiles`.
4. Each person opens their URL, signs in with Google, and connects a model provider in Lettuce under **Settings -> Providers & models**. One API key per person keeps spending separate. Send them **[docs/user-guide.md](docs/user-guide.md)**.

Developing lollo-rozzo runs `./lolloctl` from a clone instead, which builds every image itself and keeps its data in the clone's `data/`: [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation
**Using a stack** (for the people you give one to)
- [Your Lettuce stack](docs/user-guide.md): addresses, signing in, first steps, what to keep in mind.
- The "Using it" section of each extra: [vault](docs/vault.md#using-it), [Gmail triggers](docs/gmail-triggers.md#using-it), [News Radar](docs/news-radar.md#using-it), [code editor](docs/code-server.md#using-it), [document tools](docs/doc-tools.md#using-it).

**Installing and running it** (for admins)
- [One-time Cloudflare and Google setup](docs/cloudflare-setup.md): the domain, Zero Trust, Google sign-in and the API token.
- [Operations](docs/operations.md): everyday commands, people, profiles, upgrades, backups, troubleshooting and security notes.
- [How it works](docs/architecture.md): the design, the two addresses per stack, what runs, where the data lives, limits.
- [Google for agents](docs/google-setup.md): Gmail, Calendar, Tasks and Contacts for the agents (`google` profile).
- The guide of each extra, with how to run it and its security model: [vault](docs/vault.md), [Gmail triggers](docs/gmail-triggers.md), [News Radar](docs/news-radar.md), [code editor](docs/code-server.md), [egress proxy](docs/egress-firewall.md), [document tools](docs/doc-tools.md).

**Developing lollo-rozzo**
- [CONTRIBUTING.md](CONTRIBUTING.md): where to start, the rules, tests and how the docs are organised.
- [ARCHITECTURE.md](ARCHITECTURE.md): the whole stack's containers, networks, volumes and routes, and a map of the repo.
- Each folder's README ([lib/](lib/README.md), [compose/](compose/README.md), [vault/](vault/README.md), [gmail-triggers/](gmail-triggers/README.md), [news-radar/](news-radar/README.md), [code-server/](code-server/README.md), [landing/](landing/README.md), [lollo-common/](lollo-common/README.md), [pipelock/](pipelock/README.md), [doc-tools/](doc-tools/README.md), [agent-skills/](agent-skills/README.md), [backup/](backup/README.md), [tests/](tests/README.md)), each with its rules.
- [AGENTS.md](AGENTS.md): the short directives for coding agents, linking to those rules.

## Licence
MIT, for the contents of this repository only. See [LICENSE](LICENSE).
