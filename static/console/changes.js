// 變更檢視(P0-3):回答「agent 到底改了什麼」。
//
// 為什麼需要兩支端點:`/diff` 是**單檔**的(必須帶 ?path=),bridge 原本沒有
// 「列出改動檔」的路由 —— 本輪補了 `/changes`(git status --porcelain)。
// 這一頁先列檔、點了再逐檔抓 diff,避免一次把整個 repo 的 diff 拉下來。
"use strict";

let chFiles = [];
let chSessionID = null;

async function chLoad() {
  const id = state.current;
  const lab = document.getElementById("ch-session");
  const list = document.getElementById("ch-list");
  if (!id) {
    lab.textContent = "先在 Agents 選一條 session";
    list.innerHTML = '<div class="empty">沒有選取的 session</div>';
    return;
  }
  chSessionID = id;
  const s = state.sessions.find((x) => x.id === id);
  lab.textContent = (s && s.title) || id;
  list.innerHTML = '<div class="empty">讀取變更…</div>';
  try {
    const d = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/changes");
    chFiles = d.files || [];
    if (!chFiles.length) {
      list.innerHTML = '<div class="empty">工作目錄乾淨,沒有待定變更</div>';
      document.getElementById("ch-diff").innerHTML =
        '<div class="placeholder">沒有變更</div>';
      return;
    }
    list.innerHTML = chFiles.map((f, i) =>
      '<div class="ch-item" data-i="' + i + '">' +
      '<span class="ch-st ch-st-' + esc(f.untracked ? "new" : "mod") + '">' +
      esc(f.untracked ? "新" : f.status) + "</span>" +
      '<span class="ch-path">' + esc(f.path) + "</span></div>").join("") +
      (d.truncated ? '<div class="empty">(只列前 400 個)</div>' : "");
    list.querySelectorAll(".ch-item").forEach((n) =>
      n.addEventListener("click", () => chOpen(Number(n.dataset.i), n)));
  } catch (err) {
    // 人格沒有工作目錄 → bridge 回 400,這是預期狀況,不是故障
    list.innerHTML = '<div class="empty">' +
      esc(err.message === "400" ? "這個 provider 沒有工作目錄(人格)" : "讀取失敗") +
      "</div>";
  }
}

async function chOpen(i, node) {
  const f = chFiles[i];
  if (!f) return;
  document.querySelectorAll(".ch-item.active").forEach((n) => n.classList.remove("active"));
  if (node) node.classList.add("active");
  const box = document.getElementById("ch-diff");
  box.textContent = "讀取 diff…";
  try {
    const d = await apiJSON("/app/v2/sessions/" + encodeURIComponent(chSessionID) +
                            "/diff?path=" + encodeURIComponent(f.path));
    const text = d.diff || "";
    if (!text.trim()) { box.innerHTML = '<div class="placeholder">這個檔沒有待定變更</div>'; return; }
    // 逐行著色:+ 綠 / - 紅 / @@ 藍。不引入任何語法高亮套件。
    box.innerHTML = text.split("\n").map((ln) => {
      let cls = "";
      if (ln.startsWith("+++") || ln.startsWith("---")) cls = "d-meta";
      else if (ln.startsWith("@@")) cls = "d-hunk";
      else if (ln.startsWith("+")) cls = "d-add";
      else if (ln.startsWith("-")) cls = "d-del";
      return '<span class="' + cls + '">' + esc(ln) + "</span>";
    }).join("\n") + (d.truncated ? '\n<span class="d-meta">(diff 已截斷)</span>' : "");
  } catch (err) {
    box.textContent = "讀取 diff 失敗:" + err.message;
  }
}

document.getElementById("ch-reload").addEventListener("click", chLoad);

// ── 第三欄(P2):寬螢幕右側常駐的「這輪改了什麼」 ──
// 與「變更」分頁共用同一支端點,但只列檔名;點了就跳到變更分頁看內容。
// 窄螢幕不抓 —— 看不到的東西沒必要花一次請求。
const RAIL_MIN_WIDTH = 1280;
function railVisible() { return window.innerWidth >= RAIL_MIN_WIDTH; }

async function railLoad() {
  const box = document.getElementById("rail-list");
  if (!box || !railVisible()) return;
  const id = state.current;
  if (!id) { box.innerHTML = '<div class="empty">選一條 session</div>'; return; }
  box.innerHTML = '<div class="empty">讀取中…</div>';
  try {
    const d = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/changes");
    const fs = d.files || [];
    if (!fs.length) { box.innerHTML = '<div class="empty">工作目錄乾淨</div>'; return; }
    box.innerHTML = fs.slice(0, 60).map((f) =>
      '<div class="rail-item" data-path="' + esc(f.path) + '">' +
      '<span class="ch-st ch-st-' + (f.untracked ? "new" : "mod") + '">' +
      esc(f.untracked ? "新" : f.status) + "</span>" +
      '<span class="ch-path">' + esc(f.path) + "</span></div>").join("");
    box.querySelectorAll(".rail-item").forEach((n) =>
      n.addEventListener("click", () => {
        window.PocketConsoleMode.set("changes");
        const i = chFiles.findIndex((x) => x.path === n.dataset.path);
        if (i >= 0) chOpen(i, null);
      }));
  } catch (err) {
    // 人格沒有工作目錄 → 400,是預期狀況
    box.innerHTML = '<div class="empty">此 provider 無工作目錄</div>';
  }
}
document.getElementById("rail-reload").addEventListener("click", railLoad);
window.addEventListener("resize", () => { if (railVisible()) railLoad(); });
window.PocketConsoleRail = { load: railLoad };

// 切到「變更」分頁就自動抓一次(與 memory.js 同一套 onMode 掛法)。
// 用鏈接而不是覆寫:memory.js 也掛在同一個點上,直接指派會把它蓋掉。
(function () {
  const prev = window.PocketConsoleMode && window.PocketConsoleMode.onMode;
  if (!window.PocketConsoleMode) return;
  window.PocketConsoleMode.onMode = function (mode) {
    if (typeof prev === "function") prev(mode);
    if (mode === "changes") chLoad();
  };
})();
