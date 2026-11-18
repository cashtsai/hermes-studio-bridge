// Console 待審核(P0)的行為守門 —— 跑的是**出貨的那份 app.js**(用 vm 抽段
// 執行,不重寫一份),所以測試不會跟實作漂開。
//
// 釘住的四件事,每一件都對著一個真實病症:
//   1. 能力驅動改 fail-open 後**舊行為不能退化** —— 本機模式不准突然藏掉
//      原本能用的東西(hermes 的中斷、本機的變更檢視)。
//   2. CC AskUserQuestion 複選:送的是「作答後應被勾選的完整集合」+ submit,
//      而且勾選本身**不打網路**(舊版每點一下就送一個數字鍵 = 在 TUI 裡亂
//      toggle,畫面完全沒變)。
//   3. option 的 `style` → danger class 的映射;`firstApprovalButton` 查不到
//      語意就**不動作**(舊版退回最後一顆 → 鍵盤 d 按到「全部允許」)。
//   4. `approval.resolved` 立即收卡,不等 800ms debounce。
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../static/console/app.js"), "utf8");
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `source section: ${start}`);
  return source.slice(a, b);
}
const APPROVAL_SRC = section("function approvalIdOf(meta) {", "function renderAwaiting()");
const KEYS_SRC = section("function firstApprovalButton(", "// ── IME 三重判準");
const CAPS_SRC = section("function borrowed() {", "function syncControls(");
const EVENTS_SRC = section("let listTimer = null;", "main();");

