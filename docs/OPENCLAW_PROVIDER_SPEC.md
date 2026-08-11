# OpenClaw Provider Spec（Pocket 第四 provider）

> 階段 0 靶機偵察成果（2026-07-27，OpenClaw **2026.7.1-2**，隔離環境
> `~/apps/openclaw-dev`，port 19801，ollama/qwen3:4b 靶機模型）。
> 本文件是 bridge `openclaw_provider.py` 的契約依據；改實作先改這裡。

## 0. 靶機環境（重現步驟）

```sh
# Node ≥24.15 必要（系統 24.14.1 不夠）→ 隔離下載
~/apps/openclaw-dev/node-v24.18.0-darwin-arm64/bin/node
# 安裝:npm prefix 隔離
~/apps/openclaw-dev/npm/node_modules/.bin/openclaw   # 2026.7.1-2
# 隔離 home(絕不碰 ~/.openclaw / 現役 Hermes):
export OPENCLAW_HOME=~/apps/openclaw-dev/home \
       OPENCLAW_STATE_DIR=~/apps/openclaw-dev/home/state \
       OPENCLAW_CONFIG_PATH=~/apps/openclaw-dev/home/openclaw.json \
       OPENCLAW_GATEWAY_TOKEN=openclaw-dev-pocket-7f3a
openclaw gateway run --port 19801 --bind loopback --auth token --allow-unconfigured
```

- 冷啟很慢（插件載入 30s–2min 才 LISTEN），CLI 子命令同樣慢 —— 不是掛掉。
- 靶機模型走本機 ollama（`models.providers.ollama.baseUrl` 必須含 `/v1`）。
- 靶機設定檔見 `~/apps/openclaw-dev/home/openclaw.json`；探測腳本
  `probe.mjs` / `cleanprobe.mjs` 同目錄。
- 未綁任何 TG channel（`channels:{}`）；gateway 有 30 分鐘 heartbeat 自跑
  回合（`isHeartbeat:true` 事件，bridge 需忽略或照常出卡 —— v1 選擇忽略
  heartbeat 回合的推播，卡片照出）。

## 1. 傳輸與握手

- **單一 WebSocket**（`ws://host:port`，同 port 也服務 Control UI 與少數 HTTP：
  `GET /health` → `{"ok":true,"status":"live"}`）。訊框一律 JSON text：
  - req：`{"type":"req","id":"r1","method":"...","params":{...}}`
  - res：`{"type":"res","id":"r1","ok":true,"payload":{...}}`；錯誤
    `ok:false, error:{code,message,details?,retryable?,retryAfterMs?}`
- **握手（三步）**：連上後 server 先推
  `{"type":"event","event":"connect.challenge","payload":{nonce,ts}}` →
  client 送 `connect` req：

```json
{"minProtocol":1,"maxProtocol":5,
 "client":{"id":"cli","version":"<ver>","platform":"macos","mode":"cli"},
 "role":"operator",
 "scopes":["operator.read","operator.write","operator.approvals"],
 "auth":{"token":"<OPENCLAW_TOKEN>"}}
```

  → 回 `hello-ok`：`{protocol:4, server:{version,connId}, features:{methods[],
  events[]}, auth:{role,scopes}, policy:{maxPayload,tickIntervalMs,...}}`。
- **坑**：`client.mode` 用 `webchat` 會觸發 Control UI origin 檢查
  （`CONTROL_UI_ORIGIN_NOT_ALLOWED`）——bridge 一律用 `mode:"cli"`。
- 心跳：server 週期 `tick` 事件（15s）；另有 `health` 事件（含
  `defaultAgentId`、agents/sessions 摘要）。

## 2. 本提案用到的 RPC（protocol 4 實測）

