# Egress proxy (pipelock)

The `garden-pipelock` profile gives each stack its own [pipelock](https://github.com/pipelab-org/pipelock),
an egress proxy for AI agents. The agents' tools send their web traffic through it. It checks
where each request goes, blocks some destinations, and writes a **signed receipt** for every
decision. It is opt-in and **advisory**: it catches mistakes and naive attempts, and records
what went where, but an agent that wants to can go around it. Read "What it does not do" before
relying on it.

![What goes through pipelock, what it records, and what goes around it](diagrams/egress-proxy-flow.svg)

An interactive version, [egress-proxy-flow.html](diagrams/egress-proxy-flow.html), opens in a browser from a clone of this repo.

How pipelock is built and wired, for developers: [pipelock/README.md](../pipelock/README.md).

## What it does
- **Proxy settings** (`compose/pipelock-proxy.yml`): `HTTPS_PROXY`/`HTTP_PROXY` in the
  app-server (letta-code itself, with its calls to the model provider, and agent shells, Claude
  Code and Codex subagents, `npm`, `pip`, `git`, `curl`...), in SearXNG and in the DuckDuckGo
  reader. Traffic inside the stack (the vault, Gmail triggers, News Radar, the search sidecars,
  localhost, `host.docker.internal`) stays direct: pipelock must never see the vault's values, and
  `lettucectl check` refuses a stack where it would.
- **Destination checks** (balanced mode, `pipelock/pipelock.yaml`):
  - a short built-in blocklist of paste and file-drop sites (pastebin, transfer.sh, ...);
  - private, internal and cloud-metadata addresses (SSRF): the LAN, the host, `169.254.169.254`.
    This covers letta-code's model calls too: a model server elsewhere on your network is
    refused unless you list it in `PIPELOCK_DIRECT` (see "A model server on your network");
    one on the Docker host, reached as `host.docker.internal`, stays direct;
  - data hidden in host names (long random-looking subdomains);
  - at most 1200 requests per minute to one site (each plain-HTTP request and each HTTPS
    connection counts, letta-code's model calls included), which only stops runaway loops.
- **Page checks, for pages read through `/fetch`.** Agents that follow the
  `fetching-web-pages` skill read pages through `http://garden-pipelock:8888/fetch?url=...`. Pipelock
  fetches the page itself, so it checks the whole URL for secrets and the page's text for
  prompt injection, and returns the text as JSON. A page with prompt injection is **blocked**
  (pipelock's balanced mode would only log a warning and hand the agent the page): the agent
  gets a 403 with `"block_reason": "response contains prompt injection: ..."` and a block
  receipt is signed. A page that only discusses prompt injection can be refused too; the agent
  tells you, and you decide.
- **Signed receipts.** Every allow and block is written to a hash-chained log, each entry signed
  with the stack's own Ed25519 key. They show where the agents' tools went, and that this
  pipelock decided it.

## What it does not do
- **It is not a boundary.** The stack network still reaches the internet directly, and any agent
  can unset the variable or use a tool that ignores it. The BFF, the vault, google-mcp, Gmail
  triggers and News Radar connect directly by design; so does what the BFF does for agents,
  such as its Remote Pi runs over SSH, which carry the agent's instructions. With the `telegram`
  profile, the Telegram gateway connects directly too, including the packages it installs at
  every start. Hosts in `PIPELOCK_DIRECT` are reached directly.
- **No content checks on HTTPS.** There is no TLS interception, so for normal HTTPS requests
  pipelock sees only the host name: not the path, the request or the response. Secrets in a
  request body, or prompt injection in a page fetched with `curl`, pass unseen. Only `/fetch`
  sees content.
- **Blocklists don't stop a determined leak.** Any site that is not on the list is allowed, and
  an attacker can use a new one. Plain personal data (an address, an email's text) sent to such
  a site is not detected.
- **Prompt-injection checks are pattern matching.** They catch common phrasings, not text
  written to evade them. Treat every page as data either way.
- **Request bodies are only warned about.** Pipelock's check for secrets in a plain-HTTP request
  body only writes a warning to its log (and it never sees HTTPS bodies). Secrets in a URL are
  blocked.
- **Receipts are signed by the operator's key.** They show what this pipelock decided and that
  it was this key; they don't prove the operator honest, nor that nothing went around the proxy.

## Running it

### Turning it on
```bash
lettucectl profiles cloudflared,search,garden-pipelock   # your current list plus garden-pipelock
lettucectl skills all                             # installs the fetching-web-pages skill
```
`up` builds the image (`pipelock/Dockerfile`) from pipelock's source at a pinned release,
without its separately licensed enterprise code. The first start creates the stack's receipt
signing key. Dropping the profile removes the container and the proxy settings, and keeps the
receipts.

### Reading the receipts
```bash
lettucectl receipts alfred                        # verify every receipt, list the sessions
lettucectl receipts alfred ~/alfred-receipts.html # HTML report of the newest session
lettucectl receipts alfred ~/r.html proxy.run.... # ... or of a listed session
```
Pipelock refuses to verify a file that changes while it reads it, which a running pipelock
does with every request, so `receipts` tries three times. If it still fails while pipelock is
running, retry when the stack is quiet, or after `lettucectl down alfred`.

Each start of pipelock is a session. The report shows each decision with its time, destination
and reason, and checks the signatures against the stack's public key. Both run in a one-off
container with no network and the receipts mounted read-only. Reports name every site the
agents reached: keep them private (they are written mode 600, and never inside this repo).

Expect some noise: each SearXNG search first tries HTTP/3 through the proxy, which pipelock
refuses (an SSRF block on `pipelock` itself) before the search goes through normally.

The signing key and the receipts live on the volume `lettuce-<name>_pipelock-data`, which only
pipelock mounts. Receipts are never pruned (removing one would break the chain); they grow by
one line of about 1.5 KB per request, model calls included, so a busy stack adds several MB a
day. `backup` copies them, and `remove` deletes them with the stack. To start over, take a
`backup`, then remove the volume with the stack down: pipelock makes a new signing key and
chain at its next start, and the old receipts verify only from the backup.

### A model server on your network
Pipelock refuses private addresses, and letta-code's model calls go through it, so a model
server elsewhere on your LAN (Ollama, vLLM, a LiteLLM proxy) is refused while the profile is on.
List it in `PIPELOCK_DIRECT`, comma-separated host names or IP addresses, each with an optional
port, or `.domain` suffixes:
```bash
# config.env: for every stack
PIPELOCK_DIRECT=models.lan,10.0.0.5:11434
# stacks/alfred/stack.env: added to the list above, for alfred only
PIPELOCK_DIRECT=gpu-box.lan
```
Then run `lettucectl up all` (or `up alfred`). lettucectl appends the list to `NO_PROXY`, so
those hosts are reached directly, without checks or receipts, by letta-code and by every agent
tool in that stack. `*`, URLs and address ranges are refused. A model server on the Docker
host needs none of this: point Lettuce at `host.docker.internal`.

With the profile on, Node processes in the app-server also print `[UNDICI-EHPA] Warning:
EnvHttpProxyAgent is experimental` on stderr, so agents sometimes see it in a tool's output. It
is harmless: Node says its proxy support may change, not that something failed.

### When an agent is blocked
The agent gets a 403 with an `X-Pipelock-Block-Reason` header (or `blocked: true` and a
`block_reason` from `/fetch`).
The skill tells agents to report it rather than try another way. To see why, read the receipts
or `lettucectl logs alfred garden-pipelock`. The policy is the same for every stack and lives in
`pipelock/pipelock.yaml`; after changing it, `lettucectl up all` rebuilds the image.
