# doc-tools

The document tools' image: [OfficeAgent.NET](https://github.com/ilia-sokolov/OfficeAgent.NET) and this repo's PDF MCP server, with their own Python, under `/opt/lettuce-doc-tools`. With the `garden-doc-tools` profile, every stack's app-server mounts that tree read-only, and agents call the two stdio MCP servers, `office` and `pdf`, from their workspaces. Conversions to PDF, merges, rotations, watermarks and stamps go to one shared Gotenberg per host. For users and admins (using it, running it, the security model): [docs/doc-tools.md](../docs/doc-tools.md).

![The document tools: image, volume, agent call and Gotenberg](../docs/diagrams/doc-tools-architecture.svg)

An interactive version, [doc-tools-architecture.html](../docs/diagrams/doc-tools-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `Dockerfile` | Multi-stage build. OfficeAgent's Linux x64 release, pinned by version and SHA-256; a standalone CPython and a venv installed from `requirements.lock` with `--require-hashes`; the launchers and OfficeAgent's config. A `test` stage runs pytest. The final stage is `FROM scratch` with only the tree at `/opt/lettuce-doc-tools`: it is never run, only copied into the tools' volume. |
| `bin/officeagent` | Launcher for OfficeAgent's MCP server (stdio). Pins .NET's content root to the read-only tree, so an `appsettings.json` in the workspace is never read, and sets the one file-system connection's root to the workspace. |
| `bin/pdf-mcp` | Launcher for the PDF server: `python -I -B -m pdf_mcp` with `PDF_WORKSPACE_ROOT` and `GOTENBERG_URL=http://gotenberg:3000`. |
| `lib/launch.sh` | Sourced by both launchers: the workspace is the `/work/<agent-id>` that `pwd -P` is in (anywhere else, the server refuses to start), `HOME`, caches and `TMPDIR` under `/tmp/lettuce-doc-tools`, no core dumps. |
| `office/config.json` | OfficeAgent's config: one connection, `workspace`, for .docx, .pptx and .xlsx up to 50 MB, tracked changes by default, no inline content. Its root is a placeholder in the read-only tree, so a start without the launcher fails. |
| `src/pdf_mcp/` | The PDF server (MCP SDK 2.x `MCPServer` over stdio). `paths.py` the workspace jail (each component opened with `O_NOFOLLOW` from a root fd, outputs written to a temp file and renamed, sources never overwritten); `ops.py` the in-process operations (PyMuPDF, pypdf); `gotenberg.py` the client (uploads only, never URLs, no proxy); `server.py` the twelve tools; `config.py` the limits read from the environment; `__main__.py` the entry point. |
| `pyproject.toml`, `requirements.lock` | The pinned dependencies and the hash-locked list the image installs; the lock's header has the command that regenerates it. |
| `tests/` | pytest, run in the image's `test` stage (Debian 12, like the app-server). |

`doc_tools_tag` (`lib/common.sh`) hashes this directory to tag the image and the volume, leaving out `tests/`, this `README.md` and Python caches: editing these docs does not change the tag, so it neither rebuilds the image nor recreates the app-servers.

## How it plugs in
- **Image:** `lettuce-doc-tools:<tag>`, built by lettucectl with plain `docker build` (`ensure_doc_tools_image`), never by Compose and never run.
- **Volume:** `lettuce-doc-tools-<tag>`, one per host, shared by every stack. `ensure_doc_tools_volume` fills it under a host-wide `flock` (`stacks/.doc-tools.lock`): a container of the image is created, never started, and `docker cp` streams the tree into a container of the backup image (no network, no capabilities but file ownership), which unpacks it and writes `.lettuce-doc-tools-complete` last. The volume is labelled `lettuce.doc-tools.tag=<tag>`; one without that label, not `local` or with driver options is refused.
- **Overlay** (`compose/doc-tools.yml`, merged by the `compose` wrapper only with the profile): mounts the volume read-only at `/opt/lettuce-doc-tools` in the app-server and adds the stack's network `lettuce-doc-tools-<name>` beside `default`. Both are `external`, so `down -v` never removes them. lettucectl passes `DOC_TOOLS_VOLUME` and `DOC_TOOLS_NETWORK` through `env -i`; they are never written to `stack.env`.
- **Network:** `lettuce-doc-tools-<name>`, created `--internal` with the label `lettuce.doc-tools.stack=<name>` (`ensure_doc_tools_network`).
- **Gotenberg** (`compose/shared/gotenberg.yml`): one container per host in the Compose project `lettuce_shared` (`compose_shared`). `sync_doc_tools`, at the end of `add`, `up` and every `profiles` run, starts it and connects it with the alias `gotenberg` to each complete stack's network when that network is internal and labelled for that stack; it deletes networks of stacks that are gone or without the profile, and volumes of older tags. With the profile off it stops Gotenberg and deletes every labelled network and volume.
- **Settings -> MCP:** `lettucectl mcp` streams `lib/mcp-servers.mjs` into the app-server, which adds `office` (`/opt/lettuce-doc-tools/bin/officeagent`) and `pdf` (`/opt/lettuce-doc-tools/bin/pdf-mcp`) once per stack (`DOC_TOOLS_MCP_REGISTERED`), then restarts the BFF alone so the `mcp-servers` skill names them (`refresh_mcp_skill`). `add` registers a new stack's servers. With the profile off, `up` and `profiles` remove them.
- **Agents** call the servers through Lettuce's `mcp-servers` skill (`letta mcp call`); the `working-with-documents` skill ([agent-skills/](../agent-skills/README.md)) tells them which server does what.
- **`check_stack`:** whatever the profiles, only the app-server may mount an image or the tools' volume. With the profile, the mount must be this tree's volume, read-only, at `/opt/lettuce-doc-tools`; only the app-server joins the network and it keeps `default`; the network must be internal and labelled for the stack (`check_doc_tools`). Before Gotenberg starts, `check_gotenberg` refuses it without its hardening, as root, with a port, a mount or a non-internal network, without any of the flags that stop documents fetching URLs or with one set back to `false`, or with an allow-list, disabled web security or debug logging. See [Key invariants](../AGENTS.md#key-invariants).
- **No tunnel route, no ports:** nothing here is reachable from outside the host.

### At a glance
```
stack alfred                                   stack hal
app-server (agents, root)                      app-server
  letta mcp call ─► office / pdf (stdio,         letta mcp call ─► office / pdf
  one process per call, /work/<agent-id>)        (/work/<agent-id>)
        │ /opt/lettuce-doc-tools (read-only volume, the same one for every stack)
        │                                               │
  lettuce-doc-tools-alfred (internal)          lettuce-doc-tools-hal (internal)
        └──────────────► gotenberg ◄────────────────────┘
                 (one per host, Compose project lettuce_shared,
                  no ports, no route out, no workspace mounted)
```

### What `up` does with the profile
1. It fills the volume `lettuce-doc-tools-<hash>` when it is missing or lacks its completion mark,
   once per host (about 300 MB, a second or two), and marks it complete last; a volume without the
   mark is filled again. The copy runs in a container with no network and no capabilities beyond
   file ownership. Only for a fill, it first builds `lettuce-doc-tools:<hash>` (`doc-tools/Dockerfile`)
   when that image is missing. The hash covers everything in `doc-tools/` but its tests and README,
   so a change there is a new image and a new volume. The first build downloads OfficeAgent (checked
   against its pinned SHA-256), a Python runtime and the Python packages (hash-pinned in
   `doc-tools/requirements.lock`), and takes a few minutes. A failed fill stops the `up` before
   anything else is created.
2. It creates the stack's internal network `lettuce-doc-tools-<name>`.
3. It starts the app-server with the volume mounted read-only at `/opt/lettuce-doc-tools` and on
   that network (`compose/doc-tools.yml`).
4. At the end of the command, it starts the shared Gotenberg (`compose/shared/gotenberg.yml`,
   project `lettuce_shared`) if it is not running, and connects it to every stack's network
   (`sync_doc_tools`).

The tools are a volume rather than an image mount because Docker 29 brings image mounts back
empty after a daemon restart or a reboot; a volume survives both like the rest of the stack.

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

Then run `tests/run-sidecar.sh doc-tools` and `./lettucectl up all` (what that rebuilds and keeps: [docs/doc-tools.md](../docs/doc-tools.md#after-an-update)). On every Lettuce upgrade, check the dependencies listed in AGENTS.md (how `letta mcp` starts stdio servers, the `/work/<agent-id>` layout, the app-server's glibc and ICU).

`scripts/doc-tools-e2e.sh` measured the figures in [Resource use](../docs/doc-tools.md#resource-use). Run it again after changing `doc-tools/` or the Gotenberg pin; it uses throwaway containers only.

## Upstream seams
Named in [AGENTS.md](../AGENTS.md) under `doc-tools`:
- how `letta mcp call` runs a stdio server from Settings -> MCP: started per call in the app-server, in the calling agent's cwd, with only `HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME` and the entry's `env` (no `PWD`, hence `pwd -P`); stdin closed, then SIGTERM, then SIGKILL 2 s apart; 60 s per request (`PDF_REQUEST_TIMEOUT_SECONDS` stays under it); a bare `tools` or `search` starts every stdio server;
- the shared MCP list's file, `/root/.letta/mcp-home/.letta/settings.json`, and its shape, which `lib/mcp-servers.mjs` refuses to touch when it changes;
- the `mcp-servers` skill wrapper, `/root/.letta/skills/mcp-servers/scripts/mcp.sh`, whose description the BFF renders only on a Settings -> MCP save or a connect;
- workspaces at `/work/<agent-id>`;
- an app-server image where `/opt/lettuce-doc-tools` is free, with glibc >= 2.28 and system ICU (OfficeAgent), and a `/tmp` that allows exec.
