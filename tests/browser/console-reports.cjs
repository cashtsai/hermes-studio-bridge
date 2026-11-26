const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");
const mid = "med_" + "a".repeat(32), second = "med_" + "b".repeat(32);
const ref = "/fixture/Release report.md";
const report = "# Bridge 上線檢查\n\n**結論：候選版驗證完成，尚未正式發布。**\n\n## 驗證結果\n\n| 項目 | 結果 | 備註 |\n|---|---|---|\n| Ubuntu | PASS | 重開機後啟動正常 |\n| Windows | 待驗 | 仍需真實裝置 |\n\n## 下一步\n\n1. 確認正式發布授權\n2. 驗證端到端操作\n\n```sh\ncurl --fail http://127.0.0.1:8081/health\n```\n\n<script>window.reportXSS=1</script>\n";
async function setup(t) {
  const calls = [], control = {status:200, payload:report, delay:false, pending:null, chunked:false};
  const fixture = await workstation(t, false, {isolated:true, handle: async(req,res,url) => {
    const json = data => {res.writeHead(200,{"Content-Type":"application/json"});res.end(JSON.stringify(data));};
    if (url.pathname.endsWith("/cards")) {
      json({latest_seq:2,cards:[{id:"u",role:"user",kind:"text",body:{text:"給我驗證報告"}},
        {id:"a",role:"agent",kind:"text",body:{text:"報告已完成：[Bridge 上線檢查](<"+ref+":12>)", attachments:[
          {filename:"knowledge.md",download_url:"/app/v2/artifacts/"+second,kind:"text",available:true} ]}}]}); return true;
    }
    if (!url.pathname.endsWith("/reports")) return false;
    calls.push({path:url.pathname,media:url.searchParams.get("media_id"),auth:req.headers.authorization});
    if (control.status !== 200) {res.writeHead(control.status);res.end("{}");return true;}
    if (!url.searchParams.has("media_id")) {
      json({items:[{media_id:mid,filename:"report.md",title:"Bridge 上線檢查",refs:[ref]},
        {media_id:second,filename:"knowledge.md",title:"知識庫研究",refs:["/app/v2/artifacts/"+second]}]});return true;
    }
    const finish = () => {
      res.writeHead(200, {"Content-Type":"text/plain", ...(control.chunked ? {} : {"Content-Length":Buffer.byteLength(control.payload)})});
      if (control.chunked) {
        const bytes=Buffer.from(control.payload);
        res.write(bytes.subarray(0,100));res.end(bytes.subarray(100));
      }
      else res.end(control.payload);
    };
    if (control.delay) control.pending = finish; else finish();
    return true;
  }});
  await fixture.page.waitForFunction(()=>state.sessions.length > 0);
  await fixture.page.evaluate(()=>openSession("codex:a"));
  await fixture.page.locator("#cards .report-link").first().waitFor();
  return {...fixture,calls,control};
}

test("report links render authenticated Markdown, source, TOC, download and keyboard isolation", async t => {
  const {page,calls,model,control} = await setup(t);
  control.chunked=true;
  await page.locator("#cards .report-link").first().click();
  await page.locator("#report-content table").waitFor();
  assert.equal(await page.locator("#report-title").textContent(), "Bridge 上線檢查");
  assert.equal(await page.locator("#report-toc button").count(), 3);
  assert.equal(await page.evaluate(()=>window.reportXSS), undefined);
  await page.keyboard.press("j");
  assert.equal(await page.evaluate(()=>state.current), "codex:a");
  await page.locator("#report-source").click();
  assert.equal(await page.locator(".report-raw").textContent(), report);
  await page.locator("#report-preview").click();
  const download = page.waitForEvent("download");
  await page.locator("#report-download").click();
  assert.equal((await download).suggestedFilename(), "report.md");
  assert.equal(calls.filter(c=>c.media === mid).length, 2, "download revalidates permission instead of reusing cached bytes");
  await page.keyboard.press("Escape");
  await page.locator("#report-reader").waitFor({state:"hidden"});
  assert.equal(await page.locator("#report-content").textContent(), "");
  await page.locator("#cards .report-link").nth(1).click();
  await page.waitForFunction(()=>document.querySelector("#report-title").textContent === "知識庫研究");
  assert.ok(calls.every(c=>c.auth === "Bearer synthetic-owner"));
  assert.equal(model.posts.length, 0);
  await page.evaluate(()=>showHome());
  await page.locator("#report-reader").waitFor({state:"hidden"});
});

test("reader stays within desktop, tablet and small phone viewports", async t => {
  const {page} = await setup(t);
  await page.locator("#cards .report-link").first().click();
  await page.locator("#report-content table").waitFor();
  const dir = process.env.POCKET_QA_SCREENSHOTS || "/tmp/pocket-report-qa";
  await fs.mkdir(dir,{recursive:true});
  for (const [width,height] of [[1366,768],[768,1024],[390,844],[320,568]]) {
    await page.setViewportSize({width,height});
    const box = await page.locator("#report-reader").boundingBox();
    assert.ok(box.x >= -1 && box.y >= -1 && box.x+box.width <= width+1 && box.y+box.height <= height+1);
    assert.equal(await page.locator("#report-content").evaluate(n=>n.scrollWidth <= n.clientWidth+1), true);
    for (const selector of ["#report-close","#report-download","#report-source"]) {
      const r = await page.locator(selector).boundingBox(); assert.ok(r.x >= 0 && r.x+r.width <= width+1);
    }
    await page.screenshot({path:path.join(dir,"report-"+width+".png")});
  }
  await page.evaluate(()=>applyTheme("dark"));
  await page.screenshot({path:path.join(dir,"report-dark.png")});
});

test("missing, oversized, late and revoked reports fail without leaking or stale content", async t => {
  const {page,control} = await setup(t);
  control.status = 404;
  await page.locator("#session-reports").click();
  await page.waitForFunction(()=>document.querySelector("#report-status").textContent.includes("未封存"));
  await page.locator("#report-close").click();
  control.status=200; control.payload="x".repeat(512*1024+1);
  for (const chunked of [false,true]) {
    control.chunked=chunked;
    await page.locator("#cards .report-link").first().click();
    await page.waitForFunction(()=>document.querySelector("#report-status").textContent.includes("預覽上限"));
    assert.equal(await page.locator("#report-content").textContent(), "");
    await page.locator("#report-close").click();
  }
  control.payload=report; control.delay=true;
  await page.locator("#cards .report-link").first().click();
  for(let i=0;i<100 && !control.pending;i++) await new Promise(r=>setTimeout(r,20));
  assert.ok(control.pending);
  await page.locator("#report-close").click(); control.pending();
  assert.equal(await page.locator("#report-content").textContent(), "");
  control.delay=false; control.status=401;
  await page.locator("#session-reports").click();
  await page.waitForURL(/signin/);
  assert.equal(await page.locator("#report-content").count(), 0);
  assert.equal(await page.evaluate(()=>localStorage.getItem("pocket.console.token")), null);
});
