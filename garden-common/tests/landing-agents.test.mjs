// static/landing-agents.js in a vm with a fake page, WebSocket and timer.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SCRIPT = readFileSync(new URL("../src/garden_common/static/landing-agents.js", import.meta.url), "utf8");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
    this.hidden = false;
    this.dataset = {};
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
}

function load({ withBox = true } = {}) {
  const box = new Element("section");
  box.dataset.editor = "/garden/code-server/";
  const status = new Element("p");
  status.textContent = "Asking Lettuce for your agents...";
  const list = new Element("ul");
  list.hidden = true;
  const fallback = new Element("p");
  fallback.hidden = true;
  const ids = {
    "code-agents": withBox ? box : null,
    "code-agents-status": status,
    "code-agents-list": list,
    "code-agents-fallback": fallback,
  };
  const sockets = [];
  const timers = [];
  class WebSocket {
    constructor(url) {
      this.url = url;
      this.sent = [];
      this.closed = false;
      sockets.push(this);
    }
    send(data) {
      this.sent.push(JSON.parse(data));
    }
    close() {
      this.closed = true;
    }
    receive(frame) {
      this.onmessage({ data: typeof frame === "string" ? frame : JSON.stringify(frame) });
    }
  }
  vm.runInNewContext(SCRIPT, {
    document: { getElementById: (id) => ids[id], createElement: (tag) => new Element(tag) },
    location: { protocol: "https:", host: "alfred.example.com" },
    WebSocket,
    setTimeout: (fn, ms) => timers.push({ fn, ms }) - 1,
    clearTimeout: (id) => {
      if (timers[id]) timers[id].cleared = true;
    },
    encodeURIComponent,
    JSON,
  });
  return { status, list, fallback, sockets, timers };
}

// [title, description, href] of each row
const rows = (list) =>
  list.children.map((li) => {
    const a = li.children[0];
    const [title, desc] = a.children[0].children;
    return [title.textContent, desc.textContent, a.href];
  });

test("asks Lettuce for the agents after its hello and links each to its workspace", () => {
  const { status, list, fallback, sockets } = load();
  assert.equal(sockets.length, 1);
  const ws = sockets[0];
  assert.equal(ws.url, "wss://alfred.example.com/ws");
  assert.deepEqual(ws.sent, []);
  ws.receive({ type: "__bff_hello", session_id: "s1" });
  assert.deepEqual(ws.sent, [{ type: "agent_list", request_id: "garden-agent-list", query: { limit: 100 } }]);
  ws.receive({ type: "stream_delta", __seq: 4 });
  ws.receive({
    type: "agent_list_response",
    request_id: "garden-agent-list",
    success: true,
    agents: [
      { id: "agent-local-1", name: "lucius" },
      { id: "agent-2", name: '<img src=x onerror="alert(1)">' },
      { id: "agent-3" },
      { id: "../etc", name: "escape" },
      { id: "a?b=c", name: "query" },
      { id: 7, name: "number" },
      null,
    ],
  });
  assert.deepEqual(rows(list), [
    ["lucius", "/work/agent-local-1", "/garden/code-server/?folder=%2Fwork%2Fagent-local-1"],
    ['<img src=x onerror="alert(1)">', "/work/agent-2", "/garden/code-server/?folder=%2Fwork%2Fagent-2"],
    ["agent-3", "/work/agent-3", "/garden/code-server/?folder=%2Fwork%2Fagent-3"],
  ]);
  assert.equal(list.hidden, false);
  assert.equal(status.hidden, true);
  assert.equal(fallback.hidden, true);
  assert.equal(ws.closed, true);
});

test("says why and links to the editor itself when Lettuce refuses, lists none or goes away", () => {
  for (const [reply, said] of [
    [{ type: "__bff_error", request_id: "garden-agent-list", message: "Rate limit exceeded" }, "could not list"],
    [{ type: "agent_list_response", request_id: "garden-agent-list", success: false }, "could not list"],
    [{ type: "agent_list_response", request_id: "garden-agent-list", success: true, agents: [] }, "no agents yet"],
    [null, "Could not reach Lettuce"],
  ]) {
    const { status, list, fallback, sockets } = load();
    const ws = sockets[0];
    ws.receive({ type: "__bff_hello" });
    if (reply) ws.receive(reply);
    else ws.onclose();
    assert.match(status.textContent, new RegExp(said));
    assert.equal(status.hidden, false);
    assert.equal(fallback.hidden, false);
    assert.equal(list.hidden, true);
    assert.equal(ws.closed, true);
    // The close that follows our own close() changes nothing.
    ws.onclose();
    assert.match(status.textContent, new RegExp(said));
  }
});

test("gives up after 15 seconds and ignores what is not JSON", () => {
  const { status, fallback, sockets, timers } = load();
  sockets[0].receive("not json");
  sockets[0].receive("null");
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 15000);
  timers[0].fn();
  assert.match(status.textContent, /Lettuce did not answer/);
  assert.equal(fallback.hidden, false);
  assert.equal(sockets[0].closed, true);
});

test("does nothing on a page without the list", () => {
  assert.equal(load({ withBox: false }).sockets.length, 0);
});
