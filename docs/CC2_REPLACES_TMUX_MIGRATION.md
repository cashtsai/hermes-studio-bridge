# cc2 與 tmux：從「取代」改為「共存」

> ## ⚠️ 2026-10-31 定調變更：**不取代，共存**
>
> 機主 2026-10-29 原本定調「cc2 當初就是為了取代 tmux」。10-31 盤點
> tmux 實際依賴之後改為**兩條路長期並存**。本節是新的依據；底下 §0–§8 的
> 原文**全部保留**，因為那些分析在共存之下大多仍然成立，只有被本節明確
> 推翻的幾處例外（都標了「🔁 共存修訂」）。
>
> ### 為什麼改
>
> 1. **會弄丟一條已經寫進官網 KB 的能力。** cc2 有自己的 registry，列的是
>    它知道的 session。而官網 FAQ 教使用者「先 `tmux new -s mywork` 再跑
>    `claude`，那條 session 就會出現在手機的 CC 頁籤」。**tmux 路徑一走，
>    那句話就變成不實的** —— 除非 cc2 補上「認養未知 session」，而那沒人做。
>    這是 tmux 路徑唯一**不可取代**的能力。
> 2. **終端機的持久化還要靠 tmux。** `_terminal_tmux_bin()` 決定
>    `backend`／`persistent`／`reattach` 三個欄位；沒有 tmux 時終端機仍可用，
>    但斷線即失去工作。雲端龍蝦尤其需要它。
> 3. **跑很久的回合熬不過 bridge 重啟 —— 只有 tmux 那條熬得過。**
>    一條 cc2 session 是一顆**常駐 `ClaudeSDKClient`**（`cc_sdk.py:807`），
>    跑在 **bridge 行程裡**。bridge 一重啟，**正在跑的那一輪就沒了** ——
>    `in_flight_prompt` 這個欄位存在的唯一理由就是「重啟後用來說明中斷」
>    （該處註解原文）。對話本身會留著（Claude 自己的 store ＋ resume 自癒），
>    但那一輪的工作要重來。
>
>    tmux 路徑沒有這個問題：行程活在 tmux 裡，bridge 重啟它照跑。
>
>    **機主 2026-10-31 把目標講明確了：「要讓 cc / cx 能夠持久、接力、
>    遠端操控」。** 在那個目標下這條是三個理由裡最關鍵的 —— 其餘兩條是
>    「會弄丟既有能力」，這條是「目標本身達不到」。
> 4. **「棄用 tmux」≠「機器上不裝 tmux」。** 前三點合起來的結論：該退場的是
>    **cc v1 的 tmux 傳輸層當唯一路徑**這件事，不是把 tmux 從安裝清單拿掉。
>
> ### 順帶查到：CX 在龍蝦上會退化
>
> CX 的持久性來自 **managed daemon**（`~/.codex/app-server-control/…sock`，
> 註解寫「the single writer shared by Desktop and Pocket」）—— 那顆 daemon
> 是**桌面版 Codex 帶來的**，獨立於 bridge，所以 Mac 上的 CX 回合**跨得過
> bridge 重啟**。
>
> 但**雲端龍蝦沒有桌面版** → 只能走 bridge 自己 spawn 的 `codex app-server
> --stdio` 退路 → 子行程隨 bridge 死。**在龍蝦上裝了 codex 執行檔，不等於
> 拿到持久化。** 這條要單獨解（讓 daemon 在龍蝦上以 systemd unit 常駐），
> 列在 §9。
>
> ### 共存之下的分工（新的真相）
>
> | | 誰負責 |
> |---|---|
> | **新建 session** | **cc2**（階段 4，`CC_NEW_SESSIONS_BACKEND=cc2`） |
> | **使用者自己在 tmux 裡開的 `claude`** | **cc v1**（cc2 看不到，這是它唯一不可取代的能力） |
> | **沒有 tmux 的平台**（Windows、精簡龍蝦） | **cc2**（唯一可行路徑） |
> | **內建終端機的持久化** | **tmux**（與 CC 哪條路無關） |
>
> ### 附帶收益：Windows 解鎖了
>
> `WIN_UBUNTU_LOBSTER_INSTALLER_RESEARCH_20260811.md` §1a 把 CC on Windows
> 標 🔴「需 ConPTY 重寫」，而**唯一原因就是 tmux**
> （`pty`／`termios`／`fcntl`／`setsid`／`killpg`）。cc2 這條路不碰那些
> （實查：`cc_sdk.py` 提到 tmux 38 次，**實際呼叫 0 個**，全是註解在對比
> v1）。所以 Windows 龍蝦從「分兩階段、MVP 不含 CC」變成可以一次做完。
>
> ### 這份變更推翻了什麼
>
> - **階段 5（移除 tmux 程式碼）→ 取消。** 見 §6。
> - **§8 第 4 點（清掉 `ccsess` CLI 與 `_tmux_*`）→ 不做。**
> - **§4 規則 2（解析時 cc2 優先）→ 反過來，改成「先 tmux」。** 見該處修訂。
>
> 狀態：**階段 0–4 已實作**（兩個旗標預設關）。
>
> 相關：`APP_BRIDGE_CONTRACT.md` §4.1c（cc2 的 client 契約）、`cc_sdk.py` 檔頭
> （為什麼要 SDK 路徑）。

