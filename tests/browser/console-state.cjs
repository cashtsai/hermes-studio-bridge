const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");

const previews = path.resolve(__dirname, "../../docs/console-w3-preview");
const job = {id:"hermes:yuanfang", provider:"hermes", title:"袁方", status:"idle", subtitle:"/fixture/workspace"};
async function until(check) {
  for (let n=0; n<100; n++) { if (await check()) return; await new Promise(r=>setTimeout(r,20)); }
  assert.fail("fixture event stream did not become ready");
}
function publish(model, stream, event) {
  const response = model.streams.get(stream);
  assert.ok(response, "SSE connection exists");
  response.write("data: " + JSON.stringify(event) + "\n\n");
}
async function open(t, jobs=[{...job}]) {
  const fixture = await workstation(t, false, {jobs, isolated:true, historySize:4});
  await fixture.page.locator('.sess[data-id="hermes:yuanfang"]').click();
  await until(()=>fixture.model.streams.has("/app/v2/sessions/hermes%3Ayuanfang/events"));
  await until(()=>fixture.model.streams.has("/app/v2/events"));
  return fixture;
}

test("status events expose lock reason, queue depth, reset time and distinct states", async t => {
  const phases = ["idle","running","waiting_approval","needs_input","queued","usage_limited",
    "error","dead","completed","dispatching"];
  const jobs = [{...job}, ...phases.map(status=>({id:"codex:"+status, provider:"codex", title:status, status}))];
  const {page,model} = await open(t, jobs);
  await fs.mkdir(previews, {recursive:true});
  let seq=100;
  async function status(phase, meta={}) {
    model.jobs[0] = {...job, status:phase, meta};
    publish(model, "/app/v2/sessions/hermes%3Ayuanfang/events",
      {seq:seq++, type:"session.status", data:{phase, ...meta}});
    await page.waitForFunction(p=>state.phase === p, phase);
  }
  const reason = "已被桌面版 Codex 佔用，3 則訊息已排隊，桌面釋放後自動送出";
  await status("running", {locked:true, lock_message:reason, queue_depth:3});
  assert.equal(await page.locator("#sh-status").textContent(), reason);
  assert.equal(await page.locator("#sh-status").evaluate(n=>n.classList.contains("working")), false);
  await page.screenshot({path:path.join(previews,"locked.png")});
  // Same-phase HTTP refresh must not hide richer status text.
  await page.evaluate(()=>loadSessions());
  assert.equal(await page.locator("#sh-status").textContent(), reason);
  for (const phase of phases) {
    const meta = phase === "queued" ? {queue_depth:3} : phase === "usage_limited"
      ? {usage_limit_reset_at:"2026-11-18T15:00:00Z"} : phase === "running"
      ? {label:"正在執行 Read：檢查設定", busy_since:Date.now()/1000-15} : {};
    await status(phase, meta);
    if (["running","queued","dispatching"].includes(phase)) await page.locator("#workbar").waitFor({state:"visible"});
    else await page.locator("#workbar").waitFor({state:"hidden"});
    if (phase === "queued") assert.match(await page.locator("#sh-status").textContent(), /3 則/);
    if (phase === "usage_limited") assert.match(await page.locator("#sh-status").textContent(), /2026.*11.*18.*重置/);
    assert.ok(await page.locator("#sh-status").evaluate((n,p)=>n.classList.contains("st-"+p), phase));
    await page.screenshot({path:path.join(previews,phase+".png")});
  }
  const colors = await page.evaluate(()=>Object.fromEntries(["idle","waiting_approval","needs_input","error","completed"]
    .map(p=>[p,getComputedStyle(document.querySelector('.sess[data-id="codex:'+p+'"] .st')).color])));
  assert.notEqual(colors.waiting_approval, colors.idle);
  assert.notEqual(colors.needs_input, colors.idle);
  assert.notEqual(colors.error, colors.idle);
  assert.notEqual(colors.completed, colors.idle);
  await page.evaluate(()=>updateStatusChip("usage_limited", {usage_limit_reset_at:"invalid"}));
  assert.equal(await page.locator("#sh-status").textContent(), "用量受限");
  await page.evaluate(()=>updateStatusChip("queued", {queue_depth:0}));
  assert.equal(await page.locator("#sh-status").textContent(), "排隊中");
  await status("needs_input");
  await page.locator("#workbar").waitFor({state:"hidden"});
  await page.evaluate(()=>applyTheme("dark"));
  await page.screenshot({path:path.join(previews,"needs-input-dark.png")});
});

