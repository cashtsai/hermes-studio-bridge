// Pocket 桌面 Web 主控台(1-1)。純 vanilla JS,無 build。
// 控制路徑全部走 bridge 既有 v2 端點。見 SPEC_DESKTOP_CONSOLE_20261006。
"use strict";

const TOKEN_KEY = "pocket.console.token";
const $ = (id) => document.getElementById(id);

const state = {
  token: null,
  sessions: [],
  current: null,      // 目前選的 session id
  cards: [],          // 目前 session 的卡片
  latestSeq: 0,
  stopStream: null,   // 當前 session SSE 的中止函式
  atts: [],           // 待送附件 [{kind,filename,mime,data}]
};

// ───────────────────────── bootstrap(取得 token)─────────────────────────
async function bootstrap() {
  const saved = localStorage.getItem(TOKEN_KEY);
  if (saved) { state.token = saved; return true; }

  const boot = new URLSearchParams(location.search).get("boot");
  if (!boot) { fatalAuth("缺 boot code —— 請用桌面印出的 /console?boot=… 連結開啟"); return false; }

  try {
    // 1) 用 boot code 鑄一次性配對碼
    const qr = await (await fetch("/pair/qr.json?boot=" + encodeURIComponent(boot))).json();
    if (!qr.ok) throw new Error(qr.error || "鑄碼失敗");
    // payload = pocket://pair?...&code=<claim-code>
    const code = new URLSearchParams(qr.payload.split("?")[1]).get("code");
    if (!code) throw new Error("payload 無 code");
    // 2) 裸 /pair/claim 換 pdev- token(不需 bearer/帳號)
    const claim = await (await fetch("/pair/claim", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, device_name: "Console", platform: "web" }),
    })).json();
    if (!claim.token) throw new Error("claim 無 token");
    state.token = claim.token;
    localStorage.setItem(TOKEN_KEY, claim.token);
    // 3) 把 ?boot= 從網址列抹掉(別留在歷史/Referer)
    history.replaceState(null, "", location.pathname);
    return true;
  } catch (err) {
    fatalAuth("配對失敗:" + err.message);
    return false;
  }
}

function fatalAuth(msg) {
  $("session-list").innerHTML = '<div class="empty err">' + esc(msg) + "</div>";
  setConn(false, "未配對");
}

// ───────────────────────── API helpers ─────────────────────────
async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "Authorization": "Bearer " + state.token }, opts.headers || {});
  const r = await fetch(path, opts);
  if (r.status === 401) { localStorage.removeItem(TOKEN_KEY); fatalAuth("token 失效,請重新用 boot 連結開啟"); throw new Error("401"); }
  return r;
}
async function apiJSON(path, opts) { return (await api(path, opts)).json(); }

// ───────────────────────── session 列表 ─────────────────────────
const PROVIDER_LABEL = { claude_code: "CC", codex: "CX", hermes: "人格", openclaw: "OC", cc2: "CC" };

async function loadSessions() {
  try {
    const d = await apiJSON("/app/v2/sessions");
    state.sessions = d.sessions || [];
    const degraded = d.degraded_providers || [];
    renderSessions(degraded);
    renderAwaiting();
    renderApproval();
    setConn(true, degraded.length ? ("降級:" + degraded.join(",")) : "已連線");
  } catch (err) {
    setConn(false, "取列表失敗");
  }
}

function renderSessions(degraded) {
  const el = $("session-list");
  if (!state.sessions.length) { el.innerHTML = '<div class="empty">沒有 session</div>'; return; }
  const byProv = {};
  for (const s of state.sessions) (byProv[s.provider] = byProv[s.provider] || []).push(s);
  let html = "";
  for (const prov of Object.keys(byProv)) {
    const dim = degraded.includes(prov) ? " degraded" : "";
    html += '<div class="prov-group"><div class="prov-head' + dim + '">' +
            esc(PROVIDER_LABEL[prov] || prov) + (dim ? " ⚠" : "") + "</div>";
    for (const s of byProv[prov]) {
      const active = s.id === state.current ? " active" : "";
      const locked = s.meta && s.meta.locked ? ' 🔒' : "";
      html += '<div class="sess' + active + '" data-id="' + esc(s.id) + '">' +
              '<div class="sess-name">' + esc(s.title || s.id) + locked + "</div>" +
              '<div class="sess-sub"><span class="st st-' + esc(s.status || "idle") + '">' +
              esc(s.status || "idle") + "</span> " + esc(s.subtitle || "") + "</div></div>";
    }
    html += "</div>";
  }
  el.innerHTML = html;
  el.querySelectorAll(".sess").forEach((n) =>
    n.addEventListener("click", () => openSession(n.dataset.id)));
}

