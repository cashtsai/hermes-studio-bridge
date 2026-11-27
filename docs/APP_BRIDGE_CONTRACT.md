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

### Enrollment Continuity (2026-11-24)

If the local Pocket ID enrollment is missing while a relay grant file survives,
`POST /app/v1/relay/enroll` returns HTTP 409 `RELAY_IDENTITY_CONFLICT` before
creating a host. Background enrollment uses the same guard. The existing relay
and account data are not changed; do not delete the grant to bypass this check.
An owner-verified repair must reconcile enrollment, relay and LINE ownership.
The operator runbook is `pocket-id/docs/LINE_HOST_IDENTITY_REPAIR.md`.

### LINE Session Routing (2026-11-24, Additive)

This extension requires coordinated Pocket ID and iOS updates. It is not a
new provider runtime and does not collect provider OAuth/API credentials.

- `GET /app/v1/line/targets` (owner device token): `{targets, degraded_providers, host_id, approval_mode}`.
  Targets are drawn from the same v2 session roster, require `input`, and exclude
  archived/offline/stopped rows and arbitrary terminals/delegations. Supported
  providers: Hermes, Claude Code (tmux and cc2), Codex, Gemini, Grok, OpenCode,
  OpenClaw. Listing is not proof of provider login or available model quota.
- `POST /app/v1/line/token {session_id}` (owner device token) validates the target
  and returns `{token, host_id}` with a pending, 10-minute `line-session`
  credential. `host_id` comes from bridge enrollment. A new pending token
  does not revoke the active binding. The no-body legacy persona flow remains.
- `GET /app/v1/line/binding` (LINE token): `{scope: "line-session", session_id}`.
  Pocket ID calls this through a server-derived relay URL for an account-owned
  `host_id`, not a caller-supplied URL. Redirects must not forward credentials.
- `POST /app/v1/line/activate` (pending LINE token) activates it and retires the
  old active LINE credential on this host. Pocket ID then records the binding.
- `POST /app/v1/line/messages {session_id,client_id,content}` (active LINE token)
  accepts text only, for exactly the token's session. `client_id` is
  `line-<64 hex chars>` derived from the LINE webhook event, and is persisted
  before dispatch. It returns an acceptance receipt, not model completion.

LINE credentials cannot list devices, mint tokens, access files/terminal/admin
routes, or approve actions (including WebSocket auth paths). Legacy LINE tokens
retain only the persona-message path. CC tmux input retains the console's
composer check under the paste lock and blocks leading `!`/`/` shell/commands.

Dispatch uses the existing `v2_session_input`; history remains in the original
provider session and is visible to Pocket/Console/native clients as before.
Hermes/Gemini/Grok/OpenCode and directly started Codex inputs now add `turn_id`
to receipts. Queue entries preserve the allocated ID. cc2/queued Codex use the
native user echo's client ID. CC cards add optional `body.native_message_id` and
`body.native_parent_id` from JSONL UUID/parentUuid: LINE follows that ancestry,
stops at an intervening user message and waits for idle, never guesses from a
tmux busy lamp. Missing/evicted correlation produces a notice to inspect Pocket.
`body.native_link_parent_id` is an optional resolved parent through non-rendered
`attachment`, `progress`, and `system` metadata. Raw IDs remain unchanged. The
per-session map is bounded to 4096 links and a 256-hop traversal; cycles, gaps,
unknown event types, and intervening users never authorize an unrelated reply.
This handles Claude's hidden attachment nodes without rendering private metadata
as chat cards. Regression coverage: `tests/test_line_native_ancestry.py`.

The host-local `<CANON_DB>.line.db` holds transport receipts, input hashes and
pending replies, not a second transcript. Completed replies go to the fixed
Pocket ID `/v1/line/notify` callback with token, session ID and client ID. Only
the matching incoming LINE event can receive the result. Text is sanitized;
reasoning/tool output is excluded. Manual approval exceptions remain in Pocket. Existing Hermes
delegation notices remain restricted to the bound Hermes persona.

Limits: at most 16 tracked LINE tasks per host; 30-minute reply observation;
retry backoff 10s to 5min; delivery stops after 23 hours; delivered receipts are
pruned after seven days on subsequent inputs. A restart marks unfinished
tracking interrupted and notifies the user; it NEVER resubmits an uncertain
command. Target changes/unlinking suppress old-target notifications; results
remain in the original session. Acceptance is not guaranteed LINE delivery.
Use the existing single-worker bridge deployment: restart recovery assumes
exclusive ownership of the receipts DB and in-memory provider runtimes.

Pocket ID activation and database update are not a distributed transaction.
If a connection fails during switching, refresh status and reselect the target;
never silently route to another host. Production acceptance must cover this
failure, multi-host switching, provider login/quota errors and long tasks.

#### LINE Automatic Permissions (2026-11-24)

New LINE session inputs default to single-use tool permission approval. The
host may opt out with `POCKET_LINE_AUTO_APPROVE=0` and a service restart.
`GET /app/v1/line/targets` and fresh input acceptance receipts expose
`approval_mode: "auto-once" | "manual"`; this is a policy, not a promise that
every request can be resolved automatically. No provider login, sandbox or
session-wide permission settings change. Existing unfinished tasks and legacy
persona inputs are not replayed or retroactively approved.

- Only the tracked LINE turn is eligible, using the provider request's explicit
  turn ID (`approval.body.request_turn_id`), not the card's inferred display turn.
  Native Claude Code requires the matching native user echo and no intervening
  human user message, plus the existing live-prompt check before sending a key.
- The active LINE binding is checked again under the shared approval-decision
  lock. Revoked, pending, expired or rotated host credentials cannot approve old work.
  The row must match the session, be pending, unexpired and created after input
  tracking began (second-granularity providers use that same second boundary).
- Only known single-use options are selected: never `allow_always`,
  `acceptForSession`, session-level bypass, questionnaires, notices or guest
  message approvals. ACP option objects now preserve their additive `kind`.
  Missing turn proof or a single-use option leaves the existing manual flow.
- Codex, cc2, Gemini, Grok and OpenCode carry request-turn correlation. OpenClaw
  is eligible only when the gateway supplies the request run ID. Hermes native
  tool permissions retain their existing behavior; business/harness approval
  rows without authoritative turn correlation remain manual.
- Decisions reuse the canonical Approval Hub and provider handlers, with
  existing resolved-card broadcasts and receipts. Structured logs use
  `line_permission_auto_decided` / `line_permission_auto_failed` with hashed
  client/approval IDs, never tokens or prompt content. Observer cleanup,
  30-minute tracking limit and restart no-replay rules remain unchanged.

Same-host target changes rotate the host credential and stop its old automatic
decisions. Pocket ID unlinking or switching to another host stops new delivery
and old-result notifications, but does not revoke the old host's credential or
cancel already accepted work. To stop that work, interrupt the original session
or revoke its LINE device credential on that host. This policy does not add
cross-host cancellation or a central authorization lookup on every tool call.

Verification: `tests/test_line_auto_approval.py` covers endpoint dispatch,
single-use provider outcomes, manual-decision races, revocation, event gaps,
cross-device turn isolation, question exclusions and waiter cleanup. Use the
production runtime Python for smoke tests. Restart only after all turns are
idle, from an external launchd job: an active bridge-backed Codex session must
not restart its own bridge. Read back `approval_mode` after restart; a queued
deployment is not a completed deployment.

### Console W2 Operations (2026-11-18, Additive)

`GET /app/v2/providers/capabilities` returns `{providers: {provider: caps}}`.
`caps.ops` remains derived from `_V2_SESSION_OPS`, not duplicated in clients.
The following optional metadata is additive; older clients can ignore it:

- `compact: bool`: the unified compact route is implemented. CC may respond
  `409 USE_CC_COMPRESS`; only the local owner console can follow that explicit
  response to `POST /ccsessions/{name}/compress`. A receipt means requested,
  not completed. Console-remote must not fall back to a legacy endpoint.
- `read_cursor: bool`: this provider can report to `POST /app/v2/read`.
  Currently true only for Hermes personas. This is not a claim that coding
  sessions have server-backed unread state.
- `config_fields: {field: {editable: bool, options?: [string]}}`: field-level
  support for the existing GET/POST session config endpoints. Current values
  remain in the config response. If that response supplies `field_options`
  (`[{value,name}]`), those options take precedence. Missing schema means
  read-only display, never guessed write support.

Codex exposes model and approval_policy as editable; effort and sandbox are
spawn-only/read-only. CC2 exposes model and permission_mode, with current
values from the session's meta (it has config_write, not config_read).
OpenCode exposes its known model/mode fields. No existing config route or
authorization behavior changed.

The owner console hides an operation only when the provider matrix explicitly
excludes it. It does not treat absence from a legacy per-session capability
list as an operation denial. Console-remote still uses its per-session strict
allowlist and does not fetch the global capability matrix or approval hub.

Rename receipts may change identity (`session_id`, or legacy `session.name`).
Archive uses `{archived: true}` and undo uses `{archived: false}`. The console
keeps the undo action until dismissed, including after a failed undo attempt.

When the card ring cursor is exhausted or returns 409, `/sessions/{id}/history`
is an independent transcript view. Its text/cursor and messages/offset shapes
are not converted into live cards. Pagination never changes the card stream's
latest_seq, IDs, or loaded history. Offset-based providers advance by the
requested limit; cursor-based providers must supply a progressing nextCursor.

Read reports use a stable device_id, bare persona session name, and the latest
visible final prose card's last_read_ts/message_id. Card-ring sequence numbers
are NOT global event-log sequence numbers and must never be sent as
last_read_seq. Only the focused visible tab, valid snapshot and bottom-follow
position may report. Repeated renders/focus events are deduplicated; incoming
read_cursor.update.global advances the same high-water mark.

### Persona Unread State (2026-11-27)

Hermes `/app/v2/sessions` rows and roster deltas include optional `read_state`:
`{version,last_read_seq,last_read_ts,incoming_seq,incoming_ts,last_message_ts}`.
Values are content-free metadata from the existing event log and cursor table.
`null` means unavailable, not read. The unread predicate is incoming seq beyond
the read seq AND (message timestamp unknown OR beyond the read timestamp).
Use message timestamps, not event ingestion time. Own sends and tool/system
messages are not inbound replies. Edits of already-read messages keep their
original message time and must not resurrect unread. Merge cursors monotonically
in both dimensions; timestamp-only receipts are valid. Prefer the aggregate
`global` cursor over the reporting device's cursor.

Console-remote may POST `/app/v2/sessions/hermes:{id}/read` only with a current
inventory grant carrying `read_cursor`. The body supplies `last_read_ts` and an
optional `message_id`. The session is bound to the path, the device identity is
server-derived, future timestamps and ring-sequence reports are rejected.
The response includes `global` and refreshed `read_state`. Global `/app/v2/read`
and `/app/v2/events` remain forbidden for console-remote. Roster change detection
includes read metadata so another device's receipt updates the remote sidebar.

Sidebar order: provider groups, pins, attention/running/ordinary rank (15-second
demotion hysteresis), captured recent activity, ID tie-break. Recency is captured
on first appearance and reset by explicit Refresh, not by SSE or background
snapshot refresh. iOS retains its existing pin + live recency ordering.

The owner approval hub reads all pending approval objects (including multiple
objects per session), with next_offset pagination. Decision writes use the
selected `/app/v1/approvals/{aid}/decision` and the existing P0 renderer.
For live Codex questionnaires the hub serializer overlays title, detail,
options, q_index and q_total from the active request. The DB row can describe
an older question. Clients send expected_q_index; a next_question receipt
must NOT mark the entire approval resolved. response_capability values
not_resumable/none disable responses. Existing CC multiselect continues using
the P0 answer route only when the selected approval matches the current prompt.

## 1.1 Typed visual reply envelope (additive)

Cards may include an optional `body.visual` envelope. It is versioned and must
always be accompanied by the existing `fallback_text`; older clients ignore
the envelope and render the fallback.

```json
{
  "version": 1,
  "fallback_text": "摘要",
  "blocks": [
    {"type": "text", "text": "摘要"},
    {"type": "image", "url": "https://example.invalid/a.png",
     "mime": "image/png", "alt": "預覽"},
    {"type": "link", "url": "https://example.invalid", "label": "原文"},
    {"type": "html", "html": "<p>已清理內容</p>", "sanitized": true}
  ]
}
```

Allowed block types are `text`, `markdown`, `image`, `file`, `link`, and
`html`. `html` requires server-side sanitization and is rejected if common
active-content markers are present. Provider-specific fields and secret-like
fields are dropped or rejected. Raw HTML is never placed into the normal text
field. The envelope is a capability-gated extension; it does not change the
v2 fallback path.

## 1.2 Live update event envelope (iOS and Android)

ActivityKit payloads keep the existing iOS `busy`, `phaseLabel`, and
`endedLabel` fields, and also carry the shared event fields:
`version`, `sessionID`, `seq`, `state`, and `terminal`. The allowed states are
`connecting`, `queued`, `running`, `waiting_user`, `streaming`, `completed`,
`failed`, `interrupted`, `disconnected`, and `idle`.

`completed`, `failed`, and `interrupted` are terminal states. Clients must discard an
event whose sequence is older than the last accepted event, must not turn a
transport `disconnected` state into `idle`, and must always apply a terminal
event even when non-terminal updates are throttled. Android Live Updates use
the same envelope; only the platform presentation differs.

For Codex, `turn/completed` is a provider event name, not a success verdict.
A stopped turn emits the existing v2 `turn` event with `state: "end"` and
an additive `outcome: "interrupted"`. A status snapshot can include
`last_turn_outcome`; the session is idle and ready for input unless another
turn is queued or running. A prior bridge timeout or exhausted provider retry
is reported as failure, even if the provider acknowledges the stop. Do not
end a CX activity on an idle notification alone: the actual outcome may follow.
Clients without the typed state can use the existing `busy` and `endedLabel`
fields. No v2 client needs a new phase to continue sending messages.

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
- `subscription_entitlement`

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
`pocket.example.com`; they do not run against each user's changing local tunnel.
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
  OpenClaw reply and approval pushes use the full `openclaw:{sessionKey}` as
  their preference key and respect `preview`. Preferences can restrict these
  pushes, but cannot authorize unrelated gateway turns.

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

### `POST /app/v1/live-activity/register` (ActivityKit push, phase 2)

Registers one Live Activity's ActivityKit push token so the bridge can keep
updating the lock screen after the app process is suspended. One row per
activity (the same session may have several activities across devices).

Request body:

- `activity_id` (required): the app-side `Activity.id`; the upsert key.
- `push_token` (required): hex token from `activity.pushTokenUpdates`. Tokens
  rotate; the app re-posts on every new value (idempotent overwrite).
- `session_id` (required): v2 wire session id (`claude_code:{name}` /
  `codex:{tid}` / …) whose turn progress drives this activity.
- `device_env` (optional, default `"prod"`): `"dev"` routes through the APNs
  sandbox host (`api.sandbox.push.apple.com`), `"prod"` through production.
  Debug/Xcode builds must send `"dev"`, TestFlight/App Store builds `"prod"`.
- `bundle_id` (optional): defaults to the bridge's `APNS_BUNDLE_ID`
  (`com.pocketagent.kernel`). Dev-variant builds pass their own.

Response: `{ok, activity: {activity_id, session_id, device_env, bundle_id,
token_set, updated_at}, apns_configured}`. As with `/app/v1/push/register`,
`apns_configured=false` means registration succeeded but no APNs key is
provisioned yet — the feature is silently disabled until the key appears.

`DELETE /app/v1/live-activity/register` with body `{activity_id}` removes one
row (the app calls this when it ends an activity itself); body `{session_id}`
removes every row for that session. One of the two is required (400 otherwise).