| method | params（實測/schema） | 回應 |
|---|---|---|
| `sessions.list` | `{limit?,offset?,search?,archived?,includeLastMessage?,includeDerivedTitles?,...}` | `{sessions:[SessionRow], count, totalCount, hasMore, defaults:{modelProvider,model,...}}` |
| `chat.history` | `{sessionKey, limit?≤1000, offset?, maxChars?≤5e5}` | `{sessionKey, sessionId, messages:[Msg], sessionInfo:{...}}` |
| `chat.send` | `{sessionKey, message, idempotencyKey!, attachments?:[{type?,mimeType?,fileName?,content(base64)!}], deliver?, timeoutMs?, ...}` | `{runId, status:"started"}`（立即回，回覆走事件） |
| `chat.abort` | `{sessionKey, runId?, preserveSideRuns?}` | ok |
| `sessions.reset` / `sessions.delete` | `{sessionKey…}`（**需 `operator.admin`**，v1 不用） | — |
| `agent` / `agent.wait` | 派回合（另一形態，v1 不用；chat.send 已含完整流） |
| `exec.approval.get/list/resolve` | gateway exec 審批（見 §6 限制） | — |

SessionRow 關鍵欄位（實測）：

```json
{"key":"agent:main:main","kind":"direct","chatType":"direct",
 "sessionId":"3a3dffea-…","updatedAt":1785119370803,"archived":false,
 "status":"done","hasActiveRun":false,"activeRunIds":[],
 "modelProvider":"ollama","model":"qwen3:4b","totalTokens":16009,
 "origin":{"provider":"webchat","surface":"webchat"},"lastChannel":"webchat"}
```

**`chat.send.attachments[]` 的實際受理形狀**（2026-08-11 讀靶機
`dist` 的 `normalizeRpcAttachmentsToChatAttachments`／
`parseMessageWithAttachments` 實證，取代先前「schema 是 `Type.Unknown[]`
所以形狀不明」的說法）：

```jsonc
{"type": "image",              // 選填，只當標籤/檔名 fallback
 "mimeType": "image/png",      // 選填；gateway 會 sniff，不合再以 sniff 為準
 "fileName": "shot.png",       // 選填
 "content": "<base64>"}        // 必填(也吃 data URL 前綴，會被剝掉)
```

- 也接受 Anthropic 風格 `{source:{type:"base64", media_type, data}}`。
- **坑**：normalize 最後 `.filter(a => a.content)` —— **沒有 `content` 的件
  被 gateway 靜默丟棄**。所以 `url` 型附件等於不存在，bridge 一律自己讀檔
  轉 base64 再送。
- 大小：預設每件 20MB（`agents.defaults.mediaMaxMb`），影像另有 6MiB 硬閥；
  整個 WS 訊框受 `policy.maxPayload`（靶機 26MB）限制。超限 `chat.send` 回
  `INVALID_REQUEST`（落盤失敗回 `UNAVAILABLE`），bridge 端先擋並回 413。
- 影像 ≤2MB 走 inline image block 進模型；其餘（含非影像）落 gateway
  `media://inbound/{id}` 並在 message 尾端補 `[media attached: …]`。
- `message` 與 `attachments` 只要其一有值即受理（純附件合法）。

- `sessionKey` 形如 `agent:{agentId}:{name}`（預設 `agent:main:main`）；也接受
  短名 `main`（server 自行歸一）。**bridge 一律存/傳完整 key**。
- 忙碌判定：`hasActiveRun`（或 `status` ∈ start/…；idle=`done`+無 activeRun）。

Msg（chat.history）關鍵欄位：

```json
{"role":"user|assistant|system",
 "content":"純文字（user 舊列）或 [{type:'text',text}...]",
 "timestamp":1785119281390,
 "__openclaw":{"id":"a847d350","kind":"compaction|…","seq":9},
 "provider":"ollama","model":"qwen3:4b","usage":{...},"stopReason":"stop"}
```

- `__openclaw.id` 是穩定 mid → 卡 id 錨點（重放同 id）。
- `__openclaw.kind=="compaction"` 的 system 列是內部整理，不出卡。

## 3. 事件流（chat.send 之後）

事件信封：`{"type":"event","event":"agent"|"chat","payload":{...},"seq":n}`。
廣播給所有已連線 operator（無需訂閱；`sessions.subscribe` 另有粒度訂閱，
v1 直接吃全域廣播 + 以 `sessionKey` 分流）。

