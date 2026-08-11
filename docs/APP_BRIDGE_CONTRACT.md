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

### `POST /app/v1/push/register`

Registers an APNs device token together with per-device notification
preferences. Supersedes `POST /app/v1/devices` (which remains for older apps and
registers with default preferences).

Request body:

- `token` (required): APNs device token, hex string.
- `platform` (optional): defaults to `"ios"`.
- `preview` (optional, default `true`): when `false`, persona-message pushes to
  this device show only the persona display name; the message body is replaced
  with a fixed placeholder so content never reaches the lock screen.
- `personas` (optional, default `null`): `null` subscribes to every persona; a
  list of persona session keys limits persona-message pushes to those personas.
  Non-persona pushes (task done, approvals, test) are not affected.

Response: `{ok, devices, prefs: {preview, personas}, apns_configured}`.
`apns_configured=false` means the bridge has no usable APNs key
(`APNS_KEY_PATH`/`APNS_KEY_ID`/`APNS_TEAM_ID`); registration still succeeds and
takes effect once the key is provisioned.

Rules:

- Idempotent; the app re-posts on every launch and preference change.
- Preferences live in `push_prefs.json` next to the canonical DB; pruning a dead
  token (410/BadDeviceToken) drops its preferences too.
- A missing APNs key must never prevent the bridge from starting: the push
  module silently disables itself (`push_notify` short-circuits with
  `disabled: true`) and logs a single `apns_disabled` event.

### `POST /app/v1/diagnostics`

Ingests app-side observability payloads: MetricKit crash/hang diagnostics,
MetricKit metric aggregates, and user-filed "回報問題" reports. One JSON file
per POST lands under the diagnostics directory next to the canonical DB
(`~/.local/share/pocket-agent/diagnostics/` by default; `POCKET_DIAG_DIR`
overrides). Nothing enters any database and there is no dashboard — the files
are meant to be read directly by the morning-report patrol / Claude sessions.

Request body (JSON object):

- `kind` (required): one of `metrickit_diagnostic`, `metrickit_metric`,
  `user_report`.
- `app_version`, `build`, `device`, `os` (optional): free-form metadata,
  truncated server-side (32/16/64/64 chars).
- `note` (user_report): the user's free text, truncated to 4000 chars.
- `summary` (optional): app-assembled context, e.g. recent ErrorLog lines and
  whether MetricKit diagnostics exist. Stored verbatim.
- `payload` (metrickit_*): the `jsonRepresentation()` of the MXDiagnosticPayload
  / MXMetricPayload. Stored verbatim.

Response: `{ok: true, stored: "<filename>.json"}`.

Rules:

- Bearer auth (master or per-device token), same as every app endpoint; 401
  otherwise.
- Per-request body cap 2 MB → 413 `PAYLOAD_TOO_LARGE`; malformed JSON or a
  `kind` outside the whitelist → 400.
- Directory is rotation-capped at 500 files (oldest deleted first) so a crash
  loop cannot fill the disk.
- Privacy: the payload is assembled app-side and must never contain
  conversation content — stack traces, device/version metadata, and ErrorLog
  summaries only. Automatic MetricKit uploads respect the app's
  「診斷與使用資料」 toggle (default on); user reports are always explicit.

### Interactive approval push payload

Approval pushes carry `aps.category = "POCKET_PENDING_PERMISSION"` (legacy
`SCARF_PENDING_PERMISSION` still registered app-side) so iOS/watchOS render
approve/deny action buttons on the lock screen. Payload shape:

```json
{
  "aps": {"alert": {...}, "sound": "default",
          "category": "POCKET_PENDING_PERMISSION",
          "thread-id": "<session id>"},
  "kind": "approval", "id": "<approvalId>",
  "pocket": {"kind": "approval",
             "approvalId": "<approvalId>",
             "sessionId": "claude_code:{name} | codex:{tid} | hermes:{p} | ''",
             "approveKey": "<option key>",
             "denyKey": "<option key>"},
  "scarf": { same as pocket (compat window) }
}
```

`approveKey`/`denyKey` are computed at push time from the approval row's
`options` styles (`primary` → approve, `danger` → deny; Claude Code rows
without a danger option fall back to `esc`, matching the TUI cancel key). The
app's action handler POSTs them verbatim as `{key}` to
`POST /app/v1/approvals/{id}/decision` — the same single decision path the
Approval Center uses. Rules:

- Keys are only attached when a clean approve/deny pair exists for a
  `permission`-kind approval. `question`/`notice` kinds and complex multi-option
  menus omit them; the app then falls back to the `{approve: bool}` compat
  sugar, and anything richer than two buttons is handled inside the app.
