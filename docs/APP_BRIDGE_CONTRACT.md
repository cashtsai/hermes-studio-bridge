# PocketAgent / Studio Bridge Contract

This document is the **single source of truth** for PocketAgent's app-facing
bridge API. PocketAgent should depend on these endpoints instead of Hermes
internals.

> **P1a 契約單一化（2026-07-04）**：本文件併入並取代
> `pocketagent/docs/CONTROL_PLANE_V2.md`（6/29 草案：Session/Agent 抽象、
> capabilities、統一路由）與 `studio-os/docs/PHASE0_TERMINAL_GATEWAY_CONTRACT.md`
> （7/3：卡片 digest 事件流，S1 已上線）。同路徑兩 schema 到此收斂：
> **rendering 面以卡片 digest 為準**（§5–§8）；Session/capabilities/路由抽象
> 保留並改寫成 Hermes 拓撲（§4）。改契約先改本文件。

## 0. 原則（鐵律）

- 運算盡可能放 Mac Studio；手機只做**顯示、接收傳送、本地快取、非本地不可的
  元件**；手機要能**即時跟到處理狀況並及時更新同步**。
- **digest 責任在 bridge**：CC jsonl、codex app-server 事件、persona stream →
  伺服器端統一 parser 產卡片。一份 parser，所有終端共享（手機/ESP32/e-paper
  全吃同一套）。**app 永不解析 provider 原始格式**。
- **SSE 為唯一真相**；輪詢僅在 stream 斷線 >10s 時作 fallback，重連成功即停。
- **fallback 原則**：不認得的事件 type / 卡片 kind 一律靜默降級渲染
  `fallback_text`——舊 client 永不壞。
- **單一權威**：任何 app 要消費的形狀，先寫進本文件再上線。bridge 裡存在但
  本文件未載的表面（歷史上的 `/ccsessions/*`、`/codexsessions/*` TUI 級端點），
  app 一律不得依賴；要用就先在 §4 契約化。

## 1. Authentication

- App-facing endpoints require `Authorization: Bearer <bridge token>`.
- Tokens are configured in the LaunchAgent and PocketAgent settings. Do not
  commit real tokens to git.
- `/health` may be used as a lightweight reachability check.

## 2. Stable v1 Endpoints

### `GET /health`

Returns bridge liveness and persona ids.

Expected shape:

```json
{"ok": true, "personas": ["yuanfang", "pantianqing", "xcash", "shuijing"]}
```

### `GET /capabilities`

Returns the API version, feature flags, and app endpoints. PocketAgent should
use this for compatibility checks.

Required features:

- `canonical_messages`
- `reports`
- `notifications`
- `approvals`
- `attachments`
- `vision`
- `message_dry_run`
- `accounts`
- `apple_auth`
- `account_pairing`
- `delegations`
- `control_plane_v2`

### `POST /app/v1/auth/apple`

Verifies a Sign in with Apple identity token and upserts the durable account row.
This endpoint is authenticated by the Apple JWT itself, not by the bridge bearer
token.

Request fields:

- `apple_user_id`: required, must match the verified JWT `sub`.
- `identityToken`: required Apple identity token.
- `email`: optional; Apple may only provide it on first authorization.
- `display_name`: optional.

Rules:

- JWT signature must validate against Apple's JWKS.
- `iss` must be `https://appleid.apple.com`.
- `aud` must match configured `APPLE_ID_AUDIENCES` (`com.pocketagent.ios` for M1).
- Invalid tokens return `401`.
- The response includes an account session token for account-scoped endpoints.

### `GET /app/v1/account`

Returns the current Apple account and its non-revoked paired devices. Requires an
account session from `POST /app/v1/auth/apple`, sent as
`X-Pocket-Account-Session: <session>` or as `Authorization: Bearer <session>`.

Rules:

- Device bearer tokens are never returned; only a short token hash may be shown.
- `include_revoked=true` may be used for account-management views.