// ───────────────────────── 卡片流 ─────────────────────────
async function openSession(id) {
  if (state.stopStream) { state.stopStream(); state.stopStream = null; }
  state.current = id;
  state.cards = []; state.latestSeq = 0;
  renderSessions([]);                      // 重畫 active 標記
  writeHash(id);                           // 深連結:可加書籤 / 重整不掉頁
  $("stream-head").classList.remove("hidden");
  $("composer").classList.remove("hidden");
  const s = state.sessions.find((x) => x.id === id) || {};
  $("sh-provider").textContent = PROVIDER_LABEL[s.provider] || s.provider || "";
  $("sh-name").textContent = s.title || id;
  updateStatusChip(s.status, s.meta);
  renderApproval();
  $("cards").innerHTML = '<div class="placeholder">載入卡片…</div>';

  try {
    const snap = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/cards?limit=100");
    state.cards = snap.cards || [];
    state.latestSeq = snap.latest_seq || 0;
    renderCards();
    subscribeCards(id);
  } catch (err) {
    $("cards").innerHTML = '<div class="placeholder err">卡片載入失敗:' + esc(err.message) + "</div>";
  }
}

// ───────────────────────── 待審核(P0:唯一的硬阻塞)─────────────────────────
// 偵測不需要新端點:pending 待審核已經在 session.meta 裡
//   ‧ meta.prompt   —— CC 的選單/權限提示 {title, options:[{key,label}]}
//   ‧ meta.approval —— 統一的審核列(hermes / openclaw / CC watcher 建的)
// 動作走 /app/v2/sessions/{id}/approve,三 provider 的 body 形狀不同:
//   ‧ CC      {key}
//   ‧ CX      {approve: bool}
//   ‧ hermes / openclaw {approval_id, key|approve}
//   ‧ 有 approval_id 時一律帶上 —— bridge 會走統一的 _approval_decide_core
function currentSession() {
  return state.sessions.find((x) => x.id === state.current) || null;
}

function approvalIdOf(meta) {
  const a = meta && meta.approval;
  if (!a) return null;
  return a.aid || a.id || a.approval_id || null;
}

function renderApproval() {
  const bar = $("approval");
  const s = currentSession();
  const meta = (s && s.meta) || {};
  const prompt = meta.prompt;
  const aid = approvalIdOf(meta);
  const opts = (prompt && Array.isArray(prompt.options) && prompt.options.length)
    ? prompt.options : null;

  if (!opts && !aid) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  $("ap-title").textContent =
    (prompt && prompt.title) || (meta.approval && meta.approval.title) || "等待你放行";

  const box = $("ap-actions");
  box.innerHTML = "";
  if (opts) {
    // 有選項就照選項畫(CC 的數字選單/權限提示;label 可能是任何語言)
    opts.slice(0, 8).forEach((o) => {
      const b = document.createElement("button");
      b.className = "ap-btn";
      b.textContent = (o.key ? o.key + ". " : "") + (o.label || "");
      b.addEventListener("click", () => decide({ key: String(o.key) }, b));
      box.appendChild(b);
    });
  } else {
    const yes = document.createElement("button");
    yes.className = "ap-btn ap-yes"; yes.textContent = "核准";
    yes.addEventListener("click", () => decide({ approve: true }, yes));
    const no = document.createElement("button");
    no.className = "ap-btn ap-no"; no.textContent = "否決";
    no.addEventListener("click", () => decide({ approve: false }, no));
    box.append(yes, no);
  }
}

