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

### `GET /app/v1/sessions`

Returns persona and task sessions visible to the app.

Persona sessions must include:

- `id`
- `type`
- `name`
- `preview`
- `status`

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
- Bad session message read returns `400`.
- `POST /app/v1/messages` with `dry_run: true` returns an SSE response and does
  not increase canonical DB message counts.

## Out Of Contract

Claude/Codex remote-control surfaces may exist in the bridge, but PocketAgent
must not depend on undocumented shapes. Add them here before making them a daily
use surface.
