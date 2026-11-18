# Console W3: 狀態與事件驗收

日期：2026-11-18。分支：`feat/console-w3-state`。獨立基底：`28cbca6`。

本次只修改指定工作樹並 commit/push。未合併 main、未啟動 bridge、未重啟服務、
未寫入 production DB。以下圖片全部來自隔離 localhost fixture、暫存 Chrome profile
與合成資料，不包含正式對話或憑證。

## 變更

- `message.upsert` 直接更新最新訊息預覽與活動排序；App/TG/cron 使用相同路徑。
  工具/思考內容沿用 `visibleReplyText` / `replySegments` 收納規則，不進預覽。
- `read_cursor.update` 使用伺服器的 global cursor，只進不退；以「未讀」標記呈現，
  不宣稱無法由有限快取計算的未讀數量。較舊訊息改稿不會取代較新預覽。
- 上述事件不觸發 800ms 全量列表刷新；其他事件保留既有刷新路徑。
  事件序號守門仍生效，重連重播不會將預覽倒退；不改動對話卡片或歷史。
- 頁面記憶體只保留每個 session 的最新預覽、最新 incoming 訊息及已讀高水位。
  不寫 localStorage；登出即清空。HTTP 列表快照不覆蓋這些即時資料。
- 狀態列完整呈現 `lock_message`、`queue_depth`、`usage_limit_reset_at`。
  較慢的列表回應不能覆蓋較新的 status event。
- 補齊 waiting_approval / needs_input / queued / usage_limited / error / dead /
  completed / dispatching 等狀態樣式；needs_input 優先排列。
- 活動列顯示延遲 1500ms、隱藏延遲 300ms。重複 begin/status 不重設門檻；
  保留 elapsed，切換 session / 回首頁 / 登出不殘留上一條活動。
  Shimmer 只用於真實活動 label，尊重 reduced-motion 與 forced-colors。
- Service worker shell 使用 v15（W4 使用 v14），避免離線退回舊資產。

未修改 `renderApproval`、`renderAwaiting` 或 `approval.resolved` 的處理。

## 算圖

畫面為實際 console HTML/CSS/JS，1366x768。逐態以 session SSE 送入 fixture。

| 狀態 | 圖片 |
| --- | --- |
| 桌面佔用與 3 則排隊 | [locked.png](locked.png) |
| 閒置 | [idle.png](idle.png) |
| 執行中、真實活動與 elapsed | [running.png](running.png) |
| 等待核准 | [waiting_approval.png](waiting_approval.png) |
| 需要你 | [needs_input.png](needs_input.png) |
| 需要你（深色） | [needs-input-dark.png](needs-input-dark.png) |
| 排隊數量 | [queued.png](queued.png) |
| 用量受限與重置時間 | [usage_limited.png](usage_limited.png) |
| 錯誤 | [error.png](error.png) |
| 已停止 | [dead.png](dead.png) |
| 已完成 | [completed.png](completed.png) |
| 派送中 | [dispatching.png](dispatching.png) |
| 最新預覽與未讀 | [preview-unread.png](preview-unread.png) |

重置時間以瀏覽器當地時區呈現；fixture 的日期為固定測試資料。

## 測試

```sh
PYTHON=~/apps/hermes-agent/runtime/venv/bin/python scripts/run-tests.sh
NODE_PATH=/private/tmp/console-w34-qa/node_modules node --test \
  tests/browser/console-state.cjs tests/browser/console-workstation.cjs
node --test tests/console_delivery_sort.test.cjs
```

- 新增狀態瀏覽器測試 4/4 通過：完整狀態文案/樣式、預覽/跨裝置游標、
  防閃爍/elapsed/reduced-motion、較舊 HTTP 快照競態。
- 加上既有 workstation：6 通過、0 失敗、1 跳過。跳過的是需明確啟用的
  standalone PWA 安裝情境，不假裝已驗過。
- 排序/送達 Node 測試 8/8 通過，新增 needs_input / dispatching 優先層級與鏡射活動排序。
- 完整逐檔回歸初跑：216 檔，214 通過、2 失敗。`test_persona_description`
  是交辦單已註明的既有失敗；另一項 `test_ratchet_baseline` 抓到新預覽截字規則，
  已改為字素分段與省略號，單獨重跑 1/1 通過，未修改 baseline。
- 最後一次瀏覽器回歸仍為 6 通過、0 失敗、1 跳過；新增 emoji ZWJ 與組合字
  邊界斷言也通過。1500ms / 300ms 測試使用凍結的虛擬時鐘，避免即時鐘造成邊界誤差。
- 既有 handoff + leave：10 通過、2 失敗。`lost HTTP acknowledgement` 在
  `console-handoff.cjs:288` 等「確認送達」逾時；W4 已在未修改基底確認同樣失敗。
  `console-leave.cjs:87` 等離線文字逾時，也在 `28cbca6` 的隔離副本重現。
  本次不改這兩項非 W3 範圍的既有案例。
- `node --check static/console/app.js`、`git diff --check` 通過。

原始測試輸出（此機暫存）：`/private/tmp/w3-regression.log`、
`/private/tmp/w3-ratchet.log`、`/private/tmp/w3-browser-final.log`、
`/private/tmp/w3-handoff.log`、`/private/tmp/w3-leave-baseline.log`。

## 改檔

- `static/console/app.js`：事件與狀態行為。
- `static/console/style.css`：狀態、長文與動態效果。
- `static/console/sw.js`：離線 shell 版本。
- `tests/browser/console-state.cjs`：真實渲染路徑驗收與算圖。
- `tests/console_delivery_sort.test.cjs`：既有 harness 加入活動快取與新增排序案例。
- `docs/console-w3-preview/`：本報告與 13 張算圖。

## 範圍與整合提醒

- 未改訂 roster lane；此為交辦單的選做項目，其他事件仍使用既有 debounce。
- `console-remote` 依既有權限不能讀全域 event log，仍走範圍受限的列表輪詢。
  這次沒有為了加速而擴大借用瀏覽器的資料權限。
- 本次只消費伺服器已讀游標，未新增「點開即寫回已讀」行為。
- W4/W3 為預先建立的獨立分支，未互相合併。整合 `renderSessions` 時要同時保留
  W4 的原生 button/span 與焦點處理，以及 W3 的 `sessionPreview(s)` / `unreadMark(s)`。
  保留 W4 的行動版、安全區與 toast CSS，也保留這裡新增的狀態 CSS；SW 採 v15。
- 部署與正式服務驗收由後續授權處理；本次刻意未對 production 做 smoke input。
