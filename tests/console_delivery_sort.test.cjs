// Run the shipped functions with fake transport/time; no browser or live sessions.
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
function ranking() {
  const c = vm.createContext({
    actionable: s => s.status === 'awaiting',
    activityTime: n => Number(n) || 0,
    listActivity: new Map(),
  });
  vm.runInContext(section('function sessionRank(', 'function renderSessions('), c);
  return c;
}
const row = (id, status, last_event_at = 0) => ({id, status, last_event_at});
test('downgrade waits 15 seconds from first lower observation, even after a long poll gap', () => {
  const c = ranking();
  assert.equal(c.stickyRank(row('a', 'running'), 0), 1);
  assert.equal(c.stickyRank(row('a', 'idle'), 60000), 1);
  assert.equal(c.stickyRank(row('a', 'idle'), 74999), 1);
  assert.equal(c.stickyRank(row('a', 'idle'), 75000), 2);
});
test('returning to active resets downgrade; an urgent state takes effect immediately', () => {
  const c = ranking();
  c.stickyRank(row('a', 'running'), 0);
  c.stickyRank(row('a', 'idle'), 1000);
  c.stickyRank(row('a', 'running'), 10000);
  assert.equal(c.stickyRank(row('a', 'idle'), 16000), 1);
  assert.equal(c.stickyRank(row('a', 'idle'), 30999), 1);
  assert.equal(c.stickyRank(row('a', 'awaiting'), 31000), 0);
});
// 2026-11-21 契約變更(機主拍板):同 rank 之內**不再按活動時間重排**,改穩定 id
// 序。原本「活動最近的浮上去」就是機主回報的「session 跑上跑下看到頭暈」的真因
// ——rank(需要你 > 執行中 > 一般,含 15s 降級遲滯)仍決定分層,但同層位置鎖定、
// 不隨每則訊息漂移。活動/未讀改由列上的狀態晶片/未讀標表達(位置不動)。
test('single-row groups retain rank history; within a rank, order is stable by id (not activity)', () => {
  const c = ranking();
  c.sortSessions([row('single', 'running')], 0);
  assert.equal(c.stickyRank(row('single', 'idle'), 20000), 1);
  const items = [row('b', 'idle', 5), row('a', 'idle', 5), row('recent', 'idle', 6),
    row('running', 'running'), row('urgent', 'awaiting')];
  // rank:urgent(0) > running(1) > 其餘(2);rank 2 之內純按 id → a,b,recent
  // (recent 的較新活動不再把它拉上去 —— 這正是治頭暈的點)。
  assert.deepEqual(Array.from(c.sortSessions(items, 0), s => s.id),
    ['urgent', 'running', 'a', 'b', 'recent']);
});
test('needs_input and dispatching have rank priority; within a rank, stable id order', () => {
  const c = ranking();
  c.listActivity.set('mirrored', {latest:{at:10}});
  const items = [row('old', 'idle', 9), row('mirrored', 'idle', 1),
    row('dispatch', 'dispatching'), row('input', 'needs_input')];
  // input(0) > dispatch(1) > 其餘(2);rank 2 之內按 id → mirrored < old。
  // (mirrored 的鏡像活動時間不再影響位置;排在前面純粹因為 id 序。)
  assert.deepEqual(Array.from(c.sortSessions(items, 0), s => s.id),
    ['input', 'dispatch', 'mirrored', 'old']);
});

function delivery(responses = []) {
  const drafts = new Map(), waits = [], calls = [];
  const draftFor = id => {
    if (!drafts.has(id)) drafts.set(id, {text: 'original', atts: [], reading: 0, pending: null});
    return drafts.get(id);
  };
  const c = vm.createContext({
    draftFor, showDraft: () => {}, signingOut: false,
    state: {current: 'codex:a'}, canUse: () => true, navigator: {onLine: true},
    crypto: {randomUUID: () => 'receipt-1'}, AbortSignal,
    setTimeout: (resolve, ms) => waits.push({resolve, ms}),
    api: async (url, options) => { calls.push({url, method: options.method}); throw Error('lost POST reply'); },
    apiJSON: async (url, options) => {
      calls.push({url, method: options.method || 'GET'});
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response || {cards: []};
    },
  });
  vm.runInContext(section('function finishInput(', 'async function checkDelivery(') + '\n' +
    section('async function sendInput(', 'async function interrupt('), c);
  const tick = async () => {
    assert.ok(waits.length, 'a receipt check is scheduled');
    waits.shift().resolve();
    for (let i = 0; i < 8; i++) await Promise.resolve();
  };
  return {c, draftFor, waits, calls, tick};
}
test('lost POST reply triggers three GET checks, never a second POST, and preserves draft', async () => {
  const h = delivery([new Error('offline'), {cards: []}, {cards: []}]);
  await h.c.sendInput('original');
  const delays = [];
  for (let i = 0; i < 3; i++) { delays.push(h.waits[0].ms); await h.tick(); }
  assert.deepEqual(delays, [2000, 8000, 20000]);
  assert.deepEqual(h.calls.map(c => c.method), ['POST', 'GET', 'GET', 'GET']);
  assert.equal(h.draftFor('codex:a').pending.phase, 'unknown');
  assert.equal(h.draftFor('codex:a').text, 'original');
});
test('only a matching user receipt confirms delivery; changing sessions keeps drafts separate', async () => {
  const h = delivery([
    {cards: [{role: 'assistant', body: {client_id: 'receipt-1'}}, {role: 'user', body: {client_id: 'other'}}]},
    {cards: [{role: 'user', body: {client_id: 'receipt-1'}}]},
  ]);
  await h.c.sendInput('original');
  h.c.state.current = 'codex:b';
  h.draftFor('codex:b').text = 'next session draft';
  await h.tick();
  assert.equal(h.draftFor('codex:a').pending.phase, 'verifying');
  await h.tick();
  assert.equal(h.draftFor('codex:a').pending, null);
  assert.equal(h.draftFor('codex:a').text, '');
  assert.equal(h.draftFor('codex:b').text, 'next session draft');
  assert.equal(h.calls.length, 3);
  assert.ok(h.calls.every(c => c.url.includes('codex%3Aa')));
});
test('SSE receipt arriving during wait prevents further transport and leaves next draft intact', async () => {
  const h = delivery();
  await h.c.sendInput('original');
  h.c.confirmEcho('codex:a', [{role: 'user', body: {client_id: 'receipt-1'}}]);
  h.draftFor('codex:a').text = 'next draft';
  await h.tick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.draftFor('codex:a').text, 'next draft');
});
test('ending tracking while a GET is in flight cannot clear a new draft', async () => {
  const h = delivery();
  let reply;
  h.c.apiJSON = () => new Promise(resolve => { reply = resolve; });
  await h.c.sendInput('original');
  await h.tick();
  const d = h.draftFor('codex:a');
  d.pending = null;
  d.text = 'new draft';
  reply({cards: [{role: 'user', body: {client_id: 'receipt-1'}}]});
  for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(d.text, 'new draft');
  assert.equal(d.pending, null);
  assert.equal(h.waits.length, 0);
});
