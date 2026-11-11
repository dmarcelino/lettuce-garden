# Outlook

Each stack can connect **one Outlook mailbox** for its agents: mail, calendar and
tasks, plus read-only access to shared mailboxes. The person connects their own
signed-in Outlook web session with a bookmarklet, and decides on a web page exactly
how far agents may go — read only, organise, draft, send — and who they may send to.
It is opt-in, behind the `garden-outlook` profile.

Unlike the Google Workspace integration, this needs **no app registration and no
admin consent**. It calls **Microsoft Graph** (the current, supported API), but it
gets its access token by reusing the sign-in your browser already has: the
bookmarklet hands over the refresh token the Outlook web app holds, and the sidecar
exchanges it for a Graph token under one of Microsoft's own apps (this is called
FOCI). Because that reuses an existing first-party sign-in, no admin has to approve
anything — but a strict tenant can still refuse the exchange (see the security
model), and where a normal Graph app registration is available, prefer it.

![You connect Outlook with a bookmarklet and set the limits; an agent's tool call reaches Microsoft only as far as they allow](diagrams/outlook-flow.svg)

An interactive version, [outlook-flow.html](diagrams/outlook-flow.html), opens in a browser from a clone of this repo.

How it is built and wired, for developers: [outlook/README.md](../outlook/README.md).

## Using it

1. **Open `https://<your garden>/outlook/`** and sign in as usual; `<your garden>`
   is your stack's garden address, `<name>-garden.<domain>`.
2. **Add the bookmarklet.** Drag the **Connect Outlook** link on that page to your
   browser's bookmarks bar. Expand "What the bookmarklet does" to read it first — it
   reads the sign-in your browser already holds for Outlook and sends it to your
   stack; it does not type your password and sends nothing anywhere else.
3. **Connect.** Open `https://outlook.office.com/mail/`, wait for your mail to load,
   then click the **Connect Outlook** bookmark. A new tab opens on your Outlook page
   and connects the session. You only reconnect when your Outlook session really
   ends (days, usually), not every hour.
4. **Choose what agents may do.** On the Outlook page, set a level per service and
   save:

   | Service | Levels (least to most) |
   |---|---|
   | Mail | off · readonly · organize (move, flag, categories) · drafts · send · full (mailbox settings) |
   | Calendar | off · readonly · **self-events** (block your own time, no invitations) · full (invitations, RSVPs) |
   | Tasks | off · readonly · manage |

   - **self-events** lets an agent create or change events on *your* calendar with
     no attendees, so it can hold time but cannot invite anyone. Inviting needs
     **full**.
   - **Allowed recipients:** one address or `@domain` per line. Agents may only send
     to, or invite, these (your own address is always allowed). Leave empty for no
     restriction.
   - **Shared mailboxes:** one address per line. Agents may *read* these mailboxes
     and never write to them.

To stop, **Disconnect** on the page, or turn the `garden-outlook` profile off.

## Running it
- **Turn it on:**
  ```bash
  lettucectl profiles <current list>,garden-outlook      # e.g. cloudflared,search,garden-outlook
  ```
  This routes `https://<garden>/outlook/` (and the garden's home page) to the Outlook service on every stack, on the stack's garden address and its own Access application, then starts it. Each stack gets its own `OUTLOOK_SECRET` in `stack.env`, once: the key that encrypts the stored session. There is no Microsoft-side setup and no redirect URI to register. Dropping `garden-outlook` from the list removes the route and the container, and leaves the data where it is.
- **Register it with the agents:**
  ```bash
  lettucectl mcp all      # adds outlook (and the other garden servers that are on) to each Settings -> MCP
  ```
  `add` does this for a new stack, and `profiles` prints, for each stack, whether it is registered or the entry to add by hand. `mcp` adds `{"name": "outlook", "transport": "http", "url": "http://garden-outlook:8000/mcp"}` once per stack (`OUTLOOK_MCP_REGISTERED` in `stack.env`), so a person who removes it keeps it removed; `mcp <name> --force` adds it again. An HTTP entry with that exact URL counts as Outlook whatever its name; a different server named `outlook` is left alone, and nothing is added (it says so). It then restarts the stack's BFF alone so agents see it. With `garden-outlook` dropped from the list, `up` and `profiles` remove the entry they registered; if that fails, the next `up` or `profiles` retries it.
- **Data:** the sealed session and the policy live in the Docker volume `lettuce-<name>_outlook-data`, not under `stacks/<name>/`. `lettucectl backup` deliberately does **not** copy it: reconnecting with the bookmarklet is cheap, and a backup should not carry a live mail credential. After a restore, the person reconnects and sets the policy again.
- **Removal:** `lettucectl remove <name>` deletes the volume, so removing a stack deletes its Outlook session and policy.
- **Isolation:** the Outlook container has a read-only root filesystem and no capabilities, and no host mounts. Its volume is mounted by nothing else. With `garden-pipelock`, agents' calls to it bypass the proxy (`NO_PROXY`), and the service itself calls Microsoft directly, so pipelock never sees the token or the mail. `lettucectl check` refuses a stack where any of that is not true.

## Security model
What the Outlook connection protects, and what it does not.


- **The token stays in the sidecar.** Agents get MCP tools only; the bearer token
  and refresh material never reach them. They are sealed with `OUTLOOK_SECRET`
  (held only in the sidecar's environment, never on the data volume) on a volume no
  other service mounts — `lettucectl check` refuses a stack where one does.
- **The level and recipient list are the boundary, and they are real.** Every tool
  checks the policy before acting, in the sidecar, so an agent calling the endpoint
  directly cannot exceed its level or send to an off-list address. There is no
  in-app approval step; the level and the allowed-recipients list are the whole
  control.
- **This guards the account's identity, not your data.** The allowed-recipients list
  limits who the account mails or invites. It does not stop an agent reading what the
  level allows, and agent shells have their own network access. Email, event and task
  text is untrusted input: tools label it so, but a model can still be misled by it.
- **Supported API, unsupported way in.** The data calls use Microsoft Graph, which is
  current and documented. What is unsupported is *how the token is obtained* —
  reusing the browser's first-party refresh token (FOCI) instead of a registered app.
  That is deliberate (it needs no admin), but it is a grey-area use of Microsoft's own
  apps: a tenant can refuse it with Conditional Access, token-protection or
  first-party-app restrictions. If it is refused, the `/outlook/` page says so when
  you connect. Device-based sign-in policies can also mean a refresh from the server
  is refused and you reconnect more often, or that it does not work at all.
- **Not for accounts with a supported path.** Where a normal Graph app registration is
  available, use it instead.