Push behaviour (bridge side, informative for the app team):

- Trigger points are the session's milestone events: `turn` begin/end and
  `session.status` changes.
- Updates are sent as `apns-push-type: liveactivity` to topic
  `<bundle_id>.push-type.liveactivity`. `content-state` is the §1.2 envelope:
  `busy`, `phaseLabel`, `endedLabel` plus `version`, `sessionID`, `seq`,
  `state`, `terminal`; while busy it may also carry `busySince` (unix seconds,
  the elapsed-timer anchor). Keys must stay letter-for-letter aligned with
  `PocketTurnActivityAttributes.ContentState` — ActivityKit drops undecodable
  payloads silently (extra keys are fine, missing ones are not).
- Throttle: per activity, non-terminal updates go out at most once per 3s
  (`APNS_LIVE_ACTIVITY_ACTIVITY_MIN_SECS`) and only when
  `(busy, phaseLabel, endedLabel)` actually changed.
- Turn end sends the final content-state with `"event": "end"` and
  `dismissal-date` = now + 120s at priority 10, then deletes the row. The app
  must re-register for the next turn (new activity, new token).
- APNs 410/BadDeviceToken prunes the row and logs; failures never propagate
  into the card hot path. A missing APNs key disables the whole feature
  silently (registration still accepted).

Subscription state is **server-authoritative** so it survives reinstall and stays
consistent across a user's devices. The app may never assert its own tier: the
only accepted evidence is a JWS signed by Apple. Any `tier` / `active` /
`expires_at` field the app puts in a request body is ignored.

Verification is **offline**. StoreKit 2's `signedTransaction` and Server
Notifications V2's `signedPayload` are ES256 JWS whose header carries the full
`x5c` chain (`leaf → Apple WWDR intermediate → Apple Root CA - G3`), so the
bridge validates the chain against a pinned local copy of Apple's root and never
calls Apple to check a receipt. Rationale: no dependency on Apple reachability
from a home Mac Studio behind a tunnel, no App Store Server API `.p8` key to
guard, no rate limits. Live reconciliation (renewals, refunds) arrives instead
through Server Notifications V2. Details in `apple_storekit.py`.

Required bridge environment (absent → module silently disabled):

- `APPLE_STOREKIT_BUNDLE_ID`: the only bundle id whose transactions are accepted,
  e.g. `com.pocketagent.kernel`.
- `APPLE_STOREKIT_ROOT_CA_PATH`: local Apple Root CA - G3 (PEM or DER). Export it
  with no network access at all using
  `security find-certificate -c "Apple Root CA - G3" -p /System/Library/Keychains/SystemRootCertificates.keychain > AppleRootCA-G3.pem`,
  or download `AppleRootCA-G3.cer` from Apple's certificate authority page.
- `APPLE_STOREKIT_ENVIRONMENTS` (optional): default `Production,Sandbox`. `Xcode`
  (local StoreKit testing) is rejected unless explicitly listed.
- `APPLE_STOREKIT_APP_APPLE_ID` (optional): when set, notifications whose
  `appAppleId` differs are dropped.
- `APPLE_STOREKIT_CHECK_CERT_OIDS` (optional, default on): pins Apple's leaf
  (`1.2.840.113635.100.6.11.1`) and intermediate (`1.2.840.113635.100.6.2.1`)
  certificate OIDs as defence in depth. Escape hatch if Apple ever rotates them —
  the rejection reason appears in the event log as `leaf_oid_missing`.
- `POCKET_SUBSCRIPTION_PRODUCT_TIERS` (optional): `product_id:tier,...`. Default
  is that any verified subscription product maps to tier `pro`.

**Missing configuration is silent**, exactly like the APNs key: all three
endpoints stay reachable and return `200`, but always report the free tier with
`configured: false`, write nothing, and verify nothing. A single
`subscription_disabled` event is logged. The app must treat `configured: false`
as "this bridge has no paid tier", not as a verification failure.

The `entitlement` object (identical everywhere it appears):

```json
{"tier": "pro", "active": true, "status": "active",
 "product_id": "com.pocketagent.kernel.pro.monthly",
 "expires_at": 1787753718.6, "grace_expires_at": null, "auto_renew": true,
 "environment": "Production", "original_transaction_id": "5000000000000001",
 "updated_at": 1785161795.59, "configured": true}
```

- `status` is one of `active`, `grace`, `expired`, `revoked`, `none`. It is
  recomputed from timestamps on every read, so expiry needs no cron sweep.
- `active` is true for `active` and `grace` (Apple's billing-retry grace period
  still counts as paid). `tier` falls back to `free` whenever `active` is false.
