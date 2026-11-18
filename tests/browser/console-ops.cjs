// Production assets + synthetic API only. Never connects to a real bridge/provider.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {workstation} = require("./fixtures/workstation.cjs");
const caps = ["input","attachments","replay","follow","approve"];
const providers = {
  codex:{ops:["archive","history","config_read","config_write"], compact:true,read_cursor:false,
    config_fields:{model:{editable:true},approval_policy:{editable:true,options:["untrusted","on-request","never"]},
      effort:{editable:false},sandbox:{editable:false}}},
  cc2:{ops:["rename","archive","history","config_write"],compact:true,read_cursor:false,
    config_fields:{model:{editable:true},permission_mode:{editable:true,options:["default","plan"]}}},
  openclaw:{ops:[],compact:false,read_cursor:false},
  hermes:{ops:[],compact:false,read_cursor:true},
  custom:{ops:["rename","archive","history","config_read","config_write"],compact:true,read_cursor:false,
    config_fields:{model:{editable:true},mode:{editable:true}}},
};
const jobs = () => [
  {id:"codex:alpha",title:"CX 工作台",provider:"codex"},
  {id:"cc2:beta",title:"CC 主線",provider:"cc2",meta:{model:"sonnet",permission_mode:"default"}},
  {id:"hermes:reader",title:"袁方",provider:"hermes"},
  {id:"openclaw:mirror",title:"唯讀鏡像",provider:"openclaw"},
  {id:"custom:gamma",title:"新供應者",provider:"custom"},
].map(j=>({...j,status:"idle",capabilities:caps}));
function approval(id, session="codex:alpha", over={}) {
  return {id,session_id:session,provider:session.split(":")[0],status:"pending",response_capability:"live",
    title:"確認部署 "+id,detail:"變更兩份測試設定，不會影響 production。",risk:"high",kind:"permission",
    expires_at:Date.now()/1000+3600,options:[{key:"yes",label:"允許一次",style:"primary"},
      {key:"no",label:"拒絕",style:"danger"}],...over};
}
async function fixture(t, options={}) {
  const data={calls:[], approvals:[approval("a1"),approval("a2"),approval("b1","cc2:beta")],
    config:{model:"gpt-fixture",approval_policy:"on-request",effort:"high",sandbox:"workspace-write"},
    cards:Array.from({length:60},(_,i)=>({id:"msg"+i,ts:1700000000+i,role:i%2 ? "agent":"user",
      kind:"text",final:true,body:{text:"驗收訊息 "+i}})),...options.data};
  const handle=async(req,res,url,model)=>{
    const json=(body,status=200)=>{res.writeHead(status,{"Content-Type":"application/json"});res.end(JSON.stringify(body));return true;};
    const p=decodeURIComponent(url.pathname);
    let body;
    if (req.method==="POST" && !p.endsWith("/input")) {let raw="";for await(const c of req)raw+=c;body=raw ? JSON.parse(raw):{};}
    data.calls.push({path:p,method:req.method,query:Object.fromEntries(url.searchParams),body});
    if(p==="/app/v2/providers/capabilities") return json({providers});
    if(p==="/app/v1/approvals") {
      if(data.hubFail) return json({detail:"hub 暫時離線"},503);
      const offset=Number(url.searchParams.get("offset")||0), size=data.hubPageSize||50;
      return json({approvals:data.approvals.slice(offset,offset+size),total:data.approvals.length,
        next_offset:offset+size<data.approvals.length ? offset+size:null});
    }
    if(p.endsWith("/cards")) {
      if(url.searchParams.has("before_id")) return json({detail:"ring expired"},409);
      return json({latest_seq:999999,cards:data.cards,older_cursor:data.ringCursor||null});
    }
    if(p==="/app/v2/read") return json({ok:true});
    if(p.startsWith("/app/v1/approvals/") && p.endsWith("/decision")) {
      const a=data.approvals.find(a=>a.id===p.split("/").at(-2));
      if(data.questionNext && a.q_index===0) {
        a.q_index=1;a.title="第二題";a.options=[{key:"second",label:"第二個答案",style:"primary"}];
        return json({status:"next_question",q_index:1,q_total:2});
      }
      data.approvals=data.approvals.filter(a=>a.id!==p.split("/").at(-2)); return json({status:"approved"});
    }
    const match=p.match(/^\/app\/v2\/sessions\/(.+)\/(rename|archive|config|compact|history)$/);
    if(!match) {
      if(p.startsWith("/ccsessions/") && p.endsWith("/compress")) return json({ok:true});
      return false;
    }
    const [,id,op]=match;
    if(op==="rename") {
      const s=model.jobs.find(s=>s.id===id);s.title=body.name;
      if(data.renameMoves) {s.id="cc2:"+body.name;return json({session_id:s.id});}
      return json({title:body.name});
    }
    if(op==="archive") {
      if(!body.archived && data.undoFail) return json({detail:"復原暫時失敗"},503);
      if(body.archived) {data.archived=model.jobs.find(s=>s.id===id);model.jobs=model.jobs.filter(s=>s.id!==id);}
      else model.jobs.push(data.archived);
      return json({ok:true});
    }
    if(op==="config") {
      if(req.method==="POST") {
        if(data.configFail) return json({error:{code:"CX_UNKNOWN_MODEL",message:"未知模型"}},400);
        Object.assign(data.config,body);return json({ok:true,applied:Object.keys(body)});
      }
      return json(data.config);
    }
    if(op==="compact") return data.compressRedirect
      ? json({error:{code:"USE_CC_COMPRESS",message:"use compress"}},409):json({ok:true});
    if(op==="history") {
      if(data.historyFail) return json({detail:"逐字稿暫時離線"},503);
      if(data.historyReply) return json(data.historyReply(url));
      const earlier=url.searchParams.get("cursor")==="page2";
      return json({text:earlier ? "最早的紀錄\n保留完整逐字稿" : "最近的逐字稿\n另一頁仍可讀取",
        more:!earlier,nextCursor:earlier ? null:"page2"});
    }
    return false;
  };
  const result=await workstation(t,false,{isolated:true,viewport:{width:1200,height:900},jobs:jobs(),handle,...options});
  const errors=[];result.page.on("pageerror",e=>errors.push(e.message));
  t.after(()=>assert.deepEqual(errors,[]));
  result.page.setDefaultTimeout(6000);
  await result.page.waitForFunction(()=>document.getElementById("awaiting-n").textContent==="3");
  return {...result,data};
}
const select=(page,id)=>page.locator('#session-list .sess[data-id="'+id+'"]').click();
const menu=(page,id)=>page.locator('[data-session-ops="'+id+'"]').click();
const action=(page,label)=>page.locator("#op-content").getByRole("button",{name:label,exact:true}).click();
const posts=(data,op)=>data.calls.filter(c=>c.method==="POST" && c.path.endsWith(op));
async function shot(page,name) {
  const dir=process.env.POCKET_QA_SCREENSHOTS;if(!dir)return;
  await fs.mkdir(dir,{recursive:true});await page.screenshot({path:path.join(dir,name+".png")});
}

