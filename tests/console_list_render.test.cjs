// Console 左側 session 清單的「穩定渲染」契約(2026-11-21 治頭暈):
//  1. 同 rank 的列,活動更新時位置不變(不再「跑上跑下」)。
//  2. keyed diff:內容沒變的列,跨次重畫是**同一個 DOM 節點**(不整串重建)。
//  3. 只有真的變了內容的列才就地更新 innerHTML;只有 rank 變了才移動位置。
// 載入 static/console/app.js 裡出貨的真函式(非複製),配最小 DOM shim 驅動。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/console/app.js'), 'utf8');
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `source section: ${start}`);
  return source.slice(a, b);
}

function classTokens(n) { return (n.className || '').split(/\s+/).filter(Boolean); }
function makeNode(tag) {
  return {
    tagName: tag, children: [], parentNode: null, className: '', _text: '', _html: '', dataset: {},
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); this.children = []; },  // 設 innerHTML(空或非空)都換掉子節點(同瀏覽器)
    get innerHTML() { return this._html; },
    setAttribute(k, v) { this[k] = String(v); },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      if (c.parentNode) { const i = c.parentNode.children.indexOf(c); if (i >= 0) c.parentNode.children.splice(i, 1); }
      c.parentNode = this;
      if (ref == null) this.children.push(c);
      else { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); }
      return c;
    },
    remove() { if (this.parentNode) { const i = this.parentNode.children.indexOf(this); if (i >= 0) this.parentNode.children.splice(i, 1); this.parentNode = null; } },
    get firstChild() { return this.children[0] || null; },
    get nextSibling() { const p = this.parentNode; if (!p) return null; return p.children[p.children.indexOf(this) + 1] || null; },
    contains(x) { let c = x; while (c) { if (c === this) return true; c = c.parentNode; } return false; },
    getBoundingClientRect() { return { top: 0 }; },
    animate() { return {}; },
    addEventListener() {},
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, ''), out = [];
      (function walk(n) { for (const c of n.children) { if (classTokens(c).includes(cls)) out.push(c); walk(c); } })(this);
      return out;
    },
  };
}

function harness() {
  let NOW = 1000000;
  const listEl = makeNode('div');
  const state = { current: null, sessions: [], degraded: [], listHealth: 'ready' };
  const c = vm.createContext({
    document: { createElement: makeNode, activeElement: null },
    window: { matchMedia: () => ({ matches: true }), PocketSessionOps: undefined }, // reduce-motion → 跳過 FLIP
    Date: { now: () => NOW },
    $: () => listEl,
    esc: s => String(s),
    PROVIDER_LABEL: { claude_code: 'CC', codex: 'CX', hermes: '人格', gemini: 'Gemini' },
    actionable: s => s.status === 'awaiting',
    workPhase: s => s.status || 'idle',
    phaseGlyph: s => (s && s.status === 'needs_input' ? '✋' : ''),
    sessionPreview: s => s._preview || '',
    unreadMark: s => (s._unread ? '<u>未讀</u>' : ''),
    sessionUnread: s => !!s._unread,
    workMatches: (s, q) => String(s.title || s.id).toLowerCase().includes(String(q).toLowerCase()),
    listActivity: new Map(),
    activityTime: value => typeof value === 'number' ? value * 1000 : (Date.parse(value) || 0),
    localStorage: (() => { const m = new Map();
      return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; })(),
    state,
  });
  vm.runInContext(section('function sessionRank(', 'function activityTime('), c);
  return {
    c, state, listEl,
    advance(ms) { NOW += ms; },
    rows() { return listEl.querySelectorAll('.session-row'); },
    order() { return listEl.querySelectorAll('.session-row').map(n => n.dataset.id); },
    render() { c.renderSessions(); },
  };
}
const S = (id, status, extra = {}) => ({ id, provider: 'claude_code', title: id, status, meta: {}, last_event_at: 0, ...extra });

