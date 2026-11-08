// Pocket 桌面 Web 主控台(1-1)。純 vanilla JS,無 build。
// 控制路徑全部走 bridge 既有 v2 端點。見 SPEC_DESKTOP_CONSOLE_20261006。
"use strict";

const TOKEN_KEY = "pocket.console.token";
const LOGOUT_EVENT = "pocket.console.logout";
const RESUME_KEY = "pocket.console.personal-work";
let ownerFingerprint = "", firstList = true;
const $ = (id) => document.getElementById(id);

const state = {
  token: null,
  sessions: [],
  host: "", degraded: [], listHealth: "loading",
  current: null,      // 目前選的 session id
  cards: [],          // 目前 session 的卡片
  latestSeq: 0,
  stopStream: null,   // 當前 session SSE 的中止函式
  view: 0,            // A -> B -> A must invalidate the first A response too.
};
const drafts = new Map();  // Page memory only, never persisted on a borrowed device.
function draftFor(id) {
  if (!drafts.has(id)) drafts.set(id, { text: "", atts: [], reading: 0, pending: null, notice: "" });
  return drafts.get(id);
}
function currentView(view) { return !signingOut && view === state.view; }

// The preference belongs to this login, not just this browser origin.
function forgetWork() {
  try { localStorage.removeItem(RESUME_KEY); } catch (_) {}
}
function personalDevice() {
  try {
    return !signingOut && !borrowed() && !!ownerFingerprint && state.listHealth === "ready" &&
      localStorage.getItem(TOKEN_KEY) === state.token;
  } catch (_) { return false; }
}
function resumePreference() {
  if (!personalDevice()) return null;
  try {
    const pref = JSON.parse(localStorage.getItem(RESUME_KEY));
    return pref?.owner === ownerFingerprint ? pref : null;
  } catch (_) { return null; }
}
function rememberWork(id) {
  if (!personalDevice()) return;
  try { localStorage.setItem(RESUME_KEY, JSON.stringify({owner:ownerFingerprint, id})); }
  catch (_) { toast("這個瀏覽器無法記住工作"); }
}
async function identifyPersonalLogin() {
  try {
    if (borrowed() || !crypto.subtle || localStorage.getItem(TOKEN_KEY) !== state.token) return;
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(state.token));
    ownerFingerprint = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2,"0")).join("");
  } catch (_) {} // No secure storage/crypto: still usable without personal resume.
}

// ───────────────────────── bootstrap(取得 token)─────────────────────────
const CLAIM_COOKIE = "pocket_console_claim";

/** 讀一枚 cookie(claim cookie 刻意非 HttpOnly,就是要給這裡讀)。 */
function readCookie(name) {
  const hit = document.cookie.split("; ").find((c) => c.startsWith(name + "="));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : "";
}

/** 用完就地抹掉 —— 碼本來就是一次性,留著只是多一個可被 XSS 撿走的東西。 */
function dropCookie(name) {
  document.cookie = name + "=; Max-Age=0; path=/console; SameSite=Strict";
}

/** 一次性配對碼 → pdev- token(/pair/claim 不需 bearer)。 */
async function claimToken(code) {
  const r = await fetch("/pair/claim", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, device_name: "Console", platform: "web" }),
  });
  const claim = await r.json().catch(() => ({}));
  if (!claim.token) throw new Error(claim.detail || "claim 無 token");
  return claim;
}

async function bootstrap() {
  // ① 主路徑(2026-10-19 起):在輸入頁打對碼 → bridge 303 回本頁,並種一枚
  //    60 秒的一次性 claim cookie。網址上**不留碼**,所以要從 cookie 取。
  const cookieCode = readCookie(CLAIM_COOKIE);
  // ② 相容路徑:舊的 /console?boot=… 連結(書籤/捷徑)仍要能用。
  const boot = new URLSearchParams(location.search).get("boot");
  const remote = document.body.dataset.consoleAccess === "remote";
  // Never resume an unscoped token left by an older release on a borrowed PC.
  if (remote) { forgetWork(); localStorage.removeItem(TOKEN_KEY); }

  if (!cookieCode && !boot) {
    const saved = sessionStorage.getItem(TOKEN_KEY) || (!remote && localStorage.getItem(TOKEN_KEY));
    if (saved) { state.token = saved; return true; }
    location.replace("/console?signin=1");
    return false;
  }

  try {
    let code = cookieCode;
    if (!code) {
      // 舊路徑才需要自己鑄碼;cookie 那枚是 bridge 已經鑄好的,不可重鑄。
      const qr = await (await fetch("/pair/qr.json?boot=" + encodeURIComponent(boot))).json();
      if (!qr.ok) throw new Error(qr.error || "鑄碼失敗");
      code = new URLSearchParams(qr.payload.split("?")[1]).get("code");
      if (!code) throw new Error("payload 無 code");
    }
    const claim = await claimToken(code);
    if (remote && claim.scope !== "console-remote") throw new Error("主機尚未支援受限瀏覽器登入");
    if (claim.scope === "console-remote") document.body.dataset.consoleAccess = "remote";
    state.token = claim.token;
    forgetWork();
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
    (claim.scope === "console-remote" ? sessionStorage : localStorage).setItem(TOKEN_KEY, state.token);
    sessionStorage.removeItem(LOGOUT_EVENT);
    if (cookieCode) dropCookie(CLAIM_COOKIE);
    // 舊路徑:把 ?boot= 從網址列抹掉(別留在歷史/Referer)
    if (boot) history.replaceState(null, "", location.pathname);
    return true;
  } catch (err) {
    if (cookieCode) dropCookie(CLAIM_COOKIE);   // 壞/過期的碼不要留著重試
    fatalAuth("配對失敗:" + err.message + "（重新整理可回到輸入頁重試）");
    return false;
  }
}

