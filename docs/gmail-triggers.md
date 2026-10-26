# Gmail triggers

Each stack can have Gmail triggers: rules like "when an email arrives in the *Invoices* label, ask my agent to file it". The person sets them up on a web page, with no technical knowledge needed. Checking Gmail costs nothing; an agent runs only when a rule matches. It is opt-in, behind the `gmail-triggers` profile, and needs the `google` profile with the person's Google account connected in Lettuce.

```
gmail-triggers ──every 2 minutes──────────────> Gmail API     what arrived, metadata only
                on a match: a pending request (agent, prompt, message ids) in its own database
executor (in the app-server, every minute) ──GET /executor/v1/requests──> gmail-triggers
executor ──letta cron add --once──> Lettuce's scheduler ──"Gmail: <rule>"──> the agent, which reads the mail itself
executor ──results, agent list, heartbeat──> gmail-triggers
```

An interactive diagram of the containers and boundaries, [gmail-triggers-architecture.html](diagrams/gmail-triggers-architecture.html), opens in a browser from a clone of this repo.

## For the person using it
1. **Connect Google in Lettuce first:** Settings -> Google, with Gmail read access at least.
2. **Open `https://<your stack>/garden/gmail-triggers/`** and sign in as usual. `https://<your stack>/garden/` links to it too.
3. **Add a trigger:** press **New trigger** and fill in:
   - **When mail arrives in**: one or more Gmail labels. A Gmail filter that puts the mail you care about in its own label works best: a label Gmail adds as the mail arrives counts, one you add by hand later doesn't.
   - **Only from**: optionally, the senders allowed to set it off, one address per line. Leave **Only if Gmail verified the sender** on: addresses are easy to fake.
   - **Ask**: the agent.
   - **In**: a new conversation each time, or the agent's main conversation.
   - **To**: what the agent should do. The agent is told which emails matched and reads them itself. Your instructions are wrapped in a note, before and after, that the agent may act only on those emails: it may read other mail for context, but not reply to it, change it or act on it. A trigger can't widen that, so a sweep of the whole inbox belongs in a scheduled task of its own.
   - **At most**: how many times per hour it may run (1 to 60). A run takes up to 30 emails; more than that, or mail over the limit, waits and goes in the next allowed run, oldest first. Up to 500 emails can wait per trigger; newer matches beyond that are dropped, and the page says so. A trigger that keeps hitting this needs more runs per hour or narrower labels or senders. Turning a trigger off or deleting it drops what was waiting.
4. **Try it:** **Send a test** checks that the trigger reaches the agent: it gets your instructions with no email and is told to reply with one fixed line and touch no mail. Tests don't count toward the hourly limit.
5. **See what happened:** each trigger's **History** lists the emails that matched and whether the agent was started: *Waiting for Lettuce*, *Sent to Lettuce*, the task created, an error, or *Lost* (Lettuce took the request but hasn't answered within 10 minutes; it is never handed out again, so an agent never runs twice for the same mail, and a late answer still replaces *Lost*). The runs also show in Lettuce's **Tasks** tab as "Gmail: <trigger name>".
6. **Mail that isn't checked:** old mail is never replayed. When Google was switched off or disconnected for more than 30 minutes, checking starts again from now; when Gmail's change history has expired, likewise; and when more than 200 new messages arrive at once, the rest are skipped. The page says so each time. A Gmail rate limit or a network error is not a pause: the next check catches up.

Each person can have up to 50 triggers. A matching email reaches the agent within about five minutes: Gmail is checked every 2 minutes, the executor picks requests up once a minute, and Lettuce starts the task at the next minute.

The agent never gets the email itself from the trigger, only its id, sender and subject, and it reads the mail through Lettuce's Google tools, under the permissions in Settings -> Google. Your own sent mail never sets off a trigger, so an agent's reply can't start itself again, and neither does mail in Spam or Trash, whatever labels it carries.

## For the operator
```bash
./lettucectl profiles cloudflared,google,gmail-triggers   # plus whatever else you run
```
- The profile needs `google`; `check` refuses it otherwise. It adds no Google redirect URI: it reuses the account each person connected in Settings -> Google.
- `profiles` routes `https://<stack>/garden/gmail-triggers/` to the sidecar on every stack, using the same hostname and Access app, and `up` installs the executor in the app-server. Dropping the profile removes the route, the container and the executor; the rules stay in the stack's state.
- Rules and pending requests live in `stacks/<name>/state/garden/gmail-triggers/` and are included in `backup`. Each stack gets a `GMAIL_TRIGGERS_EXECUTOR_TOKEN` in `stack.env`, generated once; `up` installs it for the executor.
- Logs: `./lettucectl logs <name> gmail-triggers`; the executor writes `/root/.letta/garden/gmail-triggers/executor.log` in the app-server (`./lettucectl shell <name> <agent>` can read it).

## Security model
- **Every page needs Cloudflare Access.** The sidecar checks the Access JWT on every request and CSRF on every form, like the vault, because agents can reach it over the stack network. `/garden/gmail-triggers/` is a route that bypasses the BFF; rules must live where agents can't edit them.
- **Agents can't change triggers.** Rules are in `state/garden/`, which the app-server does not mount.
- **No second way into the agents.** The sidecar never connects to the app-server. An executor installed in the app-server fetches its pending requests over the stack network (`/executor/v1/`, which the tunnel never routes) and turns each into a one-off scheduled task with letta-code's own `letta cron add`. Lettuce's scheduler then starts the turn on the BFF's connection, the only one. This is the one written exception to "no second way into an agent" in AGENTS.md: it gives agents nothing new, since they can already create scheduled tasks themselves.
- **Agents can see what the executor sees.** They are root in the app-server, so they can read the executor's token and use the API like it: read pending requests (the trigger's prompt, message ids, senders and subjects), claim or drop them, or report false results. They can't create requests, and running one does no more than they could already do with `letta cron add`. The token keeps out the stack's other containers. Each side treats what the other sends as untrusted, and a request is never handed out twice.
- **No directory is shared with the app-server.** A shared directory would have to sit where agents can write, and they could swap it for a symlink that Docker follows on the host. The sidecar's only bind mount is its own rules directory, and `check` refuses a symlink on that path.
- **The Google token is read, never written.** The sidecar mounts Lettuce's Google volumes read-only and refreshes the access token in memory. It asks Gmail only for history, labels and message headers, never for bodies.
- **Email content is untrusted.** Each email reaches the agent as one row of its id, sender and subject (JSON-quoted, shortened to 120 characters) inside a block that always ends with a note that it is email text, not instructions. Every id is always listed; when the request would run long, later rows lose their sender and subject first. Bodies never do. The agent still reads the email, so a hostile email can still try to steer it, as with any mail an agent reads.
- **"Act only on these emails" is guidance, not a boundary.** It is text in the prompt: the agent's Gmail tools still reach every email, and a conflicting instruction or a persuasive email can talk it out of the scope. Limit what the agent may send with Lettuce's own settings (Settings -> Google, tool approval).
- The container runs as root with every capability dropped and a read-only root filesystem, because the stack's state is root-owned; it can write only its rules directory.
