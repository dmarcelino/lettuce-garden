// Registers (or removes) the document tools' stdio servers in Lettuce's shared
// MCP list. `lettucectl mcp` and `up` stream it into the app-server and run it
// as `node --input-type=module - add|remove FILE` (docs/doc-tools.md).
//
// The list is the BFF's, in a file it renders whole (upstream
// bff/src/mcp/settings.ts): one synthetic agent, agent-local-mcp-global, whose
// mcpServers every `letta mcp` call reads. The BFF reads the file fresh before
// each change and shows what is on disk, so an entry added here is part of the
// list from then on. What is ours is any entry whose command runs a document
// tool (PREFIX): only those are added or removed, and every other entry and
// key stays as it was. A file of any other shape is refused untouched.
//
// Writes go to a temp file in the same directory, renamed over the original
// only when the original still holds what was read; a change landing in
// between (a Settings -> MCP save, the BFF's own update) makes it start over
// from that. After `settleMs` it reads the file back, since the BFF can
// overwrite a rename that fell between its own read and write, and tries again.

import fs from "node:fs/promises";
import path from "node:path";

const GLOBAL_AGENT = "agent-local-mcp-global";
const PREFIX = "/opt/lettuce-doc-tools/";
const SERVERS = [
  { name: "office", transport: "stdio", command: "/opt/lettuce-doc-tools/bin/officeagent" },
  { name: "pdf", transport: "stdio", command: "/opt/lettuce-doc-tools/bin/pdf-mcp" },
];
const ATTEMPTS = 4;

export class Refused extends Error {}

const ours = (server) => typeof server.command === "string" && server.command.startsWith(PREFIX);

/** The global agent's server list in parsed settings, or Refused. */
function serverList(settings) {
  const odd = new Refused(
    "the MCP settings are not what lettucectl expects (Lettuce may have changed them); add the servers in Settings -> MCP",
  );
  if (!settings || typeof settings !== "object" || Array.isArray(settings)) throw odd;
  if (!Array.isArray(settings.agents)) throw odd;
  const globals = settings.agents.filter((a) => a && typeof a === "object" && a.agentId === GLOBAL_AGENT);
  if (globals.length !== 1) throw odd;
  const servers = globals[0].mcpServers;
  if (!Array.isArray(servers) || !servers.every((s) => s && typeof s === "object" && !Array.isArray(s))) throw odd;
  return servers;
}

/** The list after `op`, and what changed; `next` is `servers` itself when nothing did. */
function change(servers, op) {
  const summary = { added: [], present: [], conflicts: [], removed: [] };
  if (op === "remove") {
    const kept = servers.filter((s) => !ours(s));
    summary.removed = servers.filter(ours).map((s) => s.name);
    return { next: kept.length === servers.length ? servers : kept, summary };
  }
  const next = [...servers];
  for (const want of SERVERS) {
    const same = servers.find((s) => s.name === want.name);
    if (!same) {
      next.push({ ...want });
      summary.added.push(want.name);
    } else if (same.command === want.command) {
      summary.present.push(want.name);
    } else {
      summary.conflicts.push(want.name);
    }
  }
  return { next: summary.added.length ? next : servers, summary };
}

/** Whether the file now holds what `op` should have left there. */
function settled(raw, op) {
  const servers = serverList(JSON.parse(raw));
  if (op === "remove") return !servers.some(ours);
  return SERVERS.every((want) => servers.some((s) => s.name === want.name));
}

/** The file's content, or null when it does not exist. */
async function readSettings(file) {
  try {
    return await fs.readFile(file, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

/** The file's content, waiting up to `waitMs` for the BFF to create it. */
async function waitForSettings(file, waitMs) {
  const until = Date.now() + waitMs;
  for (;;) {
    const raw = await readSettings(file);
    if (raw !== null || Date.now() >= until) return raw;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

/**
 * Applies `op` ("add" or "remove") to the settings file. A new stack's BFF
 * creates the file when it first connects, so `add` waits up to `waitMs` for
 * it; `remove` has nothing to do without it. `beforeRename` and `afterRename`
 * let tests land a concurrent write at those points.
 */
export async function update(file, op, { settleMs = 2000, waitMs = 90_000, beforeRename, afterRename } = {}) {
  let summary;
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    const raw = await waitForSettings(file, op === "add" ? waitMs : 0);
    if (raw === null) {
      if (op === "remove") return { ...change([], op).summary, attempts: attempt };
      throw new Refused("the MCP settings file does not exist yet (Lettuce creates it when it first connects); try again later");
    }
    let settings;
    try {
      settings = JSON.parse(raw);
    } catch {
      throw new Refused(
        "the MCP settings are not what lettucectl expects (not JSON); add the servers in Settings -> MCP",
      );
    }
    const servers = serverList(settings);
    const result = change(servers, op);
    // Report what this run found the first time; later attempts redo the same change.
    summary ??= result.summary;
    if (result.next === servers) return { ...summary, attempts: attempt };

    settings.agents.find((a) => a && a.agentId === GLOBAL_AGENT).mcpServers = result.next;
    const tmp = path.join(path.dirname(file), `.lettucectl-mcp-${process.pid}.tmp`);
    const { mode } = await fs.stat(file);
    try {
      await fs.writeFile(tmp, `${JSON.stringify(settings, null, 2)}\n`, { mode: mode & 0o777 });
      await fs.chmod(tmp, mode & 0o777);
      await beforeRename?.();
      if ((await readSettings(file)) !== raw) continue;
      await fs.rename(tmp, file);
    } finally {
      await fs.rm(tmp, { force: true });
    }
    await afterRename?.();
    if (settleMs) await new Promise((resolve) => setTimeout(resolve, settleMs));
    const now = await readSettings(file);
    if (now !== null && settled(now, op)) return { ...summary, attempts: attempt };
  }
  throw new Error(`the MCP settings kept changing while lettucectl wrote them; run the same command again`);
}

async function main(argv) {
  const [op, file, settle, wait] = argv;
  if (!["add", "remove"].includes(op) || !file) {
    process.stderr.write("usage: node --input-type=module - add|remove FILE [SETTLE_MS [WAIT_MS]]\n");
    return 2;
  }
  const options = {};
  if (settle !== undefined) options.settleMs = Number(settle);
  if (wait !== undefined) options.waitMs = Number(wait);
  try {
    const result = await update(file, op, options);
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return error instanceof Refused ? 3 : 1;
  }
}

// Piped in by lettucectl (`node -`), not imported by the tests.
if (process.argv[1] === "-") process.exitCode = await main(process.argv.slice(2));
