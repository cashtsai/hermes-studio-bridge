# Bridge 側交辦單:Provider 統一登入面板(方案 B 的 bridge 半)

> 2026-11-21。完整交辦單,不預設你看過對話。每條帶 file:line,與現況不符
> 以程式碼為準並回報差異。

## 紅線(repo 根 CLAUDE.md,務必先讀)
`~/apps/hermes-openwebui-bridge` 跑 production 服務 `ai.studio.hermes-bridge`。
**只能 commit+push 到 `feat/provider-auth-panel`。不准 merge main、不准
launchctl 重啟、不准寫 production DB、不要跑 bridge 服務。** 工作樹
`~/apps/bridge-authpanel` 已開好,直接在裡面做。

## 目標
機主要「在 app 內一個面板看到八個 provider 各自的登入狀態 + 登入按鈕」。
調查結論:基礎設施大半已在,bridge 這半只有兩件事要補。**不要重造已有的東西。**

## 已經存在、不要動只要確認的(調查已查證)
- `GET /app/v1/failover/config` 的 `availability` 欄 **八家全覆蓋**
  `{installed, logged_in}`(`_failover_availability()` bridge.py:27079-27101)。
  這就是 app 要的統一狀態源,**不需要新端點**。確認它八家都在、欄位穩定即可。
- in-app 登入端點已存在但受旗標 `POCKET_AGENT_LOGIN_IN_APP` 控制
  (預設關,bridge.py:37449)。claude/codex/gemini 的 login start/finish 都有
  (bridge.py:37515 codex device-auth、37674 claude PKCE、37883 gemini)。
- grok/opencode/openclaw 各有 config 端點可貼 key / 連線字串
  (`PUT /app/v1/grok/config` bridge.py:30346、opencode :30979、openclaw :31346)。
- **安全限制要保留**:login 端點對 `CONSOLE_REMOTE_SCOPE`(借來的電腦)一律
  403(bridge.py:37467,37525,37680,37889)。新端點也要套同一道。

## 要做的兩件

### 1. grok device-auth 一鍵端點(唯一的真新功能)
grok 現在只能貼 API key 或 SSH 去主機跑 `grok login`。要做成跟 codex 一樣的
in-app device-auth:
- **仿 codex 的實作** `bridge.py:37515`(start:跑 CLI、刮 URL+碼、回 app、
  行程留背景)+ `:37592`(status:輪詢偵測完成)。
- grok 的指令與判準在 `grok_sdk.py`:`login` 指引字串 `:157`、
  `login_available()` `:140`、`auth_state()` `:145`、`logged_in()` `:390`。
  先確認 `grok login --device-auth` 實際吐什麼(URL + 碼?時間窗多長?)——
  **不要假設跟 codex 一模一樣**,grok CLI 的輸出格式要實際跑過或讀 help 確認。
- 新端點:`POST /app/v1/agents/grok/login/start` + `GET …/grok/login/status`
  (命名對齊 codex 那組,放同一區)。完成後憑證落 `~/.grok/auth.json`
  (grok_sdk 自管),`logged_in()` 會轉 true,failover availability 自動反映。
- **套 CONSOLE_REMOTE_SCOPE 403 守門**(同 codex)。

### 2. 把 grok 納入 `/app/v1/agents/auth`(選配,讓狀態源統一)
現在 `/app/v1/agents/auth`(bridge.py:37433)只回 claude/codex/gemini 三家。
app 可能沿用這個端點顯示「可登入的那幾家」。評估:把 grok 也加進去
(用 `grok_sdk.auth_state()`),還是讓 app 直接吃 failover availability 的
八家 + 各 config 端點。**你決定哪個對 app 比較好接,在回報裡說明。**
(failover availability 已經八家全覆蓋,所以這條可能根本不必做 —— 確認後再說。)

## 旗標
`POCKET_AGENT_LOGIN_IN_APP` 要打開才會生效。**不要在程式裡改預設**(那會
改變所有部署的行為)—— 這個由機主在 production env 開。你只要確保:旗標開了
之後 grok 新端點跟著生效(套同一個旗標判斷,跟 codex/claude 一致)。

## 驗收:不准只看程式碼
- grok device-auth:**實際把流程跑一遍**(或至少把 `grok login --device-auth`
  的真實輸出貼出來),證明 start 端點刮得到正確的 URL+碼、status 端點偵測得到
  完成。不能只是「照 codex 抄」就說好 —— codex 和 grok 的 CLI 輸出格式不同。
- 寫一支離線測試(或 harness)餵假的 CLI 輸出,驗 start 刮 URL+碼、
  status 狀態機、CONSOLE_REMOTE_SCOPE 403。
- 跑既有測試:`PYTHON=~/apps/hermes-agent/runtime/venv/bin/python scripts/run-tests.sh`
  (逐檔跑,不能直接 pytest tests/)。`test_persona_description` 在乾淨 main
  上本來就紅,與你無關,不要修它。

## 回報
commit hash、改檔清單、grok device-auth 的真實輸出證據、測試結果、
以及「app 該吃哪個狀態源(failover availability vs agents/auth)」的建議。
前景跑到底,不要等背景通知。push `feat/provider-auth-panel`。