test("capability menus, rename identity, archive undo and retry",async t=>{
  const {page,data}=await fixture(t,{data:{renameMoves:true}});
  assert.equal(await page.locator(".session-row > button.sess").count(),jobs().length);
  assert.equal(await page.locator("button.sess button").count(),0,"menu must be a sibling, never a nested button");
  await select(page,"cc2:beta");await page.locator("#input").fill("尚未送出的草稿");
  await menu(page,"cc2:beta");await shot(page,"menu-supported");
  await action(page,"重新命名");await page.locator("#op-content input").fill("new-name");await action(page,"儲存");
  await page.locator('#session-list .sess[data-id="cc2:new-name"]').waitFor();
  await page.waitForFunction(()=>document.getElementById("input").value==="尚未送出的草稿");
  assert.ok(page.url().includes("cc2%3Anew-name"));
  await menu(page,"codex:alpha");
  assert.equal(await page.locator("#op-content").getByRole("button",{name:"重新命名",exact:true}).count(),0);
  await shot(page,"menu-codex-limited");await page.locator("#op-close").click();
  assert.equal(await page.locator('[data-session-ops="openclaw:mirror"]').count(),0);
  await select(page,"openclaw:mirror");
  for(const id of ["session-settings","session-more","session-compact","earlier-cards"])
    assert.ok(await page.locator("#"+id).isHidden(),id);
  await shot(page,"menu-unsupported");
  await menu(page,"cc2:new-name");await action(page,"封存");
  assert.equal(posts(data,"/archive").length,0);
  await action(page,"確認封存");await page.locator("#ops-notices").getByRole("button",{name:"取消封存",exact:true}).waitFor();
  assert.equal(await page.locator('#session-list .sess[data-id="cc2:new-name"]').count(),0);
  data.undoFail=true;
  await page.locator("#ops-notices").getByRole("button",{name:"取消封存",exact:true}).click();
  await page.locator("#ops-notices").getByText("操作未完成：復原暫時失敗").waitFor();data.undoFail=false;
  await page.locator("#ops-notices").getByRole("button",{name:"取消封存",exact:true}).click();
  await page.locator('#session-list .sess[data-id="cc2:new-name"]').waitFor();
  assert.deepEqual(posts(data,"/archive").map(c=>c.body.archived),[true,false,false]);
});

