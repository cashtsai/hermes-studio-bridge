# App 內 Agent 登入規格 —— 不用開 shell,頁籤裡簡單設定

> 機主 2026-10-31:「把這件事真的做成可以讓使用者簡單設定」。
> 起因:龍蝦實機測試,CC/CX 登入要使用者自己 ssh 進去跑 `claude`/`codex`,
> 那是劝退點。而遠端機(龍蝦)的 localhost callback 流程會斷,一般使用者
> 走不了。
>
> **全部機制都在龍蝦實機上實測過(45.32.119.188, codex 0.166.0 / claude 2.1.309)。**

## 0. 目標

app 的 **CC / Codex / Gemini** 頁籤,在「主機未登入」時,給一個**頁籤內的登入
入口**,不需要使用者開終端機。

現狀:app 已經有登入 gate(主機未登入時頁籤蓋版凍結,見 `agent-login-gate`
的既有設計),但**沒有登入入口** —— gate 只擋,不給路。這份規格補的就是那條路。

## 1. 三個 agent 的登入機制(實測,非推測)

### 1a. Codex —— device code 流程 ✅

`codex login --device-auth` 實測輸出:

```
Follow these steps to sign in with ChatGPT using device code authorization:
1. Open this link in your browser and sign in to your account
   https://auth.openai.com/codex/device
```

- **不靠 localhost callback** —— device code flow,遠端機完美運作。
- 使用者:開那條 URL → 輸入畫面顯示的 device code → 授權完成。
- 用自己的 ChatGPT 帳號(訂閱或 API)。

### 1b. CC —— setup-token 的 manual code 流程 ✅

`claude setup-token` 實測輸出(重點是 redirect_uri):

```
This will guide you through long-lived (1-year) auth token setup for your
Claude account. Claude subscription required.
Browser didn't open? Use the url below to sign in (c to copy)
  https://claude.com/cai/oauth/authorize?...&redirect_uri=
    https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&...&code=true
```

- **關鍵:`redirect_uri=https://platform.claude.com/oauth/code/callback`**
  —— 回到 **Anthropic 自己的伺服器**,不是 localhost。`code=true` = 授權完
  Anthropic 頁面顯示一個 code,使用者**複製貼回 CLI**。
- **所以遠端機可行**:開 URL → 手機授權 → 複製 Anthropic 給的 code → 貼回。
- 用**訂閱帳號**(文字明寫 "Claude subscription required"),不是 API key。
- 產出是 long-lived(1 年)token。

### 1c. Gemini —— API key(不是 OAuth)

`gemini_provider.py`:認證走 API key(env `GEMINI_API_KEY` 或
`~/.pocket/gemini.json`)。沒有網頁授權這回事。

## 2. 統一的互動模型

CC 和 Codex 收斂成**同一個模式**:

```
  [開網頁授權] 按鈕  →  使用者在手機瀏覽器完成  →  把一個 code 貼回輸入框  →  完成
```

差別只在:
- Codex 的 code 是**畫面上顯示的 device code**(授權前就有)
- CC 的 code 是**授權完 Anthropic 頁面給的**(授權後才有)

Gemini 是退化版:**沒有按鈕,只有一個貼 API key 的輸入框**。

所以 app 端是**一套 UI、三種填法**,不是三套。

## 3. bridge 端點(新增)

既有的 `POST /ccsessions/{name}/login` 是「把某條 session 推進 TUI 登入」,
那是 session 級、靠 tmux,**不適用這裡**(龍蝦要的是主機級、不靠 TUI)。
需要新的主機級端點:

### 3a. `POST /app/v1/agents/{provider}/login/start`

`provider` ∈ `claude` | `codex`。啟動登入流程,回使用者要看的東西。

```json
// codex 回:
{ "ok": true, "provider": "codex", "mode": "device_code",
  "url": "https://auth.openai.com/codex/device",
  "user_code": "ABCD-1234",          // 畫面顯示、使用者輸入
  "login_id": "<bridge 內部追蹤這次登入的 id>" }

// claude 回:
{ "ok": true, "provider": "claude", "mode": "manual_code",
  "url": "https://claude.com/cai/oauth/authorize?...",
  "user_code": null,                 // CC 的 code 是授權後才有,要貼回
  "login_id": "<...>" }
```

