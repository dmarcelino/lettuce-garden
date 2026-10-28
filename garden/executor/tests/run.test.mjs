// Tests for the garden executor. Each test gets two local HTTP servers playing the garden
// service and the gmail-triggers sidecar, a token file and a fake `letta` on the PATH, and
// runs run.mjs with only the environment it sets (in production, agent-env hands it PID
// 1's), with --gmail-triggers unless the test says otherwise. The servers and the fake
// letta append to the same calls log, so tests see the real order of HTTP calls and letta
// runs; each HTTP call is logged as "<sidecar> <METHOD> <path>".

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { afterEach, test } from "node:test";
import { fileURLToPath } from "node:url";

const RUN = fileURLToPath(new URL("../run.mjs", import.meta.url));
const TOKEN = "0f".repeat(32);
const ID1 = "0123456789abcdef0123456789abcdef";
const ID2 = "fedcba9876543210fedcba9876543210";

// Behaviour comes from its environment, which it inherits from the executor. A prompt
// containing BREAK-OUTPUT makes it delete the executor's temp directory for its output.
const FAKE_LETTA = `#!${process.execPath}
const fs = require("node:fs");
const argv = process.argv.slice(2);
fs.appendFileSync(process.env.FAKE_CALLS, JSON.stringify({
  kind: "letta",
  argv,
  marker: process.env.FAKE_MARKER ?? null,
}) + "\\n");
const sub = argv.slice(0, 2).join(" ");
if (sub === "cron add") {
  const mode = process.env.FAKE_CRON || "ok";
  if (mode === "fail") { process.stderr.write("Error: " + "x".repeat(600) + "\\n"); process.exit(1); }
  if (mode === "short-fail") { process.stderr.write("  Error: agent not found\\n"); process.exit(1); }
  if (mode === "text") { process.stdout.write("Created task\\n"); process.exit(0); }
  if (mode === "no-id") { process.stdout.write('{"status":"active"}'); process.exit(0); }
  if (argv.some((a) => a.startsWith("--prompt=") && a.includes("BREAK-OUTPUT"))) {
    const out = fs.readlinkSync("/proc/self/fd/1");
    fs.rmSync(require("node:path").dirname(out), { recursive: true, force: true });
  }
  const n = fs.readFileSync(process.env.FAKE_CALLS, "utf8").split("\\n").length;
  process.stdout.write(JSON.stringify({ id: "task-" + n, runner: "local" }, null, 2));
  process.exit(0);
}
if (sub === "agents list") {
  const mode = process.env.FAKE_AGENTS || "ok";
  if (mode === "fail") { process.stderr.write("Error: backend unreachable\\n"); process.exit(2); }
  if (mode === "big") {
    const items = Array.from({ length: 1000 }, (_, i) =>
      ({ id: "agent-" + i, name: "n".repeat(150), system: "s".repeat(200) }));
    process.stdout.write(JSON.stringify({ items }, null, 2));
    process.exit(0);
  }
  process.stdout.write(JSON.stringify({ items: [
    { id: "agent-1", name: "lucius", system: "big" },
    { id: "agent-2", name: "y".repeat(250) },
    { id: "agent-3" },
    { id: 4, name: "number id" },
    null,
  ] }));
  process.exit(0);
}
process.stderr.write("unexpected command\\n");
process.exit(3);
`;

const cleanups = [];
afterEach(async () => {
  while (cleanups.length) await cleanups.pop()();
});

