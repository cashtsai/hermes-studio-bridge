// 記憶檢索(2-1)。打 bridge 薄代理 /app/v2/memory/search(→ pocket-memoryd)。
// 跨所有 session(CC/CX/人格)找「當時做了什麼、改了哪、結論是什麼」。
"use strict";
(function () {
  const TOKEN_KEY = "pocket.console.token";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let token = null;
  const PROV = { claude_code: "CC", codex: "CX", hermes: "人格" };
  function tok() { return token || (token = localStorage.getItem(TOKEN_KEY)); }

  async function run() {
    const q = $("mem-q").value.trim();
    if (!q) return;
    const prov = $("mem-prov").value;
    const box = $("mem-results");
    box.innerHTML = '<div class="placeholder">搜尋中…</div>';
    try {
      const url = "/app/v2/memory/search?q=" + encodeURIComponent(q) + "&k=20" +
                  (prov ? "&provider=" + prov : "");
      const r = await fetch(url, { headers: { "Authorization": "Bearer " + tok() } });
      if (r.status === 404) { box.innerHTML = '<div class="placeholder err">記憶檢索未啟用（POCKET_MEMORY_ENABLED=0）</div>'; return; }
      if (r.status === 503) { box.innerHTML = '<div class="placeholder err">pocket-memoryd 未啟動（127.0.0.1:8082）</div>'; return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      render(d.hits || [], q);
    } catch (e) { box.innerHTML = '<div class="placeholder err">搜尋失敗：' + esc(e.message) + "</div>"; }
  }

  function render(hits, q) {
    const box = $("mem-results");
    if (!hits.length) { box.innerHTML = '<div class="placeholder">「' + esc(q) + '」沒有命中</div>'; return; }
    box.innerHTML = '<div class="mem-count">' + hits.length + ' 筆</div>' + hits.map((h) => {
      const when = h.ts ? new Date(h.ts * 1000).toLocaleString() : "?";
      return '<div class="mem-hit">' +
        '<div class="mem-head"><span class="pill">' + esc(PROV[h.provider] || h.provider) + "</span>" +
        '<span class="mem-title">' + esc(h.title || h.skey) + "</span>" +
        '<span class="mem-when">' + esc(when) + "</span></div>" +
        '<div class="mem-snip">' + hl(esc(h.snippet || ""), q) + "</div>" +
        '<div class="mem-prov">' + esc(h.role_summary || "") + " · " + esc(h.skey) + "</div></div>";
    }).join("");
  }
  function hl(text, q) {
    try { return text.replace(new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi"), "<mark>$1</mark>"); }
    catch (_) { return text; }
  }

  function wire() {
    $("mem-go").addEventListener("click", run);
    $("mem-q").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    if (window.PocketConsoleMode) {
      window.PocketConsoleMode.onMode = (m) => { if (m === "memory") setTimeout(() => $("mem-q").focus(), 50); };
    }
  }
  wire();
})();
