# Provider 能力矩陣與缺口體檢（2026-11-17）

**性質**：唯讀體檢。只讀源碼 + 打唯讀 GET。未送任何訊息、未開 session、未改產品碼、未合併、未重啟。

**觸發**：機主實測「CC 的檔案附件在 Pocket 看不到」，擔心還有其他沒人發現的洞。
（院訓：**靜默失敗是我們最大病原**；代理指標會說謊，要探行為不探存活。）

## 體檢範圍與證據來源

| 來源 | 內容 |
|---|---|
| 源碼 | `~/apps/hermes-openwebui-bridge` @ `main` `2616541`（= launchd `WorkingDirectory`，即 live 跑的那份 checkout） |
| 源碼 | `~/apps/pa-ship`（app 側）、`static/console/*`（web Console） |
| Live GET | `127.0.0.1:8081`，token 取自 `ai.studio.hermes-bridge.plist` 的 `EnvironmentVariables.BRIDGE_TOKEN` |
| Live GET | `/app/v2/providers/capabilities`、`/app/v2/sessions`(45 列)、`/app/v2/sessions/roster`、13 條 session 的 `/cards`、`/history` 負 offset 對照 |

八家 provider 均經 live 確認存在，session 數：
`openclaw 20`、`claude_code 8`、`codex 8`、`hermes 4`、`cc2 2`、`gemini 1`、`grok 1`、`opencode 1`。
`degraded_providers: []`、`host_name: CashCamp.local`。

---

## 0. 先講結論：最危險的三件事

1. **機主那個 bug 的修法已經寫好了，但躺在一個沒 commit 的 worktree 裡。**
   `~/apps/bridge-ccmedia`（分支 `feat/cc-media-attachments`）HEAD 停在 `main` 同一顆
   commit，**工作區有 507 行未提交改動**（`bridge.py`/`carddigest.py`/`cc_sdk.py`/
   `media_artifacts.py`/`docs/APP_BRIDGE_CONTRACT.md` 全部 ` M`，外加兩支未追蹤的新測試
   `tests/test_cc_media_attach_e2e.py`、`tests/test_cc_media_attachments.py`）。
   內容正是本次缺口的解（`vet_attachable_path`、`ATTACHABLE_KINDS` 納入 PDF/文件/壓縮檔、
   `attach_captured_media(..., emit=True)` 把 `media_id`/`download_url` 補到 `attachment` 卡、
   cc2 經 `MEDIA_ATTACH` 一起接上）。
   **這是目前全場最高風險項：一個 `git worktree remove` 或清磁碟就全沒了，而且它不在任何
   備份/PR 裡。** 建議第一個動作就是把它 commit 到分支上（不必合併）。

2. **「宣告 vs 實作」真正不符的那一類，方向跟直覺相反**：不是「宣告有、其實沒有」，
   而是**兩張能力表互相打架**，以及**per-session caps 把文件規定的動態宣告寫成靜態**。
   詳見 §C。

3. **`/cards` 與 `/history` 是兩套 offset 契約**，而 `/history` 的負 offset 被
   靜默夾成 0（live 實證：`offset=-6` 與 `offset=0` 回完全相同結果）。詳見 §B-6。

---

## A. 能力矩陣（列=八家）

標記：✅ 完整 ｜ ⚠️ 部分/有坑 ｜ ❌ 無 ｜ — 不適用 ｜ ❓ 未驗證（需動態驗證）

| # | 面向 | claude_code | cc2 | codex | gemini | grok | opencode | openclaw | hermes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 列表/roster + activity 六態 | ⚠️ | ⚠️ | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ⚠️ |
| 2 | 卡片流 seed + SSE | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ |
| 3a | 附件**上行**（人→agent） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 3b | 附件**下行**（agent→人） | ⚠️ | ⚠️ | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ |
| 4 | 審批（宣告 vs 實作） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 | interrupt | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ⚠️ | ⚠️ |
| 6 | history/resume + 負 offset | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ❌ | ❌ |
| 7 | rename / archive / pin | ✅/✅/❌ | ✅/✅/❌ | ❌/✅/❌ | ✅/✅/❌ | ✅/✅/❌ | ✅/✅/❌ | ❌/❌/❌ | ❌/❌/❌ |
| 8 | 模型/設定（read/write） | ❌/❌ | ❌/✅ | ✅/✅ | ❌/❌ | ❌/❌ | ✅/✅ | ❌/❌ | ❌/❌ |
| 9 | needs_input 連動 / 未讀可用 | ✅/⚠️ | ✅/❌ | ✅/⚠️ | ✅/⚠️ | ✅/❌ | ✅/❌ | ✅/⚠️ | ✅/❌ |
| 10 | 分享擴充 + 捷徑目的地 | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| — | **app 端有入口** | ✅ | ❌ 近乎隱形 | ✅ | ✅ | ⚠️ 無儀表板列 | ⚠️ 僅儀表板 | ❌ 無 enum case | ✅ |

第 7 欄 `pin`：八家全 ❌ —— server 端**沒有 session 層 pin**（§B-7）。
第 8 欄 claude_code 標 ❌/❌ 指的是 **v2 統一控制面**；它另有 v1 專屬入口（§B-8）。
第 9 欄「未讀可用」❌ = 該家 `last_event_at` live 實測恆 `null`，而未讀是純客戶端
拿該欄位比對（§B-9）。第 10 欄 hermes = 分享擴充的 `persona` 目的地。

逐格證據見 §B。

---

## B. 逐欄證據

### B-1 列表 / roster / activity 六態

`/app/v2/sessions` 與 `/app/v2/sessions/roster` 都回 45 列、八家齊全、`degraded_providers: []`
（roster 疊在 v2_sessions 之上，provider 邏輯零複製 — `bridge.py:22081-22100`），
所以**列表本身八家都在，沒有「看不到」的家**。扣分來自三個欄位的形狀不一致：

**(a) `last_event_at` 有三種型別**（live 實測，同一支 API 同一個欄位）：

| provider | 型別 | 樣本 |
|---|---|---|
| claude_code | ISO-8601 字串（6/8 列為 `null`） | `"2026-11-17T01:18:56Z"` |
| codex | ISO-8601 字串 | `"2026-11-17T00:18:06Z"` |
| gemini | ISO-8601 字串 | `"2026-11-07T12:08:21Z"` |
| openclaw | epoch float | `1794859200.116` |
| cc2 / grok / opencode / hermes | 恆 `null`（全部列） | `null` |

bridge 自己知道這件事：`_roster_ts_epoch`（`bridge.py:21988-21998`）的 docstring 寫
「`last_event_at` 的兩種形狀（ISO 字串 / epoch float）→ epoch」。
也就是說**roster 內部有做歸一化，但 `/app/v2/sessions` 對外仍直接吐三種型別**，
任何直接讀 `/sessions` 排序的客戶端都得自己做這件事。

**(b) hermes 的 `last_event_at` 語意是錯的** — `bridge.py:21825`：

```python
"last_event_at": pend.get("created_at") if pend else None,
```

它是「pending 審批的建立時間」，不是「最後事件時間」。沒有待審時恆 `null`。
roster 靠 `_roster_enrich` 另外撈 event_log 才補回 `updated_ts`（live 確實有值），
所以 roster 正確、`/sessions` 誤導。

**(c) activity 六態：兩態實務上到不了。**
`_ROSTER_ACTIVITY_BY_STATUS`（`bridge.py:21966-21978`）可產
`working/needs_input/dead/completed`，加上尾段的 `idle/dormant`。
live 45 列實際只出現 **`dormant 27` / `idle 18`** 兩種。
`needs_input` 八家都有接線（§B-9），`dead` 需 `meta.degraded`，
但 **`completed`** 要 list-status 為 `done`/`completed` —— 而
gemini/grok/opencode 的 row builder 把 status 寫死成三元
（`gemini_provider.py:1040`、`grok_sdk.py:516`、`opencode_sdk.py:649`
皆為 `"running" if busy else "idle"`，再由 `_gm_row_align`/`_gk_row_align`/
`_oce_row_align` 疊 `waiting_approval`），openclaw 的
`session_status()`（`openclaw_provider.py:371-375`）只回 `running`/`idle`。
**這四家永遠不會是 `completed`。** 標 ⚠️ 的是這四家 + claude_code/cc2/hermes
的 `last_event_at` 問題。

### B-2 卡片流 seed + SSE

