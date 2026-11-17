// Console 的中文輸入/搜尋守衛 —— 跑的是**出貨的那份 app.js**(用 vm 抽段
// 執行,不重寫一份),所以測試不會跟實作漂開。
//
// 兩組題目:
//   1. IME 三重判準:中文/日文選字按 Enter 不准被當成「送出」。Safari 的
//      組字確認 keydown 比 compositionend **晚**到 —— 這條是真實回報的誤送
//      病根,所以時間窗用假時鐘逐毫秒釘。
//   2. 搜尋摺疊:全形/相容字形/組合記號三類寫法互搜得到。
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

// ── IME 守衛:假時鐘讓 10ms 視窗可以逐毫秒斷言 ──────────────────────────
function imeContext() {
  const clock = { t: 1000 };
  const c = vm.createContext({ Date: { now: () => clock.t } });
  // `const`/箭頭函式是語彙宣告,不會掛上 context 物件 —— 明確導出。
  vm.runInContext(section('const IME_TAIL_MS = 10;', 'function wireKeys(')
    + '\nglobalThis.imeKey = imeKey;', c);
  return { c, clock };
}
const key = (over = {}) => Object.assign({ key: 'Enter', isComposing: false, keyCode: 13 }, over);

test('判準一:isComposing —— 組字中的 Enter 不送出', () => {
  const { c } = imeContext();
  assert.equal(c.imeKey(key({ isComposing: true })), true);
  assert.equal(c.imeKey(key()), false, '乾淨的 Enter 必須放行,否則中文使用者永遠送不出訊息');
});

test('判準二:keyCode 229 —— 不給 isComposing 的舊引擎', () => {
  const { c } = imeContext();
  assert.equal(c.imeKey(key({ keyCode: 229 })), true);
});

test('判準三(重現 Safari 誤送):compositionend 之後才到的 Enter', () => {
  const { c, clock } = imeContext();
  // 使用者打「ㄓㄨㄥ」→ 候選窗開著 → 按 Enter 選「中」。
  c.imeCompositionStart();
  assert.equal(c.imeKey(key({ isComposing: true })), true);
  // Safari:compositionend 先到,**然後**才送那顆確認用的 keydown,
  // 而且那顆 keydown 的 isComposing 已經是 false、keyCode 是 13。
  c.imeCompositionEnd();
  const confirming = key();                       // 乾淨得像一顆真正的送出鍵
  assert.equal(c.imeKey(confirming), true,
    '舊版(只看 isComposing + 229)在這裡回 false → 把注音半成品送出去');
  clock.t += 9;
  assert.equal(c.imeKey(key()), true, '9ms 內仍在守衛窗內');
  clock.t += 1;                                   // 第 10ms:窗關
  assert.equal(c.imeKey(key()), false, '10ms 之後必須放行,不能永久吃掉 Enter');
});

test('組字中直接按 Enter(引擎不發 compositionend)照樣被擋', () => {
  const { c, clock } = imeContext();
  c.imeCompositionStart();
  clock.t += 5000;                                // 組字可以很久(長句注音)
  assert.equal(c.imeKey(key()), true, 'composing 旗標在 compositionend 之前不准過期');
});

test('失焦會把守衛歸零 —— 不會把下一個輸入框的 Enter 一起吃掉', () => {
  const { c } = imeContext();
  c.imeCompositionStart();
  c.imeReset();
  assert.equal(c.imeKey(key()), false);
});

test('compositionstart 會清掉上一輪的尾巴視窗', () => {
  const { c, clock } = imeContext();
  c.imeCompositionEnd();
  c.imeCompositionStart();
  clock.t += 100;
  assert.equal(c.imeKey(key()), true, '仍在組字 → 擋');
  c.imeCompositionEnd();
  clock.t += 100;
  assert.equal(c.imeKey(key()), false);
});

// ── 搜尋摺疊 ─────────────────────────────────────────────────────────────
function searchContext() {
  const c = vm.createContext({ PROVIDER_LABEL: { claude_code: 'Claude Code', codex: 'Codex' } });
  vm.runInContext(section('const COMBINING_MARKS =', '\nconst PHASE_LABEL'), c);
  return c;
}
const work = (over = {}) => Object.assign({ id: 'w1', provider: 'codex', title: '' }, over);

test('中文子字串命中:漢字之間沒有空白,每個字自己就是詞邊界', () => {
  const c = searchContext();
  assert.equal(c.workMatches(work({ title: '重連速度調查' }), '重連'), true);
  assert.equal(c.workMatches(work({ title: '重連速度調查' }), '速度'), true);
  assert.equal(c.workMatches(work({ title: '重連速度調查' }), '調查 重連'), true);
  assert.equal(c.workMatches(work({ title: '重連速度調查' }), '重連 不存在'), false);
});

test('全形 → 半形摺疊(輸入法沒切回英數時打出來的那串)', () => {
  const c = searchContext();
  assert.equal(c.workMatches(work({ title: 'Pocket Console' }), 'ｐｏｃｋｅｔ'), true);
  assert.equal(c.workMatches(work({ title: 'CC 主線' }), 'ＣＣ'), true);
  assert.equal(c.workMatches(work({ title: '埠 8081' }), '８０８１'), true);
});

test('相容字形摺疊:㈱ / ㎝ / ① 與展開形互搜', () => {
  const c = searchContext();
  assert.equal(c.workMatches(work({ title: '(株)日本工程' }), '㈱'), true);
  assert.equal(c.workMatches(work({ title: '寬度 10cm' }), '10㎝'), true);
});

test('組合記號摺疊:片假名濁音與拉丁變音的兩種寫法互搜', () => {
  const c = searchContext();
  // パソコン(預組合)vs ハ+゚ソコン 一類的分解寫法
  assert.equal(c.workMatches(work({ title: 'パソコン設定' }), 'パソコン'), true);
  assert.equal(c.workMatches(work({ title: 'café 筆記' }), 'cafe'), true);
  assert.equal(c.workMatches(work({ title: 'cafe 筆記' }), 'café'), true);
});

test('全形空白當作詞間分隔(中文輸入法常打出 U+3000)', () => {
  const c = searchContext();
  assert.equal(c.workMatches(work({ title: '重連速度調查' }), '重連　調查'), true);
});

test('空查詢等於不過濾(行為與舊版一致)', () => {
  const c = searchContext();
  assert.equal(c.workMatches(work({ title: '任何' }), ''), true);
  assert.equal(c.workMatches(work({ title: '任何' }), '   '), true);
});