test("message and read events update previews/unread without HTTP refetch or history mutation", async t => {
  const {page,model} = await open(t);
  const emoji = "\u{1f469}\u200d\u{1f4bb}";
  assert.equal(await page.evaluate(text=>clipPreview(text), "字".repeat(139)+emoji+"e\u0301"), "字".repeat(139)+emoji+"…");
  assert.equal(await page.evaluate(()=>clipPreview("e\u0301短訊息")), "e\u0301短訊息");
  let listRequests=0;
  page.on("request", r=>{if (new URL(r.url()).pathname === "/app/v2/sessions") listRequests++;});
  const originalCards = await page.locator("#cards").innerHTML();
  let seq=100;
  const send = (type,data,session="yuanfang")=>publish(model,"/app/v2/events",{seq:seq++,type,session,data});
  const preview = page.locator('.sess[data-id="hermes:yuanfang"] .sess-sub');
  const unread = page.locator('.sess[data-id="hermes:yuanfang"] .unread-mark');
  const message = (id,ts,content,source="tg",role="assistant")=>send("message.upsert",{message:{id,ts,content,source,role}});
  message("new",200,"<think>private reasoning</think><details><summary>步驟</summary>raw tool output</details>Telegram 的最新回覆");
  await page.waitForFunction(()=>document.querySelector(".sess-sub").textContent.includes("Telegram 的最新回覆"));
  assert.equal(await unread.count(),1);
  assert.doesNotMatch(await preview.textContent(),/private|raw tool/);
  send("read_cursor.update",{global:{last_read_seq:100,last_read_ts:200}});
  await unread.waitFor({state:"detached"});
  message("old",100,"舊訊息延遲改稿");
  await new Promise(r=>setTimeout(r,60));
  assert.match(await preview.textContent(),/Telegram/);
  assert.equal(await unread.count(),0);
  message("cron",300,"晨報已完成","cron");
  await unread.waitFor({state:"visible"});
  send("read_cursor.update",{global:{last_read_seq:50,last_read_ts:100}});
  await page.waitForFunction(()=>listActivity.get("hermes:yuanfang").readAt === 200000);
  assert.equal(await unread.count(),1);
  send("read_cursor.update",{global:{last_read_seq:103,last_read_ts:300}});
  await unread.waitFor({state:"detached"});
  message("user",400,"App 新指令","app","user");
  await page.waitForFunction(()=>document.querySelector(".sess-sub").textContent.includes("App 新指令"));
  assert.equal(await unread.count(),0);
  // Cursor-only updates work even when no timestamp is supplied.
  message("next",500,"等待閱讀");
  await unread.waitFor({state:"visible"});
  send("read_cursor.update",{global:{last_read_seq:seq-1}});
  await unread.waitFor({state:"detached"});
  await new Promise(r=>setTimeout(r,1000));
  assert.equal(listRequests,0,"typed events should not schedule the 800ms list fetch");
  assert.equal(await page.locator("#cards").innerHTML(),originalCards);
  assert.equal(model.posts.length,0);
  await page.evaluate(()=>loadSessions());
  assert.match(await preview.textContent(),/等待閱讀/);
  assert.equal(await unread.count(),0,"HTTP snapshot must not revert the read cursor");
  message("last",600,"三來源同步完成");
  await unread.waitFor({state:"visible"});
  await page.screenshot({path:path.join(previews,"preview-unread.png")});
  // A reconnect replay cannot erase newer text or re-add unread markers.
  publish(model,"/app/v2/events",{seq:100,type:"message.upsert",session:"yuanfang",
    data:{message:{id:"new",ts:200,role:"assistant",content:"重放的舊內容"}}});
  await new Promise(r=>setTimeout(r,60));
  assert.match(await preview.textContent(),/三來源同步完成/);
  send("session.status",{phase:"idle"});
  await until(()=>listRequests === 2); // Explicit load above + existing fallback refresh.
});