- A stale decision (prompt already answered elsewhere, approval expired) must
  return 409; the app surfaces a human-readable local notification instead of
  failing silently. Network failures likewise produce a failure notification
  that deep-links back to the Approval Center for retry.

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

### `POST /app/v1/uploads/file`

Uploads one composer attachment as a bearer-authenticated multipart request.
Pocket uses this endpoint before sending a persona, Claude Code, or Codex turn
so the client can show byte-level progress and send only a local `path` in the
turn payload.

Multipart fields:

- `file`: required raw file bytes.
- `kind`: optional `image|file|audio` (defaults to `file`).
- `filename`: optional display filename; falls back to the multipart filename.
- `mime`: optional MIME type; falls back to the multipart content type.

The per-file limit is **2 GiB**. The bridge streams the request to its upload
directory and removes a partial file when the limit is exceeded. A successful
response is `{ok: true, attachment: {kind, filename, mime, path, size}}`.
The legacy `POST /app/v1/uploads` base64 batch endpoint remains available for
older clients and offline replay.

### `POST /app/v1/uploads/raw`

The current Pocket client uses this endpoint for file-backed attachments. The
request body is the raw file stream (`application/octet-stream`); the bridge
does not parse multipart and does not base64-decode the body. Metadata is sent
in `X-Pocket-Kind`, `X-Pocket-Mime`, and `X-Pocket-Filename-B64` headers. The
filename header is ASCII-safe for non-ASCII names because it contains standard
Base64 UTF-8 bytes. The response shape and **2 GiB** per-file limit match the
multipart endpoint. This route is also exempt from the legacy JSON body-size
guard, while enforcing the same streaming file cap.

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

### 報告快速行動(feat/report-actions-api)

報告可帶「行動」:閱讀器(`ReportReaderView`)尾端渲染成按鈕,點擊把
`text` 原樣送回 `target_session` — 執行口**不新開**,app 直接走既有
`POST /app/v2/sessions/{id}/input` 統一路由。

- `POST /app/v1/persona-report` 增收選填 `actions`:

  ```json
  {"session": "yuanfang", "label": "晨報", "content": "…",
   "actions": [
     {"label": "叫水鏡再算一卦", "text": "再起一卦,問今天的財運",
      "target_session": ""},
     {"label": "交辦 CC 修 bug", "text": "去修晨報提到的那個 bug",
      "target_session": "claude_code:dev-main"}
   ]}
  ```

  規範:**≤6 顆**;`label` ≤20 字、`text` ≤500 字(超限**截斷不擋件**);
  `target_session` 選填 — `claude_code:<ccsess名>` 或人格 id(如
  `yuanfang`),**空字串 = 報告所屬人格**。缺 `label`/`text` 的元素略過。
  更新語意:同一報告(同 `external_id`)重發時 actions **整組替換**,
  不帶就清空。
- `GET /app/v1/reports/{id}` 回應的 `report` 帶 `actions`(正典形,同上
  三鍵)。舊列/無行動 → `[]`;舊 bridge 沒這欄 → app 端當空。
- `GET /app/v1/reports`(列表)**不揹** actions — 行動與全文都走單筆端點。
- App 端 target 映射:`claude_code:*` 原樣、裸人格 id 補 `hermes:` 前綴、
  空字串退回 `hermes:<報告所屬 session>`,再打 v2 統一 input。

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
> `cc:` Claude Code、`cx:` Codex（2026-07 追加選配第四 provider
> `openclaw:`，見 4.1b；未配置時整段缺席）；**delegations 是連結 persona 與
> cc/cx 兩層的一等公民**。telegram / gmail / calendar 不是 session provider：TG 是 persona
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

### 4.1b OpenClaw（第四 provider，選配）

完整規格在 [`OPENCLAW_PROVIDER_SPEC.md`](OPENCLAW_PROVIDER_SPEC.md)；這裡只寫
client 對接要知道的最小集合（2026-08-01 補：Android 端曾因本節缺席猜了不存在的
`/openclawsessions` 端點——**沒有那個端點，也不會有**）：

- **provider 值 = `openclaw`，session id = `openclaw:{sessionKey}`**（同 4.1 全寫
  規則）。列表走 `GET /app/v2/sessions?provider=openclaw`，卡片流/輸入直接拿
  `openclaw:{sessionKey}` 打既有 v2 session 端點——**沒有專屬 REST 端點家族**
  （不存在 `/openclawsessions`；別比照 `/ccsessions` 猜）。
- **未配置 = 整段靜默缺席（不是 404、不是空陣列旗標）**：bridge 未設 OpenClaw
  gateway 時，v2 列表不出 `openclaw:` row、dashboard `sessions.openclaw` 整鍵
  缺席。client 判斷「這台有沒有 OpenClaw」的正規探針是
  `GET /app/v1/openclaw/config` 的 `configured` 欄。