test('initial same-rank order uses recency, with deterministic ID ties', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle', {last_event_at:100}),
    S('claude_code:c', 'idle', {last_event_at:200}), S('claude_code:b', 'idle', {last_event_at:200})];
  h.render();
  assert.deepEqual(h.order(), ['claude_code:b','claude_code:c','claude_code:a']);
  h.state.sessions[0].last_event_at = 300;
  h.render();
  assert.equal(h.order()[2], 'claude_code:a');
  vm.runInContext('sessionOrder.clear()', h.c);
  h.render();
  assert.equal(h.order()[0], 'claude_code:a');
});

test('activity update does NOT reorder same-rank rows (the anti-dizzy invariant)', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:b', 'idle'), S('claude_code:c', 'idle')];
  h.render();
  const before = h.order();
  assert.equal(before.length, 3);
  h.advance(5000);
  h.state.sessions = h.state.sessions.map(s => s.id === 'claude_code:b' ? { ...s, last_event_at: 999999, _preview: '新訊息' } : s);
  h.render();
  assert.deepEqual(h.order(), before, 'order must be identical after b gets activity');
});

test('keyed diff: an unchanged row keeps the SAME DOM node across re-renders (no teardown)', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:b', 'idle')];
  h.render();
  const a1 = h.rows().find(n => n.dataset.id === 'claude_code:a');
  h.advance(3000);
  h.state.sessions = h.state.sessions.map(s => s.id === 'claude_code:b' ? { ...s, _preview: 'changed' } : s);
  h.render();
  const a2 = h.rows().find(n => n.dataset.id === 'claude_code:a');
  assert.equal(a2, a1, 'unchanged row a must be the very same node object');
});

test('changed content updates in place; the node identity is preserved', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:b', 'idle')];
  h.render();
  const b1 = h.rows().find(n => n.dataset.id === 'claude_code:b');
  const sig1 = b1._sig;
  h.state.sessions = [{ ...h.state.sessions[0], _preview: 'brand new preview' }];
  h.render();
  const b2 = h.rows().find(n => n.dataset.id === 'claude_code:b');
  assert.equal(b2, b1, 'same node (updated in place, not replaced)');
  assert.notEqual(b2._sig, sig1, 'signature changed');
  assert.ok(b2.innerHTML.includes('brand new preview'), 'preview text updated in place');
});

test('a rank change (idle→running) moves the row up but reuses its node', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:b', 'idle')];
  h.render();
  const b1 = h.rows().find(n => n.dataset.id === 'claude_code:b');
  h.advance(1000);
  h.state.sessions = h.state.sessions.map(s => s.id === 'claude_code:b' ? { ...s, status: 'running' } : s);
  h.render();
  assert.equal(h.order()[0], 'claude_code:b', 'running b floats to top of its rank');
  assert.equal(h.rows().find(n => n.dataset.id === 'claude_code:b'), b1, 'moved, not rebuilt');
});

test('removing a session detaches its node and drops it from the list', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:c', 'idle')];
  h.render();
  const c1 = h.rows().find(n => n.dataset.id === 'claude_code:c');
  h.state.sessions = h.state.sessions.filter(s => s.id !== 'claude_code:c');
  h.render();
  assert.ok(!h.order().includes('claude_code:c'), 'c removed from list');
  assert.equal(c1.parentNode, null, 'c node detached from DOM');
});

// ── W1-④ 篩選 / 搜尋 / 釘選 ───────────────────────────────
test('search query filters the list to matching sessions only', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:alpha', 'idle'), S('claude_code:beta', 'idle'), S('claude_code:gamma', 'idle')];
  h.render();
  assert.equal(h.rows().length, 3);
  h.state.listQuery = 'bet';
  h.render();
  assert.deepEqual(h.order(), ['claude_code:beta'], 'only beta matches "bet"');
  h.state.listQuery = 'zzz';
  h.render();
  assert.equal(h.rows().length, 0, 'no matches → zero rows (shows 找不到 message)');
  assert.ok(h.listEl.innerHTML.includes('找不到'), 'zero-match guided empty state shown');
});

