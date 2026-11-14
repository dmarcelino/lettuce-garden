# Operations

## Everyday
```bash
lolloctl list                 # stacks, hostnames (Lettuce's and the lollo's), allowed users
lolloctl status               # containers and health per stack
lolloctl logs alfred          # follow all services; or: logs alfred cloudflared
lolloctl up all               # check, build what is built here, start (idempotent; also applies config changes)
lolloctl restart alfred       # down + up
lolloctl compose alfred ps    # any docker compose command for one stack
```

- Use `restart`, never `compose <name> restart app-server`. The BFF shares the app-server's network namespace and dies when the app-server is recreated on its own. `up` and `restart` always act on the whole stack. (`mcp` restarts the BFF alone, which is safe: the app-server keeps its namespace.)
- After a host reboot, Docker brings the stacks back by itself (`restart: unless-stopped`).
- `up` is offline: no Cloudflare call, no DNS lookup.

## People
Who has a stack, and who may sign in to it, is `stacks.conf` in the data directory, one line per stack:
```
# name   emails (comma-separated)
alfred   alfred@gmail.com
hal      hal@example.com,hal@work.example.com
```
```bash
lolloctl apply --dry-run      # what would change: stacks to add, each address gained or lost
lolloctl apply                # shows the same plan, asks, then does it, then up all and skills all
lolloctl apply --yes          # without asking, for scripts
```
`apply` adds each stack that is in the file but not set up (one that stopped half-way is resumed, with the file's addresses), changes the addresses of each stack whose list differs, then runs `up all` and `skills all`. A stack that fails does not stop the others: `apply` carries on, then fails at the end naming what did not finish, and running it again picks up from there. It never removes a stack: one that is not in the file is reported, with the command to remove it. It never changes the profiles either ([below](#optional-features)).

The single commands do the same for one stack, and keep `stacks.conf` in step, so a later `apply` never undoes them:

| Task | Command |
|---|---|
| Add a person | `lolloctl add <name> <email>`. One stack per person; extra addresses are comma-separated. |
| Change who may sign in to a stack | `lolloctl users <name> a@x.com,b@y.com`. This replaces the list. |
| Remove a stack, keep its data | `lolloctl remove <name>` |
| Remove a stack and its data | `lolloctl remove <name> --purge` |

`remove` asks you to type the name, and drops the stack from `stacks.conf`. It stops the stack, then deletes its DNS record, tunnel, Access application and policy. Anything it cannot delete stays recorded in `stack.env`, so you can run `remove` again; until then `apply` reports the stack.

When `add` stops half-way (a network error, a missing token permission), run the same command again, or `apply`. It resumes from the ids recorded in `stacks/<name>/stack.env`.

## Model providers and agents
Each person sets up their own stack in the Lettuce UI:
- **Settings -> Providers & models** to connect a provider.
- The sidebar to create agents.

Provider keys live in that stack's `state/letta-data`, never in this repo.

To look around as an agent does, open a shell in its workspace:
```bash
lolloctl shell alfred lucius   # agent name (any case) or id; without one, /work
```
Agents run their Bash tool as root in the app-server with no sandbox. Each agent's tool starts in `/work/<agent-id>` with `AGENT_ID`, `AGENT_NAME` and `MEMORY_DIR` set, and `shell` sets the same. Like an agent, the shell can read every agent's workspace, memory and conversations in that stack, plus `/root/.letta` (provider and Claude/Codex credentials). It is close to what the agent sees but not identical: letta-code also sets the conversation id, the agent's secrets and a few `PATH` entries. Commands can be piped in: `lolloctl shell alfred lucius <<<'ls'`.

### Working on an agent's workspace with your own tools
`shell` is the wrong place for your own credentials: agents share that container as root, so they could read anything you bring in while the shell is open. `toolbox` instead starts a throwaway container from the stack's app-server image with only that agent's workspace mounted:
```bash
lolloctl toolbox alfred lucius          # bash in /work/<agent-id>
lolloctl toolbox alfred lucius claude   # straight into Claude Code
```
It brings in whatever of these the host has, read-only, and says which:
- your ssh-agent (`SSH_AUTH_SOCK`; the key itself never enters) and `~/.ssh/known_hosts`;
- `~/.gitconfig`, and with a logged-in `gh`, a GitHub token git uses for `https://github.com`;
- Claude Code's `CLAUDE.md`, `settings.json`, `skills`, `agents` and `commands` (from `~/.claude`, or `CLAUDE_CONFIG_DIR`), and a Claude token: `CLAUDE_CODE_OAUTH_TOKEN` if you set it, otherwise the keyring entry `service claude-toolbox`. Set that entry up once with `claude setup-token`, then `secret-tool store --label='Claude toolbox' service claude-toolbox` (Ubuntu package `libsecret-tools`).

`/root` and `/tmp` are in memory, so history, caches and logins made inside are gone on exit; only what you write to the workspace stays, and the agent sees it. Tokens reach the container through docker's environment, never its command line. The container is not on the stack's network and is labelled `lolloctl.toolbox=<name>`.

For anything else, write `~/.config/lolloctl/toolbox` (mode 600; it is parsed, never sourced, and words are split on spaces):
```
# shared skills that ~/.claude/skills links to keep their host path
mount ~/team/shared-skills
mount ~/.aws/config ~/.aws/config
mount ~/scratch /scratch rw
secret AWS_SESSION_TOKEN secret-tool lookup service aws-session
env AWS_PROFILE=dev
```
`mount SRC [DEST] [rw]` is read-only unless `rw`, and `DEST` defaults to `SRC`; `~` is your home in `SRC` and `/root` in `DEST`. `secret NAME COMMAND...` runs the command without a shell and passes its output as `NAME`; a secret overrides a built-in of the same name.

The tools agents installed (`/opt/agent-tools`: `npm i -g`, `go install`, `uv tool install`, downloaded binaries) are left out unless you ask for them, before the stack name:
```bash
lolloctl toolbox --tools alfred lucius      # the agents' tools, read-only: what `shell` has
lolloctl toolbox --tools=rw alfred lucius   # installs land there, for every agent of the stack
```
Without `--tools=rw`, an install inside the toolbox is gone on exit. To install something that needs none of your credentials, `lolloctl shell alfred` is the better place: your tokens never meet what agents wrote.

Three limits:
- Only someone with Docker access on the host can run `toolbox`. Docker access is root on the host, so a stack's web-only users cannot, by design.
- The workspace is the agent's to write, and what you run there runs with your credentials: git hooks, `.git/config` (`core.sshCommand`, credential helpers), package scripts, Makefiles. Look before you push, and prefer short-lived, narrowly scoped tokens.
- `--tools` goes further: every agent of the stack writes those tools, and npm's global config (`/opt/agent-tools/etc/npmrc`) and cargo's (`/opt/agent-tools/cargo/config.toml`) are read on every call, so with it the agents' code runs with your credentials whenever you use them. With `rw`, whatever you write there is every agent's to read: never `cargo login` (its token goes to `CARGO_HOME`, on that mount) or `npm config set --location=global` with a token.

### Agent skills
`agent-skills/` holds skills this repo gives every agent, for example `using-vault-secrets` (the vault's `vault-run` and `vault-fill` helpers). Scheduled jobs that survive restarts are upstream's, in its `installing-tools` skill. After adding or changing one, push them to the running stacks:
```bash
lolloctl skills all           # or one stack: skills alfred
```
They go into each stack's global skills (`/root/.letta/skills`), which every agent of that stack loads. `skills` replaces or removes only what it installed before (listed in `.lolloctl-skills` there; a skill deleted from `agent-skills/` is removed from the stack on the next run), so upstream's skills and the ones enabled in Settings stay. A name upstream Lettuce also ships is refused, because its BFF would overwrite ours. `up` does not install skills; run `skills` after setting up a new stack. Writing a skill: [agent-skills/README.md](../agent-skills/README.md).

## Optional features
Optional features are compose profiles, the same list for **all** stacks. Upstream's are described in its `docs/CONFIGURATION.md`; lollo-rozzo's start with `lollo-`, and each has a guide.

| Profile | What it adds | When you turn it on | Guide |
|---|---|---|---|
| `search` | SearXNG + DuckDuckGo page reader for the agents' web tools | | upstream |
| `claude`, `codex` | Claude Code CLI workers; `codex` does the same for Codex | | upstream |
| `google` | Gmail/Calendar/Tasks/Contacts sidecar | It needs a Google OAuth client, and each person connects their own account. | **[google-setup.md](google-setup.md)** |
| `telegram` | Telegram channel gateway | | upstream |
| `lollo-vault` | A personal vault per stack, at `https://<name>-lollo.<domain>/vault/` | Run `lolloctl skills all` for the `using-vault-secrets` skill (`vault-run`, `vault-fill`, and how to wait for an approval). Agents reach it as the `vault` server in Settings -> MCP: `add` registers it for new stacks; for existing ones run `lolloctl mcp all` (once per stack, restarting the BFF of each stack it changed). | **[vault.md](vault.md)** |
| `lollo-gmail-triggers` | Gmail rules each person sets up at `https://<name>-lollo.<domain>/gmail-triggers/` | Needs `google` (`profiles` and `check` refuse it otherwise) and each person's Google account connected in Lettuce. `up` installs a small executor in the app-server; dropping the profile removes it and keeps the rules. | **[gmail-triggers.md](gmail-triggers.md)** |
| `lollo-news-radar` | News Radar: monitors agents set up and report on a schedule; each person sees and deletes them at `https://<name>-lollo.<domain>/news-radar/` | Run `lolloctl skills all` for the `news-radar-sources` skill, then `lolloctl mcp all`, which adds the `news-radar` server to each stack's Settings -> MCP (once per stack; `add` does it for new stacks). `up` installs the executor in the app-server. Its settings (default report times, the daily cap, the shortest fetch interval) are `NEWS_RADAR_*` keys in `config.env`, which `up` copies to every stack. | **[news-radar.md](news-radar.md)** |
| `lollo-doc-tools` | Office and PDF tools for the agents: two stdio MCP servers mounted read-only into every app-server from one shared volume, plus one shared Gotenberg per host | Run `lolloctl skills all`, then `lolloctl mcp all`, which adds the two servers to each stack's Settings -> MCP (once per stack; `add` does it for new stacks) and restarts the BFF of each stack it changed, so agents see them. | **[doc-tools.md](doc-tools.md)** |
| `lollo-code-server` | VS Code in the browser at `https://<name>-lollo.<domain>/code-server/`, on that stack's agent workspaces, with a terminal in a separate container that has the agents' tools | Every `profiles` run keeps each stack's Access cookies HttpOnly (it re-applies every stack's Access applications). | **[code-server.md](code-server.md)** |
| `lollo-pipelock` | An egress proxy per stack for letta-code's own calls (the model provider's included), the agents' shell tools, subagents, SearXNG and the DuckDuckGo reader, with a signed receipt for every request (`lolloctl receipts`). Advisory for now. | Run `lolloctl skills all` for the `fetching-web-pages` skill. Agents' model calls now depend on it: a model server on a private address other than `host.docker.internal` is refused unless it is in `PIPELOCK_DIRECT`. | **[egress-firewall.md](egress-firewall.md)** |