// A sidecar: `t[name]` says how to answer. For gmail-triggers, `requests` is the requests
// GET's list; for both, `agentsWanted` is the GET's agents_wanted (left out when
// undefined); `respond[route]` overrides an answer with { status, body, hang };
// `onResult(body)` runs before a result is answered.
function startSidecar(t, name) {
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try {
        body = raw ? JSON.parse(raw) : null;
      } catch {
        body = raw;
      }
      const route = `${name} ${req.method} ${req.url}`;
      fs.appendFileSync(t.calls, `${JSON.stringify({
        kind: "http",
        route,
        auth: req.headers.authorization ?? null,
        origin: req.headers.origin ?? null,
        type: req.headers["content-type"] ?? null,
        body,
      })}\n`);
      const side = t[name];
      const override = side.respond[route];
      if (override?.hang) return;
      if (req.headers.authorization !== `Bearer ${TOKEN}`) {
        res.writeHead(401).end("unauthorized");
        return;
      }
      if (override) {
        const headers = { "content-type": "application/json" };
        if (override.status >= 300 && override.status < 400) headers.location = "http://127.0.0.1:9/";
        res.writeHead(override.status ?? 200, headers);
        res.end(override.body ?? "");
        return;
      }
      if (route === "gmail-triggers GET /executor/v1/requests") {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ v: 1, requests: side.requests, agents_wanted: side.agentsWanted }));
        return;
      }
      if (route === "garden GET /executor/v1/status") {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ v: 1, agents_wanted: side.agentsWanted }));
        return;
      }
      if (route === "gmail-triggers POST /executor/v1/results") side.onResult?.(body);
      res.writeHead(204).end();
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      cleanups.push(
        () =>
          new Promise((done) => {
            server.closeAllConnections();
            server.close(done);
          }),
      );
      resolve(`http://127.0.0.1:${server.address().port}`);
    });
  });
}

async function setup(envExtra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "gt-exec-"));
  cleanups.push(async () => fs.rmSync(root, { recursive: true, force: true }));
  const t = {
    root,
    bin: path.join(root, "bin"),
    tmp: path.join(root, "tmp"),
    log: path.join(root, "executor.log"),
    calls: path.join(root, "calls.log"),
    tokenFile: path.join(root, "token"),
    lock: path.join(root, ".executor.lock"),
    "gmail-triggers": { requests: [], respond: {}, agentsWanted: true },
    garden: { respond: {}, agentsWanted: false },
    args: ["--gmail-triggers"],
  };
  t.sidecar = t["gmail-triggers"];
  fs.mkdirSync(t.bin);
  fs.mkdirSync(t.tmp);
  fs.writeFileSync(path.join(t.bin, "letta"), FAKE_LETTA, { mode: 0o755 });
  fs.writeFileSync(t.tokenFile, `${TOKEN}\n`, { mode: 0o600 });
  t.env = { PATH: `${t.bin}:/usr/bin:/bin`, HOME: root, TMPDIR: t.tmp, FAKE_CALLS: t.calls, ...envExtra };
  t.url = await startSidecar(t, "gmail-triggers");
  t.gardenUrl = await startSidecar(t, "garden");
  return t;
}

// Async, so this process's sidecar can answer while the executor runs.
function run(t) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [RUN, ...t.args], {
      env: {
        ...t.env,
        GARDEN_URL: t.gardenUrl,
        GT_URL: t.url,
        GT_TOKEN_FILE: t.tokenFile,
        GT_LOCK: t.lock,
        GT_LOG: t.log,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    const killer = setTimeout(() => child.kill("SIGKILL"), 40000);
    child.on("error", reject);
    child.on("close", (status) => {
      clearTimeout(killer);
      resolve({ status, stdout, stderr });
    });
  });
}

function request(fields = {}, id = ID1) {
  return {
    id,
    agent_id: "agent-1234abcd-5678",
    conversation: "new",
    name: "Gmail: invoices",
    description: "Gmail trigger: invoices",
    prompt: "Look at the new invoice mail.",
    created_at: "2026-10-25T10:00:00Z",
    ...fields,
  };
}

