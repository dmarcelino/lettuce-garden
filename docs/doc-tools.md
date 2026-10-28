# Document tools (Office and PDF)

The `doc-tools` profile lets the agents of every stack create, read, edit and convert Word,
PowerPoint and Excel files and PDFs in their own workspaces. Two MCP servers do the work:

- **`office`**: [OfficeAgent.NET](https://github.com/ilia-sokolov/OfficeAgent.NET) (MIT). It
  creates and edits .docx, .pptx and .xlsx files, with tracked changes and comments in Word,
  slides, formulas and templates.
- **`pdf`**: this repo's PDF server (`doc-tools/`). It extracts text and images, renders pages to
  PNG, creates simple PDFs, converts Office and HTML files to PDF (and PDF/A), merges, splits,
  rotates, watermarks, annotates, redacts, fills forms and encrypts. It is built on
  [PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL-3.0) and
  [pypdf](https://pypdf.readthedocs.io/) (BSD).

Conversions to PDF, merging, rotating, watermarks and stamps run on one shared
[Gotenberg](https://gotenberg.dev) per host (MIT; LibreOffice for Office files, Chromium for
HTML); everything else, splitting and extracting pages included, runs in the `pdf` server itself.

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

## Turning it on
```bash
./lettucectl profiles cloudflared,search,doc-tools   # your current list plus doc-tools
./lettucectl skills all                              # installs the working-with-documents skill
./lettucectl mcp all                                 # adds office and pdf to each Settings -> MCP
```
`mcp` adds the two servers to each stack's **Settings -> MCP**, where each person sees them next
to their own:

| Name | Transport | Command |
|---|---|---|
| `office` | stdio | `/opt/lettuce-doc-tools/bin/officeagent` |
| `pdf` | stdio | `/opt/lettuce-doc-tools/bin/pdf-mcp` |

No arguments and no environment variables are needed, and a person can also add them by hand
there. The entries survive restarts and upgrades (they live in the stack's `letta-home`).

`mcp` runs once per stack: it remembers that it did (`DOC_TOOLS_MCP_REGISTERED` in `stack.env`),
so a person who removes a server keeps it removed; `mcp <name> --force` adds it again. It adds
only what is missing, and leaves alone every other server and a server of the same name with
another command (it says so). `add` registers a new stack's servers itself. With the `secrets`
profile on, `mcp` registers the vault too, the same way and with its own marker
([secret-broker.md](secret-broker.md#for-the-operator)); it needs one of the two profiles.

The list belongs to Lettuce's BFF, which reads it from disk before every change, so an entry
added from outside stays. `mcp` writes only the entries whose command is under
`/opt/lettuce-doc-tools/` (and the vault's exact HTTP entry), and refuses, changing nothing, when the file is not the shape it
knows (after a Lettuce upgrade, say): then add the servers by hand. Two things can still undo
it: a Settings -> MCP page that was open before `mcp` ran and is saved afterwards writes back
the list it loaded, without the servers; run `mcp <name> --force` again.

Agents learn which servers exist from the description of Lettuce's `mcp-servers` skill, which
the BFF writes from the list when it connects to the app-server, not when the file changes. So
whenever `mcp` changes a stack's list, it restarts that stack's BFF alone; a turn in progress
finishes first (up to 10 minutes), and the vault and the app-server keep running. Stacks are
restarted one after another, so `mcp all` can wait that long for each busy stack, and turning
the profile off can restart a BFF that `up` has just recreated. If the
restart fails, it says so, and agents see the servers after the next `./lettucectl restart <name>`.

What `up` does with the profile:
1. It builds `lettuce-doc-tools:<hash>` (`doc-tools/Dockerfile`), once per host. The hash covers
   everything in `doc-tools/` but its tests, so a change there is a new image. The first build
   downloads OfficeAgent (checked against its pinned SHA-256), a Python runtime and the Python
   packages (hash-pinned in `doc-tools/requirements.lock`), and takes a few minutes.
2. It creates the stack's internal network `lettuce-doc-tools-<name>`.
3. It copies the image's tools into the volume `lettuce-doc-tools-<hash>`, once per host (about
   300 MB, a second or two), and marks it complete last; a volume without the mark is filled again.
   The copy runs in a container with no network and no capabilities beyond file ownership.
4. It starts the app-server with the volume mounted read-only at `/opt/lettuce-doc-tools` and on
   that network (`compose/doc-tools.yml`).
5. It starts the shared Gotenberg (`compose/shared/gotenberg.yml`, project `lettuce_shared`) if it
   is not running, and connects it to every stack's network.

The tools are a volume rather than an image mount because Docker 29 brings image mounts back
empty after a daemon restart or a reboot; a volume survives both like the rest of the stack.

## Turning it off
```bash
./lettucectl profiles cloudflared,search   # the list without doc-tools
```
The app-servers restart without the mount and the network, Gotenberg stops and is removed, and
every `lettuce-doc-tools-*` network and tools volume is deleted. Documents stay in the
workspaces. The servers `mcp` registered are removed from Settings -> MCP (a stdio server that
cannot start makes every `letta mcp tools` call fail, the other servers' included), and the BFF
restarts so agents stop seeing them; servers a person added by hand stay, and they should remove
them. If a removal fails, the next `up` or `profiles` retries it. The image stays until you
delete it (`docker image rm lettuce-doc-tools:<hash>`).

## What agents do with it
The `working-with-documents` skill tells agents which server to use for what, and how to check
their work: create or edit the Office file, convert it to PDF, render pages and look at them,
then report. Edits to existing Word files are tracked changes by default. Agents call the
servers through Lettuce's `mcp-servers` skill (`letta mcp call mcp__office__...`), because
Lettuce's native MCP tools only reach HTTP servers.

Capabilities in short:

| | office | pdf |
|---|---|---|
| Create | .docx, .pptx, .xlsx, from templates | simple PDFs from text or HTML |
| Read | structure, text, tables, comments, revisions | text by page, embedded images, page renders, metadata, form fields |
| Edit | text, paragraphs, tables, images, styles, tracked changes, comments, slides, cells, formulas | annotations, redaction, form filling, metadata, encryption (AES-256) |
| Convert | | Office, ODF, RTF, CSV, HTML to PDF; PDF/A-1b/2b/3b, PDF/UA |
| Pages | | merge, split, extract, rotate, watermark, stamp |

Not included: OCR (a scanned PDF without text is reported as such), editing a PDF's existing
text in place, and Excel calculation beyond OfficeAgent's list of supported functions.

## Security model
- **Isolation between people is the stack.** Each stack's servers run in that stack's
  app-server and see only its `/work`. Nothing of one stack is mounted in another, and the
  app-servers of two stacks share no network: each stack has its own internal network, which
  only its app-server (and the BFF in its namespace) and Gotenberg join. `check` refuses a stack
  where another service joins it, where the app-server loses its default network, or where the
  mount is anything but this image, read-only.
- **Within a stack, the path checks are a guard, not a boundary.** Each server is confined to
  the calling agent's workspace (`/work/<agent-id>`): paths are relative, `..` and absolute paths
  are refused, and symbolic links are never followed (each path component is opened from the
  workspace without following links; outputs are written to a temp file and renamed). This stops
  mistakes and confused tool calls. It does not stop the agent, which is root in the same
  container and can read every workspace of its stack with its shell anyway.
- **OfficeAgent** is started by a launcher that pins its configuration: .NET's content root is
  the read-only tools volume, so an `appsettings.json` in a workspace cannot add a connection or turn on
  inline content; diagnostics and core dumps are off (a core dump holds document content). It
  makes no network calls and sends no telemetry.
- **Gotenberg** sees only the files a conversion uploads, keeps nothing, mounts no workspace,
  publishes no port and has no route out (every network it is on is internal). Its flags stop a
  document from fetching anything: Chromium and LibreOffice may not reach private or public
  addresses, JavaScript is off, and `downloadFrom` and webhooks are disabled. `check` refuses a
  Gotenberg without these flags (or with one set back to `false`), with a port, or without its
  hardening (read-only root, no capabilities, non-root user, `no-new-privileges`). lettucectl
  connects it only to internal networks it made for a stack, labelled with that stack's name. Each conversion is independent; simultaneous requests from
  different stacks get their own results.
- **Gotenberg is the one container on every stack's network.** A conversion that compromised
  LibreOffice or Chromium could reach the other stacks' app-server namespaces (the BFF and the
  vault check Cloudflare Access tokens, which it does not have). If that risk matters more than
  memory, run one Gotenberg per stack instead (a change to this design, not a setting).
- **Logs.** At its `warn` level, Gotenberg logs no document content, but it does log every URL
  an HTML file tried to load (with its query string) and any value a request was refused for.
  The log covers every stack. Never run it at `debug`, which logs file names. The PDF server
  writes nothing about documents to its error output.
- **Licences.** PyMuPDF is AGPL-3.0. The image is built on your host from this repo and never
  published, and the repo does not include PyMuPDF. If you publish or distribute the image, the
  AGPL applies to it.

## Upgrading
Every version is pinned in `doc-tools/`:
- **OfficeAgent:** `OFFICEAGENT_VERSION` and `OFFICEAGENT_SHA256` in `doc-tools/Dockerfile`. Take
  the SHA-256 from the release's `SHA256SUMS`, and check it with
  `gh attestation verify officeagent-mcp-linux-x64.tar.gz --repo ilia-sokolov/OfficeAgent.NET`.
- **Python packages:** `doc-tools/pyproject.toml`, then regenerate `doc-tools/requirements.lock`
  with the command at its top.
- **Python:** `PYTHON_VERSION` in `doc-tools/Dockerfile`.
- **Gotenberg:** the image and its multi-arch index digest in `compose/shared/gotenberg.yml`. Read
  its release notes for removed or renamed flags: an unknown flag stops it from starting.

Then run `tests/run-sidecar.sh doc-tools` and `./lettucectl up all`. The new hash builds a new
image, fills a new volume and recreates the app-servers (the old volume is deleted once no
container uses it; old images stay until you delete them); documents, memories, conversations, MCP entries, the vault
and settings are not touched. On every Lettuce upgrade, check the dependencies listed in
AGENTS.md (how `letta mcp` starts stdio servers, the `/work/<agent-id>` layout, the app-server's
glibc and ICU).

The bundled runtimes (.NET inside OfficeAgent, CPython, MuPDF) get security fixes only when you
bump these pins.

## Removing a stack
`remove` deletes the stack's network to Gotenberg after stopping the stack; Gotenberg keeps
serving the others. The person's MCP entries live in that stack's data and go with `--purge`.
If removing the network fails, the next `up` or `profiles` removes it.

## Troubleshooting
- **`MCP error -32000: Connection closed`**: the server did not start. The usual cause is a call
  made outside an agent workspace: the servers refuse to run unless the working directory is
  inside `/work/<agent-id>`. Check with `./lettucectl shell <name> <agent>`, then
  `cd /work/<agent-id> && /opt/lettuce-doc-tools/bin/pdf-mcp </dev/null` (it prints why it
  refuses, or waits for input when it is fine).
- **`the converter (Gotenberg) is unreachable`**: check `docker compose -p lettuce_shared ps`, and
  that the stack's network has it: `docker network inspect lettuce-doc-tools-<name>`. Running
  `./lettucectl up <name>` repairs both.
- **`the converter took too long`**: letta-code gives each MCP call 60 seconds, so conversions
  stop at 50. Split the document or reduce its images.
- **`not-found` from office after parallel calls**: calls made at the same time in one workspace
  can lose each other's document ids. Open the file again by its path.
- **`spawn failed: ENOENT /opt/lettuce-doc-tools/bin/...`**, or an empty `/opt/lettuce-doc-tools`
  in the app-server: the tools are not mounted. Run `./lettucectl up <name>`; if it says the
  volume is incomplete and in use, every stack with doc-tools shares it: run
  `./lettucectl down all`, then `./lettucectl up all`.
- **`lettuce-doc-tools-<hash> was not made by lettucectl`** at `up`: a volume of that name exists
  without lettucectl's label, or with driver options (a bind to a host directory, say). Every
  app-server would run what it holds, so `up` refuses it; remove it and run `up` again.
- **`could not fill the volume lettuce-doc-tools-<hash>`** at `up`: the copy failed (disk full,
  say); the stack was not started. Fix the cause and run `up` again: an incomplete volume is
  filled again.
- **Agents do not find `office` or `pdf`** although `mcp` added them: the `mcp-servers` skill's
  description (in the app-server, `/root/.letta/skills/mcp-servers/SKILL.md`) should list them.
  If it does not, the BFF restart after `mcp` failed: run `./lettucectl restart <name>`.
- **`mcp` says the MCP settings are not what lettucectl expects**: Lettuce changed the file's
  layout; add the servers by hand in Settings -> MCP, and report it so `lib/mcp-servers.mjs`
  can follow.
- **`office` or `pdf` gone from Settings -> MCP**: a Settings page that was open before `mcp` ran
  was saved over them; run `./lettucectl mcp <name> --force`.
- **`check` refuses the stack**: the message names the rule; the overlay and the shared file are
  the reference (`compose/doc-tools.yml`, `compose/shared/gotenberg.yml`).

## Resource use
Measured with `scripts/doc-tools-e2e.sh` on one x86_64 host (Docker 29.1, app-server
`v0.22.0-letta_0.34.21`, four simulated stacks). Run it again after changing `doc-tools/` or the
Gotenberg pin; it uses throwaway containers only.

| What | Measured |
|---|---|
| `office` or `pdf` when no call runs | 0 (each call starts a process and ends it) |
| `office` call through `letta mcp call` | 0.9-1.4 s (about 0.6 s is the `letta` CLI), 68-128 MiB peak |
| `pdf` call through `letta mcp call` | 2.4-2.8 s, about 200 MiB peak, whatever the tool: most of it is loading the MCP SDK (0.9 s) and PyMuPDF (0.4 s) |
| Gotenberg idle | 7 MiB (engines start on the first job), 10 MiB a minute after the last job |
| Gotenberg converting: 1 stack, 3 files at once / 4 stacks, 12 files at once | 124 MiB peak / 139 MiB peak (all correct, 5.4 s for the 12) |
| Gotenberg peak in a run of large conversions (46 MB photo deck, 60-page HTML) | 641 MiB (limit 2 GiB) |
| First conversion after Gotenberg starts | 2.7 s (LibreOffice starts) |
| Conversion: 3-page docx / 10-slide pptx / 5-page HTML, warm | 0.1-0.4 s |
| Images on disk | doc-tools 445 MB (once per host), Gotenberg 2.46 GB |
| Extra disk per stack | about 0: 64 KiB in the app-server's container layer, the documents, and a 1 KiB `.officeagent/index.json` per workspace |

One stack or four makes no difference to the tools: they run only during calls, in each stack's
own app-server, and Gotenberg serves conversions one LibreOffice job at a time.