function fatalAuth(msg) {
  $("session-list").innerHTML = '<div class="empty err">' + esc(msg) + "</div>";
  $("home-notice").textContent = msg;
  setConn(false, "未配對");
}

let signingOut = false;
let leaveRequest = false;
async function leaveConsole(broadcast = false) {
  if (leaveRequest) return;
  signingOut = true;
  forgetWork();
  writeHash(null);
  leaveRequest = true;
  $("app").classList.add("hidden");
  $("app").inert = true;
  $("logout-screen").classList.remove("hidden");
  $("logout-state").textContent = "正在登出這台電腦…";
  $("retry-logout").hidden = true;
  sessionStorage.setItem(LOGOUT_EVENT, "pending");
  if (broadcast) localStorage.setItem(LOGOUT_EVENT, String(Date.now()));
  if (state.stopStream) state.stopStream();
  clearTimeout(snapshotRetry);
  if (state.stopList) state.stopList();
  stopBlink();
  try {
    if (state.token) {
      const r = await fetch("/console/logout", {
        method: "POST", headers: { Authorization: "Bearer " + state.token },
        credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(8000),
      });
      const result = await r.json();
      if (!(r.ok && result.revoked === true) && !(r.status === 401 && result.revoked === false)) {
        throw new Error("logout not acknowledged");
      }
    }
    localStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(LOGOUT_EVENT);
    state.token = null;
    location.replace("/console?signin=1");
  } catch (_) {
    $("logout-state").textContent = "畫面已鎖定，尚未確認登出。請連線後重試，或在手機上撤銷這台電腦。";
    $("retry-logout").hidden = false;
  } finally { leaveRequest = false; }
}

// ───────────────────────── API helpers ─────────────────────────
async function api(path, opts) {
  if (signingOut) throw new Error("已鎖定");
  opts = opts || {};
  opts.headers = Object.assign({ "Authorization": "Bearer " + state.token }, opts.headers || {});
  const r = await fetch(path, opts);
  if (r.status === 401) { void leaveConsole(); throw new Error("401"); }
  return r;
}
async function apiJSON(path, opts) {
  const r = await api(path, opts);
  if (!r.ok) throw Object.assign(new Error(String(r.status)), { status: r.status, code: r.headers.get("X-Error-Code") });
  return r.json();
}

// ───────────────────────── session 列表 ─────────────────────────
const PROVIDER_LABEL = { claude_code: "CC", codex: "CX", hermes: "人格", openclaw: "OC", cc2: "CC" };

let listRequest = 0;
async function loadSessions() {
  const request = ++listRequest;
  try {
    const d = await apiJSON("/app/v2/sessions");
    if (request !== listRequest || signingOut) return;
    if (!Array.isArray(d.sessions)) throw new Error("無效的工作清單");
    if (d.console_scope === "console-remote") {
      document.body.dataset.consoleAccess = "remote";
      forgetWork();
      if (localStorage.getItem(TOKEN_KEY) === state.token) {
        sessionStorage.setItem(TOKEN_KEY, state.token);
        localStorage.removeItem(TOKEN_KEY);
      }
    }
    state.sessions = d.sessions;
    state.host = d.host_name || location.hostname;
    state.listHealth = "ready";
    const degraded = state.degraded = d.degraded_providers || [];
    if (borrowed() && state.current && !canUse("replay")) {
      window.PocketConsoleMode?.set("agents"); showHome();
    }
    const gainedChanges = !document.body.classList.contains("can-view-changes") && canUse("changes");
    syncControls();
    if (borrowed() && gainedChanges && state.current) window.PocketConsoleRail?.load();
    if (borrowed() && state.current && state.snapshotView === state.view) {
      if (!canUse("follow") && state.stopStream) {
        state.stopStream(); state.stopStream = null; $("stream-health").textContent = "對話快照";
      } else if (canUse("follow") && !state.stopStream) subscribeCards(state.current, state.view);
    }
    renderSessions();
    renderHome();
    renderAwaiting();
    renderApproval();
    setConn(true, degraded.length ? ("降級:" + degraded.join(",")) : "已連線");
    const pref = resumePreference();
    const want = !state.current && (hashSession() || (firstList && pref?.id));
    firstList = false;
    if (want && state.sessions.some(s => s.id === want)) openSession(want);
  } catch (err) {
    if (request !== listRequest || signingOut) return;
    state.listHealth = "failed";
    syncControls();
    renderHome(); renderSessions(); renderAwaiting(); renderApproval();
    setConn(false, "取列表失敗");
  }
}

function renderSessions() {
  const el = $("session-list");
  if (!state.sessions.length) {
    el.textContent = state.listHealth === "failed" || state.degraded.length ? "清單暫時不完整" : "沒有 session";
    return;
  }
  const byProv = {};
  for (const s of state.sessions) (byProv[s.provider] = byProv[s.provider] || []).push(s);
  let html = "";
  for (const prov of Object.keys(byProv)) {
    const dim = state.degraded.includes(prov) ? " degraded" : "";
    html += '<div class="prov-group"><div class="prov-head' + dim + '">' +
            esc(PROVIDER_LABEL[prov] || prov) + (dim ? " ⚠" : "") + "</div>";
    for (const s of byProv[prov]) {
      const active = s.id === state.current ? " active" : "";
      const locked = s.meta && s.meta.locked ? ' 🔒' : "";
      html += '<div class="sess' + active + '" data-id="' + esc(s.id) + '">' +
              '<div class="sess-name">' + esc(s.title || s.id) + locked + "</div>" +
              '<div class="sess-sub"><span class="st st-' + esc(s.status || "idle") + '">' +
              esc(workPhase(s)) + "</span> " + esc(s.subtitle || "") + "</div></div>";
    }
    html += "</div>";
  }
  el.innerHTML = html;
  el.querySelectorAll(".sess").forEach((n) =>
    n.addEventListener("click", () => openSession(n.dataset.id)));
}

