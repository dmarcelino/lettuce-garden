// The garden executor: runs as root in the Lettuce app-server, from system cron every
// minute, whenever any garden page profile is on. Over HTTP on the stack network, with one
// bearer token for both sidecars, it:
// - asks the garden service whether it wants the agent list (for the landing page);
// - with --gmail-triggers (the stack has that profile), claims the requests the
//   gmail-triggers sidecar has queued, turns each into a one-off letta-code task
//   (`letta cron add --once`) and posts the outcome back;
// - when either wants it, runs `letta agents list` once and sends the list to both;
// - with --gmail-triggers, sends gmail-triggers a heartbeat for its page.
// Without --gmail-triggers it never calls gmail-triggers. One sidecar being unreachable
// never stops what the run does for the other.
//
// Every response is untrusted: agents are root in this container and can read the token,
// so they can answer in a sidecar's place. So `letta` runs from an argument array (never
// a shell) and every field is validated. Values are passed as `--flag=value`, because
// letta's argument parser refuses a separate value that starts with a dash.
//
// Cron starts it through upstream's agent-env, so its environment (and letta's) is PID 1's.
//
// Overrides (tests only): GARDEN_URL, GT_URL, GT_TOKEN_FILE, GT_LOCK, GT_LOG.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const HOME = "/root/.letta/garden/executor";
const base = (url) => url.replace(/\/+$/, "");
const GARDEN = { name: "garden", url: base(process.env.GARDEN_URL || "http://garden:8000") };
const GMAIL = { name: "gmail-triggers", url: base(process.env.GT_URL || "http://garden-gmail-triggers:8000") };
const TOKEN_FILE = process.env.GT_TOKEN_FILE || "/etc/lettuce-garden/executor-token";
const LOCK = process.env.GT_LOCK || `${HOME}/.executor.lock`;
const LOG = process.env.GT_LOG || `${HOME}/executor.log`;

const LOG_MAX = 256 * 1024;
const LOCK_STALE_MS = 10 * 60 * 1000;
const TOKEN_FILE_MAX = 1024;
const RESPONSE_MAX = 2 * 1024 * 1024;
const HTTP_TIMEOUT_MS = 15 * 1000;
const STDOUT_MAX = 64 * 1024 * 1024;
const STDERR_MAX = 64 * 1024;
const COMMAND_TIMEOUT_MS = 60 * 1000;
const ERROR_MAX = 500;
const AGENT_NAME_MAX = 200;
const AGENTS_MAX = 1000;

const TOKEN = /^[0-9a-f]{64}$/;
const ID = /^[0-9a-f]{32}$/;
const AGENT_ID = /^agent-[A-Za-z0-9-]{1,80}$/;
const CONVERSATIONS = new Set(["new", "default"]);
// C0, DEL and C1 control characters.
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

const { O_RDONLY, O_WRONLY, O_CREAT, O_EXCL, O_NOFOLLOW, O_NONBLOCK } = fs.constants;

const now = () => new Date().toISOString();

function log(message) {
  process.stdout.write(`${now()} ${message}\n`);
}

// Length in code points, as the sidecar (Python) counts it.
const length = (s) => [...s].length;

function clip(s, max) {
  const chars = [...s];
  return chars.length > max ? chars.slice(0, max).join("") : s;
}

const errorText = (s) => clip(s.trim(), ERROR_MAX);

// --- lock ------------------------------------------------------------------------

function lock(dir) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      fs.mkdirSync(dir, { mode: 0o700 });
      return true;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
    }
    let st;
    try {
      st = fs.lstatSync(dir);
    } catch (err) {
      if (err.code === "ENOENT") continue;
      throw err;
    }
    if (Date.now() - st.mtimeMs < LOCK_STALE_MS) return false;
    log("removing stale lock");
    // rmSync never follows symlinks, so a planted link loses only itself.
    fs.rmSync(dir, { recursive: true, force: true });
  }
  return false;
}

// Long runs keep the lock fresh, so a second executor never takes it as stale.
function touch(dir) {
  const t = new Date();
  try {
    fs.utimesSync(dir, t, t);
  } catch {
    // Gone or replaced: the release at the end cleans up whatever is there.
  }
}

// --- files -----------------------------------------------------------------------

function truncateLog(file) {
  let fd;
  try {
    fd = fs.openSync(file, O_WRONLY | O_NOFOLLOW | O_NONBLOCK);
  } catch (err) {
    if (err.code !== "ENOENT") log(`log: cannot open (${err.code || err.message})`);
    return;
  }
  try {
    const st = fs.fstatSync(fd);
    if (st.isFile() && st.size > LOG_MAX) fs.ftruncateSync(fd, 0);
  } finally {
    fs.closeSync(fd);
  }
}