- Times are epoch **seconds** (Apple's milliseconds are converted at the edge).

`entitlement` is included in the responses of `POST /app/v1/auth/apple` and
`GET /app/v1/account`, so a fresh sign-in already knows the paid tier without a
second round trip. `GET /capabilities` reports `subscription_configured`.

#### `POST /app/v1/subscription/verify`

Exchanges a StoreKit 2 transaction for server-side entitlement. Requires an
account session — the signature proves the subscription exists, the session is
what binds it to an Apple account so the user's other devices inherit it.

Request: `signedTransaction` (required), `signedRenewalInfo` (optional; supplies
`auto_renew` and the grace period).

Response: `{ok, configured, entitlement}`.

Errors: `SUBSCRIPTION_MISSING_RECEIPT` (400), `SUBSCRIPTION_INVALID_RECEIPT`
(401, any signature/chain failure), `SUBSCRIPTION_BUNDLE_MISMATCH` (400, a valid
receipt from a different app), `SUBSCRIPTION_ENVIRONMENT_REJECTED` (400),
`SUBSCRIPTION_RENEWAL_MISMATCH` (400, renewal info spliced from another
subscription). Idempotent: re-posting the same receipt changes nothing.

#### `GET /app/v1/subscription/status`

Requires an account session. Returns `{ok, configured, entitlement, entitlements}`
where `entitlements` lists every subscription record on the account (plan changes,
sandbox alongside production) for debugging. A device that never sent a receipt
sees the same `entitlement` as the device that did.

#### `POST /app/v1/subscription/notify`

App Store Server Notifications V2 receiver (renewals, refunds, cancellations,
plan changes). **Unauthenticated by design** — Apple cannot carry a bridge bearer
token, so the JWS certificate chain *is* the authentication. This is the only
subscription endpoint that must be publicly reachable; it is served through the
fixed-domain cloudflare tunnel, so the URL to register in App Store Connect is
`https://pocket.example.com/app/v1/subscription/notify`.

Request: `{"signedPayload": "<JWS>"}`.

Response codes matter, because non-2xx makes Apple retry for days:

- Valid and applied → `200 {ok, notification_type, subtype, applied: true, status}`.
- `TEST` notification, unconfigured bridge, another app's bundle id, or a
  notification with no `signedTransactionInfo` → `200` with `applied: false`.
  Nothing is written; retrying would be pointless.
- Signature or chain failure → `401`. Nothing is written.

Storage rules:

- Keyed by `original_transaction_id` (Apple's durable per-subscription id), in the
  `entitlements` table of the accounts DB.
- Writes are monotonic: a notification signed earlier than the stored state is
  ignored, because Apple's delivery order is not guaranteed. The one exception is
  revocation — a refund is terminal and always applies, so a late renewal can
  never resurrect a refunded subscription.
- Notifications carry no account identity. If one arrives before the app's
  `/verify`, the row is stored with a null `apple_user_id` (invisible to every
  account) and is claimed later when the app verifies the same transaction with a
  session. Subsequent notifications never clear an existing binding.

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

### Guest links（唯讀、短效、無帳號）

`POST /app/v1/guest-links` requires the normal owner/device bearer token and
accepts `{session_id, ttl_secs?}`. `ttl_secs` is clamped to 5 minutes through
7 days. The response is `{url, expires_at}`. The URL itself is the only guest
credential: it is an HMAC-signed token containing the session id and expiry.
The bridge stores neither the raw URL nor a token row, renders text-only
HTML with escaping and `noindex`, and returns `410` for invalid, expired or
unknown links without distinguishing those cases.

`POST /app/v1/guest-links/revoke-all` requires the same authenticated
owner/device bearer as the create endpoint and rotates the host-local guest
signing secret. It returns `{revoked: true}` and
invalidates all previously issued guest links. This is intentionally
all-links revocation; per-link revocation would require a token-hash denylist
and a retention policy.

#### Guest 互動版(2026-11-06,E3,t3code 旁觀者範式)

guest 頁升級為「能看、能擬稿,**送出權在 owner**」:

- **看**:頁內 5 秒輪詢(重抓同一頁 HTML、換 `#cards`/`#mine` 的
  innerHTML)。刻意不開 SSE / JSON API —— 無 auth 面保持極簡。
- **擬稿**:`POST /guest/{token}/suggest`,body `{text}`。無 auth(認證即
  token 本身,同 guest 頁);率限 **6 則/分/令牌**(429)、長度上限
  **2000 字**(413)、空文字 400;壞/過期/已撤銷 token 一律 **410**
  (revoke-all 轉密鑰後 suggest 跟著全滅)。文字落 canonical.db
  `guest_suggestions`(status `pending → sent|ignored|failed`),**絕不直接
  進 session**;渲染端(guest 頁)一律 HTML 轉義。
- **owner 面**:每則擬稿開一張審批(kind=`guest_message`、
  session_id=目標 session、detail=全文、meta `{suggestion_id, origin:
  "guest"}`、options `[{key:"send",label:"送出",style:"primary"},
  {key:"ignore",label:"忽略",style:"danger"}]`),進既有審批中心 + 審核
  推播;決議走既有 `POST /app/v1/approvals/{id}/decision`(或 v2 統一
  approve 路由):`{key:"send"}` = 把全文走該 session **既有** input 路徑
  (v2 統一路由,狀態落 `approved`);`{key:"ignore"}` = 不送(`denied`)。
  相容糖 `{approve: bool}` 照舊(true→send、false→ignore)。轉發失敗回
  `502 GUEST_FORWARD_FAILED`(審批已落終態不重送,suggestion 標 `failed`)。
  guest_message 審批是 DB-backed:pending 的 `response_capability` 恆為
  `live`(不走 provider 等待者探活)。

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

CC permission modals are anchored to the live terminal footer. A live permission
must remain visible even when historical progress text appears above it; a newer
composer or running footer must not resurrect an old permission. The operation
and file path are retained in the title/detail, and wrapped permission scopes
remain in each option's description. Detection only publishes the existing
approval card/push; it never grants permission or changes Claude's permission mode.

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
  "provider": "claude_code|cc2|codex|hermes|gemini",
  "title": "pocket-agent",
  "subtitle": "/Users/you/apps/pocketagent",  // workdir / 來源說明
  "status": "idle|running|waiting_approval|failed|stalled|done",
  "last_event_at": "2026-07-04T10:33:42Z",
  "capabilities": ["input","interrupt","approve","attachments","keys","replay","follow"],
  "meta": { /* provider 專屬,app 不硬依賴 */ }
}
```

#### 4.1a `status` 的六個值（2026-08-13 更新）

`stalled` 是本次新增到契約的字串。它**不是新行為** —— bridge 內部的
`runtime_status()` 一直算得出來，但 `/app/v2/sessions` 的 codex 列以前會把結果
壓成 `waiting_approval / running / idle` 三選一，把 `failed`／`stalled`／`done`
全部丟掉。修掉之後這三個值會真的送到 app，所以在此補齊文件。

| status | 意思 | app 端建議 |
| --- | --- | --- |
| `idle` | 沒有進行中的回合 | 一般樣式 |
| `running` | 回合進行中 | 轉圈／可中斷 |
| `waiting_approval` | 在等使用者（審批，或 codex 的 `waitingOnUserInput`） | 醒目、可點進去回應 |
| `failed` | 上一回合失敗，或 provider 回報 `systemError` | 紅字＋`meta` / `error` |
| `stalled` | 回合開著但超過 `CODEX_TURN_STALL_SECS`（預設 300s）沒有任何事件 | 建議比照 `running`，可加「疑似卡住」提示 |
| `done` | 回合已終結（本地記錄到終局） | 一般樣式 |

**app 端未處理的值一律當 `idle`／一般樣式即可**，不會壞；但 `stalled` 與 `done`
目前在 app 沒有對應 case，補上才看得出差別。

codex 這一列的 `status` 現在來自 `runtime_status()` 的單一真相，其中
provider（app-server）自己回報的 `ThreadStatus` 是權威來源：
`active` → `running`（帶 `waitingOnApproval`／`waitingOnUserInput` 旗標時 →
`waiting_approval`）、`systemError` → `failed`、`notLoaded` → 視為「無資訊」。

- **session id wire format 全寫**：`claude_code:{name}`、`codex:{thread_id}`、
  `hermes:{persona}`；delegation 列為 `delegation:{id}`（provider 欄仍是
  `claude_code|codex`）。`hp:`/`cc:`/`cx:` 是文件與路標用的三類簡稱，
  wire 上不用縮寫。routing 只 split 第一個 `:`。
- **delegation 是一等 row**：`GET /app/v2/sessions` 內 delegation 置頂列出，
  `meta` 必含 `work_order` 與 `takeover`（另含 `delegation` 完整物件與
  pending `approval`）。同一 Codex thread 已被 delegation 收養時，
  不再重複出現在裸 `codex:` 列。

### 4.1b OpenClaw（第四 provider，選配）

Notification isolation (2026-11-26): only exact `(sessionKey, runId)` pairs
issued by this bridge's input path may produce reply or approval APNs pushes.
Telegram/CLI/cron broadcasts, opening a session, and a `pocket-` run-id prefix
do not grant notification consent. History catch-up uses the same check; an
absent run ID fails closed. Input grants are registered before awaiting the
gateway and revoked on failed/cancelled submissions. Final events and their
acknowledged run aliases share one delivery attempt to prevent duplicate pushes.
The ledger is bounded to 4096 aliases with a 24-hour TTL and is process-local:
after a bridge restart, pre-restart turns remain readable but are not pushed.
This change does not implement per-user session/history authorization or an
external-session subscription API; it removes unsolicited gateway pushes.

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

### 4.1c CC provider v2（`cc2`，Claude Agent SDK 驅動）

> 2026-10-29 補。**本節補的是既有缺口**：`CC_SDK_PROVIDER=1` 在生產環境已經開著，
> 於是 bridge 一直在吐一個本文件沒有記載的 provider 值。按 §0「本文件是唯一規格」，
> app 端「不認得 cc2」在契約上是合規的——問題在這裡，不在 app。
> 實作細節見 `cc_sdk.py` 檔頭。

- **這是與 `claude_code` 並行的第二條 CC 路徑，不是取代。** tmux 路徑靠 pane-diff
  仿串流、擷取 pane 猜審批選單、`send-keys Esc` 中斷；cc2 走 Agent SDK 的結構化
  管道（真 token delta／`can_use_tool` 回呼／`interrupt()`）。
  **兩者同時存在、session id 前綴不同，client 必須當成兩種不同的東西。**
- **provider 值 = `cc2`，session id = `cc2:{name}`**（同 §4.1 全寫規則）。
  `/app/v2/agents` 會回 `{"provider":"cc2","name":"Claude Code (SDK)","kind":"code_agent"}`。
- **列表**：出現在 `GET /app/v2/sessions`（可 `?provider=cc2` 過濾）。
  **`?archived=true` 回封存的 session**（`status:"archived"`，與 tmux 的
  `GET /ccsessions?archived=true` 同字彙）；預設只回未封存。
  解除封存 = `POST /app/v2/cc2/{name}/archive` 帶 `{"archived": false}`。
  ⚠️ **不在 `/ccsessions`**——那支是 tmux 專屬的 takeover 端點。client 若只讀
  `/ccsessions` 就永遠看不到 cc2 session（2026-10-29 實查：iOS 與 Android 的 CC
  分頁都是這個情況，cc2 在手機上目前近乎隱形）。
- **上下文用量**：`session.status` 同時吐 **`usage`** 與 `context`（同一份資料：
  `{used, size, pct}`）。`usage` 是 tmux 一直在用、app 的 ContextUsage decoder
  直接吃的那個鍵 —— cc2 原本只吐 `context`，於是上下文儀表**整條空白而且不報錯**。
  新 client 請讀 `usage`；`context` 保留相容，等兩邊收斂再談移除。
- **審批**：`kind` 分 `question`（AskUserQuestion）與 `permission`，字彙與
  `/app/v1/approvals/{id}/decision` 一致（question→`answered`）。
  提問卡的 body 帶 `multiselect` / `q_index` / `q_total` / `q_headers`，
  選項帶 `detail`（選項說明）。**待審會發推播**（與 CC／OpenClaw 同一支）。
  ⚠️ 等決議上限 110 秒（必須早於 CLI 端 stdio 逾時才保證送得出明確 deny），
  逾時 = fail-closed 拒絕，卡片 `resolved:"expired"`（**不是** `denied`）。
  ⚠️ 複選題目前一次只收一個答案（管道限制），`multiselect` 仍會標真。
- **卡片流／輸入／中斷**：直接拿 `cc2:{name}` 打既有 v2 session 端點（§5、§7），
  卡片 schema 與 CC／CX／persona **完全同款**，渲染端零改動。
- **旗標關 = 整段缺席**：`CC_SDK_PROVIDER` 未設時 `/app/v2/cc2/*` 全回
  `404 CC2_DISABLED`、agents 不列 cc2、`cc2:` id 解析 404。
  正規探針 = **agents 清單裡有沒有 `provider:"cc2"`**。
- **專屬端點家族**（與 OpenClaw 不同，cc2 **有**自己的一組）：

  | 端點 | 用途 |
  |---|---|
  | `GET  /app/v2/cc2/{name}/history?limit&offset` | 歷史分頁（往前翻，見下） |
  | `GET  /app/v2/cc2/{name}/context` | context 用量 |
  | `POST /app/v2/cc2/{name}` | 建立 session |
  | `POST /app/v2/cc2/{name}/settings` | 模型／審核模式 |
  | `POST /app/v2/cc2/{name}/rename` | 改名 |
  | `POST /app/v2/cc2/{name}/compact` | 壓縮對話 |
  | `POST /app/v2/cc2/{name}/archive` | 封存 |
  | `POST /app/v2/cc2/{name}/yield` | 交還控制權 |
  | `POST /app/v2/cc2/_resume/{sid}` | 以 SDK session id 接回 |

- **進場歷史（seed）**：開 session 時 bridge 自動把 transcript 最後 N 則
  （`CC_SDK_HISTORY_SEED`，預設 **40**；0 = 關閉）種成 `final:true` 卡，帶
  **原始 ts**、不掛 `turn_id`，且**保證在一切 live 事件之前**（§7 snapshot 語意：
  app 渲染完 snapshot 才從 `latest_seq` 接 SSE）。種子卡 id 由位置決定
  （`card-cc2-{name}-h{idx}`），**重種是對同一批 id 做 upsert**，不會疊出第二份歷史。
  ✅ **2026-10-30 起與 tmux 同形狀**：seed 與 live 都走
  `carddigest.cc_event_to_cards`，所以 `tool_call`（含 `body.patch` →
  app 的 diff chip）、`tool_result`、`attachment`、thinking 都有；上限也改成
  「尾端 N 行」（預設 200，與 tmux 的 `_CC_CARD_SEED_LINES` 同語意）。
  種子卡 id 由 **jsonl 的 uuid** 衍生 → 重種是 upsert，不會疊第二份歷史。
  ⚠️ 仍有的差異：cc2 進場**不補** pending approval 卡（tmux 會）；
  live 的 thinking 尚未串流（seed 已有）。

- **`history` 的回應形狀**：

  ```jsonc
  { "messages": [ {"role":"user|assistant", "text":"…", "ts": 1761000000.0} ],
    "total": 123,        // 「讀得到的範圍內」的數量，不假裝是全部
    "more": true,        // 還有更舊的
    "truncated": true }  // 只讀了 transcript 尾端，前面讀不到 → more 必為真
  ```
  `offset` **從最新往回數**；超過總數回空頁（不繞回去拿最舊那幾則）。
  ⚠️ 已知不一致：`limit` 在路由層被夾成 `max(1, min(limit, 200))`，所以
  **`limit=0` 經 HTTP 永遠到不了** `history_page` 內部那條「0 → 空頁」分支。
  client 不要依賴 `limit=0`。

### 4.1d Gemini（第五 provider，選配）

> 2026-10-29 補，與 §4.1c 同一類缺口：bridge `/app/v2/agents` 會回
> `{"provider":"gemini","name":"Gemini","kind":"code_agent"}`，但本文件原本
> 0 次提及。

- **provider 值 = `gemini`，session id = `gemini:{key}`**（同 §4.1 全寫規則）。
- **名錄制**：列表走 `GET /app/v2/sessions?provider=gemini`；client 可本地生
  `gemini:{key}` 直接開卡片流，**第一則訊息才在 bridge 建 session**（零建立 API）。
- 卡片流／輸入走既有 v2 session 端點；審批鏡像到 canonical approvals 表
  （`provider=gemini`，同 OpenClaw 做法）。
- 未配置 = 列表回空，client 顯示空狀態。
- ℹ️ **client 側現況**：iOS／Android 都已實作但以編譯期旗標藏起來
  （`GeminiSurface.HIDDEN`，2026-10-11 機主拍板：quota 失敗與權限請求只能
  自動拒絕，怕稀釋信任）。契約照實記錄，與 client 要不要顯示是兩件事。

### 4.1e Grok Build（第六 provider，選配，2026-11-16）

> 接入依據：pa-ship `docs/GROK_BUILD_RESEARCH_20261116.md`「第五 provider
> 判決」。純 ACP（`grok agent stdio`，protocolVersion 1）——五家 code-agent
> 裡唯一不用自家 adapter 的，rail 形狀與 §4.1d 同構。

Dashboard additive extension (2026-11-20):
`GET /app/v1/dashboard` adds `sessions.grok` when the Grok binary is configured.
Counts come from the same roster as the Grok tab: `working` excludes approvals,
`needs_you` counts `waiting_approval`, and `idle` is the remainder. The default
entry follows that roster; no synthetic extra session is added. A failed/timeout
read omits the key and adds `grok` to `sessions.degraded`, not a false zero.
Clients must still honor the host's Grok capability declaration.

The same extension adds `sessions.active_provider` (`hermes`, `openclaw`, `none`,
or `auto`) from the existing host runtime selection. Clients show only the
selected persona runtime. `none` hides both; `auto` or a missing legacy field
uses existing persona availability, preferring Hermes when both are present.
An explicit selection must not fall back to the other runtime while loading.
These fields are additive; existing runtime processes and settings are untouched.

- **provider 值 = `grok`，session id = `grok:{key}`**（同 §4.1 全寫規則）。
- **名錄制**（同 gemini）：列表走 `GET /app/v2/sessions?provider=grok`；
  第一則訊息才在 bridge 建 ACP session（零建立 API）。bridge key 與 ACP
  sessionId 解耦：CLI 端 session 蒸發時換 acp_sid 續命，bridge 歷史不動
  → caps 宣告 `native_id_strength: weak`。
- **偵測**：`grok` binary 不在（PATH / `~/.local/bin` / `~/.grok/bin`，
  `POCKET_GROK_BIN` 可指定）= 整個 `grok:` id 空間 404、agents 無項、
  caps 不宣告 —— 靜默缺席。
- **認證**：`grok login` 憑證（`~/.grok/auth.json`，CLI 自己 refresh）或
  `XAI_API_KEY`（env 或 `PUT /app/v1/grok/config` 落檔 0600）。**未登入時
  provider 照樣露臉**（和 gemini 的「未配置=消失」不同）：`/app/v2/agents`
  的 `auth.connected=false`，turn 會收到登入指引卡（`grok login` /
  `--device-auth` / XAI_API_KEY），不是空白失敗。狀態讀
  `GET /app/v1/grok/config`（`installed` / `logged_in` 分開報）。
- **ops**（`_V2_SESSION_OPS`）：`rename` / `archive`（名錄層）/ `history`
  —— history 第一頁優先 `x.ai/session/updates` 負 offset 取尾（feature-
  detect，-32601 退場；回應帶 `source:"native"`），退 bridge 逐字稿；
  形狀一律 `{messages, more, total}`。
- **input**：§4.4 統一 input；client_id 去重、忙碌排隊（`queued:true` +
  `queue_depth`）、附件（影像=ACP image block、語音=轉寫、其他=落檔附路徑）。
- **interrupt**：標準 ACP `session/cancel`；無活躍 turn 一律 409。
- **resume**：ACP `session/load`（initialize 宣告 `loadSession:true` 才用；
  載不回自動 `session/new` 換 acp_sid 續命）。
- **審批**：`session/request_permission`（blocking reverse-request）→
  canonical approvals（`provider=grok`）+ 卡片流 approval 卡 + 推播。
  等待者是記憶體 Future（`approval_callbacks_live_only: true`）：bridge
  重啟後決定回 409，ACP 側由逾時路（180s，`POCKET_GROK_APPROVAL_SEC`）
  fail-closed 回 `cancelled`。放行偏好 `allow_once`（agent 把
  `allow_always` 排前面也不替使用者按「永久允許」）。
- **限制 / 下一波（v2）**：`x.ai/session/fork`、`x.ai/rewind/*`（caps 刻意
  不宣告 fork/rewind —— 管線存在≠端到端通）；per-session 模型
  （`session/set_config_option` 有，config_read/write 缺格已登記理由）；
  always-allow → per-session 記憶或 `session/new` 的 `_meta.yoloMode`；
  compact（CLI 有 `/compact` 指令與 `x.ai/compact_conversation`）。

### 4.1f OpenCode（第七 provider，選配，2026-11-16）

> 定位：Pocket **Build 主引擎**（MIT、75+ provider、OpenRouter 即插）。
> 純 ACP（`opencode acp`，protocolVersion 1），rail 形狀與 §4.1e 同構 ——
> 這是第三家純 ACP provider，JSON-RPC 泵已抽成共用層 `acp_rail.py`
> （grok/opencode 共用；gemini 與 hermes 人格軌是 production，刻意不動）。
> 下表每一列都是 2026-11-16 本機實測（1.19.5），wire bytes 錄在
> `tests/fixtures/opencode/recorded-stdio-20261116.txt`。

- **provider 值 = `opencode`，session id = `opencode:{key}`**。
  ⚠️ 與 `openclaw:`（§4.1b）只差三個字母：**前綴一律整段比對**，不可用
  `startswith("openc")` —— 搞混會把審批與 session 路由送錯家。
- **名錄制**（同 gemini/grok）：列表走
  `GET /app/v2/sessions?provider=opencode`；第一則訊息才建 ACP session
  （冷啟 5~26 秒，不在列表熱路徑上付）。bridge key 與 ACP sessionId 解耦
  → caps `native_id_strength: weak`。
- **偵測**：`opencode` binary 不在（PATH / `~/.opencode/bin` /
  `~/.local/bin`，`POCKET_OPENCODE_BIN` 可指定）= 整個 `opencode:` id
  空間 404、agents 無項、caps 不宣告 —— 靜默缺席。
- **認證：沒有登入牆（與其他家的分水嶺）**。opencode 內建免費模型池，
  **零憑證就能跑完整回合**（實測 `auth.json` 0 credentials 下
  `session/prompt` 照樣回 `end_turn`）。因此：
  `/app/v2/agents` 的 `auth.connected` 恆為 `true`、
  `GET /app/v1/opencode/config` 回 `auth_required: false`，
  `login_hint` 是**加值說明不是錯誤**（接 `opencode auth login` 或
  OpenRouter key 才解鎖 75+ provider）。client **不可**因為沒登入就藏入口
  或畫登入牆。OpenRouter key 走 `PUT /app/v1/opencode/config`（0600 落檔，
  注入子程序 `OPENROUTER_API_KEY`；模型填 `openrouter/<model-id>`）。
- **ops**（`_V2_SESSION_OPS`）：`rename` / `archive`（名錄層）/ `history` /
  **`config_read` / `config_write`** —— 五格全通。
- **per-session 設定（這家獨有，codex 之外唯一）**：
  `GET /app/v2/sessions/opencode:{key}/config` 回扁平 snake_case
  `{model, mode, model_options:[{value,name}], mode_options:[…]}`；
  `POST` 同路徑寫 `{model?, mode?}` → `{ok, applied, ignored, config}`。
  底層是 ACP `session/set_config_option`（欄位名是 **`configId`**；
  寫 `configOptionId` 回 -32602，坑錄在 fixture）。寫入前對 agent 給的
  options 驗一次，不認得回 400 `OCE_UNKNOWN_VALUE` 並列出可用值
  （盲寫會把 session 釘在不存在的模型上）。`model` 會存成「新 session 的
  預設偏好」並在 `session/new` 後自動套用；`mode`（build/plan）只屬於
  這條對話，不落全域。
- **input**：§4.4 統一 input；client_id 去重、忙碌排隊（`queued:true` +
  `queue_depth`）、附件（影像=ACP image block，`promptCapabilities.image`
  feature-detect；語音=轉寫；其他=落檔附路徑）。
- **interrupt**：標準 ACP `session/cancel`；實測 prompt 在 0.7 秒內回
  `stopReason:"cancelled"`（真收束，非 advisory）。無活躍 turn 一律 409。
  v1（`POST /app/v1/messages/interrupt`）與 v2 兩線都接。
- **resume**：ACP `session/load`（`loadSession:true` 才用；載不回自動
  `session/new` 換 acp_sid 續命）。load **會 replay 歷史逐字**，bridge 在
  泵的旁聽掛鉤擷取 → 這就是 history 的原生來源（零額外 RPC、零冷啟）。
- **history**：分頁權威是 bridge 逐字稿（offset 從最新往回數，形狀
  `{messages, more, total}`）；逐字稿為空時才用 load replay 補那一格並帶
  `source:"native"`（`more` 恆 `false`）。第二頁以後一律只看逐字稿 ——
  單一母體紀律（混用會讓翻頁重複或缺漏）。
- **審批**：`session/request_permission`（blocking reverse-request）→
  canonical approvals（`provider=opencode`）+ 卡片流 approval 卡 + 推播；
  列表同步標 `waiting_approval` + `meta.approval`。實測 options =
  `once` / `always` / `reject`（kind `allow_once`/`allow_always`/
  `reject_once`），放行偏好 `allow_once`（不替使用者按「永久允許」）。
  等待者是記憶體 Future（`approval_callbacks_live_only: true`）：bridge
  重啟後決定回 409；逾時（180s，`POCKET_OPENCODE_APPROVAL_SEC`）
  fail-closed 回 `cancelled`。
- **刻意不支援 compact**：`availableCommands` 實測沒有 compact，
  `POST /app/v2/sessions/opencode:{key}/compact` 回 400
  `UNSUPPORTED_PROVIDER`。client 要照 caps 渲染（不要畫壓縮鈕）——
  假裝把 `/compact` 當訊息送出去只會讓 agent 回「我不懂這個指令」。
- **限制 / 下一波**：`sessionCapabilities.fork` 存在但端到端未驗，caps
  **不宣告** fork/rewind（管線存在≠端到端通）；`always` 選項 →
  per-session 記憶；`session/list`（原生標題/updatedAt）目前只在
  client 內部用，未併進列表標題。

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
| `GET  /app/v2/sessions/{id}/events?since_seq=&profile=&after_durable_seq=` | SSE 卡片事件流（§5；`after_durable_seq` = 持久補洞,§5.1） | ✅ 上線（S1：cc；同上） |
| `GET  /app/v2/sessions/{id}/media?limit=&cursor=` | session 媒體永久索引（§7） | ✅ 上線（cc/cx/persona） |
| `GET  /app/v2/artifacts/{media_id}` | 已封存 artifact 位元組 | ✅ 上線 |
| `GET /app/v2/sessions/{id}/reports[?media_id=...]` | 已交付 Markdown 報告清單/讀取，沿用 replay grant | 候選版，見 `CONSOLE_REPORT_READER.md` |
| `POST /app/v1/guest-links` | 建立短效、唯讀、無帳號分享連結 | ✅ 上線 |
| `POST /app/v1/guest-links/revoke-all` | authenticated owner/device 輪替 guest signing secret，使既有連結全部失效 | ✅ 上線 |
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
GET /app/v2/sessions/{session_id}/events?since_seq=N&profile=phone&after_durable_seq=D   (SSE)
```

- **事件信封**：`{"seq": int, "ts": epoch, "type": str, "data": {...}, "durable_seq": int|null}`
  - `durable_seq`（2026-11-06，T3#1）：此事件在持久 `event_log` 的全域 id。
    **只有里程碑有值**（`final:true` 的 card.upsert、`session.status`、`turn`）；
    純串流 delta（`final:false`）、ping、去重重放（同一終態卡 reseed）為
    `null`。client 以「最後一個非 null 的 `durable_seq`」維護**持久游標**，
    `null` 時游標不動。
- **seq**：per-session 嚴格遞增。bridge 保留 ring buffer（近 2000 事件或 7 天），
  `since_seq` 補洞重放；超出範圍回 `410 Gone`（`SEQ_GONE`）→ app 改走
  snapshot 冷載。**例外**：請求帶了 `after_durable_seq` 時不回 410，改走
  §5.1 持久補洞。不帶新參數的舊 client 行為完全不變。
- **事件類型**：
  - `card.upsert` — `{card}`（見 §6）。串流中的訊息＝同一 card id 反覆 upsert、
    `rev` 遞增、`final:false→true`。**app 只做替換渲染，永不解析 provider
    原始格式**。
  - `session.status` — `{busy, mode, prompt, phase, label, locked}`。`label` 是
    **伺服器給的人話階段**（「思考中」「執行工具:Bash」「等待核准」「回覆中」）
    ——手機「即時跟到處理狀況」的直接載體，UI 原樣顯示。
    `locked`（bool，**恆存在**）＝ 這條 codex thread 正被**另一顆** codex
    app-server（桌面版 ChatGPT/Codex、VS Code）握著 thread-store 寫入鎖。
    為 `true` 時另附 `lock_reason: "thread_store_conflict"` 與 `lock_message`
    （zh-TW 人話，UI 原樣顯示）。**app 應據此掛 banner 並停用輸入框**——
    否則使用者是對著一個必定回 409 的輸入框打字（2026-08-10 實機事故的症狀
    就是「點了沒反應」）。鎖放開後 bridge 會把它翻回 `false` 並推一張恢復卡，
    不需要重啟 bridge、不需要重進 session。
    同一件事的其他出口：input 兩條路由回 `409 / CX_THREAD_LOCKED`；
    `GET /codexsessions/{id}/status` 的 `session.locked` / `lockReason` /
    `lockMessage`；`GET /app/v2/sessions` 每列的 `locked` 與 `meta.lock`。

    **`phase: "usage_limited"`(2026-11-06,t3code usage_limit 分類)**：
    回合因**限額/配額/速率上限**失敗、且 session 已停下時,phase 不是
    `idle` 而是 `usage_limited`,並附 `usage_limit_reset_at: epoch|null`
    (限額文案解析得到 reset 時間就帶,解析不到誠實 null,**app 不要自己
    猜**)。這是 app「Resume-at-reset」的依據:有 `usage_limit_reset_at`
    就可以排程在該時刻自動重送/提醒;null 則只顯示「額度已達上限」。
    解除條件:新回合真的開跑(`turn begin`)或 reset 時刻已過(bridge 下
    一次發 status 時自動不再蓋)。覆蓋面:cc2(Claude Agent SDK)、codex
    (app-server turn error)、gemini/agy 與 hermes persona(turn_end
    error);tmux CC 沒有結構化錯誤出口,此 phase 不會出現在該後端。
  - `turn` — `{state: "begin"|"end"|"interrupted", turn_id}`。
  - `ping` — keepalive，統一 `SSE_KEEPALIVE_SECS`。
  - `resync` — **只出現在 §5.1 持久補洞段的結尾**：
    `{durable_seq: N, ring_seq: M}`。client 收到後把持久游標設為 `N`、
    ring 游標重定為 `M`（之後的 ring 事件 `seq` 從 `M+1` 起）。
- **真相原則**：SSE 為唯一真相；輪詢僅在 stream 斷線 >10s 時作 fallback，
  重連成功即停。
- 取代關係：CONTROL_PLANE_V2 的
  `assistant_delta|tool_start|tool_result|thinking|…` 結構化事件 schema
  **由卡片 digest 取代**，不再實作；其 `seq`+`since_seq` 續傳設計保留如上。

### 5.1 持久補洞（`after_durable_seq`，2026-11-06 T3#1）

**為什麼**：ring 是記憶體，bridge 重啟即歸零；舊協定下 client 的 `since_seq`
必然領先 → 410 → 整頁 snapshot 冷載，體感就是「閃舊訊息/整頁重載」。
持久化側 `event_log`（canonical.db，全域單調 id）已鏡射所有里程碑
（`card.milestone`、`session.status`、`turn`；串流 delta **刻意不落地**），
重連改從它補洞，410 只剩沒帶新參數的舊 client 會看到。

**client 要維護兩個游標**：
- ring 游標（既有 `since_seq`）：snapshot 的 `latest_seq` / 信封 `seq`。
- 持久游標（新 `after_durable_seq`）：snapshot 的 `durable_seq`（§7）/
  信封非 null 的 `durable_seq` / `resync.data.durable_seq`。

**重連協定**：
1. 照舊帶 `since_seq`，**加帶** `after_durable_seq=<持久游標>`。
2. ring 游標還有效 → 行為與舊協定完全相同（ring 補洞 + 續流），無補洞段。
3. ring 斷層（重啟/游標被環擠掉）→ bridge **不回 410**，依序送：
   a. **補洞段**：`event_log` 中該 session `id > after_durable_seq` 的里程碑，
      轉成既有 wire 形狀（`card.milestone`→`card.upsert`，其餘同名）。
      補洞信封 `seq=0`（不是 ring 游標，**不要**拿去推 ring 游標）、
      `durable_seq`=全域 id（拿它推持久游標）。
   b. **`resync` 信封**：`{durable_seq: N, ring_seq: M}`。`N` 含被濾掉的
      非里程碑列（`message.upsert` 等屬 `/app/v2/events` 的型別不會漏進
      卡片流，但持久游標要跨過它們）；`M` = ring 當前尾端。
   c. **續流**：ring 事件照常，`seq` 從 `M+1` 起。
4. 補洞上限 `POCKET_DURABLE_BACKFILL_MAX`（預設 2000）；里程碑稀疏，
   正常天數的離線量遠在其下。

**重複與漏洞的取捨**：bridge 先釘 ring 尾端再查 DB —— 競態窗內的里程碑
可能補洞段與續流各出一次。`card.upsert` 以 id+rev 冪等、status/turn 重放
無害；**寧可重複，不可漏洞**。client 不得假設補洞段與續流互斥。

**delta 的語意**：重啟後打字機過程不重放（持久側只有終態卡）。app 重連
看到的是「最後完整結果」，不是重播動畫 —— 這是拍板行為，不是缺陷。

## 6. 卡片 schema v1（裝置 UI 語言）

```json
{
  "id": "card-…",          // 穩定 id;串流中不變
  "turn_id": "…",
  "role": "user"|"assistant"|"system",
  "kind": "text"|"markdown"|"tool_call"|"tool_result"|"diff"|"approval"|"status"|"table"|"kv"|"attachment"|"calendar_event",
  "rev": 3,                 // 同 id 遞增;app 以最高 rev 為準
  "final": false,
  "ts": epoch,
  "body": { ... }           // per-kind;所有 kind 必附 "fallback_text"
}
```

- per-kind body 重點：`calendar_event`(行事曆/提醒提案,見 §14)、
  `tool_call {tool, summary, detail?, patch?}`、
  `approval {approval_id, title, options[{key,label,style}], source}`、
  `diff {path, adds, dels, hunks_text}`、`status {label, spinner:bool}`。
- text/markdown 可帶
  `attachments[{kind,filename,mime,path?,url?,media_id?,download_url?,source_url?,byte_size?,available?}]`；
  attachment-only 訊息不得被 digest 丟棄。`attachment` kind 的 body 使用同組
  欄位（單一附件）。**附件從哪來、哪些 provider 會帶 → §6.1**。
- `tool_call.patch`（選配，2026-07-04 diff 卡缺口）：`{path, text, adds, dels}`
  — **該步驟自身**的變更內容，由 digest 從工具輸入（Edit old/new、Write
  content…）合成，不依賴 worktree 事後狀態——步驟過後再 commit 也能回看單步
  變更，replay 重放產出相同 patch。`text` 為 `-`/`+` 前綴行、hunk 以 `@@`
  分隔（**無行號**——事件裡沒有整檔上下文，digest 不回讀檔案以保 replay
  穩定）；上限 20k 截斷。app 不認得就忽略（fallback 原則）。
- **error 卡的 `body.meta`(2026-11-06,usage_limit 分類)**:回合錯誤卡
  (system/text,`⚠️ …`)在錯誤被分類為限額類時附
  `meta: {"error_kind": "usage_limit", "reset_at": epoch|null}`。
  `reset_at` 從 provider 文案解析(CC 的 `usage limit reached|<epoch>`、
  CX 的 `Try again in 4 days 2 hours`、Gemini 的 `retry in 32.5s` /
  RetryInfo…),解析不到 = null。與 `session.status` 的
  `phase:"usage_limited"` / `usage_limit_reset_at` 同源 —— app 的
  Resume-at-reset 讀這兩處任一即可;不認得 `meta` 的舊 client 照舊只渲染
  文字(fallback 原則)。普通錯誤(context 超長、exit code、認證失敗…)
  **不會**帶此 meta,分類器是保守白名單。
- **fallback 原則**：不認得的 kind 一律渲染 `fallback_text`——舊 client 永不壞。
- **digest 責任在 bridge**：CC jsonl、codex app-server 事件、persona stream →
  統一 parser（`carddigest.py`）產卡片。**一份 parser，伺服器端，所有終端共享**。

### 6.1 附件來源:哪些 provider 會帶 attachments(2026-11-17)

> 治「CC 給我影片/檔案在 Pocket 看不到,要開 Claude 才看得到」(機主
> 2026-11-17 實錘)。根因:「本機檔案 → 可點附件」這條管線**只接在 codex 的
> seed 一條**;CC 把檔案收進了媒體庫卻把擷取結果丟掉、cc2 連收都沒收 ——
> 於是 agent 產出的路徑在卡片裡只是裸文字。現在所有 provider 共用同一支
> 掃描器(掃**卡片**,不是掃各家 provider 的原始事件)。

**client 唯一該信的欄位是 `download_url`**(`/app/v2/artifacts/{media_id}`,
Bearer 認證)。`path` 是本機絕對路徑,只是出處標示,裝置上永遠拿不到;沒有
`download_url` 的附件 = 沒封存成功,照 `fallback_text` 渲染。

| provider | 使用者上傳的附件 | agent 產出物 → 可點附件 | live(回合中) | 冷載(進場) |
|---|---|---|---|---|
| `codex:` (cx) | ✅ | ✅ | ✅ `turn/completed` | ✅ |
| `claude_code:` (cc, tmux) | ✅ | ✅ | ✅ 逐字稿 tail 落卡時 | ✅ |
| `cc2:` (Claude Code SDK) | ✅ | ✅ | ✅ 回合收尾 | ✅ |
| `gemini:` | ✅ | ✅ | ✅ 回合收尾 | ✅ |
| `grok:` | ✅ | ✅ | ✅ 回合收尾 | ✅ |
| `opencode:` | ✅ | ✅ | ✅ 回合收尾 | ✅ |
| `openclaw:` | ✅(訊息體) | ⚠️ 只有冷載 | — | ✅ |
| `hermes:` (人格) | ✅ | ⚠️ 只限 `/tmp` | ✅ follower | ✅ |

- `openclaw` 是**遠端** agent:它產的檔案不在這台機器上,本機路徑偵測結構上
  就查不到(`isfile` 永遠 false),所以只掛共用的冷載掃描、不另接 live。
- `hermes` 人格沒有工作目錄的概念 → 允許根目錄只有 `/tmp`;要放寬走
  `POCKET_MEDIA_ATTACH_ROOTS`(冒號分隔)。
- `codex` 的工作目錄來自旁觀快取(列過 thread / 查過 workdir 就會記下)。
  **冷啟後第一次**還沒快取到時只掃暫存區 —— 掛不到不會掛錯,列一次
  session 清單就補上。
- base64 直回的圖(MCP 截圖之類,**沒有檔案路徑**)另有一條既有管線:
  cc/cc2 的 `media_sink` → `capture_bytes` → `attachment` 卡,不經路徑偵測。
- `GET /app/v2/sessions/{id}/media` 的索引範圍**比附件寬**:那是「這條
  session 碰過哪些檔」,連 `.py` 都收。附件是其中「適合當媒體呈現」的子集。

**掛在哪張卡**(優先序,契約 §6 只保證前兩類會渲染 `attachments`):
1. `attachment` 卡 —— 專為那支檔存在的那張(CC/cc2「agent 看了一張圖」)
2. `text` / `markdown` 卡 —— 回合尾的文字摘要,「這是你要的東西」的位置
3. `tool_call` / `tool_result` 只做**保底**:該回合沒有任何上面兩類提到它時
   才掛(additive;不認得的 client 照 fallback 原則忽略)

為什麼要有優先序:實測的 CC 形狀是「先宣告、再動手」—— markdown
「我把報告寫到 /x.pdf」→ `tool_call Write` → `tool_result`。單純取「最後
提到的那張」會讓 `tool_result` 搶到,而**會渲染的** markdown 卡拿不到 →
使用者看到的還是裸路徑,bridge 的 log 卻報成功。

**哪些檔案會被掛**(白名單 `media_artifacts.ATTACHABLE_KINDS`):
`image` / `video` / `audio` / `pdf` / `document` / `archive`。
**刻意排除** `text`(`.md` `.json` `.log` `.txt` `.yaml` `.csv`…)與原始碼
(`.py` `.swift`…)—— code agent 每一步都在講這些檔名,全掛上去的結果是每張
卡底下一排垃圾附件(2026-10-29 發生過一次:一個 pptx 旁邊掛了 15 個碎片)。
程式碼變更走 `tool_call.patch` / diff 卡,不走附件。

**哪些路徑會被掛**(安全邊界,三道門全部要過):
1. realpath 之後落在 **session 工作目錄(含子目錄)或 `/tmp`** 之內 ——
   **解完符號連結才比對**,所以工作目錄裡指向外部的 symlink、`..` 穿越都
   過不了。這比媒體庫自己的封存範圍(整個家目錄)**窄**:能封存 ≠ 能變成
   下載連結。另外兩個刻意的排除:
   - macOS 的 `/var/folders`(per-user TMPDIR,住著每個 app 的快取)
   - **太大的工作目錄**:家目錄、`/Users`、`/home`、`/var`、`/etc`…
     (cc2 沒給 workdir 時 cwd 會是 `~`,不擋就等於整個家目錄可下載)
2. 檔案真的存在、是一般檔案(不 stat 就不算偵測到)。
3. 大小在媒體庫上限之內;超過的只進 `/media` 索引
   (`available:false`、`unavailable_reason:"too_large"`),**不**掛成附件。

**冪等與事件**:已經有 `attachments` 的卡不覆寫(live 事件帶的附件優先)。
同一支檔案在**同一回合**只掛一張卡;**跨回合刻意不去重** —— codex 常在第 3
回合與第 18 回合引用同一份報告,只給最後一次的話使用者往上捲就又看到裸文字。

掛上附件**一律重發 `card.upsert`**(同 id、`rev` +1、`ts` 不變),冷載與 live
都一樣;client 照既有的「以最高 rev 原位替換」處理即可,不需要任何改動。
(不分冷載/live 是刻意的:兩條路共用「掃過了」記憶,若其中一條不發事件,
先跑到的那條就會把工作吃掉而讓在線的 client 永遠收不到更新。)

**先提到、後產生**:`ffmpeg … out.mp4` 的工具卡會在檔案寫完前就落地。這種
「還沒出現」與「永久不合」在 bridge 內部是兩種不同的拒絕 —— 前者保留重試,
所以檔案寫完後的下一次掃描會補掛上。

## 7. 冷載 snapshot（本地快取契約）

```
GET /app/v2/sessions/{session_id}/cards?limit=100&before_seq=M
```

回 `{cards: […], latest_seq: N, status: {…}, durable_seq: D}` → app 渲染後從
`since_seq=N` 接 SSE。`durable_seq`（2026-11-06 加欄，既有欄位一個不動）＝
該 session 在持久 `event_log` 的最大全域 id（沒有里程碑 = 0）——冷載時
一併記下，之後重連都帶 `after_durable_seq`（§5.1），ring 斷層不再 410。
`limit` 上限 500。**app 本地快取＝卡片庫**（key: session_id + card.id + rev）：
離線可讀、進場秒開；快取只是 snapshot，永不當真相。

### 7.1 歷史拉取:負 offset 取尾 + 回合切片(2026-11-16,grok steal #4)

> 治「進場閃舊訊息」:client 要的是「最後一回合/最後 N 張」秒開,再按需
> 往前翻 —— 不是冷載整包再自己丟掉前面。語義照 grok `x.ai/session/updates`
> 的 offset/turnIndex 設計。全部 additive;既有 `before_seq`/`before_id`
> 分頁一個 byte 不動,但**與新參數互斥**(混用回 400)。

```
GET /app/v2/sessions/{id}/cards?offset=-50&limit=50      # 尾巴 50 張
GET /app/v2/sessions/{id}/cards?turn_index=-1&limit=200  # 最新一個使用者回合
```

- `offset`:位置視窗。**負值從尾巴數**(Python slicing 語義:`-50` =
  最後 50 張起);非負 = 從頭數。回應多帶:
  - `window_start`(視窗落在第幾張)、`total_cards`(卡片庫現存總數)
  - `has_more`(視窗之前還有歷史)、`has_more_after`(視窗之後還有,
    正向分頁用)
- `turn_index`:按**使用者回合**切片(邊界 = `role=="user"` 的卡開新回
  合;被裁掉的歷史開頭無主卡自成第 0 片)。負值從最新回合倒數
  (`-1` = 最新)。回應多帶 `turn_index`(正規化後的非負值)、
  `turn_count`;超出保留範圍回 **409 `TURN_INDEX_GONE`**(卡片庫只保留
  最近 `POCKET_CARD_STORE_MAX` 張,更早的回合 bridge 已經沒有 ——
  client 退 `offset` 位置視窗或放棄)。
- `has_more` 同時升格為**所有模式**的一等回應欄位(含既有
  snapshot/`before_seq` 路徑;以前只在 paged 模式以 `older_cursor` 間接
  表達)。
- 兩個新參數彼此互斥、也與 `before_seq`/`before_id`/`history_page` 互斥
  (400)。`latest_seq`/`status`/`durable_seq` 欄位照舊 —— 不論用哪種
  視窗拿歷史,SSE 續流與持久游標語義完全不變。

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
  `{id, session_id, provider: claude_code|cc2|codex|hermes|gemini, kind: permission|question|notice,
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
  CX 的指令／檔案核准若帶 `availableDecisions`，卡片只顯示本次可表達的選項。
  沒有 `acceptForSession` 就不提供「本次全允許」；永久規則修改不是 session 授權。
  若可拒絕指令，`deny` 送 `decline`；若供應端只提供取消回合，按鈕改標
  「停止這一輪」，送 `cancel`。回傳未提供的選項會收到 400 且原請求保留。
  舊供應端省略清單時才沿用該方法原有的選項。
- **status 字彙**:pending / approved / **denied**(新決議;歷史列 `rejected` 等價,
  codex 線相容期仍寫 rejected)/ answered(question,result=key)/
  acknowledged(notice)/ expired。
- **建立**(hermes/skill):`POST /app/v1/approvals {title, session_id, kind?, risk?,
  detail?, options?, ttl_seconds?, callback_url?}`;`source` 為 `session_id` 舊名。
- **hermes waiting_approval**:persona 有 pending 時 v2 sessions 該列
  `status=waiting_approval` + `meta.approval`(之前恆 idle)。

### CC auto-mode onboarding (2026-11-27)

- The live `Teach auto mode about your environment?` form is a `notice`, not
  a permission request. `/ccsessions/{name}/status` exposes a `menu` prompt
  with `semantic: notice`, `busy: false`, and `mode: null` (the bar is hidden).
  The card stream reports `phase: blocked`, label `等待操作`, even while an
  earlier input is queued. The existing approval watcher deduplicates and
  publishes a notice card and notification.
- Its only option is `{key: cancel_onboarding, label: 取消導覽}`. Clients must
  send that key unchanged through the approval decision or CC key endpoint.
  It rechecks the live form under the session input lock and sends **Escape
  only**, never Enter, Space, Continue, scan consent, or a permission change.
- A resolved notice returns `acknowledged`; repeated decisions return the
  existing receipt. An obsolete action returns `409 PROMPT_STALE` without
  sending a key. Shipped CC card clients that send only `{approve: bool}` are
  supported: either boolean value activates this notice's **sole cancel
  action**, never Continue or consent. Unoffered keys return `400 UNKNOWN_KEY`.
- Detection never dismisses the form automatically. New chat input is rejected
  with `409 CC_INPUT_NOT_ACCEPTED` until it is handled, rather than being
  pasted into a settings control. No previous command is automatically retried.
- Recognition is intentionally limited to the known, bottom-anchored form
  with its navigation/toggle/cancel footer. Future CLI layout changes require
  new fixtures; arbitrary terminal prose is not treated as actionable UI.

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
    "provider": "claude_code|cc2|codex|hermes|dispatch|openclaw|gemini",
    "name": "amulet-hunter",
    "workdir": "/Users/you/apps/amulet-hunter",
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

## 14. 行事曆 / 提醒「提案」(`CALENDAR_PROPOSALS`,2026-08-13)

> **鐵律:agent 永遠不寫使用者的行事曆。** agent 只能發一張**提案**,
> Pocket 把它渲染成確認卡,**使用者按下去**才由 app 自己用 EventKit 寫入,
> 寫完回打 `/resolve` 告訴 bridge 結果。這條線的三個好處:沒有背景執行、
> 每一次寫入都有明示的逐次同意(App Review 友善)、使用者永遠看得到
> 「發生了什麼」。bridge 端**不含任何 EventKit/CalDAV 程式碼**,也不存
> 使用者的行事曆內容。

**旗標**:`CALENDAR_PROPOSALS=1`(預設 **OFF**)。關著時兩個端點都回
`404 CALENDAR_PROPOSALS_DISABLED`,`GET /capabilities` 的 `features` 也不會
出現 `calendar_proposals` —— **app 用這個 feature 探針決定要不要顯示相關 UI**,
不要靠打端點去試。

### 14.1 卡片 kind `calendar_event`(卡片 schema v1,§6)

```jsonc
{ "kind": "calendar_event",
  "body": {
    "proposal_id": "cal-<uuid8>",       // bridge 指派;app 確認時原樣回送
    "target": "calendar" | "reminder",
    "title": "與王總開會",               // ≤200 字
    "notes": "…",                        // 選配,≤2000 字
    "start": 1786690800.0,               // epoch 秒;reminder 可省略
    "end": 1786694400.0,                 // 選配;calendar 未給且非整天 → start+1h
    "all_day": false,
    "due": 1786690800.0,                 // reminder 專用(未給則沿用 start)
    "alarm_minutes_before": [10],        // 選配,≤5 項,每項 0–40320 分鐘
    "recurrence": "none|daily|weekly|monthly",   // v1 只做這四種
    "location": "…",                     // 選配,≤200 字
    "tz": "Asia/Taipei",                 // IANA,**必填**,bridge 絕不假設裝置時區
    "state": "proposed" | "accepted" | "declined",
    "calendar_event_id": "<EKEvent id>", // resolve accepted 後才出現
    "error": "…",                        // resolve 帶了 error 才出現
    "fallback_text": "📅 建議行程:與王總開會 8/14 15:00–16:00"   // **必存在**
  } }
```

- 卡片 `id` = `card-<proposal_id>`(即 `card-cal-<uuid8>`)、`role` =
  `assistant`、`turn_id` = `""`。
- **fallback 原則照舊(§0/§6)**:不認得 `calendar_event` 的舊 client 一律渲染
  `fallback_text`,所以那一行**三態都要看得懂**,由 bridge 依 `state` 改寫:
  - `proposed`:`📅 建議行程:…` / `⏰ 建議提醒:…`
  - `accepted`:`✅ 已加入行事曆:…` / `✅ 已加入提醒事項:…`
  - `declined`:`✖️ 已略過:…`
  時間一律用提案自己的 `tz` 格式化,**不吃伺服器 local time**。
- app 若想自己排版,請用結構化欄位;`fallback_text` 是降級路徑,不是資料源。

### 14.2 `POST /app/v2/sessions/{session_id}/proposals/calendar`

agent(經 MCP 工具)呼叫。body = 上面的 `body` **去掉 `proposal_id` 與
`state`**;bridge 指派 id、狀態一律從 `proposed` 起算,並把卡片 upsert 進那條
session 的卡片流(即 §5 的 `card.upsert` 事件 + §7 的 snapshot)。

回應:`{ok, proposal_id, card_id, session_id, state:"proposed", body}`。

**驗證(全部 `400 CALENDAR_PROPOSAL_INVALID` + zh-TW 訊息)** —— 自然語言時間
不是 bridge 的工作,agent 自己算好 epoch,但 bridge 會擋下這些:

| 情況 | 訊息重點 |
|---|---|
| `tz` 缺 / 不是有效 IANA 名 | `tz 必填` / `不是有效的 IANA 時區名` |
| `title` 空 / >200 字 | `title 必填` / `title 過長` |
| `notes` >2000、`location` >200 | `… 過長` |
| epoch 非數字 / NaN / ±Inf | `必須是 epoch 秒(數字)` |
| epoch < 2000-01-01 或 > 2100-01-01(含把毫秒當秒) | `超出合理範圍(2000–2100 年);確認你送的是**秒**不是毫秒` |
| `end <= start` | `end 必須晚於 start` |
| 給了 `end` 卻沒 `start` | `給了 end 就必須給 start` |
| `target=calendar` 缺 `start` | `start 必填` |
| `target=reminder` 且 `due`/`start` 都缺 | `提醒事項至少要有 due 或 start 其中之一` |
| `target` 不是 `calendar|reminder` | `target 必須是 calendar 或 reminder` |
| `recurrence` 不在四種之內 | `recurrence 必須是 none/daily/weekly/monthly` |
| `alarm_minutes_before` 非陣列 / 非數字 / 界外 / >5 項 | 各自對應訊息 |

未知 `session_id` → `404 SESSION_NOT_FOUND`(與其他 v2 session 端點同語意)。
被擋下的提案**不會留下任何卡片**。body 是**白名單**:agent 亂加的欄位(含自封的
`proposal_id`/`state`)一律丟棄,不會外流給 app。

### 14.3 `POST /app/v2/proposals/{proposal_id}/resolve`

app 寫入完成(或使用者拒絕)之後回報:
`{state: "accepted"|"declined", calendar_event_id?: "<EKEvent id>", error?}`。

bridge 只更新那張卡:`state` 改寫、`rev++`、`fallback_text` 換成人話,
於是 agent 的卡片流(以及 `agent_context` 讀到的內容)看得到結果。
回應 `{ok, proposal_id, session_id, state, calendar_event_id, error,
already_resolved}`。

- **冪等**:同一個 id 再 resolve 一次回**第一次**的結果、`already_resolved:
  true`,卡片不再變動(rev 不動)。app 重試網路失敗是安全的。
- `state` 不是 `accepted`/`declined` → `400`。
- 未知 id → **`404 CALENDAR_PROPOSAL_NOT_FOUND`**,見下。

### 14.4 儲存與 **app 必須處理的 404**(重要)

提案就存在卡片裡(session store),外加一顆**有界的記憶體索引**
(`proposal_id → session`,預設 500 筆,`CALENDAR_PROPOSAL_MAX` 可調)讓
`/resolve` 找得到卡。**不保證跨 bridge 重啟,也不保證無限期保留**:

- bridge 重啟後、或索引被更新的提案擠出去之後,舊卡片**還在畫面上**
  (卡片本身可能也隨 session store 一起消失),但 `/resolve` 會回
  `404 CALENDAR_PROPOSAL_NOT_FOUND`。
- **app 的正確處理**:不要靜默失敗、不要重試、更不要因此不寫行事曆 ——
  若使用者已按下確認且 EventKit 已寫入,**行事曆是寫成功的**,只是 bridge
  記不住了;請把卡片標成「無法回報結果」並提示使用者
  「這筆提案已過期,請再請 agent 提一次」(bridge 的 404 訊息就是這句,
  可原樣顯示)。
- 同理 `404 CALENDAR_PROPOSAL_CARD_GONE` = 卡片已被 session store 的環形上限
  擠掉,處理方式相同。
- 因為索引是行程內記憶體,**多 bridge 行程 / 重啟中的請求**都可能 404;這是
  設計取捨(換來零 DB schema 變更、merge 零風險),不是 bug。

### 14.5 agent 端(MCP 工具)

`scripts/agent-call-mcp.py` 增加兩個工具,與 `agent_call`/`agent_context`
同一顆 stdio server(同樣是極薄包裝,權限與驗證都在 bridge 側):

- `propose_calendar_event(title, start, tz, end?, notes?, location?, all_day?,
  alarm_minutes_before?, recurrence?, session?)`
- `propose_reminder(title, tz, due?, notes?, all_day?, alarm_minutes_before?,
  recurrence?, session?)`

兩者都 POST 到 §14.2;`session` 省略時 = `AGENT_CALL_SELF`(提案卡落在 agent
自己的卡片流,也就是使用者看得到的那條)。bridge 連不上/旗標沒開時工具回
錯誤文字而**不中斷 agent 的回合**。

## 15. Worker 可見層(`/app/v2/workers`,SUBPROCESS_HARNESS_DESIGN §2.4)

> 善彰 2026-08-12:「我看不出來你有在運作,你有子程序在執行嗎?我也看不到你有」
> —— AI 在幫你做事,你卻看不到它派了幾個工、做到哪。這是總控台的最後一塊。

**先分清楚兩個東西(§2.3 vs §2.4,它們並存、不是替代關係)**:

| | 子程序面板(§2.3) | 工人摺疊區(§2.4,本節) |
|---|---|---|
| 是什麼 | **獨立 session**(自己的 provider/model/審核設定) | session **內部**派出去的活 |
| 壽命 | 小時/天 | **秒/分** |
| 資料源 | `/app/v2/registry`(SQLite,家譜、TTL、配額、reaper) | `/app/v2/workers`(**純記憶體**) |
| 可操作 | 可點進去控制、派工、收編 | **唯讀**,只看狀態燈 |

工人**刻意不進 registry 的 session 表**:它們生滅以秒計、churn 極高,硬套
TTL/reaper/配額會把治理層淹掉,也會讓家譜樹爆炸(善彰一個 session 同時跑
3–7 隻手)。

**旗標**:`WORKERS=1`(**預設關**)。關著時這兩個端點回 **404**(不是 403),
且這一層完全沒有背景成本 —— 沒有計時器、沒有背景任務,過期是讀寫時惰性算的。

### 15.1 `GET /app/v2/workers?session=<v2 session id>`

`session` 就是 `/app/v2/sessions` 的 `id`(`claude_code:<name>` /
`codex:<thread_id>` / `hermes:<mid>`)。

```jsonc
{
  "session": "claude_code:Main",
  "workers": [{
    "worker_id": "toolu_01Krkzopp1BTuWnUMZJKRRfL",  // 穩定鍵,Pre/Post 同一顆
    "label": "查 worker 可見層（Explore）",            // 人話,可直接顯示
    "state": "running|done|failed",
    "parent_worker": null,        // 有值時**保證指得到本清單裡的另一筆 worker**;
                                  // 接不起來的線索一律放 meta,不放這裡
    "started_ts": 1786528221.77,
    "updated_ts": 1786528221.78,
    "meta": {"provider": "claude_code", "kind": "subagent",
             "subagent_type": "Explore",    // 純量鍵值,可能缺席
             "agent_id": "agent_abc",       // CC:這筆是在哪隻 subagent 裡派的
             "link": "cwd|provider"}        // CX:親子連線是怎麼推出來的
  }],
  "counts": {"running": 3, "done": 4, "failed": 0},
  "ttl_secs": 300.0              // 靜默期,app 可據此決定 poll 間隔
}
```

- 依 `started_ts` 由舊到新。`counts` 直接餵「⚙️ 3 個工人執行中」摺疊標題。
- **這是「現在在跑什麼」的即時視圖,不是稽核紀錄**:不落 DB、不寫磁碟,
  **bridge 重啟即空 —— 這是正確行為**,不是 bug,app 不要當錯誤處理。
- **靜默期自動過期**(`WORKER_TTL_SECS`,預設 300s):超過 TTL 沒有新回報的
  工人就從清單消失。這條規則專治「回報 done 的 hook 沒跑到」(CC 被 Ctrl-C、
  行程被殺)—— 沒有它,面板會永遠掛著一隻假的「執行中」。
- 每 session ring 上限 50,滿了汰換最舊的。
- 未知 session → `200` + 空清單(不是 404)。`session` 空字串 → `400`。

### 15.2 `POST /app/v2/workers/report`

```jsonc
{ "session": "claude_code:Main",   // 或 "cc_session_id": "<CC 的 session_id>"
  "worker_id": "toolu_01…",        // 必填,upsert 鍵
  "label": "審 PR #92 的 blocker",
  "state": "running|done|failed",
  "parent_worker": "agent_abc",    // 選填
  "meta": {"…": "純量"} }          // 選填
```
→ `{"ok": true, "session": "claude_code:Main", "worker": {…}}`

- **Upsert**:同 `worker_id` 再回報只更新 state/label/`updated_ts`,
  **`started_ts` 不變**(開工時間是它的身分)。已被靜默期收走的 id 再回報 =
  當新工人復活,拿到新的 `started_ts`。
- **不挑嘴**:認不得的 `state` 收斂成 `running` 而不是 `400`
  (`interrupted`/`stalled`/`timeout` → `failed`,`succeeded`/`complete` → `done`)。
  呼叫端是掛在工具呼叫上的 hook,不能因為打錯字就讓工人整個消失。
- `cc_session_id`:CC hook 只知道自己的 `session_id`(uuid),不知道 ccsess
  session 名。bridge 用**既有**的 name↔sid 對應表(`/ccsessions/_hook` 一直在
  維護)反查成 `claude_code:<name>`。反查不到時落到 `cc-sid:<uuid>`,
  **不丟掉這筆回報**(至少診斷時看得出「有工人但認不出是誰的」)。
- `meta` 只收純量、序列化上限 2KB(外部來源,不無條件信任)。
- **`parent_worker` 只在真的接得起來時才給**。CC 的巢狀 subagent 目前**接不
  起來**:`worker_id` 是 `tool_use_id`(`toolu_…`),而 hook payload 只給得出
  `agent_id`(另一個號碼空間),派出這隻 subagent 的那次呼叫的 `tool_use_id`
  不在 payload 裡。所以 `agent_id` 放 `meta.agent_id` 當診斷用,**不**冒充
  `parent_worker` —— 給個指不到的父節點,app 只會畫出一棵斷掉的樹。

### 15.3 三家 provider 的工人實際長什麼樣

| provider | 工人是什麼 | 怎麼來的 | `worker_id` |
|---|---|---|---|
| `claude_code` | `Agent` 工具派的 subagent | **CC hook 主動回報**(唯一可行的路:subagent 不在 tmux、無 pane、不進 ccsess 名單) | `toolu_…`(= `tool_use_id`) |
| `codex` | guardian / subagent child thread | bridge **唯讀投影** `thread/list?includeChildren=true` | `codex:<thread_id>` |
| `hermes` | dispatch `SUBSESSIONS` | bridge **唯讀投影**既有 SUBSESSIONS | `sub:<sid>` |

- **主清單不受影響**:`_codex_is_child_thread` 的過濾**原樣保留** —— child
  thread 依然不進 `/app/v2/sessions`(那個過濾當初是對的:390 筆 guardian
  thread 會把操作者的 lane 整個擠出畫面)。工人視圖是另開的一條唯讀投影。
- **CX 親子連線是啟發式,誠實標在 `meta.link`**:codex 有一張
  `thread_spawn_edges(parent_thread_id, child_thread_id)`,但實測(2026-08-12,
  `~/.codex/state_5.sqlite`)**是空的**,這個版本沒在寫。現況唯一可用訊號是
  **cwd 相等**(實測 subagent thread 與 parent 共用 cwd),故 `meta.link="cwd"`。
  哪天 provider 開始在 thread 記錄上給 `parentThreadId`,程式會自動改用它、
  `meta.link="provider"`。同 cwd 有多個 parent 時,子會同時出現在各自名下。
- **投影一律唯讀**:不改 SUBSESSIONS 的生命週期、不碰 codex thread。
- 投影也套靜默期:非 running 且超過 TTL 沒動靜的不列(否則 SUBSESSIONS 的
  永久記錄會把面板灌爆)。
- provider 掛掉 → 該來源為空清單,**不 500**。
- **CX 的 `thread/list` 有 3 秒程序級快取**(`WORKER_CODEX_CACHE_TTL`),而且
  **會分頁**去找 parent —— 只撈第一頁的話,操作者的 thread 會被前面那批
  guardian 擠掉,面板無聲回空。
- CX 時間戳讀不出來時**只信 `is_active`**:真的在跑才列,不會拿「現在」墊
  一個假的 `updated_ts` 讓它永遠留在面板上。
- Hermes 只有 status **明寫 `running`** 的才豁免靜默期(SUBSESSIONS 是永久
  記錄,壞掉的列不能永遠假裝在跑)。
- `session` 也吃 `delegation:<id>`(底下是一條 codex thread)。
- **OpenClaw 未接**(§2.4 表格列為「待查」):gateway 側 subagent 的形狀還沒
  確認,先不猜。`session` 是 openclaw id 時只會拿到 hook 回報的工人(目前為空)。

### 15.4 安裝 CC hook(**善彰手動貼**,腳本不會自己改你的設定)

CC 這一家是善彰真正要看的東西(「看見 Claude 自己派了幾隻手」),需要在
`~/.claude/settings.json` 的 `hooks` 加兩段。**這是加法** —— 你現有的
`Stop` / `UserPromptSubmit` 兩段原樣保留,只是多兩個 key:

```jsonc
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Agent|Task",
      "hooks": [{"type": "command", "timeout": 5,
                 "command": "/Users/you/apps/hermes-openwebui-bridge/scripts/worker-report-hook.py"}]
    }],
    "PostToolUse": [{
      "matcher": "Agent|Task",
      "hooks": [{"type": "command", "timeout": 5,
                 "command": "/Users/you/apps/hermes-openwebui-bridge/scripts/worker-report-hook.py"}]
    }],
    "PostToolUseFailure": [{
      "matcher": "Agent|Task",
      "hooks": [{"type": "command", "timeout": 5,
                 "command": "/Users/you/apps/hermes-openwebui-bridge/scripts/worker-report-hook.py"}]
    }]
  }
}
```

**token(擇一,建議走檔案)**:
```sh
# 建議:不依賴 env 繼承、也不必把 token 寫進 settings.json
install -m 600 /dev/null ~/.pocket/worker-hook.token
printf '%s' "$BRIDGE_TOKEN" > ~/.pocket/worker-hook.token
```
腳本讀 token 的順序:`POCKET_WORKER_TOKEN` → `BRIDGE_TOKEN` →
`~/.pocket/worker-hook.token`。(**不收 `~/.config/studio/token`** —— repo
CLAUDE.md 明寫那條已過時。)
其他 env:`POCKET_BRIDGE_URL`(預設 `http://127.0.0.1:8081`)、
`WORKER_HOOK_TIMEOUT`(預設 1.5 秒)、`POCKET_WORKER_SESSION`
(硬指定 session id,平常不用)。

**開旗標**:bridge 的 LaunchAgent plist `EnvironmentVariables` 加 `WORKERS=1`
(不開的話端點 404,hook 會安靜地什麼都不做 —— 這是預期狀態,不會噴錯)。

**為什麼 matcher 是 `Agent|Task`**:claude 2.1.207 的工具實測叫 **`Agent`**
(近三天真實 transcript 82 筆 `"name":"Agent"`、`Task` 0 筆);舊版叫 `Task`。
兩個都收,升降版都不會靜默失效。

**這支腳本絕不會擋住你的工具呼叫**:1.5 秒逾時、任何例外一律吞掉、
**永遠 exit 0**(exit 2 會讓 Claude Code 擋掉工具呼叫)、stdout 一個字都不印
(PreToolUse 的 stdout 會被當成控制 JSON 解析)。bridge 沒開、沒裝、連不上 —— 
最壞情況就是 Pocket 上看不到工人,你的 agent 照跑。

## 16. Console QR 登入(`/app/v1/console/login/*`,2026-10-30)

**所有消費者走同一條 Relay 路徑。** 官方 macOS / Ubuntu 安裝器預設
`POCKET_CONSOLE_ENTRY_MODE=relay`。不必在主機建立額外檔案，也不依使用者名稱、
方案名稱、固定網域或 IP 判斷。Relay 權益由 Pocket ID 控制；每台主機使用自己
獲配的 grant，已有 Relay 或 Pocket ID 登錄的既有主機亦採用這個模式。
當 Console 已啟用、Relay 已開、supervisor 回報 connected，且當前連線主機名與
grant 一致時，`GET /app/v1/console/boot` 才提供 `relay_qr_url`；否則為 null。
舊欄位 `url/urls` 仍只代表內網，手機不把短碼放進 Relay 網址。

Relay 模式不會退回其他公網入口：瀏覽器只能在該 grant 的 HTTPS Host 建立請求、
輪詢、兌換或使用授權。手機使用既有配對連線明示批准，瀏覽器不能批准。
停用 Relay 或 supervisor 偵測失去連線後，既有 Relay token 不准再讀資料；
不影響本機／tailnet 登入。Apple 取消自動續訂不等於有效期已到。
驗收至少包含兩個獨立帳號／主機各自登入、交叉拒絕、無 Relay 的內網直連、登出及停用。
單機先測是分批驗收，不是為單一機主改授權規則。

讓人在**任意一台電腦**上開桌面控制台:那台電腦開 `/console` → 不是可信來源
→ bridge 顯示 QR → **已配對的手機掃碼批准** → 瀏覽器才拿到憑證。

完整設計與威脅模型文件考量商業化不公開列出攻擊面細節,已移至非公開筆記;
這一節只釘 app 要用的三支端點契約,不影響這三支本身的行為。

### 16.0 旗標與未啟用時的表現

| 旗標 | 預設 | 關掉時 |
|---|---|---|
| `POCKET_CONSOLE_ENABLED` | 官方安裝器 `1`、獨立 runtime `0` | 關閉時 Console 與登入端點均 **404** |
| `POCKET_CONSOLE_ENTRY_MODE` | 官方安裝器 `relay` | 自架者可明確選 `custom` 使用自己的入口；不會自動把自架網址當作付費 Relay |
| `POCKET_CONSOLE_QR_LOGIN`、`POCKET_CONSOLE_WEB` | `0` | 自訂入口的舊開關；不能替 Relay 模式開後門 |

**404 而不是 403/501**:app 探不到就當這台 bridge 不支援,安靜地不顯示 ——
舊 bridge 的使用者不該看到錯誤。這與 `/capabilities` 的降級慣例一致。

### 16.1 `GET /app/v1/console/login/requests/{rid}`

The shared Console entry is additive: the browser starts on `https://console.pocketone.app`;
chat and approval still belong to the granted Relay. The shared QR carries only
`pocket://console-entry?rid=<32 hex>`. A paired phone calls its own bridge's
`POST /app/v1/console/entry/select` with that rid; bridge authenticates to its enrolled
Pocket ID service and returns a challenge (never a browser token). After the browser
posts its verifier and rid to the selected Relay's `/console/entry/start`, the same
phone polls `POST /app/v1/console/entry/lookup` with rid and challenge. 202 means
waiting, 200 returns a **host-local** rid. The phone shows the normal approval
sheet for that local rid. The browser keeps its own HttpOnly sid; scanning alone,
resubmitting the form, or possessing the public rid does not grant a session.
Existing host bookmarks and `console-login` QRs continue to work unchanged.

掃到 QR 之後先讀這支,把「誰在請求」顯示給人看。**需要 `Authorization: Bearer`**
(一般 device token)。

```json
{ "rid": "…32 hex…", "state": "pending",
  "ip_label": "公網 203.0.113.9", "user_agent": "Mozilla/5.0 …",
  "created_at": 1762000000, "expires_in": 118 }
```

- `state`:`pending` | `approved` | `denied` | `consumed`。**只有 `pending`
  能批准**,其餘按下去會拿到 409。
- `ip_label`:只講**網路種類**(公網／區網／tailnet／本機)＋ IP 本身。
  刻意**不做地理推測** —— 那需要外部查詢,等於把訪客 IP 送給第三方。
- `user_agent`:**請求方自稱的原文**。app 端不要美化成「Chrome on macOS」,
  這是攻擊者可以任意填的欄位,美化它會讓人更容易相信它。
- `expires_in`:秒。QR 只活 **120 秒**;app 應該自己倒數而不是每秒重打這支。
- **404 = 這個 QR 不是這台桌面的**(rid 是本機隨機值)或已過期。app 據此顯示
  「這個 QR 不是這台桌面的」。

### 16.2 `POST .../{rid}/approve`、`POST .../{rid}/deny`

無 body。回 `{"ok": true, "state": "approved"|"denied"}`。

| 狀態碼 | 意思 | app 該怎麼做 |
|---|---|---|
| 200 | 成立 | 顯示結果,收掉批准頁 |
| 404 | 不是這台桌面的 / 已過期 | 請人在電腦上重新整理登入頁再掃 |
| 409 | 這筆已被決定過(重放防護) | 顯示「已處理過」,**不要重試** |
| 503 | 鑄碼被節流 | **可以重按** —— bridge 刻意沒改 state 就是為了這個 |

`deny` 存在的理由不只是禮貌:讓瀏覽器**立刻**看到被拒(而不是等 120 秒過期),
人才知道自己剛剛可能被釣,而不是以為「網路怪怪的再掃一次」。

### 16.3 QR payload —— **位址不在裡面,這是安全前提**

```
pocket://console-login?rid=<32 hex>
```

**只有 rid。** app 必須用它**自己既有的連線**呼叫上面三支,絕不可以被 QR
指去另一個主機 —— 那正好是釣魚要的。所以:

- app 端的解析器**不得**「順便支援」`host=`／`scheme=`／`port=`。
  (`ConsoleLoginPayload` 的型別裡只有 `rid` 一個欄位,是結構性保證;
  `Pocket iOSTests/ConsoleLoginPayloadTests.swift` 有一條測試專門釘這件事。)
- 這條連結也會走 `.onOpenURL`:桌面沒裝 `qrcode` 套件時那頁只會顯示一條
  可複製的連結(bridge 側刻意的保底),人唯一的辦法就是傳到手機點開。

### 16.4 **掃到不等於批准**

批准頁必須顯示 §16.1 的請求方資訊,**並等人明示點擊**。掃到就自動批准 =
把釣魚變成一鍵得手。這是規格 §8.1 的第一道防線,不是 UI 偏好。

### 16.5 批准之後那台瀏覽器能做什麼(能力降級)

它拿到的是 **scoped device token**(`scope = "console-remote"`),不是一般的
device token:

- **有有效期**(一般 device token 沒有)—— 到期即 401。
- **路徑白名單**:只有 session／指令／核准／health／檔案那幾條走得通,
  其餘 403。白名單而不是黑名單 —— 新增端點時黑名單會**安靜地**放行。
- **檔案面唯讀且限定目錄**:`POCKET_CONSOLE_REMOTE_FILE_ROOTS`(`:` 分隔)。
  **沒設就等於一個都不給。** 範圍外回 **404 而不是 403**,不讓遠端瀏覽器
  藉錯誤碼探測檔案存不存在。
- `/pair/devices` 上看得到(`is_remote_console: true` ＋ `scope` ＋ `expires`),
  機主可以隨時撤銷。app 的裝置清單應該把它標成「遠端瀏覽器」而不是一台裝置。

## 17. Sessions Roster(`/app/v2/sessions/roster`,2026-11-16)

> 動機(grok-build steal #3):列表頁/FleetView 要的是「一眼看懂每條
> session 在幹嘛」,不是整包 provider meta。既有 `/app/v2/sessions`
> **一個 byte 不動**(相容);roster 是疊加的瘦 facade,治「列表預覽怪/
> 未讀不亮」—— activity 用六態說真話(`needs_input`/`dead` 是一級狀態),
> 摘要由 bridge 生成,app 不再自己猜。

### 17.1 冷載

```
GET /app/v2/sessions/roster
→ {"sessions": [RosterRow…], "degraded_providers": ["codex", …]}
```

RosterRow:

```jsonc
{
  "id": "claude_code:Ops",            // 同 §4.1 全寫 session id
  "provider": "claude_code|cc2|codex|hermes|gemini|openclaw",
  "title": "Ops",
  "activity": "working|idle|needs_input|dormant|completed|dead",
  "last_turn_summary": "部署腳本改好了…",   // 規則式 ≤12 字;可為 ""
  "updated_ts": 1795000000.0           // epoch;從無活動 → null
}
```

`activity` 六態(照 grok RosterActivity)與導出規則:

| activity | 導出 | app 端建議 |
| --- | --- | --- |
| `needs_input` | `status==waiting_approval` 或 `meta.approval` 存在(與 pending approvals 同步導出,**優先序最高** —— 在等你的事比什麼都搶眼) | 醒目徽章,點進去可答 |
| `working` | `status ∈ running/stalled/dispatching`(stalled 照 §4.1a 比照 running,「疑似卡住」不誤報成死) | 轉圈 |
| `dead` | `status ∈ failed/error` 或 `meta.degraded`(persona crash-loop/連續 stall)—— **runner 死也要顯示得出來**,不是從列表消失 | 紅字 |
| `completed` | `status ∈ done/completed` | 一般樣式 |
| `idle` | 無上述,且 `updated_ts` 距今 < `POCKET_ROSTER_DORMANT_SECS`(預設 24h) | 一般樣式 |
| `dormant` | 無上述且久無活動(或從無活動) | 淡化/摺疊 |

`last_turn_summary`:素材 = 該 session 最後一筆持久里程碑
(`card.milestone`/`message.upsert`)的文字。兩段式(S3,2026-11-17 起 ——
原本這裡寫著「TODO:LLM 生成」,已做掉):

1. **LLM 版已在快取** → 用它。快取在 `POCKET_TURN_SUMMARY_CACHE`
   (預設 `~/.pocket/turn-summaries.json`),鍵 = session + **素材指紋**
   (素材沒變就不重算,重算是純燒錢;素材變了舊摘要立刻失效,列表不會
   顯示上一回合的事)。
2. 沒有 → **立刻**回規則式(壓空白截斷 ≤12 字,= 舊行為、零延遲),
   同時丟背景任務生成;下一輪輪詢(§17.2 的 `POCKET_ROSTER_POLL_SECS`,
   預設 3s)自動換成 LLM 版。

**wire 欄位一個 byte 不變,app 零改動升級。** 兩個刻意的設計:

- 讀路徑**永不等 LLM** —— roster 是 3 秒心跳的 SSE 來源,在讀路徑 await
  網路會讓整條訂閱抖。
- 同一筆素材**只嘗試一次**(成功或失敗都不重試)—— 否則沒設定 textgen 的
  機器會每 3 秒丟一個註定失敗的任務 + 寫一行用量 log,直接洗版。
  改了 §18 的設定會清掉這個記錄(不然使用者設好 key 還要等素材變才看得到
  效果,體感是「設了沒用」)。

長度上限是**後置硬閘**:prompt 要求 ≤12 字只是請求,小模型不一定聽,
所以超了就截斷加 `…` —— 與規則式同一個形狀。生成走 §18 的管線;§18 沒設定
= 永遠停在規則式(**功能不會消失**)。

### 17.2 差分訂閱

```
GET /app/v2/sessions/roster/events        (SSE)
data: {"ts": …, "type": "sessions.changed",
       "data": {"upserted": [RosterRow…], "removed": ["codex:…", …]}}
```

- **transient 事件**(B1 三分類):信封無 `seq`、不落 event_log —— roster
  任何時刻可由 §17.1 全量重建,持久游標在這裡沒有意義。
- 連上先推一包全量(`upserted` = 全部列):冷載與重連走同一條「以 id
  upsert」套用路徑,client 不必分支。
- 之後每 `POCKET_ROSTER_POLL_SECS`(預設 3s)比對一次,有差才推;
  安靜期間每 `SSE_KEEPALIVE_SECS` 一行 `: keepalive` 註解。
- 長時間零變化(30 分鐘)bridge 主動斷線(半死 client 不佔 task,同既有
  follow 流);client 重連即重拿全量。

## 18. Webhook automations(入站簽章觸發 agent,2026-11-17)

> 讓外部事件(GitHub PR/CI、告警…)自動開一條 agent 工作。設計照 t3code 的
> webhook automations 搬。實作在 `webhooks.py`(純邏輯 + SQLite 落庫),路由與
> 「真的開 session」在 `bridge.py`。完整操作指南見 `docs/WEBHOOKS.md`。

### 18.1 兩個面:入站(無 bearer)與管理(需認證)

| 面 | 端點 | 認證 |
|----|------|------|
| 入站 | `POST /webhook/{hook_id}` | **無 bearer**,靠每 hook 獨立 secret 的 HMAC-SHA256 簽章 |
| 管理 | `POST/GET/PATCH/DELETE /app/v1/webhooks[/{id}]` | `Authorization: Bearer <bridge token>`(§1) |
| 日誌 | `GET /app/v1/webhooks/{id}/deliveries` | 同上 |

入站不可能帶我們的 device token(送方是 GitHub/CI),所以**唯一**的身分證明
是簽章。管理面沿用既有 bearer。

### 18.2 建立 hook

`POST /app/v1/webhooks`,body:

```jsonc
{
  "name": "github-pr",
  "provider": "codex",            // codex | cc2
  "cwd": "/Users/you/apps/repo",  // session 的工作目錄
  "prompt_template": "審一下這個 PR:{{pull_request.title}}\n{{pull_request.html_url}}",
  "filters": [                     // 全部皆過(AND)才觸發;空陣列=無條件
    {"op": "equals", "path": "action", "value": "opened"},
    {"op": "is-one-of", "path": "pull_request.base.ref", "value": ["main", "dev"]}
  ],
  "coalesce": {"key": "{{repository.full_name}}", "window_seconds": 60},  // 可省
  "dedup_source": "x-github-delivery",  // 去重用的 header;省略則用 payload 雜湊
  "rate_per_minute": 60,
  "enabled": true
}
```

回應**唯一一次**帶明文 `secret` 與 `inbound_url`/`signature_header`:

```jsonc
{ "id": "wh_ab12…", "secret": "…once-only…",
  "inbound_url": "https://…/webhook/wh_ab12…",
  "signature_header": "x-hub-signature-256", "has_secret": true, … }
```

之後任何 GET/LIST 都 **redact**(只回 `has_secret: true`)。要換 secret:
`PATCH …/{id}` 帶 `{"rotate_secret": true}` —— 舊 secret **當場失效**,新明文也
只在該次回應回一次。

### 18.3 入站處理順序(每一步都落庫)

`存在/啟用 → 簽章 → 去重 → 速率 → filters → coalesce → 觸發`。各結果對應的
HTTP 碼:

| 結果 | 碼 | `action` | 說明 |
|------|----|----------|------|
| 觸發 | 200 | `triggered` | 回 `session_id` |
| 已歸併 | 202 | `coalesced` | 進 coalesce 視窗,視窗到期才開一條 |
| 重複 | 200 | `duplicate` | delivery id / payload 雜湊 7 天內見過 |
| filter 沒過 | 200 | `filtered` | `reason` 標哪條 filter |
| 簽章缺/錯 | 401 | `missing_signature` / `bad_signature` | **仍記一筆** deliveries |
| 超速 | 429 | `rate_limited` | 每 hook 每分鐘上限;不佔去重格,重投可過 |
| 停用/不存在 | 404 | `disabled` / `not_found` | 不洩漏細節 |
| 開 session 失敗 | 502 | `trigger_error` | delivery 記 `error` |

### 18.4 Filters(JSON path)

- `path` 走 dot 記法;`[]` 通配展開陣列,**任一**命中即算該條過:
  `commits[].author.name`。
- `op`:`equals`(等值)、`is-one-of`(`value` 為陣列,屬於即過)、
  `exists`(路徑解得出值即過;`value: false` 反向斷言「不存在」)。

### 18.5 去重與 coalesce(皆落庫,重啟仍有效)

- **去重**:鍵優先取 `dedup_source` 指定的 header(如 `x-github-delivery`),
  沒有就用 payload SHA-256。保留 7 天。
- **coalesce**:同 `key`(可用 `{{path}}`)的連續投遞併進一個視窗,視窗
  `window_seconds`(10~3600)到期**只開一條** session,用併攏後最新的 payload。
  視窗在庫裡,bridge 重啟後由背景 reaper 接著燒。

### 18.6 外部資料硬化(安全關鍵)

入站 payload 的**每個欄位值都是不可信輸入**。進 prompt 前:

1. 一律包 `<webhook-data>…</webhook-data>` 標記(模板本身可信,不包);
2. 在 `$`、`@`、**行首** `/` 後插零寬字元,使 `$skill`、`@file`、
   `/command`、`//command` 這類會被 agent runtime 特別解讀的前綴**當場失效**;
3. 欄位值裡夾帶的 `</webhook-data>` 收尾標籤會被打斷,擋 tag breakout。

即使攻擊者能發出通過簽章的 payload,也無法用欄位內容把 agent 導去跑別的
技能、讀別的檔或下控制指令。

### 18.7 投遞日誌

`GET /app/v1/webhooks/{id}/deliveries?limit=100`:

```jsonc
{ "ok": true, "hook_id": "wh_…", "deliveries": [
  { "id": "dl_…", "received_at": 1800000000.0, "source": "140.82.x.x",
    "delivery_key": "id:…", "triggered": true, "reason": "triggered",
    "session_id": "codex:…", "error": "" }, … ] }
```

排查「為什麼沒開 agent」看這裡:簽錯、被去重、filter 沒過、被歸併、超速,
每一種都有 `reason`。保留 7 天。

## 19. 小任務文字生成(`/app/v1/textgen/config`,S3,2026-11-17)

> 動機:標題、回合摘要、commit 訊息、逐字稿清理這些雜務,**不該燒 coding
> agent 的訂閱額度**(CC/CX 的額度撞到就整條線停擺,而摘要一點都不值得
> 佔用它),也不該硬綁本機 Ollama(大多數機器沒有 → 功能靜默缺席)。
> 這一節給它一條**便宜 model API** 的路。

### 19.1 lane 順序

    便宜 API(設定了才有) → 本機 Ollama → 放棄

第一條能用的就用;**它壞掉才往下掉**(上游 500 時「安靜地沒摘要」比
「多花 200ms 用本機生一個」糟得多)。全掉光 → 呼叫端退回原本行為
(規則式截斷 / 原始逐字稿 / 抽取式摘要)。

**靜默失敗紅線:功能不准因為沒設定 textgen 而消失。** 所以
`configured=false` 時 app **不要**顯示「未啟用」——顯示的是「目前走本機/
規則式」。回應裡的 `fallback_note` 就是給 UI 直接用的那句話。

### 19.2 設定

```
GET /app/v1/textgen/config
→ {"configured": false,
   "base_url": "", "base_url_source": "none",
   "model": "",    "model_source": "none",
   "key_set": false, "key_source": "none",
   "local_enabled": true,
   "suggested_models": [{"model": …, "label": …, "hint": …}, …],
   "fallback_note": "未設定時各呼叫點退回原本行為(規則式/agent),功能不會消失。"}

PUT /app/v1/textgen/config   {"base_url"?, "api_key"?, "model"?}
→ {"ok": true, …同上…}
```

- **端點是 OpenAI 相容的 `POST {base_url}/chat/completions`** —— OpenAI /
  Anthropic 的 OpenAI 相容層 / OpenRouter / Ollama / LM Studio 都講同一套,
  所以一個客戶端全涵蓋,不必為每家寫 adapter。
- `configured` = **`base_url` 與 `model` 兩個都有**。`api_key` 刻意不列入:
  Ollama / LM Studio 不需要 key,設成必填就是把一個本來能用的路徑關掉
  (同 opencode「沒有登入牆」那條)。key 為空時**不送** `Authorization` 標頭。
- **`api_key` 永遠不回傳**,只回 `key_set`(同 openclaw 的 `token_set`)。
- env 優先(`POCKET_TEXTGEN_BASE_URL` / `_KEY` / `_MODEL`),三個欄位的
  `*_source ∈ {env,file,none}` **各自獨立判定** —— 只設 env model 不會讓
  檔裡的 key 消失。`source == "env"` 時 app 應把該欄畫成唯讀
  (否則就是「我改了但沒反應」)。
- PUT 是**合併寫入**(只帶 `model` 不會洗掉 `api_key`);傳**空字串**=
  明確清掉該欄(使用者要有辦法關掉);三個都不帶 → `400 EMPTY`。
- `model` **沒有預設值**:猜一個端點不認得的 id 只會換來 404(靜默失敗的
  親戚)。`suggested_models` 是給設定面板 picker 的**建議清單**,選型依據
  見 `pid-subsync/docs/LLM_PRICING_REFERENCE_20261117.md` 的便宜檔
  (Haiku / gpt-6-luna 級);價格會變,那份文件才是真相來源。
- `POCKET_TEXTGEN_LOCAL=0` 關掉本機 lane(例如不想讓雜務去搶顯卡)。
  本機 lane 連不上會**冷卻** `POCKET_TEXTGEN_LOCAL_COOLDOWN`(預設 600s)
  —— 沒裝 Ollama 的機器不會每回合都撞一次 connection refused;但**絕不
  永久放棄**,使用者中途把 Ollama 開起來會自己恢復。

### 19.3 任務分類與呼叫點

`task` 是對帳用的穩定分類鍵。目前已接的呼叫點:

| task | 呼叫點 | 沒設定時的退路 |
| --- | --- | --- |
| `roster.summary` | §17 的 `last_turn_summary` | 規則式截斷(≤12 字) |
| `transcript.cleanup` | 會議錄音清稿 | 本機 Ollama → 原始逐字稿 |
| `agent_context.summary` | `/app/v2/agent_context` 的交接簡報 | 本機 Ollama → 抽取式摘要 |

`session.title` / `commit.message` 的 prompt 包裝已在 `text_gen.py` 備好,
但**刻意還沒接呼叫點**:現有的標題來源(`_cc_session_head` 的 jsonl 掃描、
gemini 的「第一句話截 48 字」)都是**同步且有快取**的,改成非同步 LLM 會
動到列表頁的渲染路徑,風險與這一刀不同量級 —— 留下一刀,連同它自己的
列表回歸測試一起做。宣告了卻不能落地就是死鈕,所以這裡誠實標註未接。

**review / coding 類絕不走這條** —— 那本來就該走 coding agent。

### 19.4 成本留痕

每次呼叫(**含失敗**)記一筆:

- 結構化 log `[bridge-event] {"event":"textgen_usage", …}`:`task`、`ok`、
  `reason`、`model`、`elapsed_ms`、`lanes`(實際試過哪幾條)、`usage_*`。
- `session` 有值時**另外**落一筆 `event_log` 事件 `textgen.usage`
  (B1 分類 = **persist-only**:進 event_log 蓋 seq 供審計,但**不推任何
  活流** —— 使用者不需要看到「剛剛花了 37 個 token」,對帳的人需要)。
  `session` 為空的全域雜務只留 log:為沒有歸屬的呼叫在 event_log 開一個
  假 session 鍵,之後查帳的人會被那個不存在的 session 騙一次。

端點不回 `usage` 時(很多本機 server 不回)記 `usage_reported=false`,
**不寫 0** —— 寫 0 等於謊報「這次花了 0」。拿到空字串但連線成功的情況
**照樣記用量**(錢真的花了)。

## 19. 額度耗盡自動切 provider(`/app/v1/failover/config`,S4,2026-11-17)

> 動機:E3 早就認得出「撞限額」(`status.phase == "usage_limited"` +
> `usage_limit_reset_at`),但**到今天沒有任何人消費那個訊號** —— 使用者
> 看到「CC 額度用完,X 點重置」,然後只能自己去開另一家。我們接了八個
> provider,撞牆時卻還是手動換,**接八家的紅利沒拿到**。這一節是那顆缺掉
> 的消費者。

### 19.1 政策設定

```
GET /app/v1/failover/config
→ {"mode": "suggest", "mode_source": "default",
   "order": ["cc2","codex","gemini","grok","opencode"], "order_source": "default",
   "modes": ["suggest","auto"],
   "default_order": [...],
   "handoff_targets": ["codex","cc2"],
   "available": ["cc2","codex", …],
   "availability": {"codex": {"installed": true, "logged_in": null}, …}}

PUT /app/v1/failover/config   {"mode"?: "suggest"|"auto", "order"?: [...] | "a,b"}
→ {"ok": true, …}
```

- **兩種模式**:
  - `suggest`(**預設**)—— 出一張有按鈕的卡片問使用者,不自己切。
  - `auto` —— 直接切,**卡片明示已經換成誰、為什麼、失去什麼**。
- 預設是 `suggest` 不是 `auto`,照 house precedent(`orchestrator.py`:
  *failover requires explicit approval and checkpoint*)。
- 讀取端寬鬆(`mode` 認不出來一律退 `suggest` —— fail-safe 的方向是
  「寧可多問一次」);**寫入端嚴格**(打錯字 → `400 FAILOVER_BAD_MODE`,
  默默改掉會讓使用者以為自己開了 auto)。
- env 優先:`POCKET_FAILOVER_MODE` / `POCKET_FAILOVER_ORDER`。
- `order` 的語意是「我比較想用誰」,不是「誰比較強」。接受 list 或
  逗號字串;去重;**不驗 provider 名**(亂打的會在選擇時被 caps 過濾掉,
  比在設定端點拒絕整份順序好 —— 某家暫時沒裝不該讓設定存不進去)。
- `availability` / `available` 一起回,因為「我順序裡那家為什麼沒被選」
  是這個功能最常被問的問題,設定面板要能**當場**答得出來。

### 19.2 預覽與執行

```
GET  /app/v2/threads/{thread_id}/failover      ← 唯讀,不切任何東西
POST /app/v2/threads/{thread_id}/failover      {"to_provider"?, "force"?}
```

GET 回:

```jsonc
{"ok": true,
 "thread_id": "…", "current": "claude_code",
 "session_id": "claude_code:Ops",
 "limited": true,
 "mode": "suggest",
 "action": "suggest",            // none | suggest | auto | blocked
 "reset_at": 1795000000.0,       // 解不出來 = null,不編一個時間
 "plan": {"provider": "codex", "reason": "ok",
          "order": ["cc2","codex"],
          "rejected": [{"provider":"cc2","why":"unavailable"}],
          "caps_gap": ["改名","封存"]},
 "handoff_targets": ["codex","cc2"],
 "card": { …§6 卡片 body… }}
```

`action` 四態:`none`(沒撞限額)/ `suggest` / `auto` / `blocked`
(撞了但沒有可用備援 —— **也要出卡片**,沒有它使用者會以為 failover 壞了
或根本沒裝)。

選擇的過濾順序,每一層都留在 `rejected` 裡:

| `why` | 意思 |
| --- | --- |
| `same_provider` | 就是目前這家 |
| `no_caps_declared` | 沒在 `_PROVIDER_CAPS` 宣告能力 —— 宣告是入場券 |
| `no_handoff_target` | 還沒有交棒落地實作(見 `handoff_targets`) |
| `unavailable` | 未安裝,或**明確**未登入 |

`logged_in == null`(登入狀態還在探,冷啟要 10s+)**當成可用** —— 當成
沒登入會讓剛開機的機器一律沒有備援;真的沒登入的話切過去時 spawn 會失敗
並明確回報,比「安靜地不給備援」好查。opencode 沒有登入牆,一律 `true`
(報 false 會憑空造出一道不存在的登入牆)。

### 19.3 切換語義

**切換 = 新 session + 帶上下文交棒,不是假裝同一條對話。** 直接復用
§ContextTransfer v1(`POST /app/v2/threads/{id}/transfer`)的 milestone
摘要管線:萃取來源的持久里程碑 → 組交接 prompt → 在目標 provider 開新
session(首訊 = 交接 prompt)→ rebind(舊綁定標 `detached`,不是 `dead`
—— 來源原生會話還在,這是刻意交棒)→ 落 `thread.transferred`。

回應 = transfer 的完整結果**外加**:

```jsonc
{"failover": {"from": "claude_code", "to": "codex", "mode": "suggest",
              "limited": true, "forced": false,
              "reset_at": …, "caps_gap": ["改名","封存"]},
 "card": { …auto 模式的明示卡片 body… }}
```

錯誤面:
- 沒撞限額且 `force != true` → `409 FAILOVER_NOT_LIMITED`。這支是
  **「額度耗盡」的出口,不是通用搬家鍵**(通用搬家請用 `/transfer`,
  語意清楚)。
- 算不出目標 → `409 FAILOVER_NO_TARGET`(理由在 GET 的 `rejected`)。
- `to_provider` 不在 `handoff_targets` → `400 FAILOVER_BAD_PROVIDER`。
- 目標建立失敗 / rebind 撞綁定 → 原封不動由 transfer 回報(502 / 409),
  **不在這裡包第二層** —— 兩層錯誤碼會把「為什麼切失敗」蓋掉。

### 19.4 卡片(走既有 `kind: "choices"`,§6)

不需要新端點、也不需要改 app 才看得到;Telegram 上自動退化成純文字。

| `action` | 卡片 | `options` |
| --- | --- | --- |
| `suggest` | 「額度用完 —— 可以切到 X 繼續」+ 要打哪支端點 | **空** |
| `auto` | 「已自動切到 X(原因:Y 額度用完)」+ 新 session id | **空**(已成事實) |
| `blocked` | 「額度用完,且沒有可用備援」+ 考慮過誰、為什麼都不行 | 空 |

**三種卡片的 `options` 都是空的 —— 這是刻意的。** 第一版把 suggest 做成
兩顆 `choices` 的 `send` 按鈕(`/failover codex` / `/failover wait`),查下去
發現 **bridge 沒有任何地方在解析斜線指令**:按下去只會把那串文字原封不動
送進一條**正卡在限額上**的 session,然後什麼都不會發生 —— 標準的死鈕。

所以分工是:**卡片負責告知,控制面在 §19.2 的端點上。** app 要畫按鈕就用
預覽回應裡結構化的 `plan`(`provider` / `caps_gap` / `rejected`),按下去打
`POST /app/v2/threads/{id}/failover`。按鈕的 wiring 歸 app 那一波。
哪天真的有了指令解析層,再把 `options` 補回來 —— **連同它的端到端測試**
(`tests/test_failover_s4.py` 有一條守門測試會在有人加回 `send:` 按鈕時變紅)。

**誠實邊界**:`caps_gap` 把「切過去會失去什麼」寫進卡片 `detail`
(中斷回合 / 排隊送訊息 / 審批卡片 / 改名 / 封存 / 歷史 / 讀寫設定 /
即時串流降為輪詢)。只報「有 → 沒有」的方向 —— 目標多出來的能力不報,
卡片講的是邊界不是推銷。

**`auto` 不得靜默**:自動切一定伴隨那張明示卡片。安靜地換掉使用者的
provider 會讓他以為還在 CC 上,然後困惑為什麼風格變了 —— 那正是這個 repo
最忌諱的那種病。

## 附註 A:pocket-id 心跳 `notices` 下行(backup orphan 提醒,2026-11-06)

> 這一節描述的是 **bridge ↔ pocket-id** 的機器間契約(不是 app ↔ bridge),
> 放在這裡是因為消費端(bridge)與最終的使用者可見行為(本地推播)都在
> 本 repo;pocket-id 端的產生邏輯由另一棒實作,照本節的形狀對齊。

背景:pocket-id 的 reaper(`/v1/admin/cloud/reap`,每小時 timer)會掃出
**備份孤兒** —— backup 家族訂閱還在扣款,但 host 權益已到期/名下機器已
回收(帳號級清單 `backup_orphans`)。使用者在為一台不存在的機器付月費,
必須主動告知。但推播通道在 bridge(APNs 金鑰與裝置 token 在各 host 的
機主 Mac / lobster 上),pocket-id 自己推不了。

通道:沿用 `POST /v1/hosts/heartbeat` **回應**的既有下行線(同
`update_ref` 的「我們講、它聽」模式 —— 不開新常駐連線、不加狀態機)。

```jsonc
// 心跳回應(host 所屬帳號有備份孤兒時才帶 notices)
{
  "ok": true,
  "notices": [
    { "type": "backup_orphan", "product_id": "com.pocketagent.backup.monthly" }
  ]
}
```

規則:

- **沒有 `notices` 欄位 = 無事**。舊版 pocket-id/bridge 因此雙向相容。
- pocket-id 端**每次心跳都可以重複帶**(孤兒還在就帶),不需要 server 側
  去重/簽收 —— 去重是 bridge 的本地責任:同一則 notice(`type:product_id`
  為鍵)**24h 內只推一次**,記在 `~/.pocket/heartbeat-notices.json`
  (env 把手 `POCKET_HB_NOTICE_STATE`),重啟不失憶。
- bridge 收到 `type: "backup_orphan"` → 轉成本地推播(`push_notify` 既有
  管線)給所有已配對裝置:「備份訂閱仍在扣款但主機已到期,記得取消」。
  payload `data.pocket = {kind: "backup_orphan", productId}`,
  `thread_id = "account-notices"`。
- 不認識的 `type`:跳過 + `pocket_id_notice_unknown` 事件留痕(向前相容,
  新型別由升級後的 bridge 接手)。
- notices 消費失敗(APNs 掛了等)**不得**讓心跳本身看起來失敗 ——
  否則 `_pocket_id_loop` 會對健康的 pocket-id 指數退避。
#### Console history paging

`GET /app/v2/sessions/{session_id}/cards?history_page=true&limit=24` returns
the usual snapshot plus `older_cursor` (an opaque card ID, or null). Fetch an
older page with `before_id=<encoded cursor>`; do not combine this with
`before_seq`. An evicted anchor returns 409. Paging only covers history
retained by the card store. Never advance the live SSE cursor from an older
page. The default snapshot and existing sequence-based clients are unchanged.
# Desktop Console login

The paired mobile client posts `{}` to `/app/v1/console/entry/code` when Desktop Console opens. The response contains a six-digit string, a private poll handle and `expires_in: 900`. Poll the same endpoint with the handle. A returned browser rid goes through the existing select, lookup and explicit approve/deny flow. The code cannot mint a device or a login by itself.

The public relay login and scoped token have a fixed maximum lifetime of 24 hours. Visiting Console does not extend it. A host-only Secure HttpOnly cookie on `/console/session` lets the same browser recover **only its existing scoped token** via a same-origin POST to `/console/session/resume`; expiration and device revocation remain server-enforced.

### A-2:`subscription_expiring` 到期倒數(2026-11-06)

同一條下行線的第二種 notice。訂閱到期前 **60 天**起,pocket-id 每次心跳
都帶這一則,讓 app 的訂閱管理顯示倒數;但**只有拍板的節奏日才推播**。

```jsonc
{
  "ok": true,
  "notices": [
    { "type": "subscription_expiring",
      "days_left": 7,              // 無條件進位的剩餘天數(>0)
      "expires_ts": 1800000000.0,  // 選填
      "push": true }               // 只有 60/30/14/7/3/1 這幾天為 true
  ]
}
```

規則(與 backup_orphan 的差異都在這):

- **`push` 的決策權在伺服器,bridge 不自己算日子。** 兩邊各算一次遲早
  不同步;bridge 只問「這則要不要推」。`push=false` 照樣會每 60s 送來
  (app 要倒數),bridge **不推播、也不佔 dedup 格**。
- **dedup 鍵是 `subscription_expiring:{days_left}`,必須含天數。**
  沿用 `type:product_id` 那種不含天數的鍵 + 固定 24h 窗,會讓整段倒數
  **只推出第一則**(T-60 推了,T-30/T-14/T-7 全被當重複吃掉)。含天數後:
  同一天內重複心跳仍只推一次,換節奏日就是新的一則。
  (回歸測試:`tests/test_heartbeat_notices.py` 的
  `test_each_cadence_day_pushes_separately`)
- `days_left` 解不出整數 / `<= 0` → 不推(已到期交給 reaper 的冬眠流程)。
- payload `data.pocket = {kind: "subscription_expiring", daysLeft, expiresTs?}`,
  `thread_id` 同樣是 `"account-notices"`。
