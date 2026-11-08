# 借用電腦：網頁接力第一批

2026-11-08，W01 入口診斷與 W02 安全離席。此紀錄不是正式環境驗收。

## 入口現況

由開發 Mac 不帶憑證探測：私網 HTTPS 控制台回 401；主機目前配置的公網 relay 回 bridge 的 403 `local only`。公網路由有通，但不是可掃碼的登入頁；這項證據不能代替在外部 Chromebook 的實測。主機專屬 relay 與「任何人打開同一產品網址、掃碼後路由到自己的主機」仍是兩件事。

這批不開任何正式旗標。測試主機啟用 QR 模式時，遠端 GET 顯示 QR，短碼 POST 不能代替手機核准；本機/私網的既有配對流程不變。只有 web-first、未開 QR 模式的指定 relay 仍可用原短碼頁，但換出來的瀏覽器授權也是受限的。

## 離席與授權

- 權限由伺服器在鑄一次性碼時設定。遠端瀏覽器換得有期限的 `console-remote` token，不綁為一般帳號裝置；瀏覽器自報平台和名稱都不能改變它。預設授權 30 分鐘；另有 15 分鐘無已驗證請求的閒置上限（不是使用者鍵鼠閒置偵測）。
- 遠端 token 只放在頁籤的 sessionStorage，不從舊版 localStorage 恢復；授權到期最終由伺服器裁決，關閉視窗不是撤銷保證。已開啟的遠端 SSE 在下個資料塊前重驗授權。兩條終端 WebSocket 路徑均拒絕遠端瀏覽器。
- `POST /console/logout` 只撤銷 Authorization header 所持的 Console token，不接受要撤銷的目標 ID，不撤 owner/手機。成功或已被撤銷時清除本來源的 Console cookies。瀏覽器先鎖住畫面與其他開著的分頁；收到伺服器確認才完成。網路失敗可重試，不停止 agent。
- Service worker 只快取指定的靜態檔，不保存登入頁、對話或帶 query 的導覽；更新時移除舊版 Console 快取，保留同來源其他產品的快取。

## 上線前

1. 先在測試主機套用 bridge 與整套 Console 靜態資產，驗證舊 service worker 能更新。只在已審核的測試入口驗收 QR；不要為了測試而開放正式主機所有轉送入口。
2. 先前發過的瀏覽器 token 不會因這次更新而自動被降權。擁有者應從配對裝置清單核對來源，撤銷已辨認的舊瀏覽器授權後重新掃碼；**不可按裝置名稱批量撤銷手機或其他既有裝置**。核對完之前不建議對外開放。
3. 用未連私網的 Chromebook、真手機和測試主機驗證：拒絕/核准、CC 和 Codex 原對話各一來一回、手機撤銷單一瀏覽器、離線重開、第二位使用者重新登入。輪用電腦不要使用 Chrome 同步保存網站授權。不要保存 QR 或 token 到 QA 證據。
4. 以上通過再決定推出範圍。尚未交付跨使用者的固定入口、W03 訊息重送、W04 重連補齊或完整 W05/W06 接力畫面；不要把隔離測試通過當成公網已能上工。

驗證：`pytest -q tests/test_console*.py`。有 Playwright 和 Google Chrome 的環境可另跑 `node --test tests/browser/console-leave.cjs`：使用臨時瀏覽器與 loopback fixture，不接觸正式資料。Python 測真實 bridge 登入與換證處理器；Chrome 測畫面、多分頁、重試及離線快取。
