// Tests for lib/mcp-servers.mjs, which `lettucectl mcp` streams into the
// app-server: node --test tests/mcp-servers.test.mjs
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { update } from "../lib/mcp-servers.mjs";

const SCRIPT = fileURLToPath(new URL("../lib/mcp-servers.mjs", import.meta.url));
const OFFICE = { name: "office", transport: "stdio", command: "/opt/lettuce-doc-tools/bin/officeagent" };
const PDF = { name: "pdf", transport: "stdio", command: "/opt/lettuce-doc-tools/bin/pdf-mcp" };
const GOOGLE = { name: "google", transport: "http", url: "http://google-mcp:8000/mcp" };
const VAULT = { name: "vault", transport: "http", url: "http://garden-vault:8000/mcp", headers: { "x-a": "b" } };
// The entry lettucectl adds for the vault (upstream bff/src/mcp/settings.ts).
const OUR_VAULT = { name: "vault", transport: "http", url: "http://garden-vault:8000/mcp" };

// The file as upstream's BFF renders it (renderMcpSettings).
function settings(servers, extra = {}) {
  return {
    autoConversationTitles: false,
    autoConversationTitlesRollbackApplied: true,
    agents: [{ agentId: "agent-local-mcp-global", baseUrl: "local:/data/local-backend", mcpServers: servers }],
    ...extra,
  };
}

function render(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function tmpFile(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mcp-servers-"));
  const file = path.join(dir, "settings.json");
  if (content !== undefined) fs.writeFileSync(file, content, { mode: 0o644 });
  return file;
}

function serversIn(file) {
  return JSON.parse(fs.readFileSync(file, "utf8")).agents[0].mcpServers;
}

// As lettucectl runs it: the script on stdin, the operation, file and group as arguments.
function runScript(op, file, group = "doc-tools") {
  return spawnSync(process.execPath, ["--input-type=module", "-", op, file, group, "0", "0"], {
    input: fs.readFileSync(SCRIPT),
    encoding: "utf8",
  });
}

test("adds both servers after the existing ones and keeps everything else as it was", async () => {
  const file = tmpFile(render(settings([GOOGLE, VAULT], { someFutureKey: { a: 1 } })));
  const result = await update(file, "add", "doc-tools", { settleMs: 0 });
  assert.deepEqual(result.added, ["office", "pdf"]);
  const after = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.deepEqual(after, settings([GOOGLE, VAULT, OFFICE, PDF], { someFutureKey: { a: 1 } }));
  assert.equal(fs.readFileSync(file, "utf8"), render(after));
  assert.equal(fs.statSync(file).mode & 0o777, 0o644);
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ["settings.json"]);
});

test("a second run changes nothing", async () => {
  const file = tmpFile(render(settings([OFFICE, PDF])));
  const before = fs.readFileSync(file, "utf8");
  const result = await update(file, "add", "doc-tools", { settleMs: 0 });
  assert.deepEqual(result.added, []);
  assert.deepEqual(result.present, ["office", "pdf"]);
  assert.equal(fs.readFileSync(file, "utf8"), before);
});

test("a server of the same name with another command is the person's, and stays", async () => {
  const mine = { name: "pdf", transport: "stdio", command: "/usr/local/bin/my-pdf" };
  const file = tmpFile(render(settings([mine])));
  const result = await update(file, "add", "doc-tools", { settleMs: 0 });
  assert.deepEqual(result.added, ["office"]);
  assert.deepEqual(result.conflicts, ["pdf"]);
  assert.deepEqual(serversIn(file), [mine, OFFICE]);
});

test("remove takes out only entries that run the document tools", async () => {
  const renamed = { name: "docs", transport: "stdio", command: "/opt/lettuce-doc-tools/bin/pdf-mcp" };
  const other = { name: "pdf", transport: "stdio", command: "/usr/local/bin/my-pdf" };
  const file = tmpFile(render(settings([GOOGLE, OFFICE, renamed, other])));
  const result = await update(file, "remove", "doc-tools", { settleMs: 0 });
  assert.deepEqual(result.removed, ["office", "docs"]);
  assert.deepEqual(serversIn(file), [GOOGLE, other]);
});

