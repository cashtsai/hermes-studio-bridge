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
    if (remote) {
      const r = await fetch("/console/session/resume", {method:"POST", credentials:"same-origin", cache:"no-store"});
      if (r.ok) {
        const claim = await r.json();
        if (claim.scope === "console-remote" && claim.token) {
          state.token = claim.token;
          sessionStorage.setItem(TOKEN_KEY, state.token);
          return true;
        }
      }
    }
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
const PROVIDER_LABEL = { claude_code: "CC", codex: "CX", hermes: "人格", gemini: "Gemini", openclaw: "OC", cc2: "CC" };

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
        state.stopStream(); state.stopStream = null; setStreamHealth("對話快照");
      } else if (canUse("follow") && !state.stopStream) subscribeCards(state.current, state.view);
    }
    renderSessions();
    renderHome();
    renderAwaiting();
    renderApproval();
    // 交付 4 實錘修法:開著的對話,標頭狀態要跟著「清單的」狀態走 ——
    // 手機那頭發起的回合,有些 provider 的忙/閒只反映在列表 status,
    // per-session 事件沒跟上時,這裡就是唯一會把「閒置」翻成「執行中」的路。
    // 以顯示文案比對(run/running 同文案)避免把事件流帶來的細 label 洗掉。
    const cur = currentSession();
    if (cur && state.snapshotView === state.view && !cur.meta?.locked) {
      const listLabel = PHASE_LABEL[cur.status] || cur.status || "";
      const chipLabel = PHASE_LABEL[state.phase] || state.phase || "";
      if (listLabel && listLabel !== chipLabel) updateStatusChip(cur.status, cur.meta || {});
    }
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

// 交付 7:組內分層排序 —— 需要你(actionable)> 執行中 > 一般。穩定排序:
// 同層維持清單原本順序,只有狀態真的翻轉那一列才會移動。
function sessionRank(s) {
  if (actionable(s)) return 0;
  return ["run", "running", "busy", "queued"].includes(s.status) ? 1 : 2;
}

