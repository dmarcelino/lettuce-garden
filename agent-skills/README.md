# agent-skills

Skills this repo gives every agent of every stack, one directory per skill with a `SKILL.md`. `lettucectl skills` installs them into each stack's global skills, which every agent of that stack loads. They have no compose profile of their own; each one says when it applies, and does nothing on a stack without its feature. [docs/operations.md](../docs/operations.md#agent-skills) covers installing them.

![How agent skills are installed and what each one talks to](../docs/diagrams/agent-skills-architecture.svg)

An interactive version, [agent-skills-architecture.html](../docs/diagrams/agent-skills-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `using-vault-secrets/SKILL.md` | How to keep vault values out of the conversation: placeholders by default, filled at the last step into a document, an MCP call's arguments (an email, through the `mcp-servers` wrapper's `--args-file -`) or a command; `delivery: "text"` only to reason about a value, with a reason; and how to wait for the person's approval (`get_result` as a background command through the wrapper). Applies with the `garden-vault` profile. [docs/vault.md](../docs/vault.md) |
| `using-vault-secrets/vault-run` | The vault's helper (Python, standard library only): redeems one or more single-use handles in one call to `http://garden-vault:8000/redeem` (`GARDEN_VAULT_URL` overrides it; never a `VAULT_` name, which belongs to the values) with no proxy, all or none: the vault uses up no handle when one is unknown or a name clashes, with another handle's or with a `VAULT_` variable already set (sent as `taken`). It then runs one command with the values as `VAULT_<FIELD>` variables and scrubs them from its output. |
| `using-vault-secrets/vault-fill` | Run under `vault-run` (Python, standard library only): copies a template with each `{{VAULT_<NAME>}}` replaced by that variable, escaped for the format (JSON, HTML/XML, the XML inside Office and OpenDocument zips, text as is), to a file (0600, temp then rename, never over the template) or standard output; writes nothing when a placeholder has no value or is split by formatting, and prints no value. A `.csv` is filled cell by cell and written back with CSV quoting. |
| `fetching-web-pages/SKILL.md` | Read untrusted pages through pipelock's `http://garden-pipelock:8888/fetch`, so the URL and the page text are checked. On any pipelock 403 (curl, Python or `/fetch`), find the reason through `/fetch`, retry a temporary DNS failure once, and otherwise report the block: never retry another way or make a request skip the proxy, in scheduled jobs too. Applies only when `HTTPS_PROXY` is `http://garden-pipelock:8888` (the `garden-pipelock` profile). [docs/egress-firewall.md](../docs/egress-firewall.md) |
| `watchlist-sources/SKILL.md` | How to set up a monitor (find the source, prefer feeds, schema.org data and JSON over HTML, `source_inspect`, `source_test` before `source_put`, a schedule in the stack's timezone, never a `letta cron` task), follow news on a topic, and write and repair from a report. Applies when Settings -> MCP lists `watchlist` (the `garden-watchlist` profile). [docs/watchlist.md](../docs/watchlist.md) |
| `working-with-documents/SKILL.md` | Which of the `office` and `pdf` MCP servers does what, how to call them through the `mcp-servers` wrapper from the agent's workspace, and how to check a result before reporting it. Applies when Settings -> MCP lists them (the `garden-doc-tools` profile). [docs/doc-tools.md](../docs/doc-tools.md) |

Only directories that contain a `SKILL.md` are shipped (`agent-skills/*/SKILL.md`), so this README is never installed.

## How it plugs in
- **`lettucectl skills <name|all>`** (`cmd_skills`, `sync_skills`): takes each `*/SKILL.md` directory, refuses a name that is not lowercase letters, digits and `-`, and refuses a name upstream Lettuce also ships in `upstream/docker/agent-skills`, since the BFF rewrites its own skills on every connect. It streams a tar of the skills over `compose exec -T app-server` to the in-container script `SKILLS_INSTALL`, which installs them into `/root/.letta/skills` (`state/letta-home/skills` on the host).
- **The manifest:** `SKILLS_INSTALL` unpacks into a dot directory first, so a broken stream changes nothing; then it removes the skills listed in `.lettucectl-skills`, moves the new ones in and rewrites the manifest. Upstream's skills and those enabled in Settings stay, and a skill deleted from this directory is removed from the stack on the next run.
- **Not automatic:** `up` and `add` do not install skills; run `skills` after setting up a stack or changing a skill. The stack must be up.
- **`backup`** leaves out the skills listed in the manifest and upstream's, since `skills` and the BFF reinstall them.
- **What each skill talks to:** `using-vault-secrets` calls the vault's MCP server through the `mcp-servers` wrapper, `vault-run` posts to its `/redeem`, and its email recipe pipes `vault-fill` into `mcp.sh call ... --args-file -`; `fetching-web-pages` sends `curl` to pipelock's `/fetch`; `working-with-documents` calls `office` and `pdf` through the wrapper (`letta mcp call`); `watchlist-sources` calls the watchlist's MCP server through the wrapper. None of them needs a tunnel route, a network or a volume of its own.
- No `check_stack` rule is specific to skills; the features they use have theirs ([CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces)).

## Rules
- **Only `skills` installs agent skills, and it replaces only its own.** `SKILLS_INSTALL` removes what its manifest (`.lettucectl-skills`) lists and leaves upstream's and Settings' skills alone. `up` does not sync skills.
- **Names must not match upstream's `docker/agent-skills`.** The BFF rewrites those on every connect, so a shared name would be overwritten. `skills` refuses one.
- **Skills are instructions, not boundaries.** Agents are root in the app-server and can ignore them; docs never present a skill as a protection.
- **Skill text is public** and lands in every operator's stacks: keep it generic, with no names, domains or values of a real deployment.

## Build and test
```bash
tests/run.sh test_skills_                   # sync_skills and SKILLS_INSTALL (stubbed docker)
tests/run-sidecar.sh vault -k "vault_run or vault_fill"   # vault-run against a loopback /redeem, and vault-fill (mounts agent-skills/)
```
There is no build step: `lettucectl skills all` pushes a changed skill to the running stacks.

## Upstream seams
Listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams):
- the global skills directory `/root/.letta/skills`, and the BFF rewriting upstream's `docker/agent-skills` there on every connect (why a shared name is refused);
- the `mcp-servers` skill's wrapper, `/root/.letta/skills/mcp-servers/scripts/mcp.sh`, named in `working-with-documents`, `using-vault-secrets` and `watchlist-sources`;
- letta-code telling the agent when a background command finishes, which is how an agent waiting for a vault approval gets a new turn (`using-vault-secrets`);
- `letta mcp call <tool> --args-file -` reading the arguments from stdin and exiting 2 when the tool reports an error (the skill's email recipe pipes `vault-fill` into it, under `bash` for `pipefail`).