## 0. 現況與目標的落差（一句話）

今天不是「取代中」，是**並存、而且 cc2 在手機上近乎隱形** —— 兩個 app 的 CC 分頁
資料源都是 `/ccsessions`（tmux 專屬），cc2 session 在那一頁**整個不存在**。

所以目標不是「把 cc2 做得更好」，是**讓 cc2 session 出現在使用者已經在看的那一頁**。

## 1. 兩條路，推薦第二條

### (a) 讓 app 學會 cc2
iOS + Android 各加一個 provider：brand 判定、列表資料源、session id 前綴、
⋮ 選單 gate、推播路由、審核跳轉。

- 成本：兩個 app 各一輪 parity，再加 `IOS_PARITY.md` 的同步輪
- **而且方向是錯的**：使用者會在過渡期**看到兩種 Claude Code**，而機主要的是取代。
  做完還得再做一次「把 tmux 那個藏起來」

### (b) **bridge 把 `/ccsessions` 變成後端無關的「CC 列表」** ← 推薦

`/ccsessions` 不再是「tmux 的 session」，而是「**這台機器上的 Claude Code
session**」，底下是 tmux 還是 SDK 由 bridge 決定。app 永遠不需要知道 cc2 存在。

- **已驗證可行**：cc2 的 `v2_rows()`（`cc_sdk.py`）已經有 `/ccsessions` row
  需要的每一個欄位 —— `workdir`、`busy`、`queue_depth`、`busy_since`、`model`、
  `context{used,size,pct}`、以及由 pending approval 推出的 `waiting_approval`。
  **不需要新增任何資料，只需要翻譯。**
- app 端**零改動**（兩邊都在忙，這一點是決定性的）
- 符合目標：tmux 日後退場對 app 是不可見的
- 過渡期不會出現「兩個 Claude Code」

**本文件只規劃 (b)。**

## 2. 遷移槓桿：`POST /ccsessions`（建立）

不要做大爆炸式切換。真正的槓桿是**新建的 CC session 走哪個後端**：

```text
旗標 CC_NEW_SESSIONS_BACKEND = tmux(預設) | cc2

cc2 時:POST /ccsessions 建出來的是 cc2 session
  → 新 session 自然都是 SDK 路徑
  → 既有 tmux session 原地活完它的生命週期
  → 某天 tmux 沒有 session 了,才真的退場
```

**好處**：任何時刻都可回退（把旗標改回 `tmux`，新 session 又走舊路，既有 cc2
session 照樣能用）。不需要資料遷移、不需要使用者做任何事、不需要 app 改版。

## 3. 要做的事（依 app 實際使用頻率排序）

`/ccsessions` 家族共 17 支。**不是每一支都要 dispatch** —— 四支是 tmux 的本質
（cc2 有結構化替代品，app 不會對 cc2 session 呼叫它們）：