test("config schema, explicit write errors, custom provider options and compact fallback",async t=>{
  const {page,data}=await fixture(t);
  await select(page,"codex:alpha");await page.locator("#session-settings").click();
  const model=page.locator('[name="model"]');await model.waitFor();
  assert.ok(await page.locator('[name="effort"]').isDisabled());assert.ok(await page.locator('[name="sandbox"]').isDisabled());
  await shot(page,"config-codex");
  await model.fill("bad-model");data.configFail=true;await action(page,"儲存");
  await page.locator("#op-status").getByText("設定未更新：未知模型").waitFor();
  data.configFail=false;await model.fill("gpt-new");await page.locator('[name="approval_policy"]').selectOption("untrusted");
  await action(page,"儲存");await page.locator("#op-status").getByText("設定已更新").waitFor();
  assert.deepEqual(posts(data,"/config").at(-1).body,{model:"gpt-new",approval_policy:"untrusted"});
  await page.locator("#op-close").click();
  await select(page,"cc2:beta");await page.locator("#session-settings").click();
  assert.equal(await page.locator('[name="model"]').inputValue(),"sonnet");
  assert.equal(data.calls.filter(c=>c.method==="GET" && c.path==="/app/v2/sessions/cc2:beta/config").length,0);
  await page.locator("#op-close").click();
  data.config={model:"m1",mode:"plan",model_options:[{value:"m1",name:"Model One"},{value:"m2",name:"Model Two"}],
    mode_options:[{value:"plan",name:"Plan"},{value:"build",name:"Build"}]};
  await select(page,"custom:gamma");await page.locator("#session-settings").click();
  await page.locator('select[name="model"]').selectOption("m2");await page.locator('select[name="mode"]').selectOption("build");
  await action(page,"儲存");await page.locator("#op-status").getByText("設定已更新").waitFor();
  assert.deepEqual(posts(data,"/config").at(-1).body,{model:"m2",mode:"build"});
  await page.locator("#op-close").click();
  data.compressRedirect=true;await page.locator("#session-compact").click();await action(page,"開始壓縮");
  await page.locator("#ops-notices").getByText("已送出壓縮請求，完成狀態以主機回報為準").waitFor();
  assert.equal(posts(data,"/compress")[0].path,"/ccsessions/gamma/compress");
});

test("ring 409 falls back to paged transcript without replacing live cards or seq",async t=>{
  const {page,data}=await fixture(t,{data:{ringCursor:"expired-card"}});
  await select(page,"codex:alpha");await page.locator("#cards").getByText("驗收訊息 59",{exact:true}).waitFor();
  await page.locator("#earlier-cards").click();await page.locator(".op-history").getByText(/最近的逐字稿/).waitFor();
  assert.equal(await page.evaluate(()=>state.latestSeq),999999);
  assert.equal(await page.evaluate(()=>state.cards.length),60);
  await shot(page,"history-fallback");data.historyFail=true;
  await page.locator("#history-more").click();await page.locator("#op-status").getByText("歷史讀取失敗：逐字稿暫時離線").waitFor();
  data.historyFail=false;await page.locator("#history-more").click();
  await page.locator("#op-status").getByText("已到最早紀錄").waitFor();await shot(page,"history-earlier");
  const text=await page.locator(".op-history").textContent();
  assert.ok(text.indexOf("最早的紀錄")<text.indexOf("最近的逐字稿"));
  const calls=data.calls.filter(c=>c.path.endsWith("/history"));
  assert.deepEqual(calls.map(c=>c.query.cursor),[undefined,"page2","page2"]);
  assert.equal(await page.evaluate(()=>state.cards.length),60);
  await page.locator("#op-close").click();await page.locator("#earlier-cards").getByText("更早訊息（逐字稿）").waitFor();
});

