# Installing without a clone: design

Status: implemented on branch `install-without-clone` (2026-11-07), apart from the steps noted
below; nothing is published yet, and the existing install has not been moved. The plan below stays
as the design record. Where the build differs from it:

- **The shell builds on the app-server's image by name, not `service:app-server`.** The spike
  showed that building a service with `additional_contexts: base: service:app-server` makes Compose
  build the app-server too, over a pulled image. `compose/code-server.yml` names it instead
  (`docker-image://${APP_SERVER_IMAGE}`, which BuildKit took from the local store without a
  registry), lettucectl passes the name (`app_server_image`), and `check_images` compares it with
  the render. `up` builds upstream's services first, when they are built here, in a call of their
  own.
- **The Docker CLI, Compose and Buildx come from Docker's `docker:<version>-cli` image**, pinned by
  digest, rather than three static downloads with their own sums: one pin, both platforms. It
  carries CLI 29.9.3, Compose 5.7.0 and Buildx 0.38.2. The README asks for Docker Engine 25 (API
  1.44); it was run against Engine 29.1 only.
- **`publish.yml` builds one image per job**, from a matrix it reads from `release/images`, since
  GitHub's attestation action takes one image per step; the last job collects their names into
  `RELEASE`. One `docker buildx build` per image, no `bake`.
- **`toolbox` in release mode:** the wrapper runs the toolbox file's `secret` lines on the host
  too, and passes their values by name; a toolbox mount outside the operator's home is refused,
  since only the home is mounted.
- **`lettucectl dns-preflight`** runs `scripts/dns-preflight.sh` (step 2.6 of the Cloudflare guide),
  which the image carries, so a release install needs no clone for it either.
- **`LETTA_IMAGE_NS` stays in `config.env`.** Rather than copied into every `stack.env` (which
  would go stale until the next `up`), `compose_env` passes it on every call, as it passes
  `LETTUCE_HOME`; Compose prefers it to the `--env-file`'s.
- **`lettucectl version`** names the release, its tested Lettuce tag, and the backup image the
  restore steps use.