### `agent` 事件（payload.stream 分類，實測樣本）

- `lifecycle`：`data.phase` ∈ `start | finishing | end | error |
  fallback_step`；`end/error` 帶 `stopReason/aborted/error/endedAt`。
  → 對映 turn begin/end 與錯誤卡。
- `assistant`：`data:{text:"累積全文", delta:"增量"}` → 串流正文
  （同 runId 累積 upsert 同一張卡）。
- `tool`：`data.phase` ∈ `start | update | result`（**沒有 `end`**；
  2026-08-11 讀靶機 dist `selection-*.js` 實證）。
  - `start`：`{phase, name, toolCallId, args}` → tool_call 卡
  - `update`：`{phase, name, toolCallId, partialResult}` → 同卡 upsert
  - `result`：`{phase, name, toolCallId, result, isError, meta?,
    toolErrorSummary?}`（原生 item backend 另帶 `itemId`/`status`）
    → **tool_result 卡**（`card-oc-{runId}-t{toolCallId}-r`）
  - `result.result` 已被 gateway `sanitizeToolResult` 剝掉 image base64，
    形狀不保證 → 未知形狀退 JSON 字串，不整包丟掉。
- `compaction`：`data.phase start/end` — 內部整理，不出卡。
- 共同欄位：`runId, sessionKey, sessionId, agentId, seq, ts, isHeartbeat`。

### `chat` 事件

```json
{"runId":"…","sessionKey":"agent:main:main","state":"delta|final|error|aborted",
 "deltaText":"BE_OK","message":{"role":"assistant",
 "content":[{"type":"text","text":"PROBE_OK"}],"timestamp":…},
 "errorMessage":"…（state=error 時）","stopReason":"stop"}
```

- `state:"final"`+`message` = 回合定稿（推播掛這裡）；`error` 帶
  `errorMessage`（同時 message 裡也有 ⚠️ 文字）。
- **坑（實測）**：`chat.send` 送出後 gateway 可能立即廣播一則**裸 final**
  （同 runId、無 `message`）——那是 user turn 的 ack / 併入 in-flight run
  的訊號，**不是回合定稿**。判定完成一律要求 `final && message`；
  digest 對裸 final 的行為 = 只清 run 累積、不出卡、不推播。
- **坑（實測）**：緊接送出的第二個 send 可能被併進進行中 run 的佇列，
  最終回覆掛在**別的 runId** 上 —— 逐 run 嚴格對位不可靠，完成語意以
  session 為單位看「帶 message 的 final」。
- **v1 digest 以 `chat` 事件為正文權威**（delta 累積 + final 覆蓋），
  `agent.lifecycle` 只驅動 turn/status，`agent.tool` 出工具卡 ——
  兩流都掛 runId，卡 id 錨 `card-oc-{runId}`。

## 4. 能力對照（caps 宣告依據）

| capability | OpenClaw 支撐 | v1 宣告 |
|---|---|---|
| `input` | `chat.send`（idempotencyKey 必填 → 天然冪等） | ✅ |
| `interrupt` | `chat.abort {sessionKey}`(忙碌判定雙軌:digest busy OR sessions.list `hasActiveRun` —— send 剛排隊、lifecycle 未 start 的窗口實測踩過) | ✅ |
| `replay` / `follow` | `chat.history` seed + 事件流 → SessionCardStore ring | ✅ |
| `attachments` | `chat.send.attachments[]`（`{type?,mimeType?,fileName?,content(base64)!}`） | ✅（bridge 讀檔轉 base64 送出；讀不到/超限一律報錯，不靜默丟） |
| `approve` | `exec.approval.*` / `plugin.approval.*`（gateway exec 與外掛審批） | ✅（→ Approval Hub + approval 卡） |
| `keys` | 無 TUI 概念 | ⛔ |

四 provider 能力矩陣（v2 `/app/v2/sessions` 宣告）：