function calls(t) {
  if (!fs.existsSync(t.calls)) return [];
  return fs.readFileSync(t.calls, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

const lettaCalls = (t) => calls(t).filter((c) => c.kind === "letta");
const cronCalls = (t) => lettaCalls(t).filter((c) => c.argv[0] === "cron");
const httpCalls = (t) => calls(t).filter((c) => c.kind === "http");
const routes = (t) => httpCalls(t).map((c) => c.route);
const posted = (t, route) => httpCalls(t).filter((c) => c.route === route).map((c) => c.body);
const results = (t) => posted(t, "gmail-triggers POST /executor/v1/results");
const heartbeats = (t) => posted(t, "gmail-triggers POST /executor/v1/heartbeat");

// HTTP calls and letta runs in the order they happened.
function sequence(t) {
  return calls(t).map((c) => (c.kind === "http" ? c.route : `letta ${c.argv.slice(0, 2).join(" ")}`));
}

function cronArgv(req) {
  return [
    "cron",
    "add",
    `--name=${req.name}`,
    `--description=${req.description}`,
    `--prompt=${req.prompt}`,
    "--at=in 1m",
    "--once",
    `--agent=${req.agent_id}`,
    `--conversation=${req.conversation}`,
  ];
}

test("every field reaches letta as its own argument", async () => {
  const t = await setup();
  const req = request({ conversation: "default" });
  t.sidecar.requests = [req];
  const r = await run(t);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.deepEqual(cronCalls(t).map((c) => c.argv), [cronArgv(req)]);
  const [res] = results(t);
  assert.deepEqual(Object.keys(res).sort(), ["id", "ok", "task_id", "v"]);
  assert.equal(res.v, 1);
  assert.equal(res.id, ID1);
  assert.equal(res.ok, true);
  assert.match(res.task_id, /^task-\d+$/);
  assert.match(r.stdout, new RegExp(`request ${ID1}: task task-`));
  assert.ok(!r.stdout.includes(req.prompt), "the prompt must not be logged");
  assert.ok(!r.stdout.includes(TOKEN), "the token must not be logged");
  assert.ok(!fs.existsSync(t.lock));
});

test("calls go in order, with the token, as JSON and without an Origin", async () => {
  const t = await setup();
  t.sidecar.requests = [request({}, ID1), request({}, ID2)];
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(sequence(t), [
    "garden GET /executor/v1/status",
    "gmail-triggers GET /executor/v1/requests",
    "letta cron add",
    "gmail-triggers POST /executor/v1/results",
    "letta cron add",
    "gmail-triggers POST /executor/v1/results",
    "letta agents list",
    "garden PUT /executor/v1/agents",
    "gmail-triggers PUT /executor/v1/agents",
    "gmail-triggers POST /executor/v1/heartbeat",
  ]);
  assert.deepEqual(results(t).map((b) => b.id), [ID1, ID2]);
  for (const c of httpCalls(t)) {
    assert.equal(c.auth, `Bearer ${TOKEN}`);
    assert.equal(c.origin, null);
    if (!c.route.includes(" GET ")) assert.equal(c.type, "application/json");
  }
});

test("shell metacharacters and leading dashes stay literal", async () => {
  const t = await setup();
  const pwned = path.join(t.root, "pwned");
  const req = request({
    name: `Gmail: $(touch ${pwned}) \`touch ${pwned}\`; 'q' "dq"`,
    description: `--prompt=evil; touch ${pwned} | cat && echo`,
    prompt: `--once\n$(touch ${pwned})\n\`touch ${pwned}\`; touch ${pwned}\n'single' "double" \\ $HOME * ?\n-- end`,
  });
  t.sidecar.requests = [req];
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(cronCalls(t).map((c) => c.argv), [cronArgv(req)]);
  assert.ok(!fs.existsSync(pwned));
  assert.equal(results(t)[0].ok, true);
});

const INVALID = [
  ["agent id", { agent_id: "agent-x;rm" }, "agent_id is invalid"],
  ["agent id prefix", { agent_id: "--help" }, "agent_id is invalid"],
  ["long agent id", { agent_id: `agent-${"a".repeat(81)}` }, "agent_id is invalid"],
  ["conversation", { conversation: "conv-123" }, "conversation must be new or default"],
  ["missing conversation", { conversation: undefined }, "conversation must be new or default"],
  ["oversized prompt", { prompt: "p".repeat(8001) }, "prompt is over 8000 characters"],
  ["empty prompt", { prompt: "" }, "prompt is missing"],
  ["NUL in prompt", { prompt: "a\u0000b" }, "prompt contains a NUL character"],
  ["control char in name", { name: "Gmail: a\nb" }, "name contains control characters"],
  ["escape in name", { name: "Gmail: \u001b[31m" }, "name contains control characters"],
  ["long name", { name: "n".repeat(101) }, "name is over 100 characters"],
  ["control char in description", { description: "a\tb" }, "description contains control characters"],
  ["long description", { description: "d".repeat(201) }, "description is over 200 characters"],
  ["name not a string", { name: 5 }, "name is missing"],
];

test("invalid requests are refused with a result and letta is not called", async () => {
  const t = await setup();
  const ids = INVALID.map((_, i) => i.toString(16).padStart(32, "a"));
  t.sidecar.requests = INVALID.map(([, fields], i) => request(fields, ids[i]));
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(cronCalls(t), []);
  const got = results(t);
  assert.equal(got.length, INVALID.length);
  INVALID.forEach(([label, , error], i) => {
    assert.deepEqual(got[i], { v: 1, id: ids[i], ok: false, error }, label);
  });
});

test("entries without a valid id are skipped, duplicates run once", async () => {
  const t = await setup();
  t.sidecar.requests = [
    null,
    "text",
    [1],
    request({}, "not-an-id"),
    request({}, ID1.toUpperCase()),
    request({ id: undefined }),
    request({}, ID1),
    request({}, ID1),
    request({ agent_id: "bad" }, ID2),
  ];
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.equal(cronCalls(t).length, 1);
  assert.deepEqual(results(t).map((b) => [b.id, b.ok]), [[ID1, true], [ID2, false]]);
  assert.match(r.stdout, /request without a valid id skipped/);
  assert.match(r.stdout, /duplicate skipped/);
});

test("requests run in the order the sidecar sends them", async () => {
  const t = await setup();
  t.sidecar.requests = [request({ name: "Gmail: first" }, ID2), request({ name: "Gmail: second" }, ID1)];
  await run(t);
  assert.deepEqual(cronCalls(t).map((c) => c.argv[2]), ["--name=Gmail: first", "--name=Gmail: second"]);
});

test("a failing letta gives ok:false with its stderr, capped at 500 characters", async () => {
  const t = await setup({ FAKE_CRON: "fail" });
  t.sidecar.requests = [request()];
  const r = await run(t);
  const [res] = results(t);
  assert.equal(res.ok, false);
  assert.equal(res.task_id, undefined);
  assert.equal(res.error.length, 500);
  assert.ok(res.error.startsWith("Error: xxx"));
  assert.match(r.stdout, /failed, letta cron add: exited with code 1/);
  assert.ok(!r.stdout.includes("xxxxx"), "stderr goes to the result, not the log");
});

test("a failing letta's stderr is trimmed", async () => {
  const t = await setup({ FAKE_CRON: "short-fail" });
  t.sidecar.requests = [request()];
  await run(t);
  assert.equal(results(t)[0].error, "Error: agent not found");
});

test("non-JSON stdout with exit 0 is ok:false", async () => {
  const t = await setup({ FAKE_CRON: "text" });
  t.sidecar.requests = [request()];
  await run(t);
  assert.deepEqual(results(t)[0], { v: 1, id: ID1, ok: false, error: "letta cron add printed no JSON" });
});

test("JSON stdout without an id is ok:false", async () => {
  const t = await setup({ FAKE_CRON: "no-id" });
  t.sidecar.requests = [request()];
  await run(t);
  assert.equal(results(t)[0].ok, false);
  assert.equal(results(t)[0].error, "letta cron add printed no task id");
});

test("a result answered with 404 is logged and the run goes on", async () => {
  const t = await setup();
  t.sidecar.requests = [request({}, ID1), request({}, ID2)];
  t.sidecar.respond["gmail-triggers POST /executor/v1/results"] = { status: 404, body: '{"detail":"unknown"}' };
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.equal(cronCalls(t).length, 2);
  assert.equal(results(t).length, 2);
  assert.match(r.stdout, new RegExp(`result ${ID1}: gmail-triggers answered HTTP 404`));
  assert.equal(heartbeats(t).length, 1);
});

test("the agent list comes from .items[] and the heartbeat says letta is ok", async () => {
  const t = await setup();
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(lettaCalls(t).map((c) => c.argv), [["agents", "list", "--limit", "1000"]]);
  assert.deepEqual(posted(t, "gmail-triggers PUT /executor/v1/agents"), [
    {
      v: 1,
      agents: [
        { id: "agent-1", name: "lucius" },
        { id: "agent-2", name: "y".repeat(200) },
      ],
    },
  ]);
  assert.deepEqual(heartbeats(t), [{ v: 1, letta_ok: true }]);
});

test("a large agent list is read whole even though letta exits at once", async () => {
  const t = await setup({ FAKE_AGENTS: "big" });
  await run(t);
  const [{ agents }] = posted(t, "gmail-triggers PUT /executor/v1/agents");
  assert.equal(agents.length, 1000);
  assert.equal(agents[999].id, "agent-999");
});

test("a failing agents list sends no list and the heartbeat records it", async () => {
  const t = await setup({ FAKE_AGENTS: "fail" });
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(posted(t, "gmail-triggers PUT /executor/v1/agents"), []);
  assert.deepEqual(heartbeats(t), [
    { v: 1, letta_ok: false, error: "letta agents list: exited with code 2: Error: backend unreachable" },
  ]);
});

for (const [label, wanted] of [
  ["false", false],
  ["missing", undefined],
  ["not a boolean", "true"],
]) {
  test(`agents_wanted ${label} skips the agent list`, async () => {
    const t = await setup();
    t.sidecar.agentsWanted = wanted;
    t.sidecar.requests = [request()];
    assert.equal((await run(t)).status, 0);
    assert.deepEqual(sequence(t), [
      "garden GET /executor/v1/status",
      "gmail-triggers GET /executor/v1/requests",
      "letta cron add",
      "gmail-triggers POST /executor/v1/results",
      "gmail-triggers POST /executor/v1/heartbeat",
    ]);
    assert.deepEqual(heartbeats(t), [{ v: 1, letta_ok: true }]);
  });
}

test("agents_wanted true sends the agent list after the requests", async () => {
  const t = await setup();
  t.sidecar.agentsWanted = true;
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(sequence(t), [
    "garden GET /executor/v1/status",
    "gmail-triggers GET /executor/v1/requests",
    "letta agents list",
    "garden PUT /executor/v1/agents",
    "gmail-triggers PUT /executor/v1/agents",
    "gmail-triggers POST /executor/v1/heartbeat",
  ]);
});

const ID3 = "33333333333333333333333333333333";

test("a request that throws while starting letta doesn't end the batch", async () => {
  const t = await setup();
  t.sidecar.requests = [request({}, ID1), request({}, ID2), request({}, ID3)];
  // Between the first and second runs the temp directory becomes a file, so creating the
  // second request's output files throws; it is back before the third.
  t.sidecar.onResult = (body) => {
    if (body.id === ID1) {
      fs.rmSync(t.tmp, { recursive: true });
      fs.writeFileSync(t.tmp, "not a directory");
    } else if (body.id === ID2) {
      fs.rmSync(t.tmp);
      fs.mkdirSync(t.tmp);
    }
  };
  const r = await run(t);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.equal(cronCalls(t).length, 2);
  const got = results(t);
  assert.deepEqual(got.map((b) => [b.id, b.ok]), [[ID1, true], [ID2, false], [ID3, true]]);
  assert.equal(got[1].error, "executor error (ENOTDIR)");
  assert.match(r.stdout, new RegExp(`request ${ID2}: executor error \\(ENOTDIR\\)`));
  assert.equal(heartbeats(t).length, 1);
  assert.equal(posted(t, "gmail-triggers PUT /executor/v1/agents").length, 1);
});

test("a request that throws while reading letta's output doesn't end the batch", async () => {
  const t = await setup();
  t.sidecar.requests = [
    request({}, ID1),
    request({ prompt: "BREAK-OUTPUT please" }, ID2),
    request({}, ID3),
  ];
  const r = await run(t);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.equal(cronCalls(t).length, 3);
  const got = results(t);
  assert.deepEqual(got.map((b) => [b.id, b.ok]), [[ID1, true], [ID2, false], [ID3, true]]);
  assert.equal(got[1].error, "executor error (ENOENT)");
  assert.ok(!r.stdout.includes("BREAK-OUTPUT"), "the prompt must not be logged");
  assert.equal(heartbeats(t).length, 1);
});

test("a run where every letta call throws still answers each request and the heartbeat", async () => {
  const t = await setup();
  fs.rmSync(t.tmp, { recursive: true });
  fs.writeFileSync(t.tmp, "not a directory");
  t.sidecar.requests = [request({}, ID1), request({}, ID2)];
  const r = await run(t);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.deepEqual(lettaCalls(t), []);
  assert.deepEqual(results(t).map((b) => [b.id, b.ok, b.error]), [
    [ID1, false, "executor error (ENOTDIR)"],
    [ID2, false, "executor error (ENOTDIR)"],
  ]);
  assert.deepEqual(posted(t, "gmail-triggers PUT /executor/v1/agents"), []);
  assert.deepEqual(heartbeats(t), [{ v: 1, letta_ok: false, error: "letta agents list: executor error (ENOTDIR)" }]);
});

test("a missing letta claims nothing and says so in the heartbeat", async () => {
  const t = await setup();
  t.env.PATH = "/nonexistent:relative/bin";
  fs.copyFileSync(path.join(t.bin, "letta"), path.join(t.root, "letta"));
  t.sidecar.requests = [request()];
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(routes(t), ["gmail-triggers POST /executor/v1/heartbeat"]);
  assert.equal(heartbeats(t)[0].letta_ok, false);
  assert.match(heartbeats(t)[0].error, /letta not found/);
});

test("letta gets the executor's own environment", async () => {
  const t = await setup({ FAKE_MARKER: "from-executor" });
  t.sidecar.requests = [request()];
  await run(t);
  const all = lettaCalls(t);
  assert.equal(all.length, 2);
  for (const c of all) {
    assert.equal(c.marker, "from-executor");
  }
});

for (const [label, prepare] of [
  ["missing", (t) => fs.rmSync(t.tokenFile)],
  ["malformed", (t) => fs.writeFileSync(t.tokenFile, "not-a-token\n")],
  ["too short", (t) => fs.writeFileSync(t.tokenFile, TOKEN.slice(2))],
  ["upper case", (t) => fs.writeFileSync(t.tokenFile, TOKEN.toUpperCase())],
  [
    "a directory",
    (t) => {
      fs.rmSync(t.tokenFile);
      fs.mkdirSync(t.tokenFile);
    },
  ],
  [
    "a symlink",
    (t) => {
      const real = path.join(t.root, "real-token");
      fs.renameSync(t.tokenFile, real);
      fs.symlinkSync(real, t.tokenFile);
    },
  ],
]) {
  test(`a token file that is ${label} stops the run before any call`, async () => {
    const t = await setup();
    prepare(t);
    t.sidecar.requests = [request()];
    const r = await run(t);
    assert.equal(r.status, 0);
    assert.deepEqual(calls(t), []);
    assert.match(r.stdout, /token file/);
    assert.ok(!fs.existsSync(t.lock));
  });
}

test("a token the sidecar rejects claims nothing", async () => {
  const t = await setup();
  fs.writeFileSync(t.tokenFile, `${"ab".repeat(32)}\n`);
  t.sidecar.requests = [request()];
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.deepEqual(routes(t), ["garden GET /executor/v1/status", "gmail-triggers GET /executor/v1/requests"]);
  for (const c of httpCalls(t)) assert.equal(c.auth, `Bearer ${"ab".repeat(32)}`);
  assert.deepEqual(lettaCalls(t), []);
  assert.match(r.stdout, /status: garden answered HTTP 401/);
  assert.match(r.stdout, /requests: gmail-triggers answered HTTP 401/);
});

async function closedUrl() {
  const closed = http.createServer();
  await new Promise((done) => closed.listen(0, "127.0.0.1", done));
  const url = `http://127.0.0.1:${closed.address().port}`;
  await new Promise((done) => closed.close(done));
  return url;
}

test("an unreachable gmail-triggers is logged and the run exits 0", async () => {
  const t = await setup();
  t.url = await closedUrl();
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /gmail-triggers unreachable: ECONNREFUSED/);
  assert.deepEqual(lettaCalls(t), []);
  assert.deepEqual(routes(t), ["garden GET /executor/v1/status"]);
  assert.ok(!fs.existsSync(t.lock));
});

test("a gmail-triggers that never answers times out", { timeout: 60000 }, async () => {
  const t = await setup();
  t.sidecar.respond["gmail-triggers GET /executor/v1/requests"] = { hang: true };
  const started = Date.now();
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /gmail-triggers unreachable: timed out after 15 s/);
  assert.ok(Date.now() - started < 30000);
  assert.deepEqual(lettaCalls(t), []);
});

