# Architecture

What runs for one person, with every profile on, and how the parts reach each other. Each person has a stack like this one: its own Compose project (`lettuce-<name>`), Cloudflare Tunnel, two Cloudflare Access applications and state directory. Only the document tools' Gotenberg and volume are shared by the stacks of a host.

![Every container of one stack, its networks, volumes and routes](diagrams/lettuce-garden-overview.svg)

An interactive version, [lettuce-garden-overview.html](diagrams/lettuce-garden-overview.html), opens in a browser from a clone of this repo. Each folder's README has a narrower diagram of its own part.

## Two hosts per stack
| Host | Access application | Tunnel routes it to |
|---|---|---|
| `<name>.<domain>` | Lettuce's (`CF_ACCESS_AUD`) | `app-server:8080`, the BFF's port in the app-server's network namespace |
| `<name>-garden.<domain>` | the garden's (`CF_GARDEN_AUD`) | `^/vault(/\|$)` to `garden-vault:8000`, `^/gmail-triggers(/\|$)` to `garden-gmail-triggers:8000`, `^/code-server(/\|$)` to `garden-code-server:8000` (each only with its profile), `^/(assets/[^/]+)?$` to `garden:8000` (with any of them); anything else gets a 404 |

Both applications share the stack's Access policy, so `lettucectl users` changes who may sign in to both. The garden's services accept only the garden's tokens, never Lettuce's, which agents could catch inside the app-server's namespace. The routes are written by `cf_tunnel_configure` ([lib/cloudflare.sh](../lib/cloudflare.sh)) from `PROFILE_ROUTES`, on `add` and on every `profiles` run. `/executor/v1/` and `/healthz` are never routed.

## Containers
| Service | From | Profile | Networks | Mounts |
|---|---|---|---|---|
| `cloudflared` | upstream | `cloudflared` (required) | `default` | none |
| `app-server` | upstream | always | `default`; `lettuce-doc-tools-<name>` with `garden-doc-tools` | `state/letta-home`, `letta-data`, `workspaces`, `tools`; the doc-tools volume read-only with `garden-doc-tools` |
| `bff` | upstream | always | the app-server's namespace | `google-policy`, `google-creds`, `bff-data`, `bff-auth`; `workspaces`, `letta-home` and the memory files read-only |
| `channel-gateway` | upstream | `telegram` | the app-server's namespace | `letta-home`, `letta-data`, `workspaces` |
| `searxng`, `ddg-mcp` | upstream | `search` | `default` | none |
| `google-mcp` | upstream | `google` | `default` | `google-policy` read-only, `google-creds` |
| `garden` | [garden/](../garden/README.md) | any garden page profile | `default` | none |
| `garden-vault` | [vault/](../vault/README.md) | `garden-vault` | `default` | `vault-data` |
| `garden-gmail-triggers` | [gmail-triggers/](../gmail-triggers/README.md) | `garden-gmail-triggers` | `default` | `state/garden/gmail-triggers`; `google-creds` and `google-policy` read-only |
| `garden-pipelock` | [pipelock/](../pipelock/README.md) | `garden-pipelock` | `default` | `pipelock-data` |
| `garden-code-server` (the gate) | [code-server/](../code-server/README.md) | `garden-code-server` | `default` | `code-server-sock` read-only |
| `garden-code-server-editor` | [code-server/editor/](../code-server/README.md) | `garden-code-server` | `code-exec` | `workspaces`, `code-server-data`, `code-exec-keys`, `code-server-sock` |
| `garden-code-server-exec` (the shell) | [code-server/exec/](../code-server/README.md) | `garden-code-server` | `code-exec`, `code-egress` | `workspaces`, `tools`, `code-exec-keys` read-only |
| `gotenberg` (project `lettuce_shared`, one per host) | [compose/shared/](../compose/README.md) | `garden-doc-tools` | its own internal network and each stack's `lettuce-doc-tools-<name>` | none |

`state/` is `stacks/<name>/state/` on the host. Every service runs with `no-new-privileges` and a memory cap ([compose/hardening.yml](../compose/hardening.yml)); every container in the app-server's namespace drops `NET_RAW`; the garden's services, the code editor and pipelock are read-only with every capability dropped. The code editor's shell is the exception: it is as strong as an agent, so it keeps Docker's default capabilities but `NET_RAW` and a writable root. [compose/README.md](../compose/README.md) describes each overlay, and `lettucectl check` refuses a stack that departs from this table (AGENTS.md, "Key invariants").

## Networks
| Network | Internal | Members | Made by |
|---|---|---|---|
| `default` (one per stack) | no | every stack service but the editor and the shell | Compose |
| `code-exec` | yes | the editor and the shell | Compose |
| `code-egress` | no | the shell: its only way out | Compose |
| `lettuce-doc-tools-<name>` | yes | the app-server and Gotenberg (alias `gotenberg`) | lettucectl (`sync_doc_tools`) |
| `lettuce_shared`'s `internal` | yes | Gotenberg | Compose, project `lettuce_shared` |

## Volumes
| Volume | Mounted by | Notes |
|---|---|---|
| `vault-data` | `garden-vault` | the encrypted vault; `backup` copies it read-only |
| `pipelock-data` | `garden-pipelock` | receipt signing key and receipts; `backup` and `receipts` read it read-only |
| `google-creds`, `google-policy` | `bff`, `google-mcp`, `garden-gmail-triggers` (read-only) | upstream's; never the app-server |
| `bff-data`, `bff-auth` | `bff` | upstream's |
| `code-server-data` | the editor | its settings and state |
| `code-exec-keys` | the editor (writes), the shell (read-only) | the SSH keys between them |
| `code-server-sock` | the editor, the gate (read-only) | the editor's Unix socket, its only listener |
| `lettuce-doc-tools-<tag>` | every app-server, read-only | one per host, filled by lettucectl; not a Compose volume |

Named volumes of a stack are deleted with it by `lettucectl remove` (`compose down -v`). The host paths under `stacks/<name>/state/` are kept unless `--purge`.

## Who calls whom
| From | To | How |
|---|---|---|
| The BFF | the app-server | the only connection to agents; nothing else opens one or starts a turn |
| The BFF | `searxng`, `ddg-mcp` | backends of the agents' web search and page fetch |
| Agents (app-server) | `google-mcp` | MCP over HTTP, as Settings -> Google Workspace allows |
| Agents | `garden-vault` | MCP at `http://garden-vault:8000/mcp`; `vault-run` at `/redeem` |
| Agents | `garden-pipelock` | `HTTPS_PROXY` (advisory) and `/fetch` for scanned pages |
| Agents | Gotenberg | the PDF MCP, started per call from the doc-tools volume |
| The garden executor (cron in the app-server) | `garden`, `garden-gmail-triggers` | `/executor/v1/` with `GARDEN_EXECUTOR_TOKEN`: the agent list, and Gmail triggers' requests, which it runs as `letta cron add --once` ([garden-executor.md](garden-executor.md)) |
| `garden-gmail-triggers` | Gmail | read-only, with the token Settings -> Google stores |
| The gate | the editor | the editor's Unix socket |
| The editor | the shell | SSH, for terminals and every git call |

What each part does, and why it is built the way it is, is in its folder's README and its guide under `docs/`.