| | claude_code | codex | hermes | openclaw |
|---|---|---|---|---|
| input | ✅ | ✅ | ✅ | ✅ |
| interrupt | ✅ | ✅ | ✅(ACP cancel) | ✅(chat.abort) |
| keys | ✅(tmux) | — | — | — |
| attachments | ✅ | ✅ | ✅ | ✅(chat.send.attachments) |
| replay/follow | ✅ | ✅ | ✅ | ✅ |
| approve | 條件 | 條件 | ✅ | ✅(exec/plugin.approval.*) |

### 4.1 審批（`exec.approval.*` / `plugin.approval.*`）

2026-08-11 讀靶機 `dist`（`approval-shared-*.js`、`exec-approval-*.js`、
`plugin-approval-*.js`）實證的線上形狀：

| 方向 | 名稱 | 形狀 |
|---|---|---|
| event | `exec.approval.requested` / `plugin.approval.requested` | `{id, request{…}, createdAtMs, expiresAtMs}` |
| event | `exec.approval.resolved` / `plugin.approval.resolved` | `{id, decision, resolvedBy, ts, request}` |
| method | `exec.approval.resolve` / `plugin.approval.resolve` | params `{id, decision}` → `{ok:true}` |
| method | `exec.approval.list` / `plugin.approval.list` | 無參數 → **裸陣列**，元素同 requested payload |

- `decision` ∈ `allow-once` / `allow-always` / `deny`（**沒有** `approved: bool`
  形態）。每筆的可用集合在 `request.allowedDecisions`：`ask=="always"` 時
  只有 `["allow-once","deny"]`；送不允許的值回 `INVALID_REQUEST`
  + `details.reason=APPROVAL_ALLOW_ALWAYS_UNAVAILABLE`。
- 同一個 decision 重送是冪等（`{ok:true}`）；不同 decision 回
  `approval already resolved`。
- exec 的 `request`：`{command, commandPreview?, commandArgv?, cwd, host,
  nodeId, agentId, sessionKey, security, ask, warningText, commandAnalysis,
  allowedDecisions, systemRunPlan, …}`。plugin 的 `request`：
  `{pluginId, title, description, severity, toolName, toolCallId,
  allowedDecisions, agentId, sessionKey, …}`。
- **兩族事件都要 `operator.approvals` scope**（bridge 握手已申請，§1）。
- **坑**：決議者自己收不到 `*.resolved` 廣播（gateway 以 connId 排除），
  所以 bridge 決議完必須自己收尾卡片，不能等事件回來。
- **坑**：`request.sessionKey` 可能是 `null`（非 session 觸發的 exec）——
  這種待審沒有可歸屬的對話，只進審核中心、不出卡。
- bridge 對映：pending → canonical `approvals` 表（`provider="openclaw"`,
  `session_id="openclaw:{sessionKey}"`, `kind="permission"`,
  `options` = allowedDecisions）＋ approval 卡＋推播；決議走統一路由
  `POST /app/v2/sessions/{id}/approve {approval_id, key}`。
- 重連補洞：`*.approval.list` 重掃（事件無 `since` 重放，§6-4）。

## 5. Bridge 對映設計（openclaw_provider.py）

- **連線設定**：`OPENCLAW_BASE_URL`（`ws://…` 或 `http://…` 自動轉 ws）＋
  `OPENCLAW_TOKEN`。env 優先；否則讀 `~/.pocket/openclaw.json`
  （`PUT /app/v1/openclaw/config` 寫入，App 帳號頁「進階」對接）。
  **兩者皆缺 → provider 靜默缺席**（照 APNs 金鑰缺席模式）：
  v2 sessions 不列、dashboard 不給 `openclaw` 鍵、agents 不列，
  log 只記一次 `openclaw_disabled`。
- **單一常駐 WS 客戶端**（`websockets` 15，bridge venv 已有）：
  斷線指數退避重連；`call()` 逾時 30s；事件 dispatch 到
  `_OC_CARD_DIGESTS[sessionKey]`（有人訂閱過才餵，同 cx 模式）。
- **v2 id 形狀**：`openclaw:{sessionKey}`（sessionKey 含冒號 →
  `partition(":", 1)` 之後全部是 key，路由端注意）。
