// Service worker for the vault's pages only (its scope). It shows approval notifications and opens
// the request page when one is tapped. It has no fetch handler: nothing is
// cached, so every page load goes through Cloudflare Access.
"use strict";

const SCOPE = self.registration.scope;
const ICON = new URL("icon-192.png", SCOPE).href;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Vault request", {
      body: data.body || "An agent is waiting for your approval.",
      tag: data.tag || "vault-request",
      icon: ICON,
      badge: ICON,
      requireInteraction: true,
      data: { url: data.url || SCOPE },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || SCOPE, self.location.origin);
  // Only ever open this app's own pages.
  const url = target.href.startsWith(SCOPE) ? target.href : SCOPE;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (client.url.startsWith(SCOPE) && "navigate" in client) {
          const navigated = await client.navigate(url);
          if (navigated) return navigated.focus();
        }
      }
      return self.clients.openWindow(url);
    })(),
  );
});