`/app/v2/sessions/{id}/cards` 與 `/events` 都走單一 dispatcher `_v2_card_store()`
（`bridge.py:30220-30245`），八家（`cx`/`hp`/`oc`/`gm`/`gk`/`oce`/`cc2` + `cc` fallback）都在表上。
live 13 條 session 抽樣全部 HTTP 200，信封形狀**完全一致**：
`{cards, durable_seq, has_more, latest_seq, status}`，卡片欄位一致
`{id, turn_id, role, kind, rev, final, ts, body, seq}`。這一欄是全場做得最乾淨的。

兩個坑：

**(a) `status` 有時是 `{}`。** 抽樣 15 條 session：

| provider | status 空 `{}` | status 有內容 |
|---|---|---|
| cc2 | 2 | 0 |
| claude_code | 1 | 2 |
| hermes | 1 | 2 |
| codex / openclaw | 0 | 3 |
| gemini / grok / opencode | 0 | 1 |

沒有附著 live runtime 的 session 回 `status: {}` —— 不是 `null`、也不是
`{phase:"idle"}`。客戶端讀 `status.phase` 會拿到 undefined。**cc2 抽樣 2/2 全空。**

**(b) hermes 的 `durable_seq` 恆 0**（兩條抽樣皆 `durable_seq: 0`，而
`latest_seq` 是 3137 / 227），其餘七家 `durable_seq` 都是六位數。
hermes 沒有走持久事件層，重啟後這些卡靠 canonical 重建。
另外 cc2 出現 `durable_seq=249401` 但 `latest_seq=2` 的落差。
（對應既有舊案「事件副本落後」。）

### B-3 附件／媒體 ← **機主報的洞在這裡**

必須拆成**上行**與**下行**兩軸，因為兩邊接線程度差很多。

#### B-3a 上行（人送檔給 agent）：八家全通 ✅

八家各自的 input core 都收 `attachments` 並過 `_att_guard`：

| provider | 入口 | 卡片落地 |
|---|---|---|
| claude_code | `_cc_input_core_locked` `bridge.py:18798`，`att_meta` `18810-18833` | `_cc_feed_input_accepted(...)` `18846` |
| codex | `_codex_input_items` `bridge.py:9609`，`_att_guard` `9614` | `_cx_feed_input_accepted(...)` `22428/22452` |
| cc2 | `v2_session_input` `bridge.py:22353-22400` | `sess.submit(content, attachments=att_meta, ...)` `22400` |
| gemini | `bridge.py:28482` | `d.message_card({... "attachments": att_meta ...})` `28538-28539` |
| grok | `bridge.py:29081` | 同款 `29124-29125` |
| opencode | `bridge.py:29705` | 同款 `29750-29751` |
| openclaw | `_oc_input_core` `bridge.py:30149-30160`，`_oc_attachments_payload` `30125` | `d.user_card(content, idem, attachments=att_summary)` `30178` |
| hermes | `_persona_prepare_turn` `bridge.py:34291-34296` | canonical → `message_card` `carddigest.py:1960-1989` |

#### B-3b 下行（agent 產出/讀取的檔案要讓人看到）：**只有 codex 完整**

這是機主那個 bug 的所在。先看關鍵事實：

> **`carddigest.attach_captured_media` 在整個 `bridge.py`（40696 行）只被呼叫一次** ——
> `bridge.py:24114`，而該呼叫位於 `_cx_seed_card_digest`（`bridge.py:24080`，**codex 專屬**）。

`_schedule_media_capture` / `_media_capture_sync` 的所有呼叫點歸屬：

| 呼叫點 | 所屬 | 擷取進媒體庫 | 回寫到卡片 |
|---|---|---|---|
| `bridge.py:23300` `_cc_digest_lines` | claude_code | ✅ | ❌ **回傳值被丟掉** |
| `bridge.py:23614` `_cx_cards_feed` | codex | ✅ | （live 事件自帶） |
| `bridge.py:24106` → `24114` | codex seed | ✅ | ✅ **唯一一處** |
| `bridge.py:24226/24318/24346/34444` | hermes | ✅ | ❌ |
| `bridge.py:28536` | gemini | ✅（僅上行件） | ❌ |
| `bridge.py:29122` | grok | ✅（僅上行件） | ❌ |
| `bridge.py:29748` | opencode | ✅（僅上行件） | ❌ |
| —（無） | **cc2** | ❌ 完全沒接 | ❌ |
| —（無） | **openclaw** | ❌ 完全沒接 | ❌ |

**CC 下行的三段式真相**（`carddigest.cc_event_to_cards`，`carddigest.py:405-518`）：

1. **MCP 工具直回 base64 圖** → 出 `attachment` 卡，**帶 `media_id` + `download_url`** ✅
   （`carddigest.py:440-461`；sink 由 `bridge.py:23247 _cc_image_sink` →
   `media_artifacts.capture_bytes` 提供，`bridge.py:23288` 與 `cc_sdk.py:1017-1020` 兩處都綁了）
2. **工具帶圖片路徑（Read 一張圖）** → 出 `attachment` 卡，但 body 只有
   `{filename, path, mime, fallback_text}` —— **沒有 `media_id`、沒有 `download_url`** ⚠️
   （`carddigest.py:508-515`）
3. **非圖片檔案（pdf/txt/csv/zip/pptx…）** → **完全不出卡** ❌
   守門是 `_IMAGE_EXTS`（`carddigest.py:385`）只含
   `.png .jpg .jpeg .gif .webp .heic .bmp`

而第 2 類為什麼補不回來：`attach_captured_media`（`carddigest.py:86-97`）靠
**「卡片內文是否包含該絕對路徑」**比對：

```python
text = str(body.get("text") or body.get("fallback_text") or "")
hits = [c for c in items if str(c.get("source_ref") or "") and str(c["source_ref"]) in text]
```

`attachment` 卡**沒有 `body.text`**，而 `fallback_text` 是 `f"🖼 {fname}"`（只有檔名，
不是絕對路徑）→ **永遠不命中**。加上這支函式本來就只在 codex 路徑被呼叫 —— **雙重落空**。

**客戶端兩側的反應不同，要分開講（這點很重要，別誤判成 app 沒做）**：

- **iOS app 有做，而且做得對。** `CardStreamView.swift:817-893` 的 kind switch
  **同時**實作了 `.attachment` 卡（`:877-878` → `attachmentRow` `:1482-1496`，
  從 body 的扁平欄位 `filename/mime/path/url/media_id/download_url/...` 組件）
  **與** `.text`/`.markdown` 底下的 `body.attachments` 陣列（`:832-837, 901-906`）。
  這個 switch **沒有任何 `provider ==` 守門**，八家共用。
  所以 **app 不是元凶** —— 它收到什麼就畫什麼。
- **Console 沒做 `attachment` kind。** `static/console/app.js` 全檔
  **沒有任何 `kind === "attachment"` 分支**；`attachmentsHtml()`（`:894-918`）
  只讀 `body.attachments`。CC 的 `attachment` 卡既無 `body.attachments` 又無 `text`，
  於是只渲染出 `fallback_text`（`🖼 x.png`）的單色字泡 —— 正是 ccmedia 分支
  註解自述的**「看得到檔名、點不開」**。

因此 CC 下行的三類檔案在 app 上的實際結局是：

| 類別 | 卡片 body | app 取檔路徑 | 結局 |
|---|---|---|---|
| base64 圖（MCP 工具） | `media_id` + `download_url` | `StudioMediaLoader` 分支 1（`download_url`） | ✅ 看得到 |
| 路徑圖（Read 一張圖） | 只有 `path`（無 `media_id`/`download_url`） | 分支 3 → `GET /file?path=<abs>`（`bridge.py:10217`） | ⚠️ 繞道可通，但繞過媒體庫：沒有 `media_id`、受 `/file` 的 roots 與 `POCKET_FILEBROWSER_ENABLED` 擺布，scratchpad 清掉就死 |
| **非圖片檔**（pdf/pptx/csv/zip…） | **沒有卡** | — | ❌ **徹底隱形 —— 機主報的就是這個** |

> 注意：媒體庫本身早就認得非圖片 —— `media_artifacts.media_kind()`
> （`media_artifacts.py:64-90`）已分類 `pdf`/`text`/`document`/`archive`/`video`/`audio`。
> **唯一的瓶頸是 `carddigest._IMAGE_EXTS`（`carddigest.py:385`）只放七種圖片副檔名。**
> 也就是說：倉庫收得下、app 畫得出來、**只有中間那層翻譯不肯出卡**。

