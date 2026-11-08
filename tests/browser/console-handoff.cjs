const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const A = "claude_code:alpha", B = "codex:beta";
const fixtureCaps = ["replay","follow","input","attachments","interrupt","changes"];
const folder = path.resolve(__dirname, "../../static/console");
function gate() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
const row = (id, text, cid) => ({ id, kind: "text", role: cid ? "user" : "agent",
  body: { text, ...(cid ? { client_id: cid } : {}) } });
const snap = (seq, cards) => ({ latest_seq: seq, cards, status: {phase:"idle"} });
async function fixture(t, handle, list, viewport, sessionMeta = () => ({})) {
  const browser = await chromium.launch({channel:"chrome"});
  t.after(() => browser.close());
  const page = await browser.newPage({serviceWorkers:"block", viewport});
  page.setDefaultTimeout(6000);
  await page.addInitScript(() => sessionStorage.setItem("pocket.console.token", "test-only"));
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    assert.equal(url.host, "console.test");
    const match = /^\/app\/v2\/sessions\/([^/]+)\/(\w+)$/.exec(url.pathname);
    if (match) return handle(route, decodeURIComponent(match[1]), match[2], url);
    if (url.pathname === "/app/v2/sessions" && list) return list(route);
    if (url.pathname === "/app/v2/sessions") return route.fulfill({json: {sessions: [
      {id:A, provider:"claude_code", title:"Alpha", capabilities:fixtureCaps},
      {id:B, provider:"codex", title:"Beta", capabilities:fixtureCaps},
    ].map(s => {
      const meta = sessionMeta(s.id);
      return {...s, meta, capabilities: meta.approval ? [...fixtureCaps, "approve"] : fixtureCaps};
    })}});
    if (url.pathname === "/console") return route.fulfill({contentType:"text/html", body:
      (await fs.readFile(path.join(folder, "index.html"), "utf8")).replace(
        'data-console-access="local"', 'data-console-access="remote"')});
    if (url.pathname.startsWith("/console/static/")) return route.fulfill({path:path.join(folder, path.basename(url.pathname))});
    return route.fulfill({status:404, json:{}});
  });
  await page.goto("https://console.test/console");
  t.after(() => assert.deepEqual(errors, []));
  return page;
}
const stream = (route, envelopes = []) => route.fulfill({contentType:"text/event-stream",
  body: envelopes.map(e => "data: " + JSON.stringify(e) + "\n\n").join("")});

test("borrowed UI follows each session grant and never fetches hidden host tools", async t => {
  const rows = [{id:A,provider:"claude_code",title:"Demo chat",status:"running",capabilities:["replay"]},
    {id:B,provider:"codex",title:"Demo project",status:"running",capabilities:["replay","input","changes"]}];
  const calls = [];
  let changeStatus = 403;
  const page = await fixture(t, (r,id,action) => {
    calls.push([id,action]);
    if (action === "cards") return r.fulfill({json:{cards:[],latest_seq:1,status:{phase:"running"}}});
    if (action === "changes") return r.fulfill({status:changeStatus,json:changeStatus===200?{files:[]}:{detail:"unavailable"}});
    if (action === "input") return r.fulfill({json:{ok:true,session_id:id}});
    return r.fulfill({status:403,json:{}});
  }, r => r.fulfill({json:{sessions:rows}}), {width:1366,height:768});
  for (const sel of ['[data-mode="files"]','[data-mode="term"]','[data-mode="memory"]','#newsess-open','[data-mode="changes"]']) {
    assert.ok(await page.locator(sel).isHidden(), sel);
  }
  await page.getByText("Demo chat",{exact:true}).click();
  await page.getByText("這台電腦只能閱讀此對話").waitFor();
  for (const sel of ["#composer","#interrupt","#rail"]) assert.ok(await page.locator(sel).isHidden());
  await page.evaluate(() => {
    window.PocketConsoleMode.set("files");
    document.getElementById("composer").requestSubmit();
    document.getElementById("interrupt").click();
  });
  assert.ok(await page.locator("#files-view").isHidden());
  await page.getByText("Demo project",{exact:true}).click();
  await page.locator("#rail").getByText("這台電腦無權讀取工作目錄").waitFor();
  assert.ok(await page.locator("#attach").isHidden());
  assert.ok(await page.locator("#interrupt").isHidden());
  await page.locator("#input").fill("send to the existing thread");
  await page.locator("#send").click();
  await page.getByText("主機已收到").waitFor();
  changeStatus = 404;
  await page.locator('[data-mode="changes"]').click();
  await page.locator("#ch-list").getByText("找不到工作目錄或 Git 儲存庫").waitFor();
  changeStatus = 200;
  await page.locator("#ch-reload").click();
  await page.locator("#ch-list").getByText("工作目錄乾淨", {exact:false}).waitFor();
  rows[1].capabilities = ["replay"];
  await page.evaluate(() => loadSessions());
  assert.ok(await page.locator("#changes-view").isHidden());
  await page.getByText("這台電腦只能閱讀此對話").waitFor();
  assert.deepEqual(calls.filter(([id])=>id===A), [[A,"cards"]]);
  assert.deepEqual(calls.filter(([,op])=>["input","events","interrupt"].includes(op)), [[B,"input"]]);
});

