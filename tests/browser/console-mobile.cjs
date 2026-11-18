const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");
const jobs = [
  {id:"codex:idle",provider:"codex",title:"長期維護工作",status:"idle"},
  {id:"cc:run",provider:"claude_code",title:"驗證手機版面",status:"running"},
  {id:"hermes:main",provider:"hermes",title:"袁方",status:"idle",last_event_at:42},
  {id:"codex:run",provider:"codex",title:"處理介面修正",status:"running"},
];
async function shot(page, name) {
  const dir = process.env.POCKET_QA_SCREENSHOTS;
  if (!dir) return;
  await fs.mkdir(dir,{recursive:true});
  await page.screenshot({path:path.join(dir,name+".png")});
}
test("keyboard order follows rendered groups; sidebar is tabbable; palette returns focus", async t => {
  const {page} = await workstation(t,false,{jobs,isolated:true});
  await page.locator(".sess").first().waitFor();
  const ids = await page.locator(".sess").evaluateAll(rows=>rows.map(n=>n.dataset.id));
  assert.deepEqual(ids,["hermes:main","cc:run","codex:run","codex:idle"]);
  await page.locator("#refresh").focus(); await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.id),ids[0]);
  await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(()=>document.activeElement.id),"input");
  await page.locator("#cards").focus();
  for (let i=1;i<ids.length;i++) {
    await page.keyboard.press("j");
    assert.equal(await page.evaluate(()=>state.current),ids[i]);
  }
  await page.keyboard.press("k");
  assert.equal(await page.evaluate(()=>state.current),ids[2]);
  await page.locator("#input").fill("保留草稿");
  await page.keyboard.press("Control+k"); await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(()=>document.activeElement.id),"input");
  await page.keyboard.press("Control+k");
  await page.locator("#pal-q").fill("袁方"); await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(()=>document.activeElement.id),"input");
  await shot(page,"desktop-1366x768");
});
test("mobile uses full conversation and visible composer; failures persist without overlap", async t => {
  const {page} = await workstation(t,false,{jobs,isolated:true,viewport:{width:390,height:844}});
  page.setDefaultTimeout(8000);
  await page.locator('#recent-list .work-row').first().click();
  await page.locator("#cards .card").first().waitFor();
  assert.ok(await page.locator("#sidebar").isHidden());
  assert.notEqual(await page.evaluate(()=>document.activeElement.id),"input");
  for (const selector of [".icon-btn","#send","#attach"]) {
    for (const node of await page.locator(selector).all()) if (await node.isVisible()) {
      const r = await node.boundingBox(); assert.ok(r.height>=44 && r.width>=44,selector);
    }
  }
  const geometry = async () => page.evaluate(() => {
    const r = document.querySelector("#composer").getBoundingClientRect();
    const notice = document.querySelector("#toast").getBoundingClientRect();
    return {bottom:r.bottom,right:r.right,top:r.top,noticeBottom:notice.bottom,
      overflow:document.documentElement.scrollWidth>innerWidth,height:innerHeight};
  });
  await shot(page,"mobile-390x844");
  await page.evaluate(()=>{toast("無法確認決定：主機未回應",true);toast("已複製");});
  await page.waitForTimeout(3200);
  assert.equal(await page.locator("#toast").innerText(),"無法確認決定：主機未回應\n×");
  assert.equal(await page.locator("#toast").getAttribute("role"),"status");
  let r=await geometry(); assert.ok(!r.overflow && r.bottom<=r.height && r.noticeBottom<=r.top);
  await shot(page,"mobile-error-390x844");
  await page.locator("#toast button").click(); assert.ok(await page.locator("#toast").isHidden());
  await page.locator("#input").focus(); await page.setViewportSize({width:390,height:520});
  await page.waitForFunction(()=>document.querySelector("#app").getBoundingClientRect().height<=innerHeight);
  r=await geometry(); assert.ok(!r.overflow && r.bottom<=520 && r.top>=0,JSON.stringify(r));
  await shot(page,"mobile-keyboard-390x520");
  await page.setViewportSize({width:390,height:844});
  await page.locator("#back-home").click();
  assert.ok(await page.locator("#handoff").isVisible());
  assert.ok(await page.locator("#composer").isHidden());
  await shot(page,"mobile-home-390x844");
});
test("files show HTTP 500, relay 502 and network errors with a working retry", async t => {
  const {page} = await workstation(t,false,{isolated:true});
  let failure=500;
  await page.route("**/app/v2/fs/list*", route => failure==="network" ? route.abort("failed")
    : route.fulfill({status:failure||200,contentType:"application/json",
      body:JSON.stringify({path:"/fixture",parent:null,entries:[]})}));
  await page.locator('[data-mode="files"]').click();
  for (const error of [500,502,"network",401,404]) {
    failure=error;
    if(error!==500) await page.locator(".fb-retry").click();
    await page.waitForFunction(()=>document.querySelector(".fb-retry"));
    await page.locator("#fb-list .err").waitFor();
    assert.match(await page.locator("#fb-list").innerText(),/無法讀取目錄/);
    if(typeof error==="number" && error>=500) assert.match(await page.locator("#fb-list").innerText(),new RegExp(error));
  }
  failure=0; await page.locator(".fb-retry").click();
  await page.locator("#fb-list").getByText("（空資料夾）").waitFor();
});