function renderSessions() {
  const el = $("session-list");
  if (!state.sessions.length) {
    el.textContent = state.listHealth === "failed" || state.degraded.length ? "清單暫時不完整" : "沒有 session";
    return;
  }
  // FLIP 前置:記住每列現在的位置,重畫後只對「真的移動了」的列補一段位移動畫。
  const oldTops = new Map(Array.from(el.querySelectorAll(".sess"),
    (n) => [n.dataset.id, n.getBoundingClientRect().top]));
  const byProv = new Map(["hermes", "claude_code", "codex", "gemini"].map(p => [p, []]));
  for (const s of state.sessions) {
    const p = s.provider === "cc2" ? "claude_code" : s.provider;
    if (!byProv.has(p)) byProv.set(p, []);
    byProv.get(p).push(s);
  }
  for (const rows of byProv.values()) rows.sort((a, b) => sessionRank(a) - sessionRank(b));
  let html = "";
  for (const [prov, rows] of byProv) {
    const dim = state.degraded.includes(prov) || (prov === "claude_code" && state.degraded.includes("cc2")) ? " degraded" : "";
    html += '<div class="prov-group"><div class="prov-head' + dim + '">' +
            esc(PROVIDER_LABEL[prov] || prov) + (dim ? " ⚠" : "") + "</div>";
    for (const s of rows) {
      const active = s.id === state.current ? " active" : "";
      const locked = s.meta && s.meta.locked ? ' 🔒' : "";
      html += '<div class="sess' + active + '" data-id="' + esc(s.id) + '">' +
              '<div class="sess-name">' + esc(s.title || s.id) + locked + "</div>" +
              '<div class="sess-sub"><span class="st st-' + esc(s.status || "idle") + '">' +
              esc(workPhase(s)) + "</span> " + esc(s.subtitle || "") + "</div></div>";
    }
    if (!rows.length) html += '<div class="provider-empty">' + (dim ? "暫時無法讀取" : "無對話") + '</div>';
    html += "</div>";
  }
  el.innerHTML = html;
  el.querySelectorAll(".sess").forEach((n) => {
    n.addEventListener("click", () => openSession(n.dataset.id));
    const was = oldTops.get(n.dataset.id);
    if (was === undefined || !n.animate ||
        window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const delta = was - n.getBoundingClientRect().top;
    if (Math.abs(delta) > 1) n.animate(
      [{ transform: "translateY(" + delta + "px)" }, { transform: "none" }],
      { duration: 180, easing: "ease-out" });
  });
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
  busy: "執行中", failed: "失敗", stalled: "停滯", waiting_approval: "等待核准", queued: "排隊中",
  usage_limited: "用量受限", error: "錯誤", waiting: "等待中" };
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
  resetHistory();
  if (state.stopStream) { state.stopStream(); state.stopStream = null; }
  state.current = null; state.cards = []; state.latestSeq = 0;
  state.busy = false; state.phase = "idle"; bodyHtmlCache.clear(); syncBusyBar(false);
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
let historyAbort, olderCursor = null, loadingOlder = false;
function resetHistory() {
  $("new-reply").hidden = true;
  cardScroll.replyView = null;
  historyAbort?.abort();
  historyAbort = new AbortController();
  olderCursor = null;
  loadingOlder = false;
  updateOlderButton();
}
function updateOlderButton() {
  const b = $("earlier-cards");
  b.hidden = !olderCursor;
  b.disabled = loadingOlder;
  b.textContent = loadingOlder ? "載入中…" : "更早訊息";
  b.classList.toggle("working", loadingOlder);
}
function openSession(id, preserveMode = false) {
  if (signingOut) return;
  if (borrowed() && !state.sessions.find(s => s.id === id)?.capabilities?.includes("replay")) {
    toast("這台電腦無權讀取此對話"); return;
  }
  if (state.current === id && state.snapshotView === state.view && !preserveMode) {
    window.PocketConsoleMode?.set("agents"); return;
  }
  const view = ++state.view;
  resetHistory();
  clearTimeout(snapshotRetry);
  if (state.stopStream) { state.stopStream(); state.stopStream = null; }
  state.current = id;
  if (resumePreference()) rememberWork(id);
  state.cards = []; state.latestSeq = 0; bodyHtmlCache.clear(); busyStartAt = 0;
  if (!preserveMode) window.PocketConsoleMode?.set("agents");
  $("main").classList.remove("at-home");
  $("handoff").classList.add("hidden");
  $("cards").classList.remove("hidden");
  renderSessions();
  writeHash(id);                           // 深連結:可加書籤 / 重整不掉頁
  $("stream-head").classList.remove("hidden");
  $("composer").classList.remove("hidden");
  setStreamHealth("連線中", true);
  showDraft(id);
  const s = state.sessions.find((x) => x.id === id) || {};
  $("sh-provider").textContent = PROVIDER_LABEL[s.provider] || s.provider || "";
  $("sh-name").textContent = s.title || id;
  updateStatusChip(s.status, s.meta);
  syncControls();
  renderApproval();
  $("cards").innerHTML = '<div class="placeholder working" role="status">讀取最近訊息…</div>';
  cardScroll.follow = true;
  cardScroll.top = $("cards").scrollTop;
  void loadCardSnapshot(id, view);
  if (window.PocketConsoleRail) window.PocketConsoleRail.load();
}

async function loadCardSnapshot(id, view, delay = 1000) {
  if (!currentView(view)) return;
  try {
    const snap = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/cards?limit=24&history_page=true",
      { cache: "no-store", signal: AbortSignal.any([historyAbort.signal, AbortSignal.timeout(15000)]) });
    if (!currentView(view)) return;
    if (!Array.isArray(snap.cards) || !Number.isSafeInteger(snap.latest_seq) || snap.latest_seq < 0)
      throw new Error("無效的卡片回應");
    state.cards = snap.cards || [];
    state.latestSeq = snap.latest_seq || 0;
    olderCursor = snap.older_cursor || null;
    updateOlderButton();
    renderCards();
    if (snap.status && snap.status.phase) updateStatusChip(snap.status.phase, snap.status);
    confirmEcho(id, state.cards);
    state.snapshotView = view;
    if (canUse("follow")) subscribeCards(id, view);
    else setStreamHealth("對話快照");
  } catch (err) {
    if (!currentView(view)) return;
    if (err.status >= 400 && err.status < 500 && ![408, 429].includes(err.status)) {
      setStreamHealth("卡片無法讀取：" + err.status);
      $("cards").replaceChildren();
      return;
    }
    setStreamHealth("連線中斷，正在補接", true);
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
async function loadOlderCards() {
  const { current: id, view } = state, cursor = olderCursor;
  if (!cursor || loadingOlder || !canUse("replay")) return;
  loadingOlder = true; updateOlderButton();
  try {
    const page = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) +
      "/cards?limit=48&before_id=" + encodeURIComponent(cursor), {
        cache: "no-store", signal: AbortSignal.any([historyAbort.signal, AbortSignal.timeout(15000)])
      });
    if (!currentView(view) || olderCursor !== cursor) return;
    if (!Array.isArray(page.cards)) throw new Error("invalid page");
    const known = new Set(state.cards.map(c => c.id));
    state.cards = page.cards.filter(c => !known.has(c.id)).concat(state.cards);
    olderCursor = page.older_cursor || null;
    renderCards(true); // Paging never advances the live SSE cursor.
  } catch (err) {
    if (!currentView(view) || olderCursor !== cursor) return;
    if (err.status === 409) olderCursor = null;
    toast(err.status === 409 ? "這段歷史已超出主機保留範圍" : "更早訊息讀取失敗");
  } finally {
    if (currentView(view)) { loadingOlder = false; updateOlderButton(); }
  }
}
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
    setStreamHealth("正在同步", true);
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
        reflectSessionStatus(id, payload.phase);
        scheduleListRefresh();
      } else if (envelope.type === "turn") {
        // turn begin/end 是最早的忙/閒轉場訊號(session.status 是輪詢巡出來
        // 的,可能晚幾拍)。先翻狀態,之後的 status 事件再帶 label 蓋細節。
        if (payload.state === "begin") {
          updateStatusChip("run", { busy: true });
          reflectSessionStatus(id, "running");
        } else if (payload.state === "end") {
          updateStatusChip(payload.outcome === "failed" ? "failed" : "idle", {});
          reflectSessionStatus(id, payload.outcome === "failed" ? "failed" : "idle");
          scheduleListRefresh();
        }
      }
      state.latestSeq = seq;
    },
    onGone: resync,
    onOpen: () => { if (currentView(view)) setStreamHealth(""); },
    onError: () => { if (currentView(view)) setStreamHealth("連線中斷，正在補接", true); },
    onDenied: code => {
      if (!currentView(view)) return;
      state.stopStream = null;
      if (code === 401) void leaveConsole();
      else setStreamHealth("無權讀取");
    },
  });
}