function activityTime(value) {
  const ms = typeof value === "number" ? (value < 1e12 ? value * 1000 : value)
    : typeof value === "string" && /^\d{4}-\d\d-\d\dT/.test(value) ? Date.parse(value) : NaN;
  return ms > 0 && Number.isFinite(new Date(ms).getTime()) ? ms : 0;
}

function workMatches(s, query) {
  const fields = [s.title, s.id, s.provider, PROVIDER_LABEL[s.provider], s.subtitle, s.meta?.workdir,
    s.provider === "claude_code" || s.provider === "cc2" ? "Claude Code" : ""];
  return query.toLowerCase().trim().split(/\s+/).every(term =>
    fields.some(field => String(field || "").toLowerCase().includes(term)));
}

const PHASE_LABEL = { idle: "閒置", done: "已完成", running: "執行中", run: "執行中",
  busy: "執行中", failed: "失敗", stalled: "停滯", waiting_approval: "等待核准", queued: "排隊中" };
const activityFormat = new Intl.DateTimeFormat("zh-TW",
  { year:"numeric", month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit", hour12:false });
function workPhase(s) {
  if (s.meta?.locked) return "被佔用";
  if (s.status === "waiting_approval" && !actionable(s)) {
    const a = s.meta?.approval, aid = approvalIdOf(s.meta), exp = activityTime(a?.expires_at);
    if (aid && handledApproval.get(s.id) === aid || a?.status && a.status !== "pending") return "核准已結束";
    if (exp && exp <= Date.now()) return "核准已逾時";
    return "待主機確認";
  }
  return PHASE_LABEL[s.status] || s.status || "狀態未知";
}
function workRow(s) {
  const time = activityTime(s.last_event_at);
  const detail = [s.meta?.workdir || s.subtitle, s.id].filter(Boolean).join(" · ");
  const phase = workPhase(s);
  return '<button type="button" class="work-row" data-id="' + esc(s.id) + '">' +
    '<span class="work-provider">' + esc(PROVIDER_LABEL[s.provider] || s.provider) + '</span>' +
    '<span class="work-text"><strong>' + esc(s.title || s.id) + '</strong><small>' + esc(detail) +
    '</small></span><span class="work-state">' + esc(phase) +
    (time ? '<small>' + esc(activityFormat.format(time)) + '</small>' : '') + '</span></button>';
}

function renderHome() {
  if (state.current || signingOut) return;
  const ready = state.listHealth === "ready", partial = state.degraded.length > 0;
  $("home-host").textContent = state.host ? "主機 · " + state.host : "";
  $("home-notice").textContent = !ready ? (state.listHealth === "loading" ? "正在取得主機清單…" :
    "無法更新主機清單；先前的資料可能已過時。") : partial
    ? "部分服務暫時無法更新：" + state.degraded.join("、") : "";
  const q = $("home-q").value.trim();
  $("home-groups").classList.toggle("hidden", !!q);
  $("home-results").classList.toggle("hidden", !q);
  const rows = (id, sessions, empty) => {
    const el = $(id);
    const html = sessions.length ? sessions.map(workRow).join("") : '<p class="work-empty">' + esc(empty) + '</p>';
    if (el.innerHTML !== html) el.innerHTML = html;
  };
  if (q) {
    rows("home-results", state.sessions.filter(s => workMatches(s, q)),
      ready && !partial ? "沒有符合的工作" : "尚無符合的可用資料");
    return;
  }
  const recent = state.sessions.filter(s => activityTime(s.last_event_at))
    .sort((a,b) => activityTime(b.last_event_at) - activityTime(a.last_event_at));
  rows("recent-list", recent.slice(0, 5), ready && !partial ? "尚無可確認的活動時間" : "尚無最近活動資料");
  rows("pending-list", state.sessions.filter(actionable), ready && !partial
    ? "目前沒有可處理的核准" : "待處理清單尚不完整");
}

function showHome() {
  ++state.view;
  if (state.stopStream) { state.stopStream(); state.stopStream = null; }
  state.current = null; state.cards = []; state.latestSeq = 0;
  syncControls();
  writeHash(null);
  for (const id of ["stream-head", "approval", "cards", "composer", "delivery", "atts"]) $(id).classList.add("hidden");
  $("cards").replaceChildren();
  $("main").classList.add("at-home");
  $("handoff").classList.remove("hidden");
  renderSessions(); renderHome(); $("home-q").focus();
}

// ───────────────────────── 卡片流 ─────────────────────────
let snapshotRetry;
function openSession(id, preserveMode = false) {
  if (signingOut) return;
  if (borrowed() && !state.sessions.find(s => s.id === id)?.capabilities?.includes("replay")) {
    toast("這台電腦無權讀取此對話"); return;
  }
  const view = ++state.view;
  clearTimeout(snapshotRetry);
  if (state.stopStream) { state.stopStream(); state.stopStream = null; }
  state.current = id;
  if (resumePreference()) rememberWork(id);
  state.cards = []; state.latestSeq = 0;
  if (!preserveMode) window.PocketConsoleMode?.set("agents");
  $("main").classList.remove("at-home");
  $("handoff").classList.add("hidden");
  $("cards").classList.remove("hidden");
  renderSessions();
  writeHash(id);                           // 深連結:可加書籤 / 重整不掉頁
  $("stream-head").classList.remove("hidden");
  $("composer").classList.remove("hidden");
  $("stream-health").textContent = "連線中";
  showDraft(id);
  const s = state.sessions.find((x) => x.id === id) || {};
  $("sh-provider").textContent = PROVIDER_LABEL[s.provider] || s.provider || "";
  $("sh-name").textContent = s.title || id;
  updateStatusChip(s.status, s.meta);
  syncControls();
  renderApproval();
  if (window.PocketConsoleRail) window.PocketConsoleRail.load();
  $("cards").innerHTML = '<div class="placeholder">載入卡片…</div>';
  void loadCardSnapshot(id, view);
}

async function loadCardSnapshot(id, view, delay = 1000) {
  if (!currentView(view)) return;
  try {
    const snap = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/cards?limit=100",
      { cache: "no-store", signal: AbortSignal.timeout(15000) });
    if (!currentView(view)) return;
    if (!Array.isArray(snap.cards) || !Number.isSafeInteger(snap.latest_seq) || snap.latest_seq < 0)
      throw new Error("無效的卡片回應");
    state.cards = snap.cards || [];
    state.latestSeq = snap.latest_seq || 0;
    renderCards();
    if (snap.status && snap.status.phase) updateStatusChip(snap.status.phase, snap.status);
    confirmEcho(id, state.cards);
    state.snapshotView = view;
    if (canUse("follow")) subscribeCards(id, view);
    else $("stream-health").textContent = "對話快照";
  } catch (err) {
    if (!currentView(view)) return;
    if (err.status >= 400 && err.status < 500 && ![408, 429].includes(err.status)) {
      $("stream-health").textContent = "卡片無法讀取：" + err.status;
      $("cards").replaceChildren();
      return;
    }
    $("stream-health").textContent = "連線中斷，正在補接";
    snapshotRetry = setTimeout(() => loadCardSnapshot(id, view, Math.min(delay * 2, 15000)), delay);
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

function borrowed() { return document.body.dataset.consoleAccess === "remote"; }
function canUse(cap) {
  return !borrowed() || !!(state.listHealth === "ready" && currentSession()?.capabilities?.includes(cap));
}
function syncControls() {
  $("personal-work").hidden = !personalDevice();
  $("remember-work").checked = !!resumePreference();
  if (!borrowed()) return;
  const changes = canUse("changes");
  if (!changes && document.body.classList.contains("can-view-changes")) {
    for (const id of ["rail-list","ch-list","ch-diff"]) $(id).replaceChildren();
  }
  document.body.classList.toggle("can-view-changes", changes);
  if (!changes && !$("changes-view").classList.contains("hidden")) window.PocketConsoleMode?.set("agents");
  if (state.current) {
    $("composer").classList.toggle("hidden", !canUse("input"));
    $("attach").classList.toggle("hidden", !canUse("attachments"));
    $("interrupt").classList.toggle("hidden", !(canUse("interrupt") && state.busy));
    $("read-only").classList.toggle("hidden", canUse("input"));
    $("read-only").textContent = state.listHealth === "failed" ? "清單未更新，暫停操作" : "這台電腦只能閱讀此對話";
  } else $("read-only").classList.add("hidden");
}

function approvalIdOf(meta) {
  const a = meta && meta.approval;
  if (!a) return null;
  return a.aid || a.id || a.approval_id || null;
}

const deciding = new Set();
const handledApproval = new Map();
let approvalExpiry;
function actionable(s) {
  if (!s || state.listHealth !== "ready" || state.degraded.includes(s.provider)) return false;
  if ((borrowed() || Array.isArray(s.capabilities)) && !s.capabilities?.includes("approve")) return false;
  const a = s.meta?.approval, aid = approvalIdOf(s.meta);
  if (a?.status && a.status !== "pending") return false;
  const exp = activityTime(a?.expires_at);
  if (exp && exp <= Date.now()) return false;
  if (aid) return handledApproval.get(s.id) !== JSON.stringify([aid, s.meta.approval?.q_index ?? null]);
  return document.body.dataset.consoleAccess !== "remote" && !!s.meta?.prompt?.options?.length;
}
function renderApproval() {
  clearTimeout(approvalExpiry);
  const bar = $("approval");
  const s = currentSession();
  const meta = (s && s.meta) || {};
  const prompt = meta.prompt;
  const aid = approvalIdOf(meta);
  const index = meta.approval?.q_index;
  const choices = borrowed() ? meta.console_options : (prompt?.options?.length ? prompt.options : meta.approval?.options);
  const opts = Array.isArray(choices) && choices.length ? choices : null;

  const expires = activityTime(meta.approval?.expires_at);
  if (!actionable(s)) { bar.classList.add("hidden"); return; }
  if (expires) approvalExpiry = setTimeout(() => { renderApproval(); renderAwaiting(); renderHome(); }, Math.min(Math.max(1, expires - Date.now() + 1), 2147483647));
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
      b.addEventListener("click", () => decide(s.id, aid, index, { key: String(o.key) }, b));
      box.appendChild(b);
    });
  } else {
    const yes = document.createElement("button");
    yes.className = "ap-btn ap-yes"; yes.textContent = "核准";
    yes.addEventListener("click", () => decide(s.id, aid, index, { approve: true }, yes));
    const no = document.createElement("button");
    no.className = "ap-btn ap-no"; no.textContent = "否決";
    no.addEventListener("click", () => decide(s.id, aid, index, { approve: false }, no));
    box.append(yes, no);
  }
  if (deciding.has(s.id)) box.querySelectorAll("button").forEach(b => b.disabled = true);
}