test("Chromebook-sized fresh Chrome profile: resume host activity, distinguish names and settle just one approval", async t => {
  const titles = ["首頁搜尋", "工作區導覽", "字型樣張", "交接清單", "發佈檢查", "附件預覽"];
  const rows = Array.from({length:36}, (_,i) => ({id:"codex:job-" + i, title:titles[i % titles.length],
    provider:"codex", subtitle:i === 31 ? "/demo/brand" : "/demo/console",
    status:"idle", capabilities:["replay","input","approve"]}));
  rows[31].title = rows[35].title = "Release";
  rows[35].status = "running";
  rows[31].last_event_at = "2026-01-02T00:00:00Z";
  rows[35].last_event_at = "2026-01-03T00:00:00Z";
  rows[34].last_event_at = "unknown";
  const waiting = i => Object.assign(rows[i], {status:"waiting_approval",
    meta:{approval:{id:"aid-" + i, status:"pending"}}});
  waiting(4); waiting(5).meta.approval.status = "approved";
  waiting(6).meta.approval.expires_at = 1; waiting(7).capabilities = ["input"];
  rows[8].status = "waiting_approval"; rows[8].meta = {prompt:{options:[{key:"1"}]}};
  const posted = [];
  const page = await fixture(t, async (r,id,action) => {
    if (action === "cards") return r.fulfill({json:snap(0,[row("msg","original " + id)])});
    if (action === "events") return stream(r);
    if (action === "approve") posted.push([id, r.request().postDataJSON()]);
    return r.fulfill({json:{ok:true}});
  }, r => r.fulfill({json:{host_name:"Demo Studio", sessions:rows}}), {width:1366,height:768});
  await page.getByText("主機 · Demo Studio").waitFor();
  const recent = page.locator("#recent-list .work-row"), pending = page.locator("#pending-list .work-row");
  for (const id of ["#all-work","#logout"]) {
    assert.ok(await page.locator(id).evaluate(el => el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth));
  }
  if (process.env.POCKET_QA_SCREENSHOTS) {
    await fs.mkdir(process.env.POCKET_QA_SCREENSHOTS, {recursive:true});
    await page.screenshot({path:path.join(process.env.POCKET_QA_SCREENSHOTS, "handoff-home.png")});
  }
  assert.deepEqual(await recent.evaluateAll(els => els.map(e => e.dataset.id)), ["codex:job-35","codex:job-31"]);
  assert.equal(await pending.count(), 1);
  assert.equal(await page.locator("#awaiting-n").textContent(), "1");
  await recent.first().click();
  await page.locator("#cards").getByText("original codex:job-35").waitFor();
  await page.locator("#input").fill("my draft");
  await page.getByRole("button",{name:"回到工作接力"}).click();
  assert.equal(new URL(page.url()).hash,"");
  await recent.first().click();
  assert.equal(await page.locator("#input").inputValue(),"my draft");
  await page.locator("#back-home").click();
  await page.getByRole("searchbox").fill("brand release");
  const hit = page.locator("#home-results .work-row");
  assert.equal(await hit.count(),1);
  assert.match(await hit.innerText(),/job-31/);
  await hit.click();
  await page.locator("#cards").getByText("original codex:job-31").waitFor();
  await page.locator("#back-home").click();
  await page.getByRole("searchbox").fill("");
  await pending.click();
  await page.getByRole("button",{name:"核准", exact:true}).click();
  await page.locator("#awaiting").waitFor({state:"hidden"});
  await page.locator("#back-home").click();
  await page.getByText("目前沒有可處理的核准").waitFor();
  assert.deepEqual(posted,[["codex:job-4",{approve:true,approval_id:"aid-4"}]]);
  assert.ok(!(await page.evaluate(() => JSON.stringify(localStorage))).includes("codex:job"));
});

