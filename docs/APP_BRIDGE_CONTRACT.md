# PocketAgent / Studio Bridge Contract

This document is the source of truth for PocketAgent's app-facing bridge API.
PocketAgent should depend on these endpoints instead of Hermes internals.

## Authentication

- App-facing endpoints require `Authorization: Bearer <bridge token>`.
- Tokens are configured in the LaunchAgent and PocketAgent settings. Do not
  commit real tokens to git.
- `/health` may be used as a lightweight reachability check.

## Stable Endpoints

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
bearer token and an account session; the phone calls `pair/claim` with the code
and its own account session. The Apple user id on both sides must match.

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

### `GET /app/v2/sessions`

Aggregates Hermes personas, durable delegations, Claude Code sessions, and Codex
threads into one control-plane list. Delegations are first-class rows and include
`meta.work_order` plus `meta.takeover`.

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

## Smoke Test Expectations

- `/health` returns `ok: true`.
- `/capabilities` includes all required features.
- `/app/v1/sessions` returns all four personas.
- `/app/v1/delegations` returns a JSON object with `delegations`.
- `/app/v2/sessions` returns a JSON object with `sessions`.
- Bad session message read returns `400`.
- `POST /app/v1/messages` with `dry_run: true` returns an SSE response and does
  not increase canonical DB message counts.

## Out Of Contract

Claude/Codex remote-control surfaces may exist in the bridge, but PocketAgent
must not depend on undocumented shapes. Add them here before making them a daily
use surface.