// Returns the file's bytes; throws when it is missing, not a regular file (symlinks
// included) or over `max` bytes.
function readRegular(file, max) {
  // O_NONBLOCK so a planted FIFO can't hang the open.
  const fd = fs.openSync(file, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) throw new Error(`${file} is not a regular file`);
    const tooBig = () => new Error(`${file} is over ${max} bytes`);
    if (st.size > max) throw tooBig();
    const buf = Buffer.alloc(max + 1);
    let n = 0;
    for (;;) {
      const got = fs.readSync(fd, buf, n, buf.length - n, null);
      if (got === 0) break;
      n += got;
      if (n > max) throw tooBig();
    }
    return buf.subarray(0, n);
  } finally {
    fs.closeSync(fd);
  }
}

// The token is never logged: errors name the file only.
function readToken(file) {
  let raw;
  try {
    raw = readRegular(file, TOKEN_FILE_MAX);
  } catch (err) {
    return { error: `token file unusable (${err.code || "not a regular file or too big"})` };
  }
  const token = raw.toString("utf8").trim();
  return TOKEN.test(token) ? { token } : { error: "token file does not hold a valid token" };
}

// --- letta ---------------------------------------------------------------------

function findLetta(env) {
  for (const dir of (env.PATH || "").split(":")) {
    if (!path.isAbsolute(dir)) continue;
    const candidate = path.join(dir, "letta");
    try {
      if (!fs.statSync(candidate).isFile()) continue;
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      // Not here; keep looking.
    }
  }
  return null;
}

function readCapped(file, max) {
  const fd = fs.openSync(file, O_RDONLY | O_NOFOLLOW);
  try {
    const buf = Buffer.alloc(Math.min(fs.fstatSync(fd).size, max));
    const n = fs.readSync(fd, buf, 0, buf.length, 0);
    return buf.subarray(0, n).toString("utf8");
  } finally {
    fs.closeSync(fd);
  }
}

// Runs letta with an argument array and no shell. stdout and stderr go to files, not
// pipes: letta exits before flushing a pipe past 64 KiB. Throws on file errors.
async function runLetta(ctx, args) {
  touch(LOCK);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "garden-executor-"));
  try {
    const outPath = path.join(tmp, "stdout");
    const errPath = path.join(tmp, "stderr");
    const run = await spawnTo(ctx, args, outPath, errPath);
    run.stdout = readCapped(outPath, STDOUT_MAX);
    run.stderr = readCapped(errPath, STDERR_MAX);
    return run;
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function spawnTo(ctx, args, outPath, errPath) {
  const flags = O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW;
  const out = fs.openSync(outPath, flags, 0o600);
  let err;
  try {
    err = fs.openSync(errPath, flags, 0o600);
  } catch (e) {
    fs.closeSync(out);
    throw e;
  }
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    let timedOut = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fs.closeSync(out);
      fs.closeSync(err);
      resolve(result);
    };
    let child;
    try {
      child = spawn(ctx.letta, args, { cwd: ctx.cwd, stdio: ["ignore", out, err] });
    } catch (e) {
      finish({ code: null, spawnError: e.code || e.name });
      return;
    }
    timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, COMMAND_TIMEOUT_MS);
    child.on("error", (e) => finish({ code: null, spawnError: e.code || e.name }));
    child.on("exit", (code, signal) => finish({ code, signal, timedOut }));
  });
}

// Short, generic and free of request text, for errors thrown while handling one request.
const executorError = (err) => `executor error (${err.code || err.name})`;

// One line describing a failed run, for errors and the log; never includes stdout.
function failure(what, run) {
  if (run.spawnError) return `${what}: could not run letta (${run.spawnError})`;
  if (run.timedOut) return `${what}: timed out after ${COMMAND_TIMEOUT_MS / 1000} s`;
  if (run.signal) return `${what}: killed by ${run.signal}`;
  return `${what}: exited with code ${run.code}`;
}

// --- sidecar APIs ----------------------------------------------------------------