test("narrow home: retry a failed list, search all sessions and don't misreport degraded approvals", async t => {
  let fails = true, degraded = false;
  const sessions = [{id:A,title:"Same",provider:"claude_code",subtitle:"/demo/A",status:"idle",capabilities:fixtureCaps},
    {id:B,title:"Same",provider:"codex",subtitle:"/demo/B",status:"idle",capabilities:fixtureCaps}];
  const page = await fixture(t, (r,id,action) => action === "events" ? stream(r) : r.fulfill({json:snap(0,[])}),
    r => fails ? r.fulfill({status:503,json:{}}) :
      r.fulfill({json:{sessions,degraded_providers:degraded?["codex"]:[]}}), {width:390,height:844});
  await page.locator("#home-notice").getByText(/無法更新/).waitFor();
  assert.equal(await page.getByText("目前沒有可處理的核准").count(),0);
  fails = false;
  await page.locator("#home-refresh").click();
  await page.getByText("尚無可確認的活動時間").waitFor();
  await page.getByRole("button",{name:"全部工作"}).click();
  await page.locator("#pal-q").fill("demo/B");
  await page.locator("#pal-q").press("Enter");
  await page.waitForURL(/#session=codex%3Abeta$/);
  await page.locator("#back-home").click();
  degraded = true;
  await page.locator("#home-refresh").click();
  await page.getByText("待處理清單尚不完整").waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
});

test("lost HTTP acknowledgement: read back by ID, never post twice or change the other draft", async t => {
  const posts = [], posted = gate(), lose = gate(), oldSnap = gate(), oldStarted = gate();
  let snapshots = 0, showReceipt = false;
  const page = await fixture(t, async (r, id, action) => {
    if (action === "cards") {
      if (id === A && ++snapshots === 1) { oldStarted.resolve(); await oldSnap.promise; }
      return r.fulfill({json:snap(0, [row("base", "History " + id),
        ...(id === A && showReceipt ? [row("receipt", "send A", posts[0].client_id)] : [])])});
    }
    if (action === "input") {
      assert.equal(id, A); posts.push(r.request().postDataJSON()); posted.resolve();
      await lose.promise; return r.abort("failed");
    }
    return action === "events" ? stream(r) : r.fulfill({json:{}});
  });
  await page.getByText("Alpha", {exact:true}).click();
  await oldStarted.promise;
  await page.locator("#input").fill("send A");
  await page.locator("#file-pick").setInputFiles({name:"proof.txt", mimeType:"text/plain", buffer:Buffer.from("draft")});
  await page.getByText("proof.txt", {exact:false}).waitFor();
  await page.getByText("Beta", {exact:true}).first().click();
  await page.locator("#cards").getByText("History " + B).waitFor();
  oldSnap.resolve();
  await page.locator("#input").fill("B stays here");
  assert.equal(await page.locator("#atts").isHidden(), true);
  await page.getByText("Alpha", {exact:true}).first().click();
  assert.equal(await page.locator("#input").inputValue(), "send A");
  await page.locator("#composer").evaluate(f => { f.requestSubmit(); f.requestSubmit(); });
  await posted.promise;
  await page.getByText("Beta", {exact:true}).first().click();
  lose.resolve();
  assert.equal(await page.locator("#input").inputValue(), "B stays here");
  await page.getByText("Alpha", {exact:true}).first().click();
  await page.getByRole("button", {name:"確認送達"}).click();
  await page.getByText("目前未找到收據", {exact:false}).waitFor();
  showReceipt = true;
  await page.getByRole("button", {name:"確認送達"}).click();
  await page.getByText("主機已收到", {exact:true}).waitFor();
  assert.equal(await page.locator("#input").inputValue(), "");
  assert.equal(posts.length, 1);
  assert.ok(posts[0].client_id);
  assert.equal(posts[0].attachments[0].filename, "proof.txt");
  await page.getByText("Beta", {exact:true}).first().click();
  assert.equal(await page.locator("#input").inputValue(), "B stays here");
  assert.equal(await page.locator("#cards").getByText("History " + A).count(), 0);
});

test("live card envelope, cursor on reconnect, expired cursor snapshot without dispatch", async t => {
  const cursors = [], expire = gate();
  let snapshots = 0;
  const page = await fixture(t, async (r, id, action, url) => {
    if (action === "input") assert.fail("reconnecting must not start work");
    if (action === "cards") return r.fulfill({json:++snapshots === 1
      ? snap(10, []) : snap(23, [row("c", "from phone while away")])});
    if (action === "events") {
      cursors.push(url.searchParams.get("since_seq"));
      if (cursors.length === 1) return stream(r, [
        {type:"card.upsert", seq:11, data:{card:row("c", "live reply")}},
        {type:"session.status", seq:12, data:{phase:"run",busy:true}},
      ]);
      if (cursors.length === 2) { await expire.promise; return r.fulfill({status:410, json:{}}); }
      return stream(r);
    }
    return r.fulfill({json:{}});
  });
  await page.getByText("Beta", {exact:true}).click();
  await page.locator("#cards").getByText("live reply").waitFor();
  assert.equal(await page.locator("#sh-status").textContent(), "run");
  await page.locator('[data-mode="changes"]').click();
  expire.resolve();
  await page.locator("#cards").getByText("from phone while away").waitFor({state:"attached"});
  assert.ok(await page.locator("#changes-view").isVisible());
  assert.deepEqual(cursors.slice(0,2), ["10","12"]);
  assert.equal(await page.locator("#sh-status").textContent(), "idle");
  assert.equal(await page.locator("#cards .card").count(), 1);
});

test("late attachment stays with A, and the first A snapshot cannot replace the second A", async t => {
  const hold = gate(), started = gate();
  let aLoads = 0;
  const page = await fixture(t, async (r, id, action) => {
    if (action === "cards") {
      if (id === A && ++aLoads === 1) {
        started.resolve(); await hold.promise;
        return r.fulfill({json:snap(0, [row("old", "obsolete snapshot")])});
      }
      return r.fulfill({json:snap(20, [row("fresh", "current " + id)])});
    }
    return action === "events" ? stream(r) : r.fulfill({json:{}});
  });
  await page.evaluate(() => {
    const read = FileReader.prototype.readAsDataURL;
    FileReader.prototype.readAsDataURL = function(file) {
      window.finishAttachment = () => read.call(this, file);
    };
  });
  await page.getByText("Alpha", {exact:true}).click(); await started.promise;
  await page.locator("#file-pick").setInputFiles({name:"alpha-only.txt", mimeType:"text/plain", buffer:Buffer.from("A")});
  assert.ok(await page.locator("#send").isDisabled());
  await page.getByText("Beta", {exact:true}).first().click();
  await page.evaluate(() => window.finishAttachment());
  await page.locator("#cards").getByText("current " + B).waitFor();
  assert.ok(await page.locator("#atts").isHidden());
  await page.getByText("Alpha", {exact:true}).first().click();
  await page.locator("#cards").getByText("current " + A).waitFor();
  await page.locator("#atts").getByText("alpha-only.txt", {exact:false}).waitFor();
  const stale = page.waitForResponse(r => decodeURIComponent(r.url()).includes(A + "/cards"));
  hold.resolve();
  await (await stale).finished();
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  assert.equal(await page.locator("#cards .card-body").textContent(), "current " + A);
});

test("expired stream and temporarily unavailable snapshot preserve history and recover without replay", async t => {
  const cursors = [];
  let snapshots = 0, offline = true, reopened = false;
  const page = await fixture(t, async (r, id, action, url) => {
    if (action === "cards") {
      ++snapshots;
      if (snapshots > 1 && offline) return r.fulfill({status:503, json:{}});
      return r.fulfill({json:snap(snapshots === 1 ? 6 : reopened ? 16 : 15, [
        row("old", "already visible"), ...(snapshots === 1 ? [] : [row("new", "arrived while away")]),
        ...(reopened ? [row("tail","live again")] : [])])});
    }
    if (action === "events") {
      cursors.push(url.searchParams.get("since_seq"));
      if (cursors.length === 1) return r.fulfill({status:410, json:{}});
      return stream(r, [{type:"card.upsert", seq:15, data:{card:row("new","duplicate old event")}},
        {type:"card.upsert", seq:16, data:{card:row("tail","live again")}}]);
    }
    assert.equal(r.request().method(), "GET", "recovery must only read");
    return r.fulfill({json:{}});
  });
  await page.getByText("Beta", {exact:true}).click();
  await page.locator("#stream-health").getByText("連線中斷，正在補接").waitFor();
  assert.equal(await page.locator("#cards .card-body").textContent(), "already visible");
  await page.locator("#input").fill("draft survived");
  if (process.env.POCKET_TEST_LONG_OUTAGE === "1") await new Promise(r => setTimeout(r, 30000));
  offline = false;
  await page.locator("#cards").getByText("live again").waitFor({timeout:20000});
  assert.deepEqual(await page.locator("#cards .card-body").allTextContents(),
    ["already visible", "arrived while away", "live again"]);
  assert.deepEqual(cursors.slice(0,2), ["6","15"]);
  assert.equal(await page.locator("#input").inputValue(), "draft survived");
  reopened = true;
  await page.reload();
  await page.locator("#cards").getByText("live again").waitFor();
  assert.equal(await page.locator("#cards .card").count(), 3);
  assert.equal(await page.locator("#input").inputValue(), "", "borrowed-browser drafts are not persisted");
});

test("next question shares an approval ID; each displayed step sends its own index and option", async t => {
  let step = 0;
  const decisions = [];
  const page = await fixture(t, async (r, id, action) => {
    if (action === "approve") {
      const b = r.request().postDataJSON();
      decisions.push([b.approval_id, b.expected_q_index, b.key]);
      step++;
      return r.fulfill({json:{ok:true, status:step === 1 ? "next_question" : "answered"}});
    }
    return action === "events" ? stream(r) : r.fulfill({json:snap(0,[])});
  }, undefined, undefined, id => id === B && step < 2 ? {approval:{id:"aid", kind:"question", q_index:step, q_total:2,
    title:"Question " + step}, console_options:[{key:"opt0",label:"Option " + step}]} : {});
  await page.locator('#pending-list [data-id="' + B + '"]').click();
  await page.getByRole("button",{name:"opt0. Option 0",exact:true}).click();
  await page.getByRole("button",{name:"opt0. Option 1",exact:true}).click();
  await page.locator("#approval").waitFor({state:"hidden"});
  assert.deepEqual(decisions, [["aid",0,"opt0"],["aid",1,"opt0"]]);
});

test("approval expires on screen without waiting for a list refresh", async t => {
  const now = Date.now();
  const page = await fixture(t, (r, id, action) => {
    assert.equal(r.request().method(), "GET");
    return action === "events" ? stream(r) : r.fulfill({json:snap(0,[])});
  }, undefined, undefined, () => ({approval:{id:"exp", title:"Short approval", expires_at:now / 1000 + 5}}));
  await page.clock.install({time:new Date(now)});
  await page.locator('#pending-list [data-id="' + B + '"]').click();
  await page.getByRole("button",{name:"核准",exact:true}).waitFor();
  await page.clock.fastForward(5100);
  await page.locator("#approval").waitFor({state:"hidden"});
});
