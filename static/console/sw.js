// Service worker(P2):只做「離線時給一個誠實的畫面」,不做資料快取。
//
// 刻意**不快取 API 回應** —— 這個 app 的全部價值是「主機現在是什麼狀態」,
// 給你一份三分鐘前的 session 列表比明說連不上更糟。所以:
//   ‧ 靜態殼(HTML/CSS/JS/icon)→ 預先快取,離線也開得起來
//   ‧ 其他所有請求 → 一律直接走網路,失敗就讓前端自己處理
const SHELL = "pocket-console-shell-v1";
const ASSETS = [
  "/console",
  "/console/static/style.css",
  "/console/static/sse.js",
  "/console/static/app.js",
  "/console/static/filebrowser.js",
  "/console/static/changes.js",
  "/console/static/terminal.js",
  "/console/static/memory.js",
  "/console/static/icon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const isShell = e.request.mode === "navigate" ||
                  url.pathname.startsWith("/console/static/");
  if (!isShell) return;                       // API/WS:不碰,直接走網路
  e.respondWith(
    fetch(e.request)
      .then((r) => {                          // 網路優先:殼也要能更新
        const copy = r.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy)).catch(() => {});
        return r;
      })
      .catch(() => caches.match(e.request).then((hit) =>
        hit || caches.match("/console")))
  );
});
