---
name: using-vault-secrets
description: Load this BEFORE asking the user's vault (the `vault` MCP server) for anything. It shows how to wait for the user's approval so you don't miss it, and how to use a password, card number, security code or document number in a script or shell command (filling a web form with Playwright, logging in with curl) without the values entering the conversation.
---

# Waiting for the user's approval

When `request_secret` answers `pending`, the user has to approve the request first. Nothing
starts a new turn for you when they do: if your turn ends with nothing waiting, you never
collect what they approved.

1. Post the `approve_url` to the user in your reply (they may not get a notification).
2. Call `get_result` with the request's `id` and `wait_seconds` 50, as its own background
   command, so you are told when it finishes:

```bash
sh /root/.letta/skills/mcp-servers/scripts/mcp.sh call mcp__vault__get_result --args '{"request_id":"<id>","wait_seconds":50}'
```

3. While it answers `pending`, start the same command again, one call per command (a loop in
   one command runs into the command's time limit). Stop at `done`, `denied` or `expired`; a
   request expires after 10 minutes.

If you can't run commands in the background, make the same calls in the foreground before you
end your turn.

# Using vault secrets without reading them

When the `vault` MCP server's `request_secret` is called with `delivery: "env"`, you get a
**handle** instead of the values. `vault-run` trades the handle for the values and gives them
to one command as environment variables, then removes them from that command's output. The
values never appear in your conversation, so the AI provider never sees them and they are not
stored with the chat.

Use it for opaque values that a program types in for you: passwords, card numbers, security
codes, document numbers. For free text you need to read (an address, company details, notes),
use the default `delivery: "text"`. Those values are in your conversation, so follow the rules
that come with them: never repeat them in your reply, memory or files (refer to the secret by
its name, and when asked to check one, say what you checked and the result), and send them
only where the approved purpose needs them.

## Steps

1. **Write and test the script first**, with placeholder values. It must read the values from
   its environment, for example `process.env.VAULT_NUMBER` in Node or
   `os.environ["VAULT_PASSWORD"]` in Python.
2. **Request the secret** with `delivery: "env"` and a clear purpose. If the answer is pending,
   wait for the user's approval as above. Once the user approves, `get_result` answers with a
   `handle`, the variable names in `env` (one
   `VAULT_<FIELD>` per field, e.g. `VAULT_NUMBER`, `VAULT_SECURITY_CODE`, `VAULT_PASSWORD`) and a
   `run` example.
3. **Run it within 5 minutes.** The handle works once:

```bash
python3 /root/.letta/skills/using-vault-secrets/vault-run <handle> -- node /work/$AGENT_ID/checkout/fill.js
```

To use a value in an argument, put the whole command in single quotes behind `sh -c`. Without
the quotes your shell expands `$VAULT_PASSWORD` to nothing before `vault-run` sets it:

```bash
python3 /root/.letta/skills/using-vault-secrets/vault-run <handle> -- sh -c 'curl -fsS -u "me:$VAULT_PASSWORD" https://api.example.com/orders'
```

## Rules

- Run the command in the foreground. Output from anything it leaves running in the background
  is lost when `vault-run` exits.
- In the output, a value shows as `[VAULT_NUMBER]` and so on. That means it was used, not that
  it is missing. Short values such as a 3-digit security code are not hidden, so don't print them.
- Use the values only for the purpose the user approved. Never print, save, log or send them
  anywhere else, and never write them into files, memory or messages.
- "unknown or expired handle": the handle was already used or is older than 5 minutes. Ask for
  the secret again.
- One handle per `vault-run`. To use two secrets in one command, nest them:
  `python3 $V <h1> -- python3 $V <h2> -- <command>`, with
  `V=/root/.letta/skills/using-vault-secrets/vault-run`. Two secrets with the same field (two
  cards) cannot be combined this way.
