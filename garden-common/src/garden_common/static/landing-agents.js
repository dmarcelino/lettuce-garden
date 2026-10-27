// The garden landing page with the code editor on: one link per agent that opens
// the editor in its workspace (/work/<agent-id>). Lettuce alone knows the agents,
// so this asks the BFF over /ws with `agent_list`, as Lettuce's own web client
// does (AGENTS.md names the dependency), then closes the socket. Names are shown
// as text only, and an id must look like one before it goes in a link.
(() => {
  "use strict";
  const box = document.getElementById("code-agents");
  if (!box) return;
  const status = document.getElementById("code-agents-status");
  const list = document.getElementById("code-agents-list");
  const fallback = document.getElementById("code-agents-fallback");
  const editor = box.dataset.editor;
  const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  const REQUEST = "garden-agent-list";
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  let done = false;
  let timer = null;

  // message: why there is no list ("" when there is one); the page then links
  // to the editor itself.
  const finish = (message) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    socket.close();
    status.textContent = message;
    status.hidden = !message;
    fallback.hidden = !message;
  };

  const row = (agent) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.className = "menu-row";
    link.href = `${editor}?folder=${encodeURIComponent(`/work/${agent.id}`)}`;
    const text = document.createElement("span");
    text.className = "menu-row-text";
    const title = document.createElement("span");
    title.className = "menu-row-title";
    title.textContent = agent.name;
    const desc = document.createElement("span");
    desc.className = "menu-row-desc";
    desc.textContent = `/work/${agent.id}`;
    text.append(title, desc);
    link.append(text);
    item.append(link);
    return item;
  };

  const show = (agents) => {
    const rows = agents.flatMap((a) =>
      a && typeof a.id === "string" && ID.test(a.id)
        ? [{ id: a.id, name: typeof a.name === "string" && a.name.trim() ? a.name : a.id }]
        : [],
    );
    if (!rows.length) return finish("Lettuce has no agents yet.");
    list.replaceChildren(...rows.map(row));
    list.hidden = false;
    finish("");
  };

  timer = setTimeout(() => finish("Lettuce did not answer."), 15000);
  socket.onmessage = (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!frame || typeof frame !== "object") return;
    if (frame.type === "__bff_hello") {
      socket.send(JSON.stringify({ type: "agent_list", request_id: REQUEST, query: { limit: 100 } }));
    } else if (frame.request_id === REQUEST) {
      if (frame.success === false || !Array.isArray(frame.agents)) {
        finish("Lettuce could not list your agents.");
      } else {
        show(frame.agents);
      }
    }
  };
  socket.onclose = () => finish("Could not reach Lettuce.");
})();