for (const [label, answer, logged] of [
  ["junk", { body: "<html>nope</html>" }, /requests: gmail-triggers sent no JSON/],
  ["the wrong version", { body: '{"v":2,"requests":[]}' }, /requests: gmail-triggers sent an unexpected answer/],
  ["no list", { body: '{"v":1,"requests":{}}' }, /requests: gmail-triggers sent an unexpected answer/],
  ["an error status", { status: 500, body: "{}" }, /requests: gmail-triggers answered HTTP 500/],
  ["a redirect", { status: 307, body: "" }, /gmail-triggers unreachable/],
  [
    "an oversized body",
    { body: JSON.stringify({ v: 1, requests: [], pad: "p".repeat(2 * 1024 * 1024) }) },
    /gmail-triggers unreachable: response over 2097152 bytes/,
  ],
]) {
  test(`a requests answer with ${label} runs nothing`, async () => {
    const t = await setup();
    t.sidecar.requests = [request()];
    t.sidecar.respond["gmail-triggers GET /executor/v1/requests"] = answer;
    const r = await run(t);
    assert.equal(r.status, 0);
    assert.match(r.stdout, logged);
    assert.deepEqual(routes(t), ["garden GET /executor/v1/status", "gmail-triggers GET /executor/v1/requests"]);
    assert.deepEqual(lettaCalls(t), []);
  });
}