async function decide(id, aid, index, choice, btn) {
  if (!id || id !== state.current || deciding.has(id) || signingOut || !actionable(currentSession())) return;
  if (!aid && document.body.dataset.consoleAccess === "remote") return;
  deciding.add(id);
  const body = Object.assign({}, choice, aid ? { approval_id: aid } : {});
  if (Number.isInteger(index)) body.expected_q_index = index;
  const step = JSON.stringify([aid, index ?? null]);
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
      if (aid && r.status === 409 && ["STALE_QUESTION", "APPROVAL_NOT_PENDING"].includes(e.error?.code))
        handledApproval.set(id, step);
      throw new Error((e.error && e.error.message) || ("HTTP " + r.status));
    }
    const receipt = await r.json();
    if (aid) handledApproval.set(id, step);
    if (!signingOut && state.current === id) toast(receipt.idempotent ? "這項核准已處理" : "已送出決定");
  } catch (err) {
    if (!signingOut && state.current === id) toast("無法確認決定：" + err.message);
  } finally {
    deciding.delete(id);
    if (!signingOut) { renderApproval(); renderAwaiting(); renderHome(); loadSessions(); }
  }
}

// 跨 session 彙總:有幾件在等你。資料來源同上(sessions 的 meta/status),
// 不必另外打 /app/v1/approvals —— 列表本來就每次都抓。
function renderAwaiting() {
  const n = state.sessions.filter(actionable).length;
  const el = $("awaiting");
  $("awaiting-n").textContent = String(n);
  el.classList.toggle("hidden", n === 0);
  notifyAwaiting(n);
}

