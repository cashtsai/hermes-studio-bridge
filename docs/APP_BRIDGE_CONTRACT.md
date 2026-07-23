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
- `apple_web_auth`
- `account_pairing`
- `delegations`
- `control_plane_v2`
- `media_artifacts`
- `hermes_media_capabilities`
- `hermes_media_settings`

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

### Web Sign in with Apple (Developer ID builds)

Developer ID distribution does not support the native Sign in with Apple
entitlement. Pocket uses a three-step browser flow instead:

1. `POST /app/v1/auth/apple/web/start`
2. `POST /app/v1/auth/apple/web/callback` (Apple `form_post`)
3. `POST /app/v1/auth/apple/web/status`

These three endpoints form the fixed-domain public auth broker at
`pocket.tsai.cash`; they do not run against each user's changing local tunnel.
`start` is IP-rate-limited and returns a short-lived `flow_id`, a separate
high-entropy `poll_secret`, and Apple's authorization URL. `status` requires
both opaque values. The callback accepts only a single-use state, verifies the
signed nonce, exchanges the five-minute authorization code at Apple's token
endpoint, and compares the verified subjects.

The callback HTML never contains an identity token or account session. Pocket
polls the fixed-domain broker and receives the exchanged Apple identity token
once, then sends that proof to its own `127.0.0.1` bridge through
`POST /app/v1/auth/apple`. The local bridge verifies the Apple JWT and mints the
local account session. Failed, cancelled, replayed, expired, or incorrectly
keyed flows never return identity proof.

Required bridge environment:

- `APPLE_WEB_CLIENT_ID`: Apple Services ID, for example `com.pocketagent.web`.
- `APPLE_WEB_REDIRECT_URI`: exact HTTPS callback URL registered with Apple.
- `APPLE_WEB_TEAM_ID`: Apple Developer Team ID.
- `APPLE_WEB_KEY_ID`: Sign in with Apple private-key ID.
- `APPLE_WEB_PRIVATE_KEY_PATH`: local mode-600 `.p8` path.

