// Registers this page's own service worker (scope: the vault's pages, separate from
// Lettuce's) and manages Web Push on the Notifications page. Classic script
// registration: a module worker's script fetch carries no cookies, and
// Cloudflare Access would redirect it to the login page.
"use strict";

const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content ?? "";
// The vault's path prefix (config.PREFIX), from the page.
const base = meta("base");

function urlBase64ToBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": meta("csrf") },
    body: JSON.stringify(body ?? {}),
    credentials: "same-origin",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function registration() {
  return navigator.serviceWorker.register(`${base}/sw.js`, { scope: `${base}/` });
}

async function setupPush() {
  const box = document.getElementById("push");
  if (!box) return;
  const status = document.getElementById("push-status");
  const enable = document.getElementById("push-enable");
  const disable = document.getElementById("push-disable");
  const test = document.getElementById("push-test");
  const show = (text, subscribed) => {
    status.textContent = text;
    enable.hidden = subscribed;
    disable.hidden = !subscribed;
    test.hidden = !subscribed;
  };
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    status.textContent = "This browser cannot receive notifications here. On iPhone or iPad, open this page from the Home Screen.";
    return;
  }
  const reg = await registration();
  await navigator.serviceWorker.ready;
  const current = await reg.pushManager.getSubscription();
  if (current) {
    // Re-send it: the server may have dropped it, and this is idempotent.
    await post(`${base}/push/subscribe`, current.toJSON()).catch(() => {});
  }
  show(current ? "Notifications are on for this device." : "Notifications are off for this device.", !!current);

  enable.addEventListener("click", async () => {
    try {
      if ((await Notification.requestPermission()) !== "granted") {
        show("Notifications are blocked for this site in the browser settings.", false);
        return;
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBytes(meta("vapid")),
      });
      await post(`${base}/push/subscribe`, sub.toJSON());
      show("Notifications are on for this device.", true);
    } catch (e) {
      show(`Could not enable notifications: ${e.message}`, false);
    }
  });
  disable.addEventListener("click", async () => {
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await post(`${base}/push/unsubscribe`, { endpoint: sub.endpoint }).catch(() => {});
      await sub.unsubscribe();
    }
    show("Notifications are off for this device.", false);
  });
  test.addEventListener("click", async () => {
    try {
      const { delivered } = await post(`${base}/push/test`);
      status.textContent = `Test sent to ${delivered} device(s).`;
    } catch (e) {
      status.textContent = `Test failed: ${e.message}`;
    }
  });
}

if ("serviceWorker" in navigator) {
  registration().catch(() => {});
}
// Copy buttons and delete confirmations (no inline handlers: the CSP forbids them).
function setupPageHelpers() {
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copy);
      if (!target) return;
      try {
        await navigator.clipboard.writeText(target.textContent);
        const label = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = label; }, 1500);
      } catch {
        button.textContent = "Select and copy";
      }
    });
  }
  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  }
}

// Groups folded away stay so in this browser: only their ids ("form:identity:Address")
// are stored, never a value. Storage may be unavailable (a private window); then
// every group just opens again on the next page.
const CLOSED_GROUPS = "vault-closed-groups";

function setupGroups() {
  let closed;
  try {
    closed = new Set(JSON.parse(localStorage.getItem(CLOSED_GROUPS) || "[]"));
  } catch {
    closed = new Set();
  }
  const save = () => {
    try {
      localStorage.setItem(CLOSED_GROUPS, JSON.stringify([...closed]));
    } catch {}
  };
  for (const group of document.querySelectorAll("details[data-group]")) {
    if (closed.has(group.dataset.group)) group.open = false;
    group.addEventListener("toggle", () => {
      if (group.open) closed.delete(group.dataset.group);
      else closed.add(group.dataset.group);
      save();
    });
  }
  // A required field in a closed group: open the group so the browser can point at it.
  document.addEventListener("invalid", (event) => {
    const group = event.target.closest?.("details[data-group]");
    if (group) group.open = true;
  }, true);
}

// Like Bitwarden's eye button: shows what was typed into a masked box, and masks it
// again. It changes only how the box draws its text.
function setupMaskToggles() {
  for (const button of document.querySelectorAll(".mask-toggle")) {
    const box = button.previousElementSibling;
    if (!box) continue;
    button.addEventListener("click", () => {
      const shown = button.getAttribute("aria-pressed") !== "true";
      if (box.tagName === "TEXTAREA") box.classList.toggle("masked", !shown);
      else box.type = shown ? "text" : "password";
      button.setAttribute("aria-pressed", String(shown));
      button.textContent = shown ? "Hide" : "Show";
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  setupPageHelpers();
  setupGroups();
  setupMaskToggles();
  setupPush().catch((e) => {
    const status = document.getElementById("push-status");
    if (status) status.textContent = `Notifications unavailable: ${e.message}`;
  });
});
