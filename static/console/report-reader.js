"use strict";
// Reports are fetched on demand, never polled or persisted in browser storage.
window.PocketReports = (() => {
  const $ = id => document.getElementById(id);
  const LIMIT = 512 * 1024;
  let deps, controller, generation = 0, items = [], selected, source = "", endpoint = "";
  const dialog = () => $("report-reader");
  function clear() {
    ++generation; controller?.abort(); controller = null;
    items = []; selected = null; source = ""; endpoint = "";
    $("report-content").replaceChildren(); $("report-toc").replaceChildren();
    $("report-select").replaceChildren(); $("report-download").disabled = true;
    $("report-status").textContent = ""; $("report-title").textContent = "對話報告";
  }
  function close() { if (dialog().open) dialog().close(); clear(); }
  function refKey(raw) {
    let value = String(raw || "");
    if (value.startsWith("file://")) {
      try { const url = new URL(value); if (url.hostname && url.hostname !== "localhost") return "";
        value = decodeURIComponent(url.pathname); } catch (_) { return ""; }
    }
    return value.replace(/:\d+(?::\d+)?$/, "");
  }
  function render(raw = false) {
    $("report-preview").setAttribute("aria-pressed", String(!raw));
    $("report-source").setAttribute("aria-pressed", String(raw));
    const content = $("report-content"), toc = $("report-toc");
    content.replaceChildren(); toc.replaceChildren(); content.scrollTop = 0;
    if (raw) { const pre = document.createElement("pre"); pre.className = "report-raw"; pre.textContent = source; content.append(pre); return; }
    content.classList.add("md");
    content.innerHTML = window.PocketMarkdown.render(source);
    content.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((heading, i) => {
      heading.id = "report-heading-" + i;
      const button = document.createElement("button");
      button.type = "button"; button.textContent = heading.textContent;
      button.addEventListener("click", () => { heading.scrollIntoView({block:"start"}); content.focus({preventScroll:true}); });
      toc.append(button);
    });
  }
  async function boundedText(response) {
    if (Number(response.headers.get("Content-Length")) > LIMIT) { await response.body?.cancel(); throw new Error("報告超過預覽上限，請下載閱讀。"); }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let bytes = 0, text = "";
    try {
      for (;;) {
        const {done, value} = await reader.read(); if (done) break;
        bytes += value.byteLength;
        if (bytes > LIMIT) { await reader.cancel(); throw new Error("報告超過預覽上限，請下載閱讀。"); }
        text += decoder.decode(value, {stream:true});
      }
      return text + decoder.decode();
    } finally { reader.releaseLock(); }
  }
  function statusError(response) {
    return new Error(response.status === 403 ? "此對話的讀取權限已失效，請重新開啟對話。" :
      response.status === 404 ? "報告尚未封存或已不在此對話保留的歷史內。" : "報告載入失敗，請稍後重試。");
  }
  async function choose(id) {
    controller?.abort(); controller = new AbortController(); const seq = ++generation;
    selected = items.find(item => item.media_id === id); source = "";
    $("report-download").disabled = !selected; render();
    if (!selected) return;
    $("report-select").value = id; $("report-title").textContent = selected.title || selected.filename;
    $("report-status").textContent = "載入報告…";
    try {
      const response = await deps.api(endpoint + "?media_id=" + encodeURIComponent(id), {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw statusError(response);
      const text = await boundedText(response);
      if (seq !== generation || !dialog().open) return;
      source = text; render(); $("report-status").textContent = "";
    } catch (error) { if (seq === generation && error.name !== "AbortError") $("report-status").textContent = error.message; }
  }
  async function open(session, ref = "") {
    if (!session) return;
    close(); endpoint = "/app/v2/sessions/" + encodeURIComponent(session) + "/reports";
    dialog().showModal(); $("report-close").focus();
    controller = new AbortController(); const seq = ++generation;
    $("report-status").textContent = "載入報告…";
    try {
      const response = await deps.api(endpoint, {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw statusError(response);
      const data = await response.json();
      if (seq !== generation || !dialog().open) return;
      items = (data.items || []).filter(item => /^med_[a-f0-9]{32}$/.test(item.media_id));
      for (const item of items) {
        const option = document.createElement("option"); option.value = item.media_id; option.textContent = item.title || item.filename;
        $("report-select").append(option);
      }
      const item = ref ? items.find(item => (item.refs || []).some(value => refKey(value) === refKey(ref))) : items[items.length - 1];
      if (!item) { $("report-select").value = ""; $("report-status").textContent = ref
        ? "這份報告尚未封存或已不在保留歷史內，請在原對話要求重新附上報告。" : "此對話目前沒有已封存的 Markdown 報告。"; return; }
      await choose(item.media_id);
    } catch (error) { if (seq === generation && error.name !== "AbortError") $("report-status").textContent = error.message; }
  }
  async function download() {
    if (!selected || !controller) return;
    const seq = generation, name = selected.filename;
    $("report-download").disabled = true;
    try {
      // Recheck the grant on every download; never reuse the general media cache.
      const response = await deps.api(endpoint + "?media_id=" + encodeURIComponent(selected.media_id),
        {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw statusError(response);
      const blob = await response.blob();
      if (seq !== generation || !dialog().open) return;
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { if (seq === generation && error.name !== "AbortError") $("report-status").textContent = error.message; }
    finally { if (seq === generation) $("report-download").disabled = !selected; }
  }
  function init(options) {
    deps = options;
    dialog().addEventListener("cancel", e => { e.preventDefault(); close(); });
    dialog().addEventListener("close", () => { if (!dialog().open) clear(); });
    $("report-close").addEventListener("click", close);
    $("report-select").addEventListener("change", e => void choose(e.target.value));
    $("report-preview").addEventListener("click", () => render());
    $("report-source").addEventListener("click", () => render(true));
    $("report-download").addEventListener("click", () => void download());
    $("report-content").addEventListener("click", e => {
      const button = e.target.closest(".md-copy");
      if (button) { void deps.copy(button.closest(".md-code").querySelector("code").textContent, "已複製程式碼"); return; }
      const report = e.target.closest("[data-report-ref]");
      if (report) {
        const item = items.find(item => (item.refs || []).some(ref => refKey(ref) === refKey(report.dataset.reportRef)));
        if (item) void choose(item.media_id); else $("report-status").textContent = "此連結不是已交付到這個對話的報告。";
      }
      const artifact = e.target.closest("[data-dl]");
      if (artifact) $("report-status").textContent = "請回到原對話開啟其他附件。";
    });
  }
  return {init, open, close};
})();