- **digest**：`OpenClawDigest(ApprovalCardMixin)` 仿 `CodexThreadDigest`：
  - seed：`chat.history`（濾 compaction/heartbeat 雜訊）→ `card-oc-h-{mid}`
  - live：`chat` delta/final → `card-oc-{runId}` 累積 upsert;
    `agent.lifecycle` → turn begin/end + status label;
    `agent.tool` → `card-oc-{runId}-t{n}` tool_call 卡 +
    `…-t{n}-r` tool_result 卡（§3）
  - **非文字 block**：`message.content` 的 `image`/`audio`/`file` block
    不再被丟掉 —— 轉成卡片 `body.attachments` 摘要
    （`{kind, mime?, filename?, size?, omitted?}`）＋ `fallback_text`。
    純圖片訊息因此不會再整則消失。
  - 審批：`exec/plugin.approval.requested` → approval 卡（§4.1）
  - 錯誤：`chat.state=error` → system text 卡 ⚠️
- **推播**：`chat.state=final && message && !isHeartbeat` → `push_notify`
  （標題 = session 顯示名 `OpenClaw · {key 短名}`，body = 正文前 140 字，
  `pocket.kind=message, sessionId=openclaw:{sessionKey}`）。
- **dashboard**：`_dashboard_sessions()` 加 `openclaw:{working,idle}`
  （未配置 → 鍵缺席，App 端 optional decode 自動不畫）；
  取數走 `sessions.list`（limit 20，5s timeout，失敗標 degraded）。

## 6. 已知限制（v1 明示不做）

1. ~~**approve 缺席**~~ → 2026-08-11 已接（見 §4.1）。**仍未做**：靶機端
   端到端實跑未驗證（觸發條件 = agent 跑系統指令 + exec 政策 ask，靶機
   純聊天流無法穩定重現），形狀依 dist 原始碼判讀；`allow-always` 的
   persistence 語意（寫進 host 的 `exec-approvals.json` allowlist）由
   gateway 自理，bridge 不碰。
2. **attachments 送得出去、收不回來**：`chat.send.attachments` 已接（§2/§4），
   但**回讀方向**仍缺：`chat.history` / `chat` 事件會把 image/audio 的
   base64 剝成 `{omitted:true, bytes:N}`，真正的位元組要另走
   `media://inbound/{id}` → artifacts 下載管線。v1 對這類 block 只出
   附件摘要（`fallback_text` 帶得出來，不再整則消失），不入 media store。
3. **sessions.reset/delete 需 operator.admin**：v1 連線只申請
   read/write/approvals，不提供刪除/重置 UI。
4. **事件無 since 補洞**：gateway 事件 `seq` 是連線內序號，斷線期間事件
   不可重放 → 重連後對「有訂閱者的 session」強制重 seed（chat.history），
   與 CC follower 斷檔重掃同精神。
5. **QR/runtime 配置**：App 端只做手動 base_url+token（v1）；QR 配對
   （device.pair / openclaw pairing 流程）留 TODO。
6. **heartbeat 回合**：`isHeartbeat:true` 不推播;卡片照出（可回看）。

## 7. 靶機驗證紀錄

- `sessions.list` 空庫 → `count:0`；發話後 `agent:main:main` 出現，
  `status:"done"`, `hasActiveRun:false`。
- `chat.send "Reply with exactly: PROBE_OK"` → `{runId,status:"started"}` →
  `agent lifecycle start` → `assistant delta "PRO"/"BE_OK"`（`chat` delta
  同步）→ `chat final message.content=[{text:"PROBE_OK"}]` →
  `chat.history` 對得上（`__openclaw.id` 穩定）。
- 錯誤路徑實測：模型 404 → `lifecycle error` + `chat state:error`
  （`errorMessage` + ⚠️ assistant message 雙軌）→ 錯誤卡設計依此。
- 並發互撞實測：同 session 兩回合排隊 → 第二回合
  `session file changed while embedded prompt lock was released` →
  bridge 不做佇列管理，`chat.send` 回 202 由 gateway 排隊即可，
  錯誤照 §3 錯誤卡呈現。
