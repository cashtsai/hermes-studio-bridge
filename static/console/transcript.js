// Agent trace markup is data, never HTML. Keep fenced examples in the reply.
function replySegments(text) {
  const parts = [];
  const tags = /(?:^|\n)[ \t]{0,3}(\x60{3,}|~{3,})[^\n]*|<(\/?)(details|think)\b[^>]*>/gi;
  let start = 0, inner = 0, depth = 0, type = "", fence = "";
  function fold(end) {
    const content = text.slice(inner, end);
    const heading = type === "details" && /^\s*<summary\b[^>]*>([\s\S]*?)<\/summary>/i.exec(content);
    parts.push({ label: heading ? heading[1].trim() || "執行步驟" : type === "think" ? "思考摘要" : "執行步驟",
      text: (heading ? content.slice(heading[0].length) : content).trim() });
  }
  for (const m of text.matchAll(tags)) {
    if (m[1]) {
      if (!fence) fence = m[1];
      else if (m[1][0] === fence[0] && m[1].length >= fence.length &&
               m[0].trim() === m[1]) fence = "";
      continue;
    }
    if (fence) continue;
    const tag = m[3].toLowerCase();
    if (!depth && !m[2]) {
      // An inline code example mentioning <details> isn't an execution trace.
      if (tag === "details" && !/^\s*<summary\b/i.test(text.slice(m.index + m[0].length, m.index + m[0].length + 128))) continue;
      parts.push({ text: text.slice(start, m.index) });
      type = tag; depth = 1; inner = m.index + m[0].length;
    } else if (depth && tag === type) {
      if (!m[2]) depth++;
      else if (--depth === 0) { fold(m.index); start = m.index + m[0].length; }
    }
  }
  if (depth) fold(text.length);
  else parts.push({ text: text.slice(start) });
  return parts.filter(p => p.label || p.text.trim());
}

// 每則訊息泡下方的動作列(對齊手機版):時間戳常駐小字,動作鈕 hover 才現。
// 按鈕走 #cards 的事件代理(app.js wire()),這裡只畫靜態結構。
function messageActionsHtml(c) {
  return '<div class="msg-actions" data-card="' + esc(c.id) + '">' +
    '<span class="msg-time">' + esc(cardTime(c.ts)) + '</span>' +
    '<button type="button" class="msg-act" data-act="reply" title="引用這則回覆">回覆</button>' +
    '<button type="button" class="msg-act" data-act="copy" title="複製原始文字">複製</button>' +
    '<button type="button" class="msg-act" data-act="share" title="分享/複製全文">分享</button></div>';
}

function renderTurnContent(turn, node) {
  const wasOpen = new Set(Array.from(node.querySelectorAll("details[open]"), d => d.dataset.key));
  const body = document.createDocumentFragment();
  function details(key, label, content) {
    const d = document.createElement("details"), summary = document.createElement("summary");
    d.dataset.key = key; d.className = "trace"; summary.textContent = label; d.append(summary);
    const fill = () => {
      if (!d.open || d.children.length > 1) return;
      const pre = document.createElement("pre"); pre.textContent = content(); d.append(pre);
    };
    d.addEventListener("toggle", fill);
    if (wasOpen.has(key)) { d.open = true; fill(); }
    body.append(d);
  }
  let steps = [];
  function flush() {
    if (!steps.length) return;
    const group = steps; steps = [];
    details(group[0].id + ":steps", "執行步驟 · " + group.length,
      () => group.map(c => cardText(c)).join("\n\n"));
  }
  function bubble(c, html, actions = true) {
    const div = document.createElement("div");
    div.className = "card " + turn.role + (PROSE_KINDS.has(c.kind || "text") ? "" : " mono") +
      (c.final === false ? " streaming" : "");
    div.dataset.card = c.id;
    div.innerHTML = '<div class="card-body">' + html + "</div>";
    body.append(div);
    if (actions) {
      const row = document.createElement("div");
      row.innerHTML = messageActionsHtml(c);
      row.firstElementChild.classList.add(turn.role === "user" ? "from-user" : "from-agent");
      body.append(row.firstElementChild);
    }
  }
  for (const c of turn.cards) {
    const text = cardText(c);
    if (turn.role !== "user" && (["tool_call","tool_result","thinking","reasoning"].includes(c.kind) ||
        (c.kind === "text" && /^💭\s/u.test(text)))) { steps.push(c); continue; }
    flush();
    if (turn.role !== "user" && /<(?:details|think)\b/i.test(text)) {
      const visual = visualReplyHtml(c);
      if (visual) bubble(c, visual, false);
      const parts = replySegments(text);
      parts.forEach((p, i) => {
        if (p.label) details(c.id + ":" + i, p.label, () => p.text);
        else bubble(c, proseHtml(c, p.text.trim()), i === parts.length - 1);
      });
    } else bubble(c, cardBodyHtmlCached(c));
  }
  flush();
  const meta = document.createElement("div"); meta.className = "turn-meta";
  meta.innerHTML = '<span class="turn-who">' + (turn.role === "user" ? "你" : "Agent") + '</span>';
  node.replaceChildren(meta, body);
}

// 串流尾塊快修(交付 3/5):delta 進來時,若回合裡只有「最後那顆散文泡」在長,
// 就地換它的 body,不整回合重建 —— 已完成的訊息(含 markdown 渲染結果)不重算。
function patchStreamingTail(turn, node) {
  const prev = node._cards;
  if (!prev || prev.length !== turn.cards.length) return false;
  for (let i = 0; i < prev.length - 1; i++) if (prev[i] !== turn.cards[i]) return false;
  const c = turn.cards[turn.cards.length - 1];
  if (turn.role === "user" || !PROSE_KINDS.has(c.kind || "text")) return false;
  const text = cardText(c);
  if (/<(?:details|think)\b/i.test(text) || /^💭\s/u.test(text)) return false;
  if ((c.body || {}).visual) return false;
  const cards = node.querySelectorAll('.card[data-card]');
  const el = cards[cards.length - 1];
  if (!el || el.dataset.card !== c.id || !el.firstElementChild) return false;
  el.className = "card " + turn.role + (c.final === false ? " streaming" : "");
  el.firstElementChild.innerHTML = cardBodyHtmlCached(c);
  const time = node.querySelector('.msg-actions[data-card="' + (window.CSS && CSS.escape ?
    CSS.escape(c.id) : c.id) + '"] .msg-time');
  if (time) time.textContent = cardTime(c.ts);
  return true;
}
