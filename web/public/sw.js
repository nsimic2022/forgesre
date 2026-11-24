// ForgeSRE service worker: makes the UI installable on a phone (on-call) and shows a clear
// "offline" page instead of the browser's error when the appliance cannot be reached.
// It never caches /api, sign-in or incident data: everything live always comes from Core.
const SHELL = "forgesre-shell-v2";
const OFFLINE = "/app/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll([OFFLINE, "/app/icon-192.png"])));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || req.mode !== "navigate") return; // assets and API: straight to the network
  event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
});

// Web push (Settings › Appearance › Push to this device; off unless an Admin turned on
// notifications.push.enabled). The message is short: severity, incident number, host, title.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: "ForgeSRE", body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "ForgeSRE", {
      body: data.body || "",
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      requireInteraction: true,
      icon: "/app/icon-192.png",
      badge: "/app/icon-192.png",
      data: { url: data.url || "/app/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/app/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const win of wins) {
        if (win.url.includes("/app/") && "focus" in win) {
          win.navigate(url);
          return win.focus();
        }
      }
      return self.clients.openWindow(url);
    }),
  );
});
