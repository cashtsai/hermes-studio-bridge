// 刀④:側欄清單增量渲染的契約(載入 static/console/app.js 出貨真函式驅動):
//  1. 大群組只畫一個視窗(LIST_RENDER_CHUNK),其餘摺進「顯示更多」鈕,
//     鈕上明示剩餘數 —— 不靜默吃掉。
//  2. 點「顯示更多」逐段展開;差距小於 SLACK 時整組畫完、鈕消失。
//  3. j/k 的完整順序(_orderedIds)含視窗外的列 —— 導航不受折疊影響。
//  4. 展開中的那一列(state.current)永遠在視窗內。
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
    tagName: tag, children: [], parentNode: null, className: '', _text: '', _html: '',
    dataset: {}, _handlers: {},
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    setAttribute(k, v) { this[k] = String(v); },
    addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); },
    click() { for (const fn of this._handlers.click || []) fn({ target: this }); },
    appendChild(c) { c.remove(); c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c.remove();
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
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, ''), out = [];
      (function walk(n) { for (const c of n.children) { if (classTokens(c).includes(cls)) out.push(c); walk(c); } })(this);
      return out;
    },
  };
}

function harness() {
  const listEl = makeNode('div');
  const state = { current: null, sessions: [], degraded: [], listHealth: 'ready', listFilter: 'all', listQuery: '' };
  const c = vm.createContext({
    document: { createElement: makeNode, activeElement: null },
    window: { matchMedia: () => ({ matches: true }), PocketSessionOps: undefined },
    Date: { now: () => 1000000 },
    $: () => listEl,
    esc: s => String(s),
    PROVIDER_LABEL: { codex: 'CX' },
    actionable: () => false,
    workPhase: s => s.status || 'idle',
    phaseGlyph: () => '',
    sessionPreview: () => '',
    unreadMark: () => '',
    sessionUnread: () => false,
    workMatches: (s, q) => String(s.title || s.id).includes(q),
    listActivity: new Map(),
    localStorage: (() => { const m = new Map();
      return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; })(),
    state,
  });
  vm.runInContext(section('function sessionRank(', 'function activityTime('), c);
  return {
    c, state, listEl,
    rows() { return listEl.querySelectorAll('.session-row'); },
    more() { return listEl.querySelectorAll('.sess-more-rows'); },
    render() { c.renderSessions(); },
  };
}

const jobs = n => Array.from({ length: n }, (_, i) =>
  ({ id: 'codex:s' + String(i).padStart(3, '0'), title: 'work ' + i, provider: 'codex', status: 'idle' }));

test('big group renders a window and folds the rest behind an explicit count', () => {
  const h = harness();
  h.state.sessions = jobs(100);
  h.render();
  assert.equal(h.rows().length, 40);
  assert.equal(h.more().length, 1);
  assert.match(h.more()[0].textContent, /還有 60 條/);
  // 完整順序照常可導航
  assert.equal(h.listEl._orderedIds.length, 100);
});

test('clicking the sentinel expands chunk by chunk until everything is shown', () => {
  const h = harness();
  h.state.sessions = jobs(100);
  h.render();
  h.more()[0].click();                       // handler 內部自己 renderSessions
  assert.equal(h.rows().length, 80);
  assert.match(h.more()[0].textContent, /還有 20 條/);
  h.more()[0].click();                       // 100 ≤ 120+SLACK → 全畫,鈕消失
  assert.equal(h.rows().length, 100);
  assert.equal(h.more().length, 0);
});

test('small overflow under the slack renders fully with no sentinel', () => {
  const h = harness();
  h.state.sessions = jobs(45);               // 40+8 以內 → 不值得擺一顆鈕
  h.render();
  assert.equal(h.rows().length, 45);
  assert.equal(h.more().length, 0);
});

test('the open session is always inside the window', () => {
  const h = harness();
  h.state.sessions = jobs(100);
  h.state.current = 'codex:s070';
  h.render();
  const ids = h.rows().map(n => n.dataset.id);
  assert.ok(ids.includes('codex:s070'), 'current row must be rendered');
  assert.ok(h.rows().length < 100, 'window still bounded');
  assert.match(h.more()[0].textContent, /還有 \d+ 條/);
});

test('keyed diff still reuses nodes inside the window across renders', () => {
  const h = harness();
  h.state.sessions = jobs(100);
  h.render();
  const before = h.rows()[5];
  h.render();
  assert.equal(h.rows()[5], before, 'same DOM node reused');
});