async function decide(choice, btn) {
  const id = state.current;
  if (!id) return;
  const aid = approvalIdOf((currentSession() || {}).meta);
  const body = Object.assign({}, choice, aid ? { approval_id: aid } : {});
  // 樂觀:整排先停用,避免連點送出兩個決定
  const box = $("ap-actions");
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  if (btn) btn.classList.add("ap-busy");
  try {
    const r = await api("/app/v2/sessions/" + encodeURIComponent(id) + "/approve", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error((e.error && e.error.message) || ("HTTP " + r.status));
    }
    toast("已送出決定");
    $("approval").classList.add("hidden");   // 事件流會把真實狀態補回來
    loadSessions();
  } catch (err) {
    toast("放行失敗:" + err.message);
    box.querySelectorAll("button").forEach((b) => (b.disabled = false));
    if (btn) btn.classList.remove("ap-busy");
  }
}

// 跨 session 彙總:有幾件在等你。資料來源同上(sessions 的 meta/status),
// 不必另外打 /app/v1/approvals —— 列表本來就每次都抓。
function renderAwaiting() {
  const n = state.sessions.filter((s) =>
    s.status === "waiting_approval" || (s.meta && (s.meta.approval || s.meta.prompt))).length;
  const el = $("awaiting");
  $("awaiting-n").textContent = String(n);
  el.classList.toggle("hidden", n === 0);
}

// 點徽章 → 跳到第一條在等的 session
function jumpToAwaiting() {
  const s = state.sessions.find((x) =>
    x.status === "waiting_approval" || (x.meta && (x.meta.approval || x.meta.prompt)));
  if (s) openSession(s.id);
}

function subscribeCards(id) {
  const url = "/app/v2/sessions/" + encodeURIComponent(id) +
              "/events?since_seq=" + state.latestSeq + "&profile=phone";
  state.stopStream = sseStream(url, state.token, {
    onEvent: (evt) => {
      const c = evt.data && evt.data.card;
      if (c) { upsertCard(c); if (evt.data.seq) state.latestSeq = evt.data.seq; }
      // status 事件:更新狀態 chip
      if (evt.data && evt.data.status) updateStatusChip(evt.data.status.phase, evt.data.status);
    },
    onGone: () => openSession(id),         // 410 → 冷載重來
    onError: () => {},                     // sse.js 內建重連
  });
}

function upsertCard(card) {
  const i = state.cards.findIndex((c) => c.id === card.id);
  if (i >= 0) state.cards[i] = card; else state.cards.push(card);
  renderCards();
}

function cardText(c) {
  const b = c.body || {};
  return b.text || b.fallback_text || b.typed_text || (typeof c.body === "string" ? c.body : "");
}

function renderCards() {
  const el = $("cards");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  if (!state.cards.length) { el.innerHTML = '<div class="placeholder">（還沒有卡片）</div>'; return; }
  el.innerHTML = state.cards.map((c) => {
    const role = c.role === "user" ? "user" : "agent";
    const txt = cardText(c);
    const kind = c.kind && c.kind !== "text" ? '<span class="kind">' + esc(c.kind) + "</span>" : "";
    return '<div class="card ' + role + '">' + kind +
           '<div class="card-body">' + esc(txt).replace(/\n/g, "<br>") + "</div></div>";
  }).join("");
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function updateStatusChip(phase, meta) {
  const chip = $("sh-status");
  const locked = meta && meta.locked;
  const label = locked ? "🔒 被佔用" : (phase || "idle");
  chip.textContent = label;
  chip.className = "status-chip st-" + esc(phase || "idle");
  $("interrupt").classList.toggle("hidden", !(phase === "run" || phase === "running" || phase === "busy"));
}

// ───────────────────────── 送出 / 中斷 ─────────────────────────
async function sendInput(text) {
  if (!state.current) return;
  if (!text.trim() && !state.atts.length) return;
  const cid = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
  try {
    const r = await api("/app/v2/sessions/" + encodeURIComponent(state.current) + "/input", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ content: text, client_id: cid },
        state.atts.length ? { attachments: state.atts } : {})),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error((e.error && e.error.message) || ("HTTP " + r.status)); }
    $("input").value = ""; autosize();
    state.atts = []; renderAtts();
  } catch (err) { toast("送出失敗:" + err.message); }
}

