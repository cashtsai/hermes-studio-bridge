// 刀④:卡片流視窗化的契約(載入 static/console/app.js 出貨真函式驅動):
//  1. 長對話只渲染尾端一個視窗(CARD_RENDER_MAX),頂端哨兵明示折疊數。
//  2. 哨兵展開一段(CARD_RENDER_CHUNK),走 prepend 錨定路徑。
//  3. 貼底跟讀時視窗收斂回上限 —— 深翻頁離開後 DOM 不永久變大。
//  4. 往上閱讀中(非 follow)尾端來新卡,視窗起點**不前移**(不抽讀者腳下的列)。
//  5. 資料層永遠全量:state.cards 不因視窗化被裁剪。
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

const ROW_H = 10, VIEW_H = 100;
function makeNode(tag) {
  return {
    tagName: tag, children: [], parentNode: null, className: '', _text: '', _html: '',
    dataset: {}, _handlers: {}, scrollTop: 0, type: '',
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
    click() { for (const fn of this._handlers.click || []) fn({ target: this }); },
    get scrollHeight() { return this.children.length * ROW_H; },
    get clientHeight() { return this.children.length ? VIEW_H : VIEW_H; },
    get firstChild() { return this.children[0] || null; },
    get firstElementChild() { return this.children[0] || null; },
    get nextElementSibling() {
      const p = this.parentNode; if (!p) return null;
      return p.children[p.children.indexOf(this) + 1] || null;
    },
    get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return !!n._root; },
    insertBefore(c, ref) {
      c.remove(); c.parentNode = this;
      if (ref == null) this.children.push(c);
      else { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); }
      return c;
    },
    remove() { if (this.parentNode) { const i = this.parentNode.children.indexOf(this); if (i >= 0) this.parentNode.children.splice(i, 1); this.parentNode = null; } },
    getBoundingClientRect() {
      const p = this.parentNode;
      if (!p) return { top: 0, bottom: VIEW_H };
      const i = p.children.indexOf(this);
      return { top: i * ROW_H - p.scrollTop, bottom: i * ROW_H + ROW_H - p.scrollTop };
    },
  };
}

function harness() {
  const cards = makeNode('div'); cards._root = true;
  const newReply = { hidden: true };
  const state = { cards: [], view: 1 };
  const exposed = {};
  const c = vm.createContext({
    document: { createElement: makeNode },
    window: {},
    $: id => (id === 'cards' ? cards : newReply),
    state,
    patchStreamingTail: () => false,
    renderTurnContent: (turn, node) => { node._rendered = turn.cards.map(x => x.id).join(','); },
    hydrateCardMedia: () => {},
    __expose: o => Object.assign(exposed, o),
  });
  // const/let 不會掛上 vm 的 context global —— cardScroll 要顯式帶出來。
  vm.runInContext(section('function groupTurns(', 'function setStreamHealth(') +
    '\n__expose({ cardScroll });', c);
  return {
    c, state, cards, exposed,
    turnKeys() { return cards.children.filter(n => n.dataset.key !== '__older__').map(n => n.dataset.key); },
    sentinel() { return cards.children.find(n => n.dataset.key === '__older__') || null; },
  };
}

// 交錯 role → 一卡一回合,回合數 = 卡片數,邊界算術好驗。
const mkCards = (n, from = 0) => Array.from({ length: n }, (_, i) =>
  ({ id: 'c' + (from + i), ts: from + i + 1, role: (from + i) % 2 ? 'agent' : 'user', kind: 'text', final: true, body: { text: 'm' + (from + i) } }));

test('long history renders only the tail window with an explicit fold count', () => {
  const h = harness();
  h.state.cards = mkCards(500);
  h.c.renderCards();
  assert.equal(h.turnKeys().length, 200);           // CARD_RENDER_MAX
  assert.equal(h.turnKeys()[0], 'c300');
  const s = h.sentinel();
  assert.ok(s, 'sentinel present');
  assert.equal(h.cards.children[0], s, 'sentinel is the first child');
  assert.match(s.textContent, /顯示更早的 300 則訊息/);
  assert.equal(h.state.cards.length, 500, 'data layer stays complete');
});

test('sentinel click reveals one chunk via the prepend-anchored path', () => {
  const h = harness();
  h.state.cards = mkCards(500);
  h.c.renderCards();
  h.sentinel().click();                              // revealOlderCards → renderCards(true)
  assert.equal(h.turnKeys().length, 300);
  assert.equal(h.turnKeys()[0], 'c200');
  assert.match(h.sentinel().textContent, /顯示更早的 200 則訊息/);
});

test('following at the bottom re-trims the window after a deep reveal', () => {
  const h = harness();
  h.state.cards = mkCards(500);
  h.c.renderCards();
  h.sentinel().click();                              // 窗開到 300 回合
  h.exposed.cardScroll.follow = true;
  h.exposed.cardScroll.top = h.cards.scrollTop;            // trackCardScroll 不改判
  h.state.cards = h.state.cards.concat(mkCards(1, 500));
  h.c.renderCards();                                 // follow → 收斂回上限
  assert.equal(h.turnKeys().length, 200);
  assert.equal(h.turnKeys().at(-1), 'c500');
  assert.match(h.sentinel().textContent, /顯示更早的 301 則訊息/);
});

test('reading upward: new tail cards never advance the window start', () => {
  const h = harness();
  h.state.cards = mkCards(500);
  h.c.renderCards();
  h.exposed.cardScroll.follow = false;                     // 讀者在上面
  h.cards.scrollTop = 0; h.exposed.cardScroll.top = 0;
  h.state.cards = h.state.cards.concat(mkCards(3, 500));
  h.c.renderCards();
  assert.equal(h.turnKeys()[0], 'c300', 'first rendered turn unchanged');
  assert.equal(h.turnKeys().at(-1), 'c502', 'new tail still appended');
  assert.equal(h.turnKeys().length, 203);
});

test('short history renders fully with no sentinel', () => {
  const h = harness();
  h.state.cards = mkCards(50);
  h.c.renderCards();
  assert.equal(h.turnKeys().length, 50);
  assert.equal(h.sentinel(), null);
});