- **配置面**：`GET/PUT /app/v1/openclaw/config`（`base_url` + `token`；token 只
  上行不回顯；`source:"env"` 表示被伺服器 `OPENCLAW_BASE_URL` 鎖定、PUT 不生效）。
- **測試須知**：OpenClaw 是「bridge 代連外部 gateway」，只有配置了 gateway 的
  那台主機才看得到任何 openclaw 內容。對著未配置的 bridge 開發永遠是缺席態——
  這不是 bridge 缺功能。

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

  - persona(hermes)input 的 2xx ack：`{ok, session_id, accepted, queued,
    message_id, content}`。`message_id` = canonical user turn 的 mid（回顯卡
    id = `card-hp-<mid>`，app 樂觀泡泡對位鍵）；`content` = 實收 user turn
    正文 —— 語音附件的 STT transcript 已折入（feat/stt-transcript-echo），
    app 用它把「🎤 語音訊息 · 辨識中…」樂觀泡泡原地替換成辨識文字。
    `stt_lang`（body 選配）與 v1 messages 同語意：語音轉錄語言鎖定＋繁簡偏置。

  - **claude_code input 的交付語意（`fix/cc-input-delivery`；同時適用
    `POST /ccsessions/{name}/input`）**：bridge 送完 Enter 會回讀 tmux pane
    驗證訊息「真的被 TUI 收走」才回 200。2xx ack 增加三個欄位：

    - `delivery`：`accepted` = 已確認被 CLI 收走；`queued` = CLI 收下但還在
      忙上一輪，或 bridge 在驗證預算內拿不到正面證據。
    - `confirmed`：是否拿到正面證據（UserPromptSubmit hook 世代跳號／文字
      出現在輸入框以外／曾在框裡現已清空）。
    - `enter_retries`：補送 Enter 的次數（0 = 一次就過）。

    `delivery=queued` **不得**顯示成「已送達」——標排隊態，等 transcript
    回顯才收尾；也**不得**進 app 的本機補送佇列（補送擁有者是 CLI 自己的
    佇列，app 再送一次會變兩則）。

    訊息**沒被收下**時回 `409 CC_INPUT_NOT_ACCEPTED`（不再回 200，取代舊的
    502 `PASTE_NOT_SUBMITTED`），`message` 尾端帶原因：`context_full`
    （context 已滿）／`awaiting_prompt`（畫面有選單在等回覆）／
    `composer_missing`（TUI 不在可輸入狀態，如啟動對話框、全螢幕 overlay）／
    `composer_stuck`。bridge 會先把卡在輸入框的殘字清掉再回錯，不留殭屍草稿。
    app 應標成「可重試的失敗」並顯示原因，**不要**自動補送 —— 這些狀態多半
    要人先處理（先 `/compact`、先答選單）。
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

## 13. 全機發現與收編(`/app/v2/discovery`,SUBPROCESS_HARNESS_DESIGN §2.3)

> 善彰定調:Pocket 是那台機器的**指揮艙**,不是只管「從 Pocket 開的」。
> 使用者自己在桌機/CLI 開的 session、BYO-key 開的、別家模型開的,一律
> **發現 → 呈現 → 可收編**。

### 13.1 `GET /app/v2/discovery?provider=&refresh=`

四路 provider 一次掃完,~5 秒快取。`provider` 逗號分隔過濾(吃
`cc`/`cx`/`oc` 別名);`refresh=1` 略過快取。

```jsonc
{
  "items": [{
    "id": "claude_code:amulet-hunter",   // = registry session id,收編/釋放用它
    "provider": "claude_code|codex|hermes|dispatch|openclaw",
    "name": "amulet-hunter",
    "workdir": "/Users/xcash/apps/amulet-hunter",
    "state": "managed|discovered",       // discovered = 看得到、還沒收編
    "registry_state": "active|idle|done|archived|null",  // null = 還沒有戶口
    "purpose": "…", "class": "task",     // 有戶口才有
    "source": "ccsess|ccsess-disabled|tmux|cli|vscode|exec|appServer|persona|dispatch|gateway",
    "since_ts": 1786355258.0,
    "busy": false,                       // cx/oc/dispatch 有;cc 缺席(見下)
    "model": "opus", "model_provider": "openai",
    "has_api_key": false,                // BYO-key **只報有無**,永不含值
    "alive": true, "tmux_session": "…", "pane_pid": 6756,   // 僅 cc
    "permission_mode": "acceptEdits", "session_id": "…"     // 僅 cc,撈得到才有
  }],
  "providers": {"claude_code": {"ok": true, "count": 8},
                "codex": {"ok": false, "count": 0, "error": "CodexAppServerError"}},
  "counts": {"total": 12, "managed": 12, "discovered": 0},
  "generated_ts": 1786447811.2
}
```

