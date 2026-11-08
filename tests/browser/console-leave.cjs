// Run with node --test and Playwright installed. Only the isolated loopback
// fixture is contacted; no production cookies, sessions or provider are used.
const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

test("borrowed browser locks on failure, revokes on retry, and caches no work", async t => {
  const folder = path.resolve(__dirname, "../../static/console");
  let revoked = false;
  let acknowledge = false;
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    function send(body, type = "application/json", status = 200) {
      res.writeHead(status, { "Content-Type": type }); res.end(body);
    }
    if (url.pathname === "/console") {
      if (url.searchParams.has("signin")) return send("Scan again", "text/plain");
      return send((await fs.readFile(path.join(folder, "index.html"), "utf8"))
        .replace('data-console-access="local"', 'data-console-access="remote"'), "text/html");
    }
    if (url.pathname === "/pair/claim") return send(JSON.stringify({ token: "fixture", scope: "console-remote" }));
    if (url.pathname === "/console/logout") {
      if (!acknowledge) return send("{}", "application/json", 503);
      if (req.headers.authorization !== "Bearer fixture") return send("{}", "application/json", 401);
      const active = !revoked; revoked = true;
      return send(JSON.stringify({ revoked: active }), "application/json", active ? 200 : 401);
    }
    if (url.pathname === "/app/v2/sessions") {
      return send(JSON.stringify({sessions: [{ id: "cx:qa", title: "fixture project", provider: "codex" }]}));
    }
    if (url.pathname.startsWith("/console/")) {
      try {
        const name = path.basename(url.pathname);
        const data = await fs.readFile(path.join(folder, name));
        if (name === "sw.js") res.setHeader("Service-Worker-Allowed", "/console");
        return send(data, {".js": "text/javascript", ".css": "text/css", ".png": "image/png",
          ".json": "application/json"}[path.extname(name)]);
      } catch (_) { /* Not a console asset. */ }
    }
    send("{}", "application/json", 404);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject); server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => { server.closeAllConnections(); server.close(); });
  const origin = "http://127.0.0.1:" + server.address().port;
  const browser = await chromium.launch({ channel: "chrome" });
  t.after(() => browser.close());
  const ctx = await browser.newContext();
  await ctx.addCookies([{name: "pocket_console_claim", value: "test-code", url: origin + "/console"}]);
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("pocket.console.token", "stale-grant"));
  await page.goto(origin + "/console");
  await page.getByText("fixture project", {exact:true}).waitFor();
  assert.equal(await page.evaluate(() => localStorage.getItem("pocket.console.token")), null);
  assert.equal(await page.evaluate(() => sessionStorage.getItem("pocket.console.token")), "fixture");
  await page.waitForFunction(() => !!navigator.serviceWorker.controller, null, {timeout: 12000});
  const paths = await page.evaluate(async () => {
    const cache = await caches.open("pocket-console-shell-v3");
    return (await cache.keys()).map(req => new URL(req.url).pathname);
  });
  assert.ok(paths.length && paths.every(p => p.startsWith("/console/static/")));
  const popup = page.waitForEvent("popup");
  await page.evaluate(() => window.open("/console"));
  const second = await popup;
  await second.getByText("fixture project", {exact:true}).waitFor();
  await page.getByText("登出這台電腦", {exact:true}).click();
  for (const tab of [page, second]) {
    await tab.getByText("畫面已鎖定", {exact:false}).waitFor();
    assert.ok(await tab.getByText("fixture project", {exact:true}).isHidden());
  }
  assert.equal(revoked, false);
  acknowledge = true;
  for (const tab of [page, second]) {
    await tab.getByRole("button", {name:"重試登出"}).click();
    await tab.waitForURL("**/console?signin=1");
    assert.equal(await tab.evaluate(() => sessionStorage.getItem("pocket.console.token")), null);
  }
  assert.equal(revoked, true);
  await ctx.setOffline(true);
  await page.goto(origin + "/console");
  await page.getByText("離線中", {exact:false}).waitFor();
  assert.ok(await page.getByText("fixture project", {exact:true}).isHidden());
});