- **The GHCR packages needed no step by hand.** `publish.yml`'s first run (`v0.1.0-rc.1`)
  created them public, as the repository is, so hosts pull without credentials; the release
  steps now check this instead ([release/README.md](../release/README.md#cutting-a-release)).
- **Not done:** moving the existing install ([below](#moving-the-existing-install); the operator's
  to do), and `init` creating the Google login method through the API (unverified, optional).
  `init` checks the other scopes by reads, which cannot tell a read scope from an edit one.

The checks under [Verified so far](#verified-so-far) ran on 2026-11-07, and so did the spike's
first two items, `service:app-server` (above) and a tag-and-digest `image:` under `--pull missing
--no-build` (it pulled and ran). The others under [To verify in the spike](#to-verify-in-the-spike)
are still open.

## Goal

Today an admin installs lettuce-garden by cloning this repo, and the clone does four jobs at once:

1. **It ships the CLI:** `lettucectl` and `lib/`, which need `git`, `curl`, `jq`, `openssl`, `dig`
   and Docker Compose >= 2.24 on the host.
2. **It holds the build contexts** of every garden image, all built on the host by `lettucectl up`
   (`compose up -d --build`), which is why a first boot takes minutes.
3. **It holds the files lettucectl uses at run time:** the overlays in `compose/`, `agent-skills/`,
   `garden/executor/run.mjs`, `lib/mcp-servers.mjs` and `config.env.example`.
4. **It holds the instance data:** `config.env`, `.secrets/`, `upstream/` (a second clone, Lettuce
   at `LETTUCE_REF`) and `stacks/`. Upgrading lettuce-garden is a `git pull` over the data.

After this change:

- The host needs Docker Engine, and one downloaded file: the wrapper script.
- `lettucectl` runs from a published image, pinned by digest, which carries every file the CLI uses.
- The garden's images are pulled, not built, except the two that cannot be published (below).
- All instance data lives in one data directory, apart from the code.
- Who has a stack, and with which addresses, is one file, `stacks.conf`, applied with
  `lettucectl apply`.
- `lettucectl init` walks the admin through the one-time Cloudflare and Google setup.
- An upgrade restarts only the sidecars whose image changed, so a vault is locked only when the
  vault itself changed.

A clone stays the way to develop: from a clone, `lettucectl` builds every image locally, as today,
against a data directory of its own.

## Decisions

| Question | Decision |
|---|---|
| How is it distributed? | A `lettucectl` image on GHCR, and a wrapper script attached to each GitHub release that names that image by digest. No tarball release, no packages. |
| What is published? | Every garden image but `doc-tools` and the code editor's shell, for `linux/amd64` and `linux/arm64`. |
| Where are images published? | A new `.github/workflows/publish.yml`, on `v*` tags only, after every check of `ci.yml` passes. No `latest`. |
| How are images named? | By a hash of what goes into each one, plus its digest; never by the release number. An image that did not change keeps its name, so its container is not recreated. |
| How is Lettuce fetched? | The source archive of the tag `LETTUCE_REF`, over HTTPS; no `git clone`. Each garden release names the Lettuce tag it was tested with. |
| Is the people list declarative? | Yes: `stacks.conf` and `lettucectl apply`. `add`, `users` and `remove` keep the file in step, so `apply` never undoes them. A stack missing from the file is reported, never removed. |
| Are profiles declarative? | No: they keep changing only through `lettucectl profiles` ([lib/README.md](README.md#rules)). `apply` reports a stack out of step, and never changes a profile. |
| A daemon that reconciles on its own? | No: `apply` is a command the admin runs. |
| Terraform or OpenTofu for Cloudflare? | No: `lib/cloudflare.sh` stays (see [Not in scope](#not-in-scope)). |
| Backwards compatibility | None. The one existing install is moved once, by hand or with a throwaway script ([Moving the existing install](#moving-the-existing-install)). |

## The data directory

All instance data moves out of the code into one directory, `LETTUCE_DATA`:

```
/srv/lettuce-garden/
  bin/lettucectl                 the wrapper (release mode only)
  bin/lettucectl.previous        the one before the last self-upgrade
  config.env
  stacks.conf
  .secrets/cloudflare-api-token
  upstream/<LETTUCE_REF>/        Lettuce's source at that tag (read-only use)
  stacks/<name>/stack.env
  stacks/<name>/state/
  stacks/.doc-tools.lock
```

Where it is depends on how `lettucectl` runs:

- **Release mode** (the published image): the wrapper's own directory's parent, so a wrapper in
  `/srv/lettuce-garden/bin/` uses `/srv/lettuce-garden`. The admin links it onto their `PATH`.
- **Dev mode** (a clone): `$LETTUCE_HOME/data/`, which is gitignored, unless `LETTUCE_DATA` is set.
  A clone or a worktree never reaches the real stacks by default. Dev mode also refuses a data
  directory that a release runs (one with `bin/lettucectl`): running a branch's code and images
  against people's stacks would recreate them from unreviewed code and lock every vault.

`LETTUCE_HOME` keeps its meaning: where the code is (the clone, or `/opt/lettuce-garden` in the
image). In `lib/common.sh`, `config_file`, `token_file`, `upstream_dir`, `stack_dir`, `stack_env` and
`list_stacks` move from `LETTUCE_HOME` to `LETTUCE_DATA`, and so does the doc-tools lock in
`ensure_doc_tools_volume`. Everything that reads code (`compose/`, `agent-skills/`,
`garden/executor/`, `lib/mcp-servers.mjs`, `config.env.example`, the `doc-tools/` and `backup/` build
contexts) stays on `LETTUCE_HOME`.

`LETTA_STATE_DIR` in each `stack.env` is written from `LETTUCE_DATA`, so it is an absolute path in the
data directory.

**The overlays' build contexts must change.** They are relative to Compose's project directory,
`upstream/docker`, and so assume `upstream/` lives inside the code (`context: ../../garden`). With
`upstream/` in the data directory they would point nowhere. `compose_env` passes `LETTUCE_HOME` to
Compose, as it passes `DOC_TOOLS_NETWORK` today, and the overlays use `${LETTUCE_HOME}/garden`,
`${LETTUCE_HOME}/garden-common` and so on.

`backup` and `receipts` keep refusing a destination inside `LETTUCE_HOME` (a clone is public), and
also refuse one inside `LETTUCE_DATA`, where a backup would back up its own earlier backups.

`.gitignore` keeps `/config.env`, `/.secrets/`, `/upstream/` and `/stacks/`, and adds `/data/`. A
clone no longer holds instance data by design, but an old clone, or a `LETTUCE_DATA` pointed inside
one, still must not be committable.

## Lettuce without a clone

`init` and `upgrade <tag>` download `https://github.com/<owner>/<repo>/archive/refs/tags/<tag>.tar.gz`
(owner and repo from `LETTUCE_REPO`), unpack it into `upstream/<tag>.partial/` and rename it to
`upstream/<tag>/` when complete. `upstream_dir` returns `upstream/<LETTUCE_REF>`. A tag already
unpacked is not downloaded again; older tags are removed after a successful `upgrade`.

The whole source is kept, not only `docker/compose.yml`, because three things read more of it:

- `sync_skills` refuses a skill name that upstream ships in `docker/agent-skills/`;
- a host for which upstream publishes no images builds them itself ([below](#upstreams-images));
- the BFF's build context is upstream's repository root.

`upgrade` no longer runs `git fetch` and `git checkout`; `init` no longer needs `git`. `LETTUCE_REPO`
must be a GitHub URL. The archive comes over HTTPS from GitHub, as the clone does today; it is not
pinned by hash, since `LETTUCE_REF` is the admin's choice.

### Upstream's images

Whether upstream's images are pulled or built is decided once, by `init` and by `upgrade`, not on
every `up`:

- The namespace is `ghcr.io/<owner of LETTUCE_REPO, lower-cased>/`, so a fork's images come from the
  fork, matching the source that `sync_skills` and the BFF's context read.
- `init` and `upgrade` look up `lettuce-app-server:<LETTUCE_REF>` in that namespace for the host's
  platform (`docker buildx imagetools inspect`). If it is there, they write the namespace to
  `config.env`'s `LETTA_IMAGE_NS`; if not (an `arm64` host today, or a fork that publishes nothing),
  they leave it empty and say that upstream's images will be built on this host.
- `stack.env` gets the value through `write_new_stack_env` and `profiles`, as the other global
  settings do.

Upstream's published app-server always carries the Codex and Claude Code CLIs
(`COMPOSE_PROFILES=codex,claude` in its `publish.yml`); with the `codex` or `claude` profile off, the
image is larger but Settings shows nothing more. That is upstream's documented choice.

### The tested Lettuce version

Each garden release records the Lettuce tag it was tested with, `LETTUCE_REF` in its `RELEASE` file.

- `init` on a new install writes it to `config.env`.
- `self-upgrade` moves `LETTUCE_REF` to it, through `upgrade`, when it is newer than the one in
  `config.env`. It never moves it back: an older letta-code may not read what a newer one wrote.
- `upgrade <tag>` to another tag still works, after walking
  [the upgrade checklist](../CONTRIBUTING.md#upgrading-lettuce_ref), and warns that the tag is not
  the tested one; `check` and `status` say so too.

## Published images

### What can be published

| Image | Published | Why |
|---|---|---|
| `garden`, `vault`, `gmail-triggers`, `code-server` (the gate), `backup` | yes | This repo's code (MIT) on Debian and Python base images |
| `pipelock` | yes | Built without the `enterprise` build tag, and the build fails when anything under `enterprise/` would be linked in, so the binary is Apache-2.0 only ([pipelock/README.md](../pipelock/README.md#licence)) |
| `code-server-editor` | yes | Coder's code-server is MIT. Microsoft's VS Code Server, whose licence forbids hosting it for others, is not used ([code-server/DESIGN.md](../code-server/DESIGN.md#why-code-server)) |
| `lettucectl` (new) | yes | This repo's code, plus the Docker CLI and its plugins, `curl`, `jq`, `openssl`, `dig` and `util-linux` |
| `doc-tools` | **no** | The PDF server uses PyMuPDF, which is AGPL-3.0 ([docs/doc-tools.md](../docs/doc-tools.md)); publishing the image would make it AGPL. It is never run, only copied into a volume, so building it on the host costs one build per tools change. Its build context ships inside the `lettucectl` image. |
| `code-server-exec` (the shell) | **no** | Built `FROM` the app-server image (`base: service:app-server`), so it would need one image per Lettuce tag. It only adds `openssh-server` to that image, so the local build is short. |

`gotenberg` is already pulled from Docker Hub, pinned by digest.

### Image names: by content, not by release

Compose recreates a container when its service's `image:` name changes, even when the image is the
same ([Verified so far](#verified-so-far)), and recreating the vault locks it. If each release
renamed every image (`lettuce-vault:v1.4.0`, then `:v1.4.1`), every upgrade would lock every
person's vault, and restart every sidecar, for a release that changed one of them.

So each published image is named by what goes into it, the way `doc_tools_tag` names the document
tools today:

- **The content tag** is a hash of the image's build context, of `garden-common/` for the images
  that use it, and of `REBUILD`, a file at the root of the repo.
- **The full name** is `ghcr.io/<owner>/lettuce-<name>:<content tag>@sha256:<digest>`.
- `publish.yml` builds an image only when its content tag is not in the registry yet; otherwise the
  release reuses the existing image, and so its digest.
- An unchanged image therefore keeps the exact same name from release to release, and Compose leaves
  its container alone.
- Base images (`debian:12-slim`, `python:3.12-slim`, `alpine`, `golang`) get security fixes only on
  a rebuild. A rebuild is asked for by changing `REBUILD` (a date), in a release of its own: it
  renames every image, so it restarts every sidecar and locks every vault, and its release notes
  say so.
- Every release's notes list the images whose name changed, and so the sidecars that restart, from
  a diff of the two `RELEASE` files. A changed `vault` image is called out: each person unlocks their
  vault again.

### Release mode and dev mode

`lettucectl` decides how to get images from one file, `$LETTUCE_HOME/RELEASE`, which only the
published `lettucectl` image has. `publish.yml` writes it: the version, the tested `LETTUCE_REF`,
and one `GARDEN_IMAGE_<NAME>` line per published image with its full name.

- The overlays name each image by its own variable, with today's name as the default:
  `image: ${GARDEN_IMAGE_VAULT:-lettuce-vault:local}`. `compose_env` passes the variables from
  `RELEASE` in release mode, and none in dev mode.
- **Release mode:** `up_stack` builds, by name, only the services that are built on this host: the
  code editor's shell (with its profile on), and upstream's services when `LETTA_IMAGE_NS` is empty.
  It then runs `up -d --no-build --pull missing --remove-orphans`. An image that is already on the
  host is not pulled again, so `up` needs the registry only after an upgrade; a published image that
  is missing fails the command instead of being built from a context the image does not carry.
  `backup` and `receipts` use `lettuce-backup` and `lettuce-pipelock` by the same names.
- **Dev mode** (a clone): the default names, `lettuce-<name>:local`, and `up -d --build`, as today. A
  branch never runs images that are not its own code.

The switch is a file in the image instead of a setting, so an admin cannot point a clone at
published images by mistake. `doc-tools` is built by lettucectl in both modes, as today.

### `publish.yml`

- **When:** on a `v*` tag push (`vX.Y.Z`, or `vX.Y.Z-rc.N` for a release candidate to try on a test
  host). Nothing is published from `main`, and there is no `latest`: a moving tag on an image that
  holds the Docker socket would let one push reach every host. This differs from upstream, which
  publishes `latest` from `main`.
- **Gate:** it calls `ci.yml` (made a reusable workflow) and publishes only when every job passes:
  `shellcheck`, `tests/run.sh`, the Node tests, every sidecar's tests, `tests/run-pipelock.sh` and
  the link checker. It also fails when `VERSION` and the tag disagree.
- **Build:** each published image for `linux/amd64` and `linux/arm64`, with Buildx and QEMU, skipping
  those whose content tag exists. `lettucectl` is built last, since its `RELEASE` holds the others'
  digests.
- **Supply chain:** every image is pushed with an SBOM and a provenance attestation, and signed
  through GitHub's artifact attestations (`actions/attest-build-provenance`), so
  `gh attestation verify oci://<image> --repo <owner>/lettuce-garden` proves it was built by this
  workflow from this tag.
- **Check:** after the push, `docker buildx imagetools inspect` confirms that every image is in the
  registry for both platforms: upstream found that `compose build --push` can exit 0 having pushed
  nothing.
- **Release:** it attaches the wrapper (with the `lettucectl` image's digest filled in),
  `SHA256SUMS` and the release notes (the changed images) to the GitHub release, with an
  attestation for the wrapper too.
- The GHCR packages are made public once, by hand, so a host pulls without credentials.

`code-server-editor` is already pinned for both platforms (`SHA256_AMD64`, `SHA256_ARM64`).
`doc-tools` stays `amd64` only (OfficeAgent is pinned for x86_64), so `garden-doc-tools` is
unavailable on `arm64` hosts, as today; `check_stack` should say so instead of failing the build.

## `lettucectl` in a container

### The image

Debian slim with `curl`, `jq`, `openssl`, `dnsutils` and `util-linux` (`flock`); the Docker CLI and
its Compose and Buildx plugins as Docker's own static releases, each pinned by version and SHA-256,
not Debian's packages, which lag; and this repo's code at `/opt/lettuce-garden` without `tests/`,
`docs/` and the sidecars' sources (it keeps the `doc-tools/` and `code-server/exec/` build contexts).
Its entrypoint is `lettucectl`.

In release mode the Compose that runs is the image's, not the host's, so the host's Compose version
no longer matters. What matters is the Docker Engine: the image's CLI negotiates down to the
Engine's API version, and the README's requirements name the oldest Engine it supports
([To verify](#to-verify-in-the-spike)).

### The wrapper

A script of a few dozen lines, one per release, with that release's version and the `lettucectl`
image's digest written into it. It lives in `$LETTUCE_DATA/bin/` and the admin links it onto their
`PATH`. It runs, in essence:

```bash
docker run --rm -i [-t] --init \
  --user "$(id -u):$(id -g)" --group-add "<the docker socket's group>" \
  --tmpfs /tmp -e HOME=/tmp/home \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$LETTUCE_DATA:$LETTUCE_DATA" -e LETTUCE_DATA="$LETTUCE_DATA" \
  -e TERM \
  ghcr.io/<owner>/lettucectl:vX.Y.Z@sha256:<digest> "$@"
```

- **`--init`:** as PID 1, `lettucectl` would ignore the signal Docker forwards on Ctrl-C. `BACKUP_RUN`
  passes `--init` for the same reason.
- **`--user`:** the files it writes are owned by the admin, not root. The socket gives it the same
  power over Docker that `lettucectl` has today, no more.
- **A writable `HOME`:** the Docker CLI and Buildx write their configuration under `$HOME/.docker`;
  with `--user` and no `HOME`, that is `/` and the builds of the shell and `doc-tools` fail. Nothing
  there needs to outlive the command.
- **`-t`** only when stdin and stdout are terminals, so `shell` and `toolbox` work and pipes do too.
- **The digest:** the image is fetched by digest, so a tag pushed again to the registry, or a stolen
  registry token, cannot change what an installed wrapper runs.
- **A missing data directory is refused.** Otherwise Docker would create the bind source itself,
  owned by root, and the first write would fail. The wrapper prints what to run once, for example
  `sudo install -d -o "$USER" -g "$(id -gn)" -m 700 /srv/lettuce-garden/bin`, and how to download the
  wrapper into it.

### Installing and upgrading

- **Install:** download the release's wrapper into `/srv/lettuce-garden/bin/`, check it against
  `SHA256SUMS` (and, for those with the `gh` CLI, `gh attestation verify`), link it onto the `PATH`,
  then `lettucectl init`.
- **`lettucectl self-upgrade vX.Y.Z`** is handled by the wrapper, so a failure leaves the running
  install as it was:
  1. it asks the current image to download that release's wrapper and `SHA256SUMS` into
     `bin/.lettucectl.new`, and to check the sum;
  2. it pulls the image the new wrapper names, by digest; a typo in the version, or a release
     without that image, stops here;
  3. only then does it keep the current wrapper as `bin/lettucectl.previous`, move the new one into
     place, and run `lettucectl up all` with it (and `upgrade`, when the release's tested
     `LETTUCE_REF` is newer).
- **Going back:** `bin/lettucectl.previous` is a complete wrapper; run it, or move it back into place.
- The checksum comes from the same release as the wrapper, so it catches a broken download, not a
  forged release; the attestation is what proves where the wrapper came from.

### The catch: every host path must be the same inside and outside

`lettucectl` tells Docker to mount host paths, for example
`/srv/lettuce-garden/stacks/alfred/state/workspaces` into Alfred's app-server. Docker reads that path
on the host, not inside the `lettucectl` container. If the wrapper mounted the data directory at
another path, say `/data`, then `stack.env` would record `/data/stacks/alfred/state`, Docker would not
find it on the host and would create it empty and owned by root, and Alfred's stack would start with
no memory and no workspaces, with no error.

So the wrapper mounts every host path at the same path, and in release mode `lettucectl` checks it
before anything else: it reads its own container's mounts (`docker inspect "$HOSTNAME"`) and stops
unless every bind mount has the same source and destination, and `LETTUCE_DATA` is one of them. It is
one API call, with no extra container and no state file, and it covers the `backup` and `receipts`
destinations too ([Verified so far](#verified-so-far)).

### Commands that use other host paths

- **`backup <name> <dir>` and `receipts <name> [report]`** write to a directory the admin names. The
  wrapper mounts it at the same path (the `realpath` of `<dir>`, or the report's directory), and
  `lettucectl` checks that the destination it was given is one of its mounts.
- **`toolbox`** uses the admin's own credentials. Its mounts (`~/.ssh/known_hosts`, `~/.gitconfig`,
  `~/.claude/...`, the toolbox file's mounts) are read by Docker on the host, but `lettucectl` checks
  that they exist and resolves `~` itself, and runs `gh auth token` and `secret-tool lookup`. For
  `toolbox` the wrapper also mounts the admin's home read-only at the same path and passes it as
  `LETTUCECTL_HOST_HOME`, which `toolbox` uses instead of `HOME`; forwards `SSH_AUTH_SOCK`; and runs
  the two token lookups on the host, passing the tokens with `-e NAME`, never in argv. `toolbox`
  reads them from the environment when `gh` and `secret-tool` are not installed.
- **`$EDITOR config.env`** happens on the host, as today.

## `stacks.conf` and `lettucectl apply`

`stacks.conf` in the data directory lists the stacks and who may sign in to each, one per line:

```
# name   emails (comma-separated)
alfred   alfred@gmail.com
hal      hal@example.com,hal@work.example.com
```

It is plain text in the style of `config.env`, so it needs no YAML parser, and `lettucectl` reads and
writes it only through helpers in `lib/common.sh`, as it does `config.env`.

### The commands keep it in step

`add`, `users` and `remove` stay, for scripts and one-off changes, and each updates `stacks.conf`
when it succeeds: `add` adds the line, `users` rewrites it, `remove` deletes it. A change made with
them is therefore never undone by the next `apply`: an address revoked with `users` stays revoked.
If the file cannot be written, the command says so and fails, so the two never drift silently.

### What `apply` does

`lettucectl apply` makes the stacks match `stacks.conf`, through the existing commands, so their
order and rules are kept:

1. `init`'s checks, when `CF_ZONE_ID` and the others are empty.
2. `add <name> <emails>` for each line without a complete stack (an unfinished one is resumed, as
   `add` does today).
3. `users <name> <emails>` for each stack whose `ALLOWED_USERS` differs.
4. `up all`, then `skills all` (which the quickstart asks for by hand today).
5. A report, with the command to run for each:
   - each stack not in `stacks.conf`: `lettucectl remove <name>`. `apply` never removes one: a
     mistake in the file must not delete a person's data;
   - each stack whose `COMPOSE_PROFILES` differs from `PROFILES` (an interrupted `profiles`):
     `lettucectl profiles <PROFILES>`.

`apply` first prints its plan, with each address that gains or loses access by name, and asks to go
on when it is run from a terminal; `apply --dry-run` prints the plan and stops, and `apply --yes`
skips the question for scripts.

Profiles are not in `stacks.conf` and `apply` never changes them: they stay global, in `config.env`,
changed only by `lettucectl profiles` ([lib/README.md](README.md#rules), "Docs never tell operators
to edit the lists by hand").

## A guided `init`

`init` already checks the zone, the Zero Trust organisation and the Google login method. It becomes
a walk through `docs/cloudflare-setup.md`, keeping the guide's step numbers:

- It checks every step in order and stops at the first one missing, naming the step and what to do.
- It checks each scope the token needs, one call per scope, and names the missing one, instead of
  reporting that a lookup failed.
- It warns when DNSSEC is on at the registrar and the zone is still pending.
- Optionally, with a Google client id and secret on stdin, it creates the Google login method through
  the API, if the token's Access scope allows it.
- On a new install it writes the release's tested `LETTUCE_REF` and the upstream image namespace
  ([Upstream's images](#upstreams-images)).

The Cloudflare account, the zone, the Zero Trust plan and the Google OAuth client stay manual: they
need payment details or a browser sign-in.

## Moving the existing install

There is one existing install, so there is no migration code. Once, by hand or with a throwaway
script that is not committed, at a quiet time:

1. Tell everyone with a stack: their stack restarts, and their vault must be unlocked again. Every
   garden image is renamed (from `lettuce-<name>:local` to its published name), and so is upstream's
   when `LETTA_IMAGE_NS` is set, so Compose recreates every container once.
2. `lettucectl down all` from the clone.
3. Create `/srv/lettuce-garden/bin`, owned by the admin, mode 700.
4. Move `config.env`, `.secrets/` and `stacks/` into `/srv/lettuce-garden`. Delete `upstream/`;
   `init` downloads it.
5. In each `stacks/<name>/stack.env`, rewrite `LETTA_STATE_DIR` to the new path (it is absolute),
   and delete any `LETTA_IMAGE_TAG` set by hand (a test stack may have one, to keep its builds of
   upstream's images apart). Upstream's images are then named by `LETTUCE_REF` (`app_server_image`),
   and pulled when `LETTA_IMAGE_NS` is set, where no such tag exists; `check` refuses a stack whose
   app-server's image is named otherwise.
6. Write `stacks.conf` from `lettucectl list`.
7. Install the wrapper, then run `lettucectl init` and `lettucectl apply --dry-run`. The plan should
   show no Cloudflare change and no address gained or lost.
8. `lettucectl apply`.

Named volumes are named after the Compose project (`lettuce-<name>`), which does not change, so the
vault, the BFF's data and the rest are kept. The doc-tools volume is labelled by its content hash and
is kept.

## This repo's rules, checked

- **Overlays restrict, never redefine.** Only the garden's own services change `image:` and
  `build:`; no upstream service is touched. `LETTA_IMAGE_NS` is upstream's own switch.
- **Every Compose call goes through `compose`.** The new variables pass through `compose_env`, like
  `DOC_TOOLS_NETWORK`.
- **No token in argv.** The wrapper passes tokens with `-e NAME`; the Cloudflare token stays in
  `.secrets/` and is read from the file.
- **`up` makes no Cloudflare call.** `apply` calls `add` and `users`, which do; `up` still does not.
- **Never source `config.env`.** `stacks.conf` gets helpers like `env_get` and `env_set`; the wrapper
  reads nothing from the data directory.
- **Profiles change only through `lettucectl profiles`.** Kept: `apply` reports, and never edits a
  profile list or tells anyone to.
- **`users` changes the Access policy and `ALLOWED_USERS` together.** Kept, and `stacks.conf` with
  them.
- **A new safety rule goes into `check_stack`, with a `test_check_*` test.** Release mode adds one:
  every published garden service runs its `RELEASE` name, so a hand edit cannot swap an image in
  (`test_check_release_images`).
- **Nothing secret under `stacks/<name>/state/`:** unchanged; `stacks/` moves as a whole.

## Docs to update

- README: the requirements (Docker Engine, its oldest supported version) and the quickstart (the
  wrapper, `init`, `stacks.conf`, `apply`).
- `docs/cloudflare-setup.md`: step 5 stores the token in the data directory; the step numbers stay.
- `docs/operations.md`: files on the host, upgrades (`self-upgrade`, `upgrade`, going back), which
  upgrades lock the vault, adding people with `stacks.conf`.
- `docs/architecture.md`: where the data lives; and the paths shown in the
  `lettuce-garden-overview` and `lettuce-garden-at-a-glance` diagrams, if any.
- ARCHITECTURE.md "What lives where", `lib/README.md` (rules for `stacks.conf`, release mode, the
  same-path check), `compose/README.md` (the image variables), `tests/README.md`.
- CONTRIBUTING.md and AGENTS.md: the commands block ("`lettucectl up` builds the sidecar images" is
  true only from a clone), the release steps (`VERSION`, the tag, `REBUILD`), and
  `.github/workflows/ci.yml` (reusable, called by `publish.yml`), kept in step as today.

## Tests

- `tests/run.sh`, with the existing stubs:
  - paths come from `LETTUCE_DATA`, code paths from `LETTUCE_HOME`; dev mode defaults to `data/` in
    the clone and refuses a release's data directory;
  - `init` and `upgrade` download and unpack the archive (`curl` stubbed), keep a complete tag and
    replace a `.partial` one, and set `LETTA_IMAGE_NS` from the registry lookup, empty when the
    image is missing;
  - release mode's Compose arguments: the image variables, the local builds by name (the shell; and
    upstream's services when `LETTA_IMAGE_NS` is empty), `--no-build --pull missing`; dev mode's
    unchanged;
  - `test_check_release_images`;
  - the same-path check refuses a bind mount whose source and destination differ, a missing
    `LETTUCE_DATA` mount, and a `backup` destination that is not mounted (`docker inspect` stubbed);
  - `add`, `users` and `remove` update `stacks.conf`, and fail when they cannot;
  - `apply`: each kind of difference calls the right command, an address revoked with `users` stays
    revoked, an unlisted stack and an out-of-step profile list are reported and changed by nothing,
    `--dry-run` calls nothing;
  - `self-upgrade` moves `LETTUCE_REF` forward only.
- The content tags: a test that the same tree gives the same tag, and a change in a context, in
  `garden-common/` or in `REBUILD` gives a new one.
- The wrapper: `shellcheck`, and tests with `docker` stubbed: the mounts and flags (`--init`,
  `--user`, `HOME`, the digest), a missing data directory refused, tokens never in argv, and
  `self-upgrade` swapping the wrapper only after the new image is pulled.
- `publish.yml` itself: a run on an `-rc` tag in a fork, then `gh attestation verify` and
  `docker buildx imagetools inspect` on each image, and a second run that rebuilds nothing.
- End to end, by hand: a fresh `amd64` VM and a fresh `arm64` one, with only Docker, through the
  quickstart; then a self-upgrade that changes one image, checking that only its container restarts.

## Steps

1. The data directory, including the overlays' `LETTUCE_HOME` contexts and dev mode's default.
2. Lettuce from the source archive, the upstream image lookup, and the tested `LETTUCE_REF`.
3. The content tags, `RELEASE`, release mode in `up_stack`, `backup` and `receipts`,
   `test_check_release_images`; then `ci.yml` as a reusable workflow and `publish.yml`.
4. The `lettucectl` image, the wrapper, `self-upgrade`, the same-path check, and the `backup`,
   `receipts` and `toolbox` mounts.
5. `stacks.conf`, the commands that keep it in step, and `apply`.
6. Move the existing install.
7. Docs.
8. The guided `init`.

## Verified so far

On 2026-11-07, with Docker Compose 2.40.3:

- **A missing build context is harmless when the image can be pulled.** A service with
  `build.context` pointing at a missing directory and a pullable `image:` is pulled and started, with
  no error. When the pull fails, Compose falls back to a build and stops with
  `unable to prepare context: path ... not found`. No `!reset` overlay is needed. But `up_stack` runs
  `up -d --build`, which forces a build; release mode must not.
- **Renaming an image recreates its container.** A running service whose `image:` changed from
  `alpine:3.20` to another name for the same image was recreated (`Recreate`, `Recreated`). Hence
  [content tags](#image-names-by-content-not-by-release).
- **A container can read its own mounts.** In a container with the Docker socket, `docker inspect
  "$HOSTNAME"` listed every bind mount with its host source and its destination, including one
  mounted at a different path. Hence [the same-path check](#the-catch-every-host-path-must-be-the-same-inside-and-outside).
- **Upstream's compose file mounts no file from its repo.** Every bind mount is under
  `LETTA_STATE_DIR`; the rest are named volumes. Neither do this repo's overlays.
- **Upstream publishes its images for v0.22.0** (`lettuce-app-server`, `lettuce-bff`,
  `lettuce-searxng`, `lettuce-ddg-mcp`, `lettuce-google-mcp` on `ghcr.io/dmarchevsky/`), for
  `linux/amd64` only.
- **Licences:** as in [What can be published](#what-can-be-published), from
  `doc-tools/pyproject.toml`, `docs/doc-tools.md`, `pipelock/README.md` and `code-server/DESIGN.md`.
- **Paths:** every use of `LETTUCE_HOME` in `lettucectl` and `lib/`; `stack.env` stores
  `LETTA_STATE_DIR` as an absolute path; the overlays' build contexts are relative to
  `upstream/docker`.
- **Rules:** `BACKUP_RUN` passes `--init` because PID 1 ignores Ctrl-C's signal; `lib/README.md`
  says docs never tell operators to edit the profile lists by hand.

## To verify in the spike

- `additional_contexts: base: service:app-server` when the app-server's image is pulled rather than
  built.
- Compose with an `image:` that holds both a tag and a digest, under `--pull missing` and
  `--no-build`.
- The oldest Docker Engine that the image's Docker CLI and Compose work with, for the README.
- `docker buildx bake` reading this repo's compose files (with `additional_contexts`) for the
  multi-platform builds, or one `docker buildx build` per image if not.
- `docker inspect "$HOSTNAME"` when the wrapper is run with a user-defined `--hostname` or under
  rootless Docker; the check must fail closed, never pass, when it cannot find its container.
- Cloudflare's API for creating a Google login method, and the scope it needs.

## Not in scope

- **A release tarball or packages** (`.deb`, an apt repository): the `lettucectl` image is the
  release.
- **A manager service that reconciles on its own:** a long-running container holding the Docker
  socket and the Cloudflare token, and a rewrite of the CLI into a reconciler, for what `apply` gives
  on demand.
- **Terraform or OpenTofu for Cloudflare:** `lib/cloudflare.sh` is about a dozen calls, and `add`
  already resumes from the ids in `stack.env`. Terraform would add a binary, a state file holding
  tunnel tokens, and provider upgrades, and would split `add`. Drift, if it becomes a concern, is a
  `check` that reads the objects back.
- **Verifying signatures on the host by default:** it would need `cosign` or `gh` there, against
  "Docker only". The wrapper pins the digest, and `gh attestation verify` is documented for those who
  have it.
- **A `latest` tag, or images published from `main`:** see [`publish.yml`](#publishyml).
- **Ansible, cloud-init or VPS images.**
- **One Compose project for every stack:** each person's own project is the isolation.
- **Moving garden extras into upstream Lettuce.**
- **Publishing `doc-tools`:** see [What can be published](#what-can-be-published).
