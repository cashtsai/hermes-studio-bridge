# Pocket Console Web：接力與閱讀驗收

適用於 CC、CX 維護 Web Console。這是人工選用的瀏覽器走查，不是 iOS 規範或發版門檻。借用 [Taste Skill](https://github.com/Leonxlnx/taste-skill) 的做法：每次選一項工作、用真瀏覽器操作、保留變更前後的證據。不安裝 plugin、Stop hook 或更動品牌規則。

## 第一輪：接回正在跑的 CX

問題：已登入的人能否找到剛在手機使用的同一條對話，讀上一段時不被串流拉走，並分辨訊息有沒有送到？本機情境用同時存在的 CX、CC 工作用來辨認，沒有模擬掃碼配對。只有 CX 開啟並接收串流。

以 `75686b3` 的 Console 靜態資產、Chrome 156 一般分頁、80 則合成歷史重播。2026-11-10 實測：

| 已登入後的操作 | 1366×768 | 820×1180 | 390×844 |
| --- | --- | --- | --- |
| 從最近工作點進指定 CX | 1 次 | 1 次 | 1 次 |
| 頁面無水平溢出、輸入及送出可點 | 通過 | 通過 | 通過 |
| 往上滑 320px，新卡進來後原文位移 | 0px | 0px | 0px |
| 往上滑 48px，新卡進來後原文位移（容許 8px） | **−154px** | **−154px** | **−154px** |
| 離線未送、已排隊、主機已收到、送達不明提示可見 | 通過 | 通過 | 通過 |
| 點「確認送達」讀取卡片、無新增 POST | 通過 | 通過 | 通過 |

**優先修捲動。** 目前 `static/console/app.js` 的 `renderCards` 以距底部不足 80px 決定跟隨，48px 因此被當成仍在底部。修正應辨別「正在跟隨」與「使用者已上滑」：停在底部仍能看直播，上滑後即使只離開一點也保留原文，回到底部再恢復。修後同跑上表，並補驗回底會恢復跟隨、載入更早訊息時位置不變。

**區分請求與執行。** 測試主機收完訊息便切斷連線、不給收據。該次單擊在三個尺寸分別留下 5、4、4 個 HTTP POST，但同一尺寸內每個 POST 的內容與 `client_id` 都相同。這是斷線注入下的觀察，不是正式流量或模型執行次數。之後按查詢只有 GET。另跑 bridge 現有的兩個 `same_client_id` 回歸均通過。這些證據不等同外網斷線端到端驗收。

## 重跑與交接

需要能載入 Playwright 的 Node 環境（若安裝在工具目錄，用 `NODE_PATH` 指定其 `node_modules`）與已安裝的 Chrome。從 repo 根目錄執行：

```sh
POCKET_QA_READING_PILOT=1 POCKET_QA_SCREENSHOTS="$(mktemp -d -t pocket-console-qa)" \
  node --test tests/browser/console-reading-pilot.cjs tests/browser/console-workstation.cjs
```

要保留結果可將 `POCKET_QA_SCREENSHOTS` 改為指定的本機資料夾。閱覽 `<寬度>.json`、`<寬度>-read-48-before.png` 與 `-after.png`；送達狀態另有獨立截圖。任一尺寸超過位移容差會回傳非零；目前三個尺寸都應報同一項失敗。未指定試點旗標時，這三項不納入平日測試。

新的畫面只選一個主要操作，先寫預期，再重播現有合成 fixture；PR 列出版本、尺寸、實測差異和一組前後圖。保留 Pocket 的元件、字體、品牌色和手勢。兩個 UI PR 後再判斷是否納入例行檢查。不把憑證、私人 session 或真實工作截圖放進交接。

這次尺寸走查使用桌面 Chrome 與滾輪；不代表 ChromeOS 觸控板、手機軟鍵盤、PWA 安裝、跨帳號配對或三端同步已驗收。正式放行仍看 [W08 發版演練](CONSOLE_W08_RELEASE.md)。