async function readBody(res, max) {
  if (!res.body) return "";
  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > max) {
      await reader.cancel().catch(() => {});
      throw new Error(`response over ${max} bytes`);
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

// Returns { status, body } or throws with a short reason (never the token).
async function api(ctx, sidecar, method, route, payload) {
  const headers = { authorization: `Bearer ${ctx.token}`, accept: "application/json" };
  if (payload !== undefined) headers["content-type"] = "application/json";
  let res;
  try {
    res = await fetch(`${sidecar.url}${route}`, {
      method,
      headers,
      body: payload === undefined ? undefined : JSON.stringify(payload),
      redirect: "error",
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
    return { status: res.status, body: await readBody(res, RESPONSE_MAX) };
  } catch (err) {
    if (err.name === "TimeoutError") throw new Error(`timed out after ${HTTP_TIMEOUT_MS / 1000} s`);
    throw new Error(err.cause?.code || err.cause?.message || err.message);
  }
}

// For the POST and PUT calls: logs instead of throwing, so one failure doesn't stop the run.
async function send(ctx, sidecar, method, route, payload, what) {
  try {
    const { status } = await api(ctx, sidecar, method, route, payload);
    if (status < 200 || status > 299) {
      log(`${what}: ${sidecar.name} answered HTTP ${status}`);
      return false;
    }
    return true;
  } catch (err) {
    log(`${what}: ${sidecar.name}: ${err.message}`);
    return false;
  }
}

// A GET answered with a JSON object carrying "v": 1, or null after logging why not.
async function getJson(ctx, sidecar, route, what) {
  let res;
  try {
    res = await api(ctx, sidecar, "GET", route);
  } catch (err) {
    log(`${sidecar.name} unreachable: ${err.message}`);
    return null;
  }
  if (res.status !== 200) {
    log(`${what}: ${sidecar.name} answered HTTP ${res.status}`);
    return null;
  }
  let out;
  try {
    out = JSON.parse(res.body);
  } catch {
    log(`${what}: ${sidecar.name} sent no JSON`);
    return null;
  }
  if (out?.v !== 1) {
    log(`${what}: ${sidecar.name} sent an unexpected answer`);
    return null;
  }
  return out;
}

// Returns { agentsWanted }, or null when the garden service did not answer usably.
async function gardenStatus(ctx) {
  const out = await getJson(ctx, GARDEN, "/executor/v1/status", "status");
  if (out === null) return null;
  if (typeof out.agents_wanted !== "boolean") {
    log("status: garden sent an unexpected answer");
    return null;
  }
  return { agentsWanted: out.agents_wanted };
}

// Returns { requests, agentsWanted }, or null after logging why there are none.
async function claimRequests(ctx) {
  const out = await getJson(ctx, GMAIL, "/executor/v1/requests", "requests");
  if (out === null) return null;
  if (!Array.isArray(out.requests)) {
    log("requests: gmail-triggers sent an unexpected answer");
    return null;
  }
  return { requests: out.requests, agentsWanted: out.agents_wanted === true };
}

// --- requests --------------------------------------------------------------------

function textField(req, key, max, allowControl) {
  const v = req[key];
  if (typeof v !== "string" || v.length === 0) return `${key} is missing`;
  if (!v.isWellFormed()) return `${key} is not valid text`;
  if (length(v) > max) return `${key} is over ${max} characters`;
  if (v.includes("\0")) return `${key} contains a NUL character`;
  if (!allowControl && CONTROL.test(v)) return `${key} contains control characters`;
  return null;
}

// Returns an error message (which never quotes the values) or null.
function validate(req) {
  if (typeof req.agent_id !== "string" || !AGENT_ID.test(req.agent_id)) {
    return "agent_id is invalid";
  }
  if (!CONVERSATIONS.has(req.conversation)) return "conversation must be new or default";
  return (
    textField(req, "name", 100, false) ||
    textField(req, "description", 200, false) ||
    textField(req, "prompt", 8000, true)
  );
}

async function handle(ctx, req, seen) {
  const isObject = typeof req === "object" && req !== null && !Array.isArray(req);
  if (!isObject || typeof req.id !== "string" || !ID.test(req.id)) {
    log("request without a valid id skipped");
    return;
  }
  const { id } = req;
  if (seen.has(id)) {
    log(`request ${id}: duplicate skipped`);
    return;
  }
  seen.add(id);

  let result;
  try {
    const invalid = validate(req);
    if (invalid) {
      result = { ok: false, error: invalid };
      log(`request ${id}: refused, ${invalid}`);
    } else {
      result = await addTask(ctx, req);
      log(`request ${id}: ${result.ok ? `task ${result.task_id}` : `failed, ${result.log}`}`);
      delete result.log;
    }
  } catch (err) {
    result = { ok: false, error: executorError(err) };
    log(`request ${id}: ${result.error}`);
  }
  await send(ctx, GMAIL, "POST", "/executor/v1/results", { v: 1, id, ...result }, `result ${id}`);
}

async function addTask(ctx, req) {
  const run = await runLetta(ctx, [
    "cron",
    "add",
    `--name=${req.name}`,
    `--description=${req.description}`,
    `--prompt=${req.prompt}`,
    "--at=in 1m",
    "--once",
    `--agent=${req.agent_id}`,
    `--conversation=${req.conversation}`,
  ]);
  if (run.code !== 0) {
    const why = failure("letta cron add", run);
    return { ok: false, error: errorText(run.stderr) || why, log: why };
  }
  let out;
  try {
    out = JSON.parse(run.stdout);
  } catch {
    const why = "letta cron add printed no JSON";
    return { ok: false, error: why, log: why };
  }
  const taskId = out?.id;
  const usable = typeof taskId === "string" && taskId.length > 0 && length(taskId) <= 200;
  if (!usable || CONTROL.test(taskId)) {
    const why = "letta cron add printed no task id";
    return { ok: false, error: why, log: why };
  }
  return { ok: true, task_id: taskId };
}

// --- agents ----------------------------------------------------------------------

// Runs `letta agents list` once and sends the list to each of `sidecars`. Returns an error
// message, or null once the list is sent.
async function refreshAgents(ctx, sidecars) {
  const run = await runLetta(ctx, ["agents", "list", "--limit", String(AGENTS_MAX)]);
  if (run.code !== 0) {
    const why = failure("letta agents list", run);
    const stderr = errorText(run.stderr);
    return stderr ? `${why}: ${stderr}` : why;
  }
  let out;
  try {
    out = JSON.parse(run.stdout);
  } catch {
    return "letta agents list printed no JSON";
  }
  if (!Array.isArray(out?.items)) return "letta agents list printed no items";
  const agents = out.items
    .filter((a) => typeof a?.id === "string")
    .slice(0, AGENTS_MAX)
    .map((a) => ({ id: a.id, name: typeof a.name === "string" ? clip(a.name, AGENT_NAME_MAX) : "" }));
  for (const sidecar of sidecars) {
    await send(ctx, sidecar, "PUT", "/executor/v1/agents", { v: 1, agents }, "agents");
  }
  return null;
}

// --- main ------------------------------------------------------------------------

async function heartbeat(ctx, lettaOk, errors) {
  for (const e of errors) log(`error: ${e}`);
  const body = { v: 1, letta_ok: lettaOk };
  if (errors.length) body.error = errorText(errors.join("; "));
  await send(ctx, GMAIL, "POST", "/executor/v1/heartbeat", body, "heartbeat");
}

// The one argument cron may pass; anything else stops the run before any call.
function parseArgs(argv) {
  if (argv.length === 0) return { gmail: false };
  if (argv.length === 1 && argv[0] === "--gmail-triggers") return { gmail: true };
  return null;
}

async function run() {
  if (!lock(LOCK)) return;
  try {
    truncateLog(LOG);
    const args = parseArgs(process.argv.slice(2));
    if (!args) {
      log("usage: run.mjs [--gmail-triggers]");
      return;
    }
    const { token, error } = readToken(TOKEN_FILE);
    if (!token) {
      log(error);
      return;
    }
    const ctx = { token };

    // Without letta nothing is claimed, so the requests wait for a run that can do them.
    ctx.letta = findLetta(process.env);
    if (!ctx.letta) {
      const why = "letta not found on the PATH";
      if (args.gmail) await heartbeat(ctx, false, [why]);
      else log(`error: ${why}`);
      return;
    }
    const home = process.env.HOME;
    const homeDir = home && path.isAbsolute(home) && fs.statSync(home, { throwIfNoEntry: false });
    ctx.cwd = homeDir?.isDirectory() ? home : "/";

    const garden = await gardenStatus(ctx);
    const claimed = args.gmail ? await claimRequests(ctx) : null;
    if (claimed) {
      const seen = new Set();
      for (const req of claimed.requests) await handle(ctx, req, seen);
    }

    // The list goes to each sidecar that answered, whichever of them asked for it.
    let agentsError = null;
    if (garden?.agentsWanted || claimed?.agentsWanted) {
      const to = [garden && GARDEN, claimed && GMAIL].filter(Boolean);
      try {
        agentsError = await refreshAgents(ctx, to);
      } catch (err) {
        agentsError = `letta agents list: ${executorError(err)}`;
      }
    }
    if (claimed) await heartbeat(ctx, !agentsError, agentsError ? [agentsError] : []);
    else if (agentsError) log(`error: ${agentsError}`);
  } finally {
    fs.rmSync(LOCK, { recursive: true, force: true });
  }
}

run().then(
  () => {
    process.exitCode = 0;
  },
  (err) => {
    log(`error: ${err.message}`);
    process.exitCode = 1;
  },
);