// 點徽章 → 跳到第一條在等的 session
function jumpToAwaiting() {
  const s = state.sessions.find(actionable);
  if (s) openSession(s.id);
}

function subscribeCards(id, view) {
  let resyncing = false;
  function resync() {
    if (!currentView(view) || resyncing) return;
    resyncing = true;
    // A list refresh must not resubscribe with the expired cursor during snapshot backoff.
    state.snapshotView = null;
    if (state.stopStream) { state.stopStream(); state.stopStream = null; }
    $("stream-health").textContent = "正在同步";
    void loadCardSnapshot(id, view);
  }
  const url = () => "/app/v2/sessions/" + encodeURIComponent(id) +
              "/events?since_seq=" + state.latestSeq + "&profile=phone";
  state.stopStream = sseStream(url, state.token, {
    onEvent: (evt) => {
      if (!currentView(view)) return;
      const envelope = evt.data || {}, payload = envelope.data || {}, seq = envelope.seq;
      if (!Number.isSafeInteger(seq)) return;
      if (envelope.type === "ping") {
        if (seq !== state.latestSeq) resync();
        return;
      }
      if (seq <= state.latestSeq) return;
      if (seq !== state.latestSeq + 1) { resync(); return; }
      if (envelope.type === "card.upsert" && payload.card) {
        upsertCard(payload.card);
        confirmEcho(id, [payload.card]);
      } else if (envelope.type === "session.status") {
        updateStatusChip(payload.phase, payload);
        scheduleListRefresh();
      }
      state.latestSeq = seq;
    },
    onGone: resync,
    onOpen: () => { if (currentView(view)) $("stream-health").textContent = ""; },
    onError: () => { if (currentView(view)) $("stream-health").textContent = "連線中斷，正在補接"; },
    onDenied: code => {
      if (!currentView(view)) return;
      state.stopStream = null;
      if (code === 401) void leaveConsole();
      else $("stream-health").textContent = "無權讀取";
    },
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

function safeHttpUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return (url.protocol === "http:" || url.protocol === "https:") ? esc(url.href) : "";
  } catch (e) { return ""; }
}

// Typed visual replies are additive.  Keep HTML blocks as text so an old or
// compromised provider payload cannot turn the web console into an HTML sink.
function visualReplyHtml(c) {
  const visual = (c.body || {}).visual;
  if (!visual || !Array.isArray(visual.blocks)) return "";
  const blocks = visual.blocks.map((b) => {
    if (!b || typeof b !== "object") return "";
    const type = String(b.type || "").toLowerCase();
    if (type === "image") {
      const url = safeHttpUrl(b.url || b.download_url);
      return url ? '<img class="visual-image" loading="lazy" src="' + url +
        '" alt="' + esc(b.alt || "圖片") + '">' : "";
    }
    if (type === "link" || type === "file") {
      const url = safeHttpUrl(b.url || b.download_url);
      if (!url) return "";
      const label = esc(b.label || b.filename || (type === "file" ? "檔案" : "開啟連結"));
      return '<a class="visual-link" href="' + url +
        '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    }
    if (type === "text" || type === "markdown" || type === "html") {
      const text = type === "html" ? (b.fallback_text || b.text || b.html) : b.text;
      return text ? '<div class="visual-text">' + esc(text).replace(/\n/g, "<br>") + "</div>" : "";
    }
    return "";
  }).filter(Boolean).join("");
  if (!blocks) return "";
  const fallback = visual.fallback_text ?
    '<div class="visual-fallback">' + esc(visual.fallback_text).replace(/\n/g, "<br>") + "</div>" : "";
  return '<div class="visual-reply">' + blocks + fallback + "</div>";
}

function cardBodyHtml(c) {
  const visual = visualReplyHtml(c);
  const text = cardText(c);
  const body = c.body || {};
  // visual cards normally carry fallback_text but no duplicate plain text.
  const plain = body.text || body.typed_text || (typeof body === "string" ? body : "");
  return visual + (visual ? (plain ? '<div class="visual-plain">' + esc(plain).replace(/\n/g, "<br>") + "</div>" : "")
                     : esc(text).replace(/\n/g, "<br>"));
}

// ───────────────────────── 主題(與 App 的 PocketPalettes 同名同值)─────────────
// App 端三個 preset:pocket(預設,奶油暖底)/ white / dark。console 不能讀手機上
// 的選擇(不同裝置),所以本機記一份,預設與 App 預設相同。
const THEMES = ["pocket", "white", "dark"];
const THEME_LABEL = { pocket: "Pocket", white: "White", dark: "Dark" };

function applyTheme(id) {
  const t = THEMES.includes(id) ? id : "pocket";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("pocket-console-theme", t); } catch (e) {}
  // PWA 的狀態列/標題列顏色跟著主題走(standalone 模式下看得出來)。
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.content = getComputedStyle(document.documentElement)
      .getPropertyValue("--bg").trim() || "#FFF2D6";
  }
  const btn = $("theme-btn");
  if (btn) btn.title = "主題:" + THEME_LABEL[t] + "（點擊切換，同 App 的三個主題）";
}

