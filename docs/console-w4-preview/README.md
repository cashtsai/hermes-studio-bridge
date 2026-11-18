# W4 驗收

分支：feat/console-w4-mobile。基準：710ee13。只推 feature branch，未部署。

## 變更

- 760px 以下以工作接力頁作為入口，進對話隱藏側欄，返回鈕回工作接力。
- 100dvh、safe-area 與 VisualViewport 高度，避免軟鍵盤／網址列遮住 composer。
- 圖示、審批、session 列至少 44px；側欄用原生 button。
- 通知改成工作區上方的非遮罩區，具 status live region；失敗須手動關閉。
  後來的成功通知不會清掉尚未關閉的失敗。
- 檔案面板 500、502、網路失敗、401、404 都呈現錯誤及重試。
- j/k 讀實際分組排序，保留側欄焦點以便連續操作；明確開啟對話時桌面
  聚焦 composer，手機不強制彈鍵盤；命令面板 Escape 還原焦點、Tab 不逸出。
- Service worker shell v14，避免離線 fallback 留著舊資產。

## 圖片

由 tests/browser/console-mobile.cjs 餵假資料進正式 index.html/app.js/CSS。
不是另畫的 mockup，不啟動 Bridge，不讀正式對話。

- mobile-390x844.png：手機對話。
- mobile-keyboard-390x520.png：可視高度縮減後仍可用的 composer。
- mobile-error-390x844.png：失敗提示不蓋輸入框。
- mobile-home-390x844.png：返回工作列表。
- desktop-1366x768.png：桌面分組與焦點。
- workstation-browser.png：既有桌面回歸截圖。

## 測試結果

- 新增 3 項瀏覽器測試通過，既有 workstation 2 項通過；standalone PWA
  安裝模式未設定 POCKET_QA_INSTALL_PWA，因此 1 項跳過。
- run-tests.sh：216 檔第一輪 214 通過、2 失敗。IME source guard
  test_console_cjk 已調整鍵盤區塊順序並單獨重跑通過。
  僅剩交辦單已列的既有 test_persona_description 失敗。
- 額外 console-handoff 瀏覽器回歸 10/11 通過；lost HTTP acknowledgement
  在「確認送達」等待逾時，同一測試在未改的 710ee13 暫存基準也失敗。
- git diff --check、JavaScript syntax check 通過。

重跑：

    NODE_PATH=/private/tmp/console-w34-qa/node_modules POCKET_QA_SCREENSHOTS=docs/console-w4-preview node --test tests/browser/console-mobile.cjs tests/browser/console-workstation.cjs
    PYTHON=~/apps/hermes-agent/runtime/venv/bin/python scripts/run-tests.sh

Chrome headless 實際算圖；鍵盤以縮小 viewport 模擬。實機 Safari 的瀏海及
軟鍵盤仍需手機驗收，沒有宣稱這項已完成。未變更 API 或 production DB。
