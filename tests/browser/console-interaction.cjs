const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");
const {execFileSync} = require("node:child_process");
const output = process.env.POCKET_QA_SCREENSHOTS || "/tmp/pocket-interaction-qa";
const jobs = [
  {id:"hermes:fixture",provider:"hermes",title:"設計檢查",status:"idle"},
  {id:"cc:fixture",provider:"claude_code",title:"等待確認",status:"waiting_approval"},
  {id:"codex:fixture",provider:"codex",title:"Console 元件更新",status:"running"},
  {id:"gemini:fixture",provider:"gemini",title:"文件整理",status:"completed"}
];

test("interaction layer: themes, small screens, focus, press, disabled and motion", async t => {
  const {page,model} = await workstation(t,false,{jobs,isolated:true,historySize:12});
  const errors=[];
  page.on("pageerror", e=>errors.push(e.message));
  await page.locator('.sess[data-id="codex:fixture"]').click();
  await page.locator("#input").fill("檢查按鈕與狀態顯示");
  await page.locator("#workbar").waitFor({state:"visible"});
  await fs.mkdir(output,{recursive:true});
  for (const theme of ["pocket","white","dark"]) {
    await page.evaluate(t=>applyTheme(t),theme);
    for (const [width,height] of [[1366,900],[390,844],[320,740]]) {
      await page.setViewportSize({width,height});
      const metrics = await page.evaluate(()=>({
        viewport:innerWidth,body:document.body.scrollWidth,
        composer:document.querySelector(".composer").getBoundingClientRect().right,
        send:document.querySelector("#send").getBoundingClientRect().height,
        shadow:getComputedStyle(document.querySelector("#send")).boxShadow,
        textFont:getComputedStyle(document.querySelector("#input")).fontFamily,
      }));
      assert.ok(metrics.body <= width && metrics.composer <= width+1,JSON.stringify(metrics));
      assert.ok(metrics.send >=44);
      assert.notEqual(metrics.shadow,"none");
      assert.doesNotMatch(metrics.textFont,/JetBrains/);
      await page.screenshot({path:path.join(output,`console-${theme}-${width}.png`)});
    }
  }
  await page.setViewportSize({width:1366,height:900});
  const button=page.locator("#send");
  await button.hover();
  await page.waitForFunction(()=>getComputedStyle(document.querySelector("#send")).transform==="matrix(1, 0, 0, 1, 0, -1)");
  await page.mouse.down();
  await page.waitForFunction(()=>getComputedStyle(document.querySelector("#send")).boxShadow==="none");
  await page.mouse.move(5,5);
  await page.mouse.up();
  assert.equal(model.posts.length,0,"press check never submits a prompt");
  await button.evaluate(n=>n.disabled=true);
  assert.equal(await button.evaluate(n=>getComputedStyle(n).boxShadow),"none");
  await button.evaluate(n=>n.disabled=false);
  await page.emulateMedia({reducedMotion:"reduce"});
  await button.hover();
  assert.equal(await button.evaluate(n=>getComputedStyle(n).transform),"none");
  assert.equal(await page.locator(".workbar-spin").evaluate(n=>getComputedStyle(n,"::before").animationName),"none");
  await page.locator("#input").focus();
  assert.notEqual(await page.locator("#input").evaluate(n=>getComputedStyle(n).outlineStyle),"none");
  assert.deepEqual(errors,[]);
});

test("self-hosted mono assets load without changing body font or late swapping", async t => {
  // Parse the production header literal without importing/starting the bridge.
  const csp=execFileSync("python3",["-c",`
import ast, pathlib
tree=ast.parse(pathlib.Path("bridge.py").read_text())
node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(
    isinstance(t,ast.Name) and t.id=="_CONSOLE_SECURITY_HEADERS" for t in n.targets))
print(ast.literal_eval(node.value)["Content-Security-Policy"])
  `],{cwd:path.resolve(__dirname,"../.."),encoding:"utf8"}).trim();
  assert.match(csp,/font-src 'self';/);
  const {page} = await workstation(t,false,{isolated:true,headers:{"Content-Security-Policy":csp}});
  const loaded=await page.evaluate(async()=>{
    await document.fonts.load('13px "JetBrains Mono"');
    const faces=[...document.fonts].filter(f=>f.family==="JetBrains Mono");
    return {loaded:document.fonts.check('13px "JetBrains Mono"'),display:faces.map(f=>f.display),
      faces:faces.length,mono:getComputedStyle(document.documentElement).getPropertyValue("--mono"),
      body:getComputedStyle(document.body).fontFamily};
  });
  assert.ok(loaded.loaded);
  assert.equal(loaded.faces,2);
  assert.deepEqual(loaded.display,["optional","optional"]);
  assert.match(loaded.mono,/JetBrains Mono/);
  assert.match(loaded.mono,/Microsoft JhengHei/);
  assert.doesNotMatch(loaded.body,/JetBrains/);
});