test("activity bar delays and coalesces transitions, retains elapsed time and respects reduced motion", async t => {
  const {page} = await open(t);
  await page.clock.install({time:new Date("2026-11-18T00:00:00Z")});
  await page.clock.pauseAt(new Date("2026-11-18T00:01:00Z"));
  const phase = (p,meta={})=>page.evaluate(([p,m])=>updateStatusChip(p,m),[p,meta]);
  const shown = ()=>page.locator("#workbar").isVisible();
  await phase("run",{busy:true});
  await page.clock.runFor(500);
  await phase("idle");
  await page.clock.runFor(2000);
  assert.equal(await shown(),false,"short turns never flash");
  await phase("run",{busy:true});
  await page.clock.runFor(1000);
  await phase("running",{label:"正在測試",busy_since:await page.evaluate(()=>Date.now()/1000-65)});
  await page.clock.runFor(499);
  assert.equal(await shown(),false);
  await page.clock.runFor(1);
  assert.equal(await shown(),true,"status duplicate does not postpone initial show");
  assert.match(await page.locator("#workbar-elapsed").textContent(),/1 分/);
  assert.equal(await page.locator("#workbar-label").textContent(),"正在測試");
  assert.equal(await page.locator("#workbar-label").evaluate(n=>getComputedStyle(n).animationName),"activity-shimmer");
  await phase("idle");
  await page.clock.runFor(200);
  assert.equal(await shown(),true);
  await phase("running",{label:"接著測試"});
  await page.clock.runFor(200);
  assert.equal(await shown(),true,"a continuation cancels delayed hide");
  await page.emulateMedia({reducedMotion:"reduce"});
  assert.equal(await page.locator("#workbar-label").evaluate(n=>getComputedStyle(n).animationName),"none");
  await phase("completed");
  await page.clock.runFor(299);
  assert.equal(await shown(),true);
  await phase("completed");
  await page.clock.runFor(1);
  assert.equal(await shown(),false,"duplicate completion does not postpone hide");
  await phase("queued",{queue_depth:4});
  await page.clock.runFor(1500);
  assert.match(await page.locator("#workbar-label").textContent(),/4 則/);
  await page.evaluate(()=>showHome());
  assert.equal(await shown(),false,"leaving a session clears the old activity immediately");
});

test("an older in-flight list snapshot cannot overwrite newer status details", async t => {
  const {page,model} = await open(t);
  let pending;
  await page.route("**/app/v2/sessions", route=>{pending=route;});
  await page.evaluate(()=>{window.pendingList=loadSessions();});
  await until(()=>pending);
  const reason="桌面工作中，2 則訊息已排隊";
  publish(model,"/app/v2/sessions/hermes%3Ayuanfang/events",
    {seq:200,type:"session.status",data:{phase:"running",locked:true,lock_message:reason,queue_depth:2}});
  await page.waitForFunction(()=>state.phase === "running");
  await pending.fulfill({json:{sessions:[{...job}]}});
  await page.evaluate(()=>window.pendingList);
  assert.equal(await page.locator("#sh-status").textContent(),reason);
  assert.equal(await page.locator('.sess[data-id="hermes:yuanfang"] .st').textContent(),"被佔用");
  await page.unroute("**/app/v2/sessions");
});