test("refuses a file that is not what upstream renders, and leaves it as it was", async () => {
  const cases = {
    "not JSON": "{ nope",
    "not an object": "[]\n",
    "no agents": render({ autoConversationTitles: false }),
    "no global agent": render({ agents: [{ agentId: "agent-1", mcpServers: [] }] }),
    "two global agents": render({
      agents: [
        { agentId: "agent-local-mcp-global", mcpServers: [] },
        { agentId: "agent-local-mcp-global", mcpServers: [] },
      ],
    }),
    "no server list": render({ agents: [{ agentId: "agent-local-mcp-global" }] }),
    "a server that is not an object": render(settings(["office"])),
  };
  for (const [why, content] of Object.entries(cases)) {
    const file = tmpFile(content);
    await assert.rejects(update(file, "add", "doc-tools", { settleMs: 0 }), /not what lettucectl expects/, why);
    assert.equal(fs.readFileSync(file, "utf8"), content, why);
    assert.deepEqual(fs.readdirSync(path.dirname(file)), ["settings.json"], why);
  }
});

test("refuses when the file does not exist yet (the BFF creates it when it first connects)", async () => {
  const file = tmpFile();
  await assert.rejects(update(file, "add", "doc-tools", { settleMs: 0, waitMs: 0 }), /does not exist yet/);
  assert.equal(fs.existsSync(file), false);
});

test("waits for a new stack's BFF to create the file", async () => {
  const file = tmpFile();
  setTimeout(() => fs.writeFileSync(file, render(settings([]))), 300);
  const result = await update(file, "add", "doc-tools", { settleMs: 0, waitMs: 5000 });
  assert.deepEqual(result.added, ["office", "pdf"]);
  assert.deepEqual(serversIn(file), [OFFICE, PDF]);
});

test("removing from a file that does not exist has nothing to do", async () => {
  const file = tmpFile();
  const result = await update(file, "remove", "doc-tools", { settleMs: 0, waitMs: 0 });
  assert.deepEqual(result.removed, []);
  assert.equal(fs.existsSync(file), false);
});

test("a change landing between its read and its rename is kept, and the update is redone on top", async () => {
  const file = tmpFile(render(settings([GOOGLE])));
  let raced = false;
  const result = await update(file, "add", "doc-tools", {
    settleMs: 0,
    beforeRename: () => {
      if (raced) return;
      raced = true; // the person saves Settings -> MCP right then
      fs.writeFileSync(file, render(settings([GOOGLE, VAULT])));
    },
  });
  assert.deepEqual(serversIn(file), [GOOGLE, VAULT, OFFICE, PDF]);
  assert.equal(result.attempts, 2);
});

test("an overwrite after its rename (the BFF's own write) is noticed, and the servers are added again", async () => {
  const file = tmpFile(render(settings([GOOGLE])));
  let overwritten = false;
  const result = await update(file, "add", "doc-tools", {
    settleMs: 0,
    afterRename: () => {
      if (overwritten) return;
      overwritten = true;
      fs.writeFileSync(file, render(settings([GOOGLE])));
    },
  });
  assert.deepEqual(serversIn(file), [GOOGLE, OFFICE, PDF]);
  assert.equal(result.attempts, 2);
});

test("gives up after a few tries when the servers keep disappearing", async () => {
  const file = tmpFile(render(settings([GOOGLE])));
  await assert.rejects(
    update(file, "add", "doc-tools", {
      settleMs: 0,
      afterRename: () => fs.writeFileSync(file, render(settings([GOOGLE]))),
    }),
    /kept changing/,
  );
});

test("run as lettucectl runs it: a JSON summary, or exit 3 and a message", () => {
  const file = tmpFile(render(settings([GOOGLE])));
  const ok = runScript("add", file);
  assert.equal(ok.status, 0, ok.stderr);
  assert.deepEqual(JSON.parse(ok.stdout).added, ["office", "pdf"]);
  assert.deepEqual(serversIn(file), [GOOGLE, OFFICE, PDF]);

  const bad = tmpFile("{ nope");
  const refused = runScript("add", bad);
  assert.equal(refused.status, 3);
  assert.match(refused.stderr, /not what lettucectl expects/);
  assert.equal(refused.stdout, "");

  const usage = runScript("delete", file);
  assert.equal(usage.status, 2);
  const noGroup = runScript("add", file, "everything");
  assert.equal(noGroup.status, 2);
  assert.match(noGroup.stderr, /usage: .* add\|remove FILE doc-tools\|vault/);
});