| 端點 | 處置 |
|---|---|
| `POST /ccsessions/_hook` | tmux hook 回報，cc2 不需要 → **不動** |
| `POST /ccsessions/{name}/key` | send-keys 按鍵模擬 → cc2 用 `interrupt()` → **不動** |
| `GET  /ccsessions/{name}/stream` | tmux pane 串流 → cc2 用卡片事件 → **不動** |
| `POST /ccsessions/{name}/login` | 在 pane 裡跑 `claude login` → cc2 另有路徑 → **不動** |

要加 cc2 分支的（13 支）：

| 優先 | 端點 | cc2 側已有的對應 |
|---|---|---|
| **P0** | `GET /ccsessions`（列表） | `registry().v2_rows()` → 翻譯成 CC row |
| **P0** | `POST /ccsessions/{name}/input` | `sess.submit()` |
| **P0** | `POST /ccsessions/{name}/interrupt` | `sess.interrupt()` |
| **P0** | `GET /ccsessions/{name}/status` | `v2_rows` 的 status/busy |
| **P1** | `POST /ccsessions`（建立） | `POST /app/v2/cc2/{name}` 的內核（§2 槓桿） |
| **P1** | `POST /ccsessions/{name}/answer` | cc2 的審批等待者 |
| **P1** | `POST /ccsessions/{name}/mode` | `/app/v2/cc2/{name}/settings` |
| **P1** | `POST /ccsessions/{name}/model` | 同上 |
| **P2** | `POST /ccsessions/{name}/rename` | `/app/v2/cc2/{name}/rename` |
| **P2** | `POST /ccsessions/{name}/archive` | `/app/v2/cc2/{name}/archive` |
| **P2** | `POST /ccsessions/{name}/compress` | `/app/v2/cc2/{name}/compact` |
| **P2** | `GET  /ccsessions/{name}/history` | `/app/v2/cc2/{name}/history` |
| **P2** | `GET  /ccsessions/{name}/diff` | workdir 相同，邏輯可共用 |

**13 支全部都有 cc2 側的對應實作** —— 這是 (b) 便宜的根本原因：不是寫新功能，
是接線。

### 卡片流的 id 解析

app 會用 `claude_code:{name}` 開卡片流（`_v2_card_source` 現在固定回 tmux）。
要改成**先問這個 name 屬於哪個後端**：

```text
_v2_card_source("claude_code:foo")
  → foo 在 cc2 registry → ('cc2', 'foo')
  → 否則                → ('cc', 'foo', workdir)   # 現行行為
```

⚠️ 這支是熱路徑（每次開 session、每次補洞都會過），解析要便宜（registry 已在
記憶體，`load()` 有快取）。

## 4. 命名空間規則（必須先定，否則會出現兩條同名 session）

**已驗證**：tmux 的名字來自 `ccsess` 設定檔（`_cc_conf_rows`），cc2 來自自己的
registry（`SESSION_NAME_RE = [A-Za-z0-9_-]{1,64}`）—— **兩個獨立命名空間，會撞**。

規則：

1. **建立時互斥**：`POST /ccsessions` 若該名字已存在於**任一**後端 → 409。
   這條最重要，它從源頭防止撞名。
   > 🔁 **共存修訂（10-31）：這條從「最重要」升級成「唯一的防線」。**
   > 原本的想法是遷移期撞名靠「以 cc2 為準」慢慢消化掉。共存之下**沒有
   > 遷移終點**，撞名不會自己消失 —— 擋在源頭是唯一解。

2. ~~**解析時 cc2 優先**~~ → 🔁 **共存修訂：改成「先 tmux，找不到才 cc2」。**

   原文的理由是「**遷移方向是 cc2**，所以同名時以 cc2 為準」。共存之下那個
   理由整個不存在了：既有的 tmux session 不是待清的歷史殘留，而是**長期
   公民**，把使用者正在用的那條解析到另一個後端才是 bug。

   而且**實作本來就是「先 tmux」**（`_cc_backend()`／`_v2_card_source()`／
   `_cc2_rows_for_cc_list()` 三處一致，見 §6）。所以這次是**規格向實作靠攏**，
   不是改行為 —— 零程式碼變更。

   撞名仍要 log `cc_name_collision`：共存不代表撞名是正常的，它仍然代表
   規則 1 有漏。