function cycleTheme() {
  const now = document.documentElement.dataset.theme || "pocket";
  applyTheme(THEMES[(THEMES.indexOf(now) + 1) % THEMES.length]);
}

// 散文類 kind 用無襯線讀;其餘(工具輸出/diff/log)才等寬。
const PROSE_KINDS = new Set(["text", "markdown"]);

function cardTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (isNaN(d)) return "";
  const hhmm = String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0");
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? hhmm : (d.getMonth() + 1) + "/" + d.getDate() + " " + hhmm;
}

// 依 turn_id 把卡片收成一個個回合;turn_id 缺漏時以「連續同 role」退而求其次,
// 至少不會把一來一往黏成一整片。
function groupTurns(cards) {
  const turns = [];
  for (const c of cards) {
    const role = c.role === "user" ? "user" : "agent";
    const key = c.turn_id != null ? "t:" + c.turn_id : null;
    const last = turns[turns.length - 1];
    const sameTurn = last && last.role === role &&
                     (key != null ? last.key === key : last.key == null);
    if (sameTurn) last.cards.push(c);
    else turns.push({ key, role, cards: [c] });
  }
  return turns;
}

function renderCards() {
  const el = $("cards");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  if (!state.cards.length) { el.innerHTML = '<div class="placeholder">（還沒有卡片）</div>'; return; }
  // 排序:優先用 ts(伺服器時間),缺的保持原插入順序 —— SSE 亂序到達時
  // 才不會把回合排錯。slice() 避免就地改動 state.cards。
  const ordered = state.cards
    .map((c, i) => ({ c, i }))
    .sort((a, b) => ((a.c.ts || 0) - (b.c.ts || 0)) || (a.i - b.i))
    .map((x) => x.c);

  el.innerHTML = groupTurns(ordered).map((t) => {
    const who = t.role === "user" ? "你" : "Agent";
    const time = cardTime(t.cards[t.cards.length - 1].ts);
    const meta = '<div class="turn-meta"><span class="turn-who">' + who + "</span>" +
                 (time ? '<span class="turn-time">' + esc(time) + "</span>" : "") + "</div>";
    const body = t.cards.map((c) => {
      const mono = PROSE_KINDS.has(c.kind || "text") ? "" : " mono";
      const streaming = c.final === false ? " streaming" : "";
      const kind = c.kind && c.kind !== "text"
        ? '<span class="kind">' + esc(c.kind) + "</span>" : "";
      return '<div class="card ' + t.role + mono + streaming + '">' + kind +
             '<div class="card-body">' + cardBodyHtml(c) +
             "</div></div>";
    }).join("");
    return '<div class="turn ' + t.role + '">' + meta + body + "</div>";
  }).join("");
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function updateStatusChip(phase, meta) {
  const chip = $("sh-status");
  const locked = meta && meta.locked;
  const label = locked ? "🔒 被佔用" : (phase || "idle");
  chip.textContent = label;
  chip.className = "status-chip st-" + esc(phase || "idle");
  state.busy = ["run", "running", "busy"].includes(phase);
  $("interrupt").classList.toggle("hidden", !state.busy || !canUse("interrupt"));
}

// ───────────────────────── 送出 / 中斷 ─────────────────────────
function showDraft(id) {
  if (signingOut || state.current !== id) return;
  const d = draftFor(id), pending = d.pending;
  $("input").value = d.text;
  $("input").readOnly = !!pending;
  $("attach").disabled = !!pending;
  $("send").disabled = !!pending || d.reading > 0;
  $("delivery").classList.toggle("hidden", !d.notice);
  $("delivery-text").textContent = d.notice;
  $("check-delivery").hidden = !pending || pending.phase !== "unknown";
  $("forget-delivery").hidden = $("check-delivery").hidden;
  renderAtts(); autosize();
}

function finishInput(id, d, notice) {
  d.pending = null; d.text = ""; d.atts = []; d.notice = notice;
  showDraft(id);
}

function confirmEcho(id, cards) {
  const d = draftFor(id);
  if (d.pending && cards.some(c => c.role === "user" && c.body?.client_id === d.pending.cid)) {
    finishInput(id, d, "主機已收到");
    return true;
  }
  return false;
}

async function checkDelivery() {
  const id = state.current, d = draftFor(id), p = d.pending;
  if (!p || p.phase !== "unknown") return;
  p.phase = "checking"; d.notice = "正在查詢送達紀錄…"; showDraft(id);
  try {
    const snap = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/cards?limit=100",
      { signal: AbortSignal.timeout(15000), cache: "no-store" });
    if (d.pending !== p || confirmEcho(id, snap.cards || [])) return;
    d.notice = "目前未找到收據，請核對原對話。未重新送出。";
  } catch (_) {
    if (d.pending !== p) return;
    d.notice = "暫時無法確認送達。未重新送出。";
  }
  p.phase = "unknown"; showDraft(id);
}