**app 側另一個相關缺口**：`StudioMediaLoader.request(for:)`
（`Pocket iOS/Studio/StudioMedia.swift:356-385`）只有三條解析分支
（`download_url` → `source_url`/`url` → `/file?path=`），
**沒有任何「只憑 `media_id` 取檔」的分支**（`media_id` 只被當快取鍵用，
`StudioMedia.swift:402-407, 546-547`）。
→ 若將來 bridge 送出「只有 `media_id`、沒有 `download_url`」的附件，
app 會算出 `canOpen=false` 或打出空 `path` 的請求。修 bridge 時務必成對附上
`download_url`，別只給 `media_id`。

**live 實證**：13 條 session、約 240 張卡抽樣，**只有 codex 的卡出現 `body.attachments`**
（4 張：`markdown` 1、`tool_call` 2、`tool_result` 1）。其餘七家全部 0 張。

**另外**：openclaw 連上行歷史都被洗掉 —— `_openclaw_v1_message`（`bridge.py:27736-27743`）
把 `"attachments": []` **寫死**，所以就算 gateway 端有附件，過這層就歸零。

#### 為什麼 gemini/grok/opencode/openclaw 的下行是 ❌ 而不是 ⚠️

這四家的 provider 模組裡**完全沒有附件卡產生碼**。整支檔案中
`attachment` / `media_id` / `download_url` 的唯一命中就是它們**宣告** caps 的那一行：

| 檔案:行 | 內容 |
|---|---|
| `openclaw_provider.py:393` | `"capabilities": ["input","interrupt","attachments","replay","follow","approve"]` |
| `grok_sdk.py:518` | `"capabilities": ["input","interrupt","attachments", ...]` |
| `opencode_sdk.py:653` | `"capabilities": ["input","interrupt","attachments", ..., "config"]` |
| `gemini_provider.py:1042` | `"capabilities": ["input","interrupt","attachments", ...]` |

hermes 標 ⚠️ 是因為它有**真**的下行路徑（`carddigest.py:1980-1989`，canonical message
自帶 attachments 時會保留 `media_id`/`download_url` 等 9 個欄位），但 agent 自己產的檔案
不會被自動偵測掛上。

### B-4 審批（宣告 vs 實作）

**這一欄是乾淨的。** `/app/v2/sessions/{id}/approve`（`bridge.py:22206-22308`）八家都有實分支：
`cc`(22241)、`hp`(22248)、`oc`(22255)、`cc2`(22265)、`gm`/`gk`/`oce`(22289)、codex fallback(22297)，
外加 `approval_id` 統一 body 快速路徑(22237)。`_PROVIDER_CAPS` 的 `approvals: true` ×8 **屬實**。

兩個註腳：
- cc2 曾有一個真 bug（註解標 2026-10-29）：cc2 的 body 會掉進 Codex 分支、對錯 thread 下決定 —— 已修。
- gemini/grok/opencode 刻意**拒收**沒有 `approval_id` 的舊形狀 body（回 400，而非掉進 Codex fallback）—— 這是對的設計。

### B-5 interrupt

`v2_session_interrupt`（`bridge.py:22629-22706`）是**手寫 if/elif，不走 `_V2_SESSION_OPS`**，
八家都有分支。但品質差很多，而且**與 caps 宣告方向相反**（見 §C-1）：

| provider | 實作 | 評 |
|---|---|---|
| claude_code | `_cc_interrupt_core`（tmux Esc + 驗證重試） | ✅ |
| cc2 | `cc_sdk` SDK `interrupt()`，無活躍 turn → 409 | ✅ |
| codex | `client.interrupt_turn` | ✅ |
| grok | ACP `session/cancel`，無活躍 turn 一律 409 | ✅ |
| opencode | 同款；註解載明實測 0.7s 內回 `stopReason:"cancelled"` | ✅ |
| gemini | plain-ACP 走 **JSON-RPC notification、無回覆**（`gemini_provider.py:689-698`）= 射後不理；antigravity 後端才是真 `proc.kill()`（`gemini_provider.py:975-982`） | ⚠️ 半真 |
| openclaw | `chat.abort` + 雙軌忙碌判定（`bridge.py:22668-22696`）；RPC 逾時就當忙硬送 | ⚠️ 盡力而為 |
| hermes | ACP cancel + 驗證重試 ×3，但**重試用盡仍回 `ok: True`**（`bridge.py:34771-34790`） | ⚠️ 軟失敗 |

> 現行 `POCKET_GEMINI_BACKEND=antigravity`（plist 第 56 行），所以 gemini 這條**目前**是真 kill；
> 但一旦切回 plain-ACP 就退化成 fire-and-forget。這是組態決定行為、caps 不會跟著變的那類坑。

### B-6 history / resume + 負 offset 契約

**這裡有兩套 offset 契約，而且只有一套支援負值。**

**(a) `/app/v2/sessions/{id}/cards?offset=` → 負值取尾，八家一致 ✅**
單一實作 `SessionCardStore.page()`（`carddigest.py:1745-1780`）：

```python
start = max(0, total + off) if off < 0 else min(off, total)
```

live 對照（`limit=3`）：

| provider | `offset=-6` seqs | `offset=0` seqs |
|---|---|---|
| claude_code | `[284, 290, 294]` | `[1, 2, 3]` |
| codex | `[1475, 1476, 1477]` | `[2, 3, 4]` |
| gemini | `[7, 8, 9]` | `[1, 2, 3]` |
| hermes | `[202, 1108, 1111]` ⚠️ | `[1, 2, 3]` |

八家行為一致（取尾正確）。**但 hermes 的尾窗 seq 稀疏跳號**（202 → 1108 → 1111，
而 `latest_seq=3137`），其餘家是連號。配合 `durable_seq=0`，hermes 的分頁位置不可靠。

同時：`window_start` / `total_cards` / `has_more_after` **只在明確帶 `offset` 時才出現**
（不帶 `offset` 走 `snapshot()`，信封裡沒有這三個欄位）。`has_more` 的語意是
**往前還有歷史**（`has_more = start > 0`），往後要看 `has_more_after` ——
拿 `has_more` 做正向分頁判斷會永遠為真。Console 完全沒讀這三個欄位
（`static/console/*.js` 對 `offset` 零命中），它走的是 `before_id` cursor。

**(b) `/app/v2/sessions/{id}/history?offset=` → 負值被靜默夾成 0 ❌**

live 實證（`limit=3`，`offset=-6` 與 `offset=0` 回**完全相同**結果）：

| provider | HTTP | 回應形狀 | `-6` vs `0` |
|---|---|---|---|
| claude_code | 200 | `{text, more}` | 相同 |
| codex | 200 | `{text, more, nextCursor}` | 相同（本就不吃 offset，走 cursor） |
| gemini | 200 | `{messages, more, total}` | **相同** |
| cc2 | 200 | `{messages, more, total, truncated}` | **相同** |
| grok | 200 | `{messages, more, total}` | — |
| opencode | 200 | `{messages, more, total}` | — |
| openclaw | **404** | `PROVIDER_OP_UNSUPPORTED` | — |
| hermes | **404** | `PROVIDER_OP_UNSUPPORTED` | — |

夾值位置：`_cc_op_history`→`cc_session_history`（`bridge.py:18569-18594`，
`end = max(0, total - max(0, offset))`）、`v2_cc2_history`（`bridge.py:24719-24733`）、
`_gm_op_history`（`25120-25126`）、`_gk_op_history`（`25190`）、`_oce_op_history`（`25254`）
全部 `max(0, int(offset or 0))`。

**附帶發現：`/history` 回應有四種形狀** —— `{text, more}`（CC）、
`{text, more, nextCursor}`（CX）、`{messages, more, total}`（gemini/grok/opencode）、
`{messages, more, total, truncated}`（cc2）。CC/CX 回的是**文字大塊**而非訊息陣列。
客戶端非 per-provider 特判不可。

openclaw/hermes 的 404 **是誠實的**（錯誤碼 + 指名支援清單），不是靜默失敗。

### B-7 rename / archive / pin

`_V2_SESSION_OPS`（`bridge.py:25343-25386`）是單一真相，而 `/providers/capabilities` 的
`ops` **直接從它推導**（`bridge.py:25524`：
`ops = sorted(op for op, tbl in _V2_SESSION_OPS.items() if prov in tbl)`）
→ **ops 這一類不會漂移，缺格都是「宣告出來的缺」而非靜默失敗**。