// --- the garden service -----------------------------------------------------------

test("without --gmail-triggers only the garden is called, and its list sent", async () => {
  const t = await setup();
  t.args = [];
  t.garden.agentsWanted = true;
  t.sidecar.requests = [request()];
  const r = await run(t);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.deepEqual(sequence(t), ["garden GET /executor/v1/status", "letta agents list", "garden PUT /executor/v1/agents"]);
  assert.deepEqual(posted(t, "garden PUT /executor/v1/agents"), [
    {
      v: 1,
      agents: [
        { id: "agent-1", name: "lucius" },
        { id: "agent-2", name: "y".repeat(200) },
      ],
    },
  ]);
  for (const c of httpCalls(t)) assert.equal(c.auth, `Bearer ${TOKEN}`);
});

test("without --gmail-triggers and no list wanted, letta is not run", async () => {
  const t = await setup();
  t.args = [];
  assert.equal((await run(t)).status, 0);
  assert.deepEqual(sequence(t), ["garden GET /executor/v1/status"]);
});

test("without --gmail-triggers a failing agents list is logged, with no heartbeat", async () => {
  const t = await setup({ FAKE_AGENTS: "fail" });
  t.args = [];
  t.garden.agentsWanted = true;
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.deepEqual(sequence(t), ["garden GET /executor/v1/status", "letta agents list"]);
  assert.match(r.stdout, /error: letta agents list: exited with code 2/);
});

