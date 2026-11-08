// Real browser, HTTP, QR claim, SSE, scope and revocation. Only the provider boundary is synthetic.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const readline = require("node:readline");
const { chromium } = require("playwright");

const live = !!process.env.CONSOLE_QA_CODEX_SOURCE;
test((live ? "live Codex" : "fixture CC and CX") + ": QR decision, shared turns, reload and scoped sign-out",
  {timeout:live ? 180000 : 60000}, async t => {
  const owner = randomUUID();
  const host = spawn(process.env.PYTHON || "python3", [path.join(__dirname,"console_host.py")], {
    env:{PATH:process.env.PATH, CONSOLE_QA_OWNER:owner, ...(live ? {
      CONSOLE_QA_CODEX_SOURCE:process.env.CONSOLE_QA_CODEX_SOURCE,
      CONSOLE_QA_CODEX_BIN:process.env.CONSOLE_QA_CODEX_BIN,
    } : {})}, stdio:["ignore","pipe","inherit"],
  });
  const exited = once(host,"exit");
  t.after(async () => { if (host.exitCode === null) host.kill("SIGTERM"); await exited; });
  const lines = readline.createInterface({input:host.stdout});
  let thread;
  const port = await Promise.race([
    (async () => { for await (const s of lines) {
      if (/^THREAD [a-f0-9-]+$/.test(s)) thread = s.slice(7);
      if (/^PORT \d+$/.test(s)) return Number(s.slice(5));
    } })(),
    exited.then(() => { throw Error("Fixture failed to start"); }),
  ]);
  const origin = "http://127.0.0.1:" + port;
  const browser = await chromium.launch({channel:"chrome"});
  t.after(() => browser.close());
  const ctx = await browser.newContext({viewport:{width:1366,height:768},
    extraHTTPHeaders:{"X-Forwarded-For":"203.0.113.42"}});
  const page = await ctx.newPage();
  page.setDefaultTimeout(9000);
  const phone = await browser.newContext({extraHTTPHeaders:{Authorization:"Bearer " + owner}});
  const api = phone.request;
  const decision = async which => {
    const rid = new URL(await page.locator(".card code").textContent()).searchParams.get("rid");
    assert.ok(rid);
    const url = origin + "/app/v1/console/login/requests/" + encodeURIComponent(rid);
    assert.equal((await api.get(url)).status(), 200);
    assert.equal((await api.post(url + "/" + which)).status(), 200);
  };
  await page.goto(origin + "/console");
  await decision("deny");
  await page.locator("#st").getByText("已被拒絕", {exact:false}).waitFor();
  await page.reload();
  await decision("approve");
  await page.locator("#handoff").waitFor();
  if (live) assert.ok(thread);
  const sessions = live ? ["codex:" + thread] : ["claude_code:qa-cc","codex:qa-cx"];
  await page.locator('#session-list [data-id="' + sessions[0] + '"]').waitFor();
  assert.equal(await page.locator("body").getAttribute("data-console-access"),"remote");
  const tabToken = await page.evaluate(() => sessionStorage.getItem("pocket.console.token"));
  assert.ok(tabToken && tabToken !== owner);
  const tabHeaders = {Authorization:"Bearer " + tabToken};
  for (const sid of sessions) {
    const url = origin + "/app/v2/sessions/" + encodeURIComponent(sid);
    await page.locator('#session-list [data-id="' + sid + '"]').click();
    const nonce = randomUUID(), reply = live ? nonce : "fixture reply: " + nonce;
    const msg = live ? "Reply with exactly " + nonce + " and nothing else. Do not use tools." : nonce;
    const ack = page.waitForResponse(r => r.url() === url + "/input" && r.request().method() === "POST");
    await page.locator("#input").fill(msg);
    await page.locator("#send").click();
    assert.equal((await ack).status(),200);
    await page.locator("#cards").getByText(reply,{exact:true}).waitFor({timeout:live ? 45000 : 9000});
    const cards = (await (await api.get(url + "/cards")).json()).cards;
    assert.equal(cards.filter(c => c.role === "user" && c.body.text === msg).length,1);
    const followup = randomUUID();
    const next = live ? "Without tools, append :" + followup + " to your previous answer. Reply with just that string." : followup;
    const expected = live ? nonce + ":" + followup : "fixture reply: " + followup;
    assert.equal((await api.post(url + "/input",{data:{content:next,client_id:randomUUID()}})).status(),200);
    await page.locator("#cards").getByText(expected,{exact:true}).waitFor({timeout:live ? 45000 : 9000});
    await page.reload();
    await page.locator("#cards").getByText(expected,{exact:true}).waitFor();
  }
  const denied = await ctx.request.get(origin + "/files",{headers:tabHeaders});
  assert.equal(denied.status(),403);
  const revoked = page.waitForResponse(r => r.url() === origin + "/console/logout");
  await page.locator("#logout").click();
  assert.equal((await revoked).status(),200);
  assert.equal((await ctx.request.get(origin + "/app/v2/sessions",{headers:tabHeaders})).status(),401);
  assert.equal((await api.get(origin + "/app/v2/sessions")).status(),200);
});
