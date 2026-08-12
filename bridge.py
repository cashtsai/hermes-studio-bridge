#!/usr/bin/env python3
from __future__ import annotations
"""OpenAI-compatible bridge → Hermes agent (per-persona, shared memory).

Open WebUI (or any OpenAI-compatible client) points its API base at this
server. Each Hermes persona is exposed as a "model"; a chat completion runs
`hermes -z <last user msg> --continue <persona-session>` with the persona's
HERMES_HOME, so the reply comes from that persona WITH its shared long-term
memory. PocketAgent/ACP is the primary app surface; Telegram gateways are
legacy ingress/fallback surfaces.

Run:  uvicorn bridge:app --host 0.0.0.0 --port 8081
"""
import asyncio
import base64
import carddigest
import collections
import contextlib
import fcntl
import glob
import hashlib
import hmac
import json
import difflib
import mimetypes
import os
import pty
import re
import secrets
import shlex
import signal
import socket
import struct
import shutil
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import agent_call as agent_call_policy
import agent_context as agent_context_policy
import agent_registry
import host_discovery
from harness import distill as harness_distill
from harness import model as harness_model
from harness import store as harness_store
from harness import trajectory as harness_traj
import media_artifacts
import hermes_media
import openclaw_provider
import tg_outbound
import workers as workers_store
from fastapi import (FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect,
                     File, Form, UploadFile)
from fastapi.responses import (JSONResponse, StreamingResponse, FileResponse,
                               HTMLResponse, PlainTextResponse)
from starlette.websockets import WebSocketState

from acp_client import ACPPool, canonical_telegram_session

# Persistent warm ACP process per persona — removes the ~5s `hermes -z`
# cold start per message and streams output live. Cold `hermes -z` stays as a
# fallback if ACP ever fails.
POOL = ACPPool()

# M2/M3 — registry of dispatched CC/Codex sub-sessions, surfaced in GET /sessions
# and continuable like a persona. Keyed by an opaque session id.
SUBSESSIONS: dict = {}

# Strong refs to detached turn tasks so they finish (and record the reply) even
# if the client's network drops mid-stream. Without this they could be GC'd.
_BG_TASKS: set = set()
# A3-3:主事件圈把手 —— 讓 to_thread 裡的同步碼(報告同步的 notice 建立)
# 能 call_soon_threadsafe 把卡片 feed 排回單圈(SessionCardStore 不上鎖)。
# startup 時由 _start_housekeeping 填入。
_MAIN_LOOP = None

# Durable media index.  Construction is lazy so importing bridge.py in tests
# does not create production state under ~/.pocket.
_MEDIA_ARTIFACT_STORE = None
_MEDIA_ARTIFACT_STORE_LOCK = threading.Lock()
_MEDIA_ARTIFACT_ROOT = os.path.expanduser(
    os.environ.get("POCKET_MEDIA_DIR", "~/.pocket/media-artifacts")
)

# One SSE keepalive cadence for every streaming endpoint (issue #8: it was
# 2s / 4s / 10s across chat, ccsessions and codexsessions for no reason).
SSE_KEEPALIVE_SECS = 2.0
# A persona provider may stay connected but emit no ACP events. The timeout is
# deliberately configurable so the cleanup path can be exercised quickly in
# regression tests while production keeps the five-minute ceiling.
PERSONA_STALL_LIMIT_SECS = float(os.environ.get("PERSONA_STALL_LIMIT_SECS", "300"))

# A follow stream that has sent ZERO data (keepalives don't count) for this long
# gets disconnected — a client that hangs without reading otherwise pins the
# generator (and its task) forever. Shared by every long-lived streaming
# endpoint (issue #7 item 4); env-overridable so tests can trip it fast.
_STREAM_IDLE_CUTOFF_SECS = float(os.environ.get("BRIDGE_STREAM_IDLE_SECS", "1800"))

# 工具步驟 cmd/路徑的截斷上限(#38 diff 卡缺口):140 會把深路徑攔腰砍斷,
# app 的 diff chip 拿殘缺路徑去打 /filediff 就 404。所有 transcript/步驟
# 格式化共用這一個值(carddigest._CMD_MAX 同步)。
TOOL_CMD_MAX = 500

# In-flight app-turn dedup (issue #9): (session, client_id) -> {ts, task, state}.
# A duplicate POST with the same client_id while the first run is STILL RUNNING
# attaches to it instead of re-running the turn (side effects must not replay).
# Entries expire after 600s; cleanup happens on each access so the dict can't leak.
_APP_TURN_INFLIGHT: dict = {}
_APP_TURN_INFLIGHT_TTL = 600.0
_APP_TURN_INFLIGHT_LOCK = asyncio.Lock()

# CX input dedup:(thread_id, client_id) -> {ts, result, event}。
# persona 早就有 _APP_TURN_INFLIGHT,CX 一直沒有 —— 以前是靠 app-server 的
# 409(-32600)「意外」擋掉重送。本 PR 把 409 換成排隊層之後那面牆沒了,
# 於是每一條重試路徑都變成**保證重複執行**:
#   • app 端 90s client timeout 後重送
#   • OfflineOutbox 自動補送
#   • retryPending 手動重試(可連點)
# 同一個 client_id 在 TTL 內只做一次,重複請求回**原本那一次**的結果。
# interrupt「沒有回合可中斷」的 bridge 內部 sentinel。app-server 的 -32600 一律
# 被翻成 CX_TURN_IN_FLIGHT(「上一輪正在跑」),語意剛好相反,不能共用。
_CX_NO_ACTIVE_TURN_CODE = -32099

_CX_INPUT_INFLIGHT: dict = {}
_CX_INPUT_INFLIGHT_TTL = 600.0
_CX_INPUT_INFLIGHT_WAIT = 30.0     # 前一次還沒完成時,重複請求最多等這麼久
_CX_INPUT_INFLIGHT_LOCK = asyncio.Lock()

# Bearer token gate. The bridge fronts a tool-executing agent, so it must not
# be an open control surface even on the tailnet. Open WebUI sends this as its
# OpenAI API key. Override via the BRIDGE_TOKEN env var.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "CHANGE-ME")  # real value injected via LaunchAgent env

# --- Per-device tokens + one-time pairing codes -------------------------------
# Hardened pairing: the QR carries a short-lived ONE-TIME CODE, never the master
# BRIDGE_TOKEN. The desktop (which holds the master token) mints a code via
# /pair/new; the phone exchanges it at /pair/claim for its OWN device token,
# which is stored server-side and can be revoked per device. The master token
# keeps working (desktop + any already-connected client), so this is additive.
_POCKET_DIR = os.path.expanduser("~/.pocket")
_DEVICE_TOKENS_PATH = os.path.join(_POCKET_DIR, "device-tokens.json")
_PAIR_LOCK = threading.Lock()
_PAIR_CODES: dict = {}          # code -> {expiry, apple_user_id} or legacy expiry
_PAIR_CODE_TTL = 600.0          # a pairing code is valid for 10 minutes
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ID_ISSUER = "https://appleid.apple.com"
APPLE_WEB_PUBLIC_AUDIENCE = os.environ.get(
    "APPLE_WEB_PUBLIC_AUDIENCE", "com.pocketagent.web"
).strip()
APPLE_ID_AUDIENCES = tuple(dict.fromkeys([
    *(
        a.strip()
        for a in os.environ.get("APPLE_ID_AUDIENCES", "com.pocketagent.ios").split(",")
        if a.strip()
    ),
    APPLE_WEB_PUBLIC_AUDIENCE,
]))
APPLE_WEB_AUTHORIZE_URL = "https://appleid.apple.com/auth/authorize"
APPLE_WEB_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_WEB_CLIENT_ID = os.environ.get(
    "APPLE_WEB_CLIENT_ID", APPLE_WEB_PUBLIC_AUDIENCE
).strip()
APPLE_WEB_REDIRECT_URI = os.environ.get("APPLE_WEB_REDIRECT_URI", "").strip()
APPLE_WEB_TEAM_ID = os.environ.get("APPLE_WEB_TEAM_ID", "").strip()
APPLE_WEB_KEY_ID = os.environ.get("APPLE_WEB_KEY_ID", "").strip()
APPLE_WEB_PRIVATE_KEY_PATH = os.path.expanduser(
    os.environ.get("APPLE_WEB_PRIVATE_KEY_PATH", "").strip()
)
APPLE_WEB_FLOW_TTL = 600
APPLE_WEB_FLOW_LIMIT = 256
APPLE_WEB_START_RATE_LIMIT = 10
APPLE_WEB_START_RATE_WINDOW = 60.0
ACCOUNT_SESSION_PREFIX = "paacct."
ACCOUNT_SESSION_TTL = 60 * 60 * 24 * 90
_APPLE_JWK_CLIENT = None
_APPLE_WEB_FLOWS: dict = {}
_APPLE_WEB_STARTS: dict[str, collections.deque] = {}
_APPLE_WEB_FLOW_LOCK = threading.Lock()


def _load_device_tokens() -> dict:
    try:
        with open(_DEVICE_TOKENS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}          # first run — nothing paired yet, not an error
    except Exception as e:  # noqa: BLE001
        # A corrupt tokens file silently logs every paired device out; that
        # must be visible in the log, not swallowed (issue #7).
        _log_event("device_tokens_load_failed", path=_DEVICE_TOKENS_PATH,
                   error=type(e).__name__, error_message=str(e)[:160])
        return {}


def _save_device_tokens(d: dict) -> None:
    os.makedirs(_POCKET_DIR, exist_ok=True)
    tmp = _DEVICE_TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DEVICE_TOKENS_PATH)


# Brute-force guard for the token gate. Once the bridge is reachable from the
# public internet (Tailscale Funnel), the only thing between an attacker and a
# tool-executing agent is this token, so failed attempts are rate-limited. A
# VALID token is never throttled — only wrong guesses accrue, so a flood of bad
# tokens can't lock out the real client (no self-inflicted DoS). With a long
# random token, brute force is already infeasible; this mainly stops scanning
# and log spam, and signals abuse via 429.
# 2026-08-12 多租戶強化(relay 對外開放前必修):
#  ① 節流桶改 **per-client**。原本是單一全域 deque —— 有效 token 在 _check_auth
#     最上面就 return,所以正常使用者本來就不會被鎖(那部分設計是對的、註解也
#     講明了);但一個來源的錯誤嘗試會讓**其他來源**的無效請求一起吃 429 ——
#     包含新裝置配對、token 過期要重配的正常人。單機自用無感,relay 多租戶會咬人。
#  ② `_AUTH_FAIL_AGG` 原本永不清理,key=(client, path, status) —— 攻擊者變換
#     path 就能無限撐大這張 dict → 記憶體耗盡。現在有硬上限 + TTL 清理。
_AUTH_FAILS_BY_CLIENT: dict = {}   # client → deque[monotonic]
_AUTH_FAIL_WINDOW = 60.0  # seconds
_AUTH_FAIL_MAX = 12       # wrong guesses per window before 429(每個 client 各自算)
_AUTH_LOCK = threading.Lock()
_AUTH_FAIL_AGG: dict = {}
_AUTH_TABLE_MAX = 2048    # 兩張表的硬上限;超過就清掉沒動靜的條目
_AUTH_AGG_TTL = 900.0     # 聚合條目 15 分鐘沒再犯就忘掉(monotonic)


def _auth_fail_bump_locked(request: Request, now: float) -> bool:
    """記一次認證失敗,回傳「**這個 client** 是否已超額」。呼叫端須持 _AUTH_LOCK。

    per-client 分桶:一個來源灌爆自己的桶,不影響其他來源。表滿時清掉整窗
    都沒動靜的桶(掃描者換 IP 也撐不大)。
    """
    client = _client_host(request) or "?"
    dq = _AUTH_FAILS_BY_CLIENT.get(client)
    if dq is None:
        dq = collections.deque()
        _AUTH_FAILS_BY_CLIENT[client] = dq
    while dq and now - dq[0] > _AUTH_FAIL_WINDOW:
        dq.popleft()
    dq.append(now)
    if len(_AUTH_FAILS_BY_CLIENT) > _AUTH_TABLE_MAX:
        for k in [k for k, v in _AUTH_FAILS_BY_CLIENT.items()
                  if k != client and (not v or now - v[-1] > _AUTH_FAIL_WINDOW)]:
            _AUTH_FAILS_BY_CLIENT.pop(k, None)
    return len(dq) > _AUTH_FAIL_MAX


def _log_event(event: str, **fields) -> None:
    payload = {
        "event": event,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    print("[bridge-event] " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


# ── 例外可觀測性(issue #7 項目 1)─────────────────────────────────────────
# 稽核時全檔有 140+ 處 `except Exception` 完全不留痕:壞了只看得到症狀
# (B1「送圖 502 沒有 stderr」就是這樣查不動)。這裡給一個統一入口,把
# 「吞掉例外」變成「吞掉但留痕」,而且**不改控制流、不改回應碼**。
#
# 為什麼需要節流:這些 handler 有一大票在 0.5s 巡一次的 watcher 迴圈裡
# (state.db 輪詢、CC follower…)。無腦每次都印,反而會把 bridge.out.log
# 用比現在快兩個數量級的速度塞爆——那就是同一張 issue 的項目 6。所以:
#
#   expected=True (預期失敗:第一次開機表還沒建、client 丟壞 JSON、
#       tmux pane 還沒生出來…)→ 同一個 (site, 例外型別) 在 cooldown 內
#       只印一次,期間再發生只累計 suppressed 數,下次印出時一起帶出來。
#   expected=False (異常:不該發生的事)→ 每次都印,不節流。
_EXC_LOG_COOLDOWN_SECS = float(os.environ.get("BRIDGE_EXC_LOG_COOLDOWN", "60"))
_EXC_LOG_STATE: dict = {}     # (site, exc_type) -> [last_logged_monotonic, suppressed]
_EXC_LOG_LOCK = threading.Lock()


def _log_exc(site: str, exc: BaseException, *, expected: bool = False,
             **fields) -> None:
    """把被吞掉的例外記成結構化事件。`site` 是「函式名[#序號]」,可直接 grep。"""
    etype = type(exc).__name__
    suppressed = 0
    if expected:
        key = (site, etype)
        now = time.monotonic()
        with _EXC_LOG_LOCK:
            st = _EXC_LOG_STATE.get(key)
            if st is not None and now - st[0] < _EXC_LOG_COOLDOWN_SECS:
                st[1] += 1
                return
            if st is None:
                _EXC_LOG_STATE[key] = [now, 0]
            else:
                suppressed, st[0], st[1] = st[1], now, 0
    payload = {
        "site": site,
        "error": etype,
        "error_message": str(exc)[:200],
        "severity": "expected" if expected else "anomaly",
        **fields,
    }
    if suppressed:
        payload["suppressed"] = suppressed
    _log_event("exc_swallowed", **payload)


def _media_store() -> media_artifacts.MediaArtifactStore:
    global _MEDIA_ARTIFACT_STORE
    if _MEDIA_ARTIFACT_STORE is None:
        with _MEDIA_ARTIFACT_STORE_LOCK:
            if _MEDIA_ARTIFACT_STORE is None:
                _MEDIA_ARTIFACT_STORE = media_artifacts.MediaArtifactStore(
                    _MEDIA_ARTIFACT_ROOT
                )
    return _MEDIA_ARTIFACT_STORE


def _media_capture_sync(session_id: str, payload) -> list:
    return _media_store().capture_payload(session_id, payload)


def _schedule_media_capture(session_id: str, payload) -> None:
    """Copy referenced files off temp storage without blocking the event loop."""
    if not session_id or payload is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(asyncio.to_thread(
        _media_capture_sync, session_id, payload
    ))
    _BG_TASKS.add(task)

    def _done(done_task):
        _BG_TASKS.discard(done_task)
        try:
            done_task.result()
        except Exception as exc:  # noqa: BLE001
            _log_event(
                "media_capture_error",
                session_hash=_short_hash(session_id),
                error=type(exc).__name__,
            )

    task.add_done_callback(_done)


def _media_wire_item(item: dict) -> dict:
    out = dict(item)
    if out.get("source_kind") == "url":
        out["source_url"] = out.get("source_ref")
    elif out.get("available"):
        out["download_url"] = f"/app/v2/artifacts/{out['media_id']}"
    return out


# Loaded after _log_event exists so a corrupt tokens file gets logged.
_DEVICE_TOKENS: dict = _load_device_tokens()


def _client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _short_hash(value: str | None) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _attachment_stats(attachments: list) -> dict:
    kinds = collections.Counter((a or {}).get("kind") or "unknown" for a in (attachments or []))
    return {
        "attachment_count": len(attachments or []),
        "image_count": kinds.get("image", 0),
        "audio_count": kinds.get("audio", 0),
        "file_count": sum(v for k, v in kinds.items() if k not in ("image", "audio")),
    }


def _auth_agg_prune_locked(now: float) -> None:
    """聚合表清理:先丟過期(TTL),仍超上限就丟最久沒動的。

    原本這張表永不清理,key 含 request path —— 掃描者變換路徑即可無限撐大
    (記憶體耗盡)。呼叫端須持 _AUTH_LOCK。
    """
    if len(_AUTH_FAIL_AGG) <= _AUTH_TABLE_MAX:
        return
    for k in [k for k, v in _AUTH_FAIL_AGG.items()
              if now - v.get("seen", 0.0) > _AUTH_AGG_TTL]:
        _AUTH_FAIL_AGG.pop(k, None)
    if len(_AUTH_FAIL_AGG) > _AUTH_TABLE_MAX:
        for k, _ in sorted(_AUTH_FAIL_AGG.items(),
                           key=lambda kv: kv[1].get("seen", 0.0)
                           )[:len(_AUTH_FAIL_AGG) - _AUTH_TABLE_MAX]:
            _AUTH_FAIL_AGG.pop(k, None)


def _auth_fail_summary_locked(request: Request, status: int, now: float) -> dict | None:
    key = (_client_host(request), request.url.path, status)
    item = _AUTH_FAIL_AGG.setdefault(key, {"count": 0, "last_log": 0.0, "seen": now})
    item["count"] += 1
    item["seen"] = now
    _auth_agg_prune_locked(now)
    count = item["count"]
    should_log = count in (1, 10, 50, 100) or now - item["last_log"] >= 60.0
    if not should_log:
        return None
    item["last_log"] = now
    return {
        "client": key[0],
        "path": key[1],
        "status": status,
        "count": count,
        "window_seconds": int(_AUTH_FAIL_WINDOW),
    }


def _check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if hmac.compare_digest(token, BRIDGE_TOKEN):
        return  # constant-time match; master token always allowed
    if token:
        # Per-device token (issued via /pair/claim). Membership check + refresh
        # last_seen in memory only (no disk write per request).
        with _PAIR_LOCK:
            dev = _DEVICE_TOKENS.get(token)
            if dev is not None:
                if not dev.get("apple_user_id") or _account_device_for_token(token) is not None:
                    dev["last_seen"] = time.time()
                    return
        if _account_device_for_token(token) is not None:
            return
    now = time.monotonic()
    with _AUTH_LOCK:
        over = _auth_fail_bump_locked(request, now)
        summary = _auth_fail_summary_locked(request, 429 if over else 401, now)
    if summary:
        _log_event("auth_failure", **summary)
    if over:
        raise HTTPException(status_code=429, detail="too many failed auth attempts; slow down")
    raise http_err(401, "AUTH_INVALID_TOKEN", "invalid bridge token")


def _require_master_auth(request: Request) -> None:
    """Require the owner token for host-level Hermes configuration changes."""
    _check_auth(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(token, BRIDGE_TOKEN):
        raise http_err(
            403,
            "OWNER_AUTH_REQUIRED",
            "owner authorization is required for Hermes settings changes",
        )


async def _json_body(request: Request) -> dict:
    """Body-as-dict with empty/malformed JSON tolerated as {} — handlers then
    hit their own field validation (400) instead of the parser's 500."""
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_json_body", _exc, expected=True)
        return {}
    return body if isinstance(body, dict) else {}

def _first_existing_path(candidates: list[str], fallback: str) -> str:
    for raw in candidates:
        path = os.path.expanduser(raw)
        if os.path.exists(path):
            return path
    return os.path.expanduser(fallback)


HOME_ROOT = os.path.expanduser(os.environ.get(
    "HERMES_HOME_ROOT",
    "~/apps/hermes-agent/home",
))
HERMES_BIN = os.path.expanduser(os.environ.get("HERMES_BIN", "")) or _first_existing_path(
    [
        "~/apps/hermes-agent/runtime/venv/bin/hermes",
        "~/apps/hermes-agent/venv/bin/hermes",
        "~/.local/bin/hermes",
    ],
    "~/apps/hermes-agent/runtime/venv/bin/hermes",
)

# In-app terminal kill switch (TERMINAL_PTY_CONTRACT.md §安全). Paired devices
# get full shell access over /app/v1/terminal, so a self-hosted owner needs an
# escape hatch; "0" makes the endpoint refuse every handshake.
POCKET_TERMINAL_ENABLED = os.environ.get("POCKET_TERMINAL_ENABLED", "1") != "0"

# model id -> (display name, HERMES_HOME). id stays ascii for client URLs.
# G6 (wave 2): these four are the code-level BUILTINS; the personas table in
# canonical.db overlays them (rename / disable / soft-delete) and adds custom
# personas. PERSONAS itself stays a plain {id: (display, home)} dict mutated
# in place by _personas_reload(), so every existing consumer keeps working and
# CRUD takes effect without a restart.
_PERSONAS_BUILTIN = {
    "yuanfang":    ("袁方 (幕僚長/main)", HOME_ROOT),
    "pantianqing": ("潘天晴 (FLiPER)",    f"{HOME_ROOT}/profiles/fliper"),
    "xcash":       ("XCash (PocketAgent 協調)", f"{HOME_ROOT}/profiles/xcash"),
    "shuijing":    ("水鏡 (shuijing)",    f"{HOME_ROOT}/profiles/shuijing"),
}
PERSONAS = dict(_PERSONAS_BUILTIN)

# ── Persona 正典身分(TG 同源)─────────────────────────────────────────
# HOME_ROOT/avatars/ 是四人格(+自訂)的視覺與命名正典:manifest.json 提供
# name(TG bot 顯示名)/file(頭像檔)/tg(@username,可後補),圖檔供
# /app/v1/personas/<id>/avatar 直接下發。manifest 缺漏/損壞一律安靜退回
# 既有 builtins+db 行為(備援鐵律)。
_AVATARS_DIR = f"{HOME_ROOT}/avatars"
_avatar_manifest_cache = {"mtime": -1.0, "data": {}}


def _avatar_manifest() -> dict:
    path = os.path.join(_AVATARS_DIR, "manifest.json")
    try:
        mt = os.path.getmtime(path)
        if mt != _avatar_manifest_cache["mtime"]:
            with open(path, encoding="utf-8") as f:
                _avatar_manifest_cache["data"] = json.load(f).get("personas") or {}
            _avatar_manifest_cache["mtime"] = mt
    except Exception as _exc:  # noqa: BLE001 — 無 manifest = 無 overlay
        _log_exc("_avatar_manifest", _exc, expected=True)
        _avatar_manifest_cache["data"] = {}
        _avatar_manifest_cache["mtime"] = -1.0
    return _avatar_manifest_cache["data"]


def _avatar_path(pid: str):
    """頭像實檔路徑(manifest.file 優先,預設 <pid>.png);不存在/越界 → None。"""
    ent = _avatar_manifest().get(pid) or {}
    fn = ent.get("file") or f"{pid}.png"
    p = os.path.realpath(os.path.join(_AVATARS_DIR, fn))
    root = os.path.realpath(_AVATARS_DIR) + os.sep
    return p if p.startswith(root) and os.path.isfile(p) else None


def _personas_db_rows() -> list:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT id,name,home,enabled,deleted FROM personas").fetchall()
            con.close()
            return rows
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_personas_db_rows", _exc, expected=True)
        return []      # table not created yet (first boot) → builtins only


def _personas_reload() -> None:
    """Rebuild PERSONAS from builtins + the personas table. In-place mutation:
    all lookups (home_for, /sessions, message endpoints) see changes at once."""
    merged = dict(_PERSONAS_BUILTIN)
    for pid, name, home, enabled, deleted in _personas_db_rows():
        if deleted or not enabled:
            merged.pop(pid, None)
            continue
        base = merged.get(pid, (pid, HOME_ROOT))
        merged[pid] = (name or base[0], home or base[1])
    # TG 同源正典名 overlay(manifest 有名字就贏 — xcash 2026-07-05:同步是首要)
    for pid, ent in _avatar_manifest().items():
        if pid in merged and ent.get("name"):
            merged[pid] = (ent["name"], merged[pid][1])
    PERSONAS.clear()
    PERSONAS.update(merged)

# Per-(persona, conversation) hermes session name. Open WebUI doesn't send a
# stable conversation id in the OpenAI schema, so we key on persona only —
# one continuing conversation per persona (matches "talk to each persona").
def session_name(model: str) -> str:
    return f"owui-{model}"


def home_for(model: str) -> str:
    return PERSONAS.get(model, (None, HOME_ROOT))[1]


async def acp_full(model: str, prompt: str) -> str:
    """Collect a whole ACP turn into one string (non-streaming clients)."""
    session = await POOL.get(model, home_for(model))
    parts = []
    async for kind, val in session.prompt_stream(prompt):
        if kind == "text":
            parts.append(val)
    return ("".join(parts)).strip() or "(空回應)"


app = FastAPI(title="Hermes ↔ OpenAI bridge")


@app.middleware("http")
async def _body_size_guard(request: Request, call_next):
    """全域 request body 上限 — `await request.json()` 是整包進記憶體的,
    沒有這道閥一個超大 base64 就能把 bridge 打爆(修復單「附件限制」)。
    無 Content-Length(chunked)放行,由件數/單檔閥背書。"""
    try:
        cl = int(request.headers.get("content-length") or 0)
    except ValueError:
        cl = 0
    content_type = (request.headers.get("content-type") or "").lower()
    is_attachment_stream = request.url.path == "/app/v1/uploads/raw" or (
        request.url.path == "/app/v1/uploads/file" and content_type.startswith("multipart/")
    )
    if cl > _BODY_MAX_BYTES and not is_attachment_stream:
        return JSONResponse(status_code=413,
                            content={"error": {"code": "BODY_TOO_LARGE",
                                               "message": f"body 上限 {_BODY_MAX_BYTES} bytes"}})
    return await call_next(request)


# ───────────────────────── structured error codes (issue #6) ────────────────
# Every HTTP error carries a machine-readable code so the app can localize
# (pocketagent#44) instead of string-matching English detail text. The legacy
# top-level `detail` field is PRESERVED for old clients.
class BridgeError(HTTPException):
    def __init__(self, status: int, code: str, message: str, detail: str = ""):
        super().__init__(status_code=status, detail=detail or message)
        self.code = code
        self.message = message


def http_err(status: int, code: str, message: str, detail: str = "") -> BridgeError:
    """Build a coded HTTP error: raise http_err(404, "SESSION_NOT_FOUND", ...)."""
    return BridgeError(status, code, message, detail)


# Fallback codes for plain HTTPException raises that haven't adopted http_err.
_GENERIC_ERROR_CODES = {
    400: "BAD_REQUEST", 401: "AUTH_INVALID_TOKEN", 403: "FORBIDDEN",
    404: "NOT_FOUND", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
    429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 502: "UPSTREAM_FAILED",
    504: "PROVIDER_TIMEOUT",
}

from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def _bridge_http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
    code = getattr(exc, "code", "") or _GENERIC_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
    message = getattr(exc, "message", "") or detail
    body = {
        "detail": exc.detail,   # backward compat: old clients read this
        "error": {"code": code, "message": message, "detail": detail},
    }
    headers = dict(getattr(exc, "headers", None) or {})
    headers["X-Error-Code"] = code
    # issue #7 項目 1:4xx/5xx 要分明。4xx 是**客戶端**的錯(送壞 body、問不
    # 存在的 session),量大且無害,節流成一分鐘一筆免得洗版;5xx 是**我們**
    # 的錯(上游掛掉、provider 逾時),每筆都要留痕,B1「送圖 502 查不到原因」
    # 就是缺這個。回應碼與 body 一個字都不動,純加可觀測性。
    server_fault = exc.status_code >= 500
    if server_fault or _http_4xx_should_log(code, request.url.path):
        _log_event(
            "http_error_5xx" if server_fault else "http_error_4xx",
            status=exc.status_code, code=code, method=request.method,
            path=request.url.path, client=_client_host(request),
            detail=detail[:200],
        )
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


_HTTP_4XX_LOG_COOLDOWN = 60.0
_HTTP_4XX_LOG_STATE: dict = {}
_HTTP_4XX_LOG_LOCK = threading.Lock()


def _http_4xx_should_log(code: str, path: str) -> bool:
    """4xx 節流:同一 (code, path) 一分鐘一筆。掃描器/重連風暴打 401/404 時
    (稽核裡有過單一 log 檔 11,936 筆 404)不會反過來把磁碟塞爆。"""
    key = (code, path)
    now = time.monotonic()
    with _HTTP_4XX_LOG_LOCK:
        last = _HTTP_4XX_LOG_STATE.get(key, 0.0)
        if now - last < _HTTP_4XX_LOG_COOLDOWN:
            return False
        _HTTP_4XX_LOG_STATE[key] = now
        if len(_HTTP_4XX_LOG_STATE) > 2000:      # 有界,不隨路徑數無限長
            _HTTP_4XX_LOG_STATE.clear()
            _HTTP_4XX_LOG_STATE[key] = now
    return True


@app.exception_handler(Exception)
async def _bridge_unhandled_exception_handler(request: Request, exc: Exception):
    """未被接住的例外 → 結構化事件。回應與 Starlette 預設**完全相同**
    (`PlainTextResponse("Internal Server Error", 500)`),而且 ServerErrorMiddleware
    仍會把例外往外拋,所以 uvicorn 的 traceback 照樣進 bridge.err.log。
    也就是說:只多一筆可查的事件,回應碼/body/traceback 全部不變。"""
    _log_exc("unhandled_request", exc, expected=False,
             method=request.method, path=request.url.path,
             client=_client_host(request))
    return PlainTextResponse("Internal Server Error", status_code=500)


@app.get("/")
async def root():
    """Unauthenticated liveness probe. The app/monitors hit GET / to decide
    "bridge up?" — a 404 here was read as bridge-down and fueled an endless
    reconnect banner loop on the phone (11,936 404s in one log). Cheap, no
    secrets, no auth."""
    return {"ok": True, "service": "pocket-bridge", "ts": int(time.time())}


@app.get("/v1/models")
async def list_models(request: Request):
    _check_auth(request)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": now, "owned_by": "hermes",
             "name": disp}
            for mid, (disp, _home) in PERSONAS.items()
        ],
    }


async def run_hermes(model: str, prompt: str) -> str:
    home = PERSONAS.get(model, (None, HOME_ROOT))[1]
    env = dict(os.environ)
    env["HERMES_HOME"] = home
    # Cold fallback targets the SAME canonical Telegram session as the warm ACP
    # path (--resume takes a session id), so a fallback turn still lands where
    # the TG gateway looks — instead of a private owui-<persona> session the
    # phone/TG never see. Only without a mapping do we keep the old behaviour.
    sid = canonical_telegram_session(home)
    cont = ["--resume", sid] if sid else ["--continue", session_name(model)]
    proc = await asyncio.create_subprocess_exec(
        HERMES_BIN, "-z", prompt, *cont,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        return "⚠️ Hermes 回應逾時(180s)。"
    text = (out or b"").decode("utf-8", "replace").strip()
    if not text:
        text = (err or b"").decode("utf-8", "replace").strip() or "(空回應)"
    return text


# Where inbound attachments (images/files from the app's composer) land on the
# Studio box. We persist bytes here and hand the agent the path — every backend
# (Hermes persona / Claude Code / Codex) can Read a file, so this works across
# all three AND fixes the old "Claude sees the inline image but can't get the
# bytes" bug (HANDOFF known-issue #3).
UPLOAD_DIR = Path(os.path.expanduser("~/apps/hermes-agent/home/uploads"))

_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/heic": ".heic", "application/pdf": ".pdf",
             "audio/m4a": ".m4a", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
             "audio/aac": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav",
             "audio/x-wav": ".wav", "audio/webm": ".webm"}


# 附件上限(修復單「附件限制」bridge 端):count 與 /app/v1/uploads 既有 12 件
# 一致,推廣到所有直送口;單檔上限與 Pocket 對齊 2GiB。一般 app 上傳走
# /app/v1/uploads/raw 的原始串流路徑,舊版則走 /app/v1/uploads/file multipart;
# 全域 body 仍是 legacy JSON
# base64 路徑的記憶體防爆閥。
_ATT_MAX_COUNT = 12
_ATT_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
_BODY_MAX_BYTES = 768 * 1024 * 1024


def _att_guard(attachments) -> None:
    """直送 attachments 的件數守門 — 超過即 413(之前只有 uploads 有擋)。"""
    if isinstance(attachments, list) and len(attachments) > _ATT_MAX_COUNT:
        raise http_err(413, "TOO_MANY_ATTACHMENTS",
                       f"attachments 最多 {_ATT_MAX_COUNT} 件")


def _data_uri_estimated_bytes(data_uri: str) -> int:
    """base64 內容的解碼後大小估算(不真的解碼)— uploads 預檢用。"""
    i = (data_uri or "").find(";base64,")
    return 0 if i < 0 else (len(data_uri) - i - 8) * 3 // 4


def _upload_dest_path(filename: str, mime: str) -> Path:
    """落盤檔名的唯一產生處(data-URI 與 multipart 逐件上傳共用)。

    抽出來是為了讓兩條收檔路徑產生**一模一樣**的命名規則:時間戳 + 亂數
    + 淨化過的原檔名,副檔名缺了就依 mime 補。兩邊各寫一份遲早會漂移。
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", os.path.basename(filename or "")) or "file"
    if "." not in safe:
        safe += _MIME_EXT.get(mime, "")
    return UPLOAD_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{safe}"


def _save_data_uri(data_uri: str, filename: str = "") -> str | None:
    """Decode a `data:<mime>;base64,<...>` URI to UPLOAD_DIR; return the path."""
    m = re.match(r"data:([^;]+);base64,(.*)$", data_uri or "", re.DOTALL)
    if not m:
        return None
    mime, b64 = m.group(1), m.group(2)
    # 單檔大小閥(所有 data-URI 落盤的唯一咽喉):超限不落盤,skip+log —
    # 對齊 iOS 端「超過上限先略過」的行為,不炸整包請求。
    if len(b64) * 3 // 4 > _ATT_MAX_FILE_BYTES:
        _log_event("save_data_uri_rejected", reason="too_large", mime=mime,
                   filename=(filename or "")[:80], est_bytes=len(b64) * 3 // 4)
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        _log_event("save_data_uri_failed", stage="b64decode", mime=mime,
                   filename=(filename or "")[:80], error=type(e).__name__)
        return None
    path = _upload_dest_path(filename, mime)
    try:
        path.write_bytes(raw)
    except Exception as e:  # noqa: BLE001
        _log_event("save_data_uri_failed", stage="write", mime=mime,
                   path=str(path), bytes=len(raw),
                   error=type(e).__name__, error_message=str(e)[:160])
        return None
    return str(path)


def _upload_ref_path(value: str | None) -> str | None:
    """Accept only previously uploaded local files under UPLOAD_DIR."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("data:"):
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    try:
        root = UPLOAD_DIR.expanduser().resolve()
        path = Path(os.path.expanduser(raw)).resolve()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_upload_ref_path", _exc, expected=True)
        return None
    if not (path == root or root in path.parents):
        return None
    try:
        return str(path) if path.is_file() else None
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_upload_ref_path#2", _exc, expected=True)
        return None


def _save_attachment(a: dict, default_filename: str = "file") -> str | None:
    """Return an uploaded attachment path, saving legacy dataURI payloads if needed."""
    if not isinstance(a, dict):
        return None
    for key in ("path", "local_path", "file_path"):
        path = _upload_ref_path(a.get(key))
        if path:
            return path
    url_path = _upload_ref_path(a.get("url"))
    if url_path:
        return url_path
    filename = a.get("filename") or default_filename
    data_uri = a.get("data") or a.get("data_uri") or ""
    return _save_data_uri(data_uri, filename)


def _save_part_payload(value: str | None, filename: str) -> str | None:
    return _upload_ref_path(value) or _save_data_uri(value or "", filename)


# ───────────────────────── voice transcription (語音訊息) ───────────────────
# The bridge persists transport bytes, then asks the persona's Hermes profile
# to transcribe. Provider/model/endpoint/secret selection never lives here.
def _transcribe(path: str, home: str, lang: str = "") -> str:
    """Audio file path → Hermes transcript (best-effort; '' on failure)."""
    try:
        result = hermes_media.transcribe_audio(home, path, locale=lang)
    except Exception as e:  # noqa: BLE001
        _log_event(
            "hermes_stt_failed",
            error=type(e).__name__,
            error_message=str(e)[:240],
        )
        return ""
    if not result.get("success"):
        _log_event(
            "hermes_stt_failed",
            provider=result.get("provider"),
            error_message=str(result.get("error") or "unknown error")[:240],
        )
        return ""
    return str(result.get("transcript") or "").strip()


async def _transcribe_attachments(
    attachments: list,
    home: str,
    lang: str = "",
) -> str:
    """Save and transcribe audio through the persona's Hermes profile."""
    texts = []
    for a in (attachments or []):
        if a.get("kind") != "audio":
            continue
        path = _save_attachment(a, a.get("filename") or "voice.m4a")
        if not path:
            continue
        t = await asyncio.to_thread(_transcribe, path, home, lang)
        if t:
            texts.append(t)
    return " ".join(texts).strip()


# ── 會議逐字稿第一段修飾(善彰 2026-08-08:Pocket 會議錄音)───────────────
# STT 原稿常有缺標點、同音錯字、斷句錯誤。摘要前先用本地 ollama 模型做一段
# 「只修標點/錯字、不改語意」的清稿。失敗一律回原稿,絕不擋錄音→摘要主流程。
# 模型 = mistral-small3.2(2026-08-08 實測選定):18s、保留繁體、不破壞語意,
# 修主要錯字+標點。qwen3.5:27b 雖能多修冷僻同音字(慣老闆)但 211s 太慢;
# qwen3:4b 改壞語意;gpt-oss:20b 轉簡體。要換極致品質版設 MEETING_POLISH_MODEL。
_MEETING_POLISH_MODEL = os.environ.get("MEETING_POLISH_MODEL", "mistral-small3.2:latest")
_MEETING_POLISH_PROMPT = (
    "你是逐字稿校對。下面是一段中文會議語音的自動轉錄稿,可能有:缺標點、"
    "同音錯字、口語冗詞、斷句錯誤。請只做「標點與錯字修飾」:\n"
    "- 加上正確標點與段落斷句\n- 修明顯的同音/辨識錯字\n"
    "- 不改變原意、不增刪內容、不摘要、不翻譯\n- 不確定的字保留原樣\n"
    "- 專有名詞音近時優先對到這些正確寫法(缺的照聽打):STT、TTS、LLM、OCR、"
    "API、MCP、Hermes、Pocket、PocketAgent、Ollama、Whisper、Codex、Claude Code、"
    "FLiPER、Rakutai、一樂拉麵、Culture Supply、新想、"
    "袁方、潘天晴、水鏡先生、XCash、善彰\n"
    "只輸出修飾後的逐字稿,不要任何說明。\n\n逐字稿:\n"
)


async def _polish_transcript(text: str) -> str:
    """本地模型清稿(標點+錯字);失敗/逾時回原字,絕不擋摘要主流程。

    速度護欄(2026-08-08:首版清稿把回合前置拖到 272s,體感當機):
    - num_ctx 依逐字稿長度給,不吃模型預設的 262144 大 context(建 KV cache 是
      冷載入耗時大宗;一段逐字稿 8k~40k token 綽綽有餘)。
    - 90s 硬上限(asyncio.wait_for):逾時就用原始逐字稿往下走,寧可少一段清稿
      也不讓使用者枯等。keep_alive 拉長,連錄多段時第二段起省冷載入。
    """
    if not text.strip():
        return text
    # 中文 1 字約 1.2~1.5 token;輸入+輸出約 3x。夾在 8192~40960。
    num_ctx = min(40960, max(8192, len(text) * 3 + 2048))

    # 2026-08-11:這段本機 Ollama 呼叫被 Continual Harness 的夜批蒸餾共用,
    # 抽到 harness/model.py 當**唯一實作**(model/num_ctx/timeout/keep_alive/
    # temperature 逐項保持原值,行為不變)。fail-soft 語意刻意留在這裡 ——
    # 清稿要「失敗回原稿」,蒸餾要「失敗記一筆跑批錯誤」,不該由共用層代決。
    try:
        out = await harness_model.ollama_text(
            _MEETING_POLISH_PROMPT + text, model=_MEETING_POLISH_MODEL,
            num_ctx=num_ctx, timeout=90, temperature=0.2, keep_alive="15m")
        return out or text
    except asyncio.TimeoutError:
        _log_event("meeting_polish_timeout", chars=len(text), num_ctx=num_ctx)
        return text
    except Exception as e:  # noqa: BLE001
        _log_event("meeting_polish_failed",
                   error=type(e).__name__, error_message=str(e)[:200])
        return text


def _extract_user_parts(messages: list):
    """Last user message → (text, image_paths, [(label, file_path)]). Persists
    any attachments to UPLOAD_DIR."""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if not isinstance(c, list):
                return ((c or "").strip(), [], [])
            texts, images, files = [], [], []
            for p in c:
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if t == "text" and p.get("text"):
                    texts.append(p["text"])
                elif t == "image_url":
                    path = _save_part_payload((p.get("image_url") or {}).get("url", ""), "image.jpg")
                    if path:
                        images.append(path)
                elif t == "file":
                    f = p.get("file") or {}
                    path = _save_part_payload(f.get("file_data") or f.get("path") or f.get("url"),
                                              f.get("filename", "file"))
                    if path:
                        files.append((f.get("filename") or "檔案", path))
            return (" ".join(texts).strip(), images, files)
    return ("", [], [])


def _last_user_message(messages: list) -> str:
    """Text + on-disk paths for the last user turn. Used by CC/Codex sub-sessions,
    which can Read image files natively, so images stay as path references."""
    text, images, files = _extract_user_parts(messages)
    notes = [f"- 圖片:{p}" for p in images] + [f"- {label}:{p}" for label, p in files]
    if notes:
        text = (text + "\n\n[使用者附了以下檔案,已存到本機。請先用 Read/檔案工具讀取再回答]\n"
                + "\n".join(notes)).strip()
    return text


async def _ocr_image(path: str, home: str) -> str:
    """Extract image text through the persona's Hermes OCR capability."""
    try:
        result = await asyncio.to_thread(hermes_media.ocr_document, home, path)
    except Exception as e:  # noqa: BLE001
        _log_event(
            "hermes_ocr_failed",
            error=type(e).__name__,
            error_message=str(e)[:240],
        )
        return ""
    if not result.get("success"):
        _log_event(
            "hermes_ocr_failed",
            provider=result.get("provider"),
            error_message=str(result.get("error") or "unknown error")[:240],
        )
        return ""
    return str(result.get("text") or "").strip()


async def _resolve_persona_prompt(messages: list, home: str) -> str:
    """Build a persona prompt with Hermes OCR and local attachment paths."""
    text, images, files = _extract_user_parts(messages)
    notes = [f"- {label}:{p}(請用 Read 讀取)" for label, p in files]
    for path in images:
        ocr_text = await _ocr_image(path, home)
        if ocr_text:
            notes.append(f"- 圖片 OCR({path}):{ocr_text[:12000]}")
        else:
            notes.append(
                f"- 圖片:{path}"
                "(Hermes OCR 未讀到文字；需要辨識非文字畫面時請使用 vision_analyze)"
            )
    if notes:
        text = (text + "\n\n[使用者附件]\n" + "\n".join(notes)).strip()
    return text


# ───────────────────────── canonical store (M20) ───────────────────────────
# Bridge-owned source of truth for app turns, so the iPhone is NOT the only copy
# — survives reinstall / new device and interleaves with the Telegram history.
# The app talks to it through the versioned /app/v1 API; it never touches the
# Hermes state.db schema or cron JSON directly.
CANON_DB = os.environ.get("POCKET_CANON_DB") \
    or os.path.expanduser("~/.local/share/pocket-agent/canonical.db")
ACCOUNTS_DB = os.path.expanduser("~/.local/share/pocket-agent/accounts.db")
REPORT_MEMORY_FILE = "REPORTS.md"
REPORT_MEMORY_ITEMS = 20
REPORT_MEMORY_CHARS = 2400
REPORT_CONTEXT_DEFAULT = 3
REPORT_CONTEXT_TRIGGERED = 8
REPORT_CONTEXT_CHARS = 18000
REPORT_CONTEXT_ITEM_CHARS = 5000
HIDDEN_REPORT_SOURCES = {"hermes-tool-error", "bridge-health"}
HIDDEN_REPORT_NAMES = {"agent-tool-error", "bridge-health"}
HIDDEN_REPORT_LABELS = {"錯誤報告", "Bridge 健康警報", "Bridge 復原", "Bridge 警告"}
# 診斷報告要留在報告中心,但不能混進人格聊天/事件流/長期記憶。
# POCKET_ENABLE_TOOL_ERROR_REPORTS=0 只關掉工具錯誤掃描;已在庫裡的診斷報告
# 仍可從報告中心查閱,方便排查歷史問題。
TOOL_ERROR_HIDDEN_SOURCES = {"hermes-tool-error"}
TOOL_ERROR_HIDDEN_NAMES = {"agent-tool-error"}
TOOL_ERROR_HIDDEN_LABELS = {"錯誤報告"}
TOOL_ERROR_REPORTS_ENABLED = os.environ.get(
    "POCKET_ENABLE_TOOL_ERROR_REPORTS", "1").strip().lower() in {"1", "true", "yes", "on"}


def _is_hidden_report(report: dict) -> bool:
    source = str(report.get("external_source") or "").strip()
    name = str(report.get("name") or "").strip()
    label = str(report.get("label") or "").strip()
    return (
        source in HIDDEN_REPORT_SOURCES
        or name in HIDDEN_REPORT_NAMES
        or label in HIDDEN_REPORT_LABELS
    )


def _is_hidden_report_message(message: dict) -> bool:
    mid = str(message.get("id") or "")
    if not mid.startswith("rep-"):
        return False
    text = str(message.get("content") or "").strip()
    if (text.startswith("📰 **Bridge 健康警報**")
            or text.startswith("📰 **Bridge 復原**")
            or text.startswith("📰 **Bridge 警告**")):
        return True
    return text.startswith("📰 **錯誤報告**") or "工具錯誤" in text


def _is_hidden_message_event(data: dict) -> bool:
    message = data.get("message") if isinstance(data, dict) else None
    return isinstance(message, dict) and _is_hidden_report_message(message)


def _canon_init():
    import sqlite3
    os.makedirs(os.path.dirname(CANON_DB), exist_ok=True)
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        # WAL: concurrent handlers no longer serialize writers against readers;
        # busy_timeout waits out short lock contention instead of erroring.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("""CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT, attachments TEXT, created_at REAL NOT NULL, status TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_session_time ON messages(session, created_at)")
        # client_id: stable per-logical-send id so a retry after a dropped network
        # connection replays the recorded reply instead of re-running the turn.
        cols = [r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()]
        if "client_id" not in cols:
            con.execute("ALTER TABLE messages ADD COLUMN client_id TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_client ON messages(session, client_id)")
        # Reaction overlay (G2, pocketagent#39): keyed by the message id the app
        # sees in GET /app/v1/messages — canonical mids AND tg-<ts> ids alike — so
        # one table syncs reactions on both app-sent and Telegram-side messages.
        con.execute("""CREATE TABLE IF NOT EXISTS reactions(
            msg_id TEXT PRIMARY KEY, session TEXT, reaction TEXT, updated_at REAL)""")
        # Canonical reactions/pins (G2, pocketagent#39 final contract): multi-emoji
        # reactions (JSON list) + per-message pin, keyed by the id the app sees in
        # GET /app/v1/messages. Supersedes the single-`reaction` overlay above,
        # which is kept for backward compatibility with older app builds.
        con.execute("""CREATE TABLE IF NOT EXISTS message_meta(
            message_id TEXT PRIMARY KEY, reactions TEXT, pinned INTEGER,
            updated_at REAL)""")
        # G4 tombstone (wave 2): deleted messages stay in the list, flagged. The
        # table may pre-date this column, so ALTER idempotently.
        meta_cols = [r[1] for r in con.execute("PRAGMA table_info(message_meta)").fetchall()]
        if "deleted" not in meta_cols:
            con.execute("ALTER TABLE message_meta ADD COLUMN deleted INTEGER")
        # G2/#39 canonical 化收尾:pin 要能按 session 讀回(PUT/GET
        # /app/v1/sessions/{id}/pin),overlay 列補 session 歸屬。回填只認
        # canonical messages 表 — tg-<ts>/報告 id 不在其中,維持 NULL,查詢端
        # 以「messages join」補洞(見 _session_pinned_ids)。冪等:WHERE IS NULL。
        if "session" not in meta_cols:
            con.execute("ALTER TABLE message_meta ADD COLUMN session TEXT")
        con.execute("UPDATE message_meta SET session="
                    "(SELECT m.session FROM messages m WHERE m.id=message_meta.message_id)"
                    " WHERE session IS NULL")
        # G6 (wave 2): persona registry — overlays/extends the code builtins so
        # personas can be added / renamed / disabled without editing bridge.py.
        con.execute("""CREATE TABLE IF NOT EXISTS personas(
            id TEXT PRIMARY KEY, name TEXT, home TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
            created_at REAL, updated_at REAL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS approvals(
            id TEXT PRIMARY KEY, title TEXT, source TEXT, risk TEXT, detail TEXT,
            created_at REAL, expires_at REAL, status TEXT, decided_at REAL, result TEXT)""")
        # B4 (issue #9): decision push-back — the creating skill can register a
        # callback URL that gets POSTed when the approval is decided/expired,
        # instead of having to poll GET /app/v1/approvals/{id}.
        approval_cols = [r[1] for r in con.execute("PRAGMA table_info(approvals)").fetchall()]
        if "callback" not in approval_cols:
            con.execute("ALTER TABLE approvals ADD COLUMN callback TEXT")
        # A1 (Approval Hub 遷移切片): 統一 approval 物件 — 新欄位 + 回填。
        # session_id/provider/kind/options 與 source 並存(source 相容期保留原樣);
        # options 存建立方宣告的鍵(JSON 文字)。回填帶 IS NULL 守門,冪等。
        # hermes 舊列的 source 是自由字串 → session_id 不硬造(拍板:留 NULL)。
        # meta:提示層級的加值欄位(JSON 文字)。CC 的 AskUserQuestion 多選版面
        # 需要帶 multiselect / q_index / q_total —— 這些不是 per-option 資訊,
        # 塞不進 options 陣列,也不該埋進 detail 純文字。缺欄 = 舊列,讀出 None。
        for _col in ("session_id", "provider", "kind", "options", "meta"):
            if _col not in approval_cols:
                con.execute(f"ALTER TABLE approvals ADD COLUMN {_col} TEXT")
        con.execute("UPDATE approvals SET provider=CASE"
                    " WHEN source LIKE 'claude_code:%' THEN 'claude_code'"
                    " WHEN source LIKE 'codex%' THEN 'codex'"
                    " ELSE 'hermes' END WHERE provider IS NULL")
        con.execute("UPDATE approvals SET session_id=source WHERE session_id IS NULL"
                    " AND (source LIKE 'claude_code:%' OR source LIKE 'codex:%')")
        con.execute("UPDATE approvals SET kind='permission' WHERE kind IS NULL")
        con.execute("""CREATE TABLE IF NOT EXISTS devices(
            token TEXT PRIMARY KEY, platform TEXT, created_at REAL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS report_events(
            id TEXT PRIMARY KEY, session TEXT NOT NULL, label TEXT, name TEXT,
            content TEXT NOT NULL, ts REAL NOT NULL,
            external_source TEXT, external_id TEXT UNIQUE, ingested_at REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_report_session_time ON report_events(session, ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_report_external ON report_events(external_source, external_id)")
        # feat/report-actions-api:報告可帶「快速行動」按鈕(actions JSON 陣列,
        # 見 _report_actions_normalize)。舊列此欄 NULL = 無行動,讀取端當空陣列;
        # ALTER 加欄不動舊資料(與上方 approvals 遷移同款保守作法)。
        report_cols = {r[1] for r in con.execute("PRAGMA table_info(report_events)")}
        if "actions" not in report_cols:
            con.execute("ALTER TABLE report_events ADD COLUMN actions TEXT")
        # Sync engine P0 (docs/SYNC_ENGINE_REWRITE_PLAN_20260711.md §3):單一
        # append-only 事件日誌,id 即全域遞增 seq。P0/P1 只寫不讀(雙寫過渡,
        # 現有 canonical/state.db 讀取路徑不動),P2 起由 /app/v2/events 消費。
        # external_id 供來源鏡射去重(TG/cron 是重複掃描式接入,必須冪等)。
        con.execute("""CREATE TABLE IF NOT EXISTS event_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT UNIQUE,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_event_session_seq ON event_log(session, id)")
        # Sync engine P2:已讀游標的伺服器真相(取代 App 端 UserDefaults 計數
        # 器的長期方向)。一列 = 一個(session, device)的已讀位置 — 按裝置分列
        # 存,是為了「任一裝置讀過即全讀」(MAX over devices)與「每裝置各自
        # 記」兩種語意都能從同一份資料推導;多裝置語意由善彰拍板後在 App 端
        # (P3)選聚合方式,schema 不用改。
        con.execute("""CREATE TABLE IF NOT EXISTS read_cursors(
            session TEXT NOT NULL,
            device_id TEXT NOT NULL,
            last_read_seq INTEGER NOT NULL DEFAULT 0,
            last_read_ts REAL NOT NULL DEFAULT 0,
            message_id TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY(session, device_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS delegations(
            id TEXT PRIMARY KEY,
            work_order TEXT UNIQUE,
            parent_persona TEXT NOT NULL,
            parent_session TEXT,
            created_via TEXT,
            provider TEXT NOT NULL,
            title TEXT,
            objective TEXT,
            cwd TEXT,
            status TEXT,
            provider_session_id TEXT,
            codex_thread_id TEXT,
            cc_session_name TEXT,
            created_at REAL,
            updated_at REAL,
            last_error TEXT,
            meta TEXT,
            task_code TEXT,
            subtask_code TEXT)""")
        delegation_cols = [r[1] for r in con.execute("PRAGMA table_info(delegations)").fetchall()]
        for name, ddl in {
            "provider_session_id": "ALTER TABLE delegations ADD COLUMN provider_session_id TEXT",
            "codex_thread_id": "ALTER TABLE delegations ADD COLUMN codex_thread_id TEXT",
            "cc_session_name": "ALTER TABLE delegations ADD COLUMN cc_session_name TEXT",
            "last_error": "ALTER TABLE delegations ADD COLUMN last_error TEXT",
            "meta": "ALTER TABLE delegations ADD COLUMN meta TEXT",
            "task_code": "ALTER TABLE delegations ADD COLUMN task_code TEXT",
            "subtask_code": "ALTER TABLE delegations ADD COLUMN subtask_code TEXT",
        }.items():
            if name not in delegation_cols:
                con.execute(ddl)
        con.execute("CREATE INDEX IF NOT EXISTS idx_delegation_parent ON delegations(parent_persona, updated_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_delegation_provider ON delegations(provider, provider_session_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_delegation_task ON delegations(task_code, subtask_code)")
        # SUBSESSIONS persistence (issue #5, plan A): /dispatch sub-sessions used to
        # live only in the in-memory dict, so a bridge restart wiped them all —
        # transcript, resume target (cc_session) and isolate cwd included.
        con.execute("""CREATE TABLE IF NOT EXISTS subsessions(
            sid TEXT PRIMARY KEY, name TEXT, parent TEXT, tool TEXT, status TEXT,
            cwd TEXT, worktree TEXT, cc_session TEXT, last_user TEXT,
            last_at REAL, output_json TEXT)""")
        con.commit()
        con.close()
    finally:
        con.close()


ACCOUNT_USER_COLUMNS = ("apple_user_id", "email", "display_name", "created_at", "last_seen_at")
ACCOUNT_DEVICE_COLUMNS = (
    "device_id", "apple_user_id", "device_token", "platform", "label",
    "paired_at", "last_seen_at", "revoked",
)


def _accounts_init():
    import sqlite3
    os.makedirs(os.path.dirname(ACCOUNTS_DB), exist_ok=True)
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    try:
        # Same WAL rationale as canonical.db (issue #7): auth reads happen on every
        # request, so writers (pair/claim, last_seen) must not lock readers out.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            apple_user_id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS devices(
            device_id TEXT PRIMARY KEY,
            apple_user_id TEXT NOT NULL,
            device_token TEXT NOT NULL UNIQUE,
            platform TEXT,
            label TEXT,
            paired_at REAL NOT NULL,
            last_seen_at REAL,
            revoked INTEGER DEFAULT 0,
            FOREIGN KEY(apple_user_id) REFERENCES users(apple_user_id)
                ON UPDATE CASCADE ON DELETE CASCADE)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_account_devices_user ON devices(apple_user_id, revoked)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_account_devices_token ON devices(device_token)")
        con.commit()
        con.close()
    finally:
        con.close()


def _account_user_row(row):
    return dict(zip(ACCOUNT_USER_COLUMNS, row)) if row else None


def _account_device_row(row):
    return dict(zip(ACCOUNT_DEVICE_COLUMNS, row)) if row else None


def _account_public_user(user: dict | None):
    if not user:
        return None
    return {
        "apple_user_id": user.get("apple_user_id"),
        "email": user.get("email"),
        "display_name": user.get("display_name"),
        "created_at": user.get("created_at"),
        "last_seen_at": user.get("last_seen_at"),
    }


def _account_public_device(device: dict | None):
    if not device:
        return None
    token = device.get("device_token")
    return {
        "device_id": device.get("device_id"),
        "apple_user_id": device.get("apple_user_id"),
        "platform": device.get("platform"),
        "label": device.get("label"),
        "paired_at": device.get("paired_at"),
        "last_seen_at": device.get("last_seen_at"),
        "revoked": bool(device.get("revoked")),
        "token_hash": _short_hash(token),
    }


def _account_upsert_user(apple_user_id: str, email: str | None = None,
                         display_name: str | None = None):
    import sqlite3
    now = time.time()
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    try:
        con.execute(
            """INSERT INTO users(apple_user_id,email,display_name,created_at,last_seen_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(apple_user_id) DO UPDATE SET
                 email=COALESCE(excluded.email, users.email),
                 display_name=COALESCE(excluded.display_name, users.display_name),
                 last_seen_at=excluded.last_seen_at""",
            (apple_user_id, email or None, display_name or None, now, now))
        row = con.execute(
            f"SELECT {','.join(ACCOUNT_USER_COLUMNS)} FROM users WHERE apple_user_id=?",
            (apple_user_id,)).fetchone()
        con.commit()
        con.close()
        return _account_user_row(row)
    finally:
        con.close()


def _account_get_user(apple_user_id: str, touch: bool = False):
    import sqlite3
    if not apple_user_id:
        return None
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    try:
        if touch:
            con.execute("UPDATE users SET last_seen_at=? WHERE apple_user_id=?",
                        (time.time(), apple_user_id))
            con.commit()
        row = con.execute(
            f"SELECT {','.join(ACCOUNT_USER_COLUMNS)} FROM users WHERE apple_user_id=?",
            (apple_user_id,)).fetchone()
        con.close()
        return _account_user_row(row)
    finally:
        con.close()


def _account_devices_for_user(apple_user_id: str, include_revoked: bool = False):
    import sqlite3
    con = sqlite3.connect(f"file:{ACCOUNTS_DB}?mode=ro", uri=True, timeout=5)
    try:
        if include_revoked:
            rows = con.execute(
                f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
                "WHERE apple_user_id=? ORDER BY paired_at DESC",
                (apple_user_id,)).fetchall()
        else:
            rows = con.execute(
                f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
                "WHERE apple_user_id=? AND revoked=0 ORDER BY paired_at DESC",
                (apple_user_id,)).fetchall()
        con.close()
        return [_account_device_row(r) for r in rows]
    finally:
        con.close()


def _account_device_put(apple_user_id: str, device_token: str, platform: str = "ios",
                        label: str = "device", device_id: str | None = None):
    import sqlite3
    now = time.time()
    device_id = device_id or "dev-" + uuid.uuid4().hex
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            """INSERT INTO devices(device_id,apple_user_id,device_token,platform,label,
                                   paired_at,last_seen_at,revoked)
               VALUES(?,?,?,?,?,?,?,0)
               ON CONFLICT(device_token) DO UPDATE SET
                 apple_user_id=excluded.apple_user_id,
                 platform=excluded.platform,
                 label=excluded.label,
                 last_seen_at=excluded.last_seen_at,
                 revoked=0""",
            (device_id, apple_user_id, device_token, platform or "ios",
             (label or "device")[:80], now, now))
        row = con.execute(
            f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices WHERE device_token=?",
            (device_token,)).fetchone()
        con.commit()
        con.close()
        return _account_device_row(row)
    finally:
        con.close()


def _account_device_for_token(device_token: str, touch: bool = True):
    import sqlite3
    if not device_token:
        return None
    try:
        con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
        try:
            row = con.execute(
                f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
                "WHERE device_token=? AND revoked=0",
                (device_token,)).fetchone()
            if row and touch:
                con.execute("UPDATE devices SET last_seen_at=? WHERE device_token=?",
                            (time.time(), device_token))
                con.commit()
            con.close()
            return _account_device_row(row)
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_account_device_for_token", _exc, expected=True)
        return None


def _account_device_by_id(apple_user_id: str, device_id: str):
    import sqlite3
    if not apple_user_id or not device_id:
        return None
    con = sqlite3.connect(f"file:{ACCOUNTS_DB}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(
            f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
            "WHERE apple_user_id=? AND device_id=?",
            (apple_user_id, device_id)).fetchone()
        con.close()
        return _account_device_row(row)
    finally:
        con.close()


def _account_device_revoke(apple_user_id: str, device_id: str):
    import sqlite3
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    try:
        cur = con.execute(
            "UPDATE devices SET revoked=1, last_seen_at=? "
            "WHERE apple_user_id=? AND device_id=? AND revoked=0",
            (time.time(), apple_user_id, device_id))
        con.commit()
        revoked = cur.rowcount
        con.close()
        return revoked
    finally:
        con.close()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _account_session_create(apple_user_id: str):
    now = int(time.time())
    exp = now + ACCOUNT_SESSION_TTL
    payload = {"sub": apple_user_id, "iat": now, "exp": exp}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(BRIDGE_TOKEN.encode("utf-8"), body, hashlib.sha256).digest()
    return ACCOUNT_SESSION_PREFIX + _b64u(body) + "." + _b64u(sig), exp


def _account_session_payload(token: str):
    if not token or not token.startswith(ACCOUNT_SESSION_PREFIX):
        raise HTTPException(status_code=401, detail="missing account session")
    try:
        body_part, sig_part = token[len(ACCOUNT_SESSION_PREFIX):].split(".", 1)
        body = _b64u_decode(body_part)
        sig = _b64u_decode(sig_part)
        expected = hmac.new(BRIDGE_TOKEN.encode("utf-8"), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid account session")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="account session expired")
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="invalid account session")
    return payload


def _account_session_token_from_request(request: Request, body: dict | None = None):
    token = (request.headers.get("x-pocket-account-session")
             or request.headers.get("x-account-session") or "").strip()
    if not token and body:
        token = str(body.get("account_session") or body.get("accountSession") or "").strip()
    if not token:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if bearer.startswith(ACCOUNT_SESSION_PREFIX):
            token = bearer
    return token


def _account_user_from_request(request: Request, body: dict | None = None,
                               required: bool = True):
    token = _account_session_token_from_request(request, body)
    if not token:
        if required:
            raise HTTPException(status_code=401, detail="missing account session")
        return None
    payload = _account_session_payload(token)
    user = _account_get_user(payload.get("sub") or "", touch=True)
    if not user:
        raise HTTPException(status_code=401, detail="unknown account session")
    return user


def _apple_jwk_client():
    global _APPLE_JWK_CLIENT
    if _APPLE_JWK_CLIENT is None:
        import jwt as pyjwt
        _APPLE_JWK_CLIENT = pyjwt.PyJWKClient(APPLE_JWKS_URL, cache_keys=True)
    return _APPLE_JWK_CLIENT


def _apple_verify_identity_token(identity_token: str, audience=None):
    import jwt as pyjwt
    expected_audience = audience or list(APPLE_ID_AUDIENCES)
    if not expected_audience:
        raise HTTPException(status_code=500, detail="APPLE_ID_AUDIENCES is not configured")
    try:
        header = pyjwt.get_unverified_header(identity_token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("unexpected jwt header")
        signing_key = _apple_jwk_client().get_signing_key_from_jwt(identity_token)
        return pyjwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=APPLE_ID_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("apple_auth_invalid_token", error=type(e).__name__)
        raise HTTPException(status_code=401, detail="invalid apple identity token")


def _apple_web_config_error() -> str | None:
    required = {
        "APPLE_WEB_CLIENT_ID": APPLE_WEB_CLIENT_ID,
        "APPLE_WEB_REDIRECT_URI": APPLE_WEB_REDIRECT_URI,
        "APPLE_WEB_TEAM_ID": APPLE_WEB_TEAM_ID,
        "APPLE_WEB_KEY_ID": APPLE_WEB_KEY_ID,
        "APPLE_WEB_PRIVATE_KEY_PATH": APPLE_WEB_PRIVATE_KEY_PATH,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return "missing " + ", ".join(missing)
    parsed = urllib.parse.urlparse(APPLE_WEB_REDIRECT_URI)
    if parsed.scheme != "https" or not parsed.netloc:
        return "APPLE_WEB_REDIRECT_URI must be an https URL"
    key_path = Path(APPLE_WEB_PRIVATE_KEY_PATH)
    if not key_path.is_file():
        return "APPLE_WEB_PRIVATE_KEY_PATH is not readable"
    return None


def _apple_web_cleanup_locked(now: float | None = None) -> None:
    now = now or time.time()
    expired = [
        flow_id for flow_id, flow in _APPLE_WEB_FLOWS.items()
        if float(flow.get("expires_at") or 0) <= now
    ]
    for flow_id in expired:
        _APPLE_WEB_FLOWS.pop(flow_id, None)


def _apple_web_start_client_hash(request: Request) -> str:
    # cloudflared supplies this header. Direct production access is localhost
    # only, so an internet client cannot choose this value without traversing
    # Cloudflare first.
    client = (
        request.headers.get("cf-connecting-ip", "").strip()
        or _client_host(request)
        or "unknown"
    )
    return _short_hash(client)


def _apple_web_check_start_rate(request: Request) -> str:
    now = time.monotonic()
    client_hash = _apple_web_start_client_hash(request)
    with _APPLE_WEB_FLOW_LOCK:
        stale_before = now - APPLE_WEB_START_RATE_WINDOW
        for key in list(_APPLE_WEB_STARTS):
            attempts = _APPLE_WEB_STARTS[key]
            while attempts and attempts[0] <= stale_before:
                attempts.popleft()
            if not attempts:
                _APPLE_WEB_STARTS.pop(key, None)
        attempts = _APPLE_WEB_STARTS.setdefault(client_hash, collections.deque())
        if len(attempts) >= APPLE_WEB_START_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="too many sign-in attempts")
        attempts.append(now)
    return client_hash


def _apple_web_new_flow() -> dict:
    now = time.time()
    with _APPLE_WEB_FLOW_LOCK:
        _apple_web_cleanup_locked(now)
        if len(_APPLE_WEB_FLOWS) >= APPLE_WEB_FLOW_LIMIT:
            raise HTTPException(status_code=503, detail="too many active sign-in attempts")
        flow = {
            "flow_id": secrets.token_urlsafe(18),
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "poll_secret": secrets.token_urlsafe(32),
            "created_at": now,
            "expires_at": now + APPLE_WEB_FLOW_TTL,
            "status": "pending",
            "result": None,
            "error": None,
        }
        _APPLE_WEB_FLOWS[flow["flow_id"]] = flow
        return dict(flow)


def _apple_web_claim_flow(state: str) -> dict | None:
    now = time.time()
    with _APPLE_WEB_FLOW_LOCK:
        _apple_web_cleanup_locked(now)
        for flow in _APPLE_WEB_FLOWS.values():
            if hmac.compare_digest(str(flow.get("state") or ""), state):
                if flow.get("status") != "pending":
                    return None
                flow["status"] = "processing"
                return dict(flow)
    return None


def _apple_web_finish_flow(flow_id: str, status: str, result=None,
                           error: str | None = None) -> None:
    with _APPLE_WEB_FLOW_LOCK:
        flow = _APPLE_WEB_FLOWS.get(flow_id)
        if not flow or flow.get("status") != "processing":
            return
        flow["status"] = status
        flow["result"] = result
        flow["error"] = error


def _apple_web_client_secret() -> str:
    import jwt as pyjwt
    config_error = _apple_web_config_error()
    if config_error:
        raise RuntimeError(config_error)
    key_path = Path(APPLE_WEB_PRIVATE_KEY_PATH)
    if key_path.stat().st_size > 64 * 1024:
        raise RuntimeError("Sign in with Apple private key is unexpectedly large")
    private_key = key_path.read_text(encoding="utf-8")
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": APPLE_WEB_TEAM_ID,
            "iat": now - 5,
            "exp": now + 300,
            "aud": APPLE_ID_ISSUER,
            "sub": APPLE_WEB_CLIENT_ID,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": APPLE_WEB_KEY_ID},
    )


async def _apple_web_exchange_code(code: str) -> dict:
    import httpx
    client_secret = await asyncio.to_thread(_apple_web_client_secret)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            APPLE_WEB_TOKEN_URL,
            data={
                "client_id": APPLE_WEB_CLIENT_ID,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": APPLE_WEB_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
    try:
        payload = response.json()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Apple token endpoint returned invalid JSON") from e
    if response.status_code != 200:
        error_code = str(payload.get("error") or "unknown")
        _log_event("apple_web_token_exchange_failed",
                   status=response.status_code, apple_error=error_code[:80])
        raise RuntimeError("Apple authorization code validation failed")
    identity_token = str(payload.get("id_token") or "")
    if not identity_token:
        raise RuntimeError("Apple token response is missing id_token")
    return payload


def _apple_web_display_name(user_payload: dict) -> str | None:
    name = user_payload.get("name")
    if not isinstance(name, dict):
        return None
    parts = [
        str(name.get(key) or "").strip()
        for key in ("firstName", "lastName", "givenName", "familyName")
    ]
    # Apple uses firstName/lastName on the web. The second pair keeps this
    # tolerant of native-shaped fixtures without duplicating either value.
    if parts[0] or parts[1]:
        parts = parts[:2]
    else:
        parts = parts[2:]
    return " ".join(part for part in parts if part).strip() or None


def _apple_web_callback_page(kind: str) -> HTMLResponse:
    if kind == "success":
        title = "Pocket 登入完成"
        message = "已完成 Apple 登入，可以關閉這個頁面並回到 Pocket。"
    elif kind == "cancelled":
        title = "已取消登入"
        message = "你可以關閉這個頁面，回到 Pocket 後重新登入。"
    else:
        title = "登入未完成"
        message = "請關閉這個頁面，回到 Pocket 後重新嘗試。"
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
      font: 16px -apple-system, BlinkMacSystemFont, sans-serif;
      color: #15171a; background: #f5f6f8; }}
    main {{ width: min(34rem, calc(100% - 3rem)); }}
    h1 {{ margin: 0 0 .75rem; font-size: 1.75rem; letter-spacing: 0; }}
    p {{ margin: 0; color: #555b66; line-height: 1.6; }}
  </style>
</head>
<body><main><h1>{title}</h1><p>{message}</p></main></body>
</html>"""
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                       "base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


# canonical messages 的寫入版本計數(真事件推送,取代 SSE 每 2 秒重掃):
# _canon_add 成功寫入就 +1,followers 用 _canon_wait 盯版本、變了才重掃 DB。
# 純 int 比較、無鎖 — 就算極端併發丟失一次遞增,值仍有變化,喚醒不漏。
_CANON_VER: dict[str, int] = {}


def _canon_notify(session: str) -> None:
    _CANON_VER[session] = _CANON_VER.get(session, 0) + 1


async def _canon_wait(session: str, seen_ver: int) -> None:
    """等到該 session 的 canonical 版本離開 seen_ver(有新寫入)。0.2s 粒度
    的純記憶體輪詢 — 不碰 DB、不用 Condition(避開取消時的鎖重取競態);
    推送延遲 ≤0.2s,配合外層 wait_for(timeout=SSE_KEEPALIVE_SECS) 保持
    keepalive 節奏。"""
    while _CANON_VER.get(session, 0) == seen_ver:
        await asyncio.sleep(0.2)


# ── Sync engine P0:event_log 資料層(SYNC_ENGINE_REWRITE_PLAN §3/P0)────
# 單一事件日誌 + 游標訂閱的地基。這一層只提供 append / since 兩個原語;
# 誰來寫(P1 三來源鏡射)、誰來讀(P2 /app/v2/events)都在上層。
# _EVENT_VER 與 _CANON_VER 同款:純 int 版本號、無鎖,喚醒不漏即可。
_EVENT_VER: dict[str, int] = {}
# 記憶體去重快取:TG/cron 的接入是「重複掃描」式,同一批 external_id 每輪
# 都會再撞一次 DB 的 INSERT OR IGNORE;這層快取讓穩態掃描零寫入。重啟後
# 快取歸零沒關係 — DB 的 UNIQUE(external_id) 仍然守住冪等,只是第一輪
# 掃描多付幾次 no-op INSERT。
_EVENT_SEEN: dict[str, set] = {}
_EVENT_SEEN_CAP = 8192
# 全域版本計數(不分 session):/app/v2/events 省略 session 的全域訂閱
# (P3 契約 #2:App 首頁列表+未讀用單一條 SSE)靠這個喚醒,不用每 0.2s
# 掃整個 per-session dict。與 per-session 版同款:純 int、無鎖、喚醒不漏。
_EVENT_VER_ALL = 0


def _event_notify(session: str) -> None:
    global _EVENT_VER_ALL
    _EVENT_VER[session] = _EVENT_VER.get(session, 0) + 1
    _EVENT_VER_ALL += 1


async def _event_wait(session: str, seen_ver: int) -> None:
    """等到該 session 的 event_log 版本離開 seen_ver(有新事件)。與
    _canon_wait 同款 0.2s 純記憶體輪詢,不碰 DB、不用 Condition。"""
    while _EVENT_VER.get(session, 0) == seen_ver:
        await asyncio.sleep(0.2)


def _event_append(session: str, etype: str, payload: dict,
                  external_id: str | None = None) -> int:
    """Append 一筆事件,回傳全域 seq(=event_log.id);0 表示去重略過或寫入
    失敗。絕不 raise — 鏡射寫入掛在既有熱路徑上(_canon_add/_report_upsert/
    合併掃描),event_log 故障只能降級成「新路徑落後」,不准拖垮舊路徑。"""
    import sqlite3
    try:
        if external_id and external_id in _EVENT_SEEN.get(session, ()):
            return 0
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO event_log(session,type,external_id,payload,created_at) "
                "VALUES(?,?,?,?,?)",
                (session, etype, external_id,
                 json.dumps(payload, ensure_ascii=False), time.time()))
            seq = int(cur.lastrowid or 0) if cur.rowcount else 0
            con.commit()
            con.close()
            if external_id:
                seen = _EVENT_SEEN.setdefault(session, set())
                if len(seen) >= _EVENT_SEEN_CAP:
                    seen.clear()    # 粗略上限:清空後由 DB UNIQUE 繼續守冪等
                seen.add(external_id)
            if seq:
                _event_notify(session)
            return seq
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("event_append_failed", session=session, type=etype,
                   error=type(e).__name__, error_message=str(e)[:160])
        return 0


def _event_since(session: str, since_seq: int = 0, limit: int = 500) -> list[dict]:
    """撈 id > since_seq 的事件(即時 + 補洞共用同一條查詢)。信封對齊
    /app/v2/sessions/{id}/events 的 {seq,ts,type,data} 形狀。"""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT id,type,payload,created_at FROM event_log "
                "WHERE session=? AND id>? ORDER BY id LIMIT ?",
                (session, int(since_seq or 0), max(1, limit))).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("event_since_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])
        return []
    out = []
    for r in rows:
        try:
            data = json.loads(r[2] or "{}")
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_event_since", _exc, expected=True)
            data = {}
        if _is_hidden_message_event(data):
            continue
        out.append({"seq": r[0], "ts": r[3], "type": r[1], "data": data})
    return out


def _event_since_all(since_seq: int = 0, limit: int = 500) -> list[dict]:
    """全域版 _event_since:不分 session 撈 id > since_seq 的事件,餵
    /app/v2/events 省略 session 的全域訂閱。event_log.id 本來就是全域
    autoincrement,所以全域游標語意天然成立。信封比 per-session 版多帶
    session 欄位(App 端 SyncEvent 收 session|session_id 雙鍵)。
    SQL 限定 session IN 現任 PERSONAS — 落實拍板「v2 事件流只收 hermes
    人格 session」:被移除的 persona 與未來任何非人格寫入不會漏進全域流。"""
    import sqlite3
    sessions = list(PERSONAS)
    if not sessions:
        return []
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT id,session,type,payload,created_at FROM event_log "
                f"WHERE id>? AND session IN ({','.join('?' * len(sessions))}) "
                "ORDER BY id LIMIT ?",
                (int(since_seq or 0), *sessions, max(1, limit))).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("event_since_all_failed",
                   error=type(e).__name__, error_message=str(e)[:160])
        return []
    out = []
    for r in rows:
        try:
            data = json.loads(r[3] or "{}")
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_event_since_all", _exc, expected=True)
            data = {}
        if _is_hidden_message_event(data):
            continue
        out.append({"seq": r[0], "ts": r[4], "type": r[2],
                    "session": r[1], "data": data})
    return out


def _event_latest_seq(session: str) -> int:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT MAX(id) FROM event_log WHERE session=?",
                              (session,)).fetchone()
            con.close()
            return int(row[0] or 0)
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_event_latest_seq", _exc, expected=True)
        return 0


# ── Sync engine P1:三來源鏡射(SYNC_ENGINE_REWRITE_PLAN §4 P1)─────────
# App 訊息(_canon_add)/ TG(state.db 合併掃描)/ cron 晨報(_report_upsert)
# 都額外鏡射一份進 event_log。雙寫過渡:現有讀取路徑一律不動,event_log
# 在 P2 之前只做影子累積。
#
# 鍵設計:{source}:{app可見id}:{sha1(role|status|content)[:16]}。
# - 同一則訊息重複掃到 → 同鍵 → 去重(TG/cron 是重複掃描式接入)
# - 同 id 但內容/狀態變了(報告改稿、訊息補寫)→ 新鍵 → 追加一筆新的
#   message.upsert 事件,client 端以 message id 做 last-write-wins 覆蓋
_EVENT_SYNC_TS: dict[str, float] = {}
_EVENT_SYNC_MIN_SECS = float(os.environ.get("POCKET_EVENT_SYNC_SECS", "10"))


def _event_msg_key(m: dict) -> str:
    basis = f"{m.get('role')}|{m.get('status')}|{m.get('content') or ''}"
    h = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{m.get('source') or 'app'}:{m.get('id')}:{h}"


def _event_mirror_messages(session: str, msgs: list) -> int:
    """把一批 app-shape 訊息 dict 鏡射進 event_log(冪等,靠 external_id
    去重)。回傳真正新寫入的筆數。絕不 raise。"""
    n = 0
    for m in msgs or []:
        try:
            key = _event_msg_key(m)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_event_mirror_messages", _exc, expected=True)
            continue
        if _event_append(session, "message.upsert", {"message": m},
                         external_id=key):
            n += 1
    return n


def _event_sync_session(session: str, limit: int = 200,
                        force: bool = False,
                        min_secs: float | None = None) -> None:
    """把 TG(state.db)+ cron 晨報拉進 event_log 的主動同步(P2 SSE 端點
    在訂閱期間週期呼叫)。合併/清洗/去重全部沿用 _hp_merged_messages —
    鏡射就掛在它的回傳路徑上,這裡只負責觸發 + 節流(同 session 至多每
    _EVENT_SYNC_MIN_SECS 掃一次,多個訂閱者共享)。min_secs 可換小節流:
    statedb watcher 喚醒路徑用 0.4s(配合呼叫端 0.5s 去抖)— 要穿越 10s
    週期節流即時拉,但多訂閱者同時醒時仍只掃一次。"""
    now = time.monotonic()
    floor = _EVENT_SYNC_MIN_SECS if min_secs is None else min_secs
    if not force and now - _EVENT_SYNC_TS.get(session, 0.0) < floor:
        return
    _EVENT_SYNC_TS[session] = now
    try:
        _hp_merged_messages(session, limit)   # 鏡射在合併函式內完成
    except Exception as e:  # noqa: BLE001
        _log_event("event_sync_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])


# ── TG/cron → state.db 寫入即時偵測(#tg-instant-sync)───────────────────
# 根因:App 自己送出/收到的訊息走上面 _canon_notify/_canon_wait,寫入當下
# 就 bump 版本、~0.2s 內喚醒 follower。但 Telegram 端訊息與 cron 晨報是
# **Hermes 官方 gateway 進程**寫進各 persona 自己的 `<home>/state.db`(WAL
# mode),那條寫入路徑在 hermes_cli 官方套件內部 —— 鐵律規定不准碰內核,
# 所以完全不能掛 hook/callback 在寫入那一刻觸發。
#
# 這裡改用「唯讀輕量輪詢」繞過去:WAL mode 下,真正的寫入落在
# `<home>/state.db-wal`(checkpoint 前主 db 檔案本身不太動),只要每
# ~0.15s 對這個檔案做一次 os.stat()(不開檔、不連 sqlite、不解析內容),
# mtime/size 一變就代表「剛剛有新內容寫進去」,立刻 bump 一個獨立的版本
# 計數器喚醒對應 persona 的 follower 去重掃 `_hp_merged_messages`。
# 這跟 `_canon_notify` 是同一種模式(純 int 版本號、無鎖),只是觸發源從
# 「我們自己呼叫 _canon_add」換成「別人的程序寫了這個檔案」。
#
# 30s 保險絲(_hp_canon_follower 裡的 timeout=30.0)完全保留 —— 這個 stat
# watcher 是「加速觸發」疊加在上面,不是取代:watcher 掛掉/漏抓(例如
# checkpoint 時序恰好卡在兩次 stat 中間、mtime 精度不足撞期）,30s 週期
# 還是會補上,同步不會因為單一機制失效就整個停擺。
_STATEDB_VER: dict[str, int] = {}
_STATEDB_VER_ALL = 0    # 全域計數,配 _EVENT_VER_ALL(全域訂閱喚醒用)
_STATEDB_STAT_CACHE: dict[str, tuple] = {}   # session -> (path, mtime_ns, size)
_STATEDB_POLL_SECS = float(os.environ.get("POCKET_STATEDB_POLL_SECS", "0.15"))


def _statedb_notify(session: str) -> None:
    global _STATEDB_VER_ALL
    _STATEDB_VER[session] = _STATEDB_VER.get(session, 0) + 1
    _STATEDB_VER_ALL += 1


def _statedb_stat_key(home: str) -> tuple:
    """只用 os.stat(),唯讀、不開檔、不連 DB。WAL 模式下實際寫入處是
    state.db-wal;沒有的話(已 checkpoint 或非 WAL)退回 state.db 本身。
    讀不到任何一個就回傳 (None, 0, 0),呼叫端據此跳過該 session 這一輪。"""
    for name in ("state.db-wal", "state.db"):
        p = os.path.join(home, name)
        try:
            st = os.stat(p)
            return (p, st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return (None, 0, 0)


async def _state_db_watcher_loop() -> None:
    """常駐背景迴圈:每輪對每個 persona home 的 state.db(-wal) 做一次
    os.stat(),偵測到 mtime/size 變動就判定「TG/cron 剛寫入」,bump
    `_STATEDB_VER` 喚醒 `_hp_canon_follower` 立刻重掃,不必等 30s 保險絲。
    例外全吞:這條 loop 死掉不影響既有的 30s 兜底路徑,只是退回原本
    的延遲,不會讓同步整個停擺(鐵律 #4)。"""
    while True:
        try:
            for session, (_, home) in list(PERSONAS.items()):
                key = _statedb_stat_key(home)
                path = key[0]
                if path is None:
                    continue
                prev = _STATEDB_STAT_CACHE.get(session)
                if prev is not None and prev[0] == path and (prev[1] != key[1] or prev[2] != key[2]):
                    _statedb_notify(session)
                _STATEDB_STAT_CACHE[session] = key
        except Exception as e:  # noqa: BLE001
            _log_event("state_db_watcher_error", error=type(e).__name__,
                       error_message=str(e)[:160])
        await asyncio.sleep(_STATEDB_POLL_SECS)


async def _canon_or_statedb_wait(session: str, seen_canon_ver: int,
                                 seen_state_ver: int) -> None:
    """`_canon_wait` 的擴充版:canonical 版本 *或* state.db stat 版本任一
    變動就返回。同款 0.2s 純記憶體輪詢,無鎖、無 Condition。"""
    while (_CANON_VER.get(session, 0) == seen_canon_ver
           and _STATEDB_VER.get(session, 0) == seen_state_ver):
        await asyncio.sleep(0.2)


async def _event_or_statedb_wait(session: str, seen_ver: int,
                                 seen_state_ver: int) -> None:
    """`_event_wait` 的擴充版(v2 事件迴圈用):event_log 版本 *或*
    state.db stat 版本任一變動就返回。statedb 醒 = TG/cron 剛寫入但還沒
    鏡射進 event_log,呼叫端要立刻 _event_sync_session 把它拉進來 —
    v2 訂閱者的 TG 延遲從節流上限(10s)壓到 ~0.4s。"""
    while (_EVENT_VER.get(session, 0) == seen_ver
           and _STATEDB_VER.get(session, 0) == seen_state_ver):
        await asyncio.sleep(0.2)


async def _event_or_statedb_wait_all(seen_ver: int,
                                     seen_state_ver: int) -> None:
    """全域版 _event_or_statedb_wait(/app/v2/events 省略 session 的訂閱用):
    任何 session 的 event_log 或 state.db 有動靜就返回。盯兩個全域 int,
    不掃 per-session dict。"""
    while (_EVENT_VER_ALL == seen_ver
           and _STATEDB_VER_ALL == seen_state_ver):
        await asyncio.sleep(0.2)


_QUEUE_ACK_RE = re.compile(r"^\s*Queued for the next turn\.(\s*\(\d+ queued\))?\s*$")


def _is_queue_ack(text: str) -> bool:
    """persona runtime(ACP)忙碌時的排隊回執。它是狀態不是回覆 —— 落正典會
    讓聊天頁一排「Queued for the next turn.」泡泡(2026-08-04 xcash 實抓 ×6),
    真回覆本來就會在排到的回合帶出。只認**整則恰為回執**,嵌在長文中不動。"""
    return bool(_QUEUE_ACK_RE.match(text or ""))


def _canon_add(session: str, role: str, content: str, attachments=None,
               mid: str | None = None, status: str = "done",
               client_id: str | None = None, created_at: float | None = None,
               push: bool = True) -> tuple[str, bool]:
    # created_at:TG 鏡像 ingest 帶事件原始時間戳 —— 重放同一事件落同一
    # (mid, ts),不會把訊息「頂」到現在。push=False:TG 端已送達的回覆
    # 不再推播(否則同一句話 TG 通知 + Pocket 通知各一次)。
    import sqlite3
    mid = mid or uuid.uuid4().hex
    now = created_at if created_at is not None else time.time()
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("INSERT OR REPLACE INTO messages"
                        "(id,session,role,content,attachments,created_at,status,client_id) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (mid, session, role, content, json.dumps(attachments or [], ensure_ascii=False),
                         now, status, client_id))
            con.commit()
            con.close()
            _canon_notify(session)
            # Sync engine P1:App 訊息寫入點順便鏡射進 event_log(雙寫過渡)。
            # payload 形狀對齊 _canon_messages 的輸出,client 兩邊看到同一種訊息。
            _event_mirror_messages(session, [{
                "id": mid, "role": role, "content": content,
                "attachments": attachments or [], "ts": now, "status": status,
                "client_id": client_id, "source": "app"}])
            # P1-3:人格完成一則回覆 → 推播把你叫回 app(前景由 app willPresent 抑制)。
            if push and role == "assistant" and status == "done":
                _push_persona_reply(session, content)
            return mid, True
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("canonical_write_failed",
                   session=session, role=role, status=status,
                   client_id_hash=_short_hash(client_id),
                   content_chars=len(content or ""),
                   attachment_count=len(attachments or []),
                   error=type(e).__name__, error_message=str(e)[:160])
    return mid, False


def _canon_add_retry(session: str, role: str, content: str, attachments=None,
                     mid: str | None = None, status: str = "done",
                     client_id: str | None = None) -> tuple[str, bool]:
    """_canon_add + one retry (issue #9): a dropped canonical write makes the
    turn invisible to replay/idempotency, so it's worth a second attempt."""
    mid, ok = _canon_add(session, role, content, attachments, mid=mid,
                         status=status, client_id=client_id)
    if not ok:
        _log_event("canonical_write_retry", session=session, role=role,
                   client_id_hash=_short_hash(client_id))
        mid, ok = _canon_add(session, role, content, attachments, mid=mid,
                             status=status, client_id=client_id)
    return mid, ok


def _canon_reply_for_client(session: str, client_id: str):
    """If this logical send already produced a recorded assistant reply (e.g. the
    first attempt succeeded server-side but the client's network dropped), return
    it so a retry replays it instead of re-running the turn."""
    import sqlite3
    if not client_id:
        return None
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT content FROM messages WHERE session=? AND client_id=? "
                "AND role='assistant' AND status='done' AND content IS NOT NULL AND content!='' "
                "ORDER BY created_at DESC LIMIT 1", (session, client_id)).fetchone()
            con.close()
            return row[0] if row else None
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_canon_reply_for_client", _exc, expected=False, session=session)
        return None


def _steps_stripped(t: str) -> str:
    """訊息正文的壓重正規化:剝掉〈🔧 執行步驟〉/〈💭 思考〉那類 `<details>` 附錄。

    同一則訊息在兩個來源的形狀不同 —— canonical 那份帶附錄、Hermes state.db 那份
    是乾淨正文 —— 所以「是不是同一則」只能在剝完附錄後比。#37 的壓重鍵
    (role, 本函式輸出, ±600s) 就建立在這上面。

    唯一實作:`/app/v1/messages`、人格卡片流、以及 app→TG 反向鏡射(送進 TG 的正文
    要與 state.db 側同形,將來萬一被回收才對得上壓重鍵)共用同一份 —— 三處各自
    正規化就是雙氣泡回歸的入口。
    """
    body = re.sub(r"<details>.*?</details>", "", t or "", flags=re.S).strip()
    # app→TG 鏡射會在 user 那句前面加 📱 標記(讓 TG 那頭看得出是從手機說的)。
    # 壓重時當它不存在:Telegram 不會把 bot 自己送出的訊息當成 Update 回送,所以
    # 正常情況下 state.db 根本不會有這份副本;但這條不變式不該建立在別人的實作
    # 細節上 —— 萬一哪天真的被回收進來,帶標記的副本仍要能和 canonical 的原文
    # 對上,否則就是一顆多出來的泡泡。
    if body.startswith(tg_outbound.USER_PREFIX):
        body = body[len(tg_outbound.USER_PREFIX):].lstrip()
    return body


def _dedup_norm(t: str) -> str:
    """雙源壓重的比對鍵:剝附錄(_steps_stripped)後再拆掉**所有空白**。
    canonical 與 state.db 兩份落稿常見開頭多換行/空白微漂,字面比對就漏。"""
    return re.sub(r"\s+", "", _steps_stripped(t or ""))


def _session_turn_in_flight(session: str) -> bool:
    """本 session 是否有 app 回合進行中 —— 活 turn 檢疫的判定源。

    為什麼要檢疫(2026-08-04 晚間實抓):hermes agent 在長回合中會把進度短句
    逐則發到 TG(state.db),但 canonical 總結要等回合收尾才落地 —— 收尾前
    覆蓋壓重無從比對,進度句會先投遞到裝置本地庫,之後 server 端再怎麼壓重
    也收不回來(手機上就是「短句講過、總結又整段重講」)。回合進行中先把
    TG 側 assistant 新訊息扣住;收尾後 canonical 總結蓋得掉的自然被壓,
    蓋不掉的(真訊息)照常放行,只延遲不丟失。"""
    for (s, _cid), entry in list(_APP_TURN_INFLIGHT.items()):
        if s != session:
            continue
        task = entry.get("task")
        if task is not None and not task.done():
            return True
    return False


def _session_turn_started_at(session: str) -> float | None:
    """本 session 目前進行中回合裡最早的**牆鐘**起始時戳(沒有活回合 → None)。

    活 turn 檢疫要擋的是「這個回合的回覆」的 TG 進度句副本 —— 那必然發生在
    回合開始**之後**。以前檢疫窗寫死「距今 1h 內全部 TG assistant」(見呼叫端),
    連回合開始前、早就是既定歷史的訊息也一起扣住,使用者體感是「一小時內講過
    的話開著回合時全消失、收尾才回來」。改用回合起始時戳當下界,只檢疫真正可能
    是本回合重複的那些。"""
    starts = [e.get("wall") for (s, _cid), e in list(_APP_TURN_INFLIGHT.items())
              if s == session and e.get("task") is not None
              and not e["task"].done() and e.get("wall")]
    return min(starts) if starts else None


_TG_QUARANTINE_GRACE = 120.0   # TG 寫入時戳與回合牆鐘起始的時鐘偏移容差


def _tg_assistant_in_quarantine(turn_started_at, role, ts) -> bool:
    """回合進行中,只扣住「回合起始之後(含 grace)」的 TG assistant 訊息 ——
    那才可能是本回合回覆的進度句副本。turn_started_at=None(無活回合)或回合
    起始前的既定歷史一律放行。取代舊的「距今 1h 內全部 TG assistant」窗。"""
    if turn_started_at is None or role != "assistant":
        return False
    return (ts or 0) >= turn_started_at - _TG_QUARANTINE_GRACE


def _dual_source_dup(body_norm: str, role: str, ts: float, canon_recent) -> bool:
    """canonical×state.db 雙寫壓重(同 role、±600s 窗)。

    先走完全相等(便宜快路);對不上再走**覆蓋率 ≥0.90**(短方內容按序出現
    在長方的比例;生產實對 0.993/1.000,對齊式 ratio 只有 0.69-0.86 不可用) —— 兩份落稿
    會有**措辭微漂**(2026-08-04 xcash/袁方實例:「對話上下文」vs「上下文」、
    開頭多兩個換行),完全相等永遠對不上 → 同一句在 app 畫面兩顆氣泡,
    這正是「人格常回覆重複內容」的病根。長度差 >20% 先短路,只比前 400 字,
    不白付 SequenceMatcher;canon_recent 量級 = 單頁 limit,成本可忽略。
    純 TG 舊訊息(canonical 無副本)與相隔久遠的同文照舊保留。"""
    if not body_norm:
        return False
    for cts, r, c in canon_recent:
        if r != role or abs(ts - cts) >= 900 or not c:
            continue
        if c == body_norm:
            return True
        # 模糊後備用**覆蓋率**,不是對齊相似度:生產實對顯示兩份是「同核心文
        # + 前綴/後綴增生」(canonical 多開場白/附錄、TG 被長度截尾),對齊窗
        # ratio 會把增生當相異扣到 0.69-0.75;covering(短方內容按序出現在長方
        # 的比例)才是對的度量 — xcash 07/25 = 0.993、袁方 07/30 = 1.000。
        # 守門只留「短方 ≥24 字」:生產大宗(2026-08-04 xcash 16:41-17:29 整批)
        # 是 TG 側中途進度短句 ×N + canonical 一則長總結全包 —— 長度比 10-30×,
        # 任何長度比守門都會放掉它們。只壓 tg 副本、canonical 原句永在,真引用
        # 場景(長文引自己先前短句)畫面仍完整,不會消字。比對上限 600+1800:
        # 長總結可達 3000 字,短句可能落在後段,長方窗要夠深。
        s, l = (body_norm, c) if len(body_norm) <= len(c) else (c, body_norm)
        if len(s) >= 24:
            sm = difflib.SequenceMatcher(None, s[:600], l[:1800])
            cover = sum(b.size for b in sm.get_matching_blocks()) / min(len(s), 600)
            if cover >= 0.90:
                return True
    return False


def _tg_mirror_out(session: str, role: str, raw_body: str, mid: str) -> None:
    """app→TG 反向鏡射的唯一呼叫點(#32)。預設關閉,且永不影響回合。

    正文先過 `_steps_stripped`:送進 TG 的與 state.db 側的乾淨正文同形,#37 的
    壓重鍵因此依然對得上(詳見 `tg_outbound` 模組 docstring 的去重不變式)。
    """
    if session not in PERSONAS:
        return
    _, home = PERSONAS[session]
    tg_outbound.mirror_soon(home, session, role, _steps_stripped(raw_body), mid,
                            log=_log_event)


def _canon_messages(session: str, limit: int = 200):
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT id,role,content,attachments,created_at,status,client_id FROM messages "
                               "WHERE session=? ORDER BY created_at DESC LIMIT ?", (session, limit)).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_canon_messages", _exc, expected=False, session=session)
        return []
    rows.reverse()
    # 歷史殘留的排隊回執(#70 只擋新寫入)讀取時一併過濾 —— 與雙源壓重同款
    # 「讀取端治歷史」策略,DB 保持唯讀不清資料。
    return [{"id": r[0], "role": r[1], "content": r[2],
             "attachments": json.loads(r[3] or "[]"), "ts": r[4],
             "status": r[5], "client_id": r[6], "source": "app"}
            for r in rows if not (r[1] == "assistant" and _is_queue_ack(r[2] or ""))]


def _app_message_seq(m: dict) -> int:
    try:
        return int(float(m.get("ts") or 0) * 1000)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_app_message_seq", _exc, expected=True)
        return int(time.time() * 1000)


def _app_message_event(m: dict) -> dict:
    return {"seq": _app_message_seq(m), "type": "message.upsert",
            "message_id": m.get("id"), "payload": {"message": m}}


def _canonical_reply_failure(reply: str) -> tuple[str, str, str] | None:
    """Classify bridge-generated terminal replies for recovery clients.

    A timeout is persisted as a canonical assistant message so every surface
    sees it, but that does not make the turn successful. Returning `done` here
    made Pocket hide its retry control and replay the same timed-out client id.
    """
    text = (reply or "").lower()
    if ("回合逾時" in reply or "回應逾時" in reply
            or "伺服器端 5 分鐘" in reply or "turn timed out" in text):
        return "timeout", "回合逾時", "persona turn timed out"
    return None


def _app_turn_status(session: str, client_id: str | None = None,
                     acp_busy: bool = False) -> dict:
    """Current app-turn recovery status for the mobile client.

    The POST /app/v1/messages stream can legitimately be detached by a mobile
    network drop. This status surface lets Pocket recover by stable client_id
    without re-running the persona turn.
    """
    now = time.monotonic()
    entry = None
    if client_id:
        entry = _APP_TURN_INFLIGHT.get((session, client_id))
    state = entry.get("state") if entry else {}
    task = entry.get("task") if entry else None
    acc = (state or {}).get("acc") or ""
    canonical_reply = _canon_reply_for_client(session, client_id) if client_id else None
    runner_error = (state or {}).get("runner_error") or (state or {}).get("stream_error") or ""
    canonical_failure = _canonical_reply_failure(canonical_reply or "")
    in_flight = bool(task is not None and not task.done())
    if canonical_failure:
        turn_state, label, canonical_error = canonical_failure
    elif canonical_reply:
        turn_state, label = "done", "已同步"
        canonical_error = ""
    elif in_flight:
        turn_state = "streaming" if acc else ("queued" if acp_busy else "running")
        label = (state or {}).get("step_label") or ("思考中" if acc else "處理中")
        canonical_error = ""
    elif task is not None and task.done():
        # The background task has ended and canonical lookup still found no
        # reply. There is nothing left for a detached client to wait for: mark
        # it retryable instead of reporting stream_detached forever.
        turn_state, label = "failed", "回合未能保存"
        canonical_error = runner_error or (
            "persona reply was not persisted" if acc else "persona returned no reply"
        )
    elif acp_busy:
        turn_state, label = "running", "處理中"
        canonical_error = ""
    else:
        turn_state, label = "idle", "閒置"
        canonical_error = ""
    status_error = canonical_error or ("" if canonical_reply else runner_error)
    elapsed = int(now - entry["ts"]) if entry and entry.get("ts") else None
    return {"session": session, "state": turn_state, "label": label,
            "in_flight": in_flight, "acp_busy": acp_busy,
            "elapsed_seconds": elapsed, "stale_seconds": elapsed,
            "output_chars": len(acc), "canonical_reply": bool(canonical_reply),
            "canonical_reply_chars": len(canonical_reply or ""),
            "error": status_error or None}


# ───────────────────── SUBSESSIONS persistence (issue #5) ───────────────────
_SUB_OUTPUT_JSON_CAP = 2 * 1024 * 1024   # ~2MB persisted transcript per sub
_SUB_TRUNC_MARKER = ("text", "_(前段已截斷)_\n\n")


def _sub_output_json(output: list) -> str:
    """Serialize a sub's transcript, truncating OLDEST items to stay ≤ ~2MB."""
    items = [[k, v] for k, v in (output or [])]
    js = json.dumps(items, ensure_ascii=False)
    if len(js) <= _SUB_OUTPUT_JSON_CAP:
        return js
    while items and len(js) > _SUB_OUTPUT_JSON_CAP:
        drop = max(1, len(items) // 10)      # shed in chunks, not one-by-one
        items = items[drop:]
        js = json.dumps([list(_SUB_TRUNC_MARKER)] + items, ensure_ascii=False)
    return js


def _subsession_persist(sid: str) -> bool:
    """Flush one SUBSESSIONS entry to canonical.db (insert-or-replace)."""
    import sqlite3
    sub = SUBSESSIONS.get(sid)
    if not sub:
        return False
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("INSERT OR REPLACE INTO subsessions"
                        "(sid,name,parent,tool,status,cwd,worktree,cc_session,"
                        "last_user,last_at,output_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, sub.get("name"), sub.get("parent"), sub.get("tool"),
                         sub.get("status"), sub.get("cwd"), sub.get("worktree"),
                         sub.get("cc_session"), sub.get("last_user"),
                         sub.get("lastAt") or time.time(),
                         _sub_output_json(sub.get("output"))))
            con.commit()
            con.close()
            return True
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("subsession_persist_failed", sid=sid,
                   error=type(e).__name__, error_message=str(e)[:160])
        return False


def _subsessions_load():
    """Rebuild SUBSESSIONS from canonical.db on startup. Anything that was
    status=running when the bridge died is marked interrupted, with a
    transcript note, so the app shows an honest state instead of a dead row."""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            rows = con.execute(
                "SELECT sid,name,parent,tool,status,cwd,worktree,cc_session,"
                "last_user,last_at,output_json FROM subsessions").fetchall()
            interrupted = [r[0] for r in rows if r[4] == "running"]
            if interrupted:
                con.executemany("UPDATE subsessions SET status='interrupted' WHERE sid=?",
                                [(sid,) for sid in interrupted])
                con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("subsessions_load_failed",
                   error=type(e).__name__, error_message=str(e)[:160])
        return
    loaded = 0
    for (sid, name, parent, tool, status, cwd, worktree, cc_session,
         last_user, last_at, output_json) in rows:
        try:
            output = [(k, v) for k, v in json.loads(output_json or "[]")]
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_subsessions_load", _exc, expected=True)
            output = []
        if status == "running":
            status = "interrupted"
            output.append(("text", "\n\n_(bridge 重啟,行程已中斷,可追問續跑)_\n"))
        SUBSESSIONS[sid] = {
            "name": name, "parent": parent, "tool": tool, "status": status,
            "cwd": cwd, "worktree": worktree, "cc_session": cc_session,
            "last_user": last_user, "lastAt": last_at, "proc": None,
            "output": output,
        }
        loaded += 1
    if loaded:
        _log_event("subsessions_loaded", count=loaded,
                   interrupted=len(interrupted))


_WORK_ORDER_PREFIX = {
    "xcash": "XW",
    "pantianqing": "PT",
    "shuijing": "SJ",
    "yuanfang": "YF",
}

_PROVIDER_ALIASES = {
    "codex": "codex",
    "cx": "codex",
    "codex-app": "codex",
    "codex_app": "codex",
    "claude": "claude_code",
    "claude-code": "claude_code",
    "claude_code": "claude_code",
    "cc": "claude_code",
}


def _normalise_provider(raw: str | None) -> str:
    key = (raw or "codex").strip().lower()
    provider = _PROVIDER_ALIASES.get(key)
    if not provider:
        raise HTTPException(status_code=400, detail="provider must be codex/cx or claude_code/cc")
    return provider


def _new_work_order(parent_persona: str, task_code: str = "", subtask_code: str = "") -> str:
    """Work order v2: AGENT-TASK-SUBTASK-YYYYMMDD-ID4

    AGENT    : persona prefix (XW/PT/SJ/YF), same as v1.
    TASK     : project/task code, shared across every delegation under the
               same initiative (e.g. POCKETCONN) so `grep`/filter by prefix
               finds the whole thread of work, including retries.
    SUBTASK  : this specific delegation's concrete deliverable (e.g.
               APPLELOGIN). Different subtasks under the same task share the
               TASK segment but not the SUBTASK segment.
    YYYYMMDD : full 8-digit date (v1 only had MMDD, which collides across
               years — fixed here).
    ID4      : 4 hex chars, collision guard.

    Falls back to a generic TASK/SUBTASK of "GEN" if the caller doesn't supply
    one (keeps the endpoint usable without breaking older callers), but new
    callers should always pass both — see docs/DELEGATION_CONTROL_PLANE.md.
    """
    prefix = _WORK_ORDER_PREFIX.get(parent_persona, "HW")
    day = datetime.now().astimezone().strftime("%Y%m%d")
    task = _work_order_segment(task_code, fallback="GEN", max_len=16)
    subtask = _work_order_segment(subtask_code, fallback="TASK", max_len=20)
    return f"{prefix}-{task}-{subtask}-{day}-{secrets.token_hex(2).upper()}"


def _work_order_segment(text: str, fallback: str, max_len: int) -> str:
    """Slugify a work-order TASK/SUBTASK segment: uppercase alnum only, no
    separators (the segment boundaries are the dashes between fields, so an
    embedded dash would silently shift field parsing for anyone splitting on
    '-')."""
    slug = re.sub(r"[^A-Za-z0-9]+", "", (text or "")).upper()
    return (slug or fallback)[:max_len]


def _delegation_display_title(row: dict) -> str:
    wo = row.get("work_order") or "WORK"
    title = (row.get("title") or row.get("objective") or "").strip()
    return f"{wo} - {title[:80]}" if title else wo


def _safe_session_slug(text: str, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-")
    return (slug or fallback)[:48]


def _normalise_workdir(raw: str | None, *, create: bool = False) -> str:
    home = os.path.realpath(os.path.expanduser("~"))
    wd = os.path.realpath(os.path.expanduser(raw or HOME_ROOT))
    if not (wd == home or wd.startswith(home + os.sep)):
        raise HTTPException(status_code=400, detail="cwd must be under home")
    if wd == home:
        raise HTTPException(status_code=400, detail="pick a sub-folder, not your home directory")
    if create:
        try:
            os.makedirs(wd, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"cannot create cwd: {e}")
    elif not os.path.isdir(wd):
        raise HTTPException(status_code=400, detail="cwd does not exist")
    return wd


def _delegation_prompt(work_order: str, parent_persona: str, title: str,
                       objective: str, cwd: str, body: dict) -> str:
    lines = [
        f"[工號 {work_order}] {title}",
        "",
        "你是由 Hermes delegation control plane 派出的開發子程序。",
        f"- 父人格: {parent_persona}",
        f"- 工作目錄: {cwd}",
        f"- 目標: {objective}",
        "",
        "運作規則:",
        "- 每次回覆第一行保留工號，方便 Telegram、Pocket、官方 app 三邊對照。",
        "- 先提出可驗收計畫，再實作；不要改無關檔案。",
        "- 若涉及 production 寫入、正式通知、正式發文或真實使用者狀態變更，先停下等放行。",
        "- 完成時回報修改檔案、驗證命令與輸出、殘餘風險、下一步。",
        f"- 完成或到達里程碑時,執行 `studio-delegate report {work_order} \"<成果摘要>\" --status done`"
        "(進度回報用 --status running)把結果回流給派工方;此指令已在 PATH。",
    ]
    for label, key in (("規格文件", "spec_path"), ("限制", "constraints"),
                       ("驗收方式", "acceptance"), ("交接資訊", "handoff")):
        val = (body.get(key) or "").strip() if isinstance(body.get(key), str) else body.get(key)
        if val:
            lines.append(f"- {label}: {val}")
    return "\n".join(lines).strip()


def _delegation_takeover(row: dict) -> dict:
    provider = row.get("provider") or ""
    if provider == "codex":
        thread_id = row.get("codex_thread_id") or row.get("provider_session_id") or ""
        return {
            "pocket": {
                "surface": "bridge",
                "session_id": f"codex:{thread_id}" if thread_id else "",
                "input_endpoint": f"/codexsessions/{thread_id}/input" if thread_id else "",
                "stream_endpoint": f"/codexsessions/{thread_id}/stream" if thread_id else "",
                "history_endpoint": f"/codexsessions/{thread_id}/history" if thread_id else "",
                "status_endpoint": f"/codexsessions/{thread_id}/status" if thread_id else "",
                "interrupt_endpoint": f"/codexsessions/{thread_id}/interrupt" if thread_id else "",
            },
            "official": {
                "surface": "codex_app_server_thread",
                "thread_id": thread_id,
                "title": _delegation_display_title(row),
                "resume_hint": "Codex official surfaces should resume the native thread id/title created by codex app-server.",
            },
        }
    if provider == "claude_code":
        name = row.get("cc_session_name") or row.get("provider_session_id") or ""
        return {
            "pocket": {
                "surface": "bridge",
                "session_id": f"claude_code:{name}" if name else "",
                "input_endpoint": f"/ccsessions/{name}/input" if name else "",
                "stream_endpoint": f"/ccsessions/{name}/stream" if name else "",
                "history_endpoint": f"/ccsessions/{name}/history" if name else "",
                "status_endpoint": f"/ccsessions/{name}/status" if name else "",
                "interrupt_endpoint": f"/ccsessions/{name}/interrupt" if name else "",
                "key_endpoint": f"/ccsessions/{name}/key" if name else "",
            },
            "official": {
                "surface": "claude_code_remote_control",
                "session_name": name,
                "workdir": row.get("cwd") or "",
                "resume_hint": "Open/attach the same Claude Code remote-control session name or ccsess tmux session.",
            },
        }
    return {"pocket": {}, "official": {}}


def _delegation_public(row, runtime_status: str | None = None) -> dict:
    d = dict(row)
    try:
        meta = json.loads(d.get("meta") or "{}")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_delegation_public", _exc, expected=True)
        meta = {}
    d["display_title"] = _delegation_display_title(d)
    d["status"] = runtime_status or d.get("status") or "created"
    d["meta"] = meta
    d["takeover"] = _delegation_takeover(d)
    return d


def _delegation_rows(limit: int = 50, parent_persona: str = "", status: str = "",
                      task_code: str = "") -> list:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            con.row_factory = sqlite3.Row
            where, args = [], []
            if parent_persona:
                where.append("parent_persona=?")
                args.append(parent_persona)
            if status:
                where.append("status=?")
                args.append(status)
            if task_code:
                where.append("task_code=?")
                args.append(task_code.strip().upper())
            sql = "SELECT * FROM delegations"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            args.append(max(1, min(limit, 200)))
            rows = con.execute(sql, args).fetchall()
            con.close()
            return rows
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_delegation_rows", _exc, expected=True)
        return []


def _delegation_get(delegation_id: str):
    import sqlite3
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM delegations WHERE id=? OR work_order=?",
                          (delegation_id, delegation_id)).fetchone()
        con.close()
        return row
    finally:
        con.close()


def _delegation_insert(row: dict) -> None:
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        con.execute("""INSERT INTO delegations
            (id, work_order, parent_persona, parent_session, created_via, provider,
             title, objective, cwd, status, provider_session_id, codex_thread_id,
             cc_session_name, created_at, updated_at, last_error, meta,
             task_code, subtask_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row.get("id"), row.get("work_order"), row.get("parent_persona"),
             row.get("parent_session"), row.get("created_via"), row.get("provider"),
             row.get("title"), row.get("objective"), row.get("cwd"), row.get("status"),
             row.get("provider_session_id"), row.get("codex_thread_id"),
             row.get("cc_session_name"), row.get("created_at"), row.get("updated_at"),
             row.get("last_error"), json.dumps(row.get("meta") or {}, ensure_ascii=False),
             row.get("task_code"), row.get("subtask_code")))
        con.commit()
        con.close()
    finally:
        con.close()


def _delegation_update(delegation_id: str, **fields) -> None:
    if not fields:
        return
    import sqlite3
    allowed = {"status", "updated_at", "last_error", "provider_session_id",
               "codex_thread_id", "cc_session_name", "meta"}
    sets, args = [], []
    for key, val in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        if key == "meta" and not isinstance(val, str):
            val = json.dumps(val or {}, ensure_ascii=False)
        args.append(val)
    if not sets:
        return
    args.append(delegation_id)
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        con.execute(f"UPDATE delegations SET {', '.join(sets)} WHERE id=?", args)
        con.commit()
        con.close()
    finally:
        con.close()


async def _delegation_runtime_status(row) -> str:
    d = dict(row)
    provider = d.get("provider") or ""
    if provider == "codex":
        tid = d.get("codex_thread_id") or d.get("provider_session_id") or ""
        if tid:
            runtime = CODEX_APP.runtime_status(tid, d.get("status") or "")
            if runtime != "idle":
                return runtime
        if d.get("status") in ("failed", "archived"):
            return d.get("status")
        return "idle"
    if provider == "claude_code":
        name = d.get("cc_session_name") or d.get("provider_session_id") or ""
        if name:
            st, _prompt = await _v2_cc_state(name)
            return st
    return d.get("status") or "created"


async def _delegation_app_sessions() -> list:
    out = []
    for row in _delegation_rows(limit=50):
        st = await _delegation_runtime_status(row)
        d = _delegation_public(row, st)
        out.append({
            "id": f"delegation:{d['id']}",
            "type": "delegation",
            "name": d["display_title"],
            "parent": d.get("parent_persona"),
            "tool": d.get("provider"),
            "preview": (d.get("objective") or "")[:160],
            "lastAt": d.get("updated_at"),
            "status": st,
            "runtime_status": st,
            "work_order": d.get("work_order"),
            "provider_session_id": d.get("provider_session_id"),
            "takeover": d.get("takeover"),
        })
    return out


async def _delegation_v2_sessions() -> list:
    out = []
    for row in _delegation_rows(limit=50):
        st = await _delegation_runtime_status(row)
        d = _delegation_public(row, st)
        caps = ["input", "attachments", "replay", "follow"]
        if d.get("provider") in ("codex", "claude_code"):
            caps.append("interrupt")
        if d.get("provider") in ("codex", "claude_code") and st == "waiting_approval":
            caps.append("approve")
        approval = None
        if d.get("provider") == "codex":
            tid = d.get("codex_thread_id") or d.get("provider_session_id") or ""
            approval = CODEX_APP._approval_public(CODEX_APP.pending_approval_for_thread(tid))
        out.append({
            "id": f"delegation:{d['id']}",
            "provider": d.get("provider"),
            "title": d["display_title"],
            "subtitle": f"{d.get('parent_persona')} · {d.get('cwd')}",
            "status": st,
            "last_event_at": d.get("updated_at"),
            "capabilities": caps,
            "meta": {"delegation": d, "work_order": d.get("work_order"),
                     "takeover": d.get("takeover"), "approval": approval},
        })
    return out


def _delegated_codex_thread_ids() -> set:
    return {
        (dict(r).get("codex_thread_id") or dict(r).get("provider_session_id"))
        for r in _delegation_rows(limit=200)
        if dict(r).get("provider") == "codex"
    } - {""}


# ─── 委派生命週期回流(M1)+ CC↔CX 互調結果注回(M2)──────────────────────
# delegations 過去只存不回流:parent_session 存了沒用、完成無偵測,派工的人格
# 永遠不知道結果。現在:父是人格 → 寫 report_events 進該人格對話(卡片流本來
# 就會併入,Pocket 聊天串直接看到);父是另一個 delegation(CC↔CX 互調)→ 把
# 完成通知注回父 session 喚醒父代理。done/failed 另發推播。

def _delegation_meta(d: dict) -> dict:
    try:
        m = d.get("meta")
        return json.loads(m) if isinstance(m, str) else (m or {})
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_delegation_meta", _exc, expected=True)
        return {}


async def _delegation_notify(d: dict, event: str, summary: str = "") -> None:
    meta = _delegation_meta(d)
    wo = d.get("work_order") or d.get("id") or ""
    title = d.get("title") or ""
    status_txt = {"created": "已建立", "done": "已完成", "failed": "失敗",
                  "report": "進度回報"}.get(event, event)
    parent_dlg = str(meta.get("parent_delegation") or "")
    if parent_dlg:
        # CC↔CX 互調:結果注回父 delegation 的 session(喚醒父代理繼續),
        # 不再往人格灌(避免雙份)。
        prow = _delegation_get(parent_dlg)
        if prow:
            p = dict(prow)
            note = f"[子任務 {wo} {status_txt}] " + (summary.strip()[:800] or title)
            try:
                if p.get("provider") == "claude_code" and (p.get("cc_session_name") or ""):
                    await _cc_paste_text(p["cc_session_name"], note)
                elif p.get("provider") == "codex":
                    ptid = p.get("codex_thread_id") or p.get("provider_session_id") or ""
                    if ptid:
                        await CODEX_APP.start_turn(
                            ptid, await _codex_input_items(note, []),
                            client_id=f"dlg-notify-{d.get('id','')[:12]}-{event}")
            except Exception as e:  # noqa: BLE001
                _log_event("delegation_parent_notify_failed",
                           delegation=d.get("id"), error=str(e)[:160])
        return
    parent = d.get("parent_persona") or ""
    if parent in PERSONAS:
        lines = [f"[工號 {wo}] {title}", f"狀態:{status_txt}"]
        if summary.strip():
            lines += ["", summary.strip()[:2000]]
        tk = _delegation_takeover(d)
        sid = (tk.get("pocket") or {}).get("session_id") or ""
        if sid:
            lines += ["", f"接手:{sid}"]
        _report_upsert(parent, {
            "label": "委派任務", "name": f"dlg-{str(d.get('id') or '')[:12]}",
            "content": "\n".join(lines), "ts": time.time(),
            "external_source": "delegation",
            "external_id": f"dlg:{d.get('id')}:{event}:{int(time.time())}",
        })
    if event in ("done", "failed"):
        try:
            await push_notify(("✅ " if event == "done" else "❌ ") + f"[{wo}] {title[:40]}",
                              (summary.strip() or status_txt)[:160],
                              {"kind": "delegation_done",
                               "delegation_id": str(d.get("id") or "")})
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_delegation_notify", _exc, expected=True)
            pass


async def _delegation_codex_completed(tid: str, failed: bool, err_msg: str = "") -> None:
    """codex turn/completed → 對應委派 running→idle/failed 一次性回流。"""
    for row in _delegation_rows(limit=200):
        d = dict(row)
        if d.get("provider") != "codex":
            continue
        if (d.get("codex_thread_id") or d.get("provider_session_id") or "") != tid:
            continue
        if (d.get("status") or "") != "running":
            return                       # 只在 running→完成 的轉換回流一次
        new_status = "failed" if failed else "idle"
        _delegation_update(d["id"], status=new_status, updated_at=time.time(),
                           last_error=(err_msg[:300] if failed else ""))
        d["status"] = new_status
        await _delegation_notify(d, "failed" if failed else "done",
                                 summary=(err_msg if failed else ""))
        return


_DLG_CC_IDLE: dict = {}    # delegation id -> 連續 idle tick 數(debounce)


async def _delegation_cc_watcher():
    """15s 巡 created/running 的 CC 委派:busy→標 running;連兩 tick idle →
    判完成回流;tmux 不在 → failed。codex 靠 turn/completed 事件,不用巡。"""
    while True:
        await asyncio.sleep(15.0)
        try:
            for row in _delegation_rows(limit=100):
                d = dict(row)
                if d.get("provider") != "claude_code":
                    continue
                if (d.get("status") or "") not in ("created", "running"):
                    continue
                name = d.get("cc_session_name") or d.get("provider_session_id") or ""
                if not name:
                    continue
                st, _p = await _v2_cc_state(name)
                if st in ("running", "waiting_approval"):
                    _DLG_CC_IDLE.pop(d["id"], None)
                    if d.get("status") == "created":
                        _delegation_update(d["id"], status="running",
                                           updated_at=time.time())
                    continue
                if st == "failed":
                    _delegation_update(d["id"], status="failed",
                                       updated_at=time.time(),
                                       last_error="cc session not running")
                    d["status"] = "failed"
                    await _delegation_notify(d, "failed",
                                             summary="CC session 掛了(tmux 不在)")
                    _DLG_CC_IDLE.pop(d["id"], None)
                    continue
                n = _DLG_CC_IDLE.get(d["id"], 0) + 1
                _DLG_CC_IDLE[d["id"]] = n
                if n >= 2 and d.get("status") == "running":
                    _delegation_update(d["id"], status="idle",
                                       updated_at=time.time())
                    d["status"] = "idle"
                    await _delegation_notify(d, "done")
                    _DLG_CC_IDLE.pop(d["id"], None)
        except Exception as e:  # noqa: BLE001
            _log_event("delegation_cc_watch_error", error=str(e)[:160])


def _report_id(persona: str, name: str, sid: str, ts) -> str:
    raw = f"cron:{persona}:{name}:{sid}:{ts}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:24]


REPORT_ACTIONS_MAX = 6          # 一份報告最多 6 顆行動鈕(閱讀器尾端一屏放得下)
REPORT_ACTION_LABEL_MAX = 20    # 鈕面文字上限(字元)
REPORT_ACTION_TEXT_MAX = 500    # 回傳指令文字上限(字元)
REPORT_ACTION_URL_MAX = 1000    # 連結型 url 長度上限(超限**略過**——URL 截斷即斷鏈)


def _report_action_url_ok(url: str) -> bool:
    """連結型行動的 url 白名單:只收 http/https、必須有 host、長度設限。
    javascript:/data:/file:… 一律擋(app 端點了直接 openURL,不能給怪 scheme)。"""
    if not url or len(url) > REPORT_ACTION_URL_MAX:
        return False
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
    except Exception as exc:  # noqa: BLE001
        _log_exc("report_action_url_parse", exc, expected=True, url_len=len(url))
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _report_actions_normalize(raw) -> list:
    """persona-report 的 actions 收斂成正典形。兩型(feat/report-url-actions):
    - 指令型 `{label,text,target_session}`:點了把 text 送回 target session。
      label ≤20 字、text ≤500 字 — 超限**截斷不擋件**(發送端手滑不至於整包
      被拒);target_session 選填(`claude_code:<ccsess名>`/人格 id;空字串 =
      由 app 端預設回報告所屬人格)。
    - 連結型 `{label,url}`:點了開連結(app 走既有 StudioLinkRouter 分流)。
      元素帶 `url` 鍵即判連結型,url 只收 http/https + 有 host、≤1000 字 —
      壞 url **略過該顆**(截斷會斷鏈,不能比照 text 截),不落回指令型。
    - 兩型共用上限 6 顆,順序照發送端;非 list /元素非 dict /欄位不齊 →
      該顆略過,不擋整包。
    """
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:REPORT_ACTION_LABEL_MAX]
        if not label:
            continue
        if item.get("url") is not None:
            url = str(item.get("url") or "").strip()
            if not _report_action_url_ok(url):
                continue
            out.append({"label": label, "url": url})
        else:
            text = str(item.get("text") or "").strip()[:REPORT_ACTION_TEXT_MAX]
            if not text:
                continue
            out.append({"label": label, "text": text,
                        "target_session": str(item.get("target_session") or "").strip()})
        if len(out) >= REPORT_ACTIONS_MAX:
            break
    return out


def _report_actions_loads(raw) -> list:
    """report_events.actions 欄(JSON 文字或 NULL)→ list。舊列 NULL /壞 JSON
    /非 list 一律回空陣列 — 讀取端永遠拿得到可迭代的 actions。"""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_report_actions_loads", _exc, expected=True)
        return []
    return val if isinstance(val, list) else []


def _report_upsert(session: str, report: dict) -> str:
    import sqlite3
    rid = report.get("id") or _report_id(session, report.get("name") or "",
                                         report.get("session_id") or "",
                                         report.get("ts") or "")
    external_id = report.get("external_id") or rid
    content = report.get("content") or ""
    ts = float(report.get("ts") or time.time())
    label = report.get("label") or ""
    name = report.get("name") or ""
    external_source = report.get("external_source") or "hermes-cron"
    is_diagnostic = _is_hidden_report({
        "label": label,
        "name": name,
        "external_source": external_source,
    })
    # actions 是 payload 的一部分:呼叫端每次 upsert 帶完整清單(更新即整組
    # 替換);cron 同步線從不帶 → 寫 NULL,而 cron 報告本來就沒有行動,無損。
    actions = _report_actions_normalize(report.get("actions"))
    actions_json = json.dumps(actions, ensure_ascii=False) if actions else None
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        existing = con.execute(
            "SELECT label,name,content,ts,external_id,actions "
            "FROM report_events WHERE id=?", (rid,)).fetchone()
        if existing and existing == (label, name, content, ts, external_id,
                                     actions_json):
            con.close()
            return ""
        con.execute(
            "INSERT OR REPLACE INTO report_events"
            "(id,session,label,name,content,ts,external_source,external_id,"
            "ingested_at,actions) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rid, session, label, name, content, ts,
             external_source, external_id, time.time(), actions_json))
        con.commit()
        con.close()
        # Sync engine P1:cron 晨報寫入點鏡射進 event_log(雙寫過渡)。形狀走
        # _report_msg_shape = app 在 /app/v1/messages 看到的同一種報告訊息;
        # 改稿(同 rid 新內容)→ 新鍵 → 追加新事件,同 message id 覆蓋。
        if not is_diagnostic:
            _event_mirror_messages(session, [_report_msg_shape({
                "id": rid, "label": label, "content": content, "ts": ts,
            })])
        else:
            _log_event("report_event_diagnostic",
                       session=session, label=label, name=name,
                       external_source=external_source)
        return rid
    finally:
        con.close()


def _report_events(session: str, limit: int = 20, newest_first: bool = False,
                   include_diagnostics: bool = True):
    import sqlite3
    order = "DESC" if newest_first else "ASC"
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            fetch_limit = max(limit * 5, limit + 100)
            rows = con.execute(
                f"SELECT id,label,name,content,ts,external_source,external_id "
                f"FROM report_events WHERE session=? ORDER BY ts {order} LIMIT ?",
                (session, fetch_limit)).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_report_events", _exc, expected=True)
        return []
    events = [{
        "id": r[0], "label": r[1], "name": r[2], "content": r[3],
        "ts": r[4], "external_source": r[5], "external_id": r[6],
    } for r in rows]
    if not include_diagnostics:
        events = [r for r in events if not _is_hidden_report(r)]
    return events[:limit]


def _report_msg_shape(r: dict) -> dict:
    """report_events 列 → app-shape 訊息。_report_messages(v1 讀取)與
    _report_upsert 的 event_log 鏡射(P1)共用,兩邊 payload/去重鍵一致。"""
    return {
        "id": f"rep-{r['id']}", "role": "assistant",
        "content": f"📰 **{r['label']}**\n\n{r['content']}",
        "attachments": [], "ts": r["ts"], "status": "done", "source": "report",
    }


def _report_messages(session: str, limit: int = 100):
    """最新 limit 筆(newest_first)——舊版 ASC LIMIT 拿的是「史上最舊 limit 筆」,
    report_events 一超過 limit,新報告就永遠進不了 preview/對話合併
    (2026-07-15 修:人格列表與對話凍結在舊訊息的根因之一)。
    呼叫端(preview 合併/兩處對話合併)都會事後按 ts 重排,順序不影響。"""
    return [_report_msg_shape(r)
            for r in _report_events(session, limit, newest_first=True,
                                    include_diagnostics=False)]


TOOL_ERROR_REPORT_SOURCE = "hermes-tool-error"
TOOL_ERROR_REPORT_NAME = "agent-tool-error"
TOOL_ERROR_REPORT_LABEL = "錯誤報告"
# TOOL_ERROR_REPORTS_ENABLED 定義在 HIDDEN_REPORT_* 旁(隱藏判斷要用同一面旗)
TOOL_ERROR_REPORT_MAX_AGE = float(os.environ.get(
    "POCKET_TOOL_ERROR_REPORT_MAX_AGE", str(7 * 86400)))
TOOL_ERROR_REPORT_SCAN_MULTIPLIER = 8
TOOL_ERROR_DETAIL_CHARS = 5000
_TOOL_ERROR_TEXT_RES = (
    re.compile(r"traceback \(most recent call last\):", re.I),
    re.compile(r"\[exit\s+-?[1-9]\d*\]", re.I),
    re.compile(r"\bexit[_ ]?code\s*[:=]\s*-?[1-9]\d*\b", re.I),
    re.compile(r"\bstatus\s*[:=]\s*(error|failed|failure)\b", re.I),
    re.compile(r"\bok\s*[:=]\s*false\b", re.I),
    re.compile(r"\b(blocked|permission denied|unauthorized|forbidden)\b", re.I),
    re.compile(r"\bhttp error\s+(401|403|429|5\d\d)\b", re.I),
    re.compile(r"\bremote did not return json\b", re.I),
    re.compile(r"\b(no space left on device|timed out)\b", re.I),   # 裸 timeout 太泛(timeout=90 之類的程式碼就中),留 timed out
    re.compile(r"(^|\n)\s*(file not found|not found):", re.I),
    re.compile(r"(^|\n)\s*--- stderr ---", re.I),
)


def _tool_error_payload(raw: str):
    try:
        obj = json.loads(raw)
    except Exception as exc:
        _log_exc("_tool_error_payload", exc, expected=True,
                 raw_len=len(raw or ""))
        return None
    return obj if isinstance(obj, dict) else None


def _tool_error_like(raw: str) -> bool:
    """True when a TG tool row should become a user-visible diagnostic report.

    Hermes stores every tool result in state.db. Most rows are normal progress
    and must stay out of Pocket; only error-shaped rows graduate to report_events.
    """
    text = str(raw or "").strip()
    if not text:
        return False
    # 源碼傾印護欄(2026-08-05):agent 把整支腳本讀進工具輸出時(shebang 開頭),
    # 內文的 timeout=/SystemExit/HTTPError 全是程式碼不是錯誤 —— 潘天晴 cron 讀
    # FED_Revision CLI 原始碼被誤報成「工具錯誤」堆滿報告中心,就是這型。
    if text.startswith("#!"):
        return False
    payload = _tool_error_payload(text)
    if payload:
        status = str(payload.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            return True
        if payload.get("ok") is False:
            return True
        try:
            if int(payload.get("exit_code")) != 0:
                return True
        except Exception as exc:
            _log_exc("_tool_error_like.exit_code", exc, expected=True,
                     value=repr(payload.get("exit_code"))[:40])
        err = payload.get("error")
        if err not in (None, "", False):
            return True
        return False
    low = text.lower()
    return any(rx.search(low) for rx in _TOOL_ERROR_TEXT_RES)


def _tool_error_summary(raw: str) -> str:
    payload = _tool_error_payload(raw)
    candidates = []
    if payload:
        for key in ("error", "message", "stderr", "output", "content"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val)
        status = str(payload.get("status") or "").strip()
        exit_code = payload.get("exit_code")
        if status:
            candidates.append(f"status={status}")
        if exit_code not in (None, ""):
            candidates.append(f"exit_code={exit_code}")
    candidates.append(raw)
    for value in candidates:
        line = re.sub(r"\s+", " ", str(value or "")).strip()
        if not line:
            continue
        return line[:220]
    return "工具回傳錯誤"


def _fenced_text(raw: str, max_chars: int) -> str:
    text = _clip_text(str(raw or ""), max_chars)
    return text.replace("```", "'''")


def _tool_error_user_context(raw: str) -> str:
    text = _tg_clean_content(raw or "") or ""
    if text.startswith("[IMPORTANT: You are running as a scheduled cron job"):
        return ""
    return text


def _tool_error_report_content(persona: str, row: dict) -> str:
    ts = row.get("ts") or time.time()
    tool = (row.get("tool_name") or "tool").strip() or "tool"
    user_context = _tool_error_user_context(row.get("user_context") or "")
    summary = _tool_error_summary(row.get("content") or "")
    lines = [
        f"## {PERSONAS.get(persona, (persona, None))[0]} 工具錯誤",
        "",
        f"- 時間：{_fmt_ts(ts)}",
        f"- 工具：`{tool}`",
        f"- Telegram session：`{row.get('session_id') or ''}`",
        f"- 摘要：{summary}",
    ]
    if user_context:
        lines.append(f"- 前一則使用者訊息：{_clip_text(user_context, 180)}")
    lines += [
        "",
        "<details><summary>原始工具輸出</summary>",
        "",
        "```text",
        _fenced_text(row.get("content") or "", TOOL_ERROR_DETAIL_CHARS),
        "```",
        "",
        "</details>",
    ]
    return "\n".join(lines).strip()


def _persona_tool_error_reports(persona: str, limit: int = 20) -> list[dict]:
    """Newest TG/cron tool errors for a persona, shaped as report_events payloads.

    These reports make raw execution failures visible in Pocket's report/card
    surfaces without mixing traceback/stderr rows into the main chat transcript.
    """
    import sqlite3
    if persona not in PERSONAS:
        return []
    home = home_for(persona)
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return []
    scan_limit = max(limit * TOOL_ERROR_REPORT_SCAN_MULTIPLIER, limit)
    min_ts = time.time() - TOOL_ERROR_REPORT_MAX_AGE
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT m.id, m.session_id, m.tool_name, m.content, m.timestamp, "
                "(SELECT u.content FROM messages u WHERE u.session_id=m.session_id "
                " AND u.role='user' AND u.timestamp < m.timestamp "
                " AND u.content IS NOT NULL AND u.content!='' "
                " ORDER BY u.timestamp DESC LIMIT 1) AS user_context "
                "FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE s.source IN ('telegram','cron') AND m.role='tool' "
                "AND m.content IS NOT NULL AND m.content!='' "
                "AND m.timestamp >= ? ORDER BY m.timestamp DESC LIMIT ?",
                (min_ts, scan_limit)).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_tool_error_reports", _exc, expected=True)
        return []
    out = []
    for mid, sid, tool_name, content, ts, user_context in rows:
        if not _tool_error_like(content or ""):
            continue
        row = {"id": mid, "session_id": sid, "tool_name": tool_name or "tool",
               "content": content or "", "ts": ts, "user_context": user_context or ""}
        external_id = f"{TOOL_ERROR_REPORT_SOURCE}:{persona}:{sid}:{mid}"
        out.append({
            "id": _report_id(persona, TOOL_ERROR_REPORT_NAME, f"{sid}:{mid}", ts),
            "external_id": external_id,
            "external_source": TOOL_ERROR_REPORT_SOURCE,
            "session_id": sid,
            "label": TOOL_ERROR_REPORT_LABEL,
            "name": TOOL_ERROR_REPORT_NAME,
            "content": _tool_error_report_content(persona, row),
            "ts": ts,
        })
        if len(out) >= limit:
            break
    return out


def _clip_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[截斷，完整內容保存在 report_events]"


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_fmt_ts", _exc, expected=True)
        return ""


# ───────────────────────── APNs push (M23) ─────────────────────────────────
# Token-based (.p8) auth. 正式金鑰(2026-07-23 換發,sandbox/production 皆已
# 實測認證通過):
#   ~/.pocket-release-secrets/apns/AuthKey_9XNQ4PS546.p8  (chmod 600)
# 舊 Hermes 管理的 AuthKey_86FF9D976T.p8 已汰換。輪替程序見
# docs/HANDOFF_CREDENTIALS.md。
#
# feat/apns-sender:全組設定改為 env 可注入(金鑰後補插槽):
#   APNS_KEY_PATH / APNS_KEY_ID / APNS_TEAM_ID / APNS_BUNDLE_ID / APNS_HOST
# 沒設 env 時用下方預設值;金鑰檔不存在/KEY_ID/TEAM_ID 缺席時,
# `apns_configured()` 回 False,整個推播模組靜默停用(push_notify 直接短路,
# bridge 照常啟動,絕不因缺金鑰起不來)。
APNS_KEY_PATH = os.path.expanduser(os.environ.get(
    "APNS_KEY_PATH",
    "~/.pocket-release-secrets/apns/AuthKey_9XNQ4PS546.p8"))
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "9XNQ4PS546")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "4F8B93R3SH")
# 正式 app 是 Pocket kernel(com.pocketagent.kernel,見 ship-kernel.sh)。apns-topic
# 必須對上 device token 所屬 app,否則 APNs 回 400 BadTopic / DeviceTokenNotForTopic
# → 推播全滅(2026-07 之前寫成舊 SUN 的 com.pocketagent.ios,推播在正式版 100% 死)。
# token-based(.p8)是 team 級,對同 team 任何 bundle 都有效,只要 topic 對。
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "com.pocketagent.kernel")
APNS_HOST = os.environ.get(
    "APNS_HOST", "https://api.push.apple.com")   # production (TestFlight + App Store)
_apns_jwt_cache: list = [None, 0.0]        # [token, issued_at]
_apns_disabled_logged: list = [False]      # 只記一次,避免每則推播都刷 log


def apns_configured() -> bool:
    """金鑰三件套(路徑上的 .p8 檔 + KEY_ID + TEAM_ID)齊備才算配置完成。

    未配置 → 推播模組整體靜默停用:push_notify 短路回 disabled,不打 APNs、
    不讀金鑰、不炸例外。第一次偵測到未配置時 _log_event 一筆(此後安靜),
    讓「推播沒動靜」可以在 event log 裡查到原因而不是無聲消失。"""
    ok = bool(APNS_KEY_ID) and bool(APNS_TEAM_ID) and os.path.isfile(APNS_KEY_PATH)
    if not ok and not _apns_disabled_logged[0]:
        _apns_disabled_logged[0] = True
        _log_event("apns_disabled",
                   key_path=APNS_KEY_PATH,
                   key_file_exists=os.path.isfile(APNS_KEY_PATH),
                   key_id_set=bool(APNS_KEY_ID), team_id_set=bool(APNS_TEAM_ID))
    return ok


def _apns_jwt() -> str:
    """ES256 JWT for APNs, cached ~50 min (Apple requires < 60 min)."""
    import jwt as pyjwt
    now = time.time()
    if _apns_jwt_cache[0] and now - _apns_jwt_cache[1] < 3000:
        return _apns_jwt_cache[0]
    with open(APNS_KEY_PATH) as f:
        key = f.read()
    tok = pyjwt.encode({"iss": APNS_TEAM_ID, "iat": int(now)}, key,
                       algorithm="ES256", headers={"kid": APNS_KEY_ID})
    _apns_jwt_cache[0], _apns_jwt_cache[1] = tok, now
    return tok


def _devices() -> list:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT token FROM devices").fetchall()
            con.close()
            return [r[0] for r in rows]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        # An unreadable devices table means "push notifications silently off" —
        # log it so the failure is diagnosable (issue #7).
        _log_event("devices_read_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
        return []


def _device_add(token: str, platform: str = "ios") -> None:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("INSERT OR REPLACE INTO devices(token,platform,created_at) "
                        "VALUES(?,?,?)", (token, platform, time.time()))
            con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("device_add_failed", platform=platform,
                   error=type(e).__name__, error_message=str(e)[:160])


def _device_remove(token: str) -> None:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("DELETE FROM devices WHERE token=?", (token,))
            con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("device_remove_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
    _push_pref_drop(token)


# ── 推播偏好(/app/v1/push/register)────────────────────────────────────────
# 每台裝置的通知偏好,存 canonical 旁的小 JSON(不動 devices 表 schema):
#   { "<token>": {"preview": bool, "personas": [session,...] | null} }
# preview=False → 人格訊息推播只顯示人格名,body 換成固定占位(不外洩內容)。
# personas=null → 訂閱全部人格;給清單 → 只推清單內的人格回覆。
# 沒有偏好紀錄的 token(走舊 /app/v1/devices 註冊)一律預設 preview=True、
# personas=null —— 與現行行為完全一致。
PUSH_PREFS_PATH = os.path.join(os.path.dirname(CANON_DB), "push_prefs.json")
_PUSH_PREF_DEFAULT = {"preview": True, "personas": None}


def _push_prefs_load() -> dict:
    try:
        with open(PUSH_PREFS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        # 壞檔=偏好全部回預設(照推),不讓推播管線炸掉;log 供診斷。
        _log_event("push_prefs_read_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
        return {}


def _push_prefs_save(prefs: dict) -> None:
    try:
        tmp = PUSH_PREFS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prefs, f, ensure_ascii=False)
        os.replace(tmp, PUSH_PREFS_PATH)   # 原子換檔,避免半寫壞檔
    except Exception as e:  # noqa: BLE001
        _log_event("push_prefs_write_failed", error=type(e).__name__,
                   error_message=str(e)[:160])


def _push_pref_set(token: str, preview: bool = True,
                   personas: list | None = None) -> dict:
    entry = {"preview": bool(preview),
             "personas": [str(p) for p in personas] if personas is not None else None}
    prefs = _push_prefs_load()
    prefs[token] = entry
    _push_prefs_save(prefs)
    return entry


def _push_pref_get(token: str, prefs: dict | None = None) -> dict:
    entry = (prefs if prefs is not None else _push_prefs_load()).get(token)
    if not isinstance(entry, dict):
        return dict(_PUSH_PREF_DEFAULT)
    return {"preview": entry.get("preview", True) is not False,
            "personas": entry.get("personas")
            if isinstance(entry.get("personas"), list) else None}


def _push_pref_drop(token: str) -> None:
    prefs = _push_prefs_load()
    if token in prefs:
        prefs.pop(token, None)
        _push_prefs_save(prefs)


async def _apns_send(token: str, title: str, body: str, data: dict | None = None,
                     category: str | None = None, thread_id: str | None = None,
                     content_available: bool = False):
    import httpx
    headers = {"authorization": f"bearer {_apns_jwt()}",
               "apns-topic": APNS_BUNDLE_ID,
               "apns-push-type": "alert", "apns-priority": "10"}
    aps = {"alert": {"title": title, "body": body}, "sound": "default"}
    if content_available:
        # 讓通知本體也能喚醒 app 在背景拉新訊息(通知本身已帶 title/body,
        # 收到即最新;app 若在背景可順手 refresh 對話)。
        aps["content-available"] = 1
    if category:
        # 批次 3 斷點①:category 才會讓 iOS/手錶顯示 UNNotificationAction
        # 動作鈕(app 端已註冊同名 category)。
        aps["category"] = category
    if thread_id:
        aps["thread-id"] = thread_id
    payload = {"aps": aps}
    if data:
        payload.update(data)
    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        r = await client.post(f"{APNS_HOST}/3/device/{token}",
                              headers=headers, json=payload)
        return r.status_code, r.text


async def push_notify(title: str, body: str, data: dict | None = None,
                      category: str | None = None,
                      thread_id: str | None = None,
                      content_available: bool = False,
                      persona: str | None = None,
                      no_preview_body: str | None = None) -> dict:
    """Fan a push to every registered device; prune dead tokens (410/BadToken).

    Returns {sent, total, failures:[{code,detail}]}. **不再吞錯** —— 非 200/410 的
    APNs 回應(400 BadTopic、403 bad key、429…)以前被靜默吃掉,推播死了好幾週都
    查不到。現在一律 _log_event,`/push/test` 也回傳真實 code。

    feat/apns-sender:
    - 金鑰未配置(apns_configured() False)→ 整段短路,回 disabled=True,
      不打 APNs、不讀金鑰 —— bridge 缺金鑰照常活著,推播模組靜默停用。
    - persona 給定 → 逐台裝置查訂閱偏好(/app/v1/push/register),沒訂閱該
      人格的裝置跳過(skipped 計數)。
    - no_preview_body 給定 → preview=False 的裝置用它取代 body(關預覽只
      顯示人格名,訊息內容不出現在鎖屏)。"""
    if not apns_configured():
        return {"sent": 0, "total": 0, "failures": [], "disabled": True}
    toks = _devices()
    prefs = _push_prefs_load()
    sent = 0
    skipped = 0
    failures: list[dict] = []
    for tok in toks:
        pref = _push_pref_get(tok, prefs)
        if persona is not None and pref["personas"] is not None \
                and persona not in pref["personas"]:
            skipped += 1
            continue
        tok_body = body
        if no_preview_body is not None and not pref["preview"]:
            tok_body = no_preview_body
        try:
            code, text = await _apns_send(tok, title, tok_body, data,
                                          category=category, thread_id=thread_id,
                                          content_available=content_available)
            if code == 200:
                sent += 1
            elif (code == 410 or "BadDeviceToken" in text or "Unregistered" in text
                  or "DeviceTokenNotForTopic" in text):
                # DeviceTokenNotForTopic = 舊 SUN(.ios)遺留 token,對 .kernel 永遠不合 → 清掉。
                _device_remove(tok)
                failures.append({"code": code, "detail": "wrong-app/unregistered→pruned",
                                 "token": tok[:8]})
            else:
                failures.append({"code": code, "detail": (text or "")[:160],
                                 "token": tok[:8]})
        except Exception as e:  # noqa: BLE001
            _log_exc("push_notify", e, expected=True)
            failures.append({"code": "exc", "detail": str(e)[:160], "token": tok[:8]})
    if failures:
        _log_event("push_notify_failed", title=title[:48], sent=sent,
                   total=len(toks), failures=str(failures)[:400])
    return {"sent": sent, "total": len(toks), "skipped": skipped,
            "failures": failures}


# Scarf 契約遷移 Stage 1b(見 pocketagent/docs/SCARF_CONTRACT_MIGRATION_PLAN.md)。
# ⚠️ GATE:本分支只在「接受新 category 的 app(Stage 1a,pocketagent PR)」已
#    上架/普及後才可 merge+deploy。先翻 producer 會讓舊 app 收不到動作鈕。app
#    側 1a 已雙接受 POCKET_/SCARF_ 兩個 category 與 pocket/scarf 兩巢,故翻新後
#    舊 app 仍靠 scarf 巢運作,新 app 走 pocket 巢。
_APNS_APPROVAL_CATEGORY = "POCKET_PENDING_PERMISSION"


def _approval_decision_keys(aid: str) -> tuple:
    """推播動作鈕的決定鍵(feat/apns-sender):從審核列 options 的 style 挑
    成對兩顆 — primary→核准鍵、danger→駁回鍵(CC 線 options 缺 danger 時
    駁回鍵落 "esc",與 _cc_choice_key 的 deny fallback 一致)。app 按鈕按下
    時把鍵原樣回送 `{key}`(A2 單一決定路徑),CC 的 TUI 鍵(y/esc…)不經
    bool 猜測。挑不出成對兩顆(泛 question/notice/三態複雜選單)回
    (None, None) — app 落回 `{approve: bool}` 相容糖,複雜選項導去 app 內
    審核中心處理。"""
    try:
        d = _approval_get_row(aid)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_approval_decision_keys", _exc, expected=True)
        return None, None
    if not d or (d.get("kind") or "permission") != "permission":
        return None, None
    opts = [o for o in (d.get("options") or []) if isinstance(o, dict)]
    ok = next((str(o.get("key")) for o in opts
               if o.get("style") == "primary" and o.get("key")), None)
    dk = next((str(o.get("key")) for o in opts
               if o.get("style") == "danger" and o.get("key")), None)
    if not dk and d.get("provider") == "claude_code":
        dk = "esc"
    if not ok or not dk:
        return None, None
    return ok, dk


def _approval_push(aid: str, title: str, body: str, session_id: str = ""):
    """審核推播(批次 3 斷點①):category 出動作鈕、payload 巢與 app 端約定對齊、
    thread-id 以 session 分串。Stage 1b 翻新:新 `pocket.{kind, approvalId,
    sessionId}` 巢 + category;**相容期保留** 舊 `scarf` 巢與更舊頂層 {kind, id},
    讓尚未更新的 app 仍可解析(app 側 pocket 巢優先)。feat/apns-sender:巢再帶
    approveKey/denyKey(見 _approval_decision_keys)讓鎖屏動作鈕走 {key} 決定
    路徑。fire-and-forget。"""
    _approval_nest = {"kind": "approval", "approvalId": aid,
                      "sessionId": session_id}
    _ok, _dk = _approval_decision_keys(aid)
    if _ok and _dk:
        _approval_nest["approveKey"] = _ok
        _approval_nest["denyKey"] = _dk
    data = {"kind": "approval", "id": aid,   # 最舊頂層鍵(相容期保留)
            "pocket": _approval_nest,        # 新巢(app 優先讀)
            "scarf": _approval_nest}         # 舊巢(相容期保留;Stage 1c 移除)
    task = asyncio.create_task(push_notify(
        f"🔐 {title}", body[:120], data,
        category=_APNS_APPROVAL_CATEGORY,
        thread_id=session_id or "approvals"))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _push_persona_reply(session: str, content: str) -> None:
    """P1-3 人格回訊推播:人格(assistant)在 canonical 落地一則『完成』的回覆時,推播
    把你叫回 app。標題=人格顯示名,body=清過卡片/步驟的預覽。payload `pocket.kind=
    message` + sessionId 供 app deep-link 進該人格對話。content-available 讓背景也能
    順手刷新。app 前景時由 willPresent 抑制橫幅(你正在看,不吵);背景時系統自動顯示。
    fire-and-forget;無執行中 event loop(純 sync 匯入期)則跳過。"""
    if session not in PERSONAS:
        return
    disp = PERSONAS[session][0]
    try:
        clean, _bodies = carddigest.extract_studio_cards(content or "")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_push_persona_reply", _exc, expected=True)
        clean = content or ""
    clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S).strip()
    body = (clean or "傳了一則訊息")[:140]
    # sessionId 用 deep-link wire 格式 hermes:{persona}(app 點通知直達該人格對話);
    # app 的 willPresent 會剝前綴比對「正在看哪條」以決定前景是否彈橫幅。
    data = {"kind": "message",
            "pocket": {"kind": "message", "sessionId": f"hermes:{session}"}}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # persona= 讓 push_notify 逐台裝置套用訂閱偏好(沒訂閱這個人格的裝置跳過);
    # no_preview_body= 是 preview=False 裝置的替代 body(只顯示人格名+占位,
    # 訊息內容不進鎖屏)。
    t = loop.create_task(push_notify(disp, body, data,
                                     thread_id=session, content_available=True,
                                     persona=session,
                                     no_preview_body="傳了一則訊息"))
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


_canon_init()
_accounts_init()
_subsessions_load()   # issue #5: rebuild /dispatch subs after restart
_personas_reload()    # G6: apply persona overrides/customs from canonical.db


async def _persona_content_stream(model: str, prompt: str):
    """Core persona turn → yields ('content', str) pieces, ('keepalive', None)
    during gaps, ('usage', {used,size}) once. Shared by /v1/chat/completions and
    /app/v1/messages so both stream identically (the latter also records the
    accumulated reply to the canonical store)."""
    if not prompt:
        yield ("content", "(沒有收到訊息)")
        return
    q: asyncio.Queue = asyncio.Queue()
    session = await POOL.get(model, home_for(model))

    async def pump():
        try:
            async for kind, val in session.prompt_stream(prompt):
                await q.put((kind, val))
        except Exception as e:  # noqa: BLE001
            _log_exc("_persona_content_stream.pump", e, expected=False, model=model)
            await q.put(("error", str(e)))
        finally:
            await q.put(("end", None))

    pump_task = asyncio.create_task(pump())
    pump_stopped = False
    got_text = False
    completed = False
    thought_buf: list[str] = []
    steps: list[dict] = []          # 工具步驟 — 不進正文,收尾摺疊附錄

    def flush_thought():
        if thought_buf:
            t = "".join(thought_buf).strip()
            thought_buf.clear()
            if t:
                return f"\n<details><summary>💭 思考</summary>\n\n{t}\n\n</details>\n\n"
        return None

    def flush_steps():
        """收尾一次性附上摺疊的步驟清單(預設看不到,點開才展開)——
        對話正文只留人話;canonical/歷史也存這個形狀。"""
        if not steps:
            return None
        lines = []
        for i, s in enumerate(steps, 1):
            head = f"{i}. **{s['name']}**" + (f" `{s['cmd']}`" if s["cmd"] else "")
            if s.get("note"):
                head += f" — {s['note']}"
            lines.append(head)
            if s.get("result"):
                lines.append(f"\n   ```\n{s['result']}\n   ```\n")
        body = "\n".join(lines)[:6000]
        n = len(steps)
        steps.clear()
        return f"\n\n<details><summary>🔧 執行步驟 ({n})</summary>\n\n{body}\n\n</details>\n"

    import time as _t
    last_event = _t.monotonic()

    async def stop_pump(*, reset: bool) -> None:
        """Stop the task that owns ACPSession._lock and optionally retire ACP."""
        nonlocal pump_stopped
        if pump_stopped:
            return
        pump_stopped = True
        try:
            await asyncio.wait_for(session.cancel(), timeout=2.0)
        except Exception as _exc:
            _log_exc("_persona_content_stream.stop_pump", _exc, expected=True)
            pass
        if not pump_task.done():
            pump_task.cancel()
        try:
            await asyncio.wait_for(pump_task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        except Exception as _exc:
            _log_exc("_persona_content_stream.stop_pump#2", _exc, expected=True)
            reset = True
        # A five-minute silent provider is not safe to reuse even when task
        # cancellation released the Python lock: its RPC turn may still run.
        if reset or session.is_busy():
            try:
                await session.reset()
            except Exception as _exc:
                _log_exc("_persona_content_stream.stop_pump#3", _exc, expected=True)
                pass

    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECS)
                last_event = _t.monotonic()
            except asyncio.TimeoutError:
                if _t.monotonic() - last_event > PERSONA_STALL_LIMIT_SECS:
                    await stop_pump(reset=True)
                    yield ("content", "\n\n⚠️ 回合逾時(伺服器端 5 分鐘無回應),已中止。")
                    completed = True
                    break
                yield ("keepalive", None)
                continue
            if kind == "text":
                if not got_text:
                    ft = flush_thought()
                    if ft:
                        yield ("content", ft)
                got_text = True
                yield ("content", val)
            elif kind == "thought":
                thought_buf.append(val)
            elif kind == "tool_start":
                # 工具步驟不再內聯進正文(使用者回報:指令洗版、跑完消失又
                # 湧一批)。改走 status label(app 底部 working bar 原樣顯示
                # 「執行步驟 N:工具」),細節收進收尾的摺疊附錄。
                name = val.get("name", "tool")
                cmd = (val.get("cmd") or "").strip().splitlines()
                cmd1 = (cmd[0] if cmd else "")[:TOOL_CMD_MAX]
                steps.append({"name": name, "cmd": cmd1, "result": "", "note": ""})
                yield ("status", {"state": "running",
                                  "label": f"執行步驟 {len(steps)}:{name}"})
            elif kind == "tool_result":
                res = (val.get("text") or "").strip()
                if res and steps:
                    short = res[:400]
                    if len(res) > 400:
                        short += "\n…(截斷)"
                    steps[-1]["result"] = short
            elif kind == "perm":
                if steps:
                    steps[-1]["note"] = f"🔐 自動允許 {val}"
                else:
                    steps.append({"name": str(val), "cmd": "", "result": "",
                                  "note": "🔐 自動允許"})
            elif kind == "status":
                yield ("status", val)
            elif kind == "usage":
                yield ("usage", val)
            elif kind == "error":
                if not got_text:
                    try:
                        yield ("content", await run_hermes(model, prompt))
                    except Exception as e2:  # noqa: BLE001
                        _log_exc("_persona_content_stream", e2, expected=True)
                        yield ("content", f"⚠️ {e2}")
                else:
                    yield ("content", f"\n\n⚠️ 串流中斷:{val}")
            else:
                completed = True
                break
        ft = flush_thought()
        if ft:
            yield ("content", ft)
        fs = flush_steps()
        if fs:
            yield ("content", fs)
    finally:
        if not completed:
            await stop_pump(reset=False)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_auth(request)
    body = await request.json()
    model = body.get("model", "xcash")
    stream = bool(body.get("stream", False))
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    # Sub-session (dispatched CC/Codex) — replay + follow its work transcript.
    if model in SUBSESSIONS:
        sub = SUBSESSIONS[model]

        # Follow-up turn: a new, non-empty user message resumes the sub-agent.
        # Stream from the current tail so we don't re-replay the whole transcript.
        new_prompt = _last_user_message(body.get("messages", []))
        start_idx = 0
        if new_prompt and new_prompt != sub.get("last_user") and sub.get("status") != "running":
            start_idx = len(sub["output"])
            sub["last_user"] = new_prompt
            sub["status"] = "running"
            sub["output"].append(("text", f"\n\n---\n**追問:** {new_prompt}\n\n"))
            sub["lastAt"] = time.time()
            asyncio.create_task(_run_resume(model, new_prompt))

        def schunk(delta, finish=None):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def sgen():
            yield schunk({"role": "assistant", "content": ""})
            idx = start_idx
            quiet = 0.0
            # issue #7 項目 4:這圈的出口條件是 sub["status"] 不再是 running。
            # 正常路徑有 _stream_agent 的 finally 保證會落到 done,但只要有一條
            # 路徑漏掉(狀態被外部改寫、字典被換掉),這裡就是無限迴圈。加一道
            # 與 CC stream 同款的 idle 上限當保險絲。
            last_out = time.monotonic()
            while True:
                while idx < len(sub["output"]):
                    kind, val = sub["output"][idx]
                    idx += 1
                    c = _fmt_item(kind, val)
                    if c:
                        yield schunk({"content": c})
                        quiet = 0.0
                        last_out = time.monotonic()
                if sub.get("status") != "running" and idx >= len(sub["output"]):
                    break
                if time.monotonic() - last_out >= _STREAM_IDLE_CUTOFF_SECS:
                    _log_event("subagent_stream_idle_cutoff", sid=model,
                               status=sub.get("status"))
                    break
                await asyncio.sleep(0.4)
                quiet += 0.4
                if quiet >= SSE_KEEPALIVE_SECS:
                    quiet = 0.0
                    yield ": keepalive\n\n"
            yield schunk({}, finish="stop")
            yield "data: [DONE]\n\n"

        if stream:
            return StreamingResponse(sgen(), media_type="text/event-stream")
        text = "".join(v for k, v in sub["output"] if k == "text")
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created,
                             "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}]})

    if model not in PERSONAS:
        model = "xcash"
    prompt = await _resolve_persona_prompt(
        body.get("messages", []), home_for(model)
    )

    if stream:
        # Live streaming over a warm ACP session: a background pump feeds text
        # chunks onto a queue; the SSE generator drains it with a 2s timeout,
        # emitting keepalive comments during gaps (e.g. tool reasoning before
        # the first token) so the socket never goes idle long enough to drop.
        # Falls back to cold `hermes -z` only if ACP yields nothing.
        async def gen():
            def chunk(delta, finish=None):
                payload = {"id": cid, "object": "chat.completion.chunk",
                           "created": created, "model": model,
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant", "content": ""})  # open the bubble
            last_usage = None
            async for k, v in _persona_content_stream(model, prompt):
                if k == "content":
                    yield chunk({"content": v})
                elif k == "keepalive":
                    yield ": keepalive\n\n"
                elif k == "usage":
                    last_usage = v
            final = {"index": 0, "delta": {}, "finish_reason": "stop"}
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": model, "choices": [final]}
            if last_usage and last_usage.get("size"):
                payload["usage"] = {"context_used": last_usage.get("used"),
                                    "context_size": last_usage.get("size")}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        content = "(沒有收到訊息)" if not prompt else await acp_full(model, prompt)
    except Exception as _exc:
        _log_exc("chat_completions", _exc, expected=False, model=model, stage="acp_full_fallback_to_cold")
        content = await run_hermes(model, prompt)
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


CLAUDE_BIN = os.path.expanduser(os.environ.get("CLAUDE_BIN", "")) or (
    shutil.which("claude") or os.path.expanduser("~/.local/bin/claude"))
# 用能讀「新版 thread」的 codex 當 app-server。VS Code 用 codex 0.142 建 thread,
# 舊的 standalone 0.137(~/.local/bin/codex)一讀其 full turns(thread/turns/list
# itemsView=full)就 crash → UPSTREAM_FAILED「codex app-server stopped」,整條 stdio
# 卡死,app 端該 session 空白且送不出。優先挑 Codex.app 內建的 0.142(VS Code 同款、
# 共用 ~/.codex 登入),對不上再退回 standalone。CODEX_BIN 環境變數可覆蓋。
def _resolve_codex_bin() -> str:
    # 2026-07-10 事故:ChatGPT.app 更新把 Codex Desktop 併入、/Applications/Codex.app
    # 整個消失 → 舊首選路徑失效,fallback 到 0.137 又是「讀新 thread 會 crash」地雷,
    # 手機 CX 全空數小時且無錯誤日誌。候選序補上 ChatGPT.app 的新家,且 spawn 時
    # 每次重新解析(見 _ensure_started_locked),桌面 app 更新不再需要重啟 bridge。
    for c in (os.environ.get("CODEX_BIN"),
              "/Applications/Codex.app/Contents/Resources/codex",
              "/Applications/ChatGPT.app/Contents/Resources/codex",
              os.path.expanduser("~/.local/bin/codex"),
              shutil.which("codex")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.local/bin/codex")


CODEX_BIN = _resolve_codex_bin()   # 僅供顯示/預設;spawn 走 _resolve_codex_bin()

# The managed Codex daemon is the single writer shared by Desktop and Pocket.
# Its Unix socket speaks WebSocket frames (not JSONL); spawning our own
# `codex app-server --stdio` is the fallback for hosts that don't have it.
CODEX_APP_SERVER_SOCKET = os.path.expanduser(
    os.environ.get(
        "CODEX_APP_SERVER_SOCKET",
        "~/.codex/app-server-control/app-server-control.sock"))

# 傳輸選擇是**三態**,而且「有沒有 daemon」一律用「連得上嗎」判定,不是用
# `os.path.exists(socket)` 判定。兩個都是踩過的雷:
#   1. unix socket 的 inode 在 daemon 崩潰/被強制結束後**會留在磁碟上**。
#      exists() 仍為 True → 舊碼走 managed → connect 丟 ConnectionRefusedError
#      (Errno 61) → 直接 raise,永遠碰不到 stdio。桌面 app 自動更新留下殭屍
#      正是這個情境(2026-08-12)。
#   2. 開機後使用者還沒開 ChatGPT.app、或龍蝦那台無頭 Ubuntu 根本沒有桌面
#      app → socket 檔不存在 → 舊碼(預設 managed)一樣 raise → CX 全滅。
# 所以 fallback 必須寫在 except 路徑裡,不能寫在「用設定字串決定」的 else 裡。
#   auto    = 先試 managed daemon,任何連線失敗都退回自己 spawn stdio(預設)
#   managed = 只用共用 daemon,連不上就大聲壞掉(刻意要 daemon-only 的人用)
#   stdio   = 只 spawn 自己的 app-server,完全不碰 socket
CODEX_APP_SERVER_MODES = ("auto", "managed", "stdio")
CODEX_APP_SERVER_MODE = os.environ.get("CODEX_APP_SERVER_MODE", "auto").strip().lower()
if CODEX_APP_SERVER_MODE not in CODEX_APP_SERVER_MODES:
    CODEX_APP_SERVER_MODE = "auto"


def _is_clean_ws_closure(exc: BaseException) -> bool:
    """這個 WebSocket 例外是不是「對方好好地把連線關掉」?

    使用者關掉 ChatGPT.app = daemon 正常收攤 = close code 1000/1001。
    那不是故障,不該記成 failure(不然每關一次桌面 app 就一則假警報)。
    """
    try:
        from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
    except Exception:  # noqa: BLE001 - websockets 不在也不該讓 log 分類炸掉
        return type(exc).__name__ == "ConnectionClosedOK"
    if isinstance(exc, ConnectionClosedOK):
        return True
    if isinstance(exc, ConnectionClosed):
        for frame in (getattr(exc, "rcvd", None), getattr(exc, "sent", None)):
            if getattr(frame, "code", None) in (1000, 1001):
                return True
    return False


# ─────────── CC/CX 登入狀態探測(給 app gate CC/CX 頁籤)───────────
# app 端 CC/CX 頁籤在對應引擎「這台 Mac 從未登入」時要蓋版凍結、提示去 Pocket
# 桌面控制台登入。真相只在這台 Mac 的 CLI:
#   • claude auth status → JSON {"loggedIn":true,"email":…,"subscriptionType":…} exit 0
#   • codex login status → 登入時 "Logged in using ChatGPT" exit 0;未登入 exit 1
# spawn 是秒級、且不需要即時 → 快取 TTL 60s,避免每次 dashboard/gate 都開子程序。
# 純唯讀:只查狀態,不碰/不搬任何憑證。
_AGENT_AUTH_TTL = 60.0
_AGENT_AUTH_CACHE: dict = {"at": 0.0, "data": None}
_AGENT_AUTH_REFRESHING = False


async def _run_status_cli(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """跑一次狀態查詢 CLI,回 (exit_code, 合併輸出);逾時/失敗回 (-1, "")。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL)
    except Exception as _exc:  # noqa: BLE001 — 執行檔不在/不可執行
        _log_exc("_run_status_cli.spawn", _exc, expected=True,
                 argv0=(argv[0] if argv else ""))
        return -1, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception as _exc:  # noqa: BLE001 — 已自行結束的競態
            _log_exc("_run_status_cli.kill", _exc, expected=True)
        return -1, ""
    return (proc.returncode if proc.returncode is not None else -1,
            (out or b"").decode("utf-8", "replace"))


def _parse_claude_auth(exit_code: int, output: str) -> tuple[bool, str | None]:
    """claude auth status(JSON)→ (logged_in, account)。保守取第一個 {…最後一個 }。"""
    if exit_code != 0:
        return False, None
    try:
        start, end = output.find("{"), output.rfind("}")
        if start < 0 or end <= start:
            return False, None
        d = json.loads(output[start:end + 1])
    except Exception as _exc:  # noqa: BLE001 — CLI 輸出不是 JSON
        _log_exc("_parse_claude_auth", _exc, expected=True,
                 output_chars=len(output or ""))
        return False, None
    if d.get("loggedIn") is not True:
        return False, None
    parts = [p for p in (d.get("email"), d.get("subscriptionType")) if p]
    return True, (" · ".join(parts) if parts else None)


def _claude_oauth_profile_fallback() -> tuple[bool, str | None] | None:
    """Claude Code 2.1.x 有時 `auth status` 回 loggedIn:false 但 /status 其實有登入;
    非機密的側寫在 ~/.claude.json 的 oauthAccount。僅作顯示用備援,不讀/不搬憑證。"""
    try:
        with open(os.path.expanduser("~/.claude.json"), "r", encoding="utf-8") as f:
            acct = (json.load(f) or {}).get("oauthAccount") or {}
    except Exception as _exc:  # noqa: BLE001 — 檔案不存在/格式壞
        _log_exc("_claude_oauth_profile_fallback", _exc, expected=True)
        return None
    if not acct:
        return None
    parts = [p for p in (acct.get("emailAddress"),
                         acct.get("seatTier") or acct.get("billingType")) if p]
    return True, (" · ".join(parts) if parts else None)


def _parse_codex_login(exit_code: int, output: str) -> tuple[bool, str | None]:
    text = output.strip()
    low = text.lower()
    if exit_code != 0 or "logged in" not in low or "not logged in" in low:
        return False, None
    first = text.splitlines()[0] if text else None
    return True, first


async def _probe_agent_auth() -> dict:
    claude = {"installed": os.path.exists(CLAUDE_BIN),
              "logged_in": False, "account": None}
    if claude["installed"]:
        rc, out = await _run_status_cli([CLAUDE_BIN, "auth", "status"])
        logged_in, account = _parse_claude_auth(rc, out)
        if not logged_in:
            fb = _claude_oauth_profile_fallback()
            if fb is not None:
                logged_in, account = fb
        claude["logged_in"], claude["account"] = logged_in, account

    codex_bin = _resolve_codex_bin()
    codex = {"installed": os.path.exists(codex_bin),
             "logged_in": False, "account": None}
    if codex["installed"]:
        rc, out = await _run_status_cli([codex_bin, "login", "status"])
        codex["logged_in"], codex["account"] = _parse_codex_login(rc, out)

    return {"claude": claude, "codex": codex}


async def _agent_auth_refresh() -> None:
    global _AGENT_AUTH_REFRESHING
    try:
        data = await _probe_agent_auth()
        _AGENT_AUTH_CACHE["at"], _AGENT_AUTH_CACHE["data"] = time.time(), data
    except Exception as e:  # noqa: BLE001 — 探測失敗不該打斷 dashboard
        _log_event("agent_auth_probe_failed", error=str(e)[:160])
    finally:
        _AGENT_AUTH_REFRESHING = False


async def _agent_auth_status() -> dict:
    """CC/CX 登入狀態,TTL 60s 快取。**永不阻塞請求** —— 過期就丟背景刷新、
    先回上次結果;從未探測完成則回 logged_in=null(檢查中,app fail-open 不凍結)。
    冷啟首探可能 10s+(claude/codex 首次 spawn 慢),所以絕不擋在請求路徑上。"""
    global _AGENT_AUTH_REFRESHING
    now = time.time()
    data = _AGENT_AUTH_CACHE["data"]
    fresh = data is not None and now - _AGENT_AUTH_CACHE["at"] < _AGENT_AUTH_TTL
    if not fresh and not _AGENT_AUTH_REFRESHING:
        _AGENT_AUTH_REFRESHING = True
        asyncio.create_task(_agent_auth_refresh())
    if data is not None:
        return data
    return {"claude": {"installed": os.path.exists(CLAUDE_BIN),
                       "logged_in": None, "account": None},
            "codex": {"installed": os.path.exists(_resolve_codex_bin()),
                      "logged_in": None, "account": None},
            "checking": True}


class CodexAppServerError(RuntimeError):
    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.code = code


# ── thread-store 寫入鎖衝突（桌面版 Codex/ChatGPT 佔用同一條 thread）──────────
# 2026-08-10 實機診斷(善彰的機器):ChatGPT.app 自帶的 codex app-server
# (`/Applications/ChatGPT.app/Contents/Resources/codex … app-server`)握著某些
# thread 的 **writer lock**,bridge 這一顆 app-server 的 thread/resume 直接被拒。
# 兩個訊號同時出現:
#   1) JSON-RPC 回覆(**主訊號**,可靠、帶得到 thread id):
#        {"error": {"code": -32600,
#                   "message": "thread <uuid> already has an active writer"}}
#   2) app-server stderr(輔助訊號,由 `codex_app_server_stderr` 收):
#        "... ERROR codex_core::session::session: failed to initialize thread
#         persistence: thread-store conflict: thread <uuid> already has an
#         active writer"
# 舊行為的三個坑,本 PR 全部修掉:
#   • -32600 一律被翻成 CX_TURN_IN_FLIGHT(「上一輪正在跑」)—— 語意完全相反,
#     使用者被引導去等一個根本不存在的回合。
#   • warm loop 只記 `codex_thread_warm_failed` + error type,人話全丟。
#   • 沒有任何 UI 訊號:點了沒反應、送出像是排隊卻永遠不動。
# 註:-32600 是 codex 的泛用「Invalid request」碼(同碼也出現在 unknown
# variant 之類的錯誤),所以判定**以訊息文字為準**,不能只看 code。
_CX_THREAD_LOCK_MARKERS = ("already has an active writer", "thread-store conflict")
_CX_THREAD_LOCKED_CODE = -32098          # bridge 內部 sentinel,不與 codex 撞碼
_CX_UUID_PATTERN = (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# **貼著 marker 抓**,不能只做「訊息裡第一個 uuid」:stderr 行前面可能掛著
# tracing span 欄位(`session{conversation_id=…}`),抓到第一個 uuid 就會把鎖
# 記到**另一條無辜的 thread** 上 —— 那條會被掛 banner、停用輸入框、跳過 warm,
# 真正被鎖的那條反而沒事。抓不到貼身的才退回泛用搜尋。
_CX_THREAD_LOCK_ID_RE = re.compile(
    r"thread[\s:]+(" + _CX_UUID_PATTERN + r")\s+already has an active writer",
    re.IGNORECASE)
_CX_THREAD_ID_RE = re.compile(_CX_UUID_PATTERN)
# 鎖住之後的重試抑制窗:窗內不再打 thread/resume(否則 warm loop 幾秒一次的
# 重試風暴會一路洗版到 log 爆掉),窗過了才允許再試一次 —— 那一次同時就是
# 「桌面端有沒有放開」的探針,所以 banner 不需要重啟 bridge 就會自己消失。
CODEX_THREAD_LOCK_RETRY_SECS = float(
    os.environ.get("CODEX_THREAD_LOCK_RETRY_SECS", "300"))
CX_THREAD_LOCKED_MESSAGE = (
    "此對話正被桌面版 Codex/ChatGPT 佔用(thread 寫入鎖)。"
    "請在桌面 app 關閉這個對話後再試,或改用另一條 session。")
CX_THREAD_LOCKED_CARD_TEXT = "⚠️ " + CX_THREAD_LOCKED_MESSAGE
CX_THREAD_UNLOCKED_CARD_TEXT = (
    "✅ 桌面版 Codex/ChatGPT 已釋放這個對話的寫入鎖,現在可以正常送出了。")
CX_THREAD_LOCKED_REASON = "thread_store_conflict"


def _codex_thread_lock_text(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _CX_THREAD_LOCK_MARKERS)


def _codex_thread_lock_conflict(exc) -> str | None:
    """thread-store 寫入鎖衝突判定。

    回傳值刻意分三態,不用 bool —— 呼叫端常常同時要問「是不是鎖」與「鎖的是
    哪一條」:
      • None → 不是這個狀況(一般 app-server 錯誤,**不可**誤判成鎖)
      • ""   → 是鎖,但訊息裡抓不到 thread id
      • uuid → 是鎖,且這是訊息裡帶的 thread id
    """
    if getattr(exc, "code", None) == _CX_THREAD_LOCKED_CODE:
        message = str(exc or "")
    else:
        message = str(exc or "")
        if not _codex_thread_lock_text(message):
            return None
    anchored = _CX_THREAD_LOCK_ID_RE.search(message)
    if anchored:
        return anchored.group(1)
    found = _CX_THREAD_ID_RE.search(message)
    return found.group(0) if found else ""


# ─────────── codex app-server：server → client request（ServerRequest）───────────
# codex 二進位 `ServerRequest` 全集（strings 抽出的 serde variant 表）:
#   item/commandExecution/requestApproval   item/fileChange/requestApproval
#   execCommandApproval  applyPatchApproval        ← 舊 v1 名字（bridge 早就接了）
#   item/tool/call                                 ← DynamicToolCall（plugin 工具）
#   item/tool/requestUserInput                     ← ToolRequestUserInput
#   mcpServer/elicitation/request                  ← MCP elicitation
#   item/permissions/requestApproval               ← PermissionsRequestApproval
#   currentTime/read  attestation/generate  account/chatgptAuthTokens/refresh
# 之前除了前四個以外一律回 JSON-RPC -32601 → codex 端把它當「client 壞掉」，
# 整個 turn 直接失敗。實機 log 已抓到 item/tool/call 被拒（2026-08-07 ×3）。
CODEX_APPROVAL_REQUEST_METHODS = (
    "execCommandApproval",
    "applyPatchApproval",
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
)
CODEX_QUESTION_REQUEST_METHODS = (
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
)


def _codex_current_time_at() -> int:
    """CurrentTimeReadResponse.currentTimeAt（1 element）。

    型別以 codex 0.144.6 `app-server generate-json-schema` / 二進位 serde
    字串表為準：`currentTimeAt: number`，doc 寫死「Current time as whole
    Unix seconds」。先前送 RFC3339 字串會 deserialize 失敗，等於沒接。
    """
    return int(time.time())


def _codex_safe_question_result(method: str) -> dict:
    """讀不懂 / 沒人回答時，該 method 的「不打斷 turn」安全回覆。

    形狀取自 codex app-server v2 schema（ToolRequestUserInputResponse{answers}、
    McpServerElicitationRequestResponse{action,content,_meta}），與 OpenClaw
    的 `defaultServerRequestResponse` 一致。
    """
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None, "_meta": None}
    if method == "item/tool/call":
        return {"contentItems": [{"type": "inputText",
                                  "text": "This client did not handle the tool call."}],
                "success": False}
    if method == "item/permissions/requestApproval":
        return _codex_permissions_deny_result()
    return {}


# PermissionsRequestApprovalResponse 是 **3 elements**(codex 0.144.6 二進位
# serde 表:`struct PermissionsRequestApprovalResponse with 3 element`;
# `app-server generate-json-schema` 也給同一份:permissions /
# scope / strictAutoReview)。只送 2 欄會賭 serde 有沒有替
# `strictAutoReview` 補 default —— 賭輸就是 deserialize 失敗、turn 陣亡,
# 正是這個 PR 要修的那個病。三欄全帶在兩種解讀下都合法,所以一律帶滿。
#   permissions      GrantedPermissionProfile{network?,fileSystem?}
#                    → 兩個都給 null = 一項都不加授
#   scope            PermissionGrantScope enum = "turn" | "session"
#                    → "turn"(最窄)
#   strictAutoReview bool|null,doc:「Review every subsequent command in this
#                    turn before normal sandboxed execution.」→ true(最保守)
def _codex_permissions_deny_result() -> dict:
    return {"permissions": {"network": None, "fileSystem": None},
            "scope": "turn",
            "strictAutoReview": True}


def _codex_read_user_input_questions(params: dict) -> list[dict]:
    """ToolRequestUserInputParams.questions[] → 正規化清單。

    wire 欄位（codex WireToolRequestUserInputParams）:
      threadId / turnId / itemId / questions / isBlocking / autoResolutionMs
    question: id / header / question / isOther / isSecret / options[{label,description}]
    """
    raw = params.get("questions")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        qid = item.get("id")
        header = item.get("header")
        question = item.get("question")
        if not isinstance(qid, str) or not qid:
            continue
        options = []
        for opt in (item.get("options") or []) if isinstance(item.get("options"), list) else []:
            if not isinstance(opt, dict):
                continue
            label = opt.get("label")
            if isinstance(label, str) and label:
                options.append({"label": label,
                                "description": str(opt.get("description") or "")})
        out.append({"id": qid,
                    "header": header if isinstance(header, str) else "",
                    "question": question if isinstance(question, str) else "",
                    "isOther": item.get("isOther") is True,
                    "isSecret": item.get("isSecret") is True,
                    "options": options})
    return out


def _codex_user_input_detail(questions: list[dict]) -> str:
    lines = []
    for idx, q in enumerate(questions):
        if len(questions) > 1:
            lines.append(f"{idx + 1}. {q.get('header') or ''}".strip())
        elif q.get("header"):
            lines.append(str(q["header"]))
        if q.get("question"):
            lines.append(str(q["question"]))
        for opt_idx, opt in enumerate(q.get("options") or []):
            desc = opt.get("description") or ""
            lines.append(f"{opt_idx + 1}. {opt['label']}" + (f" — {desc}" if desc else ""))
        if q.get("isOther"):
            lines.append("其他:可自行輸入答案。")
        lines.append("")
    return "\n".join(lines).strip()


def _codex_build_user_input_answers(record: dict, key: str, text: str) -> dict:
    """ToolRequestUserInputResponse = {answers: {qid: {answers: [str, ...]}}}

    （形狀對齊 OpenClaw `buildAgentHarnessUserInputAnswers`。）只有第一題
    能用選項鈕回答（卡片一次只有一組選項）；其餘題目回空陣列 = 未作答。
    """
    questions = record.get("questions") or []
    if not questions:
        return {"answers": {}}
    first = questions[0]
    answer = ""
    if key and key.startswith("opt"):
        try:
            idx = int(key[3:])
        except ValueError:
            idx = -1
        opts = first.get("options") or []
        if 0 <= idx < len(opts):
            answer = opts[idx]["label"]
    elif key == "deny":
        answer = ""
    if not answer and text:
        # 自由輸入:對得上選項就用選項 label（codex 期待原字串），否則原文。
        trimmed = text.strip()
        match = next((o["label"] for o in (first.get("options") or [])
                      if o["label"].lower() == trimmed.lower()), "")
        if match:
            answer = match
        elif first.get("isOther") or not first.get("options"):
            answer = trimmed
    answers = {q["id"]: {"answers": []} for q in questions}
    if answer:
        answers[first["id"]] = {"answers": [answer]}
    return {"answers": answers}


CODEX_SECRET_ANSWER_PLACEHOLDER = "[redacted:secret]"


def _codex_redact_secret_answers(record: dict, result: dict) -> dict:
    """把 `isSecret` 題目的答案換成佔位字串，回傳新的 dict。

    `ToolRequestUserInputQuestion.isSecret` 之前只被解析、沒被使用，導致使用者
    貼進來的 API key / 密碼原文直接寫進 CANON_DB 的 `approvals.result`
    （實測抓到 `sk-live-…` 明文）。真答案只准存在於「送給 app-server 的那一個
    JSON-RPC frame」；任何會留痕的地方（DB、卡片流、log、HTTP 回應）一律用
    這份遮罩版。
    """
    if record.get("method") != "item/tool/requestUserInput":
        return result
    secret_ids = {q.get("id") for q in (record.get("questions") or [])
                  if isinstance(q, dict) and q.get("isSecret")}
    if not secret_ids:
        return result
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        return result
    masked = {}
    for qid, ans in answers.items():
        vals = ans.get("answers") if isinstance(ans, dict) else None
        if qid in secret_ids and isinstance(vals, list) and vals:
            masked[qid] = {"answers": [CODEX_SECRET_ANSWER_PLACEHOLDER for _ in vals]}
        else:
            masked[qid] = ans
    return {**result, "answers": masked}


def _codex_record_has_secret(record: dict) -> bool:
    return any(isinstance(q, dict) and q.get("isSecret")
               for q in (record.get("questions") or []))


def _codex_build_elicitation_response(record: dict, key: str) -> dict:
    """McpServerElicitationRequestResponse = {action, content, _meta}（3 elements）。

    accept 只在「requestedSchema 沒有欄位要填」時成立；有欄位卻無法對應
    （bridge 沒有表單填寫 UI）就退成 decline —— 與 OpenClaw 同款保守處理，
    寧可 decline 也不要送半套 content 讓 MCP server 拿到壞資料。
    """
    if not key or key == "deny":
        return {"action": "decline", "content": None, "_meta": None}
    schema = record.get("requested_schema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        _log_event("codex_elicitation_accept_unmappable",
                   approval_id=record.get("id"),
                   fields=",".join(sorted(str(k) for k in props.keys()))[:200])
        return {"action": "decline", "content": None, "_meta": None}
    return {"action": "accept", "content": None, "_meta": None}


class CodexAppServerClient:
    """Small JSON-RPC client for `codex app-server --stdio`.

    The bridge keeps one app-server connection warm and exposes Codex threads as
    PocketAgent-controllable sessions. This is the correct sync surface for
    Codex App/CLI threads; `codex exec` remains only a fallback path.
    """

    def __init__(self):
        self.proc = None
        self.ws = None
        self.transport = ""
        self.spawned_bin = ""
        # 上一次「印出來」的傳輸種類,用來做 once-per-transition 的日誌節流。
        self._last_transport_logged = ""
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._pending = {}
        self._reader_task = None
        self._stderr_task = None
        self.thread_events = collections.defaultdict(list)
        self.thread_event_generations = collections.defaultdict(int)
        # 忙碌時的待送佇列(CX 排隊層)。CC 早就有「一定收下、回 queued」的語意,
        # CX 卻是直送 app-server 撞牆回 4xx → app 標「送出失敗」。這裡補上對稱行為。
        self.pending_inputs = collections.defaultdict(list)
        self.active_turns = {}
        self.turn_started_at = {}
        self.turn_terminal_at = {}
        self.turn_watchdogs = {}
        self.last_event_at = {}
        self.thread_errors = {}
        self.loaded_threads = set()
        # thread-store 寫入鎖(被別的 codex app-server 佔用)。
        # thread_id -> {"since": wall, "last_at": wall, "attempts": int,
        #               "detail": str, "next_retry_at": monotonic}
        # 只有「resume/turn 成功」才會清掉 → 狀態欄的 locked 旗標會自己翻回來。
        self.thread_locks = {}
        self.remote_status = None
        self.app_server_error = ""
        self.server_started_at = 0.0
        self._streamed_item_ids = set()
        self.pending_approvals = {}
        self.pending_approvals_by_thread = collections.defaultdict(dict)
        # (thread_id, tool) 去重：同一條 thread 的同一個 plugin 工具只在
        # 對話裡提示一次「這裡跑不動」，模型連打時不洗版。
        self._dynamic_tool_notices = set()
        # wave 2: live token usage per thread (thread/tokenUsage/updated) —
        # thread/list reports tokenUsage: null, so this is the only source.
        self.token_usage = {}

    async def call(self, method: str, params: dict | None = None, timeout: float = 30.0):
        async with self._lock:
            await self._ensure_started_locked()
            rid = self._next_id
            self._next_id += 1
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            await self._write_locked({"jsonrpc": "2.0", "id": rid,
                                      "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise CodexAppServerError(f"{method} timed out") from e

    async def notify(self, method: str, params: dict | None = None):
        async with self._lock:
            await self._ensure_started_locked()
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            await self._write_locked(msg)

    async def _connect_managed_locked(self):
        """接上桌面版共用的 managed daemon(unix socket 上跑 WebSocket frames)。

        連得上才算數:socket 檔存在與否完全不參與判斷,因為 daemon 崩潰後
        inode 會留著(stale socket),`exists()` 會騙人。
        """
        import websockets
        self.ws = await websockets.unix_connect(
            CODEX_APP_SERVER_SOCKET,
            uri="ws://localhost/",
            compression=None,
            # 單一訊息上限。thread/turns/list itemsView=full 若含 computer-use
            # 截圖(base64)可能單則破 8MB → 預設 1MB 上限會直接把連線關掉。
            # 對齊 stdio 那條路徑的 128MB,吃得下含圖的大回應。
            max_size=128 * 1024 * 1024,
            user_agent_header="PocketAgent-Bridge/0.1",
        )
        self.transport = "unix-websocket"
        self.spawned_bin = "managed-daemon"
        self._reader_task = asyncio.create_task(self._read_websocket())

    async def _spawn_stdio_locked(self):
        """自己 spawn 一顆 `codex app-server --stdio`(沒有共用 daemon 時的退路)。"""
        codex_bin = _resolve_codex_bin()   # 每次 spawn 重新解析:桌面 app 更新後路徑會變
        self.spawned_bin = codex_bin
        try:
            self.proc = await asyncio.create_subprocess_exec(
                codex_bin, "app-server", "--stdio",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=HOME_ROOT,
                # StreamReader 單行上限。codex app-server 把每個 JSON-RPC 回應當「一行」
                # 送;thread/turns/list itemsView=full 若含 computer-use 截圖(base64)可能
                # 單行破 8MB → asyncio 讀取器丟 LimitOverrunError(「Separator is not found,
                # and chunk exceed the limit」)→ reader task 死 → app-server「stopped」→ 整條
                # codex 卡死(XCash 就是這樣)。放大到 128MB 吃得下含圖的大回應。
                limit=128 * 1024 * 1024,
            )
        except (FileNotFoundError, PermissionError) as e:
            _log_event("codex_spawn_failed", bin=codex_bin, error=type(e).__name__)
            raise CodexAppServerError(f"codex binary unavailable: {codex_bin}") from e
        self.transport = "stdio"
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    def _note_transport(self, transport: str, **fields):
        """只在「傳輸換人做」的時候印一行,不要每次重試都洗版。"""
        if self._last_transport_logged == transport:
            return
        self._last_transport_logged = transport
        _log_event("codex_transport_selected", transport=transport,
                   mode=CODEX_APP_SERVER_MODE, **fields)

    async def _ensure_started_locked(self):
        if self.ws is not None or (self.proc and self.proc.returncode is None):
            return
        self.app_server_error = ""
        mode = CODEX_APP_SERVER_MODE
        if mode not in CODEX_APP_SERVER_MODES:
            mode = "auto"
        managed_error = None
        if mode in ("auto", "managed"):
            try:
                await self._connect_managed_locked()
            except Exception as e:  # noqa: BLE001
                # FileNotFoundError(socket 不存在)/ ConnectionRefusedError
                # (stale socket,Errno 61)/ PermissionError / handshake 失敗 /
                # timeout——全部一視同仁:managed 這條路不通。
                self.ws = None
                self.transport = ""
                managed_error = e
                _log_event("codex_managed_connect_failed",
                           socket=CODEX_APP_SERVER_SOCKET, mode=mode,
                           error=type(e).__name__, error_message=str(e)[:200])
                if mode == "managed":
                    # 刻意選 daemon-only 的人:大聲壞掉,不要偷偷開第二顆
                    # app-server 去搶 thread-store 的 writer lock。
                    raise CodexAppServerError(
                        "managed Codex app-server unavailable; refusing to spawn a second server"
                    ) from e
        if self.ws is not None:
            self._note_transport("unix-websocket", socket=CODEX_APP_SERVER_SOCKET)
        else:
            await self._spawn_stdio_locked()
            self._note_transport(
                "stdio", bin=self.spawned_bin,
                fallback_from=("managed" if managed_error is not None else ""),
                fallback_reason=(type(managed_error).__name__
                                 if managed_error is not None else ""))
        # P1-3:這批清理(含一發 sqlite UPDATE 把 pending approvals 標 expired)
        # 只有在**真的接上傳輸**之後才做。放在連線之前的話,daemon 短暫不在
        # (桌面 app 更新/重開)就會把使用者手上待批准的 Codex 請求全部作廢,
        # 而且每次重試都再打一次 DB。
        self._pending.clear()
        self.pending_approvals.clear()
        self.pending_approvals_by_thread.clear()
        self._expire_stale_codex_approvals()
        self.server_started_at = time.time()
        init = await self._call_started_locked(
            "initialize",
            {
                "clientInfo": {
                    "name": "pocketagent-bridge",
                    "title": "PocketAgent Bridge",
                    "version": "0.1",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=10.0,
        )
        await self._write_locked({"jsonrpc": "2.0", "method": "initialized"})
        _log_event("codex_app_server_started",
                   user_agent=(init or {}).get("userAgent", ""),
                   codex_home=(init or {}).get("codexHome", ""),
                   bin=self.spawned_bin, transport=self.transport)

    def _expire_stale_codex_approvals(self):
        import sqlite3
        try:
            con = sqlite3.connect(CANON_DB, timeout=30)
            try:
                cur = con.execute(
                    "UPDATE approvals SET status='expired', decided_at=?, result=? "
                    "WHERE status='pending' AND source LIKE 'codex%'",
                    (time.time(), json.dumps({"reason": "codex app-server restarted"},
                                             ensure_ascii=False)))
                con.commit()
                changed = cur.rowcount
                con.close()
                if changed:
                    _log_event("codex_approval_stale_expired", count=changed)
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_stale_expire_failed",
                       error=type(e).__name__, error_message=str(e)[:160])

    async def _call_started_locked(self, method: str, params: dict, timeout: float):
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._write_locked({"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise CodexAppServerError(f"{method} timed out") from e

    async def _write_locked(self, msg: dict):
        if self.ws is not None:
            wire = dict(msg)
            wire.pop("jsonrpc", None)
            try:
                await self.ws.send(
                    json.dumps(wire, ensure_ascii=False, separators=(",", ":")))
            except Exception as e:  # noqa: BLE001
                # P1-4:daemon 在寫入當下掉線會丟 websockets 的 ConnectionClosed。
                # 呼叫端只認 CodexAppServerError,原始例外會一路逃到 handler 外面
                # 變成 500。統一翻譯成 CodexAppServerError。
                raise CodexAppServerError(
                    f"codex app-server send failed: {type(e).__name__}") from e
            return
        if not self.proc or not self.proc.stdin:
            raise CodexAppServerError("codex app-server is not running")
        raw = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.proc.stdin.write(raw)
        await self.proc.stdin.drain()

    async def _dispatch_wire_message(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_app_server_bad_json", error=type(e).__name__,
                       line=str(raw)[:160])
            return
        if msg.get("method"):
            await self._handle_server_message(msg)
        elif "id" in msg:
            fut = self._pending.pop(msg.get("id"), None)
            if not fut or fut.done():
                _log_event("codex_app_server_unmatched_response",
                           id_hash=_short_hash(str(msg.get("id"))))
                return
            if "error" in msg:
                err = msg.get("error") or {}
                fut.set_exception(CodexAppServerError(
                    err.get("message") or "codex app-server error", err.get("code")))
            else:
                fut.set_result(msg.get("result"))
        else:
            _log_event("codex_app_server_unknown_message",
                       keys=",".join(sorted(str(k) for k in msg.keys()))[:120])

    async def _read_stdout(self):
        proc = self.proc
        try:
            while proc and proc.stdout:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                await self._dispatch_wire_message(raw)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_app_server_reader_failed", error=type(e).__name__,
                       error_message=str(e)[:160])
        finally:
            await self._reader_cleanup(proc=proc)

    async def _read_websocket(self):
        # 注意:`recv()` 不會回 None——連線關掉是用丟例外表示的
        # (ConnectionClosedOK / ConnectionClosedError),所以這裡沒有
        # `if raw is None: break` 那種死碼。
        ws = self.ws
        try:
            while ws is not None and self.ws is ws:
                raw = await ws.recv()
                await self._dispatch_wire_message(raw)
        except Exception as e:  # noqa: BLE001
            if _is_clean_ws_closure(e):
                # P2-6:使用者正常關掉 ChatGPT.app 就是這條。以前一律記成
                # `codex_app_server_websocket_failed`,每關一次桌面 app 就假警報。
                _log_event("codex_app_server_websocket_closed",
                           reason=type(e).__name__, detail=str(e)[:160])
            else:
                _log_event("codex_app_server_websocket_failed", error=type(e).__name__,
                           error_message=str(e)[:160])
        finally:
            await self._reader_cleanup(ws=ws)

    async def _reader_cleanup(self, proc=None, ws=None):
        stopped_at = time.time()
        active = list(self.active_turns)
        self.app_server_error = "codex app-server stopped"
        for tid in active:
            self.thread_errors[tid] = self.app_server_error
            self.active_turns.pop(tid, None)
            self.turn_terminal_at[tid] = stopped_at
            self.last_event_at[tid] = stopped_at
            self._append(tid, ("text", "\n⚠️ Codex app-server 已掉線，這回合已中止。\n"))
            task = self.turn_watchdogs.pop(tid, None)
            if task and task is not asyncio.current_task():
                task.cancel()
            try:
                t = asyncio.create_task(_delegation_codex_completed(
                    tid, True, self.app_server_error))
                _BG_TASKS.add(t)
                t.add_done_callback(_BG_TASKS.discard)
            except RuntimeError:
                pass
        self.loaded_threads.clear()
        if proc is not None and self.proc is proc:
            self.proc = None
        if ws is not None and self.ws is ws:
            self.ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(CodexAppServerError("codex app-server stopped"))
        self._pending.clear()

    async def _read_stderr(self):
        try:
            while self.proc and self.proc.stderr:
                raw = await self.proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").strip()
                if text and "WARNING: proceeding" not in text:
                    _log_event("codex_app_server_stderr", message=text[:240])
                    self._note_stderr_thread_lock(text)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("CodexAppServerClient._read_stderr", _exc, expected=True)
            pass

    def _note_stderr_thread_lock(self, text: str) -> None:
        """輔助訊號:app-server 把 thread-store 衝突也吐在 stderr,而且訊息本身
        就帶 thread id,所以不需要另外做時間相關性猜測。主訊號仍是 JSON-RPC
        error(見 `_codex_thread_lock_conflict`);這裡只是替「RPC 那條路沒被
        呼叫端接住」的情況補一層;兩邊撞在一起時,log 由 `note_thread_locked`
        的抑制窗去重、卡片由 `_cx_feed_thread_locked` 的 lock.since 去重。"""
        thread_id = _codex_thread_lock_conflict(
            CodexAppServerError(text)) if _codex_thread_lock_text(text) else None
        if not thread_id:
            return
        if thread_id in self.loaded_threads:
            # stderr 是獨立的 reader 協程,可能落在一堆 log 後面才被讀到。這條
            # thread 現在已經 resume 成功(寫入權在我們手上)= 這是**過期**的
            # 舊訊息;照單全收會把一條好好的 session 打回 locked、推一張假的
            # 錯誤卡,而且鎖住 banner 整整一個抑制窗。
            return
        if self.note_thread_locked(thread_id, text):
            _log_event("codex_thread_locked", thread=thread_id[:16],
                       source="stderr", error_message=text[:200],
                       suppress_secs=CODEX_THREAD_LOCK_RETRY_SECS)
        _cx_feed_thread_locked(thread_id)

    def _append(self, thread_id: str, item):
        if not thread_id:
            return
        self.last_event_at[thread_id] = time.time()
        buf = self.thread_events[thread_id]
        buf.append(item)
        if len(buf) > 2000:
            del buf[:500]

    async def _handle_server_message(self, msg: dict):
        method = msg.get("method")
        if "id" in msg:
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method in CODEX_APPROVAL_REQUEST_METHODS:
                self._handle_approval_request(msg)
                return
            if method == "currentTime/read":
                # 瑣碎且無副作用:直接照 CurrentTimeReadResponse{currentTimeAt}
                # 正確回覆。回 -32601 只會讓需要時鐘的 turn 平白失敗。
                await self._write_server_result_safe(
                    msg.get("id"), {"currentTimeAt": _codex_current_time_at()})
                return
            if method == "item/tool/call":
                await self._handle_dynamic_tool_call(msg)
                return
            if method in CODEX_QUESTION_REQUEST_METHODS:
                await self._handle_question_request(msg)
                return
            if method == "item/permissions/requestApproval":
                # 模型要求「這回合放寬權限」。bridge 沒有把 8 欄位的
                # PermissionsRequestApprovalParams 安全回授的能力，所以一律
                # 回「不加授任何權限」（= OpenClaw supervisor 的 safe answer）。
                # 語意上等同拒絕，但 turn 活著，後續指令頂多吃到正常的權限
                # 錯誤，而不是整回合 -32601 陣亡。
                _log_event("codex_permissions_request_not_granted",
                           thread_id_hash=_short_hash(str(params.get("threadId") or "")),
                           turn_id_hash=_short_hash(str(params.get("turnId") or "")),
                           id_hash=_short_hash(str(msg.get("id"))),
                           param_keys=",".join(sorted(str(k) for k in params.keys()))[:200])
                await self._write_server_result_safe(
                    msg.get("id"), _codex_safe_question_result(method))
                return
            # 仍未支援:attestation/generate、account/chatgptAuthTokens/refresh
            # （codex 自己在 exec mode 也是回錯，bridge 沒有能力代簽/換 token）。
            # log 要夠診斷:方法、thread、params 欄位名。
            _log_event("codex_app_server_unhandled_request",
                       method=str(method or "")[:120],
                       id_hash=_short_hash(str(msg.get("id"))),
                       thread_id_hash=_short_hash(str(params.get("threadId") or "")),
                       turn_id_hash=_short_hash(str(params.get("turnId") or "")),
                       param_keys=",".join(sorted(str(k) for k in params.keys()))[:200])
            await self._write_server_error(msg.get("id"), -32601,
                                           f"server request not implemented: {method}")
            return
        self._handle_notification(msg)

    # ───────── item/tool/call（DynamicToolCall）─────────
    async def _handle_dynamic_tool_call(self, msg: dict):
        """Codex 要 client 代跑一個 dynamic tool（使用者 config.toml 裡那批
        plugin：documents/spreadsheets/computer-use/chrome/browser…）。

        bridge 不是 plugin runtime 宿主 —— 那些工具的實作住在桌面 Codex/
        ChatGPT.app 的 plugin runtime 裡，bridge 這條 stdio client 沒有能力
        執行它們。但**回 -32601 會讓整個 turn 失敗**（實機 log:2026-08-07
        同日三次），而回一個 DynamicToolCallResponse{contentItems,success:false}
        只會讓模型看到「這個工具失敗了」然後自己換路走 —— turn 活著。
        """
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "")
        tool = str(params.get("tool") or "")
        namespace = params.get("namespace")
        call_id = str(params.get("callId") or "")
        label = f"{namespace}/{tool}" if namespace else tool
        _log_event("codex_dynamic_tool_call_unsupported",
                   tool=tool[:120],
                   namespace=str(namespace or "")[:120],
                   thread_id_hash=_short_hash(thread_id),
                   turn_id_hash=_short_hash(str(params.get("turnId") or "")),
                   call_id_hash=_short_hash(call_id),
                   id_hash=_short_hash(str(msg.get("id"))))
        await self._write_server_result_safe(msg.get("id"), {
            "contentItems": [{
                "type": "inputText",
                "text": (f"Tool `{label or 'unknown'}` is not available in this "
                         "session. It is provided by a Codex plugin whose runtime "
                         "only exists in the desktop Codex client; this thread is "
                         "driven by the Pocket/Hermes bridge, which cannot execute "
                         "plugin tools. Do not retry this tool — use built-in tools "
                         "(shell, file edits) or ask the user to run it from the "
                         "desktop Codex app."),
            }],
            "success": False,
        })
        # 同一個 thread 的同一個工具只提示一次，避免模型連打時洗版。
        if thread_id and (thread_id, label) not in self._dynamic_tool_notices:
            if len(self._dynamic_tool_notices) > 500:
                self._dynamic_tool_notices.clear()
            self._dynamic_tool_notices.add((thread_id, label))
            self._append(thread_id, ("text",
                                     f"\n⚠️ Codex plugin 工具 `{label}` 在 Pocket 開的 "
                                     "thread 無法執行（plugin runtime 只在桌面 Codex 裡）。"
                                     "這回合已回報工具失敗，turn 不會中斷。\n"))

    # ───────── 問使用者問題（requestUserInput / MCP elicitation）─────────
    async def _handle_question_request(self, msg: dict):
        """`item/tool/requestUserInput` 與 `mcpServer/elicitation/request`
        語意上都是「問使用者一題」，翻成既有的 approval/question 卡走卡片流；
        使用者的答案透過 `/app/v1/approvals/{id}/decision` 或
        `POST /codexsessions/{thread_id}/answer` 回到 app-server。

        建卡失敗（例如 params 讀不懂）也**不能**丟 -32601 —— 一律退回該
        method 的安全預設回覆（空答案 / decline），turn 才活得下來。
        """
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            record = self._build_question_record(msg, method, params)
        except Exception as e:  # noqa: BLE001
            record = None
            _log_event("codex_question_request_parse_failed",
                       method=method[:120], error=type(e).__name__,
                       error_message=str(e)[:160])
        if not record:
            _log_event("codex_question_request_fallback",
                       method=method[:120],
                       id_hash=_short_hash(str(msg.get("id"))),
                       param_keys=",".join(sorted(str(k) for k in params.keys()))[:200])
            await self._write_server_result_safe(
                msg.get("id"), _codex_safe_question_result(method))
            return
        self._register_pending_request(record)
        _log_event("codex_question_request",
                   approval_id=record["id"], method=method[:120],
                   thread_id_hash=_short_hash(record.get("thread_id") or ""),
                   request_id_hash=_short_hash(str(msg.get("id"))),
                   options=len(record.get("options") or []))
        auto_ms = record.get("auto_resolution_ms")
        if isinstance(auto_ms, (int, float)) and auto_ms > 0:
            self._arm_question_auto_resolution(record["id"], float(auto_ms) / 1000.0)

    def _build_question_record(self, msg: dict, method: str, params: dict) -> dict | None:
        thread_id = str(params.get("threadId") or "")
        request_id = msg.get("id")
        created = time.time()
        stable = json.dumps([thread_id, method, request_id], sort_keys=True,
                            ensure_ascii=False, default=str)
        approval_id = "codex-" + hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()[:24]
        if method == "item/tool/requestUserInput":
            questions = _codex_read_user_input_questions(params)
            if not questions:
                return None
            first = questions[0]
            options = [{"key": f"opt{i}", "label": opt["label"],
                        "style": "primary" if i == 0 else "secondary",
                        "send": opt["label"]}
                       for i, opt in enumerate(first.get("options") or [])]
            options.append({"key": "deny", "label": "略過", "style": "deny"})
            title = first.get("header") or "Codex 需要你的回答"
            detail = _codex_user_input_detail(questions)
            extra = {"questions": questions,
                     "auto_resolution_ms": params.get("autoResolutionMs")}
        elif method == "mcpServer/elicitation/request":
            server_name = str(params.get("serverName") or "MCP server")
            message = str(params.get("message") or "")
            title = f"{server_name} 需要你的確認"
            detail = "\n".join(x for x in [title, message,
                                           f"mode: {params.get('mode')}"
                                           if params.get("mode") else ""] if x)
            options = [{"key": "approve", "label": "允許", "style": "primary"},
                       {"key": "deny", "label": "拒絕", "style": "deny"}]
            extra = {"requested_schema": params.get("requestedSchema")}
        else:
            return None
        record = {
            "id": approval_id,
            "request_id": request_id,
            "method": method,
            "params": params,
            "thread_id": thread_id,
            "kind": "question",
            "title": title,
            "source": f"codex:{thread_id}" if thread_id else "codex",
            "risk": "low",
            "detail": detail,
            "created_at": created,
            "options": options,
        }
        record.update(extra)
        return record

    def _arm_question_auto_resolution(self, approval_id: str, delay: float) -> None:
        """codex 自己宣告的 autoResolutionMs 到了就替使用者回空答案，
        免得沒人看手機時 turn 永遠掛著。"""
        async def _run():
            try:
                await asyncio.sleep(max(1.0, delay))
                record = self.pending_approvals.get(approval_id)
                if not record:
                    return
                await self.answer_question(approval_id, key="", text="",
                                           auto=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                _log_event("codex_question_auto_resolution_failed",
                           approval_id=approval_id, error=type(e).__name__,
                           error_message=str(e)[:160])
        try:
            task = asyncio.create_task(_run())
            _BG_TASKS.add(task)
            task.add_done_callback(_BG_TASKS.discard)
        except RuntimeError:
            pass

    def _claim_pending(self, approval_id: str) -> dict | None:
        """把 pending server request 從記憶體「認領」出來（pop-before-write）。

        先寫 response 再 pop 會讓同一個 JSON-RPC id 被回兩次（協定違規，實測
        兩個 result frame）—— autoResolution timer 與使用者按鈕、卡片與
        thread 層 API、兩台裝置同時按，都會撞。改成先 pop：只有第一個
        claim 得到 record，第二個拿到 None → 409，永遠只寫一個 response。
        """
        record = self.pending_approvals.pop(approval_id, None)
        if record is None:
            return None
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.pending_approvals_by_thread.get(thread_id, {}).pop(approval_id, None)
        return record

    def _unclaim_pending(self, record: dict) -> None:
        """認領後沒寫成功（app-server 掉線 / kind 不符）就放回去，
        免得卡片變成死卡、使用者連重試的機會都沒有。"""
        approval_id = record.get("id")
        if not approval_id:
            return
        self.pending_approvals[approval_id] = record
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.pending_approvals_by_thread[thread_id][approval_id] = record

    async def answer_question(self, approval_id: str, key: str = "",
                              text: str = "", auto: bool = False) -> dict:
        """question 類 server request（requestUserInput / elicitation）決議。"""
        record = self._claim_pending(approval_id)
        if not record:
            raise CodexAppServerError("codex question is no longer pending", code=404)
        if (record.get("kind") or "") != "question":
            self._unclaim_pending(record)
            raise CodexAppServerError(
                "這是 Codex 的審批請求（不是問答），請用 approve/deny 決議，"
                "不要用作答介面。", code=409)
        result = self._question_response_result(record, key, text)
        try:
            await self._write_server_result(record.get("request_id"), result)
        except Exception:
            self._unclaim_pending(record)
            raise
        # H2:isSecret 的答案只准出現在上面那個 frame,之後一律走遮罩版。
        safe_result = _codex_redact_secret_answers(record, result)
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.last_event_at[thread_id] = time.time()
        status = "answered"
        try:
            self._approval_db_decide(approval_id, status, safe_result)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_decide_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record, resolved=status)
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        _log_event("codex_question_decision", approval_id=approval_id,
                   method=record.get("method"), key=key or "", auto=auto,
                   secret=_codex_record_has_secret(record),
                   thread_id_hash=_short_hash(thread_id))
        return {"id": approval_id, "status": status, "result": safe_result,
                "thread_id": thread_id, "method": record.get("method")}

    def _question_response_result(self, record: dict, key: str, text: str) -> dict:
        method = record.get("method")
        if method == "item/tool/requestUserInput":
            return _codex_build_user_input_answers(record, key, text)
        if method == "mcpServer/elicitation/request":
            return _codex_build_elicitation_response(record, key)
        return {}

    async def _write_server_error(self, request_id, code: int, message: str):
        try:
            async with self._lock:
                if not self._transport_alive():
                    return
                await self._write_locked({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                })
        except Exception as e:  # noqa: BLE001
            _log_event("codex_app_server_error_response_failed",
                       error=type(e).__name__, error_message=str(e)[:160])

    async def _write_server_result(self, request_id, result: dict):
        async with self._lock:
            if not self._transport_alive():
                raise CodexAppServerError("codex app-server is not running")
            await self._write_locked({"jsonrpc": "2.0", "id": request_id,
                                      "result": result})

    async def _write_server_result_safe(self, request_id, result: dict) -> bool:
        """回覆 server request，寫不出去就吞掉（app-server 已掉線的路徑，
        呼叫端是 reader task，丟例外只會把整條 reader 打死）。"""
        try:
            await self._write_server_result(request_id, result)
            return True
        except Exception as e:  # noqa: BLE001
            _log_event("codex_app_server_result_response_failed",
                       error=type(e).__name__, error_message=str(e)[:160])
            return False

    def _approval_thread_id(self, method: str, params: dict) -> str:
        if method in ("execCommandApproval", "applyPatchApproval"):
            return str(params.get("conversationId") or "")
        return str(params.get("threadId") or "")

    def _approval_title(self, method: str, params: dict) -> str:
        if method in ("execCommandApproval", "item/commandExecution/requestApproval"):
            return "Codex command approval"
        return "Codex file-change approval"

    def _approval_command_text(self, params: dict) -> str:
        cmd = params.get("command")
        if isinstance(cmd, list):
            return shlex.join(str(x) for x in cmd)
        if isinstance(cmd, str):
            return cmd
        return ""

    def _approval_detail(self, method: str, params: dict) -> str:
        lines = [self._approval_title(method, params)]
        reason = params.get("reason")
        cwd = params.get("cwd")
        if cwd:
            lines.append(f"cwd: {cwd}")
        if reason:
            lines.append(f"reason: {reason}")
        command = self._approval_command_text(params)
        if command:
            lines.append("")
            lines.append("command:")
            lines.append(command)
        file_changes = params.get("fileChanges")
        if isinstance(file_changes, dict) and file_changes:
            lines.append("")
            lines.append("files:")
            for path, change in list(file_changes.items())[:30]:
                kind = ""
                if isinstance(change, dict):
                    kind_obj = change.get("kind")
                    kind = kind_obj.get("type") if isinstance(kind_obj, dict) else str(kind_obj or "")
                lines.append(f"- {path}" + (f" ({kind})" if kind else ""))
            if len(file_changes) > 30:
                lines.append(f"- ...and {len(file_changes) - 30} more")
        grant_root = params.get("grantRoot")
        if grant_root:
            lines.append(f"grant_root: {grant_root}")
        return "\n".join(lines).strip()

    def _approval_public(self, record: dict | None) -> dict | None:
        if not record:
            return None
        return {
            "id": record.get("id"),
            "method": record.get("method"),
            "kind": record.get("kind") or "permission",
            "title": record.get("title"),
            "detail": record.get("detail"),
            "risk": record.get("risk"),
            "created_at": record.get("created_at"),
            "thread_id": record.get("thread_id"),
            "options": record.get("options"),   # 發起方宣告的選項(去二元);None → app 用二元預設
        }

    def _approval_db_upsert(self, record: dict) -> None:
        import sqlite3
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            now = record.get("created_at") or time.time()
            # A1:統一欄位落庫。DB 的 options style 收斂為規範字彙(deny→danger);
            # 記憶體 record 保持原樣 — 現行 app 以 style=="deny" 判拒絕鍵,既有
            # 曝露面(v2 meta.approval、卡片流)相容期不動(A4 收斂)。
            src = str(record.get("source") or "")
            options = [({**o, "style": "danger"} if o.get("style") == "deny" else dict(o))
                       for o in (record.get("options") or [])]
            con.execute("INSERT OR REPLACE INTO approvals"
                        "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                        "session_id,provider,kind,options) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (record["id"], record["title"], record["source"], record["risk"],
                         record["detail"], now, now + 3600, "pending", None, None, None,
                         src if ":" in src else None, "codex",
                         str(record.get("kind") or "permission"),
                         json.dumps(options, ensure_ascii=False) if options else None))
            con.commit()
            con.close()
        finally:
            con.close()

    def _approval_db_decide(self, approval_id: str, status: str, result: dict | str) -> None:
        import sqlite3
        if not isinstance(result, str):
            result = json.dumps(result or {}, ensure_ascii=False)
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("UPDATE approvals SET status=?, decided_at=?, result=? WHERE id=?",
                        (status, time.time(), result, approval_id))
            con.commit()
            con.close()
        finally:
            con.close()

    def _handle_approval_request(self, msg: dict) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        request_id = msg.get("id")
        thread_id = self._approval_thread_id(method, params)
        created = time.time()
        stable = json.dumps([thread_id, method, request_id], sort_keys=True,
                            ensure_ascii=False, default=str)
        approval_id = "codex-" + hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()[:24]
        record = {
            "id": approval_id,
            "request_id": request_id,
            "method": method,
            "params": params,
            "thread_id": thread_id,
            "title": self._approval_title(method, params),
            "source": f"codex:{thread_id}" if thread_id else "codex",
            "risk": "high" if "command" in method.lower() or method == "execCommandApproval" else "medium",
            "detail": self._approval_detail(method, params),
            "created_at": created,
        }
        # 選項由發起方宣告(method-aware),不再由 carddigest 寫死二元。允許鈕
        # 依動作類型給語意標籤;style=="deny" 是唯一「拒絕」判準(app 依此送
        # approve=false)。三態第三顆 = for_session(一律允許此類、本 session 不再
        # 問)→ 對 command/fileChange 映射 Codex 原生 acceptForSession。
        _mlow = method.lower()
        _allow_label = ("允許執行" if "command" in _mlow
                        else "允許修改" if "filechange" in _mlow.replace("_", "")
                        else "允許")
        record["options"] = [
            {"key": "approve", "label": _allow_label, "style": "primary"},
        ]
        # 只有支援 acceptForSession 的 method 才給第三顆(見 _approval_response_result)。
        # key 用 approve_for_session:非 deny-ish → 舊 App(build ≤44,只認 approve/
        # deny)會安全退成「一般允許」,不會誤送拒絕;新 App 認得此 key → 送
        # for_session=true。style=secondary(較軟的允許)。
        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval"):
            record["options"].append(
                {"key": "approve_for_session", "label": "本次全允許", "style": "secondary"})
        record["options"].append(
            {"key": "deny", "label": "拒絕", "style": "deny"})
        self._register_pending_request(record)
        _log_event("codex_approval_request",
                   approval_id=approval_id,
                   method=method,
                   thread_id_hash=_short_hash(thread_id),
                   request_id_hash=_short_hash(str(request_id)))

    def _register_pending_request(self, record: dict) -> None:
        """pending server request（approval / question 共用）→ 記憶體、
        approvals DB、卡片流、推播。三條路徑各自 best-effort。"""
        approval_id = record["id"]
        thread_id = record.get("thread_id") or ""
        created = record.get("created_at") or time.time()
        self.pending_approvals[approval_id] = record
        if thread_id:
            self.pending_approvals_by_thread[thread_id][approval_id] = record
            self.last_event_at[thread_id] = created
        try:
            self._approval_db_upsert(record)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_upsert_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record)   # S2:approval 卡 + 等待核准 status
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        try:
            # 批次 3 斷點③:CX 審核進推播管線(記錄本來就進 approvals DB,
            # decide 走既有 codex 分支回流 app-server)。
            _approval_push(approval_id, record["title"],
                           record["detail"].splitlines()[0] if record["detail"] else "點開查看並決定",
                           f"codex:{thread_id}" if thread_id else "codex")
        except Exception as e:  # noqa: BLE001
            _log_event("approval_push_error", error=str(e)[:160])

    def pending_approval_for_thread(self, thread_id: str) -> dict | None:
        if not thread_id:
            return None
        pending = self.pending_approvals_by_thread.get(thread_id) or {}
        for aid, record in list(pending.items()):
            if aid in self.pending_approvals:
                return record
            pending.pop(aid, None)
        return None

    def pending_question_for_thread(self, thread_id: str) -> dict | None:
        """該 thread 上還沒回答的 question 類 server request（給
        `POST /codexsessions/{thread_id}/answer` 用）。"""
        if not thread_id:
            return None
        pending = self.pending_approvals_by_thread.get(thread_id) or {}
        for aid, record in list(pending.items()):
            if aid not in self.pending_approvals:
                pending.pop(aid, None)
                continue
            if record.get("kind") == "question":
                return record
        return None

    def _drop_approval(self, approval_id: str, status: str = "expired") -> dict | None:
        record = self.pending_approvals.pop(approval_id, None)
        if not record:
            return None
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.pending_approvals_by_thread.get(thread_id, {}).pop(approval_id, None)
        try:
            self._approval_db_decide(approval_id, status, {"reason": "server request no longer live"})
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_decide_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record, resolved=status)   # S2:approval 卡收尾
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        return record

    def _drop_approval_by_request(self, request_id) -> None:
        for aid, record in list(self.pending_approvals.items()):
            if record.get("request_id") == request_id:
                self._drop_approval(aid, status="expired")
                return

    def _drop_thread_approvals(self, thread_id: str) -> None:
        for aid in list((self.pending_approvals_by_thread.get(thread_id) or {}).keys()):
            self._drop_approval(aid, status="expired")

    def _approval_response_result(self, record: dict, approved: bool,
                                  for_session: bool = False) -> dict:
        method = record.get("method")
        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval"):
            if approved:
                decision = "acceptForSession" if for_session else "accept"
            else:
                decision = "decline"
            return {"decision": decision}
        if approved:
            decision = "approved_for_session" if for_session else "approved"
        else:
            decision = "denied"
        return {"decision": decision}

    async def decide_approval(self, approval_id: str, approved: bool,
                              for_session: bool = False) -> dict:
        record = self._claim_pending(approval_id)
        if not record:
            raise CodexAppServerError("codex approval is no longer pending", code=404)
        # B1:kind guard。question 類（requestUserInput / MCP elicitation）的
        # response 形狀是 {answers} / {action,content,_meta}，不是 {decision}；
        # 用二元核准回它 → app-server deserialize 失敗 → 整個 turn 死掉，
        # 而 DB 那列還被標成 approved（使用者看到「已允許」、實際 turn 陣亡）。
        if (record.get("kind") or "") == "question":
            self._unclaim_pending(record)
            raise CodexAppServerError(
                "這是 Codex 的『問答』請求（{}），不能用二元審批回覆；"
                "請改用 POST /codexsessions/{{thread_id}}/answer 或帶 key 的 "
                "approvals decision 作答。".format(record.get("method") or "question"),
                code=409)
        result = self._approval_response_result(record, approved, for_session=for_session)
        try:
            await self._write_server_result(record.get("request_id"), result)
        except Exception:
            self._unclaim_pending(record)
            raise
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.last_event_at[thread_id] = time.time()
        status = "approved" if approved else "rejected"
        try:
            self._approval_db_decide(approval_id, status, result)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_decide_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record, resolved=status)   # S2:approval 卡收尾
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        _log_event("codex_approval_decision",
                   approval_id=approval_id,
                   status=status,
                   method=record.get("method"),
                   thread_id_hash=_short_hash(thread_id),
                   request_id_hash=_short_hash(str(record.get("request_id"))))
        return {"id": approval_id, "status": status, "result": result,
                "thread_id": thread_id, "method": record.get("method")}

    async def decide_thread_approval(self, thread_id: str, approved: bool,
                                     for_session: bool = False) -> dict:
        """thread 層的「按了允許/拒絕」。

        B1:這條路只拿得到 thread，拿不到 approval_id，所以以前它抓到什麼就
        用二元審批回什麼 —— 包含 question 類的 server request。那會送出
        `{"decision": ...}` 去回一個期待 `{"answers": …}` 的
        `ToolRequestUserInputResponse`，deserialize 直接失敗、turn 死掉。
        現在先挑 approval 類；只剩 question 時依 method 路由到正確形狀，
        對不上的（多選題的「允許」根本沒有對應答案）就回清楚的 zh-TW 409，
        寧可讓 app 顯示錯誤，也不要把 turn 弄壞、還在 DB 留一列假的 approved。
        """
        record = None
        pending = self.pending_approvals_by_thread.get(thread_id) or {}
        for aid in list(pending.keys()):
            cand = self.pending_approvals.get(aid)
            if cand is None:
                pending.pop(aid, None)
                continue
            if (cand.get("kind") or "") != "question":
                record = cand
                break
        if record:
            return await self.decide_approval(record["id"], approved,
                                              for_session=for_session)
        question = self.pending_question_for_thread(thread_id)
        if question:
            method = question.get("method") or ""
            if method == "mcpServer/elicitation/request":
                # elicitation 本來就是「允許/拒絕」二選一,語意可以無損對映。
                return await self.answer_question(
                    question["id"], key="approve" if approved else "deny")
            if not approved:
                # requestUserInput 的「拒絕」= 略過不答(deny),語意相符。
                return await self.answer_question(question["id"], key="deny")
            opts = ", ".join(str(o.get("key") or "") for o in (question.get("options") or []))
            raise CodexAppServerError(
                "這個 session 上等的是 Codex 的『問答』（{}），沒有「一律允許」"
                "這種答案；請改用 POST /codexsessions/{}/answer 指定 keys（{}）"
                "或 text 作答。".format(method or "question", thread_id, opts),
                code=409)
        raise CodexAppServerError("no pending Codex approval for thread", code=404)

    def _handle_notification(self, msg: dict):
        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            _cx_cards_feed(method, params)   # S2 卡片 digest(有訂閱的 thread 才有)
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        if method == "remoteControl/status/changed":
            self.remote_status = params
            return
        if method == "thread/started":
            thread = params.get("thread") or {}
            tid = thread.get("id")
            if tid:
                self.loaded_threads.add(tid)
            return
        tid = params.get("threadId")
        if method == "thread/tokenUsage/updated" and tid:
            self.token_usage[tid] = params.get("tokenUsage") or params
            return
        if method == "turn/started" and tid:
            turn = params.get("turn") or {}
            self.active_turns[tid] = turn.get("id") or True
            self.turn_started_at[tid] = time.time()
            self.turn_terminal_at.pop(tid, None)
            self.last_event_at[tid] = time.time()
            self.thread_errors.pop(tid, None)
            self._start_turn_watchdog(tid)
            _codex_history_invalidate(tid)   # cached /history page is now stale
            return
        if method == "turn/completed" and tid:
            self.active_turns.pop(tid, None)
            self.turn_terminal_at[tid] = time.time()
            if self.pending_inputs.get(tid):     # 這輪結束 → 送出排隊中的下一則
                try:
                    t = asyncio.create_task(self.drain_pending(tid))
                    _BG_TASKS.add(t)
                    t.add_done_callback(_BG_TASKS.discard)
                except RuntimeError:
                    pass
            watchdog = self.turn_watchdogs.pop(tid, None)
            if watchdog and watchdog is not asyncio.current_task():
                watchdog.cancel()
            self.last_event_at[tid] = time.time()
            _codex_history_invalidate(tid)   # cached /history page is now stale
            self._drop_thread_approvals(tid)
            turn = params.get("turn") or {}
            err = turn.get("error") if isinstance(turn, dict) else None
            if err:
                msg = err.get("message", err)
                self.thread_errors[tid] = str(msg)
                self._append(tid, ("text", f"\n⚠️ Codex turn failed: {msg}\n"))
            elif not self.thread_errors.get(tid, "").startswith("Codex turn stalled"):
                self.thread_errors.pop(tid, None)
            # M1:是委派 thread → 回流父對話(running→idle/failed 轉換內部去重)。
            try:
                t = asyncio.create_task(_delegation_codex_completed(
                    tid, bool(err),
                    str(err.get("message", err))[:300] if err else ""))
                _BG_TASKS.add(t)
                t.add_done_callback(_BG_TASKS.discard)
            except RuntimeError:
                pass
            return
        if method == "error":
            _log_event("codex_app_server_error",
                       message=str(params.get("message") or params)[:240])
            return
        if method == "serverRequest/resolved":
            self._drop_approval_by_request(params.get("requestId"))
            return
        if not tid:
            return
        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta") or ""
            if item_id:
                # First delta of a NEW agent message carries the **🤖 助手:**
                # marker (same as _codex_format_item's non-streamed path) — the
                # app splits turns on it; without it streamed replies fold into
                # the user's bubble (issue #16). Later deltas of the same item
                # append bare.
                if item_id not in self._streamed_item_ids and delta:
                    delta = f"\n\n**🤖 助手:** {delta}"
                self._streamed_item_ids.add(item_id)
            self._append(tid, ("text", delta))
            return
        if method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "userMessage":
                return
            c = _codex_format_item(item, phase="started",
                                   skip_agent_ids=self._streamed_item_ids)
            if c:
                self._append(tid, ("text", c))
            return
        if method == "item/completed":
            item = params.get("item") or {}
            c = _codex_format_item(item, phase="completed",
                                   skip_agent_ids=self._streamed_item_ids)
            if c:
                self._append(tid, ("text", c))
            return
        if method == "item/fileChange/patchUpdated":
            changes = params.get("changes") or []
            c = _codex_format_file_changes(changes, "inProgress")
            if c:
                self._append(tid, ("text", c))

    async def ensure_thread_loaded(self, thread_id: str, cwd: str | None = None):
        if thread_id in self.loaded_threads:
            return
        params = {"threadId": thread_id, "excludeTurns": True}
        if cwd:
            params["cwd"] = cwd
        try:
            await self.call("thread/resume", params, timeout=30.0)
        except BaseException as e:
            if not isinstance(e, CodexAppServerError) or \
                    _codex_thread_lock_conflict(e) is None:
                # 非鎖的失敗:若這條 thread 本來就登記著鎖,窗一樣要重新上膛,
                # 否則每次輪詢都會再放行一次重試(見 rearm_thread_lock_window)。
                self.rearm_thread_lock_window(thread_id)
                raise
            raise self._thread_lock_error(thread_id, e) from e
        self.loaded_threads.add(thread_id)
        # resume 成功 = 寫入權在我們手上 → 鎖一定已經放開,狀態翻回未鎖。
        if self.clear_thread_lock(thread_id):
            _log_event("codex_thread_unlocked", thread=thread_id[:16])
            _cx_feed_thread_unlocked(thread_id)

    async def start_turn(self, thread_id: str, input_items: list, client_id: str | None = None,
                         cwd: str | None = None):
        # 事件緩衝的清空與 generation 跳號**只能在 turn/start 真的成功之後**做。
        # 2026-08-11 事故:原本擺在最前面,於是「上一輪還在跑時再送一則」——
        # 即使 resume/turn start 隨後失敗(409/502),當前正在跑那輪的事件已經被
        # 清空、generation 已跳號 → 串流斷掉、畫面變空白。送不出去是一回事,
        # 把別人正在跑的那輪弄壞是另一回事,後者嚴重得多。
        await self.ensure_thread_loaded(thread_id, cwd=cwd)
        params = {"threadId": thread_id, "input": input_items}
        if client_id:
            params["clientUserMessageId"] = client_id
        if cwd:
            params["cwd"] = cwd
        try:
            res = await self.call("turn/start", params, timeout=30.0)
        except CodexAppServerError as e:
            # resume 過關之後才被搶走寫入權(桌面 app 中途打開同一條)也走同一條路。
            if _codex_thread_lock_conflict(e) is None:
                raise
            raise self._thread_lock_error(thread_id, e) from e
        self.thread_event_generations[thread_id] += 1
        self.thread_events[thread_id].clear()
        turn = (res or {}).get("turn") or {}
        self.active_turns[thread_id] = turn.get("id") or True
        self.turn_started_at[thread_id] = time.time()
        self.turn_terminal_at.pop(thread_id, None)
        self.last_event_at[thread_id] = time.time()
        self._start_turn_watchdog(thread_id)
        return res

    async def interrupt_turn(self, thread_id: str):
        turn_id = self.active_turns.get(thread_id)
        if not isinstance(turn_id, str) or not turn_id:
            # 用 bridge 自己的 sentinel,**不能**沿用 app-server 的 -32600:
            # 本 PR 把 -32600 一律翻成 CX_TURN_IN_FLIGHT(「上一輪正在跑」),
            # 而這裡的意思剛好相反(根本沒有回合可中斷)→ 中斷端點會回報
            # 與事實完全顛倒的訊息。
            raise CodexAppServerError("no active Codex turn to interrupt",
                                      code=_CX_NO_ACTIVE_TURN_CODE)
        return await self.call("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        }, timeout=15.0)

    def _start_turn_watchdog(self, thread_id: str) -> None:
        previous = self.turn_watchdogs.pop(thread_id, None)
        if previous:
            previous.cancel()
        self.turn_watchdogs[thread_id] = asyncio.create_task(
            self._watch_turn(thread_id))

    async def _watch_turn(self, thread_id: str) -> None:
        """Interrupt a live turn that stopped emitting events.

        A live process is not proof that a turn is healthy. Without this
        watchdog a detached app-server turn leaves the UI permanently busy and
        prevents the next input from being accepted.
        """
        try:
            while self.is_active(thread_id):
                await asyncio.sleep(min(5.0, max(1.0, CODEX_TURN_STALL_SECS / 10)))
                if not self.is_active(thread_id):
                    return
                last = self.last_event_at.get(
                    thread_id, self.turn_started_at.get(thread_id, time.time()))
                if time.time() - last < CODEX_TURN_STALL_SECS:
                    continue
                message = "Codex turn stalled (no provider event)"
                self.thread_errors[thread_id] = message
                self.last_event_at[thread_id] = time.time()
                self._append(thread_id, ("text", "\n⚠️ Codex 回合卡住，已逾時中止。\n"))
                try:
                    await asyncio.wait_for(self.interrupt_turn(thread_id), timeout=15.0)
                except Exception as exc:  # noqa: BLE001
                    _log_event("codex_turn_interrupt_after_stall_failed",
                               thread=thread_id[:16], error=type(exc).__name__)
                self.active_turns.pop(thread_id, None)
                self.turn_terminal_at[thread_id] = time.time()
                if self.pending_inputs.get(thread_id):   # 卡住中止也要放行佇列
                    try:
                        t = asyncio.create_task(self.drain_pending(thread_id))
                        _BG_TASKS.add(t)
                        t.add_done_callback(_BG_TASKS.discard)
                    except RuntimeError:
                        pass
                try:
                    t = asyncio.create_task(_delegation_codex_completed(
                        thread_id, True, message))
                    _BG_TASKS.add(t)
                    t.add_done_callback(_BG_TASKS.discard)
                except RuntimeError:
                    pass
                return
        except asyncio.CancelledError:
            return
        finally:
            if self.turn_watchdogs.get(thread_id) is asyncio.current_task():
                self.turn_watchdogs.pop(thread_id, None)

    def events_for(self, thread_id: str) -> list:
        return self.thread_events.get(thread_id, [])

    def is_active(self, thread_id: str) -> bool:
        return thread_id in self.active_turns

    # ── CX 排隊層 ────────────────────────────────────────────────────────
    # 契約與 CC 對稱:忙碌時「一定收下」並回 delivery=queued,turn 結束自動送出。
    # 不回 4xx —— app 端沒有辦法分辨「真的失敗」與「只是還在忙」,一律紅字。
    def enqueue_input(self, thread_id: str, input_items: list,
                      client_id: str | None = None, cwd: str | None = None,
                      text: str = "") -> int:
        self.pending_inputs[thread_id].append(
            {"input": input_items, "client_id": client_id, "cwd": cwd,
             "text": text})
        return len(self.pending_inputs[thread_id])

    def pending_count(self, thread_id: str) -> int:
        return len(self.pending_inputs.get(thread_id) or [])

    async def drain_pending(self, thread_id: str) -> None:
        """turn 結束後送出佇列裡的下一則(一次一則:codex 是單 writer)。
        失敗不吞:把該則丟掉並記錄,否則會永遠卡在隊首反覆撞同一面牆。"""
        try:
            while self.pending_inputs.get(thread_id):
                if self.is_active(thread_id):
                    return                  # 新的一輪已經開跑,交給它結束時再 drain
                item = self.pending_inputs[thread_id].pop(0)
                try:
                    await self.start_turn(thread_id, item["input"],
                                          client_id=item.get("client_id"),
                                          cwd=item.get("cwd"))
                    _log_event("codex_pending_input_sent", thread=thread_id[:16],
                               remaining=len(self.pending_inputs.get(thread_id) or []))
                    return                  # 開跑了 → 下一則等這輪結束
                except Exception as exc:    # noqa: BLE001
                    _log_event("codex_pending_input_failed", thread=thread_id[:16],
                               error=type(exc).__name__,
                               error_message=str(exc)[:200])
                    # 排隊中的訊息被丟掉,使用者那顆泡泡卻停在「已排入下一輪」——
                    # 不推張錯誤卡的話,他到死都不知道這則根本沒送出去。
                    _cx_feed_queue_drop(thread_id, item, exc)
        finally:
            # 只在成功路徑同步 → 整個佇列全部失敗清空時 depth 永遠卡著不歸零,
            # 狀態列就一直掛著「另有 N 則排隊」。無論怎麼離開都要同步一次。
            _cx_sync_queue_depth(thread_id)

    def is_server_alive(self) -> bool:
        return self._transport_alive()

    def _transport_alive(self) -> bool:
        if self.ws is not None:
            return True
        return bool(self.proc and self.proc.returncode is None)

    # ── thread-store 寫入鎖狀態機 ─────────────────────────────────────────
    def note_thread_locked(self, thread_id: str, detail: str = "") -> bool:
        """記下「這條 thread 正被別的 codex app-server 鎖住」。

        回傳 True = 這是一個**新的抑制窗**(第一次偵測、或上一個窗已經到期),
        呼叫端該記一次 log、推一張卡;False = 還在窗內的重複偵測,安靜吞掉。
        這就是止血 retry storm 的地方:同一條 thread 每 N 分鐘最多吵一次。
        """
        if not thread_id:
            return False
        now = time.monotonic()
        rec = self.thread_locks.get(thread_id)
        fresh = rec is None or now >= float(rec.get("next_retry_at") or 0.0)
        if rec is None:
            rec = self.thread_locks[thread_id] = {"since": time.time(),
                                                  "attempts": 0}
        rec["detail"] = str(detail or "")[:300]
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_at"] = time.time()
        rec["next_retry_at"] = now + CODEX_THREAD_LOCK_RETRY_SECS
        # resume 失敗 = 這顆 app-server 並沒有接管這條 thread。留著 loaded 標記
        # 會讓後續 start_turn 直接跳過 resume,以為自己拿得到寫入權。
        self.loaded_threads.discard(thread_id)
        return fresh

    def rearm_thread_lock_window(self, thread_id: str) -> None:
        """已知鎖住的 thread 重試又失敗(但**不是**鎖的錯:逾時、app-server 掉線
        …)→ 照樣把抑制窗往後推。

        少了這一步,抑制窗就只有「鎖類失敗」能重新上膛:窗一到期、重試撞上一個
        非鎖錯誤,`next_retry_at` 永遠停在過去 → 之後每一次清單刷新/狀態輪詢都
        再打一次 resume。那就是把原本的 retry storm 換一個入口再跑一次。
        """
        rec = self.thread_locks.get(thread_id)
        if rec is not None:
            rec["next_retry_at"] = time.monotonic() + CODEX_THREAD_LOCK_RETRY_SECS

    def clear_thread_lock(self, thread_id: str) -> bool:
        """鎖放開了(resume 成功)。回傳 True = 狀態真的從 locked 翻回來。"""
        return self.thread_locks.pop(thread_id, None) is not None

    def is_thread_locked(self, thread_id: str) -> bool:
        return thread_id in self.thread_locks

    def thread_lock_retry_due(self, thread_id: str) -> bool:
        """鎖定中且抑制窗已過 → 允許再試一次(同時是「桌面端放開了沒」的探針)。"""
        rec = self.thread_locks.get(thread_id)
        return bool(rec) and time.monotonic() >= float(rec.get("next_retry_at") or 0.0)

    def thread_lock_info(self, thread_id: str) -> dict | None:
        """狀態端點/卡片流共用的 lock 描述;沒鎖 → None。"""
        rec = self.thread_locks.get(thread_id)
        if not rec:
            return None
        return {
            "locked": True,
            "reason": CX_THREAD_LOCKED_REASON,
            "message": CX_THREAD_LOCKED_MESSAGE,
            "since": rec.get("since"),
            "last_at": rec.get("last_at"),
            "attempts": int(rec.get("attempts") or 0),
            "detail": rec.get("detail") or "",
            "retry_after": max(0.0, round(
                float(rec.get("next_retry_at") or 0.0) - time.monotonic(), 1)),
        }

    def _thread_lock_error(self, thread_id: str, exc: BaseException):
        """把 thread-store 衝突翻成專屬 code,順手做「一個窗一次」的 log + 卡。"""
        if self.note_thread_locked(thread_id, str(exc)):
            _log_event("codex_thread_locked", thread=thread_id[:16],
                       code=getattr(exc, "code", None),
                       error_message=str(exc)[:200],
                       suppress_secs=CODEX_THREAD_LOCK_RETRY_SECS)
        # 卡片自己以 lock.since 去重,不必跟 log 的抑制窗綁在一起。
        _cx_feed_thread_locked(thread_id)
        return CodexAppServerError(str(exc), code=_CX_THREAD_LOCKED_CODE)

    def runtime_status(self, thread_id: str, raw_status: str = "") -> str:
        if self.pending_approval_for_thread(thread_id):
            return "waiting_approval"
        if self.is_active(thread_id):
            last = self.last_event_at.get(
                thread_id, self.turn_started_at.get(thread_id, time.time()))
            if time.time() - last >= CODEX_TURN_STALL_SECS:
                return "stalled"
            return "running"
        error = self.thread_errors.get(thread_id, "")
        if error:
            return "failed"
        if thread_id in self.turn_terminal_at:
            return "done"
        raw = (raw_status or "").lower()
        if raw in {"completed", "done", "success"}:
            return "done"
        return "idle"


CODEX_APP = CodexAppServerClient()


def _codex_usage_map(tu) -> dict | None:
    """app-server token usage (thread dict or tokenUsage/updated params) →
    the app's {used, size} meter shape. Defensive: field names probed on
    codex-cli 0.142.2 (totalTokens / inputTokens / cachedInputTokens /
    outputTokens, window in modelContextWindow); unknown shapes → None."""
    if not isinstance(tu, dict):
        return None
    inner = tu.get("tokenUsage") if isinstance(tu.get("tokenUsage"), dict) else tu
    total = inner.get("totalTokens")
    if total is None:
        total = sum(int(inner.get(k) or 0)
                    for k in ("inputTokens", "cachedInputTokens", "outputTokens"))
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    usage = {"used": total}
    window = inner.get("modelContextWindow") or tu.get("modelContextWindow")
    try:
        if window:
            usage["size"] = int(window)
    except (TypeError, ValueError):
        pass
    return usage


def _codex_enrich_summary(summary: dict) -> dict:
    tid = summary.get("thread_id") or summary.get("id") or ""
    summary["activeTurn"] = CODEX_APP.is_active(tid)
    summary["appServerAlive"] = CODEX_APP.is_server_alive()
    summary["runtimeStatus"] = CODEX_APP.runtime_status(
        tid, summary.get("status") or "")
    summary["status"] = summary["runtimeStatus"]
    usage = _codex_usage_map(CODEX_APP.token_usage.get(tid))
    if usage:
        summary["usage"] = usage
    approval = CODEX_APP.pending_approval_for_thread(tid)
    if approval:
        summary["awaitingApproval"] = True
        summary["status"] = "waiting_approval"
        summary["approval"] = CODEX_APP._approval_public(approval)
    if tid in CODEX_APP.last_event_at:
        summary["lastEventAt"] = CODEX_APP.last_event_at[tid]
    if tid in CODEX_APP.thread_errors:
        summary["error"] = CODEX_APP.thread_errors[tid]
    # thread-store 寫入鎖:`locked` **恆存在**(bool),app 才能無條件拿它決定
    # banner 與輸入框的啟用狀態,不必分辨「沒有這個欄位」與「沒被鎖」。
    # 刻意不動 `status` —— 那是既有 enum(idle/running/waiting_approval/…),
    # 塞新值會讓舊 app 顯示成未知狀態;鎖是正交的旗標。
    lock = CODEX_APP.thread_lock_info(tid)
    summary["locked"] = bool(lock)
    if lock:
        summary["lockReason"] = lock["reason"]
        summary["lockMessage"] = lock["message"]
        summary["lockedSince"] = lock["since"]
        summary["lock"] = lock
    return summary


def _codex_status_type(status) -> str:
    if isinstance(status, dict):
        return status.get("type") or "unknown"
    if isinstance(status, str):
        return status
    return "unknown"


def _codex_source_label(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        if "custom" in source:
            return str(source.get("custom") or "custom")
        if "subAgent" in source:
            return "subAgent"
    return "unknown"


def _codex_is_child_thread(thread: dict) -> bool:
    """Keep ephemeral/subagent threads out of the main operator list.

    `codex exec` creates a normal top-level state-db row, so the provider's
    source field alone cannot express parent/child ownership. These records
    remain available through `include_children=true` for debugging.
    """
    source = thread.get("source")
    source_text = str(source or "").lower()
    source_label = _codex_source_label(source).lower()
    thread_source = str(thread.get("threadSource") or
                        thread.get("thread_source") or "").lower()
    cwd = os.path.realpath(str(thread.get("cwd") or ""))
    if "guardian" in source_text or "subagent" in source_text:
        return True
    if source_label in {"subagent", "guardian"} or thread_source == "subagent":
        return True
    # The translation test burst was launched from /private/tmp. Keep this
    # rule narrow so normal exec-backed maintenance under /Users/xcash stays
    # visible in the main list.
    return cwd == "/private/tmp" or cwd.startswith("/private/tmp/")


async def _codex_v2_visible_threads(wanted: int = 20) -> list[dict]:
    """Return the main Codex threads for the v2 control-plane list.

    Codex can place a large burst of guardian/subagent threads at the head of
    ``thread/list``.  Fetching only the first ``wanted`` provider rows made
    older operator threads, including the XCash lane, disappear from Pocket.
    Keep the provider page large and paginate until the visible quota is full.
    """
    wanted = max(1, min(int(wanted or 20), 100))
    params = {
        "limit": min(100, max(wanted, 40)),
        "archived": False,
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "useStateDbOnly": False,
    }
    global _CODEX_V2_VISIBLE_CACHE
    visible: list[dict] = []
    cursor = None
    try:
        for _ in range(_CODEX_LIST_MAX_PAGES):
            if cursor:
                params["cursor"] = cursor
            else:
                params.pop("cursor", None)
            res = await CODEX_APP.call("thread/list", params, timeout=10.0)
            batch = list((res or {}).get("data", []))
            visible.extend(t for t in batch if not _codex_is_child_thread(t))
            if len(visible) >= wanted:
                break
            cursor = (res or {}).get("nextCursor")
            if not cursor or not batch:
                break
    except Exception as e:  # noqa: BLE001
        if _CODEX_V2_VISIBLE_CACHE:
            _log_event("codex_thread_list_stale", surface="v2",
                       count=len(_CODEX_V2_VISIBLE_CACHE),
                       error=type(e).__name__)
            return _CODEX_V2_VISIBLE_CACHE[:wanted]
        raise
    _CODEX_V2_VISIBLE_CACHE = [dict(t) for t in visible]
    return visible[:wanted]


def _codex_session_summary(thread: dict) -> dict:
    tid = thread.get("id") or ""
    name = (thread.get("name") or "").strip()
    preview = (thread.get("preview") or "").strip()
    provider_status = _codex_status_type(thread.get("status"))
    out = {
        "name": name or preview[:180] or (tid[:12] or "codex"),
        "thread_id": tid,
        "session_id": thread.get("sessionId") or "",
        "workdir": thread.get("cwd") or "",
        "preview": preview[:180],
        "status": provider_status,
        "providerStatus": provider_status,
        "source": _codex_source_label(thread.get("source")),
        "child": _codex_is_child_thread(thread),
        "updatedAt": thread.get("updatedAt"),
        "modelProvider": thread.get("modelProvider") or "",
    }
    # 設定面板讀回:當前 model / 審核策略 / effort / sandbox(thread/read 有才帶,
    # 缺 = 不帶欄,舊 app 容忍)。命名對齊 app-server 的 thread 物件。
    for src, dst in (("model", "model"), ("approvalPolicy", "approvalPolicy"),
                     ("reasoningEffort", "effort"), ("sandboxMode", "sandbox")):
        val = thread.get(src)
        if val:
            out[dst] = val
    # 0.142.2 returns tokenUsage: null from thread/list, but map it when a
    # future version populates it; the live overlay in _codex_enrich_summary
    # (thread/tokenUsage/updated) wins either way.
    usage = _codex_usage_map(thread.get("tokenUsage"))
    if usage:
        out["usage"] = usage
    return out


def _codex_user_input_text(content: list) -> str:
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text" and item.get("text"):
            parts.append(item["text"])
        elif t == "localImage" and item.get("path"):
            parts.append(f"[圖片: {item['path']}]")
        elif t == "image" and item.get("url"):
            parts.append(f"[圖片: {item['url']}]")
        elif item.get("path"):
            parts.append(f"[{t or 'file'}: {item['path']}]")
    return "\n".join(parts).strip()


def _codex_format_file_changes(changes: list, status: str = "") -> str:
    if not changes:
        return ""
    rows = []
    for c in changes[:8]:
        if not isinstance(c, dict):
            continue
        kind = c.get("kind") or {}
        k = kind.get("type") if isinstance(kind, dict) else str(kind)
        rows.append(f"- {k or 'change'} `{c.get('path', '')}`")
    more = f"\n- ...and {len(changes) - 8} more" if len(changes) > 8 else ""
    label = f"fileChange {status}".strip()
    return f"\n› 📝 **{label}**\n" + "\n".join(rows) + more + "\n"


def _codex_format_tool_result(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    short = text[:1200]
    more = "\n...(truncated)" if len(text) > 1200 else ""
    return f"<details><summary>↳ result</summary>\n\n```\n{short}{more}\n```\n\n</details>\n"


def _codex_format_item(item: dict, phase: str = "completed", skip_agent_ids=None) -> str:
    if not isinstance(item, dict):
        return ""
    skip_agent_ids = skip_agent_ids or set()
    t = item.get("type")
    if t == "userMessage":
        text = _codex_user_input_text(item.get("content") or [])
        return f"\n\n**🧑 你:** {text}\n\n" if text else ""
    if t == "agentMessage":
        if item.get("id") in skip_agent_ids:
            return ""
        text = item.get("text") or ""
        # Must carry the same **🤖 助手:** marker CC's _fmt_cc_event already emits.
        # Without it, conversationTurns() (app-side, splits on **🧑 你:**) can't
        # tell where the user's turn ends and the reply begins, so the whole
        # agent reply gets folded into the SAME turn as the preceding userMessage
        # and renders inside the user's (right-aligned, brand-coloured) bubble
        # instead of its own left-aligned assistant block.
        return f"\n\n**🤖 助手:** {text}\n\n" if text else ""
    if t == "plan":
        text = item.get("text") or ""
        return f"\n<details><summary>Plan</summary>\n\n{text}\n\n</details>\n" if text else ""
    if t == "reasoning":
        summary = "\n".join(item.get("summary") or []).strip()
        return f"\n<details><summary>Reasoning</summary>\n\n{summary}\n\n</details>\n" if summary else ""
    if t == "commandExecution":
        cmd = (item.get("command") or "").strip().splitlines()
        cmd1 = (cmd[0] if cmd else "")[:TOOL_CMD_MAX]
        status = item.get("status") or phase
        head = f"\n› 🔧 **command** `{cmd1}` [{status}]\n" if cmd1 else f"\n› 🔧 **command** [{status}]\n"
        return head + _codex_format_tool_result(item.get("aggregatedOutput") or "")
    if t == "fileChange":
        return _codex_format_file_changes(item.get("changes") or [], item.get("status") or phase)
    if t == "mcpToolCall":
        label = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
        status = item.get("status") or phase
        err = item.get("error") or {}
        out = f"\n› 🔧 **{label}** [{status}]\n"
        if err.get("message"):
            out += f"⚠️ {err['message']}\n"
        return out
    if t == "dynamicToolCall":
        label = item.get("tool") or "tool"
        ns = item.get("namespace")
        if ns:
            label = f"{ns}.{label}"
        return f"\n› 🔧 **{label}** [{item.get('status') or phase}]\n"
    if t == "webSearch":
        return f"\n› 🔎 **webSearch** `{str(item.get('query') or '')[:160]}`\n"
    if t == "imageGeneration":
        return f"\n› 🖼 **imageGeneration** [{item.get('status') or phase}]\n"
    return ""


def _codex_format_turns(turns: list) -> str:
    parts = []
    for turn in turns or []:
        for item in (turn.get("items") or []):
            c = _codex_format_item(item)
            if c:
                parts.append(c)
    return "".join(parts)


async def _codex_input_items(text: str, attachments: list) -> list:
    text = (text or "").strip()
    _att_guard(attachments)   # 修復單「附件限制」:直送口件數閥
    note_paths = []
    images = []
    voice_lines = []
    for a in (attachments or []):
        path = _save_attachment(a, a.get("filename") or "file")
        if not path:
            continue
        if a.get("kind") == "audio":
            t = await asyncio.to_thread(_transcribe, path)
            if t:
                voice_lines.append(t)
        elif a.get("kind") == "image":
            images.append({"type": "localImage", "path": path})
        else:
            note_paths.append(path)
    if voice_lines:
        text = (text + " " + " ".join(voice_lines)).strip()
    if note_paths:
        text = (text + "\n\n[附件已存到本機,請讀取: "
                + " ".join(note_paths) + "]").strip()
    items = []
    if text:
        items.append({"type": "text", "text": text})
    items.extend(images)
    return items


def _codex_http_error(e: Exception):
    _log_event("codex_provider_error", error=type(e).__name__,
               error_message=str(e)[:200],
               code=getattr(e, "code", None))
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        raise http_err(504, "PROVIDER_TIMEOUT", "codex app-server timeout", str(e))
    if isinstance(e, CodexAppServerError):
        locked_tid = _codex_thread_lock_conflict(e)
        if locked_tid is not None:
            # **必須排在 -32600 之前**:app-server 用同一個泛用碼回這個衝突,
            # 落到下面就會被翻成 CX_TURN_IN_FLIGHT(「上一輪正在跑」)——語意
            # 相反,使用者會去等一個不存在的回合(善彰就是這樣查了一整天)。
            #
            # 這裡是「使用者發起的請求」邊界,所以允許現場建 digest:他就算
            # 還沒開過這條 session,回頭進來也該看到原因,而不是只有一個
            # 已經被 app 吃掉的 409。
            if locked_tid:
                _cx_feed_thread_locked(locked_tid, create_if_missing=True)
            raise http_err(409, "CX_THREAD_LOCKED", CX_THREAD_LOCKED_MESSAGE, str(e))
        if e.code == _CX_NO_ACTIVE_TURN_CODE:
            # interrupt 專用:真的沒有回合可中斷。必須跟下面的 CX_TURN_IN_FLIGHT
            # 分開,否則中斷端點會回報「上一輪正在跑」——與事實完全相反。
            raise http_err(409, "CX_NO_ACTIVE_TURN",
                           "no active codex turn to interrupt", str(e))
        if e.code == 409:
            # B1 kind guard:拿二元審批去回 question 類 server request。
            # 這是呼叫端用錯介面,不是 provider 掛掉 —— 回 409 + 明確 code,
            # 別讓 app 顯示成「bridge 壞了」。
            raise http_err(409, "CX_WRONG_DECISION_SHAPE",
                           "codex server request 需要作答而不是核准", str(e))
        if e.code == -32600:
            # 舊版一律 409 + 裸訊息,app 端把它顯示成「會話目前沒有在執行」——
            # 真相通常相反(上一輪正在跑)。給結構化 code 讓 app 講對話。
            raise http_err(409, "CX_TURN_IN_FLIGHT",
                           "codex thread is busy with another turn", str(e))
        raise HTTPException(status_code=502, detail=str(e))
    raise HTTPException(status_code=502, detail=str(e))


@app.get("/codex/status")
async def codex_status(request: Request):
    _check_auth(request)
    try:
        status = await CODEX_APP.call("remoteControl/status/read", {}, timeout=15.0)
        return {"ok": True, "remoteControl": status}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


# Codex 進場延遲止血 (B1): /codexsessions/{id}/history is zero-cache and its
# thread/turns/list itemsView:"full" call is expensive; the app fires it
# several times while entering a session. A short TTL cache absorbs those
# duplicates — same pattern as _PANE_CACHE for tmux capture-pane.
_CODEX_HISTORY_TTL = 4.0
_CODEX_HISTORY_CACHE: dict = {}   # (thread_id, limit, cursor) -> (cached_at_monotonic, payload)
# A provider can keep its process alive while a turn has stopped emitting
# events. Treat that as a stalled turn, interrupt it, and expose the state
# instead of leaving Pocket's spinner up forever.
CODEX_TURN_STALL_SECS = float(os.environ.get("CODEX_TURN_STALL_SECS", "300"))
_CODEX_LIST_MAX_PAGES = 8
_CODEX_SESSION_LIST_CACHE: dict = {}
_CODEX_V2_VISIBLE_CACHE: list[dict] = []


def _codex_history_invalidate(thread_id: str) -> None:
    """Drop cached history pages for one thread (new input / turn activity)."""
    for k in [k for k in _CODEX_HISTORY_CACHE if k[0] == thread_id]:
        _CODEX_HISTORY_CACHE.pop(k, None)


def _codex_stream_turn_finished(seen_turn_activity: bool, active: bool,
                                event_index: int, event_count: int) -> bool:
    """A follow stream may stay open while idle, but once it has observed a
    turn it must finish as soon as that turn reaches a terminal state and all
    buffered events have been emitted."""
    return seen_turn_activity and not active and event_index >= event_count


async def _codex_warm_threads(thread_ids: list) -> None:
    """B3 light warmup: pre-run thread/resume for the sessions the user is most
    likely to tap next, so entering one skips the cold load. Strictly
    sequential and skip-if-loaded, so it never amplifies app-server queueing —
    at most one warm call is in the single _lock queue at a time."""
    # 風險控管:若 spawn 到的是 ~/.local/bin 的舊 standalone(0.137 地雷版),
    # 停用 warmup 的 thread/resume —— 0.137 resume/讀 0.142+ 建的 thread 會
    # 引爆 app-server crash 連鎖(下一次「CX 全空」最可能的引信)。
    if str(getattr(CODEX_APP, "spawned_bin", "")).endswith("/.local/bin/codex"):
        return
    for tid in thread_ids:
        if not tid or tid in CODEX_APP.loaded_threads:
            continue
        # thread-store 鎖住的 thread 原本每幾秒被重試一次(清單一刷就一輪),
        # log 每次都寫一筆 codex_thread_warm_failed → 實機一天 2 萬多行。
        # 抑制窗內直接跳過;窗過了才放行一次,那一次同時就是復原探針。
        if CODEX_APP.is_thread_locked(tid) and not CODEX_APP.thread_lock_retry_due(tid):
            continue
        try:
            await CODEX_APP.ensure_thread_loaded(tid)
            _log_event("codex_thread_warmed", thread=tid[:16])
        except CodexAppServerError as e:
            if getattr(e, "code", None) == _CX_THREAD_LOCKED_CODE:
                continue    # 已由 _thread_lock_error 記過一次 log + 推過卡
            _log_event("codex_thread_warm_failed", thread=tid[:16],
                       error=type(e).__name__, error_message=str(e)[:200])
        except Exception as e:  # noqa: BLE001
            _log_event("codex_thread_warm_failed", thread=tid[:16],
                       error=type(e).__name__)


@app.get("/codexsessions")
async def codex_sessions(request: Request, limit: int = 40, cwd: str | None = None,
                         archived: bool = False, cursor: str | None = None,
                         include_children: bool = False):
    _check_auth(request)
    wanted = max(1, min(limit, 100))
    params = {
        # The provider may return a burst of hidden /private/tmp exec threads
        # first, so fetch enough rows to fill the visible page after filtering.
        "limit": min(100, max(wanted, 40)),
        "archived": archived,
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "useStateDbOnly": False,
    }
    if cwd:
        params["cwd"] = cwd
    cache_key = (bool(archived), cwd or "", bool(include_children))
    try:
        data = []
        hidden_children = 0
        next_cursor = cursor
        pages = 0
        while len(data) < wanted and pages < _CODEX_LIST_MAX_PAGES:
            if next_cursor:
                params["cursor"] = next_cursor
            else:
                params.pop("cursor", None)
            res = await CODEX_APP.call("thread/list", params, timeout=45.0)
            batch = list((res or {}).get("data", []))
            if include_children:
                data.extend(batch)
            else:
                visible = [t for t in batch if not _codex_is_child_thread(t)]
                hidden_children += len(batch) - len(visible)
                data.extend(visible)
            next_cursor = (res or {}).get("nextCursor")
            pages += 1
            if not next_cursor or not batch:
                break
        data = data[:wanted]
        # Keep only the first page as a read-only fallback. A later cursor page
        # must never overwrite the main-list cache with a partial history page.
        if cursor is None:
            _CODEX_SESSION_LIST_CACHE[cache_key] = {
                "data": [dict(t) for t in data],
                "hidden_children": hidden_children,
            }
        # Do not resume threads as a side effect of listing them. In the
        # shared managed daemon, background thread/resume can contend with the
        # desktop writer lock and make an otherwise read-only list request
        # destabilize CX. Keep the old warmup as an explicit opt-in only.
        if os.environ.get("CODEX_LIST_WARMUP", "").strip() == "1":
            warm_ids = [t.get("id") for t in data[:4] if t.get("id")]
            if warm_ids:
                task = asyncio.create_task(_codex_warm_threads(warm_ids))
                _BG_TASKS.add(task)
                task.add_done_callback(_BG_TASKS.discard)
        return {
            "sessions": [_codex_enrich_summary(_codex_session_summary(t))
                         for t in data],
            "nextCursor": next_cursor,
            "includeChildren": include_children,
            "hiddenChildren": hidden_children,
        }
    except Exception as e:  # noqa: BLE001
        cached = _CODEX_SESSION_LIST_CACHE.get(cache_key) if cursor is None else None
        if cached and cached.get("data"):
            _log_event("codex_thread_list_stale", surface="codexsessions",
                       count=len(cached["data"]), error=type(e).__name__)
            return {
                "sessions": [_codex_enrich_summary(_codex_session_summary(t))
                             for t in cached["data"][:wanted]],
                "nextCursor": None,
                "includeChildren": include_children,
                "hiddenChildren": cached.get("hidden_children", 0),
                "stale": True,
            }
        _codex_http_error(e)


@app.get("/codexsessions/{thread_id}/status")
async def codex_session_status(thread_id: str, request: Request):
    _check_auth(request)
    try:
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
        thread = (res or {}).get("thread") or {}
        # provider 少回 id 的話,整個 runtime 疊加(activeTurn / locked / usage)
        # 會對到空字串 thread → 狀態欄永遠是「一切正常」。請求本身就知道 id。
        if not thread.get("id"):
            thread = {**thread, "id": thread_id}
        summary = _codex_enrich_summary(_codex_session_summary(thread))
        # 使用者停在被鎖的 session 上盯著 banner 時,這是唯一還會定期跑的請求
        # → 借它當復原探針(抑制窗保證不會變成 retry storm)。
        _codex_lock_recheck(thread_id)
        return {"session": summary}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.post("/codexsessions/{thread_id}/name")
async def codex_session_set_name(thread_id: str, request: Request):
    _check_auth(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    try:
        await CODEX_APP.call("thread/name/set", {
            "threadId": thread_id,
            "name": name,
        }, timeout=15.0)
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
        thread = (res or {}).get("thread") or {}
        summary = _codex_enrich_summary(_codex_session_summary(thread))
        return {"ok": True, "session": summary}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.post("/codexsessions/{thread_id}/archive")
async def codex_session_archive(thread_id: str, request: Request):
    """Archive (or unarchive) a Codex thread."""
    body = {}
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("codex_session_archive", _exc, expected=True)
        pass
    _check_auth(request)
    archived = body.get("archived", True)
    try:
        method = await _codex_thread_set_archived(thread_id, bool(archived))
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)
    return {"ok": True, "method": method}


async def _codex_thread_set_archived(thread_id: str, archived: bool = True) -> str:
    """Archive/unarchive a Codex thread via the app server, trying the known
    method names in order (the app server build varies). Shared by the archive
    endpoint and the registry reaper. Returns the method that worked; raises
    the last error when every variant fails."""
    last = None
    methods = (
        (
            ("thread/archive/set", {"threadId": thread_id, "archived": True}),
            ("thread/setArchived", {"threadId": thread_id, "archived": True}),
            ("thread/archive", {"threadId": thread_id}),
        )
        if archived
        else (
            ("thread/archive/set", {"threadId": thread_id, "archived": False}),
            ("thread/setArchived", {"threadId": thread_id, "archived": False}),
            ("thread/unarchive", {"threadId": thread_id}),
        )
    )
    for method, params in methods:
        try:
            await CODEX_APP.call(method, params, timeout=15.0)
            return method
        except Exception as e:  # noqa: BLE001
            _log_exc("codex_session_archive#2", e, expected=True)
            last = e
    raise last or Exception("archive failed")


@app.post("/codexsessions")
async def codex_session_create(request: Request):
    _check_auth(request)
    body = await request.json()
    text = (body.get("text") or body.get("task") or "").strip()
    attachments = body.get("attachments") or []
    input_items = await _codex_input_items(text, attachments)
    if not input_items:
        raise HTTPException(status_code=400, detail="text or attachment required")
    cwd = body.get("cwd") or HOME_ROOT
    params = {
        "cwd": cwd,
        "ephemeral": False,
        "threadSource": "user",
    }
    # spawn config(設計 §2.1):cx thread 走共用 app-server,能逐 thread 設的是
    # model/approvalPolicy(runtime 亦可,見 /settings);effort/sandbox 以
    # camelCase 透傳(app-server 不認會忽略)。api_key/profile 無法逐 thread 注入
    # (共用 app-server 一份 auth)→ 要 BYO key 請走 /dispatch 的 codex exec 子程序。
    try:
        spawn_cfg = _spawn_config_validate(body.get("config"), "cx")
    except SpawnConfigError as e:
        raise HTTPException(status_code=400, detail=e.detail)
    if body.get("model") and not spawn_cfg.get("model"):
        spawn_cfg = {**spawn_cfg, "model": body.get("model")}
    params.update(_spawn_cx_thread_params(spawn_cfg))
    cx_unsupported = [k for k in ("api_key", "profile") if spawn_cfg.get(k)]
    redacted = _spawn_config_redacted(spawn_cfg)
    if redacted:
        _log_event("codex_spawn_config", **redacted)
    # 戶政(藍圖 §3.1):thread/start 之前先過配額——超額 429,不生孤兒 thread。
    reg_parent, reg_cls, reg_purpose = _registry_spawn_fields(body, default_cls="task")
    _registry_precheck_or_429(reg_parent, reg_cls)
    try:
        res = await CODEX_APP.call("thread/start", params, timeout=30.0)
        thread = (res or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexAppServerError("thread/start returned no thread id")
        CODEX_APP.loaded_threads.add(thread_id)
        _registry_register(f"codex:{thread_id}", provider="codex",
                           name=text[:40] or thread_id, purpose=reg_purpose,
                           cls=reg_cls, parent=reg_parent)
        await CODEX_APP.start_turn(thread_id, input_items,
                                   client_id=body.get("client_id"), cwd=cwd)
        _cx_feed_input_accepted(
            thread_id, body.get("client_id"), _codex_user_input_text(input_items),
            attachments, typed_text=_codex_user_input_text(input_items),
            create_if_missing=True)
        resp = {"ok": True, "thread_id": thread_id,
                "session": _codex_enrich_summary(_codex_session_summary(thread)),
                "spawn_config": _spawn_config_public(spawn_cfg)}
        if cx_unsupported:
            # 明說哪些欄位共用 app-server 吃不下,app 可提示改走 dispatch。
            resp["spawn_config_unsupported"] = cx_unsupported
        return resp
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.get("/file")
async def serve_file(request: Request, path: str):
    """Serve a local file (image/pdf) by path so the app can render image paths
    that appear in transcripts (your attachments + files the agent references).
    Restricted to a small set of safe roots (home + the temp dirs agents write
    scratch files to), must be a regular file."""
    _check_auth(request)
    p = os.path.realpath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser("~"))]
    # Agents (incl. Claude Code's scratchpad) often emit files under the system
    # temp dirs; allow those too so generated artifacts render instead of 404ing.
    for t in ("/tmp", "/private/tmp", "/var/folders"):
        rt = os.path.realpath(t)
        if rt not in roots:
            roots.append(rt)
    if not any(p == r or p.startswith(r + os.sep) for r in roots):
        raise HTTPException(status_code=404, detail="not found")
    if os.path.isfile(p):
        # Compatibility traffic also protects the file from disappearing after
        # this response. Session-aware callers use the v2 media index instead.
        try:
            await asyncio.to_thread(
                _media_store().capture_path, "legacy:file", path
            )
        except Exception as exc:  # noqa: BLE001
            _log_event("media_legacy_capture_error", error=type(exc).__name__)
        return FileResponse(p)

    archived = await asyncio.to_thread(_media_store().resolve_original, path)
    if archived is None and p != path:
        archived = await asyncio.to_thread(_media_store().resolve_original, p)
    if archived is None:
        raise HTTPException(status_code=404, detail="not found")
    archived_path, archived_mime, _ = archived
    return FileResponse(archived_path, media_type=archived_mime or None)


async def _git_capture(*args, cwd=None, timeout: float = 20.0):
    """git 子行程一次呼叫 → (returncode, stdout 文字)。逾時殺行程回 (124, "")
    — git on a wedged repo/mount must not hang the handler (issue #7)。
    /filediff 與 session diff 端點(S2 / #38)共用。"""
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _log_event("filediff_git_timeout", args=" ".join(args[:4]),
                   timeout_s=timeout)
        return 124, ""
    return proc.returncode, (out or b"").decode("utf-8", "replace")


@app.get("/filediff")
async def serve_filediff(request: Request, path: str):
    """Diff/content for a file an agent touched (S2, pocketagent#38). Finds the
    enclosing git repo from the file's own location, returns `git diff HEAD`
    for it; a file with no pending diff (or outside any repo) falls back to its
    current content, so the app always has something to show. Same safe-root
    policy as /file.

    目錄模式（#38 缺口）：path 是目錄 → 整個目錄的 pending diff（合併
    unified）＋ `files[]` 變更檔清單，app 的多檔選單直接吃；目錄乾淨或
    不在 repo 裡 → 404 人話（目錄沒有「當前內容」可退）。"""
    _check_auth(request)
    p = os.path.realpath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser("~"))]
    for t in ("/tmp", "/private/tmp", "/var/folders"):
        rt = os.path.realpath(t)
        if rt not in roots:
            roots.append(rt)
    if (not any(p == r or p.startswith(r + os.sep) for r in roots)
            or not (os.path.isfile(p) or os.path.isdir(p))):
        raise HTTPException(status_code=404, detail="not found")

    if os.path.isdir(p):
        rc, top = await _git_capture("git", "-C", p, "rev-parse", "--show-toplevel")
        if rc != 0 or not top.strip():
            raise HTTPException(status_code=404, detail="目錄不在 git repo 裡，沒有 diff 可看")
        top = top.strip()
        rc2, out = await _git_capture("git", "-C", top, "diff", "HEAD", "--", p)
        diff = out if rc2 == 0 else ""
        if not diff:
            raise HTTPException(status_code=404, detail="目錄內沒有待提交的變更")
        files = []
        rc3, names = await _git_capture("git", "-C", top, "diff", "HEAD",
                                "--name-status", "--", p)
        if rc3 == 0:
            for line in names.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0]:
                    # rename 列是「R100\told\tnew」— 取最後一欄（現名）。
                    files.append({"status": parts[0][:1],
                                  "path": os.path.join(top, parts[-1])})
        if len(diff) > 200_000:
            diff = diff[:200_000] + "\n...(truncated)"
        return {"kind": "diff", "path": p, "text": diff, "files": files}

    d = os.path.dirname(p)
    rc, top = await _git_capture("git", "-C", d, "rev-parse", "--show-toplevel")
    diff = ""
    if rc == 0 and top.strip():
        # HEAD..worktree for this file — covers staged + unstaged edits.
        rc2, out = await _git_capture("git", "-C", top.strip(), "diff", "HEAD", "--", p)
        if rc2 == 0:
            diff = out
    if diff:
        if len(diff) > 200_000:
            diff = diff[:200_000] + "\n...(truncated)"
        return {"kind": "diff", "path": p, "text": diff}
    # No pending diff (already committed / untracked / not a repo) → current
    # content so "看檔案" still works. Reject binaries by a NUL sniff.
    try:
        with open(p, "rb") as f:
            head = f.read(200_000)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)[:200])
    if b"\x00" in head:
        raise HTTPException(status_code=415, detail="binary file")
    text = head.decode("utf-8", "replace")
    if len(head) == 200_000:
        text += "\n...(truncated)"
    return {"kind": "content", "path": p, "text": text}


# --- Session-scoped diff(S2 / pocketagent#38)------------------------------
# /filediff 吃「絕對路徑、從檔案自身找 repo」;這組端點吃「session + workdir
# 相對路徑」— transcript/卡片帶的常是相對路徑,由 bridge 用該 session 的
# workdir 解析,並把 realpath 圈死在 workdir 內(防 ../ 逃逸)。三個入口共用
# 一個核心:v1 /ccsessions|/codexsessions(issue 原文形)+ v2 統一路由。

_SESSION_DIFF_MAX = 200_000     # 截斷上限,與 /filediff 同一數字


async def _codex_thread_workdir(thread_id: str) -> str:
    try:
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)
    return ((res or {}).get("thread") or {}).get("cwd") or ""


async def _session_workdir_diff(workdir: str, path: str) -> dict:
    """Session workdir 內單檔的 pending diff 核心。

    tracked 檔走 `git diff HEAD -- <p>`(staged+unstaged 一次都在);乾淨但
    untracked 的新檔用 no-index 對 /dev/null 合成 new-file diff,app 端一樣
    有 +行綠可看(/filediff 這種情況只退回全文,是 #38 驗收的缺口)。
    回 {path, workdir, diff, truncated};diff 為空字串 = 該檔沒有待定變更。"""
    wd = os.path.realpath(os.path.expanduser(workdir or ""))
    if not workdir or not os.path.isdir(wd):
        raise HTTPException(status_code=404, detail="session 沒有可用的工作目錄")
    raw = os.path.expanduser((path or "").strip())
    if not raw:
        raise HTTPException(status_code=400, detail="path required")
    p = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(wd, raw))
    if p != wd and not p.startswith(wd + os.sep):
        raise HTTPException(status_code=400, detail="path 不在 session 工作目錄內")
    rc, top = await _git_capture("git", "-C", wd, "rev-parse", "--show-toplevel")
    if rc != 0 or not top.strip():
        raise HTTPException(status_code=404,
                            detail="工作目錄不在 git repo 裡,沒有 diff 可看")
    top = top.strip()
    rel = os.path.relpath(p, top)
    rc2, out = await _git_capture("git", "-C", top, "diff", "HEAD", "--", rel)
    diff = out if rc2 == 0 else ""
    if not diff and os.path.isfile(p):
        # tracked 且乾淨 vs untracked 新檔:porcelain 分辨;新檔合成 no-index
        # diff(它的 rc=1 是「有差異」,不是錯)。
        rcs, st = await _git_capture("git", "-C", top, "status", "--porcelain",
                                     "--untracked-files=all", "--", rel)
        if rcs == 0 and st.lstrip().startswith("??"):
            _rcn, out_n = await _git_capture("git", "-C", top, "diff",
                                             "--no-index", "--", os.devnull, rel)
            diff = out_n
    truncated = False
    if len(diff) > _SESSION_DIFF_MAX:
        diff = diff[:_SESSION_DIFF_MAX]
        truncated = True
    return {"path": p, "workdir": wd, "diff": diff, "truncated": truncated}


@app.get("/ccsessions/{name}/diff")
async def cc_session_diff(name: str, request: Request, path: str):
    _check_auth(request)
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    return await _session_workdir_diff(row[1], path)


@app.get("/codexsessions/{thread_id}/diff")
async def codex_session_diff(thread_id: str, request: Request, path: str):
    _check_auth(request)
    return await _session_workdir_diff(await _codex_thread_workdir(thread_id), path)


@app.get("/app/v2/sessions/{session_id}/diff")
async def v2_session_diff(session_id: str, request: Request, path: str):
    """統一路由 diff(卡片流表面直接用 store 的 v2 session id 打):cc=conf
    workdir、cx/delegation=thread cwd;hermes 沒有工作目錄 → 400。"""
    _check_auth(request)
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        return await _session_workdir_diff(src[2], path)
    if src[0] == "cx":
        return await _session_workdir_diff(await _codex_thread_workdir(src[1]), path)
    raise http_err(400, "UNSUPPORTED_PROVIDER", "persona session 沒有工作目錄")


# --- Client error log ------------------------------------------------------
# The app ships every error it hits (failed send, dropped stream, crash, …) here
# the moment it happens. We append to ONE file on the Mac so Claude can fetch +
# review client-side bugs each session and confirm whether they're resolved.
CLIENT_LOG = os.path.expanduser("~/.pocket/pocket-client.jsonl")


def _pair_code_meta(value):
    if isinstance(value, dict):
        return {
            "expiry": float(value.get("expiry") or 0),
            "apple_user_id": value.get("apple_user_id"),
        }
    try:
        return {"expiry": float(value), "apple_user_id": None}
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_pair_code_meta", _exc, expected=True)
        return {"expiry": 0.0, "apple_user_id": None}


def _pair_code_reject(request: Request):
    with _AUTH_LOCK:
        now = time.monotonic()
        over = _auth_fail_bump_locked(request, now)
        summary = _auth_fail_summary_locked(request, 429 if over else 400, now)
    if summary:
        _log_event("pair_claim_failure", **summary)
    raise HTTPException(status_code=429 if over else 400,
                        detail="invalid or expired pairing code")


@app.post("/app/v1/pair/new")
@app.post("/pair/new")
async def pair_new(request: Request):
    """Desktop-only (needs the master token): mint a one-time pairing code that
    the QR embeds. The phone exchanges it at /pair/claim. Never returns the token."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("pair_new", _exc, expected=True)
        body = {}
    required_account = request.url.path.startswith("/app/v1/")
    user = _account_user_from_request(request, body, required=required_account)
    ttl = _pair_clamp_ttl(body.get("ttl"))
    code = _pair_mint_code((user or {}).get("apple_user_id"), ttl=ttl)
    return {"code": code, "ttl": ttl,
            "account_bound": bool(user)}


def _pair_clamp_ttl(raw) -> int:
    """配對碼 TTL:預設 5 分鐘;免掃碼的「配對連結」場景(雲端主機把連結傳到
    手機再點)人工傳遞有延遲,允許放寬 —— 上限 30 分鐘,下限 1 分鐘。"""
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        return int(_PAIR_CODE_TTL)
    return max(60, min(1800, ttl))


def _pair_mint_code(apple_user_id=None, ttl=None) -> str:
    """鑄一枚一次性配對碼(/pair/new 與本機 /pair/qr 頁共用)。"""
    now = time.monotonic()
    code = secrets.token_urlsafe(9)
    with _PAIR_LOCK:
        for c in [c for c, v in _PAIR_CODES.items() if _pair_code_meta(v)["expiry"] < now]:
            _PAIR_CODES.pop(c, None)          # prune expired
        _PAIR_CODES[code] = {
            "expiry": now + (ttl or _PAIR_CODE_TTL),
            "apple_user_id": apple_user_id,
        }
    return code


def _pair_issue_device_token(name: str, platform: str, claim_user_id=None,
                             extra: dict | None = None):
    """發一枚 device token(/pair/claim 與 /pair/claim-voucher 共用內核)。
    呼叫端先完成各自的授權驗證(一次性碼 / voucher 簽章);這裡只負責鑄 token、
    落地 token store —— 兩條路發出的 token 形狀完全相同,app 端後續路徑零分岔。"""
    with _PAIR_LOCK:
        token = "pdev-" + secrets.token_urlsafe(32)
        device = None
        if claim_user_id:
            device = _account_device_put(claim_user_id, token, platform=platform, label=name)
        entry = {
            "name": name,
            "platform": platform,
            "created": time.time(),
            "last_seen": time.time(),
            "apple_user_id": claim_user_id,
            "device_id": (device or {}).get("device_id"),
        }
        if extra:
            entry.update(extra)
        _DEVICE_TOKENS[token] = entry
        _save_device_tokens(_DEVICE_TOKENS)
    return token, device


@app.post("/app/v1/pair/claim")
@app.post("/pair/claim")
async def pair_claim(request: Request):
    """Phone exchanges a one-time code for its OWN device token. The code IS the
    credential (no bearer needed); it's single-use and expires in 5 minutes."""
    try:
        body = await request.json()
    except Exception as _exc:
        _log_exc("pair_claim", _exc, expected=True)
        body = {}
    code = (body.get("code") or "").strip()
    name = (str(body.get("device_name") or "iPhone"))[:60]
    platform = (str(body.get("platform") or "ios"))[:32]
    now = time.monotonic()
    with _PAIR_LOCK:
        meta = _pair_code_meta(_PAIR_CODES.get(code))
        if not code or not meta["expiry"] or meta["expiry"] < now:
            _pair_code_reject(request)
    bound_user_id = meta.get("apple_user_id")
    is_app_pair_claim = request.url.path.startswith("/app/v1/")
    user = _account_user_from_request(request, body, required=False)
    if is_app_pair_claim and not bound_user_id:
        raise HTTPException(status_code=400, detail="pairing code is not account-bound")
    if bound_user_id and user and user.get("apple_user_id") != bound_user_id:
        _log_event("pair_claim_account_mismatch",
                   code_hash=_short_hash(code),
                   expected_user_hash=_short_hash(bound_user_id),
                   actual_user_hash=_short_hash(user.get("apple_user_id")))
        raise HTTPException(status_code=403, detail="pairing code belongs to another account")
    claim_user_id = bound_user_id or (user or {}).get("apple_user_id")

    with _PAIR_LOCK:
        meta = _pair_code_meta(_PAIR_CODES.get(code))
        if not code or not meta["expiry"] or meta["expiry"] < time.monotonic():
            _pair_code_reject(request)
        _PAIR_CODES.pop(code, None)           # one-time
    token, device = _pair_issue_device_token(name, platform, claim_user_id)
    _log_event("pair_claim",
               device=name,
               platform=platform,
               account_bound=bool(claim_user_id),
               apple_user_hash=_short_hash(claim_user_id),
               token_hash=_short_hash(token))
    return {"token": token,
            "device_id": (device or {}).get("device_id"),
            "account_bound": bool(claim_user_id)}


@app.get("/pair/devices")
async def pair_devices(request: Request):
    """List paired devices (desktop only). Tokens are returned hashed, not raw."""
    _check_auth(request)
    with _PAIR_LOCK:
        out = [
            {"id": _short_hash(t), "name": d.get("name", "device"),
             "platform": d.get("platform"), "device_id": d.get("device_id"),
             "account_bound": bool(d.get("apple_user_id")),
             "created": d.get("created"), "last_seen": d.get("last_seen")}
            for t, d in _DEVICE_TOKENS.items()
        ]
    return {"devices": out}


@app.post("/pair/revoke")
async def pair_revoke(request: Request):
    """Revoke a paired device by its short id (from /pair/devices). Desktop only."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:
        _log_exc("pair_revoke", _exc, expected=True)
        body = {}
    dev_id = (body.get("id") or "").strip()
    removed = 0
    with _PAIR_LOCK:
        for t in [t for t in _DEVICE_TOKENS if _short_hash(t) == dev_id]:
            dev = _DEVICE_TOKENS.pop(t, None)
            if dev and dev.get("apple_user_id") and dev.get("device_id"):
                _account_device_revoke(dev.get("apple_user_id"), dev.get("device_id"))
            removed += 1
        if removed:
            _save_device_tokens(_DEVICE_TOKENS)
    return {"revoked": removed}


# ───────────── 本機配對頁 /pair/qr(龍蝦主機三平台共用,取代 pocket-pair.py 桌面依賴)─────────────
# 安裝器最後一步開瀏覽器指到這裡;只服務 127.0.0.1(配對碼等同短效憑證,不上網段)。
# host 候選探測全走純 Python/跨平台指令,mac/ubuntu 同一份。

_PAIR_HOSTS_CACHE = {"ts": 0.0, "hosts": [], "tailscale": False}
_BRIDGE_PORT = int(os.environ.get("POCKET_BRIDGE_PORT", "8081"))
_POCKET_TUNNEL_URL_FILE = os.path.expanduser(
    os.environ.get("POCKET_TUNNEL_URL_FILE", "~/.pocket/tunnel-url"))


def _pair_local_only(request: Request) -> None:
    if _client_host(request) not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="local only")


# ── 一次性 boot code:/pair/qr 的真正閘門 ─────────────────────────────────
# _pair_local_only 在 cloudflared tunnel 部署下形同虛設:tunnel 把公網流量
# proxy 進 127.0.0.1,每個公網訪客看起來都像 loopback → 任何拿到 tunnel URL
# 的人都能開 /pair/qr 鑄配對碼 → /pair/claim 換 device token → 接管整台主機
# (含終端 PTY)。因此 /pair/qr 與 /pair/qr.json 必須帶 ?boot=<code>:
# 值放磁碟(chmod 600),只有摸得到這台機器的人(或安裝器印出的連結)才有。
# _pair_local_only 保留當 defense-in-depth 第二層,但 boot code 才是真閘。
_PAIR_BOOT_CODE_FILE = os.path.expanduser(
    os.environ.get("PAIR_BOOT_CODE_FILE", "~/.pocket/pair-boot-code"))
_PAIR_BOOT_CODE_LOCK = threading.Lock()
_PAIR_BOOT_CODE_CACHE = {"code": ""}


def _pair_boot_code() -> str:
    """讀取(或首次啟動時產生)boot code;檔案 0600、一機一碼、重啟不變。"""
    with _PAIR_BOOT_CODE_LOCK:
        if _PAIR_BOOT_CODE_CACHE["code"]:
            return _PAIR_BOOT_CODE_CACHE["code"]
        path = _PAIR_BOOT_CODE_FILE
        code = ""
        try:
            with open(path, encoding="utf-8") as f:
                code = f.read().strip()
        except FileNotFoundError:
            pass
        except Exception as _exc:  # noqa: BLE001
            _log_exc("pair_boot_code_read", _exc, expected=True)
        if not code:
            code = secrets.token_urlsafe(16)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code + "\n")
        _PAIR_BOOT_CODE_CACHE["code"] = code
        return code


def _pair_check_boot(request: Request) -> None:
    """boot code 閘:缺/錯一律 403(錯誤形狀同其他 pair 4xx)。常數時間比較。"""
    supplied = (request.query_params.get("boot") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, _pair_boot_code()):
        raise HTTPException(status_code=403, detail="invalid boot code")


try:  # 啟動即產生/載入,安裝器與人工查檔都能立刻拿到同一枚碼
    _pair_boot_code()
except Exception as _exc:  # noqa: BLE001
    _log_exc("pair_boot_code_init", _exc, expected=True)


# /pair/qr.json 鑄碼節流:配對是人手點一下的低頻動作,10/min 綽綽有餘,
# 卻能擋住拿到 boot code 後的自動化狂鑄(縮小暴力面)。
_PAIR_QR_MINT_LIMIT = 10
_PAIR_QR_MINT_WINDOW = 60.0
_PAIR_QR_MINTS = collections.deque()


def _pair_qr_check_mint_rate() -> None:
    now = time.monotonic()
    with _PAIR_LOCK:
        while _PAIR_QR_MINTS and now - _PAIR_QR_MINTS[0] > _PAIR_QR_MINT_WINDOW:
            _PAIR_QR_MINTS.popleft()
        if len(_PAIR_QR_MINTS) >= _PAIR_QR_MINT_LIMIT:
            raise HTTPException(status_code=429,
                                detail="too many pairing code requests; slow down")
        _PAIR_QR_MINTS.append(now)


def _pair_lan_ip():
    """UDP connect 戲法拿本機對外私網 IP(不真的發包),跨平台、免 ipconfig/ip。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip if ip and not ip.startswith("127.") else None
    except Exception:  # noqa: BLE001
        return None


def _pair_tailscale_host():
    """tailnet MagicDNS 名(兩端都在 tailnet 時最穩的私網路徑)。PATH 優先,
    mac App 內建路徑保底;沒裝/沒起 → None(頁面據此顯示安裝建議)。"""
    for ts in (shutil.which("tailscale"),
               "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if not ts or not os.path.exists(ts):
            continue
        try:
            out = subprocess.run([ts, "status", "--json"],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode != 0:
                continue
            self_ = json.loads(out.stdout).get("Self") or {}
            dns = (self_.get("DNSName") or "").rstrip(".")
            if dns:
                return dns
            ips = self_.get("TailscaleIPs") or []
            if ips:
                return str(ips[0])
        except Exception:  # noqa: BLE001
            continue
    return None


def _pair_tunnel_url():
    """公網保底 URL。優先 env POCKET_TUNNEL_URL(named tunnel/funnel);否則讀
    POCKET_TUNNEL_URL_FILE —— Ubuntu 安裝器的 cloudflared quick-tunnel unit 會把
    當前 trycloudflare URL 寫進去(URL 會漂,檔案由 unit 維護)。"""
    env = (os.environ.get("POCKET_TUNNEL_URL") or "").strip()
    if env:
        return env
    try:
        with open(_POCKET_TUNNEL_URL_FILE, encoding="utf-8") as f:
            url = f.read().strip()
        return url or None
    except OSError:
        return None


def _pair_host_candidates(force: bool = False):
    """依優先序的連線候選(同 pocket-pair.py 的 QR payload v2 語意):
    1) 私網直連 2) tailnet 3) 公網 tunnel。快取 30s,tailscale 探測不便宜。"""
    now = time.monotonic()
    if not force and _PAIR_HOSTS_CACHE["hosts"] and now - _PAIR_HOSTS_CACHE["ts"] < 30:
        return list(_PAIR_HOSTS_CACHE["hosts"]), _PAIR_HOSTS_CACHE["tailscale"]
    hosts = []
    lan = _pair_lan_ip()
    if lan:
        hosts.append("http://%s:%d" % (lan, _BRIDGE_PORT))
    ts = _pair_tailscale_host()
    if ts:
        hosts.append("https://%s" % ts)
    tunnel = _pair_tunnel_url()
    if tunnel:
        hosts.append(tunnel)
    _PAIR_HOSTS_CACHE.update({"ts": now, "hosts": list(hosts), "tailscale": bool(ts)})
    return hosts, bool(ts)


@app.get("/pair/qr.json")
async def pair_qr_json(request: Request):
    """鑄新碼 + 組 payload + 產 QR SVG,一次回齊(頁面 TTL 到期後再打一次換新碼)。
    ?ttl= 放寬碼效期(配對連結場景;夾 60..1800s)。"""
    _pair_check_boot(request)       # 真閘:一次性 boot code(tunnel 下 loopback 不可信)
    _pair_local_only(request)       # defense-in-depth 第二層
    _pair_qr_check_mint_rate()      # 鑄碼節流 10/min
    hosts, has_ts = _pair_host_candidates(force=True)
    if not hosts:
        return {"ok": False, "error": "no reachable host candidate (headless + no net?)"}
    ttl = _pair_clamp_ttl(request.query_params.get("ttl"))
    code = _pair_mint_code(None, ttl=ttl)
    # v1 相容鍵 scheme/host 取最後一個候選(= 公網保底,語意同 pocket-pair.py);
    # 新 app 走 hosts 依序自動選路。
    tail = urllib.parse.urlsplit(hosts[-1])
    payload = "pocket://pair?scheme=%s&host=%s&hosts=%s&code=%s" % (
        urllib.parse.quote(tail.scheme), urllib.parse.quote(tail.netloc),
        urllib.parse.quote(",".join(hosts)), urllib.parse.quote(code))
    svg = ""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(payload)
        img = qr.make_image(image_factory=SvgPathImage)
        svg = img.to_string().decode("utf-8")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("pair_qr_svg", _exc, expected=True)   # 沒 qrcode 套件時 payload 仍可手動輸入
    return {"ok": True, "payload": payload, "svg": svg, "hosts": hosts,
            "ttl": ttl, "tailscale": has_ts,
            "tunnel": _pair_tunnel_url()}


_PAIR_QR_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pocket 配對</title><style>
:root{color-scheme:light dark;font-family:-apple-system,system-ui,"Noto Sans TC",sans-serif}
body{margin:0;display:flex;justify-content:center;padding:32px 16px}
main{max-width:520px;width:100%}h1{font-size:1.35rem;margin:0 0 4px}
p.sub{margin:0 0 20px;opacity:.7}
#qr{width:280px;height:280px;margin:0 auto;display:block;border-radius:12px;background:#fff;padding:12px;box-sizing:content-box}
#qr svg{width:100%;height:100%}
.meta{text-align:center;margin:12px 0 20px;font-variant-numeric:tabular-nums}
.hosts{font-size:.85rem;opacity:.75;line-height:1.6;word-break:break-all}
.tsbox{border:1px solid color-mix(in srgb,currentColor 25%,transparent);border-radius:10px;padding:14px 16px;margin-top:20px;font-size:.9rem;line-height:1.55}
.tsbox code{user-select:all;background:color-mix(in srgb,currentColor 12%,transparent);padding:1px 6px;border-radius:5px}
.ok{opacity:.75}button{margin-left:8px}
</style></head><body><main>
<h1>用 Pocket 掃碼配對</h1>
<p class="sub">開 Pocket App → 新增主機 → 掃描下方 QR。配對碼一次性,逾時會自動換新。</p>
<div id="qr">載入中…</div>
<div class="meta"><span id="ttl"></span><button onclick="load()">重新產生</button></div>
<div class="hosts" id="hosts"></div>
<div class="tsbox" id="ts"></div>
<script>
let timer=null;
async function load(){
  // boot code 隨頁面網址帶入,轉發給 qr.json(缺/錯會 403)
  const boot=new URLSearchParams(location.search).get('boot')||'';
  const r=await fetch('/pair/qr.json?boot='+encodeURIComponent(boot));const d=await r.json();
  if(r.status===403){document.getElementById('qr').textContent='boot code 無效 — 請用安裝器印出的完整連結開啟本頁';return}
  if(r.status===429){document.getElementById('qr').textContent='產碼太頻繁,稍候再試';return}
  if(!d.ok){document.getElementById('qr').textContent=d.error||'失敗';return}
  document.getElementById('qr').innerHTML=d.svg||('<small style="word-break:break-all">'+d.payload+'</small>');
  document.getElementById('hosts').innerHTML='連線候選(依序自動選路):<br>'+d.hosts.map(h=>'· '+h).join('<br>');
  const ts=document.getElementById('ts');
  if(d.tailscale){ts.className='tsbox ok';ts.innerHTML='✓ 偵測到 Tailscale 私網 —— 出門在外走 tailnet,最穩最快。'}
  else{ts.innerHTML='建議安裝 <b>Tailscale</b>:除了公網通道外,自己建一條私網 —— 免設定、免固定 IP,'+
    '手機與這台主機同入 tailnet 後,外出連線走私網最穩。<br>Ubuntu 一行安裝:'+
    '<code>curl -fsSL https://tailscale.com/install.sh | sh</code> 之後執行 <code>sudo tailscale up</code>,'+
    '再回本頁「重新產生」。'}
  let left=d.ttl;clearInterval(timer);
  const t=document.getElementById('ttl');
  timer=setInterval(()=>{left--;t.textContent='配對碼有效 '+Math.floor(left/60)+':'+String(left%60).padStart(2,'0');
    if(left<=0){clearInterval(timer);load()}},1000);
}
load();
</script></main></body></html>"""


@app.get("/pair/qr")
async def pair_qr_page(request: Request):
    _pair_check_boot(request)       # 真閘:一次性 boot code(tunnel 下 loopback 不可信)
    _pair_local_only(request)       # defense-in-depth 第二層
    return HTMLResponse(_PAIR_QR_HTML)


# ═════════════ Pocket ID(Pairing V3):enroll + heartbeat + voucher claim ═════════════
# 藍圖:studio-os/docs/PAIRING_V3_POCKET_ID_20260811.md。
# 主機向 pocket-id 服務(id.pocket.shan.house)註冊+心跳;手機登入後「用選的」
# 配對:App 從 pocket-id 拿一張短效 Ed25519 voucher,直接對主機
# /pair/claim-voucher 換 device token。pocket-id 只當電話簿+公證人 ——
# 它從頭到尾拿不到 device token,被打穿最多洩「誰有哪些主機」。
# 全段 env 閘:POCKET_ID_URL 沒設 → 完全靜默(不註冊、不心跳、voucher 路由 404)。

POCKET_ID_URL = (os.environ.get("POCKET_ID_URL") or "").strip().rstrip("/")
POCKET_ID_ENROLL_TOKEN = (os.environ.get("POCKET_ID_ENROLL_TOKEN") or "").strip()
_POCKET_ID_STATE_PATH = os.path.expanduser(
    os.environ.get("POCKET_ID_STATE_FILE", "~/.pocket/pocket-id-enrollment.json"))
_POCKET_ID_HEARTBEAT_SECS = float(os.environ.get("POCKET_ID_HEARTBEAT_SECS", "60"))
_POCKET_ID_EXP_SKEW = 120.0     # voucher exp 容忍的時鐘偏移(秒)
_POCKET_ID_LOCK = threading.Lock()
_POCKET_ID_CACHE = {"loaded": False, "state": {}}   # 磁碟 enrollment 的行程內快取
_POCKET_ID_NONCES: dict = {}    # nonce -> 淘汰時刻(epoch);in-memory 防重放


def _pocket_id_state() -> dict:
    """讀取(lazy)已存的 enrollment:{host_id, host_secret, pocket_id_pubkey, url}。
    空 dict = 未註冊。"""
    with _POCKET_ID_LOCK:
        if not _POCKET_ID_CACHE["loaded"]:
            st: dict = {}
            try:
                with open(_POCKET_ID_STATE_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                st = loaded if isinstance(loaded, dict) else {}
            except FileNotFoundError:
                pass
            except Exception as _exc:  # noqa: BLE001
                _log_exc("pocket_id_state_load", _exc, expected=True)
            _POCKET_ID_CACHE["state"] = st
            _POCKET_ID_CACHE["loaded"] = True
        return dict(_POCKET_ID_CACHE["state"])


def _pocket_id_save_state(state: dict) -> None:
    """enrollment 落地(0600:host_secret 等同這台主機在 pocket-id 的身分)。"""
    os.makedirs(os.path.dirname(_POCKET_ID_STATE_PATH) or ".", exist_ok=True)
    tmp = _POCKET_ID_STATE_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _POCKET_ID_STATE_PATH)
    with _POCKET_ID_LOCK:
        _POCKET_ID_CACHE["state"] = dict(state)
        _POCKET_ID_CACHE["loaded"] = True


def _pocket_id_clear_state(reason: str) -> None:
    try:
        os.remove(_POCKET_ID_STATE_PATH)
        _log_event("pocket_id_unenrolled", reason=reason)
    except FileNotFoundError:
        pass
    except OSError as _exc:
        _log_exc("pocket_id_state_clear", _exc, expected=True)
    with _POCKET_ID_LOCK:
        _POCKET_ID_CACHE["state"] = {}
        _POCKET_ID_CACHE["loaded"] = True


def _pocket_id_boot() -> None:
    """開機期 enrollment 治理:POCKET_ID_RESET=1 或 env 撤掉 → 清除本機註冊;
    stored url 與現行 env 不符 → 也視同過期(那是別家 pocket-id 的身分)。"""
    reset = os.environ.get("POCKET_ID_RESET", "").strip().lower() in (
        "1", "true", "yes", "on")
    st = _pocket_id_state()
    if st and (reset or not POCKET_ID_URL):
        _pocket_id_clear_state("reset" if reset else "url_removed")
    elif st and st.get("url") and st.get("url") != POCKET_ID_URL:
        _pocket_id_clear_state("url_changed")


def _pocket_id_platform() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def _pocket_id_host_name() -> str:
    return (os.environ.get("POCKET_HOST_NAME") or "").strip() \
        or socket.gethostname() or "pocket-host"


def _pocket_id_candidates() -> list:
    """API contract 的 candidates:[{scheme,host}] —— 與 /pair/qr 廣告的同一份
    連線候選(私網 → tailnet → tunnel 優先序)。"""
    hosts, _ts = _pair_host_candidates(force=True)
    out = []
    for h in hosts:
        u = urllib.parse.urlsplit(h)
        if u.scheme and u.netloc:
            out.append({"scheme": u.scheme, "host": u.netloc})
    return out


async def _pocket_id_api(path: str, payload: dict) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(POCKET_ID_URL + path, json=payload)
        r.raise_for_status()
        return r.json()


async def _pocket_id_register() -> None:
    """一次性 enroll:帶 POCKET_ID_ENROLL_TOKEN 註冊,存回 host 身分 + 服務公鑰。"""
    data = await _pocket_id_api("/v1/hosts/register", {
        "enroll_token": POCKET_ID_ENROLL_TOKEN,
        "name": _pocket_id_host_name(),
        "platform": _pocket_id_platform(),
        "candidates": _pocket_id_candidates(),
        "capabilities": _host_capabilities(),
    })
    _pocket_id_save_state({
        "host_id": data["host_id"],
        "host_secret": data["host_secret"],
        "pocket_id_pubkey": data["pocket_id_pubkey"],
        "url": POCKET_ID_URL,
    })
    _log_event("pocket_id_enrolled", url=POCKET_ID_URL,
               host_id_hash=_short_hash(str(data["host_id"])))


async def _pocket_id_heartbeat() -> None:
    st = _pocket_id_state()
    await _pocket_id_api("/v1/hosts/heartbeat", {
        "host_id": st.get("host_id"),
        "host_secret": st.get("host_secret"),
        "candidates": _pocket_id_candidates(),
        "capabilities": _host_capabilities(),
    })


async def _pocket_id_loop() -> None:
    """常駐:未註冊 → 先 enroll;之後每 60s 心跳(candidates/capabilities 都會
    重新探測)。任何失敗只記一次 log、指數退避重試 —— 絕不弄死 bridge。"""
    backoff = 60.0
    fail_logged = False
    while True:
        try:
            if not _pocket_id_state():
                if not POCKET_ID_ENROLL_TOKEN:
                    _log_event("pocket_id_idle",
                               reason="no enrollment and no POCKET_ID_ENROLL_TOKEN")
                    return
                await _pocket_id_register()
            await _pocket_id_heartbeat()
            if fail_logged:
                _log_event("pocket_id_recovered", url=POCKET_ID_URL)
                fail_logged = False
            backoff = 60.0
            await asyncio.sleep(_POCKET_ID_HEARTBEAT_SECS)
        except asyncio.CancelledError:
            raise
        except Exception as _exc:  # noqa: BLE001
            if not fail_logged:
                _log_exc("pocket_id_loop", _exc, expected=True)
                fail_logged = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 3600.0)


@app.on_event("startup")
async def _start_pocket_id():
    _pocket_id_boot()
    if not POCKET_ID_URL:
        return          # 預設關:env 沒設 → 這段功能完全不存在,合併零風險
    task = asyncio.create_task(_pocket_id_loop())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    _log_event("pocket_id_started", url=POCKET_ID_URL,
               enrolled=bool(_pocket_id_state()))


def _pocket_id_verify_voucher(voucher: str) -> dict:
    """驗一張 pocket-id voucher:base64url(payload).base64url(sig),Ed25519 簽章
    + host_id 綁定 + exp(120s 偏移容忍)+ nonce 防重放。任何一關失敗 → 403;
    未註冊 → 404(graceful absence,app 據此整面隱藏)。"""
    st = _pocket_id_state()
    if not st.get("pocket_id_pubkey") or not st.get("host_id"):
        _log_event("pair_claim_voucher_rejected", reason="not_enrolled")
        raise http_err(404, "POCKET_ID_NOT_ENROLLED",
                       "此主機未啟用 Pocket ID 配對")

    def _fail(reason: str, message: str) -> HTTPException:
        _log_event("pair_claim_voucher_rejected", reason=reason)
        return http_err(403, "VOUCHER_INVALID", message)

    parts = voucher.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise _fail("malformed", "voucher 格式錯誤")
    try:
        payload_bytes = _b64u_decode(parts[0])
        sig = _b64u_decode(parts[1])
    except Exception:  # noqa: BLE001
        raise _fail("bad_base64", "voucher 編碼無法解析")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(st["pocket_id_pubkey"]))
        pub.verify(sig, payload_bytes)
    except Exception:  # noqa: BLE001  (InvalidSignature / 壞公鑰,一律同形 403)
        raise _fail("bad_signature", "voucher 簽章驗證失敗")
    try:
        payload = json.loads(payload_bytes)
    except Exception:  # noqa: BLE001
        raise _fail("bad_payload", "voucher 內容無法解析")
    if not isinstance(payload, dict):
        raise _fail("bad_payload", "voucher 內容無法解析")
    if str(payload.get("host_id") or "") != str(st["host_id"]):
        raise _fail("host_mismatch", "voucher 不是簽發給這台主機")
    try:
        exp = float(payload.get("exp"))
    except (TypeError, ValueError):
        exp = 0.0
    now = time.time()
    if now > exp + _POCKET_ID_EXP_SKEW:
        raise _fail("expired", "voucher 已過期,請回 App 重新選取主機")
    nonce = str(payload.get("nonce") or "")
    if not nonce:
        raise _fail("no_nonce", "voucher 缺少 nonce")
    with _POCKET_ID_LOCK:
        for n in [n for n, drop in _POCKET_ID_NONCES.items() if drop < now]:
            _POCKET_ID_NONCES.pop(n, None)      # prune 過期 nonce,dict 不外洩
        replayed = nonce in _POCKET_ID_NONCES
        if not replayed:
            _POCKET_ID_NONCES[nonce] = exp + _POCKET_ID_EXP_SKEW + 60.0
    if replayed:
        raise _fail("replay", "voucher 已被使用(重放防護)")
    return payload


@app.post("/pair/claim-voucher")
async def pair_claim_voucher(request: Request):
    """Pairing V3:手機出示 pocket-id 簽發的短效 voucher 直接配對 —— 免掃 QR、
    免 boot code(voucher 本身就是授權)。驗過即發 device token,token 形狀與
    /pair/claim 完全相同,app 既有的 post-claim 路徑零改動。"""
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("pair_claim_voucher", _exc, expected=True)
        body = {}
    voucher = str(body.get("voucher") or "").strip()
    name = (str(body.get("device_name") or "iPhone"))[:60]
    platform = (str(body.get("platform") or "ios"))[:32]
    if not voucher:
        _log_event("pair_claim_voucher_rejected", reason="missing")
        raise http_err(403, "VOUCHER_INVALID", "缺少 voucher")
    payload = _pocket_id_verify_voucher(voucher)
    account_id = str(payload.get("account_id") or "")
    token, device = _pair_issue_device_token(
        name, platform, None,
        extra={"pocket_account_id": account_id})
    _log_event("pair_claim_voucher",
               device=name, platform=platform,
               account_hash=_short_hash(account_id),
               token_hash=_short_hash(token))
    return {"token": token,
            "device_id": (device or {}).get("device_id"),
            "account_bound": False}


# --- In-app terminal (bridge PTY) --------------------------------------------
# Contract: studio-os/docs/TERMINAL_PTY_CONTRACT.md v0. One WebSocket = one
# local PTY shell on this Mac (the bridge already runs here — no SSH). Text
# JSON both directions, UTF-8, no base64. Kernel/OSS feature too (self-serve
# ops), so it is unconditionally present. Gated by POCKET_TERMINAL_ENABLED.

def _terminal_enabled() -> bool:
    """Default ON. POCKET_TERMINAL_ENABLED=0/false/no/off/'' → endpoint 403s."""
    return os.environ.get("POCKET_TERMINAL_ENABLED", "1").strip().lower() \
        not in ("0", "false", "no", "off", "")


def _terminal_tmux_bin() -> str | None:
    return shutil.which(TMUX_BIN) or shutil.which("tmux")


def _terminal_capabilities() -> dict:
    tmux_bin = _terminal_tmux_bin()
    return {
        "enabled": _terminal_enabled(),
        "backend": "tmux" if tmux_bin else "shell",
        "persistent": bool(tmux_bin),
        "reattach": bool(tmux_bin),
    }


def _ws_bearer_token(websocket: WebSocket) -> str:
    """Same token as every other /app/v1/* call: Authorization: Bearer <t>, or
    ?token=<t> query fallback (the contract lets the bridge accept either, since
    setting headers on a WS handshake isn't always convenient on the client)."""
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (websocket.query_params.get("token") or "").strip()


def _ws_token_authorized(token: str) -> bool:
    """Accept branches mirror _check_auth: master token, a paired device token,
    or an account-bound device token. No rate-limit bookkeeping here (the WS
    handshake is not a brute-force surface the way the JSON gate is)."""
    if not token:
        return False
    if hmac.compare_digest(token, BRIDGE_TOKEN):
        return True
    with _PAIR_LOCK:
        dev = _DEVICE_TOKENS.get(token)
        if dev is not None:
            if not dev.get("apple_user_id") or _account_device_for_token(token) is not None:
                dev["last_seen"] = time.time()
                return True
    if _account_device_for_token(token) is not None:
        return True
    return False


@app.websocket("/app/v1/terminal")
async def app_v1_terminal(websocket: WebSocket):
    # Reject BEFORE accept() so Starlette answers the handshake with HTTP 403 —
    # the iOS client keys "終端機已停用"/no-retry off that status code.
    if not _terminal_enabled():
        _log_event("terminal_rejected", reason="disabled")
        await websocket.close(code=1008)
        return
    token = _ws_bearer_token(websocket)
    if not _ws_token_authorized(token):
        _log_event("terminal_rejected", reason="auth")
        await websocket.close(code=1008)
        return

    await websocket.accept()

    shell = os.environ.get("SHELL") or "/bin/zsh"
    home = os.path.expanduser("~")
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env.pop("POCKET_TERMINAL_ENABLED", None)  # bridge-internal, don't leak into the shell

    # tmux-backed so the shell survives WS disconnects: reconnecting with the
    # same ?session=<name> re-attaches the SAME live tmux session (running agents,
    # state, scrollback all intact) instead of spawning a fresh shell. `-A` =
    # attach-or-create. Killing the client (killpg below) only detaches — the tmux
    # server keeps the session alive for the next attach. No ?session → a stable
    # default, so even the current single-terminal UX becomes persistent.
    raw_sess = (websocket.query_params.get("session") or "").strip()
    tmux_bin = _terminal_tmux_bin()
    if tmux_bin and raw_sess and await _tmux_alive(raw_sess):
        # 既有 tmux session(如 ccsess 的 "Ops"/"FLiPER")→ 直接 attach 進去,
        # 讓 app 的 SSH 連線能接到那個跑著 Claude Code/Codex 的 session。
        sess = raw_sess
    elif raw_sess:
        sess = "pocket-" + re.sub(r"[^A-Za-z0-9_-]", "_", raw_sess)[:60]
    else:
        sess = "pocket-term"

    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"openpty failed: {e}"}))
        await websocket.close()
        return

    try:
        argv = ([tmux_bin, "new-session", "-A", "-s", sess, "-c", home]
                if tmux_bin else [shell, "-l"])
        proc = subprocess.Popen(
            argv,
            preexec_fn=os.setsid,               # own session+pgroup → killpg reaps only the client
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=home, env=env, close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        _log_exc("app_v1_terminal", e, expected=True)
        os.close(master_fd)
        os.close(slave_fd)
        await websocket.send_text(json.dumps({"type": "error", "message": f"spawn failed: {e}"}))
        await websocket.close()
        return
    os.close(slave_fd)  # parent keeps only the master end
    _log_event("terminal_open", device=_short_hash(token), shell=shell,
               tmux=sess if tmux_bin else None,
               mode="tmux" if tmux_bin else "shell")  # no keystrokes/output

    loop = asyncio.get_running_loop()

    def _read_master() -> bytes:
        try:
            return os.read(master_fd, 65536)
        except OSError:
            return b""                          # EIO on macOS when the child's side closes

    async def pump_output():
        """PTY → client. Ends (returns) on EOF, i.e. the shell exited."""
        while True:
            data = await loop.run_in_executor(None, _read_master)
            if not data:
                return
            try:
                await websocket.send_text(json.dumps(
                    {"type": "output", "data": data.decode("utf-8", "replace")}))
            except Exception as _exc:  # noqa: BLE001 — socket went away mid-send
                _log_exc("app_v1_terminal.pump_output", _exc, expected=True)
                return

    async def pump_input():
        """client → PTY. Ends (returns) when the socket closes/errors."""
        while True:
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return
            try:
                msg = json.loads(raw)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("app_v1_terminal.pump_input", _exc, expected=True)
                continue
            mtype = msg.get("type")
            if mtype == "input":
                data = msg.get("data") or ""
                if data:
                    try:
                        os.write(master_fd, data.encode("utf-8"))
                    except OSError:
                        return
            elif mtype == "resize":
                try:
                    cols = max(1, min(int(msg.get("cols") or 80), 1000))
                    rows = max(1, min(int(msg.get("rows") or 25), 1000))
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
                except (OSError, ValueError, TypeError):
                    pass

    out_task = asyncio.create_task(pump_output())
    in_task = asyncio.create_task(pump_input())
    try:
        done, pending = await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        shell_exited = out_task in done
    finally:
        # Reap the shell + its process group; closing the master fd unblocks any
        # os.read still parked in the executor thread.
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=2)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("app_v1_terminal#2", _exc, expected=True)
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        _log_event("terminal_close", device=_short_hash(token))
    # Tell the client the shell died (only meaningful if it, not the socket, ended).
    if shell_exited and websocket.client_state == WebSocketState.CONNECTED:
        try:
            code = proc.returncode if proc.returncode is not None else 0
            await websocket.send_text(json.dumps({"type": "exit", "code": code}))
            await websocket.close()
        except Exception as _exc:  # noqa: BLE001
            _log_exc("app_v1_terminal#3", _exc, expected=True)
            pass


@app.post("/clientlog")
async def client_log_write(request: Request):
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    os.makedirs(os.path.dirname(CLIENT_LOG), exist_ok=True)
    entry = {
        "server_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ts": body.get("ts"),
        "level": str(body.get("level", "error"))[:16],
        "build": str(body.get("build", "?"))[:16],
        "context": str(body.get("context", ""))[:120],
        "msg": str(body.get("msg", ""))[:1000],
        "detail": str(body.get("detail", ""))[:4000],
    }
    with open(CLIENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Cap the file so it can't grow unbounded (keep newest ~3000 lines).
    try:
        lines = open(CLIENT_LOG, encoding="utf-8").read().splitlines()
        if len(lines) > 3000:
            with open(CLIENT_LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-3000:]) + "\n")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("client_log_write", _exc, expected=True)
        pass
    return {"ok": True}


@app.get("/clientlog")
async def client_log_read(request: Request, limit: int = 100, level: str = ""):
    _check_auth(request)
    if not os.path.exists(CLIENT_LOG):
        return {"entries": []}
    out = []
    for line in open(CLIENT_LOG, encoding="utf-8").read().splitlines()[-1000:]:
        try:
            e = json.loads(line)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("client_log_read", _exc, expected=True)
            continue
        if level and e.get("level") != level:
            continue
        out.append(e)
    return {"entries": out[-limit:]}


# ─────────────── diagnostics ingest(MetricKit + 使用者回報)────────────────
# App 端可觀測性的落地口:MetricKit 崩潰/卡頓/指標 payload 與「回報問題」
# 使用者回報,各自一筆一檔落在 DIAG_DIR(canonical.db 旁的 diagnostics/,
# 可用 POCKET_DIAG_DIR 覆蓋)。刻意不進 db、不做 dashboard —— 檔案能被
# 晨報巡查 / Claude session 直接 ls+read 就夠了。
# 隱私語意:payload 由 app 端組裝,只含堆疊/裝置/版本/錯誤記錄摘要,
# 不含對話內容;app 端有「診斷與使用資料」開關(預設開)管自動上傳。
DIAG_DIR = os.environ.get("POCKET_DIAG_DIR") \
    or os.path.join(os.path.dirname(CANON_DB), "diagnostics")
_DIAG_MAX_BYTES = 2 * 1024 * 1024   # 單筆上限 2MB(MetricKit crash json 通常 <300KB)
_DIAG_MAX_FILES = 500               # 目錄輪替上限 — 防當機迴圈灌爆磁碟
_DIAG_KINDS = {"metrickit_diagnostic", "metrickit_metric", "user_report"}


def _diag_prune() -> None:
    """把 DIAG_DIR 的 json 檔數修剪到 _DIAG_MAX_FILES(刪最舊,冪等)。"""
    try:
        files = [os.path.join(DIAG_DIR, f) for f in os.listdir(DIAG_DIR)
                 if f.endswith(".json")]
        if len(files) <= _DIAG_MAX_FILES:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[: len(files) - _DIAG_MAX_FILES]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


@app.post("/app/v1/diagnostics")
async def diagnostics_ingest(request: Request):
    _check_auth(request)
    raw = await request.body()
    if len(raw) > _DIAG_MAX_BYTES:
        raise http_err(413, "PAYLOAD_TOO_LARGE",
                       f"diagnostics 單筆上限 {_DIAG_MAX_BYTES} bytes")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("not a dict")
    except Exception:  # noqa: BLE001
        raise http_err(400, "BAD_REQUEST", "bad json")
    kind = str(body.get("kind", "")).strip()
    if kind not in _DIAG_KINDS:
        raise http_err(400, "BAD_REQUEST",
                       "kind 必須是 " + " / ".join(sorted(_DIAG_KINDS)))
    entry = {
        "server_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        # 裝置/版本中繼資料 — 截斷防灌爆;缺了也照收(舊 app 相容)。
        "app_version": str(body.get("app_version", ""))[:32],
        "build": str(body.get("build", ""))[:16],
        "device": str(body.get("device", ""))[:64],
        "os": str(body.get("os", ""))[:64],
        # user_report:使用者文字 + 錯誤記錄摘要;MetricKit:payload 原文。
        "note": str(body.get("note", ""))[:4000],
        "summary": body.get("summary"),
        "payload": body.get("payload"),
    }
    os.makedirs(DIAG_DIR, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    fname = f"{stamp}-{kind}-{uuid.uuid4().hex[:8]}.json"
    with open(os.path.join(DIAG_DIR, fname), "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=1)
    _diag_prune()
    return {"ok": True, "stored": fname}


async def _cx_input_claim(thread_id: str, client_id):
    """CX input 的 (thread_id, client_id) 冪等閘門。

    回傳 `(entry, prior)`:
      • `entry` 非 None → 這是第一次,呼叫端負責跑完後呼叫 `_cx_input_settle()`
        (成功)或 `_cx_input_release()`(失敗,讓重試能重新進來)
      • `prior` 非 None → 重複請求,呼叫端改呼叫 `_cx_input_replay(prior)`
      • 兩者皆 None → 沒帶 client_id,無法去重(維持原行為)
    """
    if not client_id:
        return None, None
    key = (str(thread_id), str(client_id))
    async with _CX_INPUT_INFLIGHT_LOCK:
        now = time.monotonic()
        for k in [k for k, e in _CX_INPUT_INFLIGHT.items()
                  if now - e["ts"] > _CX_INPUT_INFLIGHT_TTL]:
            _CX_INPUT_INFLIGHT.pop(k, None)      # 每次存取順手清 TTL,不會洩漏
        prior = _CX_INPUT_INFLIGHT.get(key)
        if prior is not None:
            return None, prior
        entry = {"ts": now, "key": key, "result": None, "event": asyncio.Event()}
        _CX_INPUT_INFLIGHT[key] = entry
        return entry, None


def _cx_input_settle(entry, result: dict) -> None:
    """第一次跑完 → 記下結果,喚醒正在等的重複請求。"""
    if entry is None:
        return
    entry["result"] = dict(result or {})
    entry["ts"] = time.monotonic()
    entry["event"].set()


def _cx_input_release(entry) -> None:
    """第一次就失敗 → 釋放 claim,否則重試會被自己擋整整一個 TTL(issue #9 同款坑)。"""
    if entry is None:
        return
    _CX_INPUT_INFLIGHT.pop(entry["key"], None)
    entry["event"].set()


async def _cx_input_replay(prior) -> dict:
    """重複請求:回傳第一次的結果。第一次還沒跑完就等一下(它可能正在
    await turn/start),等不到就回「收下了、排隊中」——重點是**不再送第二次**。"""
    if not prior["event"].is_set():
        try:
            await asyncio.wait_for(asyncio.shield(prior["event"].wait()),
                                   timeout=_CX_INPUT_INFLIGHT_WAIT)
        except (asyncio.TimeoutError, TimeoutError):
            pass
    return dict(prior.get("result") or {"queued": True, "delivery": "queued"})


@app.post("/codexsessions/{thread_id}/input")
async def codex_session_input(thread_id: str, request: Request):
    # 註:此端點呼叫 start_turn() → ensure_thread_loaded() → thread/resume,
    # 這裡「有」呼叫 resume,但跟 /stream 舊坑不同類——送新訊息本來就是
    # 要在這條 app-server 上真正「接管」該 thread 才能執行 turn/start,
    # resume 對這個操作是必要、無法避免的(這是 Codex 單一 writer 的本質
    # 限制,不是這支端點自己的 bug)。/stream 是唯讀回放,完全不需要接管
    # 就能用 thread/turns/list 讀到內容,所以那裡才是純粹的誤用。若使用者
    # 真的對一個「正被別的 codex app-server(ChatGPT 桌面 App/VS Code)持有」
    # 的 thread 送訊息,resume 仍可能卡住——但那是搶奪同一 thread 寫入權的
    # 固有衝突,防呆方式是 UI 層提示/衝突偵測,不是在這裡跳過 resume(跳過
    # 就送不出訊息了)。
    _check_auth(request)
    body = await _json_body(request)
    _registry_call_safe("touch", f"codex:{thread_id}")   # 戶政:活動記帳
    input_items = await _codex_input_items((body.get("text") or "").strip(),
                                           body.get("attachments") or [])
    if not input_items:
        raise HTTPException(status_code=400, detail="empty")
    _codex_history_invalidate(thread_id)     # new user turn → history changed
    text = _codex_user_input_text(input_items)
    client_id = body.get("client_id")
    # 冪等閘門必須包住**直送與入佇列兩條路**:排隊層拿掉 409 之後,重試就是
    # 保證重複執行(90s client timeout / OfflineOutbox / retryPending)。
    entry, prior = await _cx_input_claim(thread_id, client_id)
    if prior is not None:
        return {"ok": True, "thread_id": thread_id, "duplicate": True,
                **await _cx_input_replay(prior)}
    try:
        # 上一輪還在跑 → 收下入佇列(delivery=queued),**絕不回 4xx**。
        # 舊行為是直送 app-server 撞牆 → 409/502 → app 紅字「送出失敗」,而且 409
        # 的人話還是「會話目前沒有在執行」,跟真相完全相反。CC 早有 queued 語意。
        if CODEX_APP.is_active(thread_id):
            depth = CODEX_APP.enqueue_input(thread_id, input_items,
                                            client_id=client_id,
                                            cwd=body.get("cwd"), text=text)
            _cx_feed_input_accepted(
                thread_id, client_id, text,
                body.get("attachments") or [], typed_text=text,
                create_if_missing=True, queued=True)
            # `queued`(bool)才是 app `StudioBridgeV2.InputAck` 實際解的欄位;
            # 只回字串 delivery 的話 app 永遠當成沒排隊(泡泡不標「已排入下一輪」)。
            res = {"turn": None, "delivery": "queued",
                   "queued": True, "queue_depth": depth}
            _cx_input_settle(entry, res)
            return {"ok": True, "thread_id": thread_id, **res}
        try:
            started = await CODEX_APP.start_turn(thread_id, input_items,
                                                 client_id=client_id,
                                                 cwd=body.get("cwd"))
            _cx_feed_input_accepted(
                thread_id, client_id, text,
                body.get("attachments") or [], typed_text=text,
                create_if_missing=True)
            res = {"turn": (started or {}).get("turn"), "delivery": "accepted",
                   "queued": False}
            _cx_input_settle(entry, res)
            return {"ok": True, "thread_id": thread_id, **res}
        except Exception as e:  # noqa: BLE001
            _codex_http_error(e)
    except BaseException:
        _cx_input_release(entry)     # 失敗要放掉 claim,不然重試被自己擋整個 TTL
        raise


# S3 (wave 2): Codex-side model / approval-policy switching. The app-server
# exposes `thread/settings/update` (needs the experimentalApi capability the
# bridge already requests at initialize). Live-probed against codex-cli
# 0.142.2: accepted fields include `model` and `approvalPolicy`; the policy
# enum is validated server-side as below. No global setter exists — settings
# are per-thread.
_CODEX_APPROVAL_POLICIES = ("untrusted", "on-failure", "on-request",
                            "granular", "never")


@app.get("/codexsessions/{thread_id}/settings")
async def codex_session_settings_read(thread_id: str, request: Request):
    """讀回 per-thread 當前設定(model/approvalPolicy/effort/sandbox)給設定面板。
    來源 = thread/read;缺欄 = 不帶(舊 app 容忍)。runtime 可改的欄:model、
    approvalPolicy(見同路徑 POST);effort/sandbox 目前 spawn-only。"""
    _check_auth(request)
    try:
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
        thread = (res or {}).get("thread") or {}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)
    settings = {}
    for src, dst in (("model", "model"), ("approvalPolicy", "approvalPolicy"),
                     ("reasoningEffort", "effort"), ("sandboxMode", "sandbox")):
        val = thread.get(src)
        if val:
            settings[dst] = val
    return {"thread_id": thread_id, "settings": settings,
            "runtime_settable": ["model", "approvalPolicy"],
            "approval_policies": list(_CODEX_APPROVAL_POLICIES)}


@app.post("/codexsessions/{thread_id}/settings")
async def codex_session_settings(thread_id: str, request: Request):
    """Update per-thread Codex settings. body {"model": str?,
    "approvalPolicy": "untrusted"|"on-failure"|"on-request"|"granular"|"never"}
    — at least one field required."""
    _check_auth(request)
    body = await request.json()
    params = {"threadId": thread_id}
    model = str(body.get("model") or "").strip()
    policy = str(body.get("approvalPolicy") or "").strip()
    if model:
        params["model"] = model
    if policy:
        if policy not in _CODEX_APPROVAL_POLICIES:
            raise HTTPException(status_code=400,
                                detail="approvalPolicy must be one of "
                                       + "|".join(_CODEX_APPROVAL_POLICIES))
        params["approvalPolicy"] = policy
    if len(params) == 1:
        raise HTTPException(status_code=400, detail="model or approvalPolicy required")
    try:
        await CODEX_APP.ensure_thread_loaded(thread_id)
        await CODEX_APP.call("thread/settings/update", params, timeout=15.0)
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)
    applied = {k: v for k, v in params.items() if k != "threadId"}
    _log_event("codex_settings_update", thread=thread_id[:16], **applied)
    return {"ok": True, "thread_id": thread_id, "applied": applied}


@app.post("/codexsessions/{thread_id}/interrupt")
async def codex_session_interrupt(thread_id: str, request: Request):
    _check_auth(request)
    try:
        await CODEX_APP.interrupt_turn(thread_id)
        return {"ok": True, "thread_id": thread_id}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.post("/codexsessions/{thread_id}/answer")
async def codex_session_answer(thread_id: str, request: Request):
    """回答 codex 的 question 類 server request（`item/tool/requestUserInput`
    / `mcpServer/elicitation/request`）。

    body 形狀刻意與 CC 那條 `POST /ccsessions/{name}/answer` 對齊:
        {"keys": ["opt0"], "text": "自由輸入（選填）", "submit": true}
    `keys` 只取第一個（codex 的卡一次只帶一組選項）；`key` 單數也吃。
    沒有 pending question → 409。
    """
    _check_auth(request)
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        b = {}
    if not isinstance(b, dict):
        b = {}
    keys = b.get("keys")
    if isinstance(keys, list) and keys:
        key = str(keys[0] or "")
    else:
        key = str(b.get("key") or "")
    text = str(b.get("text") or b.get("answer") or "")
    record = CODEX_APP.pending_question_for_thread(thread_id)
    if not record:
        raise http_err(409, "QUESTION_NOT_PENDING",
                       "no pending Codex question for thread")
    okeys = [str(o.get("key") or "") for o in (record.get("options") or [])]
    if key and key not in okeys:
        raise http_err(400, "UNKNOWN_KEY", f"key 必須是 {okeys} 之一")
    if not key and not text:
        raise http_err(400, "MISSING_ANSWER", "需要 keys 或 text 其中之一")
    try:
        result = await CODEX_APP.answer_question(record["id"], key=key, text=text)
    except CodexAppServerError as e:
        if e.code == 404:
            raise http_err(409, "QUESTION_NOT_PENDING",
                           "Codex question is no longer live")
        _codex_http_error(e)
    return {"ok": True, "thread_id": thread_id, "id": record["id"],
            "status": result["status"], "key": key, "result": result["result"]}


@app.get("/codexsessions/{thread_id}/history")
async def codex_session_history(thread_id: str, request: Request, limit: int = 40,
                                cursor: str | None = None):
    _check_auth(request)
    lim = max(1, min(limit, 100))
    key = (thread_id, lim, cursor or "")
    hit = _CODEX_HISTORY_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CODEX_HISTORY_TTL:
        _log_event("codex_history_cache_hit", thread=thread_id[:16],
                   limit=lim, cursor=bool(cursor))
        return hit[1]
    try:
        params = {
            "threadId": thread_id,
            "limit": lim,
            "itemsView": "full",
            "sortDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor
        res = await CODEX_APP.call("thread/turns/list", params, timeout=45.0)
        turns = list((res or {}).get("data", []))
        turns.reverse()
        payload = {"text": _codex_format_turns(turns),
                   "more": bool((res or {}).get("nextCursor")),
                   "nextCursor": (res or {}).get("nextCursor")}
        _CODEX_HISTORY_CACHE[key] = (time.monotonic(), payload)
        return payload
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.get("/codexsessions/{thread_id}/stream")
async def codex_session_stream(thread_id: str, request: Request, replay: int = 20,
                               follow: bool = False):
    _check_auth(request)
    cid = "codexsess-" + uuid.uuid4().hex[:16]

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": thread_id, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        # 只讀 replay 不做 thread/resume(同 _cx_card_digest 的防呆):resume 會
        # 「接管」該 thread,若它正被別的 codex app-server(如 ChatGPT 桌面
        # App、VS Code,thread source=vscode/appServer)持有就會卡死整條
        # stdio → 之後所有 codex 呼叫一起 hang(Pocket app「連線中...」卡死
        # 就是這樣引爆的)。thread/turns/list 本來就不需 resume 也讀得到
        # (/codexsessions/{id}/history 就是這樣讀的),所以這裡直接列
        # turns,不呼叫 ensure_thread_loaded()/thread/resume。真正要送
        # 新訊息時 /codexsessions/{id}/input → start_turn() 才需要
        # resume(那是必要的,因為要在這條 app-server 上真的接管+送 turn)。
        if replay > 0:
            try:
                res = await CODEX_APP.call("thread/turns/list", {
                    "threadId": thread_id,
                    "limit": max(1, min(replay, 50)),
                    "itemsView": "full",
                    "sortDirection": "desc",
                }, timeout=30.0)
                turns = list((res or {}).get("data", []))
                turns.reverse()
                text = _codex_format_turns(turns)
                if text:
                    yield chunk({"content": text})
            except Exception as e:  # noqa: BLE001
                _log_exc("codex_session_stream.gen", e, expected=True)
                yield chunk({"content": f"\n⚠️ history failed: {e}\n"})
        idx = 0
        if replay <= 0 and not CODEX_APP.is_active(thread_id):
            idx = len(CODEX_APP.events_for(thread_id))
        event_generation = CODEX_APP.thread_event_generations[thread_id]
        seen_turn_activity = CODEX_APP.is_active(thread_id)
        idle = 0
        idle_limit = 120 if follow else 0
        while True:
            if await request.is_disconnected():
                break
            events = CODEX_APP.events_for(thread_id)
            current_generation = CODEX_APP.thread_event_generations[thread_id]
            if current_generation != event_generation:
                event_generation = current_generation
                idx = 0
                seen_turn_activity = True
            # Defensive fallback for buffer compaction or an older producer
            # that clears the list without bumping the generation.
            if idx > len(events):
                idx = 0
                seen_turn_activity = True
            while idx < len(events):
                kind, val = events[idx]
                idx += 1
                c = _fmt_item(kind, val)
                if c:
                    yield chunk({"content": c})
            active = CODEX_APP.is_active(thread_id)
            if active:
                seen_turn_activity = True
            if _codex_stream_turn_finished(seen_turn_activity, active,
                                           idx, len(events)):
                break
            if not active and idx >= len(events) and not follow:
                break
            await asyncio.sleep(0.5)
            idle += 1
            if idle >= max(1, int(SSE_KEEPALIVE_SECS / 0.5)):
                idle = 0
                yield ": keepalive\n\n"
            if follow and idle_limit > 0 and not active:
                idle_limit -= 1
                if idle_limit <= 0:
                    break
            elif follow and active:
                idle_limit = 120
        yield chunk({}, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ═══════════════ cc/cx spawn config「全集控制面」(設計 §2.1/§2.2)═══════════
# 一份 config dict → 正確的 CLI flags / codex app-server 參數 / 注入 env。
# 這裡全是純函式(端點只負責驗證 + 呼叫),讓「派子程序時用這個配置」以及
# 未來 Harness 的 Subagent 設定共用同一份翻譯層。
#
# 鐵則:api_key 全程「只進該子程序的 env」,永不進 _log_event、永不進
# canonical.db、永不落明文檔(除既有 0600 host-local secret 慣例)。
#
# runtime vs spawn-only(每 provider):
#   cc:模型/審核方法 = runtime(/model、/mode 已在);effort/budget/fallback/
#      append-system-prompt = launch-only(且 budget/fallback 只在 --print
#      headless 生效,互動 TUI 不吃)。
#   cx:模型/approvalPolicy = runtime(/settings 已在,thread/settings/update);
#      effort/sandbox/profile/api_key = launch-only,且只有 `codex exec` 子程序
#      (走 /dispatch)吃得到——共用 app-server 的 thread 無法逐 thread 注入 key。

_SPAWN_CC_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# claude --permission-mode 的 CLI enum(與互動 TUI 的 _CC_MODES 不同,別混用)
_SPAWN_CC_PERMISSION_MODES = ("acceptEdits", "auto", "manual", "plan",
                              "bypassPermissions")
_SPAWN_CX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
_SPAWN_CX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
# cx 審核策略沿用既有 _CODEX_APPROVAL_POLICIES(單一真相,與 /settings 對齊)。


class SpawnConfigError(ValueError):
    """spawn config 驗證失敗;`.detail` 為要回給 app 的 zh-TW 人話(400)。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _spawn_fmt_usd(v) -> str:
    s = ("%.4f" % float(v)).rstrip("0").rstrip(".")
    return s or "0"


def _spawn_config_validate(raw, provider: str) -> dict:
    """把 app 傳來的 config 正規化成內部 dict。

    - None / 缺欄 = 沿用今天行為(舊 app 不帶 config 完全不受影響)。
    - 未知欄位一律忽略(前向相容:新 app 多帶的欄不會炸舊 bridge,反之亦然)。
    - 任何 enum 違規 → SpawnConfigError(呼叫端翻 400 + zh-TW)。
    provider: "cc"(claude_code / dispatch claude)或 "cx"(codex)。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpawnConfigError("config 必須是物件(JSON object)")
    cfg: dict = {}

    # --- 字串欄 ---
    for k in ("model", "fallback_model", "append_system_prompt", "api_key",
              "profile"):
        v = raw.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            raise SpawnConfigError(f"{k} 必須是字串")
        if k == "append_system_prompt":
            if v.strip() == "":
                continue
            cfg[k] = v            # 系統提示保留原樣(含換行)
        else:
            v = v.strip()
            if v == "":
                continue
            cfg[k] = v

    # --- 花費上限(美元)---
    b = raw.get("max_budget_usd")
    if b is not None:
        if isinstance(b, bool) or not isinstance(b, (int, float)):
            raise SpawnConfigError("max_budget_usd 必須是數字(美元)")
        if b <= 0:
            raise SpawnConfigError("max_budget_usd 必須大於 0")
        cfg["max_budget_usd"] = float(b)

    # --- effort(enum 依 provider 不同)---
    e = raw.get("effort")
    if e is not None:
        e = str(e).strip()
        allowed = _SPAWN_CC_EFFORTS if provider == "cc" else _SPAWN_CX_EFFORTS
        if e not in allowed:
            raise SpawnConfigError(
                f"effort 需為 {'/'.join(allowed)} 其一(收到 {e!r})")
        cfg["effort"] = e

    # --- 審核 ---
    if provider == "cc":
        pm = raw.get("permission_mode")
        if pm is not None:
            pm = str(pm).strip()
            if pm not in _SPAWN_CC_PERMISSION_MODES:
                raise SpawnConfigError(
                    "permission_mode 需為 "
                    f"{'/'.join(_SPAWN_CC_PERMISSION_MODES)} 其一(收到 {pm!r})")
            cfg["permission_mode"] = pm
    else:  # cx
        ap = raw.get("approval_policy")
        if ap is None:
            ap = raw.get("approvalPolicy")   # 容錯:app 傳 camelCase 也接
        if ap is not None:
            ap = str(ap).strip()
            if ap not in _CODEX_APPROVAL_POLICIES:
                raise SpawnConfigError(
                    "approval_policy 需為 "
                    f"{'/'.join(_CODEX_APPROVAL_POLICIES)} 其一(收到 {ap!r})")
            cfg["approval_policy"] = ap
        sb = raw.get("sandbox")
        if sb is not None:
            sb = str(sb).strip()
            if sb not in _SPAWN_CX_SANDBOXES:
                raise SpawnConfigError(
                    "sandbox 需為 "
                    f"{'/'.join(_SPAWN_CX_SANDBOXES)} 其一(收到 {sb!r})")
            cfg["sandbox"] = sb

    return cfg


def _spawn_cc_flags(cfg: dict) -> list:
    """config → claude CLI flags(headless `-p` 子程序用)。不含 api_key(走 env)。
    注意 --max-budget-usd / --fallback-model 只在 --print 模式生效(headless 有、
    互動 TUI 無)。"""
    argv: list = []
    if cfg.get("model"):
        argv += ["--model", cfg["model"]]
    if cfg.get("effort"):
        argv += ["--effort", cfg["effort"]]
    if cfg.get("permission_mode"):
        argv += ["--permission-mode", cfg["permission_mode"]]
    if cfg.get("max_budget_usd") is not None:
        argv += ["--max-budget-usd", _spawn_fmt_usd(cfg["max_budget_usd"])]
    if cfg.get("fallback_model"):
        argv += ["--fallback-model", cfg["fallback_model"]]
    if cfg.get("append_system_prompt"):
        argv += ["--append-system-prompt", cfg["append_system_prompt"]]
    return argv


def _spawn_cx_exec_flags(cfg: dict) -> list:
    """config → `codex exec` flags(headless dispatch 子程序用)。api_key 走 env。"""
    argv: list = []
    if cfg.get("model"):
        argv += ["-m", cfg["model"]]
    if cfg.get("approval_policy"):
        argv += ["-a", cfg["approval_policy"]]
    if cfg.get("sandbox"):
        argv += ["-s", cfg["sandbox"]]
    if cfg.get("effort"):
        argv += ["-c", f"model_reasoning_effort={cfg['effort']}"]
    if cfg.get("profile"):
        argv += ["-p", cfg["profile"]]
    return argv


def _spawn_cx_thread_params(cfg: dict) -> dict:
    """config → codex app-server `thread/start` 額外參數(共用 app-server 的
    thread 路徑)。app-server 已實測吃 model / approvalPolicy;effort / sandbox
    以 camelCase 透傳,app-server 不認得的欄位會被忽略(前向相容)。"""
    params: dict = {}
    if cfg.get("model"):
        params["model"] = cfg["model"]
    if cfg.get("approval_policy"):
        params["approvalPolicy"] = cfg["approval_policy"]
    if cfg.get("effort"):
        params["reasoningEffort"] = cfg["effort"]
    if cfg.get("sandbox"):
        params["sandboxMode"] = cfg["sandbox"]
    return params


def _spawn_env(cfg: dict, provider: str, base_env=None) -> dict:
    """回傳注入了 BYO api_key 的 env(只給這一個子程序)。無 key → 原封不動的 env
    (= 沿用主機自己的 auth)。key 只寫進 env,絕不進 log。"""
    env = dict(base_env if base_env is not None else os.environ)
    key = cfg.get("api_key")
    if key:
        if provider == "cc":
            env["ANTHROPIC_API_KEY"] = key
        else:
            env["OPENAI_API_KEY"] = key
    return env


def _spawn_config_redacted(cfg: dict) -> dict:
    """log 安全版:api_key → 遮罩。任何 _log_event 都必須先過這個。"""
    if not cfg:
        return {}
    out = dict(cfg)
    if out.get("api_key"):
        out["api_key"] = "***redacted***"
    return out


def _spawn_config_public(cfg: dict) -> dict:
    """回給 app 讀回的設定(status/settings)。api_key 完全不回,只回布林
    has_api_key,讓面板能顯示「已帶自備金鑰」但拿不到明文。"""
    if not cfg:
        return {}
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    if cfg.get("api_key"):
        out["has_api_key"] = True
    return out


# ── cc 互動 session(ccsess)的 launch-config pin 持久化 ──────────────────
# ccsess 的 claude_cmd() 在 launch 時逐檔讀 pin(model pin 已在)。這裡把 spawn
# config 落成同慣例的側寫檔,交給 ccsess 於下次啟動翻成 flags;api_key 落 0600
# secret 檔(不進 conf、不進 log)。bridge 只負責「寫 pin + 讀回」,honor 由
# ccsess 端(companion change)完成——缺 companion 時 model pin 仍照舊生效。
CCSESS_SPAWN_DIR = os.path.expanduser("~/.config/ccsess/spawn")
CCSESS_SECRET_DIR = os.path.expanduser("~/.config/ccsess/secret")


def _cc_write_spawn_pins(name: str, cfg: dict) -> dict:
    """把 launch-time config 落到 ccsess pin。回傳 redacted 版(供 log/回應)。"""
    redacted = _spawn_config_redacted(cfg)
    # model 走既有 pin(_run_ccsess new 已處理);這裡只落其餘 launch flags。
    flag_cfg = {k: v for k, v in cfg.items() if k not in ("api_key",)}
    try:
        os.makedirs(CCSESS_SPAWN_DIR, exist_ok=True)
        path = os.path.join(CCSESS_SPAWN_DIR, name + ".json")
        if flag_cfg:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(flag_cfg, f, ensure_ascii=False)
        elif os.path.exists(path):
            os.remove(path)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_write_spawn_pins.flags", _exc, expected=True)
    # api_key → 0600 secret 檔(host-local,永不外洩)。
    key = cfg.get("api_key")
    try:
        os.makedirs(CCSESS_SECRET_DIR, exist_ok=True)
        os.chmod(CCSESS_SECRET_DIR, 0o700)
        spath = os.path.join(CCSESS_SECRET_DIR, name)
        if key:
            fd = os.open(spath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, key.encode("utf-8"))
            finally:
                os.close(fd)
        elif os.path.exists(spath):
            os.remove(spath)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_write_spawn_pins.secret", _exc, expected=True)
    return redacted


def _cc_read_spawn_config(name: str) -> dict:
    """讀回某 cc session 的 launch config(status 讀回用)。含 model pin +
    spawn.json;api_key 不回明文,只回 has_api_key 布林。"""
    out: dict = {}
    try:
        mpath = os.path.expanduser(f"~/.config/ccsess/model/{name}")
        if os.path.exists(mpath):
            with open(mpath, encoding="utf-8") as f:
                m = f.read().strip()
            if m:
                out["model"] = m
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_read_spawn_config.model", _exc, expected=True)
    try:
        path = os.path.join(CCSESS_SPAWN_DIR, name + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                out.update(json.load(f) or {})
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_read_spawn_config.flags", _exc, expected=True)
    try:
        if os.path.exists(os.path.join(CCSESS_SECRET_DIR, name)):
            out["has_api_key"] = True
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_read_spawn_config.secret", _exc, expected=True)
    return out


# headless dispatch 子程序的 BYO key/config(記憶體 only,永不持久化/log)。
_SPAWN_SECRETS: dict = {}


def _claude_argv(parent: str, prompt: str, resume: str | None = None,
                 config: dict | None = None):
    """Build a headless Claude Code argv. `resume` continues an existing CC
    session id so follow-up turns keep the sub-agent's full context.
    `config` = 已驗證的 spawn config(model/effort/permission_mode/budget/
    fallback/append-system-prompt);api_key 不進 argv(走 env)。"""
    mem_home = home_for(parent or "yuanfang")
    mcp_cfg = json.dumps({"mcpServers": {"studio-memory": {
        "command": "python3",
        "args": [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "studio_memory_mcp.py")],
        "env": {"STUDIO_MEMORY_HOME": mem_home}}}}, ensure_ascii=False)
    hint = ("你可以用 studio-memory MCP 的 read_memory / search_memory 讀善彰的"
            "Hermes 長期記憶(身份、持倉、專案、人脈),做任務前先讀以對齊脈絡;"
            "有值得長期記住的新事實再用 write_memory 寫回。")
    cfg = config or {}
    perm = cfg.get("permission_mode") or "bypassPermissions"
    argv = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", perm,
            "--mcp-config", mcp_cfg, "--append-system-prompt", hint]
    # spawn config 的其餘 flags(permission-mode 已在上面併入,不重複)。
    extra = {k: v for k, v in cfg.items() if k != "permission_mode"}
    argv += _spawn_cc_flags(extra)
    if resume:
        argv += ["--resume", resume]
    return argv


# A sub-agent that produces NOTHING on stdout for this long is stalled: kill it
# so its _BG_TASKS entry finishes instead of leaking a forever-pending task.
# env-overridable so the regression test can trip it in milliseconds.
_AGENT_STALL_SECS = float(os.environ.get("BRIDGE_AGENT_STALL_SECS", "1800"))


async def _stream_agent(sid: str, argv: list, cwd: str, fail_label: str,
                        env: dict | None = None):
    """Run a sub-agent subprocess, append its transcript to the sub's output
    buffer, capture the Claude Code session id (for later --resume), and mark
    the sub done when it exits. `env` 覆寫子程序環境(BYO api_key 注入用)。"""
    sub = SUBSESSIONS[sid]
    out = sub["output"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env)
        sub["proc"] = proc
        while True:
            # No-progress watchdog: a wedged provider (network black-hole, dead
            # MCP…) otherwise streams nothing forever.
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(),
                                             timeout=_AGENT_STALL_SECS)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                sub["status"] = "stalled"
                out.append(("text", "\n⚠️ (超過 30 分鐘無輸出,已強制中止子代理行程)"))
                _log_event("subagent_stalled", sid=sid, cwd=cwd,
                           tool=sub.get("tool"))
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception as e:  # noqa: BLE001
                _log_event("subagent_bad_json", sid=sid,
                           error=type(e).__name__, line=line[:160])
                continue
            sess = ev.get("session_id") if isinstance(ev, dict) else None
            if sess:
                sub["cc_session"] = sess          # latest id → resume target
            for item in _parse_agent_event(ev):
                out.append(item)
            sub["lastAt"] = time.time()
        await proc.wait()
    except Exception as e:                                  # noqa: BLE001
        out.append(("text", f"\n⚠️ {fail_label}:{e}"))
        _log_event("subagent_stream_failed", sid=sid,
                   error=type(e).__name__, error_message=str(e)[:160])
    finally:
        # issue #7 項目 3:任何離開路徑都不准留下孤兒子行程。
        # `except Exception` 接不到 CancelledError(它是 BaseException),所以
        # 關機/呼叫端取消時,原本會直接跳到 finally 而把 claude/codex 子行程
        # 留在那裡跑到天荒地老——這就是「hang task 累積」的來源。放在 finally
        # 最前面:就算下面的 await 出事,也已經先收掉了。
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                _log_event("subagent_proc_killed", sid=sid, pid=proc.pid,
                           tool=sub.get("tool"))
            except ProcessLookupError:
                pass
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_stream_agent.kill", _exc, expected=True, sid=sid)
        if sub.get("status") != "stalled":
            sub["status"] = "done"
            # 戶政:headless dispatch 行程結束 = 真正完工(不是 turn 完成的
            # 過度激進判定)→ 記 done,由 reaper 下一輪照寬限歸檔。
            _registry_call_safe("mark_done", sid)
        sub["lastAt"] = time.time()
        # Isolated dispatch: reclaim the worktree if the agent left it clean.
        if sub.get("worktree"):
            await _cleanup_worktree(sid, sub)
        _subsession_persist(sid)   # issue #5: flush transcript + resume target
        # M23: push when a dispatched CC/Codex task finishes, so the app surfaces
        # it even when backgrounded (Telegram is the fallback now, not the primary
        # signal). Fire-and-forget; failures are swallowed inside push_notify.
        _label = sub.get("name") or sub.get("tool") or "任務"
        asyncio.create_task(push_notify(
            "✅ 任務完成", str(_label)[:120],
            {"kind": "task_done", "session_id": sid}))


async def _run_dispatch(sid: str, tool: str, task: str, cwd: str, isolate: bool = False,
                        config: dict | None = None):
    """Spawn a headless Claude Code / Codex sub-agent for the initial task.
    `config` = 已驗證 spawn config(含 BYO api_key,只在此進子程序 env)。"""
    sub = SUBSESSIONS[sid]
    cfg = config or _SPAWN_SECRETS.get(sid) or {}
    provider = "cx" if tool == "codex" else "cc"
    run_cwd = cwd
    if isolate:
        wt = await _make_worktree(cwd, sid)
        if wt != cwd:
            run_cwd = wt
            sub["worktree"] = wt
            sub["base_cwd"] = cwd   # fall back here if the worktree is reclaimed
            sub["cwd"] = wt   # follow-ups stay in the same isolated tree
            # 戶政:worktree 實際路徑落籍——reaper 只收「登記過路徑」的樹,
            # 絕不用猜的(藍圖 §3.2)。
            _registry_call_safe("set_worktree", sid, wt)
            sub["output"].append(("text", f"_(隔離工作區 worktree:`{wt}` · 分支 `pocket/{sid}`)_\n\n"))
    if tool == "codex":
        argv = [_resolve_codex_bin(), "exec"] + _spawn_cx_exec_flags(cfg) + ["--json", task]
    else:
        argv = _claude_argv(sub.get("parent", "yuanfang"), task, config=cfg)
    env = _spawn_env(cfg, provider)
    await _stream_agent(sid, argv, run_cwd, "dispatch 失敗", env=env)


async def _run_resume(sid: str, prompt: str):
    """Follow-up turn into an existing sub-session — resumes the CC session so
    the sub-agent keeps its full prior context."""
    sub = SUBSESSIONS[sid]
    cwd = sub.get("cwd") or HOME_ROOT
    cfg = _SPAWN_SECRETS.get(sid) or {}   # 沿用開派時的 spawn config + BYO key
    if sub.get("tool") == "codex":
        argv = [_resolve_codex_bin(), "exec"] + _spawn_cx_exec_flags(cfg) + ["--json", prompt]
        env = _spawn_env(cfg, "cx")
    else:
        argv = _claude_argv(sub.get("parent", "yuanfang"), prompt,
                            resume=sub.get("cc_session"), config=cfg)
        env = _spawn_env(cfg, "cc")
    await _stream_agent(sid, argv, cwd, "追問失敗", env=env)


def _parse_agent_event(ev: dict):
    """Map a Claude-Code / Codex stream-json event → transcript items."""
    items = []
    t = ev.get("type")
    if t == "assistant":
        for c in ((ev.get("message") or {}).get("content") or []):
            if c.get("type") == "text" and c.get("text"):
                items.append(("text", c["text"]))
            elif c.get("type") == "tool_use":
                name = c.get("name", "tool")
                inp = c.get("input") or {}
                cmd = inp.get("command") or inp.get("file_path") or inp.get("path") \
                    or (json.dumps(inp, ensure_ascii=False)[:120] if inp else "")
                items.append(("tool_start", {"name": name, "cmd": cmd}))
    elif t == "user":
        for c in ((ev.get("message") or {}).get("content") or []):
            if c.get("type") == "tool_result":
                res = c.get("content")
                if isinstance(res, list):
                    res = " ".join(p.get("text", "") for p in res if isinstance(p, dict))
                if res:
                    items.append(("tool_result", {"text": str(res), "status": "done"}))
    elif t in ("item.completed", "message"):  # codex-ish fallback
        txt = ev.get("text") or ev.get("content")
        if isinstance(txt, str) and txt:
            items.append(("text", txt))
    return items


def _persona_preview_tg(home: str):
    """Latest user-visible message of the persona's Telegram session → (text, ts).
    Walks back past rows that are pure runtime injection so the conversation list
    never previews machine-facing preamble."""
    import sqlite3
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return (None, None)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.execute(
                "SELECT m.role, m.content, m.timestamp FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE s.source='telegram' AND m.role IN ('user','assistant') "
                "AND m.content IS NOT NULL AND m.content != '' "
                "ORDER BY m.timestamp DESC LIMIT 10")
            rows = cur.fetchall()
            con.close()
            for role, content, ts in rows:
                text, _atts = _tg_extract_attachments(str(content))
                if role == "user":
                    text = _tg_clean_content(text)
                    if text is None:
                        continue
                if text:
                    return (text[:80], ts)
        finally:
            con.close()
    except Exception as _exc:
        _log_exc("_persona_preview_tg", _exc, expected=True)
        pass
    return (None, None)


def _persona_preview_canon(session: str):
    """Latest user-visible message from the persona's CANONICAL store (app turns)
    → (text, ts). studio-card fences collapse to their fallback text and the
    folded 〈執行步驟〉 appendix is stripped, matching what the conversation view
    shows — so the list preview never leaks a raw fence or tool log."""
    try:
        msgs = _canon_messages(session, 20)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_preview_canon", _exc, expected=True)
        return (None, None)
    for m in reversed(msgs):
        if m.get("role") not in ("user", "assistant"):
            continue
        content = (m.get("content") or "")
        if not content.strip():
            continue
        clean, _bodies = carddigest.extract_studio_cards(content)
        clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S).strip()
        if clean:
            return (clean[:80], m.get("ts"))
    return (None, None)


def _persona_preview(home: str, session: str | None = None):
    """Conversation-list preview for a persona → (text, ts).

    The list must mirror the merged conversation view (/app/v1/messages =
    canonical ⊕ Telegram): reading TG only left app-side turns invisible, so the
    preview stayed stale AND the app's preview-change unread detector never fired
    for in-app messages. Take the newer of the two sources (both epoch seconds)."""
    cands = []
    tg_text, tg_ts = _persona_preview_tg(home)
    if tg_text and tg_ts is not None:
        cands.append((tg_ts, tg_text))
    if session:
        cn_text, cn_ts = _persona_preview_canon(session)
        if cn_text and cn_ts is not None:
            cands.append((cn_ts, cn_text))
    if not cands:
        return (None, None)
    ts, text = max(cands, key=lambda x: x[0])
    return (text, ts)


def _persona_preview_merged(session: str, home: str):
    """Conversation-list preview drawn from the SAME merged source as the card
    stream (canonical ⊕ Telegram ⊕ cron reports), PLUS who sent the latest line
    and the last *inbound* (persona) line.

    Returns (latest_text, latest_ts, sender, inbound_text, inbound_ts) where
    `sender` is "persona" (assistant) | "user" | None.

    Why the extra fields: in a two-sided TG-style chat the newest message is
    often the user's own send. The client's bell/unread detector keys off
    `inbound_ts` so a self-send never lights the dot, and the notification
    subtitle shows `inbound_text` so it never echoes the user's own words.

    Skips the report *sync* (`_sync_persona_reports`) — the 30s card follower
    already keeps report_events fresh — so this is just a few cheap read-only
    sqlite queries, safe to run on every /sessions poll."""
    msgs = []
    canon = []
    try:
        canon = _canon_messages(session, 30)               # app turns (canonical.db)
        msgs.extend(canon)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_preview_merged", _exc, expected=True)
        pass
    try:
        # ⚠️ TG 側要套**與卡片流同一組過濾**(`_persona_messages` / v2 卡片流的
        # `_tg_dup` + `_tg_quarantined`)。少套這層 = 預覽顯示一則點進去找不到
        # 的訊息 —— 使用者體感是「App 弄丟我的訊息」(0806 巡檢 S1)。
        # ① 雙寫壓重:同一句在 canonical 與 state.db 各留一份,卡片流壓掉 TG 那
        #    份;預覽若不壓,兩份文字微漂時會挑到卡片流不會顯示的那一份。
        # ② 活 turn 檢疫:回合進行中的 TG assistant 近訊(1h 內)卡片流先不出頁,
        #    等 canonical 總結落地;預覽照顯示就會領先畫面一則。
        canon_recent = [((m.get("ts") or 0), m.get("role"),
                         _dedup_norm(m.get("content") or ""))
                        for m in canon if m.get("role") in ("user", "assistant")]
        turn_started = _session_turn_started_at(session)
        for m in _persona_history(home, 30):               # Telegram (state.db), user-cleaned
            if _dual_source_dup(_dedup_norm(m.get("content") or ""), m.get("role"),
                                m.get("ts") or 0, canon_recent):
                continue
            if _tg_assistant_in_quarantine(turn_started, m.get("role"), m.get("ts") or 0):
                continue
            msgs.append(m)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_preview_merged#2", _exc, expected=True)
        pass
    try:
        msgs.extend(_report_messages(session, 10))         # cron briefs (role=assistant)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_preview_merged#3", _exc, expected=True)
        pass

    def _visible(m) -> str | None:
        if m.get("role") not in ("user", "assistant"):
            return None
        clean, _bodies = carddigest.extract_studio_cards(m.get("content") or "")
        clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S).strip()
        return clean or None

    msgs.sort(key=lambda m: m.get("ts") or 0)
    latest_text = latest_ts = sender = None
    inbound_text = inbound_ts = None
    for m in reversed(msgs):
        text = _visible(m)
        if not text:
            continue
        if latest_text is None:
            latest_text, latest_ts = text[:120], m.get("ts")
            sender = "persona" if m.get("role") == "assistant" else "user"
        if m.get("role") == "assistant" and inbound_text is None:
            inbound_text, inbound_ts = text[:120], m.get("ts")
        if latest_text is not None and inbound_text is not None:
            break
    return (latest_text, latest_ts, sender, inbound_text, inbound_ts)


# TG→app media (N4, pocketagent#36): Hermes' image_routing appends a
# `[Image attached at: /local/path]` hint line to the stored user text for
# every photo the TG gateway downloads (into <home>/image_cache). state.db has
# no media column, so those hint lines ARE the media record — parse them back
# into real attachments.
_TG_IMAGE_MARKER = re.compile(r"\[Image attached at: ([^\]\n]+)\]")
# Replied-to media cached by the TG gateway (gateway/platforms/telegram.py:5796):
# [Replied-to image 'file_36.jpg' saved at: /path]
_TG_REPLIED_MEDIA = re.compile(r"\[Replied-to (\w+) '([^']*)' saved at: ([^\]\n]+)\]")
# TG→app files (承 N4 思路,補齊照片以外的三類):gateway 對 document/audio/
# video 各寫一種 saved-at 提示行(gateway/run.py:1838-1848/8646/8665),同樣
# 「提示行即媒體記錄」— 解析回一等附件。app 端 Attachment.Kind 只有
# image/file/audio:document/video → file(video 帶 video/* mime),audio → audio。
_TG_TEXTDOC_NOTE = re.compile(
    r"\[The user sent a text document: '([^']*)'\.(?s:.*?)also saved at: ([^\]\n]+)\]")
_TG_FILE_NOTE = re.compile(
    r"\[The user sent (a document|an audio file attachment|a video attachment): "
    r"'([^']*)'\. It is saved at: (.+?)\.\s(?s:.*?)\]")
# hermes 0.19 起,TG 使用者照片走 vision 自動描述(gateway/run.py:16552-16568):
#   [The user sent an image~ Here's what I can see:\n<描述>]
#   [If you need a closer look, use vision_analyze with image_url: <path> ~]
# 描述與提示行都是 agent 面文字,app 端要的是圖本身 → 從 image_url 拿回路徑
# 掛一等附件、整塊移除。描述失敗的兩種變體("couldn't quite see it"/
# "something went wrong")同樣以 `vision_analyze using image_url: <path>]` 收尾。
_TG_VISION_IMAGE = re.compile(
    r"\[The user sent an image~ Here's what I can see:(?s:.*?)\]\s*"
    r"\[If you need a closer look, use vision_analyze with image_url: "
    r"([^\]\s]+)\s*~\]")
_TG_VISION_IMAGE_FAIL = re.compile(
    r"\[The user sent an image but (?s:.*?)vision_analyze using image_url: "
    r"([^\]\s]+)\]")
# 人格在 TG 端直接回媒體:state.db 的 assistant 列存「原話+遞送標記
# `MEDIA:<path>`」(hermes tools/send_message_tool 語法;實例:live db row
# 9591 的 HR 表單 PDF)。副檔名集合抄 hermes gateway/platforms/base.py
# MEDIA_DELIVERY_EXTS —— 同一組可遞送類型;標記在,代表 gateway 當時真的
# 把這個檔案發進了 TG,對 app 就是一等附件。路徑允許含空白(錨定副檔名),
# 引號/反引號包裹的變體照 hermes 同款收。
_TG_MEDIA_EXTS = (
    "png|jpe?g|gif|webp|bmp|tiff|svg|mp4|mov|avi|mkv|webm|mp3|wav|ogg|opus|"
    "m4a|flac|pdf|docx?|odt|rtf|txt|md|epub|xlsx?|ods|csv|tsv|json|xml|"
    "ya?ml|pptx?|odp|key|zip|tar|gz|tgz|bz2|xz|7z|rar|apk|ipa|html?")
_TG_MEDIA_TAG = re.compile(
    r'''[`"']?MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''~?/\S+(?:[^\S\n]+\S+)*?\.(?:''' + _TG_MEDIA_EXTS + r'''))'''
    r'''(?=[\s`"',;:)\]}]|$)[`"']?''', re.IGNORECASE)
# 跨 session send_message 的鏡射列(hermes tools/send_message_tool.py
# `_describe_media_for_mirror`):舊 hermes 純媒體訊息在 state.db 只留
# `[Sent image attachment]` 這種佔位 —— 路徑不落任何欄位(mirror_to_session
# 只寫文字),bridge 端無檔可引。能做的是把工程占位字翻成人話,別讓
# 「[Sent image attachment]」直接出現在對話泡泡(#36 善彰實測的痛點)。
_TG_SENT_MIRROR = re.compile(
    r"^\[Sent (?:(?P<n>\d+) media attachments|"
    r"(?P<kind>image|video|audio|document) attachment|voice message)\]$",
    re.M)   # 行錨定:0.19 compaction 會把多則訊息併成一列,佔位變成行中段
_TG_SENT_MIRROR_HUMAN = {
    "image": "（已在 Telegram 傳送圖片附件）",
    "video": "（已在 Telegram 傳送影片附件）",
    "audio": "（已在 Telegram 傳送音訊附件）",
    "document": "（已在 Telegram 傳送文件附件）",
    None: "（已在 Telegram 傳送語音訊息）",
}
# 新 hermes(0.19 本地 patch/mirror-media-path)鏡射列帶路徑,逐檔一行:
#   [Sent image attachment: /path/to/file.jpg]
#   [Sent voice message: /path/to/voice.ogg]
# 這才是 #36 缺口的正解:路徑在,鏡射的媒體就是一等附件(封存回退同
# `_tg_file_ok`);檔案兩邊都不在 → 人話占位帶檔名。舊佔位(無路徑)
# 照上面 _TG_SENT_MIRROR 人話化,新舊 hermes 都不壞。
_TG_SENT_MIRROR_PATH = re.compile(
    r"^\[Sent (?:(?P<kind>image|video|audio|document) attachment|voice message): "
    r"(?P<path>[^\]\n]+)\]$",
    re.M)   # 行錨定理由同上;路徑允許含空白(image_cache 檔名會有)


def _tg_extract_attachments(content: str):
    """Split a TG-side message into (display_text, attachments).

    Attachments use the SAME shape the app already renders for app-sent turns
    ({kind, filename, mime, path} — see att_meta in POST /app/v1/messages);
    the app fetches `path` through the existing GET /file endpoint, so no new
    media endpoint and no app-side decoding change is needed. Markers whose
    file has since been pruned from image_cache become a short human-readable
    note (帶檔名) — the raw path marker is engineering language the app must
    not show. Covers both directions of the TG side:使用者傳入(image hint /
    0.19 vision 描述塊 / document・audio・video saved-at 提示)與人格傳出
    (assistant 列的 MEDIA:<path> 遞送標記);send_message 跨 session 鏡射
    帶路徑的新標記([Sent … attachment: <path>])解析回一等附件,舊佔位
    (無路徑可引)翻成人話文字。"""
    attachments: list = []

    def _tg_file_ok(path: str) -> bool:
        """原檔在就直接用;被清掉(image_cache 週期修剪)但 media artifacts
        已封存快照 → 引用仍有效:GET /file 對缺檔路徑會走 resolve_original
        從封存供檔,app 端照常渲染。兩者皆無才退占位。"""
        if not path:
            return False
        if os.path.isfile(path):
            return True
        try:
            return _media_store().resolve_original(path) is not None
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_tg_extract_attachments._tg_file_ok", _exc, expected=True)
            return False

    def _img_att(path: str) -> str:
        """圖片路徑 → 附件(回空字串);檔案不在 → 人話占位帶檔名。"""
        if _tg_file_ok(path):
            attachments.append({
                "kind": "image",
                "filename": os.path.basename(path),
                "mime": mimetypes.guess_type(path)[0] or "image/jpeg",
                "path": path,
            })
            return ""
        name = os.path.basename(path or "")
        return f"（附件圖片『{name}』已失效）" if name else "（附件圖片已失效）"

    def _repl(m):
        return _img_att(m.group(1).strip())

    def _repl_vision(m):
        # 0.19 vision 描述塊:描述本身是給 agent 的,不是使用者說的話 —
        # 拿掉整塊,只留圖(檔案不在則留人話占位,至少知道有張圖)。
        return _img_att(m.group(1).strip())

    def _repl_replied(m):
        kind, name, path = m.group(1), m.group(2).strip(), m.group(3).strip()
        if _tg_file_ok(path):
            att_kind = {"image": "image", "audio": "audio", "voice": "audio"}.get(kind, "file")
            attachments.append({
                "kind": att_kind,
                "filename": name or os.path.basename(path),
                "mime": mimetypes.guess_type(path)[0]
                        or ("image/jpeg" if att_kind == "image" else "application/octet-stream"),
                "path": path,
            })
        return ""      # engineering note either way — never shown as text

    def _att_for(path: str, name: str, kind_hint: str) -> str:
        """共用落點:檔案在就掛附件(回空字串),不在就人話註記。"""
        if _tg_file_ok(path):
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            kind = "audio" if kind_hint == "audio" else "file"
            attachments.append({"kind": kind,
                                "filename": name or os.path.basename(path),
                                "mime": mime, "path": path})
            return ""
        return f"（附件『{name or os.path.basename(path or '')}』已失效）"

    def _repl_textdoc(m):
        # 內文已 inline 在下方 → 只把提示行變附件,正文保留。
        return _att_for(m.group(2).strip(), m.group(1).strip(), "file")

    def _repl_file(m):
        what, name, path = m.group(1), m.group(2).strip(), m.group(3).strip()
        hint = "audio" if what.startswith("an audio") else "file"
        return _att_for(path, name, hint)

    def _repl_media_tag(m):
        # MEDIA:<path> 遞送標記(assistant 回媒體)。引號/反引號包裹先剝掉,
        # kind 依 mime 分派:image→image、audio→audio、其餘(含 video)→file
        # (對齊 app Attachment.Kind;video 帶 video/* mime)。
        path = m.group("path").strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
            path = path[1:-1].strip()
        path = os.path.expanduser(path)
        mime = mimetypes.guess_type(path)[0] or ""
        if mime.startswith("image/"):
            return _img_att(path)
        return _att_for(path, os.path.basename(path),
                        "audio" if mime.startswith("audio/") else "file")

    text = _TG_IMAGE_MARKER.sub(_repl, content or "")
    text = _TG_VISION_IMAGE.sub(_repl_vision, text)
    text = _TG_VISION_IMAGE_FAIL.sub(_repl_vision, text)
    text = _TG_REPLIED_MEDIA.sub(_repl_replied, text)
    text = _TG_TEXTDOC_NOTE.sub(_repl_textdoc, text)
    text = _TG_FILE_NOTE.sub(_repl_file, text)
    text = _TG_MEDIA_TAG.sub(_repl_media_tag, text)

    # send_message 鏡射列「帶路徑」變體(新 hermes patch/mirror-media-path)
    # → 一等附件。kind 分派對齊 _repl_media_tag:image→image、
    # audio/voice→audio、video/document→file(mime 由副檔名帶);檔案與
    # 封存皆無 → 人話占位帶檔名(_img_att/_att_for 內建)。
    def _repl_mirror_path(m):
        kind = m.group("kind")          # None → voice message
        path = os.path.expanduser(m.group("path").strip())
        if kind == "image":
            return _img_att(path)
        hint = "audio" if kind in (None, "audio") else "file"
        return _att_for(path, os.path.basename(path), hint)
    text = _TG_SENT_MIRROR_PATH.sub(_repl_mirror_path, text)

    # send_message 鏡射佔位(舊 hermes,無路徑可引)→ 人話。逐「行」錨定
    # 替換:佔位獨佔一行才算(mirror 產物的真實形狀),行中引用原字串不誤傷。
    def _repl_mirror(m):
        n = m.group("n")
        return (f"（已在 Telegram 傳送 {n} 件媒體附件）" if n
                else _TG_SENT_MIRROR_HUMAN[m.group("kind")])
    text = _TG_SENT_MIRROR.sub(_repl_mirror, text)
    if text != (content or ""):     # something was extracted or replaced
        # Collapse the blank lines the removed hint lines leave behind.
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, attachments
    return content, attachments


# ── persona message de-noising ──────────────────────────────────────────────
# Hermes (gateway/run.py, acp_adapter/server.py, tools/process_registry.py) and
# this bridge itself wrap the user's actual words in machine-facing preambles
# before storing them in state.db. The app must show ONLY what the user really
# said. Every rule below is anchored to the exact producer format found in the
# Hermes source; anything unrecognized passes through UNCHANGED (better to leak
# a wrapper than to eat a user's words).

# Block wrappers: (open marker, end-of-preamble marker). The user text is what
# follows the end marker.
_TG_TEMPORAL_OPEN = "[Internal runtime time context"          # gateway/run.py:727, acp_adapter/server.py:143
_TG_TEMPORAL_CLOSE = "[/Internal runtime time context]"
_TG_TEMPORAL_BLOCK = re.compile(
    re.escape(_TG_TEMPORAL_OPEN) + r"(?s:.*?)" + re.escape(_TG_TEMPORAL_CLOSE))
_TG_REPORT_OPEN = "【PocketAgent 近期報告上下文】"                 # bridge _report_context_for_prompt
_TG_REPORT_USER = "【使用者現在的訊息】"
_TG_REPORT_BLOCK = re.compile(
    re.escape(_TG_REPORT_OPEN) + r"(?s:.*?)" + re.escape(_TG_REPORT_USER) + r"\n?")
_TG_OBSERVED_OPEN = "[Observed Telegram group context - context only, not requests]"   # gateway/run.py:691
_TG_OBSERVED_USER = ("[Current addressed message - answer only this unless it "
                     "explicitly asks you to use the observed context]")               # gateway/run.py:692

# Inline / whole-message patterns.
_TG_VOICE_TRANSCRIPT = re.compile(                    # gateway/run.py:12812
    r'\[The user sent a voice message~\s*Here\'s what they said: "((?s:.*?))"\]')
_TG_VOICE_NOTE = re.compile(                          # path/duration + failure variants, run.py:12786-12843
    r"\[The user sent a voice message(?: but |: )(?s:[^\]]*)\]")
_TG_REPLY_QUOTE = re.compile(                         # gateway/run.py:8713, whatsapp.py:1154
    r'\[Replying to: "(?s:.*?)"\][ \t]*\n*')          # unanchored: merged rows carry it mid-text
_TG_BG_PROCESS_OPEN = "[IMPORTANT: Background process "   # tools/process_registry.py:1637/1668
_TG_TIMESTAMP_PREFIX = re.compile(                    # gateway/message_timestamps.py:85 (config-gated)
    r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\]]{1,12}\]\s*")
# Header lines left behind once a block wrapper is stripped, and the image
# placeholder Hermes stores in place of raw pixels (run_agent.py:1632).
_TG_NOISE_LINES = {"[User message]", "[User message and attachments follow]",
                   "[screenshot]"}


def _tg_clean_content(text: str):
    """Strip machine-facing wrappers from a TG-side USER message.

    Returns the user's actual words, or None when the whole row is internal
    (pure runtime injection with no user content). Unrecognized formats are
    returned unchanged — this function may under-clean, never over-delete."""
    if not text:
        return None
    out = text
    # Temporal-context blocks: removed WHEREVER they sit — normally a prefix,
    # but queued/merged turns leave them mid-text and some rows carry them as
    # a suffix after the user's words. An open marker without its close is an
    # unknown format → left untouched.
    out = _TG_TEMPORAL_BLOCK.sub("\n\n", out)
    # Bridge report-context injections: each block runs from its exact header
    # to the 【使用者現在的訊息】 marker; merged multi-turn rows carry SEVERAL
    # such blocks with real user text between them, so remove every pair and
    # keep all the text segments. A trailing header with no marker after it
    # (older append-style prompt) is cut to end-of-text.
    out = _TG_REPORT_BLOCK.sub("\n\n", out)
    i = out.find(_TG_REPORT_OPEN)
    if i != -1 and _TG_REPORT_USER not in out[i:]:
        out = out[:i]
    # Observed-group-context preamble (prefix-anchored, per producer).
    s = out.lstrip()
    if s.startswith(_TG_OBSERVED_OPEN):
        i = s.find(_TG_OBSERVED_USER)
        if i != -1:
            out = s[i + len(_TG_OBSERVED_USER):]
    s = out.lstrip()
    # Whole-row internal notification (tool → agent, zero user content).
    if s.startswith(_TG_BG_PROCESS_OPEN):
        return None
    # Voice: keep the transcript (that IS what the user said), drop the frame;
    # untranscribable-voice notes are agent-facing → noise.
    out = _TG_VOICE_TRANSCRIPT.sub(r"\1", out)
    out = _TG_VOICE_NOTE.sub("", out)
    # Reply-quote preamble (the quoted snippet is the OTHER side's text).
    out = _TG_REPLY_QUOTE.sub("", out.lstrip())
    # Optional per-message timestamp prefix (config-gated, default off).
    out = _TG_TIMESTAMP_PREFIX.sub("", out)
    # Line-level scrub of leftover header/placeholder lines.
    lines = [ln for ln in out.splitlines() if ln.strip() not in _TG_NOISE_LINES]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out or None


def _persona_history(home: str, limit: int = 100):
    """Full recent transcript of the persona's canonical Telegram session, so a
    fresh app install / new device can render the conversation instead of a
    blank thread. Returns oldest→newest [{role, content, ts, attachments}]."""
    import sqlite3
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.execute(
                "SELECT m.role, m.content, m.timestamp FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE s.source='telegram' AND m.role IN ('user','assistant') "
                "AND m.content IS NOT NULL AND m.content != '' "
                "ORDER BY m.timestamp DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            con.close()
            rows.reverse()  # oldest → newest for natural top-to-bottom rendering
            out = []
            for r in rows:
                text, atts = _tg_extract_attachments(r[1])
                if r[0] == "user":
                    # 前台只呈現使用者真正說的話:剝掉 runtime context 等機器面
                    # 包裹;整條都是內部注入(剝完全空)且無附件 → 不出現。
                    text = _tg_clean_content(text)
                    if text is None and not atts:
                        continue
                out.append({"role": r[0], "content": text or "", "ts": r[2],
                            "attachments": atts})
            return out
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        # A broken state.db read renders the persona thread empty on every
        # device; that deserves a log line, not silence (issue #7).
        _log_event("persona_history_read_failed", home=home,
                   error=type(e).__name__, error_message=str(e)[:160])
        return []


@app.get("/sessions")
async def list_sessions(request: Request):
    """Unified conversation list: personas (pinned) + dispatched sub-sessions."""
    _check_auth(request)
    out = []
    for mid, (disp, home) in PERSONAS.items():
        text, ts, sender, in_text, in_ts = _persona_preview_merged(mid, home)
        out.append({"id": mid, "type": "persona", "name": disp,
                    "preview": text, "lastAt": ts, "status": "idle",
                    # who sent `preview` (persona|user) + the last inbound line,
                    # so the app's bell/unread + notification subtitle stay role-
                    # aware instead of echoing the user's own last message.
                    "sender": sender, "inboundPreview": in_text, "inboundAt": in_ts})
    out.extend(await _delegation_app_sessions())
    for key, s in SUBSESSIONS.items():
        out.append({"id": key, "type": "subprocess", "name": s.get("name"),
                    "parent": s.get("parent"), "tool": s.get("tool"),
                    "preview": s.get("preview"), "lastAt": s.get("lastAt"),
                    "status": s.get("status", "running")})
    return {"sessions": out}


@app.get("/sessions/{persona}/messages")
async def persona_messages(persona: str, request: Request, limit: int = 100):
    """Server-side persona history (from Hermes state.db) so the app can seed a
    conversation that survives reinstall / new device, not just local storage."""
    _check_auth(request)
    if persona not in PERSONAS:
        raise HTTPException(status_code=404, detail="unknown persona")
    _, home = PERSONAS[persona]
    return {"messages": _persona_history(home, max(1, min(limit, 500)))}


# ───────────────────────── ccsess remote Claude Code sessions ──────────────
# Persistent `claude --remote-control` sessions (managed by ~/.local/bin/ccsess
# in tmux). The app reads each session's live transcript jsonl directly and can
# type into it via tmux send-keys — same live view/control as SSH-ing in.

CCSESS_CONF = os.path.expanduser(os.environ.get("CCSESS_CONF", "~/.config/ccsess/sessions.conf"))
TMUX_BIN = "/opt/homebrew/bin/tmux" if os.path.exists("/opt/homebrew/bin/tmux") else "tmux"
POCKET_CC_TMUX = os.environ.get("POCKET_CC_TMUX", "pocket-cc")
POCKET_AGENT_LANES = os.path.join(os.path.dirname(CCSESS_CONF), "pocket-agent-lanes.json")
_CC_HOOK_STATE: dict[str, dict] = {}
_CC_HOOK_TTL = 600.0

# P0 修復(2026-07-10,root cause #3 — "Escape 打錯 turn"):
# 每個 session 一個單調遞增的 turn 世代編號,由 UserPromptSubmit hook 事件遞增
# (代表一個新 turn 開始了)。_cc_interrupt_core 在自己的 3 次重試迴圈中,每次
# 送出 Escape 前後都會比對這個世代編號 —— 如果編號在等待期間變了,代表原本
# 想中斷的那個 turn 已經結束、一個新 turn 已經開始,這時再送 Escape 極可能誤
# 打進新 turn 的 Bash 工具執行期間,讓 CLI 誤判為「使用者拒絕工具呼叫」,進而
# 讓那個新 turn 掉進無限期等待使用者回覆的假死狀態(過去實測卡過 5.4 / 13.5
# 小時)。偵測到世代已變就立刻停手,不再送下一次 Escape。
_CC_TURN_GEN: dict[str, int] = {}

# Hard ceiling for any single tmux invocation. tmux normally answers in ms; a
# hung tmux server used to hang the handler (and its _BG_TASKS entry) forever.
_TMUX_TIMEOUT = 15.0


async def _tmux_run(*args, timeout: float = _TMUX_TIMEOUT):
    """Run one tmux command with a kill-on-timeout guard.
    Returns (rc, stdout_str, stderr_str); rc=124 on timeout."""
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        _log_event("tmux_timeout", args=" ".join(str(a) for a in args[:4]),
                   timeout_s=timeout)
        return 124, "", "tmux timed out"
    return (p.returncode,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace").strip())


# TTL cache for capture-pane (issue #8): the home list polls every session on
# every request — 50 sessions used to mean 50 subprocess spawns per poll.
_PANE_CACHE_TTL = 5.0
_PANE_CACHE: dict = {}   # name -> (cached_at_monotonic, pane_text)


async def _tmux_capture_cached(name: str) -> str:
    now = time.monotonic()
    hit = _PANE_CACHE.get(name)
    if hit and now - hit[0] < _PANE_CACHE_TTL:
        return hit[1]
    _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
    _PANE_CACHE[name] = (now, pane)
    return pane


def _cc_project_dir(workdir: str) -> str:
    return os.path.expanduser("~/.claude/projects/" + workdir.replace("/", "-"))


def _cc_latest_jsonl(workdir: str):
    files = glob.glob(os.path.join(_cc_project_dir(workdir), "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


# ─── CC session 身分(per-session jsonl)────────────────────────────────────
# 「session 身分 = 工作目錄」是身分混淆 bug 的根:同 workdir 的兩個 tmux session
# (如 Main 與 cc-51a85f55)全被 dir-latest jsonl 代表,誰最後寫誰就是全目錄——
# 清單/status/stream/卡片流全部混流。正解:從 tmux pane 的子行程樹找 claude 的
# cmdline,parse --resume/--session-id 的 uuid → <projects>/<slug>/<uuid>.jsonl。
# 實測本機 pgrep -P 對部分 pane 回空,所以用一次性 ps 快照(TTL 共用)。
_CC_SID_RE = re.compile(r"--(?:resume|session-id)\s+([0-9a-fA-F][0-9a-fA-F-]{7,63})")
_CC_SID_CACHE: dict = {}   # name -> (cached_at_monotonic, sid_or_None)
_CC_SID_TTL = 30.0         # claude 行程在 session 生命週期內穩定;None 也快取避免狂掃
_CC_SID_PINS: dict[str, str] = {}     # name -> hook-confirmed current sid
_CC_SID_HISTORY: dict[str, list[str]] = {}  # name -> recent known sid chain
_CC_SID_HISTORY_MAX = 8
_PS_SNAP = (0.0, {})       # (cached_at, {pid: (ppid, command)})
_PS_SNAP_TTL = 5.0


def _cc_valid_sid(sid: str | None) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]{7,63}", sid or ""))


def _cc_name_for_sid(sid: str) -> str | None:
    """用 sid 反查它屬於哪個 session name —— **完全不依賴 cwd**。

    sid 是 Claude Code 的 session 全域唯一識別;pin/cache/history 都是 hook 自己
    確認過的 name→sid 對應。只要 sid 對得上,就能權威地認出是哪個 session,
    不管它現在 cwd 在哪。

    2026-07-31 修:hook 走的 `_cc_names_for_cwd` 是拿「當前 cwd」比對 sessions.conf
    設定的 workdir。使用者在 session 內 `cd` 到別的目錄(worktree)後,cwd 飄離
    原始 workdir → 找不到候選 → hook 被 `transcript_cwd_mismatch`/`no_cwd_candidate`
    丟掉 → Stop 收不到 → bridge status 卡 busy → app 永遠顯示「思考中」。sid 反查
    補上這條路:身分明確就別讓 cwd 飄移把 hook 誤殺。

    回唯一 name;對不上、或多個撞同 sid(不該發生)→ None,交回 cwd 路徑。"""
    if not sid:
        return None
    matched = []
    for name in set(_CC_SID_PINS) | set(_CC_SID_CACHE) | set(_CC_SID_HISTORY):
        pinned = _CC_SID_PINS.get(name)
        cached = (_CC_SID_CACHE.get(name) or (0, None))[1]
        history = _CC_SID_HISTORY.get(name) or []
        if sid == pinned or sid == cached or sid in history:
            matched.append(name)
    matched = _cc_unique_names(matched)
    return matched[0] if len(matched) == 1 else None


def _cc_note_sid(name: str, sid: str | None) -> None:
    if not name or not _cc_valid_sid(sid):
        return
    hist = _CC_SID_HISTORY.setdefault(name, [])
    if sid in hist:
        hist.remove(sid)
    hist.append(str(sid))
    del hist[:-_CC_SID_HISTORY_MAX]


def _cc_cache_sid(name: str, sid: str | None, *, now: float | None = None,
                  pin: bool = False) -> None:
    now = time.monotonic() if now is None else now
    _CC_SID_CACHE[name] = (now, sid)
    _cc_note_sid(name, sid)
    if pin and _cc_valid_sid(sid):
        _CC_SID_PINS[name] = str(sid)


def _cc_write_resume_pin(name: str, sid: str) -> None:
    if not name or not _cc_valid_sid(sid):
        return
    pdir = os.path.expanduser("~/.config/ccsess/resume")
    os.makedirs(pdir, exist_ok=True)
    ptmp = os.path.join(pdir, name + ".tmp")
    with open(ptmp, "w") as f:
        f.write(sid + "\n")
    os.replace(ptmp, os.path.join(pdir, name))


def _cc_remote_control_pin_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    return os.path.expanduser(f"~/.config/ccsess/remote-control/{safe}")


def _cc_remote_debug_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    path = os.path.expanduser(f"~/.local/share/ccsess/logs/remote-control-{safe}.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _cc_write_remote_control_pin(name: str, display_name: str | None = None) -> bool:
    """Enable Claude App remote-control for a ccsess-managed tmux lane.

    Returns True when the pin changed. A running bare `claude --resume` process
    still needs a restart before the official app can see the lane.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    if not safe:
        return False
    pdir = os.path.expanduser("~/.config/ccsess/remote-control")
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, safe)
    value = (display_name or name).strip() or safe
    old = None
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read().strip()
    except FileNotFoundError:
        pass
    if old == value:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(tmp, path)
    return True


def _cc_remote_resume_argv(name: str, sid: str) -> list[str]:
    return [
        CLAUDE_BIN,
        "--resume", sid,
        "--remote-control", name,
        "--debug-file", _cc_remote_debug_path(name),
    ]


def _cc_reseed_pins_from_files() -> int:
    """啟動時把 ~/.config/ccsess/resume/<name> 的 sid 重載回 _CC_SID_PINS。

    根因(2026-07-16 cc-51a85f55 不同步案):`claude --resume <舊id>` 續聊會
    寫進**新**的 session 檔,但行程 cmdline 永遠停在啟動時的 `--resume <舊id>`。
    於是 _cc_pane_session_id 解 cmdline 拿到凍結的舊 sid,服務凍結的舊 jsonl。
    hook(UserPromptSubmit)每回合帶真正當前 session_id 覆寫 _CC_SID_PINS 並
    落地 resume-pin 檔來補這個洞——但 _CC_SID_PINS 是**記憶體態**,bridge 一
    重啟就清空,直到該 session 下次送 prompt 才重建。這中間的盲窗會讓 app 顯示
    舊內容(這次正是部署 scope-v2 重啟後、使用者剛好在盲窗內開 cc-51a85f55)。
    修法:啟動即從 pin 檔 reseed,盲窗歸零;pin 指向的 jsonl 若不存在,
    _cc_session_jsonl 仍會優雅 fallback,所以這裡只驗 sid 格式。"""
    pdir = os.path.expanduser("~/.config/ccsess/resume")
    seeded = 0
    try:
        names = os.listdir(pdir)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_reseed_pins_from_files", _exc, expected=True)
        return 0
    for name in names:
        if name.endswith(".tmp"):
            continue
        try:
            with open(os.path.join(pdir, name)) as f:
                sid = f.read().strip()
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_reseed_pins_from_files#2", _exc, expected=True)
            continue
        if _cc_valid_sid(sid):
            _CC_SID_PINS[name] = sid
            _cc_note_sid(name, sid)
            seeded += 1
    return seeded


async def _ps_snapshot():
    global _PS_SNAP
    now = time.monotonic()
    if now - _PS_SNAP[0] < _PS_SNAP_TTL:
        return _PS_SNAP[1]
    p = await asyncio.create_subprocess_exec(
        "/bin/ps", "-axo", "pid=,ppid=,command=",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 10.0)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        return _PS_SNAP[1]
    procs = {}
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])
    _PS_SNAP = (now, procs)
    return procs


async def _cc_pane_session_id(name: str):
    """tmux pane 子行程樹裡 claude 的 --resume/--session-id uuid;失敗回 None。"""
    now = time.monotonic()
    pinned = _CC_SID_PINS.get(name)
    if pinned:
        _cc_cache_sid(name, pinned, now=now)
        return pinned
    hit = _CC_SID_CACHE.get(name)
    if hit and now - hit[0] < _CC_SID_TTL:
        return hit[1]
    sid = None
    try:
        rc, out, _ = await _tmux_run("list-panes", "-t", name, "-F", "#{pane_pid}")
        pane_pid = int(out.split()[0]) if rc == 0 and out.strip() else 0
        if pane_pid:
            procs = await _ps_snapshot()
            kids: dict = {}
            for pid, (ppid, _cmd) in procs.items():
                kids.setdefault(ppid, []).append(pid)
            stack, seen = [pane_pid], set()
            while stack:                      # 走整棵子孫樹(claude 可能包在 zsh 下)
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                cmd = procs.get(pid, (0, ""))[1]
                if "claude" in cmd:
                    m = _CC_SID_RE.search(cmd)
                    if m:
                        sid = m.group(1)
                        break
                stack.extend(kids.get(pid, []))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_pane_session_id", _exc, expected=True)
        sid = None
    _cc_cache_sid(name, sid, now=now)
    return sid


async def _cc_pane_has_remote_control(name: str) -> bool:
    """Return True when the live Claude process under this pane advertises
    Claude App remote-control for the expected ccsess name."""
    try:
        rc, out, _ = await _tmux_run("list-panes", "-t", name, "-F", "#{pane_pid}")
        pane_pid = int(out.split()[0]) if rc == 0 and out.strip() else 0
        if not pane_pid:
            return False
        procs = await _ps_snapshot()
        kids: dict = {}
        for pid, (ppid, _cmd) in procs.items():
            kids.setdefault(ppid, []).append(pid)
        stack, seen = [pane_pid], set()
        remote_arg = f"--remote-control {name}"
        remote_eq = f"--remote-control={name}"
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            cmd = procs.get(pid, (0, ""))[1]
            if "claude" in cmd and (remote_arg in cmd or remote_eq in cmd):
                return True
            stack.extend(kids.get(pid, []))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_pane_has_remote_control", _exc, expected=True)
        return False
    return False


async def _cc_session_jsonl(name: str, workdir: str):
    """這個 ccsess 的專屬 jsonl:pane 行程 sid 優先,失敗才 fallback dir-latest。
    已知限制:TUI 內 /clear 或 /resume 會讓 cmdline 的 uuid 過期(行程不重啟、
    實際寫新 jsonl)——hook(/ccsessions/_hook)帶的 session_id 會即時覆寫
    _CC_SID_CACHE 來補這個洞(UserPromptSubmit 每回合都帶最新 sid,權威)。"""
    sid = await _cc_pane_session_id(name)
    if sid:
        p = os.path.join(_cc_project_dir(workdir), sid + ".jsonl")
        if os.path.exists(p):
            return p
        p = _cchist_find(sid)      # slug 正規化差異時跨 project glob
        if p:
            return p
    return _cc_latest_jsonl(workdir)   # 向下相容:找不到行程/parse 失敗/裸 claude


def _cc_conf_rows():
    try:
        with open(CCSESS_CONF) as f:
            return host_discovery.parse_conf_rows(f.read())
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_conf_rows", _exc, expected=True)
        return []


# 寫 sessions.conf 前先留一份備份(全機收編 §2.3:這個檔是使用者手寫的
# 常駐名單,bridge 動它之前一定要有可回滾的副本)。只留最近幾份免得長草。
_CC_CONF_BACKUP_KEEP = int(os.environ.get("CCSESS_CONF_BACKUP_KEEP", "5") or 5)


def _cc_conf_backup() -> str | None:
    """複製一份 `sessions.conf.bak.<epoch>`;沒有原檔就不用備份。"""
    try:
        if not os.path.exists(CCSESS_CONF):
            return None
        dst = f"{CCSESS_CONF}.bak.{int(time.time())}"
        shutil.copy2(CCSESS_CONF, dst)
        olds = sorted(glob.glob(CCSESS_CONF + ".bak.*"))
        for old in olds[:-_CC_CONF_BACKUP_KEEP] if _CC_CONF_BACKUP_KEEP > 0 else []:
            try:
                os.remove(old)
            except OSError:
                pass
        return dst
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_conf_backup", _exc, expected=True)
        return None


def _cc_conf_mutate(transform) -> None:
    """Rewrite sessions.conf under the same lock convention as ccsess.

    `transform(lines) -> lines` 是純文字轉換(host_discovery 那幾支),
    註解與行序原樣保留 —— 這個檔是 ccsess CLI 與使用者共用的。
    """
    os.makedirs(os.path.dirname(CCSESS_CONF), exist_ok=True)
    lock = CCSESS_CONF + ".lock"
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise http_err(502, "CCSESS_CONF_LOCKED",
                               "ccsess config lock timeout",
                               "sessions.conf lock timeout")
            time.sleep(0.1)
    try:
        try:
            with open(CCSESS_CONF, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []
        out = transform(lines)
        if out is None:
            return
        tmp = f"{CCSESS_CONF}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out).rstrip() + "\n")
        os.replace(tmp, CCSESS_CONF)
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _cc_conf_upsert(name: str, workdir: str, enabled: str = "1") -> None:
    """Update sessions.conf with the same lock convention as ccsess.

    Used only for explicit `--resume <sid>` sessions when ccsess' same-workdir
    guard rejects a fixed lane, and by 全機收編(§2.3). Those launches do not
    rely on `--continue`, so the original same-workdir footgun does not apply.
    """
    if not name:
        return
    _cc_conf_mutate(lambda lines: host_discovery.conf_upsert_lines(
        lines, name, workdir, enabled))


async def _cc_register_explicit_resume(name: str, workdir: str) -> None:
    try:
        await _run_ccsess("register", name, workdir)
        return
    except HTTPException as e:
        detail = str(getattr(e, "detail", ""))
        if "同目錄" not in detail and "workdir" not in detail:
            raise
        _log_event("ccsess_register_duplicate_workdir_resume_upsert",
                   session=name, cwd_hash=_short_hash(workdir))
    _cc_conf_upsert(name, workdir, "1")


# App-owned CC sessions registry. CCSESS_CONF is shared with the ccsess CLI
# (daemon sessions like "Culture Supply"/"Ops"/"FLiPER" live there too), and its
# `name|workdir|enabled` format is read by many 3-tuple callers — so instead of
# adding a 4th field we keep a SEPARATE bridge-managed list of the CC sessions
# THIS app created (via POST /ccsessions). The approval watcher only pushes for
# these, so a foreign ccsess session's TUI prompt never reaches the app's審核中心
# / push. One name per line.
APP_OWNED_CC = os.path.join(os.path.dirname(CCSESS_CONF), "app-owned.txt")
# 審核作用域 v2(2026-07-16):舊制「只掃 app 開的 session」在那批 session 死光
# 後名單清空,watcher 六天零產出 —— 聊天窗選項卡/審核中心/推播整條斷炊。
# 新制:enabled 的 ccsess 一律在作用域內,除非列進排除檔(一行一名,# 註解)。
# app-owned 仍保留(app 開的必收,即使之後改預設也不受影響)。
APPROVALS_EXCLUDE = os.path.join(os.path.dirname(CCSESS_CONF), "approvals-exclude.txt")


def _cc_app_owned_names() -> set:
    try:
        with open(APP_OWNED_CC) as f:
            return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_app_owned_names", _exc, expected=True)
        return set()


def _cc_approvals_excluded() -> set:
    try:
        with open(APPROVALS_EXCLUDE) as f:
            return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_approvals_excluded", _exc, expected=True)
        return set()


def _cc_approval_scope_names() -> set:
    """審核外送作用域 = (enabled ccsess ∪ app-owned) − 排除檔。"""
    enabled = {name for name, _wd, en in _cc_conf_rows() if en == "1"}
    return (enabled | _cc_app_owned_names()) - _cc_approvals_excluded()


def _cc_mark_app_owned(name: str) -> None:
    """Record that the app opened this CC session (idempotent append)."""
    name = (name or "").strip()
    if not name or name in _cc_app_owned_names():
        return
    try:
        os.makedirs(os.path.dirname(APP_OWNED_CC), exist_ok=True)
        with open(APP_OWNED_CC, "a") as f:
            f.write(name + "\n")
    except Exception as e:  # noqa: BLE001
        _log_event("cc_app_owned_write_failed", session=name, error=str(e)[:160])


def _norm_cc_workdir(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or "")))


def _cc_name_for_cwd(cwd: str | None):
    if not cwd:
        return None
    target = _norm_cc_workdir(cwd)
    for name, workdir, _enabled in _cc_conf_rows():
        if _norm_cc_workdir(workdir) == target:
            return name
    return None


def _cc_names_for_cwd(cwd: str | None) -> list:
    """同 workdir 的所有 conf 名(撞 workdir 時 hook 需要用 session_id 消歧)。"""
    if not cwd:
        return []
    target = _norm_cc_workdir(cwd)
    return [n for n, w, _e in _cc_conf_rows() if _norm_cc_workdir(w) == target]


def _cc_fresh_hook_state(name: str):
    state = _CC_HOOK_STATE.get(name)
    if not state:
        return None
    try:
        if time.time() - float(state.get("updated_at") or 0) <= _CC_HOOK_TTL:
            return state
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_fresh_hook_state", _exc, expected=True)
        return None
    return None


async def _tmux_alive(name: str) -> bool:
    try:
        rc, _, _ = await _tmux_run("has-session", "-t", name)
        return rc == 0
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_tmux_alive", _exc, expected=True)
        return False


_cc_tail_cache: dict = {}   # jsonl path -> (mtime, preview)
_cc_ctx_cache: dict = {}    # jsonl path -> (mtime, usage|None)

# model id 片段 → context window。Claude 5 家族與 Opus/Sonnet 4.6+ 都是 1M;
# Haiku 4.5 與 Claude 3.x 是 200K。認不出來的一律當 1M(現役機種的多數)。
_CC_CTX_WINDOWS = (
    ("haiku", 200_000),
    ("claude-3", 200_000),
    ("sonnet-4-5", 200_000),
    ("opus-4-5", 200_000),
    ("opus-4-1", 200_000),
    ("opus-4-0", 200_000),
    ("sonnet-4-0", 200_000),
)
_CC_CTX_WINDOW_DEFAULT = 1_000_000


def _cc_context_window(model: str) -> int:
    m = (model or "").lower()
    for frag, size in _CC_CTX_WINDOWS:
        if frag in m:
            return size
    return _CC_CTX_WINDOW_DEFAULT


def _cc_context_usage(jsonl: str):
    """這條 CC session 目前吃掉多少 context → {"used", "size"}(app 的儀表形狀,
    與 Codex 的 _codex_usage_map 同款)。取 transcript 最後一則帶 usage 的 assistant
    訊息:input + cache_read + cache_creation = 這回合送進去的 prompt 大小。

    為什麼不讀終端狀態列:CC 的「% context used」只在快滿時才畫出來(平常那行是
    5h/7d 用量配額,不是 context),對「提早知道該不該壓縮」剛好太晚。

    尾巴反向掃 256KB;工具輸出很肥時一則 assistant 可能落在更外面,再退一次 4MB。
    取不到 → None(app 端整條隱藏,不顯示假數字)。"""
    for window in (262_144, 4_194_304):
        try:
            size = os.path.getsize(jsonl)
            with open(jsonl, "rb") as f:
                start = 0
                if size > window:
                    f.seek(-window, os.SEEK_END)
                    start = f.tell()
                chunk = f.read().decode("utf-8", "replace")
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_context_usage", _exc, expected=True)
            return None
        lines = chunk.splitlines()
        if start > 0 and lines:
            lines = lines[1:]          # 半行開頭丟掉
        for line in reversed(lines):
            if '"usage"' not in line or not line.lstrip().startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_cc_context_usage.parse", _exc, expected=True)
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            try:
                used = (int(u.get("input_tokens") or 0)
                        + int(u.get("cache_read_input_tokens") or 0)
                        + int(u.get("cache_creation_input_tokens") or 0))
            except (TypeError, ValueError):
                continue
            if used <= 0:
                continue
            return {"used": used, "size": _cc_context_window(msg.get("model"))}
        if start == 0:
            break                      # 整個檔都掃過了,再放大也沒用
    return None


def _cc_context_usage_cached(jsonl):
    """_cc_context_usage 的 mtime 快取版(檔案沒動就零讀取,同 _cc_last_activity)。"""
    if not jsonl:
        return None
    try:
        mtime = os.path.getmtime(jsonl)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_context_usage_cached", _exc, expected=True)
        return None
    cached = _cc_ctx_cache.get(jsonl)
    if cached and cached[0] == mtime:
        return cached[1]
    usage = _cc_context_usage(jsonl)
    if len(_cc_ctx_cache) > 512:
        _cc_ctx_cache.clear()
    _cc_ctx_cache[jsonl] = (mtime, usage)
    return usage


def _cc_tail_preview(jsonl: str) -> str:
    """Transcript 尾巴 64KB 反向掃,抽最後一則 user/assistant 可讀文字。
    tool_result/系統包裹(list 無 text 塊、'<'開頭)自然跳過。"""
    try:
        size = os.path.getsize(jsonl)
        with open(jsonl, "rb") as f:
            start = 0
            if size > 65536:
                f.seek(-65536, os.SEEK_END)
                start = f.tell()
            chunk = f.read().decode("utf-8", "replace")
        lines = chunk.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        for line in reversed(lines):
            if not line.lstrip().startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_cc_tail_preview", _exc, expected=True)
                continue
            if d.get("type") not in ("user", "assistant"):
                continue
            content = (d.get("message") or {}).get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text" \
                            and (blk.get("text") or "").strip():
                        text = blk["text"]
                        break
            text = (text or "").strip()
            if text and not text.startswith("<") and not text.startswith("Caveat:"):
                return text[:160]
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_tail_preview#2", _exc, expected=True)
        pass
    return ""


def _cc_last_activity(jsonl):
    """(mtime, preview) — mtime 供 recency 排序;preview 改為真的從 transcript
    尾巴抽最後訊息(2026-07-15 前這裡永遠回空字串,app 端 SentLog 優先又讓
    列表凍結在「你上次從 app 送的那句」——終端機工作的 session 預覽永不更新)。
    以 (path, mtime) 快取,檔案沒動就零讀取。"""
    if not jsonl:
        return (0.0, "")
    try:
        mtime = os.path.getmtime(jsonl)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_last_activity", _exc, expected=True)
        return (0.0, "")
    cached = _cc_tail_cache.get(jsonl)
    if cached and cached[0] == mtime:
        return (mtime, cached[1])
    preview = _cc_tail_preview(jsonl)
    if len(_cc_tail_cache) > 512:
        _cc_tail_cache.clear()
    _cc_tail_cache[jsonl] = (mtime, preview)
    return (mtime, preview)


_cc_head_cache: dict = {}   # jsonl path -> (sessionId, title)


def _cc_session_head(jsonl):
    """(sessionId, title) for the Claude session this remote is running, so the app
    can map a Pocket remote ("Main") to its Claude-app session ("Session review…").
    sessionId = jsonl basename (free). title = first real user message, read from the
    top and stopped early. Cached BY PATH: both are stable for the life of the session
    file, so even a huge actively-appended jsonl is read at most once (never re-read
    per poll like _cchist_meta would).
    收 per-session jsonl(_cc_session_jsonl 解析),不再吃 dir-latest。"""
    if not jsonl:
        return (None, None)
    cached = _cc_head_cache.get(jsonl)
    if cached:
        return cached
    sid = os.path.basename(jsonl)[:-len(".jsonl")]
    title = ""
    try:
        with open(jsonl, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 200:            # first real user msg is near the top; bound the scan
                    break
                try:
                    d = json.loads(line)
                except Exception as _exc:  # noqa: BLE001
                    _log_exc("_cc_session_head", _exc, expected=True)
                    continue
                if d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = next((x.get("text") for x in c
                                  if isinstance(x, dict) and x.get("type") == "text"), "")
                    if isinstance(c, str):
                        t = c.strip()
                        if t and not t.startswith("<") and not t.startswith("Caveat:"):
                            title = t[:120]
                            break
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_session_head#2", _exc, expected=True)
        pass
    res = (sid, title or None)
    _cc_head_cache[jsonl] = res
    return res


async def _cc_sessions():
    out = []
    for name, workdir, enabled in _cc_conf_rows():
        if enabled != "1":
            continue
        alive = await _tmux_alive(name)
        busy = False
        awaiting = False
        if alive:
            hook_state = _cc_fresh_hook_state(name)
            # Mid-turn? Capture the pane and look for the working spinner — so the
            # home list can animate a running CC session (parity with Codex).
            try:
                pane = await _tmux_capture_cached(name)
                if hook_state:
                    busy = bool(hook_state.get("busy"))
                else:
                    busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
                # Parked on a permission / approval prompt → the home list flags it
                # ("待放行") so a session waiting on you is never invisible.
                if not busy and _cc_prompt(pane) is not None:
                    awaiting = True
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_cc_sessions", _exc, expected=True)
                busy = False
        jsonl = await _cc_session_jsonl(name, workdir)
        mtime, preview = _cc_last_activity(jsonl)
        sid, stitle = _cc_session_head(jsonl)
        # Claude 標題(桌面 App rename 改的就是這個,存在終端 pane_title)→ app 當
        # 副標題,主名仍是 ccsess 名。取不到就 None,不影響列表。
        claude_title = await _cc_pane_title(name) if alive else None
        row = {"name": name, "workdir": workdir,
               "status": "running" if alive else "down", "busy": busy,
               "awaiting": awaiting, "updatedAt": mtime, "preview": preview,
               "sessionId": sid, "sessionTitle": stitle,
               "claudeTitle": claude_title}
        # context 用量(與 Codex 同款 {used,size});取不到就不放這個 key,
        # app 端整條隱藏而不是顯示 0%。
        ctx = _cc_context_usage_cached(jsonl)
        if ctx:
            row["usage"] = ctx
        out.append(row)
    return out


# CC session 顯示副標題:Claude(桌面 App rename / CLI 自動任務摘要)寫進終端標題,
# tmux 存成 pane_title;前面帶狀態字元(✳ / braille spinner ⠂⠐ / ✓ …),剝掉取乾淨標題。
_CC_TITLE_STRIP_RE = re.compile(r"^[\s☀-➿⠀-⣿·•⏺*]+")


async def _cc_pane_title(name: str):
    """這條 CC session 的 Claude 標題(終端 pane_title,剝掉前置狀態字元)。桌面
    App 的 rename 改的就是這個。給 app 當副標題;取不到/空 → None。絕不 raise。"""
    try:
        rc, out, _ = await _tmux_run("display-message", "-t", name, "-p", "#{pane_title}")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_pane_title", _exc, expected=True)
        return None
    if rc != 0:
        return None
    t = _CC_TITLE_STRIP_RE.sub("", (out or "").strip()).strip()
    return t or None


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in (None, "text"))
    return ""


def _cc_time(ts) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M:%S")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_time", _exc, expected=True)
        return ""


def _fmt_cc_event(d: dict) -> str:
    """One transcript jsonl event → display markdown the app's TranscriptView
    already renders (tool rows, collapsible thinking/results, answer text)."""
    t = d.get("type")
    msg = d.get("message") or {}
    if t == "user":
        content = msg.get("content")
        if isinstance(content, str):
            head = content.lstrip()[:80]
            if any(tag in head for tag in ("<task-notification>", "<system-reminder>",
                                           "[Internal", "<command-name>", "<local-command",
                                           "[Your previous response")):
                return ""           # harness/system plumbing, not something 善彰 typed
            ts = _cc_time(d.get("timestamp"))
            stamp = f" _{ts}_" if ts else ""
            return f"\n\n**🧑 你:**{stamp} {content}\n\n"
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    txt = _blocks_text(b.get("content"))
                    if txt:
                        short = txt[:900]
                        more = "\n…(截斷)" if len(txt) > 900 else ""
                        parts.append(f"<details><summary>↳ 結果</summary>\n\n```\n{short}{more}\n```\n\n</details>\n")
            return "".join(parts)
        return ""
    if t == "assistant":
        content = msg.get("content")
        if not isinstance(content, list):
            return ""
        out = []
        # Stamp the reply time on assistant messages that carry visible text, so
        # the app can show when each answer came back. App-only marker (CC
        # sessions never go to Telegram); the app extracts and strips it.
        has_text = any(isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                       for b in content)
        ts = _cc_time(d.get("timestamp"))
        if has_text and ts:
            out.append(f"**🤖 助手:** _{ts}_")
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text"):
                out.append(b["text"])
            elif bt == "thinking" and b.get("thinking"):
                out.append(f"\n<details><summary>💭 思考</summary>\n\n{b['thinking']}\n\n</details>\n")
            elif bt == "tool_use":
                name = b.get("name", "tool")
                inp = b.get("input") or {}
                if name == "ExitPlanMode" and isinstance(inp, dict) and inp.get("plan"):
                    # wave 2: the plan IS the content — a 140-char one-liner
                    # (the generic cmd path below) buried it. Full markdown.
                    out.append(f"\n› 🔧 **ExitPlanMode**\n\n📋 **計畫**\n\n{inp['plan']}\n")
                    continue
                cmd = (inp.get("command") or inp.get("file_path") or inp.get("path")
                       or inp.get("pattern") or "")
                if not cmd and isinstance(inp, dict):
                    cmd = next((str(v) for v in inp.values() if isinstance(v, (str, int))), "")
                cmd = str(cmd).splitlines()[0][:TOOL_CMD_MAX] if cmd else ""
                out.append(f"\n› 🔧 **{name}**" + (f" `{cmd}`" if cmd else "") + "\n")
        return "\n".join(out)
    return ""


# wave 2: CC usage meter + full plan text, both read from the session's
# transcript jsonl tail (last 256KB — a turn's final assistant event always
# lands near EOF). mtime-keyed cache so the app's 1.2s status poll doesn't
# re-scan an unchanged file.
_CC_CONTEXT_WINDOW = 200_000
_CC_JSONL_TAIL_BYTES = 262_144
_CC_JSONL_SCAN_CACHE: dict = {}   # jsonl path -> (jsonl, mtime, usage, plan)


def _cc_jsonl_tail_events(jsonl: str):
    with open(jsonl, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - _CC_JSONL_TAIL_BYTES)
        f.seek(start)
        data = f.read().decode("utf-8", "replace")
    events = []
    lines = data.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    for line in lines:
        if not line.lstrip().startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_jsonl_tail_events", _exc, expected=True)
            continue
    return events


def _cc_scan_jsonl(jsonl):
    """→ (usage_dict_or_None, latest_plan_or_None) for the session's live jsonl.
    收 per-session jsonl(_cc_session_jsonl 解析);快取以 jsonl path 為 key,
    同 workdir 的兩個 session 不再共用同一筆(身分混淆 bug)。"""
    if not jsonl:
        return (None, None)
    try:
        mt = os.path.getmtime(jsonl)
    except OSError:
        return (None, None)
    hit = _CC_JSONL_SCAN_CACHE.get(jsonl)
    if hit and hit[0] == jsonl and hit[1] == mt:
        return (hit[2], hit[3])
    usage = plan = None
    try:
        for d in reversed(_cc_jsonl_tail_events(jsonl)):
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            if usage is None:
                u = msg.get("usage") or {}
                used = sum(int(u.get(k) or 0) for k in
                           ("input_tokens", "cache_creation_input_tokens",
                            "cache_read_input_tokens", "output_tokens"))
                if used:
                    # The jsonl doesn't state the context window. Default to
                    # 200k; a session already past that is on a long-context
                    # model (observed live: 224k used on this box) → report
                    # the 1M window so the meter never reads >100%.
                    size = 1_000_000 if used > _CC_CONTEXT_WINDOW else _CC_CONTEXT_WINDOW
                    usage = {"used": used, "size": size}
            if plan is None:
                for b in (msg.get("content") or []):
                    if (isinstance(b, dict) and b.get("type") == "tool_use"
                            and b.get("name") == "ExitPlanMode"
                            and (b.get("input") or {}).get("plan")):
                        plan = str(b["input"]["plan"])
                        break
            if usage is not None and plan is not None:
                break
    except Exception as e:  # noqa: BLE001
        _log_event("cc_jsonl_scan_failed", jsonl=os.path.basename(jsonl or ""),
                   error=type(e).__name__, error_message=str(e)[:120])
    _CC_JSONL_SCAN_CACHE[jsonl] = (jsonl, mt, usage, plan)
    return (usage, plan)


def _cc_match_question(qs, current):
    """多題 AskUserQuestion 對齊:jsonl 整組只有一個 tool_use,要三題全答完才寫
    tool_result,所以無法從 jsonl 判斷「現在問到第幾題」。唯一反映畫面現況的訊號是
    即時 pane 選單(current)。用 pane 的選項 label 比對每一題的 options,挑最吻合的
    那題。pane label 會被終端寬度截斷,故雙向 startswith 比對。比不出來(pane 還沒
    渲染或都不吻合)回 None,呼叫端落回 qs[0](維持原行為)。"""
    if not current or len(qs) <= 1:
        return None
    pane = [p for p in (str((o or {}).get("label") or "").strip()
                        for o in (current.get("options") or [])) if p]
    if not pane:
        return None
    def _score(q):
        labels = [str((op or {}).get("label") or "").strip()
                  for op in (q.get("options") or []) if op]
        return sum(1 for p in pane
                   if any(l and (l.startswith(p) or p.startswith(l)) for l in labels))
    best = max(qs, key=_score)
    return best if _score(best) > 0 else None


def _cc_pending_ask(jsonl, current=None):
    """讀 jsonl 尾巴,找「已發出但還沒被回答」的 AskUserQuestion(tool_use 無對應
    tool_result)→ 回完整結構化 ask(問題全文 + 每個選項 label+description)。
    多題時用 current(即時 pane 選單)對齊到畫面上正在問的那題,而非永遠第一題。

    這是 _cc_prompt 螢幕擷取的內容取代:終端只渲染截斷的 label(砍到終端寬/一行),
    jsonl 的 tool_use input 有全文,app 才判斷得了(否則使用者得回 Claude app 看)。
    偵測靠「tool_use 無 tool_result」比掃畫面錨點可靠(掃畫面在忙/捲動/多個 ask 連發
    時會漏)。None = 沒有 pending ask。絕不 raise。"""
    if not jsonl:
        return None
    try:
        events = _cc_jsonl_tail_events(jsonl)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_pending_ask", _exc, expected=True)
        return None
    answered = set()          # 已有 tool_result 的 tool_use_id
    for d in events:
        if d.get("type") != "user":
            continue
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        answered.add(tid)
    for d in reversed(events):
        if d.get("type") != "assistant":
            continue
        for b in ((d.get("message") or {}).get("content") or []):
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "AskUserQuestion"
                    and b.get("id") not in answered):
                qs = (b.get("input") or {}).get("questions") or []
                if not qs:
                    continue
                q0 = _cc_match_question(qs, current) or qs[0]   # 對齊畫面上那題;比不出來落回第一題
                opts = []
                for i, op in enumerate(q0.get("options") or []):
                    if not isinstance(op, dict):
                        continue
                    opts.append({"key": str(i + 1),      # 對齊 TUI 選項編號(送鍵用)
                                 "label": str(op.get("label") or "").strip(),
                                 "description": str(op.get("description") or "").strip()})
                if len(opts) < 2:
                    continue
                # 不再送 `multi`(= 原 ask 有多題)。它與 `multiselect`(= 這一題
                # 可複選)只差兩個字、語意完全不同,是現成的誤讀陷阱;而它承載的
                # 資訊 `q_total > 1` 已經表達得更清楚。
                # **q_total 在 pane 路徑上是「從 PR #89 起」才有的** —— #89 之前
                # 整個 repo 只有這條 jsonl 路徑會產出 q_total,所以本 PR 必須排在
                # #89 **之後**合併,否則 pane 路徑會變成沒有任何多題訊號。
                # 這條 jsonl 路徑本身實測從不命中(掃全部 transcript:288 次
                # AskUserQuestion、0 筆懸空 tool_use,CC 是答完才 flush),
                # app 端宣告了 `multi` 但沒有任何渲染端消費 → 兩端皆死,直接拿掉。
                return {"kind": "menu", "semantic": "question",
                        "title": str(q0.get("question") or "").strip(),
                        "header": str(q0.get("header") or "").strip() or None,
                        "options": opts,
                        "multiselect": bool(q0.get("multiSelect")),
                        "q_index": qs.index(q0), "q_total": len(qs)}
    return None


@app.get("/ccsessions")
async def cc_list(request: Request, archived: bool = False):
    _check_auth(request)
    if archived:
        # Archived = disabled (enabled != 1) in the ccsess config.
        return {"sessions": [{"name": n, "workdir": w, "status": "archived", "busy": False}
                             for n, w, e in _cc_conf_rows() if e != "1"]}
    return {"sessions": await _cc_sessions()}


@app.post("/ccsessions/{name}/rename")
async def cc_session_rename(name: str, request: Request):
    _check_auth(request)
    body = await request.json()
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name required")
    if any(ch in new_name for ch in "/|:\n\r\t"):
        raise HTTPException(status_code=400, detail="unsupported session name")
    rows = _cc_conf_rows()
    current = next((r for r in rows if r[0] == name), None)
    if not current:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    try:
        p = await asyncio.create_subprocess_exec(
            os.path.expanduser("~/.local/bin/ccsess"), "rename", name, new_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        if p.returncode != 0:
            detail = (err or out or b"rename failed").decode("utf-8", "replace")[:300]
            raise HTTPException(status_code=502, detail=detail)
        status = "running" if await _tmux_alive(new_name) else "down"
        return {"ok": True, "session": {"name": new_name, "workdir": current[1], "status": status}}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


async def _run_ccsess(*args):
    p = await asyncio.create_subprocess_exec(
        os.path.expanduser("~/.local/bin/ccsess"), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), 30)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        _log_event("ccsess_timeout", args=" ".join(str(a) for a in args[:3]))
        raise http_err(502, "TMUX_FAILED", "ccsess timed out", "ccsess timed out (30s)")
    if p.returncode != 0:
        detail = (err or out or b"ccsess failed").decode("utf-8", "replace")[:300]
        raise http_err(502, "TMUX_FAILED", "ccsess failed", detail)
    return (out or b"").decode("utf-8", "replace")


@app.post("/ccsessions/{name}/archive")
async def cc_session_archive(name: str, request: Request):
    """Archive a Claude Code session (saves scrollback, kills tmux, disables), or
    unarchive it when the body has {"archived": false} (re-enable + relaunch)."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("cc_session_archive", _exc, expected=True)
        body = {}
    if body.get("archived") is False:
        await _run_ccsess("enable", name)
        await _run_ccsess("ensure")          # relaunch the now-enabled session
        return {"ok": True, "archived": False}
    await _run_ccsess("archive", name)
    return {"ok": True, "archived": True}


_CC_COMPRESS_RUNNING: dict = {}   # name -> started_ts(防重複點;compress 要跑數分鐘)


@app.post("/ccsessions/{name}/compress")
async def cc_session_compress(name: str, request: Request):
    """使用者主動觸發 `ccsess compress <name>`(自建工具:壓接力包落盤 → 清
    context → 重啟接回)。跑數分鐘,故 fire-and-forget:立即回 started,
    進度由 session 重啟本身呈現(app 端 status 會看到斷線→回來)。"""
    _check_auth(request)
    if not any(row[0] == name for row in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    now = time.time()
    started = _CC_COMPRESS_RUNNING.get(name, 0)
    if started and now - started < 900:
        raise http_err(409, "COMPRESS_RUNNING",
                       f"{name} 壓縮已在進行({int(now - started)}s 前啟動)")
    _CC_COMPRESS_RUNNING[name] = now

    async def _run():
        try:
            p = await asyncio.create_subprocess_exec(
                os.path.expanduser("~/.local/bin/ccsess"), "compress", name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(p.communicate(), 1200)
            _log_event("cc_compress_done", session=name, rc=p.returncode,
                       tail=(out or b"").decode("utf-8", "replace")[-200:])
        except Exception as e:  # noqa: BLE001
            _log_event("cc_compress_failed", session=name,
                       error=type(e).__name__, error_message=str(e)[:160])
        finally:
            _CC_COMPRESS_RUNNING.pop(name, None)

    task = asyncio.get_running_loop().create_task(_run())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return {"ok": True, "session": name, "action": "compress", "started": True,
            "message": f"已啟動 {name} 壓縮(接力包→清 context→重啟),session 會短暫離線再回來"}


@app.post("/ccsessions/{name}/login")
async def cc_session_login(name: str, request: Request):
    """Open Claude Code's official login flow for a managed session.

    This endpoint is intentionally user initiated. It does not rotate tokens,
    switch providers, or fall back to an API key; `ccsess login` owns the tmux
    recovery needed to put `/login` into the correct Claude TUI.
    """
    _check_auth(request)
    if not any(row[0] == name for row in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    out = (await _run_ccsess("login", name)).strip()
    return {
        "ok": True,
        "session": name,
        "action": "login",
        "message": out or f"已在 {name} 開啟登入流程",
    }


def _pretrust_claude_dir(path: str):
    """Mark a directory as trusted in ~/.claude.json so Claude Code doesn't open
    a brand-new session on the "Do you trust the files in this folder?" dialog
    (which the app would surface as an endless review prompt). Read-modify-write
    preserves all existing config; atomic replace avoids torn writes."""
    cfg = os.path.expanduser("~/.claude.json")
    try:
        with open(cfg) as f:
            d = json.load(f)
    except Exception as _exc:
        _log_exc("_pretrust_claude_dir", _exc, expected=True)
        d = {}
    projs = d.setdefault("projects", {})
    proj = projs.setdefault(path, {})
    proj["hasTrustDialogAccepted"] = True
    try:
        tmp = cfg + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, cfg)
    except Exception as _exc:
        _log_exc("_pretrust_claude_dir#2", _exc, expected=True)
        pass


async def _cc_wait_ready(name: str, timeout: float = 12.0):
    """Poll until the new session's Claude TUI is actually up, so the app opens a
    live session instead of flashing offline while it boots."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await _tmux_alive(name):
            _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
            pane = pane.lower()
            if "for shortcuts" in pane or "esc to interrupt" in pane or "❯" in pane:
                return True
        await asyncio.sleep(0.6)
    return False


def _pocket_lane_bindings() -> dict:
    try:
        with open(POCKET_AGENT_LANES, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        _log_event("pocket_lane_bindings_read_failed", error=str(e)[:160])
        return {}


def _pocket_lane_note(provider: str, tmux_name: str, native_id: str, cwd: str,
                      title: str = "") -> None:
    d = _pocket_lane_bindings()
    d[provider] = {
        "tmux": tmux_name,
        "native_id": native_id,
        "cwd": cwd,
        "title": title,
        "updated_at": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(POCKET_AGENT_LANES), exist_ok=True)
        tmp = POCKET_AGENT_LANES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, POCKET_AGENT_LANES)
    except Exception as e:  # noqa: BLE001
        _log_event("pocket_lane_bindings_write_failed",
                   provider=provider, tmux=tmux_name, error=str(e)[:160])


def _pocket_existing_dir(raw: str | None, fallback: str = HOME_ROOT) -> str:
    for cand in (raw, fallback):
        if not cand:
            continue
        wd = os.path.realpath(os.path.abspath(os.path.expanduser(str(cand))))
        if os.path.isdir(wd):
            return wd
    raise http_err(409, "WORKDIR_MISSING",
                   "session workdir does not exist",
                   f"workdir missing: {raw or fallback or '(none)'}")


async def _pocket_tmux_replace(name: str, cwd: str, argv: list[str]) -> None:
    if await _tmux_alive(name):
        rc, _, err = await _tmux_run("kill-session", "-t", name)
        if rc != 0:
            raise http_err(502, "TMUX_FAILED", "tmux kill-session failed",
                           (err or "tmux kill-session failed")[:200])
    rc, _, err = await _tmux_run("new-session", "-d", "-s", name, "-c", cwd, *argv)
    if rc != 0:
        raise http_err(502, "TMUX_FAILED", "tmux new-session failed",
                       (err or "tmux new-session failed")[:200])
    # Keep the lane visible/re-attachable even after the app disconnects or the
    # agent exits, matching the manual `codex-current` recovery setup.
    await _tmux_run("set-option", "-t", name, "remain-on-exit", "on")
    await _tmux_run("set-option", "-t", name, "destroy-unattached", "off")
    _PANE_CACHE.pop(name, None)


async def _pocket_selected_cc(body: dict) -> tuple[str, str, str, str]:
    sid = str(body.get("session_id") or body.get("sessionId")
              or body.get("sid") or "").strip()
    cwd = str(body.get("cwd") or body.get("workdir") or "").strip()
    source_name = str(body.get("name") or body.get("session_name")
                      or body.get("sessionName") or "").strip()
    title = str(body.get("sessionTitle") or body.get("claudeTitle")
                or body.get("title") or source_name or "").strip()

    if source_name:
        row = next((r for r in _cc_conf_rows() if r[0] == source_name), None)
        if row:
            cwd = cwd or row[1]
            jsonl = await _cc_session_jsonl(source_name, row[1])
            head_sid, head_title = _cc_session_head(jsonl)
            if not _cc_valid_sid(sid):
                sid = head_sid or sid
            title = title or head_title or source_name

    if not _cc_valid_sid(sid):
        raise http_err(409, "SESSION_ID_MISSING",
                       "Claude session id is required to bind the fixed lane",
                       "this CC row has no resolved Claude session id yet")

    if not cwd:
        path = _cchist_find(sid)
        meta = _cchist_meta(path) if path else None
        cwd = (meta or {}).get("cwd") or ""
        title = title or (meta or {}).get("title") or ""
    cwd = _pocket_existing_dir(cwd, "")
    return sid, cwd, title, source_name


async def _pocket_bind_cc_source(name: str, sid: str, cwd: str,
                                 title: str) -> dict:
    """Bind Pocket to an existing ccsess without replacing its remote control.

    A live Claude App session is already the single owner of its transcript.
    Pocket controls that same tmux pane; cloning the sid into `pocket-cc` would
    archive the original remote-control card and create two transcript writers.
    """
    _cc_write_remote_control_pin(name)
    running = await _tmux_alive(name)
    status = "running"
    if running:
        current_sid = await _cc_pane_session_id(name)
        if current_sid and current_sid != sid:
            raise http_err(409, "SOURCE_SESSION_CHANGED",
                           "Claude session changed; refresh and reconnect",
                           f"{name} now points at a different session id")
        if not await _cc_pane_has_remote_control(name):
            _CC_HOOK_STATE.pop(name, None)
            _CC_SID_CACHE.pop(name, None)
            _CC_SID_PINS.pop(name, None)
            await _pocket_tmux_replace(name, cwd, _cc_remote_resume_argv(name, sid))
            ready = await _cc_wait_ready(name)
            status = "running" if ready else "starting"
    else:
        _CC_HOOK_STATE.pop(name, None)
        _CC_SID_CACHE.pop(name, None)
        _CC_SID_PINS.pop(name, None)
        await _pocket_tmux_replace(name, cwd, _cc_remote_resume_argv(name, sid))
        ready = await _cc_wait_ready(name)
        status = "running" if ready else "starting"

    await _cc_register_explicit_resume(name, cwd)
    _cc_write_resume_pin(name, sid)
    _cc_cache_sid(name, sid, pin=True)
    _cc_mark_app_owned(name)
    _pocket_lane_note("claude_code", name, sid, cwd, title)
    _log_event("pocket_cc_source_bound", tmux=name,
               native_hash=_short_hash(sid), cwd_hash=_short_hash(cwd),
               reused=running)
    return {"name": name, "workdir": cwd, "status": status,
            "sessionId": sid, "sessionTitle": title or None}


async def _pocket_activate_cc_lane(body: dict) -> dict:
    sid, cwd, title, source_name = await _pocket_selected_cc(body)
    if source_name and source_name != POCKET_CC_TMUX:
        return await _pocket_bind_cc_source(source_name, sid, cwd, title)

    lane = POCKET_CC_TMUX
    _cc_write_remote_control_pin(lane)
    if await _tmux_alive(lane):
        current_sid = await _cc_pane_session_id(lane)
        if current_sid == sid:
            status = "running"
            if not await _cc_pane_has_remote_control(lane):
                _CC_HOOK_STATE.pop(lane, None)
                _CC_SID_CACHE.pop(lane, None)
                _CC_SID_PINS.pop(lane, None)
                await _pocket_tmux_replace(lane, cwd, _cc_remote_resume_argv(lane, sid))
                ready = await _cc_wait_ready(lane)
                status = "running" if ready else "starting"
            await _cc_register_explicit_resume(lane, cwd)
            _cc_write_resume_pin(lane, sid)
            _cc_cache_sid(lane, sid, pin=True)
            _cc_mark_app_owned(lane)
            _pocket_lane_note("claude_code", lane, sid, cwd, title)
            return {"name": lane, "workdir": cwd, "status": status,
                    "sessionId": sid, "sessionTitle": title or None}

    _CC_HOOK_STATE.pop(lane, None)
    _CC_SID_CACHE.pop(lane, None)
    _CC_SID_PINS.pop(lane, None)
    await _pocket_tmux_replace(lane, cwd, _cc_remote_resume_argv(lane, sid))
    await _cc_register_explicit_resume(lane, cwd)
    _cc_write_resume_pin(lane, sid)
    _cc_cache_sid(lane, sid, pin=True)
    _cc_mark_app_owned(lane)
    ready = await _cc_wait_ready(lane)
    _pocket_lane_note("claude_code", lane, sid, cwd, title)

    _log_event("pocket_lane_activate", provider="claude_code",
               tmux=lane, native_hash=_short_hash(sid), cwd_hash=_short_hash(cwd))
    return {"name": lane, "workdir": cwd,
            "status": "running" if ready else "starting",
            "sessionId": sid, "sessionTitle": title or None}


async def _pocket_activate_cx_lane(body: dict) -> dict:
    thread_id = str(body.get("thread_id") or body.get("threadId")
                    or body.get("id") or "").strip()
    if not thread_id:
        raise http_err(400, "THREAD_ID_REQUIRED", "thread_id required")
    cwd = _pocket_existing_dir(body.get("cwd") or body.get("workdir"), HOME_ROOT)
    title = str(body.get("name") or body.get("title") or thread_id[:12] or "codex").strip()
    # Pocket already controls Codex through the app-server endpoints. Starting
    # `codex resume <thread_id>` in another tmux would make that CLI and the
    # official app-server compete for the same thread. Record only the logical
    # binding; leaving Pocket then has no process or archive side effect.
    _pocket_lane_note("codex", "", thread_id, cwd, title)
    _log_event("pocket_lane_activate", provider="codex",
               control="app_server", native_hash=_short_hash(thread_id),
               cwd_hash=_short_hash(cwd))
    return {"thread_id": thread_id, "session_id": None, "name": title, "workdir": cwd,
            "preview": body.get("preview") or "", "status": body.get("status") or "idle",
            "source": "codex-app-server", "updatedAt": body.get("updatedAt"),
            "activeTurn": bool(body.get("activeTurn", False))}


@app.post("/app/v1/agent-lanes/{provider}/activate")
async def app_agent_lane_activate(provider: str, request: Request):
    """Bind Pocket's provider page to a native session.

    Claude Code reuses an existing named tmux in place so the Claude App remote
    control remains alive. A fixed `pocket-cc` fallback is created only for a
    history sid with no live/source session name. Codex keeps the existing
    app-server control path without spawning a competing CLI process.
    """
    _check_auth(request)
    body = await _json_body(request)
    p = (provider or "").lower().replace("-", "_")
    if p in ("cc", "claude", "claude_code"):
        session = await _pocket_activate_cc_lane(body)
        return {"ok": True, "provider": "claude_code", "tmux": session["name"],
                "session": session}
    if p in ("cx", "codex"):
        session = await _pocket_activate_cx_lane(body)
        return {"ok": True, "provider": "codex", "tmux": None,
                "session": session}
    raise http_err(404, "PROVIDER_NOT_FOUND", "unknown agent lane provider")


@app.post("/ccsessions")
async def cc_session_create(request: Request):
    """Create + start a new Claude Code session."""
    _check_auth(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    workdir = (body.get("workdir") or body.get("cwd") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if any(ch in name for ch in "/|:\n\r\t"):
        raise HTTPException(status_code=400, detail="unsupported session name")
    # Resolve + create the workdir. Without this, a non-existent path makes ccsess
    # silently fall back to $HOME and (because $HOME has history) launch with
    # --continue — hijacking the home conversation. So: require a real dir under
    # home, create it, and pre-trust it so there's no startup review prompt.
    home = os.path.realpath(os.path.expanduser("~"))
    wd = os.path.realpath(os.path.expanduser(workdir)) if workdir else os.path.join(home, "apps", re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "session")
    if not (wd == home or wd.startswith(home + os.sep)):
        raise HTTPException(status_code=400, detail="workdir must be under home")
    if wd == home:
        raise HTTPException(status_code=400, detail="pick a sub-folder, not your home directory")
    try:
        os.makedirs(wd, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"cannot create workdir: {e}")
    _pretrust_claude_dir(wd)
    # P0 派工分級(2026-07-10):model 參數對應 ccsess 的 per-session model
    # pin(`ccsess model <name> <model>`),讓企劃/大局思考類任務可指定旗艦
    # 模型、機械性任務指定輕量模型,不必全域切換 delegation.model。
    # spawn config(設計 §2.1):派互動 cc session 時套的 launch 配置。
    # config.model 覆寫舊的 body.model;其餘 flags 落 ccsess pin(companion
    # 端 honor)。api_key 落 0600 secret 檔。舊 app 不帶 config → 完全照舊。
    try:
        spawn_cfg = _spawn_config_validate(body.get("config"), "cc")
    except SpawnConfigError as e:
        raise HTTPException(status_code=400, detail=e.detail)
    cc_model = (spawn_cfg.get("model") or body.get("model") or "").strip()
    if cc_model:
        spawn_cfg = {**spawn_cfg, "model": cc_model}
    # 戶政(藍圖 §3.1):配額前檢在真正 spawn 之前——超額就地 429,不留孤兒。
    reg_parent, reg_cls, reg_purpose = _registry_spawn_fields(body, default_cls="task")
    _registry_precheck_or_429(reg_parent, reg_cls)
    _cc_write_remote_control_pin(name)
    redacted = _cc_write_spawn_pins(name, spawn_cfg) if spawn_cfg else {}
    if redacted:
        _log_event("cc_spawn_config", session=name, **redacted)
    new_args = ["new", name, wd] + ([cc_model] if cc_model else [])
    await _run_ccsess(*new_args)
    _cc_mark_app_owned(name)   # 這條是 app 開的 → 只有它的審核會進 app(見 _cc_approval_watcher)
    ready = await _cc_wait_ready(name)
    _registry_register(f"claude_code:{name}", provider="claude_code", name=name,
                       purpose=reg_purpose, cls=reg_cls, parent=reg_parent)
    return {"ok": True, "session": {"name": name, "workdir": wd,
                                    "status": "running" if ready else "starting",
                                    "model": cc_model or None,
                                    "spawn_config": _spawn_config_public(spawn_cfg)}}


@app.put("/app/v1/owned-cc-sessions")
async def cc_set_app_owned(request: Request):
    """App 宣告「我 SSH 列表裡的 CC session」——覆寫 app-owned.txt 為權威清單。
    _cc_approval_watcher 只推這些 session 的審核(hermes 另計),別處的 ccsess
    (Culture Supply/FLiPER…)不外漏。app 端於 load 時用 sshStore 的 CC 記錄呼叫,
    所以「審核中心 = 這台 app 的 SSH 清單 ⊕ hermes」恆等對齊(含之前接進來的 Ops)。"""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("cc_set_app_owned", _exc, expected=True)
        body = {}
    names = body.get("names") or body.get("sessions") or []
    clean = []
    seen = set()
    for n in names if isinstance(names, list) else []:
        s = str(n or "").strip()
        # 容錯:傳 "claude_code:Ops" 也接受,取冒號後段當 ccsess 名。
        if s.startswith("claude_code:"):
            s = s.split(":", 1)[1]
        if s and s not in seen:
            seen.add(s); clean.append(s)
    try:
        os.makedirs(os.path.dirname(APP_OWNED_CC), exist_ok=True)
        with open(APP_OWNED_CC, "w") as f:
            f.write("".join(x + "\n" for x in clean))
    except Exception as e:  # noqa: BLE001
        raise http_err(500, "WRITE_FAILED", f"could not write app-owned list: {e}")
    _log_event("cc_app_owned_set", count=len(clean))
    return {"ok": True, "count": len(clean), "names": clean}


@app.get("/ccsessions/{name}/stream")
async def cc_session_stream(name: str, request: Request, replay: int = 80):
    """Live transcript of a ccsess session: replay the recent tail of its
    Claude Code jsonl, then follow it in real time (OpenAI-style SSE so the app
    reuses its chat stream parser)."""
    _check_auth(request)
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    workdir = row[1]
    cid = "ccsess-" + uuid.uuid4().hex[:16]

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": name, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        jsonl = await _cc_session_jsonl(name, workdir)
        pos = 0
        if jsonl and os.path.exists(jsonl):
            try:
                lines = open(jsonl, encoding="utf-8", errors="replace").read().splitlines()
            except Exception as _exc:  # noqa: BLE001
                _log_exc("cc_session_stream.gen", _exc, expected=True)
                lines = []
            # replay=0 means "follow only" (reconnect). Guard against Python's
            # lines[-0:] == lines[0:] which would replay the ENTIRE file on every
            # reconnect — that ballooned the app's buffer and made it scroll
            # forever after an idle stream drop.
            for line in (lines[-replay:] if replay > 0 else []):
                try:
                    c = _fmt_cc_event(json.loads(line))
                except Exception as _exc:  # noqa: BLE001
                    _log_exc("cc_session_stream.gen#2", _exc, expected=True)
                    continue
                if c:
                    yield chunk({"content": c})
            pos = os.path.getsize(jsonl)
        # follow
        idle = 0
        last_data = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            if time.monotonic() - last_data >= _STREAM_IDLE_CUTOFF_SECS:
                # 30min without a single data chunk → cut the stream cleanly
                # (keepalive comments don't count as data).
                _log_event("cc_stream_idle_cutoff", session=name)
                yield chunk({}, finish="stop")
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(1.0)
            cur = await _cc_session_jsonl(name, workdir)
            if cur != jsonl:                      # session rotated to a new jsonl
                jsonl, pos = cur, 0
            if jsonl and os.path.exists(jsonl):
                size = os.path.getsize(jsonl)
                if size > pos:
                    with open(jsonl, encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        new = f.read()
                        pos = f.tell()
                    for line in new.splitlines():
                        if not line.strip():
                            continue
                        try:
                            c = _fmt_cc_event(json.loads(line))
                        except Exception as _exc:  # noqa: BLE001
                            _log_exc("cc_session_stream.gen#3", _exc, expected=True)
                            continue
                        if c:
                            yield chunk({"content": c})
                            last_data = time.monotonic()
                    idle = 0
            idle += 1
            if idle >= max(1, int(SSE_KEEPALIVE_SECS)):   # quiet → keepalive comment.
                # Frequent so any HTTP/tunnel buffering flushes the last data chunk
                # promptly — an idle session shouldn't leave the transcript's tail
                # held in a buffer (looked "stuck" on entry until something poked it).
                idle = 0
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _cc_format_lines(lines):
    parts = []
    for line in lines:
        if not line.strip():
            continue
        try:
            c = _fmt_cc_event(json.loads(line))
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_format_lines", _exc, expected=True)
            continue
        if c:
            parts.append(c)
    return "".join(parts)


@app.get("/ccsessions/{name}/history")
async def cc_session_history(name: str, request: Request, offset: int = 0, limit: int = 150):
    """A page of older transcript events for scroll-back: the `limit` events that
    end `offset` events from the newest. `more` is true if older events remain."""
    _check_auth(request)
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    jsonl = await _cc_session_jsonl(name, row[1])
    if not jsonl or not os.path.exists(jsonl):
        return {"text": "", "more": False}
    try:
        lines = open(jsonl, encoding="utf-8", errors="replace").read().splitlines()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("cc_session_history", _exc, expected=True)
        return {"text": "", "more": False}
    total = len(lines)
    end = max(0, total - max(0, offset))
    start = max(0, end - max(1, min(limit, 500)))
    return {"text": _cc_format_lines(lines[start:end]), "more": start > 0}


# ---- CC history (S1, pocketagent#37): browse ALL past sessions & resume ----
# ~/.claude/projects/<slug>/<session-uuid>.jsonl is Claude Code's own store —
# one file per session, the true `cwd` recorded inside (no lossy slug
# reversal). We surface them read-only, and resume by spawning
# `claude --resume <id>` in a fresh tmux session registered in CCSESS_CONF —
# it then IS a normal live ccsession, so the app's existing live view / send /
# interrupt / status all apply unchanged.

CC_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_cchist_meta_cache: dict = {}   # path -> (mtime, meta); title needs a file read


def _cchist_meta(path: str):
    try:
        mtime = os.path.getmtime(path)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cchist_meta", _exc, expected=True)
        return None
    cached = _cchist_meta_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    sid = os.path.basename(path)[:-len(".jsonl")]
    title, cwd, events = "", "", 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                events += 1
                if title and cwd:
                    continue
                try:
                    d = json.loads(line)
                except Exception as _exc:  # noqa: BLE001
                    _log_exc("_cchist_meta#2", _exc, expected=True)
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if not title and d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = next((x.get("text") for x in c
                                  if isinstance(x, dict) and x.get("type") == "text"), "")
                    if isinstance(c, str):
                        t = c.strip()
                        # skip harness noise (<local-command…>, Caveat banners)
                        if t and not t.startswith("<") and not t.startswith("Caveat:"):
                            title = t[:120]
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cchist_meta#3", _exc, expected=True)
        return None
    meta = {"id": sid, "title": title or "(無標題)", "cwd": cwd,
            "project": os.path.basename(os.path.dirname(path)),
            "last_at": mtime, "events": events}
    _cchist_meta_cache[path] = (mtime, meta)
    return meta


def _cchist_find(sid: str):
    """jsonl path for a session id — id is validated so it can't traverse."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid or ""):
        return None
    hits = glob.glob(os.path.join(CC_PROJECTS_DIR, "*", sid + ".jsonl"))
    return hits[0] if hits else None


@app.get("/cchistory")
async def cc_history_list(request: Request, limit: int = 50, offset: int = 0, q: str = ""):
    """All past Claude Code sessions across every project, newest first.
    `q` filters on title/project. Metas are cached by (path, mtime)."""
    _check_auth(request)
    files = glob.glob(os.path.join(CC_PROJECTS_DIR, "*", "*.jsonl"))

    def _mt(p):
        try:
            return os.path.getmtime(p)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("cc_history_list._mt", _exc, expected=True)
            return 0.0
    files.sort(key=_mt, reverse=True)
    needle = (q or "").strip().lower()
    out = []
    for p in files:
        m = _cchist_meta(p)
        if not m:
            continue
        if needle and needle not in m["title"].lower() and needle not in m["project"].lower():
            continue
        out.append(m)
    lim = max(1, min(limit, 200))
    page = out[max(0, offset): max(0, offset) + lim]
    return {"sessions": page, "total": len(out), "more": max(0, offset) + len(page) < len(out)}


@app.get("/cchistory/{sid}/transcript")
async def cc_history_transcript(sid: str, request: Request, limit: int = 200):
    """Read-only tail of a past session, in the SAME transcript text format as
    the live view (**🧑 你:** markers) so the app renders it with zero new code."""
    _check_auth(request)
    path = _cchist_find(sid)
    if not path:
        raise HTTPException(status_code=404, detail="unknown history session")
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)[:200])
    take = lines[-max(1, min(limit, 1000)):]
    meta = _cchist_meta(path) or {}
    return {"text": _cc_format_lines(take), "more": len(lines) > len(take),
            "cwd": meta.get("cwd", ""), "title": meta.get("title", "")}


@app.post("/cchistory/{sid}/resume")
async def cc_history_resume(sid: str, request: Request):
    """Resume a past session: `claude --resume <id>` in a new tmux session at
    the session's own cwd, registered in CCSESS_CONF → it becomes a normal live
    ccsession the app can talk to immediately. Returns its name."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("cc_history_resume", _exc, expected=True)
        body = {}
    path = _cchist_find(sid)
    if not path:
        raise HTTPException(status_code=404, detail="unknown history session")
    meta = _cchist_meta(path) or {}
    cwd = meta.get("cwd") or ""
    if not cwd or not os.path.isdir(cwd):
        raise HTTPException(status_code=409, detail=f"workdir missing: {cwd or '(none)'}")
    # Unique tmux/conf name (user-suggested or cc-<id8>), never clobbering.
    base = re.sub(r"[^A-Za-z0-9_-]", "-", (body.get("name") or "").strip()) or f"cc-{sid[:8]}"
    existing = {r[0] for r in _cc_conf_rows()}
    name, i = base, 2
    while name in existing or await _tmux_alive(name):
        name, i = f"{base}-{i}", i + 1
    _cc_write_remote_control_pin(name)
    rc, _, err = await _tmux_run("new-session", "-d", "-s", name, "-c", cwd,
                                 *_cc_remote_resume_argv(name, sid))
    if rc != 0:
        raise http_err(502, "TMUX_FAILED", "tmux new-session failed",
                       (err or "tmux new-session failed")[:200])
    # conf 單一寫者:走 ccsess register(內含 conf 鎖),不再直接 append ——
    # 裸 append 會被 ccsess 端 mktemp+mv 全檔重寫蓋掉,或讓 rename 讀到半新不舊。
    await _cc_register_explicit_resume(name, cwd)
    # 精準 resume pin:這條 session 是明確 --resume <sid> 起的,直接落 pin,
    # 重開機後 ensure 走 --resume 接回同一條對話(不再靠 --continue 猜目錄)。
    try:
        pdir = os.path.expanduser("~/.config/ccsess/resume")
        os.makedirs(pdir, exist_ok=True)
        tmp = os.path.join(pdir, name + ".tmp")
        with open(tmp, "w") as f:
            f.write(sid + "\n")
        os.replace(tmp, os.path.join(pdir, name))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("cc_history_resume#2", _exc, expected=True)
        pass
    _log_event("cc_history_resume", session_id=sid, name=name, cwd=cwd)
    return {"ok": True, "name": name, "cwd": cwd}


@app.post("/ccsessions/{name}/input")
async def cc_session_input(name: str, request: Request):
    """Type a line into the live Claude Code session (tmux send-keys), exactly
    as if you SSH-attached and typed it. Sent literally, then Enter."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await request.json()
    return await _cc_input_core(name, body)


async def _cc_input_core(name: str, body: dict) -> dict:
    """cc 輸入核心 — /ccsessions/{name}/input 與 v2 統一路由 input 共用。
    附件轉存＋語音轉寫＋tmux bracketed paste。"""
    _registry_call_safe("touch", f"claude_code:{name}")   # 戶政:活動記帳
    text = (body.get("text") or body.get("content") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    _att_guard(body.get("attachments"))   # 修復單「附件限制」:直送口件數閥
    # Relay layer (like the persona attachment path): persist any attachments and
    # inject their on-disk paths into the typed line. Claude Code can Read files
    # (and sees images natively), so a bare path is enough — no vision pre-pass.
    # Audio attachments are transcribed (voice message → typed command).
    saved = []
    att_meta = []
    voice_lines = []
    for a in (body.get("attachments") or []):
        path = _save_attachment(a, a.get("filename") or "file")
        if not path:
            continue
        if a.get("kind") == "audio":
            t = await asyncio.to_thread(_transcribe, path)
            if t:
                voice_lines.append(t)
        else:
            saved.append(path)
            # 附件 metadata 帶上真實 on-disk 路徑(比照人格 _persona_prepare_turn),
            # input.accepted 卡片才有可載來源 → app 走 /file?path= 顯示圖片,不再
            # 「來源已失效」。原本傳 _input_attachment_summary 會把 path 剝光(keep-list
            # 只有 kind/filename/mime),CC 卡片因此拿不到來源。
            meta = {"path": path, "available": True}
            for k in ("kind", "filename", "mime"):
                if a.get(k) not in (None, ""):
                    meta[k] = a.get(k)
            att_meta.append(meta)
    if voice_lines:
        text = (text + " " + " ".join(voice_lines)).strip()
    visible_text = text
    if saved:
        # SINGLE-LINE reference (no embedded newlines — a newline in send-keys/
        # paste submits the prompt early). Claude Code's Read tool handles image
        # files too, so a bare path is enough.
        refs = " ".join(saved)
        text = (text + f"  [附件已存到本機,請用 Read 讀取/檢視: {refs}]").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty")
    receipt = await _cc_paste_text(name, text)
    _cc_feed_input_accepted(name, client_id, visible_text, att_meta,
                            typed_text=text)
    # 排隊空窗回音:輸入落地「當下」就發 status 事件,不等 transcript digest。
    # session 若正忙上一輪,pane 不會立即轉 busy,舊行為是 app 一路顯示
    # 「待命」直到真正接手(可能好幾分鐘)——使用者看起來就是沒反應。
    # follower 在 queued 寬限內不以 idle 蓋掉;真 busy 一出現即交還正常路徑。
    store = _cc_card_store(name)
    store.queued_until = time.time() + _CC_QUEUED_GRACE_SECS
    store.set_status({"busy": True, "mode": None, "prompt": None,
                      "phase": "queued", "label": "已排入佇列,等待接手…"})
    # delivery 語意(2026-07-28):200 不再一律等於「已送達」。
    #   accepted — 已經確認被 CLI 收走並開跑
    #   queued   — CLI 收下但還在忙上一輪(或我們無法在預算內確認)→ app 顯示
    #              排隊態,等 transcript 回顯才轉「已送達」
    # 「沒被收下」在 _cc_paste_text 就已丟 409 CC_INPUT_NOT_ACCEPTED,走不到這。
    return {"ok": True,
            "delivery": receipt.get("delivery", "accepted"),
            "confirmed": bool(receipt.get("confirmed")),
            "enter_retries": receipt.get("attempts", 0)}


async def _tmux_run_stdin(args: list, data: bytes,
                          timeout: float = _TMUX_TIMEOUT):
    """同 _tmux_run,但把 data 餵進 stdin(load-buffer 用)。"""
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(input=data), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        _log_event("tmux_timeout", args=" ".join(str(a) for a in args[:4]),
                   timeout_s=timeout)
        return 124, "", "tmux timed out"
    return (p.returncode,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace").strip())


# ── CC 送出驗證(2026-07-28 靜默掉訊息事故)────────────────────────────────
# 現場:CLI 正在跑工具(pane 顯示 spinner + `100% context used`),Pocket 送出的
# 那句話停在 `❯` 輸入框裡沒送出,bridge 卻回了 200 —— app 顯示「已送達」,
# 訊息實際永遠不會被處理。比 UI 錯亂嚴重:使用者不知道要重送。
#
# 根因不在「沒有驗證」(2026-07-14 已經加了回讀重試),而在**驗證是 fail-open**:
#   1) `pane.rfind("❯") < 0` 直接當成功。畫面重繪、overlay 蓋住、輸入框被
#      擠出可視區時看不到提示符 —— 「看不到輸入框」是未知,不是成功。
#   2) 用「輸入框裡看不到我們的字」單一快照當成功。但 CLI 忙碌時貼上的字有
#      可能還沒被 render 出來(PTY 還沒被讀走),此時輸入框當然是空的 ——
#      這正是現場那筆「0 次 cc_paste_enter_retry 就回 200」的來源。
#      **不能用「沒看到」證明送出**,要先看到字真的出現過,再看到它離開。
#   3) 子字串比對對空白/換行敏感:Claude Code 的輸入框用 U+00A0 當提示符後
#      的間隔,長句在 80 欄會折行,兩者都會讓 probe 對不上 → 假清空。
# 另外多路徑併發送出(app 的 drainNext + 離線補送 + 使用者手動)會交錯打進
# 同一個 pane,C-u/paste/Enter 三步互相插隊 —— 這裡補 per-session 序列化鎖。
_CC_COMPOSER_MARKS = ("❯", "›")
_CC_CONTEXT_FULL_RE = re.compile(r"(?:9[5-9]|100)\s*%\s*context\s+used",
                                 re.IGNORECASE)
_CC_VERIFY_BUDGET_SECS = 8.0        # 驗證總預算(手機 POST 可接受的等待上限)
_CC_VERIFY_SETTLE_SECS = 0.8        # 送 Enter 後先讓 TUI 消化貼上再回讀
_CC_VERIFY_POLL_SECS = 0.5          # 之後的回讀間隔
_CC_VERIFY_MAX_ENTER_RETRIES = 4    # 卡住時最多補幾次 Enter
_CC_PASTE_LOCKS: dict = {}


def _cc_paste_lock(name: str) -> asyncio.Lock:
    """同一個 session 的貼上/送出全序列化 —— 併發打進同一個 pane 會讓
    C-u(清空)插到別人的 paste 與 Enter 中間,兩則都可能擱淺。"""
    lock = _CC_PASTE_LOCKS.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _CC_PASTE_LOCKS[name] = lock
    return lock


def _cc_squash(s: str) -> str:
    """比對用正規化:拿掉所有空白(含 U+00A0)與換行。
    輸入框把提示符後的間隔畫成 NBSP、長句在 80 欄折行,原字串比對會假性
    對不上;拿掉空白後「字有沒有還在框裡」才問得準。"""
    return "".join((s or "").split()).replace(" ", "")


def _cc_composer_split(pane: str):
    """把畫面切成 (提示符以上的內容, 輸入框區域)。找不到提示符時輸入框回
    **None** —— 呼叫端必須把 None 當「未知」處理,不准當成「已清空 = 已送出」。
    輸入框在下邊框收尾:框下面還有 statusline / 背景 agent 清單,整段吃進來
    會讓短訊息(1-3 字)在那些文字裡誤中,判成「還卡在框裡」。"""
    idx = max(pane.rfind(m) for m in _CC_COMPOSER_MARKS)
    if idx < 0:
        lines = pane.splitlines()
        for i in range(len(lines) - 1, max(-1, len(lines) - 13), -1):
            if lines[i].lstrip().startswith(">"):    # 舊版 layout 的提示符
                idx = sum(len(x) + 1 for x in lines[:i])
                break
        if idx < 0:
            return pane, None
    out = []
    for ln in pane[idx:].splitlines():
        s = ln.strip()
        if out and s and set(s) <= {"─", "-", "═", "━", "│"}:
            break                                    # 輸入框下邊框 → 收尾
        out.append(ln)
    return pane[:idx], "\n".join(out)


def _cc_composer_region(pane: str) -> str | None:
    return _cc_composer_split(pane)[1]


def _cc_not_accepted_reason(pane: str) -> str:
    """文字卡在輸入框時,分辨 CLI 是處在哪種「吃掉 Enter」的狀態。"""
    if _CC_CONTEXT_FULL_RE.search(pane):
        return "context_full"
    if _cc_prompt(pane):
        return "awaiting_prompt"
    return "composer_stuck"


_CC_NOT_ACCEPTED_HINT = {
    "context_full": "CC 這條線 context 已滿,先 /compact 或壓縮再送",
    "awaiting_prompt": "CC 正在等你回覆畫面上的選項,先處理再送",
    "composer_stuck": "CC 沒有收下這則訊息,請重送",
    "composer_missing": "CC 現在不在可輸入狀態(啟動中或有對話框蓋住畫面),"
                        "處理完再重送",
    "never_rendered_idle": "CC 沒有收下這則訊息(當時是待命中,收到就會立刻開跑),"
                           "請重送",
}

# idle 判死前的最後一次機會:Enter 落地到 spinner 畫出來還有幾百毫秒,
# 驗證預算剛好卡在這個空隙用完時不能就地判死。
_CC_IDLE_REGRASP_SECS = 1.5


async def _cc_verify_submitted(name: str, probe: str, gen0: int,
                               pre_pane: str = "") -> dict:
    """送 Enter 之後回讀 pane,判定貼上的文字**到底有沒有被 CLI 收走**。

    回 {"state": ..., "reason": ..., "attempts": n, "pane": 最後一次畫面}
      accepted    — 有正面證據:UserPromptSubmit hook 世代跳號 / 文字出現在
                    輸入框以外(= 已進 transcript)/ 曾經在框裡、現在不在了。
      stranded    — 補完 Enter 之後文字還卡在輸入框 → 必須回錯誤,不准回 200。
      stranded/composer_missing — 整段預算裡都看不到輸入框(啟動中的信任
                    對話框、全螢幕 overlay…)。實測真 CLI 就是這樣:字被打進
                    看不見的輸入框然後留在那裡。當失敗處理,不當佇列。
      unconfirmed — 輸入框看得到、也一直是空的,但從沒看到我們的字出現。
                    既不能證明送出、也沒有擱淺證據(CLI 可能已經收進自己的
                    佇列)→ 回「已排入佇列、尚未確認」,由 transcript 回顯
                    收尾,**不謊稱已送達**。
    """
    deadline = time.monotonic() + _CC_VERIFY_BUDGET_SECS
    seen_in_composer = False
    attempts = 0
    held_streak = 0
    pane = ""
    delay = _CC_VERIFY_SETTLE_SECS
    squashed_probe = _cc_squash(probe)
    # 重送同一句話時,舊那則的回顯還留在畫面上 —— 拿它當「這次送出了」的
    # 證據會再度變成假成功。貼上前畫面就有的字一律不計分。
    stale_echo = bool(squashed_probe) and squashed_probe in _cc_squash(pre_pane)
    while True:
        await asyncio.sleep(delay)
        delay = _CC_VERIFY_POLL_SECS
        if _CC_TURN_GEN.get(name, 0) != gen0:
            # UserPromptSubmit hook 跳號 = CLI 真的收到一則使用者輸入(權威)。
            return {"state": "accepted", "reason": "turn_started",
                    "attempts": attempts, "pane": pane}
        pane = await _cc_capture_pane_fresh(name)
        body, region = _cc_composer_split(pane)
        # ① 先看「有沒有被收下」的正面證據,再看「是不是卡住」。
        # 順序很重要:忙碌中送出時 Claude Code 會把訊息排進自己的佇列並在
        # 輸入框上方回顯,但輸入框本身有幾百毫秒的清空延遲 —— 先判「卡住」
        # 就會補 Enter,把同一則訊息**排進佇列兩次**(2026-07-28 真 CLI 實測
        # 抓到重複)。只要它已經出現在輸入框以外,就是收下了,絕不再補 Enter。
        # region is None(看不到輸入框)時不採信:分不出那段字是回顯還是被
        # overlay 蓋住的輸入框殘字。
        if region is not None and squashed_probe and not stale_echo \
                and squashed_probe in _cc_squash(body):
            return {"state": "accepted", "reason": "echoed_in_pane",
                    "attempts": attempts, "pane": pane}
        held = region is not None and squashed_probe in _cc_squash(region)
        if held:
            seen_in_composer = True
            held_streak += 1
            # ② 連續兩次都還在框裡才補 Enter:單一快照可能只是 TUI 消化貼上
            # 的延遲,一看到就補 Enter 同樣會送出兩次。
            if held_streak < 2 and time.monotonic() < deadline:
                continue
            if attempts >= _CC_VERIFY_MAX_ENTER_RETRIES or \
                    time.monotonic() >= deadline:
                return {"state": "stranded",
                        "reason": _cc_not_accepted_reason(pane),
                        "attempts": attempts, "pane": pane}
            attempts += 1
            held_streak = 0
            _log_event("cc_paste_enter_retry", session=name,
                       probe_chars=len(probe), attempt=attempts)
            await _tmux_run("send-keys", "-t", name, "Enter")
            continue
        held_streak = 0
        if seen_in_composer:
            return {"state": "accepted", "reason": "composer_cleared",
                    "attempts": attempts, "pane": pane}
        if time.monotonic() >= deadline:
            if region is None:
                # 整段預算都看不到輸入框 = TUI 不在可輸入狀態,字很可能被打進
                # 被蓋住的輸入框裡擱淺(2026-07-28 真 CLI 實測就是這樣)。
                return {"state": "stranded", "reason": "composer_missing",
                        "attempts": attempts, "pane": pane}
            return {"state": "unconfirmed", "reason": "never_rendered",
                    "attempts": attempts, "pane": pane}


async def _cc_paste_text(name: str, text: str) -> dict:
    """tmux 貼字唯一原語(B1):bracketed paste,buffer 內容改走 load-buffer
    stdin——set-buffer 把整段文字放 argv,長貼文/特殊字元踩 exec 邊界就 502,
    這一整類從此消失。每步 rc/stderr 失敗即進 log(502 先可觀測再談修)。

    送出後一律回讀 pane 驗證(見上方 _cc_verify_submitted):
      * 卡在輸入框 → 清掉殘字並丟 409 CC_INPUT_NOT_ACCEPTED,**不准回 200**
      * 確認送出   → {"delivery": "accepted"|"queued", "confirmed": True}
      * 無法確認 + pane 真的在忙 → {"delivery": "queued", "confirmed": False}
      * 無法確認 + pane 待命中   → 409(idle 不可能排隊,見下方 idle_recheck)
    """
    async with _cc_paste_lock(name):
        return await _cc_paste_text_locked(name, text)


async def _cc_paste_text_locked(name: str, text: str) -> dict:
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")

    # 貼上前先看畫面:CLI 正停在「權限審核」這類選單時,貼進去的字只會變成
    # 選單裡的垃圾輸入(而且送不出去)—— 與其之後回報擱淺,不如當場擋下並
    # 告訴使用者要先處理選單。AskUserQuestion(semantic=question)容許自由
    # 文字作答,放行。busy 中 _cc_prompt 本來就回 None,不影響正常送出。
    pre_pane = await _cc_capture_pane_fresh(name)
    pre_prompt = _cc_prompt(pre_pane)
    if pre_prompt and pre_prompt.get("semantic") != "question":
        _log_event("cc_input_not_accepted", session=name, reason="awaiting_prompt",
                   stage="pre_check", text_chars=len(text))
        raise http_err(409, "CC_INPUT_NOT_ACCEPTED",
                       "session is waiting on an on-screen prompt",
                       _CC_NOT_ACCEPTED_HINT["awaiting_prompt"])

    gen0 = _CC_TURN_GEN.get(name, 0)
    buf = "pa-" + uuid.uuid4().hex[:8]
    try:
        rc_clear, _, e_clear = await _tmux_run("send-keys", "-t", name, "C-u")
        rc_load, _, e_load = await _tmux_run_stdin(
            ["load-buffer", "-b", buf, "-"],
            text.encode("utf-8", "replace"))
        rc_paste, _, e_paste = await _tmux_run("paste-buffer", "-t", name,
                                               "-b", buf, "-p", "-d")
        await asyncio.sleep(0.25)                    # let the editor settle
        rc_enter, _, e_enter = await _tmux_run("send-keys", "-t", name, "Enter")
    except Exception as e:  # noqa: BLE001
        _log_event("cc_paste_failed", session=name, text_chars=len(text),
                   step="exec", error=f"{type(e).__name__}: {str(e)[:160]}")
        raise HTTPException(status_code=500, detail=str(e))
    if rc_load or rc_paste or rc_enter:              # don't false-report success
        _log_event("cc_paste_failed", session=name, text_chars=len(text),
                   step=("load" if rc_load else "paste" if rc_paste else "enter"),
                   rc_clear=rc_clear, rc_load=rc_load, rc_paste=rc_paste,
                   rc_enter=rc_enter,
                   stderr=(e_load or e_paste or e_enter or "")[:200])
        detail = (e_load or e_paste or e_enter or "tmux paste failed")[:200]
        raise http_err(502, "TMUX_FAILED", "tmux paste failed", detail)
    if rc_clear:
        _log_event("cc_paste_clear_warn", session=name,
                   rc=rc_clear, stderr=(e_clear or "")[:120])

    probe = text[:24].strip()
    if not probe:
        return {"delivery": "accepted", "confirmed": False, "attempts": 0}
    verdict = await _cc_verify_submitted(name, probe, gen0, pre_pane)
    pane = verdict.get("pane") or ""
    if verdict["state"] == "stranded":
        # 2026-07-14 草稿擱淺 / 2026-07-28 靜默掉訊息:重試耗盡文字還在框裡。
        # 舊行為回 200 = 沉默失敗;回 200 之外還有第二個坑 —— 殘字會變成跨
        # 重開機的殭屍草稿,下一則送出時被 C-u 吃掉也可能與新字黏在一起。
        # 這裡先把「我們貼的那段」清掉(框裡確定是我們的字才清),再誠實回 409。
        await _tmux_run("send-keys", "-t", name, "C-u")
        _log_event("cc_input_not_accepted", session=name, stage="verify",
                   reason=verdict["reason"], attempts=verdict["attempts"],
                   text_chars=len(text), busy=_cc_pane_busy(pane))
        raise http_err(409, "CC_INPUT_NOT_ACCEPTED",
                       f"message not accepted by the TUI ({verdict['reason']})",
                       _CC_NOT_ACCEPTED_HINT.get(verdict["reason"],
                                                 _CC_NOT_ACCEPTED_HINT["composer_stuck"]))
    confirmed = verdict["state"] == "accepted"
    busy = _cc_pane_busy(pane)
    if not confirmed and not busy:
        # 2026-07-29 訊息被吃掉:待命中的 CLI 一收到輸入就會**立刻**開跑,所以
        # 「驗證不到 + pane 也不忙」代表這段字根本沒進 TUI ——「排隊」在 idle
        # session 上根本不成立。舊行為把它一律標成 queued,app 顯示「已排入
        # 佇列,等待接手…」,120 秒寬限過完就安靜消失:使用者看到一則永遠不會
        # 被處理的訊息。實測 07:51 那則「壓縮」在 CC transcript 裡完全不存在,
        # 當下 session 已閒置 4 分鐘。只有真 busy 才准叫排隊,idle 一律 fail-closed。
        await asyncio.sleep(_CC_IDLE_REGRASP_SECS)
        late_pane = await _cc_capture_pane_fresh(name)
        if _CC_TURN_GEN.get(name, 0) != gen0 or _cc_pane_busy(late_pane):
            busy = True                       # 只是 spinner 慢一拍,確實收下了
        else:
            await _tmux_run("send-keys", "-t", name, "C-u")
            _log_event("cc_input_not_accepted", session=name,
                       stage="idle_recheck", reason="never_rendered_idle",
                       attempts=verdict["attempts"], text_chars=len(text),
                       busy=False)
            raise http_err(409, "CC_INPUT_NOT_ACCEPTED",
                           "message never reached the TUI (session was idle)",
                           _CC_NOT_ACCEPTED_HINT["never_rendered_idle"])
    # busy 取捨:CLI 自己就有輸入佇列(忙碌中送出會排到本回合結束才處理),
    # bridge 端再疊一層持久佇列會出現兩個排序來源 → 重複送/亂序。所以這裡
    # 不排隊,只把「已被 CLI 收下、但還沒開始跑」誠實標成 queued 讓 app 顯示
    # 排隊態;真正「沒被收下」才回 409 由 app 重試。
    delivery = "queued" if (busy or not confirmed) else "accepted"
    if not confirmed:
        _log_event("cc_input_unconfirmed", session=name,
                   reason=verdict["reason"], text_chars=len(text))
    return {"delivery": delivery, "confirmed": confirmed,
            "attempts": verdict["attempts"], "reason": verdict["reason"]}


async def _cc_capture_pane_fresh(name: str) -> str:
    """Capture the tmux pane RIGHT NOW (no cache) — used where staleness would
    lie, e.g. verifying an interrupt actually landed."""
    _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
    return pane


def _cc_pane_busy(pane: str) -> bool:
    return bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())


# CC interrupt + busy status (parity with Codex's stop/active). The app uses
# these to offer a stop button and to detect a running turn reliably instead of
# guessing from stream silence (which mis-fires on long, quiet commands).
@app.post("/ccsessions/{name}/interrupt")
async def cc_session_interrupt(name: str, request: Request):
    """Send Escape to the live TUI — same as pressing Esc to interrupt — then
    VERIFY via the pane's busy spinner that the turn actually stopped, retrying
    up to 3 Escapes. Previously this blind-fired one Escape and returned ok,
    so the app's stop button could 200 six times while the turn kept running."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    return await _cc_interrupt_core(name)


async def _cc_interrupt_core(name: str) -> dict:
    """cc 中斷核心(Esc + 驗證重試 3 次)— v1 與 v2 統一路由共用。

    P0 修復(2026-07-10,root cause #3 —「Escape 打錯 turn」):
    之前這裡完全沒有機制確保 Escape 打中「當下正在跑的那個 turn」。3 次重試
    迴圈横跨最多約 2.1 秒(3 × (送鍵 + 0.7s 觀察)),如果在這段時間裡原本的
    turn 已經自然結束、且緊接著一個新 turn 開始了(UserPromptSubmit),後續的
    Escape 就可能打進新 turn 的 Bash 工具執行期間,讓 CLI 誤判成「使用者中途
    拒絕這次工具呼叫」,新 turn 因而進入無限期等待使用者回覆的假死狀態(過去
    實測卡過 5.4 小時、13.5 小時)。

    修法:兩層防護,擇一命中就不再盲送 Escape。
      1) 送出第一個 Escape 前,若 hook 回報的 busy 狀態新鮮且為 False(代表
         目前根本沒有活躍 turn),直接視為「沒有需要中斷的對象」並跳過整個
         tmux 操作 —— 這同時涵蓋「使用者快速連按兩次停止鍵」:第一次已經真的
         中斷成功並同步了 busy=False(見前次 P0 修復),第二次點擊此時應該
         被判定為無事可做,而不是再送一個 Escape 去賭運氣。
      2) 重試迴圈中,每次送出 Escape 之前、以及送出後等待驗證之前,都比對
         _CC_TURN_GEN 的世代編號是否還等於呼叫開始時記下的 gen0。世代編號由
         UserPromptSubmit hook 遞增,代表「一個新 turn 開始了」。只要世代變了
         就代表原本要中斷的 turn 已經結束、新 turn 已經開始 —— 立刻停止重試,
         不再送下一個 Escape,並在回應中標記 stale_turn=True 讓呼叫端知道這次
         interrupt 沒有(也不應該)打中任何東西。
    """
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    fresh = _cc_fresh_hook_state(name)
    if fresh is not None and fresh.get("busy") is False:
        # hook 有新鮮資料且明確說「不忙碌」→ 沒有活躍 turn 可中斷,不送 Escape。
        _log_event("cc_interrupt", session=name, interrupted=True, attempts=0,
                   reason="already_idle_per_hook")
        return {"ok": True, "interrupted": True, "attempts": 0,
                "reason": "already_idle"}
    gen0 = _CC_TURN_GEN.get(name, 0)
    attempts = 0
    interrupted = False
    stale = False
    for _ in range(3):
        if _CC_TURN_GEN.get(name, 0) != gen0:
            # 世代已變:原本要中斷的 turn 已結束、新 turn 已開始,不該再送 Escape。
            stale = True
            break
        attempts += 1
        rc, _, err = await _tmux_run("send-keys", "-t", name, "Escape")
        if rc:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                           err[:200] or "interrupt failed")
        _PANE_CACHE.pop(name, None)              # the cached pane is now stale
        await asyncio.sleep(0.7)                 # let the TUI react before checking
        if _CC_TURN_GEN.get(name, 0) != gen0:
            # 送出後才變:接下來的 pane 忙碌判斷可能量到的是新 turn 的狀態,
            # 不可信,不當作「打中原 turn」的證據,也不再送下一次 Escape。
            stale = True
            break
        pane = await _cc_capture_pane_fresh(name)
        if not _cc_pane_busy(pane):
            interrupted = True
            break
    if interrupted:
        # P0 修復(2026-07-10):interrupt 成功時 pane 已確認不忙,但 busy 的
        # 權威真相來源是 _CC_HOOK_STATE(hook 沒發 Stop 事件就不會更新),
        # 若這裡不主動同步,對外 busy 會維持 true 直到 600s TTL 到期才 fallback
        # 去看 pane —— 這正是「interrupt 回真成功但 busy 卡好幾分鐘」的根因之一。
        # 一併清掉可能殘留的 queued 提示文字,避免下次 status 誤讀成忙碌。
        _CC_HOOK_STATE[name] = {
            "busy": False,
            "updated_at": time.time(),
            "source": "interrupt",
        }
    _log_event("cc_interrupt", session=name, interrupted=interrupted,
               attempts=attempts, stale_turn=stale)
    return {"ok": True, "interrupted": interrupted, "attempts": attempts,
            "stale_turn": stale}


# Claude Code's TUI shows a working spinner like "· Fermenting… (1m 51s · ↓ 6.5k
# tokens)" while a turn runs — capture the pane and look for it. Covers long,
# silent commands (the spinner stays up), which a stream-silence heuristic misses.
_CC_BUSY_RE = re.compile(r"\((?:\d+m\s*)?\d+(?:\.\d+)?s\s*·.*tokens", re.IGNORECASE)
_CC_OPT_NUM_RE = re.compile(r"^(\d+)[.)]\s+(.{1,120})$")
_CC_OPT_LABEL_RE = re.compile(r"^(allow once|always allow|don.t allow|allow|deny|yes,|yes\b|no,|no\b)", re.IGNORECASE)
_CC_MARKER_CHARS = "❯>•· "


def _cc_is_border(s: str) -> bool:
    """整行只有框線字元(TUI 會在選單上下、甚至選項之間畫線)。"""
    return bool(s) and set(s) <= {"─", "-", "═", "—", "_", "━"}


def _cc_opt_match(raw: str):
    return _CC_OPT_NUM_RE.match(raw.strip().lstrip(_CC_MARKER_CHARS).strip())


_CC_CHECKBOX_RE = re.compile(r"^\[[ xX✔✓]?\]\s*")
_CC_CHECKED_RE = re.compile(r"^\[[xX✔✓]\]")        # 已勾選(未勾選是 "[ ]")

# ── AskUserQuestion 多選版面(multiSelect)的版面常數 ───────────────────────────
# 全部經 Claude Code 2.1.207 實機驗證(2026-08-11,獨立 tmux session)並與該版
# binary 內的 TUI 原始碼對照過:
#
#   ←  ☐ Fruits  ☐ Drinks  ☐ Desserts  ✔ Submit  →   ← 多題頁籤列(單題也有)
#   問題本文
#   ❯ 1. [ ] 選項一        數字鍵 = 切換勾選(不移動游標、不送出)
#     2. [✔] 選項二
#     4. [ ] Type something  自由輸入列(永遠是最後一列)
#   ❯    Submit            內嵌送出鈕;聚焦時 "❯"+4 空格、未聚焦時 5 空格
#   ────
#     5. Chat about this   在送出鈕之外,不屬於多選清單
#
# 送出鈕文字:停在最後一題是 "Submit",還有後續題目時是 "Next"。
_CC_SUBMIT_ROW_RE = re.compile(r"^(❯)?[ ]{4,6}(Submit|Next)[ ]*$")
_CC_CHIP_ON = "☒☑"          # 該題已有作答(figures.checkboxOn 在此機器上是 ☒)
_CC_CHIP_OFF = "☐"
_CC_CHIP_ANY = _CC_CHIP_ON + _CC_CHIP_OFF
_CC_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_CC_TAB_BAR_SCAN = 30       # 頁籤列離選單很近,不必掃整個 pane


def _cc_indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _cc_submit_row(lines: list[str], footer_i: int):
    """多選版面的內嵌送出鈕 → {"line","label","focused"};沒有回 None。

    這一列是 bridge 對外唯一的送出途徑:它不帶編號(`_cc_opt_match` 不認),
    舊版還會被當成「上一個選項的說明」黏進 description。
    """
    for i in range(max(0, footer_i - 20), footer_i):
        m = _CC_SUBMIT_ROW_RE.match(lines[i].rstrip())
        if m:
            return {"line": i, "label": m.group(2), "focused": bool(m.group(1))}
    return None


def _cc_strip_sgr(s: str) -> str:
    return _CC_SGR_RE.sub("", s)


def _cc_tab_bar(raw: str) -> dict | None:
    """頁籤列(形如 `←  ☒ Fruits  ☐ Drinks  ✔ Submit  →`)→ 題數/題號。

    - `q_total` = 題目 chip 數,永遠可靠。
    - `q_headers` = 每題的短標籤(同一個 ask 的各題之間穩定 → 拿來當 ask 簽名)。
    - `q_index` **只在能確定時才給**:純文字擷取分不出「停在第 1 題但已作答」與
      「停在第 2 題尚未作答」——兩者的 plain text 完全相同(2026-08-11 實機
      對照確認)。真正的游標是 ANSI 背景色,只有 `capture-pane -pe` 才看得到;
      呼叫端若餵帶跳脫碼的行,這裡就解得出精確題號,否則回 None(不猜)。
    """
    plain = _cc_strip_sgr(raw).strip()
    if not plain:
        return None
    head = plain.lstrip("←").lstrip()
    if not head or head[0] not in _CC_CHIP_ANY:
        return None                      # 不是頁籤列
    headers: list[str] = []
    answered: list[bool] = []
    for m in re.finditer(f"[{_CC_CHIP_ANY}] ", plain):
        rest = plain[m.end():]
        cut = re.search(f"\\s\\s+[{_CC_CHIP_ANY}✔]|\\s+→\\s*$", rest)
        headers.append((rest[:cut.start()] if cut else rest).strip())
        answered.append(plain[m.start()] in _CC_CHIP_ON)
    if not headers:
        return None
    out = {"q_total": len(headers), "q_headers": headers,
           "q_answered": answered, "q_index": None}
    # ANSI 路徑:目前這一格有背景色(48;…)→ 就是所在題。
    if "\x1b[" in raw:
        bg = False
        pos = 0
        state = {"idx": None, "chip": -1}

        def _eat(seg: str, on: bool) -> None:
            for ch in seg:
                if ch in _CC_CHIP_ANY:
                    state["chip"] += 1
                    if on and state["idx"] is None:
                        state["idx"] = state["chip"]

        for m in _CC_SGR_RE.finditer(raw):
            _eat(raw[pos:m.start()], bg)
            for p in (m.group(1) or "0").split(";"):
                if p in ("", "0", "49"):
                    bg = False                     # reset / 預設背景
                elif p == "48" or (len(p) == 2 and p[0] == "4") or p[:3] in (
                        "100", "101", "102", "103", "104", "105", "106", "107"):
                    bg = True                      # 40-47 / 48;5;N / 100-107
            pos = m.end()
        _eat(raw[pos:], bg)
        idx = state["idx"]
        if idx is not None and idx < len(headers):
            out["q_index"] = idx
    if out["q_index"] is None and len(headers) == 1:
        out["q_index"] = 0
    return out


def _cc_menu_from_footer(lines: list[str], footer_i: int):
    """從 "Enter to select" footer 往上收選單區塊 → (title, options, multiselect)。

    為什麼要這樣收:舊版固定往上掃 `lines[-28:]`,會掃進**對話正文**。2026-07-29
    實際炸開 —— 我方訊息裡有「1. …2. …3. …4. 草稿存附件 …」的編號段落,整段被
    當成選單選項,假選項還卡在真選項前面,害標題搜尋跑到更上面找不到東西,
    退回通用字串「cc-65bc73e9 等待核准」→ App 上問題整句消失、選項多出垃圾。

    真實 TUI 結構(實機擷取):
        ────────────────
         ☐ 優先序                ← header chip
        (空行)
        問題本文                  ← 非縮排普通文字
        (空行)
        ❯ 1. 選項一
             說明第一行           ← 縮排 = 上一個選項的說明
             說明第二行
          2. 選項二
        ────────────────         ← 框線也會出現在選項「之間」,不能當停止點
          5. Chat about this
        Enter to select · …

    所以界線是「第一個非縮排、非編號、非框線的普通文字」= 問題本身,停在那。

    縮排門檻是 **≥2 格**,不是舊版的 ≥4 格:AskUserQuestion 的多選版面
    (multiSelect,選項帶 [ ] checkbox)說明行只縮 2 格,舊門檻把說明行誤判成
    問題本文 → 往上掃提早停,標題變成某選項的說明、之上的選項全部丟失
    (2026-08-11 實機炸開:六個選項只剩「[ ] Type something」+「Chat about
    this」)。單選版面說明縮 5 格,兩種門檻都吃;問題本文與其折行永遠貼 0 格,
    所以 2 格是安全界線。
    """
    i = footer_i - 1
    block_start = footer_i
    while i >= 0:
        raw = lines[i]
        s = raw.strip()
        if (not s or _cc_is_border(s) or _cc_opt_match(raw)
                or _cc_indent(raw) >= 2 or _CC_SUBMIT_ROW_RE.match(raw.rstrip())):
            # 空行 / 框線 / 編號選項 / 縮排的說明行 / 內嵌送出鈕 —— 都還在選單
            # 區塊裡。送出鈕被游標選到時長成「❯    Submit」= 0 格縮排,不列進來
            # 的話往上掃會提早停,整個選單連同問題一起消失(游標一走到送出鈕,
            # App 的卡片就整張不見)。
            block_start = i
            i -= 1
            continue
        break                                  # 撞到問題本文 → 區塊到此為止

    # 問題可能折成多行:繼續往上收連續的普通文字,停在空行/框線/header chip
    # (多題時 chip 長成「←  ☐ 標籤  ✔ Submit  →」的頁籤列,以 ← 或 ☐ 開頭)。
    title_lines: list[str] = []
    while i >= 0:
        s = lines[i].strip()
        if (not s or _cc_is_border(s) or s[0] in "☐☑←" or _cc_opt_match(lines[i])):
            break
        title_lines.insert(0, s)
        i -= 1

    # 區塊內正向掃一次:編號行 = 選項,其後縮排行 = 該選項的說明。
    options: list[dict] = []
    seen_keys: set[str] = set()
    multiselect = False
    for raw in lines[block_start:footer_i]:
        m = _cc_opt_match(raw)
        if m:
            key = m.group(1)
            if key in seen_keys:               # 同號重複 → 只留第一個
                continue                       # (假選項與真選項撞號會顯示兩次)
            seen_keys.add(key)
            label = m.group(2).strip()
            checked = None
            if _CC_CHECKBOX_RE.match(label):   # 多選版面:label 帶 [ ]/[x] 勾選框
                multiselect = True             # → 剝掉殘渣,App 端才不會把
                checked = bool(_CC_CHECKED_RE.match(label))     # 目前的勾選狀態
                label = _CC_CHECKBOX_RE.sub("", label).strip()  # 「[ ] …」當標籤渲染
            opt = {"key": key, "label": label, "description": ""}
            if checked is not None:
                opt["checked"] = checked
            options.append(opt)
            continue
        s = raw.strip()
        if _CC_SUBMIT_ROW_RE.match(raw.rstrip()):
            continue                           # 內嵌送出鈕不是誰的說明(舊版會把
            # 「     Submit」黏成上一個選項的 description)
        if s and not _cc_is_border(s) and _cc_indent(raw) >= 2 and options:
            prev = options[-1]                 # 縮排續行 → 併進上一個選項的說明
            prev["description"] = (prev["description"] + " " + s).strip()
    return " ".join(title_lines)[:280], options, multiselect


def _cc_jsonl_sid(path: str | None) -> str:
    base = os.path.basename(str(path or ""))
    if not base.endswith(".jsonl"):
        return ""
    sid = base[:-len(".jsonl")]
    return sid if _cc_valid_sid(sid) else ""


def _cc_hook_transcript_path(body: dict) -> str:
    return str(body.get("transcript_path") or body.get("transcriptPath") or "").strip()


def _cc_hook_sid(body: dict) -> tuple[str, str]:
    hook_sid = str(body.get("session_id") or "").strip()
    path_sid = _cc_jsonl_sid(_cc_hook_transcript_path(body))
    if hook_sid and not _cc_valid_sid(hook_sid):
        return "", "bad_sid"
    if hook_sid and path_sid and hook_sid != path_sid:
        return "", "sid_transcript_mismatch"
    return hook_sid or path_sid, ""


def _cc_transcript_path_matches_cwd(path: str, cwd: str | None) -> bool:
    if not path or not cwd:
        return True
    try:
        real_path = os.path.realpath(os.path.expanduser(path))
        expected_dir = os.path.realpath(_cc_project_dir(_norm_cc_workdir(cwd)))
        if os.path.dirname(real_path) == expected_dir:
            return True
        meta = _cchist_meta(real_path)
        if meta and _norm_cc_workdir(meta.get("cwd") or "") == _norm_cc_workdir(cwd):
            return True
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cc_transcript_path_matches_cwd", _exc, expected=True)
        return False
    return False


def _cc_unique_names(names: list[str]) -> list[str]:
    out, seen = [], set()
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


async def _cc_busy_hook_candidates(names: list[str], *, attempts: int = 6,
                                   delay: float = 0.35) -> list[str]:
    """For same-cwd hooks, sample live panes briefly and return busy names.

    UserPromptSubmit can reach the bridge just before the TUI paints its spinner,
    so a short bounded poll is a better discriminator than a single cached pane.
    """
    for i in range(max(1, attempts)):
        busy = []
        for name in names:
            try:
                if _cc_pane_busy(await _cc_capture_pane_fresh(name)):
                    busy.append(name)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_cc_busy_hook_candidates", _exc, expected=True)
                continue
        busy = _cc_unique_names(busy)
        if busy or i == attempts - 1:
            return busy
        await asyncio.sleep(delay)
    return []


async def _cc_disambiguate_hook_name(body: dict, names: list[str],
                                     hook_sid: str) -> tuple[str | None, str]:
    names = _cc_unique_names(names)
    if not names:
        return None, "no_cwd_candidate"
    if len(names) == 1:
        return names[0], "cwd_unique"
    if hook_sid:
        matched = []
        for name in names:
            cached_sid = (_CC_SID_CACHE.get(name) or (0, None))[1]
            if (cached_sid == hook_sid or _CC_SID_PINS.get(name) == hook_sid
                    or hook_sid in (_CC_SID_HISTORY.get(name) or [])):
                matched.append(name)
        matched = _cc_unique_names(matched)
        if len(matched) == 1:
            return matched[0], "sid_history"
        if len(matched) > 1:
            return None, "sid_history_ambiguous"
    event = body.get("hook_event_name")
    if event == "Stop":
        active = [n for n in names if (_cc_fresh_hook_state(n) or {}).get("busy")]
        active = _cc_unique_names(active)
        if len(active) == 1:
            return active[0], "fresh_busy_state"
        if len(active) > 1:
            return None, "fresh_busy_state_ambiguous"
    if event == "UserPromptSubmit":
        # UserPromptSubmit 送達時 claude 行程還卡在等這個 hook 的 HTTP 回應
        # (hook 是 turn 開跑前同步執行的),TUI spinner 根本還沒畫出來——在
        # handler 裡同步輪詢 busy 永遠等不到(2026-07-15 實測:全數落
        # ambiguous_same_cwd)。改成 hook 回應後的延後輪詢,見
        # _cc_hook_deferred_disambiguate。
        return None, "needs_busy_poll"
    return None, "ambiguous_same_cwd"


def _cc_hook_commit(name: str, event: str, body: dict, hook_sid: str,
                    resolution: str) -> dict:
    """消歧成功後的統一寫入:sid 快取+pin、busy 狀態、log。"""
    if hook_sid:
        # 身分有把握 → hook sid 是權威。寫入 pin 後,即使 claude cmdline 還
        # 掛著舊 --resume uuid,下一輪 _cc_pane_session_id 也不會把 cache 洗回去。
        _cc_cache_sid(name, hook_sid, pin=True)
        try:
            _cc_write_resume_pin(name, hook_sid)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_hook_commit", _exc, expected=True)
            pass
    state = {"busy": event == "UserPromptSubmit", "updated_at": time.time(),
             "source": "hook"}
    if event == "Stop":
        state["last_assistant_message"] = body.get("last_assistant_message")
    if event == "UserPromptSubmit":
        # P0 修復(root cause #3):新 turn 開始 → 世代編號 +1,讓仍在跑的
        # _cc_interrupt_core 重試迴圈偵測「原目標 turn 已結束」,不再誤送
        # Escape 進新 turn。放在 commit 統一寫入點 → 延後消歧路徑也會 bump。
        _CC_TURN_GEN[name] = _CC_TURN_GEN.get(name, 0) + 1
    _CC_HOOK_STATE[name] = state
    _log_event("cc_hook_state",
               name=name,
               hook_event_name=event,
               busy=state["busy"],
               resolution=resolution,
               hook_sid_hash=_short_hash(hook_sid),
               cwd_hash=_short_hash(str(body.get("cwd") or "")),
               last_assistant_message_chars=len(str(body.get("last_assistant_message") or "")))
    return state


# 延後 busy 輪詢的參數:hook 回 200 後 claude 才會開跑,spinner 通常在
# ~0.5-2.5s 內出現;拉 8s 窗口涵蓋慢機器。測試會把這兩個值 patch 小。
_CC_HOOK_BUSY_POLL_ATTEMPTS = 20
_CC_HOOK_BUSY_POLL_DELAY = 0.4
_CC_HOOK_BG_TASKS: set = set()   # 防 GC;done_callback 自清


async def _cc_hook_deferred_disambiguate(body: dict, names: list[str],
                                         hook_sid: str) -> None:
    """UserPromptSubmit 的同 cwd 消歧延後版:等 hook 已回應、TUI 真正開跑
    畫出 busy 後再輪詢候選 pane;唯一 busy 者即為事主。"""
    try:
        busy = _cc_unique_names(await _cc_busy_hook_candidates(
            names, attempts=_CC_HOOK_BUSY_POLL_ATTEMPTS,
            delay=_CC_HOOK_BUSY_POLL_DELAY))
        if len(busy) == 1:
            _cc_hook_commit(busy[0], "UserPromptSubmit", body, hook_sid,
                            "pane_busy_deferred")
            return
        _log_event("cc_hook_ambiguous",
                   hook_event_name="UserPromptSubmit",
                   candidate_count=len(names),
                   reason=("pane_busy_deferred_ambiguous" if busy
                           else "pane_busy_deferred_none"),
                   hook_sid_hash=_short_hash(hook_sid),
                   cwd_hash=_short_hash(str(body.get("cwd") or "")))
    except Exception as e:  # noqa: BLE001
        _log_event("cc_hook_deferred_error", error=type(e).__name__,
                   error_message=str(e)[:160])


def _cc_prompt(pane: str):
    """Detect a Claude Code interactive choice prompt so the app can render real
    buttons. Returns {kind,title,options:[{key,label}]} or None.
    Two shapes: (1) AskUserQuestion / generic numbered menu — anchored on the
    "Enter to select" footer, labels can be ANY language (a keyword filter here
    made every Chinese question invisible to the app); (2) permission prompts —
    the original STRICT keyword path, kept as fallback for older layouts.
    Never when working."""
    low = pane.lower()
    if "esc to interrupt" in low or _CC_BUSY_RE.search(pane):
        return None
    lines = pane.splitlines()
    tail = lines[-16:]                  # the prompt always sits at the bottom
    tail_low = "\n".join(tail).lower()
    # (1) generic choice menu: the selection footer only exists while a menu is
    # live, so numbered lines above it ARE the options — no keyword gate needed.
    if "enter to select" in tail_low:
        footer_i = max(i for i, ln in enumerate(lines)
                       if "enter to select" in ln.lower())
        title, opts, multiselect = _cc_menu_from_footer(lines, footer_i)
        if len(opts) >= 2:
            # A1 semantic:泛選單(AskUserQuestion 等)= Approval Hub 的 question,
            # 不是 permission — app 端永不再用 label 猜語意。kind 欄位維持
            # "menu"(app 現行相容),語意走新增的 semantic 欄位。
            out = {"kind": "menu", "semantic": "question", "title": title,
                   "options": opts[:8], "multiselect": multiselect}
            # 送出鈕(多選版面才有)+ 頁籤列題號 —— jsonl 路徑在現行 CC 永遠不會
            # 命中(CC 是答完才 flush transcript,掃過全部 jsonl:288 次
            # AskUserQuestion 問答、0 筆懸空 tool_use),所以 q_index/q_total
            # 只能從 pane 解,否則 runtime 永遠是空的。
            sub = _cc_submit_row(lines, footer_i)
            if sub:
                out["submit_label"] = sub["label"]
                out["submit_focused"] = sub["focused"]
            for raw in reversed(lines[max(0, footer_i - _CC_TAB_BAR_SCAN):footer_i]):
                bar = _cc_tab_bar(raw)
                if bar:
                    out["q_total"] = bar["q_total"]
                    out["q_index"] = bar["q_index"]
                    out["q_headers"] = bar["q_headers"]
                    break
            return out
    has_context = any(k in tail_low for k in ("wants to", "do you want", "proceed?", "would you like"))
    if has_context:
        opts = []
        for ln in tail:
            s = ln.strip().lstrip("❯>•· ").strip()
            m = _CC_OPT_NUM_RE.match(s)
            if m and re.search(r"allow|deny|yes|no|proceed|don.t|reject|approve", s, re.IGNORECASE):
                opts.append({"key": m.group(1), "label": m.group(2).strip()})
            elif _CC_OPT_LABEL_RE.match(s):
                opts.append({"key": str(len(opts) + 1), "label": s[:50]})
        if opts:
            title = next((ln.strip()[:140] for ln in tail
                          if "wants to" in ln.lower() or "do you want" in ln.lower()), "")
            return {"kind": "menu", "semantic": "permission", "title": title,
                    "options": opts[:5]}
    if re.search(r"\(y/n\)|press y\b|y to (confirm|continue|proceed)", tail_low):
        return {"kind": "yesno", "semantic": "permission", "title": "",
                "options": [{"key": "y", "label": "是"}, {"key": "n", "label": "否"}]}
    return None


def _log_hook_dropped(reason: str, event, body: dict) -> None:
    """Hook 被丟掉就一定要留痕。

    2026-07-29 事故追查:`cc_session_hook` 的四個早退全部靜靜 return
    `{"ok": True, "ignored": True}`,curl 收 200、CC 端無感,日誌一個字也沒有。
    實測全 log 210 筆 hook POST 只有 66 筆被採用、6 筆記成 ambiguous ——
    **138 筆(66%)憑空消失**,而 `_CC_TURN_GEN` 的跳號只靠 UserPromptSubmit,
    它一死,送出驗證就只剩回讀畫面猜(那正是「訊息被吃掉」那條的幫兇)。
    當時無法從日誌分辨是哪一個閘門擋的,所以先補可觀測性。

    只記形狀不記內容:欄位名、路徑的 basename、雜湊後的 sid/cwd —— 足以指認
    是哪個閘門、對上是哪個 session,不會把使用者的 prompt 寫進 log。
    """
    path = _cc_hook_transcript_path(body) if isinstance(body, dict) else ""
    _log_event("cc_hook_dropped",
               reason=reason,
               hook_event_name=str(event) if event is not None else None,
               payload_keys=sorted(body.keys())[:20] if isinstance(body, dict) else [],
               transcript_basename=os.path.basename(path) if path else "",
               sid_hash=_short_hash(str(body.get("session_id") or "")
                                    if isinstance(body, dict) else ""),
               cwd_hash=_short_hash(str(body.get("cwd") or "")
                                    if isinstance(body, dict) else ""))


@app.post("/ccsessions/_hook")
async def cc_session_hook(request: Request):
    host = _client_host(request)
    if host not in ("127.0.0.1", "::1", "localhost"):
        _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("cc_session_hook", _exc, expected=True)
        return {"ok": True, "ignored": True}
    if not isinstance(body, dict):
        _log_hook_dropped("body_not_dict", None, {})
        return {"ok": True, "ignored": True}
    event = body.get("hook_event_name")
    if event not in ("UserPromptSubmit", "Stop"):
        _log_hook_dropped("event_not_tracked", event, body)
        return {"ok": True, "ignored": True}
    # 同 workdir 撞名時只在能唯一消歧時才把 hook 記到某個 name;拒絕
    # names[:1] 猜測,避免 Main/cc-* 同 cwd 時把 busy 與 sid 寫到錯的 session。
    hook_sid, sid_error = _cc_hook_sid(body)
    if sid_error:
        _log_hook_dropped(sid_error, event, body)
        return {"ok": True, "ignored": True, "reason": sid_error}
    # sid 是權威身分 —— 先用它直接認 session,完全不靠 cwd。使用者在 session 內
    # `cd` 到別的目錄(worktree)後 cwd 飄離原始 workdir,下面的 cwd 檢查與
    # _cc_names_for_cwd 都會失效把 hook 誤殺(2026-07-31 查到 app 卡「思考中」的
    # 根因)。sid 反查到唯一 name 就直接落定,不受 cwd 飄移影響。
    sid_name = _cc_name_for_sid(hook_sid) if hook_sid else None
    if sid_name:
        state = _cc_hook_commit(sid_name, event, body, hook_sid, "sid_pin")
        return {"ok": True, "session": sid_name, "busy": state["busy"], "source": "hook"}
    # 沒有可靠 sid 反查(首次、pin 還沒建、或舊 client)→ 退回原本的 cwd +
    # transcript 位置推斷。這條路仍保留原本防「同 cwd 撞名寫錯 session」的檢查。
    transcript_path = _cc_hook_transcript_path(body)
    if transcript_path and not _cc_transcript_path_matches_cwd(
            transcript_path, body.get("cwd")):
        _log_hook_dropped("transcript_cwd_mismatch", event, body)
        return {"ok": True, "ignored": True, "reason": "transcript_cwd_mismatch"}
    all_names = _cc_names_for_cwd(body.get("cwd"))
    name, resolution = await _cc_disambiguate_hook_name(body, all_names, hook_sid)
    if not name and resolution == "needs_busy_poll":
        # 同 cwd 多候選、快速判據都對不上 → 回應 hook 放行 claude 開跑,
        # 背景任務等 spinner 出現後用「唯一 busy pane」認人。
        task = asyncio.create_task(
            _cc_hook_deferred_disambiguate(body, list(all_names), hook_sid))
        _CC_HOOK_BG_TASKS.add(task)
        task.add_done_callback(_CC_HOOK_BG_TASKS.discard)
        return {"ok": True, "deferred": True, "reason": "busy_poll_deferred"}
    if not name:
        _log_event("cc_hook_ambiguous",
                   hook_event_name=event,
                   candidate_count=len(all_names),
                   reason=resolution,
                   hook_sid_hash=_short_hash(hook_sid),
                   cwd_hash=_short_hash(str(body.get("cwd") or "")))
        return {"ok": True, "ignored": True, "reason": resolution}
    state = _cc_hook_commit(name, event, body, hook_sid, resolution)
    return {"ok": True, "session": name, "busy": state["busy"], "source": "hook"}


# ── TG→Pocket 鏡像 ingest(XW-BRIDGE-TGMIRROR-20260714-340A)─────────────
# 四個 hermes gateway 的 pocket_mirror hook(hermes-agent home*/hooks/
# pocket_mirror/handler.py)對每則 TG 往來 POST 一筆事件到這裡:
# inbound(agent:start, role=user)= 使用者在 TG 說的話;
# outbound(agent:end, role=assistant)= 人格的 TG 回覆。
# 寫進 canonical store 後,GET /app/v1/messages 與卡片流的三來源合併
# 自然把它帶進 Pocket 人格對話,不用等 state.db watcher 的掃描週期。
#
# 冪等/防回聲雙寫(這條路和 state.db 掃描是同一則訊息的兩個來源):
# 1. mid 由事件內容決定(tgm-<sha1(session|chat|thread|anchor|role|
#    content-hash)>)→ hook 重送/重放同一事件 INSERT OR REPLACE 落同一列。
# 2. state.db 掃出的同一則訊息由合併端 10 分鐘同文壓重擋掉(_tg_dup,
#    雙 role — 見 _hp_merged_messages / GET /app/v1/messages)。
# 3. 內容先過 _tg_extract_attachments(雙 role;user 再過 _tg_clean_content)
#    —— 與 _persona_history 的 state.db 讀取路徑同一套清洗,兩條路落出
#    同一種文字+附件,同文壓重才對得上。
@app.post("/internal/v1/mirror/telegram-event")
async def tg_mirror_event(request: Request):
    host = _client_host(request)
    if host not in ("127.0.0.1", "::1", "localhost"):
        _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("tg_mirror_event", _exc, expected=True)
        body = None
    if not isinstance(body, dict):
        return {"ok": True, "ignored": True, "reason": "bad_body"}
    if (body.get("platform") or "") != "telegram":
        return {"ok": True, "ignored": True, "reason": "platform"}
    session = str(body.get("session") or "")
    role = str(body.get("role") or "")
    content = str(body.get("content") or "")
    if session not in PERSONAS:
        _log_event("tg_mirror_unknown_session", session=session)
        return {"ok": True, "ignored": True, "reason": "session"}
    if role not in ("user", "assistant"):
        return {"ok": True, "ignored": True, "reason": "role"}
    # 兩個 role 都先過媒體萃取(#36:assistant 的 MEDIA:<path> / [Sent …]
    # 鏡射佔位也要還原/翻譯)—— 與 _persona_history 的 state.db 路徑同一套,
    # 兩條路落出同一種文字+附件,合併端同文壓重才對得上(不然 canonical
    # 留原字串、state.db 掃描出人話版,同一則變兩顆氣泡)。
    content, attachments = _tg_extract_attachments(content)
    if role == "user":
        content = _tg_clean_content(content) or ""
    if not content.strip() and not attachments:
        # 整條都是 runtime 注入(剝完全空)或 gateway 送了空事件 → 不落地。
        return {"ok": True, "ignored": True, "reason": "empty"}
    try:
        ts = float(body.get("ts") or 0)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("tg_mirror_event#2", _exc, expected=True)
        ts = 0.0
    now = time.time()
    if not (now - 86400 * 366 < ts < now + 3600):
        ts = now      # 時間戳缺席/離譜(時鐘歪掉)→ 用收件時間,別排進遠古
    # anchor = gateway 的 reply-anchor message_id(inbound/outbound 同一turn
    # 共用,role 區分)。缺席時退回 10 分鐘時間桶 —— 不同回合的同文不能
    # 互相覆蓋,而 hook 重放帶原 ts,同桶仍冪等。
    chash = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()
    anchor = str(body.get("message_id") or "") or f"t{int(ts // 600)}"
    basis = "|".join((session, str(body.get("chat_id") or ""),
                      str(body.get("thread_id") or ""), anchor, role, chash))
    mid = "tgm-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]
    _, ok = _canon_add(session, role, content, attachments, mid=mid,
                       created_at=ts, push=False)
    _log_event("tg_mirror_event", session=session, role=role, stored=ok,
               mid=mid, event_type=str(body.get("event_type") or ""),
               content_chars=len(content), attachment_count=len(attachments))
    return {"ok": ok, "session": session, "id": mid, "stored": ok}


async def _cc_refine_q_index(name: str, prompt) -> None:
    """多題 ask 的題號補件 —— 只在純文字解不出來時才多花一次 capture。

    純文字的頁籤列在「停在 Q1 且已作答」與「停在 Q2 尚未作答」兩種狀態下
    完全一樣(2026-08-11 實機對照:兩次都是 `←  ☒ Fruits  ☐ Drinks  ☐ Desserts
    ✔ Submit  →`)。真正的游標是 ANSI 背景色,得用 `capture-pane -pe` 才看得到。
    單題 / 最後一題的常見情形 `_cc_tab_bar` 已經解得出來,不會走到這裡。
    """
    if not isinstance(prompt, dict):
        return
    if (prompt.get("q_total") or 0) < 2 or prompt.get("q_index") is not None:
        return
    try:
        rc, pane_e, _ = await _tmux_run("capture-pane", "-pe", "-t", name)
        if rc:
            return
        for raw in reversed(pane_e.splitlines()):
            bar = _cc_tab_bar(raw)
            if bar and bar.get("q_index") is not None:
                prompt["q_index"] = bar["q_index"]
                return
    except Exception as e:  # noqa: BLE001
        _log_event("cc_qindex_refine_error", session=name, error=str(e)[:160])


async def _cc_status_core(name: str) -> dict:
    """CC session 的 busy/mode/prompt 判讀 — /ccsessions status 端點與
    Phase 0 卡片 follower 共用同一份真相。"""
    if not await _tmux_alive(name):
        return {"busy": False, "running": False, "mode": None, "prompt": None}
    pane = await _tmux_capture_cached(name)
    hook_state = _cc_fresh_hook_state(name)
    if hook_state:
        busy = bool(hook_state.get("busy"))
    else:
        busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
    low = pane.lower()
    # S3 (wave 2): this box's Claude Code cycles FOUR states on shift+tab —
    # normal → accept edits → plan → auto mode → normal. "accept edits" and
    # "auto mode" used to both report as "auto", which made the app's mode
    # picker snap back; they are distinct now (contract: normal|acceptEdits|
    # plan|auto).
    if "plan mode on" in low:
        mode = "plan"
    elif "accept edits on" in low:
        mode = "acceptEdits"
    elif "auto mode on" in low or "bypass" in low:
        mode = "auto"
    elif busy:
        # A running turn replaces the bottom bar with "esc to interrupt" — the
        # mode marker is hidden, not absent. Claiming "normal" here made the app
        # snap the user's pick back to 一般 on the next 1.2s reconcile.
        mode = None
    else:
        mode = "normal"
    prompt = _cc_prompt(pane)
    # wave 2: usage meter + full plan text from the transcript jsonl.
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    jsonl = await _cc_session_jsonl(name, row[1]) if row else None
    # AskUserQuestion 完整內容(問題全文 + 選項 description)從 jsonl 讀,取代終端
    # 截斷的螢幕擷取。pane-scrape 偵測到 question 選單 → 換成 jsonl 全文(修「太簡略」);
    # pane 漏抓且沒在忙 → 也用 jsonl 補上(修「沒跳出來」);忙碌時不補,避免被跳脫的
    # 殘留 ask 誤觸。權限 y/n(非 tool_use)仍走 pane-scrape。
    if jsonl:
        ask = _cc_pending_ask(jsonl, current=prompt if isinstance(prompt, dict) else None)
        if ask and (
            (isinstance(prompt, dict) and prompt.get("semantic") == "question")
            or (prompt is None and not busy)
        ):
            prompt = ask
    await _cc_refine_q_index(name, prompt)
    st = {"busy": busy, "running": True, "mode": mode, "prompt": prompt}
    if jsonl:
        usage, plan = _cc_scan_jsonl(jsonl)
        if usage:
            st["usage"] = usage
        if prompt and plan and "plan" in low:
            # The live prompt is a plan approval — hand the app the COMPLETE
            # plan markdown (the pane preview is truncated by the TUI).
            prompt["plan"] = plan
    return st


@app.get("/ccsessions/{name}/status")
async def cc_session_status(name: str, request: Request):
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    st = await _cc_status_core(name)
    # 讀回這條 session 的 launch config(model/effort/permission_mode/…);
    # 舊 app 不看這欄不受影響,缺欄 = nil。api_key 只回 has_api_key 布林。
    cfg = _cc_read_spawn_config(name)
    if cfg:
        st["spawn_config"] = cfg
        if cfg.get("model") and not st.get("model"):
            st["model"] = cfg["model"]
    if not st["running"]:
        base = {"busy": False, "running": False}
        if cfg:
            base["spawn_config"] = cfg
        return base
    return st


# Send a single control key into the live TUI (arrows / Enter / Esc / Tab /
# Shift-Tab / y / n / digits) so interactive prompts, menus and plan-mode toggle
# can be driven from the phone — closing the gap vs the desktop Claude Code app.
_CC_KEYS = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "enter": "Enter", "escape": "Escape", "esc": "Escape",
    "tab": "Tab", "btab": "BTab", "shift-tab": "BTab", "space": "Space",
}


@app.post("/ccsessions/{name}/key")
async def cc_session_key(name: str, request: Request):
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await request.json()
    raw = str(body.get("key") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key required")
    return await _cc_key_core(name, raw)


# ── 同一個 tmux pane 的「多鍵送出序列」互斥 ─────────────────────────────
# `/answer` 的送出序列最壞要按十幾個鍵、花二十幾秒(toggle → 導覽 → Enter →
# 確認頁),中間完全沒有互斥。兩台裝置、卡片 vs 輸入列、`/answer` 撞 `/key`
# —— 都會把按鍵交錯打進**同一個 pane**:數字是 toggle,交錯的結果是勾錯
# 選項,最糟的情況是在確認頁按到 Cancel、或多按一次 Down 把提問整個取消。
# 搶不到就 **409**,不排隊:排隊只會讓第二個請求撐到 app 端 timeout 之後
# 再重送一次,問題更大。
_CC_SEQ_LOCKS: dict = {}


def _cc_seq_lock(name: str) -> asyncio.Lock:
    lock = _CC_SEQ_LOCKS.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _CC_SEQ_LOCKS[name] = lock
    return lock


@contextlib.asynccontextmanager
async def _cc_seq_guard(name: str, what: str = "keys"):
    """取得該 session 的按鍵序列鎖;已被佔用就 409。

    `lock.locked()` 檢查與 `acquire()` 之間沒有讓權點(未競爭的
    `Lock.acquire()` 不會 yield),所以在單執行緒事件圈上這是原子的。
    """
    lock = _cc_seq_lock(name)
    if lock.locked():
        raise http_err(409, "SESSION_BUSY_TYPING",
                       "這個 session 正在送出另一組按鍵，請稍候再試",
                       f"another key sequence is in flight (wanted: {what})")
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


async def _cc_key_core(name: str, raw: str) -> dict:
    """cc 控制鍵核心 — v1 與 v2 統一路由(key/approve)共用。"""
    async with _cc_seq_guard(name, f"key:{raw}"):
        return await _cc_key_core_locked(name, raw)


async def _cc_key_core_locked(name: str, raw: str) -> dict:
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    args = ["send-keys", "-t", name]
    mapped = _CC_KEYS.get(raw.lower())
    submit_after_key = False
    if mapped:
        args.append(mapped)                  # named control key
    elif len(raw) == 1 and raw.isprintable():
        # 選單/是否題的單字元答案(y/n/1-9)—— 送出前先拿「現在」的畫面重驗一次
        # 是不是真的還有相符的選項在等。App 端的選單清單可能是稍早抓的:如果
        # CLI 這段時間自己把提示解掉了(auto-accept、逾時、或已經被別的方式
        # 回掉),畫面上其實已經沒有選單、focus 落在自由輸入框 —— 這時候盲送
        # 字元只會變成打進聊天框的垃圾字(而且送不出去,因為沒送 Enter),使用
        # 者會看到同一顆「核准」不斷冒出來、字元越疊越多卻永遠沒有真的解掉。
        # 沒有相符選項就直接拒絕,好過默默打錯地方。
        _PANE_CACHE.pop(name, None)           # 強制拿最新畫面,不吃快取
        pane_now = await _tmux_capture_cached(name)
        prompt_now = _cc_prompt(pane_now)
        # Keep key validation in sync with /status: AskUserQuestion details may
        # only be visible in the transcript jsonl, while the pane has a trimmed
        # or transient rendering. Still avoid resurrecting stale asks mid-turn.
        low_now = pane_now.lower()
        busy_now = bool(_CC_BUSY_RE.search(pane_now)) or ("esc to interrupt" in low_now)
        row = next((r for r in _cc_conf_rows() if r[0] == name), None)
        if row and (
            (isinstance(prompt_now, dict) and prompt_now.get("semantic") == "question")
            or (prompt_now is None and not busy_now)
        ):
            jsonl = await _cc_session_jsonl(name, row[1])
            ask = _cc_pending_ask(
                jsonl, current=prompt_now if isinstance(prompt_now, dict) else None
            ) if jsonl else None
            if ask:
                prompt_now = ask
        valid_keys = {str(o.get("key") or "").lower()
                      for o in (prompt_now or {}).get("options", [])}
        if not prompt_now or raw.lower() not in valid_keys:
            raise http_err(409, "PROMPT_STALE", "no matching live prompt right now",
                           "the on-screen menu may already be resolved — refresh and retry")
        # AskUserQuestion / generic question menus need a real submit. Sending
        # only "1"/"2"/"3" leaves the digit in the TUI selection field on some
        # Claude Code layouts; permission prompts keep the old single-key path.
        submit_after_key = (prompt_now or {}).get("semantic") == "question"
        args += ["-l", raw]                  # literal single char (y / n / 1-3)
    else:
        raise HTTPException(status_code=400, detail="unsupported key")
    rc, _, err = await _tmux_run(*args)
    if rc:
        raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                       err[:200] or "send-keys failed")
    if submit_after_key:
        await asyncio.sleep(0.08)
        rc_enter, _, err_enter = await _tmux_run("send-keys", "-t", name, "Enter")
        if rc_enter:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys enter failed",
                           err_enter[:200] or "send-keys Enter failed")
    # The key just changed the TUI (mode toggle, menu pick) — a cached pane
    # would feed the app a pre-keystroke mode/prompt for up to TTL seconds.
    _PANE_CACHE.pop(name, None)
    return {"ok": True}


# ─────────── AskUserQuestion 多選作答(POST /ccsessions/{name}/answer)──────────
# 為什麼不能沿用 /key:`_cc_key_core` 對 semantic=="question" 一律「送數字 +
# 0.08s 後送 Enter」。那在**單選**版面是對的(數字本身就成交,Enter 是保險),
# 但多選版面的語意完全不同 —— 數字只是「切換游標所在清單的第 N 格勾選」,
# Enter 是「切換游標那一列」。數字+Enter 的結果會是「勾了 A 又順手切掉 B」
# 或送出空選集,而且永遠不會真的送出(送出鈕在清單之外)。
#
# 以下每一條都是 2026-08-11 在獨立 tmux session(自己開的 claude 2.1.207,
# 沒碰任何人正在用的 session)實機打過、逐格 capture-pane 對照確認的:
#   1. 數字 1-9 = 切換第 N 列的勾選;游標不動、不送出。實測送 "1"、"3" 後
#      畫面變 `[✔] Chips` / `[✔] Fruit`,游標仍在第 1 列。
#   2. 游標停在「Type something」自由輸入列時,數字**會被打進輸入框**
#      (實測畫面變成 `❯ 4. [✔] 1` —— 那個 "1" 是文字內容,不是勾選)。
#      這正是任務書說的「盲送 = 打進聊天框的垃圾字」,所以送數字前先驗游標。
#   3. Down 逐列往下走,走過最後一列(自由輸入列)再一次 Down 就聚焦到送出鈕,
#      畫面從 `     Submit`(5 空格)變成 `❯    Submit`。**再往下就會連「Chat
#      about this」一起聚焦,那時候按 Enter 會變成「找 Claude 聊聊」把整個提問
#      取消掉** —— 2026-08-11 用本端點實跑時真的踩到了(重繪還沒完成就回讀,
#      讀到上一格畫面 → 多按一次 Down → Enter 直接把 ask 取消)。所以每一次
#      按鍵之後都**等畫面真的變了**再判斷,而不是睡固定秒數就當它好了。
#   4. 送出鈕上按 Enter **不是最終送出**,是進「Review your answers /
#      Ready to submit your answers?」確認頁;在該頁按 "1"(Submit answers)
#      才真的成交(實測 CC 回 `User answered Claude's questions: · … → Chips, Fruit`)。
#   5. 多題時送出鈕文字是 "Next"(推進到下一題),只有最後一題才是 "Submit"。
#      Tab 在這個版面被外層頁籤列吃掉(會跳到下一題/Submit 頁籤),所以走 Down。
_CC_ANSWER_KEY_GAP = 0.12      # 每個切換鍵之間的間隔(實測 0.5s 綽綽有餘)
_CC_ANSWER_SETTLE = 0.06       # 回讀輪詢的間隔
_CC_ANSWER_SETTLE_TRIES = 12   # 等重繪最多輪幾次(≈0.7s;逾時就用最後一張)
_CC_ANSWER_CONFIRM_TRIES = 40  # 等確認頁收掉最多輪幾次(≈2.4s;CC 結案要一下)
_CC_ANSWER_MAX_NAV = 14        # 走到送出鈕的最多 Down 次數(選項上限 8 + 緩衝)
_CC_REVIEW_MARKERS = ("ready to submit your answers", "review your answers")

# 整條序列的**時間預算**(app 端 client timeout 必須大於它,否則 app 先放棄、
# 使用者重試 → 同一組答案被送兩次)。實測最壞情形:
#   toggle 8 次 × (0.7 等重繪 + 0.12 間隔) ≈ 6.6s
#   導覽 14 次 × 0.7                        ≈ 9.8s
#   Enter 等換頁  40 × 0.06                 ≈ 2.4s
#   確認頁「1」等收掉 40 × 0.06             ≈ 2.4s
#   tmux capture/送鍵 的固定開銷            ≈ 幾百 ms
# 合計 ≈ 21–22s;抓 28s 當硬預算,超過就中止並明確回報(不會半途留下亂鍵,
# 因為每一步都閉環驗證過)。
#   → **app 端 `StudioBridge.swift` 的 timeout 必須 ≥ 35s**(28 + 網路餘裕)。
_CC_ANSWER_BUDGET_SECS = 28.0

# 冪等:端點本身不冪等的話,「app 逾時 → 使用者重試」= 同一組答案被送兩次
# (第二次會答到下一題去)。兩條互補的認人方式:
#
# 1. **同一個 prompt + 同一組 keys**(`_CC_ANSWER_DONE`):擋雙擊、擋卡片與
#    輸入列同時送。只在「畫面上還是同一個 prompt」時有效 —— 一旦該 session
#    的 live prompt 換了簽名就整批清掉,免得下一題剛好長得一樣被誤判成重播。
# 2. **client_id**(`_CC_ANSWER_CLIENT`):擋 app 逾時後的重送 / OfflineOutbox
#    補送 —— 那時 prompt 早就換頁了,只有呼叫端自帶的冪等鍵認得出來。
#    與 `_APP_TURN_INFLIGHT` / `_CX_INPUT_INFLIGHT` 同一套慣例。
_CC_ANSWER_DONE: dict = {}          # (name, sig, keys, submit) -> {"ts","res"}
_CC_ANSWER_LAST_SIG: dict = {}      # name -> 上一次看到的 live prompt sig
_CC_ANSWER_CLIENT: dict = {}        # (name, client_id) -> {"ts","res"}
_CC_ANSWER_DONE_TTL = 120.0
_CC_ANSWER_CLIENT_TTL = 600.0
_CC_ANSWER_DONE_MAX = 256


def _cc_answer_prune(store: dict, ttl: float) -> None:
    now = time.time()
    for k in [k for k, v in store.items() if now - v["ts"] > ttl]:
        store.pop(k, None)
    while len(store) > _CC_ANSWER_DONE_MAX:
        store.pop(next(iter(store)), None)


def _cc_answer_client_get(name: str, client_id: str):
    if not client_id:
        return None
    _cc_answer_prune(_CC_ANSWER_CLIENT, _CC_ANSWER_CLIENT_TTL)
    hit = _CC_ANSWER_CLIENT.get((name, client_id))
    return dict(hit["res"], replayed=True) if hit else None


def _cc_answer_idem_key(name: str, prompt: dict, keys: list, submit: bool) -> tuple:
    """live prompt 換了就把這個 session 的舊快取全丟掉。

    簽名比 `_cc_prompt_sig`(只吃 title+options)再多帶送出鈕文字與題號 ——
    多題 ask 的兩題有可能長得一模一樣，只差在鈕上寫 Next 還是 Submit。
    """
    sig = "|".join([_cc_prompt_sig(prompt),
                    str(prompt.get("submit_label") or ""),
                    str(prompt.get("q_index") or ""),
                    str(prompt.get("q_total") or "")])
    if _CC_ANSWER_LAST_SIG.get(name) != sig:
        for k in [k for k in _CC_ANSWER_DONE if k[0] == name]:
            _CC_ANSWER_DONE.pop(k, None)
        _CC_ANSWER_LAST_SIG[name] = sig
    return (name, sig, ",".join(keys), bool(submit))


def _cc_answer_idem_get(k: tuple):
    _cc_answer_prune(_CC_ANSWER_DONE, _CC_ANSWER_DONE_TTL)
    hit = _CC_ANSWER_DONE.get(k)
    return dict(hit["res"], replayed=True) if hit else None


def _cc_answer_idem_put(k: tuple, res: dict, name: str = "",
                        client_id: str = "") -> None:
    _CC_ANSWER_DONE[k] = {"ts": time.time(), "res": dict(res)}
    _cc_answer_prune(_CC_ANSWER_DONE, _CC_ANSWER_DONE_TTL)
    if client_id:
        _CC_ANSWER_CLIENT[(name, client_id)] = {"ts": time.time(),
                                                "res": dict(res)}
        _cc_answer_prune(_CC_ANSWER_CLIENT, _CC_ANSWER_CLIENT_TTL)


async def _cc_fresh_prompt(name: str):
    """強制拿最新畫面(不吃 5s 快取)→ (pane, prompt)。"""
    _PANE_CACHE.pop(name, None)
    pane = await _tmux_capture_cached(name)
    return pane, _cc_prompt(pane)


async def _cc_send(name: str, *args) -> None:
    rc, _, err = await _tmux_run("send-keys", "-t", name, *args)
    _PANE_CACHE.pop(name, None)
    if rc:
        raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                       err[:200] or "send-keys failed")


async def _cc_send_and_redraw(name: str, *args, before: str = "", until=None,
                              tries: int = 0):
    """送一個鍵,然後**等畫面真的反映了那個鍵**再回讀 → (pane, prompt)。

    兩層教訓,都是實跑踩出來的:
    1. 固定 sleep 不夠 —— 重繪比 sleep 慢就會讀到按鍵前的畫面,導覽迴圈誤判
       「還沒到」而多按一次;在送出鈕上多按一次 Down,游標就落到「Chat about
       this」,再按 Enter 等於把整個提問取消掉。
    2. 「整張 pane 變了」也不夠 —— 底部狀態列(`Claude | 5h 80% | 7d 23%`、
       spinner)自己就會變,於是「畫面變了」在游標還沒動的時候就成立了,
       第 1 點照樣重演。所以導覽時等的是**游標真的移動了**(`until`),
       不是「有什麼東西變了」。
    """
    await _cc_send(name, *args)
    pane = before
    for _ in range(tries or _CC_ANSWER_SETTLE_TRIES):
        await asyncio.sleep(_CC_ANSWER_SETTLE)
        _PANE_CACHE.pop(name, None)
        pane = await _tmux_capture_cached(name)
        if until(pane) if until else (pane != before):
            break
    return pane, _cc_prompt(pane)


def _cc_ms_focus_row(lines: list[str]):
    """游標(❯)停在哪一列 → ("option"|"input"|"submit"|"other", key)。

    多選清單的最後一列永遠是自由輸入列(TUI 原始碼 `[...options, inputOption]`),
    停在那裡送數字會變成打字,所以要先認出來。

    **「Chat about this」優先於送出鈕**:走過送出鈕之後,實機畫面上兩列都會
    帶 ❯,但真正吃 Enter 的是 chat(按下去 = 取消整個提問)。誰先回報錯了,
    後面就會在該退一格的時候直接按 Enter。
    """
    sub_i = None
    sub_focused = False
    for i, raw in enumerate(lines):
        if _CC_SUBMIT_ROW_RE.match(raw.rstrip()):
            sub_i = i
            sub_focused = raw.lstrip().startswith("❯")
    if sub_i is not None:
        for raw in lines[sub_i + 1:]:
            if raw.lstrip().startswith("❯") and _cc_opt_match(raw):
                return "other", _cc_opt_match(raw).group(1)
    if sub_focused:
        return "submit", ""
    last_opt_key = ""
    for i, raw in enumerate(lines):
        if sub_i is not None and i > sub_i:
            break                       # 送出鈕以下(Chat about this)不屬於清單
        m = _cc_opt_match(raw)
        if m and _CC_CHECKBOX_RE.match(m.group(2).strip()):
            last_opt_key = m.group(1)
    for raw in lines:
        if not raw.lstrip().startswith("❯"):
            continue
        m = _cc_opt_match(raw)
        if not m:
            continue
        key = m.group(1)
        if not _CC_CHECKBOX_RE.match(m.group(2).strip()):
            return "other", key         # 例:游標在「Chat about this」
        return ("input" if key == last_opt_key else "option"), key
    return "other", ""


def _cc_ms_checked(prompt: dict) -> set:
    return {str(o.get("key")) for o in (prompt.get("options") or [])
            if o.get("checked")}


def _cc_answer_stale(prompt, keys: list[str]):
    """提示還在嗎?這些 key 現在真的存在嗎?—— 不在就別盲送。"""
    if not isinstance(prompt, dict) or prompt.get("semantic") != "question":
        return "no live question prompt right now"
    valid = {str(o.get("key") or "") for o in (prompt.get("options") or [])}
    missing = [k for k in keys if k not in valid]
    if missing:
        return f"keys not on screen: {','.join(missing)}"
    return ""


@app.post("/ccsessions/{name}/answer")
async def cc_session_answer(name: str, request: Request):
    """AskUserQuestion 多選作答。body {"keys": ["1","3"], "submit": true}

    - `keys` = 作答後**應該被勾選的完整集合**(不是「要按哪幾個鍵」)。因為
      TUI 的數字是 toggle,若使用者已在終端機上先勾了東西,盲送 keys 會把它
      切掉;所以這裡先讀畫面上的現況,只送「現況 ⊕ 目標」的差集。
    - `submit=false` → 只勾選、不送出(版面留在原地,使用者可在終端機接手)。
    - `submit=true` → 走到內嵌送出鈕按下去;最後一題會再過一次確認頁才成交,
      非最後一題(鈕上寫 Next)則只推進到下一題,回 `submitted:false` +
      `advanced:true`,由 app 接著答下一題。
    """
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await request.json()
    raw_keys = body.get("keys")
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    if not isinstance(raw_keys, list) or not raw_keys:
        raise http_err(400, "KEYS_REQUIRED", "keys required (list of option keys)")
    keys, seen = [], set()
    for k in raw_keys:
        k = str(k).strip()
        if not k:
            continue
        if len(k) != 1 or not k.isdigit():
            raise http_err(400, "BAD_KEY", "only single-digit option keys are supported",
                           f"got {k!r}")
        if k not in seen:
            seen.add(k)
            keys.append(k)
    if not keys:
        raise http_err(400, "KEYS_REQUIRED", "keys required (list of option keys)")
    submit = body.get("submit")
    submit = True if submit is None else bool(submit)
    client_id = str(body.get("client_id") or "").strip()[:64]
    # client_id 冪等要在拿鎖**之前**判 —— 逾時重送時第一次可能還在跑,
    # 這時直接回原結果比 409「正在打字」對 app 更有用。
    replay = _cc_answer_client_get(name, client_id)
    if replay is not None:
        _log_event("cc_answer_replayed", session=name, client_id=client_id)
        return replay
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    async with _cc_seq_guard(name, "answer"):
        replay = _cc_answer_client_get(name, client_id)
        if replay is not None:
            return replay
        return await _cc_answer_locked(name, keys, submit, client_id)


async def _cc_answer_locked(name: str, keys: list, submit: bool,
                            client_id: str = "") -> dict:
    started = time.monotonic()

    def _budget_left() -> float:
        return _CC_ANSWER_BUDGET_SECS - (time.monotonic() - started)

    def _check_budget(stage: str) -> None:
        if _budget_left() <= 0:
            _log_event("cc_answer_budget_exceeded", session=name, stage=stage,
                       budget=_CC_ANSWER_BUDGET_SECS)
            raise http_err(504, "ANSWER_TIMEOUT",
                           "作答序列超過時間預算，已中止（畫面上的勾選維持現狀）",
                           f"stage={stage} budget={_CC_ANSWER_BUDGET_SECS}s")

    pane, prompt = await _cc_fresh_prompt(name)
    why = _cc_answer_stale(prompt, keys)
    if why:
        raise http_err(409, "PROMPT_STALE", "no matching live prompt right now", why)
    idem = _cc_answer_idem_key(name, prompt, keys, submit)
    hit = _cc_answer_idem_get(idem)
    if hit is not None:
        # 同一個 prompt 還在畫面上、同一組 keys 又送一次 = 雙擊/兩個入口
        # 同時送。回原本那次的結果，不要再跑一輪 toggle 把勾選切掉。
        _log_event("cc_answer_replayed", session=name, keys=",".join(keys))
        return hit
    if not prompt.get("multiselect"):
        # 單選版面:沒有勾選框、沒有送出鈕,語意與 /key 完全相同(數字即成交)。
        # 讓 app 只維護一條作答路徑,但行為一字不改地沿用既有 /key。
        # 用 `_locked` 版:序列鎖已經在外層拿著了,再拿一次會自己撞自己。
        if len(keys) > 1:
            raise http_err(400, "NOT_MULTISELECT",
                           "this prompt only accepts one option", "send a single key")
        res = await _cc_key_core_locked(name, keys[0])
        out = {"ok": True, "sent": keys, "submitted": True,
               "multiselect": False, **{k: v for k, v in res.items() if k != "ok"}}
        _cc_answer_idem_put(idem, out, name, client_id)
        return out

    lines = pane.splitlines()
    kind, _focus_key = _cc_ms_focus_row(lines)
    if kind in ("input", "other"):
        # 游標在自由輸入列 / Chat about this 上 —— 數字會被當成打字。Up 可以
        # 把游標移開(TUI 原始碼:輸入列聚焦時 up 仍會傳給清單、chat 聚焦時
        # up 先取消 chat 焦點)。移不開就不送,寧可回錯也不要打出垃圾字。
        pane, prompt = await _cc_send_and_redraw(name, "Up", before=pane)
        if _cc_answer_stale(prompt, keys):
            raise http_err(409, "PROMPT_STALE", "prompt vanished while re-focusing")
        kind, _focus_key = _cc_ms_focus_row(pane.splitlines())
        if kind in ("input", "other"):
            raise http_err(409, "FOCUS_UNSAFE",
                           "cursor is on the free-text row — digits would be typed, not toggled",
                           "move the cursor in the terminal and retry")

    want = set(keys)
    sent: list[str] = []
    for _attempt in range(2):
        have = _cc_ms_checked(prompt)
        todo = sorted(want ^ have, key=int)
        if not todo:
            break
        for k in todo:                      # 數字是 toggle → 只送「現況 ⊕ 目標」
            _check_budget("toggle")
            expect = _cc_ms_checked(prompt) ^ {k}
            pane, prompt = await _cc_send_and_redraw(
                name, "-l", k, before=pane,
                # 等的是「那一格真的翻了」。底部狀態列自己會變,拿整張 pane
                # 比對會在勾選還沒重繪時就放行,下一輪算差集就算錯 → 又送一次
                # → 把剛勾好的又切掉。
                until=lambda p, _e=expect: _cc_ms_checked(_cc_prompt(p) or {}) == _e)
            sent.append(k)
            await asyncio.sleep(_CC_ANSWER_KEY_GAP)
        if _cc_answer_stale(prompt, keys):
            raise http_err(409, "PROMPT_STALE", "prompt vanished while toggling",
                           f"sent={','.join(sent)}")
    final = _cc_ms_checked(prompt)
    if final != want:
        # 回讀對不上就**不要**繼續送出 —— 送出一個錯的選集比不送出更糟。
        _log_event("cc_answer_toggle_mismatch", session=name,
                   want=sorted(want), got=sorted(final), sent=sent)
        raise http_err(409, "TOGGLE_MISMATCH",
                       "on-screen selection does not match the requested keys",
                       f"want={','.join(sorted(want))} got={','.join(sorted(final))}")
    out = {"ok": True, "sent": sent, "selected": sorted(final, key=int),
           "multiselect": True, "submitted": False,
           "budget_secs": _CC_ANSWER_BUDGET_SECS}
    if prompt.get("q_total"):
        out["q_total"] = prompt["q_total"]
    if not submit:
        _cc_answer_idem_put(idem, out, name, client_id)
        return out
    _check_budget("submit")
    res = await _cc_multiselect_submit(name, pane, prompt, _check_budget)
    out.update(res)
    if out.get("submitted") is False and not out.get("advanced"):
        # 「確認頁沒收掉」= 這次**沒有成交**。舊碼回 HTTP 200 + submitted:false,
        # 呼叫端一律把 200 當成功、對使用者說「已送出」—— 那是最壞的一種錯:
        # 使用者以為答完了,CC 其實還停在確認頁等人按。改成 409,app 不可能誤判。
        _log_event("cc_answer_not_submitted", session=name,
                   selected=sorted(final, key=int), detail=str(res)[:160])
        raise http_err(409, "ANSWER_NOT_SUBMITTED",
                       "選項已勾好，但確認頁沒有收掉 —— 這次沒有送出，請在終端機確認",
                       json.dumps({k: v for k, v in out.items() if k != "ok"},
                                  ensure_ascii=False)[:400])
    _cc_answer_idem_put(idem, out, name, client_id)
    return out


async def _cc_multiselect_submit(name: str, pane: str, prompt: dict,
                                 check_budget=None) -> dict:
    """走到內嵌送出鈕 → Enter → (最後一題)確認頁 → 成交。閉環驗證,不盲送。"""
    label = prompt.get("submit_label") or ""
    if not label:
        raise http_err(409, "NO_SUBMIT_ROW",
                       "this layout has no inline submit button",
                       "selection kept; submit from the terminal")
    # 導覽以「游標現在停在哪一列」為準,不看 `submit_focused` —— 走過頭時
    # 送出鈕的 ❯ **不會消失**,「Chat about this」也跟著亮,兩列都帶 ❯;只看
    # submit_focused 會在那個狀態下判定「到了」然後按 Enter,結果是把整個提問
    # 取消掉(2026-08-11 實跑兩次都栽在這裡)。`_cc_ms_focus_row` 讓 chat 優先。
    focus = _cc_ms_focus_row(pane.splitlines())
    presses = 0
    while focus[0] != "submit" and presses < _CC_ANSWER_MAX_NAV:
        if check_budget:
            check_budget("navigate")
        step = "Up" if focus[0] == "other" else "Down"     # 走過頭就退回來
        pane, prompt2 = await _cc_send_and_redraw(
            name, step, before=pane,
            until=lambda p, _was=focus: _cc_ms_focus_row(p.splitlines()) != _was)
        presses += 1
        if not isinstance(prompt2, dict) or not prompt2.get("submit_label"):
            raise http_err(409, "PROMPT_STALE",
                           "the menu disappeared while walking to the submit button")
        label = prompt2.get("submit_label") or label
        focus = _cc_ms_focus_row(pane.splitlines())
    if focus[0] != "submit":
        raise http_err(409, "SUBMIT_UNREACHABLE",
                       "could not focus the submit button",
                       f"{presses} presses, cursor is on {focus[0]!r}")
    # 按下送出鈕之後畫面會整頁換掉(確認頁 / 下一題),但 CC 要一點時間才畫完。
    # 這裡等的是「真的換頁了」而不是「有東西變了」—— 否則會讀到按 Enter 前的
    # 選單,把一次成功的送出誤報成「推進到下一題」(實跑踩過)。
    title_before = str(prompt.get("title") or "")

    def _moved_on(p: str) -> bool:
        if any(m in p.lower() for m in _CC_REVIEW_MARKERS):
            return True
        q = _cc_prompt(p)
        if not isinstance(q, dict) or q.get("semantic") != "question":
            return True                       # 選單整個收掉了
        return str(q.get("title") or "") != title_before

    pane, after = await _cc_send_and_redraw(name, "Enter", before=pane,
                                            until=_moved_on,
                                            tries=_CC_ANSWER_CONFIRM_TRIES)
    low = pane.lower()
    if any(m in low for m in _CC_REVIEW_MARKERS):
        # 確認頁:`❯ 1. Submit answers` / `  2. Cancel`(這一頁沒有 "Enter to
        # select" footer,所以 _cc_prompt 看不到它,只能用文字認)。
        confirm = next((ln for ln in pane.splitlines()
                        if re.match(r"^\s*❯?\s*1\.\s+Submit answers\s*$", ln)), None)
        if not confirm:
            raise http_err(409, "REVIEW_UNEXPECTED",
                           "review screen did not offer 'Submit answers'",
                           "selection kept; confirm from the terminal")
        # 「畫面變了」不等於「確認頁收掉了」:CC 要花約 1 秒才把提問結掉,期間
        # 狀態列/spinner 已經在動,確認頁的字還在。實跑時就是這樣把一次**成功**
        # 的送出回報成 submitted:false —— 所以這裡等的是「確認頁不見了」。
        await _cc_send(name, "-l", "1")
        still = True
        for _ in range(_CC_ANSWER_CONFIRM_TRIES):
            await asyncio.sleep(_CC_ANSWER_SETTLE)
            _PANE_CACHE.pop(name, None)
            pane2 = await _tmux_capture_cached(name)
            still = any(m in pane2.lower() for m in _CC_REVIEW_MARKERS)
            if not still:
                break
        _log_event("cc_answer_submitted", session=name, nav_presses=presses,
                   confirmed=not still)
        return {"submitted": not still, "submit_label": label,
                "confirmed_review": True}
    if isinstance(after, dict) and after.get("semantic") == "question":
        # 還在選單裡 = 剛才那顆是 "Next",已推進到下一題(多題 ask)。
        await _cc_refine_q_index(name, after)
        return {"submitted": False, "advanced": True, "submit_label": label,
                "q_total": after.get("q_total"), "q_index": after.get("q_index")}
    # 既不是確認頁也不是選單 —— 版面已收掉,當成已送出但標注未確認。
    _log_event("cc_answer_submitted", session=name, nav_presses=presses,
               confirmed=False, note="menu_gone_without_review")
    return {"submitted": True, "submit_label": label, "confirmed_review": False}


# ─────────── 批次 3 斷點③:CC waiting_approval → approval feed + 推播 ────────
# persona(Approval Center)/CX(app-server request)本來就有 approval 記錄;CC 的
# 「審核」是 TUI prompt,這裡補一個常駐 watcher:prompt 出現 → 建記錄+推播,
# prompt 消失 → 過期。decide 回流在 _approval_decide_core 的 claude_code 分支
# (送 TUI 鍵),三線同一條 approval 管線(批次 3 完成判準)。

_CC_APPROVAL_ACTIVE: dict = {}      # name -> {"aid": str, "sig": str}
_CC_APPROVAL_POLL_SECS = 1.5   # 4.0→1.5:審核偵測延遲主項;只巡 app-owned,可負擔
_CC_APPROVAL_TTL = 900.0

_CC_ALLOW_RE = re.compile(r"^(always allow|allow|yes)", re.IGNORECASE)
_CC_DENY_RE = re.compile(r"^(don.t allow|deny|no)", re.IGNORECASE)


def _cc_prompt_sig(prompt: dict) -> str:
    """同一個 prompt 的穩定簽名 — watcher 每 tick 都看到它,不能重複建。"""
    raw = json.dumps([prompt.get("title"),
                      [(o.get("key"), o.get("label"))
                       for o in prompt.get("options") or []]],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _cc_ask_sig(prompt: dict) -> str:
    """「同一個 AskUserQuestion」的簽名 —— 跨題目穩定,答完 Q1 換 Q2 也不變。

    用頁籤列的題目短標籤(`←  ☒ Fruits  ☐ Drinks  ✔ Submit  →`)+ 題數:
    這兩者在整個 ask 的生命週期裡固定,只有勾選記號會變(已排除)。單題 ask
    回空字串 —— 單題沒有「推進到下一題」的情境,走原本的建卡路徑就好。
    """
    total = prompt.get("q_total") or 0
    headers = prompt.get("q_headers") or []
    if total < 2 or not headers:
        return ""
    raw = json.dumps([total, headers], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _cc_find_pending_aid(name: str, sig: str) -> str | None:
    """重啟/記憶體遺失後,認領 DB 裡同一 live prompt 的既有 pending 審核。
    背景:`_CC_APPROVAL_ACTIVE` 是行程內狀態,重啟即清空,但 DB 的 pending 列
    還在;watcher 若 active=None 就盲建新 aid,App 手上的舊 aid 立刻變孤兒——
    按了 `active.aid != aid` 吃 409、鍵送不進 TUI(2026-07-16 使用者回報「在
    Pocket 按 CC 審核沒反應,得回 CC 裡按」的根因)。這裡以重建的 prompt sig
    對映既有 pending 列;找到就認領該 aid(不另建),找不到回 None。"""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            rows = con.execute(
                "SELECT id,title,options FROM approvals "
                "WHERE source=? AND status='pending' ORDER BY created_at DESC",
                (f"claude_code:{name}",)).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_adopt_query_error", session=name, error=str(e)[:160])
        return None
    for rid, title, options in rows:
        try:
            opts = json.loads(options) if options else []
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_find_pending_aid", _exc, expected=True)
            opts = []
        if _cc_prompt_sig({"title": title, "options": opts}) == sig:
            return rid
    return None


def _cc_reseed_approvals_from_db() -> int:
    """啟動時把 DB 的 pending CC 審核重新灌回 `_CC_APPROVAL_ACTIVE`,補上重啟
    清空記憶體與 watcher 首巡(≤1.5s)之間的空窗。同一 session 多筆 pending 時
    留最新一筆、其餘標 expired(收孤兒重複列)。回重灌筆數。"""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            rows = con.execute(
                "SELECT id,source,title,options FROM approvals "
                "WHERE source LIKE 'claude_code:%' AND status='pending' "
                "ORDER BY created_at DESC").fetchall()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_reseed_error", error=str(e)[:160])
        return 0
    seen: set = set()
    n = 0
    for rid, source, title, options in rows:
        name = source.split(":", 1)[1] if ":" in source else source
        if name in seen:
            _cc_approval_set_status(rid, "expired")   # 同 session 舊的重複列 → 收掉
            continue
        seen.add(name)
        try:
            opts = json.loads(options) if options else []
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_reseed_approvals_from_db", _exc, expected=True)
            opts = []
        sig = _cc_prompt_sig({"title": title, "options": opts})
        _CC_APPROVAL_ACTIVE[name] = {"aid": rid, "sig": sig}
        n += 1
    if n:
        _log_event("cc_approval_reseeded", count=n)
    return n


def _cc_choice_key(prompt: dict, approve: bool) -> str:
    """approve 布林 → prompt option key。認得 allow/deny 字樣就精準選;
    認不得時 approve=第一個選項、deny=Esc(TUI 的通用取消)。"""
    options = prompt.get("options") or []
    pat = _CC_ALLOW_RE if approve else _CC_DENY_RE
    for o in options:
        if pat.match(str(o.get("label") or "").strip()):
            return str(o.get("key") or "")
    if approve and options:
        return str(options[0].get("key") or "")
    return "esc"


def _cc_approval_set_status(aid: str, status: str) -> bool:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            cur = con.execute("UPDATE approvals SET status=?, decided_at=? "
                              "WHERE id=? AND status='pending'",
                              (status, time.time(), aid))
            con.commit()
            con.close()
            return bool(cur.rowcount)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_db_error", approval_id=aid, error=str(e)[:160])
        return False


def _cc_approval_kind(prompt: dict) -> str:
    # A1:語意在誕生點標好 — _cc_prompt 的分支即分類(泛選單=question、
    # 權限/yesno=permission);permission 的鍵由 bridge 標 style,app 只渲染,
    # 不再用 label 猜「哪顆是拒絕」。question 無 danger 語意(spec §2)。
    return "question" if prompt.get("semantic") == "question" else "permission"


def _cc_approval_payload(name: str, prompt: dict) -> dict:
    """prompt → 審核記錄的可變欄位(title/detail/options/meta)。

    2026-08-11:options 以前只寫 {key,label,style},把 `description` 整段丟掉,
    而 CC 卡片流預設開,使用者在 Pocket 看到的就是這張卡 —— 等於「選項說明」
    在最常走的那條路上永遠是空的。多選還多了 multiselect/q_index/q_total
    三個決定 app 該畫單選還是複選、要不要顯示「第 2/3 題」的欄位。
    """
    title = (prompt.get("title") or "").strip() or f"{name} 等待核准"
    opts_txt = " / ".join(str(o.get("label") or "")[:30]
                          for o in (prompt.get("options") or [])[:4])
    detail = f"session: {name}\n{title}" + (f"\n選項: {opts_txt}" if opts_txt else "")
    kind = _cc_approval_kind(prompt)
    options = []
    for o in (prompt.get("options") or [])[:8]:
        okey = str(o.get("key") or "").strip()
        if not okey:
            continue
        ent = {"key": okey, "label": str(o.get("label") or "")[:80]}
        desc = str(o.get("description") or "").strip()
        if desc:
            ent["description"] = desc[:400]
        if o.get("checked") is not None:
            ent["checked"] = bool(o.get("checked"))
        if kind == "permission":
            lab = ent["label"].strip()
            if _CC_DENY_RE.match(lab):
                ent["style"] = "danger"
            elif _CC_ALLOW_RE.match(lab):
                ent["style"] = "primary"
            else:
                ent["style"] = "secondary"
        options.append(ent)
    meta = {}
    if kind == "question":
        meta["multiselect"] = bool(prompt.get("multiselect"))
    for k in ("q_index", "q_total"):
        if prompt.get(k) is not None:
            meta[k] = prompt[k]
    if prompt.get("q_headers"):
        meta["q_headers"] = prompt["q_headers"]
    return {"title": title, "detail": detail, "kind": kind, "options": options,
            "meta": meta}


def _cc_approval_create(name: str, prompt: dict) -> str:
    import sqlite3
    aid = "cc-" + uuid.uuid4().hex[:24]
    p = _cc_approval_payload(name, prompt)
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        con.execute("INSERT OR REPLACE INTO approvals"
                    "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                    "session_id,provider,kind,options,meta) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, p["title"], f"claude_code:{name}",
                     "high" if p["kind"] == "permission" else "low", p["detail"],
                     now, now + _CC_APPROVAL_TTL, "pending", None, None, None,
                     f"claude_code:{name}", "claude_code", p["kind"],
                     json.dumps(p["options"], ensure_ascii=False) if p["options"] else None,
                     json.dumps(p["meta"], ensure_ascii=False) if p["meta"] else None))
        con.commit()
        con.close()
        return aid
    finally:
        con.close()


def _cc_approval_update(aid: str, name: str, prompt: dict) -> bool:
    """就地換掉 pending 審核的內容(同一個 ask 推進到下一題時用)。

    為什麼不重建:`_cc_prompt_sig` 只吃 title+options,多題 ask 每答完一題,
    畫面上的題目與選項就整組換掉 → sig 變 → 舊卡標 expired、新開一張卡、
    再推播一次。三題 = 三次推播 + 兩張過期灰卡,而使用者從頭到尾只是在回答
    同一個提問。同一個 ask 沿用同一個 approval id,App 手上的卡也不會變孤兒。
    """
    import sqlite3
    p = _cc_approval_payload(name, prompt)
    try:
        # 只 close 一次(finally),而且 `cur.rowcount` 在 close **之前**讀 ——
        # 舊碼 try 內 close 完再讀 rowcount,是在已關閉的 cursor 上取值。
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            cur = con.execute(
                "UPDATE approvals SET title=?,detail=?,kind=?,options=?,meta=? "
                "WHERE id=? AND status='pending'",
                (p["title"], p["detail"], p["kind"],
                 json.dumps(p["options"], ensure_ascii=False) if p["options"] else None,
                 json.dumps(p["meta"], ensure_ascii=False) if p["meta"] else None,
                 aid))
            changed = bool(cur.rowcount)
            con.commit()
            return changed
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_db_error", approval_id=aid, error=str(e)[:160])
        return False


# ── 2b:人格 choices 卡 → 審核中心(2026-07-16 XCash 拍板:所有 choices 卡進、
#    中心只放決策鈕、純連結選項留聊天)。FLiPER 複審卡等走選擇閘道契約,原本只是
#    人格報告/訊息、不進 approvals 表 → 中心看不到。這裡定期掃 report_events,把
#    kind:choices 卡同步成 hermes pending 審核;決議時把選項 send 文字當人格回合送回。
_HP_CHOICES_SCAN_WINDOW = 6 * 3600     # 掃近 6h 的卡(含已解除的,好把殭屍審核收掉);
_HP_CHOICES_TTL = 12 * 3600            # 建了之後 pending 最多留 12h(未決自動過期)。
_HP_CHOICES_POLL_SECS = 30.0
_HP_CHOICES_FENCE = "```studio-card"

# 即時待檢討真相來源:report 卡是快照,審核可能已在 FLiPER/TG 那邊解除(resume)。
# 只靠 report 建審核會產生殭屍(2026-07-16 使用者回報:審查已結束卻還能按)。
# review_pipeline.json 是 FLiPER 待檢討狀態機的落地檔(resume/hold 都寫它),
# 用它驗證某貼文『當下是否真的還在 held(待檢討)』。
FLIPER_REVIEW_STATE = os.path.expanduser(
    "~/apps/lobster-tg/workspace/state/review_pipeline.json")


def _fliper_review_state() -> dict | None:
    """FLiPER 待檢討狀態(貼文 id → 記錄);讀不到回 None(狀態未知,不動作)。"""
    try:
        with open(FLIPER_REVIEW_STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_fliper_review_state", _exc, expected=True)
        return None


def _fliper_ref_held(state: dict, ref: str) -> bool:
    """該貼文當下是否還在待檢討(任一審查階段 held=true)。"""
    v = state.get(ref) if state else None
    return bool(isinstance(v, dict)
                and (v.get("first_review_held") or v.get("second_review_held")))


def _hp_extract_choices(content: str) -> dict | None:
    """從內容抽第一張 kind:choices 的 studio-card;無/壞則 None。"""
    if _HP_CHOICES_FENCE not in content or '"choices"' not in content:
        return None
    i = content.find(_HP_CHOICES_FENCE)
    after = content[i + len(_HP_CHOICES_FENCE):]
    end = after.find("```")
    if end < 0:
        return None
    try:
        card = json.loads(after[:end].strip())
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_hp_extract_choices", _exc, expected=True)
        return None
    if not isinstance(card, dict) or card.get("kind") != "choices" or not card.get("options"):
        return None
    return card


def _hp_choices_stable_id(persona: str, card: dict) -> str:
    ref = str(card.get("ref") or card.get("title") or "")
    return "hpc-" + hashlib.sha1(f"{persona}|{ref}".encode("utf-8", "replace")).hexdigest()[:24]


def _hp_choices_upsert(persona: str, card: dict) -> str | None:
    """建一筆人格 choices 審核(session_id=hermes:{persona},kind=question)。只收
    帶 send 的決策鈕(純連結選項有 url → 留聊天,不進中心)。已決議的同卡不復活。"""
    import sqlite3
    if persona not in PERSONAS:
        return None
    decision_opts = []
    for o in card.get("options") or []:
        if o.get("url"):
            continue                                   # 純連結鈕不進中心(拍板)
        key = str(o.get("key") or "").strip()
        send = o.get("send") or o.get("label")
        if not key or not send:
            continue
        decision_opts.append({"key": key, "label": str(o.get("label") or "")[:80],
                              "style": o.get("style") or "primary", "send": str(send)})
    if not decision_opts:
        return None
    aid = _hp_choices_stable_id(persona, card)
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        row = con.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()
        if row:
            con.close()
            return aid if row[0] == "pending" else None    # 已在/已決議 → 不重寫、不復活
        title = str(card.get("title") or "需要你選擇")[:200]
        detail = str(card.get("detail") or "")[:400]
        con.execute("INSERT INTO approvals"
                    "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                    "session_id,provider,kind,options) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, title, f"hermes:{persona}", "low", detail,
                     now, now + _HP_CHOICES_TTL, "pending", None, None, None,
                     f"hermes:{persona}", "hermes", "question",
                     json.dumps(decision_opts, ensure_ascii=False)))
        con.commit()
        con.close()
        _log_event("hp_choices_approval_created", session=persona, approval_id=aid,
                   title=title[:60], options=len(decision_opts))
        return aid
    finally:
        con.close()


def _hp_choices_expire(aid: str) -> None:
    """把一筆殭屍 choices 審核收掉(已在 FLiPER 解除,不再需要決策)。"""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            cur = con.execute("UPDATE approvals SET status='expired', decided_at=? "
                              "WHERE id=? AND status='pending'", (time.time(), aid))
            con.commit()
            n = cur.rowcount
            con.close()
            if n:
                _log_event("hp_choices_approval_expired", approval_id=aid, reason="resolved_upstream")
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("hp_choices_expire_failed", approval_id=aid, error=str(e)[:160])


def _hp_choices_scan() -> int:
    """掃近 _HP_CHOICES_SCAN_WINDOW 的人格報告,同步 choices 卡到審核中心。
    真相以 FLiPER review_pipeline.json 的即時 held 狀態為準:確實還在待檢討 → 建;
    已解除 → 收掉殭屍。無法對映 FLiPER 貼文(非複審卡/查不到狀態)→ 保守不進中心
    (避免無從得知解除的殭屍),那類卡的按鈕仍在聊天視窗可用。回新建數。"""
    import sqlite3
    since = time.time() - _HP_CHOICES_SCAN_WINDOW
    state = _fliper_review_state()
    if state is None:
        return 0                            # 狀態未知 → 這輪不動作(不建、不亂 expire)
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            rows = con.execute(
                "SELECT session, content FROM report_events WHERE ts > ? "
                "AND content LIKE '%```studio-card%' AND content LIKE '%\"choices\"%' "
                "ORDER BY rowid DESC LIMIT 100", (since,)).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("hp_choices_scan_failed", error=str(e)[:160])
        return 0
    created, seen = 0, set()
    for session, content in rows:
        if session not in PERSONAS:
            continue
        card = _hp_extract_choices(content or "")
        if not card:
            continue
        ref = str(card.get("ref") or "").strip()
        if not ref or ref not in state:
            continue                        # 非 FLiPER 貼文 / 查不到 → 不進中心
        sid = _hp_choices_stable_id(session, card)
        if sid in seen:
            continue
        seen.add(sid)
        if _fliper_ref_held(state, ref):
            if _hp_choices_upsert(session, card):
                created += 1
        else:
            _hp_choices_expire(sid)         # 已解除 → 收掉殭屍(若存在且 pending)
    return created


async def _hp_choices_watcher():
    """常駐:定期把人格 choices 卡同步成審核中心 pending 列。"""
    while True:
        await asyncio.sleep(_HP_CHOICES_POLL_SECS)
        try:
            _hp_choices_scan()
        except Exception as e:  # noqa: BLE001
            _log_event("hp_choices_watcher_error", error=str(e)[:160])


async def _cc_approval_watcher():
    """常駐:每 1.5s 巡一輪 owned CC sessions,強制拿新 pane(不吃 5s 快取)。
    舊配置(4s 間隔 + 5s 舊 pane)讓「prompt 出現→建 approval」最壞 ~9 秒;
    現在 ≤1.5s。只巡 app-owned(通常 1-7 條),capture-pane 一次 ~10-20ms,
    可負擔;順帶把新鮮 pane 回填快取給首頁清單用。"""
    while True:
        await asyncio.sleep(_CC_APPROVAL_POLL_SECS)
        # 作用域 v2:enabled ccsess 一律掃(訂閱制),排除靠 approvals-exclude.txt。
        # 舊制只掃 app-owned,那批 session 死光後名單空,watcher 靜默斷炊六天
        # (2026-07-10~16 零 approval)——聊天窗選項卡/審核中心/推播整條跟著死。
        scope = _cc_approval_scope_names()
        for name, _workdir, enabled in _cc_conf_rows():
            if enabled != "1":
                continue
            if name not in scope:
                continue
            try:
                _PANE_CACHE.pop(name, None)   # 審核偵測不能吃舊畫面
                st = await _cc_status_core(name)
                prompt = st.get("prompt")
                active = _CC_APPROVAL_ACTIVE.get(name)
                if prompt:
                    sig = _cc_prompt_sig(prompt)
                    if active and active["sig"] == sig:
                        continue                     # 同一個 prompt,已建過
                    ask_sig = _cc_ask_sig(prompt)
                    if (active and ask_sig and active.get("ask_sig") == ask_sig
                            and _cc_approval_update(active["aid"], name, prompt)):
                        # 同一個 ask 的下一題 → 就地換內容,不過期、不重建、不重推
                        active["sig"] = sig
                        try:
                            _cc_cards_feed_approval(
                                name, _approval_get_row(active["aid"]) or {})
                        except Exception as e:  # noqa: BLE001
                            _log_event("cc_cards_feed_error", error=str(e)[:160])
                        _log_event("cc_approval_question_advanced", session=name,
                                   approval_id=active["aid"],
                                   q_index=prompt.get("q_index"),
                                   q_total=prompt.get("q_total"))
                        continue
                    if active:
                        _cc_approval_set_status(active["aid"], "expired")
                        try:
                            _cc_cards_feed_approval(
                                name, _approval_get_row(active["aid"]) or {},
                                resolved="expired")
                        except Exception as e:  # noqa: BLE001
                            _log_event("cc_cards_feed_error", error=str(e)[:160])
                    # 記憶體沒有(常見於重啟後)→ 先認領 DB 既有的同一 prompt
                    # pending 列,別另建新 aid 讓 App 手上的舊 aid 變孤兒(按了
                    # 吃 409、TUI 收不到鍵)。認領到就沿用該 aid,不重推。
                    adopted = _cc_find_pending_aid(name, sig)
                    if adopted:
                        _CC_APPROVAL_ACTIVE[name] = {"aid": adopted, "sig": sig,
                                                     "ask_sig": ask_sig}
                        _log_event("cc_approval_adopted", session=name,
                                   approval_id=adopted)
                        continue
                    aid = _cc_approval_create(name, prompt)
                    _CC_APPROVAL_ACTIVE[name] = {"aid": aid, "sig": sig,
                                                 "ask_sig": ask_sig}
                    opts = " / ".join(str(o.get("label") or "")[:20]
                                      for o in (prompt.get("options") or [])[:3])
                    _approval_push(aid, prompt.get("title") or f"{name} 等待核准",
                                   f"{name}" + (f" · {opts}" if opts else ""),
                                   f"claude_code:{name}")
                    try:
                        # A3:CC 卡片流補齊 — pending → approval 卡(三 provider
                        # 同一組 wire shape,見 carddigest.ApprovalCardMixin)。
                        _cc_cards_feed_approval(name, _approval_get_row(aid) or {})
                    except Exception as e:  # noqa: BLE001
                        _log_event("cc_cards_feed_error", error=str(e)[:160])
                    _log_event("cc_approval_created", session=name,
                               approval_id=aid)
                elif active:
                    # prompt 消失(TUI 上被回掉/回合結束)→ 記錄過期,feed 不留殭屍
                    rec = _approval_get_row(active["aid"]) or {}
                    _cc_approval_set_status(active["aid"], "expired")
                    try:
                        _cc_cards_feed_approval(name, rec, resolved="expired")
                    except Exception as e:  # noqa: BLE001
                        _log_event("cc_cards_feed_error", error=str(e)[:160])
                    _CC_APPROVAL_ACTIVE.pop(name, None)
            except Exception as e:  # noqa: BLE001
                _log_event("cc_approval_watch_error", session=name,
                           error=str(e)[:160])


# S3 (wave 2): one-tap CC permission-mode / model switching. shift+tab cycles
# FOUR states on this box (normal → accept edits → plan → auto mode → normal),
# so instead of blind-counting presses we close the loop: press → fresh pane →
# check, up to 6 presses. Immune to cycle-order drift across CC versions.
_CC_MODES = ("normal", "acceptEdits", "plan", "auto")
_CC_MODE_ALIASES = {"default": "normal"}   # older app builds say "default"
_CC_MODE_MAX_PRESSES = 6


async def _cc_mode_fresh(name: str) -> str | None:
    _PANE_CACHE.pop(name, None)
    st = await _cc_status_core(name)
    return st.get("mode")


@app.post("/ccsessions/{name}/mode")
async def cc_session_mode(name: str, request: Request):
    """Switch the CC permission mode. body {"mode": "normal"|"acceptEdits"|
    "plan"|"auto"}. Sends shift+tab (BTab) and VERIFIES via the pane's bottom
    bar after each press; replies with the mode actually reached."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    body = await request.json()
    raw = str(body.get("mode") or "").strip()
    target = _CC_MODE_ALIASES.get(raw, raw)
    if target not in _CC_MODES:
        raise HTTPException(status_code=400,
                            detail=f"mode must be one of {'|'.join(_CC_MODES)}")
    mode = await _cc_mode_fresh(name)
    if mode is None:
        # A running turn hides the bottom-bar mode marker — a blind toggle
        # could not be verified, so refuse instead of guessing.
        raise http_err(409, "CC_BUSY", "turn running; mode bar hidden — retry when idle")
    presses = 0
    while mode != target and presses < _CC_MODE_MAX_PRESSES:
        rc, _, err = await _tmux_run("send-keys", "-t", name, "BTab")
        if rc:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                           err[:200] or "send-keys failed")
        presses += 1
        await asyncio.sleep(0.35)          # let the TUI repaint the bottom bar
        mode = await _cc_mode_fresh(name)
    _log_event("cc_mode_switch", session=name, target=target,
               reached=mode, presses=presses)
    if mode != target:
        raise http_err(502, "MODE_UNREACHED",
                       f"sent {presses} shift+tab, pane reports {mode or 'unknown'}")
    return {"ok": True, "mode": mode, "presses": presses}


_CC_MODEL_RE = re.compile(r"^[A-Za-z0-9 ._/-]{1,60}$")


@app.post("/ccsessions/{name}/model")
async def cc_session_model(name: str, request: Request):
    """Switch the CC model by typing the /model slash command into the live
    TUI. body {"model": "opus"|"sonnet"|full model name}. Confirmation is
    best-effort: we re-capture the pane and report whether the requested name
    shows up (confirmed), but the command is sent either way."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    body = await request.json()
    model = str(body.get("model") or "").strip()
    if not _CC_MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="invalid model name")
    pane_before = await _cc_capture_pane_fresh(name)
    if _cc_pane_busy(pane_before):
        raise http_err(409, "CC_BUSY", "turn running; model switch needs an idle prompt")
    await _cc_paste_text(name, f"/model {model}")
    await asyncio.sleep(0.8)               # slash command feedback repaint
    _PANE_CACHE.pop(name, None)
    pane = await _cc_capture_pane_fresh(name)
    tail = "\n".join(pane.strip().splitlines()[-12:])
    confirmed = model.lower() in tail.lower()
    _log_event("cc_model_switch", session=name, model=model, confirmed=confirmed)
    return {"ok": True, "model": model, "confirmed": confirmed}


# ───────────────────────── /app/v2 control-plane facade ─────────────────────
# Additive: aggregates claude_code / codex / hermes into one Session shape
# (docs/CONTROL_PLANE_V2.md). CC sessions awaiting a permission prompt surface as
# status=waiting_approval so the app can list them. v1/ccsessions/codexsessions
# stay untouched.

async def _v2_cc_state(name: str):
    if not await _tmux_alive(name):
        return ("failed", None)
    _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
    prompt = _cc_prompt(pane)
    if prompt:
        return ("waiting_approval", prompt)
    busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
    return ("running" if busy else "idle", None)


@app.get("/app/v2/agents")
async def v2_agents(request: Request):
    _check_auth(request)
    return {"agents": [
        {"provider": "claude_code", "name": "Claude Code", "kind": "code_agent",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": False},
        {"provider": "codex", "name": "Codex", "kind": "code_agent",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": True},
        {"provider": "hermes", "name": "Hermes", "kind": "persona",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": False},
    ] + ([
        {"provider": "openclaw", "name": "OpenClaw", "kind": "code_agent",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": False},
    ] if OPENCLAW.configured() else [])}


@app.get("/app/v2/sessions")
async def v2_sessions(request: Request, provider: str = "", status: str = ""):
    _check_auth(request)
    out = []
    degraded = []   # 取清單失敗的 provider(目前只有 codex 分支會標)
    out.extend(await _delegation_v2_sessions())
    for name, workdir, enabled in _cc_conf_rows():
        if enabled != "1":
            continue
        st, prompt = await _v2_cc_state(name)
        caps = ["input", "interrupt", "keys", "attachments", "replay", "follow"]
        if prompt:
            caps.append("approve")
        meta = {}
        if prompt:
            meta["prompt"] = prompt   # 相容期保留(A4 刪),app 舊版仍讀這裡
            # A1:meta.approval 統一物件 — 由 watcher 建的 DB 列對回。watcher
            # 巡週期 1.5s,prompt 剛出現的窄縫可能還沒有列 → 只給 prompt。
            active = _CC_APPROVAL_ACTIVE.get(name)
            d = _approval_get_row(str(active.get("aid"))) if active else None
            if d and d.get("status") == "pending":
                meta["approval"] = d
        out.append({"id": f"claude_code:{name}", "provider": "claude_code", "title": name,
                    "subtitle": workdir, "status": st, "last_event_at": None,
                    "capabilities": caps, "meta": meta})
    # A1(spec §7-5):hermes persona 有 pending 待審 → waiting_approval +
    # meta.approval 統一物件(之前恆 idle 是 spec 點名的缺口)。
    hp_pending = _hermes_pending_by_session()
    for mid, (disp, _home) in PERSONAS.items():
        pend = hp_pending.get(f"hermes:{mid}")
        out.append({"id": f"hermes:{mid}", "provider": "hermes", "title": disp,
                    "subtitle": None,
                    "status": "waiting_approval" if pend else "idle",
                    "last_event_at": pend.get("created_at") if pend else None,
                    "capabilities": ["input", "attachments", "replay", "follow", "approve"],
                    "meta": ({"approval": pend} if pend else {})})
    delegated_codex_ids = _delegated_codex_thread_ids()
    try:
        for t in await _codex_v2_visible_threads(20):
            s = _codex_enrich_summary(_codex_session_summary(t))
            if (s.get("thread_id") or s.get("id")) in delegated_codex_ids:
                continue
            thread_id = s.get("thread_id") or s.get("id")
            approval = CODEX_APP.pending_approval_for_thread(thread_id)
            active = bool(s.get("activeTurn")) or s.get("status") in ("active", "running")
            caps = ["input", "interrupt", "attachments", "replay", "follow"]
            if approval:
                caps.append("approve")
            pub = CODEX_APP._approval_public(approval) if approval else None
            if pub:
                # A1:疊上統一欄位(session_id/provider/kind/status…)。options
                # 相容期保留記憶體版 — 現行 app 以 style=="deny" 判拒絕鍵;
                # method/thread_id 為 codex 專屬欄位,照舊(A4 收斂)。
                drow = _approval_get_row(str(pub.get("id") or ""))
                if drow:
                    pub = {**drow,
                           **{k: pub[k] for k in ("method", "thread_id") if k in pub},
                           "options": pub.get("options") or drow.get("options")}
            # thread-store 寫入鎖:清單這一層也要看得出來,不然 app 只能在
            # 使用者按下送出、吃到 409 之後才知道這條進不去。
            lock = CODEX_APP.thread_lock_info(thread_id)
            meta = {"approval": pub, "locked": bool(lock)}
            if lock:
                meta["lock"] = lock
                _codex_lock_recheck(thread_id)
            out.append({"id": f"codex:{thread_id}", "provider": "codex",
                        "title": s.get("name") or "codex", "subtitle": s.get("workdir"),
                        "status": "waiting_approval" if approval else ("running" if active else "idle"),
                        "last_event_at": s.get("lastEventAt"),
                        "capabilities": caps,
                        "locked": bool(lock),
                        "meta": meta})
    except Exception as e:  # noqa: BLE001
        # 不再無聲吞錯:codex app-server 掛掉時 CX 區直接消失、log 零痕跡,
        # 「CX 全空」查不到原因(2026-07-10 ChatGPT.app 併購式更新事故)。
        _log_event("v2_codex_list_failed", error=type(e).__name__,
                   error_message=str(e)[:200])
        degraded.append("codex")
    # S4:openclaw sessions(SPEC §5)。未配置 → 整段缺席(零影響現有使用者);
    # 配置了但 gateway 掛 → 標 degraded,照 codex 同款不無聲吞錯。
    if OPENCLAW.configured():
        try:
            res = await OPENCLAW.call("sessions.list", {"limit": 20}, timeout=10.0)
            # 待審中的 session 要看得出來(同 hermes/codex 線的 §7-5):
            # 沒有這個,exec 審批一來 app 只看到 idle,使用者完全不知道在等他。
            oc_pending = _openclaw_pending_by_session()
            for row in (res or {}).get("sessions", [])[:20]:
                v2 = openclaw_provider.session_v2_row(row)
                pend = oc_pending.get(v2["id"])
                if pend:
                    v2["status"] = "waiting_approval"
                    v2["meta"] = {**(v2.get("meta") or {}), "approval": pend}
                out.append(v2)
        except Exception as e:  # noqa: BLE001
            _log_event("v2_openclaw_list_failed", error=type(e).__name__,
                       error_message=str(e)[:200])
            degraded.append("openclaw")
    if provider:
        out = [s for s in out if s["provider"] == provider]
    if status:
        out = [s for s in out if s["status"] == status]
    # degraded_providers:清單為空 ≠ 沒有 session,可能是 provider 暫時掛了。
    # 舊 app 忽略新欄位,向後相容;新 app 可據此顯示「清單暫時無法取得」。
    return {"sessions": out, "degraded_providers": degraded}


def _approval_bool_from_body(body: dict) -> bool:
    if "approve" in body:
        return bool(body.get("approve"))
    raw = str(body.get("decision") or body.get("status") or body.get("action") or "").strip().lower()
    if raw in ("approve", "approved", "accept", "accepted", "allow", "yes", "true"):
        return True
    if raw in ("reject", "rejected", "deny", "denied", "decline", "cancel", "no", "false"):
        return False
    raise HTTPException(status_code=400, detail="approve boolean or decision required")


def _codex_thread_from_v2_session_id(session_id: str) -> str:
    if session_id.startswith("codex:"):
        return session_id.split(":", 1)[1]
    if session_id.startswith("delegation:"):
        row = _delegation_get(session_id.split(":", 1)[1])
        if not row:
            raise HTTPException(status_code=404, detail="unknown delegation")
        d = dict(row)
        if d.get("provider") != "codex":
            raise HTTPException(status_code=400, detail="session is not a Codex session")
        thread_id = d.get("codex_thread_id") or d.get("provider_session_id") or ""
        if not thread_id:
            raise HTTPException(status_code=409, detail="delegation has no Codex thread")
        return thread_id
    raise HTTPException(status_code=400, detail="unsupported session id")


@app.post("/app/v2/sessions/{session_id}/approve")
async def v2_session_approve(session_id: str, request: Request):
    """統一路由 approve(契約 §4.4):cx=app-server 決議、cc=TUI 鍵(body
    {key},即 approval/prompt 的 option key)、hermes=Approval Center 決議
    (body {approval_id, approve})。"""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("v2_session_approve", _exc, expected=True)
        body = {}
    # A1(spec §3.3):統一 body {approval_id, key}(approve bool 相容糖)—
    # 三 provider 一律轉呼 _approval_decide_core;三種舊 body({key}/{approve}/
    # {approval_id})走下方原分支,相容期照收(A4 刪)。
    uni_aid = str(body.get("approval_id") or "").strip()
    if uni_aid and ("key" in body or "approve" in body or "decision" in body):
        result = await _approval_decide_core(uni_aid, body)
        return {"ok": True, "session_id": session_id, **result}
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        key = str(body.get("key") or "").strip()
        if not key:
            raise http_err(400, "KEY_REQUIRED",
                           "cc approve 需要 key(approval 卡/prompt 的 option key)")
        res = await _cc_key_core(src[1], key)
        return {"ok": True, "session_id": session_id, **res}
    if src[0] == "hp":
        aid = str(body.get("approval_id") or "").strip()
        if not aid:
            raise http_err(400, "APPROVAL_ID_REQUIRED",
                           "hermes approve 需要 approval_id(approval 卡或 GET /app/v1/approvals)")
        result = await _approval_decide_core(aid, body)
        return {"ok": True, "session_id": session_id, **result}
    if src[0] == "oc":
        # openclaw:exec/plugin 審批(gateway `*.approval.resolve`)。
        # body {approval_id, key}(key ∈ allow-once|allow-always|deny);
        # {approve: bool} 走同一條相容糖。
        aid = str(body.get("approval_id") or "").strip()
        if not aid:
            raise http_err(400, "APPROVAL_ID_REQUIRED",
                           "openclaw approve 需要 approval_id(approval 卡或 GET /app/v1/approvals)")
        result = await _approval_decide_core(aid, body)
        return {"ok": True, "session_id": session_id, **result}
    approved = _approval_bool_from_body(body)
    for_session = bool(body.get("for_session") or body.get("approve_for_session") or
                       body.get("remember"))
    thread_id = src[1]
    try:
        result = await CODEX_APP.decide_thread_approval(thread_id, approved,
                                                        for_session=for_session)
        return {"ok": True, "session_id": session_id, "thread_id": thread_id, **result}
    except CodexAppServerError as e:
        if e.code == 404:
            raise http_err(409, "APPROVAL_NOT_PENDING",
                           "no pending Codex approval for this session")
        _codex_http_error(e)


@app.post("/app/v2/sessions/{session_id}/input")
async def v2_session_input(session_id: str, request: Request):
    """統一路由 input(契約 §4.4):cc=tmux bracketed paste、cx=turn/start、
    hermes=fire-and-forget 回合(回覆走 S3 卡片事件流,不在此串流)。
    body {content|text, attachments?, client_id?}。"""
    _check_auth(request)
    body = await _json_body(request)
    src = _v2_card_source(session_id)
    # 戶政:任何 turn 輸入 = 活著的證據(active⇄idle 的 idle 判定基準)。
    _registry_call_safe("touch", session_id)
    if src[0] == "cx" and not session_id.startswith("codex:"):
        _registry_call_safe("touch", f"codex:{src[1]}")
    if src[0] == "cc":
        res = await _cc_input_core(src[1], body)
        return {"session_id": session_id, **res}
    if src[0] == "oc":
        return await _oc_input_core(src[1], session_id, body)
    if src[0] == "cx":
        content = (body.get("content") or body.get("text") or "").strip()
        attachments = body.get("attachments") or []
        if not content and not attachments:
            raise HTTPException(status_code=400, detail="empty")
        items = await _codex_input_items(content, attachments)
        text = _codex_user_input_text(items)
        client_id = body.get("client_id")
        # 與 v1 同一套冪等閘門(排隊層拿掉 409 → 重試 = 保證重複執行)。
        entry, prior = await _cx_input_claim(src[1], client_id)
        if prior is not None:
            return {"ok": True, "session_id": session_id, "accepted": True,
                    "duplicate": True, **await _cx_input_replay(prior)}
        try:
            if CODEX_APP.is_active(src[1]):    # 忙碌 → 入佇列(同 v1,不回 4xx)
                depth = CODEX_APP.enqueue_input(src[1], items, client_id=client_id,
                                                text=text)
                _cx_feed_input_accepted(src[1], client_id, text, attachments,
                                        typed_text=text, create_if_missing=True,
                                        queued=True)
                # `queued`(bool)= app `StudioBridgeV2.InputAck` 真正解的欄位,
                # persona 的 v2 input 早就有回;CX 少了它 → app 永遠當成沒排隊。
                res = {"delivery": "queued", "queued": True, "queue_depth": depth}
                _cx_input_settle(entry, res)
                return {"ok": True, "session_id": session_id, "accepted": True, **res}
            try:
                await CODEX_APP.start_turn(src[1], items, client_id=client_id)
            except CodexAppServerError as e:
                _codex_http_error(e)
            _cx_feed_input_accepted(src[1], client_id, text, attachments,
                                    typed_text=text, create_if_missing=True)
            res = {"delivery": "accepted", "queued": False}
            _cx_input_settle(entry, res)
            return {"ok": True, "session_id": session_id, "accepted": True, **res}
        except BaseException:
            _cx_input_release(entry)
            raise
    return await _v2_persona_input(src[1], session_id, body, request)


async def _v2_persona_input(session: str, session_id: str, body: dict,
                            request: Request):
    """hermes input:與 v1 POST /app/v1/messages 同一套前置/冪等/回合機器,
    差別只在回應——不開 SSE,立即回 {accepted};deltas/收尾全走 S3 卡片
    事件流(進行中裝置與其他裝置看到同一份)。"""
    content = (body.get("content") or body.get("text") or "").strip()
    attachments = body.get("attachments") or []
    if not content and not attachments:
        raise HTTPException(status_code=400, detail="empty")
    client_id = body.get("client_id")
    cid = "appmsg-" + uuid.uuid4().hex[:20]
    turn_started = time.monotonic()
    common_log = {
        "cid": cid,
        "session": session,
        "client_id_hash": _short_hash(client_id),
        "client": _client_host(request),
        "dry_run": False,
        "input_chars": len(content),
        **_attachment_stats(attachments),
        "via": "v2_input",
    }
    _log_event("app_turn_received", **common_log)

    inflight_entry = None
    if client_id:
        # 冪等(與 v1 同款):已完成 → replayed;進行中 → in_flight,不重跑。
        prior = _canon_reply_for_client(session, client_id)
        if prior is not None:
            return {"ok": True, "session_id": session_id, "replayed": True}
        async with _APP_TURN_INFLIGHT_LOCK:
            _now = time.monotonic()
            for k in [k for k, e in _APP_TURN_INFLIGHT.items()
                      if _now - e["ts"] > _APP_TURN_INFLIGHT_TTL]:
                _APP_TURN_INFLIGHT.pop(k, None)
            if _APP_TURN_INFLIGHT.get((session, client_id)) is not None:
                return {"ok": True, "session_id": session_id, "in_flight": True}
            inflight_entry = {"ts": _now, "wall": time.time(), "task": None, "state": None}
            _APP_TURN_INFLIGHT[(session, client_id)] = inflight_entry

    try:
        content, att_meta, prompt = await _persona_prepare_turn(
            session, content, attachments, stt_lang=str(body.get("stt_lang") or ""))
        acp_session = await POOL.get(session, home_for(session))
        queued = acp_session.is_busy()
        user_mid, canonical_user_ok = _canon_add_retry(session, "user", content,
                                                       att_meta, client_id=client_id)
        _hp_cards_turn_start(session, cid, user_mid, content, att_meta)
        task, state, _q = _persona_launch_turn(session, prompt, client_id, common_log,
                                               turn_started, canonical_user_ok, cid,
                                               user_text=content, user_mid=user_mid)
    except BaseException:
        # claim → launch 之間失敗(STT/附件前置、ACP spawn…)必須釋放 claim,
        # 否則同 client_id 的重試會被 in_flight 擋整整 600s TTL(issue #9)。
        if inflight_entry is not None and \
                _APP_TURN_INFLIGHT.get((session, client_id)) is inflight_entry:
            _APP_TURN_INFLIGHT.pop((session, client_id), None)
            _log_event("app_turn_inflight_released", session=session,
                       client_id_hash=_short_hash(client_id),
                       reason="prelaunch_error", via="v2_input")
        raise
    if inflight_entry is not None:
        inflight_entry["task"] = task
        inflight_entry["state"] = state
    # `content` = 實收 user turn 正文(語音附件的 STT transcript 已由
    # _persona_prepare_turn 折入)。app 靠它把樂觀語音泡泡「🎤 語音訊息 ·
    # 辨識中…」原地替換成辨識文字(feat/stt-transcript-echo);message_id
    # 仍是回顯卡對位鍵。舊 app 忽略多的欄位,無害。
    return {"ok": True, "session_id": session_id, "accepted": True,
            "queued": queued, "message_id": user_mid, "content": content}


@app.post("/app/v2/sessions/{session_id}/interrupt")
async def v2_session_interrupt(session_id: str, request: Request):
    """統一路由 interrupt(契約 §4.4):cc=Esc 驗證重試、cx=turn/interrupt、
    hermes=ACP cancel 驗證重試。無活躍 turn 一律 409。"""
    _check_auth(request)
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        res = await _cc_interrupt_core(src[1])
        return {"session_id": session_id, **res}
    if src[0] == "hp":
        res = await _persona_interrupt_core(src[1])
        return {"session_id": session_id, **res}
    if src[0] == "oc":
        # SPEC §4:chat.abort {sessionKey}。v2 契約「無活躍 turn 一律 409」,
        # 忙碌判定雙軌:digest.busy(lifecycle start 之後)OR gateway
        # sessions.list 的 hasActiveRun(send 剛排隊、lifecycle 未 start 的
        # 窗口 —— 使用者送出後馬上按停止就落在這裡,實測踩過)。
        d = _OC_CARD_DIGESTS.get(src[1])
        busy = bool(d is not None and d.busy)
        if not busy:
            try:
                res = await OPENCLAW.call("sessions.list", {"limit": 100},
                                          timeout=8.0)
                busy = any(str(r.get("key") or "") == src[1] and r.get("hasActiveRun")
                           for r in (res or {}).get("sessions", []))
            except Exception as _exc:  # noqa: BLE001
                # 生成高峰時 gateway 事件圈會塞住,RPC 逾時 ≠ 不忙(實測:
                # 長文生成中連 sessions.list 都答不了)。查不到就當忙,
                # 盡力送 abort —— gateway 對無活躍 run 的 abort 無害。
                _log_exc("oc_interrupt_active_probe", _exc, expected=True)
                busy = True
        if not busy:
            raise http_err(409, "NO_ACTIVE_TURN", "no active OpenClaw run")
        try:
            await OPENCLAW.call("chat.abort", {"sessionKey": src[1]})
        except openclaw_provider.OpenClawError as e:
            _oc_http_error(e)
        return {"ok": True, "session_id": session_id, "interrupted": True}
    try:
        await CODEX_APP.interrupt_turn(src[1])
    except CodexAppServerError as e:
        if "no active" in str(e).lower():
            raise http_err(409, "NO_ACTIVE_TURN", "no active Codex turn")
        _codex_http_error(e)
    return {"ok": True, "session_id": session_id, "interrupted": True}


@app.post("/app/v2/sessions/{session_id}/key")
async def v2_session_key(session_id: str, request: Request):
    """統一路由 key(契約 §4.4,capability keys):僅 claude_code。"""
    _check_auth(request)
    body = await request.json()
    src = _v2_card_source(session_id)
    if src[0] != "cc":
        raise http_err(400, "UNSUPPORTED_PROVIDER",
                       "key 僅支援 claude_code(capability: keys)")
    raw = str(body.get("key") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key required")
    res = await _cc_key_core(src[1], raw)
    return {"session_id": session_id, **res}


# ─────────── Phase 0 S1：CC 卡片事件流 / snapshot（契約 §1-§3, issue #15）───────────
# digest 本體在 carddigest.py（單一模組,S2 codex / S3 persona / 衛星終端共用）;
# 這裡只做 CC jsonl 的 tail-follow 接線與兩個 v2 端點。

_CC_CARD_STORES: dict = {}      # name -> carddigest.SessionCardStore
_CC_CARD_FOLLOWERS: dict = {}   # name -> asyncio.Task
_CC_CARD_SEED_LINES = 200       # 冷載種子:最新 jsonl 的尾端行數
_CC_QUEUED_GRACE_SECS = 120     # input 送達後,「已排入佇列」狀態最長維持秒數


def _cc_card_store(name: str):
    store = _CC_CARD_STORES.get(name)
    if store is None:
        store = _CC_CARD_STORES[name] = carddigest.SessionCardStore()
        store.media_session_id = f"claude_code:{name}"
    return store


def _cc_cards_feed_approval(name: str, record: dict, resolved: str = "") -> None:
    """A3(APPROVAL_HUB_SPEC §4/§6):approval 建立/決議 → 對應 CC session 的
    approval 卡。同 `_cx_cards_feed_approval` 的形狀,只是 CC 沒有獨立
    digest 物件 —— 直接對 `SessionCardStore`(掛了 `ApprovalCardMixin`)呼叫。
    只餵「已有人訂閱過」的 store(`_CC_CARD_STORES` 有登記);沒人在看的
    session 不必為了一張卡片就建 store。"""
    store = _CC_CARD_STORES.get(name)
    if not store:
        return
    if resolved:
        store.resolve_approval(record, resolved)
    else:
        store.handle_approval(record)


def _cc_feed_input_accepted(name: str, client_id: str | None, text: str,
                            attachments=None, typed_text: str | None = None) -> None:
    store = _cc_card_store(name)
    card = carddigest.make_input_accepted_card(
        "claude_code", client_id, text,
        # 附件已在 _cc_input_core 帶好 path/available(比照人格);不再走
        # _input_attachment_summary 剝掉 path,否則 app 拿不到來源 →「來源已失效」。
        attachments=attachments,
        typed_text=typed_text)
    # 反向合併:paste 驗證等待期間 follower 已把 jsonl 回顯 digest 成卡的話,
    # 併進那張、不開第二張(附件送出幾乎必中的雙泡根因,見 carddigest)。
    card = carddigest.absorb_echo_into_accepted(store, card)
    store.upsert_card(card)


def _cc_card_uid(d: dict, jsonl_path: str, lineno: int) -> str:
    u = d.get("uuid")
    if u:
        return str(u)[:32]
    fh = hashlib.md5((jsonl_path or "").encode()).hexdigest()[:6]
    return f"{fh}-L{lineno}"


# ─────────── CC token 級串流(pane-diff 草稿卡,旗標 CC_TOKEN_STREAM=1)───────
# CC 是唯一沒有即時打字感的 provider:jsonl 只在訊息塊「完成」時落一行,
# 助手回覆整段彈出、還晚 ~1-1.5s(1Hz tail)。Codex/persona 早就用
# card.upsert rev++ final:false 在同一張卡上流 token,app 已會渲染。
#
# 做法(A 案,pane-diff):有訂閱者且 turn 忙碌時,把 pane 擷取頻率提到
# ~250ms,前後兩張 capture 做「附加文字」diff,萃取新增的助手 prose,
# 以 final:false 草稿卡漸進 upsert;jsonl 的正典訊息行到達時,正典卡
# **接管草稿卡的 id**(同 id、rev++、final:true → app 原位換文,零重複)。
#
# 不走 B 案(--output-format stream-json):該旗標只在 `-p` print 模式下有效
# (見 _claude_argv 的 headless 子代理),會整個取代互動式 TUI —— tmux 控制面
# (send-keys 輸入、審批選單、Esc 中斷、shift+tab 模式切換、--remote-control)
# 全部報廢。claude 也沒有「TUI 照跑、另開 token 流」的旗標;hooks 只有
# UserPromptSubmit/Stop,沒有 delta。故 B 案否決。
#
# 保守鐵律:diff 對不上(重繪/rewrap/整屏捲動超出視窗)就跳過該 tick 重定
# 基線,寧可少流一段也不出垃圾;萃取失敗只影響草稿,正典卡永遠會到。

_CC_STREAM_INTERVAL = min(1.0, max(0.1, float(
    os.environ.get("CC_TOKEN_STREAM_INTERVAL", "0.25"))))  # busy 時擷取節奏
_CC_STREAM_TAIL_WINDOW = 240     # 舊內容尾端錨定窗(捲動後重新對齊用)
_CC_STREAM_MAX_TICK = 4000       # 單 tick 附加上限;超過視同重繪,跳過
_CC_STREAM_MAX_DRAFT = 12000     # 草稿文字上限;飽和後停止增長,等正典卡
_CC_STREAM_FINAL_GRACE = 4.0     # turn 結束後等正典卡接管的寬限秒數

# 尾端 UI 雜訊(輸入框上方):spinner/狀態行、快捷鍵列、分隔線。
_CC_STREAM_CHROME_RE = re.compile(
    r"^\s*(?:[✻✽✶✢✳∗＊·✦✧].*|esc to interrupt.*|\? for shortcuts.*"
    r"|⏵⏵.*|[─╌╍]{4,}\s*)$", re.IGNORECASE)
_CC_STREAM_BOX_TOP_RE = re.compile(r"^\s*╭")
# 「⏺ ToolName(args…)」的工具呼叫塊;誤把 prose 當工具只會少流(正典補),
# 反向誤判才會把垃圾塞進草稿 —— 所以規則從寬。
_CC_STREAM_TOOL_RE = re.compile(r"^[A-Za-z][\w.:-]{0,60}\(")
_CC_STREAM_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # 防禦性(capture -p 本不帶)


def _cc_token_stream_enabled() -> bool:
    """旗標每次呼叫讀 env:預設 OFF,merge 零風險;善彰可在 plist 加
    CC_TOKEN_STREAM=1 後重啟啟用(per-restart 開關)。"""
    return str(os.environ.get("CC_TOKEN_STREAM", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _cc_stream_content(pane: str):
    """pane 擷取 → 對話區純文字;認不出版面(找不到輸入框)回 None,
    呼叫端跳過該 tick(保守優先)。"""
    if not pane:
        return None
    pane = _CC_STREAM_ANSI_RE.sub("", pane)
    lines = [ln.rstrip() for ln in pane.splitlines()]
    box_top = None
    for i, ln in enumerate(lines):
        if _CC_STREAM_BOX_TOP_RE.match(ln):
            box_top = i                     # 取最後一個 ╭ ── 即最下方的輸入框
    if box_top is None:
        return None
    content = lines[:box_top]
    # 尾端往上剝:空行、spinner/狀態行、busy 計時行(輸入框上那圈雜訊,
    # 每張 capture 都在變,不剝乾淨 diff 永遠對不上)。
    while content:
        tail = content[-1]
        if not tail.strip() or _CC_STREAM_CHROME_RE.match(tail) \
                or _CC_BUSY_RE.search(tail):
            content.pop()
            continue
        break
    return "\n".join(content)


def _cc_stream_diff_append(prev: str, new: str):
    """回 (appended, ok)。ok=False = 對不上(重繪/rewrap),呼叫端重定基線。
    附加型變化(token 打字、換行)→ new 以 prev 開頭;pane 捲動把頂部擠掉
    → 用 prev 尾端錨定窗在 new 裡重新對齊。"""
    if new == prev:
        return "", True
    if new.startswith(prev):
        return new[len(prev):], True
    # 錨定窗由大到小重試:pane 很短(prev 整串 < 大窗)時,大窗等於整串,
    # 頂部一捲就永遠對不上 —— 縮窗到還留在畫面上的尾端即可重新對齊。
    for win in (_CC_STREAM_TAIL_WINDOW, _CC_STREAM_TAIL_WINDOW // 2, 64):
        tail = prev[-win:]
        if not tail:
            continue
        i = new.rfind(tail)
        if i != -1:
            return new[i + len(tail):], True
    return "", False


class CCPaneStream:
    """每 session 一份的 pane-diff 萃取狀態機(純邏輯,無 I/O,可單測)。

    feed(pane) → (draft_text, changed):維護基線、辨識「⏺ prose / ⏺ Tool(...) /
    ⎿ 結果 / > 使用者回顯」塊別,只累積助手 prose 進草稿。"""

    def __init__(self):
        self.baseline = None      # 上一張 content 文字(None = 未定基線)
        self.in_prose = False     # 目前塊是否為助手 prose
        self.draft = ""           # 累積草稿
        self.draft_id = ""        # 開著的草稿卡 id("" = 無)
        self.draft_turn = ""      # 草稿所屬 turn
        self.final_deadline = 0.0 # turn 結束後的定稿期限(0 = 未武裝)
        self._seq = 0             # 草稿卡 id 流水號(跨 turn 不重複)

    # ── 生命週期 ──
    def begin_turn(self):
        self.draft = ""
        self.draft_id = ""
        self.draft_turn = ""
        self.in_prose = False
        self.final_deadline = 0.0

    def resolve_draft(self):
        """正典卡接管(或定稿)後關閉草稿。in_prose 一併關:同塊殘尾不再
        另起片段草稿,新 prose 要等下一個 ⏺ 標記。"""
        self.draft = ""
        self.draft_id = ""
        self.draft_turn = ""
        self.in_prose = False
        self.final_deadline = 0.0

    def next_draft_id(self, turn_id: str) -> str:
        self._seq += 1
        self.draft_id = f"card-cc-stream-{self._seq}-{turn_id or 'noturn'}"
        self.draft_turn = turn_id
        return self.draft_id

    # ── 萃取 ──
    def feed(self, pane: str):
        before = self.draft
        content = _cc_stream_content(pane)
        if content is None:
            return self.draft, False          # 版面認不出 → 跳過
        if self.baseline is None:
            self.baseline = content
            return self.draft, False
        appended, ok = _cc_stream_diff_append(self.baseline, content)
        self.baseline = content
        if not ok or not appended:
            return self.draft, False          # 重繪 → 重定基線,不出垃圾
        if len(appended) > _CC_STREAM_MAX_TICK:
            return self.draft, False          # 不合理暴增,多半是重繪
        self._consume(appended)
        return self.draft, self.draft != before

    def _consume(self, appended: str):
        parts = appended.split("\n")
        # parts[0] 是「同一行長出來的字」(token 打字最常見),接在目前塊上。
        if parts[0] and self.in_prose:
            self._grow(parts[0], newline=False)
        for ln in parts[1:]:
            self._line(ln)

    def _line(self, ln: str):
        t = ln.strip()
        if not t:
            return          # 空行:不動草稿(段落以換行 join 呈現即可)
        ch = t[0]
        if ch in "⏺●":
            rest = t[1:].strip()
            if _CC_STREAM_TOOL_RE.match(rest):
                self.in_prose = False          # 工具呼叫塊
            else:
                self.in_prose = True
                if rest:
                    self._grow(rest, newline=True)
        elif ch in "⎿>│╭╰❯":
            self.in_prose = False              # 工具結果/使用者回顯/框線
        elif _CC_STREAM_CHROME_RE.match(ln) or _CC_BUSY_RE.search(ln):
            return                             # 漏網雜訊:忽略,不翻塊別
        else:
            if self.in_prose:
                self._grow(t, newline=True)

    def _grow(self, piece: str, newline: bool):
        if len(self.draft) >= _CC_STREAM_MAX_DRAFT:
            return                             # 飽和:停止增長,等正典卡
        if newline and self.draft and not self.draft.endswith("\n"):
            piece = "\n" + piece
        self.draft = (self.draft + piece)[:_CC_STREAM_MAX_DRAFT]


def _cc_stream_state(store) -> CCPaneStream:
    st = getattr(store, "cc_stream", None)
    if st is None:
        st = store.cc_stream = CCPaneStream()
    return st


def _cc_stream_upsert_draft(store, st: CCPaneStream, draft: str):
    if not st.draft_id:
        st.next_draft_id(store.turn_id)
    st.draft = draft
    store.upsert_card(carddigest.make_card(
        st.draft_id, st.draft_turn or store.turn_id, "assistant", "markdown",
        {"text": draft, "fallback_text": draft, "origin": "pane.stream"},
        final=False))


def _cc_stream_reconcile(store, card: dict) -> dict:
    """jsonl 正典助手 markdown 卡落地 → 接管開著的草稿卡 id。
    app 端同 id、rev++、final:true → 原位換成正典全文,草稿無痕退場。
    只碰「還不在庫裡」的新卡;重放/rev 遞增路徑原樣通過。"""
    st = getattr(store, "cc_stream", None)
    if not st or not st.draft_id:
        return card
    if (isinstance(card, dict) and card.get("role") == "assistant"
            and card.get("kind") == "markdown"
            and card.get("id") not in getattr(store, "cards", {})):
        out = dict(card)
        out["id"] = st.draft_id
        st.resolve_draft()
        return out
    return card


def _cc_stream_finalize(store, st: CCPaneStream):
    """正典卡遲遲不來(被 digest 濾掉等)→ 草稿自行定稿,不讓 app 永遠
    掛著 final:false 的「打字中」。文字以庫裡那張卡為準(st.draft 只是
    extractor 的工作副本)。"""
    if st.draft_id:
        prev = (getattr(store, "cards", {}) or {}).get(st.draft_id) or {}
        text = (st.draft or (prev.get("body") or {}).get("text") or "").strip()
        if text:
            store.upsert_card(carddigest.make_card(
                st.draft_id, st.draft_turn or store.turn_id, "assistant",
                "markdown",
                {"text": text, "fallback_text": text,
                 "origin": "pane.stream"}))
    st.resolve_draft()


def _cc_stream_on_turn_begin(store):
    st = _cc_stream_state(store)
    if st.draft_id:
        _cc_stream_finalize(store, st)   # 上一 turn 的殘稿先定稿再歸零
    st.begin_turn()


def _cc_stream_on_turn_end(store):
    st = getattr(store, "cc_stream", None)
    if st and st.draft_id:
        st.final_deadline = time.time() + _CC_STREAM_FINAL_GRACE


def _cc_stream_finalize_expired(store):
    st = getattr(store, "cc_stream", None)
    if st and st.draft_id and st.final_deadline \
            and time.time() > st.final_deadline:
        _cc_stream_finalize(store, st)


async def _cc_stream_subticks(name: str, store):
    """busy + 有訂閱者時的快節奏子迴圈:總長約 1s(取代外圈那次 sleep),
    每 _CC_STREAM_INTERVAL 抓一張新鮮 pane、diff、upsert 草稿。
    任何一 tick 例外只記 log 並提前收工 —— 絕不外洩打死 follower。"""
    ticks = max(1, int(round(1.0 / _CC_STREAM_INTERVAL)))
    for _ in range(ticks):
        await asyncio.sleep(_CC_STREAM_INTERVAL)
        if store.subscribers <= 0:
            return                         # 訂閱者中途走光 → 立刻停
        try:
            st = _cc_stream_state(store)
            pane = await _cc_capture_pane_fresh(name)
            draft, changed = st.feed(pane)
            if changed and draft.strip():
                _cc_stream_upsert_draft(store, st, draft)
        except Exception as e:  # noqa: BLE001
            _log_event("cc_stream_tick_error", session=name,
                       error=type(e).__name__, error_message=str(e)[:160])
            return


def _cc_image_sink(session_id: str):
    """carddigest 的 base64 圖入庫回呼:decode → media store capture_bytes →
    回 {media_id, download_url, filename, mime} 給 attachment 卡。單張失敗回
    None(carddigest 端不斷流)。MCP 直回圖(無檔案路徑)因此也進得了 Pocket。"""
    import base64 as _b64
    def sink(mime: str, b64data: str):
        try:
            data = _b64.b64decode(b64data)
            ext = {"image/png": ".png", "image/jpeg": ".jpg",
                   "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".png")
            item = _media_store().capture_bytes(
                session_id, data,
                filename=f"cc-image{ext}", mime=mime, kind="image")
            out = _media_wire_item(item)
            return {"media_id": out.get("media_id"),
                    "download_url": out.get("download_url"),
                    "filename": out.get("filename"), "mime": mime}
        except Exception as e:  # noqa: BLE001
            _log_event("cc_image_sink_failed", error=type(e).__name__,
                       error_message=str(e)[:120])
            return None
    return sink


def _cc_digest_lines(store, lines, jsonl_path: str, start_lineno: int) -> int:
    """把 jsonl 行灌進卡片庫;回傳新增/更新的卡數。順手維護人話 label 素材。"""
    n = 0
    media_payloads = []
    for off, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_cc_digest_lines", _exc, expected=True)
            continue
        media_payloads.append(d)
        uid = _cc_card_uid(d, jsonl_path, start_lineno + off)
        for card in carddigest.cc_event_to_cards(
                d, uid, turn_id=store.turn_id,
                image_sink=_cc_image_sink(getattr(store, "media_session_id", "") or "cc")):
            card = carddigest.merge_input_accepted_echo(store, card)
            # CC_TOKEN_STREAM:正典助手卡接管 pane-diff 草稿卡的 id(原位換文)。
            card = _cc_stream_reconcile(store, card)
            store.upsert_card(card)
            n += 1
            if card["kind"] == "tool_call":
                store.last_tool = card["body"].get("tool") or ""
            elif card["kind"] == "markdown":
                store.saw_output = True
                store.last_tool = ""
    if media_payloads:
        _schedule_media_capture(
            getattr(store, "media_session_id", ""), media_payloads
        )
    return n


async def _cc_card_seed(store, name: str, workdir: str):
    """冷載種子(冪等):最新 jsonl 尾端 → 卡片庫,並設好 tail 游標。"""
    if store.seeded:
        return
    store.seeded = True
    jsonl = await _cc_session_jsonl(name, workdir)
    if not jsonl or not os.path.exists(jsonl):
        return
    try:
        text = await asyncio.to_thread(
            lambda: open(jsonl, encoding="utf-8", errors="replace").read())
    except Exception as e:  # noqa: BLE001
        _log_event("cc_card_seed_error", session=name, error=str(e)[:200])
        return
    lines = text.splitlines()
    store.tail_partial = ""
    if text and not text.endswith("\n") and lines:
        store.tail_partial = lines.pop()
    seed = lines[-_CC_CARD_SEED_LINES:]
    _cc_digest_lines(store, seed, jsonl, len(lines) - len(seed))
    store.tail_file = jsonl
    store.tail_pos = len(text.encode("utf-8", errors="replace"))
    store.tail_lineno = len(lines)
    # A3 冷載:pending approval 不在 jsonl 裡(它活在 watcher + DB),seed 時
    # 從 DB 對回 —— 與 codex seed 補 pending_approval_for_thread 同一精神。
    active = _CC_APPROVAL_ACTIVE.get(name)
    if active:
        try:
            rec = _approval_get_row(str(active.get("aid") or ""))
            if rec and rec.get("status") == "pending":
                _cc_cards_feed_approval(name, rec)
        except Exception as e:  # noqa: BLE001
            _log_event("cc_cards_feed_error", error=str(e)[:160])


async def _cc_card_follower(name: str, workdir: str):
    """每秒 tail 該 session 的 jsonl → digest 進卡片庫;有訂閱者時再巡
    busy/mode/prompt(tmux capture 有成本)發 session.status / turn 事件。"""
    store = _cc_card_store(name)
    await _cc_card_seed(store, name, workdir)
    prev_busy = None
    while True:
        streamed = False
        try:
            cur = await _cc_session_jsonl(name, workdir)
            if cur != store.tail_file:               # session 換了新 jsonl
                store.tail_file, store.tail_pos, store.tail_lineno = cur or "", 0, 0
                store.tail_partial = ""
            j = store.tail_file
            if j and os.path.exists(j):
                size = os.path.getsize(j)
                if size > store.tail_pos:
                    def _read_new():
                        with open(j, encoding="utf-8", errors="replace") as f:
                            f.seek(store.tail_pos)
                            return f.read(), f.tell()
                    new, store.tail_pos = await asyncio.to_thread(_read_new)
                    chunk = (getattr(store, "tail_partial", "") or "") + new
                    nl = chunk.splitlines()
                    if chunk and not chunk.endswith("\n") and nl:
                        store.tail_partial = nl.pop()
                    else:
                        store.tail_partial = ""
                    _cc_digest_lines(store, nl, j, store.tail_lineno)
                    store.tail_lineno += len(nl)
            if store.subscribers > 0:
                st = await _cc_status_core(name)
                busy = bool(st.get("busy"))
                if busy:
                    store.queued_until = 0.0   # 真忙了 → 排隊寬限交還正常路徑
                # 新訂閱者(開啟/重連時可能在「忙碌中途」接入)→ 強制重發一次當前
                # 狀態,否則 set_status「有變才發」會讓中途接入者停在舊的「待命」,
                # 整段回覆期看起來像沒反應(snapshot 冷載不帶 status)。
                if store.subscribers > getattr(store, "_last_subs", 0):
                    store.status = None
                store._last_subs = store.subscribers
                if prev_busy is not None and busy != prev_busy:
                    if busy:
                        store.turn_id = "turn-" + uuid.uuid4().hex[:12]
                        store.saw_output = False
                        store.last_tool = ""
                        store.push_turn("begin", store.turn_id)
                        if _cc_token_stream_enabled():
                            _cc_stream_on_turn_begin(store)
                    else:
                        store.push_turn("end", store.turn_id)
                        store.turn_id = ""
                        if _cc_token_stream_enabled():
                            _cc_stream_on_turn_end(store)
                prev_busy = busy
                if not busy and time.time() < getattr(store, "queued_until", 0.0):
                    # input 已送達但 session 還沒接手(忙上一輪/思考中):
                    # 不用 idle 蓋掉「已排入佇列」,避免 UI 誤示「待命」死寂。
                    status = {"busy": True, "mode": st.get("mode"),
                              "prompt": st.get("prompt"),
                              "phase": "queued",
                              "label": "已排入佇列,等待接手…"}
                else:
                    label = carddigest.cc_status_label(busy, st.get("prompt"),
                                                       store.last_tool, store.saw_output)
                    status = {"busy": busy, "mode": st.get("mode"),
                              "prompt": st.get("prompt"),
                              "phase": "run" if busy else "idle",
                              "label": label}
                # v1 /ccsessions status 已算好的 usage {used,size} 順手帶進
                # v2 session.status(同形,app ContextUsage decoder 直接吃;
                # 有才帶 —— 缺鍵 = 舊行為,不會把 None 灌給 app)。
                if st.get("usage"):
                    status["usage"] = st["usage"]
                store.set_status(status)
                # CC_TOKEN_STREAM(預設 OFF):busy + 有訂閱者才啟動快節奏
                # pane-diff 子迴圈(約 1s,取代外圈 sleep);idle/無人看零成本。
                if _cc_token_stream_enabled():
                    _cc_stream_finalize_expired(store)
                    if busy:
                        streamed = True
                        await _cc_stream_subticks(name, store)
        except Exception as e:  # noqa: BLE001
            _log_event("cc_card_follower_error", session=name, error=str(e)[:200])
            await asyncio.sleep(2.0)
            continue
        if not streamed:
            await asyncio.sleep(1.0)


def _ensure_cc_card_follower(name: str, workdir: str):
    t = _CC_CARD_FOLLOWERS.get(name)
    if t and not t.done():
        return
    _CC_CARD_FOLLOWERS[name] = asyncio.create_task(_cc_card_follower(name, workdir))


def _oc_safe_session_key(key: str) -> str:
    """OpenClaw sessionKey 防呆:sub-key 與 agent id 同名(`agent:<a>:<a>`)會撞到
    gateway 的 default agent lane —— 一次 chat.send 被同時投到「default lane」與
    「session lane」各跑一個 prompt,兩者搶同一份 session 檔互相 takeover,
    使用者送一則卻雙泡泡、且回覆被吞成空(2026-08-02 rakutai 測試機實測坐實:
    `agent:main:main` 起 2 個 prompt、`agent:main:pocket` 起 1 個)。

    把這種撞名的 sub-key 統一改道到 `pocket`。**在 _v2_card_source 這個唯一入口
    改道** → send / history / abort / 卡片 digest / 推播全走同一個安全 key,
    不會「送到 A 讀 B」對不上。gateway 上原本那條 `:main` session 弃用即可
    (它本來就壞,歷史夾著 takeover 錯誤卡)。"""
    parts = key.split(":")
    if len(parts) == 3 and parts[0] == "agent" and parts[2] == parts[1]:
        return f"{parts[0]}:{parts[1]}:pocket"
    return key


def _v2_card_source(session_id: str) -> tuple:
    """v2 session id 路由 → ('cc', name, workdir) / ('cx', thread_id) /
    ('hp', persona)。

    S1 = claude_code:{name}（或裸 CC session 名）；S2 = codex:{thread_id} 與
    delegation:{id}(provider=codex)；S3 = hermes:{persona}。
    """
    if ":" in session_id:
        prov, _, rest = session_id.partition(":")
        if prov == "codex":
            return ("cx", rest)
        if prov == "delegation":
            return ("cx", _codex_thread_from_v2_session_id(session_id))
        if prov == "hermes":
            if rest not in PERSONAS:
                raise http_err(404, "SESSION_NOT_FOUND", "unknown persona")
            return ("hp", rest)
        if prov == "openclaw":
            # S4:rest = 完整 sessionKey(本身含冒號,如 agent:main:main),
            # partition 之後整段原樣保留。未配置 → 404(對外表現同不存在)。
            if not OPENCLAW.configured():
                raise http_err(404, "SESSION_NOT_FOUND", "openclaw not configured")
            if not rest:
                raise http_err(404, "SESSION_NOT_FOUND", "empty openclaw session key")
            return ("oc", _oc_safe_session_key(rest))
        if prov != "claude_code":
            raise http_err(400, "UNSUPPORTED_PROVIDER",
                           f"不支援的 provider: {prov}")
        session_id = rest
    row = next((r for r in _cc_conf_rows() if r[0] == session_id), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    return ("cc", row[0], row[1])


# ─────────── Phase 0 S2:codex 卡片事件流（同兩端點,事件驅動無輪詢）────────

_CX_CARD_DIGESTS: dict = {}     # thread_id -> carddigest.CodexThreadDigest
_CX_CARD_SEED_TURNS = 50        # 冷載種子:thread/turns/list 的 turn 數
_CX_CARD_RESEED_TTL = 8.0       # 已 seed 後的 canonical catch-up 最短間隔


def _cx_cards_feed(method: str, params: dict) -> None:
    """CodexAppServerClient 通知 → 有訂閱過的 thread 餵進 digest。
    digest 只在首次 cards/events 請求時建立,之前的歷史由 seed 補。"""
    tid = str(params.get("threadId") or "")
    # Delta notifications can arrive many times per second. Full item events
    # contain the same attachment/path data without flooding the thread pool.
    if tid and method in {"item/started", "item/completed"}:
        _schedule_media_capture(f"codex:{tid}", params)
    d = _CX_CARD_DIGESTS.get(tid) if tid else None
    if d:
        d.handle(method, params)


def _cx_cards_feed_approval(record: dict, resolved: str = "") -> None:
    """approval 建立/決議 → 對應 thread 的 approval 卡。"""
    d = _CX_CARD_DIGESTS.get(str(record.get("thread_id") or ""))
    if not d:
        return
    d.feed_approval(record, resolved)


def _input_attachment_summary(attachments) -> list[dict]:
    out = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        row = {}
        for key in ("kind", "filename", "mime", "content_type", "size"):
            if a.get(key) not in (None, ""):
                row[key] = a.get(key)
        if row:
            out.append(row)
    return out


def _cx_sync_queue_depth(thread_id: str) -> None:
    """把 CX 佇列深度同步進卡片流 digest,狀態卡才畫得出「已排入佇列」。"""
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        return
    try:
        d.queue_depth = CODEX_APP.pending_count(thread_id)
        d._status()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cx_sync_queue_depth", _exc, expected=True)


def _cx_sync_thread_lock(thread_id: str) -> None:
    """把「被桌面版 Codex 鎖住」同步進卡片流的 session.status。

    app 靠 `session.status.locked` 決定要不要掛 banner / 停用輸入框;鎖放開時
    這裡把它翻回 False,banner 就會自己消失,不必重啟 bridge。"""
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        return
    try:
        info = CODEX_APP.thread_lock_info(thread_id)
        d.locked = bool(info)
        d.lock_reason = (info or {}).get("reason") or ""
        d.lock_message = (info or {}).get("message") or ""
        d._status()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cx_sync_thread_lock", _exc, expected=True)


def _cx_feed_thread_locked(thread_id: str, create_if_missing: bool = False) -> None:
    """thread 被別的 codex app-server 鎖住 → 推一張系統卡進**那條 session** 的
    卡片流,並同步 status.locked。

    為什麼一定要有卡:偵測點多半不是「使用者剛按送出」(warm loop、背景 stderr
    都會走到),只回 409 的話畫面上什麼都不會變 —— 那正是原本「點了沒反應」的
    症狀。

    背景偵測(warm loop / stderr)**不替沒人開過的 thread 憑空建 digest**:那樣
    一次會替 20 條沒人在看的 thread 建出未 seed 的 digest,之後所有 live
    notification 都往裡面灌。使用者真的開這條 session 時,`_cx_card_digest` 會
    補推同一張卡。`create_if_missing` 只給**使用者發起的請求**用(HTTP 打進來
    的那一刻,他確實在等這條 session 的回應)。

    去重以「這一次鎖定事件」(lock.since)為身分,所以重複偵測、或開 session 時
    的補推,都只會有一張卡。"""
    if not thread_id:
        return
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        if not create_if_missing:
            return
        d = _CX_CARD_DIGESTS[thread_id] = carddigest.CodexThreadDigest()
    try:
        info = CODEX_APP.thread_lock_info(thread_id)
        if not info:
            return
        if getattr(d, "lock_card_since", None) != info["since"]:
            d.lock_card_since = info["since"]
            d.store.upsert_card(carddigest.make_card(
                f"card-cx-locked-{d.store.seq}", d.store.turn_id, "system", "text",
                {"text": CX_THREAD_LOCKED_CARD_TEXT,
                 "fallback_text": CX_THREAD_LOCKED_CARD_TEXT,
                 "error_code": "CX_THREAD_LOCKED",
                 "reason": CX_THREAD_LOCKED_REASON}))
        _cx_sync_thread_lock(thread_id)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cx_feed_thread_locked", _exc, expected=True)


def _cx_feed_thread_unlocked(thread_id: str) -> None:
    """鎖放開了 → 翻回未鎖 + 推一張恢復卡。只在真的推過鎖定卡的 session 推,
    否則使用者會看到一張「已釋放」卻從沒看過「被佔用」。"""
    if not thread_id or thread_id not in _CX_CARD_DIGESTS:
        return
    try:
        d = _CX_CARD_DIGESTS[thread_id]
        if getattr(d, "lock_card_since", None) is None:
            _cx_sync_thread_lock(thread_id)
            return
        d.lock_card_since = None
        d.store.upsert_card(carddigest.make_card(
            f"card-cx-unlocked-{d.store.seq}", d.store.turn_id, "system", "text",
            {"text": CX_THREAD_UNLOCKED_CARD_TEXT,
             "fallback_text": CX_THREAD_UNLOCKED_CARD_TEXT}))
        _cx_sync_thread_lock(thread_id)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cx_feed_thread_unlocked", _exc, expected=True)


_CX_LOCK_PROBES: set = set()      # 正在跑的復原探針(thread_id)


def _codex_lock_recheck(thread_id: str) -> None:
    """鎖定中且抑制窗已過 → 背景補一次 resume 探針。

    復原路徑的核心:使用者可能只是坐在 session 裡盯著 banner,沒有再按送出。
    沒有這根探針,`locked` 就只能等下一次 warm loop(要 app 去拉清單)才翻回來。

    兩道剎車缺一不可:
      • 抑制窗(`thread_lock_retry_due`)—— 最多每 N 分鐘一次;
      • in-flight 去重(`_CX_LOCK_PROBES`)—— resume 還沒回來之前窗還沒被推,
        而 app 的狀態輪詢是幾秒一次;少了這道,一次 30s 的逾時就會累積出十幾條
        併發 resume。
    """
    if not thread_id or thread_id in _CX_LOCK_PROBES:
        return
    if not CODEX_APP.thread_lock_retry_due(thread_id):
        return

    async def _probe():
        try:
            await CODEX_APP.ensure_thread_loaded(thread_id)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_codex_lock_recheck", _exc, expected=True)
        finally:
            _CX_LOCK_PROBES.discard(thread_id)

    try:
        task = asyncio.create_task(_probe())
    except RuntimeError:
        return
    _CX_LOCK_PROBES.add(thread_id)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _cx_feed_queue_drop(thread_id: str, item: dict, exc: BaseException) -> None:
    """排隊中的訊息送不出去被丟掉 → 推一張錯誤卡。

    沒有這張卡的話,使用者看到的是泡泡永遠停在「已排入下一輪」,而 bridge 這邊
    早就把它丟了 —— 靜默掉訊息比送出失敗更糟,至少要讓他知道要重送。
    """
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        return
    try:
        text = str((item or {}).get("text") or "").strip()
        preview = (text[:60] + "…") if len(text) > 60 else text
        # 被桌面版 Codex 鎖住是**可行動**的原因,不能只丟一個 exception 名字。
        cause = (f"({type(exc).__name__})"
                 if _codex_thread_lock_conflict(exc) is None
                 else f"({CX_THREAD_LOCKED_MESSAGE})")
        msg = ("⚠️ 排隊中的訊息送不出去,已丟棄,請重送"
               + (f":「{preview}」" if preview else "")
               + cause)
        d.store.upsert_card(carddigest.make_card(
            f"card-cx-queue-drop-{d.store.seq}", d.store.turn_id, "system", "text",
            {"text": msg, "fallback_text": msg}))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cx_feed_queue_drop", _exc, expected=True)


def _cx_feed_input_accepted(thread_id: str, client_id: str | None, text: str,
                            attachments=None, typed_text: str | None = None,
                            create_if_missing: bool = False,
                            queued: bool = False) -> None:
    if not thread_id:
        return
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        if not create_if_missing:
            return
        d = _CX_CARD_DIGESTS[thread_id] = carddigest.CodexThreadDigest()
    card = carddigest.make_input_accepted_card(
        "codex", client_id, text, attachments=_input_attachment_summary(attachments),
        typed_text=typed_text)
    if queued:
        # 排隊中的那則:讓 app 顯示「已排入佇列」而不是「已送達」。
        (card.get("body") or {})["delivery"] = "queued"
        # 只設欄位不發事件的話,狀態列要等下一次巡邏才追上積壓 →
        # 走 _cx_sync_queue_depth 一併 push session.status。
        _cx_sync_queue_depth(thread_id)
    # start_turn 先 await 才走到這裡:live userMessage 事件常在等待期間已把
    # transcript 回顯 digest 成卡(card-cx-<uuid>),accepted 晚到再開一張=
    # 同句兩顆泡泡。CC 同款 race 用 absorb 反向合併,cx 一直沒接上。
    d.store.upsert_card(carddigest.absorb_echo_into_accepted(d.store, card))


async def _cx_seed_card_digest(thread_id: str, d, required: bool = False) -> None:
    """Seed/catch-up from canonical turns without resuming the thread.

    Desktop Codex/VS Code can append turns that this bridge never sees as live
    app-server notifications. A light request-time catch-up keeps v2 cards in
    sync while preserving the no-resume rule that avoids stealing the thread.
    """
    now = time.monotonic()
    if d.seeding:
        return
    if d.seeded and not required and now - d.last_seed_at < _CX_CARD_RESEED_TTL:
        return
    d.seeding = True
    try:
        # 只讀 seed 不做 thread/resume:resume 會「接管」該 thread,若它正被
        # 別的 codex app-server(如 VS Code,thread source=vscode)持有就會卡死
        # 整條 stdio → 之後所有 codex 呼叫一起 hang(XCash 就是這樣空白+送不出)。
        # thread/turns/list 本來就不需 resume 也讀得到(/codexsessions/{id}/history
        # 就是這樣讀的),所以這裡直接列 turns。
        res = await CODEX_APP.call("thread/turns/list", {
            "threadId": thread_id, "limit": _CX_CARD_SEED_TURNS,
            "itemsView": "full", "sortDirection": "desc"}, timeout=45.0)
        turns = list((res or {}).get("data", []))
        turns.reverse()
        await asyncio.to_thread(
            _media_capture_sync, f"codex:{thread_id}", turns
        )
        d.seed_turns(turns, emit_unchanged=required)
        # Seed 期間先收到的 live notification 必須在 canonical 舊→新卡片
        # 落地後重播，否則 live 卡會先佔住 order 尾端，舊歷史反而接在今天後面。
        d.finish_seed()
        rec = CODEX_APP.pending_approval_for_thread(thread_id)
        if rec:
            d.handle_approval(rec)
        d.seeded = True
        d.last_seed_at = now
    except Exception as e:  # noqa: BLE001
        # 即使 canonical seed 失敗，也不能丟掉等待中的 live 事件；讓事件先
        # 正常落地，下一次 request 再重試 seed。
        d.finish_seed()
        if required:
            d.seeded = False   # 下次請求重試 seed
            _log_event("cx_card_seed_error", thread=thread_id[:16],
                       error=str(e)[:200])
            _codex_http_error(e)
        _log_event("cx_card_reseed_error", thread=thread_id[:16],
                   error=str(e)[:200])
    finally:
        d.seeding = False


async def _cx_card_digest(thread_id: str):
    """取得(必要時建立+seed)該 thread 的 digest。先註冊再 seed——seed 期間的
    live 事件與 seed 產同一批卡 id,不會漏也不會雙份；已 seed 的 digest 會
    依 TTL 從 canonical turns/list 補桌面端進度。"""
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        d = _CX_CARD_DIGESTS[thread_id] = carddigest.CodexThreadDigest()
    await _cx_seed_card_digest(thread_id, d, required=not d.seeded)
    # 每次冷載/接流都把鎖狀態同步進 session.status:app 進到 session 的第一
    # 個 snapshot 就帶著 locked,不必等下一次事件。若鎖是背景(warm loop /
    # stderr)先偵測到的,那時沒有 digest 可推卡 —— 在這裡補上,使用者一進來
    # 就看得到原因,而不是一個沒反應的輸入框。
    if CODEX_APP.is_thread_locked(thread_id):
        _cx_feed_thread_locked(thread_id)
    else:
        _cx_sync_thread_lock(thread_id)
    return d


# ─────────── Phase 0 S3:persona 卡片事件流（canonical + live turn 掛鉤）─────

_HP_CARD_DIGESTS: dict = {}     # persona -> carddigest.PersonaDigest
_HP_CARD_FOLLOWERS: dict = {}   # persona -> asyncio.Task
_HP_CARD_SEED_MSGS = 200        # 冷載種子:canonical 訊息數


def _hp_cards_feed_approval(session_id: str, record: dict, resolved: str = "") -> None:
    """A3(APPROVAL_HUB_SPEC §4/§6):hermes persona 的 approval 建立/決議 →
    對應 persona 卡片流的 approval 卡。`session_id` 是統一物件的
    `hermes:{persona}` 形狀(approval_create 落庫時已經這樣寫);只餵
    「已有人訂閱過」的 digest(`_HP_CARD_DIGESTS` 有登記),沒訂閱的
    persona 不必為了一張卡就建 digest。"""
    if not session_id.startswith("hermes:"):
        return
    persona = session_id.split(":", 1)[1]
    d = _HP_CARD_DIGESTS.get(persona)
    if not d:
        return
    if resolved:
        d.resolve_approval(record, resolved)
    else:
        d.handle_approval(record)


def _hp_digest_maybe(session: str):
    """live turn 掛鉤用:有訂閱過才回 digest,否則 None(不建立)。"""
    return _HP_CARD_DIGESTS.get(session)


def _hp_cards_turn_start(session: str, cid: str, user_mid: str | None,
                         content: str, att_meta: list):
    """回合起點掛鉤:user 卡即時出(canonical mid → follower 不重出)+
    turn begin。無訂閱者時 no-op。"""
    _schedule_media_capture(
        f"hermes:{session}", {"content": content, "attachments": att_meta}
    )
    d = _hp_digest_maybe(session)
    if d is None:
        return
    try:
        if user_mid:
            d.message_card({"id": user_mid, "role": "user", "content": content,
                            "attachments": att_meta, "ts": time.time()})
        d.turn_begin(cid)
    except Exception as e:  # noqa: BLE001
        _log_event("hp_card_turn_error", session=session, error=str(e)[:160])


def _hp_merged_messages(session: str, limit: int = 200):
    """人格卡片流的訊息來源:canonical(app 回合)⊕ Telegram(state.db)⊕ cron 晨報,
    與 /app/v1/messages 同一套合併/去重。之前卡片流只讀 canonical,所以你在 TG 講的
    和晨報都不會進 Pocket 人格聊天(卡在最後一次 app 內回合 = 7/6)。改吃這個合併後,
    TG 對話 + 晨報/午報都會出現。"""
    out = _canon_messages(session, limit)
    if session not in PERSONAS:
        return out
    _, home = PERSONAS[session]
    # 雙 role 同文壓重:assistant 是 app 回合雙寫(canonical+state.db)的老
    # 案例;user 是 TG 鏡像 ingest(/internal/v1/mirror/telegram-event)落
    # canonical 後,state.db 掃描會再掃到同一句 —— 同 role+同文+10 分鐘內
    # 視為同一則,壓掉 tg 側。app 端純 TG 舊訊息(canonical 無副本)不受影響。
    canon_recent = [((m.get("ts") or 0), m.get("role"),
                     _dedup_norm(m.get("content") or ""))
                    for m in out if m.get("role") in ("user", "assistant")]
    def _tg_dup(m) -> bool:
        # 完全相等 + 相似度模糊後備(措辭微漂也壓得掉),見 _dual_source_dup。
        return _dual_source_dup(_dedup_norm(m["content"]), m["role"],
                                m["ts"] or 0, canon_recent)
    # 活 turn 檢疫:回合進行中,只扣住「回合起始之後」的 TG assistant(可能是
    # 本回合回覆的進度句副本),等 canonical 總結落地由壓重定奪。起始前的既定
    # 歷史照常放行(見 _session_turn_started_at)。
    _turn_started = _session_turn_started_at(session)
    def _tg_quarantined(m) -> bool:
        return _tg_assistant_in_quarantine(_turn_started, m["role"], m["ts"] or 0)
    try:
        for m in _persona_history(home, limit):
            if _tg_dup(m) or _tg_quarantined(m):
                continue
            out.append({"id": f"tg-{m['ts']}", "role": m["role"], "content": m["content"],
                        "attachments": m.get("attachments") or [], "ts": m["ts"],
                        "status": "done", "source": "telegram"})
        _sync_persona_reports(session, 50)
        out.extend(_report_messages(session, limit))
    except Exception as e:  # noqa: BLE001
        # TG/cron 合併失敗不能拖垮卡片流 → 退回只有 canonical(至少不會壞掉整頁)。
        _log_event("hp_merge_error", session=session, error=str(e)[:200])
    out.sort(key=lambda m: m.get("ts") or 0)
    out = out[-limit:]
    # Sync engine P1:TG 訊息沒有 bridge 端寫入點(Hermes 官方 gateway 直寫
    # state.db),接入點就是這裡的合併掃描 — 卡片 follower 重掃 / v2 events
    # 的 _event_sync_session 都會經過。掃描是重複式的,靠 external_id 冪等,
    # 穩態時 _EVENT_SEEN 快取讓這行零寫入。順帶把 event_log 出生前的舊訊息
    # 回填進日誌(§3.3:任何裝置從 seq=0 重放即可重建歷史)。
    _event_mirror_messages(session, out)
    return out


async def _hp_canon_follower(session: str):
    """canonical 寫入版本喚醒(#28 的 _canon_wait)⊕ state.db stat 版本喚醒
    (#tg-instant-sync 的 _statedb_notify)→ 補掃出卡。known_mids 去重;
    兩條喚醒源任一觸發都立刻重掃,30s 仍是最後保險絲(見 timeout=30.0
    註解)。"""
    d = _HP_CARD_DIGESTS[session]
    ver = _CANON_VER.get(session, 0)
    sver = _STATEDB_VER.get(session, 0)
    while True:
        try:
            await asyncio.wait_for(_canon_or_statedb_wait(session, ver, sver),
                                   timeout=30.0)
        except asyncio.TimeoutError:
            pass
        except Exception as e:  # noqa: BLE001
            _log_event("hp_card_follower_error", session=session,
                       error=str(e)[:200])
            await asyncio.sleep(2.0)
        ver = _CANON_VER.get(session, 0)
        sver = _STATEDB_VER.get(session, 0)
        try:
            # 保險絲重掃:同時把 TG/state.db + cron 晨報合併進來。正常情況下
            # 這一段已經是被 state.db stat watcher 立刻(~0.2-0.3s)喚醒觸發
            # 的,不是等 30s timeout —— watcher 偵測不到才會落回 30s 週期
            # (見 _state_db_watcher_loop 註解:TG/cron 不寫 canonical,不會
            # 觸發 _canon_notify,靠 stat 版本或最終這條 timeout 補上)。
            messages = _hp_merged_messages(session, 80)
            await asyncio.to_thread(
                _media_capture_sync, f"hermes:{session}", messages
            )
            d.seed_messages(messages)
        except Exception as e:  # noqa: BLE001
            _log_event("hp_card_follower_error", session=session,
                       error=str(e)[:200])


def _ensure_hp_card_follower(session: str):
    t = _HP_CARD_FOLLOWERS.get(session)
    if t and not t.done():
        return
    _HP_CARD_FOLLOWERS[session] = asyncio.create_task(_hp_canon_follower(session))


async def _hp_card_digest(session: str):
    d = _HP_CARD_DIGESTS.get(session)
    if d is None:
        d = _HP_CARD_DIGESTS[session] = carddigest.PersonaDigest()
    if not d.seeded:
        d.seeded = True
        try:
            msgs = await asyncio.to_thread(_hp_merged_messages, session,
                                           _HP_CARD_SEED_MSGS)
            await asyncio.to_thread(
                _media_capture_sync, f"hermes:{session}", msgs
            )
            d.seed_messages(msgs)
            # A3 冷載:pending approval 從 DB 對回(與 cc/cx seed 同精神)——
            # 卡誕生時沒人訂閱這條的話,feed 是 no-op,全靠這裡補。
            pend = _hermes_pending_by_session().get(f"hermes:{session}")
            if pend:
                d.handle_approval(pend)
        except Exception as e:  # noqa: BLE001
            d.seeded = False
            _log_event("hp_card_seed_error", session=session, error=str(e)[:200])
            raise HTTPException(status_code=500, detail="persona card seed failed")
    _ensure_hp_card_follower(session)
    return d


# ─────────── Phase 0 S4:openclaw 卡片事件流(gateway 廣播,事件驅動)─────────
# 契約:docs/OPENCLAW_PROVIDER_SPEC.md。傳輸層在 openclaw_provider.py,
# digest 在 carddigest.OpenClawDigest;這裡只做接線:
# - OPENCLAW 單例(未配置 → configured() False,全部端點靜默缺席)
# - 事件泵:gateway 廣播 chat/agent → 有訂閱過的 digest;final 掛推播
# - seed:chat.history(重連後 digest 標記重 seed,SPEC §6-4)

OPENCLAW = openclaw_provider.OpenClawClient(log=_log_event)
_OC_CARD_DIGESTS: dict = {}      # sessionKey -> carddigest.OpenClawDigest

# B(即時推播):openclaw 訊息版本計數 + 等待者,給 /app/v1/messages/events 的
# openclaw 分支用 —— gateway 事件一到就喚醒 SSE、重抓 chat.history 吐新訊息,取代
# app 端慢輪詢(消「送出後等很久」)。同 canonical 的 _CANON_VER/_canon_wait 手法。
_OC_MSG_VER: dict = {}       # sessionKey -> 單調遞增版本
_OC_MSG_WAITERS: dict = {}   # sessionKey -> list[asyncio.Future]


def _oc_msg_notify(key: str) -> None:
    _OC_MSG_VER[key] = _OC_MSG_VER.get(key, 0) + 1
    for fut in _OC_MSG_WAITERS.get(key) or []:
        if not fut.done():
            fut.set_result(True)
    _OC_MSG_WAITERS[key] = []


async def _oc_msg_wait(key: str, seen_ver: int, timeout: float) -> None:
    if _OC_MSG_VER.get(key, 0) != seen_ver:
        return
    fut = asyncio.get_running_loop().create_future()
    _OC_MSG_WAITERS.setdefault(key, []).append(fut)
    try:
        await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        lst = _OC_MSG_WAITERS.get(key)
        if lst and fut in lst:
            lst.remove(fut)
_OC_CARD_SEED_MSGS = 200         # 冷載種子:chat.history 訊息數
_OC_HEARTBEAT_RUNS: "collections.OrderedDict" = collections.OrderedDict()
_OC_HEARTBEAT_RUNS_MAX = 256     # runId → heartbeat 旗標(推播過濾用)
_OC_PUMP_TASK: list = [None]     # 常駐事件泵 task(單例)


def _oc_http_error(e: Exception):
    """OpenClawError → HTTP 錯誤(同 _codex_http_error 精神)。"""
    _log_event("openclaw_provider_error", error=type(e).__name__,
               error_message=str(e)[:200], code=getattr(e, "code", None))
    if isinstance(e, openclaw_provider.OpenClawError):
        if e.code == "NOT_CONFIGURED":
            raise http_err(404, "SESSION_NOT_FOUND", "openclaw not configured")
        if e.code == "TIMEOUT":
            raise http_err(504, "PROVIDER_TIMEOUT", "openclaw gateway timeout", str(e))
        raise HTTPException(status_code=502, detail=str(e))
    raise HTTPException(status_code=502, detail=str(e))


# ── OpenClaw 審批(exec / plugin)→ Approval Hub + approval 卡 ──────────────
# 契約 SPEC §6-1 的 TODO 收尾。gateway 端形狀(2026-08-11 讀靶機 dist
# `approval-shared-*.js` / `exec-approval-*.js` / `plugin-approval-*.js` 實證):
#   event `exec.approval.requested`  payload {id, request{…}, createdAtMs, expiresAtMs}
#   event `exec.approval.resolved`   payload {id, decision, resolvedBy, ts, request}
#   method `exec.approval.resolve`   params {id, decision} → {ok:true}
#   method `exec.approval.list`      無參數 → **裸陣列**,元素同 requested payload
# `plugin.approval.*` 走同一份 helper,只有 request 內容不同(pluginId/title/
# description/severity/toolName…),所以兩族共用一條路。
# decision 只吃 `allow-once` / `allow-always` / `deny`;每筆的可用集合在
# `request.allowedDecisions`(ask=="always" 時沒有 allow-always)。
_OC_APPROVAL_EVENTS = {
    "exec.approval.requested": ("exec.approval.resolve", "exec"),
    "plugin.approval.requested": ("plugin.approval.resolve", "plugin"),
}
_OC_APPROVAL_RESOLVED_EVENTS = ("exec.approval.resolved", "plugin.approval.resolved")
_OC_APPROVAL_LIST_METHODS = (("exec.approval.list", "exec.approval.resolve", "exec"),
                             ("plugin.approval.list", "plugin.approval.resolve", "plugin"))
_OC_APPROVAL_DECISIONS = ("allow-once", "allow-always", "deny")
_OC_APPROVAL_LABELS = {"allow-once": ("允許一次", "primary"),
                       "allow-always": ("永遠允許", "primary"),
                       "deny": ("拒絕", "danger")}
_OC_APPROVAL_TTL_FALLBACK = 300.0
# approval id → resolve 用的 gateway method。DB 只存 source/session_id,
# 記不住「這筆要打哪個 method」,所以另存一份記憶體對照(重連時重建)。
_OC_APPROVAL_METHODS: dict = {}


def _oc_approval_options(request: dict) -> list:
    """request.allowedDecisions → 統一 approval 物件的 options(契約 §1)。
    gateway 沒給就退三鍵全開 —— 送了不允許的 decision 只會被 gateway 打回,
    不會誤放行。"""
    allowed = [d for d in (request.get("allowedDecisions") or [])
               if d in _OC_APPROVAL_DECISIONS] or list(_OC_APPROVAL_DECISIONS)
    out = []
    for d in _OC_APPROVAL_DECISIONS:      # 固定順序,不隨 gateway 排列漂移
        if d not in allowed:
            continue
        label, style = _OC_APPROVAL_LABELS[d]
        out.append({"key": d, "label": label, "style": style})
    return out


def _oc_approval_title(kind: str, request: dict) -> str:
    if kind == "plugin":
        return str(request.get("title")
                   or request.get("toolName")
                   or request.get("pluginId") or "OpenClaw 外掛請求核准")[:200]
    cmd = str(request.get("command") or request.get("commandPreview") or "").strip()
    cmd = cmd.splitlines()[0] if cmd else ""
    host = str(request.get("host") or "") or "gateway"
    return (f"OpenClaw 要在 {host} 執行:{cmd}" if cmd
            else "OpenClaw 要執行系統指令")[:200]


def _oc_approval_detail(kind: str, request: dict) -> str:
    lines = []
    if kind == "plugin":
        for label, k in (("外掛", "pluginId"), ("工具", "toolName"),
                         ("嚴重度", "severity")):
            v = request.get(k)
            if v:
                lines.append(f"{label}:{v}")
        desc = str(request.get("description") or "").strip()
        if desc:
            lines.append(desc)
    else:
        cmd = str(request.get("command") or request.get("commandPreview") or "").strip()
        if cmd:
            lines.append(cmd)
        for label, k in (("cwd", "cwd"), ("host", "host"), ("node", "nodeId"),
                         ("agent", "agentId")):
            v = request.get(k)
            if v:
                lines.append(f"{label}:{v}")
        warn = str(request.get("warningText") or "").strip()
        if warn:
            lines.append(f"⚠️ {warn}")
        analysis = request.get("commandAnalysis")
        if isinstance(analysis, dict):
            for w in (analysis.get("warningLines") or [])[:5]:
                lines.append(f"⚠️ {w}")
    return "\n".join(str(x) for x in lines)[:400]


def _oc_approval_risk(kind: str, request: dict) -> str:
    if kind == "plugin":
        sev = str(request.get("severity") or "").lower()
        return {"critical": "high", "warning": "medium"}.get(sev, "low")
    analysis = request.get("commandAnalysis")
    if isinstance(analysis, dict) and (analysis.get("riskKinds")
                                       or analysis.get("warningLines")):
        return "high"
    return "medium" if request.get("warningText") else "low"


def _oc_approval_record(payload: dict, kind: str) -> dict | None:
    """gateway requested payload → 統一 approval 物件(A1 wire shape)。"""
    aid = str(payload.get("id") or "").strip()
    if not aid:
        return None
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    # M-1:gateway 事件帶的是**原始** sessionKey;bridge 這一側整條路
    # (digest / v2 session id / 卡片流)用的都是 `_oc_safe_session_key()`
    # 改道之後的 key。這裡不改道的話,`agent:main:main` 這種撞名 lane 的
    # 審批卡會落在一個沒人訂閱的 key 上 → 卡片靜默消失,使用者只看到
    # 「等待審批」卻沒有任何按鈕可按。
    key = _oc_safe_session_key(str(request.get("sessionKey") or ""))
    created = payload.get("createdAtMs")
    expires = payload.get("expiresAtMs")
    now = time.time()
    created_at = created / 1000.0 if isinstance(created, (int, float)) and created else now
    expires_at = (expires / 1000.0 if isinstance(expires, (int, float)) and expires
                  else now + _OC_APPROVAL_TTL_FALLBACK)
    return {"id": aid, "title": _oc_approval_title(kind, request),
            "source": f"openclaw:{key}" if key else "openclaw",
            "risk": _oc_approval_risk(kind, request),
            "detail": _oc_approval_detail(kind, request),
            "created_at": created_at, "expires_at": expires_at,
            "status": "pending", "decided_at": None, "result": None,
            "session_id": f"openclaw:{key}" if key else "",
            "provider": "openclaw", "kind": "permission",
            "options": _oc_approval_options(request),
            "_session_key": key}


def _oc_cards_feed_approval(key: str, record: dict, resolved: str = "") -> None:
    """approval 建立/決議 → 對應 openclaw session 的 approval 卡。
    沒人訂閱過的 session 不為了一張卡就建 digest(同 cc/cx 模式)。

    M-1:這是所有審批卡的**唯一**出卡點,所以改道就收斂在這裡做一次 ——
    不管呼叫端拿到的是 gateway 的原始 sessionKey(事件流)還是 DB 存的
    session_id(決議路徑),一律先過 `_oc_safe_session_key()`,才對得上
    `_v2_card_source` 建立 digest 時用的 key。
    """
    key = _oc_safe_session_key(key)
    d = _OC_CARD_DIGESTS.get(key)
    if d is None:
        return
    if resolved:
        d.resolve_approval(record, resolved)
    else:
        d.handle_approval(record)
    _oc_msg_notify(key)


def _oc_approval_upsert(record: dict, method: str) -> bool:
    """statement:pending approval 落 canonical approvals 表(讓審核中心 /
    推播 / 過期全部沿用既有機制)+ 出卡。已存在的 id 不重寫、不復活。"""
    import sqlite3
    aid = record["id"]
    _OC_APPROVAL_METHODS[aid] = method
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        row = con.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()
        if row:
            con.close()
            return row[0] == "pending"
        con.execute("INSERT INTO approvals"
                    "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                    "session_id,provider,kind,options) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, record["title"], record["source"], record["risk"],
                     record["detail"], record["created_at"], record["expires_at"],
                     "pending", None, None, None, record["session_id"],
                     "openclaw", "permission",
                     json.dumps(record["options"], ensure_ascii=False)))
        con.commit()
        con.close()
        return True
    finally:
        con.close()


def _oc_approval_mark(aid: str, status: str, result: str = "") -> bool:
    """DB 側收尾(gateway 已決議 / 本 bridge 決議完回寫)。回是否真的改到。"""
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        cur = con.execute("UPDATE approvals SET status=?, decided_at=?, result=? "
                          "WHERE id=? AND status='pending'",
                          (status, time.time(), result, aid))
        con.commit()
        n = cur.rowcount
        con.close()
        return bool(n)
    finally:
        con.close()


# ── H-2:openclaw 事件的 sqlite 一律不准跑在 WS 事件圈上 ────────────────
# `_oc_events_feed` 是 openclaw WS reader 的**同步** callback，跑在 FastAPI
# 的事件圈裡。裡面 `sqlite3.connect(CANON_DB, timeout=30)` + INSERT/UPDATE
# 只要撞到別人的寫鎖，整條事件圈(所有 HTTP、所有 SSE、所有 provider)就
# 凍結最多 30 秒 —— main 的 `_oc_events_feed` 完全沒有 DB I/O，這是本 PR
# 新引進的風險，必須搬走。
#
# 用「單一 worker + FIFO queue」而不是各自 `create_task`:審批事件有順序
# 相依(requested 必須先於 resolved 落庫)，並行化會讓 resolved 撲空。
# 沒有 running loop(單元測試 / 匯入期)就原地同步跑，行為與舊版一致。
_OC_DB_QUEUE: "asyncio.Queue | None" = None
_OC_DB_WORKER: "asyncio.Task | None" = None
_OC_DB_QUEUE_MAX = 2000


async def _oc_db_worker() -> None:
    while True:
        make_coro = await _OC_DB_QUEUE.get()
        try:
            await make_coro()
        except Exception as e:  # noqa: BLE001 — 單筆毒丸不斷流
            _log_event("oc_events_feed_error", error=str(e)[:160])
        finally:
            _OC_DB_QUEUE.task_done()


def _oc_queue_db_event(make_coro) -> bool:
    """把「會碰 sqlite 的 openclaw 事件處理」排進序列化背景 worker。
    回 False = 沒有 running loop，呼叫端請自己同步跑。"""
    global _OC_DB_QUEUE, _OC_DB_WORKER
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _OC_DB_QUEUE is None:
        _OC_DB_QUEUE = asyncio.Queue(maxsize=_OC_DB_QUEUE_MAX)
    if _OC_DB_WORKER is None or _OC_DB_WORKER.done():
        _OC_DB_WORKER = loop.create_task(_oc_db_worker())
        _BG_TASKS.add(_OC_DB_WORKER)
        _OC_DB_WORKER.add_done_callback(_BG_TASKS.discard)
    try:
        _OC_DB_QUEUE.put_nowait(make_coro)
    except asyncio.QueueFull:
        _log_event("oc_db_queue_full", depth=_OC_DB_QUEUE.qsize())
        return False
    return True


def _oc_approval_requested_prepare(event: str, payload: dict):
    method, kind = _OC_APPROVAL_EVENTS[event]
    record = _oc_approval_record(payload, kind)
    if record is None:
        _log_event("oc_approval_malformed", gateway_event=event,
                   payload_keys=",".join(sorted(payload.keys()))[:120])
        return None
    key = record.pop("_session_key")
    return record, key, method, kind


def _oc_approval_requested_finish(record: dict, key: str, kind: str) -> None:
    """DB 落完之後的收尾(出卡 / log / 推播)—— 一定要留在事件圈上跑:
    `_oc_approval_push` 靠 `asyncio.get_running_loop()` 判斷能不能推播,
    搬進 worker thread 會靜默失去推播。"""
    _oc_cards_feed_approval(key, record)
    _log_event("oc_approval_pending", approval_id=record["id"], kind=kind,
               session=key[:48], title=record["title"][:60],
               options=",".join(o["key"] for o in record["options"]))
    _oc_approval_push(record)


def _oc_approval_requested(event: str, payload: dict) -> None:
    """同步版(沒有 running loop 時才走這條)。"""
    prep = _oc_approval_requested_prepare(event, payload)
    if not prep:
        return
    record, key, method, kind = prep
    if not _oc_approval_upsert(record, method):
        return                                  # 已決議過的 id,不復活
    _oc_approval_requested_finish(record, key, kind)


async def _oc_approval_requested_async(event: str, payload: dict) -> None:
    prep = _oc_approval_requested_prepare(event, payload)
    if not prep:
        return
    record, key, method, kind = prep
    if not await asyncio.to_thread(_oc_approval_upsert, record, method):
        return
    _oc_approval_requested_finish(record, key, kind)


def _oc_approval_resolved_prepare(event: str, payload: dict):
    aid = str(payload.get("id") or "").strip()
    if not aid:
        return None
    decision = str(payload.get("decision") or "")
    status = "denied" if decision == "deny" else "approved"
    return aid, decision, status


def _oc_approval_resolved_finish(event: str, payload: dict, aid: str,
                                 decision: str, status: str) -> None:
    _OC_APPROVAL_METHODS.pop(aid, None)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    key = _oc_safe_session_key(str(request.get("sessionKey") or ""))
    _oc_cards_feed_approval(key, {"id": aid, "title": _oc_approval_title(
        "plugin" if event.startswith("plugin") else "exec", request)}, status)
    _log_event("oc_approval_resolved_upstream", approval_id=aid,
               decision=decision, status=status, session=key[:48],
               resolved_by=str(payload.get("resolvedBy") or "")[:60])


def _oc_approval_resolved(event: str, payload: dict) -> None:
    """gateway 端(或別的 operator client)決議 → 同卡收尾 + DB 收尾。
    本 bridge 自己決議時 gateway 不會回送這則(廣播排除決議者),所以
    `_oc_approval_decide` 另外自己收尾 —— 兩條路都冪等。"""
    prep = _oc_approval_resolved_prepare(event, payload)
    if not prep:
        return
    aid, decision, status = prep
    _oc_approval_mark(aid, status, decision)
    _oc_approval_resolved_finish(event, payload, aid, decision, status)


async def _oc_approval_resolved_async(event: str, payload: dict) -> None:
    prep = _oc_approval_resolved_prepare(event, payload)
    if not prep:
        return
    aid, decision, status = prep
    await asyncio.to_thread(_oc_approval_mark, aid, status, decision)
    _oc_approval_resolved_finish(event, payload, aid, decision, status)


def _oc_approval_push(record: dict) -> None:
    """待審推播(沿用審核中心既有 `_approval_push`,鎖屏動作鈕/巢形狀一致)。
    無執行中 event loop(純 sync 匯入期/測試)則跳過。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        _approval_push(record["id"], record["title"], record["detail"],
                       record.get("session_id") or "")
    except Exception as e:  # noqa: BLE001 — 推播失敗不該讓待審整筆掉
        _log_event("oc_approval_push_failed", approval_id=record.get("id"),
                   error=type(e).__name__, error_message=str(e)[:160])


def _oc_pending_approval_ids() -> set:
    """DB 裡還掛著 pending 的 openclaw 待審 id(對帳用)。"""
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        rows = con.execute("SELECT id FROM approvals WHERE provider='openclaw' "
                           "AND status='pending'").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


async def _oc_approvals_reseed() -> None:
    """連上線就補洞(SPEC §6-4:gateway 事件無 since 重放)。
    `*.approval.list` 無參數、回**裸陣列**。

    兩件事:
    1. **補**:斷線/未啟動期間新開的待審抓回來（冷啟也要做 —— 見
       `_oc_on_connect`，否則 bridge 起來之前就在等的那筆永遠看不到）。
    2. **對帳**:gateway 已經不列出、DB 卻還 pending 的，標成 expired。
       只加不減的話，斷線期間被別的 operator 決議掉的待審會在審核中心變成
       永遠按不動的殭屍列（按下去 gateway 回 "already resolved"）。
       兩族都成功列出才敢對帳，否則寧可留著。
    """
    seen: set = set()
    listed_all = True
    for list_method, resolve_method, kind in _OC_APPROVAL_LIST_METHODS:
        try:
            res = await OPENCLAW.call(list_method, {}, timeout=10.0)
        except Exception as e:  # noqa: BLE001 — 某族不存在/沒權限不該拖垮另一族
            listed_all = False
            _log_event("oc_approvals_reseed_failed", method=list_method,
                       error=type(e).__name__, error_message=str(e)[:160])
            continue
        rows = res if isinstance(res, list) else (res or {}).get("approvals") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = _oc_approval_record(row, kind)
            if record is None:
                continue
            key = record.pop("_session_key")
            seen.add(record["id"])
            # H-2:reseed 也跑在事件圈上（on_connect 建的 task），DB 一樣搬走。
            if await asyncio.to_thread(_oc_approval_upsert, record, resolve_method):
                _oc_cards_feed_approval(key, record)
        _log_event("oc_approvals_reseeded", method=list_method, count=len(rows))
    if not listed_all:
        return
    stale = await asyncio.to_thread(_oc_pending_approval_ids)
    for aid in stale - seen:
        if await asyncio.to_thread(_oc_approval_mark, aid, "expired"):
            _OC_APPROVAL_METHODS.pop(aid, None)
            _log_event("oc_approval_reconciled_expired", approval_id=aid)


def _oc_on_connect(was_reconnect: bool) -> None:
    """每次握手成功(含**冷啟第一次**)都補待審清單。

    舊接線只掛 `on_reconnect` → bridge 冷啟時 gateway 上早就掛著的待審
    完全看不到，使用者在 app 裡永遠等不到那張卡。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_oc_approvals_reseed())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


async def _oc_approval_decide(aid: str, row: dict, b: dict) -> dict:
    """統一決議路由的 openclaw 分支:key → gateway decision。

    `{approve: bool}` 相容糖 → allow-once / deny(照 options 的 primary/danger
    第一鍵取,與 `_approval_decide_core` 其他 provider 同語意)。
    """
    options = row.get("options") or _oc_approval_options({})
    okeys = [str(o.get("key") or "") for o in options]
    key = str(b.get("key") or "").strip()
    if not key:
        approve = _approval_bool_from_body(b)
        want = "primary" if approve else "danger"
        key = next((str(o.get("key")) for o in options if o.get("style") == want),
                   "allow-once" if approve else "deny")
    if key not in okeys:
        raise http_err(400, "UNKNOWN_KEY", f"key 必須是 {okeys} 之一")
    method = _OC_APPROVAL_METHODS.get(aid) or (
        "plugin.approval.resolve" if aid.startswith("plugin:")
        else "exec.approval.resolve")
    try:
        await OPENCLAW.call(method, {"id": aid, "decision": key}, timeout=15.0)
    except openclaw_provider.OpenClawError as e:
        if "already resolved" in str(e) or "unknown or expired" in str(e):
            await asyncio.to_thread(_oc_approval_mark, aid, "expired")
            raise http_err(409, "APPROVAL_NOT_PENDING",
                           "OpenClaw 這筆審批已決議或已過期")
        _oc_http_error(e)
    status = "denied" if key == "deny" else "approved"
    # H-2:HTTP handler 也在同一條事件圈上,同步 sqlite 一樣會凍結全服務。
    if not await asyncio.to_thread(_oc_approval_mark, aid, status, key):
        raise HTTPException(status_code=409, detail="already decided or expired")
    _OC_APPROVAL_METHODS.pop(aid, None)
    sid = str(row.get("session_id") or "")
    _oc_cards_feed_approval(sid.split(":", 1)[1] if ":" in sid else "",
                            row, status)
    _log_event("oc_approval_decision", approval_id=aid, status=status,
               key=key, method=method, session=sid[:48])
    return {"id": aid, "status": status, "key": key}


def _oc_events_feed(event: str, payload: dict) -> None:
    """OPENCLAW.on_event:gateway 廣播 → 依 sessionKey 分流到有訂閱過的
    digest(沒人看過的 session 不建 digest,同 cx 模式);chat final 掛推播。
    審批事件的 sessionKey 埋在 payload.request 裡,所以要在 sessionKey
    守門之前先分流 —— 之前整族被靜默丟棄,Pocket 端於是永遠卡住不動。"""
    try:
        if event in _OC_APPROVAL_EVENTS:
            # H-2:這兩族會寫 sqlite → 丟到序列化 worker，不在事件圈上等鎖。
            if not _oc_queue_db_event(
                    lambda: _oc_approval_requested_async(event, payload)):
                _oc_approval_requested(event, payload)
            return
        if event in _OC_APPROVAL_RESOLVED_EVENTS:
            if not _oc_queue_db_event(
                    lambda: _oc_approval_resolved_async(event, payload)):
                _oc_approval_resolved(event, payload)
            return
        key = _oc_safe_session_key(str(payload.get("sessionKey") or ""))
        if not key:
            return
        if event == "agent" and payload.get("isHeartbeat"):
            rid = str(payload.get("runId") or "")
            if rid:
                _OC_HEARTBEAT_RUNS[rid] = True
                while len(_OC_HEARTBEAT_RUNS) > _OC_HEARTBEAT_RUNS_MAX:
                    _OC_HEARTBEAT_RUNS.popitem(last=False)
        d = _OC_CARD_DIGESTS.get(key)
        if d is not None:
            d.handle(event, payload)
        if event in ("chat", "agent"):
            _oc_msg_notify(key)   # B:喚醒該 session 的 events SSE(即時推播)
        if event == "chat" and payload.get("state") == "final":
            _oc_push_final(key, payload)
    except Exception as e:  # noqa: BLE001 — 單筆事件毒丸不斷流
        _log_event("oc_events_feed_error", error=str(e)[:160])


def _oc_push_final(key: str, payload: dict) -> None:
    """openclaw 回合定稿 → 推播(照 _push_persona_reply 同款;heartbeat 自跑
    回合不吵人)。fire-and-forget。"""
    rid = str(payload.get("runId") or "")
    if rid and rid in _OC_HEARTBEAT_RUNS:
        return
    msg = payload.get("message") or {}
    text = carddigest._oc_msg_text(msg.get("content")).strip()
    if not text:
        return
    title = f"OpenClaw · {openclaw_provider.session_short_name(key)}"
    data = {"kind": "message",
            "pocket": {"kind": "message", "sessionId": f"openclaw:{key}"}}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(push_notify(title, text[:140], data,
                                     thread_id=f"openclaw:{key}",
                                     content_available=True,
                                     no_preview_body="傳了一則訊息"))
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


def _oc_reseed_on_reconnect() -> None:
    """斷線期間 gateway 事件不可重放(SPEC §6-4)→ 全部 digest 標記重 seed,
    下一次 cards/events 請求會重讀 chat.history 補洞(卡 id 穩定,重疊只是
    rev 遞增)。

    待審清單的補洞**不在這裡**做 —— 改掛 `on_connect`,才連冷啟第一次也
    補得到(卡在那裡等人按的審批不能因為 bridge 剛起來就人間蒸發)。"""
    for d in _OC_CARD_DIGESTS.values():
        d.seeded = False


OPENCLAW.on_event = _oc_events_feed
OPENCLAW.on_reconnect = _oc_reseed_on_reconnect
OPENCLAW.on_connect = _oc_on_connect


def _openclaw_default_v2_row() -> dict:
    return {"id": "openclaw:agent:main:main", "provider": "openclaw",
            "title": "OpenClaw", "subtitle": None, "status": "idle",
            "last_event_at": None,
            "capabilities": ["input", "interrupt", "attachments", "replay",
                             "follow", "approve"],
            "meta": {"default": True}}


async def _openclaw_v2_rows(limit: int = 20) -> list:
    if not OPENCLAW.configured():
        return []
    res = await OPENCLAW.call("sessions.list", {"limit": limit}, timeout=10.0)
    rows = [openclaw_provider.session_v2_row(row)
            for row in (res or {}).get("sessions", [])[:limit]]
    if not rows:
        rows = [_openclaw_default_v2_row()]
    return rows


async def _openclaw_v1_sessions() -> list:
    """Legacy home/session list shape for clients that do not consume v2 yet.

    OpenClaw is not a Hermes persona registry.  Still, a fresh OpenClaw install
    needs one obvious conversation entry; expose the gateway's main session as a
    provider session so old clients have somewhere to send the first message.
    """
    try:
        rows = await _openclaw_v2_rows(20)
    except Exception as e:  # noqa: BLE001
        _log_event("openclaw_v1_session_list_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
        return []
    out = []
    for row in rows:
        title = row.get("title") or "OpenClaw"
        if title == "main":
            title = "OpenClaw"
        subtitle = row.get("subtitle") or "OpenClaw gateway"
        out.append({"id": row.get("id"), "type": "openclaw", "provider": "openclaw",
                    "name": title, "preview": subtitle,
                    "lastAt": row.get("last_event_at"), "status": row.get("status", "idle"),
                    "capabilities": row.get("capabilities") or []})
    return out


async def _openclaw_v1_personas() -> list:
    """Virtual persona-list entry for legacy clients.

    The mobile tab historically reads `/app/v1/personas`, but product-wise that
    tab is now "the active connection's entries": Hermes exposes personas,
    OpenClaw exposes its main provider session.  Use id `openclaw` so old v1
    message routes can map it to `openclaw:agent:main:main`.
    """
    sessions = await _openclaw_v1_sessions()
    preview = (sessions[0].get("preview") if sessions else None) or "OpenClaw gateway"
    status = (sessions[0].get("status") if sessions else None) or "idle"
    return [{
        "id": "openclaw",
        "name": "OpenClaw",
        "profile": "main",
        "home": "",
        "enabled": True,
        "deleted": False,
        "builtin": True,
        "username": "",
        "avatar_rev": 0,
        "provider": "openclaw",
        "type": "openclaw",
        "session_id": "openclaw:agent:main:main",
        "preview": preview,
        "status": status,
    }]


def _openclaw_key_from_session_id(session_id: str) -> str | None:
    sid = str(session_id or "").strip()
    if sid.startswith("openclaw:"):
        return sid.split(":", 1)[1] or None
    if sid == "openclaw":
        return _oc_safe_session_key("agent:main:main")
    return None


def _openclaw_message_text(msg: dict) -> str:
    try:
        return carddigest._oc_msg_text(msg.get("content")).strip()
    except Exception:  # noqa: BLE001
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
            return "\n".join(p for p in parts if p).strip()
        return str(content or "").strip()


def _openclaw_message_ts(msg: dict) -> float:
    for key in ("createdAt", "updatedAt", "ts", "timestamp"):
        val = msg.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val) / 1000.0 if val > 1e11 else float(val)
    return time.time()


def _openclaw_message_role(msg: dict) -> str:
    raw = str(msg.get("role") or msg.get("speaker") or msg.get("source") or "").lower()
    if raw in ("user", "human"):
        return "user"
    if raw in ("assistant", "agent", "tool"):
        return "assistant"
    if msg.get("fromUser") is True:
        return "user"
    return "assistant"


def _openclaw_v1_message(session_key: str, msg: dict, idx: int) -> dict:
    mid = str(msg.get("id") or msg.get("messageId") or msg.get("runId")
              or f"oc-{hashlib.sha1((session_key + ':' + str(idx) + ':' + _openclaw_message_text(msg)).encode()).hexdigest()[:16]}")
    return {"id": mid, "role": _openclaw_message_role(msg),
            "content": _openclaw_message_text(msg),
            "attachments": [], "ts": _openclaw_message_ts(msg),
            "client_id": msg.get("clientId") or None,
            "provider": "openclaw", "session": f"openclaw:{session_key}"}


async def _openclaw_v1_messages(session_id: str, limit: int = 200) -> dict:
    key = _openclaw_key_from_session_id(session_id)
    if not key:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown openclaw session")
    try:
        res = await OPENCLAW.call("chat.history", {"sessionKey": key,
                                                   "limit": max(1, min(limit, 500))},
                                  timeout=10.0)
    except openclaw_provider.OpenClawError as e:
        _oc_http_error(e)
    messages = [_openclaw_v1_message(key, msg, i)
                for i, msg in enumerate((res or {}).get("messages") or [])]
    return {"messages": messages}


async def _openclaw_v1_post_message(body: dict, request: Request):
    session_id = str(body.get("session") or "openclaw:agent:main:main")
    key = _openclaw_key_from_session_id(session_id)
    if not key:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown openclaw session")
    content = (body.get("content") or body.get("text") or "").strip()
    attachments = body.get("attachments") or []
    client_id = body.get("client_id")
    dry_run = bool(body.get("dry_run"))
    cid = "ocmsg-" + uuid.uuid4().hex[:20]
    created = int(time.time())

    def chunk(delta, finish=None, **extra):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session_id,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        payload.update(extra)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def agen():
        yield chunk({"role": "assistant", "content": ""})
        try:
            if dry_run:
                res = {"run_id": "dry-run"}
            else:
                res = await _oc_input_core(
                    key, f"openclaw:{key}",
                    {"content": content, "attachments": attachments,
                     "client_id": client_id})
            yield chunk({}, None, status={"state": "accepted",
                                          "label": "OpenClaw 已收到，正在處理。"},
                        accepted=True, dry_run=dry_run, run_id=res.get("run_id"))
            yield chunk({}, "stop", accepted=True, dry_run=dry_run,
                        run_id=res.get("run_id"))
        except HTTPException as e:
            yield chunk({"content": f"⚠️ OpenClaw 連線失敗：{e.detail}"},
                        "stop", error=True)
        except Exception as e:  # noqa: BLE001
            _log_event("openclaw_v1_post_failed", error=type(e).__name__,
                       error_message=str(e)[:160], client=_client_host(request))
            yield chunk({"content": f"⚠️ OpenClaw 連線失敗：{type(e).__name__}"},
                        "stop", error=True)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        agen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform",
                 "X-Accel-Buffering": "no"},
    )

def _dashboard_active_provider() -> str:
    """Provider chosen by PocketConnect's first-run installer.

    Clean installs may have more than one runtime present over time (for
    example the tester tries Hermes, then OpenClaw). Dashboard should reflect
    the active provider selected for this bridge, not every stale runtime or
    legacy Hermes health template that happens to exist on disk.
    """
    raw = (os.environ.get("POCKET_ACTIVE_PROVIDER")
           or os.environ.get("POCKET_PROVIDER")
           or "").strip().lower()
    return raw if raw in ("hermes", "openclaw", "none") else "auto"


def _oc_ensure_pump() -> None:
    """常駐事件泵(單例):配置存在才有意義;未配置時 run_forever 便宜輪空。"""
    t = _OC_PUMP_TASK[0]
    if t is not None and not t.done():
        return
    _OC_PUMP_TASK[0] = asyncio.create_task(OPENCLAW.run_forever())


async def _oc_card_digest(key: str):
    """取得(必要時建立+seed)該 sessionKey 的 digest。先註冊再 seed ——
    seed 期間的 live 事件與 seed 產不同批卡 id(h- vs run-),known_mids
    去重靠 __openclaw.id,不會雙份。"""
    d = _OC_CARD_DIGESTS.get(key)
    if d is None:
        d = _OC_CARD_DIGESTS[key] = carddigest.OpenClawDigest()
        d.store.media_session_id = f"openclaw:{key}"
    if not d.seeded:
        d.seeded = True
        try:
            res = await OPENCLAW.call("chat.history",
                                      {"sessionKey": key,
                                       "limit": _OC_CARD_SEED_MSGS})
            d.seed_messages((res or {}).get("messages") or [])
            si = (res or {}).get("sessionInfo") or {}
            d.busy = bool(si.get("hasActiveRun"))
            d._status()
        except Exception as e:  # noqa: BLE001
            d.seeded = False   # 下次請求重試 seed
            _log_event("oc_card_seed_error", session=key[:48],
                       error=str(e)[:200])
            _oc_http_error(e)
    return d


# OpenClaw `chat.send.attachments[]` 的實際受理形狀(靶機 dist 實證,
# `src/gateway/server-methods/attachment-normalize.ts` 的
# `normalizeRpcAttachmentsToChatAttachments`):
#   {type?: str, mimeType?: str, fileName?: str, content: base64 str}
# 也吃 Anthropic 風格 `{source:{type:"base64", media_type, data}}`。
# **關鍵**:該函式最後 `.filter(a => a.content)` —— 沒有 `content` 的件會被
# gateway **靜默丟掉**。SPEC §2 舊表寫的 `url|content` 其中 `url` 是誤記,
# bridge 一律自己把檔案讀成 base64 再送,絕不寄望 url。
# 大小:gateway 預設 20MB/件(agents.defaults.mediaMaxMb),影像另有 6MiB 硬閥,
# 且整個 WS 訊框受 policy.maxPayload(靶機 26MB)限制 —— 送太大等於斷線,
# 所以 bridge 端先擋,擋下來一律報錯,不靜默丟。
_OC_ATT_MAX_IMAGE_BYTES = 6 * 1024 * 1024
_OC_ATT_MAX_FILE_BYTES = 20 * 1024 * 1024
_OC_ATT_MAX_TOTAL_BYTES = 20 * 1024 * 1024


def _oc_attachment_mime(a: dict, path: str | None = None) -> str:
    """宣告 mime 優先，其次拿檔名/路徑猜 —— 落盤前就得知道套哪個上限。"""
    declared = str(a.get("mime") or a.get("content_type")
                   or a.get("mimeType") or "").strip()
    if declared:
        return declared
    hint = path or str(a.get("filename") or a.get("fileName") or "")
    return (mimetypes.guess_type(hint)[0] if hint else None) or "application/octet-stream"


def _oc_attachment_cap(mime: str) -> int:
    return (_OC_ATT_MAX_IMAGE_BYTES if mime.startswith("image/")
            else _OC_ATT_MAX_FILE_BYTES)


def _oc_safe_file_name(name: str) -> str:
    """轉送給 gateway 的 `fileName` 淨化(不是本機落盤檔名 —— 那條已經有
    `_upload_dest_path` 在淨化)。app 傳什麼就原封轉出去的話，
    `../../etc/passwd` 這種名字會直接進到 OpenClaw 端的存檔邏輯裡。
    規則與 `_upload_dest_path` 一致,避免兩邊漂移。"""
    base = os.path.basename((name or "").replace("\\", "/"))
    safe = re.sub(r"[^\w.\-]", "_", base).strip(".")
    return safe or "file"


def _oc_attachment_payload(a: dict, idx: int,
                           budget_left: int = _OC_ATT_MAX_TOTAL_BYTES) -> tuple[dict, dict]:
    """app 直送 attachment → (gateway chat.send 件, 卡片摘要件)。

    失敗一律 raise(400/413)—— 這裡是「附件被靜默丟棄」那個資料遺失缺陷的
    根治點:讀不到就吵,絕不回傳 None 讓呼叫端當沒事。

    **H-1(live bridge 存活)**:量在前、讀在後。舊版先 `read_bytes()` 把整個
    檔案吞進記憶體，才拿 `len(raw)` 比上限 —— 12 件 × 2GiB 宣告 = 最多 24GB
    灌進 RAM 之後才回 413，production 的 bridge 早被 OOM killer 收走了。
    """
    if not isinstance(a, dict):
        raise http_err(400, "ATTACHMENT_INVALID",
                       f"attachments[{idx}] 不是物件")
    filename = str(a.get("filename") or a.get("fileName") or "").strip()
    cap = min(_oc_attachment_cap(_oc_attachment_mime(a)), max(budget_left, 0))
    # ① data URI:不解碼就能估大小 → 超標的話連落盤都不做。
    data_uri = str(a.get("data") or a.get("data_uri") or "")
    if data_uri:
        est = _data_uri_estimated_bytes(data_uri)
        if est > cap:
            raise http_err(413, "ATTACHMENT_TOO_LARGE",
                           f"attachments[{idx}] {filename or 'file'} 約 {est} bytes "
                           f"超過 OpenClaw 上限 {cap}(未落盤)")
    path = _save_attachment(a, filename or "file")
    if not path:
        # data-URI 落盤失敗 / path 不在 UPLOAD_DIR / 只給了外部 url。
        raise http_err(400, "ATTACHMENT_UNREADABLE",
                       f"attachments[{idx}] 取不到本機檔案(需要 uploads 路徑或 data URI)")
    mime = _oc_attachment_mime(a, path)
    name = filename or os.path.basename(path)
    cap = min(_oc_attachment_cap(mime), max(budget_left, 0))
    # ② 先 stat 再讀 —— 舊版是 read_bytes() 完才比大小。
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise http_err(400, "ATTACHMENT_UNREADABLE",
                       f"attachments[{idx}] 讀檔失敗:{type(e).__name__}") from e
    if size > cap:
        raise http_err(413, "ATTACHMENT_TOO_LARGE",
                       f"attachments[{idx}] {name} {size} bytes 超過 OpenClaw 上限 {cap}")
    # ③ 讀取本身也封頂 cap+1:就算檔案在 stat 之後被換掉/長大(TOCTOU),
    #    也絕不會把超過上限的量吞進記憶體。
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
    except OSError as e:
        raise http_err(400, "ATTACHMENT_UNREADABLE",
                       f"attachments[{idx}] 讀檔失敗:{type(e).__name__}") from e
    if len(raw) > cap:
        raise http_err(413, "ATTACHMENT_TOO_LARGE",
                       f"attachments[{idx}] {name} 超過 OpenClaw 上限 {cap}")
    size = len(raw)
    kind = str(a.get("kind") or "").strip()
    safe_name = _oc_safe_file_name(name)
    item = {"type": kind or ("image" if mime.startswith("image/") else "file"),
            "mimeType": mime, "fileName": safe_name,
            "content": base64.b64encode(raw).decode("ascii")}
    summary = {"filename": safe_name, "mime": mime, "size": size}
    if kind:
        summary["kind"] = kind
    return item, summary


def _oc_attachments_payload(attachments: list) -> tuple[list, list]:
    """attachments[] → (chat.send 用的件, 卡片摘要);任一件失敗整包報錯。

    總額度是**邊走邊扣**再往下傳的:第 N 件能讀多少取決於前 N-1 件用掉
    多少 —— 「12 件各 19MB」在第 2 件就會 413，不會先全部讀進來再算總和。
    """
    items, summaries, total = [], [], 0
    for i, a in enumerate(attachments or []):
        left = _OC_ATT_MAX_TOTAL_BYTES - total
        if left <= 0:
            raise http_err(413, "ATTACHMENT_TOO_LARGE",
                           f"attachments 合計超過 OpenClaw 上限 "
                           f"{_OC_ATT_MAX_TOTAL_BYTES}")
        item, summary = _oc_attachment_payload(a, i, budget_left=left)
        total += summary["size"]
        if total > _OC_ATT_MAX_TOTAL_BYTES:
            raise http_err(413, "ATTACHMENT_TOO_LARGE",
                           f"attachments 合計 {total} bytes 超過 OpenClaw 上限 "
                           f"{_OC_ATT_MAX_TOTAL_BYTES}")
        items.append(item)
        summaries.append(summary)
    return items, summaries


async def _oc_input_core(key: str, session_id: str, body: dict) -> dict:
    """v2 input(oc):chat.send fire-and-forget,回覆走 S4 卡片事件流。
    client_id → idempotencyKey(gateway 原生冪等,重試不重跑)。

    附件:真的送出去(SPEC §4)。gateway 的 chat.send 只要 `message` 或
    `attachments` 其一有值就受理,所以純附件也放行。**任何送不出去的附件
    一律報錯**——之前「有文字+有附件」時附件被靜默丟棄,app 顯示成功但
    OpenClaw 端根本沒收到圖,是資料遺失不是顯示問題。
    """
    content = (body.get("content") or body.get("text") or "").strip()
    attachments = body.get("attachments") or []
    _att_guard(attachments)
    oc_atts, att_summary = _oc_attachments_payload(attachments)
    if not content and not oc_atts:
        raise HTTPException(status_code=400, detail="empty")
    client_id = str(body.get("client_id") or "").strip()
    idem = f"pocket-{client_id}" if client_id else f"pocket-{uuid.uuid4().hex[:20]}"
    params = {"sessionKey": key, "message": content, "idempotencyKey": idem}
    if oc_atts:
        params["attachments"] = oc_atts
    try:
        res = await OPENCLAW.call("chat.send", params)
    except openclaw_provider.OpenClawError as e:
        _oc_http_error(e)
    d = _OC_CARD_DIGESTS.get(key)
    if d is not None:
        # user 回顯卡:openclaw 的 chat 事件只帶 assistant 訊息,app 送話
        # 在這裡即時出卡(idem 冪等 → 重試同 id 不雙份)。
        d.user_card(content, idem, attachments=att_summary)
    _log_event("oc_input", session=key[:48], chars=len(content),
               attachments=len(oc_atts),
               attachment_bytes=sum(s["size"] for s in att_summary),
               run_id=str((res or {}).get("runId") or ""))
    return {"ok": True, "session_id": session_id, "accepted": True,
            "attachments": len(oc_atts),
            "run_id": str((res or {}).get("runId") or "")}


@app.get("/app/v1/openclaw/config")
async def openclaw_config_get(request: Request):
    """App 帳號頁「進階」讀 OpenClaw 連線設定(token 永不回傳明文)。"""
    _check_auth(request)
    cfg = openclaw_provider.load_config()
    return {"configured": OPENCLAW.configured(),
            "base_url": cfg["base_url"], "token_set": bool(cfg["token"]),
            "source": cfg["source"]}


@app.put("/app/v1/openclaw/config")
async def openclaw_config_put(request: Request):
    """App 手動配置 base_url+token(v1;QR/runtime 配對留 TODO,SPEC §6-5)。
    base_url 給空字串 = 清除配置(provider 回到靜默缺席)。env 有值時 env
    優先,source 欄位讓 app 能提示「目前由伺服器 env 鎖定」。"""
    _check_auth(request)
    body = await _json_body(request)
    base = str(body.get("base_url") or "").strip()
    token = str(body.get("token") or "").strip()
    openclaw_provider.save_config(base, token)
    OPENCLAW.reset()
    _oc_reseed_on_reconnect()
    if OPENCLAW.configured():
        _oc_ensure_pump()
    cfg = openclaw_provider.load_config()
    _log_event("openclaw_config_updated", configured=OPENCLAW.configured(),
               source=cfg["source"])
    return {"ok": True, "configured": OPENCLAW.configured(),
            "base_url": cfg["base_url"], "token_set": bool(cfg["token"]),
            "source": cfg["source"]}


async def _v2_card_store(session_id: str):
    """cards/events 共用:session id → 已 seed 的 SessionCardStore。"""
    src = _v2_card_source(session_id)
    if src[0] == "cx":
        return (await _cx_card_digest(src[1])).store
    if src[0] == "hp":
        return (await _hp_card_digest(src[1])).store
    if src[0] == "oc":
        return (await _oc_card_digest(src[1])).store
    _, name, workdir = src
    store = _cc_card_store(name)
    await _cc_card_seed(store, name, workdir)
    _ensure_cc_card_follower(name, workdir)
    return store


@app.get("/app/v2/sessions/{session_id}/cards")
async def v2_session_cards(session_id: str, request: Request, limit: int = 100,
                           before_seq: int | None = None):
    """契約 §3 冷載 snapshot:{cards, latest_seq} → app 渲染後從 since_seq 接流。"""
    _check_auth(request)
    store = await _v2_card_store(session_id)
    return store.snapshot(limit=max(1, min(limit, 500)), before_seq=before_seq)


@app.get("/app/v2/sessions/{session_id}/media")
async def v2_session_media(session_id: str, request: Request, limit: int = 100,
                           cursor: int | None = None):
    """Durable media index for persona, Claude Code, and Codex sessions."""
    _check_auth(request)
    # Reuse the canonical session router and seed path. Besides validating the
    # id, this captures references from cold history before returning the index.
    await _v2_card_store(session_id)
    page = await asyncio.to_thread(
        _media_store().list_session,
        session_id,
        limit=max(1, min(limit, 500)),
        before=cursor,
    )
    page["items"] = [_media_wire_item(item) for item in page["items"]]
    return page


@app.get("/app/v2/artifacts/{media_id}")
async def v2_artifact_download(media_id: str, request: Request):
    """Serve an archived artifact. External URLs stay external and are never
    fetched by the bridge, so they intentionally have no download endpoint."""
    _check_auth(request)
    opened = await asyncio.to_thread(_media_store().open_media, media_id)
    if opened is None:
        raise http_err(
            404, "ARTIFACT_UNAVAILABLE",
            "檔案未封存或來源已失效",
        )
    path, mime, _filename = opened
    return FileResponse(path, media_type=mime or None)


@app.get("/app/v2/sessions/{session_id}/events")
async def v2_session_events(session_id: str, request: Request, since_seq: int = 0,
                            profile: str = "phone"):
    """契約 §1 SSE 事件流:信封 {seq,ts,type,data};since_seq 補洞;超範圍 410。"""
    _check_auth(request)
    if profile != "phone":
        raise http_err(400, "UNSUPPORTED_PROFILE", "v0 只實作 profile=phone(契約 §4)")
    store = await _v2_card_store(session_id)
    backlog = store.since(since_seq)
    if backlog is None:
        raise http_err(410, "SEQ_GONE",
                       "since_seq 超出 ring buffer 範圍,請走 snapshot 冷載")

    async def gen():
        cursor = since_seq
        store.subscribers += 1
        waker = store.attach_waker()   # _push 落 ring 即刻喚醒(取代 0.5s 輪詢)
        # issue #7 項目 4:這條原本是全檔唯一「完全沒有出口」的 stream ——
        # 沒有 deadline、沒有 idle 上限,一路 while True。客戶端半死(TCP 沒收
        # FIN,is_disconnected 永遠 False)就永久佔著一個 task + 一個 subscriber
        # 計數,而 subscribers>0 會讓 follower 持續巡 status,連 CPU 一起漏。
        #
        # 為什麼 30 分鐘 idle 斷線不會漏事件:idle 的定義就是「這段時間
        # store.seq 沒有前進」,也就是 ring buffer 完全沒有新事件。斷線時
        # cursor == store.seq,app 帶 since_seq=cursor 重連時 `store.since()`
        # 的三個 None 條件(領先 seq / 洞 / 空 ring 但落後)一個都不成立,
        # 必定回傳空 backlog 然後續流 —— 零漏事件。真的漏不掉的情形(bridge
        # 重啟過、ring 滾過)本來就回 410,app 走 snapshot 冷載,行為不變。
        last_event = time.monotonic()
        last_sent = last_event
        keepalive = max(1.0, float(SSE_KEEPALIVE_SECS))
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                cursor = ev["seq"]
            while True:
                if await request.is_disconnected():
                    break
                if time.monotonic() - last_event >= _STREAM_IDLE_CUTOFF_SECS:
                    _log_event("v2_events_idle_cutoff", session=session_id,
                               cursor=cursor)
                    break
                if store.seq > cursor:
                    fresh = store.since(cursor)
                    if fresh is None:      # ring 已滾過游標(理論上不會) → 斷線重載
                        break
                    for ev in fresh:
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        cursor = ev["seq"]
                    last_event = time.monotonic()
                    last_sent = last_event
                    continue    # 灌流時整批 drain 完再回頭檢查 —— 不逐事件空轉
                # 已追平 → 事件驅動等待。順序鐵律:先 clear 再重驗 seq。
                # 單事件圈下 clear 與 wait 之間沒有 await,不可能漏訊號;
                # 上面 yield 期間落地的 _push 則由這個 re-check 接住。
                waker.clear()
                if store.seq > cursor:
                    continue
                now = time.monotonic()
                if now - last_sent >= keepalive:
                    yield f"data: {json.dumps(store.ping(), ensure_ascii=False)}\n\n"
                    last_sent = now
                try:
                    # 有事件 → waker 秒醒(取代舊 0.5s 輪詢的量化延遲);
                    # 沒事件 → 準時 timeout 去補 keepalive,節奏不變。
                    await asyncio.wait_for(
                        waker.wait(),
                        timeout=max(0.05, keepalive - (time.monotonic() - last_sent)))
                except asyncio.TimeoutError:
                    pass
        finally:
            store.detach_waker(waker)
            store.subscribers -= 1

    return StreamingResponse(gen(), media_type="text/event-stream")


# ───────────────────────── scheduled reports + notification toggles ─────────
# Hermes runs the daily briefs via cron (jobs.json); each job already has an
# enabled/paused state the scheduler honours, and `hermes cron pause/resume`
# toggles it safely. The app surfaces the reports (so they land in Pocket Agent,
# not just Telegram) and exposes per-notification on/off switches.

CRON_JOBS_JSON = os.path.expanduser("~/apps/hermes-agent/home/cron/jobs.json")
HERMES_HOME_DIR = os.path.expanduser("~/apps/hermes-agent/home")
STATE_DB = os.path.join(HERMES_HOME_DIR, "state.db")

# User-facing notification jobs → friendly label. Everything else (signal
# collector, session reset/hygiene) is internal and hidden from the app.
NOTIFY_LABELS = {
    "morning-brief-0700": "晨報",
    "stock-premarket-0850": "盤前速覽",
    "afternoon-brief-1330": "午報",
    "memory-consolidation-2200": "晚間三省",
}

# Per-persona daily notification jobs (cron job *name* -> in-chat label) that
# should appear INSIDE that persona's conversation, like Telegram shows them —
# not only in the separate Reports tab. Each persona reads its OWN home's
# state.db + cron/jobs.json, so adding a new daily job (e.g. a 潘天晴 "今日精選"
# / 限動 cron) here is all it takes for it to surface in-chat automatically.
PERSONA_REPORTS = {
    "yuanfang": NOTIFY_LABELS,
    "pantianqing": {
        "fliper-editorial-brief-0715": "編輯台晨報",
        "TNH 名家觀點自動建稿": "台北文創名家觀點掃描",
    },
    "xcash": {
        "xcash-morning-dev-brief-0730": "開發晨報",
    },
    "shuijing": {
        "shuijing-sunrise-oracle": "水鏡晨卦",
    },
}

# A3-3:哪些 cron 報告在同步進 report_events 時順手建 kind=notice approval
# (app 通知中心的入口 + 已讀 ack)。先行兩個試跑,穩了再擴。
NOTICE_REPORT_JOBS = {
    "xcash": {"xcash-morning-dev-brief-0730"},
    "shuijing": {"shuijing-sunrise-oracle"},
}
_NOTICE_REPORT_MAX_AGE = 12 * 3600.0   # 只通知 12h 內的報告(防冷庫回灌灌爆)
_NOTICE_REPORT_TTL = 86400.0           # 晨報 ack 給一天;過期由掃描同卡收尾


def _notice_for_report(session: str, report: dict) -> None:
    """A3-3:新 cron 報告 → kind=notice approval(不推播 —— 報告本體已由
    TG/推播管道送達;這裡補的是 app 通知中心的入口與已讀 ack)。
    approval id 錨在 report id 上:同 id 已存在(不論狀態)就不重建,
    報告內容修訂不會把已 ack 的通知翻回 pending。"""
    import sqlite3
    name = report.get("name") or ""
    if _is_hidden_report(report):
        return
    if name not in NOTICE_REPORT_JOBS.get(session, ()):
        return
    rid = str(report.get("id") or "")
    if not rid:
        return
    if time.time() - float(report.get("ts") or 0) > _NOTICE_REPORT_MAX_AGE:
        return
    aid = "ntc-" + hashlib.sha1(rid.encode()).hexdigest()[:20]
    sid = f"hermes:{session}"
    title = report.get("label") or name or "報告"
    detail = _clip_text(report.get("content") or "", 200)
    options = [{"key": "ack", "label": "知道了", "style": "primary"}]
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        if con.execute("SELECT 1 FROM approvals WHERE id=?", (aid,)).fetchone():
            return
        con.execute(
            "INSERT INTO approvals"
            "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
            "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, title, sid, "", detail, now, now + _NOTICE_REPORT_TTL,
             "pending", None, None, None, sid, "hermes", "notice",
             json.dumps(options, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()
    _log_event("notice_created", id=aid, session=session, report=name)
    rec = {"id": aid, "title": title, "detail": detail, "options": options,
           "kind": "notice"}
    loop = _MAIN_LOOP

    def _feed():
        try:
            _hp_cards_feed_approval(sid, rec)
        except Exception as e:  # noqa: BLE001
            _log_event("hp_cards_feed_error", error=str(e)[:160])
    if loop and loop.is_running():
        # 報告同步常跑在 to_thread(卡片流 seed / v1 merge)—— 卡片庫不上鎖,
        # 一律排回主圈做 feed(同圈呼叫也安全:排到下一輪跑)。
        loop.call_soon_threadsafe(_feed)


def _cron_jobs():
    try:
        data = json.load(open(CRON_JOBS_JSON, encoding="utf-8"))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cron_jobs", _exc, expected=True)
        return []
    out = []
    for j in data.get("jobs", []):
        name = j.get("name", "")
        on = bool(j.get("enabled", True)) and j.get("state") != "paused"
        out.append({"id": j.get("id"), "name": name, "label": NOTIFY_LABELS.get(name),
                    "schedule": j.get("schedule_display") or j.get("schedule", {}).get("display", ""),
                    "enabled": on, "notify": name in NOTIFY_LABELS})
    return out


async def _hermes_cron(action: str, job_id: str):
    env = dict(os.environ)
    env["HERMES_HOME"] = HERMES_HOME_DIR
    try:
        p = await asyncio.create_subprocess_exec(
            HERMES_BIN, "cron", action, job_id, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=30)
        return p.returncode == 0, (out or b"").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        _log_exc("_hermes_cron", e, expected=True)
        return False, str(e)


_REPORT_START = re.compile(r"(🌅|🌙|☀️|🌇|🌃|📊|🗓️|善彰[，,、]?\s*(早安|午安|晚安)|早安|午安|晚安)")
_REPORT_INLINE_START = re.compile(
    r"(\*\*[^*\n]*(晨報|午報|晚間|速覽|掃描|晨卦)[^*\n]*\*\*|"
    r"#{1,3}\s*[^\n]*(晨報|午報|晚間|速覽|掃描|晨卦)|"
    r"(🌅|🌙|☀️|🌇|🌃|📊|🗓️)[^\n]*|"
    r"善彰[，,、]?\s*(早安|午安|晚安))"
)


def _clean_report(s: str) -> str:
    """Trim a leading English working-note preamble some cron runs leak before
    the actual brief ("I have all the data… Now composing…"), WITHOUT eating the
    real title. Keep from the first line that carries real content: any CJK, a
    markdown header, or one of the known report openers (🌅/善彰早安…)."""
    text = s.strip()
    inline = _REPORT_INLINE_START.search(text)
    if inline and inline.start() > 0:
        return text[inline.start():].lstrip(" -—\n").strip()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        t = line.strip()
        if not t:
            continue
        has_cjk = any("一" <= c <= "鿿" for c in t)
        if has_cjk or t.startswith("#") or _REPORT_START.match(t):
            return "\n".join(lines[i:]).strip()
    return text


def _reports(limit: int = 20):
    """Latest delivered report per recent cron run (the session's final assistant
    message), newest first — only the user-facing notification jobs."""
    import sqlite3
    if not os.path.exists(STATE_DB):
        return []
    jobs = {j["id"]: j for j in _cron_jobs()}
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        try:
            sids = con.execute(
                "SELECT m.session_id, MAX(m.timestamp) ts FROM messages m "
                "JOIN sessions s ON s.id = m.session_id WHERE s.source='cron' "
                "GROUP BY m.session_id ORDER BY ts DESC LIMIT ?", (limit * 3,)).fetchall()
            out = []
            for sid, _ts in sids:
                mobj = re.search(r"cron_([0-9a-f]+)_", str(sid))
                job = jobs.get(mobj.group(1)) if mobj else None
                if not (job and job.get("notify")):
                    continue                       # skip internal / unknown jobs
                last = con.execute(
                    "SELECT content, timestamp FROM messages WHERE session_id=? "
                    "AND role='assistant' AND content IS NOT NULL AND content!='' "
                    "ORDER BY timestamp DESC LIMIT 1", (sid,)).fetchone()
                if last and last[0]:
                    out.append({"label": job.get("label") or job.get("name"),
                                "name": job.get("name"), "content": _clean_report(last[0]),
                                "ts": last[1]})
                if len(out) >= limit:
                    break
            con.close()
            return out
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_reports", _exc, expected=True)
        return []


def _cron_names_for(home: str) -> dict:
    """job_id -> job name, from a given persona home's own cron/jobs.json."""
    try:
        data = json.load(open(os.path.join(home, "cron", "jobs.json"), encoding="utf-8"))
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        return {j.get("id"): j.get("name", "") for j in jobs}
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_cron_names_for", _exc, expected=True)
        return {}


def _persona_reports(persona: str, limit: int = 20):
    """Daily notification reports for ANY persona, read from that persona's OWN
    home state.db + cron/jobs.json (not just the main 袁方 home). Mirrors
    _reports but generalised so 潘天晴's 編輯台晨報 (and future 今日精選 / 限動)
    surface in-conversation too. Returns newest-first cleaned briefs."""
    import sqlite3
    labels = PERSONA_REPORTS.get(persona)
    if not labels:
        return []
    home = home_for(persona)
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return []
    idname = _cron_names_for(home)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            sids = con.execute(
                "SELECT m.session_id, MAX(m.timestamp) ts FROM messages m "
                "JOIN sessions s ON s.id = m.session_id WHERE s.source='cron' "
                "GROUP BY m.session_id ORDER BY ts DESC LIMIT ?", (limit * 3,)).fetchall()
            out = []
            for sid, _ts in sids:
                mobj = re.search(r"cron_([0-9a-f]+)_", str(sid))
                name = idname.get(mobj.group(1)) if mobj else None
                label = labels.get(name)
                if not label:
                    continue                       # not a user-facing daily job
                last = con.execute(
                    "SELECT content, timestamp FROM messages WHERE session_id=? "
                    "AND role='assistant' AND content IS NOT NULL AND content!='' "
                    "ORDER BY timestamp DESC LIMIT 1", (sid,)).fetchone()
                if last and last[0]:
                    external_id = f"cron:{persona}:{name}:{sid}"
                    out.append({"id": _report_id(persona, name or "", str(sid), last[1]),
                                "external_id": external_id,
                                "external_source": "hermes-cron",
                                "session_id": sid, "label": label, "name": name,
                                "content": _clean_report(last[0]), "ts": last[1]})
                if len(out) >= limit:
                    break
            con.close()
            return out
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_reports", _exc, expected=True)
        return []


def _write_report_memory(session: str, reports: list[dict]) -> None:
    if session not in PERSONAS:
        return
    home = home_for(session)
    memdir = os.path.join(home, "memories")
    try:
        os.makedirs(memdir, exist_ok=True)
        latest = sorted(reports, key=lambda r: r.get("ts") or 0, reverse=True)[:REPORT_MEMORY_ITEMS]
        lines = [
            "# REPORTS.md",
            "",
            "PocketAgent/Hermes bridge 維護的近期報告索引。全文 canonical 存在",
            "`~/.local/share/pocket-agent/canonical.db` 的 `report_events` 表；",
            "此檔提供 persona / studio-memory 快速讀取最近報告脈絡。",
            "",
        ]
        for r in latest:
            lines += [
                f"## {_fmt_ts(r.get('ts'))} {r.get('label') or r.get('name') or '報告'}",
                f"- session: {session}",
                f"- source: {r.get('external_source') or 'hermes-cron'}",
                f"- external_id: {r.get('external_id') or r.get('id')}",
                "",
                _clip_text(r.get("content") or "", REPORT_MEMORY_CHARS),
                "",
            ]
        tmp = os.path.join(memdir, f".{REPORT_MEMORY_FILE}.tmp")
        final = os.path.join(memdir, REPORT_MEMORY_FILE)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp, final)
    except Exception as e:  # noqa: BLE001
        _log_event("report_memory_write_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])


def _sync_persona_reports(session: str, limit: int = 50) -> list[dict]:
    reports = _persona_reports(session, limit)
    if TOOL_ERROR_REPORTS_ENABLED:
        reports.extend(_persona_tool_error_reports(session, min(limit, 20)))
    # Continual Harness 的待審提案段(HARNESS=1 才有;關著時回空陣列)。
    # 掛在這裡而不是改寫 cron 晨報本文:改本文會動到內容雜湊,`_report_upsert`
    # 每次都當成新報告重新鏡射一則聊天訊息 —— 這裡走獨立 report,一天一則。
    reports.extend(_persona_harness_reports(session))
    if not reports:
        return _report_events(session, limit, newest_first=True)
    upserted = 0
    for r in reports:
        try:
            if _report_upsert(session, r):
                upserted += 1
                # 2026-08-10 移除:報告不再自動生 kind=notice「知道了」approval。
                # 報告本體已經走 TG/推播送達、且在對話裡本來就有一張報告卡,這顆
                # 「知道了」是多餘的已讀動作 —— 按下去 POST 決議會擾動人格卡片流
                # (使用者回報「按知道了就回覆斷線」)。改為不建立(_notice_for_report
                #  仍保留,供未來明確需要通知中心入口時再接;晨報 job 名單見
                #  NOTICE_REPORT_JOBS)。
        except Exception as e:  # noqa: BLE001
            _log_event("report_event_write_failed", session=session,
                       report_id=r.get("id"), label=r.get("label"),
                       error=type(e).__name__, error_message=str(e)[:160])
    latest = _report_events(session, max(limit, REPORT_MEMORY_ITEMS), newest_first=True,
                            include_diagnostics=True)
    visible_latest = [r for r in latest if not _is_hidden_report(r)]
    _write_report_memory(session, visible_latest)
    if upserted:
        _log_event("report_events_synced", session=session, count=upserted)
    return latest


_REPORT_QUERY_RE = re.compile(
    r"(報告|晨報|午報|盤前|晚間|三省|晨卦|卦|速覽|編輯台|名家觀點|"
    r"剛剛|今天|今日|第二點|第三點|上面|前面|剛才)"
)


def _report_context_for_prompt(session: str, user_text: str) -> str:
    reports = _sync_persona_reports(session, 50)
    if not reports:
        return ""
    want_more = bool(_REPORT_QUERY_RE.search(user_text or ""))
    max_items = REPORT_CONTEXT_TRIGGERED if want_more else REPORT_CONTEXT_DEFAULT
    selected = reports[:max_items]  # newest-first from _sync_persona_reports
    budget = REPORT_CONTEXT_CHARS
    blocks = []
    for r in selected:
        title = f"{_fmt_ts(r.get('ts'))} {r.get('label') or r.get('name') or '報告'}"
        content = _clip_text(r.get("content") or "", min(REPORT_CONTEXT_ITEM_CHARS, budget))
        block = f"### {title}\n{content}".strip()
        if len(block) > budget:
            block = _clip_text(block, budget)
        blocks.append(block)
        budget -= len(block)
        if budget <= 1200:
            break
    if not blocks:
        return ""
    return (
        "【PocketAgent 近期報告上下文】\n"
        "以下是這個 persona 最近已投遞到 PocketAgent 對話窗的報告內容。"
        "使用者若提到報告、晨報、午報、盤前、晚間、晨卦、剛剛、上面或第幾點，"
        "必須優先以這些報告內容回答；若資訊不足，再明確說需要查原始來源。\n\n"
        + "\n\n".join(blocks)
    )


@app.get("/cron/jobs")
async def cron_jobs(request: Request):
    _check_auth(request)
    return {"jobs": _cron_jobs()}


@app.post("/cron/jobs/{job_id}/{action}")
async def cron_toggle(job_id: str, action: str, request: Request):
    _check_auth(request)
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=400, detail="action must be pause|resume")
    if not any(j["id"] == job_id for j in _cron_jobs()):
        raise HTTPException(status_code=404, detail="unknown job")
    ok, msg = await _hermes_cron(action, job_id)
    if not ok:
        raise HTTPException(status_code=500, detail=msg[:300] or "toggle failed")
    return {"ok": True, "enabled": action == "resume"}


@app.get("/reports")
async def reports(request: Request, limit: int = 20):
    _check_auth(request)
    return {"reports": _reports(max(1, min(limit, 50)))}


# ───────────────────────── versioned app API (M20) ─────────────────────────
# The app's stable contract. Wraps the Hermes internals (state.db, cron JSON,
# ACP) so the client never depends on them directly.

@app.post("/app/v1/uploads")
async def app_uploads(request: Request):
    """Pre-upload composer attachments and return stable local file references.

    Mobile clients should call this before sending a turn with images/files, then
    submit the lightweight returned `path` fields to persona/CC/Codex endpoints.
    Legacy clients can still send `data` directly to those endpoints.
    """
    _check_auth(request)
    body = await request.json()
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list):
        raise HTTPException(status_code=400, detail="attachments must be a list")
    if len(attachments) > _ATT_MAX_COUNT:
        raise HTTPException(status_code=413, detail="too many attachments")
    # 修復單「附件限制」:單檔/總量預檢(base64 長度估算,不先解碼)——
    # 之前 size 只進 log,從不比對任何上限。
    total_est = 0
    for idx, a in enumerate(attachments):
        if not isinstance(a, dict):
            raise HTTPException(status_code=400, detail=f"attachment {idx} must be an object")
        est = _data_uri_estimated_bytes(str(a.get("data") or ""))
        total_est += est
        if est > _ATT_MAX_FILE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"attachment {idx} ({a.get('filename') or 'file'}) "
                                       f"超過單檔上限 {_ATT_MAX_FILE_BYTES} bytes")
    if total_est > _ATT_MAX_COUNT * _ATT_MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="attachments 總量超過上限")
    saved = []
    for idx, a in enumerate(attachments):
        if not isinstance(a, dict):
            raise HTTPException(status_code=400, detail=f"attachment {idx} must be an object")
        path = _save_attachment(a, a.get("filename") or "file")
        if not path:
            raise HTTPException(status_code=400, detail=f"attachment {idx} upload failed")
        try:
            size = Path(path).stat().st_size
        except Exception as _exc:  # noqa: BLE001
            _log_exc("app_uploads", _exc, expected=True)
            size = 0
        saved.append({
            "kind": a.get("kind") or "file",
            "filename": a.get("filename") or Path(path).name,
            "mime": a.get("mime") or "application/octet-stream",
            "path": path,
            "size": size,
        })
    _log_event("app_uploads_saved", attachment_count=len(saved),
               bytes=sum(int(a.get("size") or 0) for a in saved),
               client=_client_host(request))
    return {"ok": True, "attachments": saved}


_UPLOAD_CHUNK = 1 << 20      # 1MB:串流落盤的讀取塊大小


@app.post("/app/v1/uploads/file")
async def app_upload_file(request: Request,
                          file: UploadFile = File(...),
                          kind: str = Form("file"),
                          filename: str = Form(""),
                          mime: str = Form("")):
    """逐件上傳(multipart)—— 讓 app 畫得出**單一附件的**上傳進度。

    為什麼不沿用 `/app/v1/uploads`:那支是「一次收整包 base64 JSON」,client 在
    請求送完之前拿不到任何回饋,而且 base64 讓傳輸量膨脹 33%。逐件 + 原始位元組
    之後,app 端才有辦法用 URLSession 的 didSendBodyData 畫出每個 chip 的進度。

    舊端點**原封不動保留** —— 舊版 app 還在用它,而且離線補送路徑也走同一支。
    這裡只是多一條路,不是取代。

    串流落盤:一次讀 1MB,不把整個檔案堆進記憶體(舊路徑得先把整包 base64 讀進
    來再 decode,大型影片也只會由 multipart parser 串流落盤)。超過單檔上限就中止並刪掉
    半套檔案 —— 不留下一個「看起來上傳好了」的截斷檔。
    """
    _check_auth(request)
    name = (filename or file.filename or "file").strip()
    content_type = (mime or file.content_type or "application/octet-stream").strip()
    path = _upload_dest_path(name, content_type)
    written = 0
    try:
        with open(path, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > _ATT_MAX_FILE_BYTES:
                    raise _UploadTooLarge()
                out.write(chunk)
    except _UploadTooLarge:
        _unlink_quietly(path)
        _log_event("app_upload_file_rejected", reason="too_large",
                   filename=name[:80], mime=content_type[:60],
                   limit=_ATT_MAX_FILE_BYTES)
        raise http_err(413, "ATTACHMENT_TOO_LARGE",
                       f"超過單檔上限 {_ATT_MAX_FILE_BYTES} bytes")
    except Exception as exc:  # noqa: BLE001
        _unlink_quietly(path)
        _log_event("app_upload_file_failed", filename=name[:80],
                   error=type(exc).__name__, error_message=str(exc)[:160])
        raise HTTPException(status_code=500, detail="upload failed")
    _log_event("app_upload_file_saved", filename=name[:80],
               mime=content_type[:60], bytes=written,
               client=_client_host(request))
    return {"ok": True, "attachment": {
        "kind": kind or "file",
        "filename": name,
        "mime": content_type,
        "path": str(path),
        "size": written,
    }}


@app.post("/app/v1/uploads/raw")
async def app_upload_raw(request: Request):
    """Upload one raw file stream without multipart or base64 buffering.

    Pocket sends the file directly from disk with URLSession.upload(fromFile:),
    so a 2GiB attachment never needs a second in-memory representation or a
    temporary multipart body. Metadata travels in ASCII-safe headers.
    """
    _check_auth(request)
    try:
        encoded_name = request.headers.get("x-pocket-filename-b64") or ""
        name = base64.b64decode(encoded_name, validate=True).decode("utf-8") if encoded_name else ""
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid filename metadata")
    name = (name or request.headers.get("x-pocket-filename") or "file").strip()
    kind = (request.headers.get("x-pocket-kind") or "file").strip()
    if kind not in {"image", "file", "audio"}:
        kind = "file"
    content_type = (request.headers.get("x-pocket-mime") or
                    request.headers.get("content-type") or
                    "application/octet-stream").strip()
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > _ATT_MAX_FILE_BYTES:
        raise http_err(413, "ATTACHMENT_TOO_LARGE",
                       f"超過單檔上限 {_ATT_MAX_FILE_BYTES} bytes")

    path = _upload_dest_path(name, content_type)
    written = 0
    try:
        with open(path, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > _ATT_MAX_FILE_BYTES:
                    raise _UploadTooLarge()
                out.write(chunk)
    except _UploadTooLarge:
        _unlink_quietly(path)
        _log_event("app_upload_raw_rejected", reason="too_large",
                   filename=name[:80], mime=content_type[:60],
                   limit=_ATT_MAX_FILE_BYTES)
        raise http_err(413, "ATTACHMENT_TOO_LARGE",
                       f"超過單檔上限 {_ATT_MAX_FILE_BYTES} bytes")
    except Exception as exc:  # noqa: BLE001
        _unlink_quietly(path)
        _log_event("app_upload_raw_failed", filename=name[:80],
                   error=type(exc).__name__, error_message=str(exc)[:160])
        raise HTTPException(status_code=500, detail="upload failed")
    _log_event("app_upload_raw_saved", filename=name[:80],
               mime=content_type[:60], bytes=written,
               client=_client_host(request))
    return {"ok": True, "attachment": {
        "kind": kind,
        "filename": name,
        "mime": content_type,
        "path": str(path),
        "size": written,
    }}


class _UploadTooLarge(Exception):
    """串流途中超過單檔上限 —— 內部訊號,轉成 413。"""


def _unlink_quietly(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_unlink_quietly", _exc, expected=True)


def _media_persona_home(persona: str) -> str:
    persona_id = str(persona or "").strip() or "xcash"
    if persona_id not in PERSONAS:
        raise http_err(404, "PERSONA_NOT_FOUND", "unknown persona")
    return home_for(persona_id)


@app.get("/app/v2/hermes/media-capabilities")
async def app_hermes_media_capabilities(
    request: Request,
    persona: str = "xcash",
    probe: bool = True,
):
    """Secret-free effective media settings from the selected Hermes profile."""
    _check_auth(request)
    persona = str(persona or "").strip() or "xcash"
    home = _media_persona_home(persona)
    try:
        capabilities = await asyncio.to_thread(
            hermes_media.get_capabilities,
            home,
            probe=probe,
            attachment_max_bytes=_ATT_MAX_FILE_BYTES,
            attachment_max_count=_ATT_MAX_COUNT,
        )
    except hermes_media.HermesMediaError as exc:
        raise http_err(503, "HERMES_MEDIA_UNAVAILABLE", str(exc)[:240])
    return {
        "persona": persona,
        "profile": _persona_profile_of(home),
        **capabilities,
    }


@app.put("/app/v2/hermes/media-settings")
async def app_hermes_media_settings(
    request: Request,
    persona: str = "xcash",
):
    """Update allowlisted Hermes media settings; owner authorization only."""
    _require_master_auth(request)
    persona = str(persona or "").strip() or "xcash"
    home = _media_persona_home(persona)
    body = await _json_body(request)
    try:
        capabilities = await asyncio.to_thread(
            hermes_media.update_settings,
            home,
            body,
        )
    except hermes_media.HermesMediaError as exc:
        raise http_err(400, "HERMES_MEDIA_SETTINGS_INVALID", str(exc)[:240])
    _log_event(
        "hermes_media_settings_updated",
        persona=persona,
        stt_provider=(capabilities.get("stt") or {}).get("provider"),
        ocr_provider=(capabilities.get("ocr") or {}).get("provider"),
    )
    return {
        "ok": True,
        "persona": persona,
        "profile": _persona_profile_of(home),
        **capabilities,
    }


# ───────────────────────── in-app terminal (PTY over WS) ────────────────────
# docs/TERMINAL_PTY_CONTRACT.md is the authority; keep this section in sync
# with it. One WS = one local PTY login shell running as the bridge's own
# execution identity (no privilege escalation, no user switch). A paired
# device token therefore equals full shell access — see POCKET_TERMINAL_ENABLED
# above for the kill switch, and §日誌 below for what never gets logged.

def _terminal_token_from_ws(websocket: WebSocket) -> str:
    """Same device-token contract as every other /app/v1/* endpoint
    (`Authorization: Bearer <token>`), plus a `?token=` query fallback for
    WS clients that can't set a header on the upgrade request."""
    auth = websocket.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = (websocket.query_params.get("token") or "").strip()
    return token


def _terminal_device_id_for_token(token: str) -> str | None:
    """Mirrors _check_auth's token membership checks (master token, per-device
    token, account-bound device) but returns a device id for logging instead
    of raising — a WS handshake rejects with a close code, not an
    HTTPException."""
    if not token:
        return None
    if hmac.compare_digest(token, BRIDGE_TOKEN):
        return "master"
    with _PAIR_LOCK:
        dev = _DEVICE_TOKENS.get(token)
        if dev is not None:
            if not dev.get("apple_user_id") or _account_device_for_token(token) is not None:
                dev["last_seen"] = time.time()
                return dev.get("device_id") or _short_hash(token)
    acct_dev = _account_device_for_token(token)
    if acct_dev is not None:
        return acct_dev.get("device_id") or _short_hash(token)
    return None


class _TerminalSession:
    """One local PTY + login shell child process for one terminal WS."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._write_buf = bytearray()

    def start(self) -> None:
        shell = os.environ.get("SHELL") or "/bin/zsh"
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        home = os.path.expanduser("~")
        master_fd, slave_fd = pty.openpty()
        try:
            self.proc = subprocess.Popen(
                [shell, "-l"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=home, env=env,
                preexec_fn=os.setsid,   # new session/process group → clean signal targeting
                close_fds=True,
            )
        finally:
            os.close(slave_fd)   # child already dup'd it; parent only needs master
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.master_fd = master_fd

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        try:
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
        except (OSError, ValueError, struct.error):
            pass

    def write(self, data: str) -> None:
        """Queue client keystrokes for the PTY. Non-blocking: the master fd is
        O_NONBLOCK, so a full PTY input buffer (e.g. a huge paste) falls back
        to an event-loop writer instead of blocking the whole bridge."""
        if self.master_fd is None or not data:
            return
        self._write_buf.extend(data.encode("utf-8", "replace"))
        self._flush_write()

    def _flush_write(self) -> None:
        if self.master_fd is None:
            self._write_buf.clear()
            return
        try:
            while self._write_buf:
                n = os.write(self.master_fd, bytes(self._write_buf))
                del self._write_buf[:n]
            try:
                self._loop.remove_writer(self.master_fd)
            except (ValueError, OSError):
                pass
        except BlockingIOError:
            self._loop.add_writer(self.master_fd, self._flush_write)
        except OSError:
            self._write_buf.clear()

    def read_nonblocking(self, size: int = 65536) -> bytes | None:
        """None means EOF (shell exited); b"" means nothing was ready (kept
        defensive — add_reader should only fire when data is available)."""
        try:
            data = os.read(self.master_fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return data if data else None

    def exit_code(self) -> int:
        """Blocking (bounded); always call via run_in_executor, never inline
        on the event loop."""
        if self.proc is None:
            return -1
        try:
            return self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return self.proc.returncode if self.proc.returncode is not None else -1

    def close(self) -> None:
        """Blocking (bounded); always call via run_in_executor. Kills the
        shell's whole process group and reaps it — no zombies, no fd leak."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except (ProcessLookupError, OSError):
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    self.proc.wait(timeout=2)
                except Exception as _exc:  # noqa: BLE001
                    _log_exc("_TerminalSession.close", _exc, expected=True)
                    pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


async def _terminal_recv_loop(websocket: WebSocket, session: "_TerminalSession") -> None:
    """Client → server: {"type":"input"} and {"type":"resize"} only (contract
    §訊息). Unknown message types/fields are ignored, not rejected, so the app
    can add fields later without a bridge redeploy."""
    while True:
        try:
            raw = await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype == "input":
            data = msg.get("data")
            if isinstance(data, str) and data:
                session.write(data)
        elif mtype == "resize":
            try:
                cols = int(msg.get("cols") or 0)
                rows = int(msg.get("rows") or 0)
            except (TypeError, ValueError):
                continue
            if cols > 0 and rows > 0:
                session.resize(cols, rows)


@app.websocket("/app/v1/terminal")
async def terminal_ws(websocket: WebSocket) -> None:
    if not POCKET_TERMINAL_ENABLED:
        # Pre-accept reject. uvicorn's ASGI websocket implementation hardcodes
        # HTTP 403 for any pre-accept `websocket.close` regardless of the code
        # passed (it discards the numeric close code entirely for handshake
        # rejections) — that happens to be exactly the "端點回 403" the
        # contract asks for here, so no accept() round-trip is needed.
        await websocket.close(code=1013)
        return
    token = _terminal_token_from_ws(websocket)
    device_id = _terminal_device_id_for_token(token)
    if not device_id:
        # A pre-accept close would also flatten to plain HTTP 403 (see above),
        # which loses the "4401" the contract asks for. Accept first so the
        # rejection is a real WS close *frame*, whose code the client can read.
        await websocket.accept()
        try:
            await websocket.send_json({"type": "error", "message": "invalid or missing device token"})
        except Exception as _exc:  # noqa: BLE001
            _log_exc("terminal_ws", _exc, expected=True)
            pass
        await websocket.close(code=4401)
        return

    await websocket.accept()
    loop = asyncio.get_running_loop()
    session = _TerminalSession(loop)
    try:
        session.start()
    except Exception as e:  # noqa: BLE001 — PTY/shell spawn failed
        _log_exc("terminal_ws#2", e, expected=True)
        try:
            await websocket.send_json({"type": "error",
                                       "message": f"pty spawn failed: {type(e).__name__}"})
        except Exception as _exc:  # noqa: BLE001
            _log_exc("terminal_ws#3", _exc, expected=True)
            pass
        await websocket.close(code=1011)
        return

    master_fd = session.master_fd
    started_at = time.time()
    exited = asyncio.Event()
    output_q: asyncio.Queue = asyncio.Queue()
    _EOF = object()

    def _on_readable() -> None:
        chunk = session.read_nonblocking()
        if chunk is None:
            try:
                loop.remove_reader(master_fd)
            except (ValueError, OSError):
                pass
            exited.set()
            output_q.put_nowait(_EOF)
            return
        if chunk:
            output_q.put_nowait(chunk)

    loop.add_reader(master_fd, _on_readable)
    _log_event("terminal_open", device_id=device_id)  # never log keystrokes/output

    async def _writer_loop() -> None:
        while True:
            item = await output_q.get()
            if item is _EOF:
                return
            try:
                await websocket.send_json({"type": "output",
                                           "data": item.decode("utf-8", "replace")})
            except Exception as _exc:  # noqa: BLE001 — client gone; recv/exit path cleans up
                _log_exc("terminal_ws._writer_loop", _exc, expected=True)
                return

    recv_task = asyncio.create_task(_terminal_recv_loop(websocket, session))
    writer_task = asyncio.create_task(_writer_loop())
    exit_task = asyncio.create_task(exited.wait())
    try:
        await asyncio.wait({recv_task, exit_task}, return_when=asyncio.FIRST_COMPLETED)
        if exit_task.done():
            await writer_task  # drain any output queued before the EOF sentinel
            code = await loop.run_in_executor(None, session.exit_code)
            try:
                await websocket.send_json({"type": "exit", "code": code})
            except Exception as _exc:  # noqa: BLE001
                _log_exc("terminal_ws#4", _exc, expected=True)
                pass
    finally:
        for t in (recv_task, writer_task, exit_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(recv_task, writer_task, exit_task, return_exceptions=True)
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
        try:
            loop.remove_writer(master_fd)
        except (ValueError, OSError):
            pass
        await loop.run_in_executor(None, session.close)
        try:
            await websocket.close(code=1000)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("terminal_ws#5", _exc, expected=True)
            pass
        _log_event("terminal_close", device_id=device_id,
                   duration_s=round(time.time() - started_at, 3))


@app.get("/capabilities")
async def capabilities(request: Request):
    _check_auth(request)
    return {"api": "app/v1",
            "features": ["canonical_messages", "reports", "notifications",
                         "approvals", "cc_sessions", "attachments", "vision",
                         "message_dry_run", "message_interrupt", "message_status",
                         "message_events", "apns_push", "accounts",
                         "apple_auth", "apple_web_auth", "account_pairing",
                         "delegations", "control_plane_v2", "attachment_uploads",
                         "interactive_push", "media_artifacts",
                         "hermes_media_capabilities",
                         "hermes_media_settings",
                         "push_register", "dashboard", "openclaw_config"] +
                        (["terminal"] if POCKET_TERMINAL_ENABLED else []) +
                        (["openclaw_provider"] if OPENCLAW.configured() else []),
            "terminal": _terminal_capabilities(),
            "endpoints": ["/app/v1/sessions", "/app/v1/messages", "/reports",
                          "/app/v1/uploads",
                          "/app/v1/reactions", "/app/v1/pins",
                          "/app/v1/messages/{id}", "/app/v1/sessions/{id}/pin",
                          "/app/v1/messages/retract", "/app/v1/personas",
                          "/app/v1/messages/status", "/app/v1/messages/events",
                          "/app/v1/messages/interrupt",
                          "/cron/jobs", "/ccsessions", "/app/v1/approvals",
                          "/app/v1/devices", "/app/v1/push/test",
                          "/app/v1/push/register",
                          "/app/v1/auth/apple", "/app/v1/account",
                          "/app/v1/auth/apple/web/start",
                          "/app/v1/auth/apple/web/callback",
                          "/app/v1/auth/apple/web/status",
                          "/app/v1/pair/new", "/app/v1/pair/claim",
                          "/app/v1/devices/{id}/revoke",
                          "/app/v1/delegations", "/app/v2/sessions",
                          "/app/v2/sessions/{id}/media",
                          "/app/v2/artifacts/{media_id}",
                          "/app/v2/hermes/media-capabilities",
                          "/app/v2/hermes/media-settings",
                          "/app/v2/sessions/{id}/approve", "/app/v1/terminal",
                          "/app/v1/usage", "/app/v1/dashboard",
                          "/app/v1/openclaw/config"]}


@app.post("/app/v1/auth/apple")
async def app_auth_apple(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    apple_user_id = str(body.get("apple_user_id") or body.get("appleUserID") or "").strip()
    identity_token = str(body.get("identityToken") or body.get("identity_token") or "").strip()
    if not apple_user_id:
        raise HTTPException(status_code=400, detail="apple_user_id required")
    if not identity_token:
        raise HTTPException(status_code=400, detail="identityToken required")
    claims = await asyncio.to_thread(_apple_verify_identity_token, identity_token)
    if claims.get("sub") != apple_user_id:
        _log_event("apple_auth_subject_mismatch",
                   apple_user_hash=_short_hash(apple_user_id),
                   token_subject_hash=_short_hash(claims.get("sub")))
        raise HTTPException(status_code=401, detail="apple user id mismatch")

    display_name = body.get("display_name") or body.get("displayName") or body.get("name")
    if isinstance(display_name, dict):
        display_name = " ".join(
            str(display_name.get(k) or "").strip()
            for k in ("givenName", "familyName") if display_name.get(k)
        ).strip()
    display_name = str(display_name or "").strip() or None
    email = str(body.get("email") or claims.get("email") or "").strip() or None
    user = _account_upsert_user(apple_user_id, email=email, display_name=display_name)
    session_token, expires_at = _account_session_create(apple_user_id)
    _log_event("apple_auth_success",
               apple_user_hash=_short_hash(apple_user_id),
               audience=str(claims.get("aud") or ""))
    return {
        "ok": True,
        "user": _account_public_user(user),
        "session": {
            "type": "account",
            "token": session_token,
            "expires_at": expires_at,
        },
    }


@app.post("/app/v1/auth/apple/web/start")
async def app_auth_apple_web_start(request: Request):
    client_hash = _apple_web_check_start_rate(request)
    config_error = _apple_web_config_error()
    if config_error:
        _log_event("apple_web_auth_not_configured", reason=config_error)
        raise HTTPException(status_code=503, detail="web Apple sign-in is not configured")
    flow = _apple_web_new_flow()
    authorization_url = APPLE_WEB_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": APPLE_WEB_CLIENT_ID,
        "redirect_uri": APPLE_WEB_REDIRECT_URI,
        "response_type": "code id_token",
        "response_mode": "form_post",
        "scope": "name email",
        "state": flow["state"],
        "nonce": flow["nonce"],
    })
    _log_event("apple_web_auth_started",
               flow_hash=_short_hash(flow["flow_id"]), client_hash=client_hash)
    return {
        "ok": True,
        "flow_id": flow["flow_id"],
        "poll_secret": flow["poll_secret"],
        "authorization_url": authorization_url,
        "expires_at": int(flow["expires_at"]),
        "poll_interval": 2,
    }


@app.post("/app/v1/auth/apple/web/status")
async def app_auth_apple_web_status(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    flow_id = str(body.get("flow_id") or "").strip()
    poll_secret = str(body.get("poll_secret") or "").strip()
    if not flow_id or not poll_secret:
        raise HTTPException(status_code=400, detail="flow_id and poll_secret required")
    with _APPLE_WEB_FLOW_LOCK:
        _apple_web_cleanup_locked()
        flow = _APPLE_WEB_FLOWS.get(flow_id)
        if not flow or not hmac.compare_digest(
                str(flow.get("poll_secret") or ""), poll_secret):
            raise HTTPException(status_code=404, detail="sign-in attempt not found")
        status = str(flow.get("status") or "pending")
        expires_at = int(flow.get("expires_at") or 0)
        if status == "complete":
            result = flow.get("result") or {}
            _APPLE_WEB_FLOWS.pop(flow_id, None)
        elif status in ("failed", "cancelled"):
            result = {"error": str(flow.get("error") or status)}
            _APPLE_WEB_FLOWS.pop(flow_id, None)
        else:
            result = None
    if status == "complete":
        return {"ok": True, "status": status, **result}
    if status in ("failed", "cancelled"):
        return {"ok": False, "status": status, **result}
    return {"ok": True, "status": status, "expires_at": expires_at}


@app.get("/app/v1/auth/apple/web/callback")
async def app_auth_apple_web_callback_get():
    return _apple_web_callback_page("failed")


@app.post("/app/v1/auth/apple/web/callback")
async def app_auth_apple_web_callback(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return _apple_web_callback_page("failed")
    raw_body = await request.body()
    if len(raw_body) > 16 * 1024:
        return _apple_web_callback_page("failed")
    try:
        form = urllib.parse.parse_qs(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=16,
        )
    except (UnicodeDecodeError, ValueError):
        return _apple_web_callback_page("failed")

    def field(name: str) -> str:
        values = form.get(name) or []
        return str(values[0]).strip() if len(values) == 1 else ""

    state = field("state")
    if not state:
        return _apple_web_callback_page("failed")
    flow = _apple_web_claim_flow(state)
    if not flow:
        return _apple_web_callback_page("failed")

    flow_id = str(flow["flow_id"])
    apple_error = field("error")
    if apple_error:
        status = "cancelled" if apple_error == "user_cancelled_authorize" else "failed"
        error = "cancelled" if status == "cancelled" else "authorization_failed"
        _apple_web_finish_flow(flow_id, status, error=error)
        _log_event("apple_web_auth_cancelled" if status == "cancelled"
                   else "apple_web_auth_failed",
                   flow_hash=_short_hash(flow_id), apple_error=apple_error[:80])
        return _apple_web_callback_page(status)

    try:
        code = field("code")
        front_identity_token = field("id_token")
        if not code or not front_identity_token:
            raise ValueError("missing authorization response")
        front_claims = await asyncio.to_thread(
            _apple_verify_identity_token,
            front_identity_token,
            APPLE_WEB_CLIENT_ID,
        )
        expected_nonce = str(flow.get("nonce") or "")
        actual_nonce = str(front_claims.get("nonce") or "")
        if not expected_nonce or not hmac.compare_digest(expected_nonce, actual_nonce):
            raise ValueError("nonce mismatch")

        token_payload = await _apple_web_exchange_code(code)
        exchanged_claims = await asyncio.to_thread(
            _apple_verify_identity_token,
            str(token_payload["id_token"]),
            APPLE_WEB_CLIENT_ID,
        )
        if exchanged_claims.get("sub") != front_claims.get("sub"):
            raise ValueError("subject mismatch")
        exchanged_nonce = str(exchanged_claims.get("nonce") or "")
        if exchanged_nonce and not hmac.compare_digest(expected_nonce, exchanged_nonce):
            raise ValueError("exchanged nonce mismatch")

        user_payload = {}
        raw_user = field("user")
        if raw_user:
            parsed_user = json.loads(raw_user)
            if not isinstance(parsed_user, dict):
                raise ValueError("bad user payload")
            user_payload = parsed_user
        apple_user_id = str(exchanged_claims.get("sub") or "").strip()
        if not apple_user_id:
            raise ValueError("missing subject")
        email = str(
            exchanged_claims.get("email") or front_claims.get("email")
            or user_payload.get("email") or ""
        ).strip() or None
        display_name = _apple_web_display_name(user_payload)
        _apple_web_finish_flow(
            flow_id,
            "complete",
            result={
                "identity": {
                    "apple_user_id": apple_user_id,
                    "identity_token": str(token_payload["id_token"]),
                    "email": email,
                    "display_name": display_name,
                },
            },
        )
        _log_event(
            "apple_web_auth_success",
            flow_hash=_short_hash(flow_id),
            apple_user_hash=_short_hash(apple_user_id),
            audience=str(exchanged_claims.get("aud") or ""),
        )
        return _apple_web_callback_page("success")
    except Exception as e:  # noqa: BLE001
        _apple_web_finish_flow(flow_id, "failed", error="verification_failed")
        _log_event(
            "apple_web_auth_failed",
            flow_hash=_short_hash(flow_id),
            error=type(e).__name__,
        )
        return _apple_web_callback_page("failed")


# ── /app/v1/usage: Codex + Claude Code 本機用量(不打雲端 API)──────────
# 資料來源事實(僅取自 AIBar README 描述的檔案格式知識,程式碼為本專案重寫,
# 沒有看過/拷貝過它的 Swift 原始碼):
#   1. Codex:~/.codex/sessions/**/*.jsonl 裡 event_msg.token_count 的
#      payload.rate_limits(primary=5h window, secondary=7d/weekly window)。
#      剩餘額度 = 100 - used_percent。只挑「最近修改的幾個」session 檔、
#      每檔只讀尾部,避免整檔掃描動輒上百 MB 的 jsonl。
#   2. Claude 官方額度:~/.ai-usage/claude-status/*.json(Claude Code 官方
#      statusLine hook 寫入)。本機若沒裝這個 hook,目錄根本不存在 ——
#      必須優雅地回 available:false / official_synced:false,不能報錯。
#   3. Claude 本機備援:~/.claude/projects/**/*.jsonl 裡 assistant message
#      的 usage 欄位,用 message.id 去重(同一個 assistant turn 常因串流/
#      重試在 jsonl 裡留下多筆同 id 記錄)後加總 token 數。這只有 token
#      count,沒有官方配額百分比或重置時間。
#
# 信任邊界(AIBar README 明訂的設計原則,這裡照樣遵守):Claude 的官方額度
# 只能來自 claude-status/*.json 的 rate_limits;來源不存在、沒有
# rate_limits、或該視窗已過期,一律視為未同步 —— 絕對不能拿
# plan-usage-history.json / Desktop cache / IndexedDB 解析結果 / 本地
# token 加總去推算一個看起來像官方的百分比。
_USAGE_CACHE = {"ts": 0.0, "data": None}
_USAGE_CACHE_TTL = 10.0  # 秒;擋掉 app 端高頻輪詢造成的重複 jsonl 全掃

CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CLAUDE_STATUS_DIR = os.path.expanduser("~/.ai-usage/claude-status")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

_USAGE_TAIL_BYTES = 200_000   # 每個 session 檔只讀最後 ~200KB 找 rate-limit
_USAGE_MAX_CODEX_FILES = 8    # 只挑最近修改的幾個 codex session 檔
_USAGE_MAX_CLAUDE_FILES = 40  # 備援統計只掃最近修改的幾個 claude jsonl


def _usage_iso_utc(epoch_seconds):
    """Unix epoch (int/float, seconds) -> ISO8601 UTC string, or None."""
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_usage_iso_utc", _exc, expected=True)
        return None


def _usage_now_iso_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _usage_normalize_iso_str(ts):
    """jsonl timestamps are already ISO8601 UTC (e.g. '...T16:00:50.586Z');
    just drop sub-second precision so every usage field matches the same
    'YYYY-MM-DDTHH:MM:SSZ' shape."""
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_usage_normalize_iso_str", _exc, expected=True)
        return None


def _usage_newest_files(root, pattern="*.jsonl", limit=8):
    """Recently-modified files under root (recursive), newest first, capped
    at `limit` — this endpoint only ever needs the freshest session logs,
    never a full walk of a directory holding months of history."""
    try:
        paths = [str(p) for p in Path(root).rglob(pattern) if p.is_file()]
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_usage_newest_files", _exc, expected=True)
        return []
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths[:limit]


def _usage_tail_text(path, max_bytes=_USAGE_TAIL_BYTES):
    """Last max_bytes of a file, decoded loosely. Session jsonl files can run
    into the hundreds of MB; the newest token_count/rate_limits event (or the
    newest assistant usage record) is always near the end, so seeking from
    EOF instead of parsing the whole file line-by-line keeps this endpoint
    fast even on old, huge logs."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_usage_tail_text", _exc, expected=True)
        return ""


def _codex_latest_rate_limits():
    """Newest {timestamp, rate_limits} token_count event across the most
    recently modified codex session files, or None if nothing usable found."""
    best = None  # (timestamp_str, rate_limits_dict)
    for path in _usage_newest_files(CODEX_SESSIONS_DIR, "*.jsonl", _USAGE_MAX_CODEX_FILES):
        text = _usage_tail_text(path)
        if not text or '"token_count"' not in text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_codex_latest_rate_limits", _exc, expected=True)
                continue
            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            rl = payload.get("rate_limits")
            if not isinstance(rl, dict):
                continue
            ts = rec.get("timestamp") or ""
            if best is None or ts > best[0]:
                best = (ts, rl)
    if best is None:
        return None
    return {"timestamp": best[0], "rate_limits": best[1]}


def _codex_usage_snapshot():
    """codex wire block per /app/v1/usage contract. AIBar's rule: remaining
    quota is 100 - used_percent, headline percentage comes from the primary
    (5h) rate-limit window; resets_at comes straight from that same window."""
    found = _codex_latest_rate_limits()
    if not found:
        return {"available": False}
    primary = (found["rate_limits"].get("primary") or {})
    used_percent = primary.get("used_percent")
    if used_percent is None:
        return {"available": False}
    try:
        used_percent = float(used_percent)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_codex_usage_snapshot", _exc, expected=True)
        return {"available": False}
    return {
        "available": True,
        "used_percent": round(used_percent, 2),
        "remaining_percent": round(100.0 - used_percent, 2),
        "reset_at": _usage_iso_utc(primary.get("resets_at")),
        "source": "codex_sessions_jsonl",
        "last_synced_at": _usage_now_iso_utc(),
    }


def _claude_official_snapshot():
    """~/.ai-usage/claude-status/*.json written by Claude Code's official
    statusLine hook. Trust boundary (per AIBar's own design note): the ONLY
    legitimate source for Claude's official quota percentage is this file's
    rate_limits field. No hook installed / no rate_limits / an expired reset
    window all mean "not synced" — never backfill a percentage from anywhere
    else (Desktop cache, IndexedDB dumps, local jsonl token counts, etc.)."""
    try:
        files = [str(p) for p in Path(CLAUDE_STATUS_DIR).glob("*.json") if p.is_file()]
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_claude_official_snapshot", _exc, expected=True)
        return None
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    now = time.time()

    def _window(block):
        if not isinstance(block, dict):
            return None
        used = block.get("used_percentage", block.get("used_percent"))
        resets_at = block.get("resets_at")
        if used is None or resets_at is None:
            return None
        try:
            resets_at = float(resets_at)
            used = float(used)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_claude_official_snapshot._window", _exc, expected=True)
            return None
        if resets_at <= now:   # window 已過期 -> 視為未同步,不能沿用舊值
            return None
        return {
            "used_percent": round(used, 2),
            "remaining_percent": round(100.0 - used, 2),
            "reset_at": _usage_iso_utc(resets_at),
        }

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                doc = json.load(f)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_claude_official_snapshot#2", _exc, expected=True)
            continue
        rate_limits = doc.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        five_w = _window(rate_limits.get("five_hour"))
        seven_w = _window(rate_limits.get("seven_day"))
        if five_w is None and seven_w is None:
            continue  # 這份 status 檔沒有可用的 rate_limits -> 未同步
        return {
            "five_hour": five_w,
            "seven_day": seven_w,
            "mtime": os.path.getmtime(path),
            "account_label": doc.get("account_label") or doc.get("email") or None,
        }
    return None


def _claude_local_fallback_usage():
    """~/.claude/projects/**/*.jsonl assistant message usage, de-duplicated by
    (path, message.id) — a streamed/retried turn can appear multiple times in
    the log — and summed. Token counts ONLY, no percentage/reset time; exists
    purely so the app has *something* while official_synced is false, and
    must never be dressed up as an official quota number."""
    total_input = total_output = total_cache_read = total_cache_creation = 0
    seen_ids = set()
    latest_ts = None
    for path in _usage_newest_files(CLAUDE_PROJECTS_DIR, "*.jsonl", _USAGE_MAX_CLAUDE_FILES):
        text = _usage_tail_text(path)
        if not text or '"assistant"' not in text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_claude_local_fallback_usage", _exc, expected=True)
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = msg.get("id")
            dedupe_key = (path, mid) if mid else (path, line[:80])
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            total_input += int(usage.get("input_tokens") or 0)
            total_output += int(usage.get("output_tokens") or 0)
            total_cache_read += int(usage.get("cache_read_input_tokens") or 0)
            total_cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
            ts = rec.get("timestamp")
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
    if not seen_ids:
        return None
    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_creation,
        "latest_timestamp": latest_ts,
    }


def _claude_usage_snapshot():
    """claude wire block. official_synced only ever comes from the
    statusLine hook's rate_limits; the local jsonl fallback supplies raw
    token counts (not percent) when the hook isn't installed / hasn't
    produced a fresh window yet."""
    official = _claude_official_snapshot()
    if official is not None:
        return {
            "available": True,
            "official_synced": True,
            "five_hour": official["five_hour"],
            "seven_day": official["seven_day"],
            "source": "claude_statusline",
            "last_synced_at": _usage_iso_utc(official["mtime"]),
            "account_label": official.get("account_label"),
        }
    fallback = _claude_local_fallback_usage()
    if fallback is None:
        return {"available": False, "official_synced": False}
    return {
        "available": True,
        "official_synced": False,
        "five_hour": None,
        "seven_day": None,
        "source": "claude_projects_jsonl_fallback",
        "last_synced_at": _usage_normalize_iso_str(fallback["latest_timestamp"]) or _usage_now_iso_utc(),
        "account_label": None,
        "token_usage": {
            "input_tokens": fallback["input_tokens"],
            "output_tokens": fallback["output_tokens"],
            "cache_read_input_tokens": fallback["cache_read_input_tokens"],
            "cache_creation_input_tokens": fallback["cache_creation_input_tokens"],
        },
    }


@app.get("/app/v1/usage")
async def app_usage(request: Request):
    """Codex + Claude Code 本機用量,供 Pocket app 設定頁消費。純讀本機
    session/status 檔案,不打任何雲端用量 API(比照 AIBar 的做法)。
    10 秒快取,擋掉高頻輪詢造成的重複 jsonl 全掃。"""
    _check_auth(request)
    now = time.time()
    cached = _USAGE_CACHE["data"]
    if cached is not None and now - _USAGE_CACHE["ts"] < _USAGE_CACHE_TTL:
        return cached
    data = {
        "codex": _codex_usage_snapshot(),
        "claude": _claude_usage_snapshot(),
    }
    _USAGE_CACHE["data"] = data
    _USAGE_CACHE["ts"] = now
    return data


@app.get("/app/v1/account")
async def app_account(request: Request, include_revoked: bool = False):
    user = _account_user_from_request(request)
    devices = _account_devices_for_user(user["apple_user_id"], include_revoked=include_revoked)
    return {
        "user": _account_public_user(user),
        "devices": [_account_public_device(d) for d in devices],
    }


@app.post("/app/v1/devices/{device_id}/revoke")
async def app_revoke_account_device(device_id: str, request: Request):
    user = _account_user_from_request(request)
    device = _account_device_by_id(user["apple_user_id"], device_id)
    if not device:
        raise HTTPException(status_code=404, detail="unknown device")
    revoked = _account_device_revoke(user["apple_user_id"], device_id)
    token = device.get("device_token")
    if revoked and token:
        with _PAIR_LOCK:
            if token in _DEVICE_TOKENS:
                _DEVICE_TOKENS.pop(token, None)
                _save_device_tokens(_DEVICE_TOKENS)
    _log_event("account_device_revoked",
               apple_user_hash=_short_hash(user.get("apple_user_id")),
               device_id=device_id,
               token_hash=_short_hash(token))
    return {"revoked": revoked}


@app.get("/app/v1/sessions")
async def app_sessions(request: Request):
    return await list_sessions(request)


@app.get("/app/v1/delegations")
async def app_delegations(request: Request, parent_persona: str = "",
                          status: str = "", task_code: str = "", limit: int = 50):
    _check_auth(request)
    rows = _delegation_rows(limit=limit, parent_persona=parent_persona,
                             status=status, task_code=task_code)
    out = []
    for row in rows:
        out.append(_delegation_public(row, await _delegation_runtime_status(row)))
    return {"delegations": out}


@app.get("/app/v1/delegations/{delegation_id}")
async def app_delegation_get(delegation_id: str, request: Request):
    _check_auth(request)
    row = _delegation_get(delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown delegation")
    return {"delegation": _delegation_public(row, await _delegation_runtime_status(row))}


@app.post("/app/v1/delegations/{delegation_id}/input")
async def app_delegation_input(delegation_id: str, request: Request):
    _check_auth(request)
    row = _delegation_get(delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown delegation")
    d = dict(row)
    body = await request.json()
    text = (body.get("content") or body.get("text") or body.get("message") or "").strip()
    if not text and not body.get("attachments"):
        raise HTTPException(status_code=400, detail="content or attachments required")
    text = f"[工號 {d.get('work_order')}] {text}".strip()
    if d.get("provider") == "codex":
        thread_id = d.get("codex_thread_id") or d.get("provider_session_id") or ""
        if not thread_id:
            raise HTTPException(status_code=409, detail="delegation has no codex thread")
        input_items = await _codex_input_items(text, body.get("attachments") or [])
        try:
            res = await CODEX_APP.start_turn(thread_id, input_items,
                                             client_id=body.get("client_id"),
                                             cwd=d.get("cwd"))
        except Exception as e:  # noqa: BLE001
            _codex_http_error(e)
        _delegation_update(d["id"], status="running", updated_at=time.time(), last_error="")
        return {"ok": True, "delegation_id": d["id"], "work_order": d.get("work_order"),
                "provider": "codex", "thread_id": thread_id,
                "turn": (res or {}).get("turn")}
    if d.get("provider") == "claude_code":
        name = d.get("cc_session_name") or d.get("provider_session_id") or ""
        if not name:
            raise HTTPException(status_code=409, detail="delegation has no cc session")
        saved = []
        voice_lines = []
        for a in (body.get("attachments") or []):
            path = _save_attachment(a, a.get("filename") or "file")
            if not path:
                continue
            if a.get("kind") == "audio":
                t = await asyncio.to_thread(_transcribe, path)
                if t:
                    voice_lines.append(t)
            else:
                saved.append(path)
        if voice_lines:
            text += "\n\n[語音附件轉寫]\n" + " ".join(voice_lines)
        if saved:
            text += "\n\n[附件已存到本機,請用 Read 讀取/檢視]\n" + "\n".join(saved)
        await _cc_paste_text(name, text)
        _delegation_update(d["id"], status="running", updated_at=time.time(), last_error="")
        return {"ok": True, "delegation_id": d["id"], "work_order": d.get("work_order"),
                "provider": "claude_code", "session_name": name}
    raise HTTPException(status_code=400, detail="unsupported delegation provider")


@app.post("/app/v1/delegations/{delegation_id}/report")
async def app_delegation_report(delegation_id: str, request: Request):
    """子代理主動回報成果/里程碑(M1-3)。成果摘要由做事的人自己寫,品質最高、
    不靠 watcher 猜。body {summary, files?, verification?, status?}。id 或工號皆可。
    status=done/idle → 標完成並回流「已完成」;failed → 失敗;其餘 → 進度回報。"""
    _check_auth(request)
    row = _delegation_get(delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown delegation")
    d = dict(row)
    body = await request.json()
    summary = str(body.get("summary") or body.get("content") or "").strip()
    if not summary:
        raise HTTPException(status_code=400, detail="summary required")
    status_in = str(body.get("status") or "").strip().lower()
    meta = _delegation_meta(d)
    meta["last_report"] = {"summary": summary[:4000], "ts": time.time(),
                           "files": body.get("files") or [],
                           "verification": str(body.get("verification") or "")[:2000]}
    fields = {"meta": meta, "updated_at": time.time()}
    if status_in in ("done", "idle"):
        fields["status"] = "idle"
    elif status_in in ("failed", "running"):
        fields["status"] = status_in
    _delegation_update(d["id"], **fields)
    d["meta"] = json.dumps(meta, ensure_ascii=False)
    if "status" in fields:
        d["status"] = fields["status"]
    event = ("done" if status_in in ("done", "idle")
             else "failed" if status_in == "failed" else "report")
    await _delegation_notify(d, event, summary=summary)
    return {"ok": True, "delegation_id": d["id"], "work_order": d.get("work_order"),
            "event": event}


@app.post("/app/v1/delegations")
async def app_delegation_create(request: Request):
    """Create a durable CC/Codex work-order session.

    This is the shared dispatch surface for every Hermes persona. It creates a
    provider-native session first (Codex app-server thread or ccsess Claude Code
    session), then stores the parent persona + work_order mapping so Pocket,
    Telegram, and official provider surfaces can all point to the same work.
    """
    _check_auth(request)
    body = await request.json()
    parent = (body.get("parent_persona") or body.get("parent") or "xcash").strip()
    if parent not in PERSONAS:
        raise HTTPException(status_code=400, detail="unknown parent_persona")
    provider = _normalise_provider(body.get("provider") or body.get("tool") or "codex")
    objective = (body.get("objective") or body.get("task") or body.get("text") or "").strip()
    if not objective:
        raise HTTPException(status_code=400, detail="objective required")
    title = (body.get("title") or objective.splitlines()[0]).strip()[:120]
    cwd = _normalise_workdir(body.get("cwd") or body.get("workdir") or HOME_ROOT,
                             create=(provider == "claude_code"))

    task_code_raw = (body.get("task_code") or body.get("task_id") or "").strip()
    subtask_code_raw = (body.get("subtask_code") or body.get("subtask_id") or "").strip()
    explicit_work_order = (body.get("work_order") or "").strip()
    if not explicit_work_order and not (task_code_raw and subtask_code_raw):
        raise HTTPException(
            status_code=400,
            detail="task_code and subtask_code are required (e.g. task_code=POCKETCONN, "
                   "subtask_code=APPLELOGIN) so work orders stay filterable by project/"
                   "subtask; pass an explicit work_order instead only for one-off cases")
    task_code = _work_order_segment(task_code_raw, fallback="GEN", max_len=16)
    subtask_code = _work_order_segment(subtask_code_raw, fallback="TASK", max_len=20)
    work_order = (explicit_work_order or
                  _new_work_order(parent, task_code, subtask_code)).strip().upper()
    if not re.match(r"^[A-Z0-9][A-Z0-9._-]{2,60}$", work_order):
        raise HTTPException(status_code=400, detail="unsupported work_order")
    # M2:CC↔CX 互調的呼叫鏈標記 + 防遞迴。parent_delegation = 父工號/父 id,
    # depth 隨鏈遞增,>2 擋(防互派炸鏈);同父併發 running 子任務 >3 擋。
    parent_delegation_ref = str(body.get("parent_delegation") or "").strip()
    parent_dlg_id = ""
    depth = 0
    if parent_delegation_ref:
        prow = _delegation_get(parent_delegation_ref)
        if not prow:
            raise HTTPException(status_code=400, detail="unknown parent_delegation")
        pd = dict(prow)
        parent_dlg_id = pd.get("id") or ""
        depth = int(_delegation_meta(pd).get("depth") or 0) + 1
        if depth > 2:
            raise HTTPException(status_code=400,
                                detail="delegation chain too deep (max depth 2)")
        running_children = sum(
            1 for r in _delegation_rows(limit=200)
            if _delegation_meta(dict(r)).get("parent_delegation") == parent_dlg_id
            and dict(r).get("status") == "running")
        if running_children >= 3:
            raise HTTPException(status_code=429,
                                detail="parent already has 3 running children")
    did = "dlg-" + uuid.uuid4().hex[:16]
    now = time.time()
    # 戶政(藍圖 §3.1):registry 配額前檢(depth/子額/全域 task 上限)。
    # 家譜:互調鏈掛父 delegation,人手派工掛發起人格(persistent 常駐)。
    reg_parent = (f"delegation:{parent_dlg_id}" if parent_dlg_id
                  else f"hermes:{parent}")
    reg_cls = _registry_class_of(body, default_cls="task")
    _registry_precheck_or_429(reg_parent, reg_cls)
    prompt = _delegation_prompt(work_order, parent, title, objective, cwd, body)
    provider_session_id = ""
    codex_thread_id = ""
    cc_session_name = ""
    status = "created"
    meta = {
        "parent_display": PERSONAS[parent][0],
        "created_by": "bridge",
        "created_via": body.get("created_via") or "bridge",
        "parent_delegation": parent_dlg_id,
        "depth": depth,
    }

    if provider == "codex":
        input_items = await _codex_input_items(prompt, body.get("attachments") or [])
        params = {"cwd": cwd, "ephemeral": False, "threadSource": "user"}
        if body.get("model"):
            params["model"] = body.get("model")
        try:
            res = await CODEX_APP.call("thread/start", params, timeout=30.0)
            thread = (res or {}).get("thread") or {}
            codex_thread_id = thread.get("id") or ""
            if not codex_thread_id:
                raise CodexAppServerError("thread/start returned no thread id")
            provider_session_id = codex_thread_id
            CODEX_APP.loaded_threads.add(codex_thread_id)
            try:
                await CODEX_APP.call("thread/name/set", {
                    "threadId": codex_thread_id,
                    "name": f"{work_order} - {title[:80]}",
                }, timeout=15.0)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("app_delegation_create", _exc, expected=True)
                pass
            await CODEX_APP.start_turn(codex_thread_id, input_items,
                                       client_id=f"delegation-{did}", cwd=cwd)
            status = "running"
        except Exception as e:  # noqa: BLE001
            _codex_http_error(e)
    else:
        requested = (body.get("session_name") or body.get("name") or "").strip()
        # work_order 本身已含 task/subtask 資訊，直接當 session 名稱即可，不再
        # 額外拼 title slug（新格式比 v1 長，拼上去容易變成過長又重複的 tmux
        # session 名稱）。
        cc_session_name = requested or work_order.lower()
        if any(ch in cc_session_name for ch in "/|:\n\r\t"):
            raise HTTPException(status_code=400, detail="unsupported session_name")
        _pretrust_claude_dir(cwd)
        # P0 派工分級(2026-07-10):正式派工端點也支援 model 參數(與 Codex
        # 分支的 params["model"] 對齊),企劃/大局思考類任務可指定旗艦模型。
        cc_model = (body.get("model") or "").strip()
        cc_new_args = ["new", cc_session_name, cwd] + ([cc_model] if cc_model else [])
        await _run_ccsess(*cc_new_args)
        ready = await _cc_wait_ready(cc_session_name)
        cc_prompt = prompt
        saved = []
        voice_lines = []
        for a in (body.get("attachments") or []):
            path = _save_attachment(a, a.get("filename") or "file")
            if not path:
                continue
            if a.get("kind") == "audio":
                t = await asyncio.to_thread(_transcribe, path)
                if t:
                    voice_lines.append(t)
            else:
                saved.append(path)
        if voice_lines:
            cc_prompt += "\n\n[語音附件轉寫]\n" + " ".join(voice_lines)
        if saved:
            cc_prompt += "\n\n[附件已存到本機,請用 Read 讀取/檢視]\n" + "\n".join(saved)
        await _cc_paste_text(cc_session_name, cc_prompt)
        provider_session_id = cc_session_name
        status = "running" if ready else "starting"

    row = {
        "id": did,
        "work_order": work_order,
        "parent_persona": parent,
        "parent_session": body.get("parent_session") or "",
        "created_via": body.get("created_via") or "bridge",
        "provider": provider,
        "title": title,
        "objective": objective,
        "cwd": cwd,
        "status": status,
        "provider_session_id": provider_session_id,
        "codex_thread_id": codex_thread_id,
        "cc_session_name": cc_session_name,
        "created_at": now,
        "updated_at": now,
        "last_error": "",
        "meta": meta,
        "task_code": task_code,
        "subtask_code": subtask_code,
    }
    try:
        _delegation_insert(row)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"delegation registry write failed: {e}")
    _registry_register(
        f"delegation:{did}", provider=provider, name=title,
        purpose=(body.get("purpose") or "").strip() or objective[:200],
        cls=reg_cls, parent=reg_parent,
        meta={"work_order": work_order, "cc_session_name": cc_session_name,
              "codex_thread_id": codex_thread_id})
    _log_event("delegation_created",
               work_order=work_order,
               parent_persona=parent,
               provider=provider,
               created_via=meta.get("created_via"),
               depth=depth,
               provider_session_hash=_short_hash(provider_session_id),
               objective_chars=len(objective),
               attachment_count=len(body.get("attachments") or []))
    # M1:建立即回流一張「已建立」卡進父人格對話(父是 delegation 則注回父 session)。
    try:
        await _delegation_notify(dict(row), "created")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("app_delegation_create#2", _exc, expected=True)
        pass
    return {"ok": True, "delegation": _delegation_public(row, status)}


@app.post("/app/v1/persona-report")
async def app_post_persona_report(request: Request):
    """外部內容線(FLiPER fed 的 today-pick / story 發佈)灌一則報告進某人格對話流。
    寫進 report_events(external_source 自訂,不會被 cron 同步蓋掉),再由
    _report_messages 併進 v1/v2 卡片流 → 出現在 Pocket 該人格聊天(卡片流 30s 保險絲
    週期補掃)。fed 端在發佈 today-pick / story 時 POST 這裡即可。"""
    _check_auth(request)
    body = await request.json()
    session = str(body.get("session") or "").strip()
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown persona session")
    content = str(body.get("content") or "").strip()
    if not content:
        raise http_err(400, "EMPTY_CONTENT", "content required")
    ts = float(body.get("ts") or time.time())
    report = {
        "label": str(body.get("label") or "今日精選"),
        "name": str(body.get("name") or "fed-today"),
        "content": content,
        "ts": ts,
        "external_source": str(body.get("external_source") or "fed"),
        "external_id": str(body.get("external_id") or "")
                       or _report_id(session, "fed-today", "", ts),
        # feat/report-actions-api:選填快速行動鈕(≤6 顆)。指令型
        # {label,text,target_session}(label ≤20/text ≤500 超限截斷;
        # target_session = claude_code:<名> 或人格 id,空 = 報告所屬人格),
        # 點了把 text 送回 target session。feat/report-url-actions 增收連結型
        # {label,url}(http/https 驗過才收),點了開連結(StudioLinkRouter)。
        "actions": _report_actions_normalize(body.get("actions")),
    }
    rid = _report_upsert(session, report)
    return {"ok": True, "id": rid}


def _report_key_normalize(rid: str) -> str:
    """App 側可見的報告識別形一律收:report_events.id / external_id 原形之外,
    也收訊息形 `rep-<id>`(_report_msg_shape)與卡片形 `card-hp-rep-<id>`
    (PersonaDigest.message_card 的 id 錨),去前綴後即原 id。"""
    key = (rid or "").strip()
    if key.startswith("card-hp-"):
        key = key[len("card-hp-"):]
    if key.startswith("rep-"):
        key = key[len("rep-"):]
    return key


def _report_lookup(rid: str):
    """單筆報告查找(唯讀):先按 id,再按 external_id(fed/persona-report
    的外部識別)。找不到回 None。"""
    import sqlite3
    key = _report_key_normalize(rid)
    if not key:
        return None
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT id,session,label,name,content,ts,external_source,external_id,"
                "actions FROM report_events WHERE id=? OR external_id=? LIMIT 1",
                (key, key)).fetchone()
            con.close()
        finally:
            con.close()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_report_lookup", _exc, expected=True)
        return None
    if not row:
        return None
    report = {"id": row[0], "session": row[1], "label": row[2] or "",
              "name": row[3] or "", "content": row[4] or "", "ts": row[5],
              "external_source": row[6] or "", "external_id": row[7] or "",
              # 舊列 NULL → [](app 端「無 actions 區塊不出現」的約定)。
              "actions": _report_actions_loads(row[8])}
    return report


@app.get("/app/v1/reports")
async def app_get_reports(session: str, request: Request, limit: int = 20,
                          include_diagnostics: bool = True):
    """報告列表(唯讀,給日後的報告總覽用):某人格最新 limit 筆,newest-first。
    只回 metadata + 200 字 preview,全文走單筆端點 —— 列表不揹整包長文。
    報告中心是聊天外的獨立入口,所以讀列表前會先同步一次 cron/tool report;
    診斷報告仍只留在報告中心,不會鏡射進聊天事件流。"""
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown persona session")
    limit = max(1, min(int(limit or 20), 100))
    _sync_persona_reports(session, max(limit, 50))
    rows = _report_events(session, limit, newest_first=True,
                          include_diagnostics=include_diagnostics)
    return {"reports": [{
        "id": r["id"], "session": session, "label": r["label"] or "",
        "name": r["name"] or "", "ts": r["ts"],
        "external_source": r["external_source"] or "",
        "external_id": r["external_id"] or "",
        "diagnostic": _is_hidden_report(r),
        "preview": _clip_text(r["content"] or "", 200),
        "chars": len(r["content"] or ""),
    } for r in rows]}


@app.get("/app/v1/meetings")
async def app_get_meetings(request: Request, limit: int = 50):
    """會議記錄列表(Pocket 儀表板會議錄音)——跨所有人格聚合
    external_source='meeting-recorder' 的逐字稿報告,newest-first。會議可能送給
    不同人格摘要,故不綁單一 session;回 session 讓 app 能跳回該人格對話。
    metadata + 200 字 preview,全文走既有 /app/v1/reports/{id}。"""
    _check_auth(request)
    limit = max(1, min(int(limit or 50), 200))
    items = []
    for pid in list(PERSONAS.keys()):
        for r in _report_events(pid, max(limit, 50), newest_first=True):
            if (r.get("external_source") or "") != "meeting-recorder":
                continue
            items.append({
                "id": r["id"], "session": pid, "label": r["label"] or "",
                "name": r["name"] or "", "ts": r["ts"],
                "preview": _clip_text(r["content"] or "", 200),
                "chars": len(r["content"] or ""),
            })
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return {"meetings": items[:limit]}


@app.get("/app/v1/reports/{report_id}")
async def app_get_report(report_id: str, request: Request):
    """單筆報告全文(唯讀)— Pocket 原生報告閱讀器的資料源。report_id 收
    report_events.id / external_id / `rep-<id>` / `card-hp-rep-<id>` 四形
    (app 從人格卡片流拿到的是卡片 id,直接原樣打過來即可)。"""
    _check_auth(request)
    r = _report_lookup(report_id)
    if not r:
        raise http_err(404, "REPORT_NOT_FOUND", "no such report")
    return {"report": r}


@app.get("/app/v1/messages")
async def app_get_messages(session: str, request: Request, limit: int = 200):
    """Canonical history for a persona: app turns (bridge canonical store) merged
    with the Telegram history (Hermes state.db), ordered by time — so every
    device sees the same interleaved conversation."""
    _check_auth(request)
    if _openclaw_key_from_session_id(session):
        return await _openclaw_v1_messages(session, limit)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    out = _canon_messages(session, limit)
    _, home = PERSONAS[session]
    # 同一則訊息會在兩個來源各留一份:
    # - assistant:app 回合寫 canonical(正文+〈🔧 執行步驟〉摺疊附錄、帶
    #   client_id)、Hermes state.db 另存乾淨正文(tg-* id、無 client_id)。
    # - user:TG 鏡像 ingest(/internal/v1/mirror/telegram-event)落 canonical
    #   (tgm-* id)後,state.db 掃描會再掃到同一句。
    # 兩份文字/ID 不同 → app 端按文字去重必然失敗,同一句畫面出現兩顆氣泡。
    # 在源頭壓掉 tg 側重複:同 role、剝附錄後正文相同、時間差 10 分鐘內,
    # 視為同一則。純 TG 對話(canonical 無副本)與相隔久遠的同文不受影響。
    canon_recent = [((m.get("ts") or 0), m.get("role"),
                     _dedup_norm(m.get("content") or ""))
                    for m in out if m.get("role") in ("user", "assistant")]
    def _tg_dup(m) -> bool:
        # 完全相等 + 相似度模糊後備(措辭微漂也壓得掉),見 _dual_source_dup。
        return _dual_source_dup(_dedup_norm(m["content"]), m["role"],
                                m["ts"] or 0, canon_recent)
    # 活 turn 檢疫:回合進行中,只扣住「回合起始之後」的 TG assistant(可能是
    # 本回合回覆的進度句副本),等 canonical 總結落地由壓重定奪。起始前的既定
    # 歷史照常放行(見 _session_turn_started_at)。
    _turn_started = _session_turn_started_at(session)
    def _tg_quarantined(m) -> bool:
        return _tg_assistant_in_quarantine(_turn_started, m["role"], m["ts"] or 0)
    for m in _persona_history(home, limit):
        if _tg_dup(m) or _tg_quarantined(m):
            continue
        out.append({"id": f"tg-{m['ts']}", "role": m["role"], "content": m["content"],
                    "attachments": m.get("attachments") or [], "ts": m["ts"],
                    "status": "done", "source": "telegram"})
    # Surface each persona's daily briefs (cron-delivered) IN its conversation,
    # like Telegram does — not only in the separate Reports tab. 袁方's 晨報/午報
    # etc. and 潘天晴's 編輯台晨報 (+ future 今日精選/限動) read from each persona's
    # OWN home, so the app thread matches what TG received this morning.
    _sync_persona_reports(session, 50)
    out.extend(_report_messages(session, limit))
    out.sort(key=lambda m: m.get("ts") or 0)
    out = out[-limit:]
    # Sync engine P1:app 每次輪詢這頁就是一次現成的三來源合併掃描,順手
    # 鏡射進 event_log(冪等、穩態零寫入;鏡射在 reaction overlay 疊加前,
    # payload 保持訊息本體的正典形狀)。
    _event_mirror_messages(session, out)
    # Reaction overlay (G2/#39) — one lookup for the whole page, ids as-is
    # (canonical mids and tg-<ts> alike), so reactions survive reinstall and
    # show identically on every device.
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT msg_id, reaction FROM reactions WHERE session=?",
                               (session,)).fetchall()
            overlay = {r[0]: r[1] for r in rows if r[1]}
            # Canonical multi-emoji reactions + pins (G2/#39 final contract). Both
            # fields are optional in the payload: omitted when there's no data.
            meta_rows = con.execute(
                "SELECT message_id, reactions, pinned, deleted FROM message_meta").fetchall()
            con.close()
            meta = {}
            for mid_, rx, pn, dl in meta_rows:
                try:
                    lst = json.loads(rx) if rx else []
                except Exception as _exc:  # noqa: BLE001
                    _log_exc("app_get_messages", _exc, expected=True)
                    lst = []
                meta[mid_] = ([str(r) for r in lst if r] if isinstance(lst, list) else [],
                              bool(pn), bool(dl))
            for m in out:
                mid_ = str(m.get("id"))
                legacy = overlay.get(mid_)
                if legacy:
                    m["reaction"] = legacy
                if mid_ in meta:
                    reactions, pinned, deleted = meta[mid_]
                    if reactions:
                        m["reactions"] = reactions
                    if pinned:
                        m["pinned"] = True
                    if deleted:
                        m["deleted"] = True    # G4 tombstone: row stays, flagged
                elif legacy:
                    # Older builds wrote the single-reaction overlay only; surface
                    # it in the new list field too so nothing disappears mid-migration.
                    m["reactions"] = [legacy]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        # Failing open (messages without reactions/pins) is right, but silent
        # failure made it undiagnosable (issue #7).
        _log_event("reaction_overlay_read_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])
    return {"messages": out}


@app.get("/app/v1/messages/status")
async def app_get_message_status(session: str, request: Request,
                                 client_id: str = ""):
    """Recovery status for a persona turn started by /app/v1/messages.

    Pocket polls this when a mobile upload/stream detaches. Returning an honest
    state here lets the app show delivered/running/done and avoids re-running
    image-heavy turns just because the phone lost its SSE connection.
    """
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    acp = await POOL.get(session, home_for(session))
    return _app_turn_status(session, client_id or None, acp_busy=acp.is_busy())


async def _oc_events_gen(session: str, key: str, since: int, follow: bool):
    """B:openclaw 版 events SSE —— gateway 事件驅動(_oc_msg_notify)喚醒後重抓
    chat.history 吐新訊息,取代 app 端慢輪詢。事件形狀與 persona 完全一致
    (`_app_message_event`,seq = ts×1000),app 既有 events 消費者直接吃。
    15s 兜底重掃(防漏信號),配置不良/拉歷史失敗不斷流。"""
    cursor = int(since or 0)
    deadline = time.monotonic() + (120.0 if follow else 0.0)
    seen_ver = -1
    last_scan = 0.0
    while True:
        sent = False
        ver = _OC_MSG_VER.get(key, 0)
        if ver != seen_ver or time.monotonic() - last_scan >= 15.0:
            last_scan = time.monotonic()
            seen_ver = ver
            try:
                data = await _openclaw_v1_messages(session, 80)
            except Exception as e:  # noqa: BLE001 — 拉歷史失敗不斷流,下輪再試
                _log_event("oc_events_gen_history_error", error=str(e)[:160])
                data = {"messages": []}
            for msg in data.get("messages") or []:
                seq = _app_message_seq(msg)
                if seq <= cursor:
                    continue
                event = _app_message_event(msg)
                cursor = max(cursor, int(event["seq"]))
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                sent = True
        if not follow:
            yield "data: [DONE]\n\n"
            return
        if not sent:
            yield ": keepalive\n\n"
        if time.monotonic() >= deadline:
            yield "data: [DONE]\n\n"
            return
        await _oc_msg_wait(key, seen_ver, SSE_KEEPALIVE_SECS)


@app.get("/app/v1/messages/events")
async def app_get_message_events(session: str, request: Request,
                                 since: int = 0, follow: bool = True):
    """SSE feed for canonical persona messages.

    This is intentionally backed by the canonical store instead of an in-memory
    queue, so it survives bridge restarts and covers turns that completed after
    the client disconnected.

    openclaw session(v1 pseudo-persona)改走 gateway 事件驅動的即時 SSE
    (`_oc_events_gen`,B),不靠 app 慢輪詢。
    """
    _check_auth(request)
    if session not in PERSONAS:
        oc_key = _openclaw_key_from_session_id(session)
        if oc_key is None:
            raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
        if not OPENCLAW.configured():
            raise http_err(404, "SESSION_NOT_FOUND", "openclaw not configured")
        return StreamingResponse(_oc_events_gen(session, oc_key, since, follow),
                                 media_type="text/event-stream")

    async def gen():
        cursor = int(since or 0)
        deadline = time.monotonic() + (120.0 if follow else 0.0)
        last_ver = -1        # canonical 版本(首輪必掃,補斷線期積壓)
        last_state_ver = -1  # state.db stat 版本(TG/cron 剛寫入)
        last_scan = 0.0      # 保險絲:沒收到信號至少每 30s 重掃一次
        while True:
            sent = False
            ver = _CANON_VER.get(session, 0)
            sver = _STATEDB_VER.get(session, 0)
            # 重掃條件:canonical 變 OR state.db 變(TG/cron 寫入)OR 30s 保險絲。
            # 以前只盯 canonical → 純 TG 訊息(pocket_mirror 以 push=False 寫入、
            # 不 bump canonical 版本)只能等 30s 兜底,是「TG 同步斷斷續續」主因。
            # 現在掛 state.db watcher 版本,TG 一寫入 ~0.2s 醒;掃描源也從
            # canonical-only 換成 `_hp_merged_messages`(canonical ⊕ TG ⊕ 晨報,
            # 去重/檢疫在合併函式內),與 GET /app/v1/messages 同源 → 即時流看得到 TG。
            if (ver != last_ver or sver != last_state_ver
                    or time.monotonic() - last_scan >= 30.0):
                last_scan = time.monotonic()
                last_ver = ver
                last_state_ver = sver
                for msg in _hp_merged_messages(session, 80):
                    seq = _app_message_seq(msg)
                    if seq <= cursor:
                        continue
                    event = _app_message_event(msg)
                    cursor = max(cursor, int(event["seq"]))
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    sent = True
            if not follow:
                yield "data: [DONE]\n\n"
                return
            if not sent:
                yield ": keepalive\n\n"
            if time.monotonic() >= deadline:
                yield "data: [DONE]\n\n"
                return
            try:
                await asyncio.wait_for(
                    _canon_or_statedb_wait(session, last_ver, last_state_ver),
                    timeout=SSE_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


# ─────────────── Sync engine P2:/app/v2 統一事件流 + 已讀游標 ───────────
_EVENTS_FOLLOW_MAX_SECS = 120.0   # 與 v1 messages/events 同款:app 週期重連


@app.get("/app/v2/events")
async def app_v2_events(request: Request, session: str | None = None,
                        since_seq: int = 0, follow: bool = True):
    """SYNC_ENGINE_REWRITE_PLAN §3.1 的統一訂閱端點:從 event_log 撈
    id > since_seq 的所有列,補洞 + 即時走同一條 SSE,三來源(App/TG/cron)
    與已讀游標不再各走各的加速通道。信封 {seq, ts, type, data} 與
    /app/v2/sessions/{id}/events 卡片流對齊;event_log 是持久表,沒有卡片
    ring buffer 的 410 SEQ_GONE 問題 — 任何裝置 since_seq=0 重放即可重建
    完整歷史(§3.3 / backlog B3)。

    session 可省略(P3 契約 #2):省略 = 全域訂閱,單一條 SSE 涵蓋全部
    hermes 人格 session(App 首頁列表+未讀靠這條,不用每 persona 開一條)。
    全域信封多帶 session 欄位;event_log.id 全域單調,since_seq 游標語意
    與 per-session 模式相同。"""
    _check_auth(request)
    if session is not None and session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")

    async def gen():
        cursor = max(0, int(since_seq or 0))
        # 連上先主動拉一次 TG/cron(force 穿越節流):新訂閱者立刻看到外部
        # 來源的積壓,不用等下一個同步週期。
        await asyncio.to_thread(_event_sync_session, session, 200, True)
        deadline = time.monotonic() + (_EVENTS_FOLLOW_MAX_SECS if follow else 0.0)
        last_ver = -1     # 首輪必掃(補訂閱者斷線期間的積壓)
        last_sync = time.monotonic()
        sver = _STATEDB_VER.get(session, 0)   # watcher 版本基準(#tg-instant-sync)
        while True:
            sent = False
            cur_sver = _STATEDB_VER.get(session, 0)
            if cur_sver != sver:
                # state.db 剛被 TG/cron 寫入(stat watcher bump)→ 立刻拉進
                # event_log,不等下面的 10s 週期節流。先去抖:睡到距上次掃
                # 描 ≥0.5s 再掃,吸掉同一批寫入的連續 bump;之後用 0.4s 小
                # 節流 — 自己的時間線必然通過(剛睡滿 0.5s),只有別的訂閱
                # 者在我們去抖期間已經掃過(該掃必然晚於這次寫入,已涵蓋)
                # 才跳過。寫入絕不會被節流吞掉只剩 10s 兜底。
                sver = cur_sver
                gap = 0.5 - (time.monotonic() - _EVENT_SYNC_TS.get(session, 0.0))
                if gap > 0:
                    await asyncio.sleep(min(gap, 0.5))
                last_sync = time.monotonic()
                await asyncio.to_thread(_event_sync_session, session, 200,
                                        False, 0.4)
            ver = _EVENT_VER.get(session, 0)
            if ver != last_ver:
                last_ver = ver
                while True:
                    batch = await asyncio.to_thread(_event_since, session,
                                                    cursor, 500)
                    for ev in batch:
                        cursor = ev["seq"]
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        sent = True
                    if len(batch) < 500:
                        break
            if not follow:
                yield "data: [DONE]\n\n"
                return
            if not sent:
                yield ": keepalive\n\n"
            if time.monotonic() >= deadline:
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() - last_sync >= _EVENT_SYNC_MIN_SECS:
                # 週期主動拉當保險絲:statedb watcher(上面的 sver 檢查)是
                # 即時觸發主力,這條是 watcher 失效時的兜底 — v2 訂閱者的
                # TG 延遲上限仍 = _EVENT_SYNC_MIN_SECS,不依賴單一機制。
                last_sync = time.monotonic()
                await asyncio.to_thread(_event_sync_session, session, 200)
            try:
                await asyncio.wait_for(
                    _event_or_statedb_wait(session, last_ver, sver),
                    timeout=SSE_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                pass

    async def gen_all():
        # 全域訂閱:與 gen() 同構,差異只在 (a) 初連/兜底同步掃全部
        # persona (b) watcher 以 per-session snapshot 找出「誰剛被寫入」
        # 只拉那幾個 (c) 批次改走 _event_since_all、版本盯 _EVENT_VER_ALL。
        cursor = max(0, int(since_seq or 0))
        for s in list(PERSONAS):
            await asyncio.to_thread(_event_sync_session, s, 200, True)
        deadline = time.monotonic() + (_EVENTS_FOLLOW_MAX_SECS if follow else 0.0)
        last_ver = -1     # 首輪必掃
        last_sync = time.monotonic()
        svers = dict(_STATEDB_VER)   # per-session watcher 版本基準
        while True:
            sent = False
            # 先讀全域計數再算 changed:之後才 bump 的寫入會讓下面的
            # wait 立刻返回,喚醒不漏(順序反過來就有睡過頭的窗)。
            cur_sall = _STATEDB_VER_ALL
            changed = [s for s in list(PERSONAS)
                       if _STATEDB_VER.get(s, 0) != svers.get(s, 0)]
            if changed:
                # 去抖 + 小節流與 per-session 版同參數;gap 以 changed 中
                # 最近一次掃描起算(保守但上限 0.5s)。
                for s in changed:
                    svers[s] = _STATEDB_VER.get(s, 0)
                gap = 0.5 - (time.monotonic() - max(
                    _EVENT_SYNC_TS.get(s, 0.0) for s in changed))
                if gap > 0:
                    await asyncio.sleep(min(gap, 0.5))
                last_sync = time.monotonic()
                for s in changed:
                    await asyncio.to_thread(_event_sync_session, s, 200,
                                            False, 0.4)
            ver = _EVENT_VER_ALL
            if ver != last_ver:
                last_ver = ver
                while True:
                    batch = await asyncio.to_thread(_event_since_all,
                                                    cursor, 500)
                    for ev in batch:
                        cursor = ev["seq"]
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        sent = True
                    if len(batch) < 500:
                        break
            if not follow:
                yield "data: [DONE]\n\n"
                return
            if not sent:
                yield ": keepalive\n\n"
            if time.monotonic() >= deadline:
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() - last_sync >= _EVENT_SYNC_MIN_SECS:
                last_sync = time.monotonic()
                for s in list(PERSONAS):
                    await asyncio.to_thread(_event_sync_session, s, 200)
            try:
                await asyncio.wait_for(
                    _event_or_statedb_wait_all(last_ver, cur_sall),
                    timeout=SSE_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(gen() if session is not None else gen_all(),
                             media_type="text/event-stream")


def _read_cursor_rows(session: str) -> list[dict]:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT device_id,last_read_seq,last_read_ts,message_id,updated_at "
                "FROM read_cursors WHERE session=?", (session,)).fetchall()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("read_cursor_read_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])
        return []
    return [{"device_id": r[0], "last_read_seq": r[1], "last_read_ts": r[2],
             "message_id": r[3], "updated_at": r[4]} for r in rows]


def _read_cursor_global(rows: list[dict]) -> dict:
    """已拍板語意(2026-07-11 善彰):「任一裝置讀過即全讀」= 全裝置 MAX。
    這裡做伺服器端聚合,App 端(P3)未讀數直接拿 global.last_read_seq 比
    event seq,不用自己算;cursors 仍按裝置分列保留原始資料(若未來要改
    per-device 語意,資料都在,schema 不用動)。"""
    return {
        "last_read_seq": max((int(r.get("last_read_seq") or 0) for r in rows),
                             default=0),
        "last_read_ts": max((float(r.get("last_read_ts") or 0.0) for r in rows),
                            default=0.0),
    }


@app.get("/app/v2/read")
async def app_v2_read_get(session: str, request: Request):
    """該 session 全部裝置的已讀游標(新裝置冷載算未讀用)。global 欄位
    是拍板語意「任一裝置讀過即全讀」的聚合(見 _read_cursor_global)。"""
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    rows = _read_cursor_rows(session)
    return {"session": session, "cursors": rows,
            "global": _read_cursor_global(rows)}


@app.post("/app/v2/read")
async def app_v2_read_post(request: Request):
    """回報已讀游標(SYNC_ENGINE_REWRITE_PLAN §3.1 read_cursor.update)。
    body: {session, device_id, last_read_seq 或 last_read_ts, message_id?}
    游標只進不退(多裝置/亂序回報取 max);真的前進才追加一筆
    read_cursor.update 事件,其他訂閱中的裝置從 /app/v2/events 收到就能
    同步已讀狀態 — 未讀從此有伺服器真相,不再是各裝置本地計數器瞎猜。"""
    import sqlite3
    _check_auth(request)
    body = await _json_body(request)
    session = (body.get("session") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    if not device_id:
        raise http_err(400, "DEVICE_ID_REQUIRED",
                       "device_id 必填(已讀游標按裝置分列)")
    try:
        req_seq = max(0, int(body.get("last_read_seq") or 0))
        req_ts = max(0.0, float(body.get("last_read_ts") or 0))
    except (TypeError, ValueError):
        raise http_err(400, "CURSOR_INVALID", "last_read_seq/last_read_ts 需為數字")
    message_id = str(body.get("message_id") or "").strip() or None
    if req_seq <= 0 and req_ts <= 0:
        raise http_err(400, "CURSOR_REQUIRED",
                       "last_read_seq 或 last_read_ts 至少要有一個")
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        row = con.execute(
            "SELECT last_read_seq,last_read_ts,message_id,updated_at "
            "FROM read_cursors WHERE session=? AND device_id=?",
            (session, device_id)).fetchone()
        prev_seq, prev_ts = (row[0], row[1]) if row else (0, 0.0)
        new_seq, new_ts = max(prev_seq, req_seq), max(prev_ts, req_ts)
        moved = new_seq > prev_seq or new_ts > prev_ts
        if moved:
            message_id = message_id or (row[2] if row else None)
            con.execute(
                "INSERT OR REPLACE INTO read_cursors"
                "(session,device_id,last_read_seq,last_read_ts,message_id,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (session, device_id, new_seq, new_ts, message_id, now))
            con.commit()
        grow = con.execute(
            "SELECT MAX(last_read_seq),MAX(last_read_ts) FROM read_cursors "
            "WHERE session=?", (session,)).fetchone()
    finally:
        con.close()
    # 拍板語意「任一裝置讀過即全讀」的聚合,事件與回應都帶上 — 其他訂閱
    # 中的裝置收到 read_cursor.update 直接用 global 更新未讀,不用再 GET。
    gcur = {"last_read_seq": int(grow[0] or 0),
            "last_read_ts": float(grow[1] or 0.0)}
    if moved:
        cursor = {"device_id": device_id, "last_read_seq": new_seq,
                  "last_read_ts": new_ts, "message_id": message_id,
                  "updated_at": now}
        seq = _event_append(session, "read_cursor.update",
                            {"session": session, **cursor, "global": gcur})
    else:
        # 沒前進(重送/亂序)→ 冪等回現存游標,不追加事件
        cursor = {"device_id": device_id, "last_read_seq": prev_seq,
                  "last_read_ts": prev_ts,
                  "message_id": row[2] if row else None,
                  "updated_at": row[3] if row else None}
        seq = 0
    return {"ok": True, "moved": moved, "seq": seq, "cursor": cursor,
            "global": gcur}


@app.post("/app/v1/messages/{mid}/reaction")
async def app_set_reaction(mid: str, request: Request):
    """Set / clear one emoji reaction on a message (G2/#39). Works for both
    app-sent turns (canonical mid) and Telegram-side rows (tg-<ts> id) — the
    overlay table doesn't care where the message lives."""
    _check_auth(request)
    body = await request.json()
    session = (body.get("session") or "").strip()
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    reaction = (body.get("reaction") or "").strip()[:8]   # one emoji, not an essay
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            if reaction:
                con.execute("INSERT INTO reactions(msg_id, session, reaction, updated_at) "
                            "VALUES(?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET "
                            "reaction=excluded.reaction, updated_at=excluded.updated_at",
                            (mid, session, reaction, time.time()))
            else:
                con.execute("DELETE FROM reactions WHERE msg_id=?", (mid,))
            con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "reaction": reaction or None}


def _message_meta_load(con, message_id: str):
    """→ (reactions_list, pinned_int) for one message; ([], 0) when no row."""
    row = con.execute("SELECT reactions, pinned FROM message_meta WHERE message_id=?",
                      (message_id,)).fetchone()
    reactions: list = []
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, list):
                reactions = [str(r) for r in parsed if r]
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_message_meta_load", _exc, expected=False)
            reactions = []
    return reactions, (int(row[1] or 0) if row else 0)


def _message_session_of(con, message_id: str):
    """Session a message belongs to, from the canonical messages table.
    None for tg-<ts>/report ids — those live outside canonical (merged 流),
    the overlay row then keeps session NULL(讀取端用 join 補洞)。"""
    row = con.execute("SELECT session FROM messages WHERE id=?",
                      (message_id,)).fetchone()
    return row[0] if row else None


def _message_meta_upsert(con, message_id: str, reactions: list,
                         pinned: int, session=None):
    """One shared upsert for every message_meta writer (G2/#39). session 只在
    有值時覆蓋(COALESCE)— per-message 端點解析不出 tg id 的歸屬時,不把
    PUT /sessions/{id}/pin 已寫入的歸屬洗掉。"""
    con.execute(
        "INSERT INTO message_meta(message_id, reactions, pinned, session, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
        "reactions=excluded.reactions, pinned=excluded.pinned, "
        "session=COALESCE(excluded.session, session), "
        "updated_at=excluded.updated_at",
        (message_id, json.dumps(reactions, ensure_ascii=False),
         pinned, session, time.time()))


@app.post("/app/v1/reactions")
async def app_reactions(request: Request):
    """Canonical reactions (G2/#39): add/remove one emoji on a message and
    return the message's full current emoji list. Works for canonical mids and
    tg-<ts> ids alike — the overlay doesn't care where the message lives."""
    _check_auth(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "").strip()
    emoji = str(body.get("emoji") or "").strip()[:16]
    action = str(body.get("action") or "add").strip().lower()
    if not message_id or not emoji or action not in ("add", "remove"):
        raise HTTPException(status_code=400,
                            detail="message_id, emoji and action=add|remove required")
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            reactions, pinned = _message_meta_load(con, message_id)
            if action == "add":
                if emoji not in reactions:
                    reactions.append(emoji)
            else:
                reactions = [r for r in reactions if r != emoji]
            _message_meta_upsert(con, message_id, reactions, pinned,
                                 session=_message_session_of(con, message_id))
            con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="reaction",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "reactions": reactions}


@app.post("/app/v1/pins")
async def app_pins(request: Request):
    """Canonical per-message pin (G2/#39) — cross-device, survives reinstall."""
    _check_auth(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "").strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    pinned = 1 if body.get("pinned") else 0
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            reactions, _old = _message_meta_load(con, message_id)
            _message_meta_upsert(con, message_id, reactions, pinned,
                                 session=_message_session_of(con, message_id))
            con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="pin",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True}


@app.patch("/app/v1/messages/{mid}")
async def app_patch_message(mid: str, request: Request):
    """G2/#39 issue 合約收尾:單值 reaction 的 PATCH 形狀。body {"reaction":
    "👍" | null}(null/空字串=清除)。只認 canonical messages 表的 id —
    不存在回 404(合約要求存在性檢查;tg-<ts>/報告 id 不在 canonical,
    請走 id-agnostic 的 POST /app/v1/reactions,那條才蓋得到 TG 側訊息)。
    寫入同時落 legacy 單值 overlay 與 message_meta 清單(取代整串),
    GET /app/v1/messages 的 reaction/reactions 兩欄一起對齊。"""
    _check_auth(request)
    body = await _json_body(request)
    if "reaction" not in body:
        raise http_err(400, "BAD_REQUEST", "body must carry a 'reaction' key")
    raw = body.get("reaction")
    reaction = str(raw).strip()[:16] if raw is not None else ""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            session = _message_session_of(con, mid)
            if session is None:
                con.close()
                raise http_err(404, "MESSAGE_NOT_FOUND",
                               "no canonical message with this id",
                               "TG/cron-sourced ids: use POST /app/v1/reactions")
            _reactions_old, pinned = _message_meta_load(con, mid)
            if reaction:
                con.execute("INSERT INTO reactions(msg_id, session, reaction, updated_at) "
                            "VALUES(?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET "
                            "reaction=excluded.reaction, updated_at=excluded.updated_at",
                            (mid, session, reaction, time.time()))
                _message_meta_upsert(con, mid, [reaction], pinned, session=session)
            else:
                con.execute("DELETE FROM reactions WHERE msg_id=?", (mid,))
                _message_meta_upsert(con, mid, [], pinned, session=session)
            con.commit()
            con.close()
        finally:
            con.close()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="reaction_patch",
                   message_id=mid, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "id": mid, "reaction": reaction or None}


def _session_pinned_ids(con, session: str) -> list:
    """All pinned message ids belonging to a session, oldest-pin first.
    session 欄有值直接比;NULL(舊列/歸屬未知)用 canonical messages join
    補洞 — tg-<ts> 舊 pin 列兩邊都對不上時寧可漏,不跨 session 誤傷。"""
    rows = con.execute(
        "SELECT message_id FROM message_meta WHERE pinned=1 AND (session=? OR "
        "(session IS NULL AND message_id IN (SELECT id FROM messages WHERE session=?)))"
        " ORDER BY updated_at", (session, session)).fetchall()
    return [r[0] for r in rows]


@app.put("/app/v1/sessions/{sid}/pin")
async def app_put_session_pins(sid: str, request: Request):
    """G2/#39 issue 合約收尾:per-session 置頂全量替換。body
    {"pinned_message_ids": [...]}(空清單=全部解除)。id 收 GET
    /app/v1/messages 回的任何穩定 id(canonical mid / tg-<ts> / 報告 id)—
    寫入時直接掛 session 歸屬,tg id 從此也能按 session 讀回。解除只掃
    「歸屬得到本 session」的列,不動其他人格的置頂。"""
    _check_auth(request)
    if sid not in PERSONAS:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await _json_body(request)
    ids = body.get("pinned_message_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids):
        raise http_err(400, "BAD_REQUEST",
                       "pinned_message_ids must be a list of message ids")
    ids = [i.strip() for i in ids]
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        for stale in _session_pinned_ids(con, sid):
            if stale in ids:
                continue
            reactions, _pin = _message_meta_load(con, stale)
            _message_meta_upsert(con, stale, reactions, 0, session=sid)
        for mid in ids:
            reactions, _pin = _message_meta_load(con, mid)
            _message_meta_upsert(con, mid, reactions, 1, session=sid)
        pinned_now = _session_pinned_ids(con, sid)
        con.commit()
        con.close()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="session_pin",
                   session=sid, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "session": sid, "pinned_message_ids": pinned_now}


@app.get("/app/v1/sessions/{sid}/pin")
async def app_get_session_pins(sid: str, request: Request):
    """PUT 的讀回面(G2/#39):本 session 目前置頂的訊息 id 清單。
    (GET /app/v1/messages 的每則 pinned 旗標照舊,這條是 per-session 檢視。)"""
    _check_auth(request)
    if sid not in PERSONAS:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    import sqlite3
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
    try:
        pinned = _session_pinned_ids(con, sid)
    finally:
        con.close()
    return {"session": sid, "pinned_message_ids": pinned}


@app.post("/app/v1/messages/retract")
async def app_message_retract(request: Request):
    """G4 tombstone: mark a message deleted. The row stays in
    GET /app/v1/messages with "deleted": true — every device renders the same
    tombstone instead of the messages silently diverging."""
    _check_auth(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "").strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            reactions, pinned = _message_meta_load(con, message_id)
            con.execute("INSERT INTO message_meta(message_id, reactions, pinned, deleted, updated_at) "
                        "VALUES(?,?,?,1,?) ON CONFLICT(message_id) DO UPDATE SET "
                        "deleted=1, updated_at=excluded.updated_at",
                        (message_id, json.dumps(reactions, ensure_ascii=False),
                         pinned, time.time()))
            con.commit()
            con.close()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="retract",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True}


# ───────────────────────── personas CRUD (G6, wave 2) ──────────────────────
_PERSONA_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _persona_profile_of(home: str) -> str:
    """Human-facing profile name for a home path (main / profiles/<x> tail)."""
    if home == HOME_ROOT:
        return "main"
    prefix = f"{HOME_ROOT}/profiles/"
    return home[len(prefix):] if home.startswith(prefix) else os.path.basename(home or "")


def _persona_home_from_body(body: dict) -> str | None:
    """Resolve home from body {home} or {profile}; None when neither given."""
    home = str(body.get("home") or "").strip()
    profile = str(body.get("profile") or "").strip()
    if home:
        return os.path.realpath(os.path.expanduser(home))
    if profile:
        return HOME_ROOT if profile == "main" else f"{HOME_ROOT}/profiles/{profile}"
    return None


def _persona_public(pid: str, name: str, home: str, enabled: bool,
                    deleted: bool) -> dict:
    ent = _avatar_manifest().get(pid) or {}
    ap = _avatar_path(pid)
    return {"id": pid, "name": ent.get("name") or name,
            "profile": _persona_profile_of(home),
            "home": home, "enabled": enabled, "deleted": deleted,
            "builtin": pid in _PERSONAS_BUILTIN,
            # TG 同源身分:@username(manifest.tg,可後補)與頭像版本
            # (檔案 mtime;0=無圖,app 端以 rev 做快取失效)
            "username": ent.get("tg") or "",
            "avatar_rev": int(os.path.getmtime(ap)) if ap else 0}


def _persona_row_get(pid: str):
    import sqlite3
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
    try:
        r = con.execute("SELECT id,name,home,enabled,deleted FROM personas WHERE id=?",
                        (pid,)).fetchone()
        con.close()
        return r
    finally:
        con.close()


def _persona_row_upsert(pid: str, name: str, home: str, enabled: int, deleted: int):
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("INSERT INTO personas(id,name,home,enabled,deleted,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name, home=excluded.home, enabled=excluded.enabled, "
                    "deleted=excluded.deleted, updated_at=excluded.updated_at",
                    (pid, name, home, enabled, deleted, time.time(), time.time()))
        con.commit()
        con.close()
    finally:
        con.close()


@app.get("/app/v1/personas")
async def personas_list(request: Request):
    """Full persona registry (builtins + custom), including disabled/deleted
    entries so the app can render a management list. What the conversation UI
    should offer is exactly the entries with enabled && !deleted (== the live
    PERSONAS routing table)."""
    _check_auth(request)
    # OpenClaw 部署(PocketConnect 設 POCKET_ACTIVE_PROVIDER=openclaw)的人格分頁
    # 顯示 gateway 主 session。刻意用「明確 openclaw」而非快照的 _provider_enabled
    # (auto 模式對兩個 provider 都回 True,會讓沒設 env 的 Hermes 主機被劫持)。
    if _dashboard_active_provider() == "openclaw":
        return {"personas": await _openclaw_v1_personas()}
    rows = {r[0]: r for r in _personas_db_rows()}
    out = []
    for pid, (disp, home) in _PERSONAS_BUILTIN.items():
        r = rows.pop(pid, None)
        if r:
            out.append(_persona_public(pid, r[1] or disp, r[2] or home,
                                       bool(r[3]) and not r[4], bool(r[4])))
        else:
            out.append(_persona_public(pid, disp, home, True, False))
    for pid, r in rows.items():
        out.append(_persona_public(pid, r[1] or pid, r[2] or HOME_ROOT,
                                   bool(r[3]) and not r[4], bool(r[4])))
    return {"personas": out}


@app.get("/app/v1/personas/{pid}/avatar")
async def personas_avatar(pid: str, request: Request):
    """Persona 頭像 — TG 同源正典(HOME_ROOT/avatars)。無圖 404,app 退 glyph 圓盤。"""
    _check_auth(request)
    if not _PERSONA_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="bad persona id")
    p = _avatar_path(pid)
    if not p:
        raise HTTPException(status_code=404, detail="no avatar")
    return FileResponse(p)


@app.post("/app/v1/personas")
async def personas_create(request: Request):
    """Add a persona without touching bridge.py. The home (or profile) must
    already exist on disk — a Hermes profile is provisioned outside the bridge;
    this endpoint only registers it for routing."""
    _check_auth(request)
    body = await request.json()
    pid = str(body.get("id") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    if not pid and name:
        pid = re.sub(r"[^a-z0-9_-]", "", name.lower())[:32]
    if not _PERSONA_ID_RE.match(pid or ""):
        raise HTTPException(status_code=400,
                            detail="id required: 1-32 chars of a-z 0-9 _ -")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    home = _persona_home_from_body(body)
    if not home:
        raise HTTPException(status_code=400, detail="profile or home required")
    if not os.path.isdir(home):
        raise HTTPException(status_code=400,
                            detail=f"persona home not found: {home}")
    existing = _persona_row_get(pid)
    if pid in PERSONAS or (existing and not existing[4]):
        raise http_err(409, "PERSONA_EXISTS", "persona id already in use")
    _persona_row_upsert(pid, name, home, 1, 0)
    _personas_reload()
    _log_event("persona_created", id=pid, home=home)
    return _persona_public(pid, name, home, True, False)


@app.patch("/app/v1/personas/{pid}")
async def personas_patch(pid: str, request: Request):
    """Rename / re-home / enable / disable a persona (builtin or custom).
    enabled=true also un-deletes, so DELETE is reversible from the app."""
    _check_auth(request)
    body = await request.json()
    row = _persona_row_get(pid)
    builtin = _PERSONAS_BUILTIN.get(pid)
    if row is None and builtin is None:
        raise http_err(404, "PERSONA_NOT_FOUND", "unknown persona")
    cur_name = (row[1] if row else None) or (builtin[0] if builtin else pid)
    cur_home = (row[2] if row else None) or (builtin[1] if builtin else HOME_ROOT)
    cur_enabled = bool(row[3]) if row else True
    cur_deleted = bool(row[4]) if row else False
    name = str(body.get("name") or "").strip() or cur_name
    home = _persona_home_from_body(body) or cur_home
    if not os.path.isdir(home):
        raise HTTPException(status_code=400,
                            detail=f"persona home not found: {home}")
    if "enabled" in body:
        enabled = bool(body.get("enabled"))
        deleted = False if enabled else cur_deleted
    else:
        enabled, deleted = cur_enabled, cur_deleted
    _persona_row_upsert(pid, name, home, 1 if enabled else 0, 1 if deleted else 0)
    _personas_reload()
    _log_event("persona_patched", id=pid, enabled=enabled, deleted=deleted)
    return _persona_public(pid, name, home, enabled and not deleted, deleted)


@app.delete("/app/v1/personas/{pid}")
async def personas_delete(pid: str, request: Request):
    """Soft delete: the row is kept (deleted=1, enabled=0) and the persona
    drops out of routing; PATCH {"enabled": true} restores it."""
    _check_auth(request)
    row = _persona_row_get(pid)
    builtin = _PERSONAS_BUILTIN.get(pid)
    if row is None and builtin is None:
        raise http_err(404, "PERSONA_NOT_FOUND", "unknown persona")
    name = (row[1] if row else None) or (builtin[0] if builtin else pid)
    home = (row[2] if row else None) or (builtin[1] if builtin else HOME_ROOT)
    _persona_row_upsert(pid, name, home, 0, 1)
    _personas_reload()
    _log_event("persona_deleted", id=pid)
    return {"ok": True}


async def _persona_prepare_turn(session: str, content: str, attachments: list,
                                stt_lang: str = ""):
    """persona 回合前置(附件轉存/語音轉寫/多模態 parts/prompt 組裝)→
    (content, att_meta, prompt)。v1 POST /app/v1/messages 與 v2 統一路由
    input 共用。"""
    _att_guard(attachments)   # 修復單「附件限制」:直送口件數閥
    normalized_attachments = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        na = dict(a)
        path = _save_attachment(na, na.get("filename") or "file")
        if path:
            na["path"] = path
            na.pop("data", None)       # keep the persona turn body lightweight
            na.pop("data_uri", None)
        normalized_attachments.append(na)
    attachments = normalized_attachments

    # Voice messages: transcribe any audio attachment and fold the transcript
    # into the turn text. The audio still rides along as an attachment so the
    # conversation shows the voice bubble; the model gets the words.
    persona_home = home_for(session)
    voice_text = await _transcribe_attachments(
        attachments, persona_home, stt_lang
    )
    if voice_text:
        # 會議錄音(檔名 meeting-*,Pocket 儀表板錄音):先本地模型修飾標點/錯字,
        # 存成報告(app 卡片流即出現「會議逐字稿」報告卡=可點的逐字稿連結),再把
        # 修飾稿餵人格出摘要。一般語音訊息(iOS voice message 等)不走此路,維持
        # 原「轉寫併入 content」行為。修飾/存報告任一失敗都回退,不擋摘要主流程。
        is_meeting = any(
            isinstance(a, dict) and a.get("kind") == "audio"
            and str(a.get("filename") or "").startswith("meeting-")
            for a in attachments
        )
        if is_meeting:
            polished = await _polish_transcript(voice_text)
            title = "會議記錄 " + time.strftime("%m/%d %H:%M")
            try:
                await asyncio.to_thread(_report_upsert, session, {
                    "content": polished, "label": "會議逐字稿", "name": title,
                    "external_source": "meeting-recorder", "ts": time.time(),
                })
            except Exception as e:  # noqa: BLE001
                _log_event("meeting_report_failed",
                           error=type(e).__name__, error_message=str(e)[:200])
            content = (content + "\n\n" + polished).strip() if content else polished
        else:
            content = (content + "\n" + voice_text).strip() if content else voice_text

    parts = []
    if content:
        parts.append({"type": "text", "text": content})
    for a in attachments:
        if a.get("kind") == "image":
            path = _upload_ref_path(a.get("path")) or _save_attachment(a, a.get("filename") or "image.jpg")
            if path:
                parts.append({"type": "image_url", "image_url": {"url": path}})
        elif a.get("kind") == "audio":
            continue                       # transcript already in `content`
        else:
            path = _upload_ref_path(a.get("path")) or _save_attachment(a, a.get("filename") or "file")
            if not path:
                continue
            parts.append({"type": "file", "file": {"filename": a.get("filename"),
                          "mime_type": a.get("mime"), "file_data": path}})
    prompt = await _resolve_persona_prompt(
        [{"role": "user", "content": parts or content}],
        persona_home,
    )
    report_context = _report_context_for_prompt(session, content)
    if report_context:
        prompt = f"{report_context}\n\n---\n【使用者現在的訊息】\n{prompt}"

    att_meta = [{"kind": a.get("kind"), "filename": a.get("filename"),
                 "mime": a.get("mime"), "path": _upload_ref_path(a.get("path"))}
                for a in attachments]
    return content, att_meta, prompt


def _persona_launch_turn(session: str, prompt: str, client_id, common_log: dict,
                         turn_started: float, canonical_user_ok, cid: str,
                         user_text: str = "", user_mid: str = ""):
    """建 queue/state、把 persona 回合掛成獨立背景任務,回 (task, state, q)。

    v1 POST /app/v1/messages 串流消費 q;v2 統一路由 input 不消費 q(回覆走
    S3 卡片事件流)。回合獨立於 client 連線:斷網不斷回合,收尾一定落
    canonical。S3 digest 掛鉤都在這裡——delta/status/收尾,單一實作兩邊共用。

    `user_text`/`user_mid`:app→TG 反向鏡射(#32)用的 user 側原文與 canonical id。
    給了才鏡射 user 那句;`_persona_inject_turn`(approval relay)不給 —— 那不是
    使用者在 app 打的字,不該出現在 TG 聊天室裡。assistant 側一律在收尾處鏡射。
    預設關閉,見 `tg_outbound`。
    """
    q: asyncio.Queue = asyncio.Queue()
    state = {"acc": "", "usage": None, "content_chunks": 0, "keepalives": 0,
             "first_content_ms": None, "first_status_ms": None, "status_updates": 0,
             "runner_error": "", "stream_error": "", "canonical_reply_ok": None,
             "done_sent": False}

    async def run_turn():
        # Drains the persona turn to completion INDEPENDENTLY of the client
        # connection. If the app's network drops mid-stream this task keeps
        # going and records the reply, so the canonical store always reflects
        # what actually happened server-side (the tool ran, the calendar was
        # created) — a reload then shows the real reply instead of losing it.
        digest = _hp_digest_maybe(session)
        # app→TG:使用者這句先貼進 TG,順序才像一段對話(問在前、答在後)。
        if user_text:
            _tg_mirror_out(session, "user", user_text, user_mid)
        try:
            async for k, v in _persona_content_stream(session, prompt):
                if k == "content":
                    state["acc"] += v
                    state["content_chunks"] += 1
                    if state["first_content_ms"] is None:
                        state["first_content_ms"] = int(
                            (time.monotonic() - turn_started) * 1000
                        )
                    state["step_label"] = ""     # 正文恢復 → 步驟 label 讓位
                elif k == "usage":
                    state["usage"] = v
                elif k == "status":
                    # 步驟進度(執行步驟 N:工具)— 讓輪詢的 /messages/status
                    # 也能給 working bar 同一句人話。
                    state["step_label"] = (v or {}).get("label") or ""
                    state["status_updates"] += 1
                    if state["first_status_ms"] is None:
                        state["first_status_ms"] = int(
                            (time.monotonic() - turn_started) * 1000
                        )
                if digest is not None:
                    try:
                        if k == "content":
                            digest.turn_delta(cid, v)
                        elif k == "status":
                            digest.turn_status((v or {}).get("label") or "")
                    except Exception as e:  # noqa: BLE001
                        _log_event("hp_card_turn_error", session=session,
                                   error=str(e)[:160])
                await q.put((k, v))
        except Exception as e:  # noqa: BLE001
            _log_exc("_persona_launch_turn.run_turn", e, expected=False, session=session, cid=cid)
            state["runner_error"] = f"{type(e).__name__}: {str(e)[:180]}"
            await q.put(("error", str(e)))
        finally:
            reply_mid = ""
            if state["acc"] and _is_queue_ack(state["acc"]):
                # 排隊回執是狀態不是回覆:不落正典、不鏡 TG(卡片流即時顯示不受影響)。
                state["canonical_reply_ok"] = True
                _log_event("queue_ack_skipped", session=session, client=client_id or "")
            elif state["acc"]:
                _schedule_media_capture(f"hermes:{session}", state["acc"])
                reply_mid, reply_ok = _canon_add_retry(session, "assistant", state["acc"],
                                                       client_id=client_id)
                state["canonical_reply_ok"] = reply_ok
                # app→TG:人格的回覆也要進 TG 聊天室,否則 TG 那頭只看到問句。
                _tg_mirror_out(session, "assistant", state["acc"], reply_mid)
            if digest is not None:
                try:
                    digest.turn_end(cid, state["acc"], reply_mid=reply_mid or "",
                                    error=state["runner_error"])
                except Exception as e:  # noqa: BLE001
                    _log_event("hp_card_turn_error", session=session,
                               error=str(e)[:160])
            _log_event("app_turn_background_done", **common_log,
                       output_chars=len(state["acc"]),
                       content_chunks=state["content_chunks"],
                       first_content_ms=state["first_content_ms"],
                       first_status_ms=state["first_status_ms"],
                       status_updates=state["status_updates"],
                       usage_used=(state["usage"] or {}).get("used"),
                       usage_size=(state["usage"] or {}).get("size"),
                       canonical_user_ok=canonical_user_ok,
                       canonical_reply_ok=state["canonical_reply_ok"],
                       runner_error=state["runner_error"] or None,
                       duration_ms=int((time.monotonic() - turn_started) * 1000))
            await q.put((None, None))

    _log_event("app_turn_model_start", **common_log,
               prompt_chars=len(prompt), canonical_user_ok=canonical_user_ok)
    # The turn runs as a handler-scope task (not inside the generator) so a
    # duplicate POST can attach to it via _APP_TURN_INFLIGHT before the client
    # even starts reading this response.
    task = asyncio.create_task(run_turn())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task, state, q


@app.post("/app/v1/messages")
async def app_post_message(request: Request):
    """Send a turn: record the user message canonically, run the persona turn,
    stream the reply (OpenAI-style SSE), and record the reply canonically too."""
    _check_auth(request)
    body = await request.json()
    session = body.get("session") or "xcash"
    if _openclaw_key_from_session_id(session):
        return await _openclaw_v1_post_message(body, request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    content = (body.get("content") or "").strip()
    attachments = body.get("attachments") or []   # [{kind,filename,mime,data(dataURI)|path}]
    dry_run = bool(body.get("dry_run"))

    client_id = body.get("client_id")    # stable across retries; enables idempotency
    cid = "appmsg-" + uuid.uuid4().hex[:20]
    created = int(time.time())
    turn_started = time.monotonic()
    common_log = {
        "cid": cid,
        "session": session,
        "client_id_hash": _short_hash(client_id),
        "client": _client_host(request),
        "dry_run": dry_run,
        "input_chars": len(content),
        **_attachment_stats(attachments),
    }
    _log_event("app_turn_received", **common_log)

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def status_chunk(state: str, label: str):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session,
                   "status": {"state": state, "label": label},
                   "choices": [{"index": 0, "delta": {}, "finish_reason": None}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def stream_response(events):
        # Explicit anti-buffering headers protect token deltas when this route
        # sits behind nginx/CDN; uvicorn also flushes each yielded SSE frame.
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # Retry idempotency: if this exact logical send already produced a recorded
    # reply (first attempt completed server-side but the app's network dropped
    # before it saw the reply), replay that reply — do NOT re-run the turn or
    # repeat its side effects (e.g. creating the calendar event twice).
    if not dry_run and client_id:
        prior = _canon_reply_for_client(session, client_id)
        if prior is not None:
            async def replay_agen():
                done_sent = False
                try:
                    yield chunk({"role": "assistant", "content": ""})
                    yield status_chunk("replayed", "已找到上一輪完成回覆，正在重播。")
                    yield chunk({"content": prior})
                    payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                               "model": session, "replayed": True,
                               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    done_sent = True
                finally:
                    _log_event("app_turn_stream_done", **common_log,
                               replayed=True, output_chars=len(prior),
                               done_sent=done_sent,
                               duration_ms=int((time.monotonic() - turn_started) * 1000),
                               canonical_user_ok=None, canonical_reply_ok=True)
            return stream_response(replay_agen())

    # In-flight idempotency (issue #9): the canonical replay above only covers
    # turns that already FINISHED. A duplicate POST while the first run is still
    # going must not start a second run — it attaches to the in-flight one.
    inflight_key = (session, client_id) if client_id else None
    inflight_entry = None
    attached = None
    if not dry_run and inflight_key:
        async with _APP_TURN_INFLIGHT_LOCK:
            _now = time.monotonic()
            for k in [k for k, e in _APP_TURN_INFLIGHT.items()
                      if _now - e["ts"] > _APP_TURN_INFLIGHT_TTL]:
                _APP_TURN_INFLIGHT.pop(k, None)   # TTL cleanup on each access
            attached = _APP_TURN_INFLIGHT.get(inflight_key)
            if attached is None:
                inflight_entry = {"ts": _now, "wall": time.time(), "task": None, "state": None}
                _APP_TURN_INFLIGHT[inflight_key] = inflight_entry

    if attached is not None:
        _log_event("app_turn_attach", **common_log)

        async def attach_agen():
            done_sent = False
            acc = ""
            sent_chars = 0
            last_label = None
            last_emit = time.monotonic()
            try:
                yield chunk({"role": "assistant", "content": ""})
                yield status_chunk("attached", "同一則訊息已在處理中，附掛原回合等待結果。")
                t0 = time.monotonic()
                while True:
                    _task = attached.get("task")
                    st = attached.get("state") or {}
                    current = st.get("acc") or ""
                    if len(current) > sent_chars:
                        yield chunk({"content": current[sent_chars:]})
                        sent_chars = len(current)
                        last_emit = time.monotonic()
                    label = st.get("step_label") or ""
                    if label and label != last_label:
                        yield status_chunk("running", label)
                        last_label = label
                        last_emit = time.monotonic()
                    if _task is not None and _task.done():
                        break
                    if _task is None and time.monotonic() - t0 > 30:
                        break   # original request died before starting its turn
                    if time.monotonic() - t0 > _APP_TURN_INFLIGHT_TTL:
                        break
                    if time.monotonic() - last_emit >= SSE_KEEPALIVE_SECS:
                        yield ": keepalive\n\n"
                        last_emit = time.monotonic()
                    await asyncio.sleep(0.1)
                st = attached.get("state") or {}
                acc = st.get("acc") or ""
                if len(acc) > sent_chars:
                    yield chunk({"content": acc[sent_chars:]})
                    sent_chars = len(acc)
                elif not acc:
                    yield chunk({"content": "(原回合沒有產出回覆)"})
                payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": session, "replayed": True,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                done_sent = True
            finally:
                _log_event("app_turn_stream_done", **common_log,
                           replayed=True, attached=True, output_chars=len(acc),
                           done_sent=done_sent,
                           duration_ms=int((time.monotonic() - turn_started) * 1000),
                           canonical_user_ok=None, canonical_reply_ok=None)
        return stream_response(attach_agen())

    if dry_run:
        async def dry_agen():
            done_sent = False
            text = f"✅ dry-run ok: {session} message path is reachable; nothing was persisted."
            try:
                yield chunk({"role": "assistant", "content": ""})
                yield status_chunk("accepted", "dry-run 已送達 bridge。")
                yield chunk({"content": text})
                payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": session,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                done_sent = True
            finally:
                _log_event("app_turn_stream_done", **common_log,
                           replayed=False, output_chars=len(text),
                           done_sent=done_sent,
                           duration_ms=int((time.monotonic() - turn_started) * 1000),
                           canonical_user_ok=None, canonical_reply_ok=None)
        return stream_response(dry_agen())

    try:
        content, att_meta, prompt = await _persona_prepare_turn(
            session, content, attachments, stt_lang=str(body.get("stt_lang") or ""))
        acp_session = await POOL.get(session, home_for(session))
        queued_at_accept = acp_session.is_busy()

        # Record the transcript as the canonical text (so other devices see what
        # was said even without the audio bytes), tagged so the app can show 🎤.
        _user_mid, canonical_user_ok = _canon_add_retry(session, "user", content,
                                                        att_meta, client_id=client_id)
        _hp_cards_turn_start(session, cid, _user_mid, content, att_meta)

        task, state, q = _persona_launch_turn(session, prompt, client_id, common_log,
                                              turn_started, canonical_user_ok, cid,
                                              user_text=content, user_mid=_user_mid)
    except BaseException:
        # claim → launch 之間失敗必須釋放 claim,否則同 client_id 的重試會被
        # 附掛到一個永遠不會啟動的 entry(task=None)直到 600s TTL(issue #9)。
        if inflight_entry is not None and \
                _APP_TURN_INFLIGHT.get(inflight_key) is inflight_entry:
            _APP_TURN_INFLIGHT.pop(inflight_key, None)
            _log_event("app_turn_inflight_released", session=session,
                       client_id_hash=_short_hash(client_id),
                       reason="prelaunch_error", via="v1_messages")
        raise
    if inflight_entry is not None:
        inflight_entry["task"] = task
        inflight_entry["state"] = state

    async def agen():
        try:
            yield chunk({"role": "assistant", "content": ""})
            if queued_at_accept:
                yield status_chunk("queued", "已收到 · 上一輪還在跑，這則會排隊處理。")
            else:
                yield status_chunk("accepted", "已送達 Hermes，等待回覆。")
            while True:
                k, v = await q.get()
                if k is None:
                    break
                if k == "content":
                    yield chunk({"content": v})
                elif k == "keepalive":
                    state["keepalives"] += 1
                    yield ": keepalive\n\n"
                elif k == "status":
                    if isinstance(v, dict):
                        yield status_chunk(v.get("state") or "running",
                                           v.get("label") or "Hermes 開始處理")
                elif k == "error":
                    state["stream_error"] = str(v)[:180]
            final = {"index": 0, "delta": {}, "finish_reason": "stop"}
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": session, "choices": [final]}
            if state["usage"] and state["usage"].get("size"):
                payload["usage"] = {"context_used": state["usage"].get("used"),
                                    "context_size": state["usage"].get("size")}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            state["done_sent"] = True
        finally:
            _log_event("app_turn_stream_done", **common_log,
                       replayed=False,
                       output_chars=len(state["acc"]),
                       content_chunks=state["content_chunks"],
                       first_content_ms=state["first_content_ms"],
                       first_status_ms=state["first_status_ms"],
                       status_updates=state["status_updates"],
                       keepalives=state["keepalives"],
                       done_sent=state["done_sent"],
                       canonical_user_ok=canonical_user_ok,
                       canonical_reply_ok=state["canonical_reply_ok"],
                       stream_error=state["stream_error"] or None,
                       duration_ms=int((time.monotonic() - turn_started) * 1000))

    return stream_response(agen())


@app.post("/app/v1/messages/interrupt")
async def app_message_interrupt(request: Request):
    _check_auth(request)
    body = await request.json()
    session = body.get("session") or "xcash"
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    return await _persona_interrupt_core(session)


async def _persona_interrupt_core(session: str) -> dict:
    """persona 中斷核心 — v1 與 v2 統一路由共用。
    Same verify-and-retry contract as /ccsessions/{name}/interrupt: don't
    report ok on a cancel that didn't land — check busy and retry up to 3×."""
    acp_session = await POOL.get(session, home_for(session))
    if not acp_session.is_busy():
        raise HTTPException(status_code=409, detail="no active turn")
    attempts = 0
    interrupted = False
    for _ in range(3):
        attempts += 1
        await acp_session.cancel()
        await asyncio.sleep(0.7)
        if not acp_session.is_busy():
            interrupted = True
            break
    _log_event("persona_interrupt", session=session,
               interrupted=interrupted, attempts=attempts)
    return {"ok": True, "session": session,
            "interrupted": interrupted, "attempts": attempts}


# ───────────────────────── Approval Center (M21) ───────────────────────────
# Hermes skills (post / email / story / backup cleanup / risky tasks) POST an
# approval here; the app shows a native approve/reject card with TTL + risk; the
# skill polls the decision. Bridge owns the store (no Hermes internals exposed).

# A1:讀取端共用的欄位序 — SELECT 一律用這一串,tuple 索引不漂移。
_APPROVAL_COLS = ("id,title,source,risk,detail,created_at,expires_at,status,"
                  "decided_at,result,session_id,provider,kind,options,meta")
_APPROVAL_KINDS = ("permission", "question", "notice")


def _approval_default_options(kind: str) -> list:
    """options 未宣告時的預設鍵(APPROVAL_HUB_SPEC §1/§2)。"""
    if kind == "notice":
        return [{"key": "ack", "label": "知道了", "style": "primary"}]
    return [{"key": "approve", "label": "允許", "style": "primary"},
            {"key": "deny", "label": "拒絕", "style": "danger"}]


def _approval_provider_of(source: str) -> str:
    if source.startswith("claude_code:"):
        return "claude_code"
    if source.startswith("codex"):
        return "codex"
    if source.startswith("openclaw"):
        return "openclaw"
    return "hermes"


def _approval_row(r):
    """DB tuple(_APPROVAL_COLS 序)→ 統一 approval 物件(spec §1)。
    舊欄位全保留(相容期);新欄位缺值時由 source 推導 — 遷移前的舊列與
    新列走同一條序列化,wire 形狀只有這一份。"""
    src = str(r[2] or "")
    kind = r[12] if r[12] in _APPROVAL_KINDS else "permission"
    options = None
    if r[13]:
        try:
            options = json.loads(r[13])
        except (TypeError, ValueError):
            options = None
    meta = None
    if len(r) > 14 and r[14]:
        try:
            meta = json.loads(r[14])
        except (TypeError, ValueError):
            meta = None
    out = {"id": r[0], "title": r[1], "source": r[2], "risk": r[3], "detail": r[4],
           "created_at": r[5], "expires_at": r[6], "status": r[7],
           "decided_at": r[8], "result": r[9],
           "session_id": r[10] or (src if src.startswith(
               ("claude_code:", "codex:", "openclaw:")) else ""),
           "provider": r[11] or _approval_provider_of(src),
           "kind": kind,
           "options": options or _approval_default_options(kind)}
    if isinstance(meta, dict) and meta:
        out["meta"] = meta
    return out


def _approval_get_row(aid: str):
    """單筆統一物件(v2 meta.approval / 決定路由用);不存在回 None。"""
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        r = con.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE id=?",
                        (aid,)).fetchone()
        con.close()
        return _approval_row(r) if r else None
    finally:
        con.close()


def _hermes_pending_by_session() -> dict:
    """hermes persona 的 pending 待審(session_id='hermes:{mid}')→ 統一物件,
    每 persona 取最早一筆。v2 sessions 補 waiting_approval 用(spec §7-5)。"""
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            _approvals_expire(con)
            con.commit()
            rows = con.execute(
                f"SELECT {_APPROVAL_COLS} FROM approvals WHERE status='pending'"
                " AND session_id LIKE 'hermes:%' ORDER BY created_at ASC").fetchall()
            con.close()
            for r in rows:
                d = _approval_row(r)
                out.setdefault(d["session_id"], d)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("hermes_pending_scan_failed", error=str(e)[:160])
    return out


def _openclaw_pending_by_session() -> dict:
    """openclaw 的 pending 待審(session_id='openclaw:{sessionKey}')→ 統一物件,
    每 session 取最早一筆。v2 sessions 補 waiting_approval 用(同 hermes 線)。"""
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        try:
            _approvals_expire(con)
            con.commit()
            rows = con.execute(
                f"SELECT {_APPROVAL_COLS} FROM approvals WHERE status='pending'"
                " AND session_id LIKE 'openclaw:%' ORDER BY created_at ASC").fetchall()
            con.close()
            for r in rows:
                d = _approval_row(r)
                out.setdefault(d["session_id"], d)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("openclaw_pending_scan_failed", error=str(e)[:160])
    return out


def _approvals_expire(con):
    now = time.time()
    # A3:過期不只翻 DB 狀態,存在中的卡片流也要同卡收尾(不然 pending 卡
    # 掛著可點,點了才吃 409)。先撈再改;卡片收尾是記憶體操作、冪等。
    try:
        stale = con.execute(
            "SELECT id, title, session_id FROM approvals WHERE status='pending' "
            "AND expires_at IS NOT NULL AND expires_at < ?", (now,)).fetchall()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_approvals_expire", _exc, expected=True)
        stale = []
    con.execute("UPDATE approvals SET status='expired' WHERE status='pending' "
                "AND expires_at IS NOT NULL AND expires_at < ?", (now,))
    for aid, title, sid in stale:
        rec = {"id": aid, "title": title}
        sid = str(sid or "")
        try:
            if sid.startswith("hermes:"):
                _hp_cards_feed_approval(sid, rec, resolved="expired")
            elif sid.startswith("claude_code:"):
                _cc_cards_feed_approval(sid.split(":", 1)[1], rec,
                                        resolved="expired")
            elif sid.startswith("openclaw:"):
                _OC_APPROVAL_METHODS.pop(aid, None)
                _oc_cards_feed_approval(sid.split(":", 1)[1], rec,
                                        resolved="expired")
        except Exception as e:  # noqa: BLE001
            _log_event("approval_expire_feed_error", id=aid, error=str(e)[:160])


# B4 (issue #9): an approval that never expires pends forever if the phone
# misses the push — default to 1h, clamp to [30s, 7d] so a typo'd ttl can't
# create an immortal (or instantly-dead) row.
_APPROVAL_TTL_DEFAULT = 3600.0
_APPROVAL_TTL_MIN, _APPROVAL_TTL_MAX = 30.0, 7 * 86400.0


async def _approval_fire_callback(aid: str, callback: str, status: str, result,
                                  key: str = ""):
    """POST the decision to the creator's callback URL (fire-and-forget).
    A3:callback=="persona-relay:" 時不走 HTTP —— 把選中選項的 send 文字
    (缺席退 label)注入該 persona 對話,由人格接手執行(FED 審稿等
    「決定即指令」流)。"""
    if callback == "persona-relay:":
        try:
            await _approval_persona_relay(aid, status, key or str(result or ""))
        except Exception as e:  # noqa: BLE001
            _log_event("approval_relay_failed", id=aid, status=status,
                       error=type(e).__name__, error_message=str(e)[:160])
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(callback, json={"id": aid, "status": status,
                                                  "result": result, "key": key})
        _log_event("approval_callback_sent", id=aid, status=status,
                   http_status=r.status_code)
    except Exception as e:  # noqa: BLE001
        _log_event("approval_callback_failed", id=aid, status=status,
                   error=type(e).__name__, error_message=str(e)[:160])


async def _approval_persona_relay(aid: str, status: str, key: str):
    """決定 → persona 指令注入。expired/逾時不注入(沒人做決定就不該有動作)。"""
    if status == "expired" or not key:
        return
    d = _approval_get_row(aid)
    sid = str((d or {}).get("session_id") or "")
    if not sid.startswith("hermes:"):
        return
    session = sid.split(":", 1)[1]
    if session not in PERSONAS:
        _log_event("approval_relay_skipped", id=aid, reason="unknown persona",
                   session=session)
        return
    opt = next((o for o in (d.get("options") or [])
                if str(o.get("key") or "") == key), None)
    text = str((opt or {}).get("send") or (opt or {}).get("label") or "").strip()
    if not text:
        return
    _log_event("approval_relay_inject", id=aid, session=session, key=key,
               chars=len(text))
    await _persona_inject_turn(
        session,
        f"【審核決定 · {d.get('title') or aid}】{text}",
        via="approval_relay")


async def _persona_inject_turn(session: str, content: str, via: str):
    """內部發起的 persona 回合(approval persona-relay 等):與 v1/v2 input
    同一套前置/canonical/卡片掛鉤,fire-and-forget —— 回覆走 S3 卡片事件流
    與 canonical,不佔任何 client 連線。"""
    cid = "appmsg-" + uuid.uuid4().hex[:20]
    turn_started = time.monotonic()
    common_log = {"cid": cid, "session": session, "client_id_hash": None,
                  "client": "internal", "dry_run": False,
                  "input_chars": len(content), "via": via}
    _log_event("app_turn_received", **common_log)
    content, att_meta, prompt = await _persona_prepare_turn(session, content, [])
    user_mid, canonical_user_ok = _canon_add_retry(session, "user", content,
                                                   att_meta)
    _hp_cards_turn_start(session, cid, user_mid, content, att_meta)
    _persona_launch_turn(session, prompt, None, common_log, turn_started,
                         canonical_user_ok, cid)


@app.post("/app/v1/approvals")
async def approval_create(request: Request):
    """Create a pending approval (called by Hermes / a skill).
    A1(spec §3.4):`source` 升級為 `session_id`(舊名相容照收);新增
    `kind`(permission|question|notice,預設 permission)與 `options`
    (建立方宣告的鍵,bridge 驗形狀、收斂 style 字彙;缺席由讀取端給預設)。"""
    _check_auth(request)
    import sqlite3
    b = await request.json()
    aid = b.get("id") or uuid.uuid4().hex
    try:
        ttl = float(b.get("ttl_seconds") or _APPROVAL_TTL_DEFAULT)
    except (TypeError, ValueError):
        ttl = _APPROVAL_TTL_DEFAULT
    ttl = max(_APPROVAL_TTL_MIN, min(ttl, _APPROVAL_TTL_MAX))
    callback = (str(b.get("callback_url") or "").strip() or None)
    # A3:persona-relay: 為內部 callback 傳輸 —— 決定後把選中選項的 send
    # 文字注入該 persona 對話(FED 審稿等「決定即指令」流),不走 HTTP。
    if callback and callback != "persona-relay:" \
            and not callback.startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="callback_url must be http(s) or persona-relay:")
    session_id = str(b.get("session_id") or b.get("source") or "").strip()
    if callback == "persona-relay:" and not (
            session_id.startswith("hermes:")
            and session_id.split(":", 1)[1] in PERSONAS):
        raise http_err(400, "INVALID_CALLBACK",
                       "persona-relay: 需要 session_id=hermes:{persona}(已註冊人格)")
    kind = str(b.get("kind") or "permission").strip()
    if kind not in _APPROVAL_KINDS:
        raise http_err(400, "INVALID_KIND", f"kind 必須是 {'|'.join(_APPROVAL_KINDS)}")
    options = b.get("options")
    if options is not None:
        if (not isinstance(options, list) or not options
                or not all(isinstance(o, dict) and str(o.get("key") or "").strip()
                           and str(o.get("label") or "").strip() for o in options)):
            raise http_err(400, "INVALID_OPTIONS",
                           "options 需為 [{key,label[,style]}…] 且 key/label 非空")
        norm = []
        for o in options[:6]:
            ent = {"key": str(o["key"]).strip()[:40], "label": str(o["label"]).strip()[:80]}
            style = str(o.get("style") or "").strip()
            if style == "deny":                     # 舊字彙收斂(spec §1 用 danger)
                style = "danger"
            if style in ("primary", "secondary", "danger"):
                ent["style"] = style
            # A3:send = 建立方宣告「這鍵決定後要對 persona 說的話」
            # (persona-relay 消費;app 不認得就忽略,fallback 原則)。
            send = str(o.get("send") or "").strip()
            if send:
                ent["send"] = send[:200]
            norm.append(ent)
        options = norm
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        con.execute("INSERT OR REPLACE INTO approvals"
                    "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                    "session_id,provider,kind,options) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, b.get("title") or "需要核准", session_id, b.get("risk") or "",
                     b.get("detail") or "", now, now + ttl, "pending", None, None, callback,
                     session_id or None, _approval_provider_of(session_id), kind,
                     json.dumps(options, ensure_ascii=False) if options else None))
        con.commit()
        con.close()
        title = b.get("title") or "需要核准"
        try:
            # A3:hermes create 流程補齊卡片流 — pending → approval 卡(與
            # cc/codex 同一組 wire shape,見 carddigest.ApprovalCardMixin)。
            _hp_cards_feed_approval(session_id, _approval_get_row(aid) or {})
        except Exception as e:  # noqa: BLE001
            _log_event("hp_cards_feed_error", error=str(e)[:160])
        if b.get("push") is False:
            # A3:建立方已用自己的通道通知過(例:cron 報告本體已推)→ 不疊
            # 推播;待審列/卡片照常存在。
            return {"id": aid, "status": "pending", "expires_at": now + ttl,
                    "kind": kind, "session_id": session_id}
        body = (b.get("detail") or session_id or "點開查看並決定")[:120]
        _approval_push(aid, title, body, session_id)
        return {"id": aid, "status": "pending", "expires_at": now + ttl,
                "kind": kind, "session_id": session_id}
    finally:
        con.close()


@app.post("/app/v1/devices")
async def register_device(request: Request):
    """App registers its APNs device token here on launch / token refresh."""
    _check_auth(request)
    b = await request.json()
    token = (b.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="missing token")
    _device_add(token, b.get("platform") or "ios")
    return {"ok": True, "devices": len(_devices())}


@app.get("/app/v1/devices")
async def list_devices(request: Request):
    _check_auth(request)
    return {"count": len(_devices())}


@app.post("/app/v1/push/register")
async def push_register(request: Request):
    """feat/apns-sender:註冊 device token + 通知偏好(取代舊 /app/v1/devices,
    舊端點保留給還沒更新的 app)。body:

        {"token": "<hex>", "platform": "ios",
         "preview": true|false,          # 選填,預設 true;false=通知只顯示人格名
         "personas": ["sess", ...]|null} # 選填,null/缺席=訂閱全部人格

    冪等 — app 每次啟動/偏好變更都重打。回傳 apns_configured 讓 app 知道
    bridge 端金鑰是否已配置(未配置=架構就緒但推不出去)。"""
    _check_auth(request)
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    token = (b.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="missing token")
    personas = b.get("personas")
    if personas is not None and not isinstance(personas, list):
        raise HTTPException(status_code=400, detail="personas must be a list or null")
    _device_add(token, b.get("platform") or "ios")
    prefs = _push_pref_set(token,
                           preview=b.get("preview") is not False,
                           personas=personas)
    return {"ok": True, "devices": len(_devices()), "prefs": prefs,
            "apns_configured": apns_configured()}


@app.post("/app/v1/push/test")
async def push_test(request: Request):
    """Send a test push to every registered device — verifies APNs auth end-to-end."""
    _check_auth(request)
    b = await request.json() if await request.body() else {}
    res = await push_notify(b.get("title") or "Pocket Agent",
                            b.get("body") or "測試推播 ✅ M23 已接上",
                            {"kind": "test"})
    # 回傳真實 APNs 結果(topic、每台裝置的 code/detail)—— 以前一律回 200 讓人盲測。
    # apns_configured=False 時 push_notify 短路(disabled),這裡如實回報。
    return {"sent": res["sent"], "devices": res["total"],
            "apns_topic": APNS_BUNDLE_ID, "failures": res["failures"],
            "apns_configured": apns_configured(),
            "disabled": res.get("disabled", False)}


@app.get("/app/v1/approvals")
async def approval_list(request: Request, status: str = "", limit: int = 50,
                        offset: int = 0):
    """List approvals. B4 (issue #9): paginated — limit is clamped, `offset`
    pages back, `total` lets the app render 'N more'."""
    _check_auth(request)
    import sqlite3
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        _approvals_expire(con)
        con.commit()
        if status:
            total = con.execute("SELECT COUNT(*) FROM approvals WHERE status=?",
                                (status,)).fetchone()[0]
            rows = con.execute(f"SELECT {_APPROVAL_COLS} "
                               "FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                               (status, lim, off)).fetchall()
        else:
            total = con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
            rows = con.execute(f"SELECT {_APPROVAL_COLS} "
                               "FROM approvals ORDER BY created_at DESC LIMIT ? OFFSET ?",
                               (lim, off)).fetchall()
        con.close()
        out = [_approval_row(r) for r in rows]
        return {"approvals": out, "total": total,
                "next_offset": (off + lim) if off + lim < total else None}
    finally:
        con.close()


@app.get("/app/v1/approvals/{aid}")
async def approval_get(aid: str, request: Request):
    """Poll a decision (called by the requesting skill)."""
    _check_auth(request)
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        _approvals_expire(con)
        con.commit()
        r = con.execute(f"SELECT {_APPROVAL_COLS} "
                        "FROM approvals WHERE id=?", (aid,)).fetchone()
        con.close()
        if not r:
            raise http_err(404, "APPROVAL_NOT_FOUND", "unknown approval")
        return _approval_row(r)
    finally:
        con.close()


@app.post("/app/v1/approvals/{aid}/decision")
async def approval_decide(aid: str, request: Request):
    """Approve / reject (from the app)."""
    _check_auth(request)
    b = await request.json()
    return await _approval_decide_core(aid, b)


async def _approval_decide_core(aid: str, b: dict) -> dict:
    """Approval Center 決議核心 — v1 與 v2 統一路由 approve 共用。
    A1(spec §3.2):`{key}` 為決定的第一公民;`{approve: bool}` 保留為相容糖
    (approve→第一個 primary、deny→第一個 danger)。status 依 kind 落:
    permission→approved|denied、question→answered(result=key)、
    notice→acknowledged。新決議寫 `denied`(拍板);歷史列的 `rejected`
    讀取端一律視為等價,A4 收斂。"""
    import sqlite3
    key = str(b.get("key") or "").strip()
    # #13 收尾:契約 body {"approval_id","decision":"approve"|"deny"} 的
    # decision 字串與 {approve: bool} 等價 —— 入口統一折成 approve bool,
    # cc/codex/hermes 三分支照舊只認 key / approve 即可。
    if not key and "approve" not in b and str(
            b.get("decision") or b.get("status") or b.get("action") or "").strip():
        b = {**b, "approve": _approval_bool_from_body(b)}
    d = _approval_get_row(aid)
    src = str((d or {}).get("source") or "")
    if d and src.startswith("claude_code:"):
        # 批次 3 斷點③:CC 審核決議 → 回流 TUI 鍵。以「當下 pane 的 prompt」
        # 為準(推播到點按之間 prompt 可能已被回掉——過時就 409,不盲送鍵)。
        name = src.split(":", 1)[1]
        active = _CC_APPROVAL_ACTIVE.get(name)
        st = await _cc_status_core(name)
        prompt = st.get("prompt")
        if not prompt or not active or active.get("aid") != aid:
            _cc_approval_set_status(aid, "expired")
            raise HTTPException(status_code=409, detail="already decided or expired")
        key = key or _cc_choice_key(prompt, bool(b.get("approve")))
        # 決議語意:帶 approve bool 用 bool;只給 {key} 時由該鍵的 style 判斷
        # (danger/esc=否決)— 之前 {key} 決定一律被記成 rejected 是誤標。
        if "approve" in b:
            decision = "approved" if b.get("approve") else "denied"
        else:
            styles = {str(o.get("key") or ""): o.get("style")
                      for o in (d.get("options") or [])}
            decision = "denied" if (key == "esc" or styles.get(key) == "danger") \
                else "approved"
        await _cc_key_core(name, key)
        _cc_approval_set_status(aid, decision)
        _CC_APPROVAL_ACTIVE.pop(name, None)
        try:
            # A3:決定發生時也要收尾卡片流(同一決定路徑,三 provider 一致)。
            _cc_cards_feed_approval(name, d, resolved=decision)
        except Exception as e:  # noqa: BLE001
            _log_event("cc_cards_feed_error", error=str(e)[:160])
        _log_event("cc_approval_decision", session=name, approval_id=aid,
                   status=decision, key=key)
        return {"id": aid, "status": decision, "key": key}
    if d and src.startswith("openclaw"):
        # openclaw 的審批真相在 gateway 手上(bridge 只是鏡像)——一定要先
        # 打 `*.approval.resolve`,成功了才改 DB。反過來寫會出現「app 顯示
        # 已核准、agent 還在那裡等」。
        return await _oc_approval_decide(aid, d, b)
    if d and src.startswith("codex"):
        # question 類 server request(item/tool/requestUserInput /
        # mcpServer/elicitation/request)不是二元核准 —— key 就是答案，
        # 另有自由輸入 text。走 answer_question，狀態落 answered。
        if (d.get("kind") or "") == "question":
            try:
                result = await CODEX_APP.answer_question(
                    aid, key=key, text=str(b.get("text") or b.get("answer") or ""))
                return {"id": aid, "status": result["status"],
                        "key": key, "result": result["result"]}
            except CodexAppServerError as e:
                if e.code == 404:
                    raise http_err(409, "APPROVAL_NOT_PENDING",
                                   "Codex question is no longer live")
                _codex_http_error(e)
        # {key} → app-server 決議參數;approve_for_session 映射 Codex 原生
        # acceptForSession(_approval_response_result 既有機制)。codex 線的
        # 狀態字彙(approved/rejected)相容期不動 — 卡片流/記憶體 record 同源。
        if key:
            approved = key != "deny"
            for_session = key == "approve_for_session"
        else:
            approved = bool(b.get("approve"))
            for_session = bool(b.get("for_session") or b.get("approve_for_session") or
                               b.get("remember"))
        try:
            result = await CODEX_APP.decide_approval(aid, approved,
                                                     for_session=for_session)
            return {"id": aid, "status": result["status"], "result": result["result"]}
        except CodexAppServerError as e:
            if e.code == 404:
                raise http_err(409, "APPROVAL_NOT_PENDING",
                               "Codex approval is no longer live")
            _codex_http_error(e)
    # hermes / 本地列(permission|question|notice):key 或相容糖決議
    kind = (d or {}).get("kind") or "permission"
    options = (d or {}).get("options") or _approval_default_options(kind)
    okeys = [str(o.get("key") or "") for o in options]
    if d and key and key not in okeys:
        raise http_err(400, "UNKNOWN_KEY", f"key 必須是 {okeys} 之一")
    if not key:
        want = "primary" if b.get("approve") else "danger"
        key = next((str(o.get("key")) for o in options if o.get("style") == want),
                   "approve" if b.get("approve") else "deny")
    if kind == "notice":
        status = "acknowledged"
    elif kind == "question":
        status = "answered"
    else:
        styles = {str(o.get("key") or ""): o.get("style") for o in options}
        status = "denied" if styles.get(key) == "danger" else "approved"
    # result:question/notice 的答案就是 key(spec §2);permission 維持舊預設
    # (建立方自帶 result 優先,否則空字串)以免驚動既有 callback 消費者。
    result_val = str(b.get("result") or "") or (key if kind != "permission" else "")
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        cur = con.execute("UPDATE approvals SET status=?, decided_at=?, result=? "
                          "WHERE id=? AND status='pending'",
                          (status, time.time(), result_val, aid))
        con.commit()
        changed = cur.rowcount
        cb_row = con.execute("SELECT callback FROM approvals WHERE id=?", (aid,)).fetchone()
        con.close()
        if not changed:
            raise HTTPException(status_code=409, detail="already decided or expired")
        try:
            # A3:hermes 決定發生時收尾卡片流(同一決定路徑,三 provider 一致)。
            # _hp_cards_feed_approval 內部會篩 session_id 前綴,非 hermes: 的列
            # (例如舊資料 session_id 空缺)在這裡是安全 no-op。
            _hp_cards_feed_approval(str((d or {}).get("session_id") or ""),
                                   d or {}, resolved=status)
        except Exception as e:  # noqa: BLE001
            _log_event("hp_cards_feed_error", error=str(e)[:160])
        # B4 (issue #9): push the decision back to the creator (Hermes skill / TG
        # flow) so it doesn't have to poll GET /app/v1/approvals/{id}.
        if cb_row and cb_row[0]:
            asyncio.create_task(_approval_fire_callback(
                aid, cb_row[0], status, result_val, key=key))
        # 2b:人格 choices 審核決議 → 把選項的 send 文字當人格回合送回(如 FLiPER
        # 「解除待檢討」→ 送 "resume 386563" 給潘天晴,與聊天視窗點按鈕等效)。
        if src.startswith("hermes:") and status == "answered":
            persona = src.split(":", 1)[1]
            chosen = next((o for o in options if str(o.get("key")) == key), None)
            send_text = (chosen or {}).get("send")
            if persona in PERSONAS and send_text:
                asyncio.create_task(
                    _persona_inject_turn(persona, str(send_text), "approval-choice"))
                _log_event("hp_choices_decision_relayed", session=persona,
                           approval_id=aid, key=key)
        return {"id": aid, "status": status, "key": key}
    finally:
        con.close()


@app.post("/dispatch")
async def dispatch(request: Request):
    """Hermes (or a tool) asks the bridge to spawn a CC/Codex sub-agent.
    Returns a session id that shows up in GET /sessions and streams like a chat."""
    _check_auth(request)
    body = await request.json()
    tool = body.get("tool", "claude-code")
    task = (body.get("task") or "").strip()
    cwd = os.path.expanduser(body.get("cwd") or HOME_ROOT)
    parent = body.get("parent", "yuanfang")
    isolate = bool(body.get("isolate"))
    if not task:
        raise http_err(400, "TASK_REQUIRED", "task required")
    # spawn config(設計 §2.1/§2.2):派子程序時套的完整配置 + BYO api_key。
    # 這是唯一真正吃 CLI flags + env 注入的路徑(headless 子程序)。
    try:
        spawn_cfg = _spawn_config_validate(
            body.get("config"), "cx" if tool == "codex" else "cc")
    except SpawnConfigError as e:
        raise HTTPException(status_code=400, detail=e.detail)
    # 戶政(藍圖 §3.1):派工前過配額;headless dispatch 屬短命工,預設 task。
    reg_parent = (str(body.get("parent_session") or "").strip()
                  or (f"hermes:{parent}" if parent in PERSONAS else None))
    reg_cls = _registry_class_of(body, default_cls="task")
    if body.get("parent_session"):
        _registry_validate_parent(reg_parent)   # addendum 1:明給的 parent 必須存在
    _registry_precheck_or_429(reg_parent, reg_cls)
    sid = "sub-" + uuid.uuid4().hex[:16]
    redacted = _spawn_config_redacted(spawn_cfg)
    SUBSESSIONS[sid] = {"name": task[:40], "parent": parent, "tool": tool,
                        "status": "running", "lastAt": time.time(), "cwd": cwd,
                        "proc": None, "output": [("text", f"**任務:** {task}\n\n")],
                        "spawn_config": _spawn_config_public(spawn_cfg)}
    # 完整 config(含 api_key)只放記憶體,供追問 resume 重建 env;絕不持久化。
    if spawn_cfg:
        _SPAWN_SECRETS[sid] = spawn_cfg
    _subsession_persist(sid)   # issue #5: registered rows survive a restart
    _registry_register(sid, provider="dispatch", name=task[:40],
                       purpose=(body.get("purpose") or "").strip() or task[:200],
                       cls=reg_cls, parent=reg_parent)
    if redacted:
        _log_event("dispatch_spawn_config", sid=sid, tool=tool, **redacted)
    asyncio.create_task(_run_dispatch(sid, tool, task, cwd, isolate, config=spawn_cfg))
    return {"session_id": sid, "type": "subprocess", "tool": tool, "parent": parent,
            "spawn_config": _spawn_config_public(spawn_cfg)}


async def _make_worktree(base: str, sid: str):
    """Isolate a worker in its own git worktree (like a branch) so parallel
    dispatches don't clobber each other's edits. Returns the worktree path, or
    the original base if it isn't a git repo / the command fails."""
    try:
        # _git_out gives both calls a kill-on-timeout guard (issue #7): a git
        # hung on a dead network mount used to hang the dispatch handler.
        rc, out = await _git_out("-C", base, "rev-parse", "--show-toplevel")
        if rc != 0:
            return base
        top = out.strip() or base
        wt = os.path.expanduser(f"~/.pocket/worktrees/{sid}")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        rc, _ = await _git_out("-C", top, "worktree", "add",
                               "-b", f"pocket/{sid}", wt, "HEAD", timeout=60)
        return wt if rc == 0 and os.path.isdir(wt) else base
    except Exception as e:  # noqa: BLE001
        _log_event("make_worktree_failed", sid=sid, base=base,
                   error=type(e).__name__, error_message=str(e)[:160])
        return base


async def _git_out(*args, timeout: float = 15.0):
    """Run git, return (rc, stdout_str). Kill-on-timeout like _tmux_run."""
    p = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        return 124, ""
    return p.returncode, (out or b"").decode("utf-8", "replace")


async def _worktree_try_remove(sid: str, wt: str, origin: str) -> bool:
    """Remove ONE worktree if it's clean. Returns True when it's gone.

    Dirty trees are kept and logged — someone's uncommitted work lives there,
    and silently deleting a agent's unpushed diff is the one unrecoverable
    failure mode here. Shared by the end-of-dispatch path and the orphan reaper
    so both obey exactly the same rule (issue #7 item 5)."""
    if not wt or not os.path.isdir(wt):
        return False
    try:
        rc, dirty = await _git_out("-C", wt, "status", "--porcelain")
        if rc != 0 or dirty.strip():
            _log_event("worktree_kept", sid=sid, worktree=wt, origin=origin,
                       reason="status-failed" if rc != 0 else "dirty")
            return False
        # `worktree remove` must run from the MAIN repo (git refuses to remove
        # the tree it's currently -C'd into), so resolve the common dir first.
        rc, common = await _git_out("-C", wt, "rev-parse", "--git-common-dir")
        common = common.strip()
        if rc != 0 or not common:
            _log_event("worktree_kept", sid=sid, worktree=wt, origin=origin,
                       reason="no-common-dir")
            return False
        if not os.path.isabs(common):
            common = os.path.abspath(os.path.join(wt, common))
        main_root = os.path.dirname(common)
        rc, _ = await _git_out("-C", main_root, "worktree", "remove", wt, timeout=30)
        if rc == 0:
            _log_event("worktree_removed", sid=sid, worktree=wt, origin=origin)
            return True
        _log_event("worktree_remove_failed", sid=sid, worktree=wt, origin=origin, rc=rc)
        return False
    except Exception as e:  # noqa: BLE001
        _log_event("worktree_cleanup_error", sid=sid, worktree=wt, origin=origin,
                   error=type(e).__name__, error_message=str(e)[:160])
        return False


async def _cleanup_worktree(sid: str, sub: dict):
    """After an isolated sub finishes: reclaim its worktree if it's clean."""
    wt = sub.get("worktree")
    if not wt:
        return
    if await _worktree_try_remove(sid, wt, "dispatch_end"):
        sub["worktree"] = None
        if sub.get("base_cwd"):
            sub["cwd"] = sub["base_cwd"]   # follow-ups run in the main tree


_WORKTREE_ROOT = os.path.expanduser("~/.pocket/worktrees")
# Grace period before a tree with no live sub-session counts as an orphan —
# long enough that a dispatch which just created its tree (but hasn't been
# registered in SUBSESSIONS yet) can never be reaped out from under itself.
_WORKTREE_ORPHAN_MIN_AGE_SECS = float(
    os.environ.get("BRIDGE_WORKTREE_ORPHAN_AGE", "3600"))


async def _reap_orphan_worktrees() -> int:
    """Sweep ~/.pocket/worktrees for trees whose sub-session no longer exists.

    `_cleanup_worktree` only runs when `_stream_agent` reaches its `finally`.
    A crash / `launchctl kickstart` mid-dispatch skips it entirely and leaks the
    tree forever — one per killed dispatch, plus SUBSESSIONS is in-memory so
    EVERY tree is orphaned by a restart. That's the "worktrees 目錄無限成長"
    half of item 5 that the end-of-dispatch hook structurally cannot cover.
    Same clean-only rule as the normal path: dirty trees are kept.
    """
    try:
        names = os.listdir(_WORKTREE_ROOT)
    except OSError:
        return 0            # nothing dispatched in isolate mode yet
    now = time.time()
    reaped = 0
    for name in names:
        wt = os.path.join(_WORKTREE_ROOT, name)
        if not os.path.isdir(wt):
            continue
        sub = SUBSESSIONS.get(name)
        if sub is not None and sub.get("status") == "running":
            continue        # live dispatch owns it
        try:
            if now - os.path.getmtime(wt) < _WORKTREE_ORPHAN_MIN_AGE_SECS:
                continue    # too fresh to judge
        except OSError:
            continue
        if await _worktree_try_remove(name, wt, "orphan_reap"):
            reaped += 1
            if sub is not None:
                sub["worktree"] = None
                if sub.get("base_cwd"):
                    sub["cwd"] = sub["base_cwd"]
    if reaped:
        _log_event("worktree_orphans_reaped", count=reaped, root=_WORKTREE_ROOT)
    return reaped


def _fmt_item(kind, val):
    """Format one transcript item (text/tool/result/perm) → SSE content string."""
    if kind == "text":
        return val
    if kind == "tool_start":
        name = val.get("name", "tool")
        cmd = (val.get("cmd") or "").strip().splitlines()
        cmd1 = (cmd[0] if cmd else "")[:TOOL_CMD_MAX]
        return f"\n› 🔧 **{name}**" + (f" `{cmd1}`" if cmd1 else "") + "\n"
    if kind == "tool_result":
        res = (val.get("text") or "").strip()
        if not res:
            return None
        short = res[:900]
        more = "\n…(截斷)" if len(res) > 900 else ""
        return f"<details><summary>↳ 結果</summary>\n\n```\n{short}{more}\n```\n\n</details>\n"
    if kind == "perm":
        return f"\n› 🔐 自動允許 **{val}**\n"
    return None


# ───────────────────────── 儀表板聚合(GET /app/v1/dashboard)──────────────
# 首屏「指揮艙」一次拉全部:weather / oracle / approvals / sessions / health。
# 全部唯讀;weather 是唯一外呼(open-meteo),bridge 側快取 30 分鐘,app 進
# 前景/切分頁才打,不新增輪詢迴圈。

# 水鏡卦象:oracle-engine 每日產物(唯讀)。date 非今日或 status 非 ok →
# oracle 給 null,app 顯示「今日卦象尚未產生」。
ORACLE_STATE_FILE = os.environ.get(
    "POCKET_ORACLE_FILE",
    os.path.expanduser("~/apps/oracle-engine/state/daily-latest.json"))

# 未帶天氣參數時的預設城市組 — 保留機主時期的雙城,純為**舊 app 相容**:
# 已更新的 app 一律自己算出所在城市用 wx_lat/wx_lon/wx_label 傳上來(見
# `_dashboard_weather_cities`),不再吃這組預設。
_DASH_WEATHER_CITIES = (
    ("taipei", "台北", 25.03, 121.56, "Asia/Taipei"),
    ("bangkok", "曼谷", 13.75, 100.50, "Asia/Bangkok"),
)
_WEATHER_TTL = 1800.0                      # 30 min(拍板)
# 城市組 → {"at": monotonic, "data": payload}。城市由 app 決定後,快取不能再
# 是單一格子(否則 A 使用者的台北會被 B 使用者的曼谷洗掉),鍵含座標。
_WEATHER_CACHE: dict = {}
# 鍵是 client 可控的(座標任意),設上限防止無限長大;滿了先丟最舊的。
_WEATHER_CACHE_MAX = 64
# wx_label 是 client 傳的顯示字串,截斷 + 去控制字元後才回進 payload。
_WX_LABEL_MAX = 32

# 四個 hermes gateway 的 launchd label ↔ persona(健康卡)。
_DASH_GATEWAYS = (
    ("ai.hermes.gateway", "yuanfang"),
    ("ai.hermes.gateway-fliper", "pantianqing"),
    ("ai.hermes.gateway-shuijing", "shuijing"),
    ("ai.hermes.gateway-xcash", "xcash"),
)


def _weather_cache_key(cities) -> tuple:
    """快取鍵 = 城市座標組(取到小數第 2 位,≈1km,足以合併同城市的抖動)。
    名稱不入鍵——同一座標換個顯示字串沒必要重打 open-meteo。"""
    return tuple(sorted((round(lat, 2), round(lon, 2)) for _, _, lat, lon, _ in cities))


async def _weather_fetch_cities(cities=None) -> list:
    """open-meteo 當日:城市併發,高低溫 + 最大降雨機率。任一失敗整批視為
    失敗(丟例外),由呼叫端決定回舊快取或 null。`cities` 省略 = 預設城市組。"""
    import httpx

    cities = _DASH_WEATHER_CITIES if cities is None else tuple(cities)

    async def one(client, cid, name, lat, lon, tz):
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    # precip_prob 改用日「均」而非日「最高」:雨季(台北/曼谷八月)
                    # 幾乎每天都有某一小時飆到 90-100%,用 _max 就天天顯示 90-100%、
                    # 誇張且無資訊量(2026-08-07 善彰回報)。_mean 是更代表「整天下雨
                    # 機會」的單一數字。另附 precipitation_sum(當日雨量 mm)供 app
                    # 日後顯示「其實只是短陣雨」用。
                    "daily": "temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_mean,precipitation_sum",
                    "timezone": tz, "forecast_days": 1})
        r.raise_for_status()
        d = (r.json() or {}).get("daily") or {}
        return {"id": cid, "name": name,
                "temp_max": (d.get("temperature_2m_max") or [None])[0],
                "temp_min": (d.get("temperature_2m_min") or [None])[0],
                "precip_prob": (d.get("precipitation_probability_mean") or [None])[0],
                "precip_mm": (d.get("precipitation_sum") or [None])[0]}

    async with httpx.AsyncClient(timeout=8) as client:
        return list(await asyncio.gather(
            *[one(client, *c) for c in cities]))


async def _dashboard_weather(cities=None):
    """快取 30 分鐘(每個城市組各自一格);過期才外呼。外呼失敗回同組舊資料
    (stale 總比空白好),該組從未成功過則回 None(app 顯示「—」)。失敗不寫
    快取,下次再試。`cities` 省略 = 預設城市組;空序列 = 使用者關掉天氣 →
    直接 None,不外呼。"""
    cities = _DASH_WEATHER_CITIES if cities is None else tuple(cities)
    if not cities:
        return None
    key = _weather_cache_key(cities)
    now = time.monotonic()
    hit = _WEATHER_CACHE.get(key)
    if hit and hit["data"] is not None and now - hit["at"] < _WEATHER_TTL:
        return hit["data"]
    try:
        fetched = await _weather_fetch_cities(cities)
    except Exception as e:  # noqa: BLE001
        _log_event("dashboard_weather_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
        return hit["data"] if hit else None
    # 寫入前先騰位:鍵由 client 控制,不設限會被任意座標灌爆。
    while len(_WEATHER_CACHE) >= _WEATHER_CACHE_MAX and key not in _WEATHER_CACHE:
        _WEATHER_CACHE.pop(min(_WEATHER_CACHE, key=lambda k: _WEATHER_CACHE[k]["at"]))
    _WEATHER_CACHE[key] = {"at": now,
                           "data": {"fetched_at": time.time(), "cities": fetched}}
    return _WEATHER_CACHE[key]["data"]


def _dashboard_weather_cities(request):
    """把 `/app/v1/dashboard` 的天氣 query 參數解析成城市組。

    城市由 **app** 決定(裝置時區推斷或使用者在設定裡手選),bridge 只照做 —
    這樣同一個 bridge 服務不同地點的人都對。三種結果:

      * `?wx=off`                      → `()`,關掉天氣(不外呼、payload 給 null)
      * `?wx_lat=&wx_lon=[&wx_label=&wx_tz=]` → 單一城市
      * 沒帶(或帶了但不合法)         → `_DASH_WEATHER_CITIES` 雙城預設,
                                        舊版 app 行為完全不變

    `wx_tz` 省略時交給 open-meteo `timezone=auto`(依座標判時區),因此 app
    只需要送座標就會拿到當地日界的當日高低溫。
    """
    q = getattr(request, "query_params", None) or {}
    if str(q.get("wx") or "").strip().lower() in ("off", "0", "none", "false"):
        return ()
    raw_lat, raw_lon = q.get("wx_lat"), q.get("wx_lon")
    if raw_lat in (None, "") or raw_lon in (None, ""):
        return _DASH_WEATHER_CITIES
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except (TypeError, ValueError):
        lat = lon = None
    if (lat is None or not (-90.0 <= lat <= 90.0)
            or not (-180.0 <= lon <= 180.0)
            or lat != lat or lon != lon):          # NaN 自己不等於自己
        _log_event("dashboard_weather_bad_coords",
                   lat=str(raw_lat)[:24], lon=str(raw_lon)[:24])
        return _DASH_WEATHER_CITIES
    # 顯示名由 app 給(它有在地語言的城市表);沒給就退回座標字串。
    label = "".join(ch for ch in str(q.get("wx_label") or "")
                    if ch.isprintable()).strip()[:_WX_LABEL_MAX]
    if not label:
        label = f"{lat:.2f},{lon:.2f}"
    tz = str(q.get("wx_tz") or "").strip()
    # 只放行 IANA 形狀的字串,其餘一律 auto(不把 client 字串直接轉給外部 API)。
    if not tz or not re.fullmatch(r"[A-Za-z][A-Za-z0-9+_-]*(/[A-Za-z0-9+._-]+){0,2}", tz):
        tz = "auto"
    return ((f"{lat:.2f},{lon:.2f}", label, lat, lon, tz),)


def _dashboard_oracle():
    """讀 oracle-engine 每日卦象(唯讀)。缺檔/壞檔/date 非今日/status 非 ok
    → None。只回 app 要渲染的欄位,不外流 personal_context/seed。"""
    try:
        with open(ORACLE_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_dashboard_oracle", _exc, expected=True)
        return None
    if str(d.get("status") or "") != "ok":
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(str(d.get("timezone") or "Asia/Taipei"))
        today = datetime.now(tz).strftime("%Y-%m-%d")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_dashboard_oracle#2", _exc, expected=True)
        today = datetime.now().strftime("%Y-%m-%d")
    if str(d.get("date") or "") != today:
        return None
    hx = d.get("hexagrams") or {}
    itp = d.get("interpretation") or {}

    def hexa(h):
        h = h or {}
        return {"number": h.get("number"), "name": h.get("name"),
                "theme": h.get("theme")}

    def hex_name(h):
        h = h or {}
        num = h.get("number")
        name = h.get("name") or ""
        return f"{num}.{name}" if num is not None else name

    def trigram_name(t):
        t = t or {}
        name = t.get("name") or ""
        image = t.get("image") or ""
        return f"{name}{image}" if name or image else ""

    def classical_line_label(ln):
        pos = ln.get("position")
        yin_yang = ln.get("yin_yang")
        digit = "九" if yin_yang == "yang" or ln.get("value") in (7, 9) else "六"
        if pos == 1:
            return f"初{digit}"
        if pos == 6:
            return f"上{digit}"
        numerals = {2: "二", 3: "三", 4: "四", 5: "五"}
        return f"{digit}{numerals.get(pos, pos)}"

    moving = [ln for ln in (d.get("lines") or []) if ln.get("changing")]
    primary = hx.get("primary") or {}
    relating = hx.get("relating") or {}
    upper = primary.get("upper") or {}
    lower = primary.get("lower") or {}
    moving_classical = [classical_line_label(ln) for ln in moving]
    moving_text = "、".join(moving_classical) if moving_classical else "無"
    upper_text = trigram_name(upper)
    lower_text = trigram_name(lower)
    hexagram_line = (
        f"主卦：{hex_name(primary)}"
        f"{f'（上{upper_text} / 下{lower_text}）' if upper_text or lower_text else ''}"
        f" / 變卦：{hex_name(relating)} / 動爻：{moving_text}"
    )
    lower_image = lower.get("image") or lower_text
    upper_image = upper.get("image") or upper_text
    lower_keywords = "、".join((lower.get("keywords") or [])[:2])
    upper_keywords = "、".join((upper.get("keywords") or [])[:2])
    primary_theme = primary.get("theme") or itp.get("summary") or ""
    relating_theme = relating.get("theme") or ""
    trigram_sentence = (
        f"{primary.get('name') or '本'}卦是「{lower_image}在{upper_image}下」"
        if lower_image and upper_image else f"{primary.get('name') or '本'}卦"
    )
    role_sentence = ""
    if lower_keywords or upper_keywords:
        role_sentence = (
            f"下卦主{lower_keywords or lower_image}，"
            f"上卦主{upper_keywords or upper_image}。"
        )
    turn_sentence = (
        f"{moving_text}動，局勢由{primary.get('name') or '主卦'}轉入"
        f"{relating.get('name') or '變卦'}；{relating_theme}"
        if moving_classical else
        f"無動爻，今日重點是守住{primary.get('name') or '主卦'}本義；{primary_theme}"
    )
    if (relating.get("name") or "") == "蹇" and moving_classical:
        turn_sentence += " 蹇不是失敗，是提醒改走法、求援、設界線。"
    hexagram_reading = (
        f"{trigram_sentence}：{role_sentence}{primary_theme} {turn_sentence}"
    ).strip()
    return {"date": d.get("date"),
            "summary": itp.get("summary"),
            "hexagram_line": hexagram_line,
            "hexagram_reading": hexagram_reading,
            "attack_or_defend": itp.get("attack_or_defend"),
            "advice": itp.get("advice"),
            "biggest_risk": itp.get("biggest_risk"),
            "one_thing_to_push": itp.get("one_thing_to_push"),
            "primary": hexa(hx.get("primary")),
            "relating": hexa(hx.get("relating")),
            "changing_lines": d.get("changing_lines") or [],
            "changing_labels": [f"第{ln.get('position')}爻 {ln.get('label')}"
                                for ln in moving]}


def _dashboard_approvals():
    """pending 數 + 前 5 筆統一物件(重用 approvals 表與 _approval_row)。"""
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        _approvals_expire(con)
        con.commit()
        pending = con.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
        rows = con.execute(
            f"SELECT {_APPROVAL_COLS} FROM approvals WHERE status='pending' "
            "ORDER BY created_at DESC LIMIT 5").fetchall()
        con.close()
        return {"pending": pending, "items": [_approval_row(r) for r in rows]}
    finally:
        con.close()


async def _dashboard_sessions():
    """cc/cx/persona 各 {working, idle} — 重用既有列表邏輯的最便宜組合:
    cc 走 _cc_sessions()(tmux 快取 capture),persona 走 PERSONAS +
    _hermes_pending_by_session(),codex 走 thread/list(短 timeout,掛了標
    degraded 不拖垮整包)。working = running/waiting_approval 同 v2 語意。"""
    degraded = []
    cc_w = cc_i = 0
    try:
        for s in await _cc_sessions():
            if s.get("status") == "running" and (s.get("busy") or s.get("awaiting")):
                cc_w += 1
            else:
                cc_i += 1
    except Exception as e:  # noqa: BLE001
        _log_event("dashboard_cc_failed", error=str(e)[:160])
        degraded.append("claude_code")
    hp = _hermes_pending_by_session()
    p_w = sum(1 for mid in PERSONAS if f"hermes:{mid}" in hp)
    p_i = len(PERSONAS) - p_w
    cx_w = cx_i = 0
    try:
        res = await CODEX_APP.call(
            "thread/list", {"limit": 20, "archived": False,
                            "sortKey": "updated_at", "sortDirection": "desc",
                            "useStateDbOnly": False}, timeout=5.0)
        for t in (res or {}).get("data", [])[:20]:
            s = _codex_session_summary(t)
            tid = s.get("thread_id") or s.get("id")
            active = bool(s.get("activeTurn")) or s.get("status") in ("active", "running")
            if CODEX_APP.pending_approval_for_thread(tid) or active:
                cx_w += 1
            else:
                cx_i += 1
    except Exception as e:  # noqa: BLE001
        _log_event("dashboard_codex_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
        degraded.append("codex")
    out = {"cc": {"working": cc_w, "idle": cc_i},
           "cx": {"working": cx_w, "idle": cx_i},
           "persona": {"working": p_w, "idle": p_i},
           "degraded": degraded}
    # S4:openclaw 未配置 → 鍵缺席(app 端 optional decode 自動不畫那一行);
    # 配置了但掛 → 鍵缺席 + degraded 標記,同 codex 精神。
    if OPENCLAW.configured():
        try:
            res = await OPENCLAW.call("sessions.list", {"limit": 20}, timeout=5.0)
            oc_w = oc_i = 0
            for row in (res or {}).get("sessions", [])[:20]:
                if openclaw_provider.session_status(row) == "running":
                    oc_w += 1
                else:
                    oc_i += 1
            out["openclaw"] = {"working": oc_w, "idle": oc_i}
        except Exception as e:  # noqa: BLE001
            _log_event("dashboard_openclaw_failed", error=type(e).__name__,
                       error_message=str(e)[:160])
            degraded.append("openclaw")
    return out


async def _dashboard_gateways() -> list:
    """四個 hermes gateway 活著與否 — 一次 `launchctl list` 輕量判定
    (pid 欄非 '-' = 活著);launchctl 不可用時 alive 全 None(app 顯示未知)。"""
    alive_by_label = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "launchctl", "list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        alive_by_label = {}
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                alive_by_label[parts[2].strip()] = parts[0].strip() != "-"
    except Exception as e:  # noqa: BLE001
        _log_event("dashboard_launchctl_failed", error=str(e)[:160])
    return [{"label": label, "persona": persona,
             "alive": (alive_by_label.get(label, False)
                       if alive_by_label is not None else None)}
            for label, persona in _DASH_GATEWAYS]


@app.get("/app/v1/dashboard")
async def app_dashboard(request: Request):
    """儀表板聚合端點:一次回 weather/oracle/approvals/sessions/health。
    Bearer 驗證同其他 /app/v1/*;全唯讀、可併發的子項一起 gather。

    天氣城市由 app 用 `wx_lat`/`wx_lon`/`wx_label`(選配 `wx_tz`)指定,
    `wx=off` 關閉;未帶參數維持雙城預設(舊 app 相容)。詳見
    `_dashboard_weather_cities`。"""
    _check_auth(request)
    weather, sessions, gateways, agents = await asyncio.gather(
        _dashboard_weather(_dashboard_weather_cities(request)),
        _dashboard_sessions(), _dashboard_gateways(), _agent_auth_status())
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "weather": weather,
            "oracle": _dashboard_oracle(),
            "approvals": _dashboard_approvals(),
            "sessions": sessions,
            "agents": agents,
            "health": {"gateways": gateways,
                       "apns_configured": apns_configured(),
                       "devices": len(_devices())}}


@app.get("/app/v1/agents/auth")
async def app_agents_auth(request: Request):
    """CC/CX 這台 Mac 的登入狀態(app gate CC/CX 頁籤用)。
    回 {"claude": {"installed", "logged_in", "account"}, "codex": {…}}。
    TTL 60s 快取;純唯讀查狀態,不碰憑證。"""
    _check_auth(request)
    return await _agent_auth_status()


def _host_capabilities() -> dict:
    """這台龍蝦主機支援哪些 provider — 給 app 依能力顯示/隱藏功能,而不是
    讓不支援的功能看起來像壞掉(Windows/精簡安裝沒有 tmux/CC 時尤其重要)。
    全部只做便宜的存在性檢查,不 spawn 任何行程。"""
    has_tmux = bool(shutil.which(TMUX_BIN) or shutil.which("tmux"))
    return {
        "cc": has_tmux and os.path.exists(CLAUDE_BIN),
        "cx": os.path.exists(_resolve_codex_bin()),
        "hermes": os.path.exists(HERMES_BIN),
        "openclaw": OPENCLAW.configured(),
        "terminal": POCKET_TERMINAL_ENABLED,
    }


@app.get("/health")
async def health():
    # turns_in_flight:給安全重啟腳本(scripts/bridge-safe-restart.sh)看的 ——
    # 重啟會無聲殺掉進行中人格回合(2026-08-04 實害:連環部署殺了善彰的模型
    # 測試回合),腳本等這個歸零才 kickstart。
    inflight = sum(
        1 for entry in list(_APP_TURN_INFLIGHT.values())
        if entry.get("task") is not None and not entry["task"].done())
    return {"ok": True, "personas": list(PERSONAS),
            "subsessions": len(SUBSESSIONS),
            "bg_tasks": len(_BG_TASKS),
            "turns_in_flight": inflight,
            "capabilities": _host_capabilities()}


# ───────────────────────── log rotation (issue #7 item 6) ──────────────────
# launchd redirects stdout/stderr to bridge.out.log / bridge.err.log and never
# rotates them, so a long-running bridge grows them toward GBs.
#
# 為什麼是 in-process 而不是 newsyslog(見 docs/LOG_ROTATION.md 的完整評估):
# launchd 在 spawn 時就把那兩個檔開好、fd 交給行程,整個生命週期不會重開。
# newsyslog 預設的 rotate 是 rename → launchd 的 fd 還綁在被改名的 inode 上,
# 新的 bridge.out.log 會永遠是 0 byte(等於靜默停掉所有 log),而 newsyslog
# 沒有 copytruncate。要修就得讓行程收訊號重開 fd,那是更大的改動。
# copy-then-truncate 在這裡安全,因為 launchd 用 O_APPEND 開檔
# (`lsof +fg` 實測 FILE-FLAG 有 AP)—— truncate 後下一次寫入照樣落在新 EOF,
# 不會產生 sparse 空洞。
_LOG_ROTATE_MAX_BYTES = int(os.environ.get("BRIDGE_LOG_MAX_BYTES", 32 * 1024 * 1024))
# 保留幾代舊檔(.1 最新)。32MB × (1 + 3) ≈ 128MB/檔 的硬上限。
_LOG_ROTATE_KEEP = int(os.environ.get("BRIDGE_LOG_KEEP", "3"))
_LOG_ROTATE_CHECK_SECS = float(os.environ.get("BRIDGE_LOG_CHECK_SECS", "900"))


def _rotate_log_file(path: str) -> None:
    try:
        if os.path.getsize(path) < _LOG_ROTATE_MAX_BYTES:
            return
    except OSError:
        return
    try:
        import shutil
        # 先把舊代往後推 .2→.3、.1→.2,最舊的那代掉出視窗被覆蓋。
        for gen in range(_LOG_ROTATE_KEEP - 1, 0, -1):
            src, dst = f"{path}.{gen}", f"{path}.{gen + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        shutil.copyfile(path, path + ".1")
        os.truncate(path, 0)
        _log_event("log_rotated", path=path, keep=_LOG_ROTATE_KEEP)
    except Exception as e:  # noqa: BLE001
        _log_event("log_rotate_failed", path=path,
                   error=type(e).__name__, error_message=str(e)[:160])


async def _housekeeping_loop():
    """One periodic janitor for the things that grow without bound:
    log files (item 6) and leaked isolate worktrees (item 5)."""
    base = os.path.dirname(os.path.abspath(__file__))
    logs = [os.path.join(base, "bridge.out.log"),
            os.path.join(base, "bridge.err.log")]
    extra = os.environ.get("BRIDGE_LOG_ROTATE_PATHS", "")
    logs.extend(p for p in (s.strip() for s in extra.split(":")) if p)
    while True:
        for p in logs:
            if os.path.exists(p):
                _rotate_log_file(p)
        try:
            await _reap_orphan_worktrees()
        except Exception as _exc:  # noqa: BLE001
            # The janitor must never die — a dead loop silently stops rotating
            # logs too, which is the failure this issue exists to prevent.
            _log_exc("_housekeeping_loop.reap", _exc, expected=True)
        await asyncio.sleep(_LOG_ROTATE_CHECK_SECS)


# ═════════════ Agent Registry 治理層(藍圖 AGENT_INTEROP §3,戶政系統)═════════════
# 善彰的痛:「子程序不知道怎麼管理,常跑一堆出來,session 管理混亂」。
# 這一段給每個 bridge 創建的 session 出生登記(purpose/class/parent)、
# 生命週期(active⇄idle→archived)、配額(藍圖 §3.3)與收屍人(reaper)。
# 資料面在 agent_registry.py(獨立 sqlite,絕不碰 canonical.db/state.db);
# 這裡是 provider 接線:busy 信號、SSE 訂閱數、teardown 與 API。
# 治理 opt-in:只管「經創建 hook 登記」的 session;既有/旁路 session 在視圖
# 標 registered:false,reaper 永遠不碰(活水道零風險)。

REGISTRY = agent_registry.AgentRegistry(
    os.environ.get("POCKET_REGISTRY_DB")
    or os.path.join(_POCKET_DIR, "agent-registry.db"))
_REGISTRY_SWEEP_SECS = float(os.environ.get("REGISTRY_SWEEP_SECS", "600"))


def _registry_reaper_enabled() -> bool:
    """Destructive teardown 開關(預設關)。記帳與 API 恆開;真正殺東西
    (ccsess archive/tmux kill、codex archive、worktree remove)只在
    REGISTRY_REAPER=1 才做。呼叫時讀 env,不快取——測試/運維可即時切。"""
    return os.environ.get("REGISTRY_REAPER", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _registry_call_safe(method: str, *args) -> None:
    """熱路徑記帳(touch/mark_done/set_worktree):registry 故障絕不影響
    本業——輸入照送、派工照跑,只留痕。"""
    try:
        getattr(REGISTRY, method)(*args)
    except Exception as _exc:  # noqa: BLE001
        _log_exc(f"_registry_call_safe.{method}", _exc, expected=True)


def _registry_class_of(body: dict, default_cls: str = "task") -> str:
    cls = str(body.get("class") or body.get("session_class") or "").strip().lower()
    return cls if cls in agent_registry.CLASSES else default_cls


def _registry_spawn_fields(body: dict, default_cls: str = "task"):
    """創建 hook 共用:(parent, class, purpose)。purpose 缺 → 預設
    「未註明用途」+ 警告 log(藍圖 §3.1 拒絕空值的軟著陸——不破壞現有
    client,但 log 催促帶上)。parent 傳裸 persona id 時正規化成 hermes:{id}。"""
    parent = str(body.get("parent_session") or body.get("parent") or "").strip() or None
    if parent and ":" not in parent and not parent.startswith("sub-") \
            and parent in PERSONAS:
        parent = f"hermes:{parent}"
    cls = _registry_class_of(body, default_cls)
    purpose = str(body.get("purpose") or "").strip()
    if not purpose:
        _log_event("registry_purpose_missing", cls=cls, parent=parent or "")
        purpose = agent_registry.DEFAULT_PURPOSE
    _registry_validate_parent(parent)
    return parent, cls, purpose


def _registry_validate_parent(parent: str | None) -> None:
    """App 明給 parent 時的驗證(addendum 1):parent 必須是 registry 戶口或
    可解析的 live session。旁路 live session(如既有 CC lane)→ 現場補一筆
    registered=False 記帳戶口,家譜連得起來、reaper 永不碰;完全未知 → 400。
    沒給 parent = 今日行為,零影響。"""
    if not parent or REGISTRY.get(parent) is not None:
        return
    known = parent in SUBSESSIONS
    if not known:
        try:
            _v2_card_source(parent)
            known = True
        except Exception:  # noqa: BLE001  (HTTPException/BridgeError 皆=未知)
            known = False
    if not known:
        raise http_err(400, "REGISTRY_BAD_PARENT",
                       f"parent 不是已知 session:{parent}")
    provider = parent.split(":", 1)[0] if ":" in parent else "dispatch"
    try:
        REGISTRY.register(parent, provider=provider,
                          name=parent.split(":", 1)[-1],
                          purpose="(旁路 session,補記帳供家譜連結)",
                          cls="task", registered=False, enforce_quota=False)
        _log_event("registry_parent_backfilled", parent=parent)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_validate_parent", _exc, expected=True)


def _registry_precheck_or_429(parent: str | None, cls: str) -> None:
    """配額前檢(藍圖 §3.3):超額就地拒(429 + 人話原因),不排隊。
    registry 自身故障不擋 spawn(治理層壞了不能把本業拖下水)。"""
    try:
        REGISTRY.precheck(parent, cls)
    except agent_registry.QuotaExceeded as e:
        _log_event("registry_quota_rejected", parent=parent or "", cls=cls,
                   reason=e.reason)
        raise http_err(429, "REGISTRY_QUOTA", e.reason)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_precheck_or_429", _exc, expected=True)


def _registry_register(sid: str, *, provider: str, name: str = "",
                       purpose: str = "", cls: str = "task",
                       parent: str | None = None, meta: dict | None = None):
    """出生登記(spawn 成功後補戶口;配額已在 precheck 把關)。失敗只留痕。"""
    try:
        row = REGISTRY.register(sid, provider=provider, name=name,
                                purpose=purpose, cls=cls, parent=parent,
                                meta=meta, enforce_quota=False)
        _log_event("registry_registered", session=sid, provider=provider,
                   cls=row.get("class"), parent=parent or "")
        return row
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_register", _exc, expected=True, session=sid)
        return None


def _registry_ensure_personas() -> None:
    """hermes 常駐人格 = 白名單 persistent,自動落籍(藍圖 §3.1:persistent
    永不自動收)。register 冪等,重啟/人格 CRUD 後重跑無害。"""
    for mid, (disp, _home) in PERSONAS.items():
        try:
            REGISTRY.register(f"hermes:{mid}", provider="hermes", name=disp,
                              purpose=f"常駐人格:{disp}", cls="persistent",
                              enforce_quota=False)
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_registry_ensure_personas", _exc, expected=True)


# ── provider 接線:busy 信號 / SSE 訂閱數 / 卡片流 ───────────────────────

def _registry_provider_ref(sid: str) -> tuple:
    """registry id → (kind, ref):cc=ccsess 名、cx=thread id、hp=persona、
    dlg=delegation id(provider 座標在 meta)、sub=dispatch 子行程。"""
    if sid.startswith("claude_code:"):
        return ("cc", sid.split(":", 1)[1])
    if sid.startswith("codex:"):
        return ("cx", sid.split(":", 1)[1])
    if sid.startswith("hermes:"):
        return ("hp", sid.split(":", 1)[1])
    if sid.startswith("delegation:"):
        return ("dlg", sid.split(":", 1)[1])
    if sid.startswith("sub-"):
        return ("sub", sid)
    return ("?", sid)


async def _registry_is_busy(sid: str) -> bool:
    """provider 現成 busy 信號(藍圖 §3.2)。last_active 只在 input 時記帳,
    長 turn 進行中沒有新 input 會被誤判 idle——收屍前用這裡雙重確認。
    探測失敗一律當忙:收屍寧可保守,晚收十分鐘沒事,錯殺不可回復。"""
    kind, ref = _registry_provider_ref(sid)
    try:
        if kind == "cc":
            st, _ = await _v2_cc_state(ref)
            return st in ("running", "waiting_approval")
        if kind == "cx":
            d = _CX_CARD_DIGESTS.get(ref)
            return bool(d and getattr(d.store, "turn_id", ""))
        if kind == "sub":
            sub = SUBSESSIONS.get(ref)
            return bool(sub and sub.get("status") == "running")
        if kind == "hp":
            s = POOL._sessions.get(ref)   # 只窺不生:不為測 busy 冷啟 ACP
            return bool(s and s.is_busy())
        if kind == "dlg":
            meta = (REGISTRY.get(sid) or {}).get("meta") or {}
            if meta.get("cc_session_name"):
                st, _ = await _v2_cc_state(meta["cc_session_name"])
                return st in ("running", "waiting_approval")
            d = _CX_CARD_DIGESTS.get(meta.get("codex_thread_id") or "")
            return bool(d and getattr(d.store, "turn_id", ""))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_is_busy", _exc, expected=True)
        return True
    return False


def _registry_card_store(sid: str):
    """已存在的卡片 store(絕不為了發卡新建;沒人看過的 session 不建 store
    ——同 _cc_cards_feed_approval 的原則)。"""
    kind, ref = _registry_provider_ref(sid)
    try:
        if kind == "cc":
            return _CC_CARD_STORES.get(ref)
        if kind == "cx":
            d = _CX_CARD_DIGESTS.get(ref)
            return d.store if d else None
        if kind == "dlg":
            meta = (REGISTRY.get(sid) or {}).get("meta") or {}
            if meta.get("cc_session_name"):
                return _CC_CARD_STORES.get(meta["cc_session_name"])
            d = _CX_CARD_DIGESTS.get(meta.get("codex_thread_id") or "")
            return d.store if d else None
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_card_store", _exc, expected=True)
    return None


def _registry_subscribers(sid: str) -> int:
    store = _registry_card_store(sid)
    return int(getattr(store, "subscribers", 0) or 0) if store else 0


def _registry_emit_reap_warning(row: dict) -> None:
    """收屍前的「⏳ 即將回收」卡(藍圖 §3.2):有 SSE 訂閱者在看 → 發卡
    並跳過本輪(寬限一個 sweep 週期,Pocket 可一鍵續命)。卡 id 對 session
    固定,重複警告只 rev++ 原卡,不洗版。"""
    sid = row["id"]
    store = _registry_card_store(sid)
    if store is None:
        return
    mins = max(1, int(_REGISTRY_SWEEP_SECS // 60))
    txt = (f"⏳ 即將回收:「{row.get('purpose') or sid}」已閒置且壽命(TTL)到期,"
           f"約 {mins} 分鐘後歸檔。要續命請在編隊視圖延長 TTL 或改班 persistent。")
    try:
        digest = hashlib.sha1(sid.encode("utf-8")).hexdigest()[:12]
        store.upsert_card(carddigest.make_card(
            f"card-registry-reap-{digest}", "", "assistant", "text",
            {"text": txt, "origin": "registry.reap_warning"}))
        _log_event("registry_reap_warned", session=sid)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_emit_reap_warning", _exc, expected=True)


async def _registry_teardown(row: dict) -> None:
    """Destructive GC(只在 REGISTRY_REAPER=1 由呼叫端把關):
    CC → `ccsess archive`(存 scrollback + tmux kill,走既有安全路徑);
    codex → app-server archive(既有 fallback 鏈);worktree 只收 spawn 時
    「登記過路徑」的樹(dirty 樹由 _worktree_try_remove 鐵律保護,絕不硬刪);
    hermes persona 永不(persistent 根本進不到這裡)。失敗只留痕不重試。"""
    sid = row["id"]
    if not row.get("registered"):
        return                        # 鐵律 double-guard:未登記絕不動
    kind, ref = _registry_provider_ref(sid)
    meta = row.get("meta") or {}
    try:
        if kind == "cc":
            await _run_ccsess("archive", ref)
        elif kind == "cx":
            await _codex_thread_set_archived(ref, True)
        elif kind == "dlg":
            if meta.get("cc_session_name"):
                await _run_ccsess("archive", meta["cc_session_name"])
            elif meta.get("codex_thread_id"):
                await _codex_thread_set_archived(meta["codex_thread_id"], True)
        elif kind == "hp":
            return                    # persona 永不 teardown
        _log_event("registry_teardown", session=sid, kind=kind)
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_teardown", _exc, expected=True, session=sid)
    wt = row.get("worktree")
    if wt:
        await _worktree_try_remove(sid, wt, "registry_reap")


async def _registry_archive_batch(cands: list, reason: str, *,
                                  respect_subscribers: bool) -> list:
    """歸檔一批候選:busy 雙確認 → 跳過並記活動;有訂閱者(reaper 路徑)→
    警告卡 + 寬限一輪。記帳(標 archived)恆做;teardown 看旗標。"""
    out = []
    for r in cands:
        sid = r["id"]
        if not r.get("registered"):
            continue                  # 鐵律:未登記絕不收
        if await _registry_is_busy(sid):
            _registry_call_safe("touch", sid)
            continue
        if respect_subscribers and _registry_subscribers(sid) > 0:
            _registry_emit_reap_warning(r)
            continue
        REGISTRY.archive(sid, reason)
        _log_event("registry_archived", session=sid, reason=reason,
                   cls=r.get("class"), teardown=_registry_reaper_enabled())
        if _registry_reaper_enabled():
            await _registry_teardown(r)
        out.append(sid)
    return out


async def _registry_reap_once() -> list:
    """一輪收屍:idle 且 TTL 到期的 task/ephemeral → archived(藍圖 §3.2;
    「turn 完成就 done」太激進,不做)。persistent 與未登記結構上進不了候選。"""
    return await _registry_archive_batch(
        REGISTRY.sweep_candidates(require_expired=True),
        reason="reaper", respect_subscribers=True)


async def _registry_reaper_loop():
    """收屍人:每 10 分鐘巡一次(REGISTRY_SWEEP_SECS 可調)。絕不能死
    ——任何例外只留痕,下一輪照巡。"""
    while True:
        await asyncio.sleep(_REGISTRY_SWEEP_SECS)
        try:
            await _registry_reap_once()
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_registry_reaper_loop", _exc, expected=True)


# ── Registry API(APP_BRIDGE_CONTRACT 追加面;app 編隊視圖 §3.4 的資料源)──

def _registry_public_row(row: dict, children: dict, by_id: dict,
                         now: float) -> dict:
    return {
        "id": row["id"], "provider": row["provider"], "name": row["name"],
        "purpose": row["purpose"], "class": row["class"],
        "parent": row.get("parent"),
        "state": REGISTRY.effective_state(row, now),
        "created_ts": row["created_ts"], "last_active_ts": row["last_active_ts"],
        "ttl_secs": row["ttl_secs"],
        "budget": {"max_children": row.get("max_children") or REGISTRY.max_children},
        "registered": bool(row.get("registered")),
        "children": children.get(row["id"], []),
        "expires_ts": REGISTRY.expires_ts(row),
        "orphan": REGISTRY.is_orphan(row, by_id),
    }


def _registry_legacy_row(sid: str, provider: str, name: str) -> dict:
    """既有/旁路 session(沒經創建 hook):看得到、管不到——registered:false,
    reaper 永不碰。欄位補齊成同一形狀,app 端渲染不用分家。"""
    return {"id": sid, "provider": provider, "name": name,
            "purpose": None, "class": None, "parent": None, "state": None,
            "created_ts": None, "last_active_ts": None, "ttl_secs": None,
            "budget": {"max_children": REGISTRY.max_children},
            "registered": False, "children": [], "expires_ts": None,
            "orphan": False}


async def _registry_legacy_rows(known_ids: set) -> list:
    """盤點旁路 session:ccsess 設定檔、dispatch 子行程、codex 可見 threads
    (app-server 掛了就略過,不讓治理視圖跟著掛)。"""
    out = []
    try:
        for name, _workdir, enabled in _cc_conf_rows():
            if enabled != "1":
                continue
            sid = f"claude_code:{name}"
            if sid not in known_ids:
                out.append(_registry_legacy_row(sid, "claude_code", name))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_legacy_rows#cc", _exc, expected=True)
    for sid, sub in list(SUBSESSIONS.items()):
        if sid not in known_ids:
            out.append(_registry_legacy_row(sid, "dispatch",
                                            sub.get("name") or sid))
    try:
        threads = await asyncio.wait_for(_codex_v2_visible_threads(20), 8.0)
        for t in threads:
            s = _codex_session_summary(t)
            tid = s.get("thread_id") or s.get("id") or ""
            sid = f"codex:{tid}"
            if tid and sid not in known_ids:
                out.append(_registry_legacy_row(sid, "codex",
                                                s.get("name") or tid))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_legacy_rows#cx", _exc, expected=True)
    # §2.3:全機發現面掃到、但還沒收編的 session 也要在治理視圖露臉,
    # app 的「未登記」區塊才看得到使用者自己在桌機開的那些(非 ccsess 名單
    # 的 tmux claude、openclaw gateway session…)。掃描掛了不影響上面幾段。
    try:
        seen = set(known_ids) | {r["id"] for r in out}
        payload = _discovery_snapshot_nonblocking() or {}
        for it in payload.get("items", []):
            sid = it.get("id") or ""
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(_registry_legacy_row(sid, it.get("provider") or "",
                                            it.get("name") or sid))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_registry_legacy_rows#discovery", _exc, expected=True)
    return out


@app.get("/app/v2/registry")
async def v2_registry_list(request: Request, include_archived: int = 0):
    """編隊視圖資料源:登記戶(含 children/expires/orphan)+ 旁路 session
    (registered:false)。defaults 給 app 顯示配額/TTL 預設。"""
    _check_auth(request)
    _registry_ensure_personas()
    now = time.time()
    all_rows = REGISTRY.list_rows(include_archived=True)
    by_id = {r["id"]: r for r in all_rows}
    children = REGISTRY.children_ids(
        [r for r in all_rows if r.get("state") != "archived"])
    shown = all_rows if include_archived else \
        [r for r in all_rows if r.get("state") != "archived"]
    sessions = [_registry_public_row(r, children, by_id, now) for r in shown]
    sessions.extend(await _registry_legacy_rows(set(by_id)))
    return {"sessions": sessions,
            "defaults": {"task_ttl": int(REGISTRY.task_ttl),
                         "ephemeral_ttl": int(REGISTRY.ephemeral_ttl),
                         "max_children": REGISTRY.max_children}}


@app.post("/app/v2/registry/sweep")
async def v2_registry_sweep(request: Request):
    """🧹收工鈕(藍圖 §3.4):一鍵歸檔所有 idle 的 task/ephemeral(不等
    TTL——人按了收工就是要收)。active 的不動;未登記的碰不到。
    註:此路由必須宣告在 /app/v2/registry/{sid} 之前,否則 "sweep" 會被
    當成 session id 吃掉。"""
    _check_auth(request)
    archived = await _registry_archive_batch(
        REGISTRY.sweep_candidates(require_expired=False),
        reason="sweep", respect_subscribers=False)
    return {"archived": archived}


# addendum 2:Pocket 父 session 設定頁的「子程序」面板 poll 這裡。busy 是
# provider 現成信號(與 reaper 同一套 _registry_is_busy),但 CC 的判定要
# capture tmux pane —— 為避免 poll 造成 pane capture 風暴,加 3 秒 TTL 快取;
# 面板可接受 ≤3s 的 busy 陳舊度(state/last_active_ts 恆為即時值)。
_REGISTRY_BUSY_CACHE: dict = {}   # sid -> (monotonic_ts, busy)
_REGISTRY_BUSY_TTL = float(os.environ.get("REGISTRY_BUSY_TTL", "3.0"))


async def _registry_busy_cached(sid: str) -> bool:
    ent = _REGISTRY_BUSY_CACHE.get(sid)
    now = time.monotonic()
    if ent is not None and now - ent[0] < _REGISTRY_BUSY_TTL:
        return ent[1]
    busy = await _registry_is_busy(sid)
    _REGISTRY_BUSY_CACHE[sid] = (now, busy)
    if len(_REGISTRY_BUSY_CACHE) > 500:      # poll 對象有限,防禦性封頂即可
        _REGISTRY_BUSY_CACHE.clear()
    return busy


@app.get("/app/v2/registry/{sid}/children")
async def v2_registry_children(sid: str, request: Request):
    """某 parent 的子 session 清單 + 即時 busy(addendum 2)。
    形狀:{children:[{id, provider, name, purpose, class, state, busy,
    last_active_ts}]}。parent 本身不需在 registry(旁路 parent 名下也可能
    有登記過的孩子);archived 的孩子不列。"""
    _check_auth(request)
    now = time.time()
    out = []
    for r in REGISTRY.list_rows():
        if r.get("parent") != sid:
            continue
        out.append({"id": r["id"], "provider": r["provider"],
                    "name": r["name"], "purpose": r["purpose"],
                    "class": r["class"],
                    "state": REGISTRY.effective_state(r, now),
                    "busy": await _registry_busy_cached(r["id"]),
                    "last_active_ts": r["last_active_ts"]})
    return {"children": out}


@app.post("/app/v2/registry/{sid}/archive")
async def v2_registry_archive_one(sid: str, request: Request):
    """手動歸檔單一 session。記帳恆做;destructive teardown 看 REGISTRY_REAPER。"""
    _check_auth(request)
    row = REGISTRY.get(sid)
    if row is None:
        raise http_err(404, "SESSION_NOT_FOUND",
                       "registry 沒有這個 session(旁路 session 不受治理)")
    REGISTRY.archive(sid, "manual")
    _log_event("registry_archived", session=sid, reason="manual",
               cls=row.get("class"), teardown=_registry_reaper_enabled())
    teardown = False
    if _registry_reaper_enabled():
        await _registry_teardown(row)
        teardown = True
    return {"ok": True, "archived": True, "teardown": teardown}


@app.post("/app/v2/registry/{sid}")
async def v2_registry_update(sid: str, request: Request):
    """續命/改班:body {purpose?, class?, ttl_extend_secs?}。
    ttl_extend_secs = 保證「從現在起至少再活 N 秒」。"""
    _check_auth(request)
    body = await _json_body(request)
    if REGISTRY.get(sid) is None:
        raise http_err(404, "SESSION_NOT_FOUND",
                       "registry 沒有這個 session(旁路 session 不受治理)")
    cls = body.get("class")
    if cls is not None and cls not in agent_registry.CLASSES:
        raise HTTPException(status_code=400,
                            detail="class 必須是 persistent|task|ephemeral")
    ttl_extend = body.get("ttl_extend_secs")
    if ttl_extend is not None:
        try:
            ttl_extend = float(ttl_extend)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="ttl_extend_secs 必須是數字(秒)")
    row = REGISTRY.update(sid, purpose=body.get("purpose"), cls=cls,
                          ttl_extend_secs=ttl_extend)
    _log_event("registry_updated", session=sid,
               cls=cls or "", extended=bool(ttl_extend))
    now = time.time()
    all_rows = REGISTRY.list_rows(include_archived=True)
    by_id = {r["id"]: r for r in all_rows}
    children = REGISTRY.children_ids(
        [r for r in all_rows if r.get("state") != "archived"])
    return {"ok": True,
            "session": _registry_public_row(row, children, by_id, now)}


# ═══════ 全機發現與收編(SUBPROCESS_HARNESS_DESIGN_20260811 §2.3)═══════
# 善彰:「那台機器上面的 hermes/openclaw、cc/cx、使用者開的子程序,就是應該
# 收進來,讓他可以看到並且管理。」Pocket 是那台機器的**指揮艙**,不是只管
# 「從 Pocket 開的」——所以掃全機、標 managed/discovered、可一鍵收編。
#
# 安全前提(整段程式碼的紅線):
# - 掃描面**只讀**:tmux list-panes / ps 快照 / thread/list / sessions.list,
#   零 spawn、零 kill、零 send-keys、零 restart。
# - 收編是**純記帳**:cc = 加 ccsess 名單 + 登記戶口;cx/hermes/oc = 純登記。
#   pane 上跑到一半的工作完全不受影響。
# - api key **只回報有無**(has_api_key),值永不進 payload / log / 快取。
# - 任何一路 provider 掛掉只讓那一路變空,絕不讓整個 sweep 500。

_DISCOVERY_TTL = float(os.environ.get("DISCOVERY_TTL", "5.0"))
# 每路 provider 的硬上限。實測這台機器 1100+ 行程時一次 `ps -axo` 就要 2~3 秒,
# cc 那路(tmux + ps 快照 + ps -E)冷啟動會逼近 8 秒 —— 給到 15 秒才不會把
# 「掃得到但太慢」誤報成「這路掛了」。掃描結果有 ~5 秒快取,攤下來不貴。
_DISCOVERY_SLICE_TIMEOUT = float(os.environ.get("DISCOVERY_SLICE_TIMEOUT", "15.0"))
# env 探測(BYO-key 標示)可關:掃 ps -E 會把使用者的 key 讀進行程記憶體
# 一瞬間,雖然只留 bool,仍給一個總開關。
_DISCOVERY_ENV_PROBE = (os.environ.get("DISCOVERY_ENV_PROBE", "1").lower()
                        not in ("0", "false", "no", "off"))
_DISCOVERY_CACHE: dict = {"ts": 0.0, "payload": None, "refreshing": False}
_DISCOVERY_LOCK = asyncio.Lock()


async def _proc_env_has_api_key(pids, names) -> dict:
    """這些 pid 有沒有帶 API key env → `{pid: bool}`。

    ⚠️ 這是全程式唯一會看到使用者 key 明文的地方(macOS 沒有 /proc,只能
    `ps -E`)。契約:blob 只在這個函式的區域變數裡存在一瞬間,函式**只回
    bool**,不回傳、不記錄、不快取任何環境變數內容。
    """
    pids = sorted({int(p) for p in (pids or []) if p})
    if not pids or not _DISCOVERY_ENV_PROBE:
        return {}
    flags: dict = {}
    try:
        p = await asyncio.create_subprocess_exec(
            "/bin/ps", "-E", "-o", "pid=,command=",
            "-p", ",".join(str(x) for x in pids),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), 5.0)
        except asyncio.TimeoutError:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            return {}
        for line in (out or b"").decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            flags[int(parts[0])] = host_discovery.env_blob_has_key(parts[1], names)
        del out                       # blob 到此為止,只有 bool 活下來
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_proc_env_has_api_key", _exc, expected=True)
        return {}
    return flags


async def _discovery_cc_items() -> list:
    """cc:掃全機 tmux pane,比對 `~/.config/ccsess/sessions.conf`。

    `pane_current_command` 實測回的是版本字串(如 `2.1.207`)而不是
    `claude`,**不能**拿來認 agent —— 一律回頭掃 pane pid 的行程樹 cmdline
    (`_ps_snapshot` 已有 5 秒快取,不額外增加 ps 壓力)。
    """
    rc, out, _err = await _tmux_run("list-panes", "-a", "-F",
                                    host_discovery.TMUX_PANE_FORMAT)
    if rc != 0:
        return []                     # tmux server 沒起來 = 這路沒有東西可發現
    panes = host_discovery.parse_tmux_panes(out)
    procs = await _ps_snapshot()
    kids = host_discovery.build_child_map(procs)
    claude_by_pane: dict = {}
    for pane in panes:
        hit = host_discovery.find_agent_proc(
            pane["pane_pid"], procs, kids, host_discovery.is_claude_cmdline)
        if hit:
            claude_by_pane[pane["pane_pid"]] = {"pid": hit[0], "cmdline": hit[1]}
    api_flags = await _proc_env_has_api_key(
        [v["pid"] for v in claude_by_pane.values()],
        host_discovery.API_KEY_ENV_BY_PROVIDER[host_discovery.CC_PROVIDER])
    return host_discovery.cc_discovery_items(
        panes, _cc_conf_rows(), claude_by_pane,
        api_key_by_pid=api_flags if api_flags else None)


async def _discovery_cx_items(registered_ids: set) -> list:
    """cx:`thread/list` 的 sourceKinds 已含 cli/vscode/exec/appServer,
    使用者自己 `codex` 開的本來就看得到,這裡只是把它當「可收編」呈現。"""
    threads = await _codex_v2_visible_threads(40)
    return host_discovery.cx_discovery_items(
        [_codex_session_summary(t) for t in threads], registered_ids)


async def _discovery_openclaw_items(registered_ids: set) -> list:
    if not OPENCLAW.configured():
        return []
    return host_discovery.openclaw_discovery_items(
        await _openclaw_v2_rows(40), registered_ids)


def _discovery_apply_registry(items: list, rows_by_id: dict) -> list:
    """把 registry 戶口疊上發現面:登記過的一律 managed,並帶出
    registry_state / purpose / class 讓 app 直接渲染,不用再打一次 registry。"""
    for it in items:
        row = rows_by_id.get(it["id"])
        if row is None:
            it["registry_state"] = None
            continue
        it["registry_state"] = REGISTRY.effective_state(row)
        it["purpose"] = row.get("purpose")
        it["class"] = row.get("class")
        if row.get("registered") and row.get("state") != "archived":
            it["state"] = host_discovery.STATE_MANAGED
    return items


async def _discovery_sweep(force: bool = False) -> dict:
    """四路發現面的一次掃描(~5 秒 TTL 快取 + single-flight)。

    四路各自 try/except:cx app-server 掛了不該讓 cc 的清單跟著消失。
    `providers` 欄位誠實回報哪一路掛了,app 可以顯示「cx 掃描失敗」而不是
    假裝那邊沒有 session。
    """
    now = time.monotonic()
    cached = _DISCOVERY_CACHE.get("payload")
    if cached is not None and not force and \
            now - float(_DISCOVERY_CACHE.get("ts") or 0) < _DISCOVERY_TTL:
        return cached
    async with _DISCOVERY_LOCK:
        now = time.monotonic()
        cached = _DISCOVERY_CACHE.get("payload")
        if cached is not None and not force and \
                now - float(_DISCOVERY_CACHE.get("ts") or 0) < _DISCOVERY_TTL:
            return cached          # 等鎖期間別人剛掃完,不重複打 provider
        _registry_ensure_personas()
        rows = REGISTRY.list_rows(include_archived=True)
        rows_by_id = {r["id"]: r for r in rows}
        registered_ids = {r["id"] for r in rows
                          if r.get("registered") and r.get("state") != "archived"}

        async def _hermes():
            return host_discovery.hermes_discovery_items(
                [(mid, disp) for mid, (disp, _home) in PERSONAS.items()])

        async def _dispatch():
            return host_discovery.dispatch_discovery_items(dict(SUBSESSIONS))

        slices = [
            (host_discovery.CC_PROVIDER, _discovery_cc_items()),
            (host_discovery.CX_PROVIDER, _discovery_cx_items(registered_ids)),
            (host_discovery.HERMES_PROVIDER, _hermes()),
            (host_discovery.DISPATCH_PROVIDER, _dispatch()),
            (host_discovery.OPENCLAW_PROVIDER,
             _discovery_openclaw_items(registered_ids)),
        ]
        results = await asyncio.gather(
            *(asyncio.wait_for(coro, _DISCOVERY_SLICE_TIMEOUT)
              for _p, coro in slices), return_exceptions=True)
        items, providers = [], {}
        for (prov, _coro), res in zip(slices, results):
            if isinstance(res, BaseException):
                _log_exc(f"_discovery_sweep#{prov}", res, expected=True)
                providers[prov] = {"ok": False, "count": 0,
                                   "error": type(res).__name__}
                continue
            items.extend(res)
            providers[prov] = {"ok": True, "count": len(res)}
        _discovery_apply_registry(items, rows_by_id)
        payload = {"items": items, "providers": providers,
                   "generated_ts": time.time()}
        _DISCOVERY_CACHE["ts"] = time.monotonic()
        _DISCOVERY_CACHE["payload"] = payload
        return payload


def _discovery_snapshot_nonblocking() -> dict | None:
    """治理視圖(`/app/v2/registry`)用的發現面快照:**只吃已經掃好的快取**,
    過期就在背景補掃一次,當下先用舊的(沒有就回 None)。

    /app/v2/registry 是 app 高頻 poll 的端點,不該為了發現面等一趟冷掃
    (實測這台機器冷掃 ~3 秒,ps 一次就 2~3 秒)。發現面在治理視圖裡是
    「附加的未登記區塊」,晚一輪出現完全可以接受。
    """
    payload = _DISCOVERY_CACHE.get("payload")
    age = time.monotonic() - float(_DISCOVERY_CACHE.get("ts") or 0)
    if payload is not None and age < _DISCOVERY_TTL:
        return payload
    if not _DISCOVERY_CACHE.get("refreshing"):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return payload
        _DISCOVERY_CACHE["refreshing"] = True

        async def _refresh():
            try:
                await _discovery_sweep(force=True)
            except Exception as _exc:  # noqa: BLE001
                _log_exc("_discovery_background_refresh", _exc, expected=True)
            finally:
                _DISCOVERY_CACHE["refreshing"] = False

        task = loop.create_task(_refresh())
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    return payload


async def _discovery_find(sid: str) -> dict | None:
    """找一筆發現面項目;快取沒有就強制重掃一次(剛開的 session 也收得到)。"""
    for force in (False, True):
        payload = await _discovery_sweep(force=force)
        for it in payload.get("items", []):
            if it.get("id") == sid:
                return it
    return None


def _discovery_registry_view(row: dict) -> dict:
    now = time.time()
    all_rows = REGISTRY.list_rows(include_archived=True)
    by_id = {r["id"]: r for r in all_rows}
    children = REGISTRY.children_ids(
        [r for r in all_rows if r.get("state") != "archived"])
    return _registry_public_row(row, children, by_id, now)


# ───────────────────── Worker 可見層(設計書 §2.4)────────────────────────
# 「我看不出來你有在運作,你有子程序在執行嗎?」——善彰 2026-08-12。
# registry(§2.3 子程序面板)看的是**獨立 session**;這一層看的是 session
# **內部**派出的短命工人。兩者並存、不互相污染:
#   子程序 = 獨立 session(有 TTL、進家譜、可收編)
#   工人   = session 內部的活(秒/分級、記憶體、靜默期自動過期)
# 旗標預設關 —— 關著時端點 404、且這一層**完全沒有背景成本**(沒有計時器、
# 沒有背景任務,過期是讀寫時惰性算的)。
WORKERS_ENABLED = os.environ.get("WORKERS", "0").strip().lower() in (
    "1", "true", "yes", "on")


def _worker_ttl_from_env() -> float:
    """TTL 解析**絕不能讓 bridge 開不起來**。

    這是 import 期執行的:一個手殘打成 `WORKER_TTL_SECS=5m` 的 env,如果直接
    `float()` 就會在 production 啟動時炸掉整個 bridge —— 而且是在旗標根本沒開
    的情況下炸,完全違背「沒開旗標 = 零風險」。所以壞值一律退回預設。
    `nan` 要特別擋:`now - nan` 恆為 nan,所有比較都是 False,結果是**永遠不過期**
    (面板掛滿假的執行中工人),比炸掉還難查。
    """
    raw = os.environ.get("WORKER_TTL_SECS", "").strip()
    if not raw:
        return 300.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 300.0
    if val != val or val <= 0 or val == float("inf"):   # nan / 非正 / inf
        return 300.0
    return min(val, 86400.0)


WORKER_TTL_SECS = _worker_ttl_from_env()
WORKERS = workers_store.WorkerStore(ttl_secs=WORKER_TTL_SECS)


def _workers_require_enabled() -> None:
    """旗標關 = 端點根本不存在(404),不是 403。合併進 main 的風險為零:
    沒開旗標的 bridge 行為與合併前逐位元相同。"""
    if not WORKERS_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


def _worker_resolve_session(body: dict) -> str:
    """把回報方給的身分解析成 v2 session id(`claude_code:<name>` 等)。

    CC hook 的難處:hook 腳本只拿得到 Claude Code 自己的 `session_id`(uuid),
    它**不知道**自己屬於哪個 ccsess session 名。但 bridge 早就知道 —— 現有的
    `/ccsessions/_hook`(UserPromptSubmit/Stop)一直在把 name↔sid 對應釘進
    `_CC_SID_PINS`/`_CC_SID_CACHE`/`_CC_SID_HISTORY`。這裡直接沿用同一套
    `_cc_name_for_sid` 反查,不必叫 hook 腳本去猜 tmux 名字(猜錯就掛錯 session)。

    優先序:明給的 `session` > `cc_session_id` 反查。都沒有 → 400。
    """
    explicit = str(body.get("session") or "").strip()
    if explicit:
        return explicit
    cc_sid = str(body.get("cc_session_id") or "").strip()
    if cc_sid:
        name = _cc_name_for_sid(cc_sid)
        if name:
            return f"claude_code:{name}"
        # 反查不到(pin 還沒建、或這個 CC 不在 ccsess 名單裡)。不要丟掉這筆
        # 回報 —— 用 sid 自己開一格,至少 /app/v2/workers?session=cc-sid:<uuid>
        # 拿得到,診斷時看得出「有工人但認不出是誰的」。
        return f"cc-sid:{cc_sid}"
    raise HTTPException(status_code=400, detail="session or cc_session_id required")


def _codex_child_worker_rows(threads: list[dict], parent: dict,
                             now: float) -> list[dict]:
    """把 codex 的 child thread 投影成 parent 的工人。

    **不進主清單** —— `_codex_v2_visible_threads` 的過濾維持原樣(那個過濾當初
    是對的:390 筆 guardian thread 會把操作者的 session 整個擠掉)。這裡是另開
    一條唯讀投影,只在被問到某個 parent 的工人時才算。

    親子連線的實測結論(2026-08-12 查 `~/.codex/state_5.sqlite`):
    - codex 有一張 `thread_spawn_edges(parent_thread_id, child_thread_id)`,
      **但這台機器上是空的**(0 列)—— 這個版本沒在寫。有朝一日它開始寫,
      thread 記錄上出現 `parentThreadId` 之類的欄位,下面的 explicit 分支會
      自動優先採用。
    - 現況唯一可用的訊號是 **cwd**:實測 subagent thread 與其 parent 共用 cwd
      (guardian 228 筆在 /Users/xcash、38 筆在 hermes-agent/home,都與該目錄
      下的操作者 thread 對得上)。所以用 cwd 相等當連線,並在 meta 標
      `link:"cwd"` 誠實告訴 app 這是啟發式、不是 provider 給的事實。
    """
    parent_id = str(parent.get("id") or "")
    parent_cwd = os.path.realpath(str(parent.get("cwd") or "")) if parent.get("cwd") else ""
    out = []
    for t in threads:
        tid = str(t.get("id") or "")
        if not tid or tid == parent_id or not _codex_is_child_thread(t):
            continue
        explicit = str(t.get("parentThreadId") or t.get("parent_thread_id") or "")
        if explicit:
            if explicit != parent_id:
                continue
            link = "provider"
        else:
            if not parent_cwd:
                continue
            child_cwd = os.path.realpath(str(t.get("cwd") or "")) if t.get("cwd") else ""
            if child_cwd != parent_cwd:
                continue
            link = "cwd"
        # provider 沒有 done/failed 的分別,只有「還在跑 / 不在跑了」。
        running = bool(CODEX_APP.is_active(tid))
        updated = _codex_ts(t.get("updatedAt"))
        started = _codex_ts(t.get("createdAt")) or updated
        if not updated:
            # 時間戳讀不出來(provider 換格式、給了 dict…)。**絕不能墊 now** ——
            # 那等於宣告「它剛剛才動過」,靜默期就永遠濾不掉它,同 cwd 的幾百筆
            # guardian thread 會整批變成假的工人灌進面板。認不出時間就只信
            # is_active:真的在跑才留,否則當它不存在。
            if not running:
                continue
            updated = started = now
        name = (t.get("name") or t.get("preview") or "").strip()
        out.append({
            "worker_id": f"codex:{tid}",
            "label": (name or tid[:12])[:200],
            "state": "running" if running else "done",
            "parent_worker": None,
            "started_ts": started,
            "updated_ts": updated,
            "meta": {"provider": "codex", "kind": "child_thread",
                     "thread_id": tid, "link": link,
                     "source": _codex_source_label(t.get("source"))},
        })
    return out


def _codex_ts(raw) -> float:
    """codex 的時間戳可能是秒、毫秒、或 ISO 字串。**認不出來回 0**,由呼叫端
    決定怎麼辦(絕不自作主張墊 now —— 見 `_codex_child_worker_rows`)。

    數字部分沿用 `host_discovery._epoch_secs` 的同一條規則(>1e11 視為毫秒),
    不另立第二套判準。
    """
    secs = host_discovery._epoch_secs(raw)
    if secs is not None:
        return secs
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # 沒有時區的 ISO 一律當 UTC。用本地時區解讀會讓 UTC+8 的機器把
            # 「一秒前」讀成「八小時前」,正在跑的工人直接被靜默期濾掉。
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:      # noqa: BLE001
        return 0.0


# thread/list(includeChildren)每次都撈 100 列,而工人面板是**被 app 持續 poll**
# 的端點。同一份清單服務所有 parent,所以快取整份、不分 parent —— 比照隔壁
# `_REGISTRY_BUSY_CACHE`(3s,理由是「避免 poll 造成 pane capture 風暴」)與
# `_CODEX_V2_VISIBLE_CACHE`。工人視圖本來就容忍幾秒陳舊(cwd 連線是啟發式)。
_CODEX_WORKER_THREADS_CACHE: tuple[float, list[dict]] | None = None
_CODEX_WORKER_CACHE_TTL = float(os.environ.get("WORKER_CODEX_CACHE_TTL", "3.0"))


async def _codex_worker_threads() -> list[dict]:
    """撈含 child 的 thread 清單(帶短快取)。

    **必須分頁**:`_codex_v2_visible_threads` 的註解已經寫過這個坑 —— codex 會把
    一大批 guardian/subagent thread 排在 `thread/list` 的最前面(這台機器實測 390
    筆)。只撈第一頁的話,操作者自己那條 parent thread 會被擠到第二頁,
    `parent is None` → 面板回空清單,而且無聲無息。
    """
    global _CODEX_WORKER_THREADS_CACHE
    cached = _CODEX_WORKER_THREADS_CACHE
    mono = time.monotonic()
    if cached is not None and mono - cached[0] < _CODEX_WORKER_CACHE_TTL:
        return cached[1]
    params = {
        "limit": 100, "archived": False,
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        "sortKey": "updated_at", "sortDirection": "desc",
        "useStateDbOnly": False, "includeChildren": True,
    }
    rows: list[dict] = []
    cursor = None
    for _ in range(_CODEX_LIST_MAX_PAGES):
        if cursor:
            params["cursor"] = cursor
        else:
            params.pop("cursor", None)
        # 不外掛 asyncio.wait_for:CODEX_APP.call 自己有 timeout,而外層取消會在
        # 它持著 stdio 鎖、寫到一半時把 coroutine 砍掉,留半截 JSON-RPC frame 在
        # 共用管線上,毒到的是**每一條** codex session,不只這一個請求。
        res = await CODEX_APP.call("thread/list", params, timeout=8.0)
        batch = list((res or {}).get("data", []))
        rows.extend(batch)
        cursor = (res or {}).get("nextCursor")
        if not cursor or not batch:
            break
    _CODEX_WORKER_THREADS_CACHE = (mono, rows)
    return rows


async def _codex_workers_for(thread_id: str, now: float) -> list[dict]:
    if not thread_id:
        return []
    try:
        threads = await _codex_worker_threads()
    except Exception as e:      # noqa: BLE001 —— codex 掛掉不該讓工人面板整個 500
        _log_event("workers_codex_list_failed", error=type(e).__name__,
                   error_message=str(e)[:200])
        return []
    parent = next((t for t in threads if str(t.get("id") or "") == thread_id), None)
    if parent is None:
        return []
    rows = _codex_child_worker_rows(threads, parent, now)
    # 只留靜默期內的 —— 這是「現在在跑什麼」的視圖,不是 thread 歷史。
    return [r for r in rows
            if r["state"] == "running" or r["updated_ts"] >= now - WORKER_TTL_SECS]


def _hermes_workers_for(mid: str, now: float) -> list[dict]:
    """把既有 SUBSESSIONS 投影成人格的工人 —— **純唯讀**,不碰 SUBSESSIONS 自己
    的生命週期(它照舊 persist 到 canonical.db、照舊由既有邏輯收)。

    SUBSESSIONS 是永久記錄(重啟會從 canonical.db 讀回來),整份投影會把面板
    灌爆,所以同樣套靜默期:只留還在跑的、或最近有動靜的。"""
    out = []
    for sid, sub in list(SUBSESSIONS.items()):
        if str(sub.get("parent") or "") != mid:
            continue
        raw_status = str(sub.get("status") or "")
        state = workers_store.normalize_state(raw_status)
        try:
            updated = float(sub.get("lastAt") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        # 只有**明寫 running** 的才豁免靜默期。`normalize_state` 對認不得的狀態
        # 一律回 running(對 hook 回報是對的,ring 會過期收掉),但 SUBSESSIONS
        # 是**永久記錄**、重啟還會從 canonical.db 讀回來 —— 一筆 status 是 NULL
        # 的陳年 sub 若因此被當成 running,就會永遠掛在面板上,沒有東西收得掉它。
        # 同理 lastAt 缺席時是 0(遠古),不墊 now,否則它每次 poll 都像剛動過。
        if raw_status != "running" and updated < now - WORKER_TTL_SECS:
            continue
        out.append({
            "worker_id": f"sub:{sid}",
            "label": str(sub.get("name") or sid)[:200],
            "state": state,
            "parent_worker": None,
            "started_ts": updated,
            "updated_ts": updated,
            "meta": {"provider": "hermes", "kind": "subsession",
                     "sid": sid, "tool": str(sub.get("tool") or "")},
        })
    return out


async def _workers_projected(session: str, now: float) -> list[dict]:
    """provider 側現成記錄的唯讀投影(CC 沒有 —— CC 走 hook 主動回報)。"""
    if session.startswith("codex:") or session.startswith("delegation:"):
        # `delegation:<id>` 也是 app 拿得到的正牌 v2 session id,底下同樣是一條
        # codex thread。沿用既有解析器,不自己切前綴 —— 不然 delegation 這一路
        # 會安靜地永遠回空清單。
        try:
            thread_id = _codex_thread_from_v2_session_id(session)
        except HTTPException:
            return []       # 不是 codex 的 delegation / 還沒有 thread → 沒有工人
        return await _codex_workers_for(thread_id, now)
    if session.startswith("hermes:"):
        return _hermes_workers_for(session.split(":", 1)[1], now)
    return []


@app.post("/app/v2/workers/report")
async def v2_workers_report(request: Request):
    """工人回報(upsert)。body:{session | cc_session_id, worker_id, label,
    state: running|done|failed, parent_worker?, meta?}。

    這條路要**又快又不挑嘴** —— 呼叫端是 CC 的 PreToolUse/PostToolUse hook,
    卡住它就是卡住善彰的工具呼叫。所以:不寫 DB、不做 I/O、認不得的 state 收斂
    成 running 而不是 400。"""
    _workers_require_enabled()
    _check_auth(request)
    body = await _json_body(request)
    worker_id = str(body.get("worker_id") or "").strip()
    if not worker_id:
        raise HTTPException(status_code=400, detail="worker_id required")
    session = _worker_resolve_session(body)
    worker = WORKERS.report(session, worker_id,
                            label=body.get("label") or "",
                            state=body.get("state"),
                            parent_worker=body.get("parent_worker"),
                            meta=body.get("meta"))
    return {"ok": True, "session": session, "worker": worker}


@app.get("/app/v2/workers")
async def v2_workers_list(request: Request, session: str = ""):
    """某 session 現在有哪些工人在跑。

    形狀:{session, workers:[{worker_id, label, state, parent_worker,
    started_ts, updated_ts, meta}], counts:{running, done, failed}}
    工人依開工時間由舊到新。清單是「回報的」+「provider 投影的」合併結果。"""
    _workers_require_enabled()
    _check_auth(request)
    session = str(session or "").strip()
    if not session:
        raise HTTPException(status_code=400, detail="session required")
    now = time.time()
    reported = WORKERS.list(session, now)
    projected = await _workers_projected(session, now)
    merged = workers_store.merge(reported, projected)
    return {"session": session, "workers": merged,
            "counts": workers_store.counts_of(merged),
            "ttl_secs": WORKER_TTL_SECS}


@app.get("/app/v2/discovery")
async def v2_discovery(request: Request, refresh: int = 0, provider: str = ""):
    """全機發現面:那台機器上**每一個** agent session,標好誰已經在管。

    `state`:`managed`(已在治理內)/ `discovered`(看得到、還沒收編,
    reaper 永不碰)。`provider=cc,codex` 可過濾(逗號分隔,也吃
    claude_code/cx 別名)。`refresh=1` 略過 ~5 秒快取。
    """
    _check_auth(request)
    payload = await _discovery_sweep(force=bool(refresh))
    items = payload["items"]
    if provider:
        alias = {"cc": host_discovery.CC_PROVIDER, "cx": host_discovery.CX_PROVIDER,
                 "oc": host_discovery.OPENCLAW_PROVIDER}
        wanted = {alias.get(p.strip(), p.strip())
                  for p in provider.split(",") if p.strip()}
        items = [i for i in items if i.get("provider") in wanted]
    return {"items": items, "providers": payload["providers"],
            "generated_ts": payload["generated_ts"],
            "counts": {"total": len(items),
                       "managed": sum(1 for i in items
                                      if i.get("state") == host_discovery.STATE_MANAGED),
                       "discovered": sum(1 for i in items
                                         if i.get("state") == host_discovery.STATE_DISCOVERED)}}


def _cc_conf_adopt(name: str, workdir: str) -> bool:
    """把這個 pane 寫進 ccsess 常駐名單(冪等)。回「有沒有真的動到檔案」。

    **只動設定檔**:不 respawn、不 kill、不送任何按鍵。pane 上跑到一半的
    turn 完全不受影響 —— 收編就只是把它列進名單而已。

    已在名單的同名 session **不覆蓋 workdir**(那是既有 lane 的權威設定,
    使用者可能刻意設成別的目錄),只在 enabled=0 時把它打開。
    """
    if not name:
        return False
    existing = {n: (wd, en) for n, wd, en in _cc_conf_rows()}
    if name in existing:
        wd, enabled = existing[name]
        if enabled == "1":
            return False              # 已經在管了,不寫、不備份
        _cc_conf_backup()
        _cc_conf_upsert(name, wd, "1")
        return True
    _cc_conf_backup()
    _cc_conf_upsert(name, workdir or "", "1")
    return True


def _cc_conf_release(name: str) -> bool:
    """釋放時的選配動作:把該行整條從名單移除(先備份)。預設不做。"""
    if not name or not any(n == name for n, _wd, _en in _cc_conf_rows()):
        return False
    _cc_conf_backup()
    removed = {"hit": False}

    def _tx(lines):
        out, hit = host_discovery.conf_remove_lines(lines, name)
        removed["hit"] = hit
        return out if hit else None

    _cc_conf_mutate(_tx)
    return removed["hit"]


@app.post("/app/v2/discovery/{sid:path}/adopt")
async def v2_discovery_adopt(sid: str, request: Request):
    """收編:body `{purpose?, class?}`。

    收編 = **記帳**,不是重啟。cc 只多寫一行 ccsess 名單(先備份),
    cx/hermes/openclaw 純登記(它們本來就打得到)。收編後這條就有
    purpose/class/TTL、進得了家譜、受治理 —— 但進行中的工作一秒都不會斷。

    未知 id → 404;已收編 → 200 冪等。
    """
    _check_auth(request)
    body = await _json_body(request)
    cls = body.get("class")
    if cls is not None and cls not in agent_registry.CLASSES:
        raise HTTPException(status_code=400,
                            detail="class 必須是 persistent|task|ephemeral")
    item = await _discovery_find(sid)
    if item is None:
        raise http_err(404, "DISCOVERY_ID_UNKNOWN",
                       "發現面沒有這個 session",
                       f"unknown discovery id: {sid}")
    prev = REGISTRY.get(sid)
    already = bool(prev and prev.get("registered")
                   and prev.get("state") != "archived")
    conf_updated = False
    if item.get("provider") == host_discovery.CC_PROVIDER:
        conf_updated = _cc_conf_adopt(item.get("name") or "",
                                      item.get("workdir") or "")
    row = REGISTRY.adopt(
        sid, provider=item.get("provider") or "", name=item.get("name") or sid,
        purpose=body.get("purpose") or "", cls=cls,
        parent=item.get("parent") or None,
        worktree=item.get("workdir") or None,
        meta={"adopted_source": item.get("source") or "discovery"})
    _DISCOVERY_CACHE["ts"] = 0.0      # 下次 sweep 立刻反映新狀態
    _log_event("discovery_adopted", session=sid,
               provider=item.get("provider") or "", cls=cls or row.get("class"),
               already=already, conf_updated=conf_updated)
    return {"ok": True, "already_adopted": already,
            "conf_updated": conf_updated,
            "session": _discovery_registry_view(row)}


@app.post("/app/v2/discovery/{sid:path}/release")
async def v2_discovery_release(sid: str, request: Request):
    """收編的逆操作:registry 取消登記(registered=0 → reaper 永不碰它)。

    body `{remove_from_conf?}`:cc 預設**保留** ccsess 名單(釋放治理不等
    於要它別再自癒);帶 true 才連名單那行一起移除(先備份)。
    一樣不 kill、不重啟 —— 釋放只是不管它了,不是收掉它。
    """
    _check_auth(request)
    body = await _json_body(request)
    row = REGISTRY.get(sid)
    item = await _discovery_find(sid) if row is None else None
    if row is None and item is None:
        raise http_err(404, "DISCOVERY_ID_UNKNOWN",
                       "發現面與 registry 都沒有這個 session",
                       f"unknown discovery id: {sid}")
    conf_removed = False
    want_remove = str(body.get("remove_from_conf", "")).strip().lower() \
        in ("1", "true", "yes", "on")
    if sid.startswith(host_discovery.CC_PROVIDER + ":") and want_remove:
        conf_removed = _cc_conf_release(sid.split(":", 1)[1])
    released = REGISTRY.release(sid)
    _DISCOVERY_CACHE["ts"] = 0.0
    _log_event("discovery_released", session=sid,
               conf_removed=conf_removed, had_row=bool(row))
    return {"ok": True, "released": released is not None,
            "conf_removed": conf_removed,
            "session": _discovery_registry_view(released) if released else None}


# ═════════════ Agent 互調 agent_call(藍圖 AGENT_INTEROP §1,1c)═════════════
# bridge 是互調的**唯一 hub**:persona/cc/cx/openclaw 互相調用一律走
# POST /app/v2/agent_call,內部打現成的 v2 統一輸入路徑(v2_session_input),
# 不引入 agent 對 agent 直連(單一信任邊界、單一審計點)。
# 護欄(agent_call.py):AGENT_CALL=1 旗標(預設 OFF)、政策檔 default DENY、
# 深度 ≤2、循環拒絕、chain 預算;每次調用/回覆/拒絕落「🔗 代理互調」audit 卡
# 進雙方卡片流。**絕不代審**:目標端 CC/CX approval 照常走,這裡沒有任何
# 自動核准路徑。call 帳本落 registry DB(agent_calls 表,家譜可查)。

_AGENT_CALL_WAITERS: dict = {}   # call_id -> asyncio.Task(background 收割人)


def _agent_call_enabled() -> bool:
    """旗標每次呼叫讀 env:預設 OFF,merge 零風險;善彰在 plist 加
    AGENT_CALL=1 後重啟啟用(同 CC_TOKEN_STREAM 的開關慣例)。"""
    return str(os.environ.get("AGENT_CALL", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _agent_call_require_enabled() -> None:
    if not _agent_call_enabled():
        raise http_err(404, "AGENT_CALL_DISABLED",
                       "agent_call 未啟用(需 AGENT_CALL=1 + 政策檔)")


def _agent_call_timeout_default() -> float:
    try:
        return float(os.environ.get("AGENT_CALL_TIMEOUT", "") or 120.0)
    except ValueError:
        return 120.0


def _agent_call_bg_timeout() -> float:
    """background/await 轉背景後的收割窗上限(超過即 timeout 終態)。"""
    try:
        return float(os.environ.get("AGENT_CALL_BG_TIMEOUT", "") or 1800.0)
    except ValueError:
        return 1800.0


def _agent_call_reply_max() -> int:
    try:
        return int(os.environ.get("AGENT_CALL_REPLY_MAX", "") or 4000)
    except ValueError:
        return 4000


def _agent_call_normalize_sid(sid: str) -> str:
    """裸 persona id → hermes:{id}(政策檔與 API 都收得了兩種寫法)。"""
    sid = (sid or "").strip()
    if sid and ":" not in sid and sid in PERSONAS:
        return f"hermes:{sid}"
    return sid


def _agent_call_public(row: dict) -> dict:
    return {"call_id": row["id"], "caller": row["caller"],
            "target": row["target"], "mode": row["mode"],
            "status": row["status"], "reply": row.get("reply"),
            "error": row.get("error"),
            "root_call_id": row.get("root_call_id"),
            "parent_call_id": row.get("parent_call_id"),
            "depth": row.get("depth"),
            "created_ts": row.get("created_ts"),
            "finished_ts": row.get("finished_ts")}


class _AgentCallInputRequest:
    """v2_session_input 的 Request 替身:沿用原請求的 headers(auth 原樣過
    _check_auth)與來源位址,body 換成 bridge 代組的輸入。這樣 agent_call
    是**真重用**統一輸入路徑(冪等/registry touch/各 provider 分支全走原碼),
    不是複製一份。"""

    def __init__(self, request: Request, body: dict):
        self.headers = request.headers
        self.client = request.client
        self._body = body

    async def json(self):
        return self._body


async def _agent_call_audit(call: dict, phase: str, text: str,
                            sids: list) -> None:
    """audit 卡:kind "text" + 「🔗 代理互調」前綴 + fallback_text(舊 app
    照純文字渲染)。upsert 進每個 sid 的卡片流;單邊失敗只留痕不擋事。"""
    txt = f"🔗 代理互調|{text}"
    for sid in dict.fromkeys([s for s in sids if s]):
        try:
            store = await _v2_card_store(sid)
            store.upsert_card(carddigest.make_card(
                f"card-agentcall-{call['id']}-{phase}", "", "assistant", "text",
                {"text": txt, "fallback_text": txt, "origin": "agent_call",
                 "call_id": call["id"], "phase": phase,
                 "caller": call["caller"], "target": call["target"],
                 "mode": call["mode"]}))
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_agent_call_audit", _exc, expected=True, session=sid)


def _agent_call_collect_reply(store, since_seq: int) -> str:
    """從目標卡片流收割回覆:since_seq 之後 assistant 的 text/markdown 卡
    (排除 agent_call 自己的 audit 卡與 registry 警告卡),同卡多 rev 取最新,
    合併後截到 AGENT_CALL_REPLY_MAX。"""
    picked: dict = {}
    order: list = []
    for ev in store.events:
        if ev["seq"] <= since_seq or ev.get("type") != "card.upsert":
            continue
        card = (ev.get("data") or {}).get("card") or {}
        if card.get("role") != "assistant":
            continue
        if card.get("kind") not in ("text", "markdown"):
            continue
        body = card.get("body") or {}
        origin = str(body.get("origin") or "")
        if (origin.startswith("agent_call")
                or origin.startswith("agent_context")     # 👁 上下文讀取 audit 卡
                or origin == "registry.reap_warning"):
            continue
        t = str(body.get("text") or body.get("fallback_text") or "").strip()
        if not t:
            continue
        if card["id"] not in picked:
            order.append(card["id"])
        picked[card["id"]] = t
    reply = "\n\n".join(picked[cid] for cid in order).strip()
    cap = _agent_call_reply_max()
    if len(reply) > cap:
        reply = reply[:cap] + "…(回覆過長,已截斷)"
    return reply


async def _agent_call_waiter(call_id: str, caller: str, target: str,
                             store, since_seq: int) -> dict | None:
    """收割人:掛在目標卡片流上等 turn end → 收 assistant 回覆。
    subscribers+1 讓 CC follower 願意巡 status/發 turn 事件(同 SSE 訂閱者
    語意);絕不碰 approval —— 目標若停在待審,這裡就一路等到收割窗關。"""
    deadline = time.time() + _agent_call_bg_timeout()
    store.subscribers += 1
    waker = store.attach_waker()
    cursor = since_seq
    end_seen = False
    try:
        while True:
            fresh = [e for e in store.events if e["seq"] > cursor]
            if fresh:
                cursor = fresh[-1]["seq"]
                if any(e.get("type") == "turn" and
                       (e.get("data") or {}).get("state") == "end"
                       for e in fresh):
                    end_seen = True
            if end_seen:
                reply = _agent_call_collect_reply(store, since_seq)
                if reply:
                    row = REGISTRY.call_update(call_id, status="done",
                                               reply=reply)
                    _log_event("agent_call_done", call=call_id, caller=caller,
                               target=target, reply_chars=len(reply))
                    await _agent_call_audit(
                        row, "reply",
                        f"{target} 已回覆 {caller}(call {call_id[-8:]}):"
                        f"{reply[:200]}", [caller, target])
                    return row
            remain = deadline - time.time()
            if remain <= 0:
                row = REGISTRY.call_update(
                    call_id, status="timeout",
                    error="收割窗內未等到目標回覆(turn 未完成或無文字輸出)")
                _log_event("agent_call_timeout", call=call_id, caller=caller,
                           target=target)
                await _agent_call_audit(
                    row, "timeout",
                    f"{caller} → {target} 的調用(call {call_id[-8:]})逾時未收到"
                    f"回覆;目標可能仍在執行或停在待審(審核照常需人工核准)",
                    [caller, target])
                return row
            waker.clear()
            try:
                await asyncio.wait_for(waker.wait(),
                                       timeout=min(5.0, max(0.1, remain)))
            except asyncio.TimeoutError:
                pass
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_agent_call_waiter", _exc, call=call_id)
        row = REGISTRY.call_update(call_id, status="error",
                                   error=f"收割失敗:{_exc}")
        return row
    finally:
        store.subscribers -= 1
        store.detach_waker(waker)
        _AGENT_CALL_WAITERS.pop(call_id, None)


def _agent_call_record_denial(call_id: str, caller: str, target: str,
                              mode: str, message: str, code: str,
                              reason: str) -> dict:
    row = REGISTRY.call_create(call_id, caller=caller, target=target,
                               mode=mode, message=message, status="denied",
                               error=reason)
    _log_event("agent_call_denied", call=call_id, caller=caller,
               target=target, code=code, reason=reason)
    return row


async def _agent_call_deny(call_id: str, caller: str, target: str, mode: str,
                           message: str, code: str, reason: str,
                           status: int) -> None:
    """拒絕三件套:落帳(denied)、audit 卡(caller 必發;target 已有 store
    才發,不為一張拒絕卡新建 store)、丟 HTTP。"""
    row = _agent_call_record_denial(call_id, caller, target, mode, message,
                                    code, reason)
    sids = [caller]
    if _registry_card_store(target) is not None:
        sids.append(target)
    await _agent_call_audit(row, "denied",
                            f"拒絕 {caller} → {target}:{reason}", sids)
    raise http_err(status, code, reason)


@app.post("/app/v2/agent_call")
async def v2_agent_call(request: Request):
    """agent 互調入口(藍圖 §1)。body:
    {caller: session_id, target: session_id|persona_id, message,
     mode: fire_and_forget|await_reply|background, timeout_secs?,
     parent_call_id?}。

    caller 自報身分(信任邊界 = bridge token;audit 卡與帳本都以此記名)。
    1c 只調**既有** session:target 解析不到 → 404,不代為 spawn(要生新
    session 走既有派工路徑,配額由 registry precheck 把關)。"""
    _check_auth(request)
    _agent_call_require_enabled()
    body = await _json_body(request)
    caller = _agent_call_normalize_sid(str(body.get("caller") or ""))
    target = _agent_call_normalize_sid(str(body.get("target") or ""))
    message = str(body.get("message") or "").strip()
    mode = str(body.get("mode") or "await_reply").strip() or "await_reply"
    if mode not in agent_call_policy.MODES:
        raise http_err(400, "AGENT_CALL_BAD_MODE",
                       f"mode 必須是 {'|'.join(agent_call_policy.MODES)}")
    if not caller or not target or not message:
        raise http_err(400, "AGENT_CALL_BAD_REQUEST",
                       "caller、target、message 皆為必填")
    if caller == target:
        raise http_err(400, "AGENT_CALL_SELF", "不能調用自己")
    try:
        _v2_card_source(caller)
    except HTTPException:
        raise http_err(400, "AGENT_CALL_BAD_CALLER",
                       f"caller 不是已知 session:{caller}")
    try:
        _v2_card_source(target)
    except HTTPException:
        raise http_err(404, "AGENT_CALL_TARGET_NOT_FOUND",
                       f"target 不是既有 session:{target}(1c 不代為 spawn;"
                       f"要生新 session 請走派工路徑,配額由 registry 把關)")
    call_id = "call-" + uuid.uuid4().hex[:16]
    # ── 護欄 1:政策 allowlist(default DENY)──────────────────────────
    policy = agent_call_policy.load_policy()
    if not agent_call_policy.allowed(policy, caller, target, registry=REGISTRY):
        await _agent_call_deny(
            call_id, caller, target, mode, message, "AGENT_CALL_DENIED",
            f"政策未放行 {caller} → {target}(default DENY;家譜直接母子邊自動放行,"
            f"其餘請在 {agent_call_policy.policy_path()} 加 allowlist 規則)", 403)
    # ── 護欄 2:chain 深度/循環/預算 ──────────────────────────────────
    parent = None
    pid = str(body.get("parent_call_id") or "").strip()
    if pid:
        parent = REGISTRY.call_get(pid)
        if parent is None:
            raise http_err(400, "AGENT_CALL_BAD_PARENT",
                           f"parent_call_id 不存在:{pid}")
    else:
        # 推斷:有 call 正打在 caller 身上 → caller 的外呼是同 chain 下一層。
        parent = REGISTRY.call_active_for_target(
            caller, agent_call_policy.chain_window_secs())
    ancestors = REGISTRY.call_ancestors(parent) if parent else []
    chain_size = REGISTRY.call_chain_size(
        str(parent.get("root_call_id") or parent.get("id"))) if parent else 0
    try:
        root_id, parent_id, depth = agent_call_policy.check_chain(
            parent, ancestors, chain_size, caller, target)
    except agent_call_policy.CallDenied as e:
        await _agent_call_deny(call_id, caller, target, mode, message,
                               e.code, e.reason, 429)
    row = REGISTRY.call_create(
        call_id, caller=caller, target=target, mode=mode, message=message,
        status="running", root_call_id=root_id, parent_call_id=parent_id,
        depth=depth)
    _log_event("agent_call_created", call=call_id, caller=caller,
               target=target, mode=mode, depth=depth,
               root=row.get("root_call_id"))
    # 戶政:互調也是活著的證據(target 的 touch 由輸入路徑自己記)。
    _registry_call_safe("touch", caller)
    # audit 卡(request)雙邊落卡 —— Pocket 上看得到誰叫了誰、說了什麼。
    await _agent_call_audit(
        row, "request",
        f"{caller} → {target}({mode},call {call_id[-8:]}):{message[:300]}",
        [caller, target])
    # await/background 需要先掛上目標卡片流,記住基準 seq 再投遞。
    store = since_seq = None
    if mode in ("await_reply", "background"):
        store = await _v2_card_store(target)
        since_seq = store.seq
    # ── 投遞:真重用 v2 統一輸入路徑(不複製 provider 分支)────────────
    content = f"[agent_call {caller} #{call_id[-8:]}] {message}"
    shim = _AgentCallInputRequest(request, {"content": content,
                                            "client_id": call_id})
    try:
        await v2_session_input(target, shim)
    except HTTPException as e:
        REGISTRY.call_update(call_id, status="error",
                             error=f"投遞失敗:{e.detail}")
        _log_event("agent_call_dispatch_failed", call=call_id, target=target,
                   status=e.status_code, detail=str(e.detail)[:200])
        await _agent_call_audit(row, "error",
                                f"{caller} → {target} 投遞失敗:{e.detail}",
                                [caller, target])
        raise http_err(502, "AGENT_CALL_DISPATCH_FAILED",
                       f"投遞到 {target} 失敗:{e.detail}")
    if mode == "fire_and_forget":
        REGISTRY.call_update(call_id, status="sent")
        return {"ok": True, "call_id": call_id, "status": "sent"}
    task = asyncio.create_task(
        _agent_call_waiter(call_id, caller, target, store, since_seq))
    _AGENT_CALL_WAITERS[call_id] = task
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    if mode == "background":
        return {"ok": True, "call_id": call_id, "status": "running"}
    # await_reply:同步等到 timeout_secs;逾時 call 轉背景繼續收割。
    try:
        timeout_secs = float(body.get("timeout_secs") or
                             _agent_call_timeout_default())
    except (TypeError, ValueError):
        timeout_secs = _agent_call_timeout_default()
    timeout_secs = max(1.0, min(timeout_secs, 600.0))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_secs)
    except asyncio.TimeoutError:
        REGISTRY.call_update(call_id, meta_merge={"await_timed_out": True})
        _log_event("agent_call_await_timeout", call=call_id, target=target,
                   timeout_secs=timeout_secs)
        return {"ok": True, "call_id": call_id, "status": "timeout",
                "note": "await 逾時,call 轉為 background 繼續收割;"
                        "稍後用 GET /app/v2/agent_call/{call_id} 取結果"}
    fresh = REGISTRY.call_get(call_id) or row
    return {"ok": True, **_agent_call_public(fresh)}


@app.get("/app/v2/agent_call/{call_id}")
async def v2_agent_call_result(call_id: str, request: Request):
    """background/await-逾時 的收割端點(藍圖 §1 agent_result)。"""
    _check_auth(request)
    _agent_call_require_enabled()
    row = REGISTRY.call_get(call_id)
    if row is None:
        raise http_err(404, "AGENT_CALL_NOT_FOUND", "沒有這筆 call")
    return {"ok": True, **_agent_call_public(row)}


@app.get("/app/v2/agent_calls")
async def v2_agent_calls(request: Request, session: str = "", root: str = "",
                         limit: int = 50):
    """call 帳本查詢(Pocket 編隊視圖的呼叫鏈資料源):?session= 看某 session
    參與的 call;?root= 看整條 chain。"""
    _check_auth(request)
    _agent_call_require_enabled()
    rows = REGISTRY.call_list(
        session=_agent_call_normalize_sid(session) or None,
        root=root or None, limit=max(1, min(limit, 200)))
    return {"calls": [_agent_call_public(r) for r in rows]}


@app.get("/app/v2/agent_targets")
async def v2_agent_targets(request: Request, caller: str = ""):
    """該 caller 依政策可調用的對象(藍圖 §1 agent_list):id/provider/
    purpose(registry 戶口)/busy(provider 現成信號)。"""
    _check_auth(request)
    _agent_call_require_enabled()
    caller = _agent_call_normalize_sid(caller)
    if not caller:
        raise http_err(400, "AGENT_CALL_BAD_REQUEST", "caller 必填")
    policy = agent_call_policy.load_policy()
    _registry_ensure_personas()
    rows = [r for r in REGISTRY.list_rows() if r.get("state") != "archived"]
    by_id = {r["id"]: r for r in rows}
    for lr in await _registry_legacy_rows(set(by_id)):
        by_id[lr["id"]] = lr
    out = []
    for sid, r in sorted(by_id.items()):
        if sid == caller:
            continue
        if not agent_call_policy.allowed(policy, caller, sid, registry=REGISTRY):
            continue
        out.append({"id": sid, "provider": r.get("provider"),
                    "purpose": r.get("purpose"),
                    "class": r.get("class"),
                    "busy": await _registry_is_busy(sid)})
    return {"caller": caller, "targets": out,
            "policy_path": agent_call_policy.policy_path()}


# ══════════════════════════════════════════════════════════════════════════
# Continual Harness(藍圖 AGENT_INTEROP §2 / 子程序設計 §0)—— 累積層接線
# ══════════════════════════════════════════════════════════════════════════
# Prime Agent 的洞見:贏在累積,不在執行。每回合軌跡回寫 Prompt/Memory/
# Skill/Subagent 四庫,節點下次開工站在上次的肩膀上。
#
# 善彰的鐵律:**夜批蒸餾 + 晨報人審,不搞自動自改**。所以:
#   - 蒸餾器只產 state=proposed,沒有任何自動生效路徑
#   - approve 只在人打 HTTP 時發生(`_harness_store().approve` 的唯一呼叫點)
#   - prompt 提案核准 → 寫進該節點的 ccsess spawn pin,**下次** spawn 才吃到
#     (不重啟、不干擾進行中的工作)
#
# 資料面在 harness/ 套件(獨立 sqlite,env HARNESS_DB,預設 ~/.pocket/
# harness.db)。**canonical.db / state.db 一律 mode=ro 唯讀**;harness 整個
# 炸掉最多是少一晚提案,聊天資料零風險。
#
# 旗標:HARNESS=1 才開,預設 OFF(merge = 零風險)——關閉時端點 404、
# 背景蒸餾/收集任務完全不啟動、晨報不加 harness 段。

_HARNESS_STORE = None
_HARNESS_INGEST_SECS = float(os.environ.get("HARNESS_INGEST_SECS", "300"))
_HARNESS_DISTILL_HOUR = int(os.environ.get("HARNESS_DISTILL_HOUR", "4"))
_HARNESS_DISTILL_HOURS = float(os.environ.get("HARNESS_DISTILL_HOURS", "24"))
_HARNESS_REPORT_LABEL = "蒸餾提案"
_HARNESS_REPORT_NAME = "harness-proposals"


def _harness_enabled() -> bool:
    """旗標每次呼叫讀 env(同 AGENT_CALL 慣例):預設 OFF,merge 零風險。"""
    return str(os.environ.get("HARNESS", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _harness_require_enabled() -> None:
    if not _harness_enabled():
        raise http_err(404, "HARNESS_DISABLED",
                       "Continual Harness 未啟用(需 HARNESS=1)")


def _harness_store():
    """惰性建庫 —— 旗標關著時連 DB 檔都不會被建出來。"""
    global _HARNESS_STORE
    if _HARNESS_STORE is None:
        _HARNESS_STORE = harness_store.HarnessStore(
            os.environ.get("HARNESS_DB")
            or os.path.join(_POCKET_DIR, "harness.db"))
    return _HARNESS_STORE


# ── 軌跡收集(唯讀:卡片流 ring + registry meta)───────────────────────────

def _harness_node_meta(sid: str) -> dict:
    """registry 的節點中繼:purpose / provider / node_config(已去密)。

    node_config 走現成的 `_spawn_config_public()`(api_key 連遮罩版都不給,
    只留 has_api_key 布林),再由 `trajectory.redact_config()` 擋第二道。
    """
    row = REGISTRY.get(sid) or {}
    provider = str(row.get("provider") or "")
    cfg: dict = {}
    src = None
    try:
        src = _v2_card_source(sid)
    except Exception:  # noqa: BLE001 — 認不得的 id 就沒有 spawn config
        src = None
    if src and src[0] == "cc":
        try:
            cfg = _spawn_config_public(_cc_read_spawn_config(src[1]))
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_harness_node_meta.cfg", _exc, expected=True)
    meta = {"purpose": str(row.get("purpose") or ""),
            "provider": provider or (src[0] if src else ""),
            "node_config": cfg,
            "parent": row.get("parent") or "",
            "class": row.get("class") or ""}
    return meta


async def _harness_ingest_session(sid: str) -> int:
    """把某個 session 卡片流 ring 裡的完成回合正規化後落 harness DB。

    冪等:trajectory id = sha1(session|turn),重放同一段 ring 只會更新同一
    列。所以「每 N 分鐘掃一次」這種粗暴做法不會產生重複軌跡,也不需要在
    turn.end 熱路徑上掛鉤子(掛鉤子 = 動到 production 最敏感的那條線)。
    """
    if not _harness_enabled():
        return 0
    try:
        store = await _v2_card_store(sid)
    except Exception as _exc:  # noqa: BLE001 — 認不得/取不到就跳過
        _log_exc("_harness_ingest_session.store", _exc, expected=True)
        return 0
    meta = _harness_node_meta(sid)
    try:
        trajs = harness_traj.from_card_events(
            list(getattr(store, "events", []) or []), session_id=sid,
            provider=meta["provider"], purpose=meta["purpose"],
            node_config=meta["node_config"])
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_harness_ingest_session.normalize", _exc, expected=True)
        return 0
    live_turn = str(getattr(store, "turn_id", "") or "")
    hs = _harness_store()
    n = 0
    for t in trajs:
        if t["turn_id"] and t["turn_id"] == live_turn:
            continue          # 進行中的回合不落庫(軌跡還沒寫完)
        try:
            hs.put_trajectory(t)
            n += 1
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_harness_ingest_session.put", _exc, expected=True)
    return n


async def _harness_ingest_sweep() -> int:
    """巡一輪所有活著的登記節點,收軌跡。只讀,不動任何 provider 狀態。"""
    total = 0
    for row in REGISTRY.list_rows():
        if row.get("state") == "archived":
            continue
        total += await _harness_ingest_session(row["id"])
    return total


async def _harness_ingest_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_HARNESS_INGEST_SECS)
            if not _harness_enabled():
                continue
            n = await _harness_ingest_sweep()
            if n:
                _log_event("harness_ingest", trajectories=n)
        except asyncio.CancelledError:
            raise
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_harness_ingest_loop", _exc, expected=True)


# ── 夜批(排程)────────────────────────────────────────────────────────────

async def _harness_run_distill(hours: float | None = None,
                               dry_run: bool = False) -> dict:
    """跑一輪蒸餾(收軌跡 → 蒸餾 → 落提案)。模型走 harness/model.py 的
    本機 Ollama —— 與會議清稿同一條線,不新增任何雲端依賴/金鑰。"""
    await _harness_ingest_sweep()
    out = await harness_distill.run(
        _harness_store(), hours=hours or _HARNESS_DISTILL_HOURS,
        current_prompt=_harness_current_prompt, dry_run=dry_run)
    _log_event("harness_distilled", trajectories=out["trajectories"],
               groups=out["groups"], proposals=len(out["proposals"]),
               errors=len(out["errors"]), dry_run=dry_run)
    return out


async def _harness_distill_loop() -> None:
    """夜批排程:每小時醒一次,到 HARNESS_DISTILL_HOUR(預設凌晨 4 點)就跑。

    刻意不用 cron:bridge 是常駐行程,自己數鐘點最簡單、也最容易在晨報上
    回報「昨晚跑了沒」。同一天只跑一次(用 last_run 的日期擋)。
    """
    while True:
        try:
            await asyncio.sleep(3600)
            if not _harness_enabled():
                continue
            now = time.localtime()
            if now.tm_hour != _HARNESS_DISTILL_HOUR:
                continue
            last = _harness_store().last_run()
            if last and time.localtime(last["started_ts"]).tm_yday == now.tm_yday:
                continue
            await _harness_run_distill()
        except asyncio.CancelledError:
            raise
        except Exception as _exc:  # noqa: BLE001
            _log_exc("_harness_distill_loop", _exc, expected=True)


# ── prompt 提案 → spawn-config pin(核准後才走,閉環)──────────────────────

def _harness_current_prompt(node: str) -> str:
    """該節點現行的 append_system_prompt(做 diff 預覽用)。"""
    try:
        return str(_cc_read_spawn_config(node).get("append_system_prompt") or "")
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_harness_current_prompt", _exc, expected=True)
        return ""


def _harness_apply_prompt(node: str, fragment: str) -> str:
    """把核准的片段寫進該節點的 ccsess spawn pin,**下次** spawn 生效。

    ⚠️ 刻意不呼叫 `_cc_write_spawn_pins()`:那支在 cfg 沒有 api_key 時會
    **刪掉** BYO key 的 0600 secret 檔。這裡是局部更新(只動
    append_system_prompt),絕不能連帶砍掉使用者自帶的金鑰。所以只讀寫
    flags 檔本身,secret 檔一根汗毛都不碰。

    回傳給人看的一句話(寫進提案的 apply_note)。
    """
    fragment = str(fragment or "").strip()
    if not fragment:
        return "片段是空的,未寫入"
    path = os.path.join(CCSESS_SPAWN_DIR, node + ".json")
    cfg: dict = {}
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f) or {}
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_harness_apply_prompt.read", _exc, expected=True)
        cfg = {}
    cfg.pop("api_key", None)          # 防禦:flags 檔本來就不該有金鑰
    cfg.pop("has_api_key", None)
    cfg["append_system_prompt"] = fragment
    # 走現成的驗證(enum/長度/型別),壞值不落地
    cfg = _spawn_config_validate(cfg, "cc")
    os.makedirs(CCSESS_SPAWN_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.replace(tmp, path)
    _log_event("harness_prompt_pinned", node=node, chars=len(fragment))
    return f"已寫入 {node} 的 spawn pin,下次開此節點的子程序時生效"


def _harness_apply(row: dict) -> str:
    """核准後的落地動作。目前只有 prompt 庫有現成的機械可以閉環;
    memory/skill/route 先進庫等消費端(v0 誠實邊界,見 docs)。"""
    if row.get("store") != "prompt":
        return ""
    node = str(row.get("node") or "")
    provider = str(row.get("provider") or "")
    if not node:
        return ""
    if provider and provider not in ("cc", "claude_code"):
        return (f"{provider} 沒有持久的 spawn pin,片段已核准但需在派工時"
                "手動帶入(v0 限制)")
    return _harness_apply_prompt(node, str(row.get("fragment") or ""))


# ── 端點(晨報審核面)──────────────────────────────────────────────────────

def _harness_public(row: dict) -> dict:
    """回給 app/晨報的提案形狀。內容早在正規化階段就過遮罩,這裡不再改寫。"""
    out = {k: row.get(k) for k in
           ("id", "store", "scope", "key", "version", "state", "rationale",
            "evidence", "preview", "created_ts", "updated_ts", "decided_ts",
            "decided_by", "applied", "apply_note", "meta")}
    _pfx, extra = harness_store.STORES[row["store"]]
    out["payload"] = {name: row.get(name) for name, _d in extra}
    return out


@app.get("/app/v2/harness/proposals")
async def v2_harness_proposals(request: Request, state: str = "proposed",
                               store: str | None = None,
                               scope: str | None = None, limit: int = 100):
    """待審提案清單(晨報與 Pocket 審核頁的資料源)。

    state 給空字串 = 全部狀態。每筆帶 rationale(為什麼)、evidence
    (哪幾條軌跡)、preview(會變成什麼)—— 人審要的三件事。
    """
    _check_auth(request)
    _harness_require_enabled()
    if store and store not in harness_store.STORES:
        raise HTTPException(status_code=400,
                            detail=f"store 需為 {'/'.join(harness_store.STORES)} 其一")
    if state and state not in harness_store.STATES:
        raise HTTPException(status_code=400,
                            detail=f"state 需為 {'/'.join(harness_store.STATES)} 其一")
    rows = _harness_store().list(store=store, state=state or None, scope=scope,
                                 limit=max(1, min(limit, 500)))
    return {"proposals": [_harness_public(r) for r in rows],
            "stores": list(harness_store.STORES),
            "last_run": _harness_store().last_run()}


@app.post("/app/v2/harness/proposals/{pid}/approve")
async def v2_harness_approve(pid: str, request: Request):
    """人審通過:proposed → approved → active,並執行落地動作。

    **這是整個 harness 唯一會讓東西生效的入口,而且只有人打得到。**
    prompt 提案核准 = 片段寫進該節點 spawn pin,下次 spawn 就吃到 ——
    與既有的 spawn-config 機械閉環。
    """
    _check_auth(request)
    _harness_require_enabled()
    body = await _json_body(request)
    by = str(body.get("by") or "human")[:60]
    hs = _harness_store()
    row = hs.get(pid)
    if row is None:
        raise http_err(404, "HARNESS_PROPOSAL_NOT_FOUND", "找不到這筆提案")
    try:
        row = hs.approve(pid, by=by)
    except harness_store.StateError as exc:
        raise http_err(409, "HARNESS_STATE", exc.detail)
    note = ""
    try:
        note = _harness_apply(row)
    except Exception as exc:  # noqa: BLE001 — 落地失敗不回滾核准,但要說清楚
        note = f"核准了,但落地失敗:{type(exc).__name__}: {exc}"[:300]
        _log_exc("_harness_apply", exc, expected=True)
    if note:
        hs.mark_applied(pid, note)
        row = hs.get(pid)
    _log_event("harness_approved", proposal=pid, store=row["store"],
               key=row["key"], by=by, applied=bool(note))
    return {"ok": True, "proposal": _harness_public(row), "applied_note": note}


@app.post("/app/v2/harness/proposals/{pid}/reject")
async def v2_harness_reject(pid: str, request: Request):
    """人審否決。理由留在 apply_note,下次蒸餾出同樣的東西時看得到前科。"""
    _check_auth(request)
    _harness_require_enabled()
    body = await _json_body(request)
    hs = _harness_store()
    if hs.get(pid) is None:
        raise http_err(404, "HARNESS_PROPOSAL_NOT_FOUND", "找不到這筆提案")
    try:
        row = hs.reject(pid, by=str(body.get("by") or "human")[:60],
                        reason=str(body.get("reason") or "")[:500])
    except harness_store.StateError as exc:
        raise http_err(409, "HARNESS_STATE", exc.detail)
    _log_event("harness_rejected", proposal=pid, store=row["store"], key=row["key"])
    return {"ok": True, "proposal": _harness_public(row)}


@app.post("/app/v2/harness/distill")
async def v2_harness_distill(request: Request):
    """手動催一輪夜批(驗收/補跑用)。body {hours?, dry_run?}。"""
    _check_auth(request)
    _harness_require_enabled()
    body = await _json_body(request)
    try:
        hours = float(body.get("hours") or _HARNESS_DISTILL_HOURS)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="hours 必須是數字(小時)")
    out = await _harness_run_distill(hours=hours, dry_run=bool(body.get("dry_run")))
    return {"ok": True, "trajectories": out["trajectories"],
            "groups": out["groups"], "proposals": len(out["proposals"]),
            "written": out["written"], "errors": out["errors"],
            "dry_run": out["dry_run"]}


@app.get("/app/v2/harness/status")
async def v2_harness_status(request: Request):
    """旗標/庫況一覽(善彰開燈後第一個該看的)。"""
    _check_auth(request)
    _harness_require_enabled()
    hs = _harness_store()
    counts = {s: len(hs.list(store=s, state="proposed", limit=500))
              for s in harness_store.STORES}
    active = {s: len(hs.active(s)) for s in harness_store.STORES}
    return {"enabled": True, "db": hs.db_path,
            "model": harness_model.distill_model(),
            "distill_hour": _HARNESS_DISTILL_HOUR,
            "pending": counts, "active": active,
            "last_run": hs.last_run()}


# ── 晨報段(善彰已經在看的地方)────────────────────────────────────────────

def _harness_report_content(pending: list, last_run: dict | None) -> str:
    """待審提案 → 晨報 markdown。照 `_tool_error_report_content` 的樣式。"""
    lines = ["## 蒸餾提案待審", ""]
    if last_run:
        lines.append(
            f"- 昨夜蒸餾:{_fmt_ts(last_run.get('started_ts') or 0)}"
            f",看了 {last_run.get('trajectories') or 0} 條軌跡"
            f",提了 {last_run.get('proposals') or 0} 案")
        if last_run.get("error"):
            lines.append(f"- ⚠️ 跑批有錯:{_clip_text(last_run['error'], 200)}")
    if not pending:
        lines += ["", "目前沒有待審提案。"]
        return "\n".join(lines).strip()
    lines += [f"- 待審 **{len(pending)}** 筆(核准前不會有任何東西生效)", ""]
    label = {"memory": "記憶", "skill": "技能", "prompt": "系統提示",
             "subagent_route": "路由"}
    for p in pending[:12]:
        lines += [
            f"### [{label.get(p['store'], p['store'])}] {p['key']}",
            f"- 範圍:`{p['scope']}` · 版本 v{p['version']}",
            f"- 理由:{p.get('rationale') or '(無)'}",
            f"- 證據:{len(p.get('evidence') or [])} 條軌跡",
            "",
            "```diff",
            _fenced_text(p.get("preview") or "", 800),
            "```",
            "",
        ]
    if len(pending) > 12:
        lines.append(f"…另有 {len(pending) - 12} 筆,請到 Pocket 的蒸餾提案頁查看。")
    lines += ["", f"核准/否決:`POST /app/v2/harness/proposals/<id>/approve|reject`"]
    return "\n".join(lines).strip()


def _persona_harness_reports(persona: str, limit: int = 1) -> list[dict]:
    """晨報的 harness 段(report_events 產出者,接在 `_sync_persona_reports`)。

    旗標關著、或沒有任何待審提案且昨夜沒跑批 → 回空(不打擾)。
    external_id 以日期為鍵:一天最多一則,夜批跑完更新內容,不會洗版。
    """
    if not _harness_enabled():
        return []
    try:
        hs = _harness_store()
        pending = hs.list(state="proposed", limit=200)
        last = hs.last_run()
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_persona_harness_reports", _exc, expected=True)
        return []
    if not pending and not last:
        return []
    day = time.strftime("%Y-%m-%d")
    external_id = f"harness:{persona}:{day}"
    ts = time.time()
    return [{"id": _report_id(persona, _HARNESS_REPORT_NAME, day, ts),
             "external_id": external_id,
             "external_source": "harness",
             "session_id": f"harness-{day}",
             "label": _HARNESS_REPORT_LABEL,
             "name": _HARNESS_REPORT_NAME,
             "content": _harness_report_content(pending, last),
             "ts": ts}]


@app.on_event("startup")
async def _start_harness():
    """旗標關著就完全不啟動任何背景工作(zero risk),也不建 DB 檔。"""
    if not _harness_enabled():
        _log_event("harness_disabled")
        return
    for coro in (_harness_ingest_loop(), _harness_distill_loop()):
        task = asyncio.create_task(coro)
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    _log_event("harness_started", db=_harness_store().db_path,
               model=harness_model.distill_model(),
               ingest_secs=_HARNESS_INGEST_SECS,
               distill_hour=_HARNESS_DISTILL_HOUR)
# ═════════ 跨 session 上下文互讀 agent_context(接手/協作的資訊落差)═════════
# agent_call 解決「A 叫得動 B」;這裡解決「A 看得懂 B 在幹嘛」——cc/cx 接手
# 或並行時,不用再靠人肉貼上下文。三種模式,由便宜到貴:
#   summary  蒸餾過的交接簡報(本機 Ollama;依 (session, last_seq) 快取)
#   recent   最近 N 張卡的 fallback_text(原文,封頂)
#   search   在「這個 caller 讀得到的範圍內」找關鍵字(原文片段,封頂)
# 護欄(agent_context.py):AGENT_CONTEXT=1 旗標(預設 OFF → 404)、default DENY
# 的三來源放行(母子邊/context_targets 規則/agent_call 放行隱含 summary)、
# **強制遮罩**(連餵給模型的素材都先遮)、每 caller 速率上限、回應字元硬上限。
# 每次讀取往**被讀的 session** 落一張「👁 上下文讀取」卡 —— 誰讀了誰看得見。
# 資料源全部是現成的(卡片流 ring:CC jsonl / codex thread / persona 都已由
# _v2_card_store 統一 seed),這裡不新增任何紀錄機制、不寫 canonical.db。

_AGENT_CONTEXT_SUMMARY_CACHE: dict = {}   # target -> {seq, text, ts}
_AGENT_CONTEXT_HITS: dict = {}            # caller -> [ts,…](速率窗)


def _agent_context_enabled() -> bool:
    """旗標每次呼叫讀 env:預設 OFF,merge 零風險(同 AGENT_CALL 慣例)。"""
    return str(os.environ.get("AGENT_CONTEXT", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _agent_context_require_enabled() -> None:
    if not _agent_context_enabled():
        raise http_err(404, "AGENT_CONTEXT_DISABLED",
                       "agent_context 未啟用(需 AGENT_CONTEXT=1 + 政策檔)")


def _agent_context_rate_check(caller: str) -> None:
    """每 caller 每分鐘 N 次(預設 30)。被拒的讀取也記次數 —— 不然探測政策
    邊界是免費的。"""
    limit = agent_context_policy.rate_per_min()
    if limit <= 0:
        return
    now = time.time()
    hits = [t for t in _AGENT_CONTEXT_HITS.get(caller, []) if now - t < 60.0]
    if len(hits) >= limit:
        _AGENT_CONTEXT_HITS[caller] = hits
        _log_event("agent_context_rate_limited", caller=caller, limit=limit)
        raise http_err(429, "AGENT_CONTEXT_RATE_LIMITED",
                       f"上下文讀取太頻繁({caller} 每分鐘上限 {limit} 次);"
                       f"請改用 summary(有快取)或稍後再試")
    hits.append(now)
    _AGENT_CONTEXT_HITS[caller] = hits
    if len(_AGENT_CONTEXT_HITS) > 500:      # 冷 caller 的空窗回收
        for k in [k for k, v in _AGENT_CONTEXT_HITS.items() if not v]:
            _AGENT_CONTEXT_HITS.pop(k, None)


def _agent_context_warm_store(sid: str):
    """**已在記憶體裡**的卡片流。search 掃全範圍時只看熱 store:為了搜尋去冷
    載入整台機器的 session(seed jsonl / 拉 codex 歷史)會把一個回合拖垮,
    而且會把沒人在看的 session 全部叫醒。明示單一 target 時才走 _v2_card_store。"""
    store = _registry_card_store(sid)
    if store is not None:
        return store
    try:
        kind, ref = _registry_provider_ref(sid)
        if kind == "hp":
            d = _HP_CARD_DIGESTS.get(ref)
            return d.store if d else None
        if sid.startswith("openclaw:"):
            d = _OC_CARD_DIGESTS.get(_oc_safe_session_key(sid.split(":", 1)[1]))
            return d.store if d else None
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_agent_context_warm_store", _exc, expected=True, session=sid)
    return None


def _agent_context_card_text(card: dict) -> str:
    """一張卡的可讀文字。**自己的 audit 卡不算內容** —— 不然讀取行為會污染
    下一次讀到的內容,還會讓摘要快取每讀必失效。"""
    body = card.get("body") or {}
    if str(body.get("origin") or "").startswith("agent_context"):
        return ""
    return str(body.get("fallback_text") or body.get("text") or "").strip()


def _agent_context_source_seq(store) -> int:
    """快取鍵用的「內容 seq」= 排除 audit 卡之後的最大 seq。

    直接用 `store.seq` 會壞事:每次讀取都會落一張 audit 卡把 seq 推高,快取
    永遠 miss,summary 就變成每次都燒一次模型。這裡只認真正的內容事件。
    """
    seq = 0
    for ev in getattr(store, "events", None) or []:
        if ev.get("type") == "card.upsert":
            card = (ev.get("data") or {}).get("card") or {}
            if str(((card.get("body") or {}).get("origin") or "")).startswith(
                    "agent_context"):
                continue
        seq = max(seq, int(ev.get("seq") or 0))
    return seq


def _agent_context_lines(store, limit: int) -> list:
    """最近 limit 張有文字的卡 → 已遮罩的行(時間順)。"""
    order = list(getattr(store, "order", None) or [])
    cards = getattr(store, "cards", None) or {}
    out: list = []
    for cid in reversed(order):
        card = cards.get(cid) or {}
        text = _agent_context_card_text(card)
        if not text:
            continue
        out.append(agent_context_policy.card_line(
            card.get("role") or "", text, card.get("ts")))
        if len(out) >= max(1, limit):
            break
    out.reverse()
    return out


def _agent_context_search_store(store, sid: str, query: str,
                                max_hits: int) -> list:
    """單一 store 的子字串搜尋(新→舊掃,回時間順;片段已遮罩)。"""
    order = list(getattr(store, "order", None) or [])
    cards = getattr(store, "cards", None) or {}
    hits: list = []
    for cid in reversed(order):
        card = cards.get(cid) or {}
        text = _agent_context_card_text(card)
        if not text:
            continue
        frag = agent_context_policy.match_snippet(text, query)
        if frag is None:
            continue
        hits.append({"session": sid, "ts": card.get("ts"),
                     "role": card.get("role") or "", "snippet": frag})
        if len(hits) >= max(1, max_hits):
            break
    hits.reverse()
    return hits


async def _agent_context_meta(sid: str) -> dict:
    """回應的固定欄位:戶口(purpose/provider/parent/model)+ 現況(busy)。"""
    row = REGISTRY.get(sid) or {}
    meta = row.get("meta") or {}
    spawn = meta.get("spawn_config") if isinstance(
        meta.get("spawn_config"), dict) else {}
    model = str(meta.get("model") or spawn.get("model") or "").strip()
    return {"id": sid,
            "provider": row.get("provider") or (sid.split(":", 1)[0]
                                                if ":" in sid else ""),
            "purpose": agent_context_policy.redact_text(row.get("purpose") or ""),
            "parent": row.get("parent") or None,
            "class": row.get("class") or None,
            "model": model or None,
            "worktree": row.get("worktree") or None,
            "busy": await _registry_is_busy(sid),
            "last_active_ts": row.get("last_active_ts")}


def _agent_context_family(caller: str, target: str) -> bool:
    return agent_context_policy.is_family_edge(
        REGISTRY.get(caller), REGISTRY.get(target), caller, target)


# ── 蒸餾:本機 Ollama(理由與 harness/model.py 同,見下)────────────────
# bridge 現有「送文字給模型、拿文字回來」的路只有四條,能用的只有第一條:
#   1. 本機 Ollama(`_polish_transcript` 那條)—— 真無狀態、無金鑰、本機免費 ✅
#   2. `acp_full()` ACP persona pool —— **綁善彰真正的 Telegram canonical
#      session**:摘要提示詞會噴到他手機上,還會搶 `self._lock` 卡住真人回合 ❌
#   3. `run_hermes()` —— 同上,刻意打同一個 canonical session ❌
#   4. headless `claude -p` dispatch —— 會建 SUBSESSIONS/worktree/registry
#      配額,為了一段摘要生一個子 agent,層級完全不對 ❌
# (2/3 已逐條核對過 bridge 現碼:ACPSession 持有 persona 的 canonical
#  session,POOL 依 persona 復用同一條連線。)所以走第一條。
# feat/continual-harness 的 `harness.model.ollama_text()` 是同一段的抽象版;
# 那棵樹合併後,這裡應改成 import 它,不要留兩份 Ollama 呼叫。

def _agent_context_model_name() -> str:
    return (os.environ.get("AGENT_CONTEXT_MODEL", "").strip()
            or os.environ.get("HARNESS_MODEL", "").strip()
            or os.environ.get("MEETING_POLISH_MODEL", "").strip()
            or "mistral-small3.2:latest")


def _agent_context_model_timeout() -> float:
    try:
        return float(os.environ.get("AGENT_CONTEXT_MODEL_TIMEOUT", "") or 60.0)
    except ValueError:
        return 60.0


async def _agent_context_summarize(material: str) -> str:
    """一發式本機蒸餾。**失敗一律回空字串**,由呼叫端 fail-soft 退成抽取式
    摘要 —— 模型掛了不該讓「讀不到隊友在幹嘛」變成硬錯誤。"""
    prompt = agent_context_policy.SUMMARY_PROMPT + material
    num_ctx = min(40960, max(8192, len(prompt) * 2 + 2048))

    async def _run() -> str:
        import httpx
        base = (os.environ.get("OLLAMA_URL", "").strip()
                or "http://127.0.0.1:11434").rstrip("/")
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                base + "/api/chat",
                json={"model": _agent_context_model_name(), "stream": False,
                      "keep_alive": "15m",
                      "messages": [{"role": "user", "content": prompt}],
                      "options": {"temperature": 0.2, "num_ctx": num_ctx}})
            r.raise_for_status()
            return ((r.json().get("message") or {}).get("content") or "").strip()

    try:
        return await asyncio.wait_for(_run(),
                                      timeout=_agent_context_model_timeout())
    except asyncio.TimeoutError:
        _log_event("agent_context_summary_timeout", chars=len(prompt))
        return ""
    except Exception as e:  # noqa: BLE001
        _log_event("agent_context_summary_failed", error=type(e).__name__,
                   error_message=str(e)[:200])
        return ""


async def _agent_context_summary(target: str, meta: dict,
                                 store) -> tuple[str, int, bool]:
    """摘要 + 快取。回 (text, source_seq, cached)。

    快取鍵 = (session, 內容 seq):目標沒有新動靜就重複用同一份,重讀免費;
    一有新卡片(seq 前進)即失效。fail-soft 的抽取式退路**不進快取**,
    否則模型復活後還會拿到那份沒蒸餾過的。
    """
    src_seq = _agent_context_source_seq(store)
    ent = _AGENT_CONTEXT_SUMMARY_CACHE.get(target)
    if ent and ent.get("seq") == src_seq and ent.get("text"):
        return (ent["text"], src_seq, True)
    lines = _agent_context_lines(
        store, agent_context_policy.summary_material_cards())
    material = agent_context_policy.build_summary_material(meta, lines)
    raw = await _agent_context_summarize(material)
    # 模型輸出再遮一次:素材已遮過,但模型可能從別處(系統提示、幻覺)吐出
    # key 形狀的字串。遮罩是結構性的,不靠「上游應該已經乾淨了」。
    text = agent_context_policy.redact_text(raw or "").strip()
    if not text:
        return (agent_context_policy.fallback_summary(meta, lines),
                src_seq, False)
    _AGENT_CONTEXT_SUMMARY_CACHE[target] = {"seq": src_seq, "text": text,
                                            "ts": time.time()}
    cap = agent_context_policy.cache_max_entries()
    if len(_AGENT_CONTEXT_SUMMARY_CACHE) > cap:
        for k, _v in sorted(_AGENT_CONTEXT_SUMMARY_CACHE.items(),
                            key=lambda kv: kv[1].get("ts") or 0)[:len(
                                _AGENT_CONTEXT_SUMMARY_CACHE) - cap]:
            _AGENT_CONTEXT_SUMMARY_CACHE.pop(k, None)
    return (text, src_seq, False)


async def _agent_context_audit(caller: str, target: str, mode: str,
                               read_id: str, note: str = "",
                               create_store: bool = True) -> None:
    """audit 卡落**被讀的 session**:使用者在 Pocket 上看得到誰讀了自己。
    kind "text" + fallback_text(舊 app 照純文字渲染),origin=agent_context
    (回覆收割與內容擷取都會跳過它)。單邊失敗只留痕不擋事。"""
    txt = f"👁 {caller} 讀取了本 session 的上下文({mode})"
    if note:
        txt += f"|{note}"
    try:
        store = (await _v2_card_store(target)) if create_store \
            else _agent_context_warm_store(target)
        if store is None:
            return
        store.upsert_card(carddigest.make_card(
            f"card-agentctx-{read_id}", "", "assistant", "text",
            {"text": txt, "fallback_text": txt, "origin": "agent_context",
             "caller": caller, "target": target, "mode": mode,
             "read_id": read_id}))
    except Exception as _exc:  # noqa: BLE001
        _log_exc("_agent_context_audit", _exc, expected=True, session=target)


async def _agent_context_deny(caller: str, target: str, mode: str,
                              reason: str) -> None:
    """拒絕:落 log(誰想讀誰、被什麼擋下)、對已有 store 的目標落一張留痕卡
    (不為一張拒絕卡冷載入目標),丟 403。"""
    _log_event("agent_context_denied", caller=caller, target=target,
               mode=mode, reason=reason[:200])
    await _agent_context_audit(caller, target, mode,
                               "deny-" + uuid.uuid4().hex[:8],
                               note="(已被政策拒絕,未取得任何內容)",
                               create_store=False)
    raise http_err(403, "AGENT_CONTEXT_DENIED", reason)


def _agent_context_candidates() -> list:
    """search 全範圍的候選 session:registry 未歸檔戶口 + 常駐人格。
    只回 id;能不能讀、有沒有熱 store 由呼叫端再過濾。"""
    _registry_ensure_personas()
    return sorted({r["id"] for r in REGISTRY.list_rows()
                   if r.get("state") != "archived"})


@app.post("/app/v2/agent_context")
async def v2_agent_context(request: Request):
    """跨 session 上下文互讀。body:
    {caller: session_id, target: session_id(search 可省略 = 全可讀範圍),
     mode: summary|recent|search, query?(search 必填), limit?}。

    caller 自報身分(信任邊界 = bridge token,與 agent_call 同);audit 卡與
    log 都以此記名。回應固定含 target/purpose/provider/model/busy/
    last_active_ts/content/truncated/source_seq。
    """
    _check_auth(request)
    _agent_context_require_enabled()
    body = await _json_body(request)
    caller = _agent_call_normalize_sid(str(body.get("caller") or ""))
    target = _agent_call_normalize_sid(str(body.get("target") or ""))
    mode = str(body.get("mode") or "summary").strip() or "summary"
    query = str(body.get("query") or "").strip()
    if mode not in agent_context_policy.MODES:
        raise http_err(400, "AGENT_CONTEXT_BAD_MODE",
                       f"mode 必須是 {'|'.join(agent_context_policy.MODES)}")
    if not caller:
        raise http_err(400, "AGENT_CONTEXT_BAD_REQUEST", "caller 必填")
    if mode != "search" and not target:
        raise http_err(400, "AGENT_CONTEXT_BAD_REQUEST",
                       f"{mode} 模式的 target 必填")
    if mode == "search" and not query:
        raise http_err(400, "AGENT_CONTEXT_BAD_REQUEST", "search 模式 query 必填")
    if target and caller == target:
        raise http_err(400, "AGENT_CONTEXT_SELF",
                       "不能讀自己的上下文(自己的卡片流走 /app/v2/sessions)")
    try:
        _v2_card_source(caller)
    except HTTPException:
        raise http_err(400, "AGENT_CONTEXT_BAD_CALLER",
                       f"caller 不是已知 session:{caller}")
    _agent_context_rate_check(caller)
    policy = agent_context_policy.load_policy()
    read_id = "ctx-" + uuid.uuid4().hex[:12]
    try:
        limit = int(body.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0

    # ── search 全範圍:掃「這個 caller 讀得到」的熱 store ────────────────
    if mode == "search" and not target:
        return await _agent_context_search_all(caller, query, policy, limit,
                                               read_id)

    # ── 單一 target ────────────────────────────────────────────────────
    try:
        _v2_card_source(target)
    except HTTPException:
        raise http_err(404, "AGENT_CONTEXT_TARGET_NOT_FOUND",
                       f"target 不是既有 session:{target}")
    ok, basis = agent_context_policy.decide(
        policy, caller, target, mode,
        family=_agent_context_family(caller, target))
    if not ok:
        await _agent_context_deny(caller, target, mode, basis)
    store = await _v2_card_store(target)
    meta = await _agent_context_meta(target)
    hits: list = []
    cached = False
    if mode == "summary":
        content, src_seq, cached = await _agent_context_summary(
            target, meta, store)
    elif mode == "recent":
        n = limit or agent_context_policy.recent_limit_default()
        n = max(1, min(n, agent_context_policy.recent_limit_max()))
        src_seq = _agent_context_source_seq(store)
        lines = _agent_context_lines(store, n)
        content = "\n".join(lines) if lines else "(這個 session 的卡片流是空的)"
    else:                                    # search(單一 target 範圍)
        n = limit or agent_context_policy.search_max_hits()
        n = max(1, min(n, agent_context_policy.search_max_hits()))
        src_seq = _agent_context_source_seq(store)
        hits = _agent_context_search_store(store, target, query, n)
        content = _agent_context_hits_text(hits, query)
    content, truncated = agent_context_policy.clip(
        content, agent_context_policy.max_chars())
    _log_event("agent_context_read", read=read_id, caller=caller,
               target=target, mode=mode, basis=basis, cached=cached,
               chars=len(content), truncated=truncated, source_seq=src_seq)
    _registry_call_safe("touch", caller)
    await _agent_context_audit(caller, target, mode, read_id)
    return {"ok": True, "read_id": read_id, "mode": mode, "target": target,
            "purpose": meta["purpose"], "provider": meta["provider"],
            "model": meta["model"], "busy": meta["busy"],
            "last_active_ts": meta["last_active_ts"],
            "content": content, "truncated": truncated,
            "source_seq": src_seq, "cached": cached, "basis": basis,
            "hits": hits}


def _agent_context_hits_text(hits: list, query: str) -> str:
    if not hits:
        return f"(在可讀範圍內找不到「{query}」)"
    return "\n".join(
        agent_context_policy.card_line(
            f"{h['session']} {h.get('role') or ''}".strip(),
            h["snippet"], h.get("ts"))
        for h in hits)


async def _agent_context_search_all(caller: str, query: str, policy: dict,
                                    limit: int, read_id: str) -> dict:
    """跨 session 搜尋 —— **範圍即權限**:讀不到的 session 連命中都看不到。

    只掃熱 store(見 `_agent_context_warm_store`),命中的 session 各落一張
    audit 卡(沒命中的沒外流內容,不打擾)。
    """
    max_hits = agent_context_policy.search_max_hits()
    n = max(1, min(limit or max_hits, max_hits))
    scanned = 0
    hits: list = []
    scope: list = []
    for sid in _agent_context_candidates():
        if scanned >= agent_context_policy.search_max_sessions():
            break
        if sid == caller:
            continue
        ok, _reason = agent_context_policy.decide(
            policy, caller, sid, "search",
            family=_agent_context_family(caller, sid))
        if not ok:
            continue
        store = _agent_context_warm_store(sid)
        if store is None:
            continue
        scope.append(sid)
        scanned += 1
        hits.extend(_agent_context_search_store(store, sid, query,
                                                max(1, n - len(hits))))
        if len(hits) >= n:
            break
    hits.sort(key=lambda h: float(h.get("ts") or 0))
    hits = hits[:n]
    content, truncated = agent_context_policy.clip(
        _agent_context_hits_text(hits, query),
        agent_context_policy.max_chars())
    _log_event("agent_context_read", read=read_id, caller=caller, target="*",
               mode="search", scanned=scanned, hits=len(hits),
               chars=len(content), truncated=truncated)
    _registry_call_safe("touch", caller)
    for sid in dict.fromkeys(h["session"] for h in hits):
        await _agent_context_audit(caller, sid, "search", read_id,
                                   note=f"關鍵字「{query[:40]}」",
                                   create_store=False)
    return {"ok": True, "read_id": read_id, "mode": "search", "target": "*",
            "purpose": "", "provider": "", "model": None, "busy": False,
            "last_active_ts": None, "content": content,
            "truncated": truncated, "source_seq": 0, "cached": False,
            "basis": "scope", "hits": hits, "scope": scope}


@app.get("/app/v2/agent_context_targets")
async def v2_agent_context_targets(request: Request, caller: str = "",
                                   mode: str = ""):
    """這個 caller 讀得到誰、各自能讀到什麼程度(空 mode = 逐模式列出)。
    給 agent 自己盤點用,也給善彰驗證政策有沒有設對。"""
    _check_auth(request)
    _agent_context_require_enabled()
    caller = _agent_call_normalize_sid(caller)
    if not caller:
        raise http_err(400, "AGENT_CONTEXT_BAD_REQUEST", "caller 必填")
    if mode and mode not in agent_context_policy.MODES:
        raise http_err(400, "AGENT_CONTEXT_BAD_MODE",
                       f"mode 必須是 {'|'.join(agent_context_policy.MODES)}")
    policy = agent_context_policy.load_policy()
    modes = (mode,) if mode else agent_context_policy.MODES
    out = []
    for sid in _agent_context_candidates():
        if sid == caller:
            continue
        fam = _agent_context_family(caller, sid)
        allowed = [m for m in modes
                   if agent_context_policy.decide(policy, caller, sid, m,
                                                  family=fam)[0]]
        if not allowed:
            continue
        row = REGISTRY.get(sid) or {}
        out.append({"id": sid, "provider": row.get("provider"),
                    "purpose": agent_context_policy.redact_text(
                        row.get("purpose") or ""),
                    "modes": allowed, "family": fam})
    return {"caller": caller, "targets": out,
            "policy_path": agent_context_policy.policy_path(),
            "tiering": {"family": list(agent_context_policy.family_modes()),
                        "context_rule_default": list(
                            agent_context_policy.rule_default_modes()),
                        "agent_call_implies": list(
                            agent_context_policy.call_implies_modes())}}


@app.on_event("startup")
async def _start_agent_registry():
    _registry_ensure_personas()
    task = asyncio.create_task(_registry_reaper_loop())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    _log_event("registry_started", db=REGISTRY.db_path,
               reaper=_registry_reaper_enabled(),
               sweep_secs=_REGISTRY_SWEEP_SECS)


@app.on_event("startup")
async def _start_housekeeping():
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    task = asyncio.create_task(_housekeeping_loop())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@app.on_event("startup")
async def _start_openclaw_pump():
    # S4:openclaw 常駐事件泵。未配置 → 只記一次 log,泵便宜輪空
    # (30s 一次 configured() 檢查),配置後(env 或 PUT config)自動接上。
    if not OPENCLAW.configured():
        _log_event("openclaw_disabled", reason="no base_url configured")
    _oc_ensure_pump()


@app.on_event("startup")
async def _reseed_cc_resume_pins():
    # 重啟盲窗修復:把 hook 落地的 resume-pin 重載回記憶體,避免重啟後
    # cmdline 解到凍結舊 sid(見 _cc_reseed_pins_from_files 註解)。
    n = _cc_reseed_pins_from_files()
    _log_event("cc_resume_pins_reseeded", count=n)
    # 同源盲窗:`_CC_APPROVAL_ACTIVE` 也是行程內狀態,重啟即清空 → watcher
    # 首巡前的空窗會把 App 手上舊 aid 的 CC 審核決議打成 409。先從 DB 灌回。
    _cc_reseed_approvals_from_db()


@app.on_event("startup")
async def _start_cc_approval_watcher():
    # 批次 3 斷點③:CC waiting_approval → approval feed + 推播(常駐)
    task = asyncio.create_task(_cc_approval_watcher())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    # M1:CC 委派完成偵測(15s;codex 走 turn/completed 事件不用巡)
    dtask = asyncio.create_task(_delegation_cc_watcher())
    _BG_TASKS.add(dtask)
    dtask.add_done_callback(_BG_TASKS.discard)
    # 2b:人格 choices 卡 → 審核中心(30s 巡 report_events)
    htask = asyncio.create_task(_hp_choices_watcher())
    _BG_TASKS.add(htask)
    htask.add_done_callback(_BG_TASKS.discard)


@app.on_event("startup")
async def _start_state_db_watcher():
    # #tg-instant-sync:TG/cron 寫進各 persona home 的 state.db,唯讀 stat
    # 輪詢偵測寫入 → 立刻喚醒 _hp_canon_follower(見該函式與
    # _state_db_watcher_loop 上方註解)。只讀檔案 mtime/size,不碰
    # hermes_cli 內核、不寫 state.db,常駐到 process 生命週期結束。
    stask = asyncio.create_task(_state_db_watcher_loop())
    _BG_TASKS.add(stask)
    stask.add_done_callback(_BG_TASKS.discard)
