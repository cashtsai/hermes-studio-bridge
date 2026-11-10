// Isolated origin/profile; no real provider, OS account or user session.
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const {chromium} = require("playwright");

async function workstation(t, standalone = false, options = {}) {
  const assets = path.resolve(__dirname, "../../../static/console");
  const jobs = options.jobs || ["codex:a","codex:b"].map(id => ({id, title:id, provider:"codex", status:"idle"}));
  const model = {posts:[], remote:false, jobs, streams:new Map()};
  const server = http.createServer(async (req,res) => {
    const url = new URL(req.url,"http://localhost");
    const send = (type,body,status=200) => {
      res.writeHead(status, {"Content-Type":type,"Cache-Control":"no-store"}); res.end(body);
    };
    const json = data => send("application/json",JSON.stringify(data));
    if (url.pathname === "/setup" || url.searchParams.has("signin")) return send("text/html","<title>Fixture</title>");
    if (url.pathname === "/console/logout") return json({revoked:true});
    if (url.pathname === "/app/v2/sessions") return json({sessions:model.jobs,
      ...(model.remote ? {console_scope:"console-remote"} : {})});
    if (url.pathname.endsWith("/cards")) return json({latest_seq:80, cards:Array.from({length:80},(_,i) => ({
      id:"m"+i, ts:i+1, role:i%2?"agent":"user", kind:"text", body:{text:"測試訊息 "+i}}))});
    if (url.pathname.endsWith("/changes")) return json({files:[{path:"sample.txt",status:"M"}]});
    if (url.pathname.endsWith("/diff")) return json({diff:"+sample"});
    if (url.pathname.endsWith("/input")) {
      let raw=""; for await (const chunk of req) raw+=chunk;
      model.posts.push(JSON.parse(raw));
      if (model.onInput) return model.onInput(res);
      return json({ok:true});
    }
    if (url.pathname.endsWith("/events")) {
      res.writeHead(200,{"Content-Type":"text/event-stream"}); res.write(": ping\n\n");
      model.streams.set(url.pathname, res);
      res.on("close", () => { if (model.streams.get(url.pathname) === res) model.streams.delete(url.pathname); });
      return;
    }
    if (url.pathname === "/console" || url.pathname.startsWith("/console/")) {
      const name=url.pathname === "/console" ? "index.html" : path.basename(url.pathname);
      const type={".html":"text/html",".css":"text/css",".js":"text/javascript",".json":"application/json",
        ".png":"image/png"}[path.extname(name)];
      if (type) {
        try { return send(type, await fs.readFile(path.join(assets,name))); } catch (_) {}
      }
    }
    return send("application/json","{}",404);
  });
  await new Promise((ok,fail) => server.once("error",fail).listen(0,"127.0.0.1",ok));
  t.after(() => {server.closeAllConnections(); server.close();});
  const origin = "http://127.0.0.1:"+server.address().port;
  const context = await chromium.launchPersistentContext("", {channel:"chrome", headless:!standalone,
    viewport:options.viewport || {width:1366,height:768}, ...(options.isolated ? {serviceWorkers:"block"} : {})});
  t.after(() => context.close());
  if (options.isolated) await context.route("**/*", route => new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort());
  const page = context.pages()[0];
  await page.goto(origin+"/setup");
  await page.evaluate(() => localStorage.setItem("pocket.console.token","synthetic-owner"));
  await page.goto(origin+"/console");
  return {origin,context,page,model};
}

module.exports = {workstation};