test("transcript offset pages deduplicate IDs and invalid cursors are retryable without mutation",async t=>{
  const {page,data}=await fixture(t);
  data.historyReply=url=>Number(url.searchParams.get("offset"))===0
    ? {messages:[{id:"old2",role:"agent",content:"第二筆"}],more:true}
    : {messages:[{id:"old1",role:"user",content:"第一筆"},{id:"old2",role:"agent",content:"第二筆"}],more:false};
  await select(page,"custom:gamma");await page.locator("#earlier-cards").click();
  await page.locator(".op-history").getByText("第二筆").waitFor();await page.locator("#history-more").click();
  await page.locator("#op-status").getByText("已到最早紀錄").waitFor();
  assert.equal(await page.locator(".op-history article").count(),2);
  assert.deepEqual(data.calls.filter(c=>c.path.endsWith("/history")).map(c=>c.query.offset),["0","48"]);
  await page.locator("#op-close").click();
  data.historyReply=()=>({text:"不可重複",more:true,nextCursor:null});
  await page.locator("#earlier-cards").click();await page.locator("#op-status").getByText(/有效的下一頁/).waitFor();
  assert.equal(await page.locator(".op-history").textContent(),"");
  data.historyReply=()=>({text:"不可重複",more:false,nextCursor:null});
  await page.locator("#history-more").click();await page.locator("#op-status").getByText("已到最早紀錄").waitFor();
  assert.equal(await page.locator(".op-history pre").count(),1);
  assert.equal(data.calls.filter(c=>c.path.endsWith("/history")).at(-1).query.offset,"0");
});

test("hub has multiple approvals per session, reuses P0 detail/risk, writes selected ID and reacts to resolution",async t=>{
  const {page,data,model}=await fixture(t,{data:{hubPageSize:2}});
  await page.locator("#awaiting").click();await page.locator('#hub-list [data-approval-id="a2"]').waitFor();
  assert.equal(await page.locator("#awaiting-n").textContent(),"3");
  await page.locator("#hub-more").click();await page.locator('#hub-list [data-approval-id="b1"]').waitFor();
  assert.equal(await page.locator("#hub-list .hub-row").count(),3);await shot(page,"hub-multiple");
  await page.locator('#hub-list [data-approval-id="a2"]').click();
  await page.locator("#ap-title").getByText("確認部署 a2").waitFor();
  assert.equal(await page.locator("#ap-risk").textContent(),"高風險");
  assert.match(await page.locator("#ap-detail").textContent(),/兩份測試設定/);
  assert.ok(await page.locator("#ap-actions .ap-danger").isVisible());await shot(page,"hub-selected");
  await page.locator("#ap-actions .ap-yes").click();
  await page.waitForFunction(()=>document.getElementById("awaiting-n").textContent==="2");
  assert.equal(posts(data,"/decision")[0].path,"/app/v1/approvals/a2/decision");
  assert.equal(posts(data,"/decision")[0].body.key,"yes");
  await page.locator('#hub-list [data-approval-id="a1"]').click();
  data.approvals=data.approvals.filter(a=>a.id!=="a1");
  const stream=model.streams.get("/app/v2/events");assert.ok(stream);
  stream.write("data: "+JSON.stringify({seq:1,type:"approval.resolved",data:{approval_id:"a1"}})+"\n\n");
  await page.waitForFunction(()=>document.getElementById("approval").classList.contains("hidden"));
  await page.waitForFunction(()=>document.getElementById("awaiting-n").textContent==="1");
  data.hubFail=true;await page.locator("#hub-refresh").click();
  await page.locator("#hub-status").getByText("待審清單讀取失敗：hub 暫時離線").waitFor();
  assert.equal(await page.locator("#hub-list .hub-row").count(),1);
  data.hubFail=false;data.approvals[0].response_capability="not_resumable";
  await page.locator("#hub-refresh").click();await page.locator('#hub-list [data-approval-id="b1"]').click();
  await page.locator("#ap-hint").getByText("主機已無等待中的請求，無法作答").waitFor();
  assert.ok(await page.locator("#ap-actions .ap-yes").isDisabled());
});

