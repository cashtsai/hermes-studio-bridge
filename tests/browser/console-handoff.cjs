const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const A = "claude_code:alpha", B = "codex:beta";
const folder = path.resolve(__dirname, "../../static/console");
function gate() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
const row = (id, text, cid) => ({ id, kind: "text", role: cid ? "user" : "agent",
  body: { text, ...(cid ? { client_id: cid } : {}) } });
const snap = (seq, cards) => ({ latest_seq: seq, cards, status: {phase:"idle"} });
async function fixture(t, handle, sessionMeta = () => ({})) {
  const browser = await chromium.launch({channel:"chrome"});
  t.after(() => browser.close());
  const page = await browser.newPage({serviceWorkers:"block"});
  page.setDefaultTimeout(6000);
  await page.addInitScript(() => sessionStorage.setItem("pocket.console.token", "test-only"));
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    assert.equal(url.host, "console.test");
    const match = /^\/app\/v2\/sessions\/([^/]+)\/(\w+)$/.exec(url.pathname);
    if (match) return handle(route, decodeURIComponent(match[1]), match[2], url);
    if (url.pathname === "/app/v2/sessions") return route.fulfill({json: {sessions: [
      {id:A, provider:"claude_code", title:"Alpha"}, {id:B, provider:"codex", title:"Beta"},
    ].map(s => ({...s, meta:sessionMeta(s.id)}))}});
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
  expire.resolve();
  await page.locator("#cards").getByText("from phone while away").waitFor();
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
  }, id => id === B && step < 2 ? {approval:{id:"aid", kind:"question", q_index:step, q_total:2,
    title:"Question " + step, options:[{key:"opt0",label:"Option " + step}]}} : {});
  await page.getByText("Beta",{exact:true}).click();
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
  }, () => ({approval:{id:"exp", title:"Short approval", expires_at:now / 1000 + 5}}));
  await page.clock.install({time:new Date(now)});
  await page.getByText("Beta",{exact:true}).click();
  await page.getByRole("button",{name:"核准",exact:true}).waitFor();
  await page.clock.fastForward(5100);
  await page.locator("#approval").waitFor({state:"hidden"});
});
