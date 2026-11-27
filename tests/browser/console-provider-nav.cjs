const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");

const jobs = () => ["claude_code", "cc2", "codex", "gemini", "openclaw", "grok", "opencode"].flatMap(provider =>
  Array.from({length:provider === "cc2" ? 2 : 10}, (_,i) => ({
    id:provider+":"+String(i).padStart(2,"0"), provider, title:i === 0 ? "Release review" : "Pocket workspace "+i,
    subtitle:"/demo/pocket", status:"idle", capabilities:["replay","input"],
  })));

async function screenshot(page, name) {
  if (!process.env.POCKET_QA_SCREENSHOTS) return;
  await fs.mkdir(process.env.POCKET_QA_SCREENSHOTS,{recursive:true});
  await page.screenshot({path:path.join(process.env.POCKET_QA_SCREENSHOTS,name+".png"),animations:"disabled"});
}

async function scrollIntoGroup(page, provider, offset=180) {
  await page.evaluate(({provider,offset}) => {
    const list=document.getElementById("session-list");
    const group=list.querySelector('[data-provider="'+provider+'"]');
    list.scrollTop += group.getBoundingClientRect().top-list.getBoundingClientRect().top+offset;
  },{provider,offset});
  await page.waitForFunction(provider => {
    const list=document.getElementById("session-list");
    const head=list.querySelector('[data-provider="'+provider+'"] .prov-head');
    return Math.abs(head.getBoundingClientRect().top-list.getBoundingClientRect().top)<1;
  },provider);
}

for (const theme of ["pocket","white","dark"]) test(theme+": sticky provider identity survives live updates and keyboard navigation", async t => {
  const {page,model} = await workstation(t,false,{jobs:jobs(),isolated:true});
  page.setDefaultTimeout(10000);
  const errors=[]; page.on("pageerror",e=>errors.push(e.message));
  await page.locator('.prov-group[data-provider="codex"] .sess').first().waitFor();
  await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
  assert.deepEqual(await page.locator(".prov-name").allTextContents(),
    ["Hermes","Claude Code","Codex","Gemini","OpenClaw","Grok","OpenCode"]);
  assert.equal(await page.locator('.prov-group[data-provider="claude_code"] .prov-count').textContent(),"12");
  await page.locator('.sess[data-id="claude_code:00"]').click();
  await page.locator('#cards [data-key="m79"]').waitFor();
  assert.equal(await page.locator('.sess[data-id="claude_code:00"] .sess-provider').textContent(),"CC");
  assert.equal(await page.locator('.sess[data-id="codex:00"] .sess-provider').textContent(),"CX");

  for (const provider of ["claude_code","codex"]) {
    await scrollIntoGroup(page,provider);
    const head=page.locator('.prov-group[data-provider="'+provider+'"] .prov-head');
    assert.equal(await head.evaluate(el=>getComputedStyle(el).position),"sticky");
    assert.ok(await head.evaluate(el=>{
      const r=el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
    }),"header must paint above rows");
    await screenshot(page,"provider-"+theme+"-"+provider);
  }
  assert.ok(await page.evaluate(()=>{
    const cc=document.querySelector('[data-provider="claude_code"] .prov-head').getBoundingClientRect();
    const cx=document.querySelector('[data-provider="codex"] .prov-head').getBoundingClientRect();
    return cc.bottom<=cx.top;
  }),"previous provider must leave when the next provider takes over");
  await page.evaluate(()=>{
    const list=document.getElementById("session-list");
    const group=list.querySelector('[data-provider="claude_code"]');
    list.scrollTop+=group.getBoundingClientRect().bottom-list.getBoundingClientRect().top-210;
  });
  await screenshot(page,"provider-"+theme+"-transition");
  await scrollIntoGroup(page,"codex");
  const before=await page.evaluate(()=>{
    window.providerRow=document.querySelector('.sess[data-id="codex:05"]').parentNode;
    window.providerHeading=document.querySelector('[data-provider="codex"] .prov-head');
    return document.getElementById("session-list").scrollTop;
  });
  model.jobs.find(s=>s.id==="codex:05").subtitle="/demo/updated";
  await page.evaluate(()=>loadSessions());
  assert.deepEqual(await page.evaluate(()=>[
    window.providerRow===document.querySelector('.sess[data-id="codex:05"]').parentNode,
    window.providerHeading===document.querySelector('[data-provider="codex"] .prov-head'),
    document.getElementById("session-list").scrollTop]),[true,true,before]);

  await page.evaluate(()=>{state.current="codex:05"; moveSelection(-1);});
  await page.waitForFunction(()=>document.activeElement?.dataset.id==="codex:04");
  assert.ok(await page.locator('.sess[data-id="codex:04"]').evaluate(el=>{
    const r=el.getBoundingClientRect(),h=el.closest(".prov-group").querySelector(".prov-head").getBoundingClientRect();
    return r.top>=h.bottom && el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
  }),"keyboard focus must not hide behind the sticky header");
  await page.locator("#sess-q").fill("Release review");
  await page.waitForFunction(()=>document.querySelectorAll(".session-row").length===7);
  assert.equal(await page.locator('.prov-group[data-provider="claude_code"] .prov-count').textContent(),"2");
  assert.equal(await page.locator('.prov-group[data-provider="codex"] .prov-count').textContent(),"1");
  await page.locator("#session-list").evaluate(el=>{el.scrollTop=0;});
  await screenshot(page,"provider-"+theme+"-search");
  await page.locator("#sess-q").fill("No matching session");
  await page.getByText("找不到符合的 session",{exact:true}).waitFor();
  await page.locator(".list-empty-clear").click();
  assert.equal(await page.locator('.prov-group[data-provider="codex"] .prov-count').textContent(),"10");
  assert.deepEqual(errors,[]);
});

test("provider labels remain bounded at Chromebook, tablet and mobile widths", async t => {
  const {page,model}=await workstation(t,false,{jobs:jobs(),isolated:true});
  page.setDefaultTimeout(10000);
  await page.locator('.prov-group[data-provider="codex"] .sess').first().waitFor();
  await page.locator('.sess[data-id="codex:00"]').click();
  for (const width of [1366,1024,820,768,390,320]) {
    await page.setViewportSize({width,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator("#sidebar").isVisible(),width>760);
    if(width>760) {
      await scrollIntoGroup(page,"codex");
      assert.ok(await page.locator("#session-list").evaluate(el=>el.scrollWidth<=el.clientWidth));
      assert.ok(await page.locator(".prov-name").evaluateAll(nodes=>nodes.every(n=>n.scrollWidth<=n.clientWidth)));
    }
    await screenshot(page,"provider-width-"+width);
  }
  await page.setViewportSize({width:1024,height:768});
  model.jobs.push({id:"unknown:1",provider:'<img src=x onerror="alert(1)">',title:"Unknown provider",status:"idle"});
  await page.evaluate(()=>loadSessions());
  assert.equal(await page.locator("#session-list img").count(),0);
  assert.ok((await page.locator(".prov-name").allTextContents()).includes('<img src=x onerror="alert(1)">'));
});