test('filter=unread shows only unread sessions', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:b', 'idle', { _unread: true })];
  h.render();
  assert.equal(h.rows().length, 2);
  h.state.listFilter = 'unread';
  h.render();
  assert.deepEqual(h.order(), ['claude_code:b'], 'only the unread one remains');
});

test('filter=attn shows only rank-0 (actionable / needs_input) sessions', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:b', 'needs_input'), S('claude_code:c', 'awaiting')];
  h.render();
  h.state.listFilter = 'attn';
  h.render();
  assert.deepEqual(h.order().sort(), ['claude_code:b', 'claude_code:c'], 'idle a hidden; needs_input + awaiting shown');
});

test('pinned session floats to the top of its provider group and persists', () => {
  const h = harness();
  h.state.sessions = [S('claude_code:a', 'idle'), S('claude_code:b', 'idle'), S('claude_code:c', 'idle')];
  h.render();
  assert.deepEqual(h.order(), ['claude_code:a', 'claude_code:b', 'claude_code:c'], 'default id order');
  h.c.togglePin('claude_code:c');   // togglePin re-renders internally
  assert.equal(h.order()[0], 'claude_code:c', 'pinned c jumps to top');
  // the pinned row carries the on-state toggle
  const cRow = h.rows().find(n => n.dataset.id === 'claude_code:c');
  assert.ok(cRow.innerHTML.includes('sess-pin on'), 'pin button marked on');
  assert.ok(cRow.innerHTML.includes('sess pinned') || cRow.innerHTML.includes('pinned'), 'row marked pinned');
  h.c.togglePin('claude_code:c');   // unpin
  assert.deepEqual(h.order(), ['claude_code:a', 'claude_code:b', 'claude_code:c'], 'back to id order after unpin');
});

test('provider headings carry full names, visible counts and row identities, including cc2', () => {
  const h = harness();
  h.state.sessions = [S('cc2:a', 'idle', {provider:'cc2',title:'Same title'}),
    S('claude_code:b', 'idle', {title:'Same title'}),
    S('codex:a', 'idle', {provider:'codex',title:'Same title'})];
  h.render();
  const cc = h.listEl._provGroups.get('claude_code');
  const cx = h.listEl._provGroups.get('codex');
  assert.equal(cc.dataset.provider, 'claude_code');
  assert.equal(cc['aria-label'], 'Claude Code');
  assert.match(cc._head.innerHTML, /prov-name">Claude Code/);
  assert.match(cc._head.innerHTML, /aria-label="2 個對話">2/);
  assert.match(cx._head.innerHTML, /prov-name">Codex/);
  assert.match(h.rows()[0].innerHTML, /class="sess-provider" title="Claude Code" aria-label="Claude Code">CC/);
  assert.match(h.rows()[2].innerHTML, /class="sess-provider" title="Codex" aria-label="Codex">CX/);
  const header = cc._head, sig = header._sig;
  h.render();
  assert.equal(cc._head, header);
  assert.equal(header._sig, sig);
  h.state.listQuery = 'Same'; h.state.listFilter = 'unread';
  h.state.sessions[0]._unread = true;
  h.render();
  assert.equal(h.listEl._provGroups.size, 1);
  assert.match(cc._head.innerHTML, /aria-label="1 個對話">1/);
});

test('degraded provider retains its identity and an explicit warning; unknown providers stay readable', () => {
  const h = harness();
  h.state.sessions = [S('cc2:a','idle',{provider:'cc2'}), S('future:a','idle',{provider:'Future Provider'})];
  h.state.degraded = ['cc2'];
  h.render();
  const cc = h.listEl._provGroups.get('claude_code');
  assert.match(cc._head.className, /degraded/);
  assert.match(cc._head.innerHTML, /連線異常，清單可能不完整/);
  assert.equal(h.listEl._provGroups.get('Future Provider')['aria-label'], 'Future Provider');
  assert.match(h.c.sessionRowHTML(h.state.sessions[1]), /title="Future Provider"/);
  h.state.degraded = []; h.render();
  assert.doesNotMatch(cc._head.innerHTML, /prov-warning/);
});
