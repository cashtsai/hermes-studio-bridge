# Codex 家目錄隔離（`POCKET_CODEX_ISOLATED`）

> 一句話：**讓 bridge 用自己的 `CODEX_HOME`，thread-store 完全歸自己所有，
> 不再跟 ChatGPT 桌面 app 搶 writer lock、也不再依賴它活著。**
>
> 狀態：程式碼已合併但**預設關閉**（macOS）。開關在 launchd plist，見下面
> 「怎麼開 / 怎麼關回去」。

---

## 1. 為什麼要做

codex 的 thread-store **每條 thread 只允許一個 writer**。想寫的有三方：
ChatGPT 桌面 app（自帶 app-server）、這支 bridge、任何 `codex` CLI。

2026-08-12 的事故鏈：

```
桌面 app 05:32 自動更新 → 殘留殭屍 app-server
    → thread-store conflict: already has an active writer
        → Pocket 的 CX 全滅（數小時）
```

當天的止血（`codex/fix-managed-codex-transport`，已合併）是讓 bridge 去接桌面
app 的 **managed daemon**，大家共用同一顆 writer。衝突是解掉了，但依賴方向被
倒過來：

> **Pocket 能不能用，變成取決於一個第三方 GUI app 有沒有活著。**

這對產品不可接受；而且龍蝦那台無頭 Ubuntu（`feat/lobster-ubuntu`）根本沒有
桌面 app，那條路在那台機器上不成立。

隔離之後：bridge 有自己的 thread-store，**任何時候都能起自己的 app-server**，
桌面 app 開不開、更不更新，Pocket 都不受影響。

## 2. 代價（善彰已明確接受）

語意變得**跟 CC 一模一樣**：

> **只有「透過 bridge / Pocket 開的 session」才會出現在 Pocket 裡。**

在 VS Code、ChatGPT 桌面 app、終端機 `codex` 開的新 thread，Pocket **看不到**。
反過來也一樣：Pocket 開的 thread，桌面 app 看不到。

舊的 thread 不會消失、也不會壞掉——它們原封不動留在 `~/.codex`，桌面 app 和
VS Code 照樣看得到、用得到。只是預設不會出現在 Pocket 裡，**除非你把它搬過來**
（見第 5 節，可以搬，實測可 resume、可讀完整歷史）。

---

## 3. 三個實作決定（都經過實機驗證，codex-cli 0.147）

### 3.1 憑證：`auth.json` 用 symlink，不複製

`auth.json` 住在 `CODEX_HOME` 底下，所以隔離家目錄一開始是「未登入」
（實測 `CODEX_HOME=<空目錄> codex login status` → `Not logged in`）。

實測 **codex 寫 `auth.json` 是就地 truncate+write，不是 tmp+rename**：拿
symlink 當 `auth.json`、跑一次寫入路徑（`codex login --with-api-key`）之後，
symlink 仍在，內容寫進了它指到的那個檔。

所以 bootstrap 只做一件事：

```
~/.pocket/codex-home/auth.json  →  symlink 到  ~/.codex/auth.json
```

好處是**只有一份實體憑證、一條 refresh token 世系**。如果改用「複製一份」，
兩邊會各自 refresh，token 轉動時很可能把其中一邊踢下線——那是我們最不想要
的失敗模式。symlink 沒有這個問題，而且不用把 token 內容再落地到第二個地方。

例外處理：
* 隔離家目錄裡已經有**實體** `auth.json`（有人刻意放專用帳號憑證）→ 不動它。
* symlink 斷掉（來源被搬走過）→ 來源回來就自動重接。
* 來源根本沒有 `auth.json` → 記一則 log，不當機。

### 3.2 設定：複製 `config.toml`，而且會自動跟上

codex 沒有 `include` 語法，隔離家目錄讀不到 `~/.codex/config.toml`
（`model_reasoning_effort = "xhigh"`、各專案設定會整組消失）。所以 bootstrap
**複製**一份過去，並記下「複製當下的內容雜湊」（`.pocket-config-origin.json`）。

之後每次 bridge 要起 app-server 前都會重跑一次 bootstrap（成本 = 一顆 7KB
檔的 sha256）：

