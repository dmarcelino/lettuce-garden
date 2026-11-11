# Watchlist

Each stack can have a watchlist: ask an agent, in plain words, to keep an eye on something and report on a schedule. "Tell me which films open soon at my cinema", "let me know when these promoters announce a concert", "give me the major international news at 9am, Monday to Friday". The agent finds the sources and sets the monitor up once; from then on a small service checks the sources without a model, and the agent reports at the chosen times only when something is new. It is opt-in, behind the `garden-watchlist` profile.

![From a request in chat to a report when something is new: you, your agent, monitors, websites, the garden executor, Lettuce's scheduler](diagrams/monitors-flow.svg)

An interactive version, [monitors-flow.html](diagrams/monitors-flow.html), opens in a browser from a clone of this repo.

How the watchlist is built and wired, for developers: [watchlist/README.md](../watchlist/README.md); the design record is [watchlist/DESIGN.md](../watchlist/DESIGN.md).

## Using it
1. **Ask your agent.** Say what you want to follow and, if you like, when to hear about it: "every weekday at 9", "twice a day", "on Sunday evenings". Without a time, you get the stack's default (08:00, 14:00 and 20:00 unless your admin changed it). Times are in the stack's timezone, which the agent tells you when it confirms.
2. **The agent sets it up.** It looks for where the information is published (a feed, a calendar, the data a page loads, the page itself), tests that it can read it, and saves a monitor. It tells you the schedule in words and shows what the first report would hold.
3. **Reports come only when there is news.** At each scheduled time the service looks at what is new or changed since the last report; with nothing new, nothing happens and no agent runs. Otherwise the agent gets the new items and writes you a short report: in a new conversation each time, or in its main conversation if you asked for that. Each report also shows in Lettuce's **Tasks** tab as "Watchlist: <schedule>".
4. **Change it by asking.** "Only science fiction and independent films", "stop the concerts one", "move the news to 8am": the agent updates the monitor.
5. **See and stop them on the page:** `https://<your garden>/watchlist/` (your stack's garden address, `<name>-garden.<domain>`; its home page links to it) lists every monitor of every agent, with its schedule, sources and last report, and each source with whether it is working. **Delete** removes a monitor, or a source no monitor uses. A warning shows when Lettuce isn't picking up reports.

**News on a topic** ("any significant news about <topic>") is followed through news searches; articles about the same story are grouped, and the agent decides what is significant for you.

**When a source breaks** (a site changed its layout, or stopped answering), the next report says so and the agent tries to repair it in the same turn. A broken source is mentioned at most once a day on its own. A site that starts asking for a login, a CAPTCHA or a "checking your browser" page can't be monitored any more; the agent tells you.

**Not everything can be monitored.** Sites behind a login, a waiting room, a CAPTCHA or a paywall are out of reach, as are sites whose robots.txt refuses automated readers. Pages that only work with a browser's scripts can often still be read through the data they load; when they can't, the agent says so.

**Newsletters** are not a monitor source. Subscribe with a Gmail address of yours (a `you+events@gmail.com` alias works), filter those emails into a label, and use [Gmail triggers](gmail-triggers.md) on that label.

## Running it
```bash
./lettucectl profiles cloudflared,garden-watchlist   # plus whatever else you run
./lettucectl mcp all                                 # adds the watchlist server to Settings -> MCP
./lettucectl skills all                              # installs the watchlist-sources skill
```
- `profiles` routes `https://<garden>/watchlist/` to the service on every stack, on the stack's garden address and its own Access application, and `up` installs the garden executor in the app-server with the watchlist's part on. Dropping the profile removes the route and the container; the data stays on its volume until the stack is removed.
- `mcp` registers `watchlist -> http://garden-watchlist:8000/mcp` in each stack's Settings -> MCP, once per stack (a server the person removed stays removed; `--force` adds it again).
- **Settings**, in `config.env`, copied into every stack by `up`; an empty value keeps the default:

  | Key | Default | What |
  |---|---|---|
  | `WATCHLIST_DEFAULT_TIMES` | `08:00,14:00,20:00` | When a monitor reports if the person names no time, every day, in `TZ` |
  | `WATCHLIST_MAX_REPORTS_PER_DAY` | `6` | Reports one agent may get per day, all its monitors together (1 to 48) |
  | `WATCHLIST_MIN_INTERVAL` | `1h` | The shortest interval between two fetches of one source (5m to 7d) |
  | `WATCHLIST_MAX_SOURCES` | `100` | Sources per stack |
  | `WATCHLIST_MAX_MONITORS` | `50` | Monitors per stack |

  Change them in `config.env` and run `./lettucectl up all`.
- **Data:** monitors, sources and what they found live on the stack's named volume `lettuce-<name>_watchlist-data`. `backup` copies it; `remove` deletes it with the stack.
- **Logs:** `./lettucectl logs <name> garden-watchlist` shows fetches (source names, counts, failure reasons; never the text found) and reports queued.
- **Traffic:** the service fetches from the stack's network, not through pipelock; it identifies itself as `lettuce-garden-watchlist` with a link to this project. Sources are fetched every few hours at most, and at least 2 seconds apart per site.

## Security model
- **Agents set monitors up; the person sees and deletes them.** Agents create, change and delete monitors and sources, schedules included, through the watchlist MCP server. The `/watchlist/` page, behind Cloudflare Access like the other garden pages, shows all of them and can delete any. Its pages load only as top-level pages, its forms post only from its own pages, and they open cut off from any other tab.
- **Any agent of the stack can manage any agent's monitors.** The server can't tell which agent is calling: agents name themselves, and nothing checks it. A stack is one person's, so that stays within what that person's agents can do.
- **Monitors start agent turns.** At a monitor's scheduled times, and only when there is something to report, the service queues a report; the garden executor in the app-server turns it into a one-off task with letta-code's own `letta cron add`, and Lettuce's scheduler starts the turn on the BFF's connection, the only one. That gives agents nothing new: they can already schedule tasks themselves. Reports are capped per agent per day (`WATCHLIST_MAX_REPORTS_PER_DAY`); the cap covers the turns monitors start, not tasks agents schedule on their own.
- **Agents can see what the executor sees.** They are root in the app-server, so they can read the executor's token and use its API: read pending reports, claim or drop them, or report false results. They can't create reports. The token keeps out the stack's other containers.
- **Everything fetched is untrusted.** It reaches the agent quoted, shortened and marked as text from websites, not instructions. A hostile page can still try to steer the agent that reads it, as with any page an agent reads, and a source an agent registered keeps feeding it until someone deletes it: the page is how you find and stop one.
- **The service fetches public addresses only.** It refuses private, loopback and link-local addresses, checks every address it actually connects to (and every redirect), runs no script, keeps no cookies, sends no credentials and logs in nowhere. It honours robots.txt and treats challenge and waiting-room pages as failures. This keeps a scheduled job off the stack's own services; it is not a boundary for agents, which reach the stack network themselves.
- **No model runs in the service.** It only fetches, extracts and compares. Deciding what matters is the agent's, in its own turn, with its own model settings.
- **Its data stays on its own volume.** Only the service mounts it (and `backup`, read-only); it holds no secret. The container runs as a non-root user, read-only, with every capability dropped.
