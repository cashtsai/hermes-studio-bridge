// 跑**出貨中的** static/console/app.js 裡那段 subscribeCards,餵它一串由
// bridge 自己產出的全域序號,看它會不會掉進無限重同步。
//
// 用 vm 切一段原始碼出來跑(同 tests/console_delivery_sort.test.cjs 的作法):
// 不是抄一份邏輯來驗,是驗真的那一份 —— 所以同一個檔在舊 commit 上會驗到
// 舊的那份邏輯,這正是「base 失敗 / head 通過」要的。
//
// 輸入(env):
//   SCENARIO_APP_JS    要驗的 app.js 路徑
//   SCENARIO_SEQ_FILE  {"seqs":[…],"tip":N} —— 由 python 那半邊打 bridge 的
//                      API 真的跑出來的全域序號(帶洞),不是手編的數字
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.env.SCENARIO_APP_JS, "utf8");
const lane = JSON.parse(fs.readFileSync(process.env.SCENARIO_SEQ_FILE, "utf8"));

function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `app.js 裡找不到這一段: ${start}`);
  return source.slice(a, b);
}

function console_() {
  const calls = {resyncs: 0, applied: [], health: []};
  let handlers = null;
  const ctx = vm.createContext({
    state: {latestSeq: 0, token: "t", current: "lane", stopStream: null,
            snapshotView: "v"},
    currentView: () => true,
    setStreamHealth: (msg) => calls.health.push(msg),
    loadCardSnapshot: () => { calls.resyncs += 1; },
    sseStream: (url, token, h) => { handlers = h; return () => {}; },
    upsertCard: (card) => calls.applied.push(card.id),
    confirmEcho: () => {},
    updateStatusChip: () => {},
    reflectSessionStatus: () => {},
    scheduleListRefresh: () => {},
    encodeURIComponent,
    Number,
  });
  vm.runInContext(section("function subscribeCards(", "function visibleReplyText("), ctx);
  vm.runInContext('subscribeCards("claude_code:lane-a", "v")', ctx);
  assert.ok(handlers, "subscribeCards 沒有訂閱任何串流");
  return {ctx, calls, feed: (envelope) => handlers.onEvent({data: envelope})};
}

const upsert = (seq) => ({seq, type: "card.upsert", durable_seq: seq,
                          data: {card: {id: "c" + seq, kind: "markdown",
                                        body: {text: "hi"}}}});
const ping = (seq) => ({seq, type: "ping", data: {}, durable_seq: null});

test("全域序號有洞是正常的 —— 進 session 不該卡在「正在補接」", () => {
  const {ctx, calls, feed} = console_();
  // 真實形狀:本 lane 拿到 2、5、7(3/4/6 被別的 session 佔走),中間夾著
  // 帶全域尖端的 keepalive ping。
  for (const seq of lane.seqs) feed(upsert(seq));
  assert.deepEqual(calls.applied, lane.seqs.map(s => "c" + s),
    `卡片被丟掉了 —— 收到 ${calls.applied.length}/${lane.seqs.length} 張。` +
    "使用者看到的就是「訊息沒出來」。");
  assert.equal(calls.resyncs, 0,
    `序號有洞就重同步了 ${calls.resyncs} 次 —— 每次重同步又會再看到洞,` +
    "於是永遠在補接:進 session 卡死在「連線中斷,正在補接」(2026-11-10 實錘)。");
  assert.equal(ctx.state.latestSeq, lane.seqs[lane.seqs.length - 1]);
});

test("ping 只推進游標,絕不觸發重同步", () => {
  const {ctx, calls, feed} = console_();
  feed(upsert(lane.seqs[0]));
  feed(ping(lane.tip));                 // 全域尖端 ≠ 本 lane 的序號
  assert.equal(calls.resyncs, 0,
    "keepalive ping 帶的是全域尖端,拿它跟本 lane 的序號比就是每次心跳都重同步");
  assert.equal(ctx.state.latestSeq, lane.tip, "ping 應該把重連游標推到全域尖端");
});

test("往後的序號要安靜丟掉,不是重同步", () => {
  const {calls, feed} = console_();
  feed(upsert(lane.seqs[1]));
  feed(upsert(lane.seqs[0]));           // 重放/亂序:比游標舊
  assert.deepEqual(calls.applied, ["c" + lane.seqs[1]]);
  assert.equal(calls.resyncs, 0);
});
