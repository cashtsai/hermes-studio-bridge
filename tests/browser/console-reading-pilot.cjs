// Manual visual review, not a release gate. Output contains synthetic work only.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");

const title = "手機主線：核對昨天的部署證據與尚待確認的中文輸入問題";
const id = "codex:qa-cx";
const base = "/app/v2/sessions/" + encodeURIComponent(id);
const output = process.env.POCKET_QA_SCREENSHOTS;
const viewports = [{width:1366,height:768},{width:820,height:1180},{width:390,height:844}];

// A label can exist and still be under the rail, keyboard area or a clipped ancestor.
function onScreen(el) {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && [0.15,0.85].every(y =>
    el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + y * r.height)));
}

for (const viewport of viewports) test("Pocket CX reading / " + viewport.width, {skip:process.env.POCKET_QA_READING_PILOT !== "1"}, async t => {
  const {page,model,context} = await workstation(t,false,{viewport,isolated:true,jobs:[
    {id,title,provider:"codex",status:"running",last_event_at:1794209400},
    {id:"cc2:qa",title:"手機主線：整理上一輪部署",provider:"cc2",status:"idle",last_event_at:1794208800}
  ]});
  const checks = [];
  const check = (name,pass,actual) => checks.push({name,pass,actual});
  const shot = async name => {
    if (!output) return;
    await fs.mkdir(output,{recursive:true});
    await page.screenshot({path:path.join(output,viewport.width+"-"+name+".png")});
  };
  page.setDefaultTimeout(5000);
  const row = page.locator('#recent-list .work-row').first();
  await row.waitFor();
  check("first recent job is identifiable and clickable",await row.getAttribute("data-id") === id &&
    await row.evaluate(onScreen),await row.innerText());
  await shot("recent");
  await row.click();
  await page.locator("#sh-name").getByText(title,{exact:true}).waitFor();
  check("resume via one selection",true,"click on first recent job");
  const cards = page.locator("#cards"), input = page.locator("#input");
  await cards.getByText("測試訊息 79",{exact:true}).waitFor();
  check("no document overflow",await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),viewport.width);
  check("composer exposed",await input.evaluate(onScreen),await input.boundingBox());
  const send = page.locator("#send");
  check("send exposed",await send.evaluate(onScreen),await send.boundingBox());
  let seq=80;
  for (const distance of [320,48]) {
    await cards.evaluate(el => { el.scrollTop = el.scrollHeight; });
    await cards.hover();
    await page.mouse.wheel(0,-distance);
    await page.waitForFunction(n => {
      const el=document.querySelector("#cards");
      return Math.abs(el.scrollHeight-el.clientHeight-el.scrollTop-n)<3;
    },distance);
    const anchor = await cards.evaluate(el => {
      const y=el.getBoundingClientRect().top;
      const node=Array.from(el.children).find(n => n.getBoundingClientRect().bottom > y+8);
      return {key:node.dataset.key,y:node.getBoundingClientRect().top};
    });
    await shot("read-"+distance+"-before");
    const stream=model.streams.get(base+"/events");
    assert.ok(stream,"fixture session stream is connected");
    ++seq;
    const text="合成串流回覆 "+seq;
    const card={id:"qa-"+seq,ts:seq,role:"agent",turn_id:"qa-turn-"+seq,kind:"text",body:{text}};
    stream.write("data: "+JSON.stringify({type:"card.upsert",seq,data:{card}})+"\n\n");
    await cards.getByText(text,{exact:true}).waitFor({state:"attached"});
    const y=await cards.locator('[data-key="'+anchor.key+'"]').evaluate(el => el.getBoundingClientRect().top);
    const drift=Math.round((y-anchor.y)*10)/10;
    check(distance+"px scroll: reader holds position",Math.abs(drift)<=8,{driftPx:drift});
    await shot("read-"+distance+"-after");
  }

  const notice=page.locator("#delivery-text");
  async function delivery(name,label,expectedPosts) {
    await page.waitForFunction(value => document.querySelector("#delivery-text").textContent === value,label);
    check(name,await notice.evaluate(onScreen) && (expectedPosts === undefined || model.posts.length === expectedPosts),
      {label,posts:model.posts.length});
    await shot(name);
  }
  await input.fill("合成訊息：只送給本機測試主機");
  await context.setOffline(true);
  await send.click();
  await delivery("offline","目前離線，訊息尚未送出",0);
  await context.setOffline(false);
  model.onInput = res => {res.writeHead(200,{"Content-Type":"application/json"});res.end('{"ok":true,"queued":true}');};
  await send.click();
  await delivery("queued","已排入佇列",1);
  delete model.onInput;
  await input.fill("測試：已收到"); await send.click();
  await delivery("accepted","主機已收到",2);
  const beforeLoss=model.posts.length;
  model.onInput = res => res.destroy();
  await input.fill("測試：收據中斷"); await send.click();
  await delivery("unknown","送達狀態不明，請先確認。未重新送出。");
  const lossPosts=model.posts.slice(beforeLoss);
  const ids=new Set(lossPosts.map(p => p.client_id));
  check("one logical send after lost receipt",ids.size===1 && lossPosts.every(p => p.client_id && p.content==="測試：收據中斷"),
    {httpPosts:lossPosts.length,distinctClientIds:ids.size});
  const beforeQuery=model.posts.length;
  const receiptRead=page.waitForRequest(req => req.method()==="GET" && new URL(req.url()).pathname===base+"/cards");
  await page.locator("#check-delivery").click();
  await receiptRead;
  await delivery("no-resend","目前未找到收據，請核對原對話。未重新送出。",beforeQuery);
  const report={viewport,chrome:await page.evaluate(()=>navigator.userAgent),checks};
  if (output) await fs.writeFile(path.join(output,viewport.width+".json"),JSON.stringify(report,null,2)+"\n");
  const failed=checks.filter(x=>!x.pass);
  assert.deepEqual(failed,[],JSON.stringify(failed));
});
