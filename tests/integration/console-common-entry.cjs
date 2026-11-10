// Optional joint test; PYTHONPATH includes the Pocket ID candidate, not production.
const test=require("node:test"), assert=require("node:assert/strict");
const {spawn}=require("node:child_process");
const {createInterface}=require("node:readline"), path=require("node:path");
const {randomUUID}=require("node:crypto");
const {chromium}=require("playwright");

for(const mode of ["qr","code"]) test("common "+mode+" to scoped relay login across browser origins",{skip:process.env.ENTRY_QA!=="1",timeout:45000},async t=>{
  const key=randomUUID(), child=spawn(process.env.PYTHON||"python3",[path.join(__dirname,"common_entry_fixture.py")],
    {env:{...process.env,BRIDGE_TOKEN:"fixture-test-only",ENTRY_QA_KEY:key},stdio:["ignore","pipe","inherit"]});
  t.after(()=>child.kill());
  const lines=createInterface({input:child.stdout});
  let port;
  for await(const line of lines){ if(line.startsWith('{"port":')){port=JSON.parse(line).port; break;} }
  assert.ok(port,"fixture did not start");
  const base="http://127.0.0.1:"+port;
  const browser=await chromium.launch({channel:"chrome"});
  t.after(()=>browser.close());
  const context=await browser.newContext({serviceWorkers:"block"}), page=await context.newPage();
  const relay="https://owner.relay.example";
  await context.route("**/*",async route=>{
    const req=route.request(), url=new URL(req.url());
    if(!["console.pocketone.app","owner.relay.example"].includes(url.host)) return route.abort();
    if(url.origin===relay && url.pathname==="/console")
      return route.fulfill({contentType:"text/html",body:"<title>QA claim</title>"});
    const headers={...await req.allHeaders(),host:url.host};
    delete headers["content-length"];
    const response=await context.request.fetch(base+url.pathname,{method:req.method(),headers,data:req.postDataBuffer()||undefined});
    const out={...response.headers()}; delete out["content-length"];
    await route.fulfill({status:response.status(),headers:out,body:await response.body()});
  });
  const mobile=async (op,body)=>{
    const r=await context.request.post(base+"/"+op,{headers:{"x-qa":key},data:body});
    assert.equal(r.status(),200,await r.text());return r.json();
  };
  const ticket=page.waitForResponse(r=>r.url().endsWith("/start"));
  await page.goto("https://console.pocketone.app/");
  const {rid}=await (await ticket).json();
  await page.waitForFunction(()=>{const q=document.querySelector("#qr");return q && !q.hidden && q.naturalWidth>0});
  if(process.env.POCKET_QA_SCREENSHOTS) await page.screenshot({path:path.join(process.env.POCKET_QA_SCREENSHOTS,"common-entry.png")});
  if(mode==="code"){
    const offer=await mobile("code",{});
    assert.equal(offer.expires_in,900);
    await page.locator("#code").fill(offer.code);
    await page.locator("#manual button").click();
    await page.locator("#handoff").waitFor();
    assert.equal((await mobile("code",{handle:offer.handle})).rid,rid);
  }
  const {challenge}=await mobile("scan",{rid});
  await page.getByRole("button",{name:"繼續到這台主機"}).click();
  await page.getByText("請回到手機上的 Pocket 核對並批准，不需要再掃一次。").waitFor();
  const local=await mobile("lookup",{rid,challenge});
  await mobile("approve",{rid:local.rid});
  await page.waitForURL(relay+"/console");
  const claimed=await page.evaluate(async()=>{
    const code=document.cookie.match(/(?:^|; )pocket_console_claim=([^;]+)/)?.[1];
    if(!code) return "missing claim";
    const r=await fetch("/pair/claim",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code:decodeURIComponent(code),platform:"web",device_name:"QA"})});
    return (await r.json()).scope;
  });
  assert.equal(claimed,"console-remote");
});
