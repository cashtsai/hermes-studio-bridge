// The same /console URL can return a private login challenge or a workbench.
// Never cache navigations (including query strings), even for offline launch.
const SHELL = "pocket-console-shell-v8";
const ASSETS = ["style.css", "sse.js", "md.js", "app.js", "transcript.js", "filebrowser.js", "changes.js",
  "terminal.js", "memory.js", "icon-192.png"].map(name => "/console/static/" + name);

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys
    .filter(key => key.startsWith("pocket-console-shell-") && key !== SHELL)
    .map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (req.mode === "navigate" && url.pathname === "/console") {
    e.respondWith(fetch(req).catch(() => new Response("離線中。請連上網路再開啟 Pocket。", {
      status: 503, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    })));
  } else if (!url.search && ASSETS.includes(url.pathname)) {
    e.respondWith(fetch(req).catch(() => caches.open(SHELL).then(cache => cache.match(req))));
  }
});
