# Pocket Console：W08 發版演練

**2026-11-08：NO-GO。** 桌面功能與本機接力可測；沒有「同一產品入口、兩個帳號、兩台主機」或外網真手機的通過證據。這個 bridge 的 web vhost 仍辨識單一主機的 relay。不要拿主機專屬書籤、模擬手機 API 或 Chrome 視窗尺寸冒充通用入口和實機。沒有可用的通用入口時，W01 是產品缺口，不是改旗標即可解決。

## 候選版的本機證據

W07–W08 以整合版 `17bf582` 為基底；重測時以實際 checkout 的完整 SHA 建立記錄。主機為 macOS 27.0.1、Chrome 156.0.8078.80。下列是隔離開發測試，不填入外網驗收表：

| 範圍 | 做了什麼 | 結果 |
| --- | --- | --- |
| W07 一般分頁、已安裝的 PWA | 臨時 profile；一般分頁及真正安裝、啟動的獨立視窗均檢查 display-mode。1366×768、80 張歷史卡，同時點得到側欄、最後回覆及多行輸入；中文組字中不送，明確 Ctrl+Enter 只送一次；可看逐檔 diff。測後卸載。 | 通過；不是 ChromeOS 或實體中文鍵盤走查 |
| W07 記住工作 | 既有個人登入才提供，需主動勾選；以當次登入區分，手動深連結優先。登出、重新配對或改借用模式清除；安裝 PWA 不等於個人授權。 | 通過；不儲存對話或草稿 |
| W02–W06、W07 回歸 | 13 個 Chrome 情境（含 30 秒斷線）及 148 個後端相關測試、17 個子測試；含未授權、非 Git、缺目錄、已確認無變更和 Git 失敗。 | 通過 |
| 真 bridge、合成 CC/CX | 手機 API 送 A → Web 看 A 送 B → 手機 API 續問；驗順序、重整、舊授權讀寫和核准均 401；關閉頁面、手機繼續、新掃碼回同一條。 | 通過；不是原生三端實機 |
| 真 Codex app-server | 獨立 thread 經 Web 與手機 API 接續上一個隨機答案，重整後仍可讀，撤銷後舊憑證失效。 | 本候選版通過；Claude Code 實機未驗，上次實測遇到週額度 |

開發重跑（Python 需有 repo 測試依賴，Node 需能載入 Playwright；Chrome PWA 安裝測試需圖形桌面）：

```sh
BRIDGE_TOKEN=test-unit-token python -m pytest -q tests/test_console*.py tests/test_no_private_infra_leak.py
POCKET_TEST_LONG_OUTAGE=1 POCKET_QA_INSTALL_PWA=1 PYTHON=python3 node --test tests/browser/*.cjs tests/integration/*.cjs
```

真 Codex 的選用指令見[整合驗收](CONSOLE_BORROWED_BROWSER.md)。不要對正式 session 跑故障注入。

日常 Web 改動另有[三種尺寸的接力與閱讀驗收](CONSOLE_WEB_DESIGN_QA.md)，不取代下列外網放行程序。

## 外網放行程序

只使用 HTTPS staging、已配對的實體 iPhone、兩個測試帳號及分別歸屬的主機。借用瀏覽器用無痕或乾淨 profile，不登入 Google、不預裝 Pocket、不連私人網路。以同一候選版部署的 bridge、靜態資產、入口及 iOS build 為一批；任一元件更換即記新批次，不混用證據。

| 記錄項 | 操作與判定 |
| --- | --- |
| entry/online-1 到 5 | 每次清除上一次登入，從輸入同一產品網址開始計時；手機掃碼、核准、找到原工作，Web 送出首則並收到主機確認才停錶。記五個實測秒數，至少四次不超過 60 秒。離線另列，不用挑最快的五次。 |
| entry/two-hosts、offline-deny-expire | 兩位使用者從同一網址各進自己的主機，網址不含憑證、不列別人主機。測離線、QR 過期及拒絕；可安全重試、不可退回較高權限。 |
| 各平台 /cc、/codex | ChromeOS Chrome 與安裝的 PWA、Windows Chrome 和 Edge、macOS Chrome **各跑兩條**。原生端建測試 session；手機送 A，Web 看 A 送 B，原生端續回 C。三端對照順序和數量，各做一次核准與拒絕。關 Web 工作仍繼續，重開接到最新卡。PWA 另以實體中文輸入法選字換行。 |
| security/second-user、revoke-tabs | 兩頁籤；先斷網登出、確認鎖定、連線重試。測手機單獨撤銷及逾期，舊授權讀、送、核准均拒絕。按上一頁、離線重開不可見原內容；帳號 B 在同 profile 重新登入不得見 A。手機仍可工作。 |
| continuity/slow-duplicate、30s-gap | 延遲快照，送出後丟成功回應、連按及切工作，不重送也不串台。斷網 30 秒間手機續答，回網後補齊；過期游標冷載、核准競爭只生效一次。 |
| home/30-jobs、scope/direct-api | 準備至少 30 條含同名的工作，兩次操作內找到手機剛用的那條。檢查過期核准；有權的回覆執行一次，無權的讀檔、終端、跨工作操作在 API 層拒絕，UI 不展示。 |
| rollback/staging | **僅在 staging** 撤新入口路由或恢復測試前的設定。若兩個新旗標原先都關，將 `POCKET_CONSOLE_WEB` 和 `POCKET_CONSOLE_QR_LOGIN` 一併還原；只關 QR 而保留 WEB 會暴露短碼頁。不關既有 Console、不清 session DB、不撤手機。封住新入口後依既有流程撤銷測試瀏覽器憑證；驗舊私網 Console、手機登入與原對話仍正常。 |

每列用獨立的 `qa-xxxxxxxx`（隨機八位 hex，不用 session ID）關聯受控測試日誌；於該日誌寫明入口類型、瀏覽器與 OS 版本、手機 build、provider、步驟、耗時與結果。只用合成內容；不保存 QR、bearer、cookie、私有 URL、真實對話或未遮蔽的網路錄製。

```sh
node scripts/console-w08.cjs init "$(git rev-parse HEAD)" > /tmp/pocket-w08-runs.json
# 實際演練後填 status、environment（例：Windows Edge 156.0）、runId 與 seconds。
node scripts/console-w08.cjs check "$(git rev-parse HEAD)" /tmp/pocket-w08-runs.json
```

初始全部 NOT_RUN。缺列、非 PASS、錯版、錯平台、重複或缺證、登入耗時未達標均不放行。即使表格完整，仍須人工核對關聯日誌；檢查器不會部署或變更旗標。未取得通用測試入口、實體設備、可用的兩種 provider 與兩位測試帳號前，維持 NO-GO。