test("without --gmail-triggers a missing letta is logged and nothing is called", async () => {
  const t = await setup();
  t.args = [];
  t.env.PATH = "/nonexistent";
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.deepEqual(calls(t), []);
  assert.match(r.stdout, /error: letta not found on the PATH/);
});

for (const args of [["--gmail"], ["--gmail-triggers", "x"], ["--gmail-triggers=1"]]) {
  test(`an unknown argument (${args.join(" ")}) stops the run before any call`, async () => {
    const t = await setup();
    t.args = args;
    const r = await run(t);
    assert.equal(r.status, 0);
    assert.deepEqual(calls(t), []);
    assert.match(r.stdout, /usage: run.mjs \[--gmail-triggers\]/);
    assert.ok(!fs.existsSync(t.lock));
  });
}

for (const [label, gardenWants, gmailWants] of [
  ["the garden", true, false],
  ["gmail-triggers", false, true],
  ["both", true, true],
]) {
  test(`a list wanted by ${label} is made once and sent to both`, async () => {
    const t = await setup();
    t.garden.agentsWanted = gardenWants;
    t.sidecar.agentsWanted = gmailWants;
    t.sidecar.requests = [request()];
    assert.equal((await run(t)).status, 0);
    assert.deepEqual(sequence(t), [
      "garden GET /executor/v1/status",
      "gmail-triggers GET /executor/v1/requests",
      "letta cron add",
      "gmail-triggers POST /executor/v1/results",
      "letta agents list",
      "garden PUT /executor/v1/agents",
      "gmail-triggers PUT /executor/v1/agents",
      "gmail-triggers POST /executor/v1/heartbeat",
    ]);
    assert.deepEqual(posted(t, "garden PUT /executor/v1/agents"), posted(t, "gmail-triggers PUT /executor/v1/agents"));
  });
}