3. **列表去重**：同名只出一列。
   > 🔁 **共存修訂：留 tmux 那條**（與規則 2 一致；原文寫「cc2 那條」）。
   > 實作已經是這樣：`_cc2_rows_for_cc_list()` 的註解寫「撞名時以 tmux 為準
   > 並略過 cc2 那條」。

## 4.5 取代門檻的完成狀態（2026-10-30）

機主拍板「先把 SDK 做到齊現有功能再切過去」。§C 的五條門檻**已全部完成**：

| 門檻 | commit | 做了什麼 |
|---|---|---|
| 1. seed 走共用 digest | `dba9e64` | tool_call（含 `patch`）／tool_result／attachment／thinking 一次補齊；上限改「尾端 200 行」；id 由 jsonl uuid 衍生（冪等） |
| 1b. live 卡片同形狀 | `6ae9446` | `kind` 從 `text` 改回 `tool_call`／`tool_result`；補路徑圖的 attachment；結果上限 800→2000 |
| 2. `status` 吐 `usage` | `6ae9446` | 原本只吐 `context` → app 的儀表整條空白且不報錯 |
| 3. 審批推播 + 逾時誠實 | `ba0c8b6` | 補抄漏的 `_approval_push`；逾時與「使用者拒絕」分開講 |
| 4. kind + 複選 meta | `ba0c8b6` | DB 鏡像不再硬寫 `permission`；補 `multiselect`/`q_index`/`q_total`；選項保留說明 |
| 5. 封存可查 | `ba0c8b6` | `v2_rows(include_archived=)` + `?archived=true`，`status:"archived"` |

**測試 37/37 綠，每一刀都驗過鑑別力**（注入回歸確認測試會轉紅）。

### 仍未做（不阻擋切換，但要知道）

| 項 | 影響 |
|---|---|
| live 的 thinking 串流 | 長思考階段畫面安靜（seed 已有 thinking 卡） |
| 進場不補 pending approval 卡 | 重啟後正在等的審批要靠推播／審核中心看到，卡片流裡沒有 |
| 複選一次只收一個答案 | 管道限制（只能靠 deny message 回答模型）；`multiselect` 已標真，app 不會畫錯版面 |
| 多題 ask 只答第一題 | 已在 deny message 裡明講「其餘未回答」 |
| `claudeTitle`／`sessionTitle`／`sessionId`／`plan` 欄位 | 列表副標與計畫審批的全文 |
| `last_event_at` 不跨重啟 | bridge 重啟後 cc2 列排到最底 |
| 無 `failed`／`down` 狀態 | 主機沒登入的 session 顯示「待命」，送出才報錯 |
| 戶政註冊 + 配額 precheck、delegation 不支援 cc2 | 委派派不給 cc2；**若機主會用委派／工人面板，這條要先補** |

## 5. 切過去會像退步的地方（原始分析，保留備查）

這是 (b) 唯一的真實風險：**同一條對話在 cc2 下「進場看到的東西比 tmux 少」**。

| | tmux seed | cc2 seed | 影響 |
|---|---|---|---|
| 範圍 | 尾端 **200 行**原始 jsonl | 過濾後 **40 則** | 中 |
| 工具卡／thinking | **有** | **沒有** | **高** —— 使用者會覺得「歷史不見了」 |
| 附件卡 | **有** | 沒有 | 中 |
| 進場 pending approval 卡 | **有** | 沒有 | **高** —— 「有東西在等你」會漏掉 |

**P0（切換前必做）**：cc2 seed 補工具卡與 pending approval 卡。
`cc_sdk._seed_row` 目前只收 user/assistant 文字；而 **`carddigest.cc_event_to_cards`
已經會做全套翻譯**（tmux seed 就是用它）。所以方向是「seed 改走共用 digest」，
不是在 cc2 裡重寫一套 —— 同契約 §0「一份 parser」。

> 這一條與 `fix/cc2-seed-honesty` 的第 ④ 項（`_SEED_SKIP_TAGS` 不留複本）是同一個
> 方向的延伸。

## 6. 階段與回退