| op | 有 | 無 |
|---|---|---|
| rename | claude_code, cc2, gemini, grok, opencode | codex, openclaw, hermes |
| archive | claude_code, codex, cc2, gemini, grok, opencode | openclaw, hermes |
| history | claude_code, codex, cc2, gemini, grok, opencode | openclaw, hermes |

每個缺格在 `tests/test_v2_session_ops.py:31-62` 的 `_V2_OPS_KNOWN_GAPS` 都有架構理由，
CI 會擋沒理由的缺格。**這是本 repo 做得最好的一塊守門，建議其他維度照抄。**

**pin：八家全無，而且是 `ops` 裡根本不存在的 op。**
server 端唯一的 pin 是 `PUT`/`GET /app/v1/sessions/{sid}/pin`（`bridge.py:33913-33966`），
那是**訊息層**的 `message_meta.pinned`（provider 無關的訊息標註），
**不是「把這個工作釘到列表頂端」**。session 層 pin 無端點、無表格、無 caps 欄位。

### B-8 模型 / 設定讀寫

| provider | config_read | config_write | 備註 |
|---|---|---|---|
| codex | ✅ | ✅ | inline `bridge.py:22715-22816`，含寫入前 `CX_UNKNOWN_MODEL` 護欄 |
| opencode | ✅ | ✅ | 協定一級公民（`session/set_config_option`） |
| cc2 | ❌ | ✅ | 唯寫；現值由 row 的 `meta` 回報（live 確認 `meta.permission_mode`） |
| claude_code | ❌ | ❌ | 另有 v1 專屬入口 `POST /ccsessions/{name}/mode`(21508)、`/model`(21610) |
| gemini | ❌ | ❌ | 模型是單一長命 ACP 子程序的 spawn 參數；全域 `/app/v1/gemini/config` |
| grok | ❌ | ❌ | ACP 有 `session/set_config_option` **但沒接線**（KNOWN_GAPS 自述「管線存在≠端到端通」） |
| openclaw | ❌ | ❌ | 設定住 gateway，bridge 唯讀 |
| hermes | ❌ | ❌ | 設定 = persona 定義本身 |

全部如實反映在 `ops`，無誤報。

### B-9 未讀 / needs_input 連動

**needs_input：八家全通 ✅。** `_roster_activity`（`bridge.py:22061-22078`）由
`status == "waiting_approval"` 或 `meta.approval` 導出，各家 row builder 都會疊：
cc_sdk（`cc_sdk.py:2012`）、hermes（`bridge.py:21807`）、
gemini `_gm_row_align`（`28400`）、grok `_gk_row_align`（`29031`）、
opencode `_oce_row_align`（`29654`）、openclaw（`21916-21917`）、codex（自家 row builder）。
gemini 這條曾是真缺口（`_gm_row_align` docstring 自述舊行為「gemini 只有 busy→running」，
審批在列表上隱形、在 180 秒窗內靜默逾時），已修。

**未讀：server 端完全不存在 ⚠️。** `bridge.py` 全檔沒有 `unread_count` 之類欄位；
依 `bridge.py:15498-15499`、`15523-15526` 的註解，未讀/鈴鐺是**純客戶端**行為，
拿 `last_event_at` / `inbound_ts` 跟本地「最後看過」比對。

**這就是 §B-1(a)(b) 為什麼會痛**：未讀偵測的輸入值
（`last_event_at`）對 **cc2/grok/opencode/hermes 恆為 `null`**，
hermes 還是「審批建立時間」的錯語意。
**未讀靠一個四家恆空、三種型別、一家語意錯的欄位** ——
與既有舊案「未讀不亮 / 列表預覽怪 / 進場閃舊訊息」同一個根。

**附帶**：hermes 的 `last_turn_summary` 會漏生 HTML —— live roster 實測
`hermes:xcash` 的摘要是 `"<details><su…"`。`_roster_summary_text`
（`bridge.py:22001-22007`）只去 markdown 圍欄（`^```[a-z-]*`），不處理 HTML 標籤。

### B-10 分享擴充 + 捷徑目的地涵蓋

見 §D（app 端）。

---

## C. 「caps 宣告 vs 實作不符」清單（最危險那類）

重要前提：**`ops` 不在此列** —— 它從 `_V2_SESSION_OPS` 推導，結構上不會漂移。
真正的問題集中在**另外兩張表**，而且它們**互相打架**。

三張表並存：

| 表 | 位置 | 對外欄位 |
|---|---|---|
| ① `_PROVIDER_CAPS` | `bridge.py:25408-25471` | `/app/v2/providers/capabilities` 的 `interrupt`/`approvals`/… |
| ② per-session caps 陣列 | 各 provider 模組 + `bridge.py:21826` 等 | `/app/v2/sessions[].capabilities` |
| ③ `_V2_SESSION_OPS` | `bridge.py:25343` | `capabilities.ops`（推導，可信） |

### C-1 【最高】表①與表②對 `interrupt` 的宣告相反

| provider | 表① `_PROVIDER_CAPS.interrupt` | 表② per-session `capabilities` | 實際有實作？ |
|---|---|---|---|
| gemini | **`false`** | **含 `"interrupt"`**（`gemini_provider.py:1042`） | 有（品質 ⚠️） |
| openclaw | **`false`** | **含 `"interrupt"`**（`openclaw_provider.py:393`） | 有（品質 ⚠️） |
| hermes | `false` | 不含（`bridge.py:21826`） | 有（軟失敗 ⚠️） |

live 回應可直接對照：`/providers/capabilities` 說 gemini `"interrupt": false`，
但 `/sessions` 裡 gemini 那列的 `capabilities` 是
`["input","interrupt","attachments","replay","follow","approve"]`。

**影響取決於 app 讀哪張表**：讀表①→停止鍵被藏起來（功能活著但使用者碰不到）；
讀表②→畫出按鈕，按下去對 gemini 是射後不理、對 openclaw 是盡力而為。
**兩張表都對同一個旗標發言，而且答案相反** —— 這是最該先修的一格。

### C-2 【高】表②把文件規定的「動態宣告」寫成靜態 `approve`

`docs/APP_BRIDGE_CONTRACT.md:1131` 明文：

> `approve` ｜ 有待核准動作（**動態：pending 時才宣告**）｜ cc, cx, persona

live 實測（45 列全部無待審）：

| provider | caps 含 `approve`？ | 符合契約？ |
|---|---|---|
| claude_code / cc2 / codex | **否** | ✅ 動態，正確 |
| gemini / grok / opencode / openclaw / hermes | **是**（恆含） | ❌ 靜態寫死 |

寫死位置：`gemini_provider.py:1042`、`grok_sdk.py:518`、`opencode_sdk.py:653`、
`openclaw_provider.py:393`、`bridge.py:21826`(hermes)。
→ app 若照契約把 `approve` 當「現在有事等你審」的訊號，這五家會**恆亮**。

### C-3 【高】表② `attachments` 五家名實不符（= 機主那個 bug 的宣告面）

八家 per-session caps **全部**含 `"attachments"`，但**下行**實況見 §B-3b：
gemini/grok/opencode/openclaw 的模組裡沒有任何附件卡產生碼。
且契約 `:1132` 只列 `cc, cx, persona` 三家 —— **文件與實作雙向都不同步**。

`attachments` 這個旗標同時被當成「我收得下你傳的檔」（上行，八家真的都通）
與「我給得出檔給你看」（下行，只有 codex 完整）。**一個旗標兩種語意**，
這是為什麼宣告看起來沒錯、使用者卻看不到檔案。
→ 建議拆成 `attachments_in` / `attachments_out` 兩個旗標。

### C-4 【中】契約 §4.3 能力表整體過期

`docs/APP_BRIDGE_CONTRACT.md:1126-1134` 每一列的「有的 session 類」都只寫
`cc, cx, persona`（含 `replay`/`follow` 標 `all`）。該表早於
cc2/gemini/grok/opencode/openclaw 五家。
`:1142` 還寫著「cx 待 S2、persona 待 S3」—— 兩者 live 都早已上線。
文件比實作落後五家，照文件做判斷的人會做錯。

### C-5 【中】delegation 偽 provider 的硬編碼 allowlist

`_delegation_v2_sessions`（`bridge.py:3967-3991`）：

```python
caps = ["input", "attachments", "replay", "follow"]
if d.get("provider") in ("codex", "claude_code"):
    caps.append("interrupt")
if d.get("provider") in ("codex", "claude_code") and st == "waiting_approval":
    caps.append("approve")