test("an unreachable garden doesn't stop the gmail-triggers run", async () => {
  const t = await setup();
  t.gardenUrl = await closedUrl();
  t.sidecar.requests = [request()];
  const r = await run(t);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /garden unreachable: ECONNREFUSED/);
  assert.deepEqual(sequence(t), [
    "gmail-triggers GET /executor/v1/requests",
    "letta cron add",
    "gmail-triggers POST /executor/v1/results",
    "letta agents list",
    "gmail-triggers PUT /executor/v1/agents",
    "gmail-triggers POST /executor/v1/heartbeat",
  ]);
  assert.equal(results(t)[0].ok, true);
  assert.deepEqual(heartbeats(t), [{ v: 1, letta_ok: true }]);
});

for (const [label, answer, logged] of [
  ["junk", { body: "<html>nope</html>" }, /status: garden sent no JSON/],
  ["the wrong version", { body: '{"v":2,"agents_wanted":true}' }, /status: garden sent an unexpected answer/],
  ["agents_wanted not a boolean", { body: '{"v":1,"agents_wanted":"true"}' }, /status: garden sent an unexpected/],
  ["an error status", { status: 500, body: "{}" }, /status: garden answered HTTP 500/],
  ["a redirect", { status: 307, body: "" }, /garden unreachable/],
]) {
  test(`a garden status answer with ${label} gets no list and doesn't stop gmail-triggers`, async () => {
    const t = await setup();
    t.garden.respond["garden GET /executor/v1/status"] = answer;
    t.sidecar.agentsWanted = false;
    t.sidecar.requests = [request()];
    const r = await run(t);
    assert.equal(r.status, 0);
    assert.match(r.stdout, logged);
    assert.deepEqual(sequence(t), [
      "garden GET /executor/v1/status",
      "gmail-triggers GET /executor/v1/requests",
      "letta cron add",
      "gmail-triggers POST /executor/v1/results",
      "gmail-triggers POST /executor/v1/heartbeat",
    ]);
  });
}