async function interrupt() {
  if (!state.current) return;
  try {
    const r = await api("/app/v2/sessions/" + encodeURIComponent(state.current) + "/interrupt", { method: "POST" });
    if (r.status === 409) { toast("目前沒有進行中的回合"); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    toast("已送出中斷");
  } catch (err) { toast("中斷失敗:" + err.message); }
}

// ───────────────────────── UI 小工具 ─────────────────────────
function setConn(ok, text) {
  $("conn").className = "conn " + (ok ? "ok" : "bad");
  $("conn-text").textContent = text;
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
let toastTimer = null;
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 3000);
}
function autosize() {
  const ta = $("input"); ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

// ───────────────────────── 綁定 + 啟動 ─────────────────────────
function wire() {
  $("refresh").addEventListener("click", loadSessions);
  $("awaiting").addEventListener("click", jumpToAwaiting);
  $("newsess-open").addEventListener("click", () => $("newsess").classList.remove("hidden"));
  $("newsess-cancel").addEventListener("click", () => $("newsess").classList.add("hidden"));
  $("newsess-go").addEventListener("click", createSession);
  $("pal-q").addEventListener("input", (e) => renderPalette(e.target.value));
  $("palette").addEventListener("click", (e) => { if (e.target.id === "palette") closePalette(); });
  wireKeys();
  $("attach").addEventListener("click", () => $("file-pick").click());
  $("file-pick").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  const dz = $("stream");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dropping"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dropping"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); dz.classList.remove("dropping");
    if (state.current) addFiles(e.dataTransfer.files);
  });
  $("input").addEventListener("paste", (e) => {
    const f = Array.from(e.clipboardData.files || []);
    if (f.length) { e.preventDefault(); addFiles(f); }   // 直接貼截圖
  });
  $("interrupt").addEventListener("click", interrupt);
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); sendInput($("input").value); });
  $("input").addEventListener("input", autosize);
  $("input").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); sendInput($("input").value); }
  });
}


// ───────────────────────── P1:深連結(可加書籤)─────────────────────────
// #session=<id> —— 把常用的工作釘成書籤,開筆電直接進去;重新整理也不會掉回首頁。
function hashSession() {
  const m = /(?:^|&)session=([^&]+)/.exec(location.hash.slice(1));
  return m ? decodeURIComponent(m[1]) : null;
}
function writeHash(id) {
  const next = id ? "#session=" + encodeURIComponent(id) : "";
  if (location.hash !== next) history.replaceState(null, "", location.pathname + location.search + next);
}

// ───────────────────────── P1:鍵盤操作(筆電的本體優勢)──────────────────
// ⌘K 快速切 session / j·k 上下移動 / a·d 核准·否決 / ⌘↵ 送出(既有)
// 刻意不攔在輸入框裡打字的狀況 —— 只有焦點不在 input/textarea 時才作用。
function typingNow() {
  const t = document.activeElement;
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
}
function moveSelection(delta) {
  const ids = state.sessions.map((s) => s.id);
  if (!ids.length) return;
  const i = ids.indexOf(state.current);
  const next = ids[Math.max(0, Math.min(ids.length - 1, (i < 0 ? 0 : i + delta)))];
  if (next && next !== state.current) openSession(next);
}
function firstApprovalButton(wantYes) {
  const btns = Array.from($("ap-actions").querySelectorAll("button"));
  if (!btns.length) return null;
  const hit = btns.find((b) => b.classList.contains(wantYes ? "ap-yes" : "ap-no"));
  return hit || (wantYes ? btns[0] : btns[btns.length - 1]);
}
function wireKeys() {
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); openPalette(); return;
    }
    if (e.key === "Escape") { closePalette(); return; }
    if (typingNow()) return;
    if (e.key === "j") { e.preventDefault(); moveSelection(1); }
    else if (e.key === "k") { e.preventDefault(); moveSelection(-1); }
    else if (e.key === "a" || e.key === "d") {
      const b = firstApprovalButton(e.key === "a");
      if (b && !b.disabled) { e.preventDefault(); b.click(); }
    }
  });
}

