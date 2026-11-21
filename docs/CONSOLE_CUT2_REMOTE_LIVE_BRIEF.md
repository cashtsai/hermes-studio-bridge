# 交辦單：Console 刀② — 遠端 scope 即時化（10s 輪詢 → 受限 SSE）

> 2026-11-21。完整脈絡,不預設你看過對話。接棒就做,與現況不符以程式碼為準並回報。
> 這是「Console 商業化四刀」的第二刀;刀①③(治頭暈的穩定渲染)已在
> `fix/console-stable-list` 分支完成並測過。本刀**獨立於刀①③**,但也動 `app.js`,
> 若與刀①③ 同時進行請先 rebase 其上,避免撞 `loadSessions`/事件層。

## 機主要什麼（原話）
> 「雖然是希望遠端可以直接登,不過因為是網頁介面...該有的功能應該要到位。」
> 遠端（借來的電腦 / console-remote scope）現在**每 10 秒才輪詢一次**才更新清單,
> 比本機（SSE 即時）明顯慢、不像手機。要讓遠端也有「即時感」。

## 現況（已查證,接線即可，不要重查）
- **本機 owner scope**:全域 SSE `/app/v2/events?follow=true&since_seq=…`,
  `app.js` 的 `sseStream`（`sse.js`）即時推。清單事件走 `handleListEvent`。
- **遠端 console-remote scope**:**沒有**全域事件流可用 → `app.js:~2113`
  `setInterval(loadSessions, 10000)` + `visibilitychange` 重抓。這就是「慢」的來源。
- 為什麼遠端沒接 SSE:全域 `/app/v2/events` **不在**遠端白名單 `_CONSOLE_GET`
  （`bridge.py:819`）內,且它會吐**全部** session 的事件 —— 遠端 token 的前提是
  「人不一定在可信電腦前」,不能把未授權 session 的內容/標題交出去。
- 遠端授權模型(`bridge.py:796-958`):
  - `CONSOLE_REMOTE_SCOPE = "console-remote"`,有**有效期**（一般 device token 沒有）。
  - 統一關卡 `_console_remote_scope_guard`（`bridge.py:1310`）:有效期 + **路徑白名單**。
  - `_console_remote_list()`（`bridge.py:901`）已經在做「把清單過濾成遠端可見的子集」
    （按 `_console_remote_file_ok` 的 workdir 白名單）。**這就是本刀要複用的授權投影。**

## 要做的
### bridge 端：一條「遠端專用的全域摘要 SSE」
新增端點（例：`GET /app/v2/console-remote/summary?follow=true&since_seq=…`）:
- 複用既有事件匯流排（P2 `/app/v2/events` 那套全域訂閱,`bridge.py:3045+`）,
  但**投影成遠端可見的縮小 payload**:只給清單需要的欄位（session id / status /
  未讀游標 / preview 摘要），**不給** chat payload、檔案內容、未授權 session。
- 過濾用**既有的** `_console_remote_list()` 授權邏輯（同一把尺,不要另寫一套白名單）。
  一個事件若屬於遠端看不到的 session → 根本不發。
- 套 `_console_remote_scope_guard` 的有效期檢查;把新路徑**加進白名單**
  （`_CONSOLE_GET` 或等價的 remote-allow 集合,`bridge.py:819`）。白名單漏加只會
  壞功能、不漏權限 —— 但記得加,否則 guard 會擋掉自己。
- **硬約束**:owner 的全域 `/app/v2/events` **維持 owner-only**,不要為了省事把它放進
  遠端白名單（那會把全部 session 交出去,正是這條路最怕的事）。

### app 端：遠端改吃 SSE，拔掉 10s 輪詢
- `app.js` 連線路徑分流（`~2110-2128`）:remote scope 時,不要 `setInterval`,
  改訂閱上面的 summary SSE,事件進**同一個** `handleListEvent`/`applyListEvent`
  （刀①③ 已讓這條路就地更新單列、不重排、不重建 —— 直接受惠）。
- 保留 `visibilitychange` 與斷線 fallback:SSE 斷掉時退回一次 `loadSessions()` 補狀態,
  但不要退回常駐輪詢。
- borrowed/console-remote 仍**唯讀**:這條只是「更快看到狀態」,不新增任何可寫動作。

## 2026-11-21 精確安全缺口(實查，照這個做就安全)
- roster SSE 端點**已存在**:`GET /app/v2/sessions/roster/events`(`bridge.py:22640`
  `v2_sessions_roster_events`),poll `_roster_build` → diff → 推 `sessions.changed`
  {upserted, removed}。transient、不落 event_log。**這就是清單即時流的現成骨架。**
- **但它沒套遠端過濾**:`_console_remote_list`(遠端授權投影)**只在
  `/app/v2/sessions` 的 22281 被呼叫**,roster SSE 完全沒有。直接把遠端接上去 =
  **把全部未授權 session 串給借用裝置**(session 外洩)。這是這條的頭號風險。
- **安全修法(最小、對稱)**:在 `v2_sessions_roster_events` 的 gen 裡,當
  `request.state.console_remote is True` 時,對每輪 `_roster_build` 產出的 rows
  **套用 `_console_remote_list`**(跟 `/app/v2/sessions` 同一把尺、同樣每輪套),
  再 diff/推。等於把輪詢既有的過濾原封搬到串流。
- 把 `/app/v2/sessions/roster/events` 加進遠端白名單(`_CONSOLE_GET`,`bridge.py:819`)
  —— 現在不在白名單(fail-closed),所以遠端目前根本打不到它(這也是為何沒出事)。
- app.js:遠端分流(`app.js:2315` 的 `setInterval(loadSessions,10000)`)改訂閱
  roster SSE,事件進 handleListEvent;斷線退回一次 loadSessions,不退回常駐輪詢。
- **必測安全性質**:一個 workdir 不在白名單的 session,其列**不得**出現在遠端
  roster SSE 的輸出(仿 `test_console_remote_scope.py`)。這條測試是這刀的門檻。

## 驗收
- 遠端 scope 下,某 session 狀態/預覽變動 → 清單**秒級**更新（非 10s）。
- 遠端**看不到**未授權 session 的任何事件（拿一個 workdir 不在白名單的 session 驗:
  它的事件完全不應出現在 summary 流裡）。
- owner `/app/v2/events` 行為不變;遠端打它仍 403。
- 新增 bridge 測試:summary 流只含授權 session、縮小 payload 不含 chat body;
  仿 `tests/test_console_remote_scope.py` / `test_console_remote_usable.py` 的既有風格。
- 回報:branch 名 + commit hash + 改檔清單 + 測試結果。**只 commit+push feature 分支**
  （本 repo CLAUDE.md 紅線:不准合 main、不准重啟 ai.studio.hermes-bridge）。

## 刀④（backlog，排最後，不卡本刀）
終端機全螢幕 TUI、web push、長列表虛擬化、Console 直接吃共享 `PocketThemeSpec`
（現為手抄 CIS 值,見 `style.css:7-10` 註）。這些是 nice-to-have,等①②③ 落地再議。
