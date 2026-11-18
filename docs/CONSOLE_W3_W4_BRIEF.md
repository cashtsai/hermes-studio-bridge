# Console UIUX 升級:W4 與 W3 交辦單

> 2026-11-18。這份是給接手的 agent 的**完整**交辦單 —— 不預設你看過任何
> 對話,每條結論都帶 file:line,照著做即可。

Console(web)UIUX 升級:W4 與 W3 兩波。請照順序做,做完一波就 commit+push 再做下一波。

## 紅線(repo 根目錄 CLAUDE.md,務必先讀)
`~/apps/hermes-openwebui-bridge` 跑的是 production 服務 ai.studio.hermes-bridge。
**只能 commit+push 到指定的 feature branch。不准 merge main、不准 launchctl 重啟
任何服務、不准對 production DB 非唯讀寫入、不要跑 bridge 服務。**
工作樹我已經開好,直接進去做,不要自己在主工作樹上改。

## 背景:這些結論是稽核出來的,不用重查
console 的病是「bridge 早做好、消費端落後」—— 過去幾個月 app 吃下了範式,
console 只拿到渲染層改版。以下每條都查證到 file:line。

==================== W4(先做)====================
工作樹 `~/apps/bridge-console-w4`,分支 `feat/console-w4-mobile`。
主題:手機瀏覽器可用 + 鍵盤/焦點 + 錯誤可見。幾乎只動 style.css / index.html,
與其他波衝突最小。

1. 手機版面(style.css:377-380):760px 斷點只做了「#main 改 column + #sidebar
   寬 100% + max-height:40vh」→ 開著對話時 session 列表永久吃掉 40% 畫面高度。
   CSS 註解自己承認「第一刀先簡單處理,之後再做抽屜」。改成:進了對話就收起
   側欄 + 一顆返回鈕(`#back-home` 已存在於 index.html:70)。另一個 760px 區塊
   在 style.css:591-598,只在 `#main.at-home` 時隱藏側欄,要一起對齊。
2. `#app{height:100vh}`(style.css:136)→ 改 100dvh。行動版 Safari 的網址列會把
   底部吃掉,composer 被推出畫面。
3. 全檔**零** `env(safe-area-inset-*)`,但 index.html:5 的 viewport 有
   `viewport-fit=cover` → iPhone 上輸入框壓在 home indicator 下面。composer
   (style.css:301 附近)補 `padding-bottom:max(12px, env(safe-area-inset-bottom))`,
   頂欄同理處理瀏海。
4. 觸控尺寸:`.icon-btn` 無 padding 無 min-size、font-size 16px → 實測約 19px
   (style.css:154,↻ ＋ ← 主題鈕全是)。`.ap-btn` 約 27px(style.css:401-402)。
   `.sess` 約 38px(style.css:527)。全檔只有 `#new-reply` 達 44px。
   Apple 建議 44、WCAG 2.5.8 下限 24 —— 把 icon 鈕與審批鈕拉到 44。
5. toast(style.css:364)`position:fixed;bottom:20px` **蓋住輸入框**;而且
   `#toast`(index.html:203)沒有 role/aria-live,螢幕閱讀器完全不念;3 秒自動
   消失、無關閉鈕、無歷史。走 toast 的失敗有:審批失敗(app.js:731)、中斷失敗
   (:1311)、開新工作失敗(:1556)、更早訊息失敗(:613)、下載(:1012)、複製(:1025)。
   **審批按錯的錯誤訊息跟「已送出決定」用同一個 3 秒泡泡、同樣式**(:729 vs :731)。
   → 加 role="status"、讓開 composer、失敗類不自動消且給關閉鈕。
6. **確認的靜默失敗**:filebrowser.js:30-47,401/404 有寫訊息,但
   `if (!r.ok) throw new Error("HTTP " + r.status)` 這條沒寫任何訊息,而
   catch 的註解還寫著「訊息已寫進 list」—— 對 500/斷網是假的。
   bridge 500、relay 502、斷網 → 檔案面板一片空白,一個字都沒有。補訊息。
7. 鍵盤與焦點三處不一致:
   · `j`/`k`(app.js:1505-1506)走 `moveSelection`(:1447-1453)用的是
     `state.sessions` 原始 API 順序,但側欄是 provider 分組 + sticky 排序後
     渲染的(renderSessions :310-333 + sortSessions :292-299,而 sortSessions
     排的是 byProv 子陣列不是 state.sessions)→ **按 j 跳到的不是你看到的下一列**。
   · `openSession`(:465-501)**不聚焦輸入框** → ⌘K 選完 session 還要用滑鼠點
     進 textarea。筆電流程上最扎的一刀。
   · `.sess` 是 `<div>` + click(:326、:335-336),沒有 role/tabindex →
     整個主側欄 Tab 不到、Enter 不能啟動。(首頁的 `.work-row` 反而是正規
     `<button>`,:397 —— 照它改。)
   · `closePalette`(:1520)只加 hidden,不還原先前焦點 → 焦點掉到 body。

