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
  listFilter: "all",  // 清單篩選:all / attn(需要我) / unread
  listQuery: "",      // 清單搜尋字串
};
const drafts = new Map();  // Page memory only, never persisted on a borrowed device.
// Only the newest preview/incoming message and monotonic read cursor per session.
// Keep these separate from list snapshots: an older HTTP response must not undo SSE.
const listActivity = new Map();
const previewSegmenter = typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, {granularity:"grapheme"}) : null;
let listActivityFrame = null;
function clipPreview(text) {
  let result = "", count = 0;
  for (const part of previewSegmenter ? previewSegmenter.segment(text) : text) {
    if (count++ === 140) return result + "…";
    result += part.segment ?? part;
  }
  return result;
}
function listActivityFor(id) {
  if (!listActivity.has(id)) listActivity.set(id, {readSeq:0, readAt:0});
  return listActivity.get(id);
}
function sessionPreview(s) {
  return listActivity.get(s.id)?.latest?.text || s.last_turn_summary || s.subtitle || "";
}
function sessionUnread(s) {
  const a = listActivity.get(s.id), m = a?.incoming;
  return !!m && m.seq > a.readSeq && (!m.at || m.at > a.readAt);
}
function unreadMark(s) {
  return sessionUnread(s) ? '<span class="unread-mark">未讀</span>' : "";
}
function applyListEvent(event) {
  if (!["message.upsert", "read_cursor.update"].includes(event.type)) return false;
  const data = event.data || {}, key = event.session || data.session;
  if (typeof key !== "string" || !key) return false;
  const id = key.includes(":") ? key : "hermes:" + key;
  const a = listActivityFor(id);
  if (event.type === "read_cursor.update") {
    const cursor = data.global || data;
    const seq = Number(cursor.last_read_seq);
    if (Number.isSafeInteger(seq) && seq > a.readSeq) a.readSeq = seq;
    a.readAt = Math.max(a.readAt, activityTime(cursor.last_read_ts));
  } else {
    const m = data.message;
    if (!m || !["user", "assistant", "agent"].includes(m.role)) return true;
    const raw = typeof m.content === "string" ? m.content : "";
    const text = (m.role === "user" ? raw : visibleReplyText({role:"agent", kind:"text", body:{text:raw}}))
      .replace(/\s+/g, " ").trim();
    if (!text && !m.attachments?.length) return true;
    const next = {id:m.id, at:activityTime(m.ts), seq:event.seq,
      text:clipPreview(text || "附件")};
    // A late edit of yesterday's message must not displace today's preview.
    const newer = previous => !previous || next.at > previous.at ||
      (next.at === previous.at && next.seq > previous.seq) || (next.id && next.id === previous.id);
    if (newer(a.latest)) a.latest = next;
    if (m.role !== "user" && newer(a.incoming)) a.incoming = next;
  }
  if (listActivityFrame === null) listActivityFrame = requestAnimationFrame(() => {
    listActivityFrame = null;
    if (!signingOut) { renderSessions(); renderHome(); }
  });
  return true;
}
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
  catch (_) { toast("這個瀏覽器無法記住工作", true); }
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
  window.PocketSessionOps?.leave();
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
  syncBusyBar(false);
  listActivity.clear();
  cancelAnimationFrame(listActivityFrame); listActivityFrame = null;
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
  const statusRevision = state.statusRevision;
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
    const liveRows = new Map(state.sessions.map(s => [s.id, s]));
    state.sessions = d.sessions.map(s => {
      const live = liveRows.get(s.id);
      return live?._statusRevision > (statusRevision || 0)
        ? {...s, status:live.status, meta:{...s.meta, ...live.meta}, _statusRevision:live._statusRevision} : s;
    });
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
    // An in-flight list snapshot must not undo a newer status event.
    // Equivalent phases (run/running) retain details omitted by the thin list.
    const cur = currentSession();
    if (cur && state.snapshotView === state.view && statusRevision === state.statusRevision) {
      const samePhase = (PHASE_LABEL[cur.status] || cur.status) === (PHASE_LABEL[state.phase] || state.phase);
      updateStatusChip(cur.status, {...(samePhase ? state.statusMeta : {}), ...cur.meta});
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

// 組內分層排序:需要你(actionable)> 執行中 > 一般。
function sessionRank(s) {
  if (actionable(s)) return 0;
  if (s.status === "needs_input") return 0;
  return ["run", "running", "busy", "queued", "dispatching"].includes(s.status) ? 1 : 2;
}

// 列表位置的「降級遲滯」。CC 在工具步驟之間會短暫回 idle,排序立刻把那列
// 往下丟、下一步開跑又彈回來 —— 2026-11-13 機主回報「執行中跑上去又跑下來」
// 就是這個。規則:**升級(變更緊急)立即生效**,降級要連續維持 15 秒才算數;
// 同級之內按最近活動排,再用 id 墊底,讓次序完全確定(原本同級順序跟著
// 資料到達順序飄)。
const RANK_DEMOTE_MS = 15000;
const rankMemo = new Map();   // id → { rank, pending, since }

function stickyRank(s, now) {
  const raw = sessionRank(s);
  const memo = rankMemo.get(s.id);
  if (!memo || raw < memo.rank) {           // 升級或首見:立即生效並記住
    rankMemo.set(s.id, { rank: raw, pending: null, since: now });
    return raw;
  }
  if (raw === memo.rank) { memo.pending = null; return raw; }
  if (memo.pending !== raw) { memo.pending = raw; memo.since = now; }
  // 降級:15 秒內先維持原位;過了才真的掉下去。
  if (now - memo.since < RANK_DEMOTE_MS) return memo.rank;
  rankMemo.set(s.id, { rank: raw, pending: null, since: now });
  return raw;
}

// 釘選:存 localStorage,置頂於所屬 provider 群組。手機沒有「釘選」概念,這是
// 桌面大螢幕的便利——常盯的那幾條固定在上面,不被其他列推走。
const PIN_KEY = "pocket-console-pins";
let pinnedIds = null;
function pins() {
  if (pinnedIds) return pinnedIds;
  try { pinnedIds = new Set(JSON.parse(localStorage.getItem(PIN_KEY)) || []); }
  catch (_) { pinnedIds = new Set(); }
  return pinnedIds;
}
function isPinned(id) { return pins().has(id); }
function togglePin(id) {
  const p = pins();
  if (p.has(id)) p.delete(id); else p.add(id);
  try { localStorage.setItem(PIN_KEY, JSON.stringify(Array.from(p))); } catch (_) {}
  renderSessions();
}

// 清單篩選/搜尋:all=全部、attn=需要我(actionable/needs_input=rank 0)、unread=未讀。
// 搜尋沿用 ⌘K 的 workMatches(含 CJK 摺疊),比對 title/id/provider/workdir。
function sessionVisible(s) {
  const f = state.listFilter;
  if (f === "attn" && sessionRank(s) !== 0) return false;
  if (f === "unread" && !sessionUnread(s)) return false;
  if (state.listQuery && !workMatches(s, state.listQuery)) return false;
  return true;
}

function sortSessions(rows, now) {
  // 每個 session 每次快照只更新一次,包括只有一列的 provider。
  // 2026-11-21:**同 rank 之內不再按活動時間重排**。原本的次層
  // `max(last_event_at, latest.at)` 會在每一則訊息到達時把列往上搬,就是
  // 「跑上跑下」的真因(遲滯只擋住 rank 層的彈跳,擋不住這層)。改成:釘選優先、
  // 再 rank(需要你 > 執行中 > 一般,已含 15s 降級遲滯),同 rank 用 id 墊底 →
  // 位置完全確定、不隨活動漂移。活動/未讀改由列上的狀態晶片與未讀標表達
  // (renderSessions 就地更新那一列),不靠「整列跳位」。
  const ranks = new Map(rows.map(s => [s.id, stickyRank(s, now)]));
  return rows.sort((a, b) =>
    ((isPinned(b.id) ? 1 : 0) - (isPinned(a.id) ? 1 : 0)) ||
    (ranks.get(a.id) - ranks.get(b.id)) ||
    (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

// 一列的內容 HTML(.session-row 的內部)。當這串與上次相同 → 完全不碰 DOM。
function sessionRowHTML(s) {
  const active = s.id === state.current;
  const locked = s.meta && s.meta.locked ? ' 🔒' : "";
  const pinned = isPinned(s.id);
  return '<button type="button" class="sess' + (active ? " active" : "") + (pinned ? " pinned" : "") +
    '" data-id="' + esc(s.id) + '" aria-current="' + (active ? "true" : "false") + '">' +
    '<span class="sess-name">' + esc(s.title || s.id) + locked + unreadMark(s) + "</span>" +
    '<span class="sess-sub"><span class="st st-' + esc(s.status || "idle") + '">' +
    esc(workPhase(s)) + "</span> " + esc(sessionPreview(s)) + "</span></button>" +
    '<button type="button" class="sess-pin' + (pinned ? " on" : "") + '" data-id="' + esc(s.id) +
    '" title="' + (pinned ? "取消釘選" : "釘選到頂") + '" aria-label="' + (pinned ? "取消釘選" : "釘選") +
    '">' + (pinned ? "★" : "☆") + "</button>" +
    (window.PocketSessionOps?.menuButton(s) || "");
}

// 有引導的空狀態(對齊手機/商業級產品):圖示 + 人話標題 + 下一步,必要時給動作鈕。
// 取代原本一行乾字的「沒有 session」。
function listEmptyHTML(icon, title, hint, action = "") {
  return '<div class="list-empty"><div class="list-empty-ic" aria-hidden="true">' + icon + "</div>" +
    '<div class="list-empty-title">' + esc(title) + "</div>" +
    '<div class="list-empty-hint">' + esc(hint) + "</div>" + action + "</div>";
}

// 清單點選改用「事件委派」:一個掛在容器上的 listener,不再逐列掛/重掛 ——
// keyed diff 會重用列節點,逐列掛 handler 會在就地更新時連同按鈕一起丟掉。
function bindSessionListClicks(el) {
  if (el._clickBound) return;
  el._clickBound = true;
  el.addEventListener("click", (ev) => {
    if (ev.target.closest(".list-empty-new")) { $("newsess").classList.remove("hidden"); return; }
    if (ev.target.closest(".list-empty-clear")) {   // 清除篩選 → 回全部
      state.listQuery = ""; state.listFilter = "all";
      const q = $("sess-q"); if (q) q.value = "";
      $("sess-filters")?.querySelectorAll(".sf-chip").forEach((c) =>
        c.classList.toggle("active", (c.dataset.filter || "all") === "all"));
      renderSessions(); return;
    }
    const pin = ev.target.closest(".sess-pin");   // 釘選鈕也是 .session-row 的兄弟,先攔
    if (pin && el.contains(pin)) { ev.preventDefault(); ev.stopPropagation(); togglePin(pin.dataset.id); return; }
    const btn = ev.target.closest(".sess");   // ops 選單鈕是 .session-row 的兄弟,不在 .sess 內 → 不會誤觸
    if (btn && el.contains(btn)) openSession(btn.dataset.id);
  });
}

function renderSessions() {
  const el = $("session-list");
  bindSessionListClicks(el);
  const focusedId = el.contains(document.activeElement) ? document.activeElement.dataset.id : null;
  if (!state.sessions.length) {
    el._provGroups = null;        // 下次有 session 時重建群組結構
    el.innerHTML = (state.listHealth === "failed" || state.degraded.length)
      ? listEmptyHTML("⚠️", "清單暫時不完整",
          "部分服務連不上。按 ↻ 重新整理,或檢查橋接是否在線。")
      : listEmptyHTML("✦", "還沒有 session",
          "按 ＋ 開第一條工作,或用右上角掃碼把手機上的工作接過來。",
          '<button type="button" class="list-empty-btn list-empty-new">＋ 開第一條工作</button>');
    return;
  }
  // 篩選/搜尋。有在篩時,只顯示有命中的 provider 群組(空群組不顯示「無對話」,
  // 否則一篩就滿屏空群組);零命中給明確訊息 + 清除鈕,不是一片空白。
  const filtering = !!(state.listQuery || (state.listFilter && state.listFilter !== "all"));
  const visible = filtering ? state.sessions.filter(sessionVisible) : state.sessions;
  if (!visible.length) {
    el._provGroups = null;
    const what = state.listQuery ? ('「' + esc(state.listQuery) + '」')
      : (state.listFilter === "attn" ? "「需要我」" : "「未讀」");
    el.innerHTML = listEmptyHTML("⌕", "找不到符合的 session",
      "目前的篩選 " + what + " 沒有命中任何工作。",
      '<button type="button" class="list-empty-btn list-empty-clear">清除篩選</button>');
    return;
  }
  // FLIP 前置:記住每列現在的位置,重畫後只對「真的移動了」的列補一段位移動畫。
  const oldTops = new Map(Array.from(el.querySelectorAll(".session-row"),
    (n) => [n.dataset.id, n.getBoundingClientRect().top]));
  const byProv = filtering ? new Map()
    : new Map(["hermes", "claude_code", "codex", "gemini"].map(p => [p, []]));
  for (const s of visible) {
    const p = s.provider === "cc2" ? "claude_code" : s.provider;
    if (!byProv.has(p)) byProv.set(p, []);
    byProv.get(p).push(s);
  }
  const now = Date.now();
  for (const rows of byProv.values()) sortSessions(rows, now);

  reconcileSessionList(el, byProv);

  // FLIP:只有「真的換了位置」的列(= rank 變動,已稀有)會動;就地更新內容的
  // 列節點不動 → 不閃、不丟 scroll、不播無謂動畫。
  if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    el.querySelectorAll(".session-row").forEach((n) => {
      const was = oldTops.get(n.dataset.id);
      if (was === undefined || !n.animate) return;
      const delta = was - n.getBoundingClientRect().top;
      if (Math.abs(delta) > 1) n.animate(
        [{ transform: "translateY(" + delta + "px)" }, { transform: "none" }],
        { duration: 180, easing: "ease-out" });
    });
  }
  // 焦點復原:只有被「就地重建內容」的那一列才會掉焦點(重用+移動的不會)。
  if (focusedId && !el.contains(document.activeElement)) {
    for (const b of el.querySelectorAll(".sess")) {
      if (b.dataset.id === focusedId) { b.focus({ preventScroll: true }); break; }
    }
  }
}

// keyed diff:容器內一個 provider 一個 .prov-group;群組內一列一個 .session-row
// (以 session id 為 key)。只對「內容真的變了」的列改 innerHTML、只移動「真的
// 換位」的列,其餘完全不碰 DOM —— 這是消除閃爍/頭暈/丟 scroll 的關鍵。
function reconcileSessionList(el, byProv) {
  let groupEls = el._provGroups;
  if (!groupEls) { el.innerHTML = ""; el._provGroups = groupEls = new Map(); }
  // 移除已不存在的 provider 群組
  for (const [prov, g] of groupEls) {
    if (!byProv.has(prov)) { g.remove(); groupEls.delete(prov); }
  }
  let prevGroup = null;
  for (const [prov, rows] of byProv) {
    let g = groupEls.get(prov);
    if (!g) {
      g = document.createElement("div");
      g.className = "prov-group";
      g._head = document.createElement("div");
      g.appendChild(g._head);
      g._rows = new Map();   // id → .session-row
      g._empty = null;
      groupEls.set(prov, g);
    }
    // 把群組排到正確位置(順序固定,幾乎不會動)
    const want = prevGroup ? prevGroup.nextSibling : el.firstChild;
    if (want !== g) el.insertBefore(g, want);
    prevGroup = g;
    // 群組標頭(降級態)
    const dim = state.degraded.includes(prov) || (prov === "claude_code" && state.degraded.includes("cc2"));
    const headText = (PROVIDER_LABEL[prov] || prov) + (dim ? " ⚠" : "");
    const headCls = "prov-head" + (dim ? " degraded" : "");
    if (g._head.className !== headCls) g._head.className = headCls;
    if (g._head.textContent !== headText) g._head.textContent = headText;
    reconcileRows(g, rows, dim);
  }
}

function reconcileRows(g, rows, dim) {
  const rowMap = g._rows, desired = new Set(rows.map(s => s.id));
  for (const [id, node] of rowMap) {
    if (!desired.has(id)) { node.remove(); rowMap.delete(id); }
  }
  if (g._empty) { g._empty.remove(); g._empty = null; }
  if (!rows.length) {
    g._empty = document.createElement("div");
    g._empty.className = "provider-empty";
    g._empty.textContent = dim ? "暫時無法讀取" : "無對話";
    g.appendChild(g._empty);
    return;
  }
  let prev = g._head;
  for (const s of rows) {
    const html = sessionRowHTML(s);
    let node = rowMap.get(s.id);
    if (!node) {
      node = document.createElement("div");
      node.className = "session-row";
      node.dataset.id = s.id;
      node.innerHTML = html;
      node._sig = html;
      rowMap.set(s.id, node);
    } else if (node._sig !== html) {   // 內容變了才就地重建這一列
      node.innerHTML = html;
      node._sig = html;
    }
    if (prev.nextSibling !== node) g.insertBefore(node, prev.nextSibling);
    prev = node;
  }
}

function activityTime(value) {
  const ms = typeof value === "number" ? (value < 1e12 ? value * 1000 : value)
    : typeof value === "string" && /^\d{4}-\d\d-\d\dT/.test(value) ? Date.parse(value) : NaN;
  return ms > 0 && Number.isFinite(new Date(ms).getTime()) ? ms : 0;
}

// 搜尋字串正規化(⌘K 面板 / 工作清單過濾)。照 dsh 的 text-search:
//   NFKD → 去組合記號 → NFKC → lowercase
// 一次解掉三類中日文搜尋落空:
//   ‧ 全形/半形不摺疊 ——「ＣＣ」搜不到 "CC"、「ｐｏｃｋｅｔ」搜不到 "pocket"
//   ‧ 相容字形 ——「㈱」「㎝」「①」與其展開形搜不到彼此
//   ‧ 組合記號 —— 片假名濁音(か+゙ vs が)、拉丁變音(é vs e)兩種寫法互搜
// 中文**不用**切詞:漢字之間沒有空白,每個漢字自己就是詞邊界,所以
// 子字串比對(includes)本來就是對的做法 —— 真正缺的只有上面這層摺疊。
const COMBINING_MARKS = /\p{M}+/gu;
function foldSearchText(value) {
  const s = String(value == null ? "" : value);
  try {
    return s.normalize("NFKD").replace(COMBINING_MARKS, "").normalize("NFKC").toLowerCase();
  } catch (_) {           // normalize 不可用/輸入含孤立 surrogate → 至少別炸
    return s.toLowerCase();
  }
}
function workMatches(s, query) {
  const fields = [s.title, s.id, s.provider, PROVIDER_LABEL[s.provider], s.subtitle, s.meta?.workdir,
    s.provider === "claude_code" || s.provider === "cc2" ? "Claude Code" : ""].map(foldSearchText);
  // 全形空白(U+3000)也算詞間空白 —— \s 在 JS 已涵蓋,摺疊後更穩。
  return foldSearchText(query).trim().split(/\s+/).filter(Boolean)
    .every(term => fields.some(field => field.includes(term)));
}

const PHASE_LABEL = { idle: "閒置", done: "已完成", running: "執行中", run: "執行中",
  busy: "執行中", failed: "失敗", stalled: "停滯", waiting_approval: "等待核准", queued: "排隊中",
  usage_limited: "用量受限", error: "錯誤", waiting: "等待中", needs_input: "需要你",
  dead: "已停止", completed: "已完成", dispatching: "派送中", dormant: "暫無活動" };
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
  const time = Math.max(activityTime(s.last_event_at), listActivity.get(s.id)?.latest?.at || 0);
  const detail = [sessionPreview(s) || s.meta?.workdir, s.id].filter(Boolean).join(" · ");
  const phase = workPhase(s);
  return '<button type="button" class="work-row" data-id="' + esc(s.id) + '">' +
    '<span class="work-provider">' + esc(PROVIDER_LABEL[s.provider] || s.provider) + '</span>' +
    '<span class="work-text"><strong>' + esc(s.title || s.id) + unreadMark(s) + '</strong><small>' + esc(detail) +
    '</small></span><span class="work-state st-' + esc(s.status || "idle") + '">' + esc(phase) +
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
  renderSessions(); renderHome();
  if (!mobileConsole()) $("home-q").focus({preventScroll:true});
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
  b.hidden = !olderCursor && !(state.current && canUse("history"));
  b.disabled = loadingOlder;
  b.textContent = loadingOlder ? "載入中…" : olderCursor ? "更早訊息" : "更早訊息（逐字稿）";
  b.classList.toggle("working", loadingOlder);
}
function mobileConsole() { return window.matchMedia("(max-width:760px)").matches; }
function focusComposer() {
  if (!$("composer").classList.contains("hidden") && !mobileConsole())
    $("input").focus({preventScroll:true});
}
function openSession(id, preserveMode = false, focusInput = true) {
  if (signingOut) return;
  ensureNotifyPermission();                   // 開對話是使用者手勢 → 這時才問通知權限
  resetForceStop();                           // 換對話,收掉上一條的強制停止升級
  if (borrowed() && !state.sessions.find(s => s.id === id)?.capabilities?.includes("replay")) {
    toast("這台電腦無權讀取此對話", true); return;
  }
  if (state.current === id && state.snapshotView === state.view && !preserveMode) {
    window.PocketConsoleMode?.set("agents"); if (focusInput) focusComposer(); return;
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
  if (focusInput && !preserveMode) focusComposer();
  renderApproval();
  $("cards").innerHTML = '<div class="placeholder working" role="status">讀取最近訊息…</div>';
  cardScroll.follow = true;
  cardScroll.top = $("cards").scrollTop;
  void loadCardSnapshot(id, view);
  if (window.PocketConsoleRail) window.PocketConsoleRail.load();
}


// ── 2026-11-11 黑盒子:console 的 JS 錯誤自己寄回 bridge(/clientlog),
//    不然遠端使用者炸在哪永遠只能用猜的。節流:同 key 60 秒一次。
const _errReported = new Map();
function reportConsoleError(where, err) {
  try {
    const key = where + ":" + String(err && err.message).slice(0, 80);
    const now = Date.now();
    if ((_errReported.get(key) || 0) > now - 60000) return;
    _errReported.set(key, now);
    const body = JSON.stringify({ context: "console." + where, level: "error",
      msg: String(err && err.message).slice(0, 300),
      detail: String(err && err.stack).slice(0, 900),
      build: "console", ua: navigator.userAgent.slice(0, 120) });
    fetch("/clientlog", { method: "POST", headers: Object.assign(
      { "Content-Type": "application/json" },
      state.token ? { Authorization: "Bearer " + state.token } : {}), body }).catch(() => {});
  } catch (_) {}
}
window.addEventListener("error", e => reportConsoleError("window", e.error || { message: e.message, stack: e.filename + ":" + e.lineno }));
window.addEventListener("unhandledrejection", e => reportConsoleError("promise", e.reason || {}));

// 舊瀏覽器沒有 AbortSignal.any / AbortSignal.timeout(各 Chrome 116/103+)。
// 有就雙訊號,沒有就退 historyAbort 單訊號 —— 寧可少逾時,不可整頁炸。
function snapshotSignal() {
  try {
    if (AbortSignal.any && AbortSignal.timeout)
      return AbortSignal.any([historyAbort.signal, AbortSignal.timeout(15000)]);
  } catch (_) {}
  return historyAbort.signal;
}

// 渲染保底:富渲染炸掉時至少把文字放上去(純 textContent,零解析)。
function renderCardsPlain() {
  const host = $("cards");
  host.replaceChildren();
  for (const c of state.cards.slice(-50)) {
    const d = document.createElement("div");
    d.className = "card-plain";
    d.textContent = ((c.body && (c.body.fallback_text || c.body.text)) || c.kind || "");
    host.appendChild(d);
  }
}

async function loadCardSnapshot(id, view, delay = 1000) {
  if (!currentView(view)) return;
  try {
    const snap = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/cards?limit=24&history_page=true",
      { cache: "no-store", signal: snapshotSignal() });
    if (!currentView(view)) return;
    if (!Array.isArray(snap.cards) || !Number.isSafeInteger(snap.latest_seq) || snap.latest_seq < 0)
      throw new Error("無效的卡片回應");
    state.cards = snap.cards || [];
    state.latestSeq = snap.latest_seq || 0;
    olderCursor = snap.older_cursor || null;
    updateOlderButton();
    // 渲染炸 ≠ 網路斷:掉進下面的 catch 會變成「連線中斷,正在補接」無限
    // 重抓(2026-11-11 機主實錘的卡死路徑)。渲染錯誤要顯性+回報+保底,
    // 且**不能擋住訂閱** —— 靜默失敗是最大病原。
    try { renderCards(); }
    catch (renderErr) {
      reportConsoleError("renderCards", renderErr);
      setStreamHealth("顯示層發生錯誤(已回報),改用純文字顯示");
      try { renderCardsPlain(); } catch (_) {}
    }
    if (snap.status && snap.status.phase) updateStatusChip(snap.status.phase, snap.status);
    confirmEcho(id, state.cards);
    state.snapshotView = view;
    window.PocketSessionOps?.scheduleRead();
    if (canUse("follow")) subscribeCards(id, view);
    else setStreamHealth("對話快照");
  } catch (err) {
    if (!currentView(view)) return;
    if (err.status >= 400 && err.status < 500 && ![408, 429].includes(err.status)) {
      setStreamHealth("卡片無法讀取：" + err.status);
      $("cards").replaceChildren();
      return;
    }
    reportConsoleError("snapshot", err);
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
  if (loadingOlder || !canUse("replay")) return;
  if (!cursor) { if (canUse("history")) window.PocketSessionOps?.history(currentSession()); return; }
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
    if (err.status === 409 && canUse("history")) {
      olderCursor = null; window.PocketSessionOps?.history(currentSession());
    } else {
      if (err.status === 409) olderCursor = null;
      toast(err.status === 409 ? "主機沒有可接續的逐字稿" : "更早訊息讀取失敗", true);
    }
  } finally {
    if (currentView(view)) { loadingOlder = false; updateOlderButton(); }
  }
}
function currentSession() {
  return state.sessions.find((x) => x.id === state.current) || null;
}

function borrowed() { return document.body.dataset.consoleAccess === "remote"; }

// ── 能力驅動(fail-open + hide-only,抄 iOS `ProviderCapsStore`)──────────
//
// 舊版 `canUse` 的第一個條件是 `!borrowed()` → 在自己家的 console 上**無條件
// 放行**,bridge 每列都給的 per-session `capabilities` 完全沒作用(宣告了
// 也沒人讀)。改成兩種語意分開:
//
//   · 借用的電腦(console-remote):`capabilities` 是**授權**白名單 ——
//     沒宣告 = 不准。這條不動(安全面,fail-closed 是對的)。
//   · 自己的電腦:`capabilities` 是**能力**宣告 —— fail-open + hide-only。
//     不知道(清單還沒就緒 / 這一列沒帶 capabilities)→ 放行,行為與今天
//     一個像素不差;只有「明確宣告了、而且就是沒有這一項」才隱藏。
//
// `CAP_VOCAB` 是「缺席才有意義」的詞彙白名單 —— **不在表內的能力一律放行**。
// 為什麼需要這張表:per-session `capabilities` 的缺席不是每個詞都代表
// 「不支援」。
//   · `changes` 只有 `_console_remote_list()` 會加(借用機的檔案授權旗標),
//     本機清單永遠不帶 → 放進表內等於把本機的「變更」檢視整個藏掉。
//   · `interrupt` 在 hermes 人格列沒宣告(bridge.py:22067),但
//     `/app/v2/sessions/{id}/interrupt` 的 `hp` 分支是**真的通**的
//     (bridge.py:23066)→ 放進表內會藏掉一個現在就能用的功能。
// 兩個都是「本機模式不該突然藏掉原本能用的東西」的實例,所以刻意缺席。
const CAP_VOCAB = new Set(["input", "attachments", "replay", "follow", "approve",
  "rename", "archive", "history", "config_read", "config_write", "compact", "read_cursor"]);
const OP_CAP_VOCAB = new Set(["rename", "archive", "history", "config_read", "config_write", "compact", "read_cursor"]);
function canUse(cap, session = currentSession()) {
  const caps = session?.capabilities;
  if (borrowed()) return !!(state.listHealth === "ready" && caps?.includes(cap));
  if (state.listHealth !== "ready" || !CAP_VOCAB.has(cap)) return true;   // 不知道 → 不藏
  if (OP_CAP_VOCAB.has(cap)) {
    // Session capabilities predate these ops; only the provider matrix can deny them.
    const declared = typeof window === "undefined" ? undefined : window.PocketSessionOps?.capability(cap, session);
    return declared !== false;
  }
  return !Array.isArray(caps) || caps.includes(cap);
}
function syncControls() {
  window.PocketSessionOps?.syncControls();
  updateOlderButton();
  $("personal-work").hidden = !personalDevice();
  $("remember-work").checked = !!resumePreference();
  // 變更檢視的收斂只在借用機上做 —— `changes` 是 console-remote 專屬的授權
  // 旗標(見 CAP_VOCAB 註解),本機沒有「被收回」這回事。
  if (borrowed()) {
    const changes = canUse("changes");
    if (!changes && document.body.classList.contains("can-view-changes")) {
      for (const id of ["rail-list","ch-list","ch-diff"]) $(id).replaceChildren();
    }
    document.body.classList.toggle("can-view-changes", changes);
    if (!changes && !$("changes-view").classList.contains("hidden")) window.PocketConsoleMode?.set("agents");
  }
  if (state.current) {
    $("composer").classList.toggle("hidden", !canUse("input"));
    $("attach").classList.toggle("hidden", !canUse("attachments"));
    $("interrupt").classList.toggle("hidden", !(canUse("interrupt") && state.busy));
    $("read-only").classList.toggle("hidden", canUse("input"));
    // 同一個節點現在兩種模式共用 —— 本機看到它代表「這個 provider 沒宣告
    // input」,講成「這台電腦只能閱讀」會把人帶到錯的方向。
    $("read-only").textContent = state.listHealth === "failed" ? "清單未更新，暫停操作"
      : borrowed() ? "這台電腦只能閱讀此對話" : "這個對話目前不接受輸入";
  } else $("read-only").classList.add("hidden");
}

function approvalIdOf(meta) {
  const a = meta && meta.approval;
  if (!a) return null;
  return a.aid || a.id || a.approval_id || null;
}

const deciding = new Set();
const handledApproval = new Map();
// approval.resolved(bridge.py:3093)收到即收卡 —— 不留懸空:別的端答掉了,
// 這裡不該還掛著一顆按下去吃 409 的按鈕。只存 aid,上限防無限長大。
const resolvedApprovals = new Set();
function noteApprovalResolved(aid) {
  if (!aid) return;
  if (resolvedApprovals.size > 400) resolvedApprovals.clear();
  resolvedApprovals.add(String(aid));
}
let approvalExpiry;
function actionable(s) {
  if (!s || state.listHealth !== "ready" || state.degraded.includes(s.provider)) return false;
  const a = s.meta?.approval, aid = approvalIdOf(s.meta);
  // 借用機:白名單(沒宣告=不准)。本機:宣告了卻沒 approve 才擋(見 canUse)。
  if ((borrowed() || Array.isArray(s.capabilities)) && !s.capabilities?.includes("approve")) return false;
  if (a?.status && a.status !== "pending") return false;
  if (aid && resolvedApprovals.has(String(aid))) return false;
  const exp = activityTime(a?.expires_at);
  if (exp && exp <= Date.now()) return false;
  if (aid) return handledApproval.get(s.id) !== JSON.stringify([aid, s.meta.approval?.q_index ?? null]);
  return document.body.dataset.consoleAccess !== "remote" && !!s.meta?.prompt?.options?.length;
}

// ── 審批的單一讀取點 ───────────────────────────────────────────────────
// 三個資料源(CC 的 `meta.prompt`、統一物件 `meta.approval`、借用機的
// `meta.console_options`)在這裡收斂成一個形狀,下游只讀這一份,不再各自長 if。
//
// options 以 prompt 為主(直接解 pane,比 watcher 寫的 DB 列新鮮),但**把統一
// 物件的 style/description 依 key 疊回去**:`style` 只有統一物件有
// (`_cc_approval_payload` 標的),丟掉它就等於丟掉「哪顆是拒絕」——
// 那正是鍵盤 `d` 會按到選單最後一項的真兇。
const APPROVAL_RISK = {
  low: ["低風險", "low"], medium: ["中風險", "medium"], high: ["高風險", "high"],
  normal: ["一般", "low"], danger: ["高風險", "high"],   // gemini 用的字彙
};
const APPROVAL_ICON = { question: "❓", notice: "🔔" };
const APPROVAL_MAX_OPTS = 24;   // 防爆上限;超過的部分**明示**,不靜默吃掉

function approvalView(s) {
  const meta = (s && s.meta) || {};
  const prompt = meta.prompt || null;
  const a = meta.approval || null;
  const am = (a && a.meta) || {};
  const base = borrowed() ? meta.console_options
    : (prompt?.options?.length ? prompt.options : a?.options);
  const styled = new Map((a?.options || []).map(o => [String(o.key), o]));
  const options = (Array.isArray(base) ? base : [])
    .filter(o => o && o.key !== undefined && o.key !== null && String(o.key) !== "")
    .map(o => {
      const ref = styled.get(String(o.key)) || {};
      const checked = o.checked !== undefined ? o.checked : ref.checked;
      return {
        key: String(o.key),
        label: String(o.label == null || o.label === "" ? o.key : o.label),
        style: String(o.style || ref.style || ""),
        description: String(o.description || ref.description || ""),
        checked: checked === undefined || checked === null ? null : !!checked,
      };
    });
  const int = v => (Number.isInteger(v) ? v : null);
  const firstBool = (...xs) => { for (const x of xs) if (typeof x === "boolean") return x; return false; };
  return {
    identity: [s?.id, approvalIdOf(meta)],
    title: String(prompt?.title || a?.title || "等待你放行"),
    detail: String(a?.detail || ""),
    risk: String(a?.risk || "").toLowerCase(),
    kind: String(a?.kind || (prompt?.semantic === "question" ? "question" : prompt ? "permission" : "")),
    options,
    multiselect: firstBool(prompt?.multiselect, am.multiselect, a?.multiselect),
    qIndex: int(prompt?.q_index ?? am.q_index ?? a?.q_index),
    qTotal: int(prompt?.q_total ?? am.q_total ?? a?.q_total),
    submitLabel: String(prompt?.submit_label || ""),
    // ⚠️ wire 用的 q_index **只認統一物件頂層那顆**(codex question 專屬)。
    // 借用機的授權守門比對 body 欄位集合(bridge.py:22647),對 CC 多送一個
    // expected_q_index 會直接 403 —— 顯示用的 qIndex 不准流到線上。
    wireQIndex: int(a?.q_index),
  };
}

// 只有 CC 的 TUI 數字鍵該顯示前綴。`approve_for_session`/`allow-always`/`ack`
// 這類內部識別字印在按鈕上只是雜訊(「ack. 知道了」)。
function approvalOptionLabel(o) {
  return (/^[0-9]$/.test(o.key) ? o.key + ". " : "") + o.label;
}
// bridge 產 options 時就標好語意(APPROVAL_HUB_SPEC §1:`style: danger` =
// 否決類)。這裡只做映射,**不猜 label**。`deny` 是舊字彙,等同 danger。
function approvalStyleClass(style) {
  if (style === "primary" || style === "success") return "ap-yes";
  if (style === "danger" || style === "deny") return "ap-no ap-danger";
  return "ap-neutral";
}

// 複選的勾選狀態。identity 必須含題目/題號 —— 多題 ask 的選項鍵恆為 1|2|3,
// 只看鍵會偵測不到換題、把上一題的勾選帶著送出(iOS 同款教訓)。
let msIdentity = null, msSelected = null;
function msReset() { msIdentity = null; msSelected = null; }
function msState(view) {
  const id = JSON.stringify([view.identity, view.qIndex, view.title, view.options.map(o => o.key)]);
  if (msIdentity !== id || !msSelected) {
    msIdentity = id;
    // 用終端當下的勾選播種:使用者可能已在 tmux 裡勾過幾個,從空集合開始送出
    // 會把那些 toggle 掉(/answer 收的是「作答後應該被勾選的完整集合」)。
    msSelected = new Set(view.options.filter(o => o.checked === true).map(o => o.key));
  }
  return msSelected;
}

function renderApproval() {
  clearTimeout(approvalExpiry);
  const bar = $("approval");
  const s = window.PocketSessionOps?.approvalSession() || currentSession();
  if (!actionable(s)) { bar.classList.add("hidden"); msReset(); return; }
  const meta = s.meta || {};
  const aid = approvalIdOf(meta);
  const view = approvalView(s);
  const index = view.wireQIndex;

  const expires = activityTime(meta.approval?.expires_at);
  if (expires) approvalExpiry = setTimeout(() => { renderApproval(); renderAwaiting(); renderHome(); }, Math.min(Math.max(1, expires - Date.now() + 1), 2147483647));
  bar.classList.remove("hidden");
  $("ap-icon").textContent = APPROVAL_ICON[view.kind] || "🔐";
  $("ap-title").textContent = view.title;

  // risk / detail:規範欄位,bridge 每家都填 —— 不顯示就是盲簽。
  const risk = APPROVAL_RISK[view.risk];
  const riskEl = $("ap-risk");
  riskEl.className = "ap-risk" + (risk ? " ap-risk-" + risk[1] : "");
  riskEl.textContent = risk ? risk[0] : "";
  riskEl.classList.toggle("hidden", !risk);

  const detailEl = $("ap-detail");
  detailEl.textContent = view.detail;
  detailEl.classList.toggle("hidden", !view.detail);

  // 「第 2/3 題」—— 使用者唯一能知道「還有幾題」的地方。
  const prog = $("ap-progress");
  prog.textContent = !(view.qTotal > 1) ? ""
    : view.qIndex === null ? "共 " + view.qTotal + " 題"
    : "第 " + (view.qIndex + 1) + "/" + view.qTotal + " 題";
  prog.classList.toggle("hidden", !prog.textContent);

  const box = $("ap-actions");
  box.replaceChildren();
  const shown = view.options.slice(0, APPROVAL_MAX_OPTS);
  const hidden = view.options.length - shown.length;
  $("ap-more").textContent = hidden > 0 ? "還有 " + hidden + " 個選項沒顯示，請在終端機或 App 作答" : "";
  $("ap-more").classList.toggle("hidden", hidden <= 0);

  let hint = "";
  if (!view.options.length) {
    // 沒有 options 的審批(codex 二元核准)才用寫死的兩鍵。
    const yes = document.createElement("button");
    yes.className = "ap-btn ap-yes"; yes.textContent = "核准";
    yes.addEventListener("click", () => decide(s.id, aid, index, { approve: true }, yes));
    const no = document.createElement("button");
    no.className = "ap-btn ap-no ap-danger"; no.textContent = "否決";
    no.addEventListener("click", () => decide(s.id, aid, index, { approve: false }, no));
    box.append(yes, no);
  } else if (view.multiselect) {
    // ── P0-1:AskUserQuestion 複選 ──
    // 舊版把每個選項當「點了就送」→ 送 {key:"3"} 走 `_cc_key_core`,那是
    // **只按一個數字鍵 = 只 toggle 一個框、永遠不按送出**:畫面完全沒變,
    // 再點一次還把前一次取消。正解是 bridge 早就備好的
    // `POST /ccsessions/{name}/answer`(keys = 作答後應被勾選的完整集合)。
    const picked = msState(view);
    const anyBox = view.options.some(o => o.checked !== null);
    const isBox = o => !anyBox || o.checked !== null;   // 一列 checked 都沒送 → 全部可勾
    const ccName = s.provider === "claude_code" ? s.id.slice(s.id.indexOf(":") + 1) : "";
    const canAnswer = !borrowed() && !!ccName && (!s._hub || s._hubPromptCurrent);
    for (const o of shown) {
      const b = document.createElement("button");
      if (!isBox(o)) {
        // 純動作列(「自行輸入…」/「與 Claude 討論」):TUI 語意就是單鍵即送。
        b.className = "ap-btn ap-neutral";
        b.textContent = approvalOptionLabel(o);
        b.addEventListener("click", () => decide(s.id, aid, index, { key: o.key }, b));
      } else {
        const on = picked.has(o.key);
        b.className = "ap-btn ap-check" + (on ? " on" : "");
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.textContent = (on ? "☑ " : "☐ ") + approvalOptionLabel(o);
        b.disabled = !canAnswer;
        b.addEventListener("click", () => {        // 純前端 toggle,不打網路
          if (picked.has(o.key)) picked.delete(o.key); else picked.add(o.key);
          renderApproval();
        });
      }
      if (o.description) b.title = o.description;
      box.appendChild(b);
    }
    const keys = view.options.filter(o => isBox(o) && picked.has(o.key)).map(o => o.key);
    const sub = document.createElement("button");
    sub.className = "ap-btn ap-submit";
    sub.textContent = (view.submitLabel || "送出") + (keys.length ? "（已選 " + keys.length + " 項）" : "");
    sub.disabled = !keys.length || !canAnswer;
    sub.addEventListener("click", () => answerMulti(s.id, ccName, keys, sub));
    box.appendChild(sub);
    hint = s._hub && !s._hubPromptCurrent ? "此待審項目不是終端目前的題目，請重新整理清單確認"
      : canAnswer ? "可複選 —— 勾好後按送出"
      : "這張是複選題，請在本機 console、App 或終端機作答";
  } else {
    for (const o of shown) {
      const b = document.createElement("button");
      b.className = "ap-btn " + approvalStyleClass(o.style);
      b.textContent = approvalOptionLabel(o);
      if (o.description) b.title = o.description;
      b.addEventListener("click", () => decide(s.id, aid, index, { key: o.key }, b));
      box.appendChild(b);
    }
  }
  $("ap-hint").textContent = hint;
  $("ap-hint").classList.toggle("hidden", !hint);
  if (["not_resumable", "none"].includes(meta.approval?.response_capability)) {
    box.querySelectorAll("button").forEach(b => b.disabled = true);
    $("ap-hint").textContent = "主機已無等待中的請求，無法作答";
    $("ap-hint").classList.remove("hidden");
  }
  if (deciding.has(s.id)) box.querySelectorAll("button").forEach(b => b.disabled = true);
}

// P0-1 的送出路徑。body 的 `keys` 是**作答後應該被勾選的完整集合**
// (bridge.py:20971 的 docstring:TUI 數字是 toggle,盲送會把使用者已勾的
// 切掉),`submit=true` 讓 bridge 走到內嵌送出鈕按下去。
// 這條路只有本機 console 走得到 —— console-remote 的路徑白名單只開
// `/app/v2/sessions/{id}/{op}`(bridge.py:947),所以借用機不畫送出鈕。
async function answerMulti(id, name, keys, btn) {
  if (!id || id !== state.current || deciding.has(id) || signingOut) return;
  const session = window.PocketSessionOps?.approvalSession() || currentSession();
  if (!name || !keys.length || borrowed() || !actionable(session) || (session._hub && !session._hubPromptCurrent) ||
      ["not_resumable", "none"].includes(session.meta?.approval?.response_capability)) return;
  deciding.add(id);
  const box = $("ap-actions");
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  if (btn) btn.classList.add("ap-busy");
  try {
    const r = await api("/ccsessions/" + encodeURIComponent(name) + "/answer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys, submit: true }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      const code = (e.error && e.error.code) || "";
      if (r.status === 409 && ["PROMPT_STALE", "STALE_QUESTION"].includes(code))
        throw new Error("這個問題已經換頁或結束，請看最新訊息");
      // 老 bridge 沒鋪這條路 → **老實說**,不降級送單鍵(送了就是在複選選單
      // 裡亂 toggle,還回報成功 —— app 側同一個拍板,見 CCCardSessionView)。
      if ([404, 405].includes(r.status) && code !== "SESSION_NOT_FOUND")
        throw new Error("這台 bridge 還沒支援複選作答，請更新 bridge 或改在終端機作答");
      throw new Error((e.error && e.error.message) || ("HTTP " + r.status));
    }
    const receipt = await r.json().catch(() => ({}));
    if (!signingOut && state.current === id)
      toast(receipt.submitted === false ? "已送出，請接著答下一題" : "已送出選擇");
  } catch (err) {
    if (!signingOut && state.current === id) toast("無法送出選擇：" + err.message);
  } finally {
    deciding.delete(id);
    msReset();            // 下一輪照終端現況重新播種
    if (!signingOut) { renderApproval(); renderAwaiting(); renderHome(); loadSessions(); }
  }
}

async function decide(id, aid, index, choice, btn) {
  const session = window.PocketSessionOps?.approvalSession() || currentSession();
  if (!id || id !== state.current || deciding.has(id) || signingOut || !actionable(session)) return;
  if (["not_resumable", "none"].includes(session.meta?.approval?.response_capability)) return;
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
    const path = session?._hub ? "/app/v1/approvals/" + encodeURIComponent(aid) + "/decision"
      : "/app/v2/sessions/" + encodeURIComponent(id) + "/approve";
    const r = await api(path, {
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
    if (session?._hub && aid) window.PocketSessionOps?.resolved(aid, receipt);
    if (!signingOut && state.current === id) toast(receipt.status === "next_question" ? "已送出，請接著答下一題"
      : receipt.idempotent ? "這項核准已處理" : "已送出決定");
  } catch (err) {
    if (!signingOut && state.current === id) toast("無法確認決定：" + err.message, true);
  } finally {
    deciding.delete(id);
    msReset();
    if (!signingOut) { renderApproval(); renderAwaiting(); renderHome(); loadSessions(); }
  }
}

// 跨 session 彙總:本機從 hub 計算 approval 數;借用機僅看授權 session,不讀全域 hub。
function renderAwaiting() {
  if (window.PocketSessionOps?.renderAwaiting()) return;
  const n = state.sessions.filter(actionable).length;
  const el = $("awaiting");
  $("awaiting-n").textContent = String(n);
  el.classList.toggle("hidden", n === 0);
  notifyAwaiting(n);
}

// 點徽章 → 跳到第一條在等的 session
function jumpToAwaiting() {
  if (window.PocketSessionOps?.toggleHub()) return;
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
      // seq 是**全域** event_log 序號:別的 session 的事件會在中間佔號,
      // 本 lane 看到的序號天生有洞。「不連號就 resync」= 永遠在重同步
      // (2026-11-10 機主實錘:進 session 卡死在「連線中斷,正在補接」)。
      // 正解同 app 端 TerminalCardStore:只要往前就收,洞是正常的;
      // 真正的斷層由 410/resync 協定負責。
      if (envelope.type === "ping") {
        // ping 帶全域尖端,僅用來推進重連游標,絕不觸發 resync。
        if (seq > state.latestSeq) state.latestSeq = seq;
        return;
      }
      if (seq <= state.latestSeq) return;
      if (envelope.type === "card.upsert" && payload.card) {
        upsertCard(payload.card);
        confirmEcho(id, [payload.card]);
      } else if (envelope.type === "session.status") {
        updateStatusChip(payload.phase, payload);
        reflectSessionStatus(id, payload.phase, payload);
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

// 視覺卡(body.visual.blocks)的純文字萃取:複製/回覆/分享要的是「可貼走的文字」,
// 不是 HTML。人格(hermes)的回覆多是視覺卡 —— 文字住在 blocks / visual.fallback_text,
// 不在 body.text。少了這條,cardText 對人格訊息回空字串,三個動作全拿到空的 → 看似壞掉。
function visualText(visual) {
  if (!visual || typeof visual !== "object") return "";
  const fromBlocks = Array.isArray(visual.blocks) ? visual.blocks.map((b) => {
    if (!b || typeof b !== "object") return "";
    const type = String(b.type || "").toLowerCase();
    if (type === "text" || type === "markdown") return b.text || "";
    if (type === "html") return b.fallback_text || b.text || "";
    if (type === "link" || type === "file") return b.label || b.filename || b.url || b.download_url || "";
    if (type === "image") return b.alt || "";
    return "";
  }).filter(Boolean).join("\n").trim() : "";
  return fromBlocks || String(visual.fallback_text || "").trim();
}

function cardText(c) {
  const b = c.body || {};
  return b.text || b.fallback_text || b.typed_text ||
    (b.visual ? visualText(b.visual) : "") ||
    (typeof c.body === "string" ? c.body : "");
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
// agent 的明確附檔標記 [[attach:/路徑]] 只是給 bridge 看的信號,顯示時濾掉
// (檔案已另外掛成附件泡泡)——不然使用者會看到一串生的標記文字。媒體掃描讀的
// 是 body 原文、不經這裡,所以濾掉只影響「人看的」那層。
const ATTACH_MARKER_RE = /\[\[attach:[^\]\n]+\]\]/g;
function proseHtml(c, text) {
  text = String(text == null ? "" : text).replace(ATTACH_MARKER_RE, "").replace(/[ \t]+\n/g, "\n").trim();
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
  } catch (err) { toast("下載失敗:" + err.message, true); }
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
  } catch (_) { toast("無法複製(瀏覽器不允許)", true); }
}

// 訊息動作列(交付 6):回覆=引用塞進輸入框;複製=原始文字;分享=系統分享
// (navigator.share),沒有就退「複製全文」。轉貼到別條 session 需要後端支
// 援,先不硬造。
function handleMessageAction(act, cardId) {
  const card = state.cards.find((c) => c.id === cardId);
  if (!card) { toast("這則訊息已不在目前視窗", true); return; }
  const text = cardText(card);
  if (act === "copy") { void copyText(text); return; }
  if (act === "reply") {
    if (!canUse("input")) { toast("這台電腦無法在此對話輸入", true); return; }
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
  window.PocketSessionOps?.scheduleRead();
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
  window.PocketSessionOps?.scheduleRead();
}

function setStreamHealth(label, working = false) {
  const el = $("stream-health");
  el.textContent = label; el.classList.toggle("working", working);
}
function statusText(phase, meta = {}) {
  const queue = Number.isSafeInteger(meta.queue_depth) && meta.queue_depth > 0 ? meta.queue_depth : 0;
  if (meta.locked) return meta.lock_message || ("被佔用" + (queue ? " · " + queue + " 則訊息排隊中" : ""));
  let text = PHASE_LABEL[phase] || phase || "狀態未知";
  if (queue) text += " · " + queue + " 則訊息排隊中";
  const reset = activityTime(meta.usage_limit_reset_at);
  if (phase === "usage_limited" && reset) text += " · " + activityFormat.format(reset) + " 重置";
  return text;
}
function updateStatusChip(phase, meta = {}) {
  meta = meta || {};
  const wasBusy = state.busy;                 // 完成偵測:忙 → 不忙 = 這回合做完了
  const chip = $("sh-status");
  const locked = !!meta.locked;
  const label = statusText(phase, meta);
  chip.textContent = label;
  chip.className = "status-chip st-" + (locked ? "locked" : phase || "idle");
  state.statusRevision = (state.statusRevision || 0) + 1;
  state.statusMeta = meta;
  state.phase = phase || "idle";
  state.busy = ["run", "running", "busy", "dispatching"].includes(phase);
  chip.classList.toggle("working", state.busy && !locked);
  $("interrupt").classList.toggle("hidden", !state.busy || !canUse("interrupt"));
  // 交付 4:輕量活動列。busy / 排隊中都算「在忙」;phase label(bridge 的
  // 人話 label,例如「正在執行 Read…」)有就用,elapsed 以 busy_since 起算。
  state.statusLabel = (typeof meta.label === "string" && meta.label) || "";
  state.statusDetail = label;
  state.activityShimmer = !!state.statusLabel && state.busy && !locked;
  const working = !locked && (state.busy || phase === "queued");
  if (working) {
    const since = activityTime(meta && meta.busy_since);
    if (since) busyStartAt = since;
    else if (!busyStartAt) busyStartAt = Date.now();
  } else busyStartAt = 0;
  syncBusyBar(working);
  // 回合完成(忙→不忙、非鎖定、非需你處理):完成音＋通知(分頁隱藏才發通知)。
  if (wasBusy && !state.busy && !locked && phase !== "needs_input" && phase !== "waiting_approval") {
    resetForceStop();                         // 停止已自然達成,收掉強制停止升級
    soundDone();
    maybeNotify("回合完成", currentSession()?.title ? (currentSession().title + " 已完成") : "工作已完成");
  }
  // 限額:排一個「重置時提醒可以繼續了」(對齊手機的 Resume;真正自動續跑需後端)。
  if (phase === "usage_limited") {
    const reset = activityTime(meta.usage_limit_reset_at);
    if (reset && reset !== usageResumeAt) {
      usageResumeAt = reset;
      clearTimeout(usageResumeTimer);
      const ms = reset - Date.now();
      if (ms > 0 && ms < 6 * 3600 * 1000) usageResumeTimer = setTimeout(() => {
        soundNeedsInput();
        maybeNotify("可以繼續了", "用量限額已重置,這條可以恢復執行");
      }, ms);
    }
  } else if (usageResumeAt) { usageResumeAt = 0; clearTimeout(usageResumeTimer); usageResumeTimer = null; }
}
let usageResumeTimer = null, usageResumeAt = 0;

let busyTimer = null, busyStartAt = 0, busyShowTimer = null, busyHideTimer = null, busySession = null;
function syncBusyBar(on) {
  const bar = $("workbar");
  on = !!on && !!state.current && !signingOut;
  const hide = () => {
    clearInterval(busyTimer); busyTimer = null;
    bar.classList.add("hidden");
    $("workbar-label").classList.remove("activity-shimmer");
  };
  if (busySession !== state.current || signingOut) {
    clearTimeout(busyShowTimer); busyShowTimer = null;
    clearTimeout(busyHideTimer); busyHideTimer = null;
    hide(); busySession = state.current;
  }
  if (!on) {
    clearTimeout(busyShowTimer); busyShowTimer = null;
    clearInterval(busyTimer); busyTimer = null;
    $("workbar-label").classList.remove("activity-shimmer");
    if (!bar.classList.contains("hidden") && !busyHideTimer) {
      busyHideTimer = setTimeout(() => { busyHideTimer = null; hide(); }, 300);
    }
    return;
  }
  clearTimeout(busyHideTimer); busyHideTimer = null;
  const tick = () => {
    const detail = state.statusMeta?.queue_depth > 0 ? state.statusDetail : "";
    $("workbar-label").textContent = [state.statusLabel, detail].filter(Boolean).join(" · ") ||
      (state.phase === "queued" ? "已排入佇列…" : "Agent 工作中…");
    $("workbar-label").classList.toggle("activity-shimmer", state.activityShimmer);
    const secs = Math.max(0, Math.round((Date.now() - busyStartAt) / 1000));
    $("workbar-elapsed").textContent = secs >= 60
      ? Math.floor(secs / 60) + " 分 " + (secs % 60) + " 秒" : secs + " 秒";
  };
  const show = () => {
    busyShowTimer = null;
    bar.classList.remove("hidden"); tick();
    if (!busyTimer) busyTimer = setInterval(tick, 1000);
  };
  if (!bar.classList.contains("hidden")) show();
  else if (!busyShowTimer) busyShowTimer = setTimeout(show, 1500);
}

// 別的客戶端(手機)發起的回合也要亮:per-session 事件一到,先就地改掉
// 列表裡那一列的狀態再排程抓清單,不等 800ms debounce。
function reflectSessionStatus(id, phase, meta) {
  const s = state.sessions.find((x) => x.id === id);
  if (!s || !phase) return;
  if (meta) s.meta = {...s.meta, ...meta};
  else if (s.status === phase) return;
  s.status = phase;
  s._statusRevision = state.statusRevision;
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
  $("delivery-text").classList.toggle("working", ["checking","sending","verifying"].includes(pending?.phase));
  $("check-delivery").hidden = !pending || pending.phase !== "unknown";
  // 自動確認(verifying)期間查詢鈕不用出(機器正在查),但「結束追蹤」要
  // 可按,讓使用者在等待間隔及網路查詢期間都能退出。
  $("forget-delivery").hidden = !pending || !["unknown", "verifying"].includes(pending.phase);
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
  if (d.atts.length && !canUse("attachments")) { toast("這條對話不允許附件，請先移除", true); return; }
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
    // POST 逾時/斷線 ≠ 沒送到 —— CC 的投遞走 tmux,慢起來會超過 90s,而指令
    // 其實已經在跑(2026-11-13 機主回報「多次送出好像沒成功」,就是每次都
    // 被丟進手動確認)。所以先**自動**查收據(憑 client_id 比對),三次都
    // 找不到才降級成手動的「查詢送達/結束追蹤」。
    autoVerifyDelivery(id, d, p);
  }
}

/// 送出結果不明時的自動確認:2s/8s/20s 三次查收據,中間使用者仍可「結束追蹤」。
async function autoVerifyDelivery(id, d, p) {
  p.phase = "verifying";
  for (const wait of [2000, 8000, 20000]) {
    d.notice = "連線不穩，正在確認送達…(可按「結束追蹤」放棄等待)"; showDraft(id);
    await new Promise(r => setTimeout(r, wait));
    if (d.pending !== p) return;          // SSE 收據先到、或使用者已結束追蹤
    try {
      const snap = await apiJSON("/app/v2/sessions/" + encodeURIComponent(id) + "/cards?limit=100",
        { signal: AbortSignal.timeout(15000), cache: "no-store" });
      if (d.pending !== p) return;
      if (confirmEcho(id, snap.cards || [])) return;   // 找到收據 → 解鎖,結案
    } catch (_) { if (d.pending !== p) return; }
  }
  p.phase = "unknown"; d.notice = "送達狀態不明，請先確認。未重新送出。"; showDraft(id);
}

// 「停止沒反應」的沉默失敗現形(t3code #1 病:silent failure):按了中斷後若 6 秒
// 仍在忙 → 把按鈕升級成「強制停止」,讓使用者看得到「剛剛那下沒生效」,再按一次
// 會再送一次中斷(目前沒有獨立 force 端點,重送是 best-effort,但至少不再靜默)。
let forceStopTimer = null, interruptBaseText = "";
function armForceStop() {
  const btn = $("interrupt"); if (!btn) return;
  if (!interruptBaseText) interruptBaseText = btn.textContent || "中斷";
  clearTimeout(forceStopTimer);
  forceStopTimer = setTimeout(() => {
    if (!state.busy) return;                 // 已經停了 → 不升級
    btn.classList.add("force");
    btn.textContent = "強制停止";
    toast("停止沒反應?再按一次強制停止", true);
  }, 6000);
}
function resetForceStop() {
  clearTimeout(forceStopTimer); forceStopTimer = null;
  const btn = $("interrupt");
  if (btn && interruptBaseText) { btn.classList.remove("force"); btn.textContent = interruptBaseText; }
}

async function interrupt() {
  if (!state.current || !canUse("interrupt")) return;
  try {
    const r = await api("/app/v2/sessions/" + encodeURIComponent(state.current) + "/interrupt", { method: "POST" });
    if (r.status === 409) { toast("目前沒有進行中的回合"); resetForceStop(); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    toast($("interrupt").classList.contains("force") ? "已再送出強制中斷" : "已送出中斷");
    armForceStop();                          // 盯 6 秒,沒停下來就現形
  } catch (err) { toast("中斷失敗:" + err.message, true); }
}

// ───────────────────────── UI 小工具 ─────────────────────────
function setConn(ok, text) {
  $("conn").className = "conn " + (ok ? "ok" : "bad");
  $("conn-text").textContent = text;
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function toast(msg, error = false) {
  const t = $("toast");
  // Keep failures until dismissed; a later success must not erase them.
  if ([...t.children].some(n => n.dataset.message === msg)) return;
  const row = document.createElement("div");
  row.className = "toast-item" + (error ? " toast-error" : "");
  row.dataset.message = msg;
  const text = document.createElement("span"); text.textContent = msg;
  const close = document.createElement("button");
  close.type = "button"; close.className = "icon-btn"; close.textContent = "×";
  close.setAttribute("aria-label", "關閉通知"); close.title = "關閉通知";
  const dismiss = () => {
    const hadFocus = row.contains(document.activeElement);
    row.remove(); t.classList.toggle("hidden", !t.children.length);
    if (hadFocus) {
      const next = t.querySelector("button");
      if (next) next.focus(); else if (state.current) focusComposer(); else $("home-q").focus({preventScroll:true});
    }
  };
  close.addEventListener("click", dismiss);
  row.append(text, close); t.append(row); t.classList.remove("hidden");
  if (!error) setTimeout(dismiss, 3000);
}
function autosize() {
  const ta = $("input"); ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

// ───────────────────────── 綁定 + 啟動 ─────────────────────────
function wire() {
  window.PocketSessionOps?.init();
  // Safari's software keyboard shrinks the visual, not the layout viewport.
  const fitViewport = () => {
    if (mobileConsole() && window.visualViewport?.scale === 1)
      $("app").style.setProperty("--app-height", window.visualViewport.height + "px");
    else $("app").style.removeProperty("--app-height");
  };
  window.visualViewport?.addEventListener("resize", fitViewport);
  window.addEventListener("resize", fitViewport); fitViewport();
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
  $("sess-q").addEventListener("input", (e) => { state.listQuery = e.target.value.trim(); renderSessions(); });
  $("sess-filters").addEventListener("click", (e) => {
    const chip = e.target.closest(".sf-chip");
    if (!chip) return;
    state.listFilter = chip.dataset.filter || "all";
    $("sess-filters").querySelectorAll(".sf-chip").forEach((c) =>
      c.classList.toggle("active", c === chip));
    renderSessions();
  });
  $("sc-close").addEventListener("click", hideShortcuts);
  $("shortcuts").addEventListener("click", (e) => { if (e.target.id === "shortcuts") hideShortcuts(); });
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
    if (["unknown", "verifying"].includes(d.pending?.phase) && confirm("指令可能已在執行。結束追蹤並清空這則草稿？不會重送或停止工作。"))
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
  const ids = Array.from($("session-list").querySelectorAll(".sess"), n => n.dataset.id);
  if (!ids.length) return;
  const i = ids.indexOf(state.current);
  const next = ids[Math.max(0, Math.min(ids.length - 1, (i < 0 ? 0 : i + delta)))];
  if (next && next !== state.current) {
    openSession(next, false, false);
    const row = Array.from($("session-list").querySelectorAll(".sess")).find(n => n.dataset.id === next);
    row?.focus({preventScroll:true}); row?.scrollIntoView({block:"nearest"});
  }
}
// 鍵盤 `a`(核准)/ `d`(否決)要按到哪一顆。
//
// ⚠️ **查不到就回 null,不猜。** 舊版找不到 `.ap-yes`/`.ap-no` 就退回
// `btns[btns.length - 1]`,而資料驅動的按鈕以前根本沒掛這兩個 class ——
// 於是 `d` 按的是選單最後一項,那可能是「本次全允許」之類的破壞性選項:
// 想否決的人反而一次放行到底。語意只認 bridge 標在 option 上的 `style`
// (APPROVAL_HUB_SPEC §1),沒標語意的選單(question / 複選)就沒有快捷鍵。
function firstApprovalButton(wantYes) {
  if ($("approval").classList.contains("hidden") ||
      !actionable(window.PocketSessionOps?.approvalSession() || currentSession())) return null;
  const sel = wantYes ? "button.ap-yes" : "button.ap-no";
  return $("ap-actions").querySelector(sel + ":not(:disabled)") || null;
}
// ── IME 三重判準(中文/日文選字按 Enter 不准送出)────────────────────────
// 1. `event.isComposing` —— 多數引擎在組字期間的標準訊號。
// 2. `keyCode === 229` —— 舊引擎(含部分 Safari/Android IME)不給 isComposing
//    時唯一的線索。
// 3. `recentlyComposing()` —— **Safari 的「選字確認」keydown 比 compositionend
//    晚到**,只看前兩條會在 compositionend 之後收到一顆乾淨的 Enter,把半成品
//    直接送出去(機主打中文最常踩的那一下)。compositionend 後再守 10ms。
//
// 三條缺一不可:只有 (1)+(2) 的版本在 Safari/iPad 上會誤送;只有 (3) 的版本
// 在組字途中按 Enter(engine 不發 compositionend)會放行。
const IME_TAIL_MS = 10;
let composing = false;
let composingUntil = 0;
const nowMs = () => Date.now();
const recentlyComposing = () => composing || nowMs() < composingUntil;
const imeKey = e => e?.isComposing === true || e?.keyCode === 229 || recentlyComposing();
function imeCompositionStart() { composing = true; composingUntil = 0; }
function imeCompositionEnd() { composing = false; composingUntil = nowMs() + IME_TAIL_MS; }
function imeReset() { composing = false; composingUntil = 0; }
// terminal.js / memory.js 的輸入框共用同一顆守衛(app.js 先載,見 index.html)。
if (typeof window !== "undefined")
  window.PocketIME = { imeKey, recentlyComposing, TAIL_MS: IME_TAIL_MS };
function wireKeys() {
  document.addEventListener("compositionstart", imeCompositionStart);
  document.addEventListener("compositionend", imeCompositionEnd);
  window.addEventListener("blur", imeReset);
  document.addEventListener("keydown", (e) => {
    if (imeKey(e)) return;
    if ($("session-ops")?.open) return; // Native dialog owns focus and Escape.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); openPalette(); return;
    }
    if (e.key === "Escape") {
      if (!$("shortcuts").classList.contains("hidden")) { hideShortcuts(); return; }
      closePalette(); return;
    }
    if (!$("palette").classList.contains("hidden")) {
      const rows = Array.from($("pal-list").querySelectorAll(".work-row"));
      const i = rows.indexOf(document.activeElement);
      if (rows.length && ["ArrowDown", "ArrowUp"].includes(e.key)) {
        e.preventDefault();
        rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))].focus();
      } else if (!imeKey(e) && e.key === "Enter" && document.activeElement === $("pal-q") && rows.length) {
        e.preventDefault(); rows[0].click();
      } else if (e.key === "Tab") {
        const stops = [$("pal-q"), ...rows], at = stops.indexOf(document.activeElement);
        e.preventDefault(); stops[(at + (e.shiftKey ? -1 : 1) + stops.length) % stops.length].focus();
      }
      return;
    }
    if (typingNow()) return;
    if (e.key === "?") { e.preventDefault(); toggleShortcuts(); return; }
    if (e.key === "j") { e.preventDefault(); moveSelection(1); }
    else if (e.key === "k") { e.preventDefault(); moveSelection(-1); }
    else if (e.key === "a" || e.key === "d") {
      const b = firstApprovalButton(e.key === "a");
      if (b && !b.disabled) { e.preventDefault(); b.click(); }
    }
  });
}

// ⌘K 快速切換面板
let paletteFocus = null;
function hideShortcuts() { $("shortcuts").classList.add("hidden"); }
function toggleShortcuts() { $("shortcuts").classList.toggle("hidden"); }

function openPalette() {
  const p = $("palette");
  if (p.classList.contains("hidden")) paletteFocus = document.activeElement;
  p.classList.remove("hidden");
  const q = $("pal-q"); q.value = ""; renderPalette(""); q.focus();
}
function closePalette(restore = true) {
  if ($("palette").classList.contains("hidden")) return;
  $("palette").classList.add("hidden");
  if (restore && paletteFocus?.isConnected) paletteFocus.focus({preventScroll:true});
  paletteFocus = null;
}
function renderPalette(q) {
  const needle = q.trim().toLowerCase();
  const hits = state.sessions.filter(s => workMatches(s, needle));
  $("pal-list").innerHTML = hits.length
    ? hits.map(workRow).join("")
    : '<div class="empty">沒有符合的 session</div>';
  $("pal-list").querySelectorAll(".work-row").forEach((n) =>
    n.addEventListener("click", () => { closePalette(false); openSession(n.dataset.id); }));
}

// ───────────────────────── P1:開新工作 ─────────────────────────
async function createSession() {
  if (borrowed()) return;
  const kind = $("new-kind").value;
  const name = $("new-name").value.trim();
  const wd = $("new-dir").value.trim();
  if (kind === "cc" && !name) { toast("CC 需要 session 名稱", true); return; }
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
  } catch (err) { toast("開新工作失敗:" + err.message, true); }
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
    if (f.size > ATT_MAX_BYTES) { toast(f.name + " 超過 8MB,改用檔案路徑引用", true); return; }
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
  soundNeedsInput();                         // 需要你:促請音(即使在看也響一聲,like 手機)
  maybeNotify("有 " + n + " 件需要你", "Pocket 有對話等你處理");
  if (!document.hidden) return;              // 人就在看 → 不閃標題
  clearInterval(titleTimer);
  let on = false;
  titleTimer = setInterval(() => {
    on = !on;
    document.title = on ? "🔴 " + n + " 件待審核" : baseTitle;
  }, 1100);
}
function stopBlink() { clearInterval(titleTimer); titleTimer = null; document.title = baseTitle; }
document.addEventListener("visibilitychange", () => { if (!document.hidden) stopBlink(); });
// 單音(WebAudio 合成,不帶資產檔,CSP 安全)。
function playTone(freq, start = 0, dur = 0.3, peak = 0.08) {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!playTone._ac) playTone._ac = new AC();
    const ac = playTone._ac, t0 = ac.currentTime + start;
    const o = ac.createOscillator(), g = ac.createGain();
    o.type = "sine"; o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(ac.destination); o.start(t0); o.stop(t0 + dur + 0.02);
  } catch (e) { /* 瀏覽器不給音訊就算了,標題/通知仍在 */ }
}
// 兩種語意音效(對齊手機):需要你 = 單高音促請;完成 = 柔和上行兩音。
function soundNeedsInput() { playTone(880, 0, 0.32); }
function soundDone() { playTone(660, 0, 0.16, 0.06); playTone(990, 0.14, 0.26, 0.06); }
function beep() { soundNeedsInput(); }   // 相容舊呼叫