### `POST /app/v1/pair/new` and `POST /app/v1/pair/claim`

Account-bound pairing flow. The desktop calls `pair/new` with both a bridge
bearer token and an account session; the phone calls `pair/claim` with the
single-use code. The bound code is the phone-side credential, so the phone does
not need its own account session. If the phone does send an account session, its
Apple user id must match the code's bound Apple user id.

Rules:

- Pairing codes are single-use and expire after five minutes.
- `pair/new` returns `{code, ttl, account_bound}` and never returns a bearer
  token.
- `pair/claim` returns the new per-device bearer token once, plus `device_id`.
- Existing legacy `/pair/*` endpoints remain for compatibility, but new app
  flows should use `/app/v1/pair/*`.

### `POST /app/v1/devices/{id}/revoke`

Revokes one account-bound device for the current Apple user. Requires an account
session. Revoked device tokens must no longer authenticate app-facing bridge
endpoints.

### `GET /app/v1/sessions`

Returns persona and task sessions visible to the app.

Persona sessions must include:

- `id`
- `type`
- `name`
- `preview`
- `status`

Delegation sessions also include `work_order`, `provider_session_id`, and
`takeover` so Pocket can continue work started from Telegram or another app
surface.

### `GET /app/v1/delegations`

Returns durable CC/CX work-order sessions created by any Hermes persona.

### `POST /app/v1/delegations`

Creates a provider-native child session and records its Hermes ownership.

Required fields:

- `parent_persona`: `xcash`, `pantianqing`, `shuijing`, or `yuanfang`.
- `provider`: `codex`/`cx` or `claude_code`/`cc`.
- `objective`: the task.
- `cwd`: the local project path.

The response includes a `work_order` and `takeover` metadata. Pocket should show
the work order in the session list and may continue via the unified endpoint:

`POST /app/v1/delegations/{id-or-work_order}/input`

See `docs/DELEGATION_CONTROL_PLANE.md` for the full contract.

### `GET /app/v1/messages?session=<persona>&limit=<n>`

Returns canonical app messages merged with server-side persona history. For
`yuanfang`, scheduled reports may also be surfaced in the conversation.

Rules:

- Unknown `session` returns `400`.
- Messages are oldest to newest.
- Each message should include `role`, `content`, `ts`, `status`, and `source`
  when available.

### `POST /app/v1/messages`

Streams one persona turn as OpenAI-style SSE.

Request fields:

- `session`: required persona id.
- `content`: user text.
- `attachments`: optional array of `{kind, filename, mime, data}`.
- `client_id`: optional stable id for retry/idempotency.
- `dry_run`: when true, verifies the path without calling Hermes or persisting
  canonical messages.

Rules:

- Unknown `session` returns `400`.
- `dry_run` must not write canonical user or assistant messages.
- Normal successful turns should persist the user message and assistant reply.
- The stream may include additive top-level `status` metadata chunks, for
  example `accepted`, `queued`, `running`, or `replayed`; clients should use
  them for delivery/working UI and must not persist them as assistant text.
- The stream ends with `data: [DONE]`.

### `GET /app/v1/approvals`

Returns approval cards for app review.

### `POST /app/v1/approvals/{id}/decision`

Records approve/reject decisions. PocketAgent must not call this in smoke tests.

### `GET /reports`

Returns scheduled reports for app reading surfaces.

### `GET /cron/jobs` and `POST /cron/jobs/{id}/{action}`

Exposes notification-producing jobs. Use this carefully because it affects both
app and Telegram delivery.

## 3. v1 遷移備註（persona 事件）

`GET /app/v1/messages/events`（persona SSE）與 `POST /app/v1/messages` 串流
屬 v1 相容表面：S3 完成後 persona 事件統一走 §5 v2 信封，v1 保留相容期。
新 client 功能不再加在 v1 事件流上。

## 4. `/app/v2` 統一控制面（Session / Agent / Capabilities）

