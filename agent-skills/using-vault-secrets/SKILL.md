---
name: using-vault-secrets
description: Load this BEFORE asking the user's vault (the `vault` MCP server) for anything. It shows how to use the user's private data (addresses, card numbers, passwords, ID numbers...) in a document, an email, a form or any command without the values entering the conversation, and how to wait for the user's approval so you don't miss it.
---

# Keep private values out of the conversation

Everything in your conversation is sent to the AI provider. The vault can put a value straight
into a file, a command or an MCP call instead, so you never read it. Before every
`request_secret`, ask yourself:

- **Do I only need to put the value somewhere?** (a letter, a form, an email, a login): use
  placeholders, the default. This is almost always the case.
- **Do I need to understand the value itself?** (decide something from it): first check the
  public details in `list_secrets` (the name, the country, a card's brand...). Only if they are
  not enough, ask with `delivery: "text"` and a `reason` the user reads before approving, for
  the fewest fields that answer the question.

Ask only for the fields the task needs: `fields: ["address1", "city", "postal_code"]`, not
the whole identity.

# Placeholders, step by step

1. **Prepare everything first, with placeholders.** Write `{{VAULT_<FIELD>}}` where each value
   goes: `{{VAULT_ADDRESS1}}`, `{{VAULT_POSTAL_CODE}}`, `{{VAULT_NUMBER}}`. A script reads
   them from its environment instead (`process.env.VAULT_NUMBER`, `os.environ["VAULT_CODE"]`).
   Check the template or script now: you can't look at the result later.
2. **Request the secret** with a clear purpose. If the answer is `pending`, wait for the user
   (below). Once approved, `get_result` answers with a `handle` and the exact `placeholders`.
3. **Fill at the last step, within 5 minutes.** The handle works once. Run one of the recipes
   below. In them, `V=/root/.letta/skills/using-vault-secrets`.

## A document

```bash
python3 $V/vault-run <handle> -- python3 $V/vault-fill letter.tmpl.docx letter.docx
```

`vault-fill` fills `.txt`, `.md`, `.csv`, `.json`, `.html`, `.xml`, `.eml` and Office or
OpenDocument files (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp`), escaping each value for
the format. For a PDF, fill the source (a `.docx` or `.html` template) and then convert the
filled copy. It refuses to write anything when a placeholder has no value, or when Word split a
placeholder across formatting: retype it in one go, with no formatting change inside it.

## An email, or any other MCP tool

Write the tool's arguments as a JSON template, with the placeholders inside its strings, then
fill and send them in one command. The pipe must be inside `bash -c '...'`: `vault-run` replaces
the values in its own output, so a pipe outside it would send `[VAULT_CITY]` instead of the city.
`pipefail` stops a failed fill from being reported as sent.

```bash
python3 $V/vault-run <handle> -- bash -c 'set -o pipefail
  V=/root/.letta/skills/using-vault-secrets
  out=$(python3 $V/vault-fill send.tmpl.json - \
    | sh /root/.letta/skills/mcp-servers/scripts/mcp.sh call <send-tool> --args-file -) \
    && echo sent || printf "failed: %s\n" "$out"'
```

It prints `sent`, or the tool's error (with long values hidden). The handle is used up either
way: to try again, request the secret again.

## A form, a login, any command

```bash
python3 $V/vault-run <handle> -- node /work/$AGENT_ID/checkout/fill.js
python3 $V/vault-run <handle> -- sh -c 'curl -fsS -u "me:$VAULT_PASSWORD" https://api.example.com/orders'
```

Put a value in an argument only inside `sh -c '...'`, in single quotes: otherwise your shell
expands `$VAULT_PASSWORD` to nothing before `vault-run` sets it.

## Several secrets in one step

A letter needs the sender's and the recipient's address, whose fields have the same names.
Request each with its own `alias` (`"alias": "home"` gives `{{VAULT_HOME_CITY}}`,
`"alias": "client"` gives `{{VAULT_CLIENT_CITY}}`), then give `vault-run` both handles:

```bash
python3 $V/vault-run <handle1> <handle2> -- python3 $V/vault-fill letter.tmpl.docx letter.docx
```

## Rules

- **Never open, print or convert-and-read the filled file**, and never read a command's output
  that could echo the values. Check the template instead, and tell the user where the filled
  file is so they can check it.
- **Make commands print only a status.** `vault-run` hides values of 6 characters or more in
  the output, as `[VAULT_NUMBER]`, but not shorter ones (a postal code, a house number, a
  security code).
- Run the command in the foreground. Output from anything it leaves running in the background
  is lost when `vault-run` exits.
- Use the values only for the purpose the user approved. Never save, log or send them anywhere
  else.
- "unknown or expired handle": the handle was already used or is older than 5 minutes. Ask for
  that secret again; with several handles, none was used up, so the others still work.
- "would come from two secrets" or "already set": two handles give the same names, or a
  `VAULT_` variable is already set. Nothing was used up: request one secret again with an
  `alias`, or unset the variable.

## Values you asked to read (`delivery: "text"`)

They are in your conversation, so follow the rules that come with them: never repeat them in
your reply, memory or files (refer to the secret by its name, and when asked to check one, say
what you checked and the result), and send them only where the approved purpose needs them.

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