// 系統通知(對齊手機推播的桌面版)。權限在使用者手勢時才要(ensureNotifyPermission),
// 只在分頁隱藏時發 —— 人就在看就別吵。拿不到權限就退回音效＋標題閃。
function ensureNotifyPermission() {
  try {
    if (borrowed() || !("Notification" in window)) return;   // 借用裝置不打擾
    if (Notification.permission === "default") Notification.requestPermission().catch(() => {});
  } catch (_) {}
}
function maybeNotify(title, body) {
  try {
    if (!document.hidden || borrowed()) return;              // 在看 or 借用機 → 不發
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const n = new Notification(title, { body: body || "", tag: "pocket-console", renotify: true });
    n.onclick = () => { try { window.focus(); n.close(); } catch (_) {} };
  } catch (_) {}
}

async function main() {
  wire();
  if (!(await bootstrap())) return;
  await identifyPersonalLogin();
  if (sessionStorage.getItem(LOGOUT_EVENT)) { await leaveConsole(); return; }
  await loadSessions();
  void window.PocketSessionOps?.loadCapabilities();
  void window.PocketSessionOps?.loadHub();
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
    // 2026-11-21(W1-⑤):從常駐 10s 輪詢改訂閱 roster 差分 SSE(已進遠端白名單,
    // bridge 端已把 last_turn_summary 消毒掉 —— 遠端不會收到真實訊息內容,
    // 只有「有變化了」這個信號)。收到 sessions.changed 就照舊整包重抓
    // /app/v2/sessions(本來就有完整的遠端授權投影,不改資料形狀依賴)。
    // 斷線時 sseStream 自己指數退避重連;visibilitychange 保留當補漏安全網
    // (分頁被瀏覽器凍結太久,回來時連線可能早就斷了卻還沒偵測到)。
    state.stopList = sseStream("/app/v2/sessions/roster/events", state.token, {
      onEvent: ({data}) => { if (data?.type === "sessions.changed") scheduleListRefresh(); },
      onError: () => setConn(false, "清單即時連線斷線,重連中…"),
      onOpen: () => {},
      onDenied: code => { if (code === 401) void leaveConsole(); else setConn(false, "無權讀取清單更新"); },
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !signingOut) loadSessions(); });
    return;
  }
  let listCursor = 0;
  state.stopList = sseStream(() => "/app/v2/events?follow=true&since_seq=" + listCursor, state.token, {
    onEvent: ({data}) => {
      if (!Number.isSafeInteger(data?.seq) || data.seq <= listCursor) return;
      listCursor = data.seq;
      handleListEvent(data);
    },
    onError: () => setConn(false, "事件流斷線,重連中…"),
    onOpen: () => {},
    onDenied: code => { if (code === 401) void leaveConsole(); else setConn(false, "無權讀取事件"); },
  });
}
let listTimer = null;
function scheduleListRefresh() { clearTimeout(listTimer); listTimer = setTimeout(loadSessions, 800); }

// 全域事件分派。舊版**連 `data.type` 都沒讀**,每一種型別都塌縮成同一個
// 800ms debounce 重抓 —— `approval.resolved`(bridge.py:3093)就是為了
// 「所有端收到即收卡,不留懸空」才加的 persisted 事件,被當成刷新鈴等於
// 把它的意義丟掉:別的端答掉之後,這裡還掛著一顆按下去吃 409 的按鈕。
//
// Message/read deltas update in place; approvals still reconcile immediately.
function handleListEvent(ev) {
  window.PocketSessionOps?.onEvent(ev);
  if (ev?.type === "approval.resolved") {
    noteApprovalResolved(ev.data?.approval_id);
    renderApproval(); renderAwaiting(); renderHome(); renderSessions();
    clearTimeout(listTimer); listTimer = null;
    loadSessions();          // 立即對帳,不等 debounce
    return;
  }
  if (!applyListEvent(ev)) scheduleListRefresh();
}

main();
