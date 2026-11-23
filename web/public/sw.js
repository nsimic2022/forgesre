// ForgeSRE service worker: makes the UI installable on a phone (on-call) and shows a clear
// "offline" page instead of the browser's error when the appliance cannot be reached.
// It never caches /api, sign-in or incident data: everything live always comes from Core.
const SHELL = "forgesre-shell-v1";
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
