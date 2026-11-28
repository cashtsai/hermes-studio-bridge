// 「重新送出」流程:同一個 client_id 重送、去重時限外要先經人確認。
// 跑的是出貨的 app.js 原始碼(vm 沙箱 + 假 transport),不開瀏覽器。
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

function ctx({apiImpl, confirmImpl} = {}) {
  const drafts = new Map();
  const c = vm.createContext({
    state: { current: 's1', sessions: [] },
    signingOut: false,
    draftFor: (id) => {
      if (!drafts.has(id)) drafts.set(id, { text: '', atts: [], reading: 0, pending: null, notice: '' });
      return drafts.get(id);
    },
    canUse: () => true,
    navigator: { onLine: true },
    crypto: { randomUUID: () => 'cid-fixed' },
    showDraft: () => {},            // DOM 更新在這裡不重要
    api: apiImpl || (async () => { throw new Error('network down'); }),
    apiJSON: async () => ({ cards: [] }),
    confirm: confirmImpl || (() => { throw new Error('confirm should not be called'); }),
    AbortSignal: { timeout: () => undefined },
    Date, JSON, Promise, setTimeout: (fn) => fn(),  // autoVerify 的等待立即走完
    encodeURIComponent,
  });
  // finishInput..retryDelivery 連續段 + autoVerifyDelivery(showDraft 用假的)
  vm.runInContext(section('function finishInput(', 'let forceStopTimer'), c);
  c.drafts = drafts;
  return c;
}

// postInput 的 catch 對 autoVerifyDelivery 是 fire-and-forget;讓 microtask 鏈走完。
async function settle() { for (let i = 0; i < 50; i++) await new Promise(r => setImmediate(r)); }

test('retry reuses the SAME client_id and clears pending on queued ack', async () => {
  const posts = [];
  let fail = true;
  const c = ctx({
    apiImpl: async (url, opts) => {
      posts.push(JSON.parse(opts.body));
      if (fail) throw new Error('timeout');
      return { ok: true, status: 200, json: async () => ({ ok: true, queued: true }) };
    },
  });
  await vm.runInContext('sendInput("hello")', c);        // 第一次:逾時 → unknown
  await settle();
  const d = c.drafts.get('s1');
  assert.equal(d.pending.phase, 'unknown');
  fail = false;
  await vm.runInContext('retryDelivery()', c);           // 重送:成功
  assert.equal(d.pending, null);
  assert.equal(d.notice, '已排入佇列');
  assert.ok(posts.length >= 2);
  assert.ok(posts.every(p => p.client_id === posts[0].client_id), 'client_id must not change on retry');
});

test('retry outside the 10-minute dedup window asks before resending', async () => {
  let asked = 0;
  const c = ctx({ confirmImpl: () => { asked++; return false; } });
  await vm.runInContext('sendInput("hello")', c);
  await settle();
  const d = c.drafts.get('s1');
  assert.equal(d.pending.phase, 'unknown');
  d.pending.startedAt = Date.now() - 10 * 60000;         // 偽裝成 10 分鐘前
  await vm.runInContext('retryDelivery()', c);
  assert.equal(asked, 1);
  assert.equal(d.pending.phase, 'unknown', 'declined confirm leaves the draft untouched');
});

test('retry does nothing unless the pending phase is unknown', async () => {
  let posts = 0;
  const c = ctx({ apiImpl: async () => { posts++; throw new Error('x'); } });
  const d = c.draftFor('s1');
  d.pending = { cid: 'cid-fixed', phase: 'verifying', startedAt: Date.now() };
  await vm.runInContext('retryDelivery()', c);
  assert.equal(posts, 0);
});