async function sendInput(text) {
  const id = state.current;
  if (!id || signingOut || !canUse("input")) return;
  const d = draftFor(id);
  if (d.atts.length && !canUse("attachments")) { toast("這條對話不允許附件，請先移除"); return; }
  if (d.pending || d.reading || (!text.trim() && !d.atts.length)) return;
  d.text = text;
  if (!navigator.onLine) { d.notice = "目前離線，訊息尚未送出"; showDraft(id); return; }
  const cid = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(
    new Uint32Array(4)), n => n.toString(16).padStart(8, "0")).join("");
  const p = { cid, phase: "sending" };
  d.pending = p; d.notice = "送出中…"; showDraft(id);
  try {
    const r = await api("/app/v2/sessions/" + encodeURIComponent(id) + "/input", {
      method: "POST", headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(90000),
      body: JSON.stringify({ content: text, client_id: cid, ...(d.atts.length ? { attachments: d.atts } : {}) }),
    });
    const ack = await r.json();
    if (d.pending !== p) return; // SSE may acknowledge first, freeing the next draft.
    if ([400, 403, 404, 413, 422].includes(r.status) && ack.error?.code) {
      d.pending = null; d.notice = "主機拒絕送出：" + (ack.error.message || r.status);
      showDraft(id); return;
    }
    if (!r.ok || ack.ok !== true || (ack.session_id && ack.session_id !== id)) throw new Error();
    finishInput(id, d, ack.queued || ack.delivery === "queued" ? "已排入佇列" : "主機已收到");
  } catch (_) {
    if (d.pending !== p) return;
    p.phase = "unknown"; d.notice = "送達狀態不明，請先確認。未重新送出。"; showDraft(id);
  }
}

async function interrupt() {
  if (!state.current || !canUse("interrupt")) return;
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
  $("remember-work").addEventListener("change", e => {
    if (e.target.checked) rememberWork(state.current);
    else if (personalDevice()) forgetWork();
    syncControls();
  });
  $("back-home").addEventListener("click", showHome);
  $("all-work").addEventListener("click", openPalette);
  $("home-refresh").addEventListener("click", loadSessions);
  $("home-q").addEventListener("input", renderHome);
  $("handoff").addEventListener("click", e => {
    const row = e.target.closest(".work-row");
    if (row) openSession(row.dataset.id);
  });
  $("logout").addEventListener("click", () => leaveConsole(true));
  $("retry-logout").addEventListener("click", () => leaveConsole());
  window.addEventListener("storage", e => {
    if (e.key === LOGOUT_EVENT && state.token) void leaveConsole();
    if (e.key === RESUME_KEY || e.key === TOKEN_KEY) syncControls();
  });
  window.addEventListener("pagehide", e => { if (e.persisted) $("app").classList.add("hidden"); });
  window.addEventListener("pageshow", e => { if (e.persisted) location.reload(); });
  $("refresh").addEventListener("click", loadSessions);
  $("awaiting").addEventListener("click", jumpToAwaiting);
  $("theme-btn").addEventListener("click", cycleTheme);
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
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); if (!composing) sendInput($("input").value); });
  $("input").addEventListener("input", () => {
    if (state.current) {
      const d = draftFor(state.current);
      d.text = $("input").value;
      if (!d.pending) { d.notice = ""; $("delivery").classList.add("hidden"); }
    }
    autosize();
  });
  $("check-delivery").addEventListener("click", checkDelivery);
  $("forget-delivery").addEventListener("click", () => {
    const id = state.current, d = draftFor(id);
    if (d.pending?.phase === "unknown" && confirm("指令可能已在執行。結束追蹤並清空這則草稿？不會重送或停止工作。"))
      finishInput(id, d, "");
  });
  $("input").addEventListener("keydown", (e) => {
    if (!imeKey(e) && (e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault(); if (!e.repeat) sendInput($("input").value);
    }
  });
}


// ───────────────────────── P1:深連結(可加書籤)─────────────────────────
// #session=<id> —— 把常用的工作釘成書籤,開筆電直接進去;重新整理也不會掉回首頁。
function hashSession() {
  const m = /(?:^|&)session=([^&]+)/.exec(location.hash.slice(1));
  try { return m ? decodeURIComponent(m[1]) : null; } catch (_) { return null; }
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
  if ($("approval").classList.contains("hidden") || !actionable(currentSession())) return null;
  const btns = Array.from($("ap-actions").querySelectorAll("button"));
  if (!btns.length) return null;
  const hit = btns.find((b) => b.classList.contains(wantYes ? "ap-yes" : "ap-no"));
  return hit || (wantYes ? btns[0] : btns[btns.length - 1]);
}
let composing = false;
const imeKey = e => composing || e.isComposing || e.keyCode === 229;
function wireKeys() {
  document.addEventListener("compositionstart", () => { composing = true; });
  document.addEventListener("compositionend", () => { composing = false; });
  window.addEventListener("blur", () => { composing = false; });
  document.addEventListener("keydown", (e) => {
    if (imeKey(e)) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); openPalette(); return;
    }
    if (e.key === "Escape") { closePalette(); return; }
    if (!$("palette").classList.contains("hidden")) {
      const rows = Array.from($("pal-list").querySelectorAll(".work-row"));
      const i = rows.indexOf(document.activeElement);
      if (rows.length && ["ArrowDown", "ArrowUp"].includes(e.key)) {
        e.preventDefault();
        rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))].focus();
      } else if (e.key === "Enter" && document.activeElement === $("pal-q") && rows.length) {
        e.preventDefault(); rows[0].click();
      }
      return;
    }
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
  const hits = state.sessions.filter(s => workMatches(s, needle));
  $("pal-list").innerHTML = hits.length
    ? hits.map(workRow).join("")
    : '<div class="empty">沒有符合的 session</div>';
  $("pal-list").querySelectorAll(".work-row").forEach((n) =>
    n.addEventListener("click", () => { closePalette(); openSession(n.dataset.id); }));
}