> 來源：CONTROL_PLANE_V2（6/29 拍板）的 Session/Agent 抽象與統一路由，
> **provider 矩陣改寫成 Hermes 拓撲**：session 只有三種——`hp:` persona、
> `cc:` Claude Code、`cx:` Codex；**delegations 是連結 persona 與 cc/cx 兩層的
> 一等公民**。telegram / gmail / calendar 不是 session provider：TG 是 persona
> session 的另一個表面（已合流，開成 provider 會出現兩份對話）；Gmail/Calendar
> 是 persona 的工具，正確形態是 studio-card。v2 是疊加的 facade，v1 與
> provider 內部不動。

### 4.1 Session

```jsonc
{
  "id": "claude_code:pocket-agent",   // 全域唯一,{provider}:{native_id} 全寫（6/29 決策 #1）
  "provider": "claude_code|codex|hermes",
  "title": "pocket-agent",
  "subtitle": "/Users/xcash/apps/pocketagent",  // workdir / 來源說明
  "status": "idle|running|waiting_approval|failed|done",
  "last_event_at": "2026-07-04T10:33:42Z",
  "capabilities": ["input","interrupt","approve","attachments","keys","replay","follow"],
  "meta": { /* provider 專屬,app 不硬依賴 */ }
}
```

- **session id wire format 全寫**：`claude_code:{name}`、`codex:{thread_id}`、
  `hermes:{persona}`；delegation 列為 `delegation:{id}`（provider 欄仍是
  `claude_code|codex`）。`hp:`/`cc:`/`cx:` 是文件與路標用的三類簡稱，
  wire 上不用縮寫。routing 只 split 第一個 `:`。
- **delegation 是一等 row**：`GET /app/v2/sessions` 內 delegation 置頂列出，
  `meta` 必含 `work_order` 與 `takeover`（另含 `delegation` 完整物件與
  pending `approval`）。同一 Codex thread 已被 delegation 收養時，
  不再重複出現在裸 `codex:` 列。

### 4.2 Agent

```jsonc
{
  "provider": "claude_code",
  "name": "Claude Code",
  "kind": "code_agent|persona",
  "status": "ready|needs_auth|unavailable",
  "auth": { "connected": true, "account": null },
  "can_create": true                 // 能不能 POST /sessions 開新的（現況僅 codex）
}
```

### 4.3 Capabilities（session 宣告 → app 決定顯示什麼控制）

| capability | 意義 | 有的 session 類 |
|---|---|---|
| `input` | 可送訊息/指令 | cc, cx, persona |
| `interrupt` | 可停止當前 turn | cc, cx |
| `keys` | 可送 TUI 控制鍵(↑↓⏎ 等) | cc |
| `approve` | 有待核准動作（動態：pending 時才宣告） | cc, cx, persona |
| `attachments` | 可附圖/檔 | cc, cx, persona |
| `replay` | 可載入更早歷史 | all |
| `follow` | 有 live 串流 | all |

### 4.4 端點總表（含實作狀態）

