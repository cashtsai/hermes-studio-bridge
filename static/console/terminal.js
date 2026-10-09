// 瀏覽器終端機(P1):接 bridge 既有的 PTY over WebSocket(/app/v1/terminal)。
//
// ⚠️ **刻意不引入 xterm.js**。Artifact/CSP 擋外部 CDN,vendoring 進 repo 要帶
// 三百 KB 以上的資產,而這一頁的用途是「在外面跑幾個指令、看 log」——
// 不是在瀏覽器裡開 vim。所以這裡是一個**夠用的行導向終端**:
//   ‧ 處理 \r(回到行首,進度條/提示符才不會疊成一團)、\b、\n
//   ‧ 剝掉 CSI/OSC 跳脫序列(不渲染顏色,但也不會噴出一堆亂碼)
//   ‧ 全螢幕 TUI(vim / tmux 內層面板)**不會正確顯示** —— 這是已知取捨,
//     需要那個就用 iOS 端的 SwiftTerm 或真 SSH。
//
// tmux 持久化由 bridge 那端負責:帶同一個 ?session= 重連就接回同一條 shell。
"use strict";

let termWS = null;
let termBuf = "";

function termWrite(chunk) {
  // 先剝控制序列:CSI(ESC[…)、OSC(ESC]…BEL/ST)、單字元 ESC 序列
  const clean = chunk
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "")
    .replace(/\x1b[@-Z\\-_]/g, "");
  for (const ch of clean) {
    if (ch === "\r") {
      // 回到行首:砍掉目前這一行已寫的部分(進度條靠這個才不會一直疊)
      const nl = termBuf.lastIndexOf("\n");
      termBuf = termBuf.slice(0, nl + 1);
    } else if (ch === "\b") {
      termBuf = termBuf.slice(0, -1);
    } else if (ch === "\x07") {
      /* bell:忽略 */
    } else {
      termBuf += ch;
    }
  }
  // 上限 200k 字元,避免長時間跑著把分頁吃爆
  if (termBuf.length > 200000) termBuf = termBuf.slice(-150000);
  const out = document.getElementById("tm-out");
  out.textContent = termBuf;
  out.scrollTop = out.scrollHeight;
}

function termConnect() {
  if (termWS) return;
  const name = (document.getElementById("tm-name").value || "console").trim();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = proto + "//" + location.host + "/app/v1/terminal?session=" +
              encodeURIComponent(name) + "&token=" + encodeURIComponent(state.token);
  termSetStatus("連線中…", false);
  let ws;
  try { ws = new WebSocket(url); } catch (e) { termSetStatus("建立連線失敗", false); return; }
  termWS = ws;
  ws.onopen = () => termSetStatus("已連線 · " + name, true);
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.type === "output") termWrite(m.data || "");
    else if (m.type === "error") termWrite("\n[錯誤] " + (m.message || "") + "\n");
    else if (m.type === "exit") { termWrite("\n[shell 結束]\n"); termSetStatus("已結束", false); }
  };
  // 1008 = bridge 在 accept 前就拒絕(終端機停用 / token 不對)
  ws.onclose = (ev) => {
    termWS = null;
    termSetStatus(ev.code === 1008 ? "被拒絕(終端機已停用或 token 失效)" : "已斷線", false);
  };
  ws.onerror = () => termSetStatus("連線錯誤", false);
}

function termDisconnect() {
  if (!termWS) return;
  termWS.close();            // tmux 那端只是 detach,下次帶同名就接回來
  termWS = null;
}

function termSend(data) {
  if (!termWS || termWS.readyState !== 1) { termSetStatus("尚未連線", false); return; }
  termWS.send(JSON.stringify({ type: "input", data }));
}

function termSetStatus(text, ok) {
  const el = document.getElementById("tm-status");
  el.textContent = text;
  el.className = "tm-status " + (ok ? "ok" : "bad");
  document.getElementById("tm-connect").textContent = ok ? "斷線" : "連線";
}

function wireTerminal() {
  document.getElementById("tm-connect").addEventListener("click", () => {
    if (termWS) termDisconnect(); else termConnect();
  });
  document.getElementById("tm-clear").addEventListener("click", () => {
    termBuf = ""; document.getElementById("tm-out").textContent = "";
  });
  const input = document.getElementById("tm-in");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); termSend(input.value + "\n"); input.value = ""; }
    else if (e.key === "Tab") { e.preventDefault(); termSend(input.value + "\t"); }
    else if (e.ctrlKey && e.key.toLowerCase() === "c") { e.preventDefault(); termSend("\x03"); }
    else if (e.ctrlKey && e.key.toLowerCase() === "d") { e.preventDefault(); termSend("\x04"); }
  });
  // 切到終端機分頁時不自動連 —— 連線會開一條 tmux,要由使用者明確按下。
  const prev = window.PocketConsoleMode && window.PocketConsoleMode.onMode;
  if (!window.PocketConsoleMode) return;
  window.PocketConsoleMode.onMode = function (mode) {
    if (typeof prev === "function") prev(mode);
    if (mode === "term") document.getElementById("tm-in").focus();
  };
}
wireTerminal();