test("read cursor only at visible bottom, deduplicates render/focus and uses message time, not ring seq",async t=>{
  const {page,data}=await fixture(t);
  async function received(count) {
    // Wait for the HTTP receipt, not a 150ms scheduling margin over debounce.
    for (let n=0; n<100 && posts(data,"/app/v2/read").length<count; n++) await page.waitForTimeout(30);
    assert.equal(posts(data,"/app/v2/read").length,count);
  }
  await select(page,"hermes:reader");
  await page.waitForFunction(()=>document.getElementById("cards").textContent.includes("驗收訊息 59"));
  await received(1);
  const first=posts(data,"/app/v2/read")[0].body;
  assert.equal(first.session,"reader");assert.equal(first.last_read_ts,1700000059);
  assert.equal(first.last_read_seq,undefined);assert.equal(first.message_id,"msg59");assert.ok(first.device_id);
  await page.evaluate(()=>{renderCards();window.dispatchEvent(new Event("focus"));});await page.waitForTimeout(550);
  assert.equal(posts(data,"/app/v2/read").length,1);
  await page.locator("#cards").evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event("scroll"));});
  await page.evaluate(()=>{state.cards.push({id:"later",ts:1700000100,kind:"text",role:"agent",body:{text:"新訊息"}});renderCards();});
  await page.waitForTimeout(550);assert.equal(posts(data,"/app/v2/read").length,1);
  await page.locator("#cards").evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event("scroll"));});
  await received(2);
  assert.equal(posts(data,"/app/v2/read")[1].body.last_read_ts,1700000100);
  await page.evaluate(()=>{
    PocketSessionOps.onEvent({type:"read_cursor.update",session:"reader",data:{global:{last_read_ts:1700000200}}});
    state.cards.push({id:"another-device-read",ts:1700000200,kind:"text",body:{text:"已在手機讀過"}});renderCards();
  });await page.waitForTimeout(550);assert.equal(posts(data,"/app/v2/read").length,2);
  await page.evaluate(()=>{
    Object.defineProperty(document,"hidden",{configurable:true,value:true});
    state.cards.push({id:"while-hidden",ts:1700000300,kind:"text",body:{text:"背景訊息"}});renderCards();
  });await page.waitForTimeout(550);assert.equal(posts(data,"/app/v2/read").length,2);
  await page.evaluate(()=>{
    Object.defineProperty(document,"hidden",{configurable:true,value:false});
    document.dispatchEvent(new Event("visibilitychange"));
  });await received(3);
});

test("questionnaire keeps one approval ID across questions with expected_q_index on each answer",async t=>{
  const {page,data}=await fixture(t);
  data.questionNext=true;Object.assign(data.approvals[0],{kind:"question",q_index:0,q_total:2});
  await page.locator("#awaiting").click();await page.locator('#hub-list [data-approval-id="a1"]').click();
  await page.locator("#ap-progress").getByText("第 1/2 題").waitFor();
  await page.locator("#session-more").click();await page.keyboard.press("a");await page.keyboard.press("j");
  assert.equal(posts(data,"/decision").length,0);
  assert.ok(page.url().includes("codex%3Aalpha"));
  await page.keyboard.press("Escape");
  await page.locator("#ap-actions .ap-yes").click();
  await page.locator("#ap-title").getByText("第二題",{exact:true}).waitFor();
  assert.equal(await page.locator("#awaiting-n").textContent(),"3");
  await page.locator("#ap-actions").getByRole("button",{name:"第二個答案",exact:true}).click();
  await page.waitForFunction(()=>document.getElementById("awaiting-n").textContent==="2");
  assert.deepEqual(posts(data,"/decision").map(c=>c.body.expected_q_index),[0,1]);
  assert.deepEqual(posts(data,"/decision").map(c=>c.body.key),["yes","second"]);
});

test("operation sheets fit phone viewport in light and dark themes",async t=>{
  const {page}=await fixture(t);
  await select(page,"codex:alpha");await page.locator("#session-settings").click();
  await page.locator('[name="model"]').waitFor();await page.setViewportSize({width:390,height:844});
  for(const theme of ["pocket","dark"]) {
    await page.evaluate(theme=>applyTheme(theme),theme);
    const fits=await page.locator("#session-ops").evaluate(el=>{
      const r=el.getBoundingClientRect();
      return r.x>=0 && r.y>=0 && r.right<=innerWidth && r.bottom<=innerHeight && el.scrollWidth<=el.clientWidth;
    });assert.ok(fits,theme);
    await shot(page,"config-mobile-"+theme);
  }
});

test("borrowed sessions never fetch global hub/caps, reveal ops, compact fallback, or send read cursor",async t=>{
  const {page,origin,model,data}=await fixture(t);model.remote=true;data.calls=[];
  await page.goto(origin+"/console");await page.waitForSelector('body[data-console-access="remote"]');
  await select(page,"hermes:reader");await page.waitForTimeout(550);
  assert.equal(await page.locator("[data-session-ops]").count(),0);
  assert.ok(await page.locator("#session-compact").isHidden());
  assert.ok(await page.locator("#session-settings").isHidden());
  assert.equal(data.calls.filter(c=>["/app/v1/approvals","/app/v2/providers/capabilities","/app/v2/read"].includes(c.path)).length,0);
});
