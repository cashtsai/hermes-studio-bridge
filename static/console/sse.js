// SSE over fetch()+ReadableStream —— 繞過瀏覽器原生 EventSource 無法帶
// Authorization header 的限制(bridge 的 /app/v2/events 只認 Bearer,沒有
// ?token= fallback)。見 SPEC_DESKTOP_CONSOLE_20261006 §3.3。
//
// 用法:
//   const stop = sseStream(url, token, {
//       onEvent: (evt) => {...},   // evt = {event, data(已 JSON.parse), id}
//       onOpen:  () => {...},
//       onError: (err) => {...},
//   });
//   stop();  // 中止
//
// 自動重連:斷線後指數退避重試(除非 stop() 被呼叫)。
function sseStream(url, token, handlers) {
  let aborted = false;
  let controller = null;
  let retry = 0;
  let timer;

  async function connect() {
    if (aborted) return;
    controller = new AbortController();
    try {
      const resp = await fetch(typeof url === "function" ? url() : url, {
        headers: { "Authorization": "Bearer " + token, "Accept": "text/event-stream" },
        signal: controller.signal,
      });
      if (aborted) return;
      if (resp.status === 401 || resp.status === 403) {
        handlers.onDenied && handlers.onDenied(resp.status);
        return;
      }
      if (resp.status === 410 && handlers.onGone) { // SEQ_GONE
        handlers.onGone && handlers.onGone();
        return;
      }
      if (!resp.ok || !(resp.headers.get("Content-Type") || "").includes("text/event-stream"))
        throw new Error("SSE HTTP " + resp.status);
      retry = 0;
      handlers.onOpen && handlers.onOpen();

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (!aborted) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE 以空行分隔事件
        let boundary;
        while (!aborted && (boundary = /\r?\n\r?\n/.exec(buf))) {
          const raw = buf.slice(0, boundary.index);
          buf = buf.slice(boundary.index + boundary[0].length);
          parseEvent(raw, handlers);
        }
      }
      if (!aborted) handlers.onError && handlers.onError(new Error("SSE disconnected"));
    } catch (err) {
      if (aborted) return;
      handlers.onError && handlers.onError(err);
    }
    // 斷了 → 退避重連
    if (!aborted) {
      retry = Math.min(retry + 1, 6);
      const wait = Math.min(1000 * Math.pow(2, retry - 1), 15000);
      timer = setTimeout(connect, wait + Math.random() * 500);
    }
  }

  function parseEvent(raw, handlers) {
    let event = "message", id = null;
    const lines = [];
    for (const line of raw.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) lines.push(line.slice(5).replace(/^ /, ""));
      else if (line.startsWith("id:")) id = line.slice(3).trim();
      // 忽略 comment(: 開頭)與 retry:
    }
    const data = lines.join("\n");
    if (!data) return;
    let parsed = data;
    try { parsed = JSON.parse(data); } catch (_) { /* 非 JSON 原樣傳 */ }
    handlers.onEvent && handlers.onEvent({ event, data: parsed, id });
  }

  connect();
  return function stop() { aborted = true; clearTimeout(timer); if (controller) controller.abort(); };
}