function visibleReplyText(card) {
  if (!card || card.role === "user" || !PROSE_KINDS.has(card.kind || "text")) return "";
  const text = cardText(card);
  if (/^💭\s/u.test(text)) return "";
  return /<(?:details|think)\b/i.test(text)
    ? replySegments(text).filter(p => !p.label).map(p => p.text).join("") : text;
}

function upsertCard(card) {
  const i = state.cards.findIndex((c) => c.id === card.id);
  if (!cardScroll.follow && $("new-reply").hidden) {
    const reply = visibleReplyText(card);
    if (reply && reply !== visibleReplyText(state.cards[i])) cardScroll.replyView = state.view;
  }
  if (i >= 0) state.cards[i] = card; else state.cards.push(card);
  if (state.frameView === state.view) return;
  const view = state.frameView = state.view;
  requestAnimationFrame(() => {
    if (!currentView(view)) return;
    state.frameView = null; renderCards();
    if (cardScroll.replyView === view) {
      if (!cardScroll.follow) $("new-reply").hidden = false;
      cardScroll.replyView = null;
    }
  });
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
      if (!text) return "";
      if (type === "markdown" && window.PocketMarkdown)
        return '<div class="visual-text md">' + PocketMarkdown.render(text) + "</div>";
      return '<div class="visual-text">' + esc(text).replace(/\n/g, "<br>") + "</div>";
    }
    return "";
  }).filter(Boolean).join("");
  if (!blocks) return "";
  const fallback = visual.fallback_text ?
    '<div class="visual-fallback">' + esc(visual.fallback_text).replace(/\n/g, "<br>") + "</div>" : "";
  return '<div class="visual-reply">' + blocks + fallback + "</div>";
}

// 散文(agent 的 text/markdown 卡)走 md.js 全功能渲染;使用者訊息與等寬卡
// 只做「轉義 + 裸網址連結化 + 換行」—— 打什麼看到什麼,與手機版一致。
// md.js 沒載成功時退回舊的純轉義路徑,訊息永遠不會變成 HTML 水槽。
function proseHtml(c, text) {
  const md = window.PocketMarkdown;
  if (!md) return esc(text).replace(/\n/g, "<br>");
  if (c.role !== "user" && PROSE_KINDS.has(c.kind || "text"))
    return '<div class="md">' + md.render(text) + "</div>";
  return md.renderText(text);
}

function fmtBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

// 卡片附件(bridge carddigest 掛的 body.attachments):圖片縮圖內嵌(經
// authed fetch → blob,見 hydrateCardMedia —— <img> 自己帶不了 Bearer),
// 其他檔案一列 chip。download_url 只認自家 /app/v2/artifacts/ 路徑,
// 不讓一張壞卡誘使 Console 把 token 打到任意位址。
function attachmentsHtml(c) {
  const atts = (c.body || {}).attachments;
  if (!Array.isArray(atts) || !atts.length) return "";
  const parts = atts.map((a) => {
    if (!a || typeof a !== "object") return "";
    const name = String(a.filename || String(a.path || "").split("/").pop() || "附件");
    const local = typeof a.download_url === "string" &&
      a.download_url.startsWith("/app/v2/artifacts/") ? a.download_url : "";
    const ext = safeHttpUrl(a.url || a.source_url);
    const size = fmtBytes(a.byte_size);
    if (a.kind === "image" && local && a.available !== false)
      return '<figure class="att-media"><img class="att-img" alt="' + esc(name) +
        '" data-src="' + esc(local) + '"><figcaption>' + esc(name) + "</figcaption></figure>";
    const icon = { image: "🖼️", video: "🎬", audio: "🎵" }[a.kind] || "📄";
    const label = icon + ' <span class="att-name">' + esc(name) + "</span>" +
      (size ? ' <small>' + size + "</small>" : "");
    if (local) return '<button type="button" class="att-chip" data-dl="' + esc(local) +
      '" data-name="' + esc(name) + '" title="下載">' + label + "</button>";
    if (ext) return '<a class="att-chip" href="' + ext +
      '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    return '<span class="att-chip att-dead" title="' + esc(a.path || "來源未封存") + '">' +
      label + "</span>";
  }).filter(Boolean).join("");
  return parts ? '<div class="card-atts">' + parts + "</div>" : "";
}

function cardBodyHtml(c) {
  const visual = visualReplyHtml(c);
  const text = cardText(c);
  const body = c.body || {};
  // visual cards normally carry fallback_text but no duplicate plain text.
  const plain = body.text || body.typed_text || (typeof body === "string" ? body : "");
  const main = visual
    ? (plain ? '<div class="visual-plain">' + proseHtml(c, plain) + "</div>" : "")
    : proseHtml(c, text);
  return visual + main + attachmentsHtml(c);
}

// 已完成卡片的渲染結果不重算(交付 3):markdown 轉換只對「串流中的尾塊」
// 每個 delta 跑一次;final 卡以 id+內容簽名快取,重畫回合時直接取。
const bodyHtmlCache = new Map();
function cardBodyHtmlCached(c) {
  if (c.final === false) return cardBodyHtml(c);
  const atts = (c.body || {}).attachments;
  const sig = cardText(c) + "\x00" + (Array.isArray(atts) ? atts.length : 0);
  const hit = bodyHtmlCache.get(c.id);
  if (hit && hit.sig === sig) return hit.html;
  const html = cardBodyHtml(c);
  bodyHtmlCache.set(c.id, { sig, html });
  return html;
}

