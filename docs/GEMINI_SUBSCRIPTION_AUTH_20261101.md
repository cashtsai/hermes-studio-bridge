# Gemini 認證:訂閱優先(2026-11-01 機主拍板)

> 「讓 gemini 跟 cc cx 的訂閱仔一樣,哪有那麼多人有 API key。Google 登入
> 馬上就有免費的。OpenClaw 綁去訂閱的 Gemini,下一個 fallback OpenRouter。」
>
> 本文件是規格與現況。**改實作先改這裡。**

## 0. 一句話

**三家 agent 的身分模型統一成「訂閱帳號優先,key 是退路」**:
CC=Claude 訂閱、CX=ChatGPT 訂閱、Gemini=Google 帳號(免費額度+AI Pro/Ultra)。

## 1. 事實基礎(全部實測,標注日期)

- CLI 0.63.0 ACP `authMethods`(2026-11-01 直接握手問出):
  `oauth-personal`(Log in with Google)/ `gemini-api-key` / `vertex-ai` / `gateway`
- 免費額度與訂閱掛 **oauth-personal**;API key 走 AI Studio 計費,與訂閱無關
- OAuth 憑證快取:`~/.gemini/oauth_creds.json`(CLI 互動登入一次即落檔)
- Hermes gateway 有 **`google-gemini-cli`** model-provider overlay
  (v2.7.5 的 11 個 overlay 之一,WS-6 文件)—— 模型呼叫可經 Hermes 走
  gemini CLI 的 OAuth 身分

## 2. 已實作(bridge 975a88c)

| 件 | 內容 |
|---|---|
| `auth_plan()` | auto:OAuth 在前、key 殿後;強制模式缺料回空不偷退路 |
| 握手 | 依計畫試,失敗留痕,連線日誌記 `auth=` 用了哪個身分 |
| `configured()` | 只有 Google 登入的人也算配置好(原本看到「未配置」)|
| `/app/v1/gemini/config` | 回 `auth`/`oauth_available`/`auth_used`,收 `auth` 欄 |

**A 軌(Mac)使用者動作**:終端機跑一次 `gemini` → Log in with Google → 完。
bridge 下次握手自動用訂閱身分。

## 3. 待做

### 3.1 app 內 Google 登入(app 側,AgentLogin 擴充)

現況:AgentLogin 的 Gemini 頁只有「貼 API key」。要加「用 Google 登入」:
- gemini CLI 支援 `NO_BROWSER=1` 的無瀏覽器流程(URL + 貼回授權碼)——
  **與 AgentLogin 既有的 Claude setup-token 流程同形狀**,bridge 跑 CLI、
  撈 URL 回 app、使用者授權後貼碼回來。
- ⚠️ 流程細節(確切 env 名/貼碼格式)**未實測** —— 實作前先在龍蝦上走一次,
  同 Codex device-auth 的驗證路徑。
- 登入成功後 app 的 Gemini 設定頁顯示「已用 Google 帳號(訂閱/免費額度)」
  (讀 `auth_used == "oauth-personal"`),不再預設推人去生 key。

### 3.2 OpenClaw 綁訂閱 Gemini + OpenRouter 退路

**鏈路**:OpenClaw 的模型呼叫 → Hermes gateway(`google-gemini-cli` overlay,
OpenAI 相容面)→ gemini CLI OAuth → **訂閱/免費額度**。

- OpenClaw 支援自訂 OpenAI 相容 provider(baseURL+key);把主要 provider
  指到 Hermes gateway,模型列 gemini;**fallback 配 OpenRouter**
  (OpenClaw 原生支援,BYO OpenRouter key)。
- 退路順序:訂閱 Gemini → OpenRouter。額度打光時 OpenClaw 自動換道,
  使用者看到的是「變慢/換模型」而不是「死掉」。
- ⚠️ 兩個未驗證點,動手前先釘:
  1. Hermes overlay 在**無頭龍蝦**上要求 OAuth 快取存在 —— 快取從哪來?
     (3.1 的流程產生,或使用者 SSH 登入一次)
  2. OpenClaw 的 fallback 行為是「provider 層」還是「請求層」——
     決定額度打光時的體感。

### 3.3 B 軌(龍蝦)預設

開箱預設改為:「**用 Google 登入(免費)**」為主 CTA,API key 為次選。
憑證保管註記:OAuth refresh token 比可撤銷 API key 重 —— 隱私文案要分開講
(「你選擇在雲端主機登入 Google,token 存於你專屬的 VM」)。

## 4. 不變的邊界

- 模型仍是 CLI spawn 參數 → per-session 模型做不到(對齊表已記)
- 本機無用量資料 → 額度百分比不准猜(`_GM_FAILURE_RULES` 已處理額度用盡的人話)