test("adds the vault after the existing ones and keeps everything else as it was", async () => {
  const file = tmpFile(render(settings([GOOGLE, OFFICE], { someFutureKey: { a: 1 } })));
  const result = await update(file, "add", "vault", { settleMs: 0 });
  assert.deepEqual(result.added, ["vault"]);
  assert.deepEqual(
    JSON.parse(fs.readFileSync(file, "utf8")),
    settings([GOOGLE, OFFICE, OUR_VAULT], { someFutureKey: { a: 1 } }),
  );
});

test("the vault added by hand under any name counts as there", async () => {
  const mine = { name: "secrets", transport: "http", url: "http://garden-vault:8000/mcp" };
  const file = tmpFile(render(settings([GOOGLE, mine])));
  const before = fs.readFileSync(file, "utf8");
  const result = await update(file, "add", "vault", { settleMs: 0 });
  assert.deepEqual(result.added, []);
  assert.deepEqual(result.present, ["secrets"]);
  assert.equal(fs.readFileSync(file, "utf8"), before);
});

test("a vault server that is not the vault's exact HTTP entry is the person's, and nothing is added", async () => {
  const cases = [
    { name: "vault", transport: "http", url: "http://garden-vault:8000/mcp/" },
    { name: "vault", transport: "http", url: "https://vault.example.com/mcp" },
    { name: "vault", transport: "sse", url: "http://garden-vault:8000/mcp" },
    { name: "vault", transport: "stdio", command: "/usr/local/bin/my-vault" },
  ];
  for (const mine of cases) {
    const file = tmpFile(render(settings([mine])));
    const before = fs.readFileSync(file, "utf8");
    const result = await update(file, "add", "vault", { settleMs: 0 });
    assert.deepEqual(result.added, [], JSON.stringify(mine));
    assert.deepEqual(result.conflicts, ["vault"], JSON.stringify(mine));
    assert.equal(fs.readFileSync(file, "utf8"), before, JSON.stringify(mine));
  }
});

test("removing the vault takes out only its exact HTTP entries, under any name", async () => {
  const renamed = { name: "secrets", transport: "http", url: "http://garden-vault:8000/mcp" };
  const other = { name: "vault", transport: "http", url: "https://vault.example.com/mcp" };
  const file = tmpFile(render(settings([GOOGLE, OFFICE, VAULT, renamed, other, PDF])));
  const result = await update(file, "remove", "vault", { settleMs: 0 });
  assert.deepEqual(result.removed, ["vault", "secrets"]);
  assert.deepEqual(serversIn(file), [GOOGLE, OFFICE, other, PDF]);
});

test("each group leaves the other's entries alone", async () => {
  const file = tmpFile(render(settings([OFFICE, OUR_VAULT, PDF])));
  const docs = await update(file, "remove", "doc-tools", { settleMs: 0 });
  assert.deepEqual(docs.removed, ["office", "pdf"]);
  assert.deepEqual(serversIn(file), [OUR_VAULT]);
  const added = await update(file, "add", "doc-tools", { settleMs: 0 });
  assert.deepEqual(added.added, ["office", "pdf"]);
  assert.deepEqual(serversIn(file), [OUR_VAULT, OFFICE, PDF]);
  const vault = await update(file, "remove", "vault", { settleMs: 0 });
  assert.deepEqual(vault.removed, ["vault"]);
  assert.deepEqual(serversIn(file), [OFFICE, PDF]);
});

test("an overwrite after its rename is noticed for the vault too, and it is added again", async () => {
  const file = tmpFile(render(settings([OFFICE, PDF])));
  let overwritten = false;
  const result = await update(file, "add", "vault", {
    settleMs: 0,
    afterRename: () => {
      if (overwritten) return;
      overwritten = true;
      fs.writeFileSync(file, render(settings([OFFICE, PDF])));
    },
  });
  assert.deepEqual(serversIn(file), [OFFICE, PDF, OUR_VAULT]);
  assert.equal(result.attempts, 2);
});

test("run as lettucectl runs it for the vault", () => {
  const file = tmpFile(render(settings([OFFICE])));
  const ok = runScript("add", file, "vault");
  assert.equal(ok.status, 0, ok.stderr);
  assert.deepEqual(JSON.parse(ok.stdout).added, ["vault"]);
  const gone = runScript("remove", file, "vault");
  assert.equal(gone.status, 0, gone.stderr);
  assert.deepEqual(JSON.parse(gone.stdout).removed, ["vault"]);
  assert.deepEqual(serversIn(file), [OFFICE]);
});