==================== W3(後做)====================
工作樹 `~/apps/bridge-console-w3`,分支 `feat/console-w3-state`。
主題:狀態語言。注意另有一刀正在 `~/apps/bridge-console-p0` 改 app.js 的審批
區塊與 global SSE 的 `approval.resolved` 這一個 case —— **你不要碰審批渲染,
也不要動 approval.resolved**,其餘事件型別才是你的範圍。

1. **全域事件流的 `data.type` 連讀都沒讀**(app.js:1679-1688):
   ```
   onEvent: ({data}) => {
     if (!Number.isSafeInteger(data?.seq) || data.seq <= listCursor) return;
     listCursor = data.seq; scheduleListRefresh();   // 800ms debounce → 全量重抓
   }
   ```
   bridge 的分類表在 bridge.py:3088-3106(EVENT_PERSISTED / EVENT_PERSIST_ONLY)。
   十種事件全塌縮成「800 毫秒後重抓列表」。優先接這兩條(其餘可留 debounce):
   · `read_cursor.update`(bridge.py:3092,多裝置未讀一致性)
   · `message.upsert`(bridge.py:3089,三來源訊息鏡射 app/TG/cron —— 側欄副標
     現在顯示 workdir(app.js:329)而不是訊息預覽,TG/cron 進來的訊息在列表上
     完全看不出來)
2. **session.status 的欄位被硬覆蓋**,這三個都是機主親自要求補的人話:
   bridge 給的是 `{busy,mode,prompt,phase,queue_depth,label,locked,
   last_turn_outcome,lock_reason,lock_message,busy_since,blocked}`
   (carddigest.py:1378-1397)+ `usage_limit_reset_at`。
   `updateStatusChip`(app.js:1154-1174)只用 phase/locked/label/busy_since:
   · `lock_message`(carddigest.py:1374-1377,例「已被桌面版 Codex 佔用,3 則
     訊息已排隊,桌面釋放後自動送出」)被 app.js:1157 **硬覆蓋成「🔒 被佔用」**,
     而且 :1167 的 `working = !locked && …` 把 workbar 關掉,所以那句話連第二個
     位置都沒有。那段註解記錄了 2026-10-08 的根因:使用者看到「送不出去」會
     瘋狂重按。
   · `usage_limit_reset_at` → 現在只顯示「用量受限」,**重置時間看不到**。
   · `queue_depth`(carddigest.py:1379)→ 現在只有「已排入佇列…」,沒有數量。
3. **roster 六態 console 只呈現 3 態**。bridge:`_ROSTER_ACTIVITY_BY_STATUS`
   (bridge.py:22383-22395)、`_roster_activity()`(:22489-22507)、瘦 facade
   (:22509-22529,含 `last_turn_summary` —— console 從來沒取用過)。
   最痛的一條:**`needs_input` 沒有 `.st-waiting_approval` CSS**,顏色跟「閒置」
   一模一樣(style.css:165-166 只上色 8 個,PHASE_LABEL 有 12 個 key)。
   「最需要你的那一態」在側欄視覺上零權重。
   **最小刀**:補 `.st-waiting_approval` / `.st-queued` / `.st-usage_limited` /
   `.st-error` / `.st-dead` / `.st-completed` / `.st-dispatching` 七個 class。
   行有餘力再改訂 roster lane(`/app/v2/sessions/roster/events`,bridge.py:22547,
   送 upserted/removed 差分)取代 800ms 全量重抓。
4. **workbar 沒有 1.5 秒門檻**(app.js:1177-1192):`syncBusyBar` 立刻 toggle,
   每個短回合都閃一下;而且 `turn:begin`(:793-795)會先翻一次、`session.status`
   再翻一次 → 可能閃兩下。參考值:顯示延遲 1.5s / 隱藏延遲 0.3s。
   elapsed 計時已經有了(:1183-1191),保留。順手把 shimmer 打在真實活動文字上
   (現在只有一顆與內容無關的通用轉圈,style.css:292-294)。

## 驗收:不准只看程式碼
這個 repo 的教訓是「靜默失敗是最大病原」「驗行為不驗存活」。
console 的頁面可以離線算圖驗證,做法:寫一頁 fixture HTML 餵假資料進渲染路徑,
再用 headless Chrome 算圖:
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --headless \
    --disable-gpu --hide-scrollbars --screenshot=out.png --window-size=390,844 <file-uri>
W4 要出**手機寬度(390x844)**與桌面兩種算圖,放 `docs/console-w4-preview/`;
W3 的狀態列各態算圖放 `docs/console-w3-preview/`。我要看圖。

## 守門
跑既有測試:`PYTHON=~/apps/hermes-agent/runtime/venv/bin/python scripts/run-tests.sh`
(逐檔跑,這個 repo 不能直接 `pytest tests/`)。注意 `test_persona_description`
在乾淨的 main 上本來就紅,與你無關,不要去修它。
每波新增測試釘住行為(例:j/k 的順序與畫面一致、lock_message 不被覆蓋、
needs_input 有獨立 class)。

完成一波就 commit + push 該分支並回報:commit hash、改檔清單、算圖路徑、測試結果。
兩波都做完再一起告訴我。遇到與上述描述不符的現況,以程式碼為準並回報差異。
