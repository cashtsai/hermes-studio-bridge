// Session operations consume the host's capability matrix, never provider-name UI branches.
window.PocketSessionOps = (() => {
  let providers = null, capsLoading = null, modalVersion = 0;
  let hubRows = [], hubTotal = 0, hubNext = null, hubReady = false, hubLoading = false;
  let hubError = "", hubRequest = 0, selectedApproval = null, hubTimer = null;
  let readTimer = null, readDevice = null;
  const reads = new Map(), pendingOps = new Set();
  const matrixOps = new Set(["rename", "archive", "history", "config_read", "config_write"]);
  const names = {model:"模型", effort:"推理強度", approval_policy:"核准策略", sandbox:"沙盒",
    permission_mode:"權限模式", mode:"模式"};
  const el = id => document.getElementById(id);
  const endpoint = (s, op) => "/app/v2/sessions/" + encodeURIComponent(s.id) + "/" + op;
  const info = s => providers?.[s?.provider];
  function capability(cap, s) {
    const p = info(s);
    if (!p) return undefined;
    if (matrixOps.has(cap)) return Array.isArray(p.ops) ? p.ops.includes(cap) : undefined;
    return typeof p[cap] === "boolean" ? p[cap] : undefined;
  }
  function canAct(cap, s) {
    return !!s && !signingOut && state.listHealth === "ready" && canUse(cap, s);
  }
  async function request(path, body, options = {}) {
    const r = await api(path, {cache:"no-store", signal:AbortSignal.timeout(60000), ...options,
      ...(body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})});
    const data = await r.json().catch(()=>({}));
    if (!r.ok) {
      const message=data.error?.message || (typeof data.detail === "string" ? data.detail : "HTTP " + r.status);
      const detail=typeof data.error?.detail === "string" ? data.error.detail : "";
      throw Object.assign(new Error(message+(detail && detail!==message ? "\n"+detail : "")),
        {status:r.status, code:data.error?.code || r.headers.get("X-Error-Code")});
    }
    return data;
  }
  function button(label, action, className = "btn-ghost") {
    const b = document.createElement("button"); b.type="button"; b.className=className;
    b.textContent=label; b.addEventListener("click", action); return b;
  }
  function notice(text, retry, actionLabel="重試", error=false) {
    if (signingOut) return;
    const row=document.createElement("div"); row.className="op-notice"+(error ? " op-error" : "");
    const message=document.createElement("span"); message.textContent=text; row.append(message);
    if (retry) row.append(button(actionLabel, async e=>{
      e.target.disabled=true;
      try { await retry(); row.remove(); }
      catch (err) { message.textContent="操作未完成："+err.message; row.classList.add("op-error"); e.target.disabled=false; }
    }));
    row.append(button("×", ()=>row.remove(), "icon-btn")); row.lastChild.setAttribute("aria-label","關閉通知");
    el("ops-notices").append(row); return row;
  }
  async function loadCapabilities() {
    if (borrowed() || signingOut) return;
    if (capsLoading) return capsLoading;
    capsLoading=(async()=>{
      try {
        const data=await request("/app/v2/providers/capabilities");
        if (signingOut || borrowed()) return;
        if (!data.providers || typeof data.providers !== "object") throw Error("無效的能力清單");
        providers=data.providers;
        renderSessions(); syncControls(); updateOlderButton(); scheduleRead();
      } catch (err) {
        notice("主機能力清單讀取失敗："+err.message, loadCapabilities, "重新讀取", true);
      } finally { capsLoading=null; }
    })();
    return capsLoading;
  }
  function menuButton(s) {
    if (!["rename","archive","config_read","config_write","history"].some(op=>canUse(op,s))) return "";
    return '<button type="button" class="icon-btn sess-more" data-session-ops="'+esc(s.id)+
      '" aria-label="'+esc((s.title||s.id)+" 的操作")+'" title="對話操作">⋯</button>';
  }
  function syncControls() {
    const s=currentSession();
    el("session-compact").hidden=!s || !canUse("compact",s);
    el("session-settings").hidden=!s || !(canUse("config_read",s)||canUse("config_write",s));
    el("session-more").hidden=!s || !menuButton(s);
    for (const id of ["session-compact","session-settings","session-more"]) el(id).disabled=state.listHealth!=="ready";
    if (borrowed()) { hubRows=[]; hubReady=false; selectedApproval=null; el("approval-hub").hidden=true; }
  }
  function close() { modalVersion++; el("session-ops").close(); scheduleRead(); }
  function show(s, title) {
    const version=++modalVersion;
    el("op-title").textContent=title;
    el("op-session").textContent=s.title||s.id;
    el("op-content").replaceChildren(); el("op-status").textContent="";
    if (!el("session-ops").open) el("session-ops").showModal();
    return version;
  }
  function current(version) { return version===modalVersion && !signingOut && el("session-ops").open; }
  function busy(version, text) {
    if (!current(version)) return;
    el("op-status").textContent=text;
    el("op-content").querySelectorAll("button,input,select").forEach(n=>n.disabled=true);
  }
  function menu(s) {
    if (!s) return;
    show(s,"對話操作");
    const items=[["rename","重新命名",()=>rename(s)],["archive","封存",()=>archive(s)],
      ["history","歷史逐字稿",()=>history(s)]];
    if (canUse("config_read",s)||canUse("config_write",s)) el("op-content").append(button("對話設定",()=>config(s),"op-menu-item"));
    for (const [cap,label,action] of items) if (canUse(cap,s)) el("op-content").append(button(label,action,"op-menu-item"));
  }
  function rename(s) {
    if (!canAct("rename",s)) return;
    const version=show(s,"重新命名"), form=document.createElement("form");
    const label=document.createElement("label"); label.textContent="名稱";
    const input=document.createElement("input"); input.value=s.title||""; input.required=true; input.maxLength=200;
    label.append(input); form.append(label);
    const save=button("儲存",()=>{} ,"btn btn-primary"); save.type="submit"; form.append(save);
    form.addEventListener("submit",async e=>{
      e.preventDefault(); const name=input.value.trim();
      if (!name || pendingOps.has(s.id) || !canAct("rename",s)) return;
      pendingOps.add(s.id); busy(version,"正在更名…");
      try {
        const result=await request(endpoint(s,"rename"),{name});
        const renamed=result.session_id || (result.session?.name ? s.id.substring(0,s.id.indexOf(":")+1)+result.session.name : s.id);
        const wasCurrent=state.current===s.id;
        const draft=drafts.get(s.id);
        if (renamed!==s.id && draft) { drafts.set(renamed,draft); drafts.delete(s.id); }
        if (current(version)) close();
        await loadSessions();
        if (wasCurrent) { if (renamed!==s.id) showHome(); openSession(renamed); el("sh-name").textContent=name; }
        notice("已重新命名為「"+name+"」");
      } catch(err) {
        if (current(version)) { el("op-status").textContent="更名失敗："+err.message; input.disabled=false; save.disabled=false; }
        else notice("更名失敗："+err.message,null,"",true);
      } finally { pendingOps.delete(s.id); }
    });
    el("op-content").append(form); input.focus(); input.select();
  }
  function archive(s) {
    if (!canAct("archive",s)) return;
    const version=show(s,"封存對話"), msg=document.createElement("p");
    msg.textContent="封存會移出工作清單，部分主機也會停止執行中的工作。對話紀錄不會刪除。";
    el("op-content").append(msg,button("確認封存",async()=>{
      if (pendingOps.has(s.id)||!canAct("archive",s)) return;
      pendingOps.add(s.id); busy(version,"正在封存…");
      try {
        await request(endpoint(s,"archive"),{archived:true});
        if (current(version)) close();
        if (state.current===s.id) showHome();
        await loadSessions();
        notice("已封存「"+(s.title||s.id)+"」",async()=>{
          if (!canAct("archive",s)) throw Error("目前無權取消封存");
          await request(endpoint(s,"archive"),{archived:false}); await loadSessions();
          notice("已取消封存「"+(s.title||s.id)+"」");
        },"取消封存");
      } catch(err) {
        if (current(version)) { el("op-status").textContent="封存失敗："+err.message; el("op-content").querySelector("button").disabled=false; }
        else notice("封存失敗："+err.message,null,"",true);
      } finally { pendingOps.delete(s.id); }
    },"btn btn-danger"));
  }
  async function config(s) {
    if (!s || !(canAct("config_read",s)||canAct("config_write",s))) return;
    const version=show(s,"對話設定"); el("op-status").textContent="讀取設定…";
    try {
      const data=canUse("config_read",s) ? await request(endpoint(s,"config")) : {...s.meta};
      if (!current(version)) return;
      const schema=info(s)?.config_fields || Object.fromEntries(Object.keys(data)
        .filter(k=>typeof data[k]==="string").map(k=>[k,{editable:false}]));
      el("op-status").textContent=Object.keys(schema).length ? "" : "主機未提供可編輯的設定";
      const form=document.createElement("form"), controls=new Map();
      for (const [key,field] of Object.entries(schema)) {
        const label=document.createElement("label"); label.textContent=names[key]||key;
        const options=data[key+"_options"] || field.options;
        const values=Array.isArray(options) ? options.map(o=>typeof o==="string" ? {value:o,name:o} : o) : [];
        const input=document.createElement(values.length ? "select" : "input");
        if (values.length) {
          if (!values.some(o=>String(o.value)===String(data[key]||""))) values.unshift({value:data[key]||"",name:data[key]||"未設定"});
          for (const o of values) { const option=document.createElement("option"); option.value=o.value; option.textContent=o.name||o.value; input.append(option); }
        }
        input.name=key; input.value=data[key]||"";
        input.disabled=!field.editable || !canUse("config_write",s);
        if (!field.editable) { input.setAttribute("aria-label",(names[key]||key)+"（建立時設定）"); input.title="建立時設定，現有對話無法修改"; }
        else input.setAttribute("aria-label",names[key]||key);
        label.append(input); form.append(label); controls.set(key,{input,field,original:String(data[key]||"")});
      }
      const save=button("儲存",()=>{},"btn btn-primary"); save.type="submit";
      save.disabled=!canUse("config_write",s)||![...controls.values()].some(c=>c.field.editable);
      form.append(save); el("op-content").append(form);
      form.addEventListener("submit",async e=>{
        e.preventDefault(); if (!canAct("config_write",s)||pendingOps.has(s.id)) return;
        const changes=Object.fromEntries([...controls].filter(([,c])=>c.field.editable && c.input.value!==c.original)
          .map(([key,c])=>[key,c.input.value.trim()]));
        if (!Object.keys(changes).length) { el("op-status").textContent="沒有變更"; return; }
        if (changes.approval_policy==="never" && !window.confirm("停用此對話的核准提示？")) return;
        pendingOps.add(s.id); busy(version,"正在儲存…");
        try {
          const result=await request(endpoint(s,"config"),changes);
          const ignored=result.ignored||[];
          const message=ignored.length ? "未套用："+ignored.join("、") : result.applied_live===false ? "設定已儲存，下次連線生效" : "設定已更新";
          if (current(version)) {
            el("op-status").textContent=message;
            for (const [key,c] of controls) if (!ignored.includes(key)) c.original=c.input.value;
          } else notice(message);
        } catch(err) {
          if (current(version)) el("op-status").textContent="設定未更新："+err.message;
          else notice("設定未更新："+err.message,null,"",true);
        } finally {
          pendingOps.delete(s.id);
          if (current(version)) {
            for (const c of controls.values()) c.input.disabled=!c.field.editable || !canAct("config_write",s);
            save.disabled=!canAct("config_write",s);
          }
        }
      });
    } catch(err) {
      if (current(version)) { el("op-status").textContent="設定讀取失敗："+err.message; el("op-content").append(button("重試",()=>config(s))); }
    }
  }
  async function compact(s) {
    if (!canAct("compact",s)||pendingOps.has(s.id)) return;
    const version=show(s,"壓縮脈絡");
    el("op-content").append(button("開始壓縮",async()=>{
      if (!canAct("compact",s)||pendingOps.has(s.id)) return;
      pendingOps.add(s.id); busy(version,"正在送出壓縮請求…");
      try {
        let result;
        try { result=await request(endpoint(s,"compact"),{}); }
        catch(err) {
          if (err.code!=="USE_CC_COMPRESS" || borrowed()) throw err;
          result=await request("/ccsessions/"+encodeURIComponent(s.id.substring(s.id.indexOf(":")+1))+"/compress",{});
        }
        if (current(version)) close(); notice(result.message||"已送出壓縮請求，完成狀態以主機回報為準");
      } catch(err) {
        if (current(version)) { el("op-status").textContent="壓縮未啟動："+err.message; el("op-content").querySelector("button").disabled=false; }
        else notice("壓縮未啟動："+err.message,null,"",true);
      } finally { pendingOps.delete(s.id); }
    },"btn btn-primary"));
  }
  function history(s) {
    if (!canAct("history",s)) return;
    const version=show(s,"歷史逐字稿");
    const content=document.createElement("div"); content.className="op-history"; content.tabIndex=0;
    const more=button("更早訊息",()=>load()); more.id="history-more";
    el("op-content").append(more,content);
    let offset=0, cursor=null, loading=false, seen=new Set();
    async function load() {
      if (loading||!current(version)||!canAct("history",s)) return;
      loading=true; more.disabled=true; el("op-status").textContent="讀取逐字稿…";
      try {
        const params=new URLSearchParams({limit:"48",offset:String(offset)});
        if (cursor) params.set("cursor",cursor);
        const page=await request(endpoint(s,"history")+"?"+params);
        if (!current(version)) return;
        if (!Array.isArray(page.messages) && typeof page.text!=="string") throw Error("無效的逐字稿回應");
        const next=page.nextCursor || page.next_cursor || null;
        if (page.more && ("nextCursor" in page || "next_cursor" in page) && (!next || next===cursor)) throw Error("主機未提供有效的下一頁游標");
        const fragment=document.createDocumentFragment();
        if (Array.isArray(page.messages)) {
          for (const message of page.messages) {
            if (message.id && seen.has(message.id)) continue;
            if (message.id) seen.add(message.id);
            const row=document.createElement("article"), who=document.createElement("strong"), text=document.createElement("div");
            who.textContent=message.role==="user" ? "你" : "Agent";
            text.className="history-text";
            text.textContent=typeof message.content==="string" ? message.content : JSON.stringify(message.content||message.text||"");
            row.append(who,text); fragment.append(row);
          }
        } else { const pre=document.createElement("pre"); pre.textContent=page.text; fragment.append(pre); }
        const oldHeight=content.scrollHeight, oldTop=content.scrollTop;
        content.prepend(fragment); content.scrollTop=offset===0 ? content.scrollHeight : oldTop+content.scrollHeight-oldHeight;
        offset+=48;
        cursor=next; more.hidden=!page.more; more.textContent="更早訊息";
        el("op-status").textContent=page.more ? "" : content.textContent ? "已到最早紀錄" : "沒有逐字稿";
      } catch(err) { if (current(version)) { el("op-status").textContent="歷史讀取失敗："+err.message; more.textContent="重試更早訊息"; } }
      finally { loading=false; if (current(version)) more.disabled=false; }
    }
    void load();
  }
  function approvalSession() {
    const a=hubRows.find(a=>a.id===selectedApproval && a.session_id===state.current && !resolvedApprovals.has(String(a.id)));
    if (!a) return null;
    const s=state.sessions.find(s=>s.id===a.session_id)||{id:a.session_id,provider:a.provider,title:a.session_id};
    const same=approvalIdOf(s.meta)===a.id;
    return {...s,_hub:true,_hubPromptCurrent:same,meta:{...s.meta,prompt:same ? s.meta?.prompt : null,approval:a}};
  }
  function pendingHub() { return hubRows.filter(a=>!resolvedApprovals.has(String(a.id)) && a.status==="pending" &&
    (!activityTime(a.expires_at)||activityTime(a.expires_at)>Date.now())); }
  // 可安全批次的 = 二元 permission(明確 allow/deny)。刻意**跳過 question/多選**:
  // 那些要「選哪個」,盲批會答錯;批次只碰「准/駁」語意清楚的。
  function binaryPending() {
    return pendingHub().filter(a=>{
      const v=approvalView({meta:{approval:a}});
      return v.kind==="permission" && !v.multiselect;
    });
  }
  async function batchDecide(approve) {
    const items=binaryPending();
    if (!items.length) return;
    const skipped=pendingHub().length-items.length;
    const extra=skipped ? `（略過 ${skipped} 項需選項的問題,要逐條處理）` : "";
    const msg=approve
      ? `確定核准全部 ${items.length} 項二元請求？${extra}\n核准＝放行這些 agent 動作,請確認你信任它們。`
      : `否決全部 ${items.length} 項？${extra}`;
    if (!window.confirm(msg)) return;
    el("hub-approve-all").disabled=el("hub-deny-all").disabled=true;
    let ok=0, fail=0;
    for (const a of items) {
      try { await request("/app/v1/approvals/"+encodeURIComponent(a.id)+"/decision",{approve,approval_id:a.id});
        noteApprovalResolved(a.id); ok++; }
      catch(e) { fail++; }
    }
    toast(`已${approve ? "核准" : "否決"} ${ok} 項`+(fail ? `,${fail} 項失敗` : ""), fail>0);
    renderHub(); void loadHub();
  }
  function renderHub() {
    const list=el("hub-list"); list.replaceChildren();
    el("hub-status").textContent=hubError || (hubLoading ? "讀取待審清單…" : hubReady && !pendingHub().length ? "目前沒有待審項目" : "");
    for (const a of pendingHub()) {
      const s=state.sessions.find(s=>s.id===a.session_id)||{id:a.session_id,provider:a.provider};
      const view=approvalView({...s,meta:{approval:a}});
      const b=button("",()=>{
        selectedApproval=a.id;
        openSession(a.session_id); renderApproval();
        el("approval").scrollIntoView({block:"nearest"});
      },"hub-row");
      b.disabled=!a.session_id || !state.sessions.some(s=>s.id===a.session_id);
      b.dataset.approvalId=a.id;
      const title=document.createElement("strong"); title.textContent=view.title;
      const detail=document.createElement("small"); detail.textContent=[s.title||a.session_id, APPROVAL_RISK[view.risk]?.[0],view.detail,
        b.disabled ? "對話不在目前清單，請重新整理或在 App 處理" : ""].filter(Boolean).join(" · ");
      b.append(title,detail); list.append(b);
    }
    el("hub-more").hidden=hubNext===null; el("hub-more").disabled=hubLoading;
    // 批次列:只有 ≥2 項可安全批次(二元 permission)時才出現,避免一兩項也擺一排鈕。
    const bin=binaryPending().length;
    el("hub-batch").hidden=bin<2;
    if (bin>=2) { el("hub-batch-label").textContent=`${bin} 項二元請求`;
      el("hub-approve-all").disabled=el("hub-deny-all").disabled=false; }
    renderAwaiting();
  }
  function renderAwaiting() {
    if (borrowed()) return false;
    const n=hubReady ? Math.max(0,hubTotal-(hubRows.length-pendingHub().length)) : null;
    el("awaiting").classList.remove("hidden"); el("awaiting-n").textContent=n===null ? "?" : String(n);
    el("awaiting").setAttribute("aria-expanded",String(!el("approval-hub").hidden));
    if (n!==null) notifyAwaiting(n);
    return true;
  }
  async function loadHub(append=false) {
    if (borrowed()||signingOut || (append && (hubLoading||hubNext===null))) return;
    const requestId=++hubRequest, offset=append ? hubNext : 0;
    hubLoading=true; hubError=""; renderHub();
    try {
      const data=await request("/app/v1/approvals?status=pending&limit=50&offset="+offset);
      if (requestId!==hubRequest||signingOut||borrowed()) return;
      if (!Array.isArray(data.approvals)) throw Error("無效的待審清單");
      hubRows=Array.from(new Map([...(append ? hubRows : []),...data.approvals].map(a=>[a.id,a])).values());
      hubTotal=data.total ?? hubRows.length; hubNext=data.next_offset ?? null; hubReady=true;
      renderApproval();
    } catch(err) { if (requestId===hubRequest) hubError="待審清單讀取失敗："+err.message; }
    finally { if (requestId===hubRequest) { hubLoading=false; if (!signingOut) renderHub(); } }
  }
  function toggleHub() {
    if (borrowed()) return false;
    el("approval-hub").hidden=!el("approval-hub").hidden;
    renderAwaiting(); if (!el("approval-hub").hidden) void loadHub();
    return true;
  }
  function resolved(aid, receipt) {
    if (["approved","denied","rejected","answered","acknowledged","expired"].includes(receipt?.status)) noteApprovalResolved(aid);
    renderHub(); void loadHub();
  }
  function onEvent(event) {
    if (event.type==="read_cursor.update") {
      const c=event.data?.global || event.data || {}, id=event.session||event.data?.session;
      if (id) { const r=reads.get(id)||{sent:0}; r.sent=Math.max(r.sent,activityTime(c.last_read_ts)/1000); reads.set(id,r); }
    }
    if (["approval.resolved","approval.requested","session.status"].includes(event.type)) {
      clearTimeout(hubTimer); hubTimer=setTimeout(()=>loadHub(),150);
    }
  }
  function scheduleRead() {
    if (readTimer||signingOut) return;
    readTimer=setTimeout(()=>{ readTimer=null; void reportRead(); },400);
  }
  async function reportRead() {
    const s=currentSession(), cards=el("cards");
    // Ring seq is NOT the global event-log seq. Report the visible message timestamp.
    const supported = borrowed() ? s?.capabilities?.includes("read_cursor") : capability("read_cursor",s) === true;
    if (!s||!canAct("read_cursor",s)||!supported||document.hidden||
        !document.hasFocus()||el("session-ops").open||el("report-reader")?.open||state.snapshotView!==state.view||!cards.clientHeight||!cardScroll.follow||
        cards.scrollHeight-cards.clientHeight-cards.scrollTop>2) return;
    const last=state.cards.filter(c=>c.final!==false && ["user","agent","assistant"].includes(c.role||"agent") &&
      ["text","markdown"].includes(c.kind||"text"))
      .reduce((prev,c)=>activityTime(c.ts)>activityTime(prev?.ts) ? c : prev,null);
    const ts=activityTime(last?.ts)/1000, key=s.id.substring(s.id.indexOf(":")+1);
    const r=reads.get(key)||{sent:0}; reads.set(key,r);
    if (!ts || r.inflight || r.warned || ts<=r.sent) return;
    if (!readDevice && !borrowed()) {
      try { readDevice=localStorage.getItem("pocket.console.read-device")||crypto.randomUUID(); localStorage.setItem("pocket.console.read-device",readDevice); }
      catch (_) { readDevice=crypto.randomUUID(); }
    }
    r.inflight=true;
    try {
      const result = await request(borrowed() ? endpoint(s,"read") : "/app/v2/read",
        {session:key,device_id:readDevice,last_read_ts:ts,message_id:last.id});
      if (result.read_state) applyReadState(s.id, result.read_state);
      else if (result.global) applyListEvent({type:"read_cursor.update",session:key,data:result});
      renderSessions(); renderHome();
      r.sent=Math.max(r.sent,ts); scheduleRead();
    } catch(err) {
      if (!r.warned) { r.warned=true; notice("已讀狀態同步失敗："+err.message,()=>{r.warned=false;scheduleRead();},"重試",true); }
    } finally { r.inflight=false; }
  }
  function init() {
    document.addEventListener("click",e=>{
      const trigger=e.target.closest("[data-session-ops]");
      if (trigger) menu(state.sessions.find(s=>s.id===trigger.dataset.sessionOps));
    });
    el("op-close").addEventListener("click",close);
    el("session-ops").addEventListener("cancel",()=>{modalVersion++;});
    el("session-ops").addEventListener("close",scheduleRead);
    el("report-reader")?.addEventListener("close",scheduleRead);
    el("session-more").addEventListener("click",()=>menu(currentSession()));
    el("session-settings").addEventListener("click",()=>config(currentSession()));
    el("session-compact").addEventListener("click",()=>compact(currentSession()));
    el("hub-refresh").addEventListener("click",()=>loadHub());
    el("hub-more").addEventListener("click",()=>loadHub(true));
    el("hub-approve-all").addEventListener("click",()=>batchDecide(true));
    el("hub-deny-all").addEventListener("click",()=>batchDecide(false));
    el("hub-close").addEventListener("click",()=>{el("approval-hub").hidden=true;renderAwaiting();});
    window.addEventListener("focus",scheduleRead);
    document.addEventListener("visibilitychange",()=>{
      if (!document.hidden) { scheduleRead(); if (!el("approval-hub").hidden) void loadHub(); }
    });
  }
  function leave() {
    close(); clearTimeout(readTimer); readTimer=null; clearTimeout(hubTimer); hubRequest++;
    hubRows=[]; reads.clear(); selectedApproval=null;
    el("ops-notices").replaceChildren(); el("hub-list").replaceChildren(); el("approval-hub").hidden=true;
    el("op-content").replaceChildren(); el("op-status").textContent="";
  }
  return {init,capability,loadCapabilities,menuButton,syncControls,history,scheduleRead,
    approvalSession,renderAwaiting,toggleHub,loadHub,resolved,onEvent,leave};
})();
