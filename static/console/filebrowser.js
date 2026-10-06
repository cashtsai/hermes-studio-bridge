// 檔案瀏覽(1-2)。掛在 /console 內,與 Agents 操作台共用同一份 token。
// 列目錄走 /app/v2/fs/list;檔案內容走 /file?path=&token=(img/video/iframe 無法
// 帶 header,所以用 ?token=;bridge 的 /file 已加 scoped fallback)。
"use strict";
(function () {
  const TOKEN_KEY = "pocket.console.token";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let token = null, curPath = "", curParent = null, booted = false;

  function tok() { return token || (token = localStorage.getItem(TOKEN_KEY)); }
  function fileURL(path) { return "/file?path=" + encodeURIComponent(path) + "&token=" + encodeURIComponent(tok()); }

  const ICON = { dir: "📁", pdf: "📄", video: "🎬", markdown: "📝", html: "🌐",
                 image: "🖼️", audio: "🎵", text: "📃", file: "📎" };

  function fmtSize(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString().slice(0, 5);
  }

  async function fsList(path) {
    const url = "/app/v2/fs/list" + (path ? ("?path=" + encodeURIComponent(path)) : "");
    const r = await fetch(url, { headers: { "Authorization": "Bearer " + tok() } });
    if (r.status === 401) { $("fb-list").innerHTML = '<div class="placeholder err">token 失效</div>'; throw new Error("401"); }
    if (r.status === 404) { $("fb-list").innerHTML = '<div class="placeholder err">檔案瀏覽未啟用（POCKET_FILEBROWSER_ENABLED=0）</div>'; throw new Error("404"); }
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  async function navigate(path) {
    try {
      const d = await fsList(path);
      curPath = d.path; curParent = d.parent;
      renderCrumb(d.path);
      renderList(d.entries, d.truncated);
      $("fb-up").disabled = !d.parent;
    } catch (e) { /* 訊息已寫進 list */ }
  }

  function renderCrumb(path) {
    $("fb-crumb").textContent = path;
  }

  function renderList(entries, truncated) {
    const el = $("fb-list");
    if (!entries.length) { el.innerHTML = '<div class="placeholder">（空資料夾）</div>'; return; }
    let html = entries.map((e, i) =>
      '<div class="fb-row" data-i="' + i + '">' +
      '<span class="fb-ico">' + (ICON[e.kind] || ICON.file) + "</span>" +
      '<span class="fb-name">' + esc(e.name) + "</span>" +
      '<span class="fb-meta">' + esc(fmtSize(e.size)) + "　" + esc(fmtTime(e.mtime)) + "</span></div>"
    ).join("");
    if (truncated) html += '<div class="placeholder">（超過 2000 筆，已截斷）</div>';
    el.innerHTML = html;
    el.querySelectorAll(".fb-row").forEach((n) => n.addEventListener("click", () => {
      const e = entries[+n.dataset.i];
      if (e.is_dir) navigate(e.path); else preview(e);
    }));
  }

  async function preview(e) {
    const pv = $("fb-preview");
    const url = fileURL(e.path);
    if (e.kind === "image") {
      pv.innerHTML = '<div class="pv-head">' + esc(e.name) + '</div><img class="pv-img" src="' + esc(url) + '">';
    } else if (e.kind === "video") {
      pv.innerHTML = '<div class="pv-head">' + esc(e.name) + '</div><video class="pv-video" controls preload="metadata" src="' + esc(url) + '"></video>';
    } else if (e.kind === "pdf" || e.kind === "html") {
      pv.innerHTML = '<div class="pv-head">' + esc(e.name) + '</div><iframe class="pv-frame" src="' + esc(url) + '"></iframe>';
    } else if (e.kind === "markdown" || e.kind === "text") {
      pv.innerHTML = '<div class="pv-head">' + esc(e.name) + '</div><div class="pv-text">載入中…</div>';
      try {
        const txt = await (await fetch(url, { headers: { "Authorization": "Bearer " + tok() } })).text();
        const box = pv.querySelector(".pv-text");
        if (e.kind === "markdown") box.innerHTML = mdLite(txt);
        else box.textContent = txt;
      } catch (_) { pv.querySelector(".pv-text").textContent = "讀取失敗"; }
    } else {
      pv.innerHTML = '<div class="pv-head">' + esc(e.name) + '</div><div class="placeholder">這個格式不預覽 — <a href="' + esc(url) + '" target="_blank">開啟/下載</a>（' + esc(fmtSize(e.size)) + '）</div>';
    }
  }

  // 極簡 markdown(標題/粗體/行內 code/連結/換行)—— 第一刀夠用,之後可換正式 parser
  function mdLite(s) {
    return esc(s)
      .replace(/^### (.*)$/gm, "<h3>$1</h3>")
      .replace(/^## (.*)$/gm, "<h2>$1</h2>")
      .replace(/^# (.*)$/gm, "<h1>$1</h1>")
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[(.+?)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')
      .replace(/\n/g, "<br>");
  }

  // 模式切換
  function setMode(mode) {
    document.querySelectorAll(".mode-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === mode));
    $("main").classList.toggle("hidden", mode !== "agents");
    $("files-view").classList.toggle("hidden", mode !== "files");
    if (mode === "files" && !booted) { booted = true; navigate(""); }
  }

  function wire() {
    document.querySelectorAll(".mode-btn").forEach((b) =>
      b.addEventListener("click", () => setMode(b.dataset.mode)));
    $("fb-up").addEventListener("click", () => { if (curParent) navigate(curParent); });
  }
  wire();
})();