// ── 圖片/檔案的 authed 載入 ──────────────────────────────────────────────
// /app/v2/artifacts/* 只認 Bearer header,<img src> 帶不了 → fetch 轉 blob。
// blob URL 以 download_url 為鍵快取,重畫回合不重抓;上限內先進先出回收。
const mediaCache = new Map();
const MEDIA_CACHE_MAX = 48;
async function mediaBlobUrl(url) {
  const hit = mediaCache.get(url);
  if (hit) return hit.promise;
  const promise = (async () => {
    const r = await api(url, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return URL.createObjectURL(await r.blob());
  })();
  mediaCache.set(url, { promise });
  if (mediaCache.size > MEDIA_CACHE_MAX) {
    const [oldKey, old] = mediaCache.entries().next().value;
    mediaCache.delete(oldKey);
    old.promise.then((u) => URL.revokeObjectURL(u), () => {});
  }
  try { return await promise; }
  catch (err) { mediaCache.delete(url); throw err; }
}

function hydrateCardMedia() {
  $("cards").querySelectorAll("img.att-img[data-src]").forEach((img) => {
    const url = img.dataset.src;
    delete img.dataset.src;
    mediaBlobUrl(url).then((blobUrl) => {
      if (!img.isConnected) return;
      img.addEventListener("load", () => {
        // 縮圖載入會長高:貼底閱讀中就跟著貼住,不要讓圖把對話頂歪。
        const el = $("cards");
        if (cardScroll.follow) { el.scrollTop = el.scrollHeight; cardScroll.top = el.scrollTop; }
      }, { once: true });
      img.src = blobUrl;
    }).catch(() => {
      img.closest(".att-media")?.classList.add("att-broken");
      img.alt = "圖片載入失敗";
    });
  });
}

async function downloadAttachment(url, name) {
  try {
    const blobUrl = await mediaBlobUrl(url);
    const a = document.createElement("a");
    a.href = blobUrl; a.download = name || "attachment";
    document.body.append(a); a.click(); a.remove();
  } catch (err) { toast("下載失敗:" + err.message); }
}

async function copyText(text, doneMsg) {
  try {
    if (navigator.clipboard && window.isSecureContext !== false)
      await navigator.clipboard.writeText(text);
    else {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    toast(doneMsg || "已複製");
  } catch (_) { toast("無法複製(瀏覽器不允許)"); }
}

// 訊息動作列(交付 6):回覆=引用塞進輸入框;複製=原始文字;分享=系統分享
// (navigator.share),沒有就退「複製全文」。轉貼到別條 session 需要後端支
// 援,先不硬造。
function handleMessageAction(act, cardId) {
  const card = state.cards.find((c) => c.id === cardId);
  if (!card) { toast("這則訊息已不在目前視窗"); return; }
  const text = cardText(card);
  if (act === "copy") { void copyText(text); return; }
  if (act === "reply") {
    if (!canUse("input")) { toast("這台電腦無法在此對話輸入"); return; }
    const quote = text.trim().split("\n").slice(0, 6).map((l) => "> " + l).join("\n");
    const input = $("input");
    const d = draftFor(state.current);
    d.text = quote + "\n\n" + (d.text ? d.text : "");
    input.value = d.text; autosize(); input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    return;
  }
  if (act === "share") {
    if (navigator.share) navigator.share({ text }).catch(() => {});
    else void copyText(text, "已複製全文(此瀏覽器無系統分享)");
  }
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

const cardScroll = { follow: true, top: 0, replyView: null };
function trackCardScroll(el) {
  if (!el.clientHeight || el.scrollTop === cardScroll.top) return;
  cardScroll.top = el.scrollTop;
  // scrollTop can be fractional; this is rounding tolerance, not a follow zone.
  cardScroll.follow = el.scrollHeight - el.clientHeight - el.scrollTop <= 1;
  if (cardScroll.follow) $("new-reply").hidden = true;
}

function renderCards(prepending = false) {
  const el = $("cards");
  const oldHeight = el.scrollHeight, oldTop = el.scrollTop;
  trackCardScroll(el); // Native scrolling can precede its queued scroll event.
  const follow = !prepending && cardScroll.follow;
  const oldNodes = new Map(Array.from(el.children, n => [n.dataset.key, n]));
  const firstVisible = follow ? null : Array.from(el.children).find(n =>
    n.getBoundingClientRect().bottom > el.getBoundingClientRect().top);
  const anchorTop = firstVisible?.getBoundingClientRect().top;
  if (!state.cards.length) { el.innerHTML = '<div class="placeholder">（還沒有卡片）</div>'; return; }
  const ordered = state.cards.slice().sort((a,b) => (a.ts || 0) - (b.ts || 0));
  let cursor = el.firstElementChild;
  for (const turn of groupTurns(ordered)) {
    const key = turn.cards[0].id;
    const node = oldNodes.get(key) || document.createElement("div");
    oldNodes.delete(key);
    node.className = "turn " + turn.role; node.dataset.key = key;
    if (!node._cards || turn.cards.length !== node._cards.length ||
        turn.cards.some((c, i) => node._cards[i] !== c)) {
      // 串流 delta 走尾塊快修;結構變了(新卡、分段、visual)才整回合重建。
      if (!patchStreamingTail(turn, node)) renderTurnContent(turn, node);
      node._cards = turn.cards;
    }
    if (node !== cursor) el.insertBefore(node, cursor);
    cursor = node.nextElementSibling;
  }
  for (const n of oldNodes.values()) n.remove();
  if (follow) el.scrollTop = el.scrollHeight;
  else if (firstVisible?.isConnected) el.scrollTop += firstVisible.getBoundingClientRect().top - anchorTop;
  else if (prepending) el.scrollTop = oldTop + el.scrollHeight - oldHeight;
  cardScroll.top = el.scrollTop; // Our anchor correction is not a reader gesture.
  hydrateCardMedia();
}

function setStreamHealth(label, working = false) {
  const el = $("stream-health");
  el.textContent = label; el.classList.toggle("working", working);
}
function updateStatusChip(phase, meta) {
  const chip = $("sh-status");
  const locked = meta && meta.locked;
  const label = locked ? "🔒 被佔用" : (PHASE_LABEL[phase] || phase || "狀態未知");
  chip.textContent = label;
  chip.className = "status-chip st-" + esc(phase || "idle");
  state.phase = phase || "idle";
  state.busy = ["run", "running", "busy"].includes(phase);
  chip.classList.toggle("working", state.busy && !locked);
  $("interrupt").classList.toggle("hidden", !state.busy || !canUse("interrupt"));
  // 交付 4:輕量活動列。busy / 排隊中都算「在忙」;phase label(bridge 的
  // 人話 label,例如「正在執行 Read…」)有就用,elapsed 以 busy_since 起算。
  state.statusLabel = (meta && typeof meta.label === "string" && meta.label) || "";
  const working = !locked && (state.busy || phase === "queued");
  if (working) {
    const since = activityTime(meta && meta.busy_since);
    if (since) busyStartAt = since;
    else if (!busyStartAt) busyStartAt = Date.now();
  } else busyStartAt = 0;
  syncBusyBar(working);
}

let busyTimer = null, busyStartAt = 0;
function syncBusyBar(on) {
  const bar = $("workbar");
  on = !!on && !!state.current && !signingOut;
  clearInterval(busyTimer); busyTimer = null;
  bar.classList.toggle("hidden", !on);
  if (!on) return;
  const tick = () => {
    $("workbar-label").textContent = state.statusLabel ||
      (state.phase === "queued" ? "已排入佇列…" : "Agent 工作中…");
    const secs = Math.max(0, Math.round((Date.now() - busyStartAt) / 1000));
    $("workbar-elapsed").textContent = secs >= 60
      ? Math.floor(secs / 60) + " 分 " + (secs % 60) + " 秒" : secs + " 秒";
  };
  tick();
  busyTimer = setInterval(tick, 1000); // 只動兩個文字節點,不重畫訊息
}

// 別的客戶端(手機)發起的回合也要亮:per-session 事件一到,先就地改掉
// 列表裡那一列的狀態再排程抓清單,不等 800ms debounce。
function reflectSessionStatus(id, phase) {
  const s = state.sessions.find((x) => x.id === id);
  if (!s || !phase || s.status === phase) return;
  s.status = phase;
  renderSessions(); renderHome();
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
  $("delivery-text").classList.toggle("working", ["checking","sending"].includes(pending?.phase));
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
  $("new-reply").addEventListener("click", () => {
    const el = $("cards");
    el.scrollTop = el.scrollHeight;
    cardScroll.top = el.scrollTop;
    cardScroll.follow = true;
    $("new-reply").hidden = true;
    el.focus({preventScroll:true});
  });
  $("cards").addEventListener("scroll", e => trackCardScroll(e.currentTarget), { passive: true });
  // 訊息區的互動全走事件代理:卡片每次重畫都換節點,逐顆綁太浪費。
  $("cards").addEventListener("click", (e) => {
    const copy = e.target.closest(".md-copy");
    if (copy) {
      const code = copy.closest(".md-code")?.querySelector("code");
      if (code) void copyText(code.textContent, "已複製程式碼");
      return;
    }
    const act = e.target.closest(".msg-act");
    if (act) {
      const id = act.closest(".msg-actions")?.dataset.card;
      if (id) handleMessageAction(act.dataset.act, id);
      return;
    }
    const img = e.target.closest("img.att-img");
    if (img && img.src) { window.open(img.src, "_blank", "noopener"); return; }
    const chip = e.target.closest(".att-chip[data-dl]");
    if (chip) void downloadAttachment(chip.dataset.dl, chip.dataset.name);
  });
  $("earlier-cards").addEventListener("click", loadOlderCards);
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
    if (!imeKey(e) && !e.shiftKey && !e.altKey && e.key === "Enter") {
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