**實作**:bridge spawn `codex login --device-auth` / `claude setup-token`,
掛在一個背景 PTY(兩者都要 TTY —— 實測 `claude setup-token` 沒有 TTY 時印
空白,要用 `script -qec` 之類給偽 TTY),撈出 URL / device code,把那顆行程
**留著等後續**(它在等使用者授權 / 等貼 code)。`login_id` 對應那顆行程。

### 3b. `POST /app/v1/agents/{provider}/login/finish`

```json
// 請求
{ "login_id": "<...>", "code": "<使用者貼回的 code>" }  // codex device-auth 可能不需要 code(授權完自動好)
// 回
{ "ok": true, "logged_in": true, "account": "user@example" }
```

**實作**:把 code 寫進那顆背景行程的 stdin(CC),或輪詢 device-auth 完成
(Codex);完成後行程退出、憑證落檔;bridge 重探 `_agent_auth_status` 清快取。

### 3c. `PUT /app/v1/agents/gemini/key`

```json
{ "api_key": "<使用者貼的 key>" }   // 寫進 ~/.pocket/gemini.json
```

### 安全

- 三支都要 `_check_auth`(device token)。
- **code 與 api_key 只進 stdin / 設定檔,絕不寫 log。**
- `login_id` 短 TTL(登入流程 ~2 分鐘),過期行程殺掉。
- device token scope:遠端控制台的 `console-remote` scope **能不能碰登入**?
  → **不能**。登入是把帳號綁上這台機器,等同機主權限,不下放給遠端瀏覽器。
  只有一般 device token(掃碼配對的手機)可以。

## 4. app UI(CC/CX/Gemini 頁籤的 gate 狀態)

主機未登入時,gate 不再只是「蓋版凍結」,而是:

```
  ┌─ Codex 尚未在這台主機登入 ─────────┐
  │  [ 開啟登入頁面 ]  ← 開 url         │
  │  輸入授權碼: [________] [ 完成 ]    │
  └────────────────────────────────────┘
```

- Codex:按鈕開 url,下方顯示 device code(bridge 回的 user_code),
  使用者在網頁輸入那個 code,回 app 按「完成」。
- CC:按鈕開 url,使用者授權後 Anthropic 給一個 code,貼進輸入框、按「完成」。
- Gemini:沒有按鈕,只有「貼上 API key」輸入框 + 完成。

**文案要講清楚 code 從哪來**(Codex 是畫面上這個、CC 是網頁給你的),否則
使用者會貼錯。

## 5. 分階段(可單獨出貨,每階段都有價值)

| 階段 | 做什麼 | 價值 |
|---|---|---|
| **1** | Gemini 的 key 輸入框(§3c + UI) | 最簡單、零 OAuth,先打通「頁籤內設定」這條路 |
| **2** | Codex device-auth(§3a/3b + UI) | 龍蝦上 Codex 一鍵登入,device flow 最乾淨 |
| **3** | CC setup-token(§3a/3b 的 claude 分支 + UI) | 補上 CC;PTY 處理最麻煩,放最後 |

**每階段獨立旗標、預設關**,合併進 main 不改變現行行為(照 bridge 慣例)。

## 6. 待決 / 待驗

1. **CC 的 setup-token 要不要 TTY,bridge 怎麼給**:實測沒 TTY 印空白,
   `script -qec` 可以給偽 TTY,但在 FastAPI 背景行程裡要用 `pty.openpty()`。
   這是階段 3 最大的實作風險,要先 spike。
2. **Codex device-auth 完成後要不要使用者按「完成」**,還是 bridge 輪詢自動偵測
   `~/.codex/auth.json` 出現。後者體驗更好,待實作確認。
3. **桌面端(Mac)要不要也用這套**:Mac 上 localhost callback 本來就能用,
   現有流程沒壞。建議這套**只在遠端/無頭主機啟用**,Mac 維持現狀(零風險)。
   判定:`_host_capabilities` 已知這台有沒有 GUI;或直接看 localhost callback
   能不能通。

## 附:這份規格的事實來源

全部 2026-10-31 在龍蝦實機(Vultr SG, 45.32.119.188)跑過:
- `codex login --device-auth` → `auth.openai.com/codex/device` + device code
- `claude setup-token` → redirect_uri = `platform.claude.com/oauth/code/callback`
  (不是 localhost)+ `code=true`(manual code)+ "subscription required"
- `gemini_provider.py` → API key(env 或 `~/.pocket/gemini.json`)
- 既有 `POST /ccsessions/{name}/login`(session 級,不適用)、
  `_agent_auth_status`(登入狀態探測,TTL 60s)