The Sign in with Apple key is a bridge runtime secret. It must not be bundled in
Pocket, committed to git, or reused as a GitHub release-signing secret.

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
- Reaction/pin overlay (G2/#39): each message may additionally carry
  `reaction`（legacy 單值）、`reactions`（清單）、`pinned`、`deleted` — 缺席
  即無資料。canonical mid 與 tg-`<ts>` id 一視同仁。

### Reaction / 置頂（G2/#39 canonical 化）

寫入端點（皆 Bearer auth）：

- `POST /app/v1/reactions` `{message_id, emoji, action:add|remove}` —
  id-agnostic（canonical mid / tg-`<ts>` / 報告 id 皆可），回全清單。
- `POST /app/v1/pins` `{message_id, pinned:bool}` — per-message 置頂。
- `PATCH /app/v1/messages/{id}` `{reaction: "👍" | null}` — issue #39 合約的
  單值形狀（null=清除）。**只認 canonical messages 表的 id，不存在回
  `404 MESSAGE_NOT_FOUND`**；TG/cron 來源訊息請走上面 id-agnostic 的 POST。
- `PUT /app/v1/sessions/{id}/pin` `{pinned_message_ids:[...]}` — per-session
  全量替換（空清單=全解除），id 收 GET /app/v1/messages 回的任何穩定 id；
  解除只掃歸屬本 session 的列。`GET` 同路徑讀回
  `{session, pinned_message_ids}`。未知 session 回 404。

儲存：`message_meta(message_id, reactions JSON, pinned, session, deleted)`
overlay（`session` 欄 idempotent ALTER + 由 messages 表回填；tg id 由
PUT pin 寫入時直接掛歸屬）。legacy `reactions` 單值表照舊鏡射，舊 app 不破。

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

### Hermes media capabilities and settings

Pocket does not select or call Whisper/OCR providers directly. The dependency
direction is:

`Pocket -> Bridge transport -> Hermes profile -> configured media provider`

`GET /app/v2/hermes/media-capabilities?persona=<id>&probe=true` returns the
secret-free effective configuration for that persona:

```json
{
  "persona": "xcash",
  "profile": "xcash",
  "stt": {
    "enabled": true,
    "provider": "siege",
    "model": "whisper-1",
    "configured": true,
    "available": true
  },
  "ocr": {
    "enabled": true,
    "provider": "siege",
    "configured": true,
    "available": true
  },
  "limits": {
    "attachment_count": 12,
    "attachment_bytes": 33554432,
    "stt_input_bytes": 26214400
  },
  "provider_options": {
    "stt": ["local", "openai", "siege"],
    "ocr": ["none", "siege"]
  }
}
```

`probe=false` returns configuration state without network health probes.
Normal paired-device tokens may read capabilities.

`PUT /app/v2/hermes/media-settings?persona=<id>` atomically updates the
allowlisted settings in that persona's Hermes `config.yaml`. It requires the
owner/master bridge token; paired-device tokens receive `403`. Accepted fields:

```json
{
  "stt": {
    "enabled": true,
    "provider": "siege",
    "siege": {
      "base_url": "http://siege-host:8081/v1",
      "model": "whisper-1",
      "language": "",
      "prompt": ""
    }
  },
  "ocr": {
    "enabled": true,
    "provider": "siege",
    "siege": {
      "base_url": "http://siege-host:8083",
      "use_doc_orientation_classify": true,
      "use_doc_unwarping": true,
      "use_textline_orientation": false,
      "return_word_box": false
    }
  }
}
```

Provider credentials are never accepted or returned by these endpoints. They
remain in the Hermes profile secret scope. Pocket must not persist provider
URLs, model credentials, or a second copy of these settings in UserDefaults or
CloudKit.

### `GET /app/v1/approvals`

Returns approval cards for app review.

### `POST /app/v1/approvals/{id}/decision`

Records approve/reject decisions. PocketAgent must not call this in smoke tests.

### `GET /reports`

Returns scheduled reports for app reading surfaces.

### `GET /cron/jobs` and `POST /cron/jobs/{id}/{action}`

Exposes notification-producing jobs. Use this carefully because it affects both
app and Telegram delivery.

### `GET /app/v1/terminal` (WebSocket)

In-app self-ops terminal. Authoritative spec:
`studio-os/docs/TERMINAL_PTY_CONTRACT.md`; this section is the bridge-side
summary kept in sync with it.

- **Auth**: same device-token contract as every other `/app/v1/*` endpoint
  (`Authorization: Bearer <token>`), plus a `?token=<device_token>` query
  fallback for WS clients that can't set a header on the upgrade request.
  An invalid/missing token gets the WS **accepted** and immediately **closed
  with code 4401** (a real close frame, so the code survives) — not a
  pre-accept reject, because uvicorn's ASGI websocket implementation
  hardcodes HTTP 403 for any pre-accept close and discards the numeric code.
- **Kill switch**: `POCKET_TERMINAL_ENABLED` env var, default `"1"`. Set to
  `"0"` to refuse the handshake outright (pre-accept close → HTTP 403 on this
  stack, matching the "端點回 403" requirement).
- **Session model**: one WS = one local PTY running a login shell
  (`$SHELL -l`, fallback `/bin/zsh -l`), `TERM=xterm-256color`, cwd = the
  bridge process's own home directory, bridge's own execution identity (no
  privilege escalation, no user switch). WS close/disconnect kills the whole
  process group and reaps it — no zombies.
- **Messages** (text JSON, UTF-8; PTY bytes decoded UTF-8 with
  `errors="replace"`):
  - Client → server: `{"type":"input","data":"<keystrokes>"}`,
    `{"type":"resize","cols":<int>,"rows":<int>}`.
  - Server → client: `{"type":"output","data":"<pty bytes as utf8>"}`,
    `{"type":"exit","code":<int>}` (then the server closes the WS),
    `{"type":"error","message":"<why>"}`.
- **Logging**: `terminal_open` / `terminal_close` events carry `device_id`
  and, on close, `duration_s` — never keystrokes or PTY output.
- **Security note**: a paired device token equals full local shell access.
  Acceptable for a self-hosted single-owner bridge; the kill switch above is
  the escape hatch. Not gated behind `POCKET_KERNEL` — this is a
  self-ops feature, available in OSS/kernel builds too.

### `POST /app/v1/agent-lanes/{provider}/activate`

Pins Pocket's provider lane to a native agent session. `{provider}` accepts
`cc` / `claude_code` and `cx` / `codex`.

- **Claude Code body**: `{name?, session_id?, workdir?, adopt_source?}`.
  A named source session is reused in place so its Claude App remote-control
  process remains alive when Pocket connects or exits. Only a history-only
  session id with no source name uses the fixed `pocket-cc` fallback.
- **Codex body**: `{thread_id, workdir?, name?, preview?}`.
  The bridge records a logical binding and returns the selected Codex session
  shape. Status/history/input continue through the Codex app-server; no
  competing `codex resume` CLI or `pocket-cx` tmux is started.
- The history-only `pocket-cc` fallback uses tmux `remain-on-exit on` and
  `destroy-unattached off`. Exiting either Pocket agent page only clears client
  UI state and never archives or terminates the native session.

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
| `GET  /app/v2/sessions/{id}/media?limit=&cursor=` | session 媒體永久索引（§7） | ✅ 上線（cc/cx/persona） |
| `GET  /app/v2/artifacts/{media_id}` | 已封存 artifact 位元組 | ✅ 上線 |
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
- text/markdown 可帶
  `attachments[{kind,filename,mime,path?,url?,media_id?,download_url?,source_url?,byte_size?,available?}]`；
  attachment-only 訊息不得被 digest 丟棄。`attachment` kind 的 body 使用同組
  欄位（單一附件）。
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

### Durable session media

```
GET /app/v2/sessions/{session_id}/media?limit=100&cursor=M
GET /app/v2/artifacts/{media_id}
```

媒體索引回 `{items, next_cursor}`，新到舊排序；`limit` 上限 500。每項至少有：

```json
{
  "media_id": "med_…",
  "session_id": "codex:…",
  "source_ref": "/tmp/Q3 report.pdf",
  "source_kind": "path",
  "filename": "Q3 report.pdf",
  "mime": "application/pdf",
  "kind": "pdf",
  "byte_size": 4096,
  "available": true,
  "unavailable_reason": null,
  "download_url": "/app/v2/artifacts/med_…"
}
```

- 本機路徑在仍存在時複製到 content-addressed blob；相同內容只存一份。
- 預設單檔封存上限為 100 MB；超過上限仍保留索引並回
  `available:false, unavailable_reason:"too_large"`。
- `media_id` 對同 session + 原始 reference 穩定。原 `/tmp` 檔刪除後仍由
  artifact endpoint 提供。
- 已來不及封存的舊 reference 仍列出，`available:false` 並附 reason；client
  必須顯示失效狀態，不得自動無限重試。
- HTTP(S) URL 只索引、不由 bridge 代抓，回 `source_url`，避免 SSRF。
- `/file?path=` 保留相容；成功讀取會順手封存，原路徑消失時會查已封存副本。

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

## 12. 統一 Approval 物件(Approval Hub A1,2026-07-10 上線)

> 完整設計見 APPROVAL_HUB_SPEC.md;本節是 app 可依賴的 wire 契約。

- **物件形狀**(v1 list/get、v2 `meta.approval` 共用;舊欄位 `source/result/decided_at` 相容期保留):
  `{id, session_id, provider: claude_code|codex|hermes, kind: permission|question|notice,
  title, detail, risk, options: [{key,label[,style: primary|secondary|danger]}],
  created_at, expires_at, status}`。
  `options` 缺席時 bridge 給預設(permission→approve/deny、notice→單鍵 ack);
  app 永不再用 label 猜語意,`style: danger` = 否決類。
  codex 的 v2 `meta.approval` 相容期額外帶 `method/thread_id`,且其 options 暫仍用
  舊 style 字彙 `deny`(app 現行判準),A4 收斂為 `danger`。
- **決定**:`POST /app/v1/approvals/{id}/decision body {key}` 為唯一語彙;
  `{approve: bool}` 為相容糖(approve→第一個 primary、deny→第一個 danger)。
  v2 統一 body:`POST /app/v2/sessions/{id}/approve {approval_id, key}`;
  三種舊 body(`{key}`/`{approve}`/`{approval_id}`)相容期照收。
  409 語意不變(已決/失效);未知 key 回 400 `UNKNOWN_KEY`。
- **status 字彙**:pending / approved / **denied**(新決議;歷史列 `rejected` 等價,
  codex 線相容期仍寫 rejected)/ answered(question,result=key)/
  acknowledged(notice)/ expired。
- **建立**(hermes/skill):`POST /app/v1/approvals {title, session_id, kind?, risk?,
  detail?, options?, ttl_seconds?, callback_url?}`;`source` 為 `session_id` 舊名。
- **hermes waiting_approval**:persona 有 pending 時 v2 sessions 該列
  `status=waiting_approval` + `meta.approval`(之前恆 idle)。