| 階段 | 做什麼 | 狀態 | 回退 |
|---|---|---|---|
| **0** | 取代門檻五條（§4.5） | ✅ `dba9e64` `6ae9446` `ba0c8b6` | 無風險，純改善 |
| **1** | `GET /ccsessions` 合併 cc2 列 | ✅ `2b6c33c` | 關 `CC_LIST_INCLUDES_CC2` |
| **2** | `claude_code:{name}` 解析後端 | ✅ `c2d2887` | 同上（同一個旗標） |
| **3** | 寫入面 dispatch（七支） | ✅ `b33ab93` | 同上 |
| **4** | 新 session 走 SDK | ✅ `fa4c82d` | 改回 `CC_NEW_SESSIONS_BACKEND=tmux` |
| ~~**5**~~ | ~~tmux session 歸零後，才談移除 tmux 程式碼~~ | 🔁 **10-31 取消** | — |

> 🔁 **共存修訂：階段 5 取消。** tmux 路徑**長期保留**，理由見檔頭三條
> （認養外部 session、終端機持久化、"棄用" 不等於 "移除"）。
>
> 所以這張表的終點不是「階段 5 完成」，而是**停在階段 4**：
> 新 session 走 cc2，既有 tmux session 繼續由 tmux 服務到它自己結束為止，
> 而使用者永遠可以自己開一條 tmux session 讓 Pocket 看到。
>
> **這也讓回退變成永久能力而不是過渡措施** —— `CC_NEW_SESSIONS_BACKEND=tmux`
> 不再是「切壞了才用」的逃生口，而是一個長期有效的選擇（例如機主想讓新
> session 也能被終端機 attach 時）。

### 旗標與啟用順序（2026-10-30）

**兩個旗標都預設關 → 合併進 main 不會改變 production 任何行為。**

```
CC_LIST_INCLUDES_CC2=1        # 階段 1-3:cc2 session 在 CC 分頁看得到/點得進去/選單能用
CC_NEW_SESSIONS_BACKEND=cc2   # 階段 4:新開的 CC session 走 SDK
```

建議**先只開前者**跑一段。那是階段 1–3 的完整驗收，而且出問題只要關旗標。
確認無誤再開後者 —— 階段 4 之後已建出來的 cc2 session 不會變回 tmux。

### 三處規則必須一致（最容易出的錯）

`_cc_backend()`（寫入面）、`_v2_card_source()`（卡片流）、`_cc2_rows_for_cc_list()`
（列表去重）**都是「先 tmux，找不到才 cc2」**。不一致會出現「列表顯示 tmux
那條、點進去開到 cc2、改名又改到第三個東西」—— 各自看起來都對。
每一處都有測試釘著，而且驗過鑑別力。

### 階段 3 的一個非選配理由

階段 1+2 讓 cc2 對 app 長得像 `claude_code:`，所以 **app 的 ⋮ 選單會對它顯示**，
而那些端點原本一律 404。所以階段 3 不是 polish，是**把階段 1+2 的破口補起來** —— 
不做就是「列表上有、點得進去、一按選單就失敗」。

**每一階段都有獨立旗標、預設關、可單獨回退。** 不接受「做完才知道能不能用」的
大爆炸。

## 7. 這條路的副作用（誠實記錄）

1. **`/ccsessions` 的語意變了**：從「tmux session」變成「CC session」。契約文件
   §4.1 必須同步改寫，否則下一個 client 作者會以為它只有 tmux。
2. **`/app/v2/cc2/*` 變成內部端點**：app 不再需要直接呼叫它們（契約 §4.1c 要
   加註「app 可以不用這一組，它們是 cc2 的原生面」）。但**不要刪**，走查與除錯要用。
3. **cc2 provider 值對 app 仍然可見**（`/app/v2/sessions`、`/app/v2/agents`、
   FleetView、登入狀態頁）。(b) 只解決 CC 分頁；那幾個面會顯示生字串 `cc2`。
   要嘛也一起翻譯成 `claude_code`，要嘛 app 端補 brand —— **待拍板**（§8）。

## 8. 待決

1. **§7.3**：FleetView／登入狀態頁／子程序徽章要不要也把 cc2 翻譯成
   `claude_code`？翻了就是「app 完全不知道 cc2 存在」最徹底的版本，但會失去
   「這條是 SDK 路徑」的可觀察性（除錯時想知道）。
   建議：**列表翻譯、診斷面保留真值**。
