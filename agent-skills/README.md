# agent-skills

Skills this repo gives every agent of every stack, one directory per skill with a `SKILL.md`. `lettucectl skills` installs them into each stack's global skills, which every agent of that stack loads. They have no compose profile of their own; each one says when it applies, and does nothing on a stack without its feature. [docs/operations.md](../docs/operations.md#agent-skills) covers installing them.

![How agent skills are installed and what each one talks to](../docs/diagrams/agent-skills-architecture.svg)

An interactive version, [agent-skills-architecture.html](../docs/diagrams/agent-skills-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `using-vault-secrets/SKILL.md` | How to wait for the person's approval of a vault request (`get_result` as a background command through the `mcp-servers` wrapper), and how to use opaque values in a command without reading them (`delivery: "env"`). Applies with the `garden-vault` profile. [docs/vault.md](../docs/vault.md) |
| `using-vault-secrets/vault-run` | The vault's helper (Python, standard library only): redeems a single-use handle at `http://garden-vault:8000/redeem` with no proxy, runs one command with the values as `VAULT_<FIELD>` variables, and scrubs them from its output. |
| `fetching-web-pages/SKILL.md` | Read untrusted pages through pipelock's `http://garden-pipelock:8888/fetch`, so the URL and the page text are checked, and report a block instead of retrying another way. Applies only when `HTTPS_PROXY` is `http://garden-pipelock:8888` (the `garden-pipelock` profile). [docs/egress-firewall.md](../docs/egress-firewall.md) |
| `working-with-documents/SKILL.md` | Which of the `office` and `pdf` MCP servers does what, how to call them through the `mcp-servers` wrapper from the agent's workspace, and how to check a result before reporting it. Applies when Settings -> MCP lists them (the `garden-doc-tools` profile). [docs/doc-tools.md](../docs/doc-tools.md) |

Only directories that contain a `SKILL.md` are shipped (`agent-skills/*/SKILL.md`), so this README is never installed.

## How it plugs in
- **`lettucectl skills <name|all>`** (`cmd_skills`, `sync_skills`): takes each `*/SKILL.md` directory, refuses a name that is not lowercase letters, digits and `-`, and refuses a name upstream Lettuce also ships in `upstream/docker/agent-skills`, since the BFF rewrites its own skills on every connect. It streams a tar of the skills over `compose exec -T app-server` to the in-container script `SKILLS_INSTALL`, which installs them into `/root/.letta/skills` (`state/letta-home/skills` on the host).
- **The manifest:** `SKILLS_INSTALL` unpacks into a dot directory first, so a broken stream changes nothing; then it removes the skills listed in `.lettucectl-skills`, moves the new ones in and rewrites the manifest. Upstream's skills and those enabled in Settings stay, and a skill deleted from this directory is removed from the stack on the next run.
- **Not automatic:** `up` and `add` do not install skills; run `skills` after setting up a stack or changing a skill. The stack must be up.
- **`backup`** leaves out the skills listed in the manifest and upstream's, since `skills` and the BFF reinstall them.
- **What each skill talks to:** `using-vault-secrets` calls the vault's MCP server through the `mcp-servers` wrapper and `vault-run` posts to its `/redeem`; `fetching-web-pages` sends `curl` to pipelock's `/fetch`; `working-with-documents` calls `office` and `pdf` through the wrapper (`letta mcp call`). None of them needs a tunnel route, a network or a volume of its own.
- No `check_stack` rule is specific to skills; the features they use have theirs (see [Key invariants](../AGENTS.md#key-invariants)).

Skills are instructions, not boundaries: agents are root in the app-server and can ignore them. Skill text is public and lands in every operator's stacks: keep it generic, with no names, domains or values of a real deployment.

## Build and test
```bash
tests/run.sh test_skills_                   # sync_skills and SKILLS_INSTALL (stubbed docker)
tests/run-sidecar.sh vault -k vault_run     # vault-run against a loopback /redeem (mounts agent-skills/)
```
There is no build step: `lettucectl skills all` pushes a changed skill to the running stacks.

## Upstream seams
Named in [AGENTS.md](../AGENTS.md):
- the global skills directory `/root/.letta/skills`, and the BFF rewriting upstream's `docker/agent-skills` there on every connect (why a shared name is refused);
- the `mcp-servers` skill's wrapper, `/root/.letta/skills/mcp-servers/scripts/mcp.sh`, named in `working-with-documents` and `using-vault-secrets`;
- letta-code telling the agent when a background command finishes, which is how an agent waiting for a vault approval gets a new turn (`using-vault-secrets`).
