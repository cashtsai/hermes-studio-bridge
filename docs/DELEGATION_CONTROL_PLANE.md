# Hermes Delegation Control Plane

This is the shared contract for Hermes personas dispatching CC/CX development
work. It is not XCash-only and not PocketAgent-only.

## Principle

Every persona that calls Claude Code or Codex must create a durable work-order
session through the bridge. Do not use `/dispatch` for real work because it is
memory-only and cannot be resumed after a bridge restart.

Required properties:

- `work_order`: stable cross-surface id shown in Telegram, Pocket, and provider
  surfaces. Format (v2, 2026-07-01): `AGENT-TASK-SUBTASK-YYYYMMDD-ID4`, e.g.
  `XW-POCKETCONN-APPLELOGIN-20260701-A4F8`. See "Work Order Format" below.
- `parent_persona`: Hermes persona that owns the orchestration.
- `parent_session`: optional upstream chat/session id.
- `provider`: `codex` for CX or `claude_code` for CC.
- `provider_session_id`: Codex thread id or Claude Code session name.
- `cwd`: real work directory.
- `takeover`: Pocket endpoints plus provider-native resume hints.

## Work Order Format (v2)

`{AGENT}-{TASK}-{SUBTASK}-{YYYYMMDD}-{ID4}`

| Segment | Meaning | Example |
|---|---|---|
| `AGENT` | Persona prefix: `XW` (XCash), `PT` (潘天晴), `SJ` (水鏡), `YF` (袁方) | `XW` |
| `TASK` | Project/initiative code, shared across every delegation under the same effort — use this to find the whole thread of work on one project, including retries after a crash/timeout | `POCKETCONN` |
| `SUBTASK` | This specific delegation's concrete deliverable | `APPLELOGIN` |
| `YYYYMMDD` | Full 8-digit date (v1 only had `MMDD`, which collides across years) | `20260701` |
| `ID4` | 4 hex chars, collision guard | `A4F8` |

Callers **must** supply `task_code` and `subtask_code` in the create request
(alnum only, dashes/punctuation stripped — they become upper-case segments).
The bridge builds the full `work_order` from them. Passing an explicit
`work_order` directly is still supported for one-off cases, but the normal
path is `task_code`/`subtask_code` so the format stays consistent and
greppable — e.g. `GET /app/v1/delegations?task_code=POCKETCONN` returns every
delegation under that project regardless of which subtask or which day.

Do not reuse a `TASK` code across unrelated projects, and do not invent a new
`TASK` code for a retry of the same subtask — same task_code + same
subtask_code across multiple delegations is expected and desired (it's how you
find "every attempt at X" later).

## Create

`POST /app/v1/delegations`

Request:

```json
{
  "parent_persona": "xcash",
  "parent_session": "tg:xcash",
  "created_via": "telegram",
  "provider": "codex",
  "task_code": "POCKETCONN",
  "subtask_code": "APPLELOGIN",
  "title": "Fix PocketAgent QR pairing",
  "objective": "Implement and verify the QR pairing flow",
  "cwd": "/Users/xcash/apps/pocketagent",
  "spec_path": "docs/ACCOUNT_CROSS_DEVICE_ARCH.md",
  "acceptance": "curl smoke test plus app build"
}
```

Aliases:

- `provider=codex` or `provider=cx` creates a Codex app-server native thread.
- `provider=claude_code` or `provider=cc` creates a ccsess Claude Code session.

Response:

```json
{
  "ok": true,
  "delegation": {
    "id": "dlg-...",
    "work_order": "XW-POCKETCONN-APPLELOGIN-20260701-A4F8",
    "display_title": "XW-POCKETCONN-APPLELOGIN-20260701-A4F8 - Fix PocketAgent QR pairing",
    "parent_persona": "xcash",
    "provider": "codex",
    "provider_session_id": "thread_...",
    "takeover": {
      "pocket": {
        "input_endpoint": "/codexsessions/thread_.../input"
      },
      "official": {
        "surface": "codex_app_server_thread",
        "thread_id": "thread_..."
      }
    }
  }
}
```

## Continue

Pocket may continue any delegation without branching on provider:

`POST /app/v1/delegations/{id-or-work_order}/input`

```json
{"content": "請照剛剛的計畫開始做 M1，完成後回報驗證輸出"}
```

Advanced clients may use `delegation.takeover.pocket` raw provider endpoints:

- Codex: `/codexsessions/{thread_id}/input`, `/history`, `/stream`, `/status`,
  `/interrupt`.
- Claude Code: `/ccsessions/{name}/input`, `/history`, `/stream`, `/status`,
  `/interrupt`, `/key`.

## List

- `GET /app/v1/delegations`
- `GET /app/v1/delegations/{id-or-work_order}`
- `GET /app/v2/sessions`

`/app/v2/sessions` lists delegations as first-class sessions with `meta.work_order`
and `meta.takeover`, so Pocket can render work orders directly.

## Persona Rules

- XCash, Pan Tianqing, ShuiJing, Yuanfang, and future personas use the same API.
- The orchestrating persona must show the `work_order` in the first line when
  reporting back to the user.
- The child session's first prompt must include the same `work_order`.
- Personas may differ in judgement, style, and domain ownership, but not in the
  CC/CX session mechanics.
- Production writes, formal notifications, publishing, and real user state
  changes still require explicit approval before the child proceeds.