// ───────────────────────── P1:開新工作 ─────────────────────────
async function createSession() {
  if (borrowed()) return;
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
  const id = state.current;
  if (!id || signingOut || !canUse("input") || !canUse("attachments")) return;
  const d = draftFor(id);
  if (d.pending) return;
  Array.from(files || []).forEach((f) => {
    if (f.size > ATT_MAX_BYTES) { toast(f.name + " 超過 8MB,改用檔案路徑引用"); return; }
    const fr = new FileReader();
    d.reading++;
    fr.onload = () => {
      d.atts.push({ kind: attKind(f.type), filename: f.name,
                        mime: f.type || "application/octet-stream", data: fr.result });
    };
    fr.onerror = () => { d.notice = "讀取 " + f.name + " 失敗"; };
    fr.onloadend = () => { d.reading--; showDraft(id); };
    fr.readAsDataURL(f);
  });
  showDraft(id);
}

function renderAtts() {
  const box = $("atts");
  const d = draftFor(state.current);
  box.classList.toggle("hidden", !d.atts.length);
  box.innerHTML = d.atts.map((a, i) =>
    '<span class="att">' + esc(a.filename) +
    '<button class="att-x" ' + (d.pending ? "disabled " : "") + 'data-i="' + i + '" title="移除">×</button></span>').join("");
  box.querySelectorAll(".att-x").forEach((b) =>
    b.addEventListener("click", () => { if (!d.pending) d.atts.splice(Number(b.dataset.i), 1); renderAtts(); }));
}


// ───────────────────────── P2:用量／成本 ─────────────────────────
// 遠端工作最容易失去「燒了多少」的感覺。接既有 /app/v1/usage。
// 拿不到就整個不顯示(老 bridge / 功能未開)—— 不要在頂部留一塊壞掉的東西。
async function loadUsage() {
  try {
    const d = await apiJSON("/app/v1/usage");
    const parts = [];
    const cost = d.cost_usd ?? d.costUSD ?? (d.today && d.today.cost_usd);
    const tok = d.tokens ?? (d.today && d.today.tokens);
    if (typeof tok === "number") parts.push(tok >= 1000 ? (tok / 1000).toFixed(1) + "k tok" : tok + " tok");
    if (typeof cost === "number") parts.push("$" + cost.toFixed(2));
    const el = $("usage");
    el.textContent = parts.join(" · ");
    el.classList.toggle("hidden", !parts.length);
  } catch (err) { $("usage").classList.add("hidden"); }
}

// ───────────────────────── P2:通知 ─────────────────────────
// 有事等你,但你切到別的分頁了 —— 標題閃爍 + 一聲短音(零權限,不碰 Web Push)。
let titleTimer = null, baseTitle = document.title, lastAwaiting = 0;
function notifyAwaiting(n) {
  if (n === lastAwaiting) return;
  const rose = n > lastAwaiting;
  lastAwaiting = n;
  if (!rose) { stopBlink(); return; }
  if (!document.hidden) return;              // 人就在看,不用吵他
  beep();
  clearInterval(titleTimer);
  let on = false;
  titleTimer = setInterval(() => {
    on = !on;
    document.title = on ? "🔴 " + n + " 件待審核" : baseTitle;
  }, 1100);
}
function stopBlink() { clearInterval(titleTimer); titleTimer = null; document.title = baseTitle; }
document.addEventListener("visibilitychange", () => { if (!document.hidden) stopBlink(); });
function beep() {
  try {
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    const o = ac.createOscillator(), g = ac.createGain();
    o.type = "sine"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, ac.currentTime);
    g.gain.exponentialRampToValueAtTime(0.08, ac.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.32);
    o.connect(g).connect(ac.destination); o.start(); o.stop(ac.currentTime + 0.34);
    setTimeout(() => ac.close(), 600);
  } catch (e) { /* 瀏覽器不給音訊就算了,標題還是會閃 */ }
}

async function main() {
  wire();
  if (!(await bootstrap())) return;
  await identifyPersonalLogin();
  if (sessionStorage.getItem(LOGOUT_EVENT)) { await leaveConsole(); return; }
  await loadSessions();
  if (signingOut) return;
  if (document.body.dataset.consoleAccess !== "remote") {
    loadUsage(); setInterval(() => { if (!signingOut) loadUsage(); }, 120000);
  }
  if ("serviceWorker" in navigator) {
    // scope 要 /console —— sw.js 由 bridge 從 /console/sw.js 服務並帶
    // Service-Worker-Allowed 標頭(放在 static/ 下的話管不到本頁)。
    navigator.serviceWorker.register("/console/sw.js", { scope: "/console" })
      .catch(() => { /* 裝不起來不影響使用,不要吵使用者 */ });
  }
  // 全域 SSE:任何 session 狀態變動都刷新列表(無 410 問題,見規格)
  if (document.body.dataset.consoleAccess === "remote") {
    // The scoped session list is allowed; the cross-session event log is not.
    setInterval(() => { if (!document.hidden && !signingOut) loadSessions(); }, 10000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !signingOut) loadSessions(); });
    return;
  }
  let listCursor = 0;
  state.stopList = sseStream(() => "/app/v2/events?follow=true&since_seq=" + listCursor, state.token, {
    onEvent: ({data}) => {
      if (!Number.isSafeInteger(data?.seq) || data.seq <= listCursor) return;
      listCursor = data.seq;
      scheduleListRefresh();
    },
    onError: () => setConn(false, "事件流斷線,重連中…"),
    onOpen: () => {},
    onDenied: code => { if (code === 401) void leaveConsole(); else setConn(false, "無權讀取事件"); },
  });
}
let listTimer = null;
function scheduleListRefresh() { clearTimeout(listTimer); listTimer = setTimeout(loadSessions, 800); }

main();