2. **§4 命名空間規則**三條照收還是要改。
3. 階段 4 的切換時機 —— 要不要先在機主自己的 session 上跑一段時間。
4. ~~tmux 退場後 `ccsess` CLI 與那些 shell 依賴要不要一起清~~
   → 🔁 **共存修訂：不做。** tmux 路徑長期保留，`ccsess`／`cc_detect.py`／
   `_tmux_*` 全部留著。

---

## 9. 共存要補的事（2026-10-31 新增）

檔頭把方向從「取代」改成「共存」，連帶產生三件原本不需要做的事：

| # | 要做什麼 | 為什麼共存才需要 |
|---|---|---|
| 1 | **兩條路在 app 上要分得出來** | 取代之下 cc2 遲早是唯一路徑，顯示成 `claude_code` 就好；共存之下使用者會**同時**有兩種，而它們能力不同（tmux 那條能用終端機 attach、cc2 那條只能 `claude --resume`）。§8 第 1 點那個「列表翻譯、診斷面保留真值」的建議在共存之下**更站得住**，建議照收。 |
| 2 | **文件要說清楚「什麼時候會拿到哪一條」** | 使用者自己開的 → tmux；在 app 裡按新增 → cc2。這條規則沒寫出來的話，「為什麼這條可以 attach 那條不行」會變成客服問題。官網 FAQ 那條 tmux 教學仍然成立，但要補一句說明兩者的差別。 |
| 3 | ~~撞名防線要真的擋住~~ ✅ **已驗，不用做** | §4 規則 1 在共存之下是唯一防線。**實查 `POST /ccsessions`：已經實作**（註解直接引規格 §4 規則 1，`_cc_backend()` 查過兩邊才放行，撞到回 409 並指出是哪個後端）。這條防線本來就在，共存只是讓它從「遷移期防呆」變成「永久護欄」。 |

### 機主的目標：持久 / 接力 / 遠端操控（2026-10-31）

> 「我目標很單純就是要讓 cc cx 能夠實現持久、接力然後遠端操控的能力」

對著這三個字逐項盤，現況與缺口：

| | 斷線／關手機 | **bridge 重啟** | 接力 | 遠端操控 |
|---|---|---|---|---|
| cc v1 (tmux) | ✅ | ✅ 行程在 tmux 裡 | ✅ 桌面 `tmux attach` | ✅ 但靠 send-keys／capture-pane 刮螢幕 |
| cc2 (SDK) | ✅ | ❌ **回合被打斷** | ✅ `claude --resume <sid>` | ✅ 原生結構化 |
| CX（managed daemon） | ✅ | ✅ daemon 獨立於 bridge | ✅ 桌面與 Pocket 共用 | ✅ |
| CX（stdio 退路，龍蝦） | ✅ | ❌ 子行程隨 bridge 死 | — | ✅ |

**「遠端操控」四條都已經有了；「接力」也都有。唯一真正的缺口是
「bridge 重啟時的持久」，而且只缺在 cc2 與 CX-stdio 兩格。**

按對目標的影響排序，要補的是：

| # | 缺口 | 影響 | 現在的解 |
|---|---|---|---|
| 1 | **cc2 回合不跨 bridge 重啟** | 長工作白跑 | **共存** —— 需要熬過重啟的工作就開在 tmux 那條（`CC_NEW_SESSIONS_BACKEND=tmux`，共存之下它是長期選項不是逃生口） |
| 2 | **龍蝦上 CX 沒有 managed daemon** | 同上，而且龍蝦正是最常重啟的環境 | ⬜ 未解。要在 `install-ubuntu.sh` 加一個 codex daemon 的 systemd unit —— 裝了執行檔不等於有持久化 |
| 3 | `last_event_at` 不跨重啟 | 重啟後 cc2 列排到最底 | ⬜ 未解（§4.5 已記） |

第 2 項是這次新發現的，之前沒有任何文件提過。

### 共存**不需要**做的事（刻意記錄，免得有人又去做）

- 移除 tmux 程式碼（階段 5）
- 清 `ccsess` CLI／`cc_detect.py`／`_tmux_*`
- 把 §4 規則 2 改成 cc2 優先（方向相反了）
- 改官網那條 tmux FAQ —— **它在共存之下仍然是真的**