| Method · Path | 用途 | 狀態 |
|---|---|---|
| `GET  /app/v2/agents` | 後端清單 + auth/health | ✅ 上線 |
| `GET  /app/v2/sessions?provider=&status=` | 統一 session 清單（delegations 一等 row） | ✅ 上線 |
| `GET  /app/v2/sessions/{id}/cards?limit=&before_seq=` | 冷載 snapshot（§7） | ✅ 上線（S1：cc；cx 待 S2、persona 待 S3） |
| `GET  /app/v2/sessions/{id}/events?since_seq=&profile=` | SSE 卡片事件流（§5） | ✅ 上線（S1：cc；同上） |
| `POST /app/v2/sessions/{id}/approve` | 核准/拒絕（body: `{approve: bool}` 或 `{decision}`；`for_session` 可記住） | ✅ 上線（現路由僅 cx；cc/persona 併入待批次 2 統一路由） |
| `POST /app/v2/sessions/{id}/input` | 送訊息/指令(可帶 attachments) | ⏳ 批次 2（統一路由；現走 v1 `/app/v1/delegations/{…}/input` 與 `/app/v1/messages`） |
| `POST /app/v2/sessions/{id}/interrupt` | 停止當前 turn | ⏳ 批次 2 |
| `POST /app/v2/sessions/{id}/key` | 送控制鍵(僅 `keys`) | ⏳ 後期（TUI 級,契約先佔位） |
| `POST /app/v2/sessions` | 開新 session | ⏳ 後期（現走 v1 `POST /app/v1/delegations`） |
| `GET  /app/v2/notifications` | 通知 feed | ⏳ 後期（P2 推播批次同步考慮） |
| `GET  /app/v2/audit` | 稽核紀錄 | ⏳ 後期 |

- `approve` 錯誤語意：無 pending 核准回 `409 APPROVAL_NOT_PENDING`；
  session id 不是可核准類回 `400`；查無 delegation 回 `404`。
- CONTROL_PLANE_V2 原表的 `GET/POST /app/v2/connectors*` **刪除**（Hermes
  拓撲下無 connector provider）。

## 5. 統一 Session 事件流（rendering 權威）

```
GET /app/v2/sessions/{session_id}/events?since_seq=N&profile=phone     (SSE)
```

- **事件信封**：`{"seq": int, "ts": epoch, "type": str, "data": {...}}`
- **seq**：per-session 嚴格遞增。bridge 保留 ring buffer（近 2000 事件或 7 天），
  `since_seq` 補洞重放；超出範圍回 `410 Gone`（`SEQ_GONE`）→ app 改走
  snapshot 冷載。
- **事件類型**：
  - `card.upsert` — `{card}`（見 §6）。串流中的訊息＝同一 card id 反覆 upsert、
    `rev` 遞增、`final:false→true`。**app 只做替換渲染，永不解析 provider
    原始格式**。
  - `session.status` — `{busy, mode, prompt, phase, label}`。`label` 是
    **伺服器給的人話階段**（「思考中」「執行工具:Bash」「等待核准」「回覆中」）
    ——手機「即時跟到處理狀況」的直接載體，UI 原樣顯示。
  - `turn` — `{state: "begin"|"end"|"interrupted", turn_id}`。
  - `ping` — keepalive，統一 `SSE_KEEPALIVE_SECS`。
- **真相原則**：SSE 為唯一真相；輪詢僅在 stream 斷線 >10s 時作 fallback，
  重連成功即停。
- 取代關係：CONTROL_PLANE_V2 的
  `assistant_delta|tool_start|tool_result|thinking|…` 結構化事件 schema
  **由卡片 digest 取代**，不再實作；其 `seq`+`since_seq` 續傳設計保留如上。

## 6. 卡片 schema v1（裝置 UI 語言）

```json
{
  "id": "card-…",          // 穩定 id;串流中不變
  "turn_id": "…",
  "role": "user"|"assistant"|"system",
  "kind": "text"|"markdown"|"tool_call"|"tool_result"|"diff"|"approval"|"status"|"table"|"kv"|"attachment",
  "rev": 3,                 // 同 id 遞增;app 以最高 rev 為準
  "final": false,
  "ts": epoch,
  "body": { ... }           // per-kind;所有 kind 必附 "fallback_text"
}
```

- per-kind body 重點：`tool_call {tool, summary, detail?, patch?}`、
  `approval {approval_id, title, options[{key,label,style}], source}`、
  `diff {path, adds, dels, hunks_text}`、`status {label, spinner:bool}`。