| 隔離副本 | 來源 `~/.codex/config.toml` | 行為 |
|---|---|---|
| 沒被手改過 | 變了 | **自動重新複製**（改完重啟 bridge 就生效） |
| 沒被手改過 | 沒變 | 什麼都不做 |
| 被手改過 | 變了 | **保留手改**，不覆蓋，記一則 log |

想要「永遠跟 `~/.codex` 一致」的話：`POCKET_CODEX_CONFIG_MODE=symlink`
（代價：codex 若寫 config 會寫回 `~/.codex`）。想完全自己管：`=none`。

### 3.3 舊 thread：可以搬，來源全程唯讀

thread-store 的組成是：

```
<home>/state_5.sqlite   ← threads 資料列（id / title / name / cwd / rollout_path …）
<home>/sessions/**/rollout-*.jsonl   ← 逐字稿本體
```

`scripts/migrate-codex-threads.py` 把「資料列 + 逐字稿」一起複製過去，並把
`rollout_path` 改寫成新家的路徑。**實測**：搬完之後隔離家目錄的 app-server
`thread/list` 看得到、`thread/resume` 成功、`thread/turns/list` 讀得到完整歷史。

安全性：
* 來源 state db **先複製快照再讀**（WAL 模式下就算 `mode=ro` 也可能寫 `-shm`）；
* 逐字稿只讀不寫，macOS/APFS 上用 `cp -c` 做 copy-on-write clone
  ——**秒殺、不佔額外空間**，而且之後在隔離家目錄續寫不會動到來源那份；
* 可重複執行（已存在就跳過）；不加 `--apply` 就是預演；
* `~/.codex` 底下**不刪、不改任何東西**。

---

## 4. 模式矩陣（隔離 × 傳輸）

規則只有一條：**隔離開啟 ⇒ 一定自己 spawn**。去接桌面版的 managed daemon
就等於沒有隔離（那顆 daemon 綁死在 `~/.codex`）。

| `POCKET_CODEX_ISOLATED` | `CODEX_APP_SERVER_MODE` | 實際行為 | 用的家目錄 |
|---|---|---|---|
| off（Mac 預設） | `auto`（預設） | 先試 managed daemon，連不上退回自己 spawn stdio | `~/.codex` |
| off | `managed` | 只用共用 daemon，連不上就大聲壞掉 | `~/.codex` |
| off | `stdio` | 自己 spawn，完全不碰 socket | `~/.codex` |
| **on** | `auto` | **stdio**（完全不碰 socket） | `~/.pocket/codex-home` |
| **on** | `managed` | **設定衝突 → 隔離贏**，走 stdio，記一則 `codex_isolation_mode_conflict` | `~/.pocket/codex-home` |
| **on** | `stdio` | stdio | `~/.pocket/codex-home` |

為什麼「隔離贏」而不是「拒絕啟動」：兩個設定都是使用者明講的，但拒絕啟動
= CX 全滅（就是我們在修的那個病），而共用 store = 回到 writer lock 戰爭。
隔離是比較強的安全性質，所以它贏，並且在日誌裡看得見。

`POCKET_CODEX_ISOLATED` 沒設 = `auto`：**macOS 關、其他平台開**。
龍蝦那台無頭 Ubuntu 沒有桌面 app 的 daemon，隔離在那裡是嚴格更好且零代價的
預設，不必特別去設。

---

## 5. 怎麼用

### 5.1 先看有哪些舊 thread 可以搬（唯讀，不動任何東西）

```sh
cd ~/apps/hermes-openwebui-bridge
scripts/migrate-codex-threads.py --list --recent 20
```

### 5.2 預演 → 真的搬

```sh
# 預演：看看會搬什麼、佔多少空間
scripts/migrate-codex-threads.py \
    --thread 019f39d3-e347-7203-ba4f-fa92948d149c \
    --thread 019f01b0-51c1-7243-8148-b76ad12218a7

# 確認沒問題再加 --apply
scripts/migrate-codex-threads.py --thread … --apply

# 或者一次搬最近 12 條
scripts/migrate-codex-threads.py --recent 12 --apply
```

**建議在開旗標之前先搬**，這樣一切換過去，Pocket 裡就已經有那幾條線。

### 5.3 開旗標（善彰自己執行）

