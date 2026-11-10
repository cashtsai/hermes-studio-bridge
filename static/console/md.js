// Pocket Console 的 Markdown 渲染器 —— 零外部依賴(Console 經 relay 服務,
// CSP connect/script 都鎖 'self',不能也不准掛 CDN)。
//
// 安全模型(XSS 紅線):
//   ‧ 所有輸出標籤都是本檔寫死的白名單字串;任何輸入文字在進入 HTML 前
//     一律經過 esc()。沒有「先拼 HTML 再過濾」這種事。
//   ‧ 連結 href 走 safeHref() 白名單(http/https/mailto),先剝控制字元再
//     驗 scheme —— `java\tscript:` 這類拼裝直接出局,降級成純文字。
//   ‧ 輸入裡的 \x00 一進來就剝掉,佔位符(\x00N\x00)不可能被偽造成注入。
//
// node 可直接 require(給 tests/test_console_md.py 的樣本跑),瀏覽器掛在
// window.PocketMarkdown。
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.PocketMarkdown = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]); }

  // scheme 白名單。控制字元/空白先剝光再驗 —— 繞過式拼裝(java\tscript:)
  // 剝完就現形;非白名單 scheme 回 null,呼叫端降級成純文字。
  function safeHref(raw) {
    const cleaned = String(raw || "").replace(/[\u0000-\u0020\u007f-\u009f]+/g, "");
    return /^(?:https?|mailto):/i.test(cleaned) ? cleaned : null;
  }

  const LABEL_MAX = 64;
  function clipLabel(label) {
    // 只截「本身就是一坨 URL/長 token」的顯示文字;一般句子不動。
    const t = String(label);
    if (t.length > LABEL_MAX && !/\s/.test(t)) return t.slice(0, LABEL_MAX - 1) + "…";
    return t;
  }

  function anchor(href, label) {
    return '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer">' +
      esc(clipLabel(label)) + "</a>";
  }

  // 裸網址自動連結。結尾的標點(中英文)不吃進網址;右括號只有在網址內
  // 左括號較多時才保留(維基式 …_(x) 連結)。
  const URL_RE = /https?:\/\/[^\s<>"']+/gi;
  function trimUrl(m) {
    let url = m;
    for (;;) {
      const tail = /[),.;:!?\]'"」』】》〉、，。；：！？…]$/.exec(url);
      if (!tail) break;
      if (tail[0] === ")" &&
          (url.match(/\(/g) || []).length >= (url.match(/\)/g) || []).length) break;
      url = url.slice(0, -1);
    }
    return url;
  }

  // ── 行內渲染 ──────────────────────────────────────────────────────────
  // 作法:行內碼/連結/裸網址先抽成佔位符(\x00N\x00,輸入端的 \x00 已剝),
  // 剩餘文字整段 esc,再在「已轉義的文字」上做粗斜體,最後回填佔位符。
  // 佔位符內容全是本檔自己生成的安全 HTML。
  function inline(raw) {
    const toks = [];
    const stash = (html) => { toks.push(html); return "\x00" + (toks.length - 1) + "\x00"; };
    let s = String(raw);

    // 行內碼(` 或 `` 圍欄;`` 允許內含單個反引號)
    s = s.replace(/(``+|`)([^`]+?)\1/g, (m, fence, code) =>
      stash("<code>" + esc(code.replace(/^ (.*) $/, "$1")) + "</code>"));

    // [文字](url) 與 ![alt](url)。圖片語法降級成連結 —— Console 的 CSP 只准
    // 'self'/blob 圖,外部圖直接 <img> 既載不出來也不該去打。
    s = s.replace(/(!?)\[([^\]\n]*)\]\(([^()\s]+)\)/g, (m, bang, label, url) => {
      const href = safeHref(url);
      if (!href) return m;             // 危險/怪 scheme:整段留作純文字
      return stash(anchor(href, label.trim() || url));
    });

    // 裸網址
    s = s.replace(URL_RE, (m) => {
      const url = trimUrl(m);
      return stash(anchor(url, url)) + m.slice(url.length);
    });

    s = esc(s);

    // 粗/斜/刪除線(此時文字已轉義,樣式規則只會包到安全內容)
    s = s.replace(/\*\*(?=\S)([\s\S]*?\S|\S)\*\*/g, "<strong>$1</strong>")
         .replace(/(^|[^\w\\])__(?=\S)([\s\S]*?\S|\S)__(?!\w)/g, "$1<strong>$2</strong>")
         .replace(/\*(\S(?:[^*\n]*\S)?)\*/g, "<em>$1</em>")
         .replace(/(^|[\s(（【「>])_(\S(?:[^_\n]*\S)?)_(?=$|[\s).,:;!?，。；：！？）】」])/g, "$1<em>$2</em>")
         .replace(/~~(?=\S)([\s\S]*?\S|\S)~~/g, "<del>$1</del>");

    // 回填(連結文字裡還可能藏著行內碼佔位符,所以要收斂到沒有為止)
    for (let guard = 0; guard < 5 && /\x00\d+\x00/.test(s); guard++)
      s = s.replace(/\x00(\d+)\x00/g, (m, i) => toks[+i] ?? "");
    return s;
  }

  function inlineLines(text) {
    return text.split("\n").map(inline).join("<br>");
  }

  function codeBlock(lang, code) {
    const label = esc(String(lang || "").trim().slice(0, 24));
    return '<div class="md-code"><div class="md-code-head"><span class="md-code-lang">' +
      (label || "code") + '</span><button type="button" class="md-copy" title="複製程式碼">複製</button></div>' +
      '<pre class="md-pre"><code>' + esc(code) + "</code></pre></div>";
  }

  const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  function list(lines, st) {
    const first = LIST_RE.exec(lines[st.i]);
    const indent = first[1].length;
    const ordered = /^\d/.test(first[2]);
    const items = [];
    while (st.i < lines.length) {
      const m = LIST_RE.exec(lines[st.i]);
      if (!m || m[1].length < indent) break;
      if (m[1].length > indent) {               // 內縮更深 → 巢狀清單
        const sub = list(lines, st);
        if (items.length) items[items.length - 1] =
          items[items.length - 1].replace(/<\/li>$/, sub + "</li>");
        else items.push("<li>" + sub + "</li>");
        continue;
      }
      if (/^\d/.test(m[2]) !== ordered) break;  // 型別切換 → 讓外層開新清單
      st.i++;
      let text = m[3];
      while (st.i < lines.length && /^\s+\S/.test(lines[st.i]) && !LIST_RE.test(lines[st.i]))
        text += "\n" + lines[st.i++].trim();    // 縮排接續行併進同一項
      items.push("<li>" + inlineLines(text) + "</li>");
    }
    return (ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>");
  }

  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }
  function table(lines, st) {
    const head = splitRow(lines[st.i]);
    st.i += 2;
    let html = '<div class="md-table"><table><thead><tr>' +
      head.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
    while (st.i < lines.length && lines[st.i].includes("|") && lines[st.i].trim()) {
      const row = splitRow(lines[st.i++]);
      html += "<tr>" + head.map((_, j) => "<td>" + inline(row[j] ?? "") + "</td>").join("") + "</tr>";
    }
    return html + "</tbody></table></div>";
  }

  // ── 區塊渲染 ──────────────────────────────────────────────────────────
  function render(src) {
    const lines = String(src == null ? "" : src)
      .replace(/\x00/g, "").replace(/\r\n?/g, "\n").split("\n");
    const out = [], para = [];
    const st = { i: 0 };
    const flush = () => {
      if (para.length) out.push("<p>" + para.map(inline).join("<br>") + "</p>");
      para.length = 0;
    };
    while (st.i < lines.length) {
      const line = lines[st.i];
      const fence = /^[ \t]{0,3}(```+|~~~+)[ \t]*([^`\s]*)[^`]*$/.exec(line);
      if (fence) {
        flush(); st.i++;
        const close = new RegExp("^[ \\t]{0,3}\\" + fence[1][0] + "{" + fence[1].length + ",}[ \\t]*$");
        const body = [];
        while (st.i < lines.length && !close.test(lines[st.i])) body.push(lines[st.i++]);
        if (st.i < lines.length) st.i++;        // 吃掉關欄;沒關欄就到檔尾
        out.push(codeBlock(fence[2], body.join("\n")));
        continue;
      }
      if (!line.trim()) { flush(); st.i++; continue; }
      if (/^ {0,3}([-*_])\s*(?:\1\s*){2,}$/.test(line)) { flush(); out.push("<hr>"); st.i++; continue; }
      const h = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      if (h) {  // 標題壓到 h3 以下:訊息泡裡不該出現比 UI 還大的字
        flush();
        const lvl = Math.min(h[1].length + 2, 5);
        out.push("<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">");
        st.i++; continue;
      }
      if (/^ {0,3}>/.test(line)) {
        flush();
        const q = [];
        while (st.i < lines.length && /^ {0,3}>/.test(lines[st.i]))
          q.push(lines[st.i++].replace(/^ {0,3}> ?/, ""));
        out.push("<blockquote>" + render(q.join("\n")) + "</blockquote>");
        continue;
      }
      if (LIST_RE.test(line)) { flush(); out.push(list(lines, st)); continue; }
      if (line.includes("|") && st.i + 1 < lines.length && TABLE_SEP.test(lines[st.i + 1]) &&
          lines[st.i + 1].includes("|")) {
        flush(); out.push(table(lines, st)); continue;
      }
      para.push(line.replace(/^ {0,3}/, ""));
      st.i++;
    }
    flush();
    return out.join("");
  }

  // 使用者訊息/等寬卡片用:只轉義 + 裸網址連結化 + 換行,**不**做樣式標記
  // (打什麼看到什麼,和 Telegram 的使用者端一致)。
  function renderText(raw) {
    const toks = [];
    let s = String(raw == null ? "" : raw).replace(/\x00/g, "");
    s = s.replace(URL_RE, (m) => {
      const url = trimUrl(m);
      toks.push(anchor(url, url));
      return "\x00" + (toks.length - 1) + "\x00" + m.slice(url.length);
    });
    s = esc(s).replace(/\x00(\d+)\x00/g, (m, i) => toks[+i] ?? "");
    return s.replace(/\n/g, "<br>");
  }

  return { render, renderText, safeHref, escapeHtml: esc };
});