- `tool_call.patch`（選配，2026-07-04 diff 卡缺口）：`{path, text, adds, dels}`
  — **該步驟自身**的變更內容，由 digest 從工具輸入（Edit old/new、Write
  content…）合成，不依賴 worktree 事後狀態——步驟過後再 commit 也能回看單步
  變更，replay 重放產出相同 patch。`text` 為 `-`/`+` 前綴行、hunk 以 `@@`
  分隔（**無行號**——事件裡沒有整檔上下文，digest 不回讀檔案以保 replay
  穩定）；上限 20k 截斷。app 不認得就忽略（fallback 原則）。
- **fallback 原則**：不認得的 kind 一律渲染 `fallback_text`——舊 client 永不壞。
- **digest 責任在 bridge**：CC jsonl、codex app-server 事件、persona stream →
  統一 parser（`carddigest.py`）產卡片。**一份 parser，伺服器端，所有終端共享**。

## 7. 冷載 snapshot（本地快取契約）

```
GET /app/v2/sessions/{session_id}/cards?limit=100&before_seq=M
```

回 `{cards: […], latest_seq: N}` → app 渲染後從 `since_seq=N` 接 SSE。
`limit` 上限 500。**app 本地快取＝卡片庫**（key: session_id + card.id + rev）：
離線可讀、進場秒開；快取只是 snapshot，永不當真相。

## 8. 裝置 profile（為衛星終端預留）

`profile=phone|compact|bitmap`：

- `phone`（v0 唯一實作）：完整 markdown body。
- `compact`（T-Embed 級預留）：`body.lines[]` 伺服器預先折行的純文字 + 卡片摘要。
- `bitmap`（e-paper 預留）：伺服器渲染點陣圖 URL。

契約先佔欄位，bridge v0 對非 phone 回 `400 UNSUPPORTED_PROFILE`。

## 9. 遷移切片與現況

| 切片 | 內容 | 狀態 |
|---|---|---|
| S0 | persona `/app/v1/messages/events` 落地 | ✅ 已上線 |
| S1 | CC sessions 走卡片契約（digest CC jsonl → cards + events + snapshot） | ✅ bridge 已上線（#19）；app `SessionView` 接線＝批次 1 |
| S2 | Codex sessions 同上（app-server 事件 → 卡片流） | ⏳ 批次 1 |
| S3 | persona 事件統一到 v2 信封（v1 留相容期）＋ app persona 線切 v2 | ⏳ 批次 2 |
| S4 | SubSessionView/SUBSESSIONS 通道退役 | ⏳ 批次 2 |

app 端先行件（不等 bridge）：卡片渲染元件 + 傳輸層抽象
（`SessionEventTransport`）以 fixture JSON 開發驗證，bridge 落地即接線。

## 10. 驗收基準（每切片同標準）

1. 手機進場長 transcript：**零客戶端解析**、冷載 <1s（快取）+ 增量接流。
2. 執行中任務：`status.label` 全程有人話階段顯示，無「不知道在幹嘛」空窗。
3. 斷線 10s 重連：`since_seq` 補洞無缺漏、無重複卡片。
4. 舊 client 相容：不認得的事件/卡片 kind 靜默降級 fallback_text。

## 11. Smoke Test Expectations

- `/health` returns `ok: true`.
- `/capabilities` includes all required features.
- `/app/v1/sessions` returns all four personas.
- `/app/v1/delegations` returns a JSON object with `delegations`.
- `/app/v2/sessions` returns a JSON object with `sessions`.
- `/app/v2/agents` returns exactly the three agents
  (`claude_code`, `codex`, `hermes`).
- For an enabled CC session: `/app/v2/sessions/claude_code:{name}/cards`
  returns `{cards, latest_seq}`, and
  `/app/v2/sessions/claude_code:{name}/events?since_seq=0` streams SSE
  envelopes with monotonic `seq`.
- `…/events?profile=compact` returns `400` (v0), out-of-range `since_seq`
  returns `410`.
- Bad session message read returns `400`.
- `POST /app/v1/messages` with `dry_run: true` returns an SSE response and does
  not increase canonical DB message counts.