```

`approval` 物件也只對 codex 回填（`3978-3980`）。
→ 被委派出去的 cc2/gemini/grok/opencode/openclaw/hermes 工作，在
delegation 列上**永遠不能中斷、不能審批**，即使底層 provider 支援。
`AGENT_TEAM=1`、`AGENT_CALL=1` 已開（plist 第 9/13 行），所以這條路徑是活的。

### C-6 【低但要記】組態能讓 caps 說謊而 caps 不自知

`POCKET_GEMINI_BACKEND=antigravity` 決定 gemini 的 interrupt 是真 kill 還是
fire-and-forget（§B-5），但 `_PROVIDER_CAPS` 的 gemini 格是靜態字面值。
同類：`ACP_RESILIENCE` / `OPENCLAW_RESILIENCE` / `CC_SDK_PROVIDER` 都會改變
行為而不改變宣告。

---

## D. app 端 UI 缺口（對照 caps）

**根本問題：app 沒有單一 provider 名錄。** 正典 enum 在
`Pocket iOS/Studio/AgentSessionModel.swift:14-47`，但**不是 `CaseIterable`** ——
全檔沒有 `Provider.allCases`，所以沒有任何東西能機械式列舉「那八家」。
每個表面（底部 tab、側抽屜、儀表板、推播路由、分享擴充、捷徑、用量面板）
各自維護一份**手寫清單**，而且彼此不一致。

**`opencode` 在整個 Swift 專案零出現**（`grep -rln "opencode" --include=*.swift` 回 0 筆）。
連 enum case 都沒有 → 任何 `opencode:*` session 解碼成 `.unknown`，顯示成通用「Agent」。

### D-1 入口涵蓋表

| provider | 底部 tab | 側抽屜 | 儀表板列 | 專屬 session 頁 |
|---|---|---|---|---|
| hermes | ✅ `.chat` | ✅ `StudioChatUI.swift:3130-3143` | ✅ | `PersonaCardSessionView` |
| claude_code | ✅ `.cc` | ✅ `:3148-3151` | ✅ | `CCCardSessionView` |
| codex | ✅ `.cx` | ✅ `:3153-3156` | ✅ | `CodexCardSessionView` |
| gemini | ✅（旗標 `GeminiSurface.hidden`） | ✅ `:3159-3164` | ✅ | 通用 `AgentCardSessionView` |
| grok | ✅（caps 守門 `:663-668, 720-726`） | ✅ 同守門 `:3171-3176` | ❌ **無** | 通用 |
| openclaw | ❌ 無 | ❌ 無 | ✅ `StudioDashboard.swift:919` | `OpenClawSessionListView` |
| **cc2** | ❌ | ❌ | ❌ | ❌ |
| **opencode** | ❌ | ❌ | ❌ | ❌（連 enum case 都沒有） |

機主已知的兩個缺口**完全證實**：opencode 無頁（是「完全不存在」等級）、
grok 無儀表板列（`StudioDashboard.swift:818` 註解自述「原本這張卡是五條 provider 列」）。

**新發現：cc2 其實是八家裡最隱形的一家。** 它解碼正確
（`Provider.cc2`，顯示名 "Claude Code (SDK)"），品牌圖示只在
`ChildSessionsPanel.swift:354` 子代理樹裡出現；無 tab、無抽屜、無儀表板列，
推播還會掉進 `SessionRoute.generic` 直接丟到儀表板
（`StudioChatUI.swift:4514-4524`，以 `warn` 等級記「不支援的 provider」）。
repo 內已有原話註解 `AgentSessionModel.swift:17-19`：**「cc2 session 在手機上近乎隱形」**。
而 live 有 2 條 cc2 session、bridge 側 cc2 的 ops 是八家最全的之一
（rename/archive/history/config_write 都有）—— 典型的「bridge 做好了、app 側消費落後」。

### D-2 硬編碼 switch 漏家（都是靜默 default）

| 位置 | 有的 case | 漏的 | 落空行為 |
|---|---|---|---|
| `StudioChatUI.swift:4203-4241` `handleSettleAdvance` | claude_code, codex | 其餘六家 | `default: return false` —— 收尾後跳下一條工作**靜默無作用** |
| `StudioChatUI.swift:3076-3089` `openApprovalSession` | claude_code, codex | 其餘六家 | `default: break` —— 點審批**無法深連**進 session，停在列表 |
| `StudioChatUI.swift:4308-4311` 歸屬判定 | claude_code, codex | 其餘六家 | — |
| `StudioConnections.swift:6-8` `SessionToolKind` | 僅 `.cc`/`.codex`（二值 enum） | 結構上排除其餘 | 無法表達第三家 |
| `SendToPocketIntent.swift:113-121` | hermes, claude_code, codex, gemini | cc2, grok, openclaw, opencode | `throw ShareBridgeError.badResponse` |
| `UsagePanelView.swift:406-428` | claude_code, codex, gemini, openclaw, hermes | cc2, grok, opencode | 優雅降級（顯示原始字串）—— 僅外觀 |

反例（做對的）：`PushPayload.swift:83-133` `SessionRoute.parse` 有
`.generic(provider:name:)` 兜底，註解載明是**因為 openclaw/cc2/gemini 白名單漏家
一再復發**才加的（2026-10-29）。但兜底只是「落到儀表板 + warn」，不是深連。

### D-3 審批 UI

- **審批卡本身八家都畫得出來** —— `kind: "approval"` 走
  `CardStreamView.swift:871-872`，無 provider 守門、也**不看** `SessionCapability.approve`
  （`grep "has(\.approve)"` 在產品碼**零命中**，只有測試檔）。
  → 這其實解釋了 §C-2 的靜態 `approve` 目前沒造成災難：**app 根本不讀那個旗標**。
  但這是「兩個錯互相抵消」，不是對的設計。
- **決議送出**統一無 provider 分支（`StudioBridgeV2.swift:319-331`）。
- **從審批深連進 session 是硬編碼兩家 allowlist**（見 D-2）。

### D-4 interrupt / rename / archive / pin / 模型設定的守門方式

**唯一真正讀 bridge caps 的地方**是 `ProviderCapsStore`
（`StudioBridgeV2.swift:690-738`，吃 `GET /app/v2/providers/capabilities`）：
`declares()` 查無 → false（藏起來）；`allows()` 查無 → **true（fail open）**。
`SessionOp` 經它守門的只有 `rename`/`archive`/`history`/`config_read`/`config_write`
（`SessionControlPanel.swift:40-52`）。`SessionControlPanel.swift:30` 還明文自律
**「一行 `if provider ==` 都不准有」** —— 這塊設計是對的。

**但餵資料進那塊面板的宿主 view 全是硬編碼**（`AgentCardSessionView.swift:77-94`，
gemini/openclaw/grok 共用這個宿主）：

```swift
private var canCompact: Bool { sessionID.hasPrefix("codex:") || sessionID.hasPrefix("gemini:") }
private var canBrowseMedia: Bool { isGemini }          // 註解:openclaw/grok 的媒體索引支援未驗證,先不開
private var canReadHistory: Bool { isNamedProvider }   // = isGemini || isGrok
private var canConfigureRuntime: Bool { isGemini }     // 模型設定:只有 gemini
private var canRemoteRename: Bool { isNamedProvider && ProviderCapsStore.shared.allows(providerKey, "rename") }
```

結論：

| 功能 | app 守門方式 | 後果 |
|---|---|---|
| interrupt | **完全不守門**（`grep "has(\.interrupt)"` 產品碼零命中），永遠給 Stop 鍵，靠 bridge 409 說話 | 與 §C-1 的 caps 矛盾無關了 —— app 兩張表都沒讀。gemini 射後不理會看起來「按了有反應」 |
| pin | **完全不守門**（`AgentNamedSessionListView.swift:422` 無 caps 檢查） | 而 server 端 session 層 pin **根本不存在**（§B-7）→ 這顆鍵在做什麼需動態驗證 |
| rename / history / archive | **先**硬編碼 `isNamedProvider`（只有 gemini+grok）**再**疊 caps | openclaw、cc2 即使 bridge 有能力也拿不到 UI（cc2 的 rename/archive/history bridge 側都有） |
| 模型設定 | **硬編碼 gemini 一家**，caps 完全不查 | grok 就算宣告 config 能力也不會有 UI（`:91-93` 註解自述「grok 的 per-session 模型是下一波」）；**opencode 是唯一 config_read+write 雙全的非 codex provider（§B-8），app 卻連它的 enum case 都沒有** |
| 媒體抽屜 | **硬編碼 gemini 一家** | openclaw/grok 永遠沒有「媒體」入口 |

### D-5 分享擴充 + 捷徑目的地涵蓋（矩陣第 10 欄）

**兩者都硬編碼成恰好四家**：`persona`(hermes)、`claudeCode`、`codex`、`gemini`。

`Pocket Shared/ShareBridgeClient.swift:22-37` 的 `Kind` 是四值 enum；
`destinations()`（`:207-242`）發**恰好四個**並行查詢，沒有第五個。
`:219-222` 註解顯示這份清單是手工逐家「晉升」維護的
（gemini 2026-11-13 才補，原因正是「分享面板落後 app 自己的 tab」）。

捷徑 / App Intents（`SendToPocketIntent.swift`，註解 `:6-12`）直接建在同一份
`destinations()` 上 → **繼承同樣的四家限制**，另有自己那份多餘的
`kind(forPrefix:)` switch（`:113-121`）會對 `cc2:`/`grok:`/`openclaw:`/`opencode:` 丟錯。
捷徑的附件**對所有 provider 都未實作**（`:14-15`「附件目前不收」）。

→ 矩陣第 10 欄定稿：**hermes/claude_code/codex/gemini = ✅；cc2/grok/openclaw/opencode = ❌**（非 ❓）。

---

## E. Console（web）盤點

`static/console/` 純 vanilla JS，無 build step。

### E-1 硬編碼 provider 清單（漏家一覽）

| 清單 | 位置 | 有 | **漏** |
|---|---|---|---|
| `PROVIDER_LABEL` | `app.js:203` | claude_code, codex, hermes, gemini, openclaw, cc2 | **grok, opencode** → 顯示原始 id |
| `byProv` 側欄分組順序 | `app.js:310` | hermes, claude_code, codex, gemini（cc2 併入 claude_code `:312`） | **grok, opencode, openclaw** → 有兜底建組，但排序位置不定 |
| `memory.js` `PROV` | `memory.js:10` | claude_code, codex, hermes | **cc2, gemini, grok, opencode, openclaw** |
| 記憶搜尋 provider 下拉 | `index.html:161-164` | `cc`, `cx`, `hermes`（**另一套短碼詞彙**） | **cc2/gemini/grok/opencode/openclaw 無法篩選** |
| 開新工作 kind 下拉 | `index.html:183-186` | `cc`, `cx` | **其餘六家無法從 Console 開新 session** |

`PROVIDER_LABEL` 全部有 `|| s.provider` 兜底，所以 grok/opencode 不會空白，
但會顯示內部 id（字面 `grok`/`opencode`），與其他六家不一致。
**無 provider 專屬 icon/color map**，所以沒有「圖示空白」風險。

`createSession()`（`app.js:1494-1519`）硬編碼二分支：
`kind === "cc"` → `POST /ccsessions`，**`else` → `POST /codexsessions`**。
→ 任何非 `cc` 的值都會**靜默路由到 codex**。目前下拉只有兩項所以碰不到，
但這是個等著被觸發的靜默錯誤。

### E-2 卡片 kind 渲染

`PROSE_KINDS = new Set(["text","markdown"])`（`app.js:1060`）。
`transcript.js:83-84` 特判 `tool_call`/`tool_result`/`thinking`/`reasoning` 折進
「執行步驟 · N」摺疊區。**其餘 kind（含未來新 kind）會顯示，不會被丟掉**
（`transcript.js:94` → 單色字泡 + `md.renderText`）。
**但全檔沒有 `kind === "attachment"` 分支**；`attachmentsHtml()`（`app.js:894-918`）
無條件對**任何** kind 讀 `body.attachments`。
→ 一張 `kind:"attachment"` 且無 text 的卡 = 空的單色字泡（見 §B-3b）。

### E-3 Console 支援的操作與守門

守門統一走 `canUse(cap)`（`app.js:605-607`）：
**非借用登入 = 全部放行；借用/`console-remote` 登入才逐項查 session `capabilities`**。
**全檔沒有任何以 `provider` 為鍵的能力判斷。**

| 操作 | 端點 | 守門 |
|---|---|---|
| 送訊息 | `POST /app/v2/sessions/{id}/input` `:1246` | `canUse("input")`；附件另需 `canUse("attachments")` `:1237,1538` |
| interrupt | `POST .../interrupt` `:1289` | `canUse("interrupt")` |
| 審批 | `POST .../approve` `:699` | `actionable()` `:636-645`，需 caps 含 `approve` `:638` |
| 冷載 / 翻更早 | `.../cards` `:532,582` | `replay` `:449,579` |
| live SSE | `.../events` `:747` | `follow` `:231-233,553` |
| diff/changes | `.../changes`,`/diff` | `canUse("changes")`（**注意：`changes` 不在 `/providers/capabilities` 的任何欄位裡**） |
| rename / archive / 模型設定 | — | **未實作** |
| pin | — | **server 端未實作**；僅 `localStorage` 的「記住這個工作」書籤（`app.js:38-49`），與 caps/provider 無關 |
| 開新 session | `/ccsessions`\|`/codexsessions` `:1501,1508` | **不查 caps**，只擋借用裝置；硬編碼兩家 |
| 檔案瀏覽 / 終端 / 記憶 | — | **完全不查 caps**；借用裝置一律只剩 `agents`+`changes`（`filebrowser.js:107`） |

### E-4 端點與分頁

Console **不用 offset**（`static/console/*.js` 對 `offset` 零命中）。
它走 **cursor**：往後 `since_seq`、往前 `before_id`/`older_cursor`
（`app.js:430,539,582,590,748,1641`）。
→ **Console 完全沒用到負 offset 契約**，也沒讀
`window_start`/`total_cards`/`has_more_after`。

### E-5 provider 特判分支

全 Console 只有三處，**全部只為 claude_code/cc2 別名**：
`app.js:312`（cc2 併入 claude_code 組）、`:320`（cc2 降級連動 claude_code 組）、
`:355`（搜尋別名 "Claude Code"）。
codex/gemini/grok/opencode/openclaw/hermes **零特判**。

值得記一筆：`app.js:568-576` 的註解**載明三家審批 payload 形狀不同**
（CC `{key}`、CX `{approve:bool}`、hermes/openclaw `{approval_id, key|approve}`），
但 `decide()`（`:687-718`）**不分支** —— 送一個超集 body
（`Object.assign({}, choice, aid ? {approval_id: aid} : {})`）讓後端自己解讀。
這剛好能用，是因為 bridge 有統一 `approval_id` 快速路徑（§B-4）。

---

## F. 契約一致性表

### F-1 卡片信封：✅ 八家完全一致

`{id, turn_id, role, kind, rev, final, ts, body, seq}`，
回應信封 `{cards, durable_seq, has_more, latest_seq, status}`（+ 帶 offset 時
多 `window_start`/`total_cards`/`has_more_after`）。live 13 條、約 240 張卡零例外。

### F-2 卡片 body 形狀（live 實測）

| provider | 出現過的 kind | body 形狀差異 |
|---|---|---|
| claude_code | text, markdown, tool_call, tool_result | `tool_call` 有時多 `patch`；user text 多 `{client_id, origin, typed_text}` |
| cc2 | text, markdown | 同 CC 子集 |
| codex | text, markdown, tool_call, tool_result | **唯一出現 `attachments`**（4 張）；`tool_call` 多 `detail` |
| gemini | text, markdown | 僅 `{text, fallback_text}` |
| grok | （抽樣 0 張） | ❓ |
| opencode | （抽樣 0 張） | ❓ |
| openclaw | text, markdown | 僅 `{text, fallback_text}` |
| hermes | text, markdown, **choices** | `choices` body = `{detail, fallback_text, options, ref, title}`（**唯一一家**） |

共同基底 `{text, fallback_text}` 穩定。
`kind` 值域（契約 `APP_BRIDGE_CONTRACT.md:1287` 宣告 12 種）：
`text|markdown|tool_call|tool_result|diff|approval|status|table|kv|attachment|calendar_event|choices`。
live 只觀察到 6 種；**`attachment` 一張都沒出現**（正是 §B-3b 的症狀）。

### F-3 `status` 物件:**三種形狀**

| provider | 形狀 |
|---|---|
| claude_code | `{busy, mode:"auto", prompt, phase, label, **usage**{input_tokens, output_tokens, cache_*, total_tokens, model, cost_usd, cost_is_estimate, used, size}}` ← **唯一有 `usage`**，也**唯一 `mode` 非 null** |
| codex | `{busy, mode:null, prompt, phase, **queue_depth**, label, **locked**}` ← 唯一有 `queue_depth`/`locked` |
| gemini / grok / opencode / openclaw / hermes | `{busy, mode:null, prompt, phase, label}` ← 最小集 |
| **任何家的無 runtime session** | **`{}`**（空物件，非 null、非 `{phase:"idle"}`）—— cc2 抽樣 2/2 全空 |

### F-4 `phase` 值域 vs roster 狀態詞彙:**兩套不相交的詞彙**

- **卡片 `status.phase`**（`carddigest.py:1108-1164` + 各家）實際字面值：
  `idle` `run` `queued` `waiting` `stalled` `error` `blocked` `dispatching` `end` `unknown`
- **列表 `status` 字串**（`/app/v2/sessions[].status`，餵 roster 的那個）：
  `idle` `running` `waiting_approval` （+ `stalled`/`dispatching`/`failed`/`error`/`dead`/`done`/`completed` 由 `_ROSTER_ACTIVITY_BY_STATUS` 宣告可接受）
- **roster `activity` 六態**：`working` `idle` `needs_input` `dormant` `completed` `dead`

**三層詞彙互不相同**（`run` vs `running`、`waiting` vs `waiting_approval`、
`end` vs `done`/`completed`）。目前不出錯是因為 `_roster_activity` 讀的是
**列表 status 字串**而不是卡片 phase（`bridge.py:22065`）。
**但這是一個等著被誤用的陷阱**：任何人把卡片 `status.phase` 餵給
`_ROSTER_ACTIVITY_BY_STATUS`，`run`/`waiting`/`end` 全都查不到 → 一律降級成
`idle`/`dormant`，**忙碌中的 session 會顯示成閒置、待審會不亮**。
建議：`_ROSTER_ACTIVITY_BY_STATUS` 加上 phase 詞彙的同義 key，或在型別上把兩者分開命名。

### F-5 `/history` 回應:**四種形狀**（見 §B-6b 表）

`{text, more}`(CC) / `{text, more, nextCursor}`(CX) /
`{messages, more, total}`(gemini/grok/opencode) /
`{messages, more, total, truncated}`(cc2) / **404**(openclaw/hermes)。

### F-6 `last_event_at`:**三種型別 + 一個錯語意**（見 §B-1a/b）

---

## G. 缺口清單（按嚴重度排序）

### G-1 🔴 修法已寫好但沒 commit，隨時會消失

- **現象**：`~/apps/bridge-ccmedia` 工作區 507 行未提交改動 + 2 支未追蹤測試，分支
  `feat/cc-media-attachments` 的 HEAD 仍停在 main 同一顆 commit（`git log main..HEAD` 空）。
- **影響**：機主報的 bug 的完整解法只存在於一個未受版控保護的工作目錄。
  `git worktree remove`、`git clean`、磁碟清理任一個就全沒。不在任何 PR/備份裡。
- **根因推測**：做完沒收尾（對應舊教訓「沒 commit 的工作才最危險」）。
- **估工**：**10 分鐘**（`git add -A && git commit && git push`，不必合併）。**建議列為第一個動作。**

### G-2 🔴 CC/cc2 的非圖片檔案下行徹底隱形（機主原報）

- **現象**：CC 讀/產出 pdf、pptx、csv、zip、txt 時，Pocket 完全沒有附件卡。
- **影響**：使用者以為檔案沒產出；靜默失敗，無錯誤、無佔位。
- **根因（已定位，雙重）**：
  1. `carddigest._IMAGE_EXTS`（`carddigest.py:385`）只放 7 種圖片副檔名 →
     非圖片根本不出 `attachment` 卡（`carddigest.py:507-515`）。
  2. `attach_captured_media`（`carddigest.py:86-97`）靠「卡片內文含絕對路徑」比對，
     而 `attachment` 卡無 `body.text`、`fallback_text` 只有檔名 → **永遠不命中**；
     且該函式**全 bridge 只被 codex seed 呼叫一次**（`bridge.py:24114`）。
  - 媒體庫端**沒問題**（`media_artifacts.media_kind()` 早就分類 pdf/document/archive），
    app 端**也沒問題**（`CardStreamView.swift:877` 有 `.attachment` 分支）。
    **瓶頸只在中間那層翻譯。**
- **估工**：**已完成於 ccmedia 分支**，剩下的是 commit + 驗收 + 合併（含 2 支新測試）。

### G-3 🔴 兩張能力表對 `interrupt` 宣告相反（§C-1）

- **現象**：`/providers/capabilities` 說 gemini/openclaw `interrupt: false`，
  `/sessions[].capabilities` 卻含 `"interrupt"`。
- **影響**：客戶端讀哪張表決定「藏掉活功能」或「畫出半死按鈕」。
  目前 app 兩張表都不讀（一律給 Stop 鍵），**災難被另一個缺陷遮住了** ——
  一旦 app 改成正確讀 caps，立刻會行為反轉。
- **根因推測**：表①（`bridge.py:25408`）2026-11-04 才加入，表②散在各 provider
  模組既有的 row builder 裡，加表①時沒去對齊表②。
- **估工**：**半天**（擇一為真相；建議表②從表①推導，比照 `ops` 的做法）+ 加守門測試。

### G-4 🟠 未讀靠一個四家恆空、三種型別、一家語意錯的欄位（§B-9 + §B-1）

- **現象**：未讀/鈴鐺純客戶端，拿 `last_event_at` 比對本地「最後看過」；
  而該欄位對 **cc2/grok/opencode/hermes 恆 `null`**（live 全列），
  三種型別（ISO 字串 / epoch float / null），hermes 還是「審批建立時間」（`bridge.py:21825`）。
- **影響**：四家的未讀不會亮；跨家排序錯亂。對應既有舊案
  「未讀不亮／列表預覽怪／進場閃舊訊息」—— **這是那個根因未治的舊案的量化證據**。
- **根因推測**：各家 row builder 獨立演化，`last_event_at` 從沒被當成契約欄位統一過；
  roster 內部偷偷做了歸一化（`_roster_ts_epoch`）反而掩蓋了問題。
- **估工**：**1-2 天**（統一成 epoch float、hermes 改讀真事件時間、補四家、加型別守門測試）。

### G-5 🟠 `/history` 負 offset 被靜默夾成 0，且四種回應形狀（§B-6b）

- **現象**：`offset=-6` 與 `offset=0` 回完全相同結果（live 對 gemini/cc2 實證）。
- **影響**：客戶端若假設「offset 到處一樣」，拿到的是第一頁而非尾頁，**無錯誤、無警告**。
  四種回應形狀迫使客戶端 per-provider 特判。
- **根因推測**：`/cards` 的 `page()` 是後來統一的、支援負值；`/history` 是六家各自既有
  端點包一層 `_V2_SESSION_OPS`，每家都自己 `max(0, offset)`。表統一了、語意沒統一。
- **估工**：**半天**（負值在 `_v2_session_op` 層統一轉成尾窗，或明確回 400 拒絕；
  形狀統一是另外 1-2 天）。

### G-6 🟠 app 側 cc2 與 opencode 近乎／完全不存在（§D-1）

- **現象**：cc2 無 tab/抽屜/儀表板列、推播掉兜底；opencode 連 `Provider` enum case 都沒有。
- **影響**：bridge 側 cc2 的 ops 是八家最全之一、opencode 是唯一 config 讀寫雙全的
  非 codex provider —— **做好的能力使用者碰不到**。
- **根因推測**：`Provider` enum 不是 `CaseIterable`、沒有單一名錄，
  每個表面手寫清單 → 新家一定漏（`PushPayload.swift` 的 `.generic` 兜底註解自述此病復發多次）。
- **估工**：**opencode 最小可用 2-3 天**（enum + 通用 `AgentCardSessionView` 掛載 + 儀表板列）；
  **cc2 補入口 1-2 天**；**根治（enum 改 `CaseIterable` + 名錄驅動各表面）1 週**。

### G-7 🟠 分享擴充 + 捷徑硬編碼四家（§D-5）

- **現象**：`ShareBridgeClient.Kind` 四值 enum，`destinations()` 恰好四個查詢。
- **影響**：cc2/grok/openclaw/opencode 無法成為分享/捷徑目的地。捷徑附件全家未實作。
- **根因推測**：手工逐家晉升（註解自述 gemini 2026-11-13 才補，因為「分享面板落後 tab」）。
- **估工**：**1 天**（改成讀 `/app/v2/sessions` 動態建目的地，而非四個硬編碼查詢）。

### G-8 🟡 per-session `approve` 五家寫死成靜態，違反契約（§C-2）

- **現象**：契約 `:1131` 規定「動態：pending 時才宣告」，
  gemini/grok/opencode/openclaw/hermes 恆含 `approve`。
- **影響**：目前無害**只因為 app 不讀這個旗標**（`has(.approve)` 產品碼零命中）。
  Console **會**讀（`app.js:638`）→ 這五家的審批動作鍵恆為可按。
- **估工**：**半天**（五處改成 pending 時才加）。

### G-9 🟡 delegation 偽 provider 硬編碼兩家 allowlist（§C-5）

- **現象**：`bridge.py:3973-3976` 只給 codex/claude_code `interrupt`+`approve`。
- **影響**：委派給其他六家的工作在 delegation 列上不能中斷/審批。`AGENT_TEAM=1` 已開。
- **估工**：**半天**（改查底層 provider 的真實 caps）。

### G-10 🟡 Console 開新 session 的 `else` 靜默路由到 codex（§E-1）

- **現象**：`createSession()` `app.js:1501/1508`，`kind !== "cc"` 一律 `POST /codexsessions`。
- **影響**：目前下拉只兩項碰不到；一旦加第三家而忘了改這裡，**會安靜地開成 codex session**。
- **估工**：**10 分鐘**（改成顯式 map + 未知值丟錯）。

### G-11 🟡 Console 漏家與第三套 provider 詞彙（§E-1）

- **現象**：`PROVIDER_LABEL` 漏 grok/opencode；`memory.js PROV` 漏五家；
  記憶篩選/開新工作下拉用 `cc`/`cx`/`hermes` **短碼**（與 session.provider 不同的詞彙）。
- **影響**：顯示原始 id；五家無法篩記憶；六家無法從 Console 開 session。
- **估工**：**半天**（補 map）+ **1 天**（統一短碼詞彙）。

### G-12 🟡 `completed` activity 四家永遠到不了（§B-1c）

- **現象**：gemini/grok/opencode 的 status 寫死三元、openclaw `session_status()` 只回兩值。
- **影響**：六態實際只有五態可用；live 45 列只出現 `idle`/`dormant`。
- **估工**：**1 天**（四家補終態回報）。

### G-13 🟢 其他已記錄

- hermes `last_turn_summary` 漏 HTML（live：`"<details><su…"`）；
  `_roster_summary_text`（`bridge.py:22001`）只去 markdown 圍欄不去 HTML 標籤。**2 小時**
- hermes `durable_seq` 恆 0 + 尾窗 seq 稀疏跳號（202→1108→1111）。**需先動態驗證**
- `status: {}` 空物件（cc2 2/2）—— 客戶端讀 `status.phase` 拿 undefined。**半天**（回最小集而非空物件）
- session 層 `pin` server 端不存在，但 app（`AgentNamedSessionListView.swift:422`）
  與 Console（localStorage 書籤）各畫了鍵。**需先釐清產品意圖**
- `changes` 是 Console 在用的 capability，但**不在 `/providers/capabilities` 任何欄位**。**2 小時**
- 契約 §4.3 能力表落後五家、`:1142` 還寫「cx 待 S2、persona 待 S3」。**半天**
- app `StudioMediaLoader` 無「只憑 media_id 取檔」分支 → 修 bridge 時務必成對給 `download_url`。**已知即可**

---

## H. 需要動態驗證才能確認的項目（唯讀體檢做不到）

本體檢**沒有**送訊息、開 session、上傳檔案、觸發審批，所以以下只能標「未驗證」。
建議排一輪動態驗收，每項都**探行為不探存活**（HTTP 200 / exit 0 / is-active 都會騙人）。

| # | 項目 | 為什麼唯讀測不到 | 建議驗法 |
|---|---|---|---|
| 1 | **CC 非圖片附件修復後是否真的看得到** | 要讓 CC 實際讀/產出一支 pdf | 在 ccmedia 分支跑：叫 CC `Read` 一支 pdf + 寫一支 csv，然後 `GET /cards` 看有無 `attachment` 卡且帶 `download_url`，再用 app 實機點開 |
| 2 | **CC 路徑圖走 `/file?path=` 是否真的通** | 需要一張真的被 Read 的圖 + 實機點開 | 看 `GET /file?path=<abs>` 回 200 且 bytes 正確；另測 scratchpad 清掉後是否變死連結 |
| 3 | **gemini interrupt 在 plain-ACP 下是否真的中斷** | 現在是 `antigravity` 後端（真 kill）；plain-ACP 是無回覆 notification | 切 `POCKET_GEMINI_BACKEND` 後發長任務再中斷，看是否真停（不是看 HTTP 200） |
| 4 | **hermes interrupt 重試用盡仍回 `ok:true`** | 要製造「cancel 不生效」的狀況 | 讓 hermes 跑長任務、壓住 ACP，看回應 `interrupted` 欄位與實際是否停 |
| 5 | **openclaw interrupt 的「查不到就當忙硬送」** | 需 gateway 事件圈塞住的情境 | 高峰期中斷，確認無活躍 run 時 abort 真的無害 |
| 6 | **八家審批端到端**（宣告 ✅ 但未實測） | 要有真的 pending approval | 每家各觸發一次審批 → 看列表轉 `needs_input` → 從 app/Console 各批一次 → 看卡片轉態 |
| 7 | **非 claude_code/codex 的審批深連**（`openApprovalSession` default: break） | 要有其他家的待審 | 讓 gemini/openclaw 出審批，從通知/審批中心點進去，看是否停在列表 |
| 8 | **cc2 推播掉 `.generic` 兜底的實況** | 要真的收一則 cc2 推播 | 對 cc2 session 觸發推播，看是否落儀表板 + `warn` ErrorLog |
| 9 | **grok / opencode 卡片 body 形狀** | 兩家 live 各只有 1 條 session、0 張卡 | 各送一輪含工具呼叫的訊息，再抽 `/cards` 比對形狀 |
| 10 | **hermes `durable_seq=0` + 尾窗 seq 跳號** | 要知道是設計如此還是副本落後 | 對同一條 hermes session 連續抽 `offset=-N` 看 seq 是否穩定；重啟後看卡片是否還在 |
| 11 | **`pin` 鍵實際行為**（app + Console 都有鍵、server 無 session 層 pin） | 按下去才知道打哪個端點 | app 按 pin 抓封包；確認是打 `/app/v1/sessions/{id}/pin`（訊息層）還是純本地 |
| 12 | **未讀/鈴鐺在四家 `last_event_at=null` 下的實況** | 要真的有新訊息進來 | 對 cc2/grok/opencode/hermes 各送一則，看 app 未讀是否亮 |
| 13 | **`settle & advance` 對六家靜默無作用** | 要走完一輪再按收尾 | 對 gemini/openclaw 收尾，看是否真的不跳下一條 |
| 14 | **openclaw 上行附件是否因 `attachments: []` 寫死而在歷史中消失** | 要先送一個附件再翻歷史 | 送圖給 openclaw → 當下卡片有（`d.user_card`）→ 重載/翻 `_openclaw_v1_messages` 看是否歸零 |
| 15 | **delegation 列上其他六家不能中斷/審批** | 要有真的委派工作 | 委派一條 gemini/cc2 工作，看 delegation 列的 caps 與按鍵 |

---

## 附錄：方法與紅線遵循

- 本輪**只**執行 GET：`/app/v2/providers/capabilities`、`/app/v2/sessions`、
  `/app/v2/sessions/roster`、`/app/v2/sessions/{id}/cards`、
  `/app/v2/sessions/{id}/history`。**零 POST/PUT/DELETE。**
- 未送訊息、未開 session、未上傳、未觸發審批、未改產品碼。
- 依 `CLAUDE.md` 鐵則：**只 commit + push 到 `docs/provider-health` 分支，
  不合併 main、不重啟 `ai.studio.hermes-bridge`。**
- `~/apps/hermes-openwebui-bridge` 工作樹全程留在 `main`（本文件寫在
  worktree `~/apps/bridge-health`）。
- 中途一次 401 風暴（產生指令時 `$T` 未 export）觸發 bridge 的認證速率限制回 429；
  等待後重打成功。**無副作用，但記一筆：bridge 對連續 401 會 429，壓測時要注意。**
