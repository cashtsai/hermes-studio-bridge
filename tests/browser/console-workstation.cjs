const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");

test("personal resume is opt-in, login bound, subordinate to a deep link and unavailable when borrowed", async t => {
  const {page,origin,model} = await workstation(t);
  const home = page.locator("#personal-work"), pref = page.locator("#remember-work");
  const launch = () => page.goto(origin+"/console");
  const pick = id => page.locator('#session-list [data-id="codex:'+id+'"]').click();
  await home.waitFor();
  assert.equal(await pref.isChecked(),false);
  await pick("a"); await launch(); await home.waitFor();
  assert.equal(await page.evaluate(() => localStorage.getItem("pocket.console.personal-work")),null);
  await pref.check(); await pick("b"); await launch();
  await page.locator("#sh-name").getByText("codex:b").waitFor();
  await page.goto(origin+"/console#session=codex%3Aa"); await page.reload();
  await page.locator("#sh-name").getByText("codex:a").waitFor();
  await page.locator("#back-home").click(); await page.locator("#home-refresh").click(); await home.waitFor();
  await pref.uncheck(); await launch(); await home.waitFor();
  await pref.check(); await pick("b"); model.jobs.pop();
  await launch(); await home.waitFor(); // Deleted job cannot be reopened.
  await pick("a");
  await page.evaluate(() => localStorage.setItem("pocket.console.token","different-owner"));
  await launch(); await home.waitFor();
  assert.equal(await pref.isChecked(),false);
  await pref.check(); await pick("a"); model.remote=true;
  await launch(); await page.waitForSelector('body[data-console-access="remote"]');
  assert.ok(await home.isHidden());
  assert.equal(await page.evaluate(() => localStorage.getItem("pocket.console.personal-work")),null);
  assert.equal(new URL(page.url()).hash,"");
  assert.deepEqual(await page.evaluate(() => [localStorage.getItem("pocket.console.token"),
    sessionStorage.getItem("pocket.console.token")]),[null,"different-owner"]);
});

for (const mode of ["browser","standalone"]) test("1366x768 "+mode+": work, latest reply, IME and changes",
  {skip:mode==="standalone" && !process.env.POCKET_QA_INSTALL_PWA}, async t => {
  const {context,origin,model,page:tab} = await workstation(t, mode==="standalone");
  let page=tab;
  const manifestId=origin+"/console";
  const cdp=await context.newCDPSession(tab);
  let installed=false;
  try {
    if (mode==="standalone") {
      await cdp.send("PWA.install",{manifestId});
      installed=true;
      await cdp.send("PWA.changeAppUserSettings",{manifestId,displayMode:"standalone"});
      const opened=context.waitForEvent("page");
      await cdp.send("PWA.launch",{manifestId});
      page=await opened;
    }
    await page.locator("#personal-work").waitFor();
    assert.equal(await page.evaluate(m=>matchMedia("(display-mode: "+m+")").matches,mode),true);
    await page.keyboard.press("Control+k");
    await page.locator("#pal-q").fill("codex:b");
    await page.keyboard.press("Enter");
    const last=page.locator("#cards").getByText("測試訊息 79",{exact:true});
    await last.waitFor();
    const input=page.locator("#input");
    await input.fill("第一行"); await input.press("Shift+Enter"); await page.keyboard.insertText("第二行");
    const ime=await context.newCDPSession(page);
    await ime.send("Input.imeSetComposition",{text:"選字",selectionStart:2,selectionEnd:2});
    await input.dispatchEvent("keydown",{key:"Enter",isComposing:true}); await input.press("Control+k");
    await ime.send("Input.insertText",{text:"選字"});
    assert.ok(await page.locator("#palette").isHidden());
    await input.dispatchEvent("keydown",{key:"Enter",ctrlKey:true,keyCode:229});
    assert.equal(await input.inputValue(),"第一行\n第二行選字");
    assert.equal(model.posts.length,0);
    // Include clipping by scrolling ancestors, not only the viewport rectangle.
    for (const item of [last,input,page.locator('#session-list [data-id="codex:b"]'),page.locator("#rail-reload")]) {
      assert.ok(await item.evaluate(el => {
        const b=el.getBoundingClientRect();
        return [0.15,0.85].every(y=>el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height*y)));
      }),"workbench item clipped: "+item);
    }
    if (process.env.POCKET_QA_SCREENSHOTS) {
      await fs.mkdir(process.env.POCKET_QA_SCREENSHOTS,{recursive:true});
      await page.screenshot({path:path.join(process.env.POCKET_QA_SCREENSHOTS,"workstation-"+mode+".png")});
    }
    const sent=page.waitForResponse(r=>r.url().endsWith("/input"));
    await input.press("Enter"); await sent;
    assert.deepEqual(model.posts.map(p=>p.content),["第一行\n第二行選字"]);
    await page.locator("#rail-list").getByText("sample.txt").click();
    await page.locator("#ch-diff").getByText("+sample",{exact:true}).waitFor();
  } finally {
    if (installed) await cdp.send("PWA.uninstall",{manifestId});
  }
});