- **單路掛掉只降級,不 500**:`providers[p].ok=false` 代表那一路掃描失敗
  (該 slice 為空)。app 應顯示「cx 掃描失敗」,**不要**當成那邊沒有 session。
- **`busy` 在 cc 缺席**:cc 的忙碌判定要 capture tmux pane,放進掃描會造成
  pane capture 風暴。cc 的 busy 請用 `/app/v2/sessions` 或
  `/app/v2/registry/{id}/children`(那裡已有 3 秒快取的 busy)。
- **掃描面完全唯讀**:tmux list-panes / ps 快照 / `thread/list` /
  `sessions.list`。零 spawn、零 kill、零 send-keys。

### 13.2 每路能發現到什麼(以及誠實的限制)

| provider | 發現方式 | 使用者自開的看得到? | 限制 |
|---|---|---|---|
| `claude_code` | `tmux list-panes -a` 掃全機,**pane pid 的行程樹**認 claude,比對 `~/.config/ccsess/sessions.conf` | ✅ | **不在 tmux 裡的 claude 發現不到、也控制不了**(沒有 pane 可控,設計如此) |
| `codex` | `thread/list`(`sourceKinds: cli/vscode/exec/appServer`) | ✅ | guardian/subagent thread 依既有規則濾掉 |
| `hermes` | bridge 自有 persona POOL | ✅ | 恆 `managed`(白名單 persistent) |
| `dispatch` | bridge 自有 SUBSESSIONS(人格雙手) | ✅ | 恆 `managed` |
| `openclaw` | gateway `sessions.list` | ✅ | 未配置 gateway → 該路為空 |

> ⚠️ `pane_current_command` **不能**用來認 claude:實測它回的是版本字串
> (如 `2.1.207`)。一律走 pane pid 的行程樹 cmdline。

`state` 判定:cc = 在 `sessions.conf` 且 `enabled=1` → `managed`
(`enabled=0` 卻還活著 → `discovered`,`source=ccsess-disabled`);
cx/openclaw = registry 有登記戶口(bridge 開的)→ `managed`,否則
`discovered`;hermes/dispatch 恆 `managed`。

### 13.3 `POST /app/v2/discovery/{id}/adopt` body `{purpose?, class?}`

收編 = **純記帳**,回 `{ok, already_adopted, conf_updated, session}`
(`session` 與 `/app/v2/registry` 的 row 同形狀)。

**安全保證(可依賴)**:
- cc:只往 `~/.config/ccsess/sessions.conf` 加/啟用一行(**寫前先備份**成
  `sessions.conf.bak.<epoch>`,註解與行序原樣保留)。**不重啟、不 kill、
  不送任何按鍵** —— pane 上跑到一半的 turn 一秒都不會斷。
- cx / hermes / openclaw:**純 registry 登記**(它們本來就打得到)。
- 已在名單的同名 lane **不覆蓋既有 workdir**(那是使用者的權威設定),
  只在 `enabled=0` 時把它打開。
- **不套配額**:行程早就在跑了,配額擋下來只會留下一個「看得到、管不到」
  的孤兒,與收編的目的相反。

收編後該 session `registered:true`、有 purpose/class/TTL、進得了家譜、
受治理(reaper 可管)。錯誤:未知 id → `404 DISCOVERY_ID_UNKNOWN`;
`class` 不是 `persistent|task|ephemeral` → `400`;重複收編 → `200` 冪等
(`already_adopted: true`,不重寫檔案、不重複備份)。

### 13.4 `POST /app/v2/discovery/{id}/release` body `{remove_from_conf?}`

收編的逆操作:registry `registered=0`。**戶口保留**(歷史/家譜不消失),
而 `registered=0` 本身就是 reaper 的免疫標記 —— 釋放後這條再也不會被自動
收屍。cc 預設**保留** ccsess 名單(釋放治理 ≠ 要它別再自癒);
`remove_from_conf: true` 才連名單那行一起移除(一樣先備份)。
一樣不 kill、不重啟。

### 13.5 與 `GET /app/v2/registry` 的關係

發現但未收編的 session 會出現在 `/app/v2/registry` 的
`sessions[]` 裡、`registered:false`(1a 既有契約)——app 的 FleetView 可以
直接把它們渲染成「**未登記**」區塊,點一下打 `…/adopt` 收編。
收編成功後該 id 就從未登記區移到正式戶口列。
