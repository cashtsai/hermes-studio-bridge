// Opt-in diagnostic; report samples, not a machine-dependent CI timing gate.
const test = require("node:test");
const assert = require("node:assert/strict");
const {workstation} = require("./fixtures/workstation.cjs");

test("Chinese composing in a long Console session", {skip:process.env.POCKET_QA_TYPING!=="1"}, async t => {
  const count=800, id="codex:a";
  const {page,model,context}=await workstation(t,false,{isolated:true,historySize:count});
  await page.locator('#session-list [data-id="'+id+'"]').click();
  await page.locator('#cards [data-key="m799"]').waitFor();
  await page.waitForFunction(()=>document.querySelector("#stream-health").textContent==="");
  const input=page.locator("#input");
  const cdp=await context.newCDPSession(page);
  let seq=count;
  for (const live of [false,true]) {
    await input.fill(""); await input.focus();
    await input.evaluate(el => {
      el.samples=[];
      el.oninput=e=>{
        const start=e.timeStamp, samples=el.samples;
        requestAnimationFrame(()=>requestAnimationFrame(()=>samples.push(performance.now()-start)));
      };
    });
    const stream=model.streams.get("/app/v2/sessions/"+encodeURIComponent(id)+"/events");
    const timer=live && setInterval(()=>{
      ++seq;
      const card={id:"live",turn_id:"probe-turn",ts:count+1,role:"agent",kind:"text",body:{text:"串流輸出 ".repeat(seq-count)}};
      stream.write("data: "+JSON.stringify({type:"card.upsert",seq,data:{card}})+"\n\n");
    },50);
    try {
      for (let i=0;i<40;i++) {
        await cdp.send("Input.imeSetComposition",{text:"測",selectionStart:1,selectionEnd:1});
        await cdp.send("Input.insertText",{text:"測"});
        await input.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
      }
    } finally { clearInterval(timer); }
    assert.equal(await input.inputValue(),"測".repeat(40));
    assert.equal(model.posts.length,0);
    const samples=(await input.evaluate(el=>el.samples)).sort((a,b)=>a-b);
    assert.ok(samples.length>=40);
    if(live) assert.ok(seq>count && await page.locator('#cards [data-key="live"]').count()===1);
    const pct=p=>Number(samples[Math.ceil(samples.length*p)-1].toFixed(1));
    t.diagnostic(JSON.stringify({live,cards:count,samples:samples.length,p50Ms:pct(.5),p95Ms:pct(.95),maxMs:pct(1),streamEvents:seq-count}));
  }
});