```sh
PLIST=~/Library/LaunchAgents/ai.studio.hermes-bridge.plist

# 1) 加一顆環境變數
/usr/libexec/PlistBuddy -c \
  "Add :EnvironmentVariables:POCKET_CODEX_ISOLATED string 1" "$PLIST"

# 2) 重載 + 重啟（看門狗的失敗計數要清掉，不然維護空窗會被誤殺）
rm -f /tmp/hermes-bridge-watchdog.fails
launchctl bootout gui/$(id -u)/ai.studio.hermes-bridge 2>/dev/null
launchctl bootstrap gui/$(id -u) "$PLIST"

# 3) 驗收：日誌應該看到 isolated=true 和新的家目錄
tail -f ~/apps/hermes-openwebui-bridge/bridge.out.log | grep -E \
  "codex_isolated_home_ready|codex_transport_selected|codex_app_server_started"
```

預期會看到：

```json
{"event": "codex_isolated_home_ready", "home": "/Users/xcash/.pocket/codex-home",
 "auth": "symlinked", "config": "copied", "created": true}
{"event": "codex_transport_selected", "transport": "stdio",
 "mode": "auto", "effective_mode": "stdio", "isolated": true}
{"event": "codex_app_server_started", "codex_home": "/Users/xcash/.pocket/codex-home",
 "isolated": true, "transport": "stdio"}
```

### 5.4 關回去（回滾，30 秒）

```sh
PLIST=~/Library/LaunchAgents/ai.studio.hermes-bridge.plist
/usr/libexec/PlistBuddy -c \
  "Delete :EnvironmentVariables:POCKET_CODEX_ISOLATED" "$PLIST"
rm -f /tmp/hermes-bridge-watchdog.fails
launchctl bootout gui/$(id -u)/ai.studio.hermes-bridge 2>/dev/null
launchctl bootstrap gui/$(id -u) "$PLIST"
```

回滾**不會損失任何東西**：`~/.pocket/codex-home` 留在原地（下次再開就接著用），
`~/.codex` 從頭到尾沒被動過。唯一的差別是「隔離期間在 Pocket 開的新 thread」
關掉旗標後在 Pocket 裡看不到了——它們還在 `~/.pocket/codex-home` 裡，再開回去
就又出現。

---

## 6. 環境變數一覽

| 變數 | 預設 | 說明 |
|---|---|---|
| `POCKET_CODEX_ISOLATED` | `auto` | `1/0/true/false/on/off/auto`；auto = macOS 關、其他平台開 |
| `POCKET_CODEX_HOME` | `~/.pocket/codex-home` | 隔離家目錄位置 |
| `POCKET_CODEX_CONFIG_MODE` | `copy` | `copy` / `symlink` / `none` |
| `CODEX_HOME` | `~/.codex` | 「共用家目錄」在哪；隔離關閉時 bridge 就是用這個 |
| `CODEX_APP_SERVER_MODE` | `auto` | `auto` / `managed` / `stdio`（見矩陣） |

## 7. 其他行為變化

* **用量 / rate limit**：隔離開啟時會同時掃「隔離家目錄」和「共用家目錄」的
  rollout jsonl。rate limit 是**帳號層級**的事實，桌面 app 寫下的那筆一樣算數，
  只掃一邊只會讓顯示變舊。
* **登入狀態探測**（app 的 CC/CX 頁籤閘門）：隔離開啟時 `codex login status`
  會帶著隔離家目錄跑，問的是「bridge 那份到底能不能用」，而不是桌面 app 的狀態。
* **`~/.codex` 是唯讀的**：這個功能全程不寫入共用家目錄；唯一的例外是
  codex 自己透過 symlink 刷新 `auth.json`——那本來就是它每天在做的事，
  而且正是我們選 symlink 的目的（單一憑證來源）。

## 8. 測試

```sh
PYTHONPATH=. python tests/test_codex_isolated_home.py     # 32 個 case
scripts/run-tests.sh                                      # 全套逐檔
```

測試守住四件事：bootstrap 一個位元組都不寫進來源家目錄；旗標關閉時 spawn
呼叫與今天**逐字相同**（連 `env=` 都不傳）；旗標開啟時真的用隔離家目錄且
不碰桌面版 socket；搬遷可重複執行、來源唯讀、預演不寫入。
