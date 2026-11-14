# doc-tools

The document tools' image: [OfficeAgent.NET](https://github.com/ilia-sokolov/OfficeAgent.NET) and this repo's PDF MCP server, with their own Python, under `/opt/lollo-doc-tools`. With the `lollo-doc-tools` profile, every stack's app-server mounts that tree read-only, and agents call the two stdio MCP servers, `office` and `pdf`, from their workspaces. Conversions to PDF, merges, rotations, watermarks and stamps go to one shared Gotenberg per host. For users and admins (using it, running it, the security model): [docs/doc-tools.md](../docs/doc-tools.md).

![The document tools: image, volume, agent call and Gotenberg](../docs/diagrams/doc-tools-architecture.svg)

An interactive version, [doc-tools-architecture.html](../docs/diagrams/doc-tools-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `Dockerfile` | Multi-stage build. OfficeAgent's Linux x64 release, pinned by version and SHA-256; a standalone CPython and a venv installed from `requirements.lock` with `--require-hashes`; the launchers and OfficeAgent's config. A `test` stage runs pytest. The final stage is `FROM scratch` with only the tree at `/opt/lollo-doc-tools`: it is never run, only copied into the tools' volume. |
| `bin/officeagent` | Launcher for OfficeAgent's MCP server (stdio). Pins .NET's content root to the read-only tree, so an `appsettings.json` in the workspace is never read, and sets the one file-system connection's root to the workspace. |
| `bin/pdf-mcp` | Launcher for the PDF server: `python -I -B -m pdf_mcp` with `PDF_WORKSPACE_ROOT` and `GOTENBERG_URL=http://gotenberg:3000`. |
| `lib/launch.sh` | Sourced by both launchers: the workspace is the `/work/<agent-id>` that `pwd -P` is in (anywhere else, the server refuses to start), `HOME`, caches and `TMPDIR` under `/tmp/lollo-doc-tools`, no core dumps. |
| `office/config.json` | OfficeAgent's config: one connection, `workspace`, for .docx, .pptx and .xlsx up to 50 MB, tracked changes by default, no inline content. Its root is a placeholder in the read-only tree, so a start without the launcher fails. |
| `src/pdf_mcp/` | The PDF server (MCP SDK 2.x `MCPServer` over stdio). `paths.py` the workspace jail (each component opened with `O_NOFOLLOW` from a root fd, outputs written to a temp file and renamed, sources never overwritten); `ops.py` the in-process operations (PyMuPDF, pypdf); `gotenberg.py` the client (uploads only, never URLs, no proxy); `server.py` the twelve tools; `config.py` the limits read from the environment; `__main__.py` the entry point. |
| `pyproject.toml`, `requirements.lock` | The pinned dependencies and the hash-locked list the image installs; the lock's header has the command that regenerates it. |
| `tests/` | pytest, run in the image's `test` stage (Debian 12, like the app-server). |

`doc_tools_tag` (`lib/common.sh`) hashes this directory to tag the image and the volume, leaving out `tests/`, this `README.md` and Python caches: editing these docs does not change the tag, so it neither rebuilds the image nor recreates the app-servers.

## How it plugs in
- **Image:** `lollo-doc-tools:<tag>`, built by lolloctl with plain `docker build` (`ensure_doc_tools_image`), never by Compose and never run.
- **Volume:** `lollo-doc-tools-<tag>`, one per host, shared by every stack. `ensure_doc_tools_volume` fills it under a host-wide `flock` (`stacks/.doc-tools.lock`): a container of the image is created, never started, and `docker cp` streams the tree into a container of the backup image (no network, no capabilities but file ownership), which unpacks it and writes `.lollo-doc-tools-complete` last. The volume is labelled `lollo.doc-tools.tag=<tag>`; one without that label, not `local` or with driver options is refused.
- **Overlay** (`compose/doc-tools.yml`, merged by the `compose` wrapper only with the profile): mounts the volume read-only at `/opt/lollo-doc-tools` in the app-server and adds the stack's network `lollo-doc-tools-<name>` beside `default`. Both are `external`, so `down -v` never removes them. lolloctl passes `DOC_TOOLS_VOLUME` and `DOC_TOOLS_NETWORK` through `env -i`; they are never written to `stack.env`.
- **Network:** `lollo-doc-tools-<name>`, created `--internal` with the label `lollo.doc-tools.stack=<name>` (`ensure_doc_tools_network`).
- **Gotenberg** (`compose/shared/gotenberg.yml`): one container per host in the Compose project `lollo_shared` (`compose_shared`). `sync_doc_tools`, at the end of `add`, `up` and every `profiles` run, starts it and connects it with the alias `gotenberg` to each complete stack's network when that network is internal and labelled for that stack; it deletes networks of stacks that are gone or without the profile, and volumes of older tags. With the profile off it stops Gotenberg and deletes every labelled network and volume.
- **Settings -> MCP:** `lolloctl mcp` streams `lib/mcp-servers.mjs` into the app-server, which adds `office` (`/opt/lollo-doc-tools/bin/officeagent`) and `pdf` (`/opt/lollo-doc-tools/bin/pdf-mcp`) once per stack (`DOC_TOOLS_MCP_REGISTERED`), then restarts the BFF alone so the `mcp-servers` skill names them (`refresh_mcp_skill`). `add` registers a new stack's servers. With the profile off, `up` and `profiles` remove them.
- **Agents** call the servers through Lettuce's `mcp-servers` skill (`letta mcp call`); the `working-with-documents` skill ([agent-skills/](../agent-skills/README.md)) tells them which server does what.
- **`check_stack`:** whatever the profiles, only the app-server may mount an image or the tools' volume. With the profile, the mount must be this tree's volume, read-only, at `/opt/lollo-doc-tools`; only the app-server joins the network and it keeps `default`; the network must be internal and labelled for the stack (`check_doc_tools`). Before Gotenberg starts, `check_gotenberg` refuses it without its hardening, as root, with a port, a mount or a non-internal network, without any of the flags that stop documents fetching URLs or with one set back to `false`, or with an allow-list, disabled web security or debug logging. The full list is under [Rules](#rules).
- **No tunnel route, no ports:** nothing here is reachable from outside the host.

### At a glance
```
stack alfred                                   stack hal
app-server (agents, root)                      app-server
  letta mcp call ─► office / pdf (stdio,         letta mcp call ─► office / pdf
  one process per call, /work/<agent-id>)        (/work/<agent-id>)
        │ /opt/lollo-doc-tools (read-only volume, the same one for every stack)
        │                                               │
  lollo-doc-tools-alfred (internal)          lollo-doc-tools-hal (internal)
        └──────────────► gotenberg ◄────────────────────┘
                 (one per host, Compose project lollo_shared,
                  no ports, no route out, no workspace mounted)
```

### What `up` does with the profile
1. It fills the volume `lollo-doc-tools-<hash>` when it is missing or lacks its completion mark,
   once per host (about 300 MB, a second or two), and marks it complete last; a volume without the
   mark is filled again. The copy runs in a container with no network and no capabilities beyond
   file ownership. Only for a fill, it first builds `lollo-doc-tools:<hash>` (`doc-tools/Dockerfile`)
   when that image is missing. The hash covers everything in `doc-tools/` but its tests and README,
   so a change there is a new image and a new volume. The first build downloads OfficeAgent (checked
   against its pinned SHA-256), a Python runtime and the Python packages (hash-pinned in
   `doc-tools/requirements.lock`), and takes a few minutes. A failed fill stops the `up` before
   anything else is created.
2. It creates the stack's internal network `lollo-doc-tools-<name>`.
3. It starts the app-server with the volume mounted read-only at `/opt/lollo-doc-tools` and on
   that network (`compose/doc-tools.yml`).
4. At the end of the command, it starts the shared Gotenberg (`compose/shared/gotenberg.yml`,
   project `lollo_shared`) if it is not running, and connects it to every stack's network
   (`sync_doc_tools`).

The tools are a volume rather than an image mount because Docker 29 brings image mounts back
empty after a daemon restart or a reboot; a volume survives both like the rest of the stack.

## Rules

### lolloctl owns the image, the volume and the networks; Compose only uses them
`compose/doc-tools.yml` declares the volume and the stack's network `external`, so `down`, `down -v`, `restart` and network recreation never touch them. lolloctl creates and deletes them:
- **The image** is built with plain `docker build`, only for a fill, and never run.
- **The volume fill** (`ensure_doc_tools_volume`) runs under a `flock` on `stacks/.doc-tools.lock`, since the volume is shared. Containers an interrupted fill left (label `lolloctl.doc-tools=fill`) are removed first. An incomplete volume that is still in use stops the `up`. So does a volume of that name without the label, not `local` or with driver options: every app-server would run what it holds.
- **`sync_doc_tools`** runs at the end of `add`, `up` and every `profiles` run, and compares what exists rather than what changed. With the profile on, it starts Gotenberg (after `check_gotenberg`) and connects it, with the alias `gotenberg`, to each complete stack's network it is not on yet, only when that network is internal and labelled for that stack. A network of that name made by hand is refused, and Gotenberg is disconnected from it. It deletes labelled networks whose stack is gone, removed or without the profile, and labelled volumes of other tags. With the profile off, it takes Gotenberg down and deletes every labelled network and volume.
- **A volume still in use** is left quietly for a later run: it is only disk space.
- **Failures** are collected and fail the command at the end ("run the same command again").
- **`remove`** deletes the stack's network after `compose down -v`, force-disconnecting Gotenberg, which stays for the others. `--purge` renders the stack without `lollo-doc-tools` (`env_without_profile`), so it needs neither the image nor the network.
- **The shared project is `lollo_shared`.** An underscore can't be in a stack name, so a stack called `shared` (`lollo-shared`) never matches it.

### What `check_stack` refuses
Whatever the profiles: a service other than the app-server with an image mount, or mounting the tools' volume (the key `doc-tools`, a `lollo-doc-tools*` source, or a top-level volume of that name).

With the profile (`check_doc_tools`):
- an app-server whose mount at `/opt/lollo-doc-tools` is not the volume `doc-tools` read-only, or whose `doc-tools` volume is not the external `lollo-doc-tools-<tag>` of this tree;
- an app-server without `default` or `doc-tools` among its networks, or another service on `doc-tools`;
- a `doc-tools` network that is not the external `lollo-doc-tools-<name>`, or, when it exists, not internal with lolloctl's label for that stack.

Before `sync_doc_tools` starts Gotenberg (`check_gotenberg`): a rendered shared Gotenberg that is not hardened (`check_hardened`), runs as root or without `no-new-privileges`, publishes a port, mounts anything, sits on a non-internal network, lacks any of the flags that stop documents fetching URLs or sets one back to `false`, or sets an allow-list, disabled web security or debug logging.

### Docs live only in this README
`doc_tools_tag` skips only `tests/`, this `README.md`, the agents' pointer to it (`AGENTS.md`, and `CLAUDE.md` linking to it) and Python caches. Any other new file here changes the tag, which rebuilds the image, refills the volume and recreates every app-server. So never add another doc file to this folder.

## Build and test
```bash
tests/run-sidecar.sh doc-tools            # the PDF server's tests (pytest in the image's test stage)
node --test tests/mcp-servers.test.mjs    # lib/mcp-servers.mjs against temp settings files
tests/run.sh doc_tools                    # the CLI: tag, image, volume, network, checks
tests/run.sh gotenberg
shellcheck -x doc-tools/bin/* doc-tools/lib/launch.sh
scripts/doc-tools-e2e.sh                  # against the real app-server image and a real Gotenberg (Docker, internet for the first build)
```
`up` builds the image of a new tag, fills its volume and recreates the app-servers.

The PDF server's tests never use the network: Gotenberg is an `httpx.MockTransport` (`FakeGotenberg`), the documents are generated by the tests, and a canary string (`CANARY_DOC_7731_DO_NOT_LEAK`) is asserted never to reach stderr or errors. They run real stdio sessions with `python -m pdf_mcp`, and check the jail against symlinks (leaf, middle, swapped mid-walk), FIFOs, devices and existing outputs. The CLI's helpers for doc-tools are in [tests/README.md](../tests/README.md).

## Maintaining
Every version is pinned in `doc-tools/`:
- **OfficeAgent:** `OFFICEAGENT_VERSION` and `OFFICEAGENT_SHA256` in `doc-tools/Dockerfile`. Take
  the SHA-256 from the release's `SHA256SUMS`, and check it with
  `gh attestation verify officeagent-mcp-linux-x64.tar.gz --repo ilia-sokolov/OfficeAgent.NET`.
- **Python packages:** `doc-tools/pyproject.toml`, then regenerate `doc-tools/requirements.lock`
  with the command at its top.
- **Python:** `PYTHON_VERSION` in `doc-tools/Dockerfile`.
- **Gotenberg:** the image and its multi-arch index digest in `compose/shared/gotenberg.yml`. Read
  its release notes for removed or renamed flags: an unknown flag stops it from starting.

Then run `tests/run-sidecar.sh doc-tools` and `./lolloctl up all` (what that rebuilds and keeps: [docs/doc-tools.md](../docs/doc-tools.md#after-an-update)). On every Lettuce upgrade, check the dependencies below ([Upstream seams](#upstream-seams): how `letta mcp` starts stdio servers, the `/work/<agent-id>` layout, the app-server's glibc and ICU).

`scripts/doc-tools-e2e.sh` measured the figures in [Resource use](../docs/doc-tools.md#resource-use). Run it again after changing `doc-tools/` or the Gotenberg pin; it uses throwaway containers only.

## Upstream seams
Listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams), under `doc-tools`:
- how `letta mcp call` runs a stdio server from Settings -> MCP: started per call in the app-server, in the calling agent's cwd, with only `HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME` and the entry's `env` (no `PWD`, hence `pwd -P`); stdin closed, then SIGTERM, then SIGKILL 2 s apart; stderr piped and never read (so the launchers keep it quiet); 60 s per request (`PDF_REQUEST_TIMEOUT_SECONDS` stays under it); a bare `tools` or `search` starts every stdio server;
- the shared MCP list's file, `/root/.letta/mcp-home/.letta/settings.json`, and its shape, which `lib/mcp-servers.mjs` refuses to touch when it changes;
- the `mcp-servers` skill wrapper, `/root/.letta/skills/mcp-servers/scripts/mcp.sh`, whose description the BFF renders only on a Settings -> MCP save or a connect;
- workspaces at `/work/<agent-id>`;
- an app-server image where `/opt/lollo-doc-tools` is free, with glibc >= 2.28 and system ICU (OfficeAgent), and a `/tmp` that allows exec.