test("an unreachable gmail-triggers doesn't stop the garden's list", async () => {
  const t = await setup();
  t.url = await closedUrl();
  t.garden.agentsWanted = true;
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /gmail-triggers unreachable: ECONNREFUSED/);
  assert.deepEqual(sequence(t), [
    "garden GET /executor/v1/status",
    "letta agents list",
    "garden PUT /executor/v1/agents",
  ]);
});

test("a garden refusing the list doesn't stop gmail-triggers getting it", async () => {
  const t = await setup();
  t.garden.respond["garden PUT /executor/v1/agents"] = { status: 400, body: '{"error":"bad request"}' };
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /agents: garden answered HTTP 400/);
  assert.equal(posted(t, "gmail-triggers PUT /executor/v1/agents").length, 1);
  assert.deepEqual(heartbeats(t), [{ v: 1, letta_ok: true }]);
});

test("a stale lock is removed", async () => {
  const t = await setup();
  fs.mkdirSync(t.lock);
  const old = new Date(Date.now() - 11 * 60 * 1000);
  fs.utimesSync(t.lock, old, old);
  t.sidecar.requests = [request()];
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.equal(results(t)[0].ok, true);
  assert.ok(!fs.existsSync(t.lock));
  assert.match(r.stdout, /removing stale lock/);
});

test("a fresh lock is respected", async () => {
  const t = await setup();
  fs.mkdirSync(t.lock);
  t.sidecar.requests = [request()];
  const r = await run(t);
  assert.equal(r.status, 0);
  assert.equal(r.stdout, "");
  assert.deepEqual(calls(t), []);
  assert.ok(fs.statSync(t.lock).isDirectory());
});

test("the log is truncated when over 256 KiB", async () => {
  const t = await setup();
  fs.writeFileSync(t.log, "x".repeat(256 * 1024 + 1));
  await run(t);
  assert.equal(fs.statSync(t.log).size, 0);
});

test("a log under 256 KiB is kept", async () => {
  const t = await setup();
  fs.writeFileSync(t.log, "x".repeat(256 * 1024));
  await run(t);
  assert.equal(fs.statSync(t.log).size, 256 * 1024);
});

test("a symlinked log is not truncated through the link", async () => {
  const t = await setup();
  const target = path.join(t.root, "big");
  fs.writeFileSync(target, "x".repeat(300 * 1024));
  fs.symlinkSync(target, t.log);
  assert.equal((await run(t)).status, 0);
  assert.equal(fs.statSync(target).size, 300 * 1024);
});