```bash
lolloctl profiles                              # show config.env and every stack
lolloctl profiles cloudflared,search,google    # set the list everywhere, then up all
```
`profiles` first checks every stack, so one broken stack stops the change before anything is written. It then writes `PROFILES` in `config.env` and `COMPOSE_PROFILES` in every `stack.env` together, and runs `up` on every stack. It also removes the containers of profiles that are off, which Compose would leave running. That last step runs every time, so a re-run cleans up after an interrupted one. When the list is already set everywhere, it changes and restarts nothing. The lollo features live on each stack's lollo address, `https://<name>-lollo.<domain>/` (`<lollo>` below), under their own Access application with the stack's list of people; Lettuce's address serves only Lettuce. Every run also re-applies each stack's tunnel routes (`<lollo>/vault/` goes to the vault only with `lollo-vault`, `<lollo>/gmail-triggers/` to Gmail triggers only with `lollo-gmail-triggers`, `<lollo>/code-server/` to the code editor only with `lollo-code-server`, and `<lollo>/`, a page linking to the ones that are on, to the lollo's own service) and both Access applications as lolloctl defines them (HttpOnly, SameSite=Lax cookies), before any stack starts; if that fails, the stacks still start and the command asks you to run it again. The lollo address is named after Lettuce's (`lollo-<name>-lollo` for a stack on its fallback name); when that name is taken in DNS, `add` uses `<...>-lollo-2` up to `-9`. `codex` and `claude` change the app-server image, and `up` rebuilds it. The list must contain `cloudflared`.

## Config changes
`config.env` values are copied into each `stack.env` when the stack is created. Use `profiles` for the profile list. To change `TZ` for existing stacks, edit `config.env` and every `stack.env`, then run `lolloctl up all`.

`check` (run by every `up`) refuses a stack whose profiles differ from `config.env`.

## Files on the host
Everything lolloctl keeps is in one data directory: `/srv/lollo-rozzo` for a release install (the directory above the wrapper's `bin/`), or `data/` in a clone. Keep it private, mode 700: it holds every secret of the installation.

| Path | What |
|---|---|
| `bin/lolloctl` | the wrapper: this release's lolloctl, pinned by digest; `bin/lolloctl.previous` is the one before the last `self-upgrade` |
| `config.env` | your settings plus cached Cloudflare ids (mode 600) |
| `stacks.conf` | who has a stack, and with which addresses (mode 600) |
| `.secrets/cloudflare-api-token` | Cloudflare API token (mode 600) |
| `upstream/<tag>/` | Lettuce's source at `LETTUCE_REF`, downloaded by `init` and `upgrade` |
| `stacks/<name>/stack.env` | that stack's secrets and Cloudflare ids (mode 600) |
| `stacks/<name>/state/` | that stack's data: `letta-home/`, `letta-data/`, `workspaces/`, `tools/` (the tools agents installed), and `landing/` for Gmail trigger rules |

Named Docker volumes hold the rest (the vault, pipelock's receipts, News Radar, Lettuce's Google tokens): [architecture.md](architecture.md) lists them.

## Installing and upgrading
A release install is a wrapper script, `/srv/lollo-rozzo/bin/lolloctl`, which runs lolloctl from its published image, pinned by digest, with the data directory mounted at the same path. Every image a stack runs is published too, except two that are built on the host: the document tools (their PDF library, PyMuPDF, is AGPL-3.0, so they are not published) and the code editor's shell (it is built on the app-server's image). The [README's quickstart](../README.md#quickstart) installs it.

The wrapper needs the data directory to exist and be yours, and refuses to run from anywhere but its `bin/`. lolloctl refuses to start unless the data directory, and the backup directory you name, are mounted at the same path inside its container: Docker reads every path lolloctl gives it on the host, and a path that differed would start a stack with an empty, root-owned directory in place of its data.

Each release's page lists the images that changed. A changed image restarts its containers; **a changed vault image locks every vault**, and each person unlocks theirs again. An unchanged image keeps its name, so most releases restart only some sidecars.
```bash
lolloctl version                    # this release, and the Lettuce tag it was tested with
lolloctl self-upgrade vX.Y.Z        # fetch that release's wrapper, pull its image, swap, then up all
/srv/lollo-rozzo/bin/lolloctl.previous version   # the previous release, kept to go back to
```
`self-upgrade` checks the new wrapper against its release's `SHA256SUMS` and pulls its image before it replaces anything, so a typo or a failed download leaves the install as it was. It then moves Lettuce forward to the tag the new release was tested with, never back. To go back, run `bin/lolloctl.previous`, or move it to `bin/lolloctl`. To check where a wrapper comes from: `gh attestation verify /srv/lollo-rozzo/bin/lolloctl --repo <owner>/lollo-rozzo`.

In release mode, `toolbox` sees your home read-only, plus your toolbox file and Claude Code config (`CLAUDE_CONFIG_DIR`) wherever they are; a `mount` line in the toolbox file must name something under your home. The toolbox file's `secret` commands run on the host, in the wrapper, which first checks that the file is yours and that nobody else can write it.

## Upgrading Lettuce
Lettuce tags look like `vX.Y.Z-letta_<letta-code version>`. Each one pins the server version its UI was compiled against. Never change versions any other way. Each lollo-rozzo release is tested with one of them, and `self-upgrade` moves to it; `upgrade` to another tag warns that it was not tested.
```bash
lolloctl upgrade vX.Y.Z-letta_A.B.C     # download it, record in config.env, update every stack
```
Lettuce's tags are on its GitHub releases page. `init` and `upgrade` also look up whether Lettuce publishes its images for this host (it does for `amd64`): if so the stacks pull them, else they are built here, which takes longer. When the registry cannot be reached, they stop and change nothing; run them again.
Back up first (below), and read upstream's CHANGELOG.md.

## Backups
```bash
lolloctl backup alfred /mnt/backup             # a snapshot in /mnt/backup/alfred/<UTC time>/
lolloctl backup all /mnt/backup --compress     # a .tar.gz per stack instead
```
`backup` copies what the images cannot recreate:
- agents, conversations and memory (`state/letta-data`);
- settings, schedules, transcripts, Claude Code/Codex/gh logins and skills installed from Settings (`state/letta-home`);
- agents' workspaces (`state/workspaces`) and the tools agents installed (`state/tools`, left out with `--no-tools`);
- `stack.env`, and the vault, whenever its volume exists (turning the `lollo-vault` profile off keeps it);
- pipelock's receipts and their signing key, whenever their volume exists (`lollo-pipelock` profile);
- the monitors, their sources and what they found, whenever their volume exists (`lollo-news-radar` profile);
- Gmail trigger rules (`state/lollo`).

It leaves out caches and the skills that come back by themselves: upstream's, which the BFF reinstalls on every connect, and those from `agent-skills/`, which `lolloctl skills` reinstalls. The state is root-owned, so `backup` reads it from a container of a small image (`backup/Dockerfile`: pulled by a release install, built from a clone; `lolloctl version` names it), with no network and read-only mounts. No sudo is needed.

- **Snapshots (the default).** Each run writes `<dir>/<name>/<UTC time>/`, owned by you with the original file modes. The original owners are not kept: a restore makes the state root's again, which is what nearly all of it is, so a file an agent gave to another user (say, a database directory owned by uid 999) comes back owned by root. Use `--compress` when those owners matter. Files unchanged since the previous snapshot are hard links to it, so a run costs only what changed, and each snapshot is still a complete copy. Hard links need a Linux filesystem at the destination (ext4, xfs, btrfs; not FAT or exFAT). `<dir>` is a local directory outside the data directory: for another machine, mount it (NAS, USB disk) or copy the snapshots onward with your own tools.
- **`--compress`.** Writes `<dir>/<name>/<UTC time>.tar.gz` instead, mode 600. Inside the archive, files keep their real owners. Each archive holds everything again, so this suits one-off copies and moving to another host better than regular backups.
- **A running stack.** Copying while agents write can catch memory, conversations or the vault mid-write and leave a corrupt backup. `backup` asks whether to pause the stack while it copies (agents and the UI freeze until it finishes; recommended), copy live anyway, or abort. `--pause` or `--live` answers ahead of time; without either and without anyone to answer, it aborts. A stopped stack is copied without asking.
- **`--keep N`.** After a successful run, removes all but the newest N snapshots (or, with `--compress`, archives) of each stack. Snapshots can hold read-only directories, such as Go's module cache, so to delete one by hand use `chmod -R u+w <snapshot>` first.
- A run that is interrupted (Ctrl-C stops the copy and unpauses the stack) leaves a `.partial-*` entry, which the next run removes. Only one backup of a stack runs at a time; a second one stops with an error.
- With `all`, a stack that fails (or that nobody answered for) does not stop the others; `backup` lists the failed ones at the end and exits with an error.

A nightly cron job (`crontab -e`) for every stack that keeps two weeks:
```
30 3 * * * /srv/lollo-rozzo/bin/lolloctl backup all /mnt/backup --pause --keep 14
```
A backup holds credentials: provider keys, Claude/Codex/gh logins and `stack.env`. Keep the destination private, and encrypt any copy that leaves the host. The vault inside it stays encrypted with the person's passphrase, which is stored nowhere, so nobody (you included) can recover a vault whose passphrase is forgotten. `config.env`, `stacks.conf` and `.secrets/` are not included; keep a copy of them too.

To restore, work in the data directory, with `B` the backup image. Stop the stack (`lolloctl down alfred`), or on a new host create its directory: `(umask 077 && mkdir -p stacks/alfred/state)`. Then, from a snapshot `S` (the state goes back to root, the vault to its uid 10001, and `state/` itself stays yours):
```bash
cd /srv/lollo-rozzo
B=$(lolloctl version | sed -n 's/^backup image: //p')
S=/mnt/backup/alfred/20261018T033000Z
install -m 600 "$S/stack.env" stacks/alfred/stack.env
docker run --rm -v "$S/state:/b:ro" -v "$PWD/stacks/alfred/state:/d" "$B" \
  sh -c "rsync -aH --numeric-ids --delete --chown=0:0 /b/ /d/ && chown $(id -u):$(id -g) /d"
# if the vault is in the snapshot:
docker run --rm -v "$S/vault:/b:ro" -v lollo-alfred_vault-data:/d "$B" \
  rsync -a --numeric-ids --delete --chown=10001:10001 /b/ /d/
# if pipelock's receipts are in the snapshot:
docker run --rm -v "$S/pipelock:/b:ro" -v lollo-alfred_pipelock-data:/d "$B" \
  rsync -a --numeric-ids --delete --chown=65532:65532 /b/ /d/
lolloctl up alfred
```
`--delete` makes the state match the snapshot, so it also deletes what the backup left out. The caches and skills come back by themselves, but for a snapshot taken with `--no-tools`, add `--exclude=/tools/` to the first `rsync` to keep the tools already there.

From an archive `A`, extract the same parts in the same image instead (the archive keeps the owners), then `up`:
```bash
docker run --rm -v "$A:/a.tar.gz:ro" -v "$PWD/stacks/alfred/state:/d" "$B" \
  tar -xzpf /a.tar.gz --numeric-owner -C /d --strip-components=2 ./state
docker run --rm -v "$A:/a.tar.gz:ro" -v lollo-alfred_vault-data:/d "$B" \
  sh -c 'tar -xzpf /a.tar.gz --numeric-owner -C /d --strip-components=2 ./vault && chown 10001:10001 /d && chmod 700 /d'
# if pipelock's receipts are in the archive:
docker run --rm -v "$A:/a.tar.gz:ro" -v lollo-alfred_pipelock-data:/d "$B" \
  sh -c 'tar -xzpf /a.tar.gz --numeric-owner -C /d --strip-components=2 ./pipelock && chown 65532:65532 /d && chmod 700 /d'
(umask 077 && tar -xzOf "$A" ./stack.env >stacks/alfred/stack.env)
```
On another host, first install the same release's wrapper ([README](../README.md#quickstart)), copy `config.env`, `stacks.conf` and `.secrets/` into its data directory, and run `lolloctl init`. If the data directory is at a different path there, fix `LETTA_STATE_DIR` in the restored `stack.env`. Cloudflare needs no change: the tunnel token travels inside `stack.env`. Never run the same stack on two hosts at once.

The named volume `lollo-<name>_bff-data` holds push-notification subscriptions and pinned-agent lists. It is not backed up; losing it is harmless. `remove` deletes the vault's volume and pipelock's receipts along with the stack.

Each stack's push-notification keypairs (`PUSH_VAPID_*` for Lettuce, `VAULT_VAPID_*` for the vault, in `stack.env`) are generated by `add` and kept from then on. Do not change or delete them: every device that enabled notifications would have to enable them again.

## Troubleshooting
| Symptom | Look at |
|---|---|
| Cloudflare error 1033 / "tunnel not connected" | `lolloctl logs <name> cloudflared`. The stack must be up; the token must match the tunnel (`stack.env`). |
| 502 / 504 after sign-in | The app-server is still starting or unhealthy: `lolloctl status`, `lolloctl logs <name> app-server`. |
| Signed in, Lettuce says forbidden or unknown user | The email isn't in `ALLOWED_USERS`. Use `lolloctl users`, which updates both places. |
| Google sign-in error at Cloudflare | Zero Trust -> Integrations -> Identity providers -> Google -> Test. Check the redirect URI in Google Cloud. |
| `add`: "both ... are taken" | Choose another stack name, or set `HOSTNAME_FALLBACK` (e.g. `ai-%s`). |
| `add`: Cloudflare API error 10000 / authentication | The token lacks a permission listed in cloudflare-setup.md step 5. |
| `init`: zone not found | The domain isn't on this Cloudflare account yet, or the token lacks Zone Read for it. |
| `check`: profiles differ | See "Config changes". |
| Image attachment fails: "the image processing worker is missing from this installation" | letta-code's self-updater ran `npm install -g` inside the app-server and moved the install aside. `compose/hardening.yml` sets `DISABLE_AUTOUPDATER=1` and `check` enforces it; recreate the stack with `lolloctl up <name>`. Do not reinstall letta-code inside the container. |
| Enabling notifications fails: "Could not load the push public key" | The stack has no VAPID keypair, so the BFF turns push off; `lolloctl check <name>` names the missing key. `add` writes the keys and nothing makes them again, so restore the stack's `stack.env` from a [backup](#backups). |
| `<lollo>/vault/` gives a 404 | The `lollo-vault` profile is off, or the route update failed: run `lolloctl profiles <list>` again. |
| An old link to a lollo page on Lettuce's address (`https://<stack>/...`) shows Lettuce | The lollo pages moved to `https://<name>-lollo.<domain>/` (`lolloctl list` shows it). |
| `<lollo>/vault/` gives 502 | The vault is not running: `lolloctl status`, `lolloctl logs <name> lollo-vault`. |
| Someone forgot their vault passphrase | It cannot be recovered. They can erase the vault themselves from the unlock page, under **Forgot your passphrase?**: it is erased 24 hours later unless someone unlocks it or cancels. To erase it at once, stop the service and delete its volume: `lolloctl compose <name> rm -sf lollo-vault`, `docker volume rm lollo-<name>_vault-data`, `lolloctl up <name>`. |
| `<lollo>/gmail-triggers/` gives a 404 | The `lollo-gmail-triggers` profile is off, or the route update failed: run `lolloctl profiles <list>` again. |
| `<lollo>/news-radar/` gives a 404 | The `lollo-news-radar` profile is off, or the route update failed: run `lolloctl profiles <list>` again. |
| News Radar page says Lettuce isn't picking up reports | The executor has not run for 3 minutes. Run `lolloctl up <name>` to reinstall it; read `/root/.letta/lollo/executor/executor.log` with `lolloctl shell <name> <agent>`. |
| A monitor's source shows as broken | The site changed, refused the fetch or answered with a challenge page; `lolloctl logs <name> lollo-news-radar` gives the reason. The agent is told in its next report and repairs what it can; a site behind a login or a challenge can't be monitored. |
| `<lollo>/code-server/` gives a 404 | The `lollo-code-server` profile is off, or the route update failed: run `lolloctl profiles <list>` again. |
| The code editor says "The editor isn't running right now" | `lolloctl status`, `lolloctl logs <name> lollo-code-server-editor`. The first `up` with the profile downloads code-server and builds two images. |
| The code editor's terminal closes at once, or says "Permission denied (publickey)" | The shell container is down or its keys are out of step: `lolloctl logs <name> lollo-code-server-exec`, then `lolloctl up <name>` (the editor rewrites the keys at every start). |
| `<lollo>/` gives a 404, or lacks a feature that is on | No lollo page profile is on (the page exists only with one), or the route update failed: run `lolloctl profiles <list>` again. The page lists the lollo page profiles of the last `up`; `profiles` runs it. |
| `<lollo>/` says the agent list is on its way, for minutes | The lollo executor in the app-server sends it; it runs every minute. Run `lolloctl up <name>` to reinstall it; read `/root/.letta/lollo/executor/executor.log` with `lolloctl shell <name> <agent>`. |
| Gmail triggers page says Lettuce is not picking up requests | The executor has not run for 3 minutes. Run `lolloctl up <name>` to reinstall it; read `/root/.letta/lollo/executor/executor.log` with `lolloctl shell <name> <agent>`. |
| The lollo executor's token leaked outside the stack (you suspect) | Delete the `LOLLO_EXECUTOR_TOKEN` line from `stack.env` and run `lolloctl up <name>`: a new one is generated and installed. Agents in the stack can always read it; that is by design (see gmail-triggers.md). |
| `add`, `up` or `profiles`: "the lollo executor is out of step" / "could not sync the lollo executor" | Installing or removing the executor in the app-server failed; the stacks still came up. Check `lolloctl logs <name> app-server`, then run `lolloctl up <name>` (after `profiles`, running it again also works). |
| `remove --purge` refuses: lollo is a symlink | `stacks/<name>/state/lollo` (or under `LETTA_STATE_DIR`) points elsewhere, and emptying it would empty the target. Look at where it points before removing it by hand. |
| `<lollo>/vault/` says "direct access is not allowed" or "may not use this stack" | The request did not come through Cloudflare Access, or the email is not in `ALLOWED_USERS`. |
| A wildcard warning during `add` | The domain has a `*` record, so public DNS can't show which names are free. Only the Cloudflare zone is checked. |
| `ping` in an agent shell: "Operation not permitted" | The app-server has no `NET_RAW` (see Security notes), and the image's `ping` is set to require it. A copy without that setting works: `cp /usr/bin/ping /tmp/ping && /tmp/ping <host>`. Tools that need raw sockets (`tcpdump`, `arping`, `nmap` SYN scans) don't work there; `curl` and `tracepath` do. |

## Security notes
- Keep `config.env`, `.secrets/` and `stacks/` mode 600/700. `lolloctl` creates them that way and `check` enforces it for `stack.env`.
- Never add `DEV_BYPASS_*` to a stack. It disables authentication, and `check` refuses to start a stack that has it.
- The Cloudflare API token can change your DNS. Scope it to one zone and one account, and consider an IP restriction.
- Agents in one stack can read everything else in that stack, including its provider keys. That is why each person gets their own stack. The vault is the exception: it lives on a volume only the vault service mounts (`check` enforces that), encrypted with each person's passphrase. Root on the host can still read the vault service's memory while it is unlocked, and anything a person shares with an agent is stored in that conversation like the rest of it.
- The app-server, and the containers sharing its network (BFF, channel-gateway), drop `NET_RAW`, so agents can't capture the Access token on its way to the BFF and use it on the vault. `check` refuses a stack that keeps it. The cost is raw sockets in agent shells, including the image's `ping` (see Troubleshooting).
- The code editor shares an address with the vault and Gmail triggers (the stack's lollo address), so nothing agents write may run in it: it stays in Restricted Mode, has no marketplace, debugger or task providers, and sends every terminal to a separate container that is exactly as strong as an agent (see [code-server.md](code-server.md)). Never trust a folder in it.
- Gmail triggers start agents without the person typing anything, but only for rules they made on their Access-protected page, and only through Lettuce's own scheduler: see the security model in [gmail-triggers.md](gmail-triggers.md). News Radar does too, at the schedules agents set and only when something is new, within a daily cap per agent; the person sees and deletes them on their page: see [news-radar.md](news-radar.md#security-model). The sidecar reads Lettuce's Google token (read-only); `check` refuses any other service mounting it.