// ───────────────────────── 迷你 DOM(零依賴)─────────────────────────
// 只實作審批區塊真的用到的那幾個 API。刻意不引 jsdom:這個 repo 的前端沒有
// build、沒有 node_modules,測試也不該是第一個長出依賴的人。
function makeDom(ids) {
  class El {
    constructor(tag) {
      this.tagName = String(tag).toUpperCase();
      this.children = [];
      this._text = "";
      this._cls = new Set();
      this.attrs = {};
      this.handlers = {};
      this.disabled = false;
      this.title = "";
      this.hidden = false;
    }
    get classList() {
      const cls = this._cls;
      return {
        add: (...c) => c.forEach(x => cls.add(x)),
        remove: (...c) => c.forEach(x => cls.delete(x)),
        contains: c => cls.has(c),
        toggle: (c, on) => { if (on === undefined) on = !cls.has(c); on ? cls.add(c) : cls.delete(c); return on; },
      };
    }
    get className() { return Array.from(this._cls).join(" "); }
    set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
    get textContent() {
      return this.children.length ? this.children.map(c => c.textContent).join("") : this._text;
    }
    set textContent(v) { this._text = v == null ? "" : String(v); this.children = []; }
    set innerHTML(v) { assert.equal(v, "", "只允許用 innerHTML=\"\" 清空"); this.children = []; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
    removeAttribute(k) { delete this.attrs[k]; }
    appendChild(n) { this.children.push(n); return n; }
    append(...ns) { ns.forEach(n => this.children.push(n)); }
    replaceChildren(...ns) { this.children = ns; }
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
    click() {
      if (this.disabled) return;
      (this.handlers.click || []).forEach(fn => fn({}));
    }
    descendants() {
      return this.children.flatMap(c => [c, ...c.descendants()]);
    }
    _match(sel) {
      // 支援 `button`、`button.cls`、`button.cls:not(:disabled)`。
      let s = sel.trim(), notDisabled = false;
      if (s.endsWith(":not(:disabled)")) { notDisabled = true; s = s.slice(0, -":not(:disabled)".length); }
      const [tag, ...cls] = s.split(".");
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      if (!cls.every(c => this._cls.has(c))) return false;
      return !(notDisabled && this.disabled);
    }
    querySelectorAll(sel) { return this.descendants().filter(n => n._match(sel)); }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  }
  const byId = new Map(ids.map(id => [id, new El("div")]));
  const document = {
    body: { dataset: { consoleAccess: "local" } },
    createElement: tag => new El(tag),
    getElementById: id => byId.get(id) || null,
  };
  return { document, byId, El };
}

const AP_IDS = ["approval", "ap-icon", "ap-title", "ap-risk", "ap-progress",
  "ap-detail", "ap-hint", "ap-actions", "ap-more"];

/** 建一個跑得動審批區塊的 vm context(DOM + 樁)。 */
function harness(opts = {}) {
  const { document, byId } = makeDom(AP_IDS);
  const calls = [];                 // 送出去的網路請求(測試斷言用)
  const toasts = [];
  const responses = opts.responses ? opts.responses.slice() : [];
  const ctx = {
    document,
    window: {},
    console,
    state: { sessions: [], current: null, listHealth: "ready", degraded: [], view: 0 },
    signingOut: false,
    calls, toasts,
    setTimeout: () => 0, clearTimeout: () => {},   // 到期重繪在測試裡不需要真的排程
    $: id => document.getElementById(id),
    borrowed() { return document.body.dataset.consoleAccess === "remote"; },
    currentSession() { return ctx.state.sessions.find(x => x.id === ctx.state.current) || null; },
    activityTime(v) {
      const ms = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : NaN;
      return ms > 0 ? ms : 0;
    },
    toast: m => toasts.push(m),
    renderAwaiting() {}, renderHome() {}, renderSessions() {},
    loadSessions() { calls.push({ path: "loadSessions" }); },
    async api(path, init) {
      calls.push({ path, method: init?.method, body: init?.body && JSON.parse(init.body) });
      const r = responses.shift() || { ok: true, status: 200, json: {} };
      return { ok: r.ok, status: r.status, json: async () => r.json };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(APPROVAL_SRC + "\n" + KEYS_SRC + "\n"
    + ["renderApproval", "actionable", "approvalView", "approvalStyleClass",
       "approvalOptionLabel", "noteApprovalResolved", "msReset", "firstApprovalButton",
       "decide", "answerMulti"].map(n => `globalThis.${n}=${n};`).join(""), ctx);
  return { ctx, byId, calls, toasts, document };
}

const future = Date.now() / 1000 + 600;
const CC_CAPS = ["input", "interrupt", "keys", "attachments", "replay", "follow", "approve"];

function ccMultiRow(over = {}) {
  return {
    id: "claude_code:Ops", provider: "claude_code", title: "Ops",
    capabilities: CC_CAPS,
    meta: {
      prompt: {
        kind: "menu", semantic: "question", multiselect: true,
        title: "要先處理哪幾件?", submit_label: "Submit",
        q_index: over.qIndex === undefined ? 0 : over.qIndex, q_total: 3,
        options: over.options || [
          { key: "1", label: "甲", checked: true },
          { key: "2", label: "乙", checked: false },
          { key: "3", label: "丙", checked: false },
          { key: "4", label: "Type something else" },   // 無 checked = 動作列
        ],
      },
      approval: {
        id: "cc-1", status: "pending", kind: "question", risk: "low",
        session_id: "claude_code:Ops", provider: "claude_code", expires_at: future,
        detail: "session: Ops\n要先處理哪幾件?",
        meta: { multiselect: true, q_index: 0, q_total: 3 },
        options: [{ key: "1", label: "甲", checked: true }],
      },
    },
  };
}
function codexRow(options, over = {}) {
  return {
    id: "codex:01JD", provider: "codex", title: "codex",
    capabilities: ["input", "interrupt", "attachments", "replay", "follow", "approve"],
    meta: {
      approval: Object.assign({
        id: "codex-1", status: "pending", kind: "permission", risk: "high",
        session_id: "codex:01JD", provider: "codex", expires_at: future,
        title: "Codex 要套用這個 patch", detail: "applyPatch · 3 檔變更\n  M bridge.py",
        options,
      }, over),
    },
  };
}
function open(h, row) {
  h.ctx.state.sessions = [row];
  h.ctx.state.current = row.id;
  h.ctx.msReset();
  h.ctx.renderApproval();
}
const buttons = h => h.byId.get("ap-actions").children;
const labels = h => buttons(h).map(b => b.textContent);

// ═════════════ 1. 能力驅動:fail-open + hide-only,舊行為不准退化 ═════════
function capsCtx() {
  const ctx = { window: {}, document: { body: { dataset: { consoleAccess: "local" } } },
                state: { sessions: [], current: null, listHealth: "ready" },
                currentSession() { return ctx.state.sessions.find(x => x.id === ctx.state.current) || null; } };
  vm.createContext(ctx);
  vm.runInContext(CAPS_SRC + "\nglobalThis.canUse=canUse;globalThis.borrowed=borrowed;", ctx);
  const use = (row, access = "local", health = "ready") => {
    ctx.document.body.dataset.consoleAccess = access;
    ctx.state.listHealth = health;
    ctx.state.sessions = row ? [row] : [];
    ctx.state.current = row ? row.id : null;
    return cap => ctx.canUse(cap);
  };
  return use;
}

test("本機:宣告缺席的能力一律放行 —— 不准藏掉原本能用的東西(退化守門)", () => {
  const use = capsCtx();
  // hermes 人格列(bridge.py:22067)**沒有宣告 interrupt**,但
  // /app/v2/sessions/{id}/interrupt 的 hp 分支是真的通的(bridge.py:23066)。
  const hermes = { id: "hermes:cash", provider: "hermes",
                   capabilities: ["input", "attachments", "replay", "follow", "approve"] };
  const can = use(hermes);
  assert.equal(can("interrupt"), true, "中斷鈕本機還能按,不准因為沒宣告就消失");
  // `changes` 只有 console-remote 的授權面會加,本機清單永遠不帶。
  assert.equal(can("changes"), true, "本機的變更檢視不准被能力表關掉");
  assert.equal(can("input"), true);
  assert.equal(can("approve"), true);
});

test("本機:明確宣告了卻沒有那一項 → 真的隱藏(hide-only 有效,不是整條關掉)", () => {
  const use = capsCtx();
  const readonly = { id: "codex:x", provider: "codex",
                     capabilities: ["attachments", "replay", "follow"] };
  const can = use(readonly);
  assert.equal(can("input"), false, "宣告了 capabilities 卻沒有 input → 藏");
  assert.equal(can("approve"), false);
  assert.equal(can("replay"), true);
});

test("本機:不知道就不藏 —— 沒帶 capabilities / 清單還沒就緒 / 沒選 session", () => {
  const use = capsCtx();
  const bare = { id: "codex:y", provider: "codex" };            // 老 bridge:沒這個欄位
  for (const cap of ["input", "approve", "replay", "follow", "attachments"])
    assert.equal(use(bare)(cap), true, `沒宣告 → 放行:${cap}`);
  const declared = { id: "codex:z", provider: "codex", capabilities: ["replay"] };
  assert.equal(use(declared, "local", "failed")("input"), true, "清單抓失敗 → fail-open");
  assert.equal(use(null)("input"), true, "沒選 session → 不做判斷");
});

test("借用的電腦:維持白名單(授權面 fail-closed,一個位元不動)", () => {
  const use = capsCtx();
  const granted = { id: "codex:x", provider: "codex", capabilities: ["replay", "follow"] };
  assert.equal(use(granted, "remote")("replay"), true);
  assert.equal(use(granted, "remote")("input"), false, "沒授權就是不准");
  assert.equal(use(granted, "remote")("changes"), false, "changes 在借用機上是授權旗標");
  assert.equal(use(granted, "remote", "loading")("replay"), false, "清單沒就緒 → 一律不准");
  assert.equal(use({ id: "codex:q", provider: "codex" }, "remote")("replay"), false,
    "借用機沒宣告 = 不准(與本機的 fail-open 相反)");
});

// ═════════════ 2. CC 複選:完整集合 + submit,勾選不打網路 ═════════════
test("複選:勾選只動畫面,一個網路請求都不准發(舊版每點一下就送一個數字鍵)", () => {
  const h = harness();
  open(h, ccMultiRow());
  const first = buttons(h)[0];
  assert.match(first.textContent, /^☑ 1\. 甲/, "bridge 說 checked:true → 進場就是勾起來的");
  buttons(h)[2].click();                       // 勾「3. 丙」
  assert.equal(h.calls.length, 0, "勾選不得送任何請求");
  assert.match(buttons(h)[2].textContent, /^☑ 3\. 丙/);
  buttons(h)[0].click();                       // 取消「1. 甲」
  assert.equal(h.calls.length, 0);
  assert.match(buttons(h)[0].textContent, /^☐ 1\. 甲/);
});

test("複選:送出走 /ccsessions/{name}/answer,keys = 作答後應被勾選的完整集合", async () => {
  const h = harness();
  open(h, ccMultiRow());
  buttons(h)[2].click();                       // 現在勾的是 1 與 3
  const submit = buttons(h).at(-1);
  assert.match(submit.textContent, /^Submit（已選 2 項）$/, "送出鈕用 bridge 給的 submit_label");
  submit.click();
  await new Promise(r => setImmediate(r));
  const req = h.calls[0];
  assert.equal(req.path, "/ccsessions/Ops/answer");
  assert.equal(req.method, "POST");
  // 完整集合、照選項原順序(不是點擊順序)—— bridge 要算「現況 ⊕ 目標」的差集。
  assert.deepEqual(req.body, { keys: ["1", "3"], submit: true });
});

test("複選:取消掉 bridge 預勾的那項,送出的集合就不含它(盲送會把它 toggle 掉)", async () => {
  const h = harness();
  open(h, ccMultiRow());
  buttons(h)[0].click();                       // 取消 1
  buttons(h)[1].click();                       // 勾 2
  buttons(h).at(-1).click();
  await new Promise(r => setImmediate(r));
  assert.deepEqual(h.calls[0].body, { keys: ["2"], submit: true });
});

test("複選:沒勾任何一項 → 送出鈕停用(不准送空集合)", () => {
  const h = harness();
  open(h, ccMultiRow({ options: [
    { key: "1", label: "甲", checked: false }, { key: "2", label: "乙", checked: false }] }));
  const submit = buttons(h).at(-1);
  assert.equal(submit.disabled, true);
  submit.click();
  assert.equal(h.calls.length, 0);
});

test("複選:換題要清掉上一題的勾選(多題 ask 的鍵恆為 1|2|3,只看鍵偵測不到)", () => {
  const h = harness();
  open(h, ccMultiRow({ qIndex: 0 }));
  buttons(h)[1].click();
  assert.match(buttons(h)[1].textContent, /^☑/);
  const next = ccMultiRow({ qIndex: 1, options: [
    { key: "1", label: "甲", checked: false },
    { key: "2", label: "乙", checked: false },
    { key: "3", label: "丙", checked: false }] });
  next.meta.prompt.title = "第二題問別的";
  h.ctx.state.sessions = [next];
  h.ctx.renderApproval();                      // 不呼 msReset —— identity 自己要認得出換題
  assert.ok(buttons(h).filter((_, i) => i < 3).every(b => b.textContent.startsWith("☐")),
    "換題後不准把上一題的勾選帶著走");
});

test("複選:非核取方塊的動作列維持單鍵即送(走 /approve,不進勾選集合)", async () => {
  const h = harness();
  open(h, ccMultiRow());
  const action = buttons(h)[3];
  assert.equal(action.textContent, "4. Type something else");
  action.click();
  await new Promise(r => setImmediate(r));
  assert.equal(h.calls[0].path, "/app/v2/sessions/claude_code%3AOps/approve");
  assert.deepEqual(h.calls[0].body, { key: "4", approval_id: "cc-1" });
});

test("複選:借用的電腦畫得出題目但不給送出(那條路由不在遠端白名單裡)", () => {
  const h = harness();
  h.document.body.dataset.consoleAccess = "remote";
  const row = ccMultiRow();
  row.meta.console_options = row.meta.prompt.options;
  open(h, row);
  assert.equal(buttons(h).at(-1).disabled, true, "送出鈕停用,而不是畫一顆按了 403 的死鈕");
  assert.match(h.byId.get("ap-hint").textContent, /本機 console/);
});

test("複選:bridge 一列 checked 都沒送 → 退回「每列都可勾」,不是一張都不能勾的死畫面", () => {
  const h = harness();
  const row = ccMultiRow({ options: [{ key: "1", label: "甲" }, { key: "2", label: "乙" }] });
  row.meta.approval.options = [{ key: "1", label: "甲" }, { key: "2", label: "乙" }];
  open(h, row);
  assert.ok(buttons(h)[0].textContent.startsWith("☐ 1. 甲"));
  buttons(h)[0].click();
  assert.ok(buttons(h)[0].textContent.startsWith("☑ 1. 甲"));
});

// ═════════════ 3. style → class 映射 + 危險鍵 ═════════════
test("style 映射:primary/success → ap-yes,danger/deny → ap-no + ap-danger,其餘中性", () => {
  const h = harness();
  assert.equal(h.ctx.approvalStyleClass("primary"), "ap-yes");
  assert.equal(h.ctx.approvalStyleClass("success"), "ap-yes");
  assert.equal(h.ctx.approvalStyleClass("danger"), "ap-no ap-danger");
  assert.equal(h.ctx.approvalStyleClass("deny"), "ap-no ap-danger", "deny 是舊字彙,等同 danger");
  assert.equal(h.ctx.approvalStyleClass("secondary"), "ap-neutral");
  assert.equal(h.ctx.approvalStyleClass(""), "ap-neutral", "沒標語意 → 中性,不猜");
});

test("codex 三鍵:class 照 bridge 標的語意上,否決鍵拿到獨立的危險視覺", () => {
  const h = harness();
  open(h, codexRow([
    { key: "approve", label: "允許修改", style: "primary" },
    { key: "approve_for_session", label: "本次全允許", style: "secondary" },
    { key: "deny", label: "停止這一輪", style: "deny" },
  ]));
  const [a, b, c] = buttons(h);
  assert.ok(a._cls.has("ap-yes"));
  assert.ok(b._cls.has("ap-neutral") && !b._cls.has("ap-no"));
  assert.ok(c._cls.has("ap-no") && c._cls.has("ap-danger"));
});

test("CC 權限提示:style 從統一物件依 key 疊回 meta.prompt(prompt 自己沒有 style)", () => {
  const h = harness();
  const row = {
    id: "claude_code:Build", provider: "claude_code", capabilities: CC_CAPS,
    meta: {
      prompt: { kind: "permission", title: "rm -rf build/", options: [
        { key: "1", label: "Yes" }, { key: "2", label: "Yes, and don't ask again" },
        { key: "3", label: "No, and tell Claude" }] },
      approval: { id: "cc-2", status: "pending", kind: "permission", risk: "medium",
        expires_at: future, options: [
          { key: "1", label: "Yes", style: "primary" },
          { key: "2", label: "Yes, and don't ask again", style: "secondary" },
          { key: "3", label: "No, and tell Claude", style: "danger" }] },
    },
  };
  open(h, row);
  assert.ok(buttons(h)[0]._cls.has("ap-yes"));
  assert.ok(buttons(h)[2]._cls.has("ap-no"), "丟掉 style 就是鍵盤 d 按到最後一項的真兇");
});

// ═════════════ 4. firstApprovalButton:查不到就不動作 ═════════════
test("鍵盤 a/d:只認 bridge 標的語意,找得到就按對的那顆", () => {
  const h = harness();
  open(h, codexRow([
    { key: "approve", label: "允許", style: "primary" },
    { key: "approve_for_session", label: "本次全允許", style: "secondary" },
    { key: "deny", label: "拒絕", style: "danger" },
  ]));
  assert.equal(h.ctx.firstApprovalButton(true).textContent, "允許");
  assert.equal(h.ctx.firstApprovalButton(false).textContent, "拒絕");
});

test("鍵盤 d:沒有 danger 語意的選單 → **不動作**,絕不退回最後一顆", () => {
  const h = harness();
  // question 類選單(spec §2:無 danger 語意)。舊版會回 btns[last]。
  open(h, codexRow([
    { key: "opt0", label: "甲", style: "secondary" },
    { key: "opt1", label: "乙", style: "secondary" },
    { key: "opt2", label: "全部允許", style: "secondary" },
  ], { kind: "question" }));
  assert.equal(h.ctx.firstApprovalButton(false), null,
    "退回最後一顆會按到「全部允許」—— 想否決的人反而一次放行到底");
  assert.equal(h.ctx.firstApprovalButton(true), null, "沒有 primary 也一樣不猜第一顆");
  assert.equal(buttons(h).length, 3, "按鈕本身還是畫得出來,只是沒有快捷鍵");
});

test("鍵盤 a/d:停用中的按鈕不算數(送出中整排 disabled,不准再按一次)", () => {
  const h = harness();
  open(h, codexRow([
    { key: "approve", label: "允許", style: "primary" },
    { key: "deny", label: "拒絕", style: "danger" },
  ]));
  buttons(h).forEach(b => (b.disabled = true));
  assert.equal(h.ctx.firstApprovalButton(true), null);
  assert.equal(h.ctx.firstApprovalButton(false), null);
});

test("鍵盤 a/d:複選題沒有快捷鍵(送出鈕不掛 ap-yes,免得一個鍵就送出半套答案)", () => {
  const h = harness();
  open(h, ccMultiRow());
  assert.equal(h.ctx.firstApprovalButton(true), null);
  assert.equal(h.ctx.firstApprovalButton(false), null);
});

// ═════════════ 5. approval.resolved 立即收卡 ═════════════
function eventsCtx() {
  const h = harness();
  const log = [];
  Object.assign(h.ctx, {
    renderApproval: h.ctx.renderApproval,
    loadSessions: () => log.push("loadSessions"),
    setTimeout: (fn, ms) => { log.push("debounce:" + ms); return 1; },
    clearTimeout: () => {},
  });
  vm.runInContext(EVENTS_SRC + "\nglobalThis.handleListEvent=handleListEvent;", h.ctx);
  return { h, log };
}

test("approval.resolved:收到即收卡 + 立刻對帳,不排 800ms debounce", () => {
  const { h, log } = eventsCtx();
  const row = codexRow([{ key: "approve", label: "允許", style: "primary" }]);
  open(h, row);
  assert.equal(h.ctx.actionable(row), true);
  assert.equal(h.byId.get("approval")._cls.has("hidden"), false);
  log.length = 0;      // 開卡時排的「到期重繪」不是本題要看的東西

  h.ctx.handleListEvent({ seq: 9, type: "approval.resolved",
    data: { approval_id: "codex-1", status: "approved", session_id: "codex:01JD" } });

  assert.equal(h.ctx.actionable(row), false, "別的端答掉了,這裡不准還掛著可按的卡");
  assert.equal(h.byId.get("approval")._cls.has("hidden"), true, "卡片當場收掉");
  assert.deepEqual(log, ["loadSessions"], "立即重抓,不等 debounce");
});

test("其他事件型別維持原樣(800ms debounce 重抓)—— 刻意不擴大戰線", () => {
  const { h, log } = eventsCtx();
  h.ctx.handleListEvent({ seq: 10, type: "message.created", data: {} });
  assert.deepEqual(log, ["debounce:800"]);
});

test("approval.resolved 少了 approval_id 也不准炸,退回一般刷新語意", () => {
  const { h, log } = eventsCtx();
  h.ctx.handleListEvent({ seq: 11, type: "approval.resolved", data: {} });
  assert.deepEqual(log, ["loadSessions"]);
});

// ═════════════ 6. 順手修:key 前綴 / 不靜默截斷 / detail+risk ═════════════
test("key 前綴只給 CC 的 TUI 數字鍵,內部識別字不准漏到畫面上", () => {
  const h = harness();
  assert.equal(h.ctx.approvalOptionLabel({ key: "3", label: "丙" }), "3. 丙");
  assert.equal(h.ctx.approvalOptionLabel({ key: "ack", label: "知道了" }), "知道了",
    "舊版長成「ack. 知道了」");
  assert.equal(h.ctx.approvalOptionLabel({ key: "approve_for_session", label: "本次全允許" }),
    "本次全允許");
  assert.equal(h.ctx.approvalOptionLabel({ key: "allow-always", label: "一直允許" }), "一直允許");
  // 空 label 在 approvalView 就補成 key —— 空白按鈕是 gemini 踩過的洞。
  open(h, codexRow([{ key: "opt7", label: "", style: "secondary" }], { kind: "question" }));
  assert.equal(buttons(h)[0].textContent, "opt7", "label 是空的也不准畫出一顆空白按鈕");
});

test("第 9 個以後的選項不准靜默消失(舊版畫到第 8 個就停,而且不說)", () => {
  const h = harness();
  const many = Array.from({ length: 12 }, (_, i) => ({ key: "opt" + i, label: "選項" + i, style: "secondary" }));
  open(h, codexRow(many, { kind: "question" }));
  assert.equal(buttons(h).length, 12);
  assert.equal(h.byId.get("ap-more")._cls.has("hidden"), true, "沒有被截就不該有那行字");
  assert.ok(labels(h).includes("選項11"));
});

test("真的多到要收(> 24)才收,而且明說還有幾個", () => {
  const h = harness();
  const many = Array.from({ length: 28 }, (_, i) => ({ key: "opt" + i, label: "s" + i, style: "secondary" }));
  open(h, codexRow(many, { kind: "question" }));
  assert.equal(buttons(h).length, 24);
  assert.equal(h.byId.get("ap-more")._cls.has("hidden"), false);
  assert.match(h.byId.get("ap-more").textContent, /還有 4 個/);
});

test("detail / risk 真的進得了畫面(規範欄位,少一個就是盲簽)", () => {
  const h = harness();
  open(h, codexRow([{ key: "approve", label: "允許", style: "primary" }]));
  assert.match(h.byId.get("ap-detail").textContent, /applyPatch/);
  assert.equal(h.byId.get("ap-detail")._cls.has("hidden"), false);
  assert.equal(h.byId.get("ap-risk").textContent, "高風險");
  assert.ok(h.byId.get("ap-risk")._cls.has("ap-risk-high"));
});

test("沒有 detail / risk 的審批不准留下空容器", () => {
  const h = harness();
  open(h, codexRow([{ key: "approve", label: "允許", style: "primary" }],
    { detail: "", risk: "", title: "沒細節" }));
  assert.equal(h.byId.get("ap-detail")._cls.has("hidden"), true);
  assert.equal(h.byId.get("ap-risk")._cls.has("hidden"), true);
});

test("多題進度與 kind 圖示:使用者唯一知道「還有幾題」的地方", () => {
  const h = harness();
  open(h, ccMultiRow({ qIndex: 1 }));
  assert.equal(h.byId.get("ap-progress").textContent, "第 2/3 題");
  assert.equal(h.byId.get("ap-icon").textContent, "❓");
  open(h, codexRow([{ key: "ack", label: "知道了", style: "primary" }], { kind: "notice" }));
  assert.equal(h.byId.get("ap-icon").textContent, "🔔");
  assert.equal(h.byId.get("ap-progress")._cls.has("hidden"), true);
});

test("顯示用的 q_index 不准流到線上 —— 借用機的授權守門是比對 body 欄位集合", async () => {
  const h = harness();
  // CC 的 q_index 住在 meta.prompt / approval.meta;統一物件頂層那顆是 codex 專屬。
  const row = ccMultiRow();
  row.meta.prompt.multiselect = false;
  row.meta.approval.meta.multiselect = false;
  open(h, row);
  buttons(h)[0].click();
  await new Promise(r => setImmediate(r));
  assert.deepEqual(Object.keys(h.calls[0].body).sort(), ["approval_id", "key"],
    "對 CC 多送一個 expected_q_index 會讓借用機直接 403");

  const h2 = harness();
  open(h2, codexRow([{ key: "opt0", label: "甲", style: "primary" }], { kind: "question", q_index: 2 }));
  buttons(h2)[0].click();
  await new Promise(r => setImmediate(r));
  assert.equal(h2.calls[0].body.expected_q_index, 2, "codex question 的頂層 q_index 照舊要帶");
});