// ⌘K 快速切換面板
function openPalette() {
  const p = $("palette");
  p.classList.remove("hidden");
  const q = $("pal-q"); q.value = ""; renderPalette(""); q.focus();
}
function closePalette() { $("palette").classList.add("hidden"); }
function renderPalette(q) {
  const needle = q.trim().toLowerCase();
  const hits = state.sessions.filter((s) =>
    !needle || (s.title || s.id).toLowerCase().includes(needle) ||
    (s.provider || "").toLowerCase().includes(needle)).slice(0, 12);
  $("pal-list").innerHTML = hits.length
    ? hits.map((s, i) => '<div class="pal-item' + (i === 0 ? " sel" : "") +
        '" data-id="' + esc(s.id) + '"><span class="pill">' +
        esc(PROVIDER_LABEL[s.provider] || s.provider || "") + "</span>" +
        esc(s.title || s.id) + "</div>").join("")
    : '<div class="empty">沒有符合的 session</div>';
  $("pal-list").querySelectorAll(".pal-item").forEach((n) =>
    n.addEventListener("click", () => { closePalette(); openSession(n.dataset.id); }));
}

// ───────────────────────── P1:開新工作 ─────────────────────────
async function createSession() {
  const kind = $("new-kind").value;
  const name = $("new-name").value.trim();
  const wd = $("new-dir").value.trim();
  if (kind === "cc" && !name) { toast("CC 需要 session 名稱"); return; }
  try {
    if (kind === "cc") {
      const r = await api("/ccsessions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, workdir: wd }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
    } else {
      const r = await api("/codexsessions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: name || "新工作", cwd: wd || undefined }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
    }
    toast("已開新工作");
    $("newsess").classList.add("hidden");
    $("new-name").value = ""; $("new-dir").value = "";
    setTimeout(loadSessions, 600);     // 給 bridge 一點時間把它登記進列表
  } catch (err) { toast("開新工作失敗:" + err.message); }
}


// ───────────────────────── P1:附件 ─────────────────────────
// input 的 attachments 直接收 {kind,filename,mime,data(dataURI)} —— 所以
// **不需要碰 multipart 上傳端點**,瀏覽器讀成 data URI 就能送。
// 代價是整包塞進 JSON body,所以設上限;大檔請用檔案瀏覽面的路徑引用。
const ATT_MAX_BYTES = 8 * 1024 * 1024;

function attKind(mime) {
  if (!mime) return "file";
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  return "file";
}

function addFiles(files) {
  Array.from(files || []).forEach((f) => {
    if (f.size > ATT_MAX_BYTES) { toast(f.name + " 超過 8MB,改用檔案路徑引用"); return; }
    const fr = new FileReader();
    fr.onload = () => {
      state.atts.push({ kind: attKind(f.type), filename: f.name,
                        mime: f.type || "application/octet-stream", data: fr.result });
      renderAtts();
    };
    fr.onerror = () => toast("讀取 " + f.name + " 失敗");
    fr.readAsDataURL(f);
  });
}

function renderAtts() {
  const box = $("atts");
  box.classList.toggle("hidden", !state.atts.length);
  box.innerHTML = state.atts.map((a, i) =>
    '<span class="att">' + esc(a.filename) +
    '<button class="att-x" data-i="' + i + '" title="移除">×</button></span>').join("");
  box.querySelectorAll(".att-x").forEach((b) =>
    b.addEventListener("click", () => { state.atts.splice(Number(b.dataset.i), 1); renderAtts(); }));
}

async function main() {
  wire();
  if (!(await bootstrap())) return;
  await loadSessions();
  const want = hashSession();              // 深連結:帶 #session= 就直接開
  if (want && state.sessions.some((s) => s.id === want)) openSession(want);
  // 全域 SSE:任何 session 狀態變動都刷新列表(無 410 問題,見規格)
  sseStream("/app/v2/events?follow=true&since_seq=0", state.token, {
    onEvent: () => { /* 去抖:每次事件觸發輕量刷新 */ scheduleListRefresh(); },
    onError: () => setConn(false, "事件流斷線,重連中…"),
    onOpen: () => {},
  });
}
let listTimer = null;
function scheduleListRefresh() { clearTimeout(listTimer); listTimer = setTimeout(loadSessions, 800); }

main();
